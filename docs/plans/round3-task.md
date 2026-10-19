# Round 3 task — scanner behavior, login flow, checkout, purchase-history calendar

Recorded for coordinator continuity. Agents (glm/kimi/qwen) are fresh after a
context reset; they explore the repo from scratch.

## 1. Scan interception (stop pasting into focused fields)

Today the M-10 (USB-HID keyboard wedge) types scanned text into whatever field
is focused; the SPA's global capture buffers and then restores editable fields,
so the text still briefly appears. Change: the SPA must globally intercept a
scan burst, classify it (QR badge vs product barcode), and route it — the
scanned text must **never** appear in any focused input/textarea (use
preventDefault on the whole burst; human-speed typing still passes through).

## 2. QR handling while logged in

- Same user's QR → "log out?" prompt with a **60-second** countdown (currently
  30s). On expiry → log out.
- Different user's QR (exists in DB, not already logged in) → save current
  user's state, log out the current user, then log in the incoming user.
  (Mostly exists: qr-identify + save-then-switch; timer is `LOGOFF_GRACE_SECONDS`
  default 30 in `app/security.py`.)

## 3. Login greeting + redirect

On login, show **"Hi <user-name>"** (the user's name), then navigate to the
catalogue. (Today it shows "Welcome, <name>" and may not route to catalogue.)

## 4. Checkout flow

Clicking "Complete purchase" currently pops a `confirm("Complete this purchase
(N item(s), …")` (static/app.js ~line 899). Change: skip the confirm and go
directly to a **review screen** where items can still be modified (quantities,
remove, add). Clicking "done" completes the purchase and navigates to the
purchase-history screen.

## 5. Purchase-history calendar

On the purchase-history screen: a calendar of the current month on the right
side of the list. Each day with a purchase gets a coloured dot; the colour
ramps light-green → red by that month's min/max spend (gradient for values in
between; the first purchase is light green). Below the calendar, show the total
spent that month.
