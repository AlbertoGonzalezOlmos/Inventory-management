# Round-3 cross-review — kimi (of r3plan-glm.md rev 2 + r3plan-qwen.md)

My rev-1 plan: `docs/plans/r3plan-kimi.md`. Verdict up front: the two peers
converged on a design that is better than mine on four specific points, and I
adopt them. Where they differ from each other the differences are small
(glm rev 2 already absorbed most of qwen). My plan has been revised to rev 2;
the delta is listed at the end.

---

## qwen (r3plan-qwen.md)

### Strengths to adopt

1. **§0.1 latent defects in today's rollback — real, verified, and mine missed
   them.** (a) The restore path dispatches `input`, but a `<select>` v-model
   listens to `change`: a scan while the category `<select>` is focused leaves
   Vue's `category` model changed by typeahead while the DOM selection is
   restored — silent filter desync. (b) `input[type=number]` (admin
   price/stock, intake price) gets wiped by non-numeric restore values.
   (c) caret is never restored. The per-type **target classification table**
   (hold text inputs/textarea; value-only snapshot for number/date;
   selectedIndex snapshot + `change` dispatch for SELECT; never hold
   contentEditable; preventDefault-only for non-editables) is the right fix
   and replaces my vague "SELECT/contentEditable keep the old path".
2. **`e.repeat` / `e.isComposing` guards.** Auto-repeat today produces <50 ms
   gaps and *is classified as a scanner* — a live false positive my
   "identical-repeated-char" heuristic only half-covered. `e.repeat → flush +
   pass` is precise and one line. IME composition exclusion is mandatory for
   any preventDefault design.
3. **The WARM fast path (D1) beats my uniform hold.** My rev-1 design holds
   the first char of *every* run for up to 50 ms — and since slowish human
   typing has >50 ms gaps, that is effectively *every keystroke* delayed by up
   to 50 ms. I understated that cost. WARM (after a proved-human flush, the
   element passes keys natively for 400 ms; a burst starting inside the window
   retracts one snapshotted char) reduces the human cost to one char per run
   while bounding the scan artifact to 1 char / 1 frame / only in a
   self-inflicted race. Ship WARM, expose `SCAN_STRICT_DEFER` for the absolute
   guarantee. I reverse my rev-1 recommendation.
4. **D6 greeting banner.** Verified: `afterLogin` awaits cart import and
   staged-scan replay (both toast) before routing, so a greeting toast is
   clobbered; and `switchAccount` never calls `afterLogin`. A dedicated
   `role="status"` banner set in `saveSession`, **deliberately not cleared in
   `_closeOverlays`** (adjacent-line call in `switchAccount`), is correct.
   This is the single clearest defect in my rev 1.
5. **D3 review screen = the catalogue section with the aside swapped.** "Add"
   is then the literal grid click path + global scan — zero duplicated grid /
   sentinel / observer code, and no search-to-add box to build (my rev-1
   scope). The same-synchronous-turn `route` + `location.hash` rule (so
   `hashchange`'s `navigated` test stays false and doesn't kill the receipt)
   is a sharp, checkable convention.
6. **D5 receipt modal exception-only** (skipped orphans or clamped lines)
   preserves the clamp-not-veto reporting doctrine my "drop it" proposal
   violated.
7. **T0 harness-first + pure-move extraction** (`static/scan-capture.js` as a
   no-behaviour-change move, dual-mode export, Node `node --test` in a
   separate CI job, pytest keeps only static contract pins: script order,
   sink count == 2, `SCAN_DEFER_MS > SCAN_MAX_GAP_MS`). Better than my
   pytest-shells-to-node idea: `uv run pytest` stays Node-free and the
   load-bearing inequality is pinned in both worlds.
8. **No `isTrusted` check + M-10-dead contingency** (synthetic-event simulator
   is the only verification path; pin the absence of `isTrusted`). I missed
   `docs/m10-unreachable-2026-10-10.md` entirely.
9. **Full 30→60 copy sweep**: `auth.py:266`, `schemas.py:154`,
   `index.html:139`, `README.md:135`, `NOTES.md:21,279` — mine had only the
   constant, one test file and the hint. Plus a `test_default_grace_is_60`
   regression test and a contract pin tying the copy to the parsed default.
10. **Flush triggers I lacked**: `pointerdown`/`focusin`/`paste`/`cut`/`drop`
    flush-held; stale-snapshot guard (replay at the *current* caret if the
    value changed since the snapshot). Cheap, closes real clobber windows.

### Weaknesses / risks

- **Size/process weight.** ~30 Node cases + DOM harness + CI job + contract
  suite + smoke-test addition is a lot of scaffolding for a 5-item round;
  several "flows to demonstrate in the PR" (§4.3) are manual evidence that
  can't gate anything. Fine as ideals, but the coordinator should treat the
  harness (T0) + the §3.4 invariants as the gate and the rest as best-effort.
