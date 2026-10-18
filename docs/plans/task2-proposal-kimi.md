# Task 2 proposal (kimi-k3): Auto QR badge at account creation

Coordinator plan reference: `docs/plans/COORDINATOR.md` Task 2 — "`register()`
/ `create_member()` generate a badge, returned once (printable SVG); seeds
excluded; tests." Verified against the current working tree (Task 1 merged
in-tree; auth/members routers as of HEAD `bfd09fe` + Task 1).

---

## 1. Where and how to generate the badge

Both creation endpoints get the badge generated **before the user INSERT
commits**, so the hash and the account land in one transaction:

### Small shared helper (new, in `app/qrbadge.py`)

The badge-issue pattern (generate payload → store hash → return payload for
one-time display) currently exists **three times, duplicated** verbatim
(`auth.py::create_my_badge`, `members.py::create_member_badge`, and it is
about to be needed twice more). Factor it once:

```python
# app/qrbadge.py
from app.security import token_hash   # no import cycle: security imports
                                      # only app.models; models imports neither

def new_badge_for(user) -> str:
    """Store the hash of a fresh badge on `user`; return the raw payload.

    The payload is a bearer credential shown exactly once (the caller
    returns it in the creation response); only its SHA-256 survives
    server-side. Nothing is committed here — the caller's transaction
    decides whether the hash ever persists (see register's 409 path).
    """
    payload = new_badge_payload()
    user.qr_badge_hash = token_hash(payload)
    return payload
```

(`user` left un-hinted or `TYPE_CHECKING`-hinted to keep `qrbadge.py` free
of a models import; the attribute contract is one line. `app.qrbadge`
importing `app.security` introduces no cycle — `security` imports only
`app.models`.)

The same PR mechanically converts the three existing issue/rotate sites to
the helper (they are covered by the existing lifecycle tests in
`tests/test_qr.py`); the two new sites use it from the start. Routers keep
wrapping the payload in `QrBadgeOut(payload=…, svg=badge_svg(…))`
themselves — `QrBadgeOut` is a schema and must not leak into the
lower-level `qrbadge` module.

### `app/routers/auth.py::register`

Current flow: validate → build `User` → `session.add(user)` →
`try: commit / except IntegrityError: 409` → `refresh` → `_issue_token`
(which commits the `AuthToken` itself).

New flow — insert the badge generation between `add` and `commit`:

```python
    user = User(email=email, name=…, password_hash=…, role="member")
    payload = new_badge_for(user)          # hash set pre-commit
    session.add(user)
    try:
        session.commit()                   # user + badge hash, one transaction
    except IntegrityError:
        session.rollback()                 # hash rolled back with the user;
        raise HTTPException(409, …)        # the in-memory payload is discarded
    session.refresh(user)
    auth_out = _issue_token(session, user)
    return RegisterOut(**auth_out.model_dump(),
                       qr_badge=QrBadgeOut(payload=payload,
                                           svg=badge_svg(payload)))
```

Key point: **generate before commit, return after commit.** The payload and
the persisted hash are derived from the same secret in the same breath; if
the commit fails, nothing was stored and nothing is returned — a badge can
never be "lost" (hash stored, payload never shown), because the payload is
only handed out on the success path.

### `app/routers/members.py::create_member`

Identical insertion point (between `session.add(user)` and the
`try: commit`), then:

```python
    session.refresh(user)
    return MemberCreatedOut(
        **UserOut.model_validate(user).model_dump(),
        qr_badge=QrBadgeOut(payload=payload, svg=badge_svg(payload)),
    )
```

The account is created with `must_change_password=True` (unchanged); the
badge works with `qr-login` immediately, and the resulting token is gated
to the exempt paths until the password change — existing behaviour,
unchanged by this task.

## 2. Response shape — badge shown exactly once, never in login responses

**Subclass with a REQUIRED field, reusing `QrBadgeOut`** (my phase-2
position; qwen/GLM suggested an *optional* field on `AuthOut` — rejected):

```python
# app/schemas.py
class RegisterOut(AuthOut):
    """AuthOut plus the one-shot badge: every registration creates a badge,
    so the field is required here — an optional field would let a code path
    silently forget it. Login responses keep the plain AuthOut shape,
    byte-identical to today."""

    qr_badge: QrBadgeOut

class MemberCreatedOut(UserOut):
    """UserOut plus the one-shot badge for staff-printed membership cards.
    list_members / /me / PATCH keep plain UserOut — badge material must
    never appear in a list or a read."""

    qr_badge: QrBadgeOut
```

