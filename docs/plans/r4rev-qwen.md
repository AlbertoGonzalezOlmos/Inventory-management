# Round 4 cross-review (qwen) — glm's and kimi's plans vs mine

Reviewed in full: `docs/plans/r4plan-glm.md` (472 lines), `docs/plans/r4plan-kimi.md`
(366 lines). Mine: `docs/plans/r4plan-qwen.md` (794 lines). Review only — no code
written, nothing committed.

Where a claim is decidable I decided it with a measurement instead of an opinion.
New measurements made **during this review** are M12–M17 below; M1–M11 are from
`r4plan-qwen.md` §0.

---

## 1. Verdict in one paragraph

All three plans converge on the same skeleton — stdlib `EVIOCGRAB` on the evdev
node, evdev-keycode→HID-usage translation fed into the **existing**
`ScanAssembler`, a thin daemon that forwards raw codes, a DB-backed per-terminal
queue, the SPA routing everything through the untouched `enqueueScan(code, kind)`
bus, one new `input` line in the vendor udev rule, and the wedge path kept as the
no-daemon fallback. That convergence is strong evidence the skeleton is right; it
should be treated as settled and not re-litigated. The three real disagreements
are the **channel cadence**, the **binding direction/credential split**, and
**queue delivery semantics**. On cadence kimi's long-poll is **disqualified by
measurement** (M9: it hangs the `ui_check` browser gate; M13: it also costs
either a blocked event loop or 1/40 of the server's sync-endpoint capacity per
kiosk). On binding, **glm's direction wins** (daemon self-registers and mints the
code; the browser claims it) and I am changing my plan to match. On delivery
semantics, **cursor + ack-pruning + a per-tab lease** beats both kimi's
consume-on-read (split delivery; a badge can log in the *wrong tab*) and glm's
unenforced one-browser rule (double cart lines). My own plan needs seven
corrections, listed in §7 — the largest is that my T2/T3 split was over-fine and
my custom header trio should be dropped for glm's reuse of `api(path, {token,
raw:true})`.

### Scorecard (5 = best)

| Axis | glm | kimi | qwen (as filed) | Winner |
|---|---|---|---|---|
| (a) daemon→SPA channel | 4 (1 s poll, gate-safe by luck not measurement) | 2 (long-poll: breaks CI — M9/M13) | 4 (short poll, measured gate-safe, but no ack-pruning) | **qwen+glm shape, kimi's TTL** |
| (b) counter-session binding | 5 (two capabilities, daemon-minted code, staff recovery) | 2 (one shared token, staff-gated bootstrap) | 3 (two capabilities, but browser-minted code + anonymous row litter) | **glm** |
| (c) badge-payload handling | 3 (5 min TTL, no prune below cursor) | 4 (2 min TTL + consume-on-read; but a **disk spool** of raw badges) | 3 (10 min TTL — too long; 1b documented) | **kimi's TTL + my cursor-ack prune** |
| (d) EVIOCGRAB mechanics/lifecycle | 5 (self-check, parity property test, unmapped-key reset, loud discovery failure) | 4 (32-bit format as data; but the `uaccess` claim is measured false — M12) | 4 (wrong-device guards, lockfile, `--diagnose`; no parity property test) | **glm** |
| (e) task ordering | 4 (5 PRs, frontend-serial observed; no shared-shape task) | 4 (8 batches; over-split B1/B2, extra D2 admin UI) | 3 (7 PRs; T2/T3 split the same new file) | **kimi's A1 + glm's daemon-in-one** |
| Hardware grounding | 2 (second-hand, stale: "M-10 is attached", by-id path) | 3 (assumes uaccess; unverified enumeration) | 5 (measured M1–M11) | **qwen** |

---

## 2. New measurements from this review

| # | Fact | Evidence |
|---|---|---|
| M12 | **logind's `uaccess` does not apply to keyboard-class input devices on this host.** The rule is joysticks-only, the node has **no ACL entries**, and a real keyboard's tags carry no `uaccess`. ⇒ kimi's "Desktop sessions get `uaccess` from logind anyway" is false here; the udev rule (or root, or `input` membership) is **mandatory on a native seat**, not just on WSL2. | `grep input /usr/lib/udev/rules.d/70-uaccess.rules` → `SUBSYSTEM=="input", ENV{ID_INPUT_JOYSTICK}=="?*", TAG+="uaccess"`; `getfacl -p /dev/input/event3` → only `user::rw- group::rw- other::---`; `udevadm info -q all -n /dev/input/event3` → `ID_INPUT_KEYBOARD=1`, `TAGS=:power-switch:` |
| M13 | **A long-poll hold is expensive in this app whichever way it is written.** The repo has **zero** `async def` data endpoints (only `lifespan`, the header middleware and the 500 handler) and `get_session` is a **sync** generator. So an `async def` long-poll doing sync SQLAlchemy blocks the event loop on every re-check tick; a `def` long-poll occupies one token of anyio's default thread limiter for the whole hold — **measured 40 tokens**. 40 held polls = every other sync endpoint starves. | `grep -rn "async def" app/`; `app/database.py:105`; `anyio.to_thread.current_default_thread_limiter().total_tokens` → `40`; starlette 1.7.0 / fastapi 0.141.1 |
| M14 | **glm's `api(path, {token, raw:true})` reuse works as claimed** — a foreign token skips the global 401 → logout/redirect cleanup. No new fetch helper and no custom headers are needed for the feed. | `static/app.js:29-30,48` (`const foreign = token !== undefined` … `if (res.status === 401 && !foreign && …)`) |
| M15 | `struct.calcsize`: `llHHi` = 24, `iiHHi` = 16. kimi's 32-bit fallback size is right; its **format is `…I` where the kernel's `value` is `__s32`** (same width, wrong signedness — harmless for EV_KEY's 0/1/2, wrong for anything negative). | `python3 -c "import struct; …"` |
| M16 | `/data/` **is** git-ignored, so kimi's spool file would not dirty the tree — the objection to it is credential-at-rest, not CI hygiene. | `.gitignore:223` |
| M17 | This host's `hostname` (`pentacosiarca-ThinkPad-T14s-Gen-4`) fits glm's `^[a-z0-9_-]{1,64}$`, but hostnames in general may carry uppercase and dots ⇒ the id must be **normalised**, not just validated. | `hostname` |

---

## 3. Settled by convergence (do not re-open)

All three plans independently chose the same answer for each of these; each is
also the answer I would defend:

1. **evdev + `EVIOCGRAB`, stdlib `fcntl.ioctl`, no `evdev` dependency.**
   `EVIOCGRAB = _IOW('E',0x90,int) = 0x40044590` (M7); hidraw is a parallel
   stream and reading it silences nothing.
2. **Decode by translation, not re-implementation**: `KEY_*` → HID usage →
   synthetic 8-byte boot report → the **unchanged** `ScanAssembler` /
   `parse_keyboard_report` from `scripts/scanner_hid.py`. (glm and kimi both note
   the mapping is *not* an offset — letters are non-contiguous 30–38/44–50 — so a
   flat dict is the only correct shape. Agreed, and it is the detail most likely
   to be gotten wrong by an implementer.)
3. **Thin daemon**: it never calls `qr-login`, `cart/scan` or stock-adjust; the
   browser stays the routing authority because the kiosk's member token lives in
   the browser. The feed's only sink is `enqueueScan(code, kind)`.
4. **DB-backed per-terminal queue, new tables, no `COLUMN_MIGRATIONS` entries**
   (`create_all()` adds tables; all three say so explicitly — good, because the
   append-only migration doctrine makes reviewers look for an entry).
5. **One new `input` line in `scripts/udev/99-opticon-scanner.rules`**,
   `MODE="0660", GROUP="plugdev"`, matching `ATTRS{idVendor}=="065a"`; glm and
   kimi both add `KERNEL=="event*"` (adopt — precision costs nothing).
6. **Grab lifetime == fd lifetime**: exit/crash/`kill -9` ⇒ kernel releases ⇒
   typing resumes; `scancapture.js` stays untouched as the fallback and the two
   paths are mutually exclusive while the grab is held.
7. **A status indicator in the SPA** so the operator can see which path is live
   (glm's `data-terminal-status` chip, kimi's header pill, my
   `data-scanner-feed`).
8. **Rejected by all three**: SSE/WebSocket (glm adds the decisive detail that
   `EventSource` cannot send an `Authorization` header; I add M9), the daemon
   serving the SPA directly (CSP `connect-src 'self'`), and the fat
   bridge-style daemon.

---

## 4. Review of `r4plan-glm.md`

### 4.1 Strengths to adopt (I am adopting all of these)

- **A1 — the parity *property* test.** "For every usage in `_USAGE_MAP`, the
  evdev path (its KEY code, shifted and unshifted) yields the same characters as
  `parse_keyboard_report` directly." This turns the reuse claim into an
  executable pin over the whole table instead of a handful of samples. Strictly
  better than my sample-based decoder tests; adopt verbatim into T2's test list.
- **A2 — unmapped keycode ⇒ log loudly + reset the assembler buffer.** My plan
  silently ignored unknown codes, which can deliver a *truncated* barcode (the
  exact failure my idle-timeout guard exists to prevent). glm's rule is the
  misread doctrine applied consistently: a character we cannot represent means
  the whole scan is not the scan. Adopt.
- **A3 — `struct.calcsize("@llHHi") == 24` boot self-check** so a 32-bit host
  fails at startup instead of mis-parsing (16-byte records). Combine with kimi's
  format-as-data (M15).
- **A4 — capability separation** (daemon `terminal_secret` = write, browser
  `terminal_token` = read): "a browser-compromised token cannot inject scans, and
  the daemon secret cannot read badge payloads out of the queue." That is the
  right split and kimi's single shared token loses it.
- **A5 — the binding direction** (daemon registers the terminal, mints the
  pairing code, browser claims it). Beats mine: it works headless/under systemd
  (`--show-pairing`), gives staff a server-side terminal inventory
  (`GET /`, `DELETE /{id}` as the recovery path), and does not let anonymous
  callers litter terminal rows — the weak point of my browser-creates design.
- **A6 — `api(path, {token, raw:true})` reuse** (verified, M14) instead of my new
  `feedFetch()` + three custom headers. Less new surface, and the foreign-token
  path already guarantees a feed 401 cannot log the kiosk's member out.
- **A7 — discovery failure is a loud exit, never a skip** ("cannot grab; the
  scanner would keep typing"), plus `--list` mirroring `scanner_hid --list`, plus
  `--count` for smoke runs. Adopt all three CLI bits.
- **A8 — loopback gate on registration** (`request.client.host`, injectable for
  `TestClient` whose peer is `testclient`, env escape hatch). A cheap, testable
  anti-litter measure my plan lacked.
- **A9 — the frontend-serial observation**: "only T4 touches
  `static/app.js`/`index.html`/`style.css`". Worth promoting to an explicit
  scheduling constraint (§6.2) — three plans each adding a header pill is exactly
  how this repo produced three colliding `COLUMN_MIGRATIONS` blocks.
- **A10 — the udev-rule contract pin** in `tests/test_scripts_contract.py` (a
  rule line that silently disappears reverts the daemon to root-only and nobody
  notices until the next re-plug). Adopt.

### 4.2 Weaknesses / risks

- **W1 — stale hardware grounding (the plan's own stated basis).** glm builds on
  `r3rev-qwen.md` §R1 / the sync doc: "the M-10 is attached", "enumerates as
  `/dev/input/by-id/usb-Opticon…-event-kbd → event14`". Measured today: **no
  `065a` on any bus** (M1), `/dev/input/by-id/` **does not exist** (M3), and
  `event14` is the headphone jack (M2). Consequences: (i) T5's hardware evidence
  protocol is not executable as written; (ii) the by-id "fast path" is dead code
  on this host and must be opportunistic only; (iii) glm's open risk #1
  ("permissions unverified from inside the repo") is **closed by M4/M5** — the
  nodes are `root:input 0660`, this user is not in `input`, and the installed rule
  has no `input` line, so sudo is required and glm's udev change is mandatory
  rather than optional.
- **W2 — no server-side classification.** D2 explicitly declines it ("the SPA
  classifies"). That contradicts the repo's own doctrine — `scan_requests.py`
  re-validates because it never trusts the client's classification — and it means
  unshaped garbage (a misfire burst, a partial scan) enters `scan_events` and is
  silently dropped client-side, with the daemon's stderr as the only record. kimi
  and I both re-derive `kind` server-side; adopt that, with a mismatch corrected
  and counted rather than an error.
- **W3 — no idempotency key.** `POST /scans` returns `{seq}`; a client-side
  timeout after the server committed (the common retry trigger) duplicates the
  scan, and a duplicated barcode surfaces as `qty 2` — i.e. as a *plausible*
  cart, not as an error. Neither glm nor kimi has this; my `event_id` +
  `UniqueConstraint(terminal_id, event_id)` closes it for one column.
- **W4 — "one active browser per terminal … documented, not enforced" (D3, risk
  #6).** Two profiles on one kiosk both poll, both get every event, both call
  `enqueueScan`. For barcodes that is a double line; for a **badge** it is two
  concurrent `qr-login`s in two tabs — one of them invisible. glm rejects
  server-side consume-on-read because it turns this into *split* delivery
  (a fair critique of kimi), but the third option — a **per-tab lease** — is
  neither: one tab is authoritative, the other is told so on screen. ~40 lines in
  the pure reducer plus one column pair; worth it.
- **W5 — badge rows live for the whole TTL (5 min) even after delivery.** glm
  purges on write/read by TTL and cap, but with a client-side cursor nothing
  deletes a row the browser has already processed. Fix: treat the poll's
  `after=<seq>` as an **ack** and delete rows `id <= seq` for that terminal
  (§5c). Exposure then falls from 5 min to roughly one poll interval.
- **W6 — `terminal_id` regex vs real hostnames** (M17): validate *and* normalise
  (lowercase, dots→dashes), or widen the pattern; otherwise the default id is
  rejected on hosts like `Counter1.shop.local`.
- **W7 — T2 is the largest single PR in any of the three plans** (decode +
  discovery + grab + terminal client + heartbeat + buffer + CLI + ~15 tests).
  Reviewable, but I would split the *server client* out (my T5) so the grab/decode
  review is not entangled with HTTP-retry review.
- **W8 — the retry buffer is "32 scans / 30 s, then drop-oldest"**: dropping
  customer scans needs a surfaced count, not just a stderr line (my
  `dropped_total` on the terminal row, reported by the next poll).

### 4.3 Disagreements with glm, and who is right

| # | glm | mine | Call |
|---|---|---|---|
| D-a | 1 s fixed poll | 400 ms live / 2 s idle / backoff | **Split the difference: 500 ms live, 2 s unpaired/hidden, ×2 backoff to 30 s.** Both are gate-safe (M10); 1 s is visible at a counter, 400 ms is needless load. Constant lives in the pure reducer and is pinned. |
| D-b | at-least-once (cursor advanced **after** the batch drains) | at-most-once (cursor persisted **on receipt**) | **glm is right for barcodes, I am right for badges — resolve with the lease + dedup.** With one authoritative tab (lease) and `event_id` dedup in the reducer, advancing after drain is safe and strictly better for the customer-visible case (a scan they watched succeed is not lost). Adopt glm's ordering, keep my dedup LRU. |
| D-c | broadcast + per-browser cursor | lease | **Both**: broadcast semantics (restart-resume, server-restart-safe) *plus* a lease so only one tab acts. glm's own risk #6 is the argument for the lease. |
| D-d | no shared classification module | `app/scanshape(s).py` + parity pin | **Mine, with kimi's fixture.** Three consumers (daemon, server, SPA mirror) need one rule; glm's "classification stays in its one home (scancapture.js)" is true only if the server never classifies, which W2 argues it must. |
| D-e | TTL 5 min, cap 128 | TTL 10 min, cap 200 | **kimi's 2 min wins** — see §5c. |

---

## 5. Review of `r4plan-kimi.md`

### 5.1 Strengths to adopt

- **B1 — the shared parity fixture** `tests/fixtures/scan_shapes.json`, asserted
  against **both** `app/scanshape.classify_scan` (pytest) and
  `HcrmScanCapture.scanShape` (`node --test`, which CI already globs). This is
  better than my "the regex source strings must be literally equal" pin: it tests
  behaviour, survives a legitimate regex refactor, and one file feeds both
  suites. Adopt as the primary pin (drop my textual-equality assertion to an
  optional extra).
- **B2 — TTL 2 minutes, with the right rationale**: "a scan made while nobody is
  at the counter must not pop up after lunch." My 600 s default was the weakest
  of the three, and the TTL's real job here is bounding a **bearer credential at
  rest**, not being a mailbox. Adopt 120 s (env-tunable).
- **B3 — server re-derives `kind`** via the shared module, correcting a mismatch
  instead of erroring, 422 for unshaped. Exactly the `scan_requests` doctrine;
  adopt (this is also W2's fix for glm).
- **B4 — `get_terminal` in `app/deps.py`**, next to `get_current_user` /
  `require_staff`. House style; my plan left it implicit in the router. Adopt.
- **B5 — the unpaired login-screen hint** (their open question #8). Yes, ship it:
  an unpaired kiosk is precisely the round-4 problem, and silence is how operators
  discover it by a scan typing into a terminal. Adopt into the SPA task.
- **B6 — single-event POST** (`{code, kind}` → `201 {id}`) instead of my 1–32
  batch. Simpler, and the only reason I batched (draining a disk spool) dies with
  B-objection-1 below. Adopt, keeping my `event_id`.
- **B7 — 32-bit `input_event` format as data** (`fmt` chosen by
  `struct.calcsize("l")`), so both shapes are unit-testable with canned bytes.
  Adopt with the signedness fix (M15) and glm's startup self-check (A3).
- **B8 — `?timeout=0` doubles as the pairing-verify call** — one less endpoint.
  (Moot under short-poll, but the trick of "the read endpoint verifies the
  credential" survives: the SPA's first poll *is* its pairing check.)

### 5.2 Weaknesses / risks

- **K1 (decisive) — the long-poll channel breaks the browser gate.** M9, measured
  with `ui_check`'s own flags: one 20 s hold cost 20.5 s of wall time, and a
  **loop** of 2 s holds *never* dumped — I killed it at 90 s. `ui_check._run` has
  `timeout=180` and records `{"error": "timeout"}` per target, so every SPA route
  would go red. Virtual time cannot advance past a pending request, and a
  re-polling loop therefore keeps the page permanently non-idle. kimi's design is
  the only one of the three that specifies a hold, and it is the only one that
  fails CI. (M10 shows the fix is free: fast responses + client-timer pacing
  dumped in 0.54 s with 0 console records.)
- **K2 — the hold is expensive even without the gate problem (M13).** The repo has
  no `async def` data endpoints and `get_session` is sync: an `async def`
  long-poll doing sync SQLAlchemy blocks the event loop on each 200 ms re-check
  (every other request waits behind it), and a `def` long-poll pins **1 of
  anyio's 40 thread tokens** for the whole hold — 40 kiosks, or 40 hung polls,
  starve every sync endpoint in the app. kimi's risk #6 considers SQLite
  contention but not the loop/thread cost, which is the binding one.
- **K3 — "Desktop sessions get `uaccess` from logind anyway" is measured false
  (M12).** The uaccess rule is joysticks-only, the event node has no ACL entries,
  and a keyboard's tags are `:power-switch:`. Their §1.4 therefore understates the
  install step: the udev rule is load-bearing on a **native desktop seat**, not
  only on WSL2. (This is the same mistake the sync doc made in October; it is
  worth killing permanently in `docs/opticon-hardware.md`.)
- **K4 — one shared terminal token for daemon *and* browser.** Stored in
  `localStorage`, it is simultaneously the read and write capability, so any
  localStorage theft (and the CSP carries `'unsafe-eval'` — see
  `app/main.py`'s own comment) lets the holder **inject** scans into that
  counter's queue as well as read badge payloads out of it. glm's A4 split
  removes that at the cost of one column. Also: a 43-char token pasted into a
  browser field is worse UX than a 6–8 char code, and it lands in the daemon's
  config/argv.
- **K5 — staff-only terminal creation** makes the kiosk bootstrap need a staff
  login *before* the scanner can be paired, and drags in a whole admin-CRUD
  frontend batch (D2). glm's daemon-registers + browser-claims flow needs no
  privileged session at the counter and shrinks the staff UI to a recovery
  listing.
- **K6 — the disk spool is a credential-at-rest regression.**
  `data/scanner-daemon-spool.jsonl`, cap **1000**, holding raw scans until the
  server returns: that is up to a thousand **badge payloads** on disk with no
  stated file mode, no TTL and no encryption, in the same directory as the DB and
  `badge.key` (M16: git-ignored, so CI would not notice). The bridge doctrine it
  cites ("survive your environment") is about not crashing, not about persisting
  secrets. **Reject**: bounded in-memory buffer (glm 32 / mine 200) with a loud
  dropped-count. A server outage of more than a few hundred scans is an incident,
  not a queue.
- **K7 — consume-on-read has a failure mode worse than double delivery: split
  delivery across tabs.** Two tabs polling concurrently each get a *subset*. A
  badge scan can therefore land in the **background** tab, which runs
  `badgeLogin`/`switchAccount` there: the hidden tab logs in (or switches
  accounts, or arms a 60 s logoff on *its* token) while the visible tab shows the
  old session. That is invisible state divergence on a customer-facing kiosk.
  glm spotted the split; the lease fixes both split and double without giving up
  restart-resume.
- **K8 — consume-on-read also gives up "browser was closed, scan still
  delivered"** (both glm and I keep it via a persisted cursor) and its loss
  window (their risk #2) is the same as at-most-once, so it buys less than it
  costs.
- **K9 — no idempotency key** (same defect as W3).
- **K10 — nits**: `struct` format `llHHI` where `value` is `__s32` (M15);
  `daemon_online` derived from a 10 s heartbeat and a 30 s window is fine but
  unspecified for the SPA-closed case; B1 as its own batch (models/schemas
  without the router) is an unreviewable PR — fold into B2.

### 5.3 Disagreements with kimi, and who is right

| # | kimi | mine | Call |
|---|---|---|---|
| D-f | long-poll `timeout=15`, async, 200 ms DB re-check | short client-paced poll, no hold | **Mine — by measurement (M9) and by cost (M13).** Not a taste call. If sub-100 ms latency is ever required, the gate-safe route is a `?scanfeed=off` flag honoured by `ui_check` plus a real hold; that makes the browser gate blind to the feed, so defer it (my Q8). |
| D-g | consume-on-read | client cursor (+ ack-pruning) | **Mine, plus the lease.** Cursor keeps restart-resume and lets the *client* choose at-least/at-most-once per batch; ack-pruning gives kimi's short credential exposure without their delivery semantics. |
| D-h | one shared terminal token, staff-created | two capabilities, browser-created | **glm's A4+A5 beats both of us**: two capabilities (kimi loses) *and* daemon-initiated registration (I lose). |
| D-i | TTL 2 min | TTL 10 min | **kimi.** Adopt 120 s. |
| D-j | disk spool | in-memory buffer | **Mine.** K6. |
| D-k | parity fixture JSON | regex-source equality | **kimi.** B1. |
| D-l | module name `app/scanshape.py`, dep in `app/deps.py` | `app/scanshapes.py`, router-local | **kimi** (house singular naming: `scanlookup.py`, `qrbadge.py`). |

---

## 6. Final recommended architecture (the synthesis)

### 6.1 Channel — server-side queue, client-paced short poll, cursor + ack-prune + lease

- **Transport**: same-origin HTTP, no hold, no streaming, no new dependency.
  Poll cadence **500 ms** while paired+visible, **2 s** while unpaired or the tab
  is hidden, ×2 backoff to **30 s** on transport errors, reset on success.
  Constants exported from the pure reducer and pinned (the `SCAN_DEFER_MS`
  pattern). Acceptance criterion carried over from my plan: **`ui_check
  --self-host` wall time must stay within ~2 s/page of `main`** (M9/M10) — that
  single check is what keeps a future contributor from reintroducing a hold.
- **Read**: `GET /api/scanner/events?after=<seq>&session=<tab-uuid>&limit=50`,
  `Authorization: Bearer <queue_token>` issued through the existing
  `api(path, {token, raw:true})` foreign-token form (M14 — no new fetch helper, no
  custom headers). The server resolves the terminal from the token hash, so no
  terminal id is needed in the request.
- **Cursor + ack**: `after` is the client's persisted cursor **and** an
  acknowledgement — the response deletes rows `id <= after` for that terminal, so
  a delivered badge payload dies at the next poll (~0.5 s), not at TTL. The
  client persists the cursor **after the batch has drained** (glm D-b), and the
  reducer dedups by `event_id` (LRU 200) so a redelivery after a crash cannot
  double-add.
- **Lease**: one authoritative tab per terminal (`session` = per-tab
  `sessionStorage` uuid, 30 s TTL, renewed by each poll). Other tabs get
  `{events: [], lease: "suspended"}`, render "another tab is driving the scanner"
  and freeze their cursor. This is what makes both split delivery (K7) and double
  delivery (W4) impossible.
- **Durability**: TTL **120 s** (`HCRM_SCANNER_EVENT_TTL_SECONDS`), cap **200**
  per terminal (drop-oldest, counted), lazy purge on every read and write (the
  `_issue_token` / `_cart_out` precedent). Ordering is the server's autoincrement
  `id`, never the daemon's clock; `scanned_at` is advisory.
- **Delivery guarantee, stated**: at-least-once per authoritative tab, deduped by
  `event_id` client-side, idempotent server-side by
  `UniqueConstraint(terminal_id, event_id)` (W3/K9).

### 6.2 Binding — daemon-registered terminal, two capabilities, code-claimed browser

```
daemon:  POST /api/scanner/terminals {terminal_id}     (loopback-gated, A8; id = normalised hostname, M17)
         → 201 {terminal_id, terminal_secret}           (write capability, shown once, 0600 state file)
