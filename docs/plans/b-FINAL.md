# Task B FINAL spec — phone + email change

Merged from `b-proposal-{glm,qwen}.md` + `b-review-{glm,qwen}.md` (kimi unavailable).

## Model + migration

- `User.phone: str | None = Field(default=None)` → `COLUMN_MIGRATIONS` entry
  `("users", "phone", "TEXT", ())`.
- `UserOut.phone: str | None`; `MemberPatch.phone: str | None` (staff may edit
  phone; **never** email — `MemberPatch` has no email field and
  `extra="forbid"` turns any attempt into 422).

## Phone normalisation (`app/security.py` or a small helper)

`normalise_phone(value: str | None) -> str | None`:
- `None` or `""` → `None` (clear).
- strip; optional leading `+`; require 6–15 digits after the sign; anything
  else → invalid (422).

## `POST /api/auth/change-phone`

- Session-only, **no password** required. Body `ChangePhoneIn {phone: str | None}`
  (both `""` and `null` clear — a single-field command body has no
  omit-semantics).
- Normalise; `phone == current` → **200** (idempotent no-op). Invalid → 422.
- Returns `UserOut`. Not exempt from the `must_change_password` gate.

## `POST /api/auth/change-email`

- Body `ChangeEmailIn {new_email, current_password}`.
- `verify_password(current_password, user.password_hash)` → 400 on mismatch.
- Normalise email (lower/strip); `email == current` → **400** (a no-op must
  never revoke sessions).
- Uniqueness pre-check + `IntegrityError` → **409** (race-safe).
- **Revoke every other session** — mirror `change_password` exactly (delete all
  `AuthToken` rows for the user except the presented hash).
- Returns `UserOut`. Not exempt from the `must_change_password` gate.
- **Badge untouched** — derivation keys on `user_id`; pin that `GET
  /api/auth/qr-badge` returns the identical payload before/after an email change.

## Tests (`tests/test_profile.py`)

~17 cases: phone normalisation table (valid/invalid/`+`/clear/null), phone
clear + no-op + gates, email happy path, wrong-password 400, no-op-email 400
before any revocation, other-sessions-revoked + presented-survives
(change-password parity), uniqueness 409 (+ IntegrityError branch mirrors
tested code), staff-never-edits-email (422), badge-untouched pin, phone==current
200 idempotent. Extend `test_migrations.py` (exact-set + legacy NULL) and
re-check existing key-set pins (Task-2 leak inventory `{token,user}`, identify
`{same_user}`) stay green.
