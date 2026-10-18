# Task 5 proposal review (kimi-k3)

Reviews of `task5-proposal-glm.md` and `task5-proposal-qwen.md` against my
own `task5-proposal-kimi.md`. All three agree on the skeleton: cart-on-API
with a serialization chain and full-`CartOut` responses, one-time legacy
import (PUT-first, key always removed), document-level timing-based scan
capture with a shape gate, `handleScan` bus, identify→schedule/cancel logoff
overlay (singleton, timer hygiene, restart-on-rescan), new-session-first
save-then-switch, 404-driven intake modal with role-aware submit, staged
anonymous scans replayed after login, one-shot badge print modal, no JS
runner (ui_check + manual hardware checklist).

Both peers also corrected the task brief the same way I had implicitly:
`ui_check.py` is headless-Chrome `--dump-dom` + a self-proving console
parser — **no interaction capability**, so the interactive flows are
manual-checklist gated. GLM states it most explicitly; adopt the wording.

---

## 1. Review: glm

### Strengths (adopt)
- **Bounded scan queue with a drain loop.** A presentation scanner fires
  faster than a round trip; my proposal (and qwen's) effectively drops or
  ignores concurrent scans. GLM's `enqueueScan` + one-at-a-time drain is
  the right shape — the bridge's never-drop doctrine applied to the SPA.
  Adopt, with GLM's own suggested cap (~10, "scanner is running ahead"
  toast).
- **`err.data = body` on the thrown error** — same intent as my R7
  (qwen's `{raw:true}` option wins on ergonomics; see §2, but GLM saw the
  same gap: dict details currently flatten to "Request failed (404)").
- **Same-user overlay**: countdown computed from the server deadline
  timestamp, one interval id with disciplined clear paths, defensive
  `logoff-cancel` at the top of the switch flow (Task-4 belt-and-braces).
  All three converge; GLM's statement of the interval-hygiene rules is the
  cleanest.
- **Staff "Scan requests" admin tab** (create-and-resolve with
  `add_to_cart: true`, dismiss, status filter) — I had deferred the queue
  UI; GLM is right that it completes the member-request loop in this task
  rather than leaving a backend orphan. Qwen agrees. Adopt.
- **`@media print` badge isolation** — both peers independently flagged
  that the current print path prints the whole page (qwen verified: no
  print CSS exists at all). My proposal reused `window.print()` blindly.
  Adopt (qwen's body-class version, §2).

### Weaknesses / avoid
- **Editable-target handling loses kiosk scans.** GLM returns early when
  the target is an input — a scan that arrives while the catalogue search
  box (or any field) has focus is *silently dropped*. At a counter, fields
  have focus constantly; this is a real bug, not a theoretical one. Qwen's
  capture-with-suppression (or my refinement of it, §3) is required.
- **Legacy-import 404 retry parses missing ids out of the error string** —
  brittle (GLM itself flags it); qwen's PUT-first-then-per-entry-POST
  fallback (which my proposal already had) is the robust version.
- **Shape gate too loose**: `/^[A-Za-z0-9_-]{4,64}$/` routes a raw 43-char
  badge token to the *barcode* flow (opaque miss → 404) even though
  qr-login/qr-identify accept that form; and min length 4 invites more
  false positives than qwen's 6. Adopt qwen's gate.
- **`scan-<gtin14>` / `scan-<slug>` auto-generated SKUs** — collide and
  surprise; qwen's *editable prefill* (`analysis.gtin14 || code`) is
  better UX and keeps the 409 path meaningful.

### Disagreements vs. mine
- Scan queue: GLM queues, I ignored — GLM wins (adopted).
- Everything else is agreement or covered by qwen-adoptions below.

---

## 2. Review: qwen

### Strengths (adopt — most are load-bearing)
- **Capture-with-suppression instead of my focus-rule drop.** The decisive
  fix: from the *second* fast key of a burst, `preventDefault()` keeps the
  scan out of whatever field has focus, and the shape gate at Enter
  decides. Scans work even when an input is focused (the common kiosk
  state) — my focus rule would have lost exactly those scans. The
  documented trade-off (a sustained sub-50 ms human burst can lose
  characters before the shape gate clears it) is real but narrow; §3
  refines it further for editable targets.
- **`data-scan-sink` stand-down**: the login badge field and a visible
  scan field mark themselves and own their Enter handling — explicit, no
  double-processing, the shipped badge-field behaviour untouched.
- **The visible scan field** ("Scan items or badge here", catalogue
  header) — the deterministic no-heuristics path and the only route for
  sub-6-char internal codes the global gate deliberately ignores. I had
  dropped the field; qwen is right to keep it as a cheap affordance.
- **`api()` options `{token}` and `{raw:true}`** — the switch flow's
  revoke-old-AFTER-saveSession (decision 3) is *impossible* without an
  explicit-token override (the stored token is already the new one), and
  the scan surface's dict-details require the non-throwing mode (after the
  global 401 handler still runs). My call-logout-before-saveSession order
  works but is more fragile; adopt qwen's options and step order (secure
  new session → saveSession → best-effort revoke old with `{token}`,
  `{raw:true}`).
