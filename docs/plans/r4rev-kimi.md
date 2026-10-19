# Round 4 cross-review (kimi) — r4plan-glm, r4plan-qwen

Reviewed in full: `docs/plans/r4plan-glm.md`, `docs/plans/r4plan-qwen.md`,
against my own `docs/plans/r4plan-kimi.md`. Verdict up front: **qwen's plan is
the strongest skeleton** — it is the only one that measured the host and the CI
browser gate before designing, and two of its measurements (M9 long-poll hang,
M4/M5 permission gap) invalidate parts of my plan and glm's. glm contributes
the capability-separation framing and the best "rejected alternatives" ledger.
My plan contributes little the other two lack (server re-classification and the
synthetic-report decode are in both peers too) — most of my distinct choices
(consume-on-read, long-poll, one shared token) lose on the merits.

---

## 1. glm-5.3's plan

### Strengths to adopt

- **Read/write capability separation** (D3): daemon holds a terminal *secret*
  (post scans, heartbeat, mint pairing codes), the browser holds a terminal
  *token* (read only). A compromised browser token cannot inject scans; a
  compromised daemon secret cannot read badge payloads. Strictly better than
  my one-shared-token design.
- **The rejected-alternatives ledger** is excellent: hidraw-read+evdev-grab
  pairing (two-node mismatch risk), the `evdev` pip dep, SSE (EventSource
  can't send `Authorization`), fat-bridge daemon, daemon-serves-SPA (CSP
  `connect-src 'self'`). All correct; the CSP point also kills any
  "daemon serves the SPA on localhost:port" idea permanently.
- **Loopback-gated `/register`** (D6): daemon self-registers without any
  staff login; a LAN peer cannot mint terminals. Nice for the
  remote/back-office deployment shape.
- **Udev-rule contract pin** (T3): one assertion in
  `test_scripts_contract.py` that the rules file contains the `input`
  subsystem line — exactly the failure class this repo's contract tests
  exist for (the rule silently disappearing reverts the daemon to
  root-only, noticed at the next re-plug).
- **Unmapped keycode ⇒ buffer reset + loud log** (T2): a barcode containing
  a character we cannot represent is *reported, never guessed* — the
  misread doctrine applied inside the decoder.
- **Parity property test**: for every usage in `_USAGE_MAP`, the evdev path
  yields the same characters as `parse_keyboard_report` — the "reuse" claim
  as an executable pin. Stronger than my scripted-stream tests alone.
- Hardware evidence protocol (T5) with `kill -9` grab-release proof and
  server-restart buffer-drain proof.

### Weaknesses / risks

- **Broadcast + per-browser cursor with no lease** (D3/D4): two paired
  tabs/browsers each get every event → double cart lines. Mitigation is
  "documented, not enforced" (one-window-per-terminal operational rule).
  That inherits the wedge world's discipline, as glm says — but the wedge
  world's failure was *visible typing in the wrong window*; this failure is
  an invisible duplicate line in a member's cart. qwen's per-tab lease
  solves it in ~20 lines of server code.
- **At-least-once delivery** (cursor persisted after `drainScanQueue()`
  settles): a browser crash mid-batch redelivers. glm argues a duplicate is
  "visible and correctable" while a lost scan is silent — defensible, but a
  duplicate line is a *data error the customer pays for if nobody notices*,
  and a lost scan is re-scannable at the counter where the operator is
  standing. qwen's at-most-once (cursor on receipt) is the safer default.
- **Daemon-printed pairing code** (D3/T2 `--show-pairing`): the code lands
  in stdout/journald. Single-use and 10-min TTL bounds it, but qwen's
  direction (code shown on the kiosk *screen*, entered at the daemon) keeps
  it out of logs entirely.
