# Task A proposal (glm) — HMAC-derived, re-viewable QR badges

Coordinator2 decision 1 + Task A. Backend only. The payload becomes a
deterministic function of a server secret — `HCRM1:` + 43 urlsafe chars of
`HMAC-SHA256(badge_secret, "badge:{user_id}:{generation}")[:32]` — so it can
be re-computed on demand forever, while **`qr_badge_hash` remains the login
authority untouched** (no crypto on the hot path; every existing
qr-login/qr-identify test keeps passing byte-for-byte).

Grounding verified: `app/qrbadge.py` (`new_badge_payload`, `parse_badge_payload`
(43-char strict), `badge_svg`, `new_badge_for` — the Task-2 helper whose
contract is "call BEFORE add"), `register()`/`create_member()` call it
pre-add, both rotate endpoints call it on existing users (id already
assigned there), `qr-login`/`qr-identify` verify via
`token_hash(badge) == user.qr_badge_hash`, `COLUMN_MIGRATIONS` is
append-only with the exact-set pin, `data/` is git-ignored,
`scripts/backup_db.sh` copies the DB only, `/api/healthz` reports
liveness facts.

---

## 1. Key bootstrap + caching (`app/qrbadge.py`)

```python
import hashlib, hmac, base64, os, secrets

_BADGE_SECRET: bytes | None = None   # module cache: loaded once per process
_BADGE_KEYFILE = "badge.key"         # inside DATA_DIR

def load_badge_secret(env: str | None = None, data_dir: str | None = None) -> bytes:
    """Load (or create) the 32-byte badge-derivation master secret.

    Precedence: the HCRM_BADGE_SECRET env var (64 hex chars) — explicit
    deployments; else <data_dir>/badge.key, auto-generated on first use
    (32 random bytes, written as hex with os.open(..., 0o600) — created
    with the mode, not chmod'd after). Announced once at startup.

    Raises BadgeSecretError on a *set but invalid* env value (wrong length
    or non-hex) — fail-fast on misconfiguration, never silently derive
    with a truncated/different key than the operator intended.
    """

def badge_secret() -> bytes:
    """The process-wide secret (loads once, caches in _BADGE_SECRET)."""

class BadgeSecretError(RuntimeError): ...
```

