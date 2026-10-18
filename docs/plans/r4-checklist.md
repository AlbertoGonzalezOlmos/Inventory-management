# Round 4 hardware checklist — the live 12-step Track-A run

The offline gates (pytest both orders, `node --test`, `ui_check --self-host`,
the smoke round trip) are CI's job. This checklist is the LIVE pass with the
scanner attached — the round-3 precedent (`docs/plans/r3-checklist.md`):
run it once per kiosk install and after any daemon/kernel/udev change, and
record the outcome (date, steps, deviations) in the PR.

Hardware facts at plan time: the M-10 in USB-HID mode enumerates as
`065a:a001` ("Opticon USB Barcode Reader"); its evdev node is
`/dev/input/eventN` (`root:input 0660` stock — the udev rule grants the
group); node NUMBERS are unstable across replugs, so nothing may cache one.

## The steps

1. **Pre-flight — permissions and decode.**
   `sudo cp scripts/udev/99-opticon-scanner.rules /etc/udev/rules.d/ && sudo
   udevadm control --reload-rules`, then re-plug the scanner.
   `ls -l /dev/input/event*` shows the scanner's node as `root:plugdev 0660`;
   `uv run python scripts/scanner_daemon.py --dry-run` grabs, decodes and
   prints: scan a barcode → it prints (and nothing types anywhere while
   the grab is held); scan a badge → it prints **redacted**
   (`HCRM1:••••`, never the payload). Ctrl-C exits cleanly (exit 0) and
   typing into windows resumes — the whole grab lifecycle in one command,
   zero HTTP.

2. **The grab stops the typing** (the round-4 headline invariant). Focus a
   plain terminal window. With the daemon running (any mode), scan a
   barcode: the decoded string appears in the daemon's output and **nothing
   — not one character — lands in the focused terminal**. Control: stop the
   daemon, scan again → the keystrokes type into the terminal (that is the
   wedge you just eliminated while the daemon lives).

3. **Pair from the header pill.** In the kiosk browser, click the
   **"no scanner"** pill in the header (a button since the login-screen
   cleanup); enter the terminal id + the 8-char pairing code the daemon
   printed (`--register`) in the popover. The pill flips to **live**
   ("scanner") and a toast confirms. (Recovery for a stale binding: the
   same popover's Unpair → daemon `--register` → new code.)

4. **Badge login with the browser UNFOCUSED** (the whole point of round 4).
   Focus a terminal (or anything that is not the browser). Scan a member's
   QR badge: the SPA logs in — greeting banner, catalogue — with zero
   keystrokes anywhere. The terminal stays clean.

5. **Barcode → cart via the feed.** Scan a catalogued barcode (unfocused
   browser is fine): the cart gains the line, the toast names the item.
   Scan an unknown code → the intake modal opens; a misread → the 422
   "re-scan" toast.

6. **Same-user badge → the 60 s overlay.** Scan the logged-in user's own
   badge: the logoff countdown appears, counts down from the SERVER
   deadline; [Stay logged in] cancels; letting it expire logs off.

7. **Different-user badge → save-then-switch.** With items in the cart,
   scan another member's badge: the outgoing cart is flushed/saved first,
   the session switches, the incoming user's cart loads, the toast says
   whose list was saved.

8. **`kill -9` → typing resumes** (the auto-release proof). `kill -9` the
   daemon mid-run and scan into a focused field: the keystrokes arrive
   again immediately — the kernel released the grab with the fd, no
   operator action. (Restart the daemon afterwards; systemd does it in 2 s
   if the unit is installed.)

9. **No double-delivery.** With the daemon running AND the browser
   focused, scan once: exactly one cart line (or one login), and no
   characters in the focused field. The two paths are mutually exclusive
   by construction — the grab means the browser sees no keystrokes; when
   the grab is gone, the queue is empty.

10. **TTL expiry.** Restart the server with
    `HCRM_SCANNER_EVENT_TTL_SECONDS=10` (scratch data dir!), close the
    kiosk tab, scan a barcode, wait > 10 s, reopen the tab: the poll
    reports nothing — a scan nobody consumed in time must never replay
    into a later session. (Then restore the normal 300 s default.)

11. **Status pill states.** Unpaired → "no scanner" (dashed). Paired and
    healthy → "scanner" (green). Stop the SERVER (not the daemon) → the
    pill flips to "scanner offline" (amber) within ~1 s and the browser
    keeps retrying; restart the server → back to green. Honest limitation:
    the pill shows the CHANNEL state (paired + server reachable) — a dead
    DAEMON leaves it green with the queue going quiet; steps 2/8 are the
    daemon-liveness observables.

12. **Unplug/replug → re-discovery.** Unplug the scanner: the daemon logs
    the loss and retries with backoff (never exits). Re-plug: it
    re-discovers by VID/PID — a NEW `eventN`, never the remembered one —
    re-grabs within ~2 s, and the next scan flows (steps 4–5 again).

## Bonus (recommended once): the server-outage spool

Stop the server, scan 2–3 barcodes (the daemon keeps grabbing, logs
"scan spooled"), restart the server: the spool replays in order, each scan
delivered exactly once, `<config>.spool` disappears. Kill the daemon
mid-outage and restart it: same result (the spool is durable, 0600).
