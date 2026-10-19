# Task 2 proposal (glm) — Auto QR badge at account creation

Coordinator Task 2 (`docs/plans/COORDINATOR.md`): `register()` and
`create_member()` generate a badge, returned once (printable SVG); seeds
excluded; tests. **Backend only** — the SPA's print-after-registration modal
is Task 5.

Grounding (current working tree, post-Task-1): `app/qrbadge.py`
(`new_badge_payload`, `badge_svg`, `parse_badge_payload`, `BADGE_PREFIX`),
`app/security.py::token_hash`, `app/routers/auth.py::register` (builds a
`User(role="member")`, catches the email `IntegrityError` → 409, then
`_issue_token` → `AuthOut`, 201), `app/routers/members.py::create_member`
(builds a `User(must_change_password=True)`, same 409 catch, returns
`UserOut`, 201), `app/schemas.py` (`QrBadgeOut {payload, svg}` documented as
shown-exactly-once; `AuthOut {token, user: UserOut}`; `UserOut` carries only
the `has_qr_badge` flag), `app/main.py::seed_default_admin` /
`_ensure_dev_admin` (construct `User` rows directly — they never call the
routers).

---

## 1. Where and how the badge is generated

**At `User` construction time, inside the existing creation transaction —
not as a follow-up write.**

```python
# app/routers/auth.py::register  (and the same two lines in create_member)
badge_payload = new_badge_payload()
user = User(
    email=email,
    name=payload.name.strip(),
    password_hash=hash_password(payload.password),
    role="member",
    qr_badge_hash=token_hash(badge_payload),   # <-- the only change
)
```

…then the response assembles `QrBadgeOut(payload=badge_payload,
svg=badge_svg(badge_payload))` from the still-in-memory plaintext. The raw
payload exists only in the request's local variables; the database sees the
SHA-256 hash, exactly like the existing opt-in endpoints.

Why construction-time rather than generate-then-second-commit:

- **One transaction, one failure domain.** The user row and its badge hash
  become visible atomically. There is never an account that exists without a
  badge, and no second commit whose failure would leave a badgeless account
  requiring a repair path.
- **The IntegrityError race needs no new thought.** A concurrent
  same-email registration fails the *whole* insert — user and badge together
  — and the existing `except IntegrityError → rollback → 409` is untouched.
  (A badge generated after the first commit would have to survive a second
  commit, i.e. a second thing to lose; construction removes the question.)
