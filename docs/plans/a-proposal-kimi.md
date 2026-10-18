# Task A proposal (kimi-k3): HMAC-derived, re-viewable QR badges

Coordinator reference: `docs/plans/COORDINATOR2.md` decision 1 + Task A
(derived badges chosen; legacy badges **auto-migrate on first view** —
structured 409 reserved for key missing/rotated). Verified against the
working tree: `app/qrbadge.py` (`new_badge_for`, `parse_badge_payload`'s
43-char contract), `app/security.py` (env-at-import constant pattern,
`token_hash`), the four `new_badge_for` call sites (`register`,
`create_member`, `create_my_badge`, `create_member_badge`),
`app/database.py::DATA_DIR`, `healthz` (`insecure_dev_admin` loud-flag
pattern), `scripts/backup_db.sh`, `app/main.py::_startup` + lifespan.

---

## 1. Master secret bootstrap (`app/security.py`)

Env-at-import constants already live here (`PBKDF2_ITERATIONS`,
`TOKEN_TTL`, `DEV_ADMIN`); the badge key follows the same pattern but is
**lazily loaded and cached** (never import-time file I/O — tests
monkeypatch the cache):

```python
from app.database import DATA_DIR  # verify at implementation: no import
                                   # cycle (database.py must not import
                                   # security — it does not today)

BADGE_KEY_FILE = os.path.join(DATA_DIR, "badge.key")
_badge_secret: bytes | None = None

def badge_secret() -> bytes:
    """The badge master secret, cached for the process.

    Source order: HCRM_BADGE_SECRET env (used verbatim, never written to
    disk) → the 0600 keyfile → auto-generate 32 random bytes into the
    keyfile (0600, O_EXCL) with a loud startup-style warning. Auto-
    generating on a missing file is DELIBERATE: the server must boot, and
    the failure mode (key loss) is detected at re-view time by the hash
    mismatch (§4), never by a boot crash. Losing the key degrades, it
    does not lock anyone out (logins verify by hash).
    """
```

- `os.open(path, O_WRONLY|O_CREAT|O_EXCL, 0o600)` — no race, right perms
  from birth; a second concurrent booter reads the winner's file.
- On generation: `logger.warning("generated a new badge master key at %s
  (0600) — back it up with the database; losing it makes badges
  non-re-viewable until rotation", path)` (the dev-admin announcement
  pattern).
- **Bootstrap call at startup** in `app/main.py::_startup` so the key
  exists deterministically before the first request and the announcement
  lands in boot logs, not mid-request.
