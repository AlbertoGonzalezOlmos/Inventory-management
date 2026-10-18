# Task 6 hardware checklist — Opticon M-10 end-to-end verification

For the PR description. Context: the dev unit is in its **USB-HID keyboard
personality** (works today, no udev rule, no bridge). USB-COM is blocked on
host fixes documented in `SCANNER-AGENT-SYNC.md` (xHCI controller
`0000:c3:00.3` died during a suspend/resume — USB-A buses 1/2 produce zero
attach events; `/dev/hidraw*` is root-only until the udev rule is
installed).

Backend proven in CI: `uv run pytest` (500+) · `scripts/ui_check.py
--self-host` · `scripts/smoke_test.py` (kiosk loop, 12 checks incl.
scan-to-cart, misread, unknown-404 with prefix sibling, ScanRequest,
schedule/cancel).

---

## Track A — USB-HID (executable now)

Setup: `./scripts/run.sh`, open the SPA, scanner plugged in (HID mode).
Two accounts with printed QR badges (register a fresh member — the badge
prints at creation — and use the admin's, issued via Members → QR badge).

1. **Typing proof**: focus any text field, scan a product barcode → digits
   + CR appear.
2. **Badge login (regression)**: on the login page, scan a badge into the
   *QR badge* field → logged in without a password.
3. **Page-level scan-to-cart**: on the catalogue with *no field focused*,
   scan a known barcode → toast + purchase-list line + total. Scan again →
   qty 2. If the unit can transmit symbology IDs, scan a UPC-E item and
   confirm it finds the EAN-13 filing. `qty > stock` → warning only.
4. **Misread**: scan a GTIN with a flipped digit (or type one into the
   scan field) → "MISREAD — re-scan" toast, nothing added.
5. **Own-badge countdown**: scan your own badge → full-screen overlay
   counts down from 30 (server deadline). [Stay logged in] cancels. Scan
   again → countdown restarts. Let one expire → logged out. **Close the
   tab mid-countdown, wait 30 s → that token is dead everywhere** (curl
   `/api/auth/me` with it → 401 — the Task-4 lazy-expiry property).
6. **Cross-account switch**: with items in user A's list, scan user B's
   badge → "Switched to B — A's list was saved". B's list loads. Switch
   back → A's list is intact. Curl the old kiosk token → 401 (revoked).
7. **Unknown barcode, staff**: scan an unknown valid GTIN → intake modal
   with region/usage line + "Same barcode family" suggestions. Suggestion
   click adds that item. Submit the form (SKU prefilled from the GTIN) →
   item created and added.
8. **Unknown barcode, member**: same scan as a member → "Send to staff".
   It appears in Admin → Scan requests; staff *Create & resolve* with the
   add-to-cart checkbox → the item lands in the member's list; Account →
   My scan requests shows *resolved*.
9. **Attribution (logged out)**: log out, scan two barcodes → banner
   "2 scanned item(s) waiting". Scan a badge → both replayed into that
   account's list.
10. **Pill masking**: scan a badge anywhere → the scan pill shows
    "scan: badge received" — **never** the `HCRM1:…` payload.
11. **Typing never hijacked (negative)**: type fast in the search box and
    the password field (incl. held keys) → no scan fires, no characters
    lost. Fire a scan while the search box is focused → the scan routes
    and the search box is restored to its pre-scan content.
12. **Print once**: register a new account → badge modal → Print yields
    the badge card alone. Reload → the code is never shown again. Same for
    the account-page badge print.

## Track B — USB-COM — [BLOCKED on host fixes]

Pre-flight (from `SCANNER-AGENT-SYNC.md`, owner/sudo required):
1. Revive xHCI `0000:c3:00.3`: unbind/bind, then PCI remove+rescan (no
   FLR — `reset_method=pm`), else reboot; or move the scanner to a port on
   a live bus. Set `power/control=on` for the controller (suspend/resume
   prevention).
2. Install `scripts/udev/99-opticon-scanner.rules` (tty/hidraw/usbfs
   permissions) and the ModemManager ignore rule
   (`ENV{ID_MM_DEVICE_IGNORE}="1"` for VID 065a).
3. Verify: `uv run python scripts/opticon_detect.py` lists `065A:A002`
   on a `/dev/ttyACM*`. [BLOCKED]

Then:
4. [BLOCKED] `uv run python scripts/m10_scan_test.py` — stream sanity.
5. [BLOCKED] `uv run python scripts/scanner_bridge.py --email … --json`:
   known GTIN → match record; `--stock-out 1` decrements atomically;
   misread → MISREAD verdict; unknown GTIN → new-entry candidate verdict.
6. [BLOCKED] Badge scan → badge_login record; **text mode shows the
   redacted token** (`… (redacted — --show-token or --json …)`), full
   value only via `--json` or `--show-token`. `--relogin` after a manual
   token revoke re-authenticates once.

**Not on the checklist**: kiosk flows over USB-COM — the counter-session
relay is Phase B, deferred; the SPA flows above are HID-transport.
