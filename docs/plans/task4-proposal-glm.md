# Task 4 proposal (glm) — Server-enforced lazy logoff deadline + same-user badge check

Coordinator Task 4 + decision 4: client-side countdown (Task 5) **plus**
server-enforced lazy expiry — `logoff_deadline` on the auth token, checked in
`get_current_user` — so a browser that dies mid-countdown still logs the
counter off. Plus the same-user badge predicate the Task-5 scan router needs
to choose "logoff screen" vs "save-then-switch". **Backend only.**

Ground truth (verified across Tasks 1–3): `AuthToken(token PK = hash,
user_id FK indexed, expires_at DateTime indexed)`; `get_current_user`
(`app/deps.py`) — 401 missing credentials → 401 token-missing-or-expired
(`expires_at < utcnow()`) → 401 user-gone → 403 `must_change_password`
outside `MUST_CHANGE_EXEMPT_PATHS = {/api/auth/me, /api/auth/logout,
/api/auth/change-password}`; `logout` revokes **only the presented token**
(via `bearer_scheme` + `session.get(AuthToken, token_hash(...))` — the
pattern the new endpoints reuse); `_issue_token` purges `expires_at`-past
rows on every login; `qr_login` accepts the full `HCRM1:` payload **or** the
raw 43-char token, uniform 401; `COLUMN_MIGRATIONS` is the append-only
`(table, column, DDL, post-DDL)` tuple in `app/main.py`, pinned by
`tests/test_migrations.py::test_every_migration_the_app_currently_needs_is_listed`
(**that test pins the exact set — adding an entry requires updating its
expectation; that is by design**).

---

## (A) Server-enforced lazy logoff deadline

### A1. Column + migration

`app/models.py`:

```python
class AuthToken(SQLModel, table=True):
    ...
    expires_at: datetime = Field(sa_type=DateTime, index=True)
    # Server-enforced logoff deadline (Task 4): while a logoff countdown is
    # pending on THIS token, the time at which it must die — even if the
    # browser holding it never asks again (lazy expiry in get_current_user).
    # Nullable: None = no pending logoff (the overwhelmingly common case).
    # Per-token by design, same principle as logout: the user's OTHER
    # sessions are unaffected.
    logoff_deadline: datetime | None = Field(default=None, sa_type=DateTime)
```

`app/main.py` — one append-only entry (never edit the shared block; the
round-5 collision lesson):

```python
    # Server-enforced logoff deadline (Task 4): NULL = no pending logoff.
    ("auth_tokens", "logoff_deadline", "DATETIME", ()),
```

**No index.** The per-request check runs on the row already fetched by the
token-hash **primary-key seek** — no query ever filters on `logoff_deadline`.
The only other touch is the login-time purge (A4), a housekeeping scan on a
small table; if that ever matters, an `INDEX_MIGRATIONS` entry is the
append-only remedy. Do not pay the index now.

`tests/test_migrations.py`: the exact-set test's expectation gains the new
entry (mandatory — the test exists to force conscious additions), plus a
case that a legacy DB (auth_tokens without the column) gains it via
`_migrate_schema`, idempotently, and that fresh `create_all()` matches.

### A2. The lazy expiry check in `get_current_user`

