# Round 4 plan — glm-5.3 (OS-level scanner grab + scan forwarding, option 2)

Grounding (spot-verified): `docs/plans/round4-task.md`; `scripts/scanner_hid.py`
(`_USAGE_MAP`, `parse_keyboard_report`, `ScanAssembler`, `find_hidraw`,
sysfs VID/PID matching); `scripts/scanner_bridge.py` (`api`/`ApiError`,
`redact_code`, the "always-on bridge survives its environment" doctrine);
`app/routers/cart.py` (`POST /api/cart/scan` envelopes: 200 added / 400 badge /
422 misread / 404 unknown+intake payload); `app/routers/auth.py` (`qr-login`,
`qr-identify`, `logoff-schedule`/`logoff-cancel`, `_RAW_TOKEN_RE`);
`app/deps.py` (bearer auth, lazy logoff expiry); `app/main.py` (router wiring,
`COLUMN_MIGRATIONS` — **new tables need no migration, `create_all()` adds
them**; CSP has `connect-src 'self'` — load-bearing for the channel design);
`static/app.js` (`enqueueScan`/`drainScanQueue` bus ~1410, `handleBadgeScan`
~1414, `handleBarcodeScan` ~1470, `armLogoff` ~1524, `switchAccount` ~1572,
`installScanCapture` ~1131, `mounted` ~2247, `api()` with `{token, raw}`
foreign-token support); `static/scancapture.js` (`isBadgeShape`,
`BADGE_FULL_RE`/`BADGE_RAW_RE`); `scripts/udev/99-opticon-scanner.rules`
(covers tty/hidraw/usb but **not** the input subsystem); `.github/workflows/ci.yml`
(pytest both orders, `node --test tests/js/*.test.mjs`, ui_check, scripts
jobs — all globs pick up new test files automatically); hardware facts from
`docs/plans/r3rev-qwen.md` §R1: the HID personality enumerates as
`/dev/input/by-id/usb-Opticon_Opticon_USB_Barcode_Reader-event-kbd → event14`
on this host, and a root-only `/dev/input/eventN` blocks *our* process, not
the X server — exactly why the grab must be ours.

Note on names: the task summary says `handleScan`; the actual bus is
`enqueueScan(code, kind)` → `handleBadgeScan`/`handleBarcodeScan`. This plan
uses the real names.

---

## D0 — The three design decisions (read first)

### D1. Grab the evdev node, read the evdev node, decode by translating
evdev keycodes → HID usages → synthetic boot reports fed to the existing
`ScanAssembler`.

- **Why evdev and not hidraw:** hidraw and evdev are parallel clients of the
  HID driver. Reading `/dev/hidraw1` never stops typing; `EVIOCGRAB` on
  `/dev/input/eventN` is the only stdlib-reachable mechanism that removes the
  scanner's keystrokes from every evdev consumer (X, Wayland/libinput, the
  console). That is the whole point of option 2.
- **Why read the grabbed stream instead of pairing a hidraw node:** one
  device, one stream — no event↔hidraw pairing risk, no stale-data window,
  works even where hidraw is absent, and reading drains the grabbed client
  queue instead of letting it wrap. The task's "reuse the HID usage map /
  `parse_keyboard_report`" is honoured structurally: the daemon keeps a flat
  `KEY_*`→HID-usage dict (~40 entries — Linux keycodes are NOT offset from
  HID usages: `KEY_A`=30 vs HID 'a'=4, `KEY_1`=2 vs HID '1'=30, and letters
  are non-contiguous 30–38/44–50, so a flat dict is the only correct shape),
  builds synthetic 8-byte boot reports `[modifier, 0, usage, 0×5]` from
  EV_KEY press events, and feeds them to `ScanAssembler` **unchanged** — the
  tested terminator/text-ordering logic is the logic that runs. Coexistence:
  `EVIOCGRAB` does not affect hidraw, so `scripts/scanner_hid.py` keeps
  working (documented, not relied on).
