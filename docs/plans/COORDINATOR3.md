# Coordinator synthesis #3 — scanner interception, login flow, checkout, calendar

Merged from `r3plan-{glm,kimi,qwen}.md` + `r3rev-{glm,kimi,qwen}.md`. (qwen's
provider quota is now exhausted — proceed with glm + kimi.)

## Decisions

1. **Capture interception (item 1)** — kimi's state machine: `HOLD`/`ARMED`
   keydown reducer. Every printable key of a candidate burst is
   `preventDefault`-ed (nothing ever lands in a field); on burst-confirm the
   shape gate routes it, on burst-abort it is **replayed** into the focused
   target via `setRangeText` + `input` (caret-preserving). Per-type targets
   (text / number value-only / SELECT selectedIndex+change); `[data-scan-sink]`
   early-return (collapsed badge field + visible scan field untouched). Guards:
   `e.repeat`, IME composing, modifiers, non-printable flush, pointer/focus/paste
   flush, stale-snapshot caret fallback. `SCAN_DEFER_MS=60 > SCAN_MAX_GAP_MS=50`
   (pinned in Node + pytest). Decision logic is a **pure reducer** so node tests
   drive scanner bursts / slow typing / fast-unshaped typing / repeats.
2. **QR 60s (item 2)** — `LOGOFF_GRACE_SECONDS` default 30 → 60, plus a full
   copy sweep of every "30 s/seconds" string (index.html:139, docstrings,
   README, NOTES). Same-user/different-user logic already exists (qr-identify +
   save-then-switch).
3. **Greeting (item 3)** — a "Hi <user-name>" banner after login, then route to
   `#/catalogue`.
4. **Checkout (item 4)** — a new `#/checkout` route renders the catalogue section
   with the aside swapped to a review panel. The **server cart is the review
   state** (reuse `_cartOp` mutations + a `setLineQty` PATCH); "Complete purchase"
   → `goToCheckout()` with **no confirm()**; "Done" → `POST /api/orders` then
   `#/account` purchases tab with the receipt pre-expanded + toast. The receipt
   modal is shown only on skipped/clamped (preserve clamp-not-veto reporting).
5. **Calendar (item 5)** — backend `GET /api/orders/calendar?year=&month=`
   (validated ints, default current UTC month) → `{days: {"YYYY-MM-DD": {cents,
   count}}, total_cents}` with **UTC-day bucketing server-side**. Client builds a
   `Date.UTC` month matrix; `dayColour = hsl(120*(1-t), 73%, 75-25t%)` where
   `t=(v-min)/(max-min)` and `min==max` ⇒ `t=0` (light green). Below the calendar:
   total spent that month.

## Task order (frontend strictly serial)

- **A (backend)**: grace 60 + copy sweep + `/api/orders/calendar` + tests.
- **B (capture)**: node test harness + the capture state-machine rewrite.
- **C (frontend flows)**: greeting banner + `#/checkout` review screen + calendar UI.
- **D**: docs / verification / hardware checklist.

Each lands as its own PR (implement glm → review kimi → consensus → merge).
