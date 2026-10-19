# Task 2 cross-review (glm) — of `task2-proposal-kimi.md` and `task2-proposal-qwen.md`

Reviewer context: my own proposal is `docs/plans/task2-proposal-glm.md`.
The three proposals converged hard before this review even started —
generate-before-commit, hash-only-at-rest, `RegisterOut`/`MemberCreatedOut`
subclasses with a **required** badge field reusing `QrBadgeOut`, structural
seed exclusion with test pins, primary coverage in `tests/test_qr.py` — so
the review arbitrates the remaining deltas: the helper question, the field
name, scope (touch the rotate endpoints or not), and which test ideas are
worth merging.

---

## 1. Review of `task2-proposal-kimi.md`

### Strengths (adopt)

- **The exactly-once framing is the sharpest of the three**: "the payload
  and the persisted hash are derived from the same secret in the same
  breath; the payload is only handed out on the success path" — that is the
  whole correctness argument for the IntegrityError race, stated once and
  correctly. (Their §4 also correctly notes the badge can never be the
  unique-violation source: email is the only unique constraint on `users`;
  `qr_badge_hash` is indexed but not unique, 256-bit collisions are
  irrelevant — same reasoning as session tokens.)
- **The flagged-account recovery asymmetry** I missed: a
  `must_change_password` member cannot self-rotate
  (`/api/auth/qr-badge` is not in `MUST_CHANGE_EXEMPT_PATHS` → 403), so a
  lost pre-first-login card must be re-issued by *staff*. Correct, and
  worth one docstring line.
- **Verified the import graph** for putting the helper in `app/qrbadge.py`
  (`security` imports only `app.models`; no cycle) — this legitimises the
  module-level import qwen avoided with a local one.
- Test T5 (409 discards the badge, first badge still works) and T2
  (exactly-once: no `qr_badge` key in subsequent logins) are the right
  response-level pins.

### Weaknesses / risks

- **Scope creep: converting the two existing rotate endpoints to the new
  helper in the same PR.** They are tested security code whose behaviour
  must not change in a feature task; the conversion buys dedup now at the
  cost of review surface and merge-conflict risk with any parallel work.
  Qwen's stance (add the helper, use it in the two *new* sites, flag the
  rotate conversion as a follow-up) is the right call and matches the
  repo's one-concern-per-PR culture.
- **A miscount**: the generate-and-store pair exists in **two** places
  today (`create_my_badge`, `create_member_badge`), not "three times,
  duplicated verbatim". Harmless, but a reviewer should not propagate it.
- `new_badge_for(user)` mutates the user while the name reads
  create-and-return; `issue_badge` (qwen) says what it does. Cosmetic.
- The dev-admin exclusion is asserted only via "one line in the existing
  subprocess scenario" without the concrete mechanism qwen supplies
  (see below).

### Disagreements with my proposal

| Point | Kimi | Me | Verdict |
|---|---|---|---|
| Helper vs inline | `new_badge_for()` in `app/qrbadge.py` | inline two lines per router | **Kimi** (with modifications) — my "a shared helper widens the seed blast radius" argument was overcautious: a seed that wants a badge can inline the pair just as easily, the seeds test is the real guard, and the helper prevents drift of a security-critical pair across now-four call sites. But take qwen's scope (no rotate conversion this PR) and name. |
| Field name | `qr_badge` | `badge` | **Kimi** (with qwen) — 2-of-3, and consistent with the `/qr-badge` routes and `has_qr_badge`. My `badge` was arbitrary. |
| Rotate endpoints | convert in this PR | untouched | **Me** (with qwen) — see scope-creep above. |

## 2. Review of `task2-proproposal-qwen.md`

*(reviewing `task2-proposal-qwen.md`)*

### Strengths (adopt)

- **Ground-truth inventory up front** (4 `User(...)` construction sites, 4
  `qr_badge_hash` write sites, no hooks/defaults/backfills) — this is what
  makes the "seeds excluded structurally" claim an argument rather than an
  assertion, and it independently confirms my own reading.
- **Three-legged seed exclusion** (structural + *documented deliberateness*
  + test-pinned in **both boot modes**). The one-line "deliberately no
  badge" comments in the two seed docstrings are cheap and prevent a future
  "why don't seeds get badges? let me add it" well-intentioned bug. Adopt.
- **The concrete dev-admin test mechanism (T9)**: extend
  `test_dev_admin.py`'s `_BOOT_SNIPPET` `accounts` tuples with
  `u["has_qr_badge"]` and assert `False` for both seeded accounts — kimi
  and I both hand-waved "a line in the subprocess test"; qwen specified
  exactly how, including that the existing unpacking stays valid.
- **T3 — the hash-at-rest DB pin** (`users.qr_badge_hash ==
  token_hash(payload)`, raw payload nowhere in the row) extends the
  existing `test_only_the_hash_is_persisted` to the creation path. The
  single most important security property of this task; I had it only
  implicitly. Adopt.
- **T4 — the leak inventory**, including the strongest form: `login`'s
  response keys are **exactly** `{token, user}` (pins that `AuthOut` never
  grew the field), plus `PATCH /members/{id}` and the revoke 204, plus
  "rotate legitimately carries a *new* payload — assert it differs from
  the creation one". More thorough than my absence assertions.
