# Task 4 proposal (kimi-k3): Server-enforced logoff deadline + same-user badge check

Coordinator reference: `docs/plans/COORDINATOR.md` Task 4 + decision 4 —
"client-side countdown for UX **plus** server-enforced lazy expiry
(`logoff_deadline` on the auth token, checked in `get_current_user`), so a
dead browser still logs the counter off." Backend only; the countdown UI is
Task 5. Verified against the working tree (Tasks 1–3 landed):
`app/models.py::AuthToken`, `app/deps.py::get_current_user`,
`app/routers/auth.py` (`_issue_token`, `logout`, `qr-login`,
`_RAW_TOKEN_RE`), `app/main.py::COLUMN_MIGRATIONS` append-only pattern.

---

## (A) Server-enforced lazy logoff deadline

### A.1 Model + migration

`app/models.py::AuthToken` gains:

```python
    # When set, the kiosk-style "log off in N seconds" deadline: once it
    # passes, get_current_user treats the token as expired (401) and
    # deletes it — a closed browser cannot leave the counter logged in.
    # Per-TOKEN, not per-user: the user's other sessions are unaffected.
    logoff_deadline: datetime | None = Field(default=None, sa_type=DateTime)
```

**Migration — required, this is a new column on an existing table.** Append
(one entry, the append-only tuple is the merge-collision lesson):

```python
    # Lazy logoff deadline for kiosk counters (Task 4); NULL = no pending
    # logoff. No index: the deadline is only ever read AFTER the primary-key
    # lookup of the presented token — no query filters on it.
    ("auth_tokens", "logoff_deadline", "DATETIME", ()),
```

**No index** (and no `INDEX_MIGRATIONS` entry): the only read path is
*after* `session.get(AuthToken, token_hash(...))` — a PK seek. A
deadline-ordered sweep would want one, but no such sweep exists (expiry
purging rides the existing `expires_at` index). Document the reasoning in
the entry comment, as the neighbouring entries do.

`tests/test_migrations.py`'s exact-set pinning test
(`test_every_migration_the_app_currently_needs_is_listed`) must be extended
with this one entry — appending is the intended flow (unlike Tasks 1–3,
which needed no entries).

### A.2 `get_current_user` enforcement (`app/deps.py`)

Current logic: PK lookup → 401 if missing or `expires_at < utcnow()` →
user lookup → 403 `must_change_password` gate. Insert the deadline check
**alongside the expiry check** (token *validity*, evaluated before the
password gate and before the exempt-path logic):

```python
    now = utcnow()
    if auth_token is None:
        raise HTTPException(401, "Token invalid or expired")
    if auth_token.logoff_deadline is not None and auth_token.logoff_deadline <= now:
        # The countdown expired while nobody cancelled: the kiosk session is
        # over even if the browser is gone. Reject-AND-DELETE — this is a
        # revocation, not a transient state; a deleted row makes the 401
        # stable and keeps no half-dead credential around.
        session.delete(auth_token)
        session.commit()
        raise HTTPException(401, "Token invalid or expired")
    if auth_token.expires_at < now:
        raise HTTPException(401, "Token invalid or expired")
```

Decisions, justified:

- **Reject-and-delete, not reject-only.** The deadline's purpose is "the
  counter is logged off, full stop". A reject-only design leaves a row that
  is valid in every other respect — a credential whose deadness depends on
  one field being read correctly forever. Deleting matches the semantics of
  `logout` (which deletes) and costs one write on a path that only dead
  tokens ever hit (normal requests: zero extra writes).
