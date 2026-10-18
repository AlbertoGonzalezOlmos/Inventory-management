# Round 4 plan (kimi) — OS-level scanner grab + scan forwarding

## 0. The one-paragraph architecture

A new daemon (`scripts/scanner_daemon.py`) exclusively grabs the scanner's
evdev keyboard node (`EVIOCGRAB` on `/dev/input/eventN`), decodes the key
events **by translating them back into HID boot-keyboard reports and feeding
the existing, tested `ScanAssembler`** from `scripts/scanner_hid.py`,
classifies each scan with a shared shape-gate module (`app/scanshape.py`,
mirroring `scancapture.js`'s `scanShape`), and POSTs it to a new
**terminal-scoped scan-event channel** on the server. The SPA long-polls that
channel — even while logged out — and feeds every received event into the
**completely unchanged `enqueueScan(code, kind)` bus**, so badge login /
qr-identify / armLogoff / save-then-switch / `/api/cart/scan` / intake /
staging all run through the existing, tested flows. The daemon never calls
`qr-login` or `cart/scan` itself: the kiosk session token lives in the
browser, so the browser must stay the routing authority (the daemon is a
transport, not a client).

Why the SPA routes (and not the daemon, bridge-style):

- The kiosk's bearer token is in the browser's `localStorage`; the daemon
  cannot (and must not) mint sessions into a browser it doesn't own.
- `handleBadgeScan`/`switchAccount`/`armLogoff`/staging are ~300 lines of
  tested SPA flow. Reimplementing them daemon-side duplicates them and
  guarantees drift; routing an event through `enqueueScan` reuses them
  verbatim.
- The channel then carries exactly one thing: `{code, kind}` — the same
  contract `scancapture.js` already emits.

## 1. EVIOCGRAB mechanics, permissions, lifecycle

### 1.1 Reading + grabbing (stdlib only, no `evdev` dependency)

- Discovery: walk `/dev/input/event*`, resolve
  `/sys/class/input/eventN/device/`, read `id/vendor`+`id/product` (hex) and
  `name` — same sysfs pattern as `scanner_hid._hidraw_sysfs`. Match VID/PID
  `065a:a001` (name "Opticon USB Barcode Reader"). If several nodes match
  (multi-interface devices), pick the one whose name matches and note in the
  log which node was grabbed.
- Event struct: `struct input_event` = `timeval(16B) + type u16 + code u16 +
  value s32` → format `"llHHI"` (24 bytes) on 64-bit, `"iiHHI"` (16 bytes)
  on 32-bit: `fmt = "llHHI" if struct.calcsize("l") == 8 else "iiHHI"`.
  Read fixed-size records with `os.read`.
- Grab: `fcntl.ioctl(fd, 0x40044590, 1)` (`EVIOCGRAB = _IOW('E', 0x90,
  int)`); ungrab with arg `0` — but simply closing the fd releases it. One
  grab per device: a second grabber gets `EBUSY` (report it cleanly: "already
  grabbed — is another daemon running?").
- **No capability is needed beyond opening the node.** The permission
  boundary is the device node itself.
- Only `EV_KEY` (type `0x01`) matters; value `1` = press, `0` = release,
  `2` = autorepeat (ignored). Scanners emit press+release per key.

### 1.2 Decode: reuse, don't rewrite

evdev key codes are not HID usages, but there is a fixed standard
translation for the keyboard page (KEY_A=30 ↔ usage 4, KEY_1=2 ↔ usage 30,
KEY_ENTER=28 ↔ usage 40, KEY_LEFTSHIFT=42 / KEY_RIGHTSHIFT=54, …). The
daemon carries one small pure table `EVDEV_TO_HID_USAGE` and one pure
function:

```python
def evdev_to_report(code: int, value: int, shift: int) -> tuple[bytes | None, int]:
    """One key event -> (synthetic 8-byte boot report | None, new shift mask).

    Press -> [modifier=shift, 0, usage, 0,0,0,0,0]; release of a shift key
    just updates the mask; release of a printable key yields no report."""
```

Each press is turned into a synthetic boot-keyboard report and fed into the
**existing** `ScanAssembler` (`scripts/scanner_hid.py`) — so rollover
tolerance, the shift table, the usage map, and the "final char + CR in one
report" fix all come from the code `tests/test_scanner_hid.py` already pins.
The daemon's own tests script `(code, value)` event streams and assert the
emitted scan strings. This satisfies "reuse the HID usage map /
parse_keyboard_report logic" literally, not just spiritually.

### 1.3 Classification

New shared module `app/scanshape.py` (pure, no imports beyond `re`):

```python
def classify_scan(code: str) -> str | None:  # "badge" | "barcode" | None
```

mirroring `scancapture.js` `scanShape` exactly: `HCRM1:` + 43 token → badge;
bare 43-char `[A-Za-z0-9_-]` → badge; `^[0-9]{4,14}$` or opaque
`(?=.*\d)[A-Za-z0-9._/-]{4,64}` → barcode; else `None` (dropped with a log
line, same as the SPA gate). Shared sample vectors live in
`tests/fixtures/scan_shapes.json` and are asserted against **both** the
Python function and `HcrmScanCapture.scanShape` under `node --test` — the
anti-drift doctrine applied across the language boundary. The daemon and the
server (§2.2) both call `classify_scan`; the SPA keeps its own gate as the
last line of defence.

### 1.4 Permissions

Extend `scripts/udev/99-opticon-scanner.rules` (one vendor-wide file is the
house pattern) with the input subsystem:

```
SUBSYSTEM=="input", ATTRS{idVendor}=="065a", KERNEL=="event*", MODE="0660", GROUP="plugdev"
```

Same install step as before (`udevadm control --reload-rules` + re-plug /
usbipd re-attach). Desktop sessions get `uaccess` from logind anyway; WSL2
has no seat, so the rule is load-bearing there — same story as §4 of
`docs/opticon-hardware.md`.

### 1.5 Lifecycle and the fallback (this is the answer to "daemon not running")

- **Grab lifetime == fd lifetime.** Daemon exits, crashes, or is killed →
  the kernel releases the grab → the scanner instantly reverts to a plain
  keyboard wedge. There is no persistent state to clean up.
- **Fallback is therefore the status quo**: with no daemon, scans type into
  the focused window and round-3 `scancapture.js` intercepts them whenever
  the kiosk browser is focused. The two modes cannot conflict: while the
  grab is active the browser receives *no* keys at all, so the capture
  reducer simply stays idle. No change to `scancapture.js`.
- **Unplug/replug (and WSL2 usbipd re-attach)**: read fails with
  `EIO`/`ENODEV` → the daemon closes, re-runs discovery with backoff
  (1 s → 30 s cap), re-grabs when the node reappears. Loud log lines both
  ways.
- **Daemon up, server down**: scans are grabbed (nothing types anywhere) and
  POST fails → appended to a bounded local JSONL spool
  (`data/scanner-daemon-spool.jsonl`, cap 1000, drop-oldest with a warning);
  a drainer re-POSTs spool FIFO before new scans once the server answers a
  heartbeat. This is the bridge's "always-on process survives its
  environment" doctrine.
- **Daemon up, SPA closed**: events queue in the DB with a 2-minute TTL
  (§2.2) — a scan made while nobody is at the counter must not pop up after
  lunch.
- The SPA shows the mode: a header pill "scanner: live" (daemon heartbeat <
  30 s ago) vs "scanner: keyboard mode" (paired but no daemon) vs nothing
  (unpaired) — the operator can *see* which fallback is in effect.

## 2. Channel + counter-session (Phase B) design

### 2.1 Why DB-backed consume-on-read (not SSE, not in-memory)

- No SSE/pub-sub infra exists; an in-memory broker breaks under restarts and
  multi-worker; the DB queue works everywhere uvicorn runs and is trivially
  testable with `TestClient` (house style).
- Latency is solved by **long-poll**: `GET` holds up to N seconds and
  returns the instant an event lands (async endpoint, 200 ms re-check loop
  on the DB — trivial read load at kiosk scale).
- **Consume-on-read**: the poll that returns events marks them consumed in
  the same transaction — two open tabs can never double-add to a cart
  (the second tab sees an empty batch).

### 2.2 Data model (two new tables; `create_all` covers them — no column
migrations needed; indexes ride along on the new tables)

```python
class ScannerTerminal(SQLModel, table=True):      # __tablename__ = "scanner_terminals"
    id: int | None = Field(primary_key=True)
    name: str = Field(unique=True)                # "counter-1"
    token_hash: str = Field(unique=True)          # SHA-256, like AuthToken.token
    created_at: datetime
    last_seen_at: datetime | None                 # daemon heartbeat

class ScanEvent(SQLModel, table=True):            # __tablename__ = "scan_events"
    id: int | None = Field(primary_key=True)      # monotonic = ordering
    terminal_id: int = Field(index=True)
    code: str
    kind: str                                     # "badge" | "barcode"
    created_at: datetime
    consumed_at: datetime | None = Field(index=True)
```

Housekeeping (the `_issue_token` purge pattern): on every events POST/GET,
delete rows `consumed_at IS NOT NULL` OR `created_at < now - 2 min`.

**Terminal token**: `secrets.token_urlsafe(32)` (43 chars), shown exactly
once at create/rotate, stored hashed via `security.token_hash` — the badge
doctrine applied to the terminal credential. One active token per terminal;
daemon and that kiosk's browser hold the same token (same trust domain, same
exposure class as the session token already in `localStorage`). Rotation
invalidates both → re-pair both; that's the recovery path for a leaked token.

