# Round 4 plan (qwen) — OS-level scanner grab + scan forwarding

Task: `docs/plans/round4-task.md`. Scope: an OS-level daemon that **grabs** the
M-10's evdev node (so it stops typing into any window), decodes scans as data,
classifies badge vs barcode, and forwards them to the SPA so the **existing**
flows run — plus the daemon→SPA channel and the counter-session binding
(the deferred "Phase B").

This is a plan only: no code was written, nothing was committed. Peers'
`r4plan-glm.md` / `r4plan-kimi.md` were **not** read (independent proposals).

---

## 0. Ground truth — measured on this host, today

Reproduce with the commands in each row. These facts drive several design
decisions below; where a fact contradicts an assumption in the task text or in
`SCANNER-AGENT-SYNC.md`, the measurement wins.

| # | Fact | Command / evidence |
|---|---|---|
| M1 | **The scanner is not attached right now.** No `065a` on any bus. | `lsusb` → root hubs, Genesys hub, USI `10ab:9309`, Chicony camera only |
| M2 | **Event-node numbers are not stable.** `event14` was the scanner on 2026-10-10 (`SCANNER-AGENT-SYNC.md`); today `event14` is *HD-Audio Generic Headphone*. | `for e in /dev/input/event*; do cat /sys/class/input/$(basename $e)/device/name; done` |
| M3 | **`/dev/input/by-id/` does not exist on this host.** Discovery cannot depend on it (contrary to the sync doc's `usb-Opticon…-event-kbd → event14` line, which was true when the device was attached). | `ls /dev/input/by-id/` → `No such file or directory` (`by-path` does exist) |
| M4 | **evdev nodes are `crw-rw---- root:input`, and this user is not in `input`** (it is in `sudo`, `plugdev`, `dialout`). | `ls -l /dev/input/`, `id` |
| M5 | **The installed udev rule does not cover the `input` subsystem** — it covers `tty`, `hidraw`, `usb` only. So the repo's existing one-time sudo install is *not* sufficient for a grab. | `/etc/udev/rules.d/99-opticon-scanner.rules` (dated Oct 10 23:02) |
| M6 | **`/dev/hidraw0` is `root:root 0600`** and is the Synaptics touchpad (`06CB:CE67`), not a scanner. | `ls -l /dev/hidraw*` |
| M7 | **`EVIOCGRAB == 0x40044590`** here (`_IOW('E', 0x90, int)`), and `struct input_event` is **24 bytes** (`llHHi`) on this 64-bit kernel `6.8.0-146-generic`. | computed with the `_IOC` formula; `struct.calcsize("llHHi")` |
| M8 | **No `evdev` package installed**, and the repo's device scripts are stdlib-only by doctrine (`scanner_hid.py`: "Stdlib only"). Adding a C-extension dependency for ~60 lines of ioctl is not justified. | `uv run python -c "import evdev"` → ModuleNotFoundError |
| M9 | **A server-side long-poll hold breaks the browser gate.** With `--virtual-time-budget=6000`, Chrome waits for the in-flight request: a single 20 s hold → the run took **20.5 s**; a *loop* of 2 s holds → **never dumped, hit my 90 s timeout** (`ui_check._run` would hit its own `timeout=180` and report `{"error": "timeout"}` = red gate). | three throwaway Chrome runs with `ui_check.BROWSER_FLAGS` (details in §2.4) |
| M10 | **A client-paced short-poll loop is gate-safe**: fast responses + `setTimeout` pacing dumped in **0.54 s**, 0 console records. | same harness, `/poll` answering immediately |
| M11 | **An aborted in-flight fetch at teardown logs nothing** (0 console records), and **a 404'd fetch logs no `Failed to load resource` record** on Chrome 155.0.8059.39 — so the feed cannot fail the gate's `FAIL_PATTERNS` on the degraded path. | same harness, `/missing-endpoint` → 404 |

Repo facts that constrain the design:

- The SPA already has **one** scan bus: `enqueueScan(code, kind)` →
  `drainScanQueue()` → `handleBadgeScan` / `handleBarcodeScan`
  (`static/app.js:1410`+). Everything round 3 built (login, 60 s logoff,
  save-then-switch, cart/scan, intake modal, anonymous staging) hangs off it.
- The badge-acceptance rule already exists server-side
  (`app/routers/auth.py::_badge_from_scanned`: `HCRM1:` **or** bare 43-char) and
  is mirrored in JS (`static/scancapture.js::scanShape`). Two mirrors, no pin.
- `POST /api/cart/scan` re-validates and rejects badges (400), misreads (422)
  and unknowns (404 + intake payload). It needs a **member** token — which the
  daemon does not and should not have.
- Tests are `TestClient`-only, per-test fresh DB, no streaming anywhere; CI runs
  pytest forward **and** in reverse node-id order, `node --test tests/js/*.test.mjs`,
  `ui_check.py --self-host`, and requires a clean `git status`.
- `scripts/run.sh` starts **one** uvicorn worker (no `--workers`), binds loopback
  by default, and refuses a network bind while dev-admin is on.

---

## 1. Architecture decision, in ten lines

1. **The daemon is a wire, not a brain.** It grabs, decodes, classifies and
   POSTs. It never resolves items, never adjusts stock, never mints a member
   session, never performs a login. (Contrast `scripts/scanner_bridge.py`, which
   is a *headless back-office* tool and therefore does its own lookups — the kiosk
   path must not fork the SPA's flows.)
2. **The SPA stays the single owner of every flow.** The feed's only entry point
   is the existing `enqueueScan(code, kind)`. Zero duplicated flow logic.
3. **The channel is a server-side, per-terminal, TTL-bounded queue in SQLite**,
   written by the daemon over HTTP and read by the SPA with a **client-paced
   short poll** (M9/M10: no server-side hold).
4. **The counter-session binding is a terminal capability pair**: a
   browser-generated `terminal_id` + `queue_token` (both in `localStorage`),
   bound to the daemon once by a **short-lived human-entered pairing code**.
   The daemon then holds a device-scoped `daemon_token` — not a member account,
   not a password.
5. **One consumer per terminal** via a per-tab lease (`sessionStorage`), so two
   browser tabs on one kiosk cannot both add the same scan to two carts.
6. **No new Python or JS dependency.** `fcntl.ioctl` + `struct` + `select`
   (stdlib), mirroring `scanner_hid.py`.
7. **Fallback is free**: no daemon ⇒ no grab ⇒ the kernel keeps wedging keys into
   the focused window ⇒ round 3's `scancapture.js` path is unchanged and still
   installed. The SPA shows which path is live.

### Rejected alternatives (with reasons)

| Alternative | Why not |
|---|---|
| Read `/dev/hidraw1` only (no grab) | The task is right: hidraw and evdev are parallel streams — reading hidraw does **not** stop typing. It also needs the same missing permission work. |
| `evdev` PyPI package | M8: a C-extension dependency for ~60 lines of stdlib ioctl, against the repo's stdlib-first device-script doctrine. Reconsider only if `input_event` portability ever bites (it is Linux-only by definition). |
| SSE (`EventSource`) | A never-completing response is the worst case for M9 (the gate would hang forever), the whole test suite is `TestClient`-based (no streaming precedent), and reconnect/resume needs the same cursor code anyway. |
| Server-side long-poll hold | M9: hangs/slows `ui_check`. Kept out of v1 **and pinned out by a contract test** (§7 T6) so nobody "optimises" it back into a red gate. |
| Daemon → SPA over a local WebSocket | CSP is `connect-src 'self'` (`app/main.py`); another port is not `'self'`, so this needs a CSP relaxation — the one header the repo treats as a security boundary. |
| Daemon writes rows into SQLite directly (same host) | Couples a device process to the schema, needs DB-path sharing, breaks the remote/back-office deployment, and cannot be tested with `TestClient`. |
| Daemon authenticates as a staff account (bridge-style `--email/--password`) | Puts a human credential in a systemd unit, and a staff session is far more privilege than "inject scans for terminal X". Device-scoped token instead (§3). |
| Server performs the flows (login/switch/logoff) on ingest | Forks the save-then-switch and 60 s-overlay logic into a second implementation with a second set of tests. Only worth it for a headless kiosk — that is variant **1b** below, deferred. |

**Variant 1b (documented, not built):** the server resolves a badge on ingest and
queues `{kind:"badge_adopt", adopt_token}`; the SPA calls
`POST /api/scanner/terminals/{id}/adopt` → `AuthOut`. Benefit: badge payloads
never sit in `scan_events`. Cost: a device token can then mint member sessions,
plus a new endpoint and a second switch path. The schema below already
accommodates it (`kind` is an open string) — coordinator decision Q1.

---

## 2. The channel: `GET/POST /api/scanner/*`

New router `app/routers/scanner.py`, prefix `/api/scanner`, tags `["scanner"]`,
wired in `app/main.py` next to the existing `include_router` block. It inherits
the security-headers middleware and the 500 handler for free (both are
path-agnostic).

### 2.1 Endpoints

| Method | Path | Credential | Purpose |
|---|---|---|---|
| `POST` | `/api/scanner/terminals` | none (capability in body) | register **and** heartbeat a terminal; idempotent |
| `POST` | `/api/scanner/terminals/pairing-code` | `X-Queue-Token` | mint a single-use 5-min pairing code (shown on the kiosk screen) |
| `POST` | `/api/scanner/pair` | pairing `code` | daemon binds to a terminal → `{terminal_id, daemon_token}` |
| `POST` | `/api/scanner/scans` | `Authorization: Bearer <daemon_token>` | ingest 1–32 events (idempotent per `event_id`) |
| `GET` | `/api/scanner/scans?after=&limit=` | `X-Scanner-Terminal` + `X-Queue-Token` + `X-Scanner-Session` | the SPA's poll (no hold, ever) |
| `POST` | `/api/scanner/terminals/unpair` | `X-Queue-Token` | revoke the daemon binding (kills `daemon_token`) |

Header names are deliberate: the SPA's `api()` wrapper owns
`Authorization: Bearer <member token>` and runs a global 401 → logout cleanup.
The feed's credentials ride **different headers**, and the feed uses its own
`feedFetch()` (not `api()`), so a revoked queue token can never log the kiosk's
member out. Pin this in a test.

Shapes (Pydantic models in `app/schemas.py`, house style: `status` discriminator
in every error envelope, never a bare detail string):

```
POST /api/scanner/terminals        {"terminal_id": "uuid", "queue_token": "43ch", "label": "Counter 1"}
  200 {"terminal_id", "label", "paired": bool,
       "daemon": {"id","label","device","last_seen","online"} | null,
       "lease":  {"session_id","expires_at"} | null}
  409 {"status":"conflict","message":"terminal_id already registered with a
       different queue token — regenerate both"}      # never echo the token

POST /api/scanner/terminals/pairing-code   (headers: terminal + queue token)
  201 {"code":"473920","expires_in":300}
  403 {"status":"staff_required", …}                  # only if hardened (Q2)

POST /api/scanner/pair             {"code":"473920","label":"kiosk-a","device":"065a:a001 /dev/input/event7"}
  200 {"terminal_id","daemon_token","label"}          # token shown ONCE
  404 {"status":"bad_code"} / 410 {"status":"expired"} / 409 {"status":"consumed"}

POST /api/scanner/scans            {"events":[{"event_id","code","kind","scanned_at","source"}]}
  201 {"accepted":n,"duplicates":m,"reclassified":k,"rejected":[{"event_id","reason"}],"queue_depth":d}
  401 {"status":"unpaired"}                            # daemon must re-pair, loudly

GET  /api/scanner/scans?after=1234&limit=50
  200 {"events":[{"seq","event_id","kind","code","scanned_at","source"}],
       "latest_seq":1250, "expired":0, "overflow":0,
       "daemon":{"online":bool,"label","device","last_seen"}|null,
       "lease":{"state":"granted"|"held"|"suspended","session_id","expires_at"}}
```

`daemon.online` = `last_seen` within `SCANNER_ONLINE_SECONDS` (default 30 s; the
daemon heartbeats every 10 s). The SPA renders that as the status pill.

### 2.2 Tables (`app/models.py`)

```
scanner_terminals                      scan_events
  id            TEXT PK (client uuid)    id          INTEGER PK AUTOINCREMENT  ← the cursor ("seq")
  queue_token_hash TEXT NOT NULL         terminal_id TEXT NOT NULL, index
  label         TEXT                     event_id    TEXT NOT NULL             ← daemon uuid
  created_at / last_seen DATETIME        kind        TEXT  ("badge"|"barcode")
  daemon_id / daemon_token_hash TEXT     code        TEXT (max_length=128)
  daemon_label / daemon_device TEXT      source      TEXT default "evdev"
  daemon_paired_at / daemon_last_seen    scanned_at / created_at / expires_at DATETIME
  pairing_code_hash TEXT                 UniqueConstraint(terminal_id, event_id)
  pairing_code_expires_at DATETIME       Index (terminal_id, id)
  pairing_failures INTEGER default 0
  lease_session TEXT / lease_expires_at DATETIME
  dropped_total INTEGER default 0
```

Both are **new tables**, so `create_all()` makes them and **no
`COLUMN_MIGRATIONS` / `INDEX_MIGRATIONS` entries are needed** — say so in the PR
description, because the append-only migration doctrine makes reviewers look for
one. Tokens/hashes only: `queue_token`, `daemon_token` and the pairing code are
stored as SHA-256 (`app.security.token_hash`), exactly like session tokens and
badge payloads.

Queue semantics:

- **Cursor = `scan_events.id`.** Global autoincrement, so per-terminal it is
  strictly increasing; `after=` filters. The SPA persists its cursor in
  `localStorage` **on receipt, before processing** ⇒ *at most once*: a tab that
  dies mid-flow loses a scan (re-scannable) rather than double-adding a line
  (a data error). Documented tradeoff.
- **TTL**: `expires_at = created_at + HCRM_SCANNER_EVENT_TTL_SECONDS` (default
  **600**). Reaped lazily on every poll *and* ingest for that terminal (the
  `cart._cart_out` orphan-reaping precedent). A scan nobody consumed must never
  replay into a later member's cart — that is invariant I3.
- **Cap**: `HCRM_SCANNER_QUEUE_MAX` (default **200**) per terminal; over the cap
  the oldest rows are deleted and counted (`overflow`, `dropped_total`) — the
  poll reports it so the kiosk can say "3 scans expired undelivered".
- **Idempotency**: `UniqueConstraint(terminal_id, event_id)`; a duplicate INSERT
  is caught and reported as `duplicates`, so a daemon retry after a timeout
  cannot double-add.
- **Server re-classifies every event** (`app/scanshapes.classify_scan`) and
  stores *its* verdict, reporting `reclassified` — the `scan_requests` doctrine:
  never trust the client's (here: the daemon's) classification. An event that
  classifies as neither is rejected with a reason (never queued: the JS gate's
  "not scan-shaped ⇒ no server call" rule, moved server-side).

### 2.3 Latency budget

decode (≤5 ms) → `POST /scans` (~5 ms loopback) → SPA poll (0–400 ms, average
~200 ms) → `enqueueScan`. **Worst case ≈ 0.5 s, typical ≈ 0.2 s**, versus
"instant" for the wedge path. That is the price of M9/M10 (no server hold); it
is invisible for a badge login and acceptable for a cart add. If the coordinator
wants sub-50 ms, the only gate-safe route is the `?scanfeed=off` flag for
`ui_check` plus a real long-poll — recorded as Q8, **not** recommended (it makes
the browser gate blind to the feed).

### 2.4 Why no server-side hold (the measurement, so nobody re-litigates it)

`ui_check.BROWSER_FLAGS` = `--headless=new --enable-logging=stderr
--virtual-time-budget=6000 --dump-dom`. Three throwaway pages (served from
`/tmp`, nothing added to the repo):

| Page | Result |
|---|---|
| one `fetch` to a 20 s-hold endpoint | Chrome exited at **20.5 s** (it waits for the in-flight request), 0 console records |
| a loop of 2 s holds, re-polling on each response | **never dumped** — killed at 90 s. `ui_check._run` has `timeout=180` and reports `{"error":"timeout"}` ⇒ red gate on every SPA route |
| fast responses + `setTimeout(poll, 1000)` pacing | dumped in **0.54 s**, 0 console records |
| `fetch` to a 404 | 0 console records on Chrome 155 (so the degraded path cannot trip `FAIL_PATTERNS`) |

Virtual time cannot advance past a pending request; a poll loop therefore keeps
the page permanently non-idle. **Rule: the feed paces itself with client timers
and every request completes fast.** T6 adds a contract test that the SPA never
sends `wait_ms`/`hold` and that the endpoint ignores unknown pacing params.

---

## 3. Counter-session binding (the deferred "Phase B")

### 3.1 Identity

- `terminal_id` — browser-generated `crypto.randomUUID()`, persisted in
  `localStorage["hcrm_terminal_id"]`. **This is the counter/kiosk identity**; it
  survives reloads and is per browser profile (one profile per kiosk = one
  terminal). It is *not* a secret (it is a routing key).
- `queue_token` — browser-generated `crypto.randomUUID()` (or 43-char
  `token_urlsafe`-shaped string), persisted in `localStorage["hcrm_queue_token"]`.
  **This is the read capability** for the terminal's queue. Server stores only
  its hash. Losing `localStorage` ⇒ regenerate both ⇒ a new terminal row; the
  old one is reaped by TTL/last_seen.
- `daemon_token` — server-issued at pairing, stored hashed, held by the daemon in
  `${XDG_CONFIG_HOME:-~/.config}/hcrm/scanner-daemon.json` (mode **0600**) or
  `HCRM_SCANNER_TOKEN` for systemd. **Write capability** for that terminal only.
  Revocable from the kiosk screen (`unpair`).
- `session_id` — per **tab** `crypto.randomUUID()` in `sessionStorage`, the lease
  holder identity (§3.3).

### 3.2 Pairing (once per install, human-mediated)

```
kiosk SPA (login screen, header chip → "Scanner" panel)
  1. POST /api/scanner/terminals            # register terminal_id + queue_token
  2. POST /api/scanner/terminals/pairing-code
  3. render:  "Scanner daemon pairing code: 473920  (expires in 5:00)"
operator, on the same box:
  4. uv run python scripts/scanner_daemon.py --pair
     → prompts "Pairing code:" (getpass-style, no argv/history exposure)
     → POST /api/scanner/pair {code,label,device}
     → writes the 0600 config, grabs the device, starts forwarding
  5. SPA panel flips to "daemon online — /dev/input/event7 (065a:a001)";
     the header pill shows data-scanner-feed="live"
```

Single-use code, 5-minute TTL, `pairing_failures` locks the terminal's pairing
after 5 wrong codes (clears on a new code). Threat model, stated plainly:

- The code is **shoulder-surfable** for its 5-minute life. What an attacker who
  steals it can do: bind their own fake daemon to this terminal and inject
  scans. Injected *barcodes* add items to whoever's cart is open (annoyance,
  recoverable); injected *badges* are useless without a real badge payload.
  They cannot read the real scanner's events (those go to the terminal's queue,
  readable only with the `queue_token`, which is never displayed).
