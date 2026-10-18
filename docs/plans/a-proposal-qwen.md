# Task A proposal (qwen3.8-max) — HMAC-derived, re-viewable QR badges (backend)

Implements `docs/plans/COORDINATOR2.md` decision 1 + Task A exactly as
adjudicated: **derivation** (not encrypt-at-rest — my review's alternative
was overruled; the key-only-leak caveat it identified is carried into docs
and risks below, per "trade-off accepted by owner's request … documented,
not hidden"), flush-then-derive issuance, legacy **auto-migration on first
view**, structured 409s for key problems, backup + healthz included.
Backend only; the SPA consumes this in Task D.

**Verified this pass:** `app/qrbadge.py` today = `new_badge_payload()`,
`parse_badge_payload()`, `badge_svg()`, and Task 2's `new_badge_for(user)`
(random payload → hash, "nothing is committed here", called BEFORE
`session.add`); `register()`/`create_member()` wrap `session.commit()` in
`try/except IntegrityError → rollback → 409`; `create_my_badge` /
`create_member_badge` use `new_badge_for` on an already-persistent user;
`qr_login`/`qr_identify` verify via `token_hash(badge) == users.qr_badge_hash`
(the authority — untouched by this task); `COLUMN_MIGRATIONS` has 4 entries
and the exact-set test pins them; `healthz` reports `insecure_dev_admin` /
`pbkdf2_iterations` (the loud-visibility precedent); `backup_db.sh`
checkpoints the WAL then copies `hcrm.db{,-wal,-shm}` into `$DEST` (no key
material today); `scripts/scanner_bridge.py` keeps its own local
`BADGE_PREFIX` and never imports `qrbadge` (derivation adds no weight to the
stdlib-only bridge contract); `data/` artifacts are git-ignore-checked by
`test_repo_hygiene` / the no-DB-in-history test.

---

## 1. Master secret — bootstrap, caching, degradation (`app/security.py`)

The key is a credential-lifetime singleton: **lazy-load once, cache in a
module global, expose through one accessor** so consumers call
`security.badge_secret()` *via module attribute at call time* (the Task-4
`LOGOFF_GRACE_SECONDS` pattern — one monkeypatch point for tests, no
from-import binding drift).

```python
BADGE_SECRET_ENV  = "HCRM_BADGE_SECRET"   # 64 hex chars = 32 bytes
BADGE_KEY_FILENAME = "badge.key"          # inside the data dir

def badge_secret() -> bytes | None:
    """The badge-derivation master secret, loaded once per process.

    Priority: HCRM_BADGE_SECRET env (validated: exactly 64 hex chars →
    bytes.fromhex; an INVALID value is treated as absent with a logged
    warning — a typo must not silently fork the derivation space) →
    <data dir>/badge.key (64-hex text; created on first use with
    O_CREAT|O_EXCL, mode 0600, content secrets.token_hex(32) — the
    exclusive create is the multi-worker race guard: losers read the
    winner's file) → None (data dir unwritable, file corrupt/unreadable).
    None never breaks authentication: qr_badge_hash is the login authority
    and needs no key. Cached; callers must use security.badge_secret()
    (module attribute) so tests can patch one name.
    """

def badge_secret_source() -> str | None:   # "env" | "file" | None — healthz/startup
```

- **Data-dir path**: computed in `security.py` from `HCRM_DATA_DIR` with the
  same default as `app/database.py` (`<repo>/data`), carrying an explicit
  *"keep in sync with app.database.DATA_DIR"* comment — deliberately NOT
  importing `app.database` (that would pull `app.embeddings`/fastembed into
  every security consumer for one path constant).
- **Startup**: `_startup()` calls `badge_secret()` once and announces the
  outcome (dev-admin announcement pattern): `"badge secret: env" / "badge
  secret: generated <path> (0600)" / "badge secret: UNAVAILABLE — badge
  re-viewing disabled, logins unaffected"`.
- **healthz** gains `"badge_derivation": bool` + `"badge_secret_source":
  "env"|"file"|null` (reads the cache; never touches disk per call — the
  healthz no-DB-pressure doctrine).
- **Rotation runbook** (docs, Task F): replace env / delete keyfile →
  restart → every derived badge 409s `key-mismatch` on re-view while
  **logins keep working** (hashes); accounts recover by rotating. That is
  the break-glass path; also the recovery for a suspected key leak (plus:
  consider fronting `/api/auth/qr-login` with proxy rate limiting — §7 R1).

## 2. Derivation (`app/qrbadge.py`)

```python
BADGE_DERIVATION_CONTEXT = "badge"        # domain-separation prefix, FROZEN

def derive_badge_payload(user_id: int, generation: int, secret: bytes) -> str:
    """The coordinator's formula, verbatim and frozen forever:
    "HCRM1:" + urlsafe_b64(HMAC_SHA256(secret, "badge:{user_id}:{generation}").digest()[:32])
    → 43 chars of [A-Za-z0-9_-] — byte-identical in shape to
    secrets.token_urlsafe(32), so parse_badge_payload, the bridge, the SPA
    gates and every existing test accept derived payloads unchanged.
    """
    digest = hmac.new(secret,
                      f"{BADGE_DERIVATION_CONTEXT}:{user_id}:{generation}".encode(),
                      hashlib.sha256).digest()
    return BADGE_PREFIX + base64.urlsafe_b64encode(digest[:32]).decode().rstrip("=")

def rederive_and_check(user) -> str | None:
    """Recompute the payload for ``user``'s stored generation and VERIFY
    token_hash(derived) == user.qr_badge_hash. Returns the payload, or None
    on mismatch (key swapped/rotated, row hand-edited) — the re-view
    endpoints answer None with a structured 409, NEVER a badge that would
    not log in. This check is the derivation scheme's self-test."""
```

**The formula is a forever-contract**: any change silently orphans every
stored hash. Pinned by a **known-answer test** (fixed 32-byte secret, user 1,
generation 1 → the exact payload string, computed once at implementation and
frozen in the test) plus a docstring warning that a formula change is a
mass-reissue event (runbook in Task F).

## 3. Issuance — `new_badge_for` becomes derivation-aware; flush-then-derive

```python
def new_badge_for(user) -> str:
    """Issue the next badge for ``user``: generation = (current or 0) + 1,
    payload derived from the master secret, hash + generation stored.
    Nothing is committed here — the caller's transaction decides.

    REQUIRES a flushed/committed ``user.id`` when a secret is available
    (derivation input); raises ValueError on ``user.id is None`` rather
    than deriving from a placeholder — loud, per house culture. Falls back
    to a RANDOM payload with generation left at 0 when the secret is
    unavailable (badge issuance must never block account creation; the
    badge simply is not re-viewable until a key exists and the account is
    rotated/auto-migrated)."""
    secret = security.badge_secret()          # module attribute, patchable
    if secret is None:
        payload = new_badge_payload()         # legacy-shape random badge
    else:
        if user.id is None:
            raise ValueError("flush the user (assign its id) before issuing a derived badge")
        generation = (user.qr_badge_generation or 0) + 1
        payload = derive_badge_payload(user.id, generation, secret)
        user.qr_badge_generation = generation
    user.qr_badge_hash = token_hash(payload)
    return payload
```

**Creation flow restructure** (`register`, `create_member`) — kimi's
single-transaction flush, chosen over the two alternatives:

```python
    user = User(...)                       # unchanged construction
    session.add(user)
    try:
        session.flush()                    # the INSERT executes HERE: id is
    except IntegrityError:                 # assigned; the email race raises
        session.rollback()                 # at flush (same 409 contract —
        raise HTTPException(409, "Email already registered")
    badge_payload = new_badge_for(user)    # id available, still uncommitted
    session.commit()
    session.refresh(user)
```

Why this ordering (the prompt's three-way question, answered):
- **vs my two-phase (commit user → issue → commit):** a second-commit
  failure would leave a *badgeless* account — and worse, `RegisterOut`
  **requires** `qr_badge`, so the endpoint would 500 *after* creating the
  account. Unacceptable response contract.
- **vs pre-assigning an id (max+1):** racy by construction; SQLite assigns
  rowids at INSERT.
- **flush-then-derive** keeps ONE commit: the generate-before-commit
  invariant survives intact (a raced insert rolls back row, generation and
  never-shown payload together — the existing IntegrityError tests pass
  unchanged, only the raising statement moves from `commit()` to `flush()`);
  no badgeless window exists.
- **Rotate endpoints** (`create_my_badge`, `create_member_badge`): the user
  is already persistent — `new_badge_for` + commit, flow otherwise
  byte-identical. Rotation increments the generation, so the old derived
  payload stops matching the hash the instant the new one is stored
  (existing rotate-kills-old tests pass unchanged).

**Model + migration:**

```python
# app/models.py — User gains:
    # Badge derivation generation (Task A): 0 = a random (legacy/pre-key)
    # badge that cannot be re-derived; >= 1 = derived from the master
    # secret and re-viewable via GET /api/auth/qr-badge. Monotonic per
    # account: rotation increments, generations are never reused.
    qr_badge_generation: int = Field(default=0)

# app/main.py — COLUMN_MIGRATIONS append (exact-set test += this entry):
    ("users", "qr_badge_generation", "INTEGER NOT NULL DEFAULT 0", ()),
```

Existing rows land at 0 = legacy ⇒ every Task-2 badge keeps working by hash
and upgrades on first re-view (§4). No index (read off the PK-fetched row,
never queried by — the `logoff_deadline` no-index precedent).

## 4. Re-view endpoints + legacy auto-migration

Shared logic in one helper (used by both routers; cross-router import
follows the `scan_requests → cart._upsert_line` precedent — helper lives in
`app/routers/auth.py`, members.py imports it):

```python
def view_badge(user: User, session: Session) -> QrBadgeViewOut:
    """The re-view core. Raises the structured HTTP errors:
    404 {reason: "no-badge"}            — hash is None (revoked/never issued)
    409 {reason: "no-key", …}           — secret unavailable; login unaffected
    409 {reason: "key-mismatch", …}     — derived hash != stored hash (key
                                          rotated/swapped, row hand-edited):
                                          rotate to re-issue; NEVER serves a
                                          badge that would not log in.
    Legacy (generation 0) AUTO-MIGRATES (coordinator decision 1): the random
    payload is unrecoverable by construction, so the only re-viewable badge
    is a fresh derived one — issue it (generation 1), commit, and report
    migrated_from_legacy=true so the UI can say plainly that the previously
    printed card just died."""
```

- `GET /api/auth/qr-badge` (self; `get_current_user`, any role, gate
  applies — not exempt) → `QrBadgeViewOut {payload, svg, generation,
  migrated_from_legacy}` (extends `QrBadgeOut`; rotate/revoke keep
  returning plain `QrBadgeOut`/204 — untouched contracts).
- `GET /api/members/{user_id}/qr-badge` (`require_staff` +
  `_get_manageable_member`: staff never view admin badges, admins view
  anyone's; 404 unknown member) — the counter reprint path, and the
  owner's "admin can regenerate" stays the existing POST beside it.
- **Auto-migrate decision + justification** (the prompt's "decide and
  justify"): follow the coordinator — auto-migrate on first view, NOT 409.
  Rationale: a legacy payload is *unrecoverable*, so "view" cannot ever
  show the old badge; forcing a separate POST makes the member click
  through a dead end ("we can't show it" → "regenerate" → "here it is")
  that auto-migration collapses into one honest response with
  `migrated_from_legacy: true`. The two real objections are answered by
  safeguards, not by refusing: (1) *GET-with-side-effect* — accepted,
  coordinator-adjudicated, documented in the endpoint docstring; the
  mutation is **idempotent and race-safe by determinism** (two concurrent
  first-views both derive generation 1 → the SAME payload/hash; unlike a
  random rotate, double-fire cannot double-rotate), and (2) *killing a
  printed card* — Task D gates the first legacy view behind a confirm
  ("viewing replaces your previous badge — printed cards stop working")
  and the response flag lets the UI state what happened. Staff-side GET
  gets the same confirm in the Members tab.
- Revoked/absent badge → 404 `no-badge` (the UI shows the existing
  Generate button). `qr-login`, `qr-identify`, `_badge_from_scanned`,
  bridge, SPA gates: **zero changes** (hash authority, payload shape
  identical).

## 5. Error matrix

| Case | Self GET | Staff GET |
|---|---|---|
| No valid token / flagged account | 401 / 403 (gate, not exempt) | 401 / 403 |
| Member caller on staff endpoint | — | 403 (`require_staff`) |
| Staff → admin target | — | 403 (`_get_manageable_member`) |
| Unknown member id | — | 404 |
| Badge revoked / never issued | 404 `{reason:"no-badge"}` | same |
| Secret unavailable | 409 `{reason:"no-key"}` | same |
| Generation ≥1, derived hash ≠ stored | 409 `{reason:"key-mismatch"}` | same |
| Generation 0 (legacy) | auto-migrate → 200 `migrated_from_legacy:true` | same |
| Happy path | 200 `{payload, svg, generation, migrated_from_legacy:false}` | same |

Structured dict details follow the Task-3 envelope convention
(`reason` discriminator; clients branch on `detail.reason`).

## 6. Tests

**New `tests/test_badge_derivation.py`:**
1. **KAT**: frozen secret/user/generation → exact payload (formula
   forever-contract); shape: `parse_badge_payload` accepts, 43 chars,
   prefix.
2. Determinism (same inputs ⇒ same payload), distinctness (different user,
   different generation ⇒ different), generation monotonic per rotation.
3. `new_badge_for` raises `ValueError` on unflushed user (id None) when a
   secret exists — the loud-fail pin.
4. Register/create_member: generation 1, payload == `derive_badge_payload`,
   stored hash == `token_hash(payload)`, badge qr-logins (prefixed + raw
   forms), `qr-identify` same_user true.
5. **Re-view is the feature**: GET twice → byte-identical payload; svg
   well-formed; GET after rotate → generation 2 payload, old payload 401s,
   new logs in.
6. **Wrong key never authenticates**: (a) monkeypatched secret → GET 409
   `key-mismatch`, stored hash/gen unchanged (no side effect on failure);
   (b) a payload derived with the wrong secret → `qr-login` 401 (hash is
   the authority); (c) subprocess with a different `HCRM_BADGE_SECRET`
   (`test_dev_admin.py` pattern) → its derived badge 401s against the
   original DB.
7. **Legacy auto-migration**: hand-built gen-0 random badge (direct DB) →
   logs in; first GET → 200 `migrated_from_legacy:true`, generation 1,
   stored hash now the derived one, **old random payload 401s**; second
   GET → same payload, `migrated_from_legacy:false` (idempotent); the
   migration commit survives a re-login (durability).
8. No-badge (revoked) → 404 `no-badge`.
9. **Graceful no-key**: `badge_secret()` patched to None → register still
   201 (random badge, generation 0, RegisterOut intact — creation never
   blocks on the key), login works, GET → 409 `no-key`, healthz
   `badge_derivation:false`; key restored → GET auto-migrates.
10. Key bootstrap: auto-generated file is 0600, 64-hex, stable across
    calls; second concurrent-style call reads the same content (O_EXCL
    loser path); corrupt file → None + login unaffected; invalid env hex →
    treated as absent (file fallback).
11. **Email change never touches the badge** — mechanism-level pin here:
    derivation inputs are exactly (secret, id, generation) — mutating
    email/name/role on the ORM object changes nothing derivable; the
    integration pin (change-email → same badge) lands in Task B and is
    cross-referenced in both test modules.
12. Staff GET: member→403, staff→admin-target→403, admin→200, unknown→404,
    legacy auto-migrate through the staff path.
13. Gates: flagged account → 403 on both GETs (not added to
    `MUST_CHANGE_EXEMPT_PATHS`).
14. Flush-race: duplicate register → 409 at flush, no user row, no badge
    residue (existing Task-2 conflict test stays green unchanged — pin the
    *moved* raise point explicitly).

**Extend:** `tests/test_qr.py` (creation-badge tests keep passing — they
now exercise derived badges; add one assertion that the register response
payload equals the re-view payload); `tests/test_migrations.py`
(exact-set += `("users","qr_badge_generation")`; legacy-DB landing: column
arrives DEFAULT 0 **and an existing stored hash still logs in after the
migration** — the "upgrade changes nothing for live badges" pin);
`tests/test_backup_and_run.py` (keyfile copied beside the DB + the
credential-bearing warning line; absent keyfile → backup still succeeds).

**Suite hygiene:** full `uv run pytest` green; `scanner_bridge` untouched
(its local `BADGE_PREFIX` and the pinned record contract see only
identically-shaped payloads); `ui_check` untouched.

## 7. Backup / healthz / hygiene / docs-truth (rides with this PR)

- `scripts/backup_db.sh`: after the DB-file copies, `[ -f "$DATA_DIR/badge.key" ]`
  → `cp -p` into `$DEST` + print "warning: the backup includes badge.key —
  it is credential-bearing (DB + key ⇒ derivable badges)". Absent key →
  unchanged behaviour. (`DATA_DIR` = `dirname "$DB"`.)
- `.gitignore`: verify `data/` coverage reaches `badge.key`; add the
  explicit path if the hygiene test wants it (`test_repo_hygiene` /
  no-DB-in-history stay green).
- healthz + startup announcement: §1.
- **Docs-truth minimum in THIS PR** (the full §9 rewrite is Task F, but A
  must not leave shipped docs lying): `docs/opticon-m10.md` §9.1/§9.4 gain
  the derivation paragraph — hash stays the login authority; payloads are
  derived from (server key, user id, generation) and therefore re-viewable;
  **the key joins the DB as protected material — DB+key ⇒ printable
  badges** (was: DB-leak-alone safe); key-only leak caveat: the derivation
  input space is (id × generation), so `qr-login`'s documented
  no-rate-limit premise now rests on key secrecy — suspect key ⇒ rotate
  (runbook) and consider proxy rate limiting. `app/qrbadge.py` module
  docstring updated to match (it currently promises "only its SHA-256 hash
  is stored server-side" — true, but now incomplete).

## 8. Risks & open questions

1. **R1 — key-only-leak collapses the payload space** (my plan-2 review,
   accepted by the coordinator with the owner): with the secret, badges are
   computable for the low-entropy (user_id × generation) space and
   `qr-login` is deliberately rate-limit-free. Mitigations: key = DB-tier
   protected material (0600, backup warning, healthz visibility), the
   §9.4 caveat, and the rotation runbook as break-glass. If the owner ever
   wants the old guarantee back, the flip is encrypt-at-rest (my review §1)
   — schema-compatible (one column either way).
2. **R2 — GET-with-side-effect** (legacy auto-migration): HTTP-impure,
   coordinator-adjudicated; idempotent + race-safe by determinism, UI
   confirm in Task D, `migrated_from_legacy` flag in every response.
   Documented in the endpoint docstring so no reviewer "fixes" it silently.
3. **R3 — frozen formula**: the KAT makes drift impossible to merge
   silently; a deliberate formula change is a mass-reissue (runbook, F).
4. **R4 — no-key degradation** is total for re-view but zero for auth:
   creation falls back to random gen-0 badges (feature-degraded, never
   blocked); pinned by test 9.
5. **R5 — secret caching** is per-process: env changes need a restart
   (house pattern for every HCRM_* knob); keyfile rotation likewise —
   documented in the runbook.
6. **Open question**: expose `generation` in `UserOut`/`has_qr_badge`-style
   flags so the SPA can pre-render "legacy — viewing will replace your
   card" *before* the confirm? Not needed (the GET response carries
   `migrated_from_legacy` and the confirm can be unconditional for
   first-time viewers); flagged for Task D taste.