daemon:  POST /api/scanner/terminals/pairing-code       (Bearer terminal_secret)
         → {code, expires_at}                           (8-char Crockford base32, single-use, 10 min, hashed)
         also printed at startup and via --show-pairing  (systemd/headless case)
browser: "Pair scanner" panel on the LOGIN screen → POST /api/scanner/pair {code}
         → {terminal_id, queue_token}                   (read capability, localStorage; re-pair overwrites, last wins)
daemon:  POST /api/scanner/heartbeat every 10 s         (Bearer terminal_secret) → daemon_online (<30 s)
daemon:  POST /api/scanner/events {event_id, code, kind, symbology, scanned_at}
browser: GET  /api/scanner/events?after=&session=        → events → enqueueScan(code, kind)
staff:   GET /api/scanner/terminals · DELETE /api/scanner/terminals/{id}   (recovery; delete cascades events)
browser: POST /api/scanner/terminals/unpair              (kills the queue token; daemon's next POST 401s loudly)
```

- **Two capabilities** (A4): a stolen `queue_token` cannot inject; a stolen
  `terminal_secret` cannot read badge payloads. Both stored hashed
  (`security.token_hash`), both rotatable (`rotate` = the recovery path).
- **No staff session needed at the counter** (K5 fixed); the human act that binds
  "this browser" to "this physical counter" is typing a short code shown by the
  daemon — and, symmetrically, the SPA's unpaired hint (B5) tells an operator
  when that step is outstanding.
- **A feed 401 must never clear the member session** — guaranteed by the
  foreign-token path (M14) and pinned by a test.
- **Terminal rows are reaped** by `last_seen` TTL (7 days) — missing from all
  three plans, including mine; without it every browser-profile reset leaves a
  row behind.
- **Exactly one task touches `static/`** (A9): the SPA feed, its pill, the pairing
  panel, the unpaired hint and the staff recovery listing all land in that one PR.

### 6.3 Badge payloads

Raw payload in the queue is **unavoidable in this architecture** (the browser must
present it to `/api/auth/qr-login`) and all three plans accept it. Bound it four
ways: 120 s TTL (B2) · **ack-prune below the cursor** (§6.1 — the real win:
exposure ≈ one poll interval) · hashed credentials + `queue_token`-gated reads ·
`redact_badge` in **every** daemon and server log path (extract
`scanner_bridge.redact_code` into the shared module, re-exported so
`tests/test_scanner_bridge.py` is untouched). **No disk spool** (K6).
The credential-free variant — server resolves the badge on ingest and queues an
`adopt_token` the SPA exchanges for an `AuthOut` — stays documented as the
upgrade path (my variant 1b), not built: it lets a device credential mint member
sessions and needs a second switch path, and with ack-pruning the thing it buys
is now ~0.5 s of exposure.

### 6.4 Daemon, grab, fallback

Everything in §3 items 1–2 and 5–8, plus:

- **Discovery** by sysfs VID/PID walk (the `_hidraw_sysfs` shape), `by-id` used
  **opportunistically only** (M3: it does not exist here), `eventN` **never cached
  across re-plugs** (M2). No candidate ⇒ **loud exit** naming what *is* attached
  (A7 + the `scanner_bridge.resolve_port` precedent).
- **Wrong-device guards** (mine, keep): VID/PID **and** a name/capability check
  before `ioctl(…, 1)`; `--allow-any NODE` as the explicit opt-out; the grabbed
  node + product name logged at every grab. Grabbing a human keyboard silently
  disables typing system-wide — this is the one catastrophic failure mode in the
  round.
- **Lifecycle**: `ENODEV`/`EIO` ⇒ release, back off 1→30 s, re-discover forever,
  never exit (the xHCI controller on this box has died before —
  `docs/m10-unreachable-2026-10-10.md`); `EBUSY` ⇒ single-instance lockfile check
  first, then a message naming the likely holders; `SIGTERM` ⇒ ungrab + flush;
  `kill -9` ⇒ kernel releases (checklist item).
- **Decoder**: press-only reports, autorepeat (`value == 2`) ignored, shift state
  tracked across press/release, idle timeout **150 ms** discards a partial burst
  (`--terminator-timeout-ms` flips discard→flush for suffix-less scanners),
  `MAX_SCAN_LEN` 128, **unmapped keycode ⇒ log + reset buffer** (A2). No timing
  heuristic is needed to tell scans from typing: with an exclusive grab, every key
  from this device is a scan key. Bonus worth documenting: evdev keycodes are
  layout-independent, so the daemon decodes US-HID semantics **regardless of the
  X/Wayland layout** — the wedge path is layout-dependent and mangles `-`/`!`/`:`
  on a non-US kiosk today (checklist item 11).
- **Server outage**: keep the grab (typing into a dead app helps nobody), buffer
  in memory (cap 200, drop-oldest, **counted and surfaced** in the next poll's
  `dropped`), retry with backoff, never exit, never `SystemExit` inside the reader
  path (the bridge's P2 doctrine).
- **Fallback matrix** unchanged from my §10: daemon down ⇒ wedge ⇒ round-3 capture
  when the browser is focused; SPA closed ⇒ queue + TTL; `--diagnose` and
  `--dry-run` for the hardware pass; `--no-grab` warns that both paths are then
  live (double delivery if the browser is focused).

### 6.5 Final task order (6 PRs)

| # | PR | Files | Tests | Depends on |
|---|---|---|---|---|
| **T1** | shared classification (pure) | `app/scanshape.py`; `app/routers/auth.py` delegates; `scripts/scanner_bridge.py` re-exports `redact_code`; `tests/fixtures/scan_shapes.json` | `tests/test_scanshape.py`, `tests/js/scan_shapes.test.mjs`; existing auth/qr/bridge suites unchanged | — |
| **T2** | backend channel | `app/models.py`, `app/schemas.py`, `app/deps.py` (`get_terminal`), `app/routers/scanner.py`, `app/main.py` (one `include_router` line), README API summary | `tests/test_scanner_channel.py` (~25 cases: auth matrix incl. *user token ≠ terminal token*, capability separation both ways, pairing lifecycle, idempotent `event_id`, kind re-derivation, TTL/cap/ack-prune, cursor exclusivity, lease grant/hold/suspend/expiry, cross-terminal isolation, staff list/delete cascade, terminal reaping, headers on 200/401/500, loopback gate + env override) | T1 |
| **T3** | daemon core — decode + grab + lifecycle (offline) | `scripts/scanner_daemon.py` | `tests/test_scanner_daemon.py` (decoder matrix, **the A1 parity property test**, struct self-check, 32-bit format detection, fake-sysfs discovery, idle/max-length guards, unmapped-key reset) + `tests/test_scanner_daemon_grab.py` (injected opener/ioctl: grab/ungrab on stop **and** on exception, EBUSY, EACCES message naming all three permission routes, wrong-device refusal, lockfile) | T1 |
| **T4** | daemon client + ops packaging | same file (register/pair/heartbeat/forward/buffer/CLI), `scripts/udev/99-opticon-scanner.rules` (+`input` line), `scripts/systemd/hcrm-scanner.service`, `docs/opticon-hardware.md` | `tests/test_scanner_daemon_forward.py` (injected transport: sequence, retry keeps reading, `--dry-run` = zero HTTP, 0600 state file, redaction in every print path) + one `test_scripts_contract.py` pin for the udev line (A10) | T2, T3 |
| **T5** | SPA feed (**the only frontend PR**) | `static/scanfeed.js` (pure reducer), `static/app.js`, `static/index.html`, `static/style.css`, `scripts/ui_check.py` pins | `tests/js/scanfeed.test.mjs` (routing order, `event_id` dedup, cursor-after-drain, lease suspended ⇒ zero routes, backoff transitions, `probe-404` ⇒ off and no further polls, dropped/expired ⇒ one notice) + `tests/test_frontend_contract.py` pins (script order, `installScanCapture` **not** removed, feed routes through `enqueueScan` verbatim, no `wait_ms`/hold anywhere, feed 401 never clears the member session, `/scanfeed.js` serves 200) | T2 |
| **T6** | docs + gates + hardware checklist | `docs/plans/r4-checklist.md`, `scripts/smoke_test.py` (hardware-free register→pair→ingest→poll→unpair round trip, runs in the existing CI `scripts` job), README, NOTES, `docs/opticon-m10.md` §9.3/§10 | `test_metadata_and_ops.py::test_readme_has_no_known_false_claims` stays green | all |

T2 and T3 are independent and can run in parallel (different files, one writer
each). T3 and T4 touch the same new file **sequentially**, never concurrently.
T5 is the single frontend writer (A9). This is glm's 5-PR shape with kimi's A1
pulled to the front and my daemon split kept for review size — 6 PRs, each
CI-green alone, each reviewable in under ~400 lines of diff.

---

## 7. Changes to my own plan (`r4plan-qwen.md`)

Intellectual honesty, largest first:

1. **Binding direction reversed → glm's (A5).** My browser-created terminal +
   daemon-entered code allowed anonymous terminal-row litter and needed an
   interactive prompt for the headless case. Now: daemon self-registers
   (loopback-gated, A8), mints the code (`--show-pairing` for systemd), the browser
   claims it from the login screen. My `X-Scanner-Terminal` header disappears —
   the server resolves the terminal from the token hash.
2. **Custom headers + `feedFetch()` dropped → `api(path, {token, raw:true})`
   (A6, M14).** Verified that the foreign-token path already prevents a feed 401
   from logging the member out; my three-header scheme was new surface for
   nothing. The lease's `session` moves to a query param.
3. **TTL 600 s → 120 s (B2), and rows are now ack-pruned below the cursor**
   (new, mine): the poll's `after=` deletes delivered rows, cutting badge exposure
   from "the TTL" to "one poll interval". This also weakens my own variant 1b
   enough that I now recommend **against** building it this round.
4. **T2+T3 merged into one daemon-core PR** (glm T2 / kimi C1 both do this):
   splitting one brand-new file across two PRs left a half-built file and
   guaranteed rebase friction. The *tests* stay in two modules (decode / grab).
5. **Batch ingest → single-event POST (B6)**, keeping my `event_id` idempotency
   (which neither peer has — W3/K9 — and which I am keeping).
6. **Parity pin: kimi's shared fixture JSON (B1) replaces my regex-source
   equality assertion.** Behaviour over text.
7. **Unmapped keycode ⇒ log + reset the buffer (A2)** — my "ignore unknown codes"
   could deliver a truncated barcode. Plus glm's A1 parity property test, A3
   struct self-check, A7 `--list`/`--count`/loud-discovery-failure, A10 udev pin.
8. **Naming/homes**: `app/scanshapes.py` → `app/scanshape.py`; `get_terminal`
   lives in `app/deps.py` (B4, D-l).
9. **Cursor advances after the batch drains, not on receipt (glm D-b)** — safe now
   that the lease and `event_id` dedup exist; strictly better for the customer who
   watched a scan succeed.
10. **New, from this review, in none of the three plans**: terminal-row reaping by
    `last_seen` (§6.2); the explicit "exactly one PR touches `static/`"
    scheduling rule (A9); the `ui_check` wall-time acceptance criterion promoted
    to a shared gate for whichever channel is chosen (§6.1); normalisation (not
    just validation) of `terminal_id` (M17); and the correction that `uaccess`
    never covers keyboards on a native seat (M12), which belongs in
    `docs/opticon-hardware.md` §4 permanently.
11. **Rejected after review, with reasons on the record**: kimi's long-poll (K1/K2),
    kimi's disk spool (K6), kimi's shared single token (K4) and staff-gated
    bootstrap (K5), glm's no-server-classification (W2) and
    one-browser-honour-system (W4).
12. **Kept, unchanged**: short client-paced poll and the no-hold rule (M9/M10,
    now with M13 as a second independent argument); wrong-device guards and the
    lockfile; `--diagnose`/`--dry-run`/`--no-grab`; in-memory bounded buffer;
    server-side re-classification; the 12-step hardware checklist (both peers'
    lists are subsets of it — glm's 5 evidence steps and kimi's 4 map onto items
    1, 2, 3, 5, 9).

---

## 8. Residual open questions for the coordinator

1. **Poll cadence**: 500 ms live / 2 s idle (recommended) vs glm's flat 1 s. Both
   gate-safe; only feel and load differ.
2. **Pairing-code direction** is settled as daemon→browser (A5), but should the
   code also be *displayable* in the staff terminal list for a remote operator
   (SSH-only kiosk)? Cheap, mild shoulder-surf surface.
3. **Register gate**: loopback-only by default with `HCRM_SCANNER_REGISTER=any`
   (glm A8) — confirm the back-office/remote-daemon deployment is out of scope for
   round 4, otherwise the gate must be a real credential.
4. **`kind` mismatch policy**: correct-and-count (kimi B3, recommended) vs reject
   (my original). Recommend correct-and-count, with the count surfaced.
5. **Barcode arriving while a modal is open** is dropped today
   (`handleBarcodeScan`). Keep the drop (parity) or requeue? All three plans kept
   it implicitly; recommend keep + surface `dropped` in the panel (glm W8).
6. **Does the queue also serve the USB-COM personality** (`source:"serial"`, so
   Phase B needs no new API)? Schema already allows it; recommend docs-only.
7. **`GET /api/healthz` scanner counters** (`{terminals, paired, pending}`):
   useful for ops and the smoke test, tiny info leak. Recommend yes.
8. **Latency escape hatch**: if 0.5 s is ever judged too slow at the counter, the
   only gate-safe upgrade is a real hold plus a `?scanfeed=off` flag honoured by
   `ui_check` — which makes the browser gate blind to the feed. Recommend
   deferring until measured as a problem.

## 9. Errata to carry into the implementation (all three plans)

| Claim | Correction | Evidence |
|---|---|---|
| "the M-10 is attached" (glm T5) | not attached; the whole live pass is blocked on a re-plug | M1 |
| `/dev/input/by-id/usb-Opticon…-event-kbd → event14` (glm D5, kimi §1.1 implicitly) | `by-id` does not exist on this host; `event14` is the headphone jack | M2, M3 |
| "permissions unverified" (glm risk #1) | verified: `root:input 0660`, user not in `input`, installed rule has no `input` line ⇒ sudo step is mandatory | M4, M5 |
| "desktop sessions get `uaccess` from logind anyway" (kimi §1.4) | false for keyboards: uaccess is joysticks-only, no ACL, `TAGS=:power-switch:` | M12 |
| long-poll is cheap (kimi §2.1) | blocks the loop (`async def` + sync `get_session`) or pins 1/40 thread tokens per kiosk | M13 |
| `struct` format `"llHHI"` / `"iiHHI"` (kimi §1.1) | `value` is `__s32` ⇒ `…i`; sizes 24/16 confirmed | M15 |
| my own TTL 600 s | too long for a bearer credential; 120 s + ack-prune | §7.3 |
