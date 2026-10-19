# Task 2 proposal review (kimi-k3)

Reviews of `task2-proposal-glm.md` and `task2-proposal-qwen.md` against my
own `task2-proposal-kimi.md`, plus the recommended standard shape.

All three proposals converge on the core: generate the badge hash into the
`User` **before the account commit** (atomic insert — the IntegrityError
race needs no new handling), return it via **subclass response schemas with
a required badge field** reusing `QrBadgeOut`, keep `AuthOut`/`UserOut`
untouched (login/me/member-list responses byte-identical), exclude seeds
structurally and pin the exclusion with tests. The real differences are:
helper-vs-inline, whether to refactor the rotate endpoints now, the field
name, and test depth.

---

## 1. Review: glm

### Strengths (adopt)
- **"By type, not by discipline"** framing of the subclass choice: with
  `AuthOut` untouched, badge material *cannot* appear in login responses —
  populating it would be a type error against the declared
  `response_model`, and reviewers check one decorator instead of every call
  site. Same conclusion as mine, better argued; adopt the phrasing.
- **Construction-time generation** (`qr_badge_hash=token_hash(payload)` as a
  constructor argument): the hash is part of the row from its birth — the
  cleanest possible statement of "one transaction, one failure domain".
  Semantically identical to my/qwen's pre-add call; slightly more elegant.
- **Verified, not hedged**: `tests/test_metadata_and_ops.py` has no OpenAPI
  schema snapshot (asserts docs kill-switch, token index, hygiene), and
  `conftest.create_user` only reads `must_change_password` — so no snapshot
  refresh and no helper changes. My proposal said "check"; GLM checked.
  Adopt as fact.
- **Concrete test homes**: create_member shape assertions belong in
  `tests/test_password_policy.py`
  (`test_staff_created_account_is_flagged_until_changed` etc.) — sharper
  than my "wherever create_member is asserted today". Adopt.
- The partial-failure analysis (user-commit succeeds, `_issue_token` fails)
  explicitly identifies the self-service rotate endpoint as the recovery
  path — same as mine, well stated.

### Weaknesses / risks
- **No helper — deliberately.** GLM inlines the two lines in both routers
  and argues a shared helper "silently widens the blast radius" (a seed or
  script could start calling it). That argument holds against a
  hypothetical `create_user_with_badge()` — not against an
  `issue_badge(user)` living in `app/qrbadge.py`: seeds in `app/main.py`
  import neither the routers nor qrbadge, and the seed tests (all three
  proposals have them) pin the exclusion regardless. Meanwhile the
  generate+hash-set pair is *the* security-critical invariant of this
  feature (payload and stored hash must be two views of one secret);
  duplicating it at four sites (two new + two existing rotate) invites
  exactly the drift the repo's shared-`analyze_scan` doctrine exists to
  prevent. Helper wins; see §3.
