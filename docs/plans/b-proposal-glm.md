# Task B proposal (glm) — Phone + email change (backend only)

Coordinator2 decisions 3–4 + Task B. Two small, self-contained endpoints and
a column — the profile feature's data layer. The security-sensitive part is
**change-email**: email is the login identifier, so it is the takeover
surface and gets the full change-password treatment (current password
required, other sessions revoked); **phone is contact info** and stays
session-only.

Grounding verified: `User` has no phone; `UserOut {id, name, email, role,
must_change_password, has_qr_badge, created_at}`; `MemberIn(_Stripped)
{name, email, password, role}` / `MemberPatch(_PatchIn) {name, role,
password}` (`_PatchIn` rejects explicit nulls, `""`-clears is the
`ItemPatch.barcode` precedent); `ChangePasswordIn` (plain BaseModel,
extra=forbid); `change_password` verifies first, rejects no-ops, and revokes
every OTHER session via the `bearer_scheme` credentials + `token_hash`
presented-token loop; `create_member` constructs `User(...)` explicitly
(so the new field must be added there), `update_member` setattr-loops over
`model_dump(exclude_unset=True)` (so a new `MemberPatch` field flows
automatically); `EMAIL_PATTERN` exists; `COLUMN_MIGRATIONS` is append-only
with the exact-set pin; Task A established that **badge derivation keys on
`user_id`** — an email change can never touch the badge (mechanism-pinned
in `test_badge_derivation.py`; the integration pin lands here).

---

## 1. Model + migration

```python
# app/models.py — User gains:
    # Contact phone, normalised (optional leading '+', 6-15 digits,
    # separators stripped — see schemas). NULL = not set; "" on input
    # clears (the ItemPatch.barcode precedent). NOT unique (families share
    # numbers); NOT verified (no SMS/OTP in scope — shape validation only).
    phone: str | None = Field(default=None)
```

`app/main.py` — one append-only entry:
`("users", "phone", "VARCHAR(32)", ())`. Existing rows land NULL — the
upgrade adds nothing to anyone's profile. `tests/test_migrations.py`:
exact-set += the entry (conscious edit, 6 columns now); legacy DB gains the
column NULL.

## 2. Phone normalisation (`app/schemas.py`, beside `_normalized_gtin_or_none`)

```python
def _normalised_phone_or_none(value):
    """Normalise a phone number: strip whitespace and -(). separators,
    optional single leading '+', then 6–15 digits (E.164-shaped, not full
    E.164 — country codes are not checked; this is shape validation, not
    verification). None/'' → None ("" CLEARS — the ItemPatch.barcode
    precedent). Anything else → ValueError (→ 422)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("phone must be a string")
    value = value.strip()
    if not value:
        return None
    compact = re.sub(r"[\s\-().]", "", value)
    has_plus = compact.startswith("+")
    digits = compact[1:] if has_plus else compact
    if not digits.isdigit() or not 6 <= len(digits) <= 15:
        raise ValueError(
            "phone must be 6-15 digits with an optional leading '+' — got "
            + repr(value)
        )
    return ("+" + digits) if has_plus else digits
```

`"+34 600 123 456"` → `"+34600123456"`; `"(555) 123-4567"` → `"5551234567"`;
`"600123456"` → `"600123456"`; `"++34"`, `"12a45"`, 5 digits, 16 digits →
422. The normalised form is what's stored (one canonical representation;
the SPA displays it verbatim).

## 3. Schemas

```python
class ChangePhoneIn(_Stripped):
    """Set (or clear) the caller's phone. Contact info, not a credential:
    session-only, no password re-entry (coordinator decision 3 — flagged
    flippable by the owner)."""
    phone: str = Field(max_length=32)   # "" clears; validator normalises

    _phone = field_validator("phone", mode="before")(_normalised_phone_or_none)

class ChangeEmailIn(BaseModel):
    """Change the caller's EMAIL — the login identifier and therefore the
    account-takeover surface: the CURRENT PASSWORD is required (a stolen
    session must not be able to move the login identity), and every OTHER
    session is revoked (change-password parity, coordinator decision 4)."""
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=255, pattern=EMAIL_PATTERN)
    current_password: str = Field(min_length=1, max_length=128)
```

