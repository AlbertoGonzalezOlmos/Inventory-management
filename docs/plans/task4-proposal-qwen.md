# Task 4 proposal (qwen3.8-max) — Server-enforced logoff deadline + `qr-identify`

Backend only (the 30 s countdown UI is Task 5). Implements coordinator Task 4
and decision **D6**: "client-side countdown for UX **plus** server-enforced
lazy expiry (`logoff_deadline` on the auth token, checked in
`get_current_user`), so a dead browser still logs the counter off."
Proposal only — grounded in the current tree (Tasks 1–3 landed).

**Verified ground truth this pass:** no `logoff`/`LOGOFF` identifier exists
anywhere yet; `AuthToken(token PK, user_id indexed, expires_at indexed)` at
`app/models.py:153`; `get_current_user` (`app/deps.py`) order = credentials →
PK lookup by `token_hash` → `expires_at < utcnow()` → 401 "Token invalid or
expired" → user fetch → `must_change_password` gate (exempt paths: `/me`,
`/logout`, `/change-password`); `logout` revokes **only the presented token**
(the scoping precedent this task copies); `_issue_token` purges
`expires_at < now` rows on every login (indexed — PLAN-v2 N8);
`COLUMN_MIGRATIONS` at `app/main.py:145` is append-only and
`tests/test_migrations.py::test_every_migration_the_app_currently_needs_is_listed`
pins its **exact set** (this task must consciously extend that test — the
first task since round 4 that legitimately does); `qr_login` inlines the
badge-acceptance rule (`parse_badge_payload`, else `_RAW_TOKEN_RE.fullmatch`
→ re-prefix); naive-UTC `utcnow()` everywhere; env-at-import constants are
the house pattern (`HCRM_PBKDF2_ITERATIONS`, `HCRM_DEV_ADMIN`).

---

## A. Server-enforced lazy logoff deadline

### A1. Model + migration (append-only; **no index**)

```python
# app/models.py — AuthToken gains:
    # Server-enforced kiosk logoff countdown (Task 4 / coordinator D6):
    # when set, this token dies at this instant even if the browser
    # vanishes mid-countdown. NULL = no countdown armed. Lazily enforced
    # in get_current_user (reject-and-delete); nothing schedules.
    logoff_deadline: datetime | None = Field(default=None, sa_type=DateTime)
```

```python
# app/main.py — COLUMN_MIGRATIONS, appended (never inserted):
    # Kiosk logoff countdown (Task 4): nullable; existing rows get NULL
    # (= unarmed), so the upgrade cannot log anyone off.
    ("auth_tokens", "logoff_deadline", "DATETIME", ()),
```

**Index: NO.** The only access path is the PK point-lookup
(`session.get(AuthToken, hash)`) — the deadline is read off the fetched row,
never queried *by*. Nothing filters on it: the login-time purge stays
`expires_at`-based (its index is why N8 exists), and there is no background
sweeper (lazy-expiry doctrine — the same reason Phase B's counter design used
"expire on next touch"). An index would be pure write overhead on the hottest
table. Recorded here because `test_migration_table_is_well_formed` allows
empty post-statements and a reviewer will ask.

**Migration-test consequences (deliberate, conscious edits):** the exact-set
test gains `("auth_tokens", "logoff_deadline")`; the legacy-DB landing tests
(`test_all_migrations_land_on_a_pre_scanner_database` and friends) gain the
column assertion; half-migration repair is automatic (empty post-statements).
Every existing token gets NULL → no one is logged off by the upgrade, and no
session invalidation is needed (contrast the round-4 hash migration note in
README).

### A2. Enforcement in `get_current_user` — reject-and-delete

Insert directly after the existing `expires_at` check (same 401, **same
detail string** — the spec says "treat as expired", and an indistinguishable
401 leaks nothing about the mechanism; Task 5's SPA knows *it* armed the
countdown and can render "logged off" without a server hint):

```python
    if (auth_token.logoff_deadline is not None
            and auth_token.logoff_deadline <= utcnow()):
        # Lazy enforcement (D6): the countdown expired — possibly because
        # the browser closed mid-countdown, the case client-side timers
        # cannot cover. Reject AND delete, so the row cannot be presented
        # again and does not linger: the same self-healing pattern as the
        # cart's orphan reap (Task 1), and equally a one-shot write — this
        # fires once per killed token, never per request (the BEGIN
        # IMMEDIATE incident in app/database.py is why that distinction is
        # stated out loud).
        session.delete(auth_token)
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalid or expired",
        )
```