Rationale for required-in-subclass over optional-on-`AuthOut`:
1. **The invariant is pinned by the type**: every creation response *must*
   carry a badge; with `qr_badge: QrBadgeOut | None = None` on `AuthOut`, a
   forgotten assignment serialises as `null` and no test may notice.
2. **Login/qr-login/change-password responses stay exactly `AuthOut`** — no
   new nullable key on the hottest auth responses, no client-visible shape
   change, no temptation for a later developer to populate it there.
3. Decorators: register gets `response_model=RegisterOut`, create_member
   gets `response_model=MemberCreatedOut`; FastAPI's response filtering
   then guarantees nothing else can leak even if internals grow fields.

`QrBadgeOut` is reused unchanged (payload + svg), matching the existing
rotate/issue endpoints — the SPA already has the print-modal markup for
exactly this shape (Task 5 wires it into the register/member-create
screens).

## 3. Seeds exclusion — structural, plus a test

Seeded accounts (default admin in `seed_default_admin`, dev admin in
`_ensure_dev_admin`, both in `app/main.py`) construct `models.User`
directly and never pass through the endpoints — so they get no badge
**by construction**, as long as the generation code lives in the two
endpoint functions (and the shared helper is only called from badge-issue
call sites). No code change in `main.py`; no `if seeded:` flag anywhere
(that would be a liability, not a guard).

Why no backfill for pre-existing accounts (already-decided, restated for
the record): the payload can only be shown once at generation, so a
batch-issued hash would be a printable secret nobody can print — dead
weight. Existing accounts keep the opt-in self/staff issue paths.

**Test guarantee** (§5): assert the seeded admin's `has_qr_badge` is
`False` via the API, so a future refactor that routes seeds through a
shared "create user" helper fails loudly if it drags badge generation
along.

## 4. Edge cases

- **IntegrityError race on register (concurrent same-email registration)**:
  the badge hash is rolled back together with the user row (same
  transaction); the losing request answers 409 and never returns its
  payload; the winning request's badge is unaffected. No orphan hash, no
  lost badge. `create_member`'s identical try/except gets the same
  behaviour. (The badge is not the unique-violation source: email is the
  only user table unique constraint involved; `qr_badge_hash` is indexed
  but not unique — 256-bit random tokens make collision a non-consideration,
  same as session tokens.)
- **Partial failure after user commit**: `register`'s second commit
  (`_issue_token`'s `AuthToken` insert) could in principle fail after the
  user+badge hash persisted. Then the account exists with a badge whose
  payload was never shown. This is recoverable, not fatal: the holder logs
  in with password and rotates (`POST /api/auth/qr-badge`), or staff
  re-issue. The payload was never transmitted, so nothing is compromised —
  worst case is dead-weight hash and one extra rotate. Not worth
  re-architecting `_issue_token` for; documented here and in the register
  docstring.
- **`must_change_password` accounts** (create_member): a flagged member
  cannot call `POST /api/auth/qr-badge` (not in
  `MUST_CHANGE_EXEMPT_PATHS` → 403), so if the printed card is lost before
  first login, staff re-issue via `/api/members/{id}/qr-badge` — the
  existing counter flow, unchanged. The auto-badge itself works for
  `qr-login` immediately (login is exempt by nature).
- **`has_qr_badge` correctness**: it is a `@property` derived from
  `qr_badge_hash is not None`; after `refresh(user)` it is `True` in the
  creation response's nested `user`/`UserOut` and in subsequent `/me` —
  no code needed, but a test pins it (a naive `has_qr_badge` field set by
  hand would be a classic desync bug).
- **Well-known-password rejection precedes generation** (both endpoints
  validate before any badge work) — no badge material is generated for
  requests that 400/403/409.
- **No new leak surface**: the payload appears only in the 201 creation
  response bodies. It is never logged (no logging in either endpoint
  today), never in `UserOut`, `GET /api/members`, `/me`, login, or
  qr-login responses. Tests assert absence, not just presence.
- **OpenAPI snapshot**: `test_metadata_and_ops.py` — check whether the
  response-model change on the two endpoints affects any snapshot/
  enumeration; refresh expectations if (and only if) that file asserts
  them.

## 5. Tests

### `tests/test_qr.py` (main coverage)

1. `test_register_returns_working_badge` — register → 201; body has
   `qr_badge.payload` (parses via `parse_badge_payload`, `HCRM1:` prefix)
   and `qr_badge.svg` (well-formed, `<svg`…`</svg>`); nested `user.
   has_qr_badge is True`. The payload immediately logs in via
   `POST /api/auth/qr-login` (both prefixed and raw-token forms, mirroring
   the existing lifecycle test); the resulting token passes `/me`.
2. `test_register_badge_shown_exactly_once` — a subsequent password login
   for the same account returns a body **without** any `qr_badge` key;
   `/api/auth/me` has `has_qr_badge: true` but no badge material; the
   string `HCRM1:` appears nowhere in login/me bodies.
3. `test_create_member_returns_working_badge` — staff creates a member →
   201 with `qr_badge`; the badge works with `qr-login` (token issued);
   with that token, `/me` (exempt path) shows `has_qr_badge: true` and
   `must_change_password: true`; a non-exempt endpoint 403s (existing
   gate, one assertion to pin the interaction).
4. `test_member_list_and_gets_carry_no_badge_material` — `GET /api/members`
   (staff) contains `has_qr_badge: true` for the created member but no
   `qr_badge` key and no `HCRM1:` substring anywhere in the body.
5. `test_register_409_discards_badge` — register email A (badge B1 works);
   re-register email A → 409, response has no `qr_badge`; B1 still works;
   the original account is untouched.
6. `test_auto_badge_rotates_and_revokes` — starting from an auto-issued
   badge (not the manual flow the existing lifecycle test covers):
   rotate → old payload 401s, new works; revoke → 401. (Guards against the
   auto-badge path diverging from the opt-in one.)
7. `test_seeded_admin_has_no_badge` — with `admin_token` (the seeded
   `admin@shop.local`): `/me` → `has_qr_badge is False`; and in
   `GET /api/members` the seeded admin entry has `has_qr_badge: false`.
   (Structural exclusion, pinned.)

### `tests/test_auth.py` (shape pinning)

8. Register response contains `qr_badge`; login response for the same
   account does **not** (keys compared exactly). Existing field-wise
   assertions stay green (additive subclass, login untouched).

### Members creation tests (wherever `create_member` is asserted today —
`tests/test_security.py` / `tests/test_concurrency.py` role paths)

