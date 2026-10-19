# Round 4 task — OS-level scanner grab + scan forwarding (option 2)

## Problem

The M-10 is in USB-HID keyboard mode (`065a:a001`), so it "types" into whatever
window has OS focus. Browser-side JS interception (round 3) only works when the
browser is focused; it cannot stop the OS from typing into a terminal/other app.
Measured inter-key gap: 8–12 ms (avg 9.9 ms) — timing is not the issue.

## Goal

Grab the scanner at the OS level so it **stops typing into any window**, decode
its scans as data, classify QR-vs-barcode, and forward them to the app so the
existing SPA flows run — regardless of which window has focus.

## Requirements

1. **Daemon** (Python): exclusively grab the scanner's Linux event device via
   `EVIOCGRAB` (ioctl on `/dev/input/eventN`) so the kernel no longer delivers
   its keystrokes to the focused window. Note: reading `/dev/hidraw*` alone does
   NOT stop typing (hidraw and evdev are parallel streams) — the **grab** is the
   whole point.
2. **Decode** the evdev key events into scan strings (reuse the HID usage map /
   `parse_keyboard_report` logic in `scripts/scanner_hid.py`), detect the CR
   (Enter) terminator, and emit one scan string per barcode.
3. **Classify** each scan: QR badge (`HCRM1:` prefix or bare 43-char token) vs
   product barcode.
4. **Forward** scans to the app so the existing flows run: badge → login /
   60 s logoff / save-then-switch; barcode → `/api/cart/scan` (logged-in user)
   or unknown-item intake.
5. **Channel**: the SPA must receive these scans even though they don't arrive
   as browser keyboard events. Design the daemon→SPA channel (server-side scan
   queue polled by the SPA, SSE, or similar) and how the daemon's scans bind to
   the correct kiosk/terminal session (the deferred "Phase B counter-session").

## Existing pieces to reuse

- `scripts/scanner_hid.py`: raw HID report parsing (stdlib), `/dev/hidraw1`.
- `scripts/scanner_bridge.py`: scan→API forwarding (badge→qr-login, barcode→find_item), `call_with_relogin`, `--json`.
- Backend: `POST /api/cart/scan`, `POST /api/auth/qr-login`, `qr-identify`,
  `logoff-schedule`/`logoff-cancel`, `GET /api/orders/calendar`.
- SPA (`static/app.js`): `handleScan`/`onScan`/`enqueueScan`, `badgeLogin`,
  the 60 s logoff overlay, save-then-switch, checkout review, calendar.

## Key design questions (resolve in the plan)

- EVIOCGRAB mechanics (stdio ioctl vs the `evdev` dependency), permissions
  (`input` group / udev rule), and lifecycle (what happens when the daemon
  stops — does typing resume?).
- The daemon→SPA channel and counter-session binding (terminal_id / kiosk id),
  including how the SPA learns of scans and reflects cart/login/logoff changes.
- Fallback when the daemon isn't running (scanner reverts to keyboard-wedge?).

## Deliverable

An ordered, reviewable task breakdown (backend daemon / backend API / frontend),
files + tests each, and the channel/counter-session design.
