# Round 3 plan — kimi (rev 2, post cross-review)

> **Revision note.** Rev 1 was cross-reviewed against `r3plan-glm.md` and
> `r3plan-qwen.md`; the record (per-peer verdicts, what was adopted and why)
> is in `docs/plans/r3rev-kimi.md`. This file supersedes rev 1. What changed:
> the greeting is a **banner** (rev 1's toast is clobbered by
> `importLegacyPurchase`/`replayStagedScans` toasts in `afterLogin`, and
> `switchAccount` never calls `afterLogin`); the capture design adopts the
> **WARM defer/replay/retract** machine with per-type target classification
> (rev 1's uniform hold delayed *every* slow-typed keystroke, and the old
> restore path has real SELECT/number/caret defects); the calendar endpoint is
> **`?year&month` with server-side UTC-day bucketing** (dots then always agree
> with `formatDate`'s naive-UTC list rows); the review screen **reuses the
> catalogue section** with a swapped aside (no search-to-add box); the receipt
> modal becomes **exception-only**; the 30→60 copy sweep is complete; a T0
> harness-first pure move precedes the risky rewrite.

Grounding: `static/app.js` (capture ~929-1010, `enqueueScan` ~1027,
`badgeLogin` 414, `afterLogin` 388-405, `completePurchase` 894-926,
`switchAccount` 1188-1225 — `saveSession` then `_closeOverlays` on adjacent
lines), `static/index.html` (sinks ~81-90/~129-137, sidebar ~202-234,
purchases tab ~329-365, "30 s countdown" at :139, receipt modal ~842-873),
`app/security.py:166-168` (`LOGOFF_GRACE_SECONDS`), `app/routers/auth.py`
(logoff-schedule 256-287, qr-identify 313+), `app/routers/orders.py`
(BEGIN IMMEDIATE checkout, `GET /mine` limit ≤ 100),
`app/routers/cart.py:277-297` (PATCH absolute qty, 0 removes),
`scripts/ui_check.py` (no scripted login; DOM pins only),
`docs/m10-unreachable-2026-10-10.md` (M-10 dead — synthetic-event simulation
is the only capture-verification path, so the handler must **never** check
`isTrusted`).

---

## Ordered task breakdown

Serialisation rule: T0/T3-T7 all touch `static/app.js` / `index.html` /
`style.css` → strictly serial. T1/T2 are backend-only, independent of each
other and of the frontend chain.

| # | Task | Layer | Files | Tests |
|---|------|-------|-------|-------|
| T0 | JS harness + pure-move extraction (no behaviour change) | infra | `static/scan-capture.js` (shape gates + constants moved verbatim), `index.html` (script tag **before** `/app.js`), `tests/js/harness/dom.js`, `tests/js/smoke.test.js`, `tests/test_frontend_contract.py`, CI `js` job (`node --test tests/js/`) | harness smoke + contract pins: script order, `data-scan-sink` count == 2, `data-scan-capture` binding, Node tests `require` the same file the SPA loads |
| T1 | Logoff grace 30 → 60 (item 2) | backend | `app/security.py:166-168`, `app/routers/auth.py:266` docstring, `app/schemas.py:154`, `index.html:139`, `README.md:135`, `NOTES.md:21,279` | `tests/test_logoff.py:86-89` 30→60 + new `test_default_grace_is_60` + contract pin tying the index.html copy to the parsed env default |
| T2 | `GET /api/orders/calendar?year&month` (item 5 data) | backend | `app/routers/orders.py`, `app/schemas.py` | new `tests/test_orders_calendar.py` (8 cases, below); one `scripts/smoke_test.py` check |
| T3 | Capture interception rewrite (item 1) | frontend | `static/scan-capture.js` (state machine), `static/app.js` (thin adapter; routing bus untouched) | Node invariant matrix (~30 cases) + `SCAN_DEFER_MS > SCAN_MAX_GAP_MS` pinned in Node AND pytest + `ui_check --self-host` + simulator transcript (PR must state hardware status) |
| T4 | Greeting banner + routing (item 3) | frontend | `app.js` (`saveSession`, `logout`, `handleSessionExpired`, `doRegister`, `switchAccount`), `index.html`, `style.css` | contract pin (`role="status"` element bound to `greeting`); manual evidence per entry point |
| T5 | `#/checkout` review screen (item 4) | frontend | `app.js` (`currentView`, watcher, `goToCheckout`/`donePurchase`/`setLineQty`), `index.html` (shared catalogue section + review aside), `style.css` | contract pins (no `confirm(` in the checkout path); flow evidence (below) |
| T6 | Calendar UI (item 5) | frontend | `static/purchase-calendar.js` (pure), `app.js` (`loadCalendar` etc.), `index.html` (purchases tab layout), `style.css`, `tests/js/purchase-calendar.test.js` | Node suite + T2 endpoint tests |
| T7 | Docs + verification sweep | docs | README, NOTES, `scripts/ui_check.py` (pin `/scan-capture.js` served: 200 + JS content-type), `round3-task.md` status | evidence bundle per PR |

T0 convention: dual-mode module — `module.exports` when
`typeof module !== "undefined"`, else `window.HcrmScanCapture` /
`window.HcrmCalendar`; classic `<script src>` (no ESM migration). The `pytest`
job stays Node-free; the new CI `js` job has no skip path (missing tool =
failure, the repo's ui_check doctrine).

---

## T1 — grace 60 (item 2)

Behaviour (same-user → `armLogoff` overlay counting from the SERVER deadline;
different-user → `qr-identify` → `switchAccount` save-then-switch) already
exists and is correct; `qr-login` minting a session regardless of existing
ones is by design (Q2 in the review: no pre-switch "Switch to \<name\>?"
prompt — enumeration/privacy cost in `QrIdentifyOut`; the post-switch toast
already names both users). Changes:

- `app/security.py`: env default `"30"` → `"60"` (keep `max(1,…)`, env
  override, module-attribute-at-call-time test hook).
- Copy sweep (all verified stale): `auth.py:266` "log off in 30 seconds?",
  `schemas.py:154` "now+30", `index.html:139` "(30 s countdown)",
  `README.md:135`, `NOTES.md:21,279`.
- `tests/test_logoff.py:86-89`: default 30→60; monkeypatched-grace tests
  untouched. Add `test_default_grace_is_60` (unmonkeypatched default + armed
  deadline ≈ now+60) and a contract pin tying the `index.html` copy to the
  parsed `app/security.py` default — one source of truth.
- No SPA change: `armLogoff` counts down from the server deadline. Deployments
  pinning `HCRM_LOGOFF_GRACE_SECONDS` keep their value (intended; note in PR).

## T4 — greeting (item 3)

- **Banner, not toast**: `greeting: ""` + 4 s auto-clear, set in
  **`saveSession`** (the single funnel for password/badge/register login *and*
  the badge switch): `this.greeting = "Hi " + user.name`. A toast is
  clobbered within milliseconds by `afterLogin`'s `loadCart →
  importLegacyPurchase` and `replayStagedScans` toasts on the returning-kiosk
  path — the decisive defect of my rev 1.
- **NOT cleared in `_closeOverlays()`** — `switchAccount` calls `saveSession`
  then `_closeOverlays` on adjacent lines; clearing there would kill the
  greeting on the path that most needs it. Cleared in `logout`,
  `handleSessionExpired`, and by overwrite on the next `saveSession`. Comment
  in code saying why it is absent from `_closeOverlays`.
- Markup: `<div class="greeting" v-if="greeting" role="status">` above the
  view sections (screen-reader announced).
- Remove `doRegister`'s duplicate "Welcome, \<name\>!" toast; keep the badge
  modal's subtitle (print context). `switchAccount`'s toast keeps only the
  "\<oldName\>'s list was saved" half (the banner carries the "Hi X").
- Routing: `afterLogin` already routes to `#/catalogue` (hash + `this.route`
  assigned in the same synchronous turn, so the `hashchange` `navigated`
  guard doesn't fire `_closeOverlays`); `must_change_password` → `#/account`
  stays (greeting still shows). No routing change.

## T3 — capture interception (item 1, canonical design)

`createScanCapture(deps)` in `static/scan-capture.js` — injected
`now/setTimeout/clearTimeout/document/onScan/sinkSelector`, no Vue. `app.js`
keeps a thin adapter: `onScan → enqueueScan` (the pill masking, queue and
`handleBadgeScan`/`handleBarcodeScan` are **untouched**; the sink early-return
stays the first statement). States:

```
IDLE  ──printable, gap≥50ms, holdable target──▶ HELD   [preventDefault, 60ms timer]
HELD  ──printable, gap<50ms──────────────────▶ BURST  [preventDefault, accumulate,
                                                        RE-ARM timer on every key —
                                                        a burst whose source dies
                                                        mid-payload cannot hang]
HELD  ──timer fires───────────────────────────▶ replay → WARM(el, 400ms)
HELD/BURST ──Enter, shape ok──────────────────▶ route (drop held; never painted)
HELD/BURST ──Enter, shape bad─────────────────▶ replay → IDLE (Enter default runs)
HELD/BURST ──printable, gap≥50ms──────────────▶ replay, then HELD(new char)
BURST ──timer fires───────────────────────────▶ replay → IDLE
WARM  ──printable─────────────────────────────▶ native (no preventDefault;
          snapshot value+caret on gap≥50ms keys — a burst may start here)
WARM  ──burst confirmed (2nd fast key)─────────▶ retract to snapshot → BURST
```

- `_buf` is maintained in every state so the Enter shape gate sees exactly
  today's string; only *where characters physically land* differs per state.
- **`SCAN_DEFER_MS = 60 > SCAN_MAX_GAP_MS = 50` > scanner inter-key (10-20 ms)**
  — the load-bearing inequality (a smaller defer lets the scan's second key
  arrive after the flush and paint). Pinned in Node AND pytest.
- **`SCAN_STRICT_DEFER`** (default false): flips to the absolute "never a
  pixel" guarantee at the cost of a 60 ms hold on every character. WARM's
  bounded exception: one char, one frame, only when a scan begins <400 ms
  after a human keystroke in the same focused field. Coordinator knob.
- **Target classification** (replaces the old blanket snapshot — fixes three
  latent defects: SELECT filter desync via `input`-vs-`change`, number-input
  wipe, caret loss):
  text-ish INPUT + TEXTAREA → hold (caret splice well-defined);
  `input[type=number|date|…]` → never held, value-only snapshot/restore
  (`selectionStart` throws; these fields use `v-model.number` → `change`);
  SELECT → never held, `selectedIndex` snapshot + `change` dispatch;
  contentEditable → never held (none exist in the SPA);
  non-editable → native for humans, preventDefault in burst (a burst's Enter
  must not activate a focused button);
  `[data-scan-sink]` ancestors → early return before ALL of this (collapsed
  badge fallback + visible scan field keep their dedicated v-model/Enter
  flows; ui_check pins stay green).
- **Replay/retract**: caret-preserving splice (`el.value = before + chars +
  after`, `setSelectionRange` in try/catch) + bubbling `input` event — the
  same dispatch the current restore already relies on. Stale-snapshot guard:
  if `el.value` changed since the snapshot, insert at the CURRENT caret.
- **Guards**: `e.repeat` → flush + clear WARM + pass (auto-repeat is human by
  definition; today it is misclassified as a scanner); `e.isComposing` /
  keyCode 229 → flush + pass; ctrl/meta/alt → reset; non-printable
  (`e.key.length !== 1`) → flush FIRST (Backspace acts on real text) then
  pass; `pointerdown`/`focusin`/`paste`/`cut`/`drop` → flush.
  **No `isTrusted` check** — pinned by a contract test.
- **Collapsed badge-code fallback**: unchanged by construction (sink early
  return; `busy` double-fire guard in `badgeLogin` untouched).
- Node matrix (~30 cases): scanner GTIN/badge/raw-43 at 12/20/45 ms; human at
  55/80/100 ms (zero held chars at end, exact sequence, one preventDefault per
  run); sink never prevented; unshaped bursts inert; repeat; composing;
  pointerdown/focus/paste flush; timer fire; caret preservation; SELECT
  selectedIndex; number value-only; the inequality pin. Real-payload fixtures
  from the repo (`HCRM1:`+43, bare 43, EAN-13, CR suffix).
- Follow-up ticket (round 3.5, out of scope): M-10 sentinel-prefix config via
  the Universal Menu Book → deterministic capture, zero heuristics.

## T5 — review screen (item 4, canonical design)

**No new cart state — the server cart IS the review state.** Every control is
an existing `_cartOp`-serialized mutation returning the full `CartOut`:

| control | call |
|---|---|
| `+`/`−` | `incPurchase` / `decPurchase` |
| qty box (`type=number`, `:value` + `@change`) | new `setLineQty(id, raw)`: PATCH absolute (`cart.py` — 0 removes); invalid/422 → toast + `loadCart()` revert to server truth. Deliberately a NON-held capture target |
| `✕` / Clear list | `removeFromPurchase` / `clearPurchase` |
| add | the catalogue grid's `addToPurchase` + global scan (`handleBarcodeScan` only blocks on modals — a route is not a modal) |
| Done | new `donePurchase()` |

- `currentView === "checkout"` renders the **same `<section>` as catalogue**
  (`v-else-if` gains `|| currentView === 'checkout'`); only the `<aside>`
  swaps `.selection-panel` → `.review-panel`. One grid, one `#sentinel`, the
  IntersectionObserver wiring untouched. Topbar "Catalogue" active state
  gains `|| currentView === 'checkout'`.
- The sidebar button becomes `goToCheckout` (sets `this.route` and
  `location.hash = "#/checkout"` **in the same synchronous turn**) — no
  `confirm()`. The remaining `confirm(` calls are the destructive ones; the
  review screen *is* the confirmation.
- Watcher treats `checkout` like `catalogue` for data loading; entry with an
  empty cart → toast + bounce to `#/catalogue`. Logged-out deep link → login
  (then catalogue — accepted; no redirect-target field exists).
- `donePurchase` = today's `completePurchase` body minus `confirm()`:
  `busy`-guarded, inside `_cartOp` (a racing scan cannot interleave), raw
  POST `/api/orders` with 400/401/503 branches. On 201: `cart = data.cart`,
  `lastCheckout = id`, `profileTab = "purchases"`, route to `#/account`
  (same-turn rule), `loadMyOrders`, `expandedOrders = [id]`, always a toast
  (`Purchase complete — <total>`), and the **receipt modal only when
  `data.skipped.length || data.purchase.lines.some(l => l.clamped)`** —
  exception-only, so the clamp-not-veto report is never silently dropped
  without a blocking dialog on the happy path. (Coordinator may prefer
  always/never — flagged.)
- Badge scans on the review screen behave as everywhere (logoff overlay over
  the screen; `logout` forces `#/login`, nothing stranded).
- PR flow evidence: edit qty/remove/add (grid + scan) on the review screen;
  done → pre-expanded history row + empty cart everywhere; clamp/skip case →
  exception modal; own-badge scan → 60 s overlay.

## T2 + T6 — calendar (item 5, canonical design)

- **Endpoint**: `GET /api/orders/calendar?year&month` — ints, `Query(ge/le)`
  validated (year 2000-2100, month 1-12 → 422), default = current UTC month,
  `get_current_user` gate (403 for `must_change_password` comes free).
  SQLModel range select on indexed `user_id` + half-open UTC window
  `[datetime(y,m,1), next month)`; Python-side bucketing by
  `created_at.date().isoformat()` (a month is tens of rows — clearer than
  `GROUP BY substr`). Response `{year, month, days: {"YYYY-MM-DD": {cents,
  count}}, total_cents, purchase_count}`; `cents` = Σ receipt `total_cents`
  (PAID amounts, price × applied_qty — the clamp case must show paid).
- **Timezone: UTC days**, one-line comment at both ends. Rationale:
  `formatDate` renders naive-UTC as local wall-clock, so UTC-day dots always
  agree with the list rows; fixing `formatDate` (append `Z`) is a separate
  follow-up. (My rev-1 local-day bucketing would make dots and rows
  disagree — reversed.)
- Tests (`tests/test_orders_calendar.py`): same-day aggregation (count==2,
  summed); boundary instants in/out (half-open range); empty month → 200 with
  `days == {}` (never 404); user isolation; staff parity with `/mine`; clamp
  case shows paid amount; 401/403/422; `days` key format regex.
- **Client** (`static/purchase-calendar.js`, pure, dual-mode, Node-tested):
  `monthMatrix(year, month)` built with **`Date.UTC`** (local-midnight
  construction shifts keys west of UTC), Monday-first, blanks padded;
  `dayColour(cents, min, max)`: `t = max > min ? (cents-min)/(max-min) : 0`
  → `hsl(120*(1-t), 73%, 75-25t%)` — t=0 is CSS lightgreen (the spec's
  "first purchase is light green" falls out free when min==max), t=1 red,
  hue path through yellow/orange (a 2-stop RGB lerp goes muddy brown);
  min/max over DAY totals (one dot per day is the spec, so one value per day
  is the scale); `monthSummary(days)` cross-checked against the endpoint's
  `total_cents` (drift detector).
- **UI**: `.history-layout` grid (list left, `.calendar-card` right,
  collapsing in the existing 900 px media query); day number kept, dot only
  on purchase days (`:style` background + `title` tooltip with count +
  amount); today ring; month total + purchase count below the grid; empty
  month renders dotless with `€0.00`, never a blank hole. Fetched on
  `selectProfileTab("purchases")` and after `donePurchase`. Prev/next month
  nav is ~10 lines once the endpoint exists — **optional, coordinator call**
  (the spec says "current month"; default in both peers' plans is to ship it).

---

## Risks / open questions (rev 2)

1. **T3 is the round's risk** — a rewrite of the most subtle frontend code
   with the hardware dead. Mitigations: T0 pure move first, injected-deps
   module, Node invariant matrix, routing bus untouched, `SCAN_STRICT_DEFER`
   escape hatch, simulator transcript in the PR (state hardware status
   explicitly; never check `isTrusted`).
2. **WARM's bounded exception** (1 char / 1 frame / scan-within-400 ms of
   human typing) vs the task's literal "never appears" — coordinator knob
   (`SCAN_STRICT_DEFER`). Both peers and I now recommend shipping WARM.
3. **Receipt modal policy** — exception-only (default), always, or never.
4. **Month navigation** — ships cheaply; trim if the spec is read strictly.
5. **Node in CI** — separate `js` job; missing Node must fail, not skip.
6. **Env override** — deployments pinning `HCRM_LOGOFF_GRACE_SECONDS` are
   unaffected; intended, called out in the PR.
7. **Native undo after a replay** — splices are invisible to the browser's
   undo stack (Ctrl+Z may jump oddly). Accepted; documented in the module
   docstring.
8. **Logged-out `#/checkout` deep link** → login then catalogue (no
   return-to-checkout intent field exists). Accepted.