- **`expires_at` handling is deliberately untouched** (no delete there —
  purging stays `_issue_token`'s housekeeping job; behaviour unchanged).
- **Uniform 401 detail** `"Token invalid or expired"` — a deadline expiry
  is indistinguishable from ordinary expiry to the client (no information
  gain about the mechanism; the SPA already knows it scheduled a logoff).
- **Ordering vs `must_change_password`**: the deadline check runs first —
  an expired token is a 401 even on exempt paths (`/me`, `/logout`,
  `/change-password`), exactly like an expired `expires_at` today. The 403
  gate only ever sees live tokens.
- **Per-token scope**: only the presented token's row is examined/deleted;
  the user's other sessions (phone, back office) are unaffected — the
  coordinator's decision is a *kiosk* logoff, and per-token is also what
  `logout` does.

### A.3 Scheduling endpoints (`app/routers/auth.py`)

Kebab-case, matching `change-password`/`qr-login`:

```python
# app/security.py (home of TOKEN_TTL, PBKDF2_ITERATIONS — same env pattern)
LOGOFF_GRACE_SECONDS = int(os.environ.get("HCRM_LOGOFF_GRACE_SECONDS", "30"))
```

- **`POST /api/auth/logoff-schedule`** → `200 {logoff_deadline: datetime}`
  (new `LogoffScheduleOut`).
  Implementation: look up the presented `AuthToken` (same
  `token_hash(credentials.credentials)` pattern `logout` uses), set
  `logoff_deadline = utcnow() + timedelta(seconds=LOGOFF_GRACE_SECONDS)`,
  commit, return it.
  - **Idempotent restart**: calling while already scheduled simply sets a
    fresh `now + grace` — this *is* the same-badge-rescan-restarts-the-
    countdown semantics, server-side. No 409, no special case.
  - Auth: `get_current_user` (401/403 as everywhere; deliberately **not**
    added to `MUST_CHANGE_EXEMPT_PATHS` — a flagged account has no business
    scheduling kiosk logoffs before fixing its password).
  - The endpoint needs the bearer credentials as well as the user, so it
    takes `credentials: HTTPAuthorizationCredentials | None =
    Depends(bearer_scheme)` like `logout` does.
- **`POST /api/auth/logoff-cancel`** → `204`.
  Sets the presented token's `logoff_deadline = None`, commits. **Idempotent
  by design** (cancelling with nothing scheduled is still 204) — the SPA
  calls it on "stay logged in", on a different-badge scan (before the
  switch), and defensively on route changes; none of those should be able
  to error.
  - 204 (no body) mirrors `logout`/`change-password`; the SPA learns
    nothing it doesn't already know.

Server-authoritative clock: the deadline is computed server-side and
returned, so Task 5 renders the countdown from `logoff_deadline − now`
(surviving poll latency and client clock skew), never from a bare
client-side 30.

**Test seam (no sleeping, ever):** two independent levers —
1. `LOGOFF_GRACE_SECONDS` is read from env at import (tests can set
   `HCRM_LOGOFF_GRACE_SECONDS` before import in a subprocess, or simply
   `monkeypatch.setattr("app.routers.auth.LOGOFF_GRACE_SECONDS", 300)` —
   the router must reference the module-level name it imported, and tests
   patch *that* reference; documented in the test file's docstring);
2. the expiry path never needs time to pass at all: tests write
   `logoff_deadline = utcnow() - timedelta(seconds=1)` directly into the
   row and assert the next request 401s. No `time.sleep` anywhere.

### A.4 Edge cases

- **Re-scan during countdown — same badge (restart)**: Task 5 calls
  `logoff-schedule` again → fresh deadline. Server-side this is trivially
  correct (idempotent set).
- **Different badge during countdown (cancel + switch)**: Task 5 calls
  `logoff-cancel` on the current token *before* `qr-login` for the new
  account. (The switch itself is Task 5; Task 4 only guarantees the
  mechanism exists and the outgoing token can be disarmed. If Task 5
  forgets, the lazy expiry still fires — safe default.)
- **Schedule when already scheduled**: restart semantics (above); no
  error, no stacked state (it's one column, not a queue).
- **Deadline in the past**: only reachable via elapsed time (schedule
  always writes `now + grace`, and grace ≤ 0 is rejected at parse:
  `max(1, int(env))` — a zero grace would make scheduling an instant
  self-logout, which is a footgun, not a feature); the lazy check in A.2
  handles it deterministically.
- **`logout` with a pending deadline**: unchanged — deletes the row;
  deadline dies with it.
- **Token TTL vs deadline**: independent; whichever passes first kills the
  request. The 7-day TTL is not shortened.

## (B) Same-user badge check — `POST /api/auth/qr-identify`

```python
class QrIdentifyOut(BaseModel):
    same_user: bool
```

- **Auth**: `get_current_user` (401 unauthenticated; 403 while
  `must_change_password` — not exempt; a flagged account goes through the
  forced change first, which is also what Task 5 shows after any badge
  login).
- **Body**: reuse `QrLoginIn {token}` — a badge payload is a bearer
  credential, so it travels in a POST **body**, never a URL/query string
  (access logs).
- **Logic**:
  1. Normalise exactly like `qr-login` — factor the shared preamble into
     `_normalise_badge(token) -> str | None` in `auth.py`
     (`parse_badge_payload`, falling back to the `_RAW_TOKEN_RE` +
     prefix-reattach form, since a scanner/bridge may strip `HCRM1:`) and
     call it from both endpoints (one parsing doctrine, no drift).
  2. Unparseable → **401 "Invalid QR badge"**, the same uniform failure as
     `qr-login` (a malformed badge reveals nothing).
  3. `same_user = user.qr_badge_hash is not None and hmac.compare_digest(
     user.qr_badge_hash, token_hash(badge))` — `hmac.compare_digest` is
     house style for credential comparisons (`verify_password`); cheap
     insurance, though a SHA-256 hex compare is not a practical oracle.
  4. Return `QrIdentifyOut(same_user=...)`. **No side effects**: no token
     minted, no token revoked, no writes of any kind — read-only.
- **Enumeration analysis**: the response carries one bit about the
  *caller's own account*. `false` for a well-formed badge tells the caller
  "this badge is valid and belongs to someone else" — but that is
  information the caller necessarily already has access to: holding the
  badge payload means they could call `qr-login` and *become* that user.
  Possession of the payload is the credential; identify leaks nothing
  beyond it. (And a badgeless caller gets 401 like any invalid payload.)
- **Why not qr-login-then-revoke** (my phase-1 approach, now rejected):
  `qr-login` *mints* a new `AuthToken` on every same-user scan — a DB
  write, token-table growth, and then the same-user path must revoke that
  brand-new token (second write) while keeping the old one. That is two
  writes and a failure window (crash between mint and revoke → stray live
  session) per *own-badge* scan, purely to answer a yes/no question.
  `qr-identify` is one indexed hash read with zero state change. The
  revoke-on-switch path in Task 5 stays untouched — it revokes the *old*
  token after securing the new session, which is a real session operation,
  not an identify.

## Tests

### New `tests/test_logoff.py`

Helpers from `conftest` (`client`, `admin_token`, `create_user`, `auth`);
direct DB access via `Session(engine)` as in `test_cart.py`.

1. `test_schedule_sets_deadline_on_presented_token_only` — schedule → 200,
   `logoff_deadline ≈ now + 30` (monkeypatch the grace constant to 300 to
   make the assertion slack-free); a *second* session of the same user has
   `logoff_deadline IS NULL` (per-token semantics, direct DB check).
2. `test_schedule_is_idempotent_restart` — schedule, schedule again → the
   deadline moved later (no 409, no error).
3. `test_cancel_clears_and_is_idempotent` — schedule → cancel → deadline
   NULL; cancel again → still 204; cancel with nothing scheduled → 204.
4. `test_expired_deadline_is_401_and_revokes` — write a past deadline
   directly into the row → next `GET /api/items` (and `/api/auth/me`) →
   401 "Token invalid or expired"; the row is **gone** afterwards (direct
   DB check); the user's *other* token still works.
5. `test_future_deadline_does_not_block` — deadline 1 h ahead → requests
   work normally.
6. `test_uniform_401_detail` — deadline-expired 401 body equals the
   expired-token 401 body (no mechanism leak).
7. `test_deadline_expiry_beats_the_password_gate` — flagged
   (`must_change_password`) account with a past deadline → 401 (not 403)
   even on the exempt `/api/auth/me`.
8. `test_unauthenticated` — schedule/cancel without a token → 401.
9. `test_logout_with_pending_deadline` — schedule → `POST /api/auth/logout`
   → 204, row gone.
10. `test_grace_env_override` — module constant reads
    `HCRM_LOGOFF_GRACE_SECONDS` (subprocess or import-time monkeypatch,
    mirroring `test_dev_admin.py`'s pattern for import-time env).

### `tests/test_qr.py` (extend — badge semantics live there)

11. `test_identify_same_user` — register (auto-badge, Task 2) → identify
    own badge → `{same_user: true}`, both the `HCRM1:`-prefixed and the
    raw-43-char forms.
12. `test_identify_other_users_badge` — two users; A identifies B's badge
    → `{same_user: false}`; A's own badge still works afterwards (no side
    effects).
13. `test_identify_has_no_side_effects` — `AuthToken` row count unchanged
    across the call (direct DB check), `qr_badge_hash` unchanged.
14. `test_identify_rejects_badges_uniformly` — garbage, wrong-version
    (`HCRM2:…`), truncated payload → 401 "Invalid QR badge".
15. `test_identify_response_is_one_bit` — body keys are exactly
    `{same_user}` (no user fields, no badge material).
16. `test_identify_auth_gates` — unauthenticated → 401; flagged account →
    403.

### `tests/test_migrations.py`

17. Extend the exact-set expectation with the `logoff_deadline` entry, and
    assert a legacy-shaped `auth_tokens` table gains the column (NULL
    default) on boot — same pattern as the existing column cases.

### Suite hygiene

Full `uv run pytest` green; the exact-set migration test updated *only* by
appending (its pinning purpose is preserved).

## Risks / notes

- **R1 — Write-on-read in the dependency.** The deadline-expiry delete is
  the only write `get_current_user` has ever done. It runs at most once
  per dead token (the row is gone afterwards) and never on the hot path;
  if the delete's commit itself fails we still raise 401 (wrap in
  try/except → log, raise 401 regardless — the deadline comparison is
  authoritative even if the cleanup write fails). Implement the try/except;
  note for review.
- **R2 — Clock source consistency**: everything uses `models.utcnow()`
  (naive UTC) — the deadline column is `sa_type=DateTime` naive, same as
  `expires_at`; no tz mixing.
- **R3 — Grace footgun**: `HCRM_LOGOFF_GRACE_SECONDS=0` would make
  scheduling an instant self-logout; clamp with `max(1, …)` at parse
  (documented in the constant's comment).
- **R4 — Task-5 contract (write it down now)**: same-badge rescan →
  `logoff-schedule` (restart); different badge → `logoff-cancel` then
  switch; "stay logged in" → `logoff-cancel`; countdown end →
  `POST /api/auth/logout`. Even if the SPA forgets every one of these,
  the lazy expiry still logs the counter off — the server never depends on
  the client for the security property.
- **R5 — Shared-token kiosks**: two browser tabs sharing one token share
  one deadline (per-token, not per-tab). Acceptable and arguably correct
  for a counter; documented.
- **Open question (minor)**: `logoff-cancel` returns 204; if Task 5 wants
  the cleared state echoed it is a one-line change to 200 +
  `{logoff_deadline: null}` — decide when the SPA lands.

## Files touched (review checklist)

| File | Change |
|---|---|
| `app/models.py` | `AuthToken.logoff_deadline` (nullable, no index) |
| `app/main.py` | one appended `COLUMN_MIGRATIONS` entry |
| `app/deps.py` | deadline check + delete in `get_current_user` |
| `app/security.py` | `LOGOFF_GRACE_SECONDS` (env, clamped ≥ 1) |
| `app/routers/auth.py` | `logoff-schedule`, `logoff-cancel`, `qr-identify`; `_normalise_badge` factored out of `qr-login` |
| `app/schemas.py` | `LogoffScheduleOut`, `QrIdentifyOut` |
| `tests/test_logoff.py` | new (cases 1–10) |
| `tests/test_qr.py` | cases 11–16 |
| `tests/test_migrations.py` | exact-set + legacy-table case (17) |
| `docs/opticon-m10.md` / README | one paragraph each (deadline semantics + env var) — can ride or go to Task 6 |