Decisions, with rationale:

- **Reject-and-delete, not reject-only.** A lingering row is inert (every
  presentation 401s), but deletion is one line, matches the cart-reap
  precedent, keeps `auth_tokens` free of countdown corpses for up to 7 days,
  and makes "deadline killed it" observable in tests (row gone). The write
  happens exactly once per dead token — the read-path-write concern from the
  `app/database.py` incident does not apply, and the comment says so.
- **`<=`, not `<`.** The countdown reaching 0 *is* the logoff moment (the
  SPA fires at 0 too); `expires_at` keeps its `<` (a token is valid up to
  the instant). The asymmetry is deliberate and pinned by the zero-grace
  test.
- **Placement before the user fetch and the `must_change_password` gate.**
  A deadline-killed token is dead on *every* path, including the three
  exempt ones (a flagged account's kiosk session dies like any other);
  `MUST_CHANGE_EXEMPT_PATHS` is untouched.
- **Interaction with `expires_at`:** independent checks, first failure wins;
  the schedule endpoint clamps the deadline to `expires_at` (A3) so the
  deadline can never outlive the token.
- **`_issue_token`'s purge is NOT extended** (deadline-killed rows of
  never-returning browsers are removed at the next login's `expires_at`
  purge, ≤ 7 days, inert until then; presented ones are deleted on
  presentation). Smallest diff, no new scan on the login path.

### A3. Scheduling endpoints (`app/routers/auth.py`)

