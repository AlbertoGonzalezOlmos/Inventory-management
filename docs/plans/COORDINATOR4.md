# Coordinator synthesis #4 — OS-level scanner grab + scan forwarding (option 2)

Merged from `r4plan-{glm,kimi,qwen}.md` + `r4rev-{glm,kimi,qwen}.md`.

## Architecture (converged)

**Thin daemon, fat SPA.** The daemon exclusively grabs the scanner's event
device (`EVIOCGRAB`, stdlib `fcntl.ioctl`) so the kernel stops typing into
windows, decodes evdev keys into scan strings, and POSTs each scan to a
**server-side per-terminal queue**. The SPA **paced-short-polls** that queue and
feeds each code into the existing `enqueueScan`/`handleScan` bus — so login /
60 s logoff / save-then-switch / cart-scan / intake all run unchanged.

## Decisions (settled)

1. **Classification** — a shared `app/scanshapes.py` (`HCRM1:` prefix / bare
   43-char token / barcode-ish / unshaped), used by auth, the bridge, and the
   daemon ingest. The **server re-derives kind at ingest** (never trust the
   client/daemon); a regex-source-equality parity test pins the three sites.
2. **Channel** — DB-backed queue, `POST /api/scanner/events` (daemon) +
   `GET /api/scanner/events` (SPA). **Paced short poll + cursor + lease**
   (at-most-once; consume-with-lease so two tabs can't double-add). No SSE (no
   auth header support / CSP). TTL **300 s** + ack-prune; badge payloads at rest
   bounded, `redact_code` in every daemon log path.
3. **Binding (counter-session)** — **reversed pairing** (glm's): the daemon
   prints a single-use 8-char pairing code; the browser enters it once to obtain
   a terminal token (stored hashed). Terminal credential split: daemon secret
   (write) vs browser token (read). Registration loopback-gated by default.
   Terminal-auth (not user-auth) so badge-login works from the logged-out screen.
4. **EVIOCGRAB** — stdlib `fcntl.ioctl(fd, EVIOCGRAB, 1)`; grab lifetime == fd
   lifetime (daemon exit/crash → auto-release → scanner reverts to keyboard
   wedge → round-3 capture takes over; no conflict). Re-discover by VID/PID on
   ENODEV/unplug; **guards** so a human keyboard is never grabbed. Udev rule
   (`KERNEL=="event*"` narrowed) grants the `input`/`plugdev` group.
5. **Badge payload** — raw payload rides the TTL-bounded, token-gated queue
   (inherent: the SPA needs it to log in); qwen's "server-resolves-badge, queues
   an adopt_token" variant is documented-and-deferred (device token minting is a
   downgrade).

## Task order (PR-sized, CI-green each)

- **T1** `app/scanshapes.py` + delegate auth/bridge + parity test.
- **T2** daemon evdev decode core (`scripts/scanner_daemon.py`) + synthetic tests.
- **T3** grab + lifecycle + udev + systemd unit + grab tests.
- **T4** queue API + models/schemas + wiring + tests.
- **T5** daemon forwarding/pairing/config + packaging + tests.
- **T6** frontend: `static/scanfeed.js` reducer + app.js adapter + status pill +
  pairing panel (the only frontend PR).
- **T7** docs + hardware-free smoke round trip (CI scripts job) + checklist.

## Note

qwen measured (review §9 errata): the scanner was detached at review time
(M1), `/dev/input/by-id` absent (M3). The daemon must tolerate replug/re-discovery
and must never assume the device is present.
