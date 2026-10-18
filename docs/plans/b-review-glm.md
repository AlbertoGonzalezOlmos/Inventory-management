# Task B cross-review (glm) — of `b-proposal-qwen.md` (kimi unavailable)

Reviewer context: my own proposal is `docs/plans/b-proposal-glm.md`. The two
proposals converged on nearly everything — the normaliser's shape and
semantics, the `""`-clears precedent, staff-never-edits-email via
`extra="forbid"`, `create_member`-only constructor change, the gate decision
(403 for flagged, no exemptions), email no-op → 400 / phone no-op → 200, the
revocation mirror, the badge-independence pin, `UserOut` returning endpoints
for Task D's one-assignment refresh. The review arbitrates the three
prompt-flagged points and a handful of qwen's additions.

## The flagged points

**1. phone==current 200 vs email==current 400 — converged, deliberately
asymmetric, keep.** Both proposals justify it identically: the email no-op
must be rejected because a "successful" change would revoke every other
session *for nothing* and return 200 looking like a change (the change-
password B9/B10 no-op doctrine); the phone no-op is harmless — no credential
semantics, no revocation, nothing to protect. The asymmetry is the design,
not an accident; it gets a comment at both endpoints. Qwen's addition worth
taking: the **no-op detection is normalise-then-compare** — `"Me@X.com "`
vs stored `me@x.com` must hit the 400 path, pinned in a test.

**2. `ChangePhoneIn` contract — I keep mine; qwen's null-as-clear rejected.**
Qwen makes `phone: str | None` and documents that BOTH `""` and `null` mean
clear ("different contracts for a single-field body vs a patch document").
That is exactly the problem: one action, two spellings of "clear", and a
third behaviour over in `MemberPatch` (null → 422). My contract —
`phone: str` (required), `""` clears, `null` → 422 — keeps ONE canonical
clear spelling and aligns the null behaviour with `_PatchIn`'s ("a client
sending null is a bug, not an intent"). Qwen itself flags this as taste;
the pin (qwen's P4, adopted) then asserts the full contract: no password
field (session-only), no null (422), `""` clears.

**3. Gate exemption — converged: not exempt.** A flagged account is
mid-mandatory-password-change; letting it mutate identity/contact fields
before clearing the flag widens the pre-hygiene surface for no benefit, and
the exempt set stays minimal-by-doctrine (`me`/`logout`/`change-password`).
Adopt qwen's fuller rationale wording; the flip cost (two path strings +
two tests) is recorded.

## Adopted from qwen

- **`new_email` field name** (with `current_password`) — mirrors
  `ChangePasswordIn {current_password, new_password}` exactly; my `email`
  broke the sibling naming for no reason.
- **P4**: the schema-contract pin — a `current_password` key in
  change-phone's body is a 422 (`extra="forbid"`), pinning session-only.
- **E4's ordering pin**: after the 400 no-op, a second session is still
  alive — the no-op is rejected BEFORE the revocation loop, not after.
- **M2 hygiene**: explicitly re-check the existing exact-key pins (login
  `{token, user}`, identify `{same_user}`, conftest field-wise assertions)
  against the `UserOut.phone` addition — cheap, prevents a surprise.
- The `str.maketrans` separator strip (cosmetic; same behaviour as my
  `re.sub`) and the interior-`"+"` rejection (redundant with `isdigit()`
  but harmless belt).
- Risk-note: the `IntegrityError` race branch is structurally untested
  (same as register's — not reproducible in TestClient); the pre-check 409
  is pinned and the catch mirrors tested code. Consistent repo posture.

## Rejected from qwen (with reasons)

- **Null-as-clear in `ChangePhoneIn`** (§2 above).
- **`Field(max_length=32)` on the table model's `phone`** — SQLModel does
  not validate table models (established in Task 1); the bound is inert
  decoration there. The bound that matters lives on the INPUT schemas
  (raw-string `max_length=32` before normalisation — which my
  `ChangePhoneIn.phone` already carries, and qwen's does too) and in the
  migration's `VARCHAR(32)` DDL.

## Final recommended shape

My proposal, amended: `ChangeEmailIn {new_email, current_password}`
(ChangePasswordIn naming mirror); the normalise-then-compare no-op check
with its case-variant test; the P4/E4/M2 pins; qwen's gate rationale
wording; `ChangePhoneIn.phone: str` required (null → 422, `""` clears —
one clear spelling); model field plain `str | None` (no inert bounds); the
separator-strip implementation either way. Everything else stands as in my
`b-proposal-glm.md`: the shared `_normalised_phone_or_none`, the
`MemberIn`/`MemberPatch` wiring (update_member zero-change via setattr),
`create_member`'s one constructor field, staff-email-impossible pin,
revocation mirror, badge-independence integration pin, ~17-case
`test_profile.py` + migrations updates.

## Changes to my own proposal

1. `ChangeEmailIn.email` → **`new_email`** (sibling-endpoint naming).
2. Add the normalise-then-compare no-op rule + its case-variant test
   (`"Me@X.com "` vs `me@x.com` → 400).
3. Add qwen's P4 (session-only contract pin: password key → 422), E4's
   no-op-before-revocation ordering pin, and the M2 existing-exact-key
   re-check to the test list.
4. Record the null-as-clear rejection and the inert-model-bound rejection
   in the review (done above) so the implementer doesn't re-litigate.