- Only the SHA-256 hash is persisted; the repo's badge doctrine
  (`docs/opticon-m10.md` §9.4, `QrBadgeOut`'s docstring) is inherited
  verbatim.

`register()` and `create_member()` are the **only** two places that get
these lines — no model default, no shared "create user" helper, nothing the
seed paths could accidentally start calling (see §3).

## 2. Response shape — badge exactly once, by type, not by discipline

Reuse `QrBadgeOut` as-is (it is precisely the shown-once contract:
`{payload, svg}`), and expose it through **two new response schemas**:

```python
# app/schemas.py

class RegisterOut(AuthOut):
    """Registration result: a session plus the account's FIRST badge.

    The badge payload is a bearer credential shown exactly once, here —
    login responses never carry it."""
    badge: QrBadgeOut


class MemberCreatedOut(UserOut):
    """Staff-side account creation result: the account plus its first badge,
    so the counter can print the membership card immediately."""
    badge: QrBadgeOut
```

- `register`'s decorator becomes `response_model=RegisterOut` and it returns
  `RegisterOut(token=…, user=…, badge=QrBadgeOut(payload=…, svg=…))` (or
  builds the `AuthOut` via `_issue_token` and re-wraps — simplest:
  `out = _issue_token(session, user)` then
  `return RegisterOut(**out.model_dump(), badge=…)`).
- `create_member`'s decorator becomes `response_model=MemberCreatedOut`, 201
  unchanged.

Why subclasses instead of an optional `badge: QrBadgeOut | None` on
`AuthOut`/`UserOut` themselves:

- **The type system now enforces "exactly once".** `login`, `qr-login`,
  `/me`, `GET /api/members`, password-change responses keep their current
  models, so badge material *cannot* appear in them — not "should not",
  cannot: populating it would be a type error against the declared
  response_model. With an optional field on the shared schema, every future
  endpoint returning `AuthOut` would be one careless `badge=…` away from a
  leak, and reviewers would have to check each call site instead of one
  decorator.
- The field is **required** on both subclasses (registration/creation always
  generates — that is the task), so a code path that somehow skipped
  generation fails loudly at response-build time rather than silently
  returning `badge: null`.
- Additive: existing tests that assert field-wise on register/create
  responses (`test_auth.py`, `test_password_policy.py`,
  `conftest.create_user` — which only reads `must_change_password` and the
  login that follows) keep passing; nothing existing consumes the new field.

`has_qr_badge` on `UserOut` remains the only persisted signal (a property on
`User`: `qr_badge_hash is not None`) — after either creation path, `/me` and
the members list report `True`, and the list still carries **no** payload or
SVG.

## 3. Seeds exclusion — structural, plus a test that pins it

The seeded accounts (`seed_default_admin` → `admin@shop.local`,
`_ensure_dev_admin` → `admin`) are constructed directly in `app/main.py` and
never pass through `register()`/`create_member()`. The guarantee is
therefore **structural**, and the proposal keeps it that way:

- Badge generation is inline in exactly the two router functions (the two
  lines from §1) — deliberately *not* extracted into a
  `create_user_with_badge()` helper that a third caller (a seed, a script, a
  future import tool) could pick up. Two copies of two lines is cheaper than
  one shared function that silently widens the blast radius.
- Rationale for excluding seeds (documented in the PR, echoed from the
  phase-2 plans): the payload can only be *shown* at generation time; a seed
  would either print a bearer credential into the server log (worse than no
  badge) or store a hash nobody can ever present (dead weight). Staff issue
  admin badges on demand via the existing `POST /api/members/{id}/qr-badge`.
- **Pinned by tests** (§5): after a fresh seed, both seeded accounts must
  report `has_qr_badge=False` / `qr_badge_hash IS NULL`. If someone later
  "helpfully" routes seeding through the badge-generating path, the suite
  fails.

## 4. Edge cases

| Case | Handling |
|---|---|
| **Email race on `register`** | Badge hash is part of the `User` insert; the raced insert fails atomically with the account, the existing 409 path is unchanged, and no orphan badge row can exist (the badge lives in a column of the user row). |
| **Email race on `create_member`** | Same — same construction, same existing catch. |
| **Partial failure between user-commit and token issue (`register`)** | Unchanged from today: `_issue_token` is a second commit. If it fails, the account *with its badge* exists and the user logs in normally; the badge is not lost (it was committed with the account). The one-time display is lost with the failed request, but the self-service rotate endpoint (`POST /api/auth/qr-badge`) already covers "I never saw my badge" — document that in the PR as the recovery path. |
| **`has_qr_badge` correctness** | Set at construction → `True` from the first `/me`. Test asserts it for both paths. |
| **Rotation/revocation** | The creation badge is simply the *first* badge: `POST /api/auth/qr-badge` (self) or `POST /api/members/{id}/qr-badge` (staff) replaces the hash and thereby revokes it — existing, tested semantics; one test pins that the creation badge dies on rotate. |
| **Staff-created account is `must_change_password=True`** | The badge *works* for `qr-login` (issuing a session) but the session is gated to password-change endpoints — existing `get_current_user` behaviour, unchanged. Worth one test line so the interaction is deliberate, not accidental: badge-login of a flagged staff-created account → 200 with `must_change_password: true`, API blocked until changed. |
| **Response carries a bearer credential** | Same exposure class as the existing rotate endpoints: shown once over the same channel that just authenticated (register: the requester's own credential; create_member: a staff session — staff could already print member badges via the existing endpoint, so no privilege change). Never logged; the PR adds no logging of response bodies (and none exists today). |
| **OpenAPI surface** | `RegisterOut`/`MemberCreatedOut` are new schemas; `tests/test_metadata_and_ops.py` has no exact OpenAPI snapshot to refresh (verified: it asserts docs kill-switch, token index, repo hygiene — not a schema inventory), but run it anyway as part of the suite. |
| **Conftest `create_user` helper** | Posts to `/api/members` and asserts `must_change_password is True` field-wise — unaffected by the additive field; no change needed. |

## 5. Tests

**`tests/test_qr.py`** (primary home — badge lifecycle already lives here):

1. `test_register_returns_working_badge` — `POST /api/auth/register` → 201;
   response has `token`, `user`, and `badge` whose `payload` passes
   `parse_badge_payload` and whose `svg` is the well-formed inline SVG
   (reuse the existing SVG assertions); that payload logs in via
   `POST /api/auth/qr-login` (both the full `HCRM1:…` form and the raw
   43-char token); `/me` with the new session shows `has_qr_badge=True`.
2. `test_staff_create_returns_working_badge` — same assertions for
   `POST /api/members` (admin token), including that the badge logs in and
   the session reports `must_change_password=True` (the flagged-account
   interaction, deliberately pinned).
3. `test_creation_badge_dies_on_rotate` — register → `POST /api/auth/qr-badge`
   → the creation payload now 401s on `qr-login`, the new one works (the
   creation badge is just the first badge).
4. `test_seeded_accounts_have_no_badge` — after the `client` fixture's fresh
   seed: log in as the default admin (pre-change token, per the
   `admin_token` fixture) → `/me` reports `has_qr_badge=False`; and a direct
   `Session(engine)` check that both seeded emails have `qr_badge_hash IS
   None` (covers `seed_default_admin`; `_ensure_dev_admin` is off in the
   suite — its exclusion is asserted by the structural argument plus, if
   desired, one line in `tests/test_dev_admin.py`'s subprocess check:
   `/me` of the dev admin reports `has_qr_badge=False`).
5. `test_no_badge_material_in_normal_responses` — for `POST /api/auth/login`,
   `POST /api/auth/qr-login`, `GET /api/auth/me`, and `GET /api/members`:
   `"badge" not in body`, `"payload" not in body`, `"svg" not in body` (the
   members list *does* show `has_qr_badge: True` for badge-holding accounts —
   assert that too, so the flag/no-material split is pinned).

**`tests/test_auth.py`** (register shape lives here):

6. Extend the existing register test: response contains `badge` with both
   keys; `user.has_qr_badge is True` in the register response itself (the
   `UserOut` embedded in `RegisterOut` is serialised after the commit, so
   the property is already `True`).

**`tests/test_password_policy.py`** (create_member shape lives here —
`test_staff_created_account_is_flagged_until_changed` and
`test_staff_created_staff_account_is_flagged_too`):

7. Additive assertion in those tests (or one new sibling): the 201 response
   carries `badge` and `has_qr_badge=True`.

**Suite hygiene:** full `uv run pytest` green; `conftest.create_user`
unaffected (field-wise assertions only — verified).

## 6. Files touched (review checklist)

| File | Change |
|---|---|
| `app/routers/auth.py` | `register`: +2 lines at construction, response_model/return → `RegisterOut` |
| `app/routers/members.py` | `create_member`: same two lines, response_model/return → `MemberCreatedOut` |
| `app/schemas.py` | + `RegisterOut(AuthOut)`, `MemberCreatedOut(UserOut)` (reuse `QrBadgeOut`) |
| `tests/test_qr.py` | + tests 1–5 above |
| `tests/test_auth.py`, `tests/test_password_policy.py` | + additive shape assertions (6–7) |
| *(untouched, deliberately)* | `app/main.py` seeds, `app/qrbadge.py`, `app/security.py`, `app/models.py`, migration tuples (no schema change), the two existing badge endpoints, the SPA |

Single small PR; no migration; no model change; the only behavioural change
an existing client can observe is the additive `badge` field on the two
creation responses.