- `UserOut` gains `phone: str | None = None` (visible to self via `/me` and
  to staff via the members list — the shop's own member directory;
  documented, same exposure class as email).
- `MemberIn` gains `phone: str | None = None` (+ the same validator):
  staff may set a phone at account creation.
- `MemberPatch` gains `phone: str | None = None` (+ validator): staff may
  edit a member's phone. The `_PatchIn` null-rejection gives the right
  semantics for free — `{"phone": null}` → 422 (omit to leave unchanged),
  `{"phone": ""}` → clears, absent → untouched. **`MemberPatch` does NOT
  gain `email`** (extra="forbid" → any `{"email": …}` attempt is a 422):
  staff changing a member's login identifier without the member present is
  a takeover vector we don't open (coordinator decision 4). `MemberIn.email`
  stays — setting the identifier AT CREATION is not editing it.

## 4. Endpoints (`app/routers/auth.py`)

### `POST /api/auth/change-phone` → 200 `UserOut`

```python
@router.post("/change-phone", response_model=UserOut)
def change_phone(
    payload: ChangePhoneIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Set or clear the caller's phone. Session-only (contact info, not a
    credential — no password re-entry). Idempotent: setting the current
    value again is a 200, not an error."""
```

Body: `{phone: "+34 600 123 456"}` or `{phone: ""}` (clears). Flow:
`user.phone = payload.phone` (already normalised/None) → commit →
`UserOut.model_validate(user)`. No-op `phone == current` → plain 200
(harmless; no sessions are involved). **Not added to
`MUST_CHANGE_EXEMPT_PATHS`** — a flagged account changes its password
first, exactly like every other non-exempt path (qr-badge, cart, logoff).

### `POST /api/auth/change-email` → 200 `UserOut`

Flow (order is load-bearing):

1. **Verify the current password** first (`verify_password`; 400
   "Current password is incorrect" — change-password's exact message).
   Verify-before-uniqueness: the taken-email 409 is only served to callers
   who proved credential possession (the 409 is an enumeration oracle of
   the same class register's already-accepted one; requiring the password
   first keeps it strictly narrower).
2. **Reject the no-op**: `new == user.email` → 400 "New email must differ
   from the current one" (change-password's no-op doctrine — and a no-op
   that proceeded would revoke every other session for nothing).
3. Normalise (`strip().lower()`), uniqueness pre-check → 409 "Email
   already registered".
4. Mutate + **revoke every other session** — mirroring `change_password`
   byte-for-byte: the `bearer_scheme` credentials dependency, the
   `token_hash(credentials.credentials)` presented-token hash, the loop
   deleting every `AuthToken` of the user except the presented one. The
   coordinator's decision 4 (change-password parity). The presented
   session survives; other tabs die with 401 → the SPA's existing
   session-expired handling.