- **Field name `badge`** — inconsistent with the route names (`/qr-badge`,
  `/qr-login`) and `has_qr_badge`. Trivial, but `qr_badge` (mine/qwen's) is
  the better name.
- **Thinner negative tests**: no hash-at-rest DB assertion (payload never
  persisted), no exact-keys assertion on the login response, no
  conflict-trace DB check after a 409. Qwen's test set covers all three —
  adopt those.

### Disagreements vs. my proposal
- Helper vs inline: GLM inline, me helper — mine (and qwen's) stands, for
  the drift reason above; GLM's counter-argument targets a different
  (user-creation) helper than the one proposed.
- Everything else (subclasses, required field, seeds structural, one PR,
  no model/migration change) is agreement.

---

## 2. Review: qwen

### Strengths (adopt)
- **`issue_badge(user)` helper placed in `app/qrbadge.py` with a
  function-local `token_hash` import** — keeps `qrbadge.py` dependency-free
  at module level (both routers import it). A refinement of my module-level
  import; either is acyclic, the local import is the more conservative
  choice for a leaf module. Adopt.
- **Do NOT touch the rotate endpoints in this task.** My proposal converted
  the three existing badge-issue sites to the helper in the same PR;
  qwen's scoping — don't refactor tested security code inside a feature
  task, flag the dedup as a Task-6 follow-up — is the safer call and keeps
  the PR mechanically reviewable. Adopt (this reverses one of my
  decisions).
- **Ground-truth inventories**: all four `User(...)` construction sites and
  all four `qr_badge_hash` write sites enumerated; seeds confirmed in
  `app/main.py` (not `app/seed.py`); no event hooks/backfills. This makes
  the "seeds excluded structurally" claim checkable rather than asserted.
  Adopt the habit.
- **Test set is the strongest of the three** — adopt in full:
  T3 (DB check `qr_badge_hash == token_hash(payload)`, raw payload nowhere
  in the row), T4 (leak inventory across login/qr-login/me/members/PATCH/
  revoke **plus exact-keys `{token, user}` on the login response** — the
  pin that `AuthOut` didn't grow), T5 (post-409 DB trace: one row, first
  registration's hash unchanged), T9 (extend `test_dev_admin.py`'s
  `_BOOT_SNIPPET` accounts tuples with `has_qr_badge` — concrete mechanism,
  better than GLM's "one line if desired"), T10 (two registrations →
  distinct payloads/hashes), T7 (password-gate pin extended to creation
  badges), T6 (parametrised across member/staff/admin targets).
- **Client-disconnect edge case** (payload lost after commit; recovery =
  rotate) — a case my "partial failure" analysis gestured at; qwen's
  formulation (documented in the endpoint docstring) is the right
  disposition. Adopt.
- **One-line comments in the two seed functions** ("deliberately no badge —
  payload can only be shown once; seeds have no viewer"). My proposal said
  `main.py` untouched; comments-only is strictly better — the next reader
  sees the decision at the decision point. Adopt.
- Field name `qr_badge` (agreement with mine, against GLM's `badge`).

### Weaknesses / risks
- Essentially none of substance. Nits: the function-local import, while
  justified, is a pattern the repo doesn't otherwise use (reviewer taste);
  and "docs ride with the PR" slightly widens the diff (three lines —
  acceptable, and better than letting docs drift to Task 6).
- The helper leaves the rotate endpoints duplicating the pair *for now* —
  a deliberate, documented trade-off; the Task-6 follow-up must actually
  happen or the drift risk GLM warned about re-enters through the back
  door. Worth a tracked note.

### Disagreements vs. my proposal
- **Rotate-site refactor timing**: qwen defers, I included it. Qwen is
  right — adopted (smaller blast radius for a feature PR; the existing
  lifecycle tests pin current behaviour, but the refactor buys nothing this
  task needs).
- **`main.py` untouched vs comments-only**: qwen's comments win — adopted.
- Helper name: `issue_badge` (qwen) vs my `new_badge_for` — cosmetic;
  `issue_badge` reads better next to `new_badge_payload`. Adopt.

---

## 3. Final recommendation — the shape to standardise on

**Take qwen's proposal as the base, verbatim in almost all particulars:**

1. **Helper**: `issue_badge(user) -> str` in `app/qrbadge.py` (function-
   local `token_hash` import), used in **exactly the two new call sites**
   in this PR. Rotate endpoints (`auth.create_my_badge`,
   `members.create_member_badge`) are **not** refactored here — the dedup
   is a flagged Task-6 follow-up (this is the one place I overruled my own
   proposal, accepting qwen's scoping and addressing GLM's drift concern
   via the follow-up rather than via no-helper).
2. **Generation placement**: `issue_badge(user)` before `session.add` /
   commit in both `register()` and `create_member()` — the hash rides the
   account INSERT; the IntegrityError→409 path is untouched and needs no
   new reasoning; the payload is returned only on the success path.
3. **Response shape**: `RegisterOut(AuthOut)` and `MemberCreatedOut(UserOut)`,
   each with a **required** `qr_badge: QrBadgeOut`; `AuthOut`/`UserOut`/
   `QrBadgeOut` untouched (type-enforced exactly-once, GLM's phrasing);
   field name **`qr_badge`** (route/property consistency — over GLM's
   `badge`).
4. **Seeds**: structural exclusion (verified site inventory), plus one-line
   comments in `seed_default_admin`/`_ensure_dev_admin`, plus test pins in
   both boot modes (main suite `/me` + DB NULL check; dev-admin
   `_BOOT_SNIPPET` tuple extension).
5. **Tests**: qwen's T1–T11, with GLM's pointer that create_member shape
   assertions live in `tests/test_password_policy.py`, and the verified
   fact that no OpenAPI snapshot refresh is needed.
6. **Docs**: the three-line `docs/opticon-m10.md` §9.2 + README addition
   rides with the PR (qwen's call; cheap and prevents drift).

## 4. Changes I would make to my own proposal

1. **Do not refactor the three existing badge-issue sites in this PR**
   (qwen) — use `issue_badge` only in the two new sites; flag the dedup as
   a tracked Task-6 follow-up.
2. **Adopt the function-local `token_hash` import and the name
   `issue_badge`** for the helper (qwen).
3. **Add qwen's T3/T4/T5/T9/T10 tests** to my test list — in particular the
   DB-level hash-at-rest assertion, the login response exact-keys pin, and
   the `_BOOT_SNIPPET` mechanism for the dev-admin exclusion (replacing my
   vaguer dev-admin line).
4. **Replace my "check test_metadata_and_ops" hedge** with the verified
   fact (both peers): no OpenAPI snapshot exists to refresh.
5. **Add the one-line "deliberately no badge" comments** in the two seed
   functions (qwen) — my "main.py untouched" becomes "comments only".
6. Keep from my proposal (against GLM): the helper itself, the `qr_badge`
   field name; (against both, no conflict): the required-not-optional
   field and the shoulder-surfing/log-exclusion risk notes for Tasks 5/6.