9. Response now carries `qr_badge` (additive); privilege matrix unchanged
   (member caller → 403 on `POST /api/members`; staff cannot create admin;
   the admin-created-by-admin case also gets a badge).

### Dev admin mode

10. `tests/test_dev_admin.py` (subprocess with `HCRM_DEV_ADMIN=1`): the
    seeded dev admin reports `has_qr_badge: false` (one line in the
    existing subprocess scenario — the flag is import-time, so this cannot
    ride the main process).

### Suite hygiene

- Full `uv run pytest` green; `test_metadata_and_ops.py` checked for
  OpenAPI/response-model snapshots and refreshed only if it pins them.

## 6. Files touched (review checklist)

| File | Change |
|---|---|
| `app/qrbadge.py` | + `new_badge_for(user)` helper (imports `token_hash`) |
| `app/schemas.py` | + `RegisterOut(AuthOut)`, + `MemberCreatedOut(UserOut)` |
| `app/routers/auth.py` | register: generate pre-commit, return `RegisterOut`; `create_my_badge` uses the helper |
| `app/routers/members.py` | create_member: generate pre-commit, return `MemberCreatedOut`; `create_member_badge` uses the helper |
| `app/main.py` | **untouched** (seeds excluded structurally) |
| migration tuples | **untouched** (no schema change — `qr_badge_hash` column already exists) |
| `tests/test_qr.py`, `test_auth.py`, member-creation tests, `test_dev_admin.py` | cases above |

## 7. Risks / open questions

- **R1 — Shoulder-surfing at the counter**: the register/member-create
  responses now contain a printable bearer credential once. Same exposure
  class as the existing rotate/issue flows; the Task-5 UI must show the
  print modal with a "shown only once" warning and must not persist the
  payload (no localStorage). Task 2 backend-only; flagged for Task 5.
- **R2 — Payload in access logs / error trackers**: response bodies are not
  logged by uvicorn/FastAPI defaults; if the deployment adds body-logging
  middleware later, `qr_badge` paths must be excluded — one doc line for
  Task 6 (docs task).
- **Q1 — Required vs optional `qr_badge` in the subclasses**: I argue
  required (pins the invariant). If the coordinator anticipates
  badge-less creation modes later (e.g. a `?badge=false` for bulk
  imports), optional-in-subclass is the cheaper future move; out of scope
  now — no such mode exists or is planned in the coordinator plan.
