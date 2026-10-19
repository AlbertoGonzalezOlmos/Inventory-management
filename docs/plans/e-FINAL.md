# Task E FINAL spec — Login auto-detect QR (UX)

Small, well-scoped UX change. The global capture (Task 5) already routes
logged-out badge scans straight to `badgeLogin`; this task removes the
"click to scan" friction and hardens the entry.

## Changes

1. **Demote the dedicated badge form** (`static/index.html` line ~66–80): hide
   it behind a collapsed "Use a badge code instead" toggle (default collapsed).
   The global scan field ("⌁ Scan items or badges here (or scan anywhere)…")
   is the primary path and remains visible on the login view.
2. **Copy** (login card + scan field): replace "Scan your QR badge here" /
   "click here to scan" language with "Scan anywhere — your QR badge logs you
   in automatically" (and, while logged in, "…or logs you off"). No "click"
   requirement anywhere.
3. **Busy double-fire guard** in `badgeLogin(code)` (`static/app.js` ~line 412):
   if `this.busy` is already true, return early (a fast double-Enter must not
   fire two `qr-login` requests).
4. **Focus rule regression check**: confirm badge scans landing in the
   email/password inputs (logged-out) are buffered and routed to login (the
   Task-5 pre-burst-restore behaviour) — this is the no-hijack guarantee for
   the "scan anywhere" claim.

## Verification

- `node --check static/app.js`; `uv run pytest` green (backend untouched).
- `ui_check.py`: add/update a count()-only login-copy pin (the new "scan
  anywhere" string) + keep the existing badge-field regression pin (it now
  targets the collapsed-fallback field).
- `ui_check.py --self-host` (if a browser is available).
- Manual checklist: badge scan with focus in email / in password / nowhere
  (each logs in, nothing typed into the field); double-Enter fires one login;
  logged-in badge scan still logs off/switch (unchanged).
