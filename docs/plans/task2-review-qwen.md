# Task 2 review (qwen3.8-max) — peer proposals for auto-QR-badge at account creation

Reviews `docs/plans/task2-proposal-glm.md` and `docs/plans/task2-proposal-kimi.md`
against `docs/plans/task2-proposal-qwen.md`, the coordinator's Task 2, and the
current tree (Task 1 landed). Claims re-verified against code this pass:
`app/security.py` imports only stdlib + `app.models` (so a `qrbadge → security`
module-level import is acyclic); exactly **two** existing rotate sites
(`auth.py::create_my_badge`, `members.py::create_member_badge`);
`MUST_CHANGE_EXEMPT_PATHS` = {me, logout, change-password} (rotate is *not*
exempt); seeds construct `models.User` directly in `app/main.py:47/:78`;
`tests/test_metadata_and_ops.py` pins no OpenAPI schema inventory.

§4 gives the merged shape; §5 the changes to my own proposal.

---

## 1. Review of `task2-proposal-glm.md`

### Strengths (adopt)

- **S1 — The "exactly once, by type not by discipline" argument.** The
  clearest statement of why subclasses with a **required** field beat an
  optional field on the shared schemas: with `response_model=RegisterOut`,
  badge material *cannot* appear in login/qr-login/me/members responses
  (populating it would fail response filtering), whereas an optional field on
  `AuthOut` puts every future endpoint returning it "one careless
  `badge=…` away from a leak". All three plans land on subclasses; glm's
  framing is the one to quote in the PR description.
- **S2 — The `_issue_token` second-commit edge, named precisely.** register's
  flow has *two* commits (user row, then the AuthToken insert inside
  `_issue_token`); if the second fails, the account exists with a stored
  hash whose payload was never shown. glm identifies this server-side case
  (sharper than my "client disconnects before reading the response" — which
  is the same recovery class but a later failure point) and lands on the
  right resolution: recoverable via password-login + rotate, document it, do
  not re-architect `_issue_token`. Adopt the framing; it belongs in the
  register docstring.
- **S3 — Raw-token form pinned in the test.** The creation badge must log in
  via `qr-login` in **both** the full `HCRM1:…` form and the bare 43-char
  token (`_RAW_TOKEN_RE` acceptance path). My test list only exercised the
  prefixed form; adopt.
- **S4 — Distributed shape assertions.** Additive `badge`-present assertions
  where the creation responses are *already* pinned (`test_auth.py` register
  test, `test_password_policy.py` staff-created tests) instead of only in
  `test_qr.py`. Keeps shape contracts next to their existing consumers;
  adopt.
- **S5 — Edge-table completeness**: race on *both* endpoints (not just
  register), rotation/revocation of the creation badge as "just the first
  badge", flagged-account gate interaction worth an explicit test line,
  staff-privilege non-change argument for `create_member` (staff could
  already print member badges via the existing endpoint, so the auto-badge
  grants no new capability). All correct; adopt.
- **S6 — Verified conftest/OpenAPI claims** (`create_user` reads only
  `must_change_password`; no schema snapshot to refresh) — matches my own
  verification.

### Weaknesses / risks (avoid or fix)

