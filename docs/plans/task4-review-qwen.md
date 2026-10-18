# Task 4 review (qwen3.8-max) — peer proposals for the logoff deadline + qr-identify

Reviews `task4-proposal-{glm,kimi}.md` against `task4-proposal-qwen.md` and
coordinator decision D6. The three proposals are close: identical column,
identical append-only migration entry, unanimous **no-index** (with the same
PK-seek reasoning), unanimous reject-and-delete + uniform 401 detail,
restart-by-overwrite, idempotent 204 cancel, per-token scope, server-computed
deadline returned, `LOGOFF_GRACE_SECONDS` in `security.py` referenced via a
monkeypatchable `auth.py` module name, zero-sleep tests, shared badge-parser
extraction, side-effect-free identify, and the mandatory conscious update of
`test_every_migration_the_app_currently_needs_is_listed`. The disagreements
are seven; §4 merges them.

---

## 1. Review of `task4-proposal-glm.md`

### Strengths (adopt)

- **S1 — Exempt all three Task-4 paths from the `must_change_password`
  gate.** This is the review's most consequential catch, and it overturns my
  own position (kimi agrees with me, so glm stands alone — but glm is
  right). Reasons: (a) `logout` is already exempt, and schedule/cancel/
  identify are the same class (session hygiene, presented-token-scoped,
  zero privilege gain — shortening or un-countdowning *your own* session is
  not a capability); (b) Task 5's scan handler is **global** — a flagged
  kiosk session still receives badge scans, and a 403 from `qr-identify`
  dead-ends the kiosk mid-flow (the SPA cannot even route "own badge →
  countdown" vs "other badge → switch"); (c) the clean-leave story: a
  flagged account must be able to participate in the countdown that ends
  with `logout` (exempt). glm's test 8 (exempt trio works while non-exempt
  paths still 403) is the pin; adopt.
- **S2 — The clock-rewind argument for delete-over-reject**: with
  reject-only, an NTP step backwards makes a "dead" token live again; a
  deleted row stays dead. Strengthens the reject-and-delete rationale
  beyond my tidiness/precedent argument; adopt into the deps comment.