- Test seam: `badge_secret()` reads the module-global cache; tests reset
  it (`monkeypatch.setattr(security, "_badge_secret", None)`) and/or set
  `security._badge_secret = b"test-key"`. No sleeping, no subprocess
  except the wrong-key boot test (§7, T10 — subprocess mirrors
  `test_dev_admin.py`'s import-time-env pattern).

## 2. Derivation (`app/qrbadge.py`)

```python
def derive_badge_payload(user_id: int, generation: int, secret: bytes) -> str:
    """payload = HCRM1: + urlsafe_b64(HMAC-SHA256(secret,
    "badge:{user_id}:{generation}")) — 43 chars, byte-identical in shape
    to token_urlsafe(32) output, so parse_badge_payload, the bridge, the
    SPA shape gate and every existing test accept derived badges
    unchanged. Derived from user_id (immutable), NEVER email: Task B's
    change-email must be badge-safe by construction."""
    msg = f"badge:{user_id}:{generation}".encode()
    digest = hmac.new(secret, msg, hashlib.sha256).digest()
    return BADGE_PREFIX + base64.urlsafe_b64encode(digest).decode().rstrip("=")
```

`new_badge_for(user)` is reworked (same signature — all four call sites
keep working), from random-issuance to generation-issuance:

```python
def new_badge_for(user) -> str:
    """Issue the next derived badge: bump the generation, store only its
    hash, return the payload. REQUIRES user.id (flush before calling for a
    not-yet-committed user). Generation only ever increases — a retired
    payload never becomes valid again."""
    user.qr_badge_generation = (user.qr_badge_generation or 0) + 1
    payload = derive_badge_payload(user.id, user.qr_badge_generation,
                                   badge_secret())
    user.qr_badge_hash = token_hash(payload)
    return payload
```

`qrbadge.py` already imports `token_hash` from `app.security` (Task 2) —
no new cycle; it gains `badge_secret` from the same module.

## 3. Migration + issuance ordering

**Column** (`app/models.py::User`):
`qr_badge_generation: int = Field(default=0)` — **0 = legacy random badge
(or none); ≥ 1 = derived**. `COLUMN_MIGRATIONS` append:
`("users", "qr_badge_generation", "INTEGER NOT NULL DEFAULT 0", ())` —
every existing row lands at 0 = legacy, so the upgrade cannot invalidate
any printed badge. Revoke keeps the generation (never decrement: re-issue
bumps from the last value, so a revoked-then-reissued badge never equals
an older one).

**Issuance (register / create_member)** — kimi's flush approach (chosen
over qwen's two-phase in review; the coordinator adopted "flush-then-
derive"): derivation needs `user.id`, and flush provides it **without a
second commit**:

```python
    user = User(...)
    session.add(user)
    try:
        session.flush()                    # assigns id, NO commit
        badge_payload = new_badge_for(user)  # gen 1, hash set
        session.commit()
    except IntegrityError:                  # duplicate email now surfaces
        session.rollback()                  # at the FLUSH — same handler
        raise HTTPException(409, "Email already registered")
    session.refresh(user)
```

One transaction: a raced/failed insert rolls back row, id, and
never-shown payload together — **no badgeless-account window** (qwen's
two-phase had one, healed by rotation; the flush removes it instead of
healing it). The `IntegrityError` catch must wrap the flush (the INSERT
now happens there, not at commit — the catch already wraps the whole
block; verify at implementation). Rotate endpoints are unchanged
call-site-wise (`new_badge_for` on a committed user).

**RegisterOut / MemberCreatedOut**: unchanged shape — the gen-1 derived
payload + SVG, returned once here and re-viewable forever after (the
Task-D copy fix changes the warning text; Task A notes it).

## 4. Re-view endpoints

Schema (`app/schemas.py`):

```python
class QrBadgeViewOut(QrBadgeOut):
    generation: int
    migrated: bool = False   # True when this view auto-migrated a legacy
                             # badge — the UI MUST warn that the old
                             # printed badge just died
```

Shared helper in `app/qrbadge.py` (it owns the badge lifecycle; takes the
session for the migration commit):

```python
def view_badge(session, user) -> QrBadgeViewOut: ...
# logic:
#   hash is None            → 404 "No QR badge — generate one first"
#   generation >= 1:
#       derived = derive(user.id, generation, badge_secret())
#       compare_digest(token_hash(derived), hash) → match: return it
#       mismatch → 409 {"status": "key-mismatch", "re_viewable": false,
#                       "message": "the badge key changed since this badge
#                        was issued — rotate to re-issue (logins are
#                        unaffected)"}     ← NEVER show a wrong badge
#   generation == 0 (legacy random badge):
#       payload = new_badge_for(user)   # gen 0→1, derived; old card dies
#       session.add(user); session.commit()
#       return migrated=True            ← coordinator decision 1
```

Endpoints:
- **`GET /api/auth/qr-badge`** (`get_current_user`; NOT in
  `MUST_CHANGE_EXEMPT_PATHS` — flagged accounts 403 like everywhere).
  Repeatable, side-effect-free except the one-time legacy migration.
- **`GET /api/members/{id}/qr-badge`** (`require_staff` +
  `_get_manageable_member` — staff can never touch an admin account; the
  counter's "reprint a member's card" path).

**Login/identify are untouched** — `qr_badge_hash` remains the sole
verification authority (indexed hash seek, uniform 401 / uniform false,
zero crypto on the hot path). Legacy random badges verify by hash
indefinitely until viewed/rotated.

## 5. Backup + healthz

- `scripts/backup_db.sh`: after the DB copy, `cp -p "$DATA_DIR/badge.key"
  "$DEST/"` when present; print a loud line — *"NOTE: this backup includes
  badge.key and is now credential-bearing (it can derive member QR
  badges) — protect it like a password list."* The restore echo gains
  "copy badge.key back too". `tests/test_backup_and_run.py`: assert the
  keyfile lands in the backup dir (the harness already subprocess-runs
  the script — add a scratch data dir with a keyfile).
- `healthz`: bootstrap at startup makes the source deterministic; report
  `"badge_derivation": true` plus `"badge_key_source": "env" | "file"`
  (not secret — same exposure class as `pbkdf2_iterations`; follows the
  `insecure_dev_admin` loud-visibility doctrine).
- Docs note for Task F (flagged here): README env-table row
  `HCRM_BADGE_SECRET`, the trade-off paragraph ("a leaked DB **plus** key
  now yields badges — protect them together"), and the rotation runbook
  (key loss ⇒ re-view 409s until users rotate; mass rotation = bump every
  generation via staff endpoint).

## 6. Error cases (decision table)

| Case | Behaviour |
|---|---|
| No badge (`hash` NULL) | GET → **404** ("generate one first") |
| Legacy badge (`generation` 0) | auto-migrate to gen 1; `migrated: true`; old payload dies immediately |
| Derived, key intact | 200, identical payload+svg forever |
| Derived, key file lost (auto-regenerated) or env key changed | GET → **409 `key-mismatch`**, never a wrong badge; **login still works**; rotation heals (rotate → GET 200) |
| Flagged account (`must_change_password`) | GET → 403 (not exempt) |
| Staff GET: member target | 200 (staff/admin actor) |
| Staff GET: admin target, staff actor | 403 (`_get_manageable_member`) |
| Anonymous | 401 |
| Register race | 409, no row, no generation/hash trace (flush rollback) |

## 7. Tests

**New `tests/test_qr_derived.py`:**
1. Derivation shape: 43 urlsafe chars, `parse_badge_payload` round-trips;
   deterministic (same user+gen ⇒ same payload); differs by generation
   and by user; **never derived from email** (rename in DB → same
   payload — the Task-B-safety pin, with a comment pointing at Task B's
   endpoint test).
2. Register → generation 1 in DB; badge qr-logins (both forms).
3. **Re-view repeatable**: GET twice → identical payload/svg; equals the
   registration payload.
4. Rotate → generation 2; new logs in, old 401s; GET shows the new one.
5. Revoke → GET 404; re-issue → generation 3 (never reuses 1 or 2).
6. Staff GET: member target 200; admin target as staff 403; member actor
   403; admin actor on admin target 200.
7. **Key mismatch never authenticates, never misleads**: monkeypatch the
   cached secret to a different value → derived ≠ stored → GET 409
   `key-mismatch`; the derived-with-wrong-key payload gets 401 at
   qr-login; login with the REAL badge still works (hash authority); then
   rotate → GET 200 (healing path).
8. **Legacy auto-migration**: hand-build a user with a random badge
   (`new_badge_payload`, generation 0) → qr-login OK → GET → 200
   `migrated: true`, generation 1, **old payload now 401s** → second GET
   → same new payload, `migrated: false`.
9. Key bootstrap: fresh temp DATA_DIR → first `badge_secret()` call
   creates the file `0600`, stable across calls; env override used
   verbatim and **no file written**; reset-cache seam works.
10. **Wrong-key boot (subprocess, `test_dev_admin.py` pattern)**: boot
    with `HCRM_BADGE_SECRET=A`, issue a badge, reboot with `=B` → GET
    409, login 401-with-derived-B-payload, real badge still 200 (hash).
11. Register 409 race → no user row, `qr_badge_generation` unchanged
    (flush-ordering pin).
12. Flagged account → GET 403.
13. `healthz` carries `badge_derivation` and `badge_key_source`.

**Extend existing:** `tests/test_qr.py` stays green (random-payload unit
tests still valid — `new_badge_payload` remains for legacy/badge-
prefix parsing; lifecycle tests now exercise derived badges transparently);
`tests/test_migrations.py` exact-set gains
`("users", "qr_badge_generation")` + legacy-DB column-lands-0 pin +
**legacy DB with a random badge still logs in post-upgrade** (the
legacy-verification pin); `tests/test_backup_and_run.py` keyfile case.

## 8. Files touched

| File | Change |
|---|---|
| `app/security.py` | `badge_secret()` + cache + keyfile bootstrap |
| `app/qrbadge.py` | `derive_badge_payload`, reworked `new_badge_for`, `view_badge` |
| `app/models.py` | `User.qr_badge_generation` |
| `app/main.py` | migration append; `_startup` key bootstrap; healthz fields |
| `app/routers/auth.py` | flush-then-derive issuance; `GET /api/auth/qr-badge` |
| `app/routers/members.py` | `GET /api/members/{id}/qr-badge` |
| `app/schemas.py` | `QrBadgeViewOut` |
| `scripts/backup_db.sh` | copy + credential-bearing warning |
| tests | `test_qr_derived.py` (new); `test_qr.py`, `test_migrations.py`, `test_backup_and_run.py` (extend) |

## 9. Risks / notes

- **R1 — flush changes where the duplicate-email error surfaces** (flush,
  not commit): same exception type, same handler, but the block's comment
  and the 409 test should be re-read at implementation; also confirms no
  other code relied on commit-time-only INSERT ordering inside register.
- **R2 — import-cycle check**: `security.py` importing
  `app.database.DATA_DIR` — `database.py` imports nothing from security
  today; verify and keep it that way (alternative: duplicate the two-line
  env resolution — rejected, single source of truth).
- **R3 — the trade-off is policy, not code**: DB+key ⇒ derivable badges.
  Mitigations shipped here are operational (0600, backup warning, healthz
  visibility, rotation runbook); the Task-F docs must carry the paragraph
  verbatim.
- **R4 — auto-generation on key loss is deliberate** (boot never bricks),
  and the detection point is the re-view mismatch — the generation
  announcement in logs is the only alarm; noted for the ops runbook.
- **R5 — `QrBadgeViewOut` for the staff GET leaks nothing extra** (staff
  could already rotate/revoke any non-admin badge — viewing is the same
  trust tier).
- **Open question (minor)**: `migrated: true` response flag vs a
  `{status: "migrated"}` detail-style envelope — the flat additive fields
  match house schema style; coordinator can flip cheaply.
