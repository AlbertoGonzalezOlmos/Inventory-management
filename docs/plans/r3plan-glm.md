# Round 3 plan — glm-5.3 (rev 2, post cross-review)

> **Revision note.** Rev 1 was cross-reviewed against
> `docs/plans/r3plan-kimi.md` and `docs/plans/r3plan-qwen.md`; the record
> (per-topic verdicts, adopted/rejected designs, and the honest ledger of
> rev-1 defects) is in `docs/plans/r3rev-glm.md`. This file is the revised
> plan and supersedes rev 1. Summary of what changed: the greeting is a
> banner (rev 1's toast was clobbered by `afterLogin`'s cart/staging toasts);
> the capture design gains qwen's WARM fast-path and per-type target
> classification, plus a BURST-state timer fix; the 30→60 copy sweep is
> complete (`index.html:139`, docstrings, NOTES); the review screen reuses
> the catalogue section; the receipt modal becomes exception-only; the
> calendar endpoint takes validated year/month ints.

Grounding (unchanged from rev 1, spot-verified again during review):
`static/app.js` (capture ~929-1010, `enqueueScan` bus, `armLogoff`,
`switchAccount`, `completePurchase` ~894-926, `afterLogin` 388-405),
`static/index.html` (sinks ~81-90/~129-137, sidebar ~202-234, purchases tab
~329-365, logoff overlay, "30 s countdown" copy at :139),
`app/security.py:166-168` (`LOGOFF_GRACE_SECONDS` env default 30, module
attribute read at call time), `app/routers/auth.py` (logoff-schedule
256-287, qr-identify 313+), `app/routers/orders.py` (`POST /api/orders`
BEGIN IMMEDIATE checkout, `GET /mine` limit ≤ 100),
`app/routers/cart.py:277-297` (PATCH absolute qty, 0 removes),
`scripts/ui_check.py` (no scripted login; DOM pins only), and
`docs/m10-unreachable-2026-10-10.md` (the M-10 is dead — the simulator is
the only capture-verification path, which is why the handler must NOT check
`isTrusted`).

---

## Ordered task breakdown

Serialisation rule: every frontend task touches `static/app.js` /
`static/index.html` / `static/style.css` → strictly serial. T0/T1/T2 are
independent of each other and of the frontend chain.

| # | Task | Layer | Files | Tests |
|---|------|-------|-------|-------|
| T0 | JS test harness + extraction convention (pure move, no behaviour change) | infra | `static/scan-capture.js` (shape gates + constants moved from app.js), `static/index.html` (script tag BEFORE app.js), `tests/js/harness/dom.js`, `tests/js/smoke.test.js`, `tests/test_frontend_contract.py`, `.github/workflows/ci.yml` (new `js` job: `setup-node` + `node --test tests/js/`) | harness self-tests + wiring pins (script order, `data-scan-sink` count == 2, `data-scan-capture` binding, Node tests require the SAME module the SPA loads) |
| T1 | Logoff grace 30 s → 60 s (item 2) | backend | `app/security.py:166-168`, `app/routers/auth.py:266,271` (docstrings), `app/schemas.py:154`, `static/index.html:139`, `README.md`, `NOTES.md:21,279` | `tests/test_logoff.py` default assertions 30→60 + new default-is-60 regression test; contract pin tying the index.html copy to the parsed default |
| T2 | Calendar aggregate endpoint (item 5 data) | backend | `app/routers/orders.py`, `app/schemas.py` | new `tests/test_orders_calendar.py` (8 cases, below); `scripts/smoke_test.py` check |
| T3 | Scan interception rewrite (item 1) | frontend | `static/scan-capture.js` (state machine body), `static/app.js` (thin adapter), `tests/js/scan-capture.test.js` | ~30-case Node matrix + contract pins + `ui_check --self-host` + simulator transcript |
| T4 | Greeting banner + catalogue routing (item 3) | frontend | `static/app.js` (`saveSession`, `afterLogin`, `doRegister`, `switchAccount`, `logout`, `handleSessionExpired`), `static/index.html`, `static/style.css` | contract pins; manual evidence per entry point |
| T5 | Checkout review screen (item 4) | frontend | `static/app.js` (`currentView`, watcher, `goToCheckout`/`donePurchase`/`setLineQty`), `static/index.html` (shared catalogue section + review panel), `static/style.css` | contract pins (no `confirm(` in the path); backend tests untouched |
| T6 | Calendar UI (item 5) | frontend | `static/purchase-calendar.js` (pure), `static/app.js`, `static/index.html`, `static/style.css`, `tests/js/purchase-calendar.test.js` | Node suite + endpoint tests from T2 |
| T7 | Docs + verification pass | docs | `README.md`, `NOTES.md`, `scripts/ui_check.py` (pin that `/scan-capture.js` is served, 200 + JS content-type), `docs/plans/round3-task.md` status | evidence bundle per PR |