- **Startup wiring** (`app/main.py::_startup`): call `badge_secret()` once —
  this both validates the env (a bad `HCRM_BADGE_SECRET` aborts boot with a
  clear message — the `HCRM_EMBED_STRICT` fail-fast pattern) and triggers
  the keyfile auto-generation + the one-line announcement ("badge
  derivation key: env | auto-generated <path> — back it up with the
  database"). `/api/healthz` gains `badge_derivation: true|false`
  (loud-visibility doctrine; false only if the file couldn't be created).
- **Testability**: `load_badge_secret(env=…, data_dir=…)` is the testable
  core; tests monkeypatch `app.qrbadge._BADGE_SECRET` (or call the loader
  directly with a tmp dir) and one subprocess case pins the
  wrong-secret-doesn't-verify property end to end.
- **Graceful issuance fallback**: if the secret is genuinely unavailable at
  issuance time (env unset + keyfile uncreatable — in practice impossible
  when the DB is writable, since the keyfile lives beside it), `new_badge_for`
  falls back to the **legacy random payload** (generation stays NULL) with a
  loud log: the account works, the badge logs in, it is simply not
  re-viewable until rotated. Registration must never brick on key I/O.

## 2. Derivation

```python
def derive_badge_payload(user_id: int, generation: int,
                         secret: bytes | None = None) -> str:
    """payload = "HCRM1:" + urlsafe_b64(HMAC(secret, f"badge:{user_id}:{generation}")[:32])

    Byte-identical in shape to the legacy random payload (43 urlsafe
    chars), so parse_badge_payload, the bridge's prefix check and the
    SPA's shape gates accept derived badges unchanged. Keyed on user_id
    (never email/name) — identity edits can never change a badge.
    """
```

(`base64.urlsafe_b64encode(digest[:32]).rstrip(b"=")` → exactly 43 chars —
the shape `parse_badge_payload` already enforces.)

## 3. Model + migration

```python
# app/models.py — User gains:
    # Badge provenance: NULL = no derived badge (a legacy Task-2 random
    # badge, or none at all — distinguished by qr_badge_hash), >= 1 =
    # derived at that generation (re-viewable: recompute from the secret).
    qr_badge_generation: int | None = Field(default=None)
```

`app/main.py` — one append-only entry:
`("users", "qr_badge_generation", "INTEGER", ())`. Existing rows land NULL
(= legacy) — the upgrade changes nothing about any existing badge.
`tests/test_migrations.py`: exact-set += the entry (conscious edit);
legacy-DB column-lands-NULL pin.

## 4. Issuance — `new_badge_for` rewritten, flush-then-derive

```python
def new_badge_for(user) -> str:
    """Derive the user's NEXT badge (generation++) and store only its hash.

    Contract change from Task 2: derivation needs user.id, so for NEW
    users the caller must add + session.flush() FIRST (kimi's
    single-transaction shape — flush assigns the id without committing, so
    the email-race IntegrityError now fires at flush and the existing
    rollback->409 still discards row, hash and generation together: no
    badgeless-account window, no two-phase commit). Rotate endpoints call
    this on existing users (id already assigned) unchanged.
    """
    generation = (user.qr_badge_generation or 0) + 1
    secret = _available_secret()
    if secret is None:                       # graceful fallback (§1)
        payload = new_badge_payload()        # legacy random shape
    else:
        payload = derive_badge_payload(user.id, generation, secret)
    user.qr_badge_generation = None if secret is None else generation
    user.qr_badge_hash = token_hash(payload)
    return payload
```

Call sites — `register()` and `create_member()` become:

```python
    session.add(user)
    try:
        session.flush()                      # assigns user.id, NO commit
        badge_payload = new_badge_for(user)  # derive with the real id
        session.commit()
    except IntegrityError:
        session.rollback()                   # race now fires at flush —
        raise HTTPException(409, …)          # same handling as today
```

Rotate endpoints (`create_my_badge`, `create_member_badge`) need **no**
ordering change (existing users) — they just get derived payloads now.
`assert user.id is not None` inside `new_badge_for` with a loud message
guards the new contract.

## 5. Re-view — `GET /api/auth/qr-badge` (repeatable) + staff variant

One shared helper (session-writes for auto-migrate, so it lives beside the
models in `app/qrbadge.py`, keeping the routers thin):

```python
def view_badge(session, user) -> tuple[str, bool]:
    """Re-view a user's badge: (payload, migrated).

    - no badge (hash NULL)                    -> raises BadgeViewError("none")
    - legacy random badge (generation NULL)   -> AUTO-MIGRATE: new_badge_for
      (generation 1, derived), commit, return (payload, migrated=True) —
      the coordinator's decision; the response flag lets the SPA say
      plainly "your previous card stopped working — print the new one"
    - derived: payload = derive(id, generation); VERIFY
      token_hash(payload) == user.qr_badge_hash BEFORE returning (a
      rotated/corrupt key can never render a WRONG badge — it fails into
      the key-mismatch branch); -> (payload, False)
    - derived but hash mismatch (rotated key) -> BadgeViewError("key-mismatch")
    - secret unavailable                      -> BadgeViewError("no-key")
    """
```

Endpoints (`app/routers/auth.py`, `app/routers/members.py`):

- `GET /api/auth/qr-badge` (any authenticated user; **not** added to
  `MUST_CHANGE_EXEMPT_PATHS` — flagged accounts change passwords first,
  same as the rotate endpoints) → 200 `QrBadgeViewOut` or:
  - 404 `{status: "none"}` — no badge on the account;
  - 409 `{status: "key-mismatch", message: "The badge key has changed —
    regenerate the badge to re-enable viewing"}`;
  - 409 `{status: "no-key", …}`.
  **Login is unaffected in every 409 case** — the hash never needed the key.
- `GET /api/members/{user_id}/qr-badge` (staff; the
  `_get_manageable_member` privilege rule — staff never view an admin's)
  — same helper, same outcomes; the owner's "admin can regenerate" stays
  the existing POST (now derived), this adds the view the Members tab
  wants (Task D).

`QrBadgeViewOut(QrBadgeOut)` — `{payload, svg, migrated: bool = False}`
(reuses the established one-shot schema; the creation/rotate responses stay
`QrBadgeOut` unchanged).

**On legacy auto-migration** (coordinator-decided; recording my review's
caveat): auto-migrate makes re-view work for the *entire* installed base on
day one (every existing account holds a Task-2 random badge); the cost is
one dead printed card per legacy viewer, one time — flagged by
`migrated: true` in the response, surfaced by the Task-D UI, and the staff
regenerate path remains the explicit alternative. The alternative
(explicit rotate-to-enable, no silent invalidation) is rejected *for this
codebase* only because there is no deployed population of printed legacy
cards to protect beyond the dev instance — if real cards exist in the
field, flip the helper's legacy branch to `BadgeViewError("legacy")` and
let the UI offer the rotate; the change is two lines.

## 6. Error-case matrix (each pinned by a test)

| Case | GET behaviour | Login (`qr-login`) |
|---|---|---|
| No badge | 404 `none` | n/a |
| Legacy badge | 200 `{payload, svg, migrated: true}` (old payload 401s from then on) | old payload works *until* the view |
| Derived, key intact | 200, hash-verified | works |
| Key rotated since issuance | 409 `key-mismatch` | **still works** (hash authority — the load-bearing pin) |
| Key unavailable | 409 `no-key` | still works |
| Invalid `HCRM_BADGE_SECRET` | boot fails loudly at startup | (never reached) |

## 7. Backup + healthz

- `scripts/backup_db.sh`: copy `badge.key` alongside the DB (same
  checkpoint-then-copy flow) and print a warning that backups are now
  **credential-bearing** (DB + key together derive every badge). Losing the
  key from a backup = logins fine, re-view broken until rotation — the
  documented runbook (mass-rotation loop via the staff endpoint) goes in
  Task F's docs.
- `/api/healthz`: `badge_derivation: true|false` (from the loaded-key
  state; the smoke test asserts it).

## 8. Tests

**`tests/test_badge_derivation.py` (new):**
1. Derivation shape: 43 urlsafe chars, `HCRM1:` prefix, accepted by
   `parse_badge_payload`; deterministic; generation+1 → different payload;
   different user_id → different payload.
2. Key bootstrap: env precedence (valid hex); keyfile auto-generation
   (0600 via `stat`, stable across two loads, hex round-trip); **invalid
   env → `BadgeSecretError`** (fail-fast pin); `data_dir` injection works.
3. **Re-view is the feature**: register → `GET /api/auth/qr-badge` returns
   *the same payload as the register response*; a second GET → identical
   (repeatable); after rotate → the new payload, old 401s.
4. **Wrong key never authenticates**: a payload derived with secret B does
   not match any stored hash → `qr-login` 401 (the secret is the root);
   plus a subprocess case (the `test_dev_admin` pattern): process 1
   derives, process 2 with a different `HCRM_BADGE_SECRET` rejects it.
5. **Key-mismatch degrades to 409, login survives**: issue with key A
   (monkeypatched secret), reload with key B → GET 409 `key-mismatch`,
   while `qr-login` with the A-derived payload **still 200s** (the hash
   never needed the key — the single most important pin).
6. **Legacy auto-migration**: hand-built legacy row (random payload +
   hash + generation NULL) → login works; GET → 200 `migrated: true`,
   generation 1, new payload logs in, **old payload 401s**.
7. No-badge account → 404 `none`; flagged account → 403 (gate applies).
8. Staff GET: views a member's badge (incl. auto-migrate); staff → admin
   target → 403 (manage rule); member self-path mirrors.
9. **Issuance ordering**: register's email race now raises at `flush` —
   409 still returned, and a forced failure leaves **no row** (hash +
   generation ride the same transaction); rotate bumps generation
   monotonically across repeated rotations.
10. **Email change never touches the badge** (the derivation-keyed-on-id
    consequence): set a user's email directly in the DB → GET returns the
    identical payload.
11. `healthz` `badge_derivation` true in the suite; the issuance fallback
    (secret unavailable → random payload, generation NULL, re-view 409)
    via a monkeypatched loader.

**`tests/test_qr.py`**: the existing lifecycle/leak tests stay green
(external contracts unchanged); the creation-flow tests now also assert
`qr_badge_generation == 1` after register/create.

**`tests/test_migrations.py`**: exact-set += `("users",
"qr_badge_generation")` (conscious edit); legacy DB gains the column NULL.

**`scripts/smoke_test.py`** (small, here rather than F — it guards the
feature): re-view GET round-trip + `healthz.badge_derivation` (+2 checks).

## 9. Files touched

| File | Change |
|---|---|
| `app/qrbadge.py` | key loader/cache, `derive_badge_payload`, `new_badge_for` rewrite, `view_badge` helper, `BadgeSecretError`/`BadgeViewError` |
| `app/models.py` | `User.qr_badge_generation` |
| `app/main.py` | migration append; startup key validation + announcement; healthz flag |
| `app/routers/auth.py` | register flush-then-derive; `GET /api/auth/qr-badge` |
| `app/routers/members.py` | create_member flush-then-derive; `GET /api/members/{id}/qr-badge` |
| `app/schemas.py` | `QrBadgeViewOut` |
| `scripts/backup_db.sh` | keyfile copy + credential-bearing warning |
| `scripts/smoke_test.py` | +2 kiosk-loop checks |
| tests | `test_badge_derivation.py` (new), `test_qr.py`, `test_migrations.py` |

*(Untouched: `qr-login`, `qr-identify`, `_badge_from_scanned`, the SPA, the
bridge — the hash path is the authority and derivation is display-side
only.)*

## 10. Risks / notes for review

1. **The doctrine change** (DB+key ⇒ printable badges) is the owner's
   accepted trade — the README security paragraph + §9 rewrite land in
   Task F; the module docstrings state it here.
2. **Flush-then-derive** moves the register race's `IntegrityError` from
   commit to flush — same handler, pinned by test 9; reviewers should
   confirm no other caller of `new_badge_for` exists (verified: exactly
   the four sites).
3. **Auto-migrate is a rotation in disguise** — mitigated by the
   `migrated` flag and the two-line escape hatch to `409 legacy` if real
   printed cards are in the field (§5).
4. `badge_secret()` in `qrbadge.py` adds an `app.database` (lazy, for
   `DATA_DIR`) import — no cycle (nothing in that graph imports qrbadge).
5. Healthz gains a field: any OpenAPI/response-shape consumer (smoke)
   updated in the same PR.