- Anonymous registration cannot hijack an existing terminal: `terminal_id` +
  a *different* `queue_token` is a 409, and polling requires the matching token.
- Default deployment is loopback-only (`scripts/run.sh`), and a network bind is
  already refused while dev-admin is on.
- Hardening switch (Q2): `HCRM_SCANNER_PAIRING_REQUIRES_STAFF=1` makes step 2
  require a staff/admin session (`require_staff`), i.e. pairing happens from the
  Admin view instead of the login screen. Default off (a kiosk must be pairable
  before anyone can log in — that is the whole badge-login bootstrap).

### 3.3 One consumer per terminal (the two-tab problem)

With cursor-per-consumer, two tabs on one kiosk would each get every event and
each `POST /api/cart/scan` ⇒ **double lines**. Fix: a **lease**.

- Poll carries `X-Scanner-Session`. The first session to poll an unleased (or
  expired-lease) terminal gets `lease.state = "granted"` for
  `SCANNER_LEASE_SECONDS` (default 30), renewed by each poll.
- Any other session gets `lease.state = "suspended"` and **zero events**; the SPA
  renders "Another tab is driving the scanner" and keeps its cursor frozen.
- The lease is a liveness mechanism, not a security one (the queue token is).

### 3.4 What the SPA does with an event (requirement 4 — reuse, verbatim)