Inserted with the existing validity check, before the user lookup (a dead
token is dead regardless of the account's flag):

```python
    auth_token = session.get(AuthToken, token_hash(credentials.credentials))
    now = utcnow()
    if auth_token is None or auth_token.expires_at < now:
        raise HTTPException(401, "Token invalid or expired")     # unchanged

    # Lazy logoff expiry (Task 4): a pending deadline that has passed kills
    # the token even if the browser that scheduled it never comes back —
    # that is the whole point of server-side enforcement.
    if (auth_token.logoff_deadline is not None
            and auth_token.logoff_deadline < now):
        # Reject AND delete: the rejection alone would leave a zombie row
        # that a clock skew could resurrect; deleting makes the logoff real
        # (same end state as logout reaching the server). Best-effort write
        # from a dependency — the get_session pattern supports it.
        session.delete(auth_token)
        session.commit()
        raise HTTPException(401, "Token invalid or expired")     # same detail:
        # deliberately indistinguishable from natural expiry — the client
        # must not learn (or depend on) WHY the session died.
```

- **Comparison mirrors `expires_at`'s** (`< utcnow()`, naive UTC both sides).
- **Applies to exempt paths too**: `/api/auth/me` polling during the
  countdown works (deadline is in the future); after expiry even `/me` 401s —
  which is exactly how a still-alive browser learns the logoff landed.
- **Ordering vs `must_change_password`**: token validity (incl. deadline)
  first, then user, then the 403 flag — a dead token on a flagged account is
  a 401, not a 403.
- **Reject-and-delete, not reject-only**: deletion is what makes the logoff
  durable (no resurrection window) and keeps the table tidy; it also matches
  `logout`'s end state. The write is one row delete + commit on a session
  the dependency already owns.

### A3. Scheduling endpoints (`app/routers/auth.py`)

| Endpoint | Method | Auth | Request | Success | Errors |
|---|---|---|---|---|---|
| `/api/auth/logoff-schedule` | POST | bearer (any role; **exempt** from the pw-change gate) | — | **200** `{"deadline": "<naive-UTC ISO>"}` | 401 (invalid/expired token) |
| `/api/auth/logoff-cancel` | POST | bearer (exempt) | — | **204** | 401 |

Both locate the presented token exactly like `logout` does
(`bearer_scheme` + `session.get(AuthToken, token_hash(...))`; the token
cannot be missing — `get_current_user` already 401'd — so no 404 path).

- **`logoff-schedule`** sets `logoff_deadline = utcnow() +
  LOGOFF_GRACE_SECONDS` on **the presented token only**, commits, and
  returns the deadline. Returning the server timestamp (rather than letting
  the SPA compute `now + 30` locally) makes the countdown deadline-driven
  and immune to client clock skew and request latency.
- **Restart semantics = overwrite.** Scheduling while a deadline is already
  pending simply sets a fresh `now + grace` — no special case. This is
  precisely the primitive the Task-5 "same badge re-scan during the
  countdown restarts it" behaviour needs: the SPA just calls schedule again.
  The server holds no badge knowledge; restart-vs-cancel is the SPA's
  decision (via B's `qr-identify`), expressed through schedule/cancel.
- **`logoff-cancel`** sets `logoff_deadline = None`, commits. 204 mirrors
  the revoke-endpoint style. This is the primitive for "Stay logged in"
  **and** for "different badge scanned during the countdown → cancel, then
  switch".
- **Deadline in the past** cannot be produced by the endpoints (grace > 0);
  the A2 check handles any past value safely (treated as expired). A
  non-positive `HCRM_LOGOFF_GRACE_SECONDS` is a misconfiguration the check
  survives (deadline ≤ now ⇒ token dies on next use); no extra guard.
- **`MUST_CHANGE_EXEMPT_PATHS` gains all three Task-4 paths**
  (`/api/auth/logoff-schedule`, `/api/auth/logoff-cancel`,
  `/api/auth/qr-identify`): logging off and routing a badge scan are session
  hygiene, like `logout`/`me` — a flagged account's kiosk session must still
  be able to leave cleanly.

**Test/demo override for the 30 s (no sleeping):** the constant lives in
`app/security.py` next to `TOKEN_TTL`:

```python
# The logoff countdown grace (Task 4). Env-overridable for demos/tests;
# read once at import like every other env knob here.
LOGOFF_GRACE_SECONDS = int(os.environ.get("HCRM_LOGOFF_GRACE_SECONDS", "30"))
```

The auth router references it **via its module namespace**
(`from app import security` … `security.LOGOFF_GRACE_SECONDS` at call time,
or import the name into `app.routers.auth` and let tests patch
`app.routers.auth.LOGOFF_GRACE_SECONDS`) so tests can monkeypatch it per
test without subprocesses. Tests never sleep: expiry is exercised either by
patching the grace to ~0 **and** monkeypatching `app.deps.utcnow` forward,
or — simplest, no time travel at all — by scheduling normally and then
pushing the row's `logoff_deadline` into the past via a direct `Session`
(the check is what is being tested, not the arithmetic).

### A4. Housekeeping (optional, one line)

`_issue_token`'s purge extends from `expires_at < now` to also drop
deadline-past rows (`or_(AuthToken.expires_at < now,
AuthToken.logoff_deadline != None, AuthToken.logoff_deadline < now)`) — a
token whose deadline passed and which is never presented again otherwise
lingers up to the 7-day TTL. Bounded table, no index implication (the OR
just makes the housekeeping scan, which is fine at this scale). If the
coordinator prefers zero change to `_issue_token`, skipping this is also
correct — A2's delete handles every presented token.

### A5. Edge cases (summary)

| Case | Behaviour |
|---|---|
| Same badge re-scanned during countdown | SPA calls `logoff-schedule` again → deadline overwritten (restart). |
| Different badge during countdown | SPA calls `logoff-cancel`, then proceeds with the switch (qr-login etc.). |
| Schedule when already scheduled | Overwrite (restart) — idempotent by design. |
| Deadline in the past (DB manipulation / skew) | A2 treats as expired: delete + uniform 401. |
| Browser dies mid-countdown | Token dead after the deadline on its next use anywhere — the requirement. |
| Still-alive browser at expiry | `/me` poll 401s; the SPA (Task 5) also calls `logout` for cleanliness, but correctness does not depend on it. |
| Other sessions of the same user | Untouched — deadline is per-token (logout principle). |
| `change-password` during countdown | Revokes all *other* tokens, spares the current one (existing behaviour) — the deadlined current token keeps its deadline. |
| Concurrent schedule/cancel | Last write wins; both are single-row commits; harmless. |
| Natural expiry before the deadline | Existing `expires_at` check fires first — same 401. |

## (B) Same-user badge check — `POST /api/auth/qr-identify`

```python
class QrIdentifyOut(BaseModel):
    same_user: bool
```

- **Request:** reuse `QrLoginIn` verbatim (`{token: 1..64, extra="forbid"}`)
  — it is exactly the shape, and reusing it keeps the two badge-consuming
  endpoints accepting the same forms.
- **Auth:** `get_current_user` (any role; in `MUST_CHANGE_EXEMPT_PATHS`).
- **Logic** (mirrors `qr_login`'s acceptance, minus every side effect):

```python
    badge = parse_badge_payload(payload.token)
    if badge is None and _RAW_TOKEN_RE.fullmatch(payload.token.strip()):
        badge = BADGE_PREFIX + payload.token.strip()      # prefix-stripped form
    same = (badge is not None and user.qr_badge_hash is not None
            and token_hash(badge) == user.qr_badge_hash)
    return QrIdentifyOut(same_user=same)
```

- **`same_user: false` for everything that is not the caller's own badge** —
  garbled scans, other people's badges, a badge the caller revoked, a
  badgeless account scanning anything. No 401 branch: the endpoint answers
  one boolean question, and a non-badge is simply "not yours" (the SPA
  toasts "unrecognised badge"). This is lazier to consume than qr-login's
  uniform-401 discipline, and it is safe because no information is
  returned beyond a bit about the caller's **own** `qr_badge_hash`.
- **No side effects:** no token issuance, no writes, no rate-limit surface
  (the badge is 256-bit, same reasoning as qr-login's lack of a rate limit).
- **No enumeration:** an authenticated caller probing someone else's badge
  learns only "not mine" — nothing about whose it is or whether it exists.

**Why not qr-login-then-compare(-and-revoke):** qr-login **mints a full
bearer session** to answer what is usually a same-user probe (the dominant
case at a counter where the operator re-scans their own badge). That (1)
creates a live credential per probe which must then be revoked in a second
round trip — a window where a throwaway session token exists; (2) makes the
kiosk's "which session do I keep" bookkeeping ambiguous (the newly minted
token vs the current one); (3) litters `auth_tokens` and the login purge
with probe artefacts; and (4) lets any authenticated caller mint sessions
for any badge holder as a side effect of probing. `qr-identify` is a pure
predicate — one bit, no credential, no state — and the switch flow still
uses `qr_login` exactly when it actually wants the new session.

## Tests

**`tests/test_logoff.py`** (new):

1. Unauthenticated `logoff-schedule` / `logoff-cancel` → 401.
2. Schedule → 200; `deadline` present and in the future; the token still
   works (`/me` 200); the row's `logoff_deadline` matches the response
   (direct DB).
3. Cancel → 204; `/me` still 200; `logoff_deadline` is NULL again (direct DB).
4. **Lazy expiry (the pin)**: schedule, push the row's deadline into the
   past via a direct `Session` → the next authenticated request is 401 with
   the *same detail string* as a naturally expired token, and the row is
   **deleted** (direct DB).
5. Expiry via the grace constant: monkeypatch the grace to 0 and
   `app.deps.utcnow` forward (or schedule + direct-DB past deadline) —
   proves the env/constant wiring without sleeping.
6. **Per-token isolation**: two sessions of one user; schedule on A; B has
   no deadline and keeps working; A dies after its deadline.
7. **Restart semantics**: schedule, advance the clock (or patch grace),
   schedule again → the row's deadline moved later (overwrite, not
   first-write-wins).
8. Flagged account (`must_change_password`): schedule/cancel/identify are
   exempt (200/204/200) while a non-exempt path still 403s.
9. `logout` during a pending countdown deletes the row (existing endpoint;
   one assertion that the deadline dies with it).
10. (If A4 adopted) a deadline-past, never-presented token is purged by the
    next login.

**`tests/test_qr.py`** additions:

11. `qr-identify` with the caller's own badge (full `HCRM1:` form **and**
    the raw 43-char form) → `{same_user: true}`; with another user's badge
    → `false`; with garbage / a product barcode / a truncated payload →
    `false` (never 401, never an error body).
12. A badgeless account (`qr_badge_hash is None`) → `false` for any input,
    including its own would-be badge.
13. **No side effects**: `auth_tokens` row count unchanged by any identify
    call (direct DB, before/after).
14. **No leak**: the response is exactly `{"same_user": ...}` — no user
    fields, no badge material.

**`tests/test_migrations.py`**: exact-set expectation gains the
`auth_tokens.logoff_deadline` entry (mandatory — the test pins the set);
legacy-DB upgrade gains the column; idempotent re-run; fresh `create_all()`
shape matches.

**`tests/test_auth.py`**: the token-hash/expiry tests stay green (the new
check is additive); optionally one assertion that a plain login token (no
deadline) behaves exactly as before.

## Files touched (review checklist)

| File | Change |
|---|---|
| `app/models.py` | `AuthToken.logoff_deadline: datetime \| None` |
| `app/main.py` | +1 `COLUMN_MIGRATIONS` entry (append-only) |
| `app/deps.py` | lazy-expiry check in `get_current_user` (delete + uniform 401); +3 `MUST_CHANGE_EXEMPT_PATHS` |
| `app/security.py` | `LOGOFF_GRACE_SECONDS` (env-overridable, default 30) |
| `app/routers/auth.py` | `POST /logoff-schedule`, `POST /logoff-cancel`, `POST /qr-identify` (+ `QrIdentifyOut` in schemas); optional `_issue_token` purge extension |
| `app/schemas.py` | `QrIdentifyOut` (reuses `QrLoginIn` for the body) |
| tests | `test_logoff.py` (new), `test_qr.py` (+identify cases), `test_migrations.py` (exact-set + legacy-upgrade), `test_auth.py` (green) |

## Risks / open questions

1. **A write inside a dependency** (the expiry delete): supported by the
   `get_session` pattern, but it is the first write `get_current_user`
   performs — reviewers should note it is one row delete, committed before
   the 401 raises, and cannot interleave with endpoint logic (the request
   dies with the 401).
2. **Uniform-401 choice**: the deadlined token is indistinguishable from a
   naturally expired one. The SPA therefore cannot show "you were logged
   off by the countdown" vs "session expired" — Task 5's UI treats both as
   "logged off", which is the intended UX anyway. Flagged in case the
   coordinator wants a distinguishable detail.
3. **`LOGOFF_GRACE_SECONDS` import-time read** (like every env knob here):
   per-test overrides go through monkeypatching the name the router
   references — the proposal pins which name (`app.routers.auth.
   LOGOFF_GRACE_SECONDS`) so tests don't reach for subprocesses.
4. **A4 purge extension** is optional; adopting it adds an OR to the
   login-time purge (small-table full scan acceptable). Coordinator's call.
5. **qr-identify vs qr-login acceptance parity**: both accept prefixed and
   raw-token forms; if one ever changes, the other should follow — a
   shared-acceptance note goes in the endpoint docstring (a formal shared
   helper is overkill for two call sites of a 3-line normalisation).
6. **Clock skew between schedule and check** is one-sided (server clock
   only — the SPA never computes the deadline), which is the point of
   returning the timestamp.