- **T5 done at the DB level**: after a duplicate-email 409, exactly one
  user row, its hash is the *first* registration's. Combines with kimi's
  response-level version into one airtight case.
- **Correct refusal to write a fake concurrency test**: a deterministic
  concurrent-register race is not reproducible in this suite; the
  atomicity argument is structural and the sequential 409 test pins the
  contract. (I said the same; qwen says it better.)
- **Docs riding the PR** (3 lines: §9.2 table row + README sentence) —
  the repo is docs-with-code; deferring three lines to Task 6 is ceremony.
  Adopt.
- SVG-after-commit ordering note (matches `create_my_badge`; failure mode
  is the rotate-recovery path) — correct.

### Weaknesses / risks

- **The function-local `token_hash` import** is needless ceremony: kimi
  verified the module-level import is acyclic, and a local import hides
  the dependency from readers of the imports block. Take module-level.
- **T9 edits an existing subprocess test's tuple shape** — specified
  carefully, but it is the one place this task touches existing
  assertions; the implementer must run `test_dev_admin.py` first and keep
  the unpacking changes minimal (qwen's own note that
  `emails = [a[0] for a in …]` stays valid is the guard).
- `MemberCreatedOut(**UserOut.model_validate(user).model_dump(), …)` works
  but is the clunkiest of the three spellings; any of the equivalents is
  fine — no action.
- Nothing else: no bugs found. The proposal is the most rigorous of the
  three and its only real cost is verbosity.

### Disagreements with my proposal

| Point | Qwen | Me | Verdict |
|---|---|---|---|
| Helper | `issue_badge` in `app/qrbadge.py`, rotates untouched | inline | **Qwen** — same verdict as kimi's row above, plus the better name and scope |
| Field name | `qr_badge` | `badge` | **Qwen** (with kimi) |
| Import style | local import | — | **Kimi's module-level** — qwen's own acyclicity note concedes this is taste; module-level wins |
| Hash-at-rest DB pin | explicit test (T3) | implicit | **Qwen** |
| Docs in PR | yes (3 lines) | defer to Task 6 | **Qwen** |

## 3. Final recommendation — the shape to standardise on

**Generation:** one helper, `issue_badge(user) -> str` in `app/qrbadge.py`
(generate payload → set `user.qr_badge_hash = token_hash(payload)` → return
payload; **module-level** `from app.security import token_hash` — acyclic,
verified). Called in exactly two places, after `User(...)` construction and
before `session.add`: `register()` and `create_member()`. Hash and account
commit in one INSERT; the payload is returned only on the success path. The
two existing rotate endpoints are **not** converted in this task (flagged
follow-up dedup).

**Response:** `RegisterOut(AuthOut)` and `MemberCreatedOut(UserOut)` with a
**required** `qr_badge: QrBadgeOut` (field name `qr_badge` — consistent with
the routes and `has_qr_badge`). `AuthOut`/`UserOut`/`QrBadgeOut` untouched —
the leak guarantee is the type system, not discipline.

**Seeds:** zero code change in `app/main.py` + one-line "deliberately no
badge" comments in both seed docstrings + pins in both boot modes
(`admin@shop.local` via the main suite; the dev admin via the extended
`_BOOT_SNIPPET` tuples).

**Tests** (union, in `tests/test_qr.py` unless noted): register returns a
working badge (logs in via qr-login, both payload forms) with
`has_qr_badge=True`; create_member likewise, parametrised over role, with
the password-gate interaction pinned; hash-at-rest DB pin; the leak
inventory with exact `login` keys `{token, user}`; duplicate-email 409
discards the badge (response + DB levels); creation badge rotates/revokes
like any badge; distinctness across two registrations; seeded admins
badge-less in both boot modes; additive shape assertions in
`test_auth.py`/`test_password_policy.py` where those endpoints are already
asserted.

**Docs:** 3 lines riding the PR (`docs/opticon-m10.md` §9.2 issuing table
gains "automatic at account creation"; README one sentence).

## 4. Changes I am making to my own proposal as a result

1. Field name `badge` → **`qr_badge`** (both peers; route/flag consistency).
2. Drop my inline-two-lines stance: adopt the **`issue_badge` helper** in
   `app/qrbadge.py` — my seed-blast-radius objection was overcautious (the
   seeds test is the guard; a helper is no more tempting to a seed than the
   inline pair, and it stops a security-critical pair drifting across four
   call sites). Keep qwen's scope: rotate endpoints untouched this PR.
3. Adopt kimi's module-level `token_hash` import over qwen's local one.
4. Adopt qwen's **T3 hash-at-rest DB pin** and **T4 leak inventory**
   (including exact login keys) — my absence assertions were weaker.
5. Merge kimi's response-level and qwen's DB-level **409-discards-badge**
   test into one case.
6. Adopt the concrete **dev-admin `_BOOT_SNIPPET` mechanism** (T9) instead
   of my hand-waved "one line in the subprocess test".
7. Adopt the **seed docstring comments** and **docs riding the PR**.
8. Add kimi's **flagged-account recovery note** (staff must re-issue a lost
   pre-first-login card; the member cannot self-rotate while flagged) to
   the endpoint docstrings.

No bugs found in either peer proposal; the two substantive corrections are
kimi's scope creep (rotate conversion) and the two-place miscount, and
qwen's local-import ceremony — all minor. Both proposals are implementable;
the merged shape above is strictly better than each.
