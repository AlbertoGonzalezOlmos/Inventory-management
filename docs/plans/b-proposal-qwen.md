# Task B proposal (qwen3.8-max) — Phone number + email change (backend only)

Implements `docs/plans/COORDINATOR2.md` decisions 3–4 + Task B, per the
tasking prompt (which is authoritative where plans diverged — note it
**reverses** my plan-2-review concession: email change DOES revoke other
sessions, "mirror change-password exactly").

**Verified this pass:** `User` has no phone; `UserOut {id, name, email, role,
must_change_password, has_qr_badge, created_at}`; `MemberIn {name, email,
password, role}` / `MemberPatch(_PatchIn) {name, role, password}` —
`_PatchIn` rejects explicit nulls (omit = unchanged), and the `""`-clears
precedent lives in `ItemPatch.barcode` via `_normalized_gtin_or_none`;
`change_password` (`auth.py:337–372`): verify current → reject no-op (B9/B10
lesson) → hash → **revoke-every-other-session loop keyed on
`token_hash(credentials.credentials)`** → commit, 204; `update_member`
applies `payload.model_dump(exclude_unset=True)` through a `setattr` loop
(so a schema-validated `phone` flows automatically); `COLUMN_MIGRATIONS`
currently ends at `("users","qr_badge_generation", …)` (5 entries,
exact-set-pinned); Task A pins derivation to `(secret, user_id, generation)`
— email is *not* an input, which this task must not break and should pin.

---

## 1. Model + migration

```python
# app/models.py — User gains (after qr_badge_generation):
    # Contact phone (Task B), stored NORMALISED (optional leading '+',
    # digits only — see app/schemas.py). NULL = not provided. Not unique
    # (households share numbers), not a credential: changing it needs only
    # a live session.
    phone: str | None = Field(default=None, max_length=32)
```

```python
# app/main.py — COLUMN_MIGRATIONS append (exact-set test → six entries):
    # Contact phone (Task B): nullable, normalised in the schemas; existing
    # rows land NULL (not provided) — the upgrade changes nothing.
    ("users", "phone", "VARCHAR(32)", ()),
```

No index (never queried by). `create_all()` covers fresh DBs; the
legacy-DB landing test extends the existing case (column lands NULL).

## 2. Phone normalisation (`app/schemas.py`)

A pure helper beside its `_normalized_gtin_or_none` sibling (same shape,
same doctrine — validate hard, store canonical):

```python
_PHONE_SEPARATORS = str.maketrans("", "", " -.()")   # visual separators

def _normalized_phone_or_none(value):
    """Canonicalise a user-supplied phone: strip separators, keep an
    optional leading '+', require 6–15 digits (coordinator decision 3 —
    E.164's ceiling; the floor admits short local numbers a village shop
    actually uses). Empty/None → None (the ""-clears precedent from
    ItemPatch.barcode). Anything else → ValueError (→ 422): a typo must
    not become a contact identity.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("phone must be a string of digits")
    cleaned = value.strip().translate(_PHONE_SEPARATORS)
    if not cleaned:
        return None
    plus = cleaned.startswith("+")
    digits = cleaned[1:] if plus else cleaned
    if not digits.isdigit() or not (6 <= len(digits) <= 15) or "+" in digits:
        raise ValueError(
            "phone must be 6-15 digits with an optional leading '+' "
            "(spaces, dashes, dots and parentheses are ignored); got "
            + repr(value))
    return ("+" if plus else "") + digits
```

Stored form: `"+34600123456"` / `"5551234567"` — deterministic equality, so
"phone == current" comparisons and any future dedup are exact. **Not
verified** (no SMS/OTP in scope — shape-only; documented).

Schema wiring:
- `UserOut` += `phone: str | None = None` (the shop's own member directory;
  staff visibility via `/api/members` is the accepted trade, decision 3).
- `MemberIn` += `phone: str | None = None` (optional at counter signup) with
  `field_validator(mode="before")` → `_normalized_phone_or_none`.