### T1 — grace 60 (details)

- `app/security.py`: env default `"30"` → `"60"` (keep the `max(1, …)` clamp,
  the env override, and the module-attribute-at-call-time test hook).
- Copy sweep (all verified stale): `app/routers/auth.py:266` "log off in 30
  seconds?", `:271` "now+30", `app/schemas.py:154` "now+30",
  `static/index.html:139` "(30 s countdown)", `README.md:135`, `NOTES.md:21,279`.
- `tests/test_logoff.py:86-89`: `grace_seconds == 60`, `now+60` delta; the
  monkeypatched-grace tests are untouched by design. Add
  `test_default_grace_is_60` so a future "30" cannot slip back in.
- No SPA logic change: `armLogoff` counts down from the server deadline.
- Item 2's different-user half needs no code (`switchAccount` =
  flush → qr-login → cancel countdown → save session → revoke old token).

### T3 — capture interception (canonical design)

States: `IDLE`, `HELD` (first char held, 60 ms defer timer), `BURST`
(2+ sub-50 ms chars, all held), `WARM(el, 400 ms)` (proved-human fast path).

```
IDLE  ──printable, gap≥50ms, holdable target──▶ HELD   [preventDefault, timer]
HELD  ──printable, gap<50ms──────────────────▶ BURST  [preventDefault, accumulate]
HELD  ──timer fires───────────────────────────▶ replay → WARM
HELD/BURST ──Enter, shape ok──────────────────▶ route (drop held; never painted)
HELD/BURST ──Enter, shape bad─────────────────▶ replay → IDLE (Enter default runs)
HELD/BURST ──printable, gap≥50ms──────────────▶ replay, then HELD(new char)
HELD/BURST ──non-printable / modifier / repeat / composing ─▶ replay → pass through
BURST ──timer fires (no key for 60 ms)────────▶ replay → IDLE   [rev-2 FIX:
          qwen's diagram armed the timer only in HELD; re-arming it on every
          held key means a burst whose source dies cannot hang. Safe because
          SCAN_DEFER_MS (60) > SCAN_MAX_GAP_MS (50) > scanner inter-key (10-20 ms).]
WARM  ──printable─────────────────────────────▶ native (no preventDefault;
          snapshot value+caret on gap≥50ms keys — a burst may start here)
WARM  ──burst confirmed (2nd fast key)─────────▶ retract to snapshot → BURST
```

- `_buf` is maintained in every state so the Enter shape gate sees exactly
  today's string; only *where characters physically land* differs per state.
- **Target classification** (dictates hold vs snapshot vs pass-through):
  text-ish INPUT/TEXTAREA → holdable (caret splice well-defined);
  `input[type=number|date|…]` → never held, value-only snapshot/restore
  (`selectionStart` throws there — the admin price/stock and intake price
  fields); `SELECT` → never held, `selectedIndex` snapshot + `change` dispatch
  (a held scan must not wipe the category filter — a real latent defect in
  today's restore path); `contentEditable` → never held (none exist in the
  SPA, verified); non-editable → native for humans, preventDefault in burst;
  `[data-scan-sink]` ancestors → early return before ANY of this (the
  collapsed badge fallback and the visible scan field keep their dedicated
  v-model/Enter flows — the ui_check pins stay green).
- **Replay/retract** are deterministic splices (no `execCommand` — deprecated
  and not stub-able in the Node harness): `el.value = before + chars + after`,
  `setSelectionRange` (try/catch), dispatch `InputEvent("input", {bubbles})`
  → Vue v-model updates. Retact = restore snapshot value + caret + input.