- **Countdown from `endsAt = receipt + grace_seconds*1000`** — avoids my
  naive-UTC `"Z"`-append pitfall entirely; sub-RTT display drift is
  irrelevant because the server deadline is the enforcement truth (Task 4).
  Adopt; my R2 evaporates.
- **`loadCart` skipped while `must_change_password`** — the cart endpoints
  403 under the gate; loading anyway spams errors on the forced-change
  screen. I missed this. Adopt (plus the switch flow's skip-and-route).
- **`logout()` defensively drops `PURCHASE_KEY`**; legacy-import semantics
  documented ("browser-global list → first logger-in on that browser").
- **Staging mirrored to localStorage** (`hcrm_staged_scans`, codes +
  timestamps only, cap 50) — survives a kiosk refresh, no secrets; better
  than my in-memory-only. Adopt (my R5 mostly evaporates).
- **`intakeQueue` for replayed unknowns** — modals one at a time, each
  skippable, skip = drop-with-toast (re-staging invites zombie queues).
  Better than my re-stage-the-rest. Adopt.
- **Shape gate precision**: badge = `^HCRM1:[A-Za-z0-9_-]{43}$` **or** bare
  43-char token; barcode = `^\d{6,14}$` or `^[A-Za-z0-9._/-]{6,64}$`;
  min length 6. Routes the raw-token badge form correctly and keeps humans
  out. Adopt over both my prefix-only gate and GLM's looser one.
- **Account-view "My scan requests"** (`GET /mine` status chips) — closes
  the member loop ("your report was resolved") that GLM's queue tab alone
  leaves open. Adopt alongside the staff tab.
- **Best-effort queue closure after staff create** (resolve any open
  request with the same code) — one pass, no new endpoints.
- **`@media print` body class** hiding everything but the badge modal —
  the first print CSS in the repo; the badge card prints as a card.
- **Copyable payload text in the badge-once modal** ("print or write it
  down") — in-memory only, cleared on close. I had over-restricted to
  SVG-only; showing the payload once is the point of the modal. Adopt
  (memory-only stands).
- **ui_check gate via static copy pins** ("Scanner ready" line,
  badge-field placeholder) — simpler than my `data-scan-capture` attribute
  and uses the existing copy-assertion mechanism. Adopt (the mount/
  zero-console-error checks remain the real regression value for a
  ~500-line app.js delta).
- **Increment slicing 5a/5b/5c** matches the coordinator's "lands
  incrementally" and is better factored than my four slices. Adopt.

### Weaknesses / rejected
- **The 403-on-identify fallback** (flagged account scans own badge →
  mint a session via qr-login, compare ids, revoke the spare, then arm).
  This is the one place qwen bends the identify design's purity, and it is
  disproportionate: a `must_change_password` account is pinned to the
  change-password screen and cannot use the API regardless — scanning does
  nothing useful for it. Recommend: on identify 403, toast "finish
  changing your password first" and ignore the scan. If the coordinator
  ever exempts qr-identify (GLM's Task-4 position), even that disappears.
  Rejected as complexity without a beneficiary.
- **Unbounded-by-default suppression** for bursts in editable fields (the
  char-eating trade-off above): keep, but refine per §3.

### Disagreements vs. mine
- Focus rule (mine) vs suppression (qwen): qwen wins — adopted with a
  refinement.
- Countdown basis (mine: parse deadline; qwen: receipt + grace): qwen
  wins — adopted.
- Staging persistence (mine: memory; qwen: localStorage): qwen wins.
- Unknown replay (mine: re-stage; qwen: intakeQueue + drop-on-skip): qwen
  wins.
- api() extension (mine: err.status; qwen: options): qwen wins.

---

## 3. Final recommended shape

**Base: qwen's proposal**, with GLM's bounded scan queue and one
refinement of the capture layer:

1. **Cart**: computed `purchase() { return this.cart.lines }` keeping the
   template diff minimal (qwen); serialization chain; full-`CartOut`
   responses; `loadCart` skipped under the password gate; PUT-first legacy
   import with per-entry fallback, key removed after processing (never
   before); `_stockWarn` replaces the client-side block (decision 6);
   `logout()` drops the legacy key.
2. **Capture**: qwen's accumulator (50 ms, min-len 6, `data-scan-sink`
   stand-down, shape gate incl. raw-43 badge form, visible scan field,
   scan-flash indicator) + GLM's bounded queue (~10) feeding a one-at-a-time
   drain. **One refinement of my own, kept**: in *editable* non-sink
   targets, buffer without `preventDefault` and record the field's
   pre-burst value; on a shape-gated Enter, restore the value and route
   the scan; on an unshaped Enter, do nothing — human typing in fields is
   then *never* mangled (qwen's char-eating edge closes), while scans
   landing in focused fields still work (my original bug stays fixed).
   Suppression (qwen's preventDefault-from-2nd-key) applies only to
   non-editable targets, where it is free.
3. **Modal discipline**: barcodes dropped-with-toast while any modal is
   open (the queue does not buffer *into* modals); badges always process
   (restart/cancel-and-switch); logoff overlay singleton with 250 ms tick,
   Esc = stay, timer cleared on every exit path incl.
   `hcrm-session-expired`.
4. **Switch**: `flushCart` → `qr-login` → `saveSession` → best-effort
   `logoff-cancel` and `logout` on the *old* token via `{token, raw:true}`
   → `loadCart` (skipped when flagged) → toast. Identify 403 → toast, not
   qwen's mint-and-revoke fallback.
5. **Barcode flows**: `{raw:true}` scan call; misread toast; 404 →
   `intakeQueue`; intake modal per qwen (context header, prefix/keyword
   groups, live suggest with `by_semantic` while typing, editable SKU
   prefill, staff create + best-effort queue closure + own-cart add,
   member request with both 409 policies); staff "Requests" admin tab
   (resolve with `add_to_cart` checkbox) + account "My scan requests";
   staging to localStorage with banner on login/register, replay after any
   login, skip=drop.
6. **Badge print**: shared `badgeOnce` modal (copyable payload, once-only
   warning, `@media print` isolation body class) for register,
   member-create, and the existing rotate flows.
7. **Verification**: server suites own semantics; ui_check extended with
   static copy pins only ("Scanner ready", badge-field placeholder,
   register card; zero console errors with the listener installed — the
   mount check is the syntax regression gate); qwen's Layer-3 manual
   hardware checklist verbatim (incl. the close-tab-mid-countdown Task-4
   observation and the old-token-revoked curl check).
8. **Slicing**: 5a cart → 5b capture+badge flows → 5c barcode
   flows+modals+print+ui_check.

## 4. Changes to my own proposal

1. **Capture**: replace my focus-rule-drop with qwen's
   capture-with-suppression, plus my editable-target refinement (record &
   restore pre-burst value; suppress only on non-editable targets); add
   the visible scan field and `data-scan-sink`; adopt qwen's shape gate
   (raw-43 badge form, min-len 6); add GLM's bounded scan queue + drain.
2. **api()**: adopt `{token}` + `{raw:true}` options (replacing my
   err.status suggestion and the call-logout-before-saveSession switch
   order).
3. **Countdown**: receipt + `grace_seconds` (drop my `"Z"`-parse and R2).
4. **Staging**: localStorage-backed, capped (drop my memory-only + R5);
   unknowns → `intakeQueue`, skip = drop (drop my re-stage).
5. **Add**: `loadCart` password-gate skip, `logout()` legacy-key drop,
   staff Requests tab, account `/mine` list, best-effort queue closure,
   `@media print` isolation, copyable payload in the badge modal,
   editable SKU prefill (replacing both my fixed gtin14-as-SKU and GLM's
   auto-gen).
6. **Reject**: qwen's 403-identify mint-and-revoke fallback (toast
   instead).
7. **Verification**: replace my `data-scan-capture` attribute with
   qwen's static copy pins; adopt qwen's Layer-3 checklist and 5a/5b/5c
   slicing.
8. **Keep from mine**: modal suspension semantics for barcodes (qwen
   agrees), overlay-singleton + timer-hygiene rules (all agree), the
   stock-warn line class, the R4 flush-ordering note, and the editable-
   target restore refinement (§3.2 — the one thing neither peer has).
