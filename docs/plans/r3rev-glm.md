# Round 3 cross-review — glm-5.3

Reviewed: `docs/plans/r3plan-kimi.md` and `docs/plans/r3plan-qwen.md` (read in
full), against my own `docs/plans/r3plan-glm.md` and the tree. Spot-verified
the contested claims: `static/index.html:139` does carry the "(30 s countdown)"
copy (both peers caught it, I missed it); `app/schemas.py:154`,
`app/routers/auth.py:266,271`, `NOTES.md:279` also say 30; the M-10 is dead
(`docs/m10-unreachable-2026-10-10.md`); `PATCH /api/cart/items/{id}` is an
absolute set with `qty == 0` removing the line (`app/routers/cart.py:277-297`),
so qwen's `setLineQty` is grounded; `afterLogin` really does run
`loadCart` → `importLegacyPurchase` (toasts) → `replayStagedScans` (toasts per
scan) before routing, so a toast-based greeting IS clobbered on the
returning-kiosk-user path.

## Verdict at a glance

| Topic | Mine | Kimi | Qwen | Winner |
|---|---|---|---|---|
| Capture mechanism | defer-all + flush timer | hold-and-flush, uniform | defer + **WARM fast-path** + retract | **qwen** (with one fix, below) |
| Replay write path | `execCommand` + fallback | `setRangeText` | manual splice + `setSelectionRange` + `InputEvent` | **qwen** (deterministic, stub-able) |
| Target classification (select/number/contenteditable) | none | partial (pass-through only) | full table (§3.3) | **qwen** |
| Testability | source-contract pins only | reducer + pytest→node subprocess | extraction + `node --test` + injected clock + separate CI job + pure-move T0 | **qwen** (kimi close second) |
| Greeting | toast (defective) | toast (defective) | **banner** (D6) | **qwen**, decisively |
| Enter on unshaped burst | replay + native default | `preventDefault` + `form.requestSubmit()` | replay + native default | **mine/qwen** |
| `isTrusted` guard | skip untrusted | — | deliberately absent | **qwen** (simulator evidence needs it) |
| Review screen | dedicated `#/review` view | dedicated `#/review` + search-to-add | `#/checkout` reusing the catalogue section, review panel as the aside | **qwen** (with kimi's skipped-toast folded in) |
| Receipt modal after done | keep always | drop + toast `skipped` | **exception-only** (skipped/clamped) | **qwen** (doctrine-faithful) |
| Calendar endpoint | `?month=YYYY-MM` dict-of-days | `?from&to` rows, client buckets LOCAL days | `?year&month` dict-of-days, UTC days | **qwen** |
| Calendar colour | HSL 120→0 | HSL 140→0 | HSL 120→0 + lightness ramp | **qwen** (lightness ramp reads better) |
| Copy sweep for 60 s | security.py + test only | + index.html | + schemas/auth docstrings, README, NOTES, contract pin | **qwen** |
| Sequencing | backend-first, 6 tasks | backend-first table | T0–T7 with PR sequence + serialisation rule | **qwen** |

Overall: qwen's plan is the strongest — deepest grounding (its §0.1 latent
defects are real), the only one that caught the greeting-toast clobbering, and
the only one that solves the every-keystroke-latency problem (WARM). Kimi's is
solid and more compact, with two genuinely good catches (the 30 s copy, the
skipped-items surfacing). My plan had the right skeleton (backend-first,
no-new-cart-state review, backend calendar aggregate, backend aggregate
rationale identical to qwen's D4) but three real defects, all fixed by adopting
qwen's designs.

## Kimi — strengths to adopt

1. **The "30 s countdown" copy at `index.html:139`** — neither I nor my test
   list would have caught the stale UI number. Adopted into the copy-sweep
   task (qwen's T1 list is the superset: schemas docstring, auth docstring,
   README, NOTES, plus a contract pin tying the copy to the parsed default).
2. **Surfacing `CheckoutOut.skipped` when the receipt modal is dropped** —
   kimi's toast ("N item(s) no longer exist and were skipped") is the right
   fallback; folded into qwen's exception-only modal as the toast that always
   accompanies a clean checkout (total), with the modal reserved for
   skipped/clamped.
3. **Held-key-repeat misroute guard** — kimi's "single repeated character
   flushes as human" is subsumed by qwen's cleaner `e.repeat` check, but the
   *concern* (holding "1" + Enter misroutes as a GTIN) is real and kimi
   raised it first. Adopted via `e.repeat`.
4. **Explicit "nothing lost, nothing reordered" framing** of the human path —
   good review language; qwen's invariants section formalises it.

## Kimi — weaknesses / risks

1. **`form.requestSubmit()` on unshaped Enter** is strictly worse than
   replay-then-native-Enter: it approximates the default action (no implicit
   submit semantics, no button-click event) for no benefit — once the held
   chars are spliced synchronously, simply NOT preventing the Enter gives the
   exact native behaviour. Reject.
2. **`setRangeText` flush breaks on `input[type=number]`** (`selectionStart`
   throws `InvalidStateError` there — the admin price/stock and intake price
   fields are number inputs). Kimi passes SELECT/contentEditable through but
   not number. Qwen's per-type classification table is the complete answer.
3. **LOCAL-day bucketing for the calendar** (client converts local month
   bounds to UTC, buckets returned rows into local days) makes dots disagree
   with the list: `formatDate` (`app.js:350-352`) parses the naive-UTC string
   as local, so the list shows the UTC wall-clock date; a local-bucketed dot
   for a 23:30 UTC purchase lands on the next local day while its list row
   stays on the UTC date. Qwen's UTC bucketing (R6/Q5, option a) keeps dot
   and row in agreement. Reject local bucketing.
4. **pytest→node subprocess tests** make Node a prerequisite for
   `uv run pytest` on every dev machine. Qwen's separate CI `js` job keeps
   the pytest surface Node-free (the repo's hygiene tests scan the tree;
   keeping concerns separated is cheaper). Adopt qwen's D2; kimi's approach is
   the fallback if the coordinator refuses a new CI job.

## Qwen — strengths to adopt (the bulk of the final design)

1. **§0.1 latent defects** (select.value wipe resets the category filter;
   number-input wipe; caret not restored). The mechanism is being replaced,
   but these defects dictate the new target-classification table — e.g. why
   `SELECT` gets `selectedIndex` snapshot + `change` dispatch, and number
   inputs are never *held*. Genuine repo archaeology my plan lacked.
2. **D1 / WARM window** — the best idea in either peer plan. My design held
   EVERY keystroke until the next event/flush timer (sustained typing renders
   one char behind); kimi's holds every burst-initial char (≤50 ms echo on
   every run start). WARM makes typing fully native after the first char of a
   run, and the residual artifact (one char, one frame, only when a scan
   starts <400 ms after a human keystroke in the same field) is a race that
   essentially cannot happen at a kiosk. The `SCAN_STRICT_DEFER` constant
   makes the trade-off a coordinator knob, not a redesign. Adopt with one
   fix (see weaknesses #1).
3. **D6 greeting banner** — the decisive catch of the review. `afterLogin`
   toasts (legacy-cart import, staged-scan replay) would overwrite a toast
   greeting within milliseconds on exactly the returning-user paths where a
   greeting matters. A dedicated banner state (set in `saveSession`, NOT
   cleared in `_closeOverlays` — the switchAccount ordering subtlety —
   cleared in `logout`/`handleSessionExpired`) is correct. My and kimi's
   toast designs were defective. Adopt wholesale.
4. **D2 testability** — pure-move T0 (extract shape gates, prove the
   browser/Node/CI loop with zero behaviour change), injected `now`/timers,
   DOM stub, ~30-case matrix, `SCAN_DEFER_MS > SCAN_MAX_GAP_MS` pinned twice.
   This is the right rig for the riskiest change of the round.
5. **D3 review screen as the catalogue section with a swapped aside** — one
   grid, one `#sentinel`, the IntersectionObserver wiring untouched, and
   "add" works by clicking catalogue items directly (the most literal reading
   of "items can still be modified … add"). Avoids duplicating the grid
   markup a dedicated screen would need. Adopt (see disagreement #3 for the
   counter-argument).
6. **D5 exception-only receipt modal** — preserves the clamp-not-veto
   reporting doctrine (`skipped`/`clamped` must be reported, never silently
   dropped) without a blocking dialog on the happy path. Strictly better than
   my "always keep" and kimi's "drop + toast".
7. **Pre-set `this.route` before `location.hash`** on every programmatic
   navigation — the `hashchange` handler's `navigated` test fires
   `_closeOverlays()` otherwise and kills the post-checkout receipt modal.
   The repo's own `mounted()` comment documents the pattern; both peers
   other than qwen (and my plan) were careless here.
8. **T1 copy sweep** (schemas docstring, auth docstring, README, NOTES, and a
   contract pin tying the index.html copy to `app/security.py`'s parsed
   default so the two cannot drift).
9. **Calendar details**: `Date.UTC` matrix construction (a local-midnight
   `new Date(y, m-1, d)` shifts keys for users west of UTC), HSL with a
   lightness ramp (a 2-stop RGB lerp goes muddy), `total_cents` = paid
   amounts (Σ `applied_qty × price` — `Purchase.total_cents` is already that
   sum, so grouping on it is correct), half-open month range tests,
   `days`-key format regex pin, and the cheap prev/next month nav the
   endpoint enables.
10. **`isTrusted` deliberately unchecked** — with the M-10 dead, the only
    verification path is console-dispatched `KeyboardEvent`s, which are
    `isTrusted: false`. My proposed `!e.isTrusted → pass through` guard would
    have disabled the simulator. The residual risk (synthetic fast-typing
    automation being swallowed) is theoretical; adopt qwen's stance and pin
    it with a contract test.

## Qwen — weaknesses / risks (and the fixes)

1. **BURST state has no timer.** In qwen's state diagram the 60 ms defer
   timer exists only in HELD; a burst whose source dies mid-stream (scanner
   unplugged, or a human's two sub-50 ms keystrokes followed by silence)
   leaves chars held until the next keydown/pointerdown. **Fix: re-arm the
   defer timer on EVERY held/burst key.** `SCAN_DEFER_MS (60) >
   SCAN_MAX_GAP_MS (50)` guarantees it never fires mid-scan (inter-key
   10–20 ms), and it makes "no keystroke is ever lost" hold by timer alone,
   not by relying on the pointerdown/focusin flushes. This is the one
   substantive change I'm making to qwen's design.
2. **Scope/weight.** T0 + a CI job + two extracted modules is a lot of
   infrastructure for one round. It's justified by T3's risk profile (a
   rewrite of the repo's most subtle frontend code, with hardware dead), but
   the coordinator should be aware the round is now ~8 PRs. The trim line, if
   needed: T0's pure-move must stay (it de-risks everything after); the
   calendar's `purchase-calendar.js` extraction is the first thing to fold
   back inline if review pressure demands.
3. **WARM's one-char/one-frame leak** technically violates the task's "never
   appears" letter in a rare race. Mitigated by the strict-mode constant and
   the bounded, documented exception. Accept with the coordinator informed;
   default remains WARM.
4. **`v-model.number` on admin price/stock inputs** listens to `change` —
   qwen classifies number inputs as never-held (correct), worth keeping as an
   explicit test, not just a table row.

## Disagreements resolved

1. **Unshaped-Enter semantics** — mine/qwen's replay-then-native-Enter over
   kimi's `requestSubmit()`. No contest: the native default is exact.
2. **`isTrusted`** — qwen over mine. Simulator-based evidence beats a
   guard against a theoretical synthetic-typing hazard.
3. **Review screen shape** — qwen's D3 over kimi's/mine dedicated view.
   Decisive factors: zero duplicated catalogue markup (one grid, one
   sentinel, the observer wiring survives), and "add" is the literal click
   path rather than a search box (kimi's debounced `/items?q=` search-to-add
   is scope creep on top of a scan-capable SPA). If the coordinator reads
   "review screen" as needing a distinct, grid-free screen, kimi's layout is
   the fallback — but then "add" needs the search box or a back button,
   which is strictly more code.
4. **Calendar timezone** — qwen's UTC bucketing over kimi's local bucketing.
   Dot/list agreement is the observable invariant; UTC is the only bucketing
   that preserves it given `formatDate`'s current parse. (Fixing `formatDate`
   to append `Z` changes every date in the app — out of scope, noted as a
   follow-up.)
5. **Calendar endpoint shape** — qwen's `?year&month` (int `ge/le`
   validation free, defaults to the current UTC month so the SPA can call it
   bare, month nav for free) over kimi's `?from&to` rows (client buckets,
   more client logic, dot/list drift) and my `?month=YYYY-MM` string (fine,
   but the ints get validation for free and match the house `Query` style).