`scanfeed` hands `(code, kind)` to `enqueueScan`, which already routes:

| Situation | Existing function | Server calls (unchanged) |
|---|---|---|
| badge, logged out | `badgeLogin(code)` → `afterLogin` | `POST /api/auth/qr-login`, then cart load + staged-scan replay + greeting |
| badge, logged in, **same** user | `armLogoff()` | `POST /api/auth/qr-identify` → `POST /api/auth/logoff-schedule` (60 s overlay, server deadline) |
| badge, logged in, **other** user | `switchAccount(code)` | `flushCart` (`PUT /api/cart`) → `qr-login` → `logoff-cancel` → `saveSession` → `logout(old)` → `loadCart` |
| barcode, logged in | `handleBarcodeScan` | `POST /api/cart/scan` → 200 add / 422 misread / 404 unknown → intake modal → `POST /api/scan-requests` |
| barcode, logged out | `stageScan` | none (localStorage staging, replayed at next login) |

So requirement 5's "how does the SPA reflect cart/login/logoff changes" has a
short answer: **it does not reflect them, it performs them.** There is one writer
per decision (the SPA), one input source added (the feed). The server-side cart
plus the token in `localStorage` already make a reload restore everything.

---

## 4. EVIOCGRAB: mechanics, permissions, lifecycle

