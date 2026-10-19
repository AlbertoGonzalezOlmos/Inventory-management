# Task C FINAL spec — greeting, checkout review screen, calendar UI

Frontend (`static/app.js`, `static/index.html`, `static/style.css`). Backend for
the calendar already exists (`GET /api/orders/calendar`).

## 1. Greeting (item 3)

After a successful login (QR or password) show a **"Hi <user-name>"** banner
(the user's name). Then route to `#/catalogue`. Replace the current
"Welcome, <name>" toast with this banner+redirect (keep any existing toast for
switch/password-change flows). The banner auto-dismisses (a few seconds) or is a
dismissable top bar.

## 2. Checkout review screen (item 4)

- New route `#/checkout`. It renders the catalogue section with the aside
  swapped to a `.review-panel` (the cart in edit mode).
- **The server cart is the review state** (`this.cart`). Reuse
  `incPurchase`/`decPurchase`/`removeFromPurchase` (already `_cartOp`-serialized)
  and add a `setLineQty(itemId, qty)` (a PATCH `/api/cart/items/{id}` with
  absolute qty; qty 0 = remove).
- "Complete purchase" (cart panel button) → `goToCheckout()`: set
  `this.route = "#/checkout"` with **no `confirm()`**.
- The review panel shows each line (qty +/−, remove), line totals, grand total,
  stock warnings; scanning still adds items while on the review screen.
- "Done" → `POST /api/orders` (raw) → on 201 route to `#/account` with
  `profileTab="purchases"`, the new receipt pre-expanded (`expandedOrders =
  [id]`, `lastCheckout = id`), and a toast. On 400 (empty-cart race) → toast.
  Show the receipt modal **only** when there were skipped/clamped lines (preserve
  clamp-not-veto reporting).
- "Keep shopping" → `#/catalogue` (cart is durable, untouched).
- Empty cart → "Done" disabled.
- Own-badge scan on the review screen → the 60 s logoff overlay still works
  over it (route transitions handled by the existing overlay/hashchange guards).

## 3. Purchase-history calendar (item 5)

- On the purchases tab: a **two-column layout** — history list left, calendar
  right.
- `GET /api/orders/calendar?year=&month=` (defaults current month). Build a
  `Date.UTC` month matrix (Monday-first week). Render each day cell; days with a
  purchase get a dot.
- Dot colour: `hsl(120*(1-t), 73%, 75-25t%)` where `t = (v-min)/(max-min)` over
  that month's per-day amounts; `min==max` ⇒ `t=0` (light green). First purchase
  (the min) is therefore light green.
- Below the calendar: "Spent this month: <total_cents>" (the endpoint's
  `total_cents`).
- Month navigation (prev/next) is optional; at minimum show the current month.

## Gates

`node --check static/app.js`; `uv run pytest` (backend unchanged); `ui_check.py
--self-host` (existing pins green; the new routes are authenticated so no new
one-shot pins, but the app must still mount with zero console errors). Manual
checklist: greeting→catalogue, checkout review (edit/done), calendar dots +
total.