- **Month navigation ships by default** although the spec says "current
  month". It is ~10 lines on top of the endpoint, but it is unrequested UI;
  keep it behind the coordinator's Q7, not silent scope.
- **Stale-snapshot/beforeinput handling** is the kind of edge-case polish that
  can gold-plate the rewrite; the one-liners (paste/focus/pointer flush,
  current-caret fallback) are worth it, anything beyond is not.

### Disagreements

- **Timezone bucketing (qwen R6/Q5, option a: UTC days) vs my rev-1 (client
  local-day bucketing).** I initially favoured true local days; qwen's
  consistency argument wins: `formatDate` renders naive UTC as local wall
  clock, so UTC-day dots always agree with the list rows, and a mixed model
  (UTC list labels, local dots) is worse than a uniformly UTC one. Fixing
  `formatDate` is correctly out of scope. **qwen is right.**
- **Endpoint shape: `?year&month` ints + server-side day map** vs my
  `?from&to` raw rows. Theirs is better: validated ints (422 on garbage),
  no client re-bucketing, `count` per day enables tooltips, defaults to the
  current UTC month so the SPA calls it bare. I adopt it wholesale.
- No substantive disagreement remains on capture after adopting WARM.

## glm (r3plan-glm.md, rev 2)

### Strengths to adopt

1. **The BURST-state timer fix** — re-arm the defer timer on every held key,
   so a burst whose source dies mid-payload cannot hang the held state
   forever. qwen's diagram arms the timer only in HELD; glm caught it. Real,
   one line, adopted.
2. **`SCAN_DEFER_MS (60) > SCAN_MAX_GAP_MS (50)` justification stated as the
   load-bearing inequality** (scanner inter-key is 10-20 ms) and pinned in
   both test worlds — same as qwen, well put.
3. **`v-model.number` caveat called out as an explicit test**, not just a
   table row (admin price/stock listen to `change`, never held).
4. **T7 verification pass** including a ui_check pin that `/scan-capture.js`
   is actually served (200 + JS content-type) — cheap diagnosis of a silently
   dead scan bus.

### Weaknesses / risks

- **It is a rev-2 rollup, not an independent plan** — most of its content is
  qwen's design re-stated; the independent contributions are the BURST timer
  fix, the copy-sweep completeness, and the ui_check serving pin. Reviewers
  should diff it against qwen rather than read it standalone.
- Same process-weight concern as qwen (evidence bundle per PR, contract pins
  everywhere) — the pins are cheap, but the CI/workflow edits belong in one
  infra PR, not scattered.

### Disagreements

- None material. glm's WARM adoption, banner greeting, catalogue-reuse review
  screen, exception-only receipt modal, and year/month endpoint are the same
  positions I now hold. On **Q2** (naming the incoming user before a switch)
  both peers recommend no change (enumeration/privacy cost in `QrIdentifyOut`)
  — I agree; the post-switch toast names both users already.

---

## Final recommended task order

Frontend tasks are strictly serial (all touch `app.js`/`index.html`/
`style.css`); T1/T2 are backend and independent.

| # | Task | Layer | Key files | Tests |
|---|------|-------|-----------|-------|
| T0 | JS harness + pure-move extraction of shape gates/constants into `static/scan-capture.js` (no behaviour change) | infra | `static/scan-capture.js`, `index.html` (script before `/app.js`), `tests/js/`, `tests/test_frontend_contract.py`, CI `js` job | harness smoke + contract pins (script order, sink count == 2) |
| T1 | Grace 30→60 + full copy sweep | backend | `app/security.py`, `auth.py`/`schemas.py` docstrings, `index.html:139`, README/NOTES | `test_logoff.py` 30→60 + `test_default_grace_is_60` + copy↔default contract pin |
| T2 | `GET /api/orders/calendar?year&month` | backend | `app/routers/orders.py`, `app/schemas.py` | `tests/test_orders_calendar.py` (8 cases: aggregation, half-open bounds, empty month, isolation, clamp=paid, 401/403/422, key format) |
| T3 | Capture rewrite (defer/replay/retract + WARM) | frontend | `static/scan-capture.js`, `static/app.js` (thin adapter) | Node invariant matrix + `SCAN_DEFER_MS > SCAN_MAX_GAP_MS` pinned twice + ui_check + simulator transcript (no `isTrusted`) |
| T4 | Greeting banner + routing check | frontend | `app.js` (`saveSession`, `logout`, `handleSessionExpired`, `doRegister`, `switchAccount`), `index.html`, `style.css` | contract pin (`role="status"` bound) + per-entry-point manual evidence |
| T5 | `#/checkout` review screen (shared catalogue section, swapped aside) | frontend | `app.js` (`currentView`, watcher, `goToCheckout`/`donePurchase`/`setLineQty`), `index.html`, `style.css` | contract pins (no `confirm(` in path) + §4.3 flow evidence |
| T6 | Calendar UI | frontend | `static/purchase-calendar.js` (pure), `app.js`, `index.html`, `style.css` | Node suite (`monthMatrix` Date.UTC-built, `dayColour` degenerate min==max, `monthSummary` cross-check) |
| T7 | Docs + verification sweep | docs | README, NOTES, `ui_check.py` (`/scan-capture.js` served pin) | evidence bundle |