### 4.1 Mechanics (stdlib)

```python
IOC_WRITE = 1
def _IOC(direction, typ, nr, size): return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr
EVIOCGRAB = _IOC(IOC_WRITE, "E", 0x90, 4)      # == 0x40044590 on Linux (M7; pinned by a test)

fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
fcntl.ioctl(fd, EVIOCGRAB, 1)     # exclusive: the kernel now delivers this
                                  # device's events to us ONLY
…read loop (select + os.read, 24-byte input_event records)…
fcntl.ioctl(fd, EVIOCGRAB, 0); os.close(fd)
```

- The `EVIOCGRAB` path in `drivers/input/evdev.c` has **no write-permission
  check** (it goes straight to `evdev_grab_client`), so `O_RDONLY` should
  suffice; T3's hardware step confirms it empirically and falls back to `O_RDWR`
  if a kernel disagrees. Permission failures surface at `open()` (M4).
- `O_NONBLOCK` + `select(timeout)` is what makes the loop interruptible for
  heartbeats, shutdown and hotplug checks. Reads must handle partial buffers
  (keep the remainder; `EVENT_SIZE = struct.calcsize("llHHi")` = 24, validated at
  import so a surprising platform fails loudly instead of mis-decoding).
- **Exclusive grab ⇒ the display server gets nothing from this device.** That is
  the requirement ("stops typing into whatever window has focus"), and it works
  below X11 *and* Wayland because it is a kernel-level redirection.
- **Do not grab hidraw.** hidraw is a parallel stream (task text, confirmed by
  `scanner_hid.py`'s docstring); touching it buys nothing and needs its own
  permission fix (M6).

### 4.2 Wrong-device guard (the catastrophic failure mode)

Grabbing a *human* keyboard silently disables typing system-wide. Guards, all
required before `ioctl(…, 1)`:

1. VID/PID match `065a:a001` (defaults; `--vid/--pid` override), resolved by
   walking sysfs parents for `HID_ID`/`idVendor` — reusing `scanner_hid.py`'s
   `:([0-9A-Fa-f]{8}):([0-9A-Fa-f]{8})$` regex (extract it to a shared helper or
   import it; do **not** copy it).
2. The device must advertise `EV_KEY` with `KEY_A`..`KEY_Z` (read
   `EVIOCGBIT(EV_KEY)` — or, simpler and sufficient, require `HID_NAME`/`name`
   to contain "Opticon" or the configured `--name-substring`).
3. Never grab more than the matching nodes; a scanner may expose a second
   consumer-control node — grab only nodes that pass (2), log the rest.
4. `--allow-any NODE` is the explicit opt-out (the `scanner_bridge.py --any-port`
   doctrine: refuse to guess, explain what *is* attached, make the operator opt
   in). The startup log always prints the grabbed device's name + node.

### 4.3 Permissions (measured gap: M4 + M5)

Three supported routes, in recommended order:

1. **udev rule (repo precedent).** Add one line to
   `scripts/udev/99-opticon-scanner.rules`, keeping the vendor-wide single-file
   doctrine:
   ```
   SUBSYSTEM=="input",  ATTRS{idVendor}=="065a", MODE="0660", GROUP="plugdev"
   ```
   (`ATTRS{}` matches any parent, which is how the existing `hidraw` line works;
   `ENV{ID_INPUT_KEYBOARD}=="1"` can be added to narrow it, and is available
   because `60-input-id.rules` runs before `99-*`.) Install + reload + **re-plug**
   (or `sudo udevadm trigger --subsystem-match=input --action=add`) — the rule
   only applies to devices (re)added afterwards.
2. **`sudo usermod -aG input $USER`** + re-login. One line, no repo change, but
   it grants *all* input devices (including the real keyboard) — document the
   difference.
3. **systemd system unit as root** (`scripts/systemd/hcrm-scanner-daemon.service`,
   `Restart=always`, `SupplementaryGroups=input`, `HCRM_*` env). No udev change
   needed; recommended for a fixed kiosk. Ship the unit file + install steps;
   do not enable it from any script.

The daemon must turn `PermissionError` at `open()` into one actionable message
naming all three (the `scanner_hid.py` PermissionError precedent), never a
traceback.

### 4.4 Lifecycle & hotplug

| Event | Behaviour |
|---|---|
| normal stop (`SIGINT`/`SIGTERM`/`--count`) | `ioctl(…, 0)` + `close()` in a `finally`; **typing resumes** |
| crash / `kill -9` | the kernel releases the grab when the fd closes ⇒ **typing resumes automatically**. This is the answer to the task's lifecycle question; the hardware checklist verifies it with `kill -9` + a wedge test |
| unplug (`ENODEV`/short read) | release, log, re-discover with backoff 1→10 s, **never exit** (M2: the node number changes, so re-discovery is by VID/PID, never a remembered `eventN`; M3: `by-id` may not exist — use it opportunistically only) |
| `EBUSY` on grab | another client holds it (a second daemon, `input-remapper`, `evtest`): single-instance lockfile (`/run/user/$UID/hcrm-scanner-daemon.lock` or `XDG_RUNTIME_DIR`) checked first, then a message naming the likely culprits |
| server unreachable | keep the grab (typing into a dead app helps nobody), buffer up to `--buffer-max` (default 200) events, retry with backoff, log loudly; `--release-on-server-down=SECONDS` is an open question (Q6) |
| xHCI controller dies again (`docs/m10-unreachable-2026-10-10.md`) | same path as unplug: backoff + re-discover forever, one log line per attempt (rate-limited), no exit |

Also: `--diagnose` (print raw `type/code/value` for 10 s and exit) for the "it
grabs but nothing decodes" case, and `--dry-run` (decode + classify + print
masked, no HTTP) for the hardware pass. `--no-grab` exists for shadow debugging
and **must** print a warning that the wedge path is then live too (double
delivery if the browser is focused).

