# Round 3 hardware checklist — scanner interception, login, checkout, calendar

Status record for the round-3 behaviours that need the physical kiosk (the
Opticon M-10 in USB-HID keyboard mode). Round 3's automated gates are all
green and architecture-independent (`uv run pytest`, `node --test
tests/js/*.test.mjs`, `scripts/ui_check.py --self-host`); what this checklist
covers is the wedge itself — the one input source no synthetic event stream
can fully stand in for.

Hardware context: `docs/m10-unreachable-2026-10-10.md` — the xHCI controller
was revived (unbind/bind + PCI remove/rescan, no reboot) and the scanner
enumerates as `065a:a001` on `/dev/hidraw1`, but that node is `root:root
0600` and the §4 prevention step (plus the udev rule,
`scripts/udev/99-opticon-scanner.rules`) has not been applied, so the
controller can die again. Re-check §8 before starting.

## Track A — USB-HID (the configured personality; executable once the scanner is reachable)

Run against a scratch server (`HCRM_SCRATCH=1 ./scripts/run.sh`), fresh
browser profile, no seeded credentials beyond the scratch admin. Each step
names the invariant it proves; a failure anywhere stops the pass.

1. **Badge scan into a focused field never paints.** Focus the login page's
   *email* input, scan a member's QR badge → the badge routes to login (the
   pill shows `scan: badge received`, the session starts) and **no characters
   appear in the email field at any moment** — not even a flash of the first
   character. (Invariant: a shape-gated burst never writes to a field.)

2. **Product barcode into a focused field never paints.** While logged in,
   focus the catalogue *search* box, scan a product barcode → the item is
   added to the purchase list (toast + list update) and the search box stays
   exactly as it was — no characters, no search firing.

3. **Human typing is untouched.** Type a password at normal speed → every
   character lands, login works. Then type quickly (fast but human, ~80 ms
   gaps) into the search box → characters appear as typed, only the first
   character of a run may lag ~60 ms. Also press-and-hold a letter key
   (auto-repeat) → the repeats land; Ctrl/Cmd+C/V and arrow keys behave
   normally mid-typing.

4. **The collapsed badge fallback still works.** On the login page, expand
   "Use a badge code instead" and type/paste a badge payload manually, press
   Enter → login (the sink's dedicated path — the global capture must never
   double-fire or eat it).

5. **Login greeting + catalogue redirect.** Log in (password or badge) → a
   "Hi \<name\>" banner appears, the catalogue loads behind it, and the
   banner dismisses itself after a few seconds (a click dismisses it early).
   The banner also appears on the badge-driven account switch.

6. **Checkout review screen (no confirm dialog).** Add items → "Complete
   purchase" → the `#/checkout` review screen opens with **no confirm()
   dialog**: change a quantity with the number box, remove a line, click a
   catalogue item to add, scan a barcode (still adds while on the review
   screen) → "Done" → lands on purchase history with the new receipt
   pre-expanded ("new" chip) and a total toast. No receipt dialog for a
   clean checkout; the dialog appears only if a line was clamped/skipped
   (verify with a stock-1 item and qty 3: the receipt shows "1 of 3").

7. **Spend calendar.** Purchase history tab → calendar on the right: a dot
   on every purchase day (make ≥ 2 purchases on different days; the
   lowest-spend day's dot is light green, the highest is red, in-between
   days interpolate), "Spent this month" total below matches the sum of the
   month's receipts, and today's purchase dots immediately after checkout.
   Month ‹ › navigation loads other months.

8. **Badge flows while logged in (60 s).** Scan your own badge → the logoff
   overlay counts down from **60** with Stay-logged-in / Log-off-now;
   letting it expire logs off. Scan a different member's badge →
   save-then-switch (the outgoing user's cart survives for their next
   login, the kiosk lands on the catalogue as the new user).

## Track B — USB-COM (VCP / CDC-ACM): **[BLOCKED]**

Unchanged from before the round: not testable until the xHCI/udev pre-flight
from `docs/m10-unreachable-2026-10-10.md` is applied (the controller fix's
§4 prevention step + the udev rule granting non-root `hidraw`/`ttyACM`
access). The USB-COM bridge's back-office modes (`docs/opticon-m10.md`
§6/§6a) are untouched by round 3; nothing in this round needs Track B to
close. Re-assess after the pre-flight lands.

---

**Pass/fail record:** (fill in on the hardware run — date, steps passed,
failures with repro.)
