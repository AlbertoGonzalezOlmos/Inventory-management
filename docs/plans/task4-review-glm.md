# Task 4 cross-review (glm) — of `task4-proposal-kimi.md` and `task4-proposal-qwen.md`

Reviewer context: my own proposal is `docs/plans/task4-proposal-glm.md`. All
three proposals converged on the core before review: nullable
`logoff_deadline` column + one append-only `COLUMN_MIGRATIONS` entry, **no
index** (PK-seek access), reject-and-delete with the **uniform 401** "Token
invalid or expired", check before the user/pw-gate, per-token scope (logout's
principle), `POST /logoff-schedule` → server-computed deadline +
`POST /logoff-cancel` → 204 idempotent, restart = overwrite, env-overridable
`LOGOFF_GRACE_SECONDS` in `app/security.py`, no-sleep tests, `qr-identify` as
a pure side-effect-free predicate with `hmac.compare_digest`, and the
migration exact-set test consciously extended. The review arbitrates the
deltas.

---

## 1. Kimi

**Strengths (adopt):**
- **401-on-unparseable for `qr-identify`** (vs my/qwen's "false for
  everything"). Winner of the one real design disagreement: it matches
  `qr-login`'s contract exactly (the two badge-consuming endpoints fail
  identically on input), and it gives Task 5 an explicit "not a badge at
  all" branch instead of routing a garbage scan into the switch flow
  (false ⇒ SPA attempts qr-login ⇒ 401 toast, one wasted round trip and a
  confusing flash). No leak: badge well-formedness is client-checkable
  (documented format), so the 401/false distinction reveals nothing.
- **The try/except around the reject-and-delete** (R1): the 401 must never
  depend on the cleanup write succeeding — wrap, log, raise regardless.
  Cheap hardening I missed.
- Docs riding the PR (opticon-m10 paragraph + README row) — Task-2/3
  precedent.
- Test 7 (deadline beats the pw-gate even on exempt paths) and test 6
  (uniform-401 body equality) — sharper pins than mine for the same
  behaviours.

**Weaknesses / rejected:**
- **`max(1, int(env))` clamps the grace away from zero** — and zero is
  exactly the elegant no-sleep test lever (arm with grace 0 → deadline ==
  now → next request 401s via `<=`). Kimi's own alternative lever
  (DB-past-deadline) works, but banning 0 to protect against a "footgun"
  that is really just a coherent "no grace" configuration costs more than
  it buys. Qwen's `max(0, …)` + inclusive boundary is better.
- Re-file of duplicate coverage is otherwise unobjectionable; no bugs
  found.

**Disagreements with mine:** exempt-paths (kimi says NOT exempt — see §3),
401-on-invalid (kimi wins, above), `<=` (kimi wins, §3).

## 2. Qwen

**Strengths (adopt):**
- **The decisive qr-identify justification** (B.4 point 1, sharper than
  mine): the same-user path must **arm the current token** —
  login-and-revoke mints a *new* token that is useless for arming the
  session the kiosk actually holds, so a schedule call is needed regardless;
  identify+schedule is the minimal decomposition. Plus the transient-live-
  credential window and table-churn arguments. Also the honest recording of
  their phase-2 flip and why (authenticated + caller-own-bit vs their
  original unauthenticated-probe objection).
- **`<=`, not `<`** (with a boundary test): the countdown reaching 0 *is*
  the logoff moment; `expires_at` keeps `<`. The asymmetry is deliberate
  and correct — my "mirror expires_at" was the lazy choice.
- **Deadline clamped to `expires_at`** (`min(now + grace, expires_at)`):
  an absurd grace cannot arm a deadline past the token's TTL. One `min()`,
  kills a whole confusion class. Both kimi and I missed it.
- **Zero-grace as the primary test lever** (`max(0, …)` + `<=` → grace 0
  arms deadline == now → next request 401s): the cleanest no-sleep expiry
  test of the three, and the reason to reject kimi's `max(1, …)`.
- The edge matrix rows I lacked: **cancel/schedule after expiry → 401**
  (a dead session cannot be resurrected — correct and worth pinning),
  **fresh logins are unarmed** (the NULL-default pin), **upgrade adds NULL
  to live rows** (deploying cannot log anyone off).