---

## 5. Decode: reuse `parse_keyboard_report`, don't re-implement it

evdev gives Linux **keycodes** (`KEY_A = 30`), not HID usages (`a = 4`). The
reuse bridge is a pure table `keycode → HID usage`, then feed a synthetic
8-byte boot report into the **existing, already-tested** `ScanAssembler`:

```python
KEYCODE_TO_USAGE = {           # data, pinned by tests
    30: 4,  … 56: 30,          # KEY_A..KEY_Z  -> usages 4..29
    2: 30, 3: 31, … 10: 38, 11: 39,   # KEY_1..KEY_9, KEY_0 -> 30..39
    28: 40, 96: 40,            # KEY_ENTER and KEY_KPENTER both terminate
    57: 44, 12: 45, 13: 46, 26: 47, 27: 48, 43: 49,
    39: 51, 40: 52, 41: 53, 51: 54, 52: 55, 53: 56,
}
MODIFIER_KEYS = {42: 0x02, 54: 0x20, 100: 0x04, 126: 0x08}   # L/R shift, L/R ctrl

report = bytes([modifier, 0x00, usage, 0, 0, 0, 0, 0])
scan = ScanAssembler().feed(report)     # scripts/scanner_hid.py — unchanged
```

Why this shape: `parse_keyboard_report` + `ScanAssembler` already carry the
shift table, rollover tolerance, the "text before terminator" fix and their own
test suite (`tests/test_scanner_hid.py`). A second char table would be a second
place for the UPC-E-vs-`!` class of bug to live.

Decoder rules:

