# Task 4 proposal review (kimi-k3)

Reviews of `task4-proposal-glm.md` and `task4-proposal-qwen.md` vs my own.
The three proposals are structurally identical on the load-bearing choices:
nullable `logoff_deadline` on `AuthToken` (per-token scoping, no index, one
append-only `COLUMN_MIGRATIONS` entry + exact-set test update),
reject-and-delete in `get_current_user` with the uniform 401 detail,
overwrite-restart scheduling, idempotent cancel, env constant +
monkeypatch (never sleep), and a side-effect-free boolean `qr-identify`.
Only the deltas need adjudication.

## GLM

**Strengths (adopt)**
- Reuse `QrLoginIn` as the `qr-identify` body (keeps the two badge-consuming
  endpoints' acceptance shapes in lockstep; qwen duplicates the bounds in a
  new `QrIdentifyIn` — unnecessary with the shared acceptance helper).
- The explicit edge-case table (`change-password` during countdown kills
  *other* armed rows via existing revocation — correct observation, worth a
  test).
- "Uniform false, never 401" for `qr-identify` (shared with qwen — see
  below; this beats my 401-on-unparseable).

**Weaknesses / rejected**
- **Extending `MUST_CHANGE_EXEMPT_PATHS` with all three Task-4 paths** —
  rejected. A flagged account is pinned to the change-password screen and
  never enters the kiosk scan loop; the one real corner (flag set *while* an
  armed deadline exists elsewhere) is still handled correctly because the
  lazy expiry kills the token regardless of what the SPA can call. Qwen's
  no-extension position keeps the exempt set minimal and the gate uniform.
- **`_issue_token` purge extension** (GLM's optional A4) — rejected in
  favour of qwen's explicit non-extension: deadline-past rows are inert
  (every presentation 401s and self-deletes), the login path keeps its
  single indexed `expires_at` scan, smallest diff.
- `==` instead of `hmac.compare_digest` for the hash compare — minor;
  house style is `compare_digest` (`verify_password`), qwen and I use it.
- "Shared helper is overkill for two call sites" — rejected; qwen's
  `_badge_from_scanned` extraction is exactly the Task-3 anti-drift
  doctrine applied to badge acceptance, and existing qr-login tests are
  its regression gate.

## Qwen

**Strengths (adopt — most are decisive refinements)**
- **`<=` boundary**: the deadline instant *is* the logoff instant
  (`expires_at` keeps `<`). This also makes the zero-grace monkeypatch seam
  work with no DB writes — schedule with grace 0, next request 401s.
  Adopt, with the boundary pinned by test (my `<` + DB-push approach works
  but is clunkier).
- **Grace clamped `max(0, …)`** and **deadline clamped to
  `min(now + grace, expires_at)`** — the absurd-grace hardening I lacked;
  zero grace becomes a legitimate test/demo feature rather than my
  `max(1)` footgun-guard.
- **Uniform `{same_user: false}` for every non-match** (garbage, product
  EAN, other user's badge, badgeless account) — no 401 branch at all. The
  endpoint answers a boolean; a malformed scan is "not yours", the SPA has
  one shape to consume, and the enumeration analysis holds (the bit is
  about the caller's *own* hash; a badge-holder learns nothing `qr-login`
  wouldn't already tell them). This beats my uniform-401 design; adopted.
- **No purge extension, no exempt-set extension** (both for
  smallest-diff/inert-row reasons, above).
- **`LogoffScheduleOut {deadline, grace_seconds}`** — echoes the
  configured grace so the SPA renders without assumptions; over my
  `{logoff_deadline}`.
- **Test set is the strongest**: boundary-inclusivity pin,
  clamp-to-`expires_at`, cancel-after-death 401 (a dead session cannot be
  resurrected), fresh-logins-are-unarmed, deadline-kills-exempt-paths,
  identify-follows-rotation, no-side-effects DB pins. Adopt wholesale.
- **Explicit non-changes as reviewed decisions** (`_issue_token`,
  `logout`, `MUST_CHANGE_EXEMPT_PATHS`, `INDEX_MIGRATIONS` untouched) and
  the import-style pin (`LOGOFF_GRACE_SECONDS` referenced as a module
  attribute at call time so monkeypatching works).

**Weaknesses**
- None of substance. Nits: a dedicated `QrIdentifyIn` duplicates
  `QrLoginIn`'s bounds (reuse instead, GLM's point); the
  compare-digest-against-None short-circuit needs the badgeless-account
  guard stated (qwen states it).

## Disagreements resolved

| Point | Mine | GLM | Qwen | Verdict |
|---|---|---|---|---|
| identify on unparseable | 401 uniform w/ qr-login | `false` | `false` | **`false`** (peers) — boolean endpoint, one shape, no leak |
| deadline comparison | `<` | `<` | `<=` | **`<=`** (qwen) — deadline instant = logoff instant; enables zero-grace seam |
| grace clamp | `max(1)` | none | `max(0)` + clamp to `expires_at` | **qwen** |
| exempt paths | don't extend | extend (+3) | don't extend | **don't extend** (qwen/mine) — lazy expiry covers GLM's corner |
| login purge extension | no | optional | explicitly no | **no** (qwen) |
| identify body schema | reuse `QrLoginIn` | reuse | new `QrIdentifyIn` | **reuse** (GLM/mine) |
| hash compare | `compare_digest` | `==` | `compare_digest` | **`compare_digest`** |

## Final recommended shape

**Qwen's proposal as the base**, with: `QrLoginIn` reused as the identify
body (GLM), and my one defensive nicety kept as reviewer-optional
(try/except around the reject-and-delete commit → 401 regardless).
Everything else per qwen: `<=` boundary, `max(0,…)` grace + `expires_at`
clamp, `{deadline, grace_seconds}`, overwrite-restart, idempotent 204
cancel, no purge/exempt-set changes, `_badge_from_scanned` extraction,
uniform-false identify, qwen's test list (incl. boundary, clamp,
cancel-after-death, unarmed-fresh-logins, identify-rotation), exact-set
migration test consciously extended.

## Changes to my own proposal

1. `qr-identify`: drop the 401-on-unparseable branch → **uniform
   `{same_user: false}`** for all non-matching input (tests 14→ rewritten
   as the uniform-false case; keep auth gates 401/403).
2. Deadline check: `<` → **`<=`**, boundary pinned by test.
3. Grace: `max(1,…)` → **`max(0,…)`**; schedule clamps the deadline to
   `min(now + grace, expires_at)`; adopt the zero-grace monkeypatch seam
   (DB-push remains as an alternative test lever).
4. Schedule response: `{logoff_deadline}` → **`{deadline, grace_seconds}`**.
5. State explicitly (qwen-style): `_issue_token` purge and
   `MUST_CHANGE_EXEMPT_PATHS` are **unchanged**, with the reasons.
6. Add qwen's tests: boundary-inclusivity, clamp-to-`expires_at`,
   cancel-after-death 401, fresh-logins-unarmed, exempt-path-kill,
   identify-follows-rotation.
7. Keep from mine: `hmac.compare_digest`, `_normalise_badge`≡
   `_badge_from_scanned` extraction, the write-on-read try/except note,
   per-token isolation emphasis, and the Task-5 choreography contract.