### 2.3 Endpoints — new `app/routers/scanner.py`, prefix `/api/scanner`

Terminal auth dependency `get_terminal`: `Authorization: Bearer <token>` →
hash → `ScannerTerminal` or 401. Distinct credential type from user tokens,
uniform header. The SPA calls these **logged out** (badge login must work
from the login screen), so user auth is *not* required on the event
endpoints — the terminal token is the counter-session binding.

| Endpoint | Auth | Contract |
|---|---|---|
| `POST /api/scanner/terminals` `{name}` | staff | 201 → `{id, name, token}` (token shown once) |
| `GET /api/scanner/terminals` | staff | list `{id, name, online, last_seen_at}` (no token) |
| `POST /api/scanner/terminals/{id}/rotate` | staff | 200 → `{token}` (old token dies immediately) |
| `DELETE /api/scanner/terminals/{id}` | staff | 204; deletes its queued events |
| `POST /api/scanner/heartbeat` | terminal | 204; sets `last_seen_at` (daemon beats every 10 s) |
| `POST /api/scanner/events` `{code, kind}` | terminal | 201 → `{id}`. **Server re-derives `kind` via `app/scanshape.classify_scan`** (never trust the client's classification — the scan_requests doctrine); unshaped → 422; a `kind` mismatch is corrected, not an error |
| `GET /api/scanner/events?timeout=15` | terminal | long-poll ≤ `min(timeout,30)` s → `{events: [{id, code, kind, created_at}], daemon_online: bool}`; returned events are marked consumed in the same commit; `timeout=0` = instant check (also the pairing-verify call) |

### 2.4 The pairing flow (how the kiosk binds)

1. Staff (in the SPA admin area, §3 D-task) creates terminal "counter-1" →
   token shown once.
2. Operator puts the token in the daemon's config
   (`--terminal-token` / `HCRM_TERMINAL_TOKEN` / config file; never logged).
3. On the kiosk browser login screen: "Pair scanner" field → paste token →
   SPA verifies with `GET /api/scanner/events?timeout=0` → on 200 stores it
   in `localStorage["hcrm_terminal_token"]` and starts the poll loop.
4. From then on the loop runs regardless of login state; badge scans arrive
   as events → `enqueueScan(code, "badge")` → existing `badgeLogin`.

### 2.5 SPA poll loop

- Start on app mount if a terminal token is stored (works logged out);
  restart after errors with backoff (2 s → 15 s cap); a 401 clears the
  stored token + pill ("scanner unpaired").
- Each batch: `for (ev of events) this.enqueueScan(ev.code, ev.kind);` —
  the unchanged bus. `enqueueScan` already masks badge payloads in the flash
  pill; the existing queue/drain serialises them.
- Status pill from `daemon_online`.
- Unpair (Account page): clear token, stop loop.

### 2.6 Security notes (accepted risks, stated)

- `scan_events` rows hold raw badge payloads for ≤ 2 minutes (consume or
  TTL). Inherent: the SPA needs the raw payload to `qr-login`. Bounded by
  consume-on-read + aggressive prune + the DB already stores only credential
  hashes elsewhere. Daemon logs use `redact_code` imported from
  `scripts/scanner_bridge.py` — badge payloads never hit journald.
- The events endpoints are terminal-authed, not user-authed: anyone holding
  the terminal token reads that counter's scan stream. That's the binding by
  construction; rotation is the mitigation; and it is strictly narrower than
  a user session token.
- No localhost-only shortcut: `run.sh` can bind `0.0.0.0`
  (`HCRM_ALLOW_INSECURE_BIND`), so the token is required on every bind.

## 3. Ordered, reviewable task breakdown

Each batch is independently mergeable and reviewable; B does not depend on
A/C code, C does not depend on B (daemon posts to a URL; tested against
fakes), D needs B's contract only.

### Batch A — shared classification core

**A1. `app/scanshape.py` + parity vectors.**
- Files: `app/scanshape.py` (new, ~40 lines), `tests/fixtures/scan_shapes.json`
  (new: badges both forms, GTINs, opaque SKUs, digit-free words, short
  garbage, 43-char non-token edge cases), `tests/test_scanshape.py` (new),
  `tests/js/scan_shapes.test.mjs` (new — same JSON against
  `HcrmScanCapture.scanShape`; CI's `node --test tests/js/*.test.mjs` picks
  it up automatically).
- No server wiring; pure module + cross-language parity.

### Batch B — backend channel

**B1. Models + schemas.**
- Files: `app/models.py` (`ScannerTerminal`, `ScanEvent`), `app/schemas.py`
  (`TerminalCreateIn`, `TerminalOut`, `TerminalPairOut`, `ScanEventIn`,
  `ScanEventOut`, `ScanEventBatchOut`).
- Tests: extend `tests/test_migrations.py`-style boot on a pre-existing DB
  to assert the new tables appear (create_all path).

**B2. `app/routers/scanner.py` + wiring.**
- Files: `app/routers/scanner.py` (new), `app/deps.py` (`get_terminal`),
  `app/main.py` (include router).
- Tests: `tests/test_scanner_channel.py` (new) — auth matrix (no token /
  bad token / user-token-is-not-a-terminal-token → 401); staff-only on
  terminal CRUD (member → 403); create→token-once→rotate→old-token-401;
  post→poll round trip; consume-on-read (second poll empty, second *tab*
  semantics); server-side kind re-derivation (post `kind:"barcode"` with a
  badge payload → stored/returned as `"badge"`; unshaped → 422); TTL prune;
  heartbeat → `daemon_online` flips; long-poll with `timeout≈0.3` returns
  promptly and empty; delete terminal cascades its events.

### Batch C — daemon

**C1. Pure decode core.**
- Files: `scripts/scanner_daemon.py` (new; `EVDEV_TO_HID_USAGE`,
  `evdev_to_report`, `parse_input_event` (struct), `find_event_device`
  (sysfs, the `_hidraw_sysfs` pattern), `classify = app.scanshape.classify_scan`).
- Tests: `tests/test_scanner_daemon.py` (new) — scripted `(code, value)`
  streams fed through `evdev_to_report` → the **real** `ScanAssembler`:
  digits, shifted punctuation, a full `HCRM1:` badge payload, CR in the same
  "report" as the last char, autorepeat (value=2) ignored, release-only
  noise ignored, inter-scan shift state reset; struct parse for both
  24-byte and 16-byte formats; classification of assembled scans.

**C2. Grab loop + forwarding.**
- Same file: `grab_and_run(node, on_scan)` (open, `EVIOCGRAB`, read loop,
  ungrab-on-close), `post_event`/`heartbeat` (urllib, the `scanner_bridge`
  ApiError patterns, bounded timeout), spool (bounded FIFO JSONL, drain
  before new), discovery-retry loop, `--terminal-token/--api/--node/--vid/
  --pid`, `redact_code` for logs.
- Tests (same file's test module): spool ordering + cap + drop-oldest with a
  fake poster; poster maps HTTP/connection errors to spool, never raises
  (bridge doctrine); heartbeat thread lifecycle with a fake clock where
  possible. The ioctl/read loop itself stays hardware-gated, exactly like
  `run_hidraw`.
- Manual hardware checklist appended to `docs/opticon-hardware.md` (evtest
  node identification, grab stops typing into a terminal, unplug/replug
  re-grab, kill → typing resumes) — CI cannot cover this.

**C3. Permissions + docs.**
- Files: `scripts/udev/99-opticon-scanner.rules` (add the `input` line),
  `docs/opticon-hardware.md` (new §: the grab daemon — install, run,
  example systemd unit, fallback semantics §1.5), `README.md` one-liner
  pointer. No tests (ops artifacts); the scripts contract suite needs no
  change (no script registry — verify during implementation).

### Batch D — frontend

**D1. Pairing + poll loop + status.**
- Files: `static/app.js` (terminal token key, `startScannerEvents /
  stopScannerEvents / pollScannerEvents / pairTerminal / unpairTerminal`,
  batch → `enqueueScan`, pill state), `static/index.html` (login-screen
  "Pair scanner" block, header pill markup), `static/style.css` (pill).
- Tests: factor the batch handler pure (`scannerBatchStep`-style: dedupe by
  id, ordered, kind passthrough) into a tiny exportable function and add
  `tests/js/scanner_events.test.mjs`; extend
  `tests/test_frontend_contract.py` with pins: `hcrm_terminal_token` key,
  poll loop started on mount (not only after login), events routed through
  `enqueueScan` verbatim, badge masking preserved.

**D2. Staff terminal management UI.**
- Files: `static/app.js` + `static/index.html` — admin section: create
  terminal (token shown once, copy button), list with online status,
  rotate (with "re-pair both sides" warning), delete.
- Tests: `test_frontend_contract.py` pins for the staff-only gating
  (`isStaff`) and one-time token display; API behaviour already covered in
  B2.

### Suggested merge order

A1 → B1 → B2 → C1 → C2 → C3 → D1 → D2. (C and B are swappable; D needs B's
contract merged to be exercisable end-to-end.)

## 4. Risks / open questions

1. **Multi-interface enumeration (verify on hardware first).** If
   `065a:a001` presents more than one input interface (keyboard +
   consumer-control), discovery must pick the keyboard node. Mitigation:
   match on name + log all candidates; worst case a `--node` override
   exists. *Action: run `evtest`/`cat /proc/bus/input/devices` on the dev
   machine before C1 is finalised.*
2. **Consume-on-read loss window.** A tab that fetched events and dies
   before `enqueueScan` finishes loses them (sub-second). Accepted; a
   two-phase ack protocol is the deferred upgrade if it ever bites.
3. **WSL2 event nodes.** Assumed present under usbipd (the HID personality
   already yields hidraw there); confirm `/dev/input/eventN` appears and
   the udev rule fires without a seat. Hardware checklist item.
4. **Two daemons, one terminal id** (misconfiguration): last heartbeat
   wins, events interleave into one queue. Documented as one terminal =
   one kiosk + one daemon; rotation is the cleanup. No interlock proposed.
5. **32-bit struct format** is handled but untestable on the x86_64 dev
   machine; the parse function takes the format as data so both are
   unit-tested with canned bytes.
6. **Long-poll vs. SQLite writer contention**: polls are short reads every
   200 ms per kiosk; `busy_timeout` already on every connection. Non-issue
   at counter scale, noted for completeness.
7. **Open question — auto-pair via QR?** A pairing QR (terminal token as
   a badge-style code, scanned by the daemonless wedge into the pair field)
   would make pairing typing-free. Nice-to-have; not in this round's scope.
8. **Open question — should an unpaired kiosk warn?** Currently silent (no
   pill). If operators forget to pair, the scanner is a wedge again and
   round-3 handles it only when the browser is focused — exactly the round-4
   problem. Propose a dismissible login-screen hint when no terminal token
   is stored; cheap, in D1.