6. **Greeting delivery** — qwen's banner over both toast designs (clobbering
   bug, see above).
7. **Node in CI** — qwen's separate `js` job over kimi's pytest-subprocess
   (keeps `uv run pytest` Node-free).

## Where my own plan was wrong

Honest ledger, for the record:

- **Greeting via toast was defective** (clobbered by `importLegacyPurchase`/
  `replayStagedScans` toasts on the returning-user path). Qwen's banner is
  the fix.
- **Deferred EVERY keystroke** with a ~60 ms worst-case flush; qwen's WARM
  gives native typing after the first char of a run. Mine also relied on
  `execCommand` (deprecated, not stub-able in a Node harness) instead of the
  deterministic splice.
- **`isTrusted` guard** would have disabled the only available verification
  path (hardware is dead).
- **Missed the 30 s copy sweep** (`index.html:139`, docstrings, NOTES).
- **No target classification** — my `_scanReplay` would have hit the same
  number-input/select defects qwen's §0.1 documents, because I never
  classified targets.
- **No per-type caret restoration** and no WARM/retract path.
- Right calls I'll keep: backend-first ordering; the no-new-cart-state review
  screen (all three plans converged); the backend calendar aggregate and its
  rationale (identical to qwen's D4, contra pure client-side); landing T3
  alone with `ui_check` + manual pass; flagging UTC bucketing, env-override,
  and receipt-modal questions (all resolved above).

## Final recommended task order

Qwen's T0–T7 sequence, adopted with the amendments above. Serialisation rule:
all frontend tasks touch `static/app.js`/`index.html`/`style.css` → strictly
serial; T0/T1/T2 are parallelisable.

1. **T0** — JS test harness + extraction convention (pure move of the shape
   gates to `static/scan-capture.js`, dual-mode export, script tag before
   `app.js`, `tests/js/` with DOM stub + manual clock, new CI `js` job,
   `tests/test_frontend_contract.py` wiring pins). No behaviour change.
2. **T1** — grace 30→60: `app/security.py` default, `app/routers/auth.py` +
   `app/schemas.py` docstrings, `static/index.html:139`, `README.md`,
   `NOTES.md`, `tests/test_logoff.py` (default assertions 30→60 + a
   default-is-60 regression test), contract pin tying the copy to the parsed
   default.
3. **T2** — `GET /api/orders/calendar?year&month` (dict-of-days, UTC days,
   defaults to current UTC month, `get_current_user` gate) +
   `tests/test_orders_calendar.py` (qwen's 8-case list, incl. the
   paid-amount/clamp case and the `days`-key format pin) + a
   `scripts/smoke_test.py` check.
4. **T3** — the capture rewrite: IDLE/HELD/BURST/WARM state machine in
   `static/scan-capture.js`, per-type target classification, `e.repeat` +
   `isComposing` + pointerdown/focusin flushes, **defer timer re-armed on
   every held/burst key** (my fix), `SCAN_DEFER_MS > SCAN_MAX_GAP_MS` pinned
   in Node and pytest, `enqueueScan`/routing bus untouched, sinks untouched,
   no `isTrusted` check. Node matrix + `ui_check --self-host` + simulator
   transcript (hardware dead).
5. **T4** — greeting: banner state set in `saveSession`, not cleared in
   `_closeOverlays`, cleared in `logout`/`handleSessionExpired`, 4 s
   auto-clear, `role="status"`; drop `doRegister`'s duplicate toast; keep the
   badge-modal subtitle; `switchAccount` toast loses the "Switched to X" half
   (the banner carries it).
6. **T5** — review screen: `#/checkout` reusing the catalogue section with a
   review-panel aside; `goToCheckout`/`donePurchase` replace
   `completePurchase`; `setLineQty` (PATCH absolute, 0 removes, 422 →
   revert to server truth); exception-only receipt modal + always-toast
   (total, or skipped note); empty-cart bounce; topbar active-state fix; all
   programmatic navigations pre-set `this.route`.
7. **T6** — calendar UI: `static/purchase-calendar.js` (Date.UTC month
   matrix, `dayColour` HSL 120→0 with lightness ramp, `monthSummary`),
   two-column `.history-layout` collapsing at 900 px, dots with title
   tooltips, month total (+ count) below, refresh on tab entry and after
   checkout; prev/next nav (cheap, endpoint already supports it).
8. **T7** — docs + verification pass (README/NOTES, ui_check pin that
   `/scan-capture.js` is served, round3-task status, evidence bundle per PR).

## The single best designs (canonical, for the coordinator)

**Capture interception** — qwen's §3 state machine with the BURST-timer fix:
first printable key of a run is held (`preventDefault`, 60 ms defer timer,
re-armed on every subsequent held key so nothing can hang); a second sub-50 ms
key confirms a burst (everything stays held, routed at a shape-gated Enter,
never painted); the timer firing or a slow gap proves human → splice the held
chars at the recorded caret + synthetic `input` → element goes WARM for 400 ms
(native typing, snapshot taken, retract-on-burst-confirm bounds the leak to one
char for one frame). Per-type classification decides holdable vs
snapshot-and-restore vs pass-through (SELECT, number, contenteditable).
`e.repeat`/`isComposing`/modifiers/pointerdown/focusin all flush-or-pass.
Sinks return before any of it. `SCAN_STRICT_DEFER` flips to the absolute
guarantee. Extracted to `static/scan-capture.js`, Node-tested with an
injected clock.

**Calendar aggregate** — `GET /api/orders/calendar?year=&month=` (defaults:
current UTC month; `ge/le`-validated ints), auth = `get_current_user`,
SQLModel range query over the indexed `user_id` + half-open
`[first-of-month, first-of-next-month)` UTC window, Python-side bucketing by
`created_at.date().isoformat()` into
`{days: {"YYYY-MM-DD": {cents, count}}, total_cents, purchase_count}` (cents
= the receipts' `total_cents`, i.e. paid amounts). Gradient stays
client-side (presentation, not data). Timezone: UTC days, documented, so dots
agree with the list's `formatDate` rendering.

**Review screen** — the hash route `#/checkout` renders the SAME catalogue
section with the aside swapped to a review panel; state is the server cart
itself (no copy, no sync); every control is an existing `_cartOp`-serialized
mutation (`incPurchase`/`decPurchase`/`setLineQty`/`removeFromPurchase`/
`clearPurchase`/`addToPurchase`/scan-to-add); "Done" is today's
`completePurchase` body minus `confirm()`, still inside `_cartOp`, then
pre-set-route navigation to `#/account` + purchases tab with the new receipt
pre-expanded, a total toast always, and the receipt modal only when the
checkout reported `skipped` or `clamped` lines.

## Residual open questions for the coordinator

1. **WARM vs strict defer** (qwen Q1) — default WARM; one constant flips it.
2. **Receipt modal policy** (qwen Q4 / kimi 5) — default exception-only.
3. **Review screen as catalogue+panel vs a distinct grid-free screen**
   (qwen D3 vs kimi/mine) — default qwen's; kimi's is the fallback if
   "screen" is read strictly.
4. **Month navigation on the calendar** (qwen Q7) — spec says current month
   only; nav ships because it is ~10 lines on top of the endpoint. Keep or
   trim.
5. **Q2 (pre-switch "Switch to <name>?" prompt)** — both kimi and qwen
   recommend NO change (enumeration/privacy cost in `QrIdentifyOut`); the
   post-switch toast already names both users. I concur.
6. **Node CI job** (qwen Q8 / kimi 8) — default separate `js` job; kimi's
   pytest-subprocess is the fallback. Either way a missing Node must fail,
   not skip (repo doctrine).