Both are **presented-token-scoped**, exactly like `logout` ("revoke only the
presented token — other sessions of the user stay alive"): arming the kiosk
session must never kill the same user's phone session. Both take
`credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)`
+ `user = Depends(get_current_user)` + `session` (same signature shape as
`logout`; the request-scoped `get_session` cache means both dependencies see
the same row).

**The grace constant** (`app/security.py`, next to `TOKEN_TTL` — credential
lifetimes live together):

```python
# Kiosk logoff countdown (coordinator D6). Env override for demos/kiosks;
# read at import time like the other HCRM_* knobs (README config table).
LOGOFF_GRACE_SECONDS = max(0, int(os.environ.get("HCRM_LOGOFF_GRACE_SECONDS", "30")))
```

`auth.py` does `from app.security import LOGOFF_GRACE_SECONDS` and references
it **inside the endpoint body** — so tests can
`monkeypatch.setattr("app.routers.auth.LOGOFF_GRACE_SECONDS", 0)` per test
and **never sleep**. (The import style is load-bearing for the override; the
expiry test failing loudly is the guard against a refactor breaking it —
noted in the proposal's test list.)

```
POST /api/auth/logoff-schedule   → 200 LogoffScheduleOut {deadline: datetime,
                                                            grace_seconds: int}
POST /api/auth/logoff-cancel     → 204 (idempotent)
```

- **schedule**: `now = utcnow()`;
  `deadline = min(now + timedelta(seconds=LOGOFF_GRACE_SECONDS),
  auth_token.expires_at)` (the clamp keeps an absurd
  `HCRM_LOGOFF_GRACE_SECONDS` from producing a deadline past the token's
  life); set, commit, return. `grace_seconds` echoes the *configured*
  value; the SPA counts down from the returned `deadline` (server truth —
  client clocks are decoration, enforcement is here).
  - **Re-scan of the same badge = call again = restart**: the write is an
    unconditional overwrite, so "schedule when already scheduled" needs no
    special case and cannot 409. The response deadline strictly moves
    forward (or stays, at zero grace) — Task 5's "restart the countdown" is
    this call.
- **cancel**: set `logoff_deadline = None`, commit, 204 — **idempotent**
  (cancelling an unarmed token is 204, not 404: house style is `logout`'s
  "desired end state, not a client error"; Task 5's [Stay logged in] button
  and any explicit user activity fire it unconditionally).
- **Auth/gates**: `get_current_user` (any role) — 401 unauthenticated, 401
  once the deadline passed (cancel-after-expiry is *supposed* to fail: a
  dead session cannot be resurrected; the SPA treats 401 as "logged off"),
  403 while `must_change_password` (flagged accounts sit on the
  change-password screen, outside the kiosk scan loop — the exempt set is
  deliberately **not** extended).
- **Schemas** (`app/schemas.py`): `LogoffScheduleOut {deadline: datetime,
  grace_seconds: int}` — no input bodies (both endpoints are bodyless
  POSTs; `extra="forbid"` concerns don't arise).

### A4. Edge-case matrix (each row pinned by a test in §C)

| Case | Behaviour |
|---|---|
| Re-scan **same** badge during countdown | SPA calls schedule again → deadline overwritten (restart). Server: idempotent, no 409 |
| Re-scan **different** badge during countdown | SPA does save-then-switch: `qr-login` (new token, `logoff_deadline` NULL by default) → **revoke old token via `logout`** (decision D3: only after the new session is secured) — the row delete takes the armed deadline with it. Belt-and-braces: if the SPA skips the cancel/logout, the armed deadline still kills the abandoned old token at T+30 |
| Schedule when already scheduled | Overwrite = restart (above) |
| Cancel when nothing armed | 204, no-op |
| Cancel/schedule **after** the deadline passed | 401 from `get_current_user` (token already dead + deleted on first presentation) |
| Deadline in the past by construction (zero/negative grace) | Next authenticated request 401s; `<=` boundary pinned |
| Absurd grace (> token TTL) | Clamped to `expires_at` |
| `change-password` while armed | Other sessions' rows are deleted (existing behaviour — armed deadlines die with them); the current session keeps its deadline (server stays mechanical; Task 5 cancels on explicit user action — flagged as a Task-5 note, not server logic) |
| Fresh logins (`login`, `qr-login`, `register`) | `_issue_token` untouched → new tokens always unarmed (NULL) |
| Upgrade of a live database | Migration adds NULL to every row → nobody is logged off by deploying this |

## B. Same-user badge check — `POST /api/auth/qr-identify`

```python
# app/schemas.py
class QrIdentifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=64)   # same bounds as QrLoginIn

class QrIdentifyOut(BaseModel):
    same_user: bool
```

```python
# app/routers/auth.py
@router.post("/qr-identify", response_model=QrIdentifyOut)
def qr_identify(payload: QrIdentifyIn, user: User = Depends(get_current_user)):
    """Is this scanned badge the CALLER's own? — the same-user question the
    kiosk flow needs before choosing logoff-screen vs save-then-switch.

    Authenticated, side-effect-free (no session issued, no writes, nothing
    armed), and enumeration-safe: every non-matching input — garbage, a
    product barcode, somebody else's badge — answers the same
    {same_user: false}; anonymous callers get 401 and learn nothing."""
    badge = _badge_from_scanned(payload.token)
    if badge is None or user.qr_badge_hash is None:
        return QrIdentifyOut(same_user=False)
    return QrIdentifyOut(
        same_user=hmac.compare_digest(token_hash(badge), user.qr_badge_hash))
```

**Shared acceptance rule (anti-drift):** extract the two lines `qr_login`
inlines today — `parse_badge_payload(scanned)`, else `_RAW_TOKEN_RE`
fullmatch → re-prefix — into `_badge_from_scanned(scanned) -> str | None`,
used by **both** `qr_login` and `qr_identify`. One definition of "what counts
as a badge presentation" (the same doctrine Task 3 applied to scan
resolution); every existing qr-login test is the regression gate for the
extraction.

Properties, each pinned:
- **No side effects**: zero `auth_tokens` rows created (DB count assertion),
  the presented token's `logoff_deadline` untouched, no badge rotation.
- **No enumeration**: uniform `false` for garbage/EAN/other-user badges;
  401 unauthenticated; `compare_digest` on the hashes (doctrine parity with
  `verify_password`, costs nothing); a badgeless account answers `false` to
  everything (`user.qr_badge_hash is None` short-circuit — `compare_digest`
  against None would raise).
- **Gate**: not added to `MUST_CHANGE_EXEMPT_PATHS` → flagged accounts get
  403 (they are on the change-password screen, not in the scan loop).
- Accepts both badge forms (prefixed + raw 43-char) exactly like `qr-login`.

**Justification vs qr-login-then-revoke** (in phase 2 I argued *against* a
`qr-identify` endpoint and for "call qr-login, compare ids, revoke the spare
token" — the coordinator's D6 design changes the calculus, and I now argue
the other side; recording the flip honestly):

1. **The same-user path must ARM THE CURRENT TOKEN.** Login-and-revoke mints
   a *new* token — useless for arming the session the kiosk actually holds —
   so a separate schedule call is needed anyway. Identify + schedule is the
   minimal correct decomposition; login-and-revoke + schedule does the same
   job with two extra writes and a transient session.
2. **No transient live credential.** Login-and-revoke creates a fully valid
   7-day token whose raw value crosses the SPA, to be revoked moments later;
   if the revoke call fails (dropped request, closed tab), a spare live
   session silently persists. Identify issues nothing — there is no failure
   state to clean up.
3. **No table churn** on a shared counter where same-badge re-scans are the
   common gesture (each would insert+delete an `auth_tokens` row).
4. **Semantics**: "is this my badge?" is a question; login-and-revoke is a
   side-effectful approximation of it. The repo consistently prefers the
   explicit contract (stock-adjust vs PATCH; the Task-3 shared decision
   core).
5. My phase-2 objection was to an **unauthenticated** probe endpoint; this
   one is authenticated and answers only a boolean about the *caller's own*
   badge — no third-party information exists to enumerate.

Task 5's composition (for context, not this task): badge scan →
`qr-identify` → `same_user: true` ⇒ `logoff-schedule` + countdown UI
([Stay logged in] → `logoff-cancel`; expiry → `logout`); `false` ⇒
save-then-switch (`qr-login` → secure new session → `logout` old →
load new cart).

## C. Tests

**New `tests/test_logoff.py`** (fixtures `client`, `admin_token`,
`create_user`, `auth`; **zero sleeps** — expiry via
`monkeypatch.setattr("app.routers.auth.LOGOFF_GRACE_SECONDS", 0)`):

1. `test_schedule_arms_only_the_presented_token` — two logins for one user;
   schedule on A → A's row has a deadline ≈ now+30 (DB check via
   `Session(engine)`), B's stays NULL; B still 200 on `/me`; response carries
   `deadline` + `grace_seconds: 30`; A still 200 while inside the window.
2. `test_expired_deadline_401s_and_deletes` — grace 0 → schedule → next
   `/me` 401 with the *same* detail as an expired token ("Token invalid or
   expired" — indistinguishable, pinned); the row is **gone** from
   `auth_tokens` (reject-and-delete pin); presenting again → 401.
3. `test_deadline_kills_every_path_including_exempt` — grace 0 → `/me`,
   `/logout`, `/change-password` all 401 (exempt set unchanged).
4. `test_cancel_disarms_and_is_idempotent` — schedule → cancel → 204, DB
   NULL, `/me` 200 past what would have been the deadline (grace 0 +
   cancel → still alive: the pin that cancel, not time, decides); second
   cancel → 204 (no 404).
5. `test_reschedule_restarts` — schedule (grace 5) → schedule (grace 30) →
   response deadline moves out; DB holds the second value (restart
   semantics, no 409).
6. `test_deadline_clamped_to_token_expiry` — monkeypatch grace to
   10**9 → DB deadline == the row's `expires_at`.
7. `test_schedule_after_expiry_is_401` — grace 0, schedule, then
   `logoff-cancel` → 401 (a dead session cannot be resurrected).
8. `test_logout_and_password_change_take_armed_deadlines_with_the_row` —
   armed token + `logout` → row gone; armed *other* session +
   `change-password` → other row gone (existing revocation, deadline rides
   along), current session untouched.
9. `test_gates` — unauthenticated schedule/cancel → 401;
   `must_change_password` token → 403 on both (gate applies; exempt set
   unchanged).
10. `test_fresh_logins_are_unarmed` — password login, qr-login, register →
    new rows have NULL deadline (default pin).
11. `test_boundary_is_inclusive` — grace 0 arms `deadline == now`; the very
    next request already 401s (`<=` semantics, distinct from `expires_at`'s
    `<`).

**`tests/test_qr.py` additions** (qr-identify; existing suites stay green —
they are the `_badge_from_scanned` extraction's regression gate):

12. `test_identify_own_badge_both_forms` — full `HCRM1:` payload and raw
    43-char token → `{same_user: true}`; **no side effects**: `auth_tokens`
    count unchanged, presented token's `logoff_deadline` still NULL.
13. `test_identify_is_uniform_false` — another user's badge, a product EAN,
    garbage, an over-long token → all `{same_user: false}`, same status 200
    (no enumeration channel); badgeless account (revoke first) → `false`.
14. `test_identify_requires_auth` — anonymous → 401 (nothing probeable).
15. `test_identify_follows_rotation` — rotate → old payload `false`, new
    `true`; after revoke → `false`.
16. `test_identify_respects_the_password_gate` — flagged token → 403.

**`tests/test_migrations.py`** (conscious edits, first since round 4):

17. exact-set test += `("auth_tokens", "logoff_deadline")`; legacy-DB
    landing test asserts the column arrives NULL on pre-existing rows and
    re-running is idempotent; malformed-entry guard untouched.

**Suite hygiene:** full `uv run pytest` green; `tests/test_auth.py` /
`test_concurrency.py` / `test_password_policy.py` unchanged (they exercise
token flows that all keep working — armed-deadline rows are opt-in);
README config table gains the `HCRM_LOGOFF_GRACE_SECONDS` row (rides with
this PR; bigger docs are Task 6).

## D. Files touched

| File | Change |
|---|---|
| `app/models.py` | `AuthToken.logoff_deadline` (+ doctrine comment) |
| `app/main.py` | one appended `COLUMN_MIGRATIONS` entry (no post-statements, **no index**) |
| `app/security.py` | `LOGOFF_GRACE_SECONDS` (env `HCRM_LOGOFF_GRACE_SECONDS`, default 30, clamped ≥ 0) |
| `app/deps.py` | deadline check after `expires_at`: reject-and-delete, same 401 detail |
| `app/routers/auth.py` | `_badge_from_scanned` extraction (used by `qr_login` + new `qr_identify`); `POST /logoff-schedule`, `POST /logoff-cancel` |
| `app/schemas.py` | `LogoffScheduleOut`, `QrIdentifyIn`, `QrIdentifyOut` |
| `tests/test_logoff.py` | new (cases 1–11) |
| `tests/test_qr.py` | cases 12–16 |
| `tests/test_migrations.py` | case 17 (exact-set + landing) |
| `README.md` | config-table row |
| *(untouched)* | `_issue_token`, `logout`, `MUST_CHANGE_EXEMPT_PATHS`, `INDEX_MIGRATIONS`, cart/scanner routers, SPA |

## E. Risks & open questions

1. **Write on a read path** (reject-and-delete): fires once per killed
   token, never per request — the cart-reap precedent; the deps comment must
   say so explicitly because `app/database.py`'s incident note makes every
   reviewer (rightly) allergic to read-path writes.
2. **Monkeypatch-ability pins the import style**: `LOGOFF_GRACE_SECONDS`
   must be a module-level name in `auth.py` referenced at call time. A
   refactor to `security.LOGOFF_GRACE_SECONDS` attribute access would break
   the test override *silently for the cancel tests* and loudly for the
   expiry test — acceptable guard, documented here.
3. **Same-detail 401** ("Token invalid or expired") is spec-literal
   ("treat as expired") and leak-free, but Task 5 cannot distinguish
   countdown-logoff from TTL expiry *from the response* — it doesn't need
   to (the SPA knows it armed). If the owner later wants a distinct kiosk
   message, one detail string changes; flagged, not done.
4. **Clock authority**: enforcement uses the server clock only; the
   schedule response's `deadline`/`grace_seconds` let the SPA render without
   trusting client time. Skew shows up only as a UX drift of the countdown
   display, never as a security gap.
5. **Dangling armed sessions**: a token armed and never presented again
   (browser closed, user never returns) lingers as an inert row until the
   next login's `expires_at` purge (≤ 7 days). Chosen over extending the
   purge (login-path scan growth) — inert means unusable: any presentation
   401s and deletes. Documented; revisit only if row counts ever matter.
6. **Phase-B compatibility (deferred relay)**: the counter-session design's
   `pending_logoff_at` maps onto this same column for adopt-issued tokens —
   Task 4 is the mechanism Phase B reuses, not a competing one.
7. **Open question for Task 5/coordinator**: should *any* authenticated
   kiosk activity auto-cancel an armed deadline server-side (activity
   detection), or is cancel purely explicit (button / SPA-side activity
   hook)? Task 4 stays mechanical — explicit endpoints only; auto-cancel
   would put policy in `get_current_user` and is not needed for D6.
8. **Open question**: `qr-identify` reveals to a *stolen session* whether a
   presented badge belongs to that session's user. Negligible next to what a
   stolen session can already do (`/me`, cart, badge rotate); noted for the
   security review pass (Task 6).