## The single best design per hard item

### Capture interception (T3)
Extract `createScanCapture(deps)` into `static/scan-capture.js` with injected
clock/timers/`onScan`; `app.js` keeps a thin adapter to `enqueueScan` (routing
bus untouched). States IDLE → HELD (first char after ≥50 ms gap:
preventDefault + 60 ms timer) → BURST (2nd fast char: keep holding, **re-arm
the timer on every key**); HELD/BURST Enter → shape-gated route (drop held,
never painted) or replay; timer fire → replay → **WARM(el, 400 ms)** (native
keys, snapshot on each ≥50 ms-gap key; burst confirm → retract to snapshot).
Per-type target classification (text/textarea hold; number/date value-only
snapshot; SELECT selectedIndex + `change`; non-editable preventDefault-only;
`data-scan-sink` early return first). Guards: `e.repeat`, `isComposing`/229,
modifiers, non-printable (flush first, then pass), `pointerdown`/`focusin`/
`paste`/`cut`/`drop` flush, stale-snapshot current-caret fallback. No
`isTrusted`. `SCAN_DEFER_MS=60 > SCAN_MAX_GAP_MS=50` pinned in Node and
pytest; `SCAN_STRICT_DEFER` constant for the absolute guarantee.
Replay/retract = caret-preserving `value` splice + bubbling `input` event.

### Calendar aggregate (T2/T6)
`GET /api/orders/calendar?year&month` (validated ints, default current UTC
month, `get_current_user` gate) → `{year, month, days: {"YYYY-MM-DD": {cents,
count}}, total_cents, purchase_count}`. SQLModel range select on indexed
`user_id` + half-open UTC month window, Python-side bucketing by
`created_at.date()`. **UTC days** — consistent with `formatDate`'s naive-UTC
wall-clock rendering, so dots never disagree with list rows (fixing
`formatDate` is a separate follow-up). Client: `monthMatrix` built with
`Date.UTC` (Monday-first), `dayColour = hsl(120*(1-t), 73%, 75-25t%)` with
`t = max>min ? (c-min)/(max-min) : 0` — t=0 is CSS lightgreen, so the
single-purchase month falls out free; min/max over day totals; month total
under the grid; grid/list two-column layout collapsing at the existing
900 px media query. Month nav optional (coordinator Q7).

### Review screen (T5)
Route `#/checkout` rendering the **same catalogue section** with the aside
swapped `.selection-panel` → `.review-panel`. No new cart state — the server
cart is the review state; all controls are existing `_cartOp`-serialized
mutations (`inc/dec/remove/clear`, grid add, scan add) plus one new
`setLineQty` (PATCH absolute qty, 0 removes, invalid/422 → toast +
`loadCart()` revert). "Complete purchase" → `goToCheckout` (no `confirm`);
`donePurchase` = old body minus confirm, on 201 → `#/account` +
`profileTab="purchases"` + pre-expanded receipt + toast; **receipt modal only
when `skipped.length || any clamped`**. Empty-cart entry bounces to catalogue;
every programmatic navigation assigns `this.route` and `location.hash` in the
same synchronous turn.

---

## Changes made to my own plan (rev 1 → rev 2)

1. Capture: uniform hold → **WARM defer/replay/retract** with per-type target
   classification, `e.repeat`/`isComposing`/pointer/paste/focus guards,
   stale-snapshot fallback, BURST timer re-arm, `SCAN_DEFER_MS=60`,
   `SCAN_STRICT_DEFER`, no-`isTrusted` pin. (Reverses my rev-1
   recommendation; keeps my "extract a pure, node-tested module" but as qwen's
   injected-deps factory + T0 pure move.)
2. Greeting: toast → **banner set in `saveSession`**, not cleared in
   `_closeOverlays`. (My rev-1 toast was provably clobbered.)
3. Review: standalone section + search-to-add → **shared catalogue section,
   swapped aside**; receipt modal exception-only instead of dropped; added
   `setLineQty`, empty-cart bounce, same-turn routing rule.
4. Calendar: `?from&to` raw rows + client local-day bucketing →
   **`?year&month` server-bucketed UTC days**; adopted the Date.UTC grid
   construction and the exact HSL ramp; month nav demoted to optional.
5. Grace: added the full copy sweep + `test_default_grace_is_60` + contract
   pin.
6. Process: added T0 (harness + pure move), separate CI `js` job instead of
   pytest→node subprocess, T7 verification pass.