- `EV_KEY` only; ignore `EV_SYN`/`EV_MSC`/`EV_LED`. `value == 1` = press,
  `0` = release, **`2` = auto-repeat → ignored** (a repeat would double a
  character; scanners don't repeat, a stuck key must not corrupt).
- Track shift press/release to build the modifier byte; a scan is decoded with
  **US-HID semantics regardless of the X/Wayland keyboard layout** — a genuine
  improvement over the wedge path, which is layout-dependent (a non-US kiosk
  layout mangles `!`/`-`/`:` today). Worth a README line and a checklist item.
- **No timing heuristic is needed**: with an exclusive grab, every key from this
  device is a scan key. The device identity *is* the classifier — this is why the
  daemon needs none of `scancapture.js`'s HOLD/ARMED/WARM machinery.
- Termination: usage 40 (Enter/CR). Plus two guards, because a truncated burst
  must never be delivered as a barcode:
  - `SCAN_IDLE_MS` (default **150**, env `HCRM_SCAN_IDLE_MS`): silence with a
    non-empty buffer ⇒ **discard** the partial and count it (a half GTIN would
    422 as a misread or, worse, exact-match nothing). `--terminator-timeout-ms`
    switches discard → flush for scanners configured *without* a CR suffix
    (docs/opticon-m10.md §7 configuration).
  - `MAX_SCAN_LEN` (default 128) ⇒ discard.
- Classification for `kind` comes from the shared module (§6), and the **server
  re-classifies anyway** (§2.2).

---

## 6. One classification rule, two languages, pinned

Today the rule exists twice with no pin: `app/routers/auth.py::_badge_from_scanned`
(+ private `_RAW_TOKEN_RE`) and `static/scancapture.js::scanShape`. Round 4 adds
a third consumer (the daemon), so extract:

**`app/scanshapes.py`** (new, stdlib):
`BADGE_FULL_RE`, `BADGE_RAW_RE`, `BARCODE_DIGITS_RE`, `BARCODE_OPAQUE_RE`,
`classify_scan(code) -> "badge" | "barcode" | None`, `is_badge(code) -> bool`,
`redact_badge(code) -> str` (moved from `scanner_bridge.redact_code`, which keeps
a re-export so `tests/test_scanner_bridge.py` is untouched),
`SCANSHAPE_JS = "static/scancapture.js"` for the parity test.
`auth.py::_badge_from_scanned` becomes a thin adapter over it (behaviour
identical: strip, full-or-raw, return the `HCRM1:`-prefixed form) — the
`app/scanlookup.py` "ONE table" precedent.

**Parity test** (`tests/test_scan_shape_parity.py`): read the four regex *source
strings* out of `static/scancapture.js` and assert they are literally equal to
the Python ones, plus a shared sample-code table classified identically by both
(the Node suite asserts the JS side). Same drift-pin pattern as
`tests/test_frontend_contract.py`'s `SCAN_DEFER_MS > SCAN_MAX_GAP_MS`.

---

## 7. Ordered task breakdown

PR boundaries are chosen so each is reviewable alone and CI-green on its own.
T1 is the unblocking contract; T2/T3 (daemon, offline) and T4 (API) can then run
in parallel; T5 needs T4; T6 needs T4 (contract) and can run parallel to T5.

```
T1 ──► T2 ──► T3 ─┐
  └──► T4 ────────┼──► T5 ──┐
        └─────────┴──► T6 ──┴──► T7
```

### T1 — shared scan shapes (backend, pure) — *small, do first*

**Files**: `app/scanshapes.py` (new), `app/routers/auth.py` (delegate),
`scripts/scanner_bridge.py` (re-export `redact_code`), `static/scancapture.js`
(comment pointing at the Python source of truth; **no behaviour change**).
**Tests**: `tests/test_scanshapes.py` (badge full/raw, barcode digits/opaque,
unshaped: empty, 3 chars, digit-free word, >64, whitespace-strip; `classify_scan`
None-cases; `redact_badge` masks both badge forms and passes barcodes through);
`tests/test_scan_shape_parity.py` (§6); existing `tests/test_auth.py` /
`test_qr.py` / `test_scanner_bridge.py` must pass unchanged (the refactor's real
gate). **Acceptance**: `qr-login` still accepts both badge forms; no regex is
defined in two languages without a pin.

### T2 — evdev decode core (daemon, offline) — *medium*

**Files**: `scripts/scanner_daemon.py` (new; module-level pure layer:
`EVENT_SIZE`, `EVIOCGRAB`, `KEYCODE_TO_USAGE`, `MODIFIER_KEYS`,
`parse_input_event(buf) -> (type, code, value) | None`,
`EvdevScanDecoder` (modifier state + `ScanAssembler` + idle/max-length guards),
`find_scanner_event_devices(sysfs_root="/sys")`, `classify`/`redact` via
`app.scanshapes`). Importable exactly like `scripts/scanner_hid.py`
(`from scripts.scanner_daemon import …` — namespace-package import, the pattern
`tests/test_scanner_hid.py` already uses).
**Tests**: `tests/test_scanner_daemon.py` — synthetic 24-byte `input_event`
streams (a `pack()` helper in the test): full GTIN + Enter ⇒ one scan;
`HCRM1:<43>` + Enter ⇒ classified `badge`; shift+1 ⇒ `!`; `KEY_KPENTER`
terminates; auto-repeat ignored; `EV_SYN`/`EV_MSC` ignored; an event split across
two reads reassembles; idle timeout discards a partial (and flushes it under
`--terminator-timeout-ms`); `MAX_SCAN_LEN` discards; keycode→usage pins
(`KEY_A=30→4`, `KEY_1=2→30`, `KEY_ENTER=28→40`, `KEY_SPACE=57→44`,
`KEY_MINUS=12→45`, `KEY_SLASH=53→56`); discovery against a **fake sysfs tree in
`tmp_path`** (one AT keyboard + one `065a:a001` with a `HID_ID` uevent) ⇒ only
the scanner matches, and a tree with no scanner yields `[]` with an actionable
message; `EVIOCGRAB == 0x40044590` and `EVENT_SIZE == 24` pinned (M7).
**Acceptance**: the decoder is 100 % offline-testable; no hardware, no root.

### T3 — grab + device lifecycle (daemon) — *medium*

**Files**: `scripts/scanner_daemon.py` (`ScannerDevice`: open/grab/read/release,
`GrabRunner`: discovery → grab → decode → release, hotplug re-discovery with
backoff, single-instance lockfile, signal handlers, `--diagnose`, `--dry-run`,
`--no-grab`, `--allow-any`, `--vid/--pid/--node`), `scripts/udev/99-opticon-scanner.rules`
(the `input` line, §4.3), `docs/opticon-hardware.md` §4 (document the new
subsystem + the `usermod -aG input` and systemd alternatives).
**Tests**: `tests/test_scanner_daemon_grab.py` — injected `opener`/`ioctl`
(monkeypatched `os.open`/`fcntl.ioctl` recording calls): grab(1) on start,
grab(0)+close on stop, **and on an exception inside the read loop** (context
manager); `EBUSY` ⇒ message naming a second daemon; `PermissionError` ⇒ message
naming the udev rule *and* `usermod -aG input` *and* the systemd unit; wrong-device
refusal (a node whose name/keycaps are not scanner-shaped is never grabbed
without `--allow-any`); lockfile ⇒ second instance exits non-zero with a message;
signal handlers call the release path (invoke them directly).
**Acceptance**: no code path can leave a grab held after the process ends; no
code path can grab a device that is not the scanner.

### T4 — queue API (backend) — *the largest task; contract for T5/T6*

**Files**: `app/models.py` (`ScannerTerminal`, `ScanEvent`), `app/schemas.py`
(the §2.1 models), `app/routers/scanner.py` (new), `app/main.py`
(`include_router`), `app/security.py` (reuse `token_hash`; a `pairing_code()`
generator if it does not already have one), README API summary + repo-layout
block. **No migration entries** (new tables — say so in the PR body).
**Tests**: `tests/test_scanner_api.py` —
register → poll (empty, correct envelope) → heartbeat bumps `last_seen`;
wrong/absent queue token ⇒ 401 with **no existence leak** (unknown terminal and
wrong token are indistinguishable); `terminal_id` reuse with a different token ⇒
409; pairing: code → pair → ingest → poll delivers **in order**; code is
single-use (second pair ⇒ 409); expired code ⇒ 410; 5 wrong codes ⇒ locked;
ingest idempotency (same `event_id` twice ⇒ one event, `duplicates:1`);
ingest re-classifies a mislabelled kind (`reclassified:1`); ingest rejects an
unshaped code (`rejected:[{event_id,reason}]`, never queued); badge payload
length > 128 ⇒ rejected; TTL reaping (freeze/patch `utcnow` ⇒ `expired:1`, row
gone); queue cap ⇒ `overflow:1` + oldest gone; cursor (`after=` excludes earlier
events, `latest_seq` monotonic); **no cross-terminal leakage** (A never sees B's
events, with B's token or A's token); lease (second session ⇒ `suspended` + zero
events; after TTL ⇒ `granted`); `unpair` ⇒ daemon token dead (401 on ingest) and
the SPA's poll still works; security headers present on every new route
(including a 401 and a 500 — the `apply_security_headers` doctrine);
`GET /api/healthz` gains non-secret counters if Q7 is accepted.
**Acceptance**: the full pairing→ingest→poll→revoke round trip is green under
`TestClient` with no hardware and no daemon; a badge payload appears in no log
line and in no error envelope.

### T5 — daemon forwarding + packaging (daemon) — *medium*

**Files**: `scripts/scanner_daemon.py` (`--pair`, `--base`, config load/save at
0600, heartbeat thread, bounded send buffer + backoff, batching, `--json`),
`scripts/systemd/hcrm-scanner-daemon.service` (new), `scripts/run.sh` (a
`--with-scanner-daemon` hint line only — **no** behaviour change),
`docs/opticon-hardware.md` (a §4b "running the grab daemon"),
`README.md` (back-office scanner section + repo layout).
**Tests**: `tests/test_scanner_daemon_forward.py` — injected transport:
one scan ⇒ one POST with the right envelope; retry with backoff on
`URLError`/500 and **the read loop keeps running** (the `scanner_bridge` P2
doctrine: nothing exits silently, no `SystemExit` inside the reader path);
`401 unpaired` ⇒ loud log + stop forwarding + keep grabbing (or re-pair if a
code is supplied); duplicate `event_id` on retry ⇒ server `duplicates`, no
double add; buffer bound ⇒ oldest dropped with a counted warning; config written
with mode 0600 and reloaded without a code; revoked token ⇒ actionable message;
`--dry-run` performs **zero** HTTP calls (assert on the transport); heartbeat
cadence; **every** stdout/stderr line masks badge payloads (`redact_badge`) —
assert with caplog/capsys for both badge forms.
**Acceptance**: `HCRM_BASE=… uv run python scripts/scanner_daemon.py --dry-run`
works with no server; a real run needs only the udev rule + one pairing code.

### T6 — SPA feed (frontend) — *medium/large*

**Files**: `static/scanfeed.js` (new **pure** reducer, dual-mode export exactly
like `scancapture.js`: `initialFeedState()`, `feedStep(state, ev) → {state,
actions}`, exported constants `FEED_POLL_MS=400`, `FEED_IDLE_POLL_MS=2000`,
`FEED_HIDDEN_POLL_MS=5000`, `FEED_BACKOFF_MAX_MS=30000`, `FEED_DEDUP_CAP=200`;
actions: `route{code,kind,seq,event_id}`, `cursor{seq}`, `schedule{delayMs}`,
`status{...}`, `notice{text}`; events: `probe-ok|probe-404|probe-fail`,
`poll-ok{events,daemon,lease,expired,overflow,latest_seq}`, `poll-fail{status}`,
`visibility{visible}`, `pair-ok|pair-fail`, `revoke`),
`static/app.js` (adapter: `installScanFeed()` next to `installScanCapture()`,
`feedFetch()` with the three custom headers, `localStorage`/`sessionStorage`
handling, `enqueueScan` as the **only** sink, `scannerFeed` state, pairing panel
methods), `static/index.html` (script tag **before** `app.js`; header chip +
panel markup with `data-scanner-feed`), `static/style.css` (chip/panel),
`scripts/ui_check.py` (pin `data-scanner-feed` is rendered on the login and
catalogue views).
**Tests**: `tests/js/scanfeed.test.mjs` (scripted streams: in-order routing;
`event_id` dedup within the LRU and eviction past the cap; cursor advance on
receipt; `suspended` lease ⇒ zero routes; `expired/overflow > 0` ⇒ one `notice`;
daemon online↔offline transitions ⇒ status actions; `probe-404` ⇒ state `off`
and **no further polls**; `poll-fail` ⇒ exponential backoff capped, never a
tight loop; hidden tab ⇒ idle interval), `tests/test_frontend_contract.py`
additions (scanfeed.js loads before app.js; `installScanFeed` is still called in
`mounted()` **and** `installScanCapture` is *not* removed — the wedge fallback
must survive; the feed never sends `wait_ms`/`hold` and the poll URL is pinned,
per §2.4; the feed's credential headers are not `Authorization`),
`tests/test_scan_feed_contract.py` (optional pytest mirror of the JS constants,
the `SCAN_DEFER_MS` precedent).
**Acceptance**: `node --check static/app.js`; `node --test tests/js/*.test.mjs`;
`ui_check.py --self-host` completes **in roughly today's wall time** (M9/M10 —
if the gate slows by more than ~2 s per page, the poll loop is wrong); no console
records from the feed on the login page with no daemon running.

### T7 — docs, smoke gate, hardware checklist — *small but mandatory*

**Files**: `docs/plans/r4-checklist.md` (new hardware pass, §9),
`scripts/smoke_test.py` (a **hardware-free** scanner round trip: register →
pairing code → pair → ingest a barcode + a badge → poll → assert order/kinds →
unpair → assert 401; runs in the existing CI `scripts` job),
`README.md` (scan-to-cart paragraph: two input paths, which is live, the pairing
step, the security note about badge payloads in the queue + TTL; API summary;
repo layout), `NOTES.md` (a round-4 paragraph), `docs/opticon-m10.md` (§9.3 badge
login now works with the browser unfocused; §10 new "10.5 The grab daemon";
§10.4 accepted risks += the queue exposure), `docs/opticon-hardware.md` (§4 input
subsystem), `docs/plans/round4-task.md` (leave as-is; answer it in the PR body).
**Tests**: `tests/test_metadata_and_ops.py::test_readme_has_no_known_false_claims`
must stay green (add claims only when true — e.g. do **not** write "scans never
reach the focused window" without the daemon-running qualifier); the smoke test's
new phase must pass in both `--allow-degraded` and full-model modes.

---

## 8. Invariants a reviewer should check off

- **I1** While the daemon holds the grab, no scanner keystroke reaches any window
  — including the browser. (Hardware checklist item 1.)
- **I2** Every scan reaches **at most one** consumer session **at most once**
  (lease + cursor-persisted-on-receipt + `event_id` dedup on both ends).
- **I3** No scan is delivered after its TTL; a stale barcode can never land in a
  later member's cart.
- **I4** The daemon never resolves items, never touches stock, never mints or
  holds a member token, never prints a badge payload.
- **I5** The SPA's flow logic is not duplicated: the feed's only sink is
  `enqueueScan(code, kind)`.
- **I6** The daemon's death returns the keyboard to the OS (kernel-released grab)
  and the SPA degrades to the wedge with **no** code-path change.
- **I7** The browser gate never hangs: no server-side hold anywhere in the feed
  (M9), pinned by a contract test.
- **I8** Classification has one Python source and one JS mirror, pinned by a
  parity test (T1).
- **I9** No new runtime dependency; `pyproject.toml`/`uv.lock` untouched, so
  `uv sync --frozen` in CI is unaffected.

---

## 9. Hardware checklist (`docs/plans/r4-checklist.md`, T7)

Blocked on re-plugging the scanner (M1) and on the permission step (§4.3, M4/M5).
Draft, in order — each step names the invariant it proves:

0. **Pre-flight**: `lsusb | grep -i 065a` shows `a001`; install the udev rule;
   re-plug; `ls -l /dev/input/event*` shows the scanner's node as
   `root:plugdev 0660`; `uv run python scripts/opticon_detect.py` names it.
1. **Grab stops the typing** (I1): focus a terminal text field, run
   `--dry-run`, scan → the decoded string prints in the daemon and **nothing
   appears in the terminal**. Control: stop the daemon, scan → the terminal
   receives the keystrokes again (proves the grab, not the read, is doing it).
2. **`kill -9` resumes typing** (I6): `kill -9` the daemon mid-run, scan into a
   focused field → keystrokes arrive.
3. **Hotplug** (M2): unplug → the daemon logs and backs off; re-plug → it
   re-discovers the *new* `eventN` and resumes without a restart.
4. **Pairing**: SPA header panel shows a code; `--pair` binds; the panel flips to
   "daemon online" with the node name; `unpair` from the panel ⇒ the daemon logs
   `401 unpaired` and stops forwarding.
5. **Badge, logged out**: scan a member badge **with the browser unfocused**
   (focus a terminal) → the SPA logs in, greeting + catalogue, no keystrokes in
   the terminal. This is the round-4 headline: the flow works without focus.
6. **Badge, same user** → 60 s overlay; cancel; expiry logs off.
7. **Badge, other user** → save-then-switch (outgoing cart intact under the old
   account).
8. **Barcode, logged in** → cart line + toast; unknown code → intake modal →
   filed as a scan request; misread → the 422 toast.
9. **No double delivery** (I1+I2): with the daemon running and the browser
   focused, one scan ⇒ exactly one cart line and no characters in the focused
   field.
10. **Two tabs** (§3.3): the second tab shows "another tab is driving the
    scanner" and adds nothing.
11. **Non-US layout bonus**: set an X layout that differs from US, scan a code
    containing `-`/`!`/`:` → the payload is byte-exact (the wedge path is not).
12. **TTL** (I3): close the tab, scan, wait > TTL, reopen → the poll reports
    `expired: 1` and nothing is added.

---

## 10. Fallback matrix (requirement: "what happens when the daemon isn't running")

| State | Scanner keys reach the focused window? | Scans reach the app? | SPA indicator |
|---|---|---|---|
| daemon running, SPA open (paired) | **no** (grabbed) | yes, via the queue | `data-scanner-feed="live"` + device name |
| daemon running, SPA closed | no | queued, then TTL-expired; daemon logs "no consumer" | — |
| daemon running, server down | no | buffered in the daemon, retried; lost past `--buffer-max` | — (nothing to render) |
| daemon **not** running | **yes** (kernel wedge) | only if the browser is focused — round 3's `scancapture.js`, unchanged | `data-scanner-feed="off"` + "scans need this window focused" |
| daemon running, terminal unpaired | no | nowhere (daemon logs "unpaired") | `data-scanner-feed="unpaired"` + pairing code button |
| `--no-grab` debug mode | yes | yes ⇒ **double delivery if the browser is focused**; the flag prints that warning | — |

The wedge path is **kept, unchanged, and still node-tested**: it is the fallback,
the pre-pairing state, and the only path on a host without the permission fix.
T6 pins that `installScanCapture()` is not removed.

---

## 11. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | **No hardware attached** (M1) ⇒ nothing above can be verified live; the xHCI controller has died before | every task is offline-testable by construction (T2/T3 inject sysfs + ioctl; T4/T5/T6 need no device); the live pass is isolated in `r4-checklist.md`, the round-3 precedent |
| R2 | Grabbing the wrong device disables a human keyboard system-wide | §4.2's four guards + `--allow-any` opt-in + the grabbed name is always logged |
| R3 | Permission work needs sudo and a re-plug; the operator may skip it | the daemon's `PermissionError` message names all three routes; `--diagnose` distinguishes "no permission" from "no device"; README quick-start lists it as a one-time step |
| R4 | **Badge payloads now live in three new places** (daemon buffer, `scan_events`, poll responses) where before they lived only in the browser | TTL 600 s + lazy reaping (I3), `redact_badge` in every print path, never echoed in error envelopes, queue readable only with the `queue_token`, `unpair` revokes the writer, README security notes + `opticon-m10.md` §10.4 entry. Hardened alternative = variant **1b** (§1, Q1) |
| R5 | The browser gate hangs (M9) | no server-side hold, client-timer pacing, contract test I7, and T6's acceptance criterion "gate wall time unchanged ±2 s/page" |
| R6 | Double delivery in the grab↔wedge transition (daemon dies mid-burst) | the wedge capture still swallows bursts (it is never removed), `event_id` dedup, TTL. A truncated burst reaching a field is bounded to one field, one scan |
| R7 | Two tabs / two kiosks on one terminal | lease (§3.3) + tests |
| R8 | Stale scans replaying into a different member's cart | TTL + cursor-persisted-on-receipt + the checklist item 12 |
| R9 | SQLite write contention at a busy counter | one small INSERT per scan, `busy_timeout=5000` already set on every connection, WAL already on; `scripts/load_test.py` unchanged |
| R10 | Multi-worker deployment | nothing in the design is in-process (no hold, no in-memory fan-out), so any `--workers` value works — a direct benefit of rejecting the hold |
| R11 | A scanner configured without a CR suffix emits nothing | `--terminator-timeout-ms` flush mode + `--diagnose` + a troubleshooting line in `opticon-m10.md` §8 |
| R12 | Scope creep into Phase-B flows (server-side sessions) | I4/I5 + variant 1b explicitly deferred with its cost written down |

## 12. Open questions for the coordinator

- **Q1** Variant **1a** (queue carries the badge payload, TTL-bounded — my
  recommendation for round 4) vs **1b** (server resolves the badge, queues an
  `adopt_token`; no credential at rest, but a device token can mint sessions and
  `switchAccount` needs a second entry point).
- **Q2** Pairing code: anonymous (default, needed for the logged-out kiosk
  bootstrap) vs `HCRM_SCANNER_PAIRING_REQUIRES_STAFF=1`.
- **Q3** Should `POST /api/scanner/scans` also accept the serial/USB-COM
  personality (`source:"serial"`), so Phase B lands with no new API? The schema
  already allows it; recommend **no code now**, one sentence in the docs.
- **Q4** A barcode arriving while a modal is open is **dropped** today
  (`handleBarcodeScan`). Keep the drop (parity, simple) or add
  `POST /api/scanner/scans/{seq}/nack` to requeue? Recommend keep-and-count,
  with `dropped_total` surfaced in the panel.
- **Q5** Defaults: TTL 600 s, queue cap 200, poll 400 ms live / 2 s unpaired /
  5 s hidden, lease 30 s, pairing code 300 s, heartbeat 10 s, online window 30 s.
  Any of these the owner wants different?
- **Q6** `--release-on-server-down=SECONDS` (drop the grab so the wedge keeps
  working when the server is unreachable) — convenient on a dev box, confusing on
  a kiosk. Recommend **not** shipping it.
- **Q7** Add non-secret scanner counters to `GET /api/healthz`
  (`{terminals, paired, pending}`)? Useful for ops/smoke; tiny info leak.
- **Q8** If sub-100 ms latency is a hard requirement, the only gate-safe route is
  a `?scanfeed=off` flag honoured by `ui_check` plus a real long-poll. Recommend
  deferring until someone measures the 0.2–0.5 s as a problem.
- **Q9** Should the daemon also serve as the *only* scanner path in a future
  kiosk-mode browser (`--kiosk`), letting us delete `scancapture.js`? Not in
  round 4: the wedge path is the fallback and is already paid for.

## 13. Verification plan (per PR)

`uv run pytest -q` (and the reverse-order run CI does) · `node --check
static/app.js static/scanfeed.js` · `node --test tests/js/*.test.mjs` ·
`uv run python scripts/ui_check.py --self-host` (wall time compared against
`main`) · `uv run python scripts/smoke_test.py --allow-degraded --fast-hashing` ·
`git status --porcelain` empty (CI hygiene: no `data/` artefacts, no scratch
files — my Chrome experiments ran entirely in `/tmp` and were deleted) ·
hardware: `docs/plans/r4-checklist.md` once the scanner is re-plugged.
