# Task D FINAL spec — docs + hardware checklist (round 3 closing)

## Docs

- `README.md`: update the scanner/scanning paragraph — scans are now **fully
  intercepted** (never typed into focused fields), QR is auto-detected and logs
  in/off, the 60 s logoff countdown, the "Hi <name>" greeting + catalogue
  redirect, the `#/checkout` review screen, and the purchase-history calendar
  (green→red spend gradient + month total). Confirm the false-claims test stays
  green.
- `NOTES.md`: a short paragraph recording the round-3 changes.
- `docs/opticon-m10.md`: a one-line update to the countdown copy (60 s) if any
  remains, and a sentence on scan interception.

## Hardware checklist (`docs/plans/r3-checklist.md`)

Track A (HID, executable now):
1. Scan a QR while the email field is focused → the badge routes to login, and
   **no characters appear in the email field**.
2. Scan a product barcode while a text field is focused → item is added, no
   characters in the field.
3. Type a password normally (human speed) → every character lands (no hold
   eats it); fast typing in a search box still works.
4. Collapsed "use a badge code instead" fallback still accepts a manual badge.
5. Log in → "Hi <name>" banner → catalogue.
6. Add items → "Complete purchase" → review screen (no confirm dialog) → edit
   qty/remove/add → "Done" → purchase history with the receipt expanded.
7. Purchase history → calendar on the right: dots on purchase days (light green
   = lowest, red = highest), total spent this month below.
8. Scan own badge while logged in → 60 s logoff overlay (cancel / expiry logs
   off); scan another user's badge → save-then-switch.

Track B (USB-COM) unchanged, still [BLOCKED] on the xHCI/udev pre-flight.

## Gates

`node --check static/app.js`; `uv run pytest`; `ui_check.py --self-host`;
README false-claims test.