- **Why stdlib `fcntl.ioctl`, not the `evdev` package:** the repo's scripts
  are deliberately stdlib (scanner_hid.py's docstring brags about it) and the
  main deps list is minimal. `EVIOCGRAB = _IOW('E', 0x90, int) = 0x40044590`,
  computable with `struct`; `input_event` on 64-bit Linux is 24 bytes
  (`@llHHi`: timeval 16 + type 2 + code 2 + value 4). Two constants, zero
  dependencies.

### D2. Thin daemon, fat SPA: the daemon forwards **raw codes** into a
per-terminal server-side queue; the SPA polls the queue and feeds each code
into the **existing** `enqueueScan` bus. The daemon does NOT call
`/api/cart/scan`, `qr-login`, or anything user-facing.

- The round-3 SPA already implements every flow the task names: badge →
  (`handleBadgeScan`) logged-out `badgeLogin` / logged-in `qr-identify` →
  `armLogoff` (60 s server-deadline overlay) or `switchAccount`
  (save-then-switch); barcode → `/api/cart/scan` → 404-unknown → intake
  modal. Making the daemon replay those flows (the `scanner_bridge.py`
  shape) would fork the logic and still leave the browser's state stale —
  the SPA would have to be told about logins, carts and countdowns it didn't
  perform. One line into the existing bus instead:
  `enqueueScan(code, HcrmScanCapture.isBadgeShape(code) ? "badge" : "barcode")`
  — the exact shape `scanFieldSubmit()` already uses, so classification
  stays in its one home (`scancapture.js`), server-side parity is pinned by
  existing tests, and the daemon classifies only for log redaction (reusing
  `scanner_bridge.redact_code`).
- Polling, not SSE/WebSocket: `EventSource` cannot send an `Authorization`
  header (query-param tokens for badge-bearing feeds are worse); SSE and
  WebSocket add connection-lifetime machinery and are awkward under the
  httpx `TestClient` the suite is built on; a 1 s same-origin poll is
  under a second of latency on a counter flow and testable with plain
  requests. CSP `connect-src 'self'` also rules out the daemon serving the
  SPA directly on `localhost:<other port>` — the server-mediated queue is
  the CSP-legal channel. (SSE remains a documented upgrade path; the
  endpoint shape below is forward-compatible: same auth, same cursor.)

### D3. Counter-session binding (Phase B): terminals with two credentials
and a one-time pairing code.

- A **terminal** is one kiosk = one scanner + one browser. The daemon
  identifies itself at registration with a long-lived **terminal secret**
  (write capability: post scans, heartbeat, mint pairing codes); the browser
  holds a long-lived **terminal token** (read capability: poll the queue).
  Separating them means a browser-compromised token cannot *inject* scans,
  and the daemon secret cannot *read* badge payloads out of the queue.
- The browser obtains its token once via a **pairing code** the daemon
  prints (single-use, short TTL) — uniform for loopback and LAN
  deployments, and a deliberate human act, which is what binds "this
  browser" to "this physical counter".
- Scope honesty: one active browser per terminal. Broadcast + per-browser
  cursor (see D4) means a second paired browser would double-process scans
  — but the keyboard-wedge world already requires one-window-per-terminal
  discipline (any focused window eats the keystrokes), so the feed inherits
  the same operational rule instead of inventing per-browser sessions.
  Documented, not enforced.

### D4. Queue semantics: DB-backed, per-terminal, broadcast reads with a
per-browser cursor, TTL + cap purge.

- `ScanEvent` rows (autoincrement id = monotonic `seq`) are **not** consumed
  on read; each browser keeps its own `after` cursor in localStorage, so a
  restarted kiosk browser resumes exactly where it stopped, and a scan that
  arrived while the browser was closed is still delivered (server restart
  too — DB-backed, matching the "durable at every instant" cart doctrine).
- Badge payloads are bearer credentials, so the queue is treated like one:
  purge on every write and read (default TTL 5 min, cap 128/terminal, env
  `HCRM_TERMINAL_SCAN_TTL` / `HCRM_TERMINAL_SCAN_CAP`), never logged raw
  server-side, terminal token/secret/pairing-code stored **hashed**
  (reusing `security.token_hash`). The data dir already holds
  `data/badge.key`; this adds no new class of secret-at-rest.
- At-least-once delivery: the SPA advances its cursor only after the batch
  has been enqueued and `drainScanQueue()` has settled. A browser crash
  mid-batch redelivers (possible duplicate cart line, visible and
  correctable via the qty stepper) rather than silently losing a scan the
  customer watched succeed. The keyboard path could only ever lose.
  Trade-off documented in the poll wiring.

---

## D5 — EVIOCGRAB mechanics, permissions, lifecycle

**Grab.** `fd = os.open(node, os.O_RDONLY)`;
`fcntl.ioctl(fd, EVIOCGRAB, 1)` where `EVIOCGRAB = 0x40044590`
(computed, with a comment deriving it from `_IOW('E', 0x90, int)`, plus a
`struct.calcsize("@llHHi") == 24` boot self-check so a 32-bit host fails
loudly at startup instead of misparsing events). Release is
`ioctl(fd, EVIOCGRAB, 0)`; **closing the fd releases the grab** — including
on process death (SIGKILL included). No cleanup protocol is needed for the
fallback to engage.

**What the grab does:** the kernel stops delivering that device's events to
every other evdev client — X/Wayland get nothing, so nothing is typed into
any window, focused or not. Our own fd still receives events; the daemon
reads and decodes them. `EBUSY` from the ioctl means another process holds
the grab (e.g. a second daemon instance) — loud `SystemExit`, never a
silent wedge.

**Permissions.** `/dev/input/eventN` is `root:input 0660` by default. The
existing `scripts/udev/99-opticon-scanner.rules` covers tty/hidraw/usb but
not input — it gains one line:

```
SUBSYSTEM=="input", KERNEL=="event*", ATTRS{idVendor}=="065a", MODE="0660", GROUP="plugdev"
```

(`ATTRS` traverses to the parent USB device, which carries `idVendor` — the
same trick the hidraw line uses; the file header comment is updated.) Then
re-plug / `udevadm trigger`. Without sudo, the fallback is running the
daemon as root via the systemd unit — documented, not preferred.

**Discovery.** Scan `/sys/class/input/event*/device`, walk up to the HID
device, match `HID_ID` VID:PID `065A:A001` — the same shape as
`_hidraw_sysfs()`. Fast path via `/dev/input/by-id/usb-*-event-kbd` symlink
when present, always verified through sysfs (product strings vary across
Opticon personalities). No event node for the VID/PID → loud exit ("cannot
grab; the scanner would keep typing") — a gate that cannot run must fail,
never skip.

**Lifecycle / replug.** Blocking read loop; on `ENODEV` (unplug, usbipd
detach) the daemon releases, re-runs discovery every 2 s, re-grabs when the
device returns (a replug creates a NEW eventN — never cache the node
across replugs). `SIGTERM` → clean close (release + flush buffered scans).
systemd unit `Restart=always` covers crashes; udev re-applies permissions
on the fresh node.

**Daemon → server outage.** `scanner_bridge`'s doctrine, copied verbatim in
spirit: per-scan API errors are reported and the listener keeps going; if
POST fails (server restarting), scans buffer in memory (cap: 32 scans /
30 s, then drop-oldest with a loud stderr line) and retry. The grab is
NEVER released because the server is down — stopping the typing is the
product; delivery is best-effort with a visible queue-depth log line.

**Fallback when the daemon is not running.** No grab → the M-10 is a plain
keyboard wedge again → the round-3 `scancapture.js` capture (already
installed in `mounted()`, works on every view, behind modals, logged in or
out) catches scans whenever the browser has focus. The two paths are
mutually exclusive in practice: daemon up ⇒ keystrokes never reach the
browser; daemon down ⇒ the queue is empty and stale, so the poll feeds
nothing. A mid-scan daemon death can at worst split one scan between the
paths (a few characters may land as a mis-shaped burst and be replayed by
the round-3 adapter). The SPA's status chip (T4) tells the operator which
mode is live, so "daemon offline" is observable, not discovered by a scan
typing into a terminal.

---

## D6 — Channel + counter-session: the concrete endpoints

Router `app/routers/terminals.py`, prefix `/api/terminals`, tags
`terminals`. All bearer credentials are looked up by SHA-256 hash
(`security.token_hash`), the house pattern.

| Endpoint | Auth | Body → Response | Notes |
|---|---|---|---|
| `POST /register` | loopback client only (gate: `request.client.host`; open with `HCRM_TERMINAL_REGISTER=any`) | `{terminal_id}` → `201 {terminal_id, terminal_secret}` (secret shown ONCE; `409` if the terminal already has one — recovery is staff delete) | Creates the terminal row: `terminal_id` (default hostname, `^[a-z0-9_-]{1,64}$`), `secret_hash`. |
| `POST /pairing-code` | Bearer terminal **secret** | `→ {pairing_code, expires_at}` | 8-char base32 (Crockford, no lookalikes), single-use, 10-min TTL, stored hashed. The daemon prints it at startup and on `--show-pairing`. |
| `POST /pair` | none (the code IS the credential; 40 bits makes brute force infeasible on a LAN) | `{pairing_code}` → `{terminal_id, terminal_token}` | Consumes the code. Wrong/expired/reused → `401` with a status-discriminated detail (house envelope style). Token = read capability, long-lived, hashed at rest. |
| `POST /heartbeat` | Bearer terminal secret | `→ {ok, server_time}` | Every 10 s from the daemon; updates `last_seen`. |
| `POST /scans` | Bearer terminal secret | `{code, symbology?}` → `201 {seq}` | Appends one scan; purges TTL/cap-overflow rows (own terminal first, then global TTL); refreshes `last_seen`. `code` max_length 128, non-empty. No classification server-side (D2) — the SPA classifies. |
| `GET /scans?after=SEQ` | Bearer terminal **token** | `→ {terminal_id, daemon: {last_seen, online}, scans: [{seq, code, symbology, scanned_at}]}` | Read-only broadcast; `after` is exclusive; missing/invalid → from 0. `online` = `last_seen` within 30 s. `401` on bad token → the SPA stops polling and shows the re-pair banner. |
| `GET /` | staff | `→ [{terminal_id, last_seen, created_at}]` | Ops/recovery listing (no secrets). |
| `DELETE /{terminal_id}` | staff | `204` | Recovery path for a lost secret/compromised kiosk; also deletes queued scans + mints nothing. |

Models (`app/models.py`, new tables — no migration entries needed):

```python
class Terminal(SQLModel, table=True):
    id: int | None = pk
    terminal_id: str = unique, indexed
    secret_hash: str                       # daemon write capability
    token_hash: str | None                 # browser read capability (until paired)
    pairing_code_hash: str | None
    pairing_expires_at: datetime | None
    last_seen: datetime | None
    created_at: datetime

class ScanEvent(SQLModel, table=True):
    id: int | None = pk                    # autoincrement == seq == cursor
    terminal_row: fk terminals.id, indexed
    code: str (max 128)
    symbology: str = ""
    created_at: datetime
```

SPA side (details in T4): localStorage `hcrm.terminalId` / `hcrm.terminalToken` /
`hcrm.terminalSeq`; poll `GET /api/terminals/scans?after=<seq>` every 1 s via
the existing `api(path, {token, raw: true})` foreign-token form; feed each
code into `enqueueScan`; await `drainScanQueue()`; persist the cursor; render
the mode chip from `daemon.online`.

Security ledger: badge payloads transit and briefly rest in the queue
(hashed credentials everywhere else, TTL-purged here — the trade is that a
kiosk browser must receive the raw payload to exchange it at `/qr-login`,
exactly as the keyboard path already delivers raw payloads to the page);
register is loopback-gated so a LAN peer cannot mint terminals (opt-in env
to open, plaintext-HTTP caveat = the app's existing LAN posture, noted);
pairing codes are hashed, single-use, 10-min TTL; injected scans via a
stolen secret can at worst make the kiosk UI try a login/lookup that fails
(redaction keeps them out of daemon logs).

---

## Ordered task breakdown

Serialization: T1 → T2 → T3 → T4 → T5 recommended (T2 and T4 both depend on
T1's contract, not on each other; only T4 touches `static/app.js` /
`index.html` / `style.css`, so the repo's frontend-serial rule is satisfied
by construction). Every task lands with its tests green in BOTH pytest
orders (CI runs forward+reverse) and `git status --porcelain` clean.

| # | Task | Layer | Files | Tests |
|---|------|-------|-------|-------|
| T1 | Terminal registry + scan queue API | backend | `app/models.py`, `app/schemas.py`, `app/routers/terminals.py`, `app/main.py` (router import+include) | `tests/test_terminals.py` |
| T2 | Grab daemon: evdev decode, EVIOCGRAB, forwarding client | daemon | `scripts/scanner_daemon.py` | `tests/test_scanner_daemon.py` |
| T3 | Ops packaging: udev input rule, systemd unit, install/recovery runbook | ops | `scripts/udev/99-opticon-scanner.rules`, `scripts/systemd/hcrm-scanner.service`, `docs/opticon-hardware.md`, `README.md` | rule-file pin in `tests/test_scripts_contract.py` (1 assertion) |
| T4 | SPA poll channel + pairing UI + mode chip | frontend | `static/kioskfeed.js` (new, pure), `static/app.js`, `static/index.html`, `static/style.css` | `tests/js/kioskfeed.test.mjs`, pins in `tests/test_frontend_contract.py` |
| T5 | Verification pass + docs sweep | all | `NOTES.md`, `README.md`, `scripts/ui_check.py` (1 DOM pin) | evidence bundle (below) |

### T1 — backend: `/api/terminals` (details)

- `app/models.py`: `Terminal`, `ScanEvent` as sketched in D6.
- `app/schemas.py`: `TerminalRegisterIn/Out`, `PairingCodeOut`, `PairIn`,
  `TerminalPairOut`, `TerminalScanIn`, `TerminalScanOut`, `TerminalScansOut`,
  `TerminalStatusOut` — status-discriminated error details per house style.
- `app/routers/terminals.py`: the eight endpoints of D6. The loopback gate
  is ONE injectable function (`_is_local(request)`) so tests can monkeypatch
  it (TestClient's client host is `testclient`, not loopback). Purge runs
  inside the scan-append transaction. `new_token`-style randomness from
  `app.security` for secret/token/pairing code (`secrets` module, 32 bytes
  base64url for the two bearers; 8-char Crockford base32 for the code).
- `app/main.py`: import + `app.include_router(terminals.router)` — one-line
  diff, appended next to the existing router list (the append-only doctrine
  that `COLUMN_MIGRATIONS` already encodes).
- `tests/test_terminals.py` (~20 cases): register (creates, loopback gate
  403 from non-local + env override, second register 409, bad terminal_id
  422); pairing-code lifecycle (mint under secret auth, pair once, reuse →
  401, expiry → 401, wrong → 401, unauthed mint → 401); scans POST (secret
  auth, token-rejected 401 — the capability separation pin, empty/128+ code
  422, seq monotonic, TTL purge with a monkeypatched clock, cap purge,
  per-terminal isolation); scans GET (after= exclusive semantics, cursor 0
  default, other terminal's rows invisible, `daemon.online` true/false by
  `last_seen`, bad token 401); staff list/delete incl. scan cleanup;
  security-header/CSP pass-through untouched (existing `test_headers.py`
  already guards the middleware globally).

### T2 — daemon: `scripts/scanner_daemon.py` (details)

Single file, stdlib only, `scripts/` CLI conventions (docstring with usage,
env overrides `HCRM_BASE`, `HCRM_HTTP_TIMEOUT`, exit codes, never a silent
exit). Structure, top to bottom:

1. **evdev decode (pure, offline-testable).**
   - `EV_KEY = 0x01`; `KEY_TO_USAGE: dict[int, int]` flat map (letters
     30–38/44–50 → 4–29, digits 2–11 → 30–39, ENTER 28 → 40, SPACE 57 → 44,
     punctuation 12,13,26,27,43,39,40,41,51,52,53 → 45–56) plus
     `KEY_LEFTSHIFT = 42` / `KEY_RIGHTSHIFT = 54` → modifier bits 0x02/0x20.
   - `class EvdevDecoder`: `feed(type_, code, value) -> bytes | None` — on
     EV_KEY value 1 (press only; 2=autorepeat ignored, 0=release only
     updates modifier state), emit the synthetic 8-byte boot report
     `[modifier, 0, usage, 0,0,0,0,0]`. An **unmapped** keycode logs loudly
     and resets the assembler buffer (a barcode with a character we cannot
     represent must be reported, never guessed — the misread doctrine).
   - `read_events(fd)` generator: `os.read(fd, 24)` → `struct.unpack("@llHHi", …)`.
   - Assembly: `scripts.scanner_hid.ScanAssembler` fed the synthetic
     reports — zero changes to the tested module.
2. **Discovery (pure, root-injectable).** `find_event_nodes(vid_pid,
   sys_root="/sys")` mirroring `find_hidraw`'s sysfs walk
   (`/sys/class/input/event*/device` → HID_ID match), plus the by-id fast
   path. `--list` prints candidates (the `scanner_hid --list` UX).
3. **Grab.** `open_grabbed(node)` context manager: open, `fcntl.ioctl(fd,
   EVIOCGRAB, 1)`, on `OSError EBUSY` → `SystemExit` naming the holder
   suspicion; exit → `ioctl(…, 0)` + close (belt; process death already
   releases). Struct-size self-check at startup.
4. **Terminal client.** Reuse `scanner_bridge.api`/`ApiError` by import
   (both module-level, argparse is inside `main()`). Registration: load
   secret from `--state` file (default `data/terminal-credentials.json`,
   `0600`); if absent → `POST /api/terminals/register`
   (`--terminal-id`, default `socket.gethostname()`), store; 409 → loud
   exit with the staff-delete recovery line. Heartbeat thread every 10 s.
5. **Forwarding loop.** Per scan: `POST /api/terminals/scans` with
   `redact_code(code)` in every human-readable log line; failure →
   bounded in-memory retry buffer (D5).
6. **CLI.** `--terminal-id`, `--event-node`, `--state`, `--base`, `--list`,
   `--show-pairing` (prints a fresh pairing code), `--count` (smoke runs).
   `SIGTERM`/`KeyboardInterrupt` → clean shutdown.

`tests/test_scanner_daemon.py` (~15 cases): decoder matrix (letters, digits,
shifted `!A)`, comma/space, enter-only → scan emitted, char-then-enter,
autorepeat ignored, release-then-press re-sync of modifiers, unmapped key →
buffer reset + no scan); **parity property test**: for every usage in
`_USAGE_MAP`, the evdev path (its KEY code, shifted and unshifted) yields
the same characters as `parse_keyboard_report` directly — the "reuse" claim
as an executable pin; discovery against a fixture sysfs tree in `tmp_path`
(match, no-match, by-id fast path); grab via a monkeypatched
`fcntl.ioctl` recording the constant and arg (0x40044590, 1 on grab, 0 on
release, EBUSY → SystemExit); forwarding with an injected fake `post`
(register → heartbeat → scan sequence, server-down buffering then retry,
409-recovery exit); redaction reuse pin; struct-size self-check.

### T3 — ops packaging (details)

- udev line from D5 + header comment update; note in the file that the
  daemon ALSO needs it (not just readability: the grab needs an open fd).
- `scripts/systemd/hcrm-scanner.service`: `After=network-online.target`,
  `ExecStart=%h/.../uv run python scripts/scanner_daemon.py` (template with
  a documented `WorkingDirectory`, `Restart=always`, `RestartSec=2`,
  `User=` the kiosk operator); install commands in the runbook.
- `docs/opticon-hardware.md` new section "§ Event-device grab (round 4)":
  mechanics, permissions, lifecycle, fallback; `README.md` ops subsection
  (install udev+unit, pair the browser, verify with `--list` + a scan).
- `tests/test_scripts_contract.py` gains one pin: the rules file contains a
  `SUBSYSTEM=="input"` line mentioning `065a` (a rule that silently
  disappears reverts the daemon to root-only, and nobody notices until the
  next re-plug — the failure mode this repo's contract tests exist for).

### T4 — frontend: poll channel, pairing, mode chip (details)

- `static/kioskfeed.js` (pure, node-tested, the `scancapture.js` pattern):
  `initialCursor()`, `onBatch(state, scans)` → `{toEnqueue, cursor}` (dup/
  out-of-order seq tolerance: only accept `seq > cursor`, sort by seq),
  `onPollError(state, status)` → next delay (401/403 → `unpaired`, stop;
  network → backoff ×2 up to 30 s; else 1 s), `daemonChip(state, daemon)`.
- `static/app.js`: load/persist terminal binding in `mounted()`; start the
  poll loop (always, logged out included — badge login IS the logged-out
  flow); per batch: `for (const s of batch) this.enqueueScan(s.code,
  HcrmScanCapture.isBadgeShape(s.code) ? "badge" : "barcode")`, then
  `await this.drainScanQueue()`, then persist the cursor (D4 at-least-once);
  `pairTerminal()`/`unpairTerminal()`; the chip state. No changes to
  `enqueueScan`, `handleBadgeScan`, `handleBarcodeScan`, logoff, or switch
  — that is the point of D2.
- `static/index.html`: `<script src="/kioskfeed.js">` BEFORE `app.js`;
  header chip (`data-terminal-status="paired-online|paired-offline|unpaired"`);
  pairing panel (code input + pair button, reachable from the login screen
  footer and Account view).
- `static/style.css`: chip + panel styles.
- `tests/js/kioskfeed.test.mjs`: cursor semantics (first poll, after=,
  dup seq, out-of-order, redelivery after restart), backoff transitions,
  chip mapping — injected clock, no DOM (the scancapture.test.mjs shape).
- `tests/test_frontend_contract.py` pins: script tag order; chip element
  present with the three data-states; `kioskfeed.js` served 200 (the
  `/scancapture.js` pin pattern); poll interval constant matches between
  app.js and kioskfeed.js default.

### T5 — verification + docs (details)

- Full suite forward+reverse, `node --test tests/js/*.test.mjs`,
  `ui_check.py --self-host` (its DOM pins keep the SPA honest), smoke/load
  jobs untouched.
- Hardware evidence protocol (the round-3 evidence-bundle style; the M-10 is
  attached per the sync doc): with the daemon running — (1) focus a
  terminal window, scan: NOTHING is typed, scan appears in daemon log AND
  in the SPA (badge → login; barcode → cart line / intake modal); (2)
  `--show-pairing` + pair a second browser profile → same queue, own
  cursor; (3) kill -9 the daemon → typing into the terminal RESUMES
  immediately (grab auto-release proof) and the SPA chip flips to
  "keyboard mode", round-3 capture still routes a scan; (4) unplug/re-plug
  → daemon re-grabs within ~2 s; (5) server restart mid-queue → daemon
  buffer drains, no scan lost. Record outcomes in `NOTES.md`.
- README/NOTES status updates; `docs/plans/round4-task.md` annotated.

---

## Risks & open questions

1. **`/dev/input/eventN` permissions on the dev host** — unverified from
   inside the repo (round-3 notes show the node exists, not its mode). The
   udev line handles it after a re-plug; if the host has no sudo, the
   systemd unit runs as root. **Open: confirm `ls -l /dev/input/eventN` +
   `id` on the dev machine before T2 verification.**
2. **WSL2:** if the kiosk were WSL2, usbipd HID may expose hidraw but no
   evdev node (stock kernel config varies). The daemon fails loudly (D5)
   rather than pretending. Not the dev machine's case (X server + event14
   observed), but the runbook must say so.
3. **Pairing UX is one manual step per terminal** (and re-pair after
   `localStorage` clear / staff terminal delete). Accepted: deliberate
   human act, uniform loopback/LAN. If it proves annoying, the documented
   upgrade is a loopback-only authenticated `GET /api/terminals/local` —
   deferred, not built.
4. **No rate limit on `POST /pair`** (no rate-limit infra in the app).
   Mitigated by a 40-bit single-use code with a 10-min TTL; on a LAN with
   plaintext HTTP, an attacker who can brute-force pairing can sniff
   bearer tokens anyway. Noted honestly rather than half-built.
5. **Mid-scan daemon death** can split one scan between grab and keyboard
   paths (a few chars land, round-3 adapter replays them as human text; the
   scan re-fires only if re-scanned). Bounded, documented, accepted.
6. **Second paired browser double-processes scans** (broadcast + local
   cursor, D3). Operational rule (one window per terminal) + chip makes it
   visible; server-side consume-on-read was rejected because it turns the
   failure into *split* delivery (worse: invisible).
7. **Badge payload at rest** in `scan_events` for ≤ TTL. Accepted with
   purge + no raw logging; flagged for the cross-review to challenge.
8. **Multi-worker uvicorn** would need the queue moved out of SQLite-per-
   process semantics — out of scope (single-process by design; noted for
   the record since SSE-upgrade would face the same constraint).
9. **32-bit hosts:** the 24-byte `input_event` self-check fails at startup
   rather than misparsing (16-byte events there); support deferred until
   such a host exists.
10. **Naming**: `/api/terminals` vs `/api/kiosk` — `terminal` chosen to
    match the task's own vocabulary; trivially renamable at review.

## Alternatives considered and rejected (for the review record)

- **Read hidraw for data, grab evdev only to silence** — works, but pairs
  two device nodes (replug mismatch risk) and leaves the grabbed evdev
  queue to wrap; the task's own framing ("decode the evdev key events")
  points at the single-stream design. Kept as a documented fallback shape.
- **`evdev` pip dependency** — one ioctl and one struct against a new
  runtime dep for a kiosk daemon; stdlib wins (repo doctrine).
- **SSE / WebSocket channel** — EventSource can't send Authorization
  headers; both add connection-lifecycle machinery and TestClient
  awkwardness for ≤1 s of latency savings. Poll now; endpoint shape
  (cursor, auth) is forward-compatible if SSE is ever wanted.
- **Daemon as a fat bridge (scanner_bridge shape)** — duplicates the SPA's
  flows AND still needs a state-push channel; the thin-daemon design needs
  only a one-line tap into the existing bus.
- **Daemon serving the SPA directly on localhost** — blocked by CSP
  `connect-src 'self'` (and would need a CSP weakening; not a trade worth
  making).
