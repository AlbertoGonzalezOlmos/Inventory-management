# Task B review (qwen3.8-max) — peer proposal for phone + email change

Sole-peer review (kimi unavailable): `b-proposal-glm.md` vs
`b-proposal-qwen.md`. The two proposals are ~90 % identical: same column +
appended migration (exact-set → six), same normalisation rules (optional
`+`, 6–15 digits, `""`-clears per the barcode precedent, invalid → 422),
same two endpoints returning **200 `UserOut`**, same **no-op asymmetry**
(email == current → 400 "must not look like a change / must not churn
revocations"; phone == current → 200 idempotent — both proposals chose it
independently with the same reasoning, so it is settled), same **gate
decision** (neither endpoint exempt; flagged → 403; flippable and documented
as such in both), same revocation mirroring of `change_password`, same
badge-independence integration pin, same staff rules (`MemberPatch.phone`
yes / email never — `extra="forbid"` → 422 pinned), same members.py touch
surface (constructor only; the setattr loop carries PATCH for free). The
deltas that matter are four.

## 1. glm — strengths (adopt)

- **S1 — The verify-before-uniqueness ordering argument, stated as security
  design**: the taken-email 409 is an enumeration oracle of register's
  accepted class, but serving it *only after* `verify_password` keeps it
  **strictly narrower** (credential-holders only). My proposal had the same
  step order but framed it as mere mirroring; glm's framing belongs in the
  endpoint docstring. **Plus the test neither of us listed**: wrong
  password + a *taken* email → **400, never 409** — the pin that proves the
  oracle sits behind the credential check. Adopt both.
- **S2 — Normalisation via `re.sub(r"[\s\-().]", "", value)`**: the `\s`
  class strips tabs/newlines too, where my translate-table (`" -.()"`) only
  handles the literal space. Trivial merge; behaviour identical on every
  realistic input, strictly wider on adversarial ones. (Both designs catch
  interior `+` via the `isdigit()` gate — glm's `"++34"` and my `"555+1234"`
  cases are equivalent; keep one of each in the 422 table.)
- **S3 — Test 12: missing `current_password` → 422** (schema-required pin)
  — cheap, adopt. And glm's test 15 *combination* (after revocation, the
  surviving session's badge re-view still 200s) folds nicely into my E6.
- **S4 — "MemberIn.email stays — setting the identifier AT CREATION is not
  editing it"**: the one-line justification for why the staff takeover rule
  doesn't touch creation. Adopt into the `MemberIn` docstring.
- **S5 — Risk 4's docs hand-off**: the narrowed-oracle note belongs in Task
  F's tracked-issues/docs pass. Adopt.

## 2. glm — weaknesses (fix/reject)

- **W1 — BUG in the `ChangePhoneIn` sketch**: `phone: str =
  Field(max_length=32)` with a before-validator that maps `""` → `None`
  **cannot clear** — pydantic then validates `None` against `str` and
  answers 422, contradicting glm's own edge table (`"" phone → clears →
  NULL`). The field must be `str | None` (my contract). This also settles
  the prompt's "ChangePhoneIn contract" question: a required
  `phone: str | None` where **both `""` and `null` clear** is coherent
  because this is a single-field *command body*, not a patch document —
  `_PatchIn`'s null-rejection exists to disambiguate null-vs-omit in
  partial updates, and there is no "omit" here (the key is mandatory).
  `MemberPatch.phone` keeps `_PatchIn` semantics (null → 422, omit →
  unchanged, `""` → clear) — the asymmetry between the two schemas is
  deliberate and documented in both docstrings.
- **W2 — Field name `email` in `ChangeEmailIn`**: keep my `new_email` —
  it sits beside `current_password` in the same body, and the
  `ChangePasswordIn` house precedent is `current_*`/`new_*`. Cosmetic;
  coordinator's call, zero cost either way.
- **W3 — No structural pin of the session-only phone contract**: my P4
  (a `current_password` key in the change-phone body → 422 via
  `extra="forbid"`) is the test that keeps decision 3 from silently
  drifting; glm lists it only as a flippable risk. Keep P4.
- **W4 — Model field without `max_length`**: mine keeps `max_length=32`
  (validation-depth-in-defence; SQLite ignores VARCHAR length anyway, and
  the hand-written migration DDL carries `VARCHAR(32)` for shape
  consistency). No behavioural difference — noted, not contested.

## 3. Disagreement summary (prompt's three focus items)

| Topic | glm | qwen | Verdict |
|---|---|---|---|
| phone==current 200 vs email==current 400 | same asymmetry | same asymmetry | **Converged** — settled; both pin "no-op email revokes nothing" (glm 13 = my E4) |
| `ChangePhoneIn` contract | `str` (sketch bug W1: `""` would 422) | `str \| None`, required; `""`/null clear | **Mine** (W1) |
| Gate exemption | not exempt, flippable | not exempt, flippable | **Converged** |

## 4. Final recommended shape

My proposal as the base, with: glm's `\s`-class normaliser (S2); the
verify-before-uniqueness rationale in the `change_email` docstring (S1);
four added tests — wrong-password+taken-email → 400-not-409 (S1), missing
`current_password` → 422 (S3), revoke+badge-review combined assertion
(S3/glm 15), and my P4 structural pin retained (W3); `MemberIn` docstring
line (S4); Task-F docs note (S5). Field name `new_email` (W2). Everything
else was already converged: column/migration, normalisation bounds,
200-`UserOut` responses, revocation mirroring, badge-independence pin,
staff email-impossibility pin, members.py constructor-only change,
migration exact-set/legacy-NULL tests, suite-hygiene key-set audit
(`phone` lives inside `user`, so Task-2's `{token, user}` and identify's
`{same_user}` pins are unaffected).

## 5. Changes to my own proposal

1. Normaliser: translate-table → `re.sub(r"[\s\-().]", "", …)` (S2).
2. `change_email` docstring gains the narrowed-oracle rationale; test list
   gains wrong-password+taken-email → 400, missing-password → 422, and the
   revoke+badge-review combination (S1/S3).
3. `MemberIn` docstring gains the creation-vs-editing line (S4); Task-F
   hand-off note added to risks (S5).
4. Kept against glm: `phone: str | None` required command-body contract
   (their sketch cannot express clear — W1), `new_email` naming (W2), P4
   structural pin (W3), model `max_length=32` (W4, cosmetic).