- **W1 — The anti-helper argument attacks a strawman shape.** glm refuses
  any shared function because "a third caller (a seed, a script, a future
  import tool) could pick up" a `create_user_with_badge()`-style helper. But
  the proposed helper (mine/kimi's) does not create users — it sets a hash
  on an object handed to it; a seed would have to *explicitly call it*, and
  the seeds-no-badge tests (which glm itself wants, §3) fail the suite the
  moment that happens. Meanwhile inline duplication puts the
  generate-payload/store-hash/return-once triplet in **four** places
  (2 rotate + 2 creation) — precisely the drift risk this coordinator plan
  already ruled against for scan resolution (decision 8: "a single shared
  decision core … so the bridge and server never drift"). Consistency with
  the plan's own doctrine favours the helper. Reject W1's conclusion, keep
  its underlying caution as a comment in the helper docstring ("call sites
  are badge-issue endpoints only; seeds construct User directly and must
  not call this").
- **W2 — Seed test precision.** Test 4 proposes a direct-DB check that
  "*both* seeded emails" have NULL hashes, in the main suite where
  `HCRM_DEV_ADMIN=0` — the dev admin row does not exist there, so half the
  assertion is vacuous (glm acknowledges this in a parenthetical but leaves
  the test as written). The real dev-admin pin must be in the subprocess
  test; my `_BOOT_SNIPPET` extension (add `has_qr_badge` to the existing
  `accounts` tuples, assert False for both seeded accounts) covers ON-mode
  with a two-line change. Use that instead of glm's "if desired, one line".
- **W3 — Field name `badge`.** Inconsistent with the `has_qr_badge` flag,
  the `/qr-badge` routes, and the `qr_badge_hash` column; 2-of-3 proposals
  say `qr_badge`. Cosmetic but this is an API contract — pick the
  consistent one.
- **W4 — No helper means no dedup follow-up path.** glm's plan leaves the
  two rotate sites and the two new sites as four hand-rolled copies; its own
  §6 "untouched, deliberately" list makes Task-6 dedup harder to justify
  later ("we decided inline was right"). Minor, but it compounds W1.
- **W5 — `RegisterOut(**out.model_dump(), badge=…)`** round-trips the
  `UserOut` through dict/str and re-validates (datetimes serialise and
  re-parse). Works, but explicit construction (`RegisterOut(token=auth.token,
  user=auth.user, qr_badge=…)`) skips the churn and keeps model instances.
  Taste-level; note it.

### Disagreements with my proposal, and verdicts

| Topic | glm | mine | Verdict |
|---|---|---|---|
| Helper vs inline | inline 2×2 lines | `issue_badge()` in `qrbadge.py` | **Mine+kimi** (W1 rebuttal; decision-8 doctrine) |
| Field name | `badge` | `qr_badge` | **Mine+kimi** (W3) |
| Partial-failure edge | `_issue_token` commit failure | client-disconnect/response-loss | **glm** — same recovery (rotate), sharper diagnosis (S2) |
| Seed-mode coverage | main-suite DB check (vacuous for dev admin) | `_BOOT_SNIPPET` tuple extension | **Mine** (W2) |
| Test placement | distributed (qr + auth + password_policy) | all in `test_qr.py` | **glm** (S4) |
| qr-login raw-token form | tested | untested | **glm** (S3) |

## 2. Review of `task2-proposal-kimi.md`

### Strengths (adopt)

- **S1 — The helper contract docstring**: "Nothing is committed here — the
  caller's transaction decides whether the hash ever persists (see
  register's 409 path)." That single sentence is the whole atomicity story
  and the exact thing a future caller could get wrong (committing inside the
  helper would break the 409 rollback). Adopt verbatim into whichever
  helper name wins.
- **S2 — Convert the two existing rotate sites to the helper in the same
  PR.** Mechanical (each is the same 3-line triplet), covered end-to-end by
  the existing lifecycle tests (`test_self_badge_lifecycle`,
  `test_staff_manages_member_badge`), and it removes the "two shapes
  coexist" drift my proposal would have created by deferring to Task 6.
  Adopt — with the sequencing discipline of a **separate commit** inside the
  PR so a reviewer can drop it without unwinding the feature (and correct
  kimi's count: there are **two** existing sites, not three; the revoke
  paths set `None` and are a different pattern).
- **S3 — "Generate before commit, return after commit"** as the named
  invariant, with the corollary "a badge can never be *lost* (hash stored,
  payload never shown) *on the failure paths*, because the payload is only
  handed out on the success path". Best one-line statement of the edge-case
  answer among the three proposals; adopt as the PR-description invariant
  (with glm's S2 as the documented exception class: post-user-commit /
  pre-display failures recover via rotate).
- **S4 — IntegrityError non-ambiguity**: email is the *only* unique
  constraint on `users`, and `qr_badge_hash` is indexed-but-not-unique, so
  the existing `except IntegrityError → 409` cannot misattribute a badge
  collision to an email conflict (and 256-bit payloads make hash collision a
  non-consideration anyway). Closes a question my proposal left implicit;
  adopt.
- **S5 — Flagged-account operational detail**: a `must_change_password`
  account gets 403 on `POST /api/auth/qr-badge` (rotate is not in
  `MUST_CHANGE_EXEMPT_PATHS` — verified), so a lost printed card before
  first login is recovered by *staff* re-issue via
  `/api/members/{id}/qr-badge`. Correct, non-obvious, and exactly the
  counter-workflow question Task 5's UI will need; adopt into the edge
  table.
- **S6 — Validation precedes generation**: well-known-password 400 /
  duplicate 409 paths never generate badge material. Free hygiene (no
  secrets minted for rejected requests); adopt as an ordering rule.
- **S7 — Leak-test technique**: assert the literal `HCRM1:` substring
  appears nowhere in login/me/members response bodies — a cheap whole-body
  sweep that catches leaks no key-name assertion would (e.g. a payload
  stuffed into a wrong field). Stronger than my key-set-only T4; adopt and
  merge with my exact-key-set assertion (`login` keys == `{token, user}`)
  and glm's `has_qr_badge: true` presence check into one inventory test.
- **S8 — `response_model` filtering as defence in depth**: even if internals
  grow fields, FastAPI strips anything not declared on the response model.
  Correct; adopt into the subclass rationale.
- **S9 — R2 (body-logging middleware would need `qr_badge` exclusions)** —
  a real forward-looking ops note; adopt as a Task-6 docs line.

### Weaknesses / risks (avoid or fix)

- **W1 — Factual miscount**: "the badge-issue pattern … currently exists
  **three times**, duplicated verbatim". It exists twice
  (`create_my_badge`, `create_member_badge`); the revoke endpoints are a
  different pattern (set `None`). Verified by grep this pass. Doesn't change
  the dedup conclusion (2 existing + 2 new = 4 sites still justifies the
  helper), but the PR text must be accurate — this repo's review culture
  checks claims.
- **W2 — `RegisterOut(**auth_out.model_dump(), …)`** — same dict/re-validate
  round-trip as glm's W5; explicit construction is cleaner. Minor.
- **W3 — Seed-exclusion test is API-only** (`has_qr_badge` via `/me` +
  members list). Adequate, but the direct-DB `qr_badge_hash IS NULL`
  assertion (mine/glm's) is strictly stronger for the seeded admin — it
  would also catch a hypothetical UI-only exposure bug — and costs one
  `Session(engine)` block. Combine: API flag + DB NULL for the suite-side
  seed, `_BOOT_SNIPPET` extension for the dev admin.
- **W4 — Helper typing hand-wave** ("`user` left un-hinted or
  `TYPE_CHECKING`-hinted"): decide, don't shrug — `TYPE_CHECKING`-guarded
  `from app.models import User` gives editors/mypy the contract with zero
  runtime import. One line.
- **W5 — Module-level `from app.security import token_hash` in
  `qrbadge.py`** — kimi asserts acyclicity and is **right** (verified:
  `security` imports only stdlib + `app.models`; `models` imports neither).
  Listed here only to close my own proposal's more-cautious local-import
  choice: module-level is the better Python and the cycle fear was unfounded.

### Disagreements with my proposal, and verdicts

| Topic | kimi | mine | Verdict |
|---|---|---|---|
| Rotate-site conversion | same PR | defer to Task 6 | **kimi** (S2), as a separate commit |
| Import shape in helper | module-level `token_hash` | function-local import | **kimi** (W5 — cycle claim verified false) |
| Helper name | `new_badge_for` | `issue_badge` | **kimi, marginally** — "issue" over-promises persistence; the docstring contract (S1) is what actually matters |
| Leak test | `HCRM1:` substring sweep | exact key-set + per-endpoint payload absence | **Merge** (S7): substring sweep + key-set + `has_qr_badge` presence |
| Seed test depth | API-only | API + direct DB NULL + `_BOOT_SNIPPET` | **Mine** (W3) |
| Q1 required-vs-optional | required, with future `?badge=false` musing | required, no musing | **Both required**; kimi's bulk-import mode is speculative — YAGNI, and flipping required→optional later is a trivial additive change, the reverse is breaking |

## 3. Three-way convergence (coordinator fast-path)

All three: generate the badge **pre-commit** so hash+account land in one
INSERT (the IntegrityError-race loser rolls both back; the existing 409 path
is untouched); store only `token_hash(payload)`; render `badge_svg()`
**post-commit** in the response (matching the rotate endpoints' ordering);
reuse `QrBadgeOut` unchanged; `RegisterOut(AuthOut)` + `MemberCreatedOut
(UserOut)` with a **required** badge field and `response_model` swaps as the
only decorator changes; `AuthOut`/`UserOut` untouched so login/me/members
shapes are byte-identical; seeds excluded **structurally** (they construct
`User` directly and are not modified) with test pins, plus a documented
rationale (a payload nobody can be shown is dead weight; logging it would
leak a credential); **no backfill** for existing accounts; no schema/migration
change (`users.qr_badge_hash` + index already exist and stay in
`COLUMN_MIGRATIONS` untouched); recovery for any post-commit/pre-display
failure is rotate; creation badges are ordinary badges (rotate/revoke/
delete-with-account all apply); `must_change_password` gate interaction
pinned by test; conftest/smoke consumers unaffected (additive field).

## 4. Final recommended shape (standardise on this)

1. **Helper** — `app/qrbadge.py` gains:
   ```python
   from app.security import token_hash        # acyclic: security imports only app.models (verified)
   if TYPE_CHECKING: from app.models import User

   def new_badge_for(user: "User") -> str:
       """Store the hash of a fresh badge on ``user``; return the raw payload.

       Nothing is committed here — the caller's transaction decides whether
       the hash ever persists (register's 409 rollback discards both).
       The payload is a bearer credential shown exactly once, by the caller,
       in a creation/rotation response; only its SHA-256 survives server-side.
       Call sites are the badge-issue endpoints only: the seed paths
       construct User directly and must never call this (pinned by
       test_seeded_accounts_have_no_badge / test_dev_admin_mode_on).
       """
       payload = new_badge_payload()
       user.qr_badge_hash = token_hash(payload)
       return payload
   ```
2. **`register()` / `create_member()`** — call `new_badge_for(user)` after
   constructing the `User` and *before* `session.add`/`commit` (validation
   400s/409s therefore precede any secret generation); existing
   `try/except IntegrityError → rollback → 409` untouched (email is the only
   unique constraint on `users`, so the catch is unambiguous); after
   `refresh`, build the response **explicitly** (no `model_dump`
   round-trip): `RegisterOut(token=auth.token, user=auth.user,
   qr_badge=QrBadgeOut(payload=p, svg=badge_svg(p)))` /
   `MemberCreatedOut(**UserOut.model_validate(user).model_dump(),
   qr_badge=…)`; decorators gain `response_model=RegisterOut` /
   `MemberCreatedOut`. Docstrings record the S2/glm-S2 recovery path
   (post-commit pre-display failure ⇒ password login + rotate; flagged
   accounts ⇒ staff re-issue, since rotate is not gate-exempt).
3. **Same PR, separate commit** — convert `create_my_badge` and
   `create_member_badge` to the helper (mechanical; existing lifecycle tests
   are the regression net). Coordinator may drop this commit without
   unwinding the feature.
4. **Schemas** — `RegisterOut(AuthOut)` / `MemberCreatedOut(UserOut)`, field
   **`qr_badge: QrBadgeOut`, required**; `AuthOut`/`UserOut`/`QrBadgeOut`
   untouched.
5. **Seeds** — `app/main.py` behaviour untouched; comment-only notes in both
   seed functions ("deliberately no badge: the payload can only be shown
   once and seeds have no viewer — staff rotate on demand").
6. **Tests** (merged superset; `test_qr.py` primary):
   - creation-badge-works for both endpoints — `parse_badge_payload` OK, SVG
     well-formed, `has_qr_badge: true` in the response, `qr-login` succeeds
     with **both** the `HCRM1:` form and the raw 43-char form (glm S3);
     parametrised over member/staff/admin targets for `create_member`;
   - gate interaction: creation badge of a flagged account issues a token,
     exempt paths work, everything else 403 until change-password (glm S5,
     kimi test 3);
   - hash-only-at-rest direct-DB check for both creation paths;
   - **leak inventory** (merged S7 + my T4 + glm test 5): `HCRM1:` substring
     absent from login/qr-login/me/members/PATCH/revoke bodies; login keys
     exactly `{token, user}`; members entry shows `has_qr_badge: true` and
     no material; rotate response payload ≠ creation payload;
   - 409 race: duplicate register/member-create → 409, no `qr_badge` in the
     error body, first account's badge still works, DB unchanged;
   - creation badge rotates/revokes like any badge (old payload → 401);
   - uniqueness across two creations;
   - seeds: main-suite `/me` flag **and** direct-DB `qr_badge_hash IS NULL`
     for `admin@shop.local`; `_BOOT_SNIPPET` `accounts` tuples extended with
     `has_qr_badge`, asserted `False` for **both** seeded accounts in
     `test_dev_admin_mode_on`;
   - distributed shape pins: additive `qr_badge`-present assertions in
     `test_auth.py`'s register test and `test_password_policy.py`'s
     staff-created tests (glm S4);
   - suite green unchanged: `conftest.create_user`, `test_metadata_and_ops.py`
     (no schema snapshot — verified), `test_migrations.py` exact-set test
     (no migration changes).
7. **Docs riding the PR** (repo is docs-with-code): `docs/opticon-m10.md`
   §9.2 issuing table gains "automatic at account creation (returned once)";
   README badge paragraph one sentence; Task 6 inherits kimi's R2 note
   (body-logging middleware would need `qr_badge` exclusions).

## 5. Changes to my own proposal (self-review)

1. **Module-level `token_hash` import** in the helper — kimi disproved my
   cycle caution (verified: `security` → `models` only); drop the
   function-local import and the "leaf-level" rationale.
2. **Helper contract docstring + naming**: adopt kimi's "nothing is
   committed here" wording (the load-bearing sentence) and prefer
   `new_badge_for` over my `issue_badge` ("issue" over-promises
   persistence); add `TYPE_CHECKING` hint instead of kimi's un-hinted
   shrug (W4).
3. **Rotate-site conversion**: move from "defer to Task 6" to "same PR,
   separate commit" (kimi S2) — my deferral would have left four hand-rolled
   copies of a security-critical triplet; note the conversion count is two
   sites, not kimi's three (W1).
4. **Partial-failure canon**: replace my client-disconnect framing with
   glm's `_issue_token`-second-commit case (mine is a subset); add kimi's
   "generate before commit, return after commit" as the named invariant and
   the email-only-unique-constraint non-ambiguity point (S4).
5. **Leak test**: add kimi's `HCRM1:` whole-body substring sweep to my
   exact-key-set inventory (S7).
6. **Test placement**: distribute additive shape assertions into
   `test_auth.py` / `test_password_policy.py` (glm S4); add the raw-token
   qr-login form (glm S3).
7. **Edge table**: add kimi's flagged-account staff-reissue detail (S5) and
   validation-precedes-generation ordering (S6); add glm's
   no-new-privilege note for `create_member`.
8. **Keep** (against peers): field name `qr_badge` (2-of-3 + internal
   consistency); direct-DB seed assertions and the `_BOOT_SNIPPET` tuple
   extension (strongest dev-admin pin, glm W2/kimi W3); comment-only seed
   notes in `app/main.py` (glm/kimi leave the file untouched — a comment is
   zero-risk and documents the exclusion at the exclusion site); explicit
   response construction without `model_dump()` round-trips (both peers
   round-trip).

## 6. Verdict

All three proposals are convergent and implementable; there is no
architectural split to arbitrate (unlike Task 1's PUT-flush bug, neither
peer proposal contains a correctness defect — the differences are
hygiene/precision). **kimi's is the strongest base** (the helper with the
transaction-contract docstring, the rotate-site dedup, the substring-sweep
leak test, the flagged-account recovery detail), **glm contributes the best
rationale text** (type-enforced exactly-once, the `_issue_token` edge, the
raw-token and distributed-shape tests), and **mine keeps the strongest seed
pins** (direct-DB NULL + `_BOOT_SNIPPET` extension covering the dev admin in
ON-mode). §4 is the merged specification I recommend the coordinator
standardise on; the only genuinely open choice it leaves is the field name
(`qr_badge`, 2-of-3) and whether the rotate-site conversion commit rides
this PR (recommend yes, droppable).
