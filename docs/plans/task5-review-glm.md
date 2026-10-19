# Task 5 cross-review (glm) — of `task5-proposal-kimi.md` and `task5-proposal-qwen.md`

Reviewer context: my own proposal is `docs/plans/task5-proposal-glm.md`. All
three converged on the fundamentals before review: cart-on-API with
full-`CartOut` mutators and the deleted localStorage watcher; PUT-first
legacy import; document-level capture-phase keydown accumulator (50 ms
timing, Enter terminator, focus exclusion); one `handleScan` bus;
identify→overlay / switch with new-session-first + old-token revoke;
stage-and-replay attribution; role-aware intake modal; one-shot badge print;
ui_check DOM/console pins + a manual M-10 checklist (all three independently
corrected the brief's "puppeteer" characterisation). The review arbitrates
the deltas — and finds one real bug in qwen's capture design.

---

## 1. Kimi

**Strengths (adopt):**
- **The `_cartOp` promise-chain queue** — cleaner than my "sequential by
  construction" hand-wave: rapid +/− clicks and scan-adjacent mutations are
  serialized, and `flushCart() = await this._cartQueue` is the switch flow's
  flush made mechanical. Adopt (qwen has the same idea, `_enqueueCart`).
- **PUT-then-per-entry-POST fallback** for the legacy import: a deleted
  legacy item poisons the atomic PUT (all-or-nothing 404); re-POSTing per
  entry and skipping individual 404s is simpler and more robust than my
  "parse the missing ids out of the 404 detail and retry once". Adopt
  (2-of-3 — qwen agrees).
- **The naive-UTC `"Z"` append gotcha** (R2): `Date.parse(deadline)` without
  appending `"Z"` parses local time and skews the countdown by the timezone
  offset — exactly the kind of bug a manual checklist would catch late.
  Pinned with a code comment. Adopt.
- **Overlay has no ✕ / no backdrop dismiss** — leaving the countdown is a
  deliberate act. Good discipline; adopt.
- **R3 (session-expired during the overlay must kill the timer)** and **R6
  (intake modal during an armed countdown: the countdown keeps running
  underneath — server-enforced; expiry mid-modal is the desired safety
  property)** — both sharp interaction notes.
- R7's self-correction to "teach `api()` to attach `err.status`/`err.detail`
  rather than an `apiRaw` duplicate" — converges with my approach.

**Weaknesses / rejected:**
- **No barcode shape gate** (§2.4: "no GTIN/shape validation client-side…
  a garbage one costs a 404/422 toast"). The 404 is not a toast — it opens
  the **intake modal** (kimi's own §3c): a fast-typed word + Enter on the
  page background would pop a name+price form for "hello". The focus rule +
  timing + Enter leaves that class open; a minimal shape gate (mine/qwen)
  closes it. Reject.
- **In-memory-only staging** (R5, with sessionStorage "if the coordinator
  wants it"): a kiosk page refresh is a routine event, not an edge case;
  qwen's localStorage stash (codes only, capped, Discard button) costs three
  lines and survives it. Adopt qwen's.
- **The staff ScanRequest queue is deferred** (Q2: "out of this task's
  scope") — but the task brief explicitly lists "staff create item +
  **resolve ScanRequest**", the endpoints exist (Task 3), and two of three
  proposals include the admin tab. Include it.

**Disagreements with mine:** the cart queue (kimi wins), the import
fallback (kimi wins), no-shape-gate (I win), staging persistence (qwen
wins over both of us).

## 2. Qwen

**Strengths (adopt):**
- **The `{raw: true}` and `{token}` `api()` options** — the scan flow wants
  status-branching, not exception control flow, and `raw` still runs the
  global 401 handler first (so natural expiry mid-scan behaves exactly as
  today). The token override is what makes best-effort old-token revocation
  clean regardless of ordering. Adopt `raw`; `token` is optional given the
  2-of-3 logout-before-saveSession ordering (see below).
- **The visible scan field** (`data-scan-sink`, catalogue header) as the
  deterministic no-heuristics path — it covers sub-min-length internal
  codes the global gate deliberately ignores, gives operators an
  explicit-intent affordance, and doubles as the fallback when the timing
  heuristic misfires on odd hardware. Cheap and genuinely useful. Adopt.
- **Bare-43-char badge routing** in the shape gate (`^[A-Za-z0-9_-]{43}$` →
  badge flow): qr-login/qr-identify both accept the prefix-stripped form, a
  scanner that strips `HCRM1:` produces exactly that, and a 43-char opaque
  SKU is not a realistic label. My gate missed it. Adopt.
- **`loadCart()` skipped while `must_change_password`** — the cart endpoints
  403 under the gate; loading would just toast noise on the way to the
  change-password screen. Small, real. Adopt.
- **The `@media print` discovery**: there is **no print CSS in the repo at
  all** — the shipped memberBadge "Print" button prints the whole page.
  Qwen adds the first `@media print` block (body class isolating the badge
  card). Both kimi and I assumed print isolation existed. Genuine catch;
  adopt.
- **`intakeQueue` with skip=drop** (R8): replayed unknowns open modals one
  at a time, each skippable, skip drops with a toast (the code is
  re-scannable; stashing invites zombie queues). More complete than my
  "[Details] buttons on the banner" and kimi's "first unknown only".
  Adopt.
- **Editable SKU field prefilled `analysis.gtin14 || code`** for staff
  intake — better than my hidden auto-generation (`scan-<gtin14>`): staff
  can correct, the prefill is meaningful, and ItemIn requires a SKU anyway.
  Adopt. Likewise the **staff pre-check** (`GET /items?barcode=` before
  `POST /items`) delivering "already filed as <sku> — add that instead"
  without parsing the 409 string detail.
- **localStorage staging** (`STAGING_KEY`, codes+timestamps, cap 50,
  Discard) and the **3-slice landing plan** (5a/5b/5c matching the
  coordinator's "cart UI first, then capture+flows, then modals").
- The always-rendered "Scanner ready — barcodes and badges are captured
  automatically" login copy: user-facing text that doubles as the ui_check
  pin. Neat.

**Weaknesses / rejected — including one real bug:**
- **The accumulator has no blanket focus rule, and its `preventDefault`
  eats held-key typing.** Qwen stands down only for `data-scan-sink`
  elements, then `preventDefault`s every fast key from the second onward
  "so it doesn't pollute the focused field". Consequence: OS key-repeat
  fires at ~30 ms — **holding any key in any non-sink input (the search
  box, the intake form) enters the "fast" regime and gets its repeats
  swallowed**. The one-char leak he documents is not the issue; the
  held-key starvation is. The fix is the blanket rule kimi and I use —
  stand down for *any* input/textarea/select/contenteditable target — which
  also makes `preventDefault` unnecessary (buffered chars only ever occur
  when focus is on non-editable targets, so there is nothing to pollute)
  and makes `data-scan-sink` redundant except for the visible scan field's
  own Enter handling. Reject qwen's capture mechanics; keep his shape gate
  additions and the visible field (marked as an input, so the blanket rule
  stands down for it and its own handler runs).
- **The flagged-session fallback** (§3.2's 403 branch: qr-login, compare
  ids, revoke the spare, proceed): re-introduces the mint-and-revokes
  pattern Task 4 explicitly rejected, for a corner case the UI already
  funnels out of (a flagged account sits on the change-password screen; its
  badge scan can toast "change your password first" and its logout — which
  *is* exempt — remains the escape hatch). Over-engineering. Reject; a
  toast suffices.
- **Receipt-based countdown** (`endsAt = Date.now() + grace_seconds`):
  robust to clock skew, but the Task-4 contract returns the deadline
  precisely so the display tracks the enforcement truth, and the skew case
  (kiosk clock wrong by more than request latency) is far rarer than the
  contract-conformance argument. Keep **deadline-based** (2-of-3), with
  kimi's `"Z"` append pin; qwen's variant noted as the alternative.

**Disagreements with mine:** `{raw}`/`{token}` options (qwen's raw adopted;
my err-attachment dropped), visible scan field (qwen wins), 43-char badge
routing (qwen wins), staging persistence (qwen wins), editable SKU (qwen
wins), intakeQueue (qwen wins), print CSS (qwen's catch), loadCart-skip
(qwen), deadline-vs-receipt countdown (I win, 2-of-3), capture mechanics
(I win — his has the held-key bug).

## 3. Final recommended shape

**Capture** (merged): document-level capture-phase keydown, always on;
**blanket focus rule** (any editable target → stand down; the login badge
field and the new visible scan field keep their own handlers); 50 ms
inter-key timing, ≥6-char minimum, Enter terminator; **shape gate** at
Enter — badge = `^HCRM1:[A-Za-z0-9_-]{43}$` **or** bare
`^[A-Za-z0-9_-]{43}$`; barcode = `^[0-9]{6,14}$` or opaque
`^[A-Za-z0-9._/-]{6,64}$`; unshaped → discarded silently, Enter keeps
default; **no preventDefault** (nothing to pollute under the blanket rule);
**modal suspension**: barcode scans toast-and-drop while any modal is open,
badge scans stay live during the logoff overlay (restart / cancel+switch);
scan-status pill flash; **visible scan field** (catalogue header) as the
deterministic path for short codes and odd hardware; scans queued and
drained one at a time.

**Bus & flows**: as all three converged — identify → overlay (countdown
**from the server deadline**, `"Z"`-appended parse, no backdrop dismiss,
single timer with disciplined teardown, re-scan re-arms) / switch
(flushCart → qr-login **first** → logout with the old token → saveSession →
loadCart, flagged → change-password routing); `raw`-mode cart/scan with
status branching; intake modal from an `intakeQueue` (skip = drop),
editable prefilled SKU for staff, pre-check-before-create, member →
ScanRequest with both 409s handled; attribution = localStorage-staged
codes + banner + replay-after-any-login; admin Requests tab (Create &
resolve with an `add_to_cart` checkbox) + account "My scan requests".

**Cart**: kimi's `_cartOp` chain, PUT-then-per-entry legacy import,
stock-warn-not-block flip, loadCart skipped while flagged.

**Verification**: ui_check copy pins (incl. the always-rendered "Scanner
ready" line) + zero-console/mount gates; the detailed manual M-10
checklist (union of all three, incl. qwen's second-tab deadline
observation and old-token-revoked curl check); pytest stays the semantics
gate.

**Slicing**: qwen's 5a (cart) → 5b (capture + badge flows) → 5c (barcode
flows + modals + print + ui_check pins).

## 4. Changes I am making to my own proposal as a result

1. Adopt the **`_cartOp` mutation queue** as the explicit flush primitive
   (replacing my "sequential by construction" argument).
2. Legacy import: replace my parse-the-404-and-retry with the **per-entry
   POST fallback**.
3. Add the **visible scan field**; raise the min length to 6 (the field
   covers shorter internal codes); add the **bare-43-char badge form** to
   the shape gate.
4. Add **modal suspension semantics** (barcodes toast-and-drop; badges live
   behind the logoff overlay).
5. Adopt `api()`'s **`{raw: true}`** option (drop my err-attachment design)
   and the optional `{token}` override.
6. **Staging to localStorage** (capped, Discard button) instead of
   in-memory.
7. Intake: **editable prefilled SKU**, **staff pre-check before create**,
   and the **intakeQueue with skip=drop** replacing my banner-[Details]
   design.
8. Admin **Requests tab with an `add_to_cart` checkbox** (not hard-coded
   true) + the account **/mine** list.
9. Add the **`@media print` block** (the repo has none — qwen's catch),
   the **loadCart-skip-while-flagged**, and the **`"Z"` append** pin on the
   deadline parse.
10. Rejected from peers (recorded): qwen's sink-only focus rule +
    preventDefault (held-key starvation bug), qwen's flagged-session
    mint-and-revoke fallback, kimi's shape-gate-less routing (garbage would
    open the intake modal), kimi's deferral of the staff queue.