- Explicit refusal to extend `_issue_token`'s purge, with the inert-row
  argument (any presentation deletes; never-presented rows are unusable and
  die at the ≤7-day TTL purge). Convinces me — **I drop my optional A4**.
- Risk 2 (the import style is load-bearing for the monkeypatch override —
  document which name tests patch) and risk 7 (auto-cancel is Task-5
  policy, not `get_current_user` policy) — both correct framings.

**Weaknesses / rejected:**
- `QrIdentifyIn` as a separate schema (vs reusing `QrLoginIn`): identical
  shape; a docstring on the reuse documents intent just as well. 2-of-3
  reuse. Trivial either way.
- "false for everything" in qr-identify — conceded to kimi's 401 branch
  (§1).

**Disagreements with mine:** `<` vs `<=` (qwen wins), purge extension (qwen
wins, I drop it), clamp-to-expires_at (qwen wins), "false for everything"
(kimi wins over both of us).

## 3. Final recommended shape

The converged design plus the adjudicated deltas:

- **Column/migration**: `logoff_deadline: datetime | None`; one appended
  `COLUMN_MIGRATIONS` entry, no index (PK-seek; no query filters on it);
  exact-set migration test consciously extended; legacy rows land NULL.
- **Enforcement** (`app/deps.py`): after the `expires_at` check —
  `logoff_deadline is not None and logoff_deadline <= utcnow()` →
  try/except-wrapped delete+commit (log on failure) → **uniform 401**
  "Token invalid or expired". Before the user fetch and the pw-gate; kills
  exempt paths too.
- **Endpoints** (`app/routers/auth.py`): `POST /logoff-schedule` → 200
  `LogoffScheduleOut {deadline, grace_seconds}` with
  `deadline = min(now + grace, token.expires_at)` (restart = overwrite, no
  409); `POST /logoff-cancel` → 204 idempotent. Both presented-token-scoped
  (logout's signature pattern). **NOT added to
  `MUST_CHANGE_EXEMPT_PATHS`** (2-of-3: a flagged account sits on the
  change-password screen, outside the scan loop; `logout` remains its
  escape hatch).
- **Constant**: `LOGOFF_GRACE_SECONDS = max(0, int(env
  "HCRM_LOGOFF_GRACE_SECONDS", 30))` in `app/security.py`; the router
  references the module-level name at call time (monkeypatch target
  documented); **zero grace is legal** ("no grace" config + the test lever).
- **No purge change** (inert-row argument).
- **`qr-identify`**: authenticated, reuses `QrLoginIn`; `_badge_from_scanned`
  extraction shared with `qr_login` (Task-3 anti-drift doctrine applied to
  badge acceptance; existing qr-login tests gate the extraction);
  unparseable → **401 "Invalid QR badge"** (contract parity with qr-login,
  explicit SPA branch); parseable → `{same_user:
  compare_digest(token_hash(badge), user.qr_badge_hash)}` with the
  badgeless short-circuit; no side effects, no enumeration.
- **Tests**: the union — qwen's 11 logoff cases (zero-grace lever, clamp,
  cancel-after-expiry, boundary-inclusive, fresh-logins-unarmed) + kimi's
  uniform-401-body and beats-the-gate pins + my per-token isolation and
  restart pins; qr-identify cases from all three (both forms, rotation
  tracking, no-side-effects DB count, one-bit response, gates);
  migration cases; README config row rides the PR.

## 4. Changes to my own proposal as a result

1. **Drop the `MUST_CHANGE_EXEMPT_PATHS` additions** (2-of-3, and their
   argument is better: flagged accounts aren't in the scan loop; logout is
   the escape hatch).
2. **`<` → `<=`** with a boundary test (the countdown hitting 0 is the
   logoff).
3. **qr-identify gains the 401-on-unparseable branch** (kimi) and drops my
   "false for everything"; keep `compare_digest` + badgeless short-circuit.
4. **Adopt the `_badge_from_scanned` extraction** — withdraw my "overkill
   for two call sites" note (it's the same anti-drift doctrine as Task 3,
   and qr-login's tests gate it).
5. **Drop my optional A4 purge extension** (qwen's inert-row argument).
6. **Add**: the `min(…, expires_at)` clamp, `grace_seconds` in the response,
   the try/except around the delete, `max(0, …)` grace clamp, cancel-after-
   expiry → 401, fresh-logins-unarmed, and the deploy-adds-NULL pins.
