# Task F — Hardware verification checklist (profile / checkout / auto-detect addendum)

Addendum to the Task-6 checklist: the profile feature's flows, verified with
the real Opticon M-10. Track A (USB-HID, `A001`) is executable today — no
udev rule, no bridge. Track B (USB-COM, `A002`) remains **[BLOCKED]** on the
host pre-flight from `SCANNER-AGENT-SYNC.md`.

## Track A — USB-HID (executable now)

### Auto-detect login (Task E)
1. On the login page, **focus nowhere** (click the page background) → scan
   a member's badge → logs in. No click, no field.
2. Focus in the **email** field → scan → the field is restored to its
   pre-scan content (nothing typed in) and the login proceeds.
3. Focus in the **password** field → scan → same restoration, login proceeds.
4. **Double-Enter** (the collapsed fallback's Enter racing the global
   capture, or a shaky scan) → exactly one login fires (the busy guard).
5. The collapsed **"Use a badge code instead"** toggle: expand → the field
   still works as the manual path (type/paste a code + Enter).

### Profile re-view (Tasks A + D)
6. Logged in → My profile → My QR badge → **Show my QR** → the modal shows
   the badge; close and re-open → **identical payload**; **Print** yields
   the badge card alone (print isolation).
7. A **legacy (generation-0) account** (pre-derivation): Show my QR → the
   migrated warning ("your previous printed badge stopped working") + the
   new badge; the old printed card now 401s at the scanner test login.
8. **Staff View**: Admin → Members → **View** on a member with a badge →
   the same modal; a staff actor against an admin target → 403 behaviour
   (no modal).

### Email / phone (Task B)
9. Change email (correct password) → the "signed out on other devices"
   toast; **a second browser tab's session dies on its next request**; the
   new email logs in, the old 401s.
10. Change email with a **wrong password** → the 400 message, nothing
    changed. Same-email no-op → 400, other sessions stay alive.
11. Change phone (`+34 600 123 456`) → saved normalised (`+34600123456`);
    empty field → cleared; `BAD` → the 422 shape message.

### Checkout + history (Tasks C + D)
12. Add items → **Complete purchase** → the receipt modal (total, lines);
    stock decremented in the staff table view; the cart panel is empty and
    the button is gone.
13. A **clamped** line (cart qty 5, stock 3) → the receipt shows
    "**3 of 5**" and the history keeps the same marker; stock is 0.
14. An **orphan** cart line (item deleted after adding) → checkout proceeds
    with the "skipped" note.
15. My profile → Purchase history → the order appears (newest first,
    pre-expanded); **edit the item's price** as staff → the history still
    shows the checkout-time price.
16. Deleting the member (staff) → their purchases disappear with the
    account (staff table / direct DB check).

### Regression (unchanged behaviours)
17. Logged-in badge flows: own badge → the 30-second countdown (cancel /
    expiry / close-tab-then-dead-token); another member's badge → the
    save-then-switch. Scan-to-cart, misread toast, unknown-item intake.

## Track B — USB-COM (`A002`) — [BLOCKED]

Blocked on the pre-flight chain from `SCANNER-AGENT-SYNC.md`:
revive the dead xHCI controller `0000:c3:00.3` (unbind/bind → PCI
remove+rescan, no FLR — `reset_method=pm`; or reboot; or move the scanner
to a USB-C port on a live controller) → install
`scripts/udev/99-opticon-scanner.rules` (+ the ModemManager ignore for
`065a`, `power/control=on` for suspend-resume). Then:

- `opticon_detect.py` sees `065A:A002` on a ttyACM.
- `m10_scan_test.py` stream check.
- Bridge modes unchanged (exact-identity lookup, `--stock-in/--stock-out`,
  badge → redacted token per §6a, `--json` the machine contract).
- **Not on this checklist**: browser kiosk flows over USB-COM — the
  counter-session relay is deferred Phase B, not built.

**Badge-key operations note (runbook §9.5)**: after any Track A session,
verify `data/badge.key` exists with mode 0600 and that `backup_db.sh`
copies it with the credential-bearing warning.