- `MemberPatch` += `phone: str | None = Field(default=None, max_length=32)`
  with the same before-validator: omit = unchanged (`exclude_unset`),
  explicit `null` = 422 (`_PatchIn`), `""` = clear → None. The
  `update_member` setattr loop carries it with **no router change** — but
  `create_member` gains `phone=payload.phone` in the `User(...)`
  construction.
- **`MemberPatch` never gains `email`** (decision 4: staff may not move a
  member's login identifier — a takeover vector we don't open); it is
  already absent and `extra="forbid"` makes the attempt a 422 — pinned.

## 3. `POST /api/auth/change-phone` (session-only)

```python
class ChangePhoneIn(_Stripped):
    phone: str | None = Field(max_length=32)   # required key; "" clears

    @field_validator("phone", mode="before")
    @classmethod
    def _canonical(cls, v):
        if v is not None and not isinstance(v, str):
            raise ValueError("phone must be a string")
        return _normalized_phone_or_none(v)
```

`POST /api/auth/change-phone` → **200 `UserOut`** (not 204: the SPA refreshes
its cached user from the response — the Task-1 "one payload, no
read-after-write" mutator contract; same for change-email below).

- Auth: `get_current_user` only — **no password** (decision 3: a phone is
  contact info, not a credential; same exposure class as saying it at the
  counter). Flagged for the owner: adding `current_password` is a one-field
  flip.
- Body: set `user.phone = payload.phone` (None clears), commit, return
  `UserOut.model_validate(user)`.
- **No-op is accepted** (phone == current → 200): unlike email there is no
  credential semantics and no revocation side effect, so a repeated submit
  is harmless (contrast §4's no-op 400 — the asymmetry is deliberate and
  commented).
- Invalid → 422 (pydantic); unauthenticated → 401; **flagged → 403** (§6).

## 4. `POST /api/auth/change-email` (password-gated identity change)

```python
class ChangeEmailIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_email: str = Field(min_length=3, max_length=255, pattern=EMAIL_PATTERN)
    current_password: str = Field(min_length=1, max_length=128)
```

Flow — each step mirrors an existing, tested pattern:

1. `verify_password(payload.current_password, user.password_hash)` → 400
   **"Current password is incorrect"** (change-password's exact message —
   one wording for one failure, no oracle).
2. `email = payload.new_email.strip().lower()` (register's normalisation).
3. **No-op → 400** `"New email must differ from the current email"` —
   mirrors change-password's no-op rejection (B9/B10 lesson: a no-op
   "change" must not return 200 looking like one; here it would also churn
   the session revocations for nothing).
4. Uniqueness pre-check (`select(User).where(User.email == email)`) → 409
   `"Email already registered"` (register's exact message — the register
   enumeration oracle is an already-accepted tracked issue; this is the
   same class, same string, no new channel), then the **`IntegrityError`
   race catch → rollback → same 409** (register's pattern verbatim: the
   pre-check is courtesy, the unique index is the law).
5. `user.email = email`; **revoke every OTHER session, keep the presented
   one** — the change-password loop copied exactly (`current_hash =
   token_hash(credentials.credentials)`; delete all `AuthToken`s of
   `user.id` where `token != current_hash`); single commit.
6. `must_change_password` untouched; the badge is untouched — see §5.
7. Return **200 `UserOut`**.

Signature carries `credentials: HTTPAuthorizationCredentials | None =
Depends(bearer_scheme)` + `user` + `session` (change-password's exact
dependency set — the revocation loop needs the presented token).

Rationale for revocation (decision 4, restoring my plan-2 position): the
email is the *login identifier* — after it moves, sessions minted under the
old identity context should not silently persist on other devices; the
password was just verified, so this is not attacker-friction but
identity-hygiene, and it matches what `change-password` already taught users
("other sessions were signed out" — Task D reuses that toast).

## 5. Badge independence (Task A cross-pin)

Derivation keys on `(secret, user_id, generation)` — nothing in this task
touches `qr_badge_hash`/`qr_badge_generation`, and `user.id` never changes.
Pinned twice: Task A's `test_email_change_never_touches_the_badge`
(DB-level rename → identical re-view) already exists; this task adds the
**endpoint-level** pin (§7, case B7): change-email → `GET /api/auth/qr-badge`
returns the identical payload/generation and the badge still qr-logins.

## 6. Gating decision (the prompt's "flagged flippable")

**Neither endpoint joins `MUST_CHANGE_EXEMPT_PATHS`** → a flagged account
gets 403 on both, exactly like every other non-exempt path (zero code; the
existing gate does it). Justification: a flagged account is mid-mandatory
password-change (staff-issued credential); letting it mutate identity/
contact fields before clearing the flag widens the pre-hygiene surface for
no user benefit (the change screens are one password-change away), and the
exempt set stays minimal-by-doctrine (`me`/`logout`/`change-password` only).
Flip cost if the owner disagrees: two path strings + two test updates.

## 7. Edge-case matrix

| Case | Behaviour |
|---|---|
| email == current | **400** (no-op must not look like a change; no revocation churn) |
| phone == current | **200** (harmless idempotent; no side effects to protect) |
| `phone: ""` | clears → NULL (barcode precedent) |
| `phone: null` (change-phone body) | clears too — `ChangePhoneIn` is not `_PatchIn` (single required field; `""` and `null` both mean clear; documented) |
| `phone` omitted from `MemberPatch` | unchanged (`exclude_unset`); explicit `null` → 422 (`_PatchIn`) |
| email taken (pre-check) | 409 register-identical message |
| email race (concurrent) | `IntegrityError` → rollback → same 409 |
| email wrong password | 400 change-password-identical message |
| malformed email | 422 (`EMAIL_PATTERN`, pydantic) |
| flagged account | 403 on both (gate, §6) |
| staff PATCH member email | 422 (`extra="forbid"` — field absent by design) |
| login with old email after change | 401 "Invalid email or password" (uniform — no existence leak beyond the accepted register oracle) |
| other sessions after email change | revoked, presented survives (change-password parity) |
| other sessions after phone change | untouched (no credential semantics) |
| badge after either change | untouched (§5) |

## 8. Tests — new `tests/test_profile.py`

Fixtures/helpers from `conftest` (`client`, `admin_token`, `create_user`,
`auth`, `unique_email`); `_flagged_token` from `test_cart`.

**Phone normalisation (via the endpoint + direct helper):**
1. P1 happy forms: `"+34 600 123 456"` → `+34600123456`;
   `"(555) 123-4567"` → `5551234567`; `"555.123.4567"` → `5551234567`;
   response is 200 `UserOut` with the normalised value; DB stores it
   verbatim; `/me` reflects it.
2. P2 invalid → 422 each: letters (`"CALL-ME"`), 5 digits, 16 digits,
   interior `"+"` (`"555+1234"`), `"++555123"`, `"-"` only, non-string.
3. P3 `""` clears (→ `phone: null`); clearing an already-empty phone → 200
   (idempotent); `phone == current` → 200 no-op.
4. P4 auth: unauthenticated → 401; flagged → 403 (§6 pin); **no password
   field exists in the schema** (`extra="forbid"` → a `current_password`
   key is 422 — pins the session-only contract).
5. P5 staff: `PATCH /api/members/{id} {phone}` works (setattr flow), `""`
   clears, invalid → 422; `PATCH` with `{"email": …}` → **422** (never
   editable by staff); `POST /api/members` with/without phone →
   `MemberCreatedOut.phone` present/null (badge fields unaffected).

**Email:**
6. E1 happy: 200 `UserOut` with the new lowercased email; login with the
   NEW email works; the OLD email 401s (uniform message).
7. E2 **badge independence**: before/after `GET /api/auth/qr-badge` →
   identical payload + generation; the badge still qr-logins (the Task-A
   cross-pin, endpoint level).
8. E3 wrong password → 400 with change-password's exact message; the email
   is unchanged in the DB.
9. E4 no-op (new == current, incl. case/space variants `"Me@X.com "` vs
   stored `me@x.com`) → 400; no sessions revoked (second session still
   alive — pins that the no-op is rejected *before* the revocation loop).
10. E5 taken email → 409 with register's exact message; DB unchanged.
11. E6 revocation parity: two live sessions; change-email on A → B is 401
    on `/me`, A is 200 (mirrors `test_change_password_flow`'s assertions).
12. E7 malformed email → 422 (`EMAIL_PATTERN`).
13. E8 flagged → 403 on change-email.
14. E9 `ChangeEmailIn` extra-field → 422 (`extra="forbid"`).

**Migration/shape:**
15. M1 `test_migrations.py`: exact-set gains `("users","phone")` ("six
    columns, one index" docstring); legacy DB lands `phone` NULL.
16. M2 existing suites green **unchanged** — explicitly re-check the
    key-set pins: Task 2's leak-inventory asserts login top-level keys
    `{token, user}` (unaffected: `phone` lives inside `user`), identify's
    `{same_user}` exact-body pin (unaffected); `conftest.create_user`'s
    field-wise assertions (unaffected).

## 9. Files touched

| File | Change |
|---|---|
| `app/models.py` | `User.phone` (+ doctrine comment) |
| `app/main.py` | one appended migration entry |
| `app/schemas.py` | `_normalized_phone_or_none`; `UserOut.phone`; `MemberIn.phone`; `MemberPatch.phone`; `ChangePhoneIn`; `ChangeEmailIn` |
| `app/routers/auth.py` | `POST /change-phone`, `POST /change-email` (revocation loop copied from `change_password`) |
| `app/routers/members.py` | `create_member`: `phone=payload.phone` in the constructor (PATCH needs no change — setattr flow) |
| `tests/test_profile.py` | new, cases P1–P5 / E1–E9 / M2 |
| `tests/test_migrations.py` | M1 |
| *(untouched)* | `deps.py` (gate does the flagging), `qrbadge.py`, `security.py`, seeds, SPA (Task D), README/docs (Task F — but the two new endpoints get docstrings stating the decisions) |

## 10. Risks & open questions

1. **No-op asymmetry** (email 400 / phone 200) is deliberate — credential-
   adjacent vs cosmetic — and commented; a reviewer may want symmetry, the
   flip is one branch either way.
2. **`ChangePhoneIn` accepts both `""` and `null` as "clear"** while
   `MemberPatch` rejects explicit nulls (`_PatchIn`): different contracts
   for a single-field body vs a patch document. Documented in both
   docstrings; alternatively make `ChangePhoneIn` reject null — one
   validator line, flagged as taste.
3. **Phone shape-only validation** (no carrier/region truth): a valid-shaped
   wrong number is indistinguishable — accepted (no OTP in scope, decision 3).
4. **Email-change revocation UX**: the owner's other devices die on an
   email change (coordinator decision 4, reversing the plan-2 review
   consensus). Task D must surface it (reuse the "other sessions were
   signed out" toast). Flip cost: delete the loop.
5. **The IntegrityError race branch is structurally untested** (same as
   register's — a true concurrent race is not reproducible in TestClient);
   the pre-check 409 is pinned and the catch mirrors tested code. Noted,
   consistent with the repo's existing posture.
6. **`UserOut.phone` exposure**: every consumer of `UserOut` (login,
   register, `/me`, members list, `CartLineOut`? no — carts embed `ItemOut`
   only) now carries phone. Checked: no `UserOut` consumer leaks it beyond
   authenticated contexts; the anonymous surface never sees `UserOut`.
7. **Forward dependency for Task D**: the SPA caches `USER_KEY`; both
   endpoints returning full `UserOut` is what lets it refresh in one
   assignment — the response choice is load-bearing for Task D, not taste.