- **S3 — Server-returned deadline as the skew defence** ("the SPA never
  computes the deadline") — same as mine/kimi's; glm states the reason
  best.
- **S4 — "No 404 path" observation** for the endpoints (the token cannot be
  missing — `get_current_user` already 401'd): keeps the error matrix
  honest; adopt.
- **S5 — The `why not qr-login-then-compare` enumeration**: point (4) —
  login-and-revoke "lets any authenticated caller mint sessions for any
  badge holder as a side effect of probing" — is a sharper framing than my
  transient-credential argument; adopt into the merged justification.

### Weaknesses / risks (avoid or fix)

- **W1 — BUG in the optional A4 purge extension.** As sketched,
  `or_(expires_at < now, AuthToken.logoff_deadline != None,
  AuthToken.logoff_deadline < now)` deletes **every row with an armed
  (future) deadline** on every login — i.e. any user logging in anywhere
  would silently kill the kiosk's live countdown session. The intent was
  presumably `and_(logoff_deadline.is_not(None), logoff_deadline < now)`.
  Verdict: **skip A4 entirely** (mine and kimi both decline it; the rows are
  inert — any presentation 401s+deletes — and bounded by the existing
  7-day purge). If a future coordinator wants it, the corrected predicate
  and its scan cost must be reviewed then. This is the one place a merged
  spec could have shipped a feature-breaking line.
- **W2 — `<` vs `<=` at the boundary.** glm mirrors `expires_at`'s strict
  `<`; kimi and I use `<=` (the countdown reaching 0 *is* the logoff
  instant — the SPA fires at 0, so server and client must agree at the
  boundary). 2-of-3, and semantically right for a deadline (vs a TTL);
  adopt `<=`, pin with a frozen-clock test (§4).
- **W3 — "A formal shared helper is overkill for two call sites"** (badge
  acceptance): rejected — the shared `_normalise_badge` extraction *is* the
  anti-drift doctrine this coordinator plan has now applied twice
  (`scanlookup`, `keyword_filter`); credential-acceptance rules are exactly
  what must not exist in two hand-rolled copies. 2-of-3 (kimi + me).
- **W4 — Plain `==` on badge hashes** where `verify_password` established
  `hmac.compare_digest` as house style for credential comparisons; costs
  nothing, adopt compare_digest (kimi + me).
- **W5 — Never-401 identify** (garbage → 200 `false`): loses the
  "garbled scan → re-scan prompt" vs "well-formed, other person → switch
  flow" distinction that Task 5's UI wants, and diverges from `qr-login`'s
  uniform-401 language for malformed badges. kimi's two-tier is better
  (§2 S3); the syntax it reveals is public (prefix + length + alphabet), so
  there is no enumeration cost.
- **W6 — No env floor** on `LOGOFF_GRACE_SECONDS` (a negative/zero value
  arms instant-kill countdowns); kimi's `max(1, …)` parse floor is the cheap
  guard (tests don't need env-0: they monkeypatch the module name or
  DB-write past deadlines).

## 2. Review of `task4-proposal-kimi.md`

### Strengths (adopt)

- **S1 — R1: the delete's commit must be wrapped** (`try: delete+commit /
  except: rollback, log`), raising the 401 regardless — "the deadline
  comparison is authoritative even if the cleanup write fails." A lock
  timeout during cleanup must never turn a logoff into a 500 (or worse, a
  pass). Neither glm nor I had this; adopt verbatim.
- **S2 — Direct-DB past-deadline as the PRIMARY expiry test** (write
  `utcnow() - 1s` into the row; assert the next request 401s and the row is
  gone): tests the enforcement point with zero time machinery. Better than
  my monkeypatch-grace-0-as-primary; keep grace-0 monkeypatch as the
  secondary (it additionally proves the endpoint→constant wiring
  end-to-end).
- **S3 — Two-tier identify**: unparseable → 401 "Invalid QR badge" (uniform
  with `qr-login`); well-formed-but-not-yours → 200 `{same_user: false}`.
  Plus the enumeration analysis that settles it: "possession of the payload
  is the credential — identify leaks nothing beyond it." Adopt (flips my
  uniform-false; see glm W5).
- **S4 — `max(1, int(env))` grace floor** (R3) — adopt (glm W6).
- **S5 — R4: write the Task-5 contract down now** (same badge → schedule
  restart; different badge → cancel-then-switch; stay → cancel; countdown
  end → logout) *with* the sentence that matters: "even if the SPA forgets
  every one of these, the lazy expiry still logs the counter off — the
  server never depends on the client for the security property." Adopt as
  the endpoint-docstring/docs paragraph.
- **S6 — R5: shared-token tabs** (two tabs, one token, one deadline —
  correct for a counter); adopt as a documented note.
- **S7 — `logoff_deadline` as the response field name** (matches the
  column); adopt over my `deadline`.
- **S8 — Reuse `QrLoginIn`** for the identify body (glm agrees; 2-of-3 over
  my `QrIdentifyIn`) — one fewer schema, and it signals "same credential
  format, same bounds". The POST-body-not-query point (bearer credential
  must never reach access logs) is worth keeping in the docstring.
- **S9 — Test 7 (deadline expiry beats the password gate → 401 not 403 on
  an exempt path)** and **Test 6 (uniform-401 body equality)**: sharper pins
  than my equivalents; adopt.

### Weaknesses / risks (avoid or fix)

- **W1 — Restructuring the existing validity check** (splitting
  `auth_token is None or expires_at < now` into separate branches to slot
  the deadline check first): same behaviour, bigger diff on the repo's most
  security-sensitive dependency. The additive block *after* the untouched
  existing check (mine/glm) is equivalent — both raise the same 401 before
  the user lookup — and keeps `test_auth.py`'s expiry pins provably
  unmoved. 2-of-3.
- **W2 — Rejected: no-exemption stance** ("a flagged account has no
  business scheduling kiosk logoffs"): scheduling a logoff is *leaving*,
  which flagged accounts may already do (`logout` exempt); and see glm S1(b)
  — the global scan handler dead-ends without the exemption.
- **W3 — Test 10's subprocess for the env knob** is over-heavy: the wiring
  is the same `int(os.environ.get(...))` idiom as `PBKDF2_ITERATIONS`
  (which has no subprocess test); pin the default (30) in-process and the
  behaviour via monkeypatch. (Contrast `HCRM_DEV_ADMIN`, which *behaves*
  differently at import and earns its subprocess.)
- **W4 — Minor**: dropping `grace_seconds` from the schedule response
  ("SPA renders from `logoff_deadline − now`") — that computation uses the
  *client* clock and is exactly as skew-exposed as counting from zero;
  returning the applied `grace_seconds` lets Task 5 start a monotonic
  countdown at receipt. Keep both fields (mine).

## 3. Disagreement summary (mine included)

| Topic | glm | kimi | qwen | Verdict |
|---|---|---|---|---|
| Gate exemption for the 3 new paths | exempt all | exempt none | exempt none | **glm** (S1 — Task-5 global scan handler + clean-leave) |
| Boundary | `<` | `<=` | `<=` | **`<=`** (2-of-3; deadline semantics), pinned by frozen-clock test |
| Purge extension (A4) | optional, **buggy sketch** | none | none | **none** (glm W1) |
| Identify on unparseable input | 200 false | 401 uniform | 200 false | **kimi** (S3) |
| Badge-parse sharing | docstring note | helper | helper | **helper** (glm W3; 2-of-3 + doctrine) |
| Hash compare | `==` | compare_digest | compare_digest | **compare_digest** (glm W4) |
| Body schema | reuse QrLoginIn | reuse QrLoginIn | new QrIdentifyIn | **reuse** (S8; I withdraw) |
| Env floor | none | max(1) | max(0) | **max(1)** (kimi S4; tests don't need env-0) |
| deps diff shape | additive block | restructured | additive block | **additive** (kimi W1) |
| Delete-commit failure | unhandled | try/except → 401 anyway | unhandled | **kimi** (S1) |
| Schedule response | `{deadline}` | `{logoff_deadline}` | `{deadline, grace_seconds}` | **`{logoff_deadline, grace_seconds}`** (S7 + my W4-of-kimi) |
| Clamp deadline ≤ expires_at | none | none | min() clamp | **keep** (cosmetic hygiene vs absurd env values; costs one expression) |

## 4. Final recommended shape (deltas over the convergent base)

Base = the unanimous core (§ intro) with my A1–A4/B structure. Merged
deltas, in file order:

- **`app/security.py`**: `LOGOFF_GRACE_SECONDS = max(1, int(os.environ.get(
  "HCRM_LOGOFF_GRACE_SECONDS", "30")))` (kimi floor; comment documents the
  footgun it prevents).
- **`app/deps.py`**: existing checks untouched; additive block after them —
  `logoff_deadline is not None and logoff_deadline <= now` → delete inside
  `try/except` (rollback + warning on failure), **always** raise the same
  401 "Token invalid or expired" (kimi R1); comment carries both
  rationales (one-shot write / cart-reap precedent + clock-rewind
  durability, glm S2).
- **`MUST_CHANGE_EXEMPT_PATHS`** += `/api/auth/logoff-schedule`,
  `/api/auth/logoff-cancel`, `/api/auth/qr-identify` (glm S1), with a
  comment: session hygiene, presented-token-scoped, zero privilege gain —
  and the Task-5 global-scan-handler reason.
- **`app/routers/auth.py`**: `_normalise_badge(token) -> str | None`
  extracted and used by `qr_login` + `qr_identify` (all existing qr tests
  are the extraction's gate); `logoff-schedule` → 200
  `LogoffScheduleOut {logoff_deadline: datetime, grace_seconds: int}` with
  `deadline = min(now + grace, token.expires_at)`; `logoff-cancel` → 204
  idempotent; `qr-identify` (body: `QrLoginIn` reused) → **401 "Invalid QR
  badge"** when `_normalise_badge` returns None, else 200
  `{same_user: bool}` via `compare_digest` with a `qr_badge_hash is None`
  short-circuit; endpoint docstrings carry the Task-5 contract paragraph
  (kimi R4) and the shared-token-tabs note (kimi R5).
- **Tests** (merged, deduplicated — `tests/test_logoff.py`):
  unauthenticated 401s; schedule arms **only the presented token** (second
  session NULL, direct DB) and stays usable inside the window; response
  fields present and deadline matches the row; **expiry primary** = direct-DB
  past deadline → next request 401 with body **equal** to a naturally
  expired token's 401, row deleted, other session alive (kimi S2/S9);
  **expiry secondary** = monkeypatched grace 0 → schedule → next request 401
  (endpoint-wiring proof); **boundary pin** = monkeypatch `app.deps.utcnow`
  to exactly the stored deadline → 401 (proves `<=`; `<` would pass it);
  restart = reschedule moves the deadline later (no 409); cancel clears +
  double-cancel 204 + cancel-with-nothing 204; cancel beats a would-be
  expiry (grace 0 + cancel → still alive); clamp (huge grace → deadline ==
  `expires_at`); post-expiry schedule/cancel → 401 (no resurrection);
  logout and change-password take armed rows with them; **exempt-trio pin**
  (flagged account: schedule/cancel/identify 200/204/200 while
  `/api/items` 403s — glm test 8); **deadline beats the gate** (flagged +
  past deadline → 401 not 403 on exempt `/me` — kimi test 7); fresh logins
  unarmed; grace default == 30 in-suite. `tests/test_qr.py`: identify own
  badge both forms → true; other-user/garbage-product-EAN → 401 (malformed)
  or false (well-formed other) per tier; badgeless account → false; rotate
  → old false/new true, revoke → false; **no side effects** (auth_tokens
  count + `logoff_deadline` unchanged + `qr_badge_hash` unchanged);
  **one-bit body** (keys exactly `{same_user}`); anonymous 401; flagged →
  200 via exemption (updated from my 403 case per glm S1).
  `tests/test_migrations.py`: exact-set += entry; legacy DB gains NULL
  column; idempotent re-run. `tests/test_auth.py` untouched and green.
- **Not adopted**: glm A4 purge extension (buggy as sketched, unnecessary);
  kimi's deps restructure; kimi's subprocess env test; my `QrIdentifyIn`
  and uniform-false identify.

## 5. Changes to my own proposal

1. **Gate exemption flip** (biggest): my "flagged accounts aren't in the
   kiosk loop" is false under Task 5's *global* scan handler — exempt all
   three paths (glm S1), and my test 16 (`test_identify_respects_the_password_gate`
   → 403) inverts to the exemption pin.
2. **Identify error tier**: uniform-200-false → kimi's two-tier (401 on
   unparseable, uniform with qr-login; false only for well-formed
   non-matches).
3. **Body schema**: withdraw `QrIdentifyIn`; reuse `QrLoginIn`.
4. **Response naming**: `deadline` → `logoff_deadline`; keep
   `grace_seconds` (skew-resistant countdown start — kimi's
   deadline-minus-client-now is not skew-proof).
5. **Env floor**: `max(0,…)` → `max(1,…)`; my zero-grace expiry tests move
   to monkeypatch/DB-write (unchanged mechanics, since the floor is at env
   parse, not at use).
6. **Add** the delete-commit try/except (kimi R1), the clock-rewind
   rationale (glm S2), the frozen-`utcnow` boundary pin (proves `<=`), and
   the Task-5 contract paragraph (kimi R4).
7. **Keep** against peers: `<=` boundary, additive deps block, shared
   `_normalise_badge` helper, compare_digest, `min()`-clamp to
   `expires_at`, no purge extension.

## 6. Verdict

All three converge on the mechanism; the merged deltas are: glm's gate
exemption (with the Task-5 rationale that glm only half-argued), kimi's
delete-failure hardening / two-tier identify / grace floor / test designs,
my `<=` boundary + clamp + helper extraction, and the rejection of glm's
A4 — whose sketched predicate would have deleted **live armed tokens** on
every login and is the single most important line for the coordinator to
strike from the merged spec. §4 is the shape I recommend standardising on.