- **Guards**: `e.isComposing || keyCode 229` → flush + pass; `e.repeat` →
  flush + clear WARM + pass (auto-repeat is human by definition; today it is
  misclassified as a scanner); ctrl/meta/alt → reset, never eaten;
  non-printable (`e.key.length !== 1`) → flush FIRST so Backspace acts on
  real text, then pass; `pointerdown`/`focusin` (document, capture) → flush
  into the snapshotted element. **No `isTrusted` check** — the M-10 is dead
  and console-dispatched `KeyboardEvent`s are the only simulator path; a
  contract test pins its absence.
- **`SCAN_DEFER_MS = 60 > SCAN_MAX_GAP_MS = 50`** pinned in BOTH the Node
  suite and pytest — the load-bearing inequality (a smaller defer window
  lets a scan's second key arrive after the flush and paint the payload).
- **`SCAN_STRICT_DEFER`** constant (default false): flips to the absolute
  "never a pixel" guarantee at the cost of a 60 ms hold on every character.
  WARM's bounded exception: one char, one frame, only when a scan begins
  <400 ms after a human keystroke in the same focused field.
- The routing half is untouched: `onScan(code, kind)` → `enqueueScan` →
  queue → `handleBadgeScan`/`handleBarcodeScan`, pill masking included.
- Tests: the Node matrix (scanner GTIN/badge/raw-43 at 12/20/45 ms; human
  at 55/80/100 ms — zero held chars at end, exact char sequence, preventDefault
  call counts; sink never prevented; unshaped bursts inert; repeat; composing;
  focus change; pointerdown; timer-fire flush; caret/selection preservation;
  `SCAN_DEFER_MS > SCAN_MAX_GAP_MS`). Plus `ui_check --self-host` and a
  browser simulator transcript (hardware dead; PR must say so explicitly).

### T4 — greeting (canonical design)

- Banner, not toast: `greeting: ""` + 4 s auto-clear timer, set in
  **`saveSession`** (the single funnel for password/badge/register/switch):
  `this.greeting = "Hi " + user.name`. A toast would be clobbered within
  milliseconds by `importLegacyPurchase`/`replayStagedScans` toasts on the
  returning-kiosk-user path — the decisive rev-1 defect.
- NOT cleared in `_closeOverlays()` (switchAccount calls `saveSession` then
  `_closeOverlays` on adjacent lines — clearing there would kill the greeting
  on the one path that most needs it); cleared in `logout`,
  `handleSessionExpired`, and by overwrite on the next `saveSession`.
- Markup: `<div class="greeting" v-if="greeting" role="status">` above the
  view sections (screen-reader announced, 4 s).
- Remove `doRegister`'s duplicate "Welcome, <name>!" toast; keep the
  badge-modal subtitle (print context); `switchAccount`'s toast keeps only
  the "<oldName>'s list was saved" half.
- Routing: `afterLogin` already routes to `#/catalogue` (verified on `main`);
  the `must_change_password` → `#/account` exception stays (greeting still
  shows). No routing change.
- All programmatic navigations pre-set `this.route` before `location.hash`
  (the `hashchange` `navigated` guard) — pattern holds for T5's transitions.

### T5 — review screen (canonical design)

- `#/checkout` renders the SAME catalogue section
  (`v-else-if="currentView === 'catalogue' || currentView === 'checkout'"`)
  with the `<aside>` swapped from `.selection-panel` to `.review-panel`:
  one grid, one `#sentinel`, the IntersectionObserver wiring untouched, and
  "add" is the literal click path on the grid (plus global scan-to-add —
  a route is not a modal, `handleBarcodeScan`'s gate does not block).
- **No new cart state.** The server cart IS the review state; every control
  is an existing `_cartOp`-serialized mutation: `+`/`−` (`incPurchase`/
  `decPurchase`), qty box → `setLineQty(id, raw)` (PATCH absolute; 0 removes;
  invalid/422 → toast + `loadCart()` revert to server truth), `✕`
  (`removeFromPurchase`), Clear list, grid add, scan add.
- `goToCheckout` (the sidebar button, replacing `completePurchase`'s
  confirm path) pre-sets `this.route` then `location.hash = "#/checkout"`;
  the `currentView` watcher loads items like catalogue and bounces an empty
  cart back with a toast.
- `donePurchase` = today's `completePurchase` body minus `confirm()`:
  `busy`-guarded, inside `_cartOp` (a scan racing the checkout cannot
  interleave), raw `POST /api/orders` with 400/401/503 branches. On 201:
  `cart = data.cart`, `lastCheckout = id`, `profileTab = "purchases"`,
  pre-set `this.route = "#/account"` then `location.hash` (so the hashchange
  guard does not fire `_closeOverlays` and kill the receipt), `loadMyOrders`,
  `expandedOrders = [id]`, **always** a toast (`Purchase complete — <total>`,
  or "N item(s) no longer exist and were skipped"), and the **receipt modal
  only when `skipped.length || any line clamped`** (exception-only — the
  clamp-not-veto reporting doctrine survives without a blocking dialog).
- Empty-cart deep link → toast + bounce to catalogue; logged-out deep link →
  login (kiosk-correct); topbar "Catalogue" active state gains
  `|| currentView === 'checkout'`.

### T2/T6 — calendar (canonical design)

- **Endpoint**: `GET /api/orders/calendar?year&month` (ints, `ge/le`
  validated, default = current UTC month; `get_current_user` gate; the
  `must_change_password` 403 comes free). SQLModel range query on the
  indexed `user_id` + half-open UTC `[month start, next month start)`;
  Python-side bucketing by `created_at.date().isoformat()`. Response:
  `{year, month, days: {"YYYY-MM-DD": {cents, count}}, total_cents,
  purchase_count}` — `cents` groups the receipts' `total_cents` (already
  paid amounts, Σ price × applied). Tests: same-day aggregation, boundary
  instants in/out, empty month (200 with `days == {}`), user isolation,
  staff parity with `/mine`, clamp case shows paid amount, 401/403/422,
  `days`-key format regex.
- **Client** (`static/purchase-calendar.js`, pure, Node-tested):
  `monthMatrix` built with `Date.UTC` (local-midnight construction shifts
  keys west of UTC), Monday-first; `dayColour(cents, min, max)` →
  `t = max > min ? (cents-min)/(max-min) : 0`,
  `hsl(120*(1-t), 73%, 75% - 25%*t)` — t=0 is CSS lightgreen (a
  single-purchase month falls out for free), t=1 red, yellow/orange ramp
  between; `monthSummary` cross-checks its total against the endpoint's
  (drift detector).
- **UI**: `.history-layout` grid (list left, `.calendar-card` right,
  collapsing at the existing 900 px media query); day cell keeps its number,
  dot only on purchase days with a `title` tooltip (count + amount);
  `isToday` ring; month total + purchase count below the grid; refreshed on
  tab entry and after `donePurchase`; prev/next month nav (cheap on top of
  the endpoint — trim if the coordinator reads the spec strictly).
- **Timezone**: UTC days, documented with a one-line comment at both the
  endpoint and the client — the only bucketing where a dot never disagrees
  with its list row (`formatDate` parses naive-UTC as local wall-clock).
  Fixing `formatDate` to append `Z` is a separate follow-up.

---

## Risks / open questions (updated)

1. **T3 is the round's risk** — a rewrite of the most subtle frontend code
   with the hardware dead. Mitigations: T0 pure-move first, extracted module,
   ~30-case Node matrix with injected clock, routing bus untouched,
   simulator transcript in the PR, `SCAN_STRICT_DEFER` escape hatch.
2. **WARM's bounded exception** (one char, one frame, scan-within-400 ms of
   human typing) vs the task's literal "never appears" — coordinator knob.
3. **Receipt modal policy** — exception-only is the default; coordinator may
   prefer always or never.
4. **Review screen shape** — catalogue+panel (default) vs a distinct
   grid-free screen; the fallback needs a search-to-add box (more code).
5. **Month navigation** — ships by default (endpoint makes it free); trim if
   the spec is read strictly.
6. **Node in CI** — new `js` job (keeps `uv run pytest` Node-free); missing
   Node must fail, not skip (repo doctrine). Fallback: pytest subprocess.
7. **Env override** — deployments pinning `HCRM_LOGOFF_GRACE_SECONDS` keep
   their value; intended, called out in the PR.
8. **Q2 (pre-switch "Switch to <name>?" prompt)** — no change recommended
   (enumeration/privacy cost in `QrIdentifyOut`; the post-switch toast
   already names both users).
9. **`v-model.number` fields** (admin price/stock) listen to `change` —
   number inputs are never held; pinned as an explicit test, not just a
   table row.