- **No ingest idempotency**: a daemon POST that times out after the server
  committed will be retried (glm's bounded buffer) and double-append. The
  autoincrement `seq` cursor makes the duplicate *visible* to the SPA but
  not *dedupable* without client state. qwen's
  `UniqueConstraint(terminal_id, event_id)` fixes this server-side.
- **`/dev/input/by-id` fast path** (D5/T2): qwen measured (M3) that
  `/dev/input/by-id/` does not exist on the dev host right now (true: the
  scanner is unplugged — but the point stands that the symlink is
  environment-dependent). glm does verify through sysfs, so this is only a
  robustness nit: use by-id opportunistically, never depend on it.
- No wrong-device guard beyond VID/PID+name; no `--dry-run`/`--diagnose`;
  no idle-timeout guard for a truncated burst (a half-GTIN *would* be
  delivered and 422 as a misread — annoying but bounded; qwen discards it).

### Disagreements

- **glm's consume-on-read critique (risk 6) is aimed at my design and is
  correct**: consume-on-read turns the two-tab failure into invisible *split
  delivery* (tab A consumed the event, tab A died, scan gone). I concede
  this — but the conclusion is not "broadcast with an operational rule",
  it's qwen's lease + cursor.
- glm's T1→T5 serial order is clean, but bundling all daemon work into T2
  (~500+ lines plus tests) makes a hard-to-review PR. qwen's split
  (decode core / grab lifecycle / forwarding) is better review hygiene.

---

## 2. qwen's plan

### Strengths to adopt (this is most of the final architecture)

- **M9 — the decisive measurement**: a server-side long-poll hold hangs the
  CI browser gate (`--virtual-time-budget` cannot advance past an in-flight
  request; a hold *loop* never dumps → `ui_check` timeout → red gate on
  every PR). **This kills my long-poll design outright**, and would kill
  any SSE variant too. qwen's rule — client-timer pacing, every request
  completes fast — plus a contract test *pinning the hold out* (so nobody
  "optimises" it back in) is the right answer. The 0.2–0.5 s latency cost
  is invisible at a counter.
- **M1–M8 host ground truth**: scanner currently unplugged; event-node
  numbers unstable (`event14` is now headphones — never cache `eventN`
  across replugs); `/dev/input/by-id` absent; the installed udev rule lacks
  the `input` subsystem and this user is not in `input`. Every plan (mine
  included) assumed some of these away.
- **Per-tab lease** (`X-Scanner-Session`, `sessionStorage`): first tab gets
  `granted`, others get `suspended` + zero events + "another tab is driving
  the scanner". Solves the two-tab double-add that glm leaves to operator
  discipline.
- **Ingest idempotency**: `event_id` (daemon uuid) +
  `UniqueConstraint(terminal_id, event_id)` → daemon retries after a
  timeout cannot double-append, reported as `duplicates`. Fixes a real hole
  in my spool-replay design.
- **Browser-initiated pairing** (§3.2): the browser generates
  `terminal_id` + `queue_token` (read capability), displays the single-use
  5-min code; `scanner_daemon.py --pair` prompts for it (getpass, never
  argv) and receives the `daemon_token` (write capability, 0600 config).
  The credential crosses the screen→operator→daemon path exactly once and
  never touches a log. Better than glm's daemon-printed code and much
  better than my copy-one-token-into-two-places flow. Anonymous
  registration can't hijack an existing terminal (409 on token mismatch),
  and `HCRM_SCANNER_PAIRING_REQUIRES_STAFF=1` is the documented hardening.
- **Wrong-device guards** (§4.2): grabbing a human keyboard disables typing
  system-wide — the catastrophic failure mode. VID/PID + keyboard-capability
  check + name substring + `--allow-any` opt-out (the bridge's `--any-port`
  doctrine) + always log the grabbed device name. My plan had only VID/PID
  discovery.
- **Decoder guards**: idle-timeout discard of truncated bursts
  (`SCAN_IDLE_MS`, with a flush mode for scanners configured without a CR
  suffix), `MAX_SCAN_LEN`, `KEY_KPENTER` terminates, auto-repeat ignored,
  partial-read reassembly, `O_NONBLOCK`+`select` for an interruptible loop,
  US-HID semantics regardless of X layout (a genuine improvement over the
  wedge path — worth the README line).
- **T1-first refactor**: extract `app/scanshapes.py` and make
  `auth.py::_badge_from_scanned` delegate to it, with a parity test that
  reads the four regex *source strings* out of `scancapture.js` and asserts
  literal equality with the Python ones. Stronger than my shared-vectors
  approach (source equality + vectors; do both).
- **T7 smoke-gate phase**: a hardware-free register→pair→ingest→poll→
  unpair round trip in `scripts/smoke_test.py`, running in the existing CI
  `scripts` job. End-to-end channel coverage in CI without a scanner —
  neither glm nor I had this.
- **Separate feed headers/fetch** (`X-Queue-Token` etc., not
  `Authorization`): the SPA's `api()` wrapper owns Bearer + global-401
  logout; a revoked queue token must never log the kiosk's member out.
  Subtle, correct, pinned by a test.
- Honest fallback matrix (§10), invariant checklist (§8), and open
  questions with recommendations (§12) — exactly what a coordinator needs.

### Weaknesses / risks

- **Complexity budget**: lease + session id + cursor + event_id dedup +
  expired/overflow counters is the largest API surface of the three plans
  (~15 test groups in T4). All of it is justified by a named failure mode
  except arguably the lease (could be v2 — but building the API without it
  and adding it later changes the poll contract, so building it now is
  actually cheaper).
- **Browser-generated identity ⇒ orphaned terminals**: losing localStorage
  mints a new terminal row and orphans the old one (reaped only by TTL/
  last_seen heuristics). There's no staff list/delete endpoint in qwen's
  table (glm has `GET /` + `DELETE /{id}` for staff). Minor gap; add glm's
  two ops endpoints.
- **TTL 600 s + at-most-once**: a scan made while the browser is closed is
  delivered up to 10 min later — into whichever member session is then
  active. I3 ("never into a later member's cart") is *stated* but the
  cursor-persist-on-receipt only covers tab death, not the
  closed-browser-reopened-later case: the event is delivered on reopen and
  enqueued under the *current* login. Actually correct behaviour for a
  kiosk (the scan is staged/added for whoever is there), but the invariant
  text overpromises; 5-min TTL (glm) bounds it tighter.
- `POST /api/scanner/terminals` unauthenticated + heartbeat merged into
  register is a bit too clever (heartbeat-as-register re-validates the
  token each beat — fine — but the endpoint does two jobs; a dedicated
  `PUT /heartbeat` would read cleaner; keep merge only if the diff stays
  small).
- No explicit udev contract pin (glm has it); no `kill -9` evidence item
  phrased as grab-release *proof* (it is checklist item 2 — fine).

### Disagreements

- **Q1 (variant 1b, server-resolves-badge): I agree with qwen's
  recommendation to reject it for round 4.** It removes the badge payload
  from the queue but hands the *write* token the power to mint member
  sessions — a strictly worse capability than "inject scans", and it forks
  `switchAccount` into a second entry point. TTL + reaping + redaction
  (all three plans converge here) is the right round-4 posture.
- **Q4 (keep modal-drop)**: agree — requeue/nack machinery for a parity
  behaviour is scope creep; counting drops is enough.
- Minor: qwen's `X-Scanner-Terminal` + `X-Queue-Token` + `X-Scanner-Session`
  triplet could collapse terminal+token into one `Authorization: Bearer` on
  a *separate feed client* — but the explicit-header choice is deliberately
  legible and the test pin is cheap. Not worth changing.

---

## 3. Axis-by-axis verdicts

**(a) Channel.** qwen wins, decisively: client-paced short poll (400 ms
live / 2 s unpaired / 5 s hidden, exponential backoff cap), no server hold
(measured, M9), cursor-per-tab + lease + ingest idempotency. My long-poll
is disqualified by the browser gate; glm's 1 s poll is gate-safe but lacks
the lease and dedup. Consume-on-read (mine) loses to cursor+lease on both
failure modes (split delivery, no resume).

**(b) Counter-session binding.** qwen's browser-initiated pairing +
capability-separated tokens wins; glm's capability separation is the same
idea from the daemon side and its loopback-gated register + staff
list/delete endpoints should be merged in. My staff-created single shared
token loses on both UX (operator copies one secret into two places) and
security (no capability separation).

**(c) Badge payload.** All three converge: raw payload in the queue,
TTL-bounded (recommend 300 s — glm's default — as the tighter bound),
lazy reaping, `redact_*` in every print/log path, hashed credentials
everywhere else. Server-resolves-badge (1b) correctly rejected by qwen and
here: it trades a bounded at-rest exposure for a session-minting device
token.

**(d) EVIOCGRAB.** Convergent core (stdlib `fcntl.ioctl(0x40044590)`,
24-byte `@llHHi`, auto-release on fd close, udev `input` line,
never-cache-eventN, wedge fallback = round-3 capture untouched). qwen adds
the decisive operational layer: wrong-device guards, lockfile,
`--dry-run`/`--diagnose`/`--no-grab`, idle/max-length discard,
`O_NONBLOCK`+`select`. Adopt qwen's §4 wholesale + glm's udev contract pin
+ my 32/64-bit struct-format handling (parameterised format, both pinned
in tests — qwen pins 24 only and would fail loudly on 32-bit, glm defers
it; parameterising is ~3 lines and keeps the door open).

**(e) Task ordering.** qwen's T1–T7 granularity wins: T1 (shapes refactor)
is a tiny, independently-valuable PR; daemon split into decode/grab/
forwarding keeps PRs reviewable; T7's smoke phase gives CI end-to-end
coverage. glm's monolithic T2 and my coarse four-batch plan both review
worse. Frontend-serial rule is satisfied by all three (only the last
feature task touches `static/`).

---

## 4. Final recommended architecture (synthesis)

1. **Decode**: evdev keycodes → HID usages → synthetic 8-byte boot reports
   → existing `ScanAssembler` (unanimous). Plus qwen's guards (idle-timeout
   discard, `MAX_SCAN_LEN`, KPENTER, auto-repeat ignore, unmapped-key loud
   reset) and glm's parity property test over `_USAGE_MAP`.
2. **Grab**: stdlib `EVIOCGRAB`, VID/PID + capability + name guards,
   lockfile, hotplug re-discovery by VID/PID (never a cached node),
   keep-the-grab on server outage with a bounded retry buffer; death
   auto-releases → wedge → round-3 capture (unanimous fallback).
3. **Classification**: `app/scanshapes.py` as the one Python source,
   `auth.py` delegates, `scancapture.js` mirror pinned by regex-source
   equality + shared vectors (qwen T1 + my vectors). Server re-classifies
   at ingest and stores its own verdict (mine and qwen's; glm classifies
   only in the SPA — acceptable but weaker against non-daemon writers).
4. **Channel**: per-terminal DB queue; `POST /scans` with `event_id`
   idempotency (daemon_token); `GET /scans?after=` with per-tab cursor,
   per-tab **lease**, TTL 300 s + cap with overflow counters, **no server
   hold ever** (contract-pinned); SPA poll paced 400 ms/2 s/5 s + backoff;
   feed credentials on non-`Authorization` headers via a dedicated
   `feedFetch`.
5. **Binding**: browser-initiated `terminal_id`+`queue_token` (read),
   screen-displayed single-use 5-min pairing code, daemon `--pair` →
   `daemon_token` (write, 0600 config), `unpair` revokes; staff
   list/delete endpoints (glm) for recovery; optional
   `HCRM_SCANNER_PAIRING_REQUIRES_STAFF=1` hardening.
6. **Delivery semantics**: at-most-once (cursor persisted on receipt);
   dropped/expired/overflow counts surfaced in the pairing panel.

### Recommended task order (qwen's skeleton, amended)

```
T1 shared scanshapes + parity pins        (small; unblocks all)
T2 evdev decode core (offline)            ┐ parallel after T1
T3 grab + lifecycle + wrong-device guard  ┘ (same file, two PRs)
T4 queue API (+ lease, idempotency, staff list/delete)   ← contract
T5 daemon forwarding + packaging (udev + pin, systemd, docs)
T6 SPA feed (pure scanfeed.js + adapter + pairing panel + chip)
T7 smoke-gate channel round trip, hardware checklist, docs sweep
```

T2/T3 ∥ T4 after T1; T5 after T3+T4; T6 after T4; T7 last. Only T6
touches `static/`.

---

## 5. Changes to my own plan (r4plan-kimi.md)

1. **Drop the server-side long-poll hold** → client-paced short poll, no
   hold, contract-pinned (qwen M9; my single biggest error — it would have
   red-gated CI on every PR).
2. **Drop consume-on-read** → per-tab cursor + lease + `latest_seq`; glm's
   split-delivery critique accepted, qwen's mechanism adopted. Delivery
   becomes at-most-once (cursor on receipt), dropping my "second poll
   empty" test for lease/cursor tests.
3. **Split the terminal credential** into browser-generated read token +
   daemon write token; **flip pairing direction** (code on the kiosk
   screen, daemon `--pair`), replacing my staff-creates-token +
   copy-twice flow. Keep staff list/delete (from glm) and the optional
   staff-gate env.
4. **Add ingest idempotency** (`event_id` + unique constraint) — my
   spool-replay could double-post after a timeout.
5. **Add decoder/device guards**: idle-timeout discard, `MAX_SCAN_LEN`,
   KPENTER, unmapped-key reset, wrong-device refusal + `--allow-any`,
   lockfile, `--dry-run`/`--diagnose`.
6. **Strengthen the parity test**: literal regex-source equality between
   `app/scanshapes.py` and `scancapture.js` (qwen), plus my shared-vectors
   JSON; `auth.py::_badge_from_scanned` delegates to the shared module
   (qwen T1).
7. **Keep from my plan**: synthetic-report decode reuse (all agree),
   server-side re-classification at ingest (qwen agrees; glm doesn't —
   recommended), 32/64-bit struct handling, udev `input` line (all agree),
   status pill, spool (now drained against an idempotent endpoint),
   `redact_code` in daemon logs.
8. **Adopt glm's udev contract pin** and **qwen's T7 smoke phase**
   (hardware-free channel round trip in the CI `scripts` job).
9. TTL: my 120 s → **300 s** (glm's default; 600 leaves the
   closed-browser-reopen window too wide for the I3 invariant to be
   honest).