5. `IntegrityError` on commit (the check-then-write race) → rollback →
   409 (register's pattern).
6. Return `UserOut` (the SPA refreshes `USER_KEY` from it — the
   change-password/`must_change_password` precedent).

Not exempt from the pw-change gate (403 while flagged) — same reasoning as
change-phone.

### members.py changes (minimal)

`create_member` constructs `User(...)` explicitly → add `phone=payload.phone`.
`update_member`'s `changes = payload.model_dump(exclude_unset=True)` +
setattr loop handles the new `MemberPatch.phone` with **zero router
changes** (and the existing staff→admin privilege rule applies to phone
edits automatically). No email path anywhere in members.

## 5. Edge cases

| Case | Behaviour |
|---|---|
| `change-phone` no-op (same value) | 200 idempotent |
| `""` phone | clears → NULL (barcode precedent) |
| `{"phone": null}` in MemberPatch | 422 (`_PatchIn` null rule; omit instead) |
| `change-email` no-op (same email) | **400** — a no-op must not revoke sessions |
| wrong current password | 400 (after password verification, before any state change) |
| taken email | 409 (pre-check); the IntegrityError race → rollback → 409 |
| other sessions after email change | revoked (401 on next use); the presented one survives |
| flagged account (`must_change_password`) | 403 both endpoints (not exempt — password first) |
| badge after email change | **untouched** — derivation keys on `user_id`; integration-pinned |
| staff editing member phone | allowed (MemberPatch; the staff→admin rule applies as usual) |
| staff editing member email | impossible — no field, extra=forbid → 422 |
| `UserOut.phone` exposure | self (`/me`) + staff (members list) — the shop's member directory, same class as email |

## 6. Tests

**New `tests/test_profile.py`:**

Phone (~9 cases):
1. Happy path: set `"+34 600 123 456"` → stored/returned `"+34600123456"`;
   `/me` reflects it.
2. Normalisation table: `"(555) 123-4567"` → `"5551234567"`; padded
   `" +34-944-123-456 "` → `"+34944123456"`; plain `"600123456"` unchanged.
3. Invalid → 422: letters, 5 digits, 16 digits, `"++34…"`, `"12 34a"`.
4. `""` clears → `/me` phone `null`.
5. No-op re-set → 200 idempotent.
6. Unauthenticated → 401; flagged → 403.
7. Staff `PATCH /api/members/{id}` sets a member phone; `""` clears;
   `{"phone": null}` → 422.
8. `MemberIn.phone` at staff creation (create_member carries it through).
9. `MemberPatch` rejects `{"email": ...}` → 422 (staff can never edit
   email — the takeover-surface pin).

Email (~8 cases):
10. Happy path: change email → login with the NEW email works, the OLD one
    401s; the response `UserOut` carries the new email.
11. Wrong current password → 400 (and the email is unchanged).
12. Missing password → 422 (schema).
13. No-op (same email) → 400 — **and no sessions were revoked** (a second
    session still works afterwards: the load-bearing consequence).
14. Duplicate email → 409; own email case-insensitively equal → the 400
    no-op path.
15. **Other sessions revoked, presented survives**: two logins; change
    email with A's token → B's next request 401s, A's works, and A's
    badge re-view (`GET /api/auth/qr-badge`) still 200s.
16. **Email change never touches the badge**: `GET /api/auth/qr-badge`
    before and after → byte-identical payload (the Task-A
    cross-referenced integration pin).
17. Case/space normalisation (`" New@Example.COM "` → logs in as
    `new@example.com`); pattern violations → 422.

**`tests/test_migrations.py`:** exact-set += `("users", "phone")` (six
columns); legacy DB gains the column NULL.

Suite hygiene: the members-list tests assert field-wise (no exact-shape
pins) — adding `phone` to `UserOut` breaks nothing; conftest `create_user`
unaffected.

## 7. Files touched

| File | Change |
|---|---|
| `app/models.py` | `User.phone: str \| None = None` |
| `app/main.py` | one `COLUMN_MIGRATIONS` append |
| `app/schemas.py` | `_normalised_phone_or_none`, `ChangePhoneIn`, `ChangeEmailIn`, `UserOut.phone`, `MemberIn.phone`, `MemberPatch.phone` |
| `app/routers/auth.py` | `POST /change-phone`, `POST /change-email` |
| `app/routers/members.py` | `create_member` passes `phone` (update_member: automatic) |
| tests | `test_profile.py` (new, ~17 cases), `test_migrations.py` |

## 8. Risks / flagged decisions

1. **`change-phone` without password** is the coordinator's flagged-flippable
   choice; if the owner disagrees, add the `current_password` field to
   `ChangePhoneIn` (one dependency, three test lines).
2. **Not exempt from the pw-change gate** (my choice): flagged accounts
   change passwords first, consistent with every other non-exempt path.
   The flippable alternative (exempting change-phone) is one line in
   `MUST_CHANGE_EXEMPT_PATHS`.
3. **Email-change session revocation** is the coordinator's decision
   (change-password parity); my Task-A-era preference was no-revoke, and
   the reasoning is recorded there — implemented as decided.
4. **The 409-taken-email oracle** exists behind password verification —
   strictly narrower than register's unauthenticated one (an accepted
   tracked trade-off); noted for the docs task.
5. **Phone is not verified** (shape only) — no SMS/OTP in scope; the
   column docstring says so.
