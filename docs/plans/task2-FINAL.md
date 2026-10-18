# Task 2 FINAL spec — Auto QR badge at account creation

Merged from `task2-proposal-{glm,kimi,qwen}.md` + reviews. Implementer follows THIS.

## Helper (`app/qrbadge.py`)

Add one shared helper (the security-critical generate+hash pair must not drift):

```python
def new_badge_for(user) -> str:
    """Generate a fresh badge for ``user`` and store only its hash.

    Nothing is committed here — the caller's transaction decides. Call
    BEFORE adding the user (generate-before-commit, return-after-commit):
    the hash rides in the same INSERT as the account, so a raced/failed
    insert rolls back both the row and the never-shown payload.
    """
    payload = new_badge_payload()
    user.qr_badge_hash = token_hash(payload)
    return payload
```

- Import `token_hash` from `app.security` at module level (the import graph is
  acyclic — `app.security` imports only `app.models`).
- Convert the **two** existing rotate sites (`create_my_badge` in
  `app/routers/auth.py`, `create_member_badge` in `app/routers/members.py`) to
  use `new_badge_for` — in a **separate, droppable commit** within the same PR.

## Endpoint changes

### `register()` (`app/routers/auth.py`)

- After building the `User` (role=member), call `payload = new_badge_for(user)`
  BEFORE `session.add(user)`. The existing commit + `IntegrityError → 409`
  handling is unchanged (a raced insert rolls the hash back with the row).
- Return `RegisterOut` (new), `response_model=RegisterOut`, still 201:
  `RegisterOut(token=..., user=..., qr_badge=QrBadgeOut(payload=payload, svg=badge_svg(payload)))`.
  Render `badge_svg` after the commit in the response expression.

### `create_member()` (`app/routers/members.py`)

- Same: `payload = new_badge_for(user)` before `session.add(user)`.
- Return `MemberCreatedOut` (new) instead of `UserOut`:
  `MemberCreatedOut(**UserOut.model_validate(user).model_dump(), qr_badge=QrBadgeOut(...))`.

## Schemas (`app/schemas.py`)

```python
class RegisterOut(AuthOut):
    qr_badge: QrBadgeOut

class MemberCreatedOut(UserOut):
    qr_badge: QrBadgeOut
```

- `AuthOut` and `UserOut` are **not** touched — badge material cannot appear
  in login/qr-login/`/me`/member-list/PATCH responses (enforced by
  `response_model` + tests).

## Seeds exclusion (structural)

- `seed_default_admin()` and `_ensure_dev_admin()` (`app/main.py`) are
  untouched: they construct `User` directly and never call `new_badge_for`.
- Add a one-line comment to each explaining why: a payload can be shown only
  once; seeds have no viewer; logging it would leak a credential.
- No backfill for existing accounts (their payload could never be shown).

## Tests

- `tests/test_qr.py` (main): register returns a working badge (qr-login in
  both token forms, immediately); create_member returns a working badge under
  the `must_change_password` gate (403 until changed, then works);
  hash-at-rest DB pin (`qr_badge_hash == token_hash(payload)`); leak inventory
  (no `HCRM1:` substring and no badge material in login/qr-login/`/me`/member
  list/PATCH/revoke responses; login keys are exactly `{token, user}`);
  409-race leaves no badge trace (direct DB check); badge uniqueness across
  registrations; creation badge rotates/revokes like any other (rotate kills
  the creation badge); seeded accounts report `has_qr_badge=False` and NULL
  hash.
- `tests/test_dev_admin.py`: extend the boot-report snippet tuple so the
  dev-admin (and default admin) report `has_qr_badge: False`.
- Additive shape assertions in `tests/test_auth.py` /
  `tests/test_password_policy.py` where register/create_member are already
  asserted.

## Out of scope

No migration changes (column + index already exist). Docs (3 lines in
`docs/opticon-m10.md` §9.2 + README) ride with the PR.
