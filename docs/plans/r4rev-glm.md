# Round 4 cross-review — glm-5.3 (reviewing r4plan-kimi.md and r4plan-qwen.md)

My plan under review: `docs/plans/r4plan-glm.md`. Both peers were read in
full. Verdict up front: **qwen's plan is the strongest of the three** — it is
the only one grounded in measurements taken on *this host today* (M1–M11),
and one of those measurements (M9) is a hard kill of kimi's headline channel
design. But qwen is also the most over-built, and kimi contributes three
things worth keeping (staff terminal management, the dual-format struct
testing, the durable-spool idea). The recommended architecture below is
qwen's skeleton, hardened with one gate from my plan, trimmed of two of
qwen's ornaments, plus kimi's ops surfaces.

---

## 1. r4plan-kimi.md

### Strengths to adopt

- **K1 — shared classification first (`app/scanshape.py` + one JSON vector
  file asserted against both the Python function and
  `HcrmScanCapture.scanShape` under `node --test`).** Both peers independently
  converged on this; my plan missed it (I left classification solely in
  `scancapture.js` and had the SPA classify, server storing the code
  verbatim). A third consumer (the daemon) plus server-side re-derivation
  makes the shared module + cross-language parity pin clearly right — it is
  the `scanlookup` "ONE table" doctrine applied across the language boundary.
- **K2 — staff terminal management (`POST/GET/rotate/DELETE
  /api/scanner/terminals`, staff-only) plus an admin UI (D2).** Rotation is
  the actual recovery path for a leaked token, and "list with online status"
  is the ops surface this feature needs. My plan had bare list/delete with no
  UI and no rotate; kimi's is better.
- **K3 — struct format as data.** `fmt = "llHHI" if calcsize("l")==8 else
  "iiHHI"` with *both* formats unit-tested against canned bytes beats my
  "24-byte self-check fails loudly at startup" (kimi's keeps 32-bit hosts
  working, not just failing politely) and qwen's equivalent. (Nit: the value
  field is `__s32`, so `"llHHi"`/`"iiHHi"` — signed — is the correct format;
  `I` cannot misparse EV_KEY's 0/1/2 in practice but `i` is right.)
- **K4 — durable JSONL spool** (`data/scanner-daemon-spool.jsonl`, cap 1000,
  drop-oldest, FIFO drain before new scans). Survives a daemon restart during
  a server outage; my in-memory buffer (32/30 s) and qwen's (200 events) do
  not. With qwen's `event_id` idempotency (see K6/Q5) a spool re-POST cannot
  double-add, so the classic objection dissolves. I still recommend in-memory
  for round 4 (a daemon crash *during* a server outage is a rare² event and
  the recovery is a physical re-scan) — but the spool is the documented
  upgrade, and the design should say so.
- **K5 — unpaired-kiosk warning** (their open question 8): a dismissible
  login-screen hint when no terminal token is stored. An unpaired kiosk is
  exactly the round-4 problem silently re-enabled; make it visible. Cheap.
- **K6 — server re-derives `kind` on ingest, unshaped → 422** (the
  `scan_requests` doctrine). Qwen has the same (`reclassified`); my plan
  explicitly declined it. The peers are right: one rule, one source, the
  daemon's label is advisory.
- **K7 — the B1 boot test** (pre-existing DB + `create_all` ⇒ new tables
  appear): a one-line pin of the "no migration entries needed" claim, which
  reviewers trained on `COLUMN_MIGRATIONS` will otherwise not believe.

### Weaknesses / risks

- **K-W1 — the long-poll is dead on arrival, and the plan never checked the
  browser gate.** Qwen measured (M9): with `ui_check`'s actual Chrome flags
  (`--virtual-time-budget=6000`), an in-flight fetch freezes virtual time —
  a single 20 s hold ran 20.5 s; a *loop* of 2 s holds never dumped the DOM
  and hit a 90 s kill (ui_check would report `timeout` = red gate on every
  SPA route). Kimi's `GET /events?timeout=15` *is* that loop. The plan's own
  CI section names the browser gate but never models the feed under it. This
  is the round's decisive design fact and kimi missed it.
- **K-W2 — consume-on-read has two loss windows, one unacknowledged.** Their
  risk 2 admits the crash window (fetch returned, tab dies before
  `enqueueScan`). The unacknowledged one: events marked consumed *in the same
  commit* that returns them are lost forever if the response itself is
  dropped (network blip, tab navigated mid-parse). Broadcast + cursor
  redelivers by construction. My plan and qwen's both chose broadcast;
  kimi's justification ("two tabs can never double-add") is real but is
  solved without the loss by qwen's lease (§3.3).
- **K-W3 — no `event_id` idempotency.** A daemon retry after a timed-out
  POST (exactly what a long-poll + spool design makes likely) double-adds
  the scan. Qwen's `UniqueConstraint(terminal_id, event_id)` + `duplicates`
  counter closes it; with K-W2 it means kimi's channel can both lose AND
  duplicate the same scan.
- **K-W4 — one shared token for daemon and browser.** Conflates write
  (ingest) and read (poll) capabilities: a browser-compromised token can
  *inject* scans. Both my plan (secret/token pair) and qwen's
  (queue_token/daemon_token pair) separate them. Also: staff must copy a
  43-char token into *two* places (daemon config and browser pair field) —
  worse pairing UX than qwen's 6-digit code.
- **K-W5 — multi-interface enumeration flagged but unsolved** (risk 1):
  name-match + `--node` override. Qwen's §4.2 guard set (VID/PID + keycap/
  name check + grab only matching nodes + `--allow-any` opt-in) is the
  complete version of the same concern.
- **K-W6 — no lease, no suspended state**: the second tab just sees an empty
  batch (whichever tab polls first wins each scan — *split* delivery across
  tabs, which is worse than either duplication or suspension because it is
  invisible).
- Minor: 2-minute TTL is short for "browser closed briefly, then reopened"
  (a scan at the counter while the tab is being restarted is lost); no
  visibility-aware cadence; no wall-time acceptance criterion for ui_check.

### Disagreements (kimi vs mine vs qwen)

- **Long-poll vs short-poll:** qwen wins, on measurement (M9/M10). My 1 s
  short-poll was directionally right but qwen's 400 ms live / 2 s degraded /
  5 s hidden cadence is better calibrated and *proven gate-safe*.
- **Consume-on-read vs broadcast+cursor:** broadcast+cursor wins (K-W2).
  Kimi's stated advantage (no double-delivery) is achieved by qwen's lease
  without the loss windows.
- **Staff-created single token vs capability pair:** capability pair wins
  (K-W4); but kimi's staff *management* surface (K2) survives on top of it.

---

## 2. r4plan-qwen.md

### Strengths to adopt

- **Q1 — the measurements (M1–M11).** This is the best-grounded plan of the
  round: the scanner is *not attached* (M1 — so every task must be
  offline-verifiable, exactly the round-3 constraint), `eventN` numbers are
  **not stable** (M2 — never cache the node; my plan said "never cache across
  replugs" but M2 shows even the *current* number was wrong within days),
  **`/dev/input/by-id` does not exist on this host** (M3 — my plan proposed
  a by-id fast path; it must be opportunistic-only at best), the permission
  gap is confirmed precisely (M4/M5), and the struct/ioctl facts are pinned
  (M7). My plan's biggest factual error (by-id fast path) is corrected by
  M3.
- **Q2 — M9/M10: the browser-gate constraint on the channel.** The single
  most valuable contribution of any plan this round. It converts a
  preference (I "noted SSE as awkward under TestClient") into a measured
  invariant (I7: no server-side hold, ever, pinned by a contract test, with
  a wall-time acceptance criterion on T6). Adopt verbatim, including the
  contract test that pins the SPA never sends `wait_ms`/`hold`.
- **Q3 — the capability-pair binding (§3.1/3.2).** Browser-generated
  `terminal_id` + `queue_token` (read capability, localStorage, hashed
  server-side) + server-issued `daemon_token` (write capability, 0600
  config) bound by a **browser-minted, single-use, short-TTL pairing code**
  consumed by `--pair` (getpass-style prompt, no argv). Better than my
  scheme (my register-is-loopback + daemon-minted code had a 409-recovery
  awkwardness: a lost daemon secret needs staff intervention, and pairing
  required the daemon to be running) and better than kimi's (no 43-char
  copy-paste, capabilities separated). The threat-model paragraph (what a
  stolen pairing code can and cannot do) is exactly the honesty the security
  review needs.
- **Q4 — the two-tab lease (§3.3).** The correct fix for the problem my plan
  waved away as "one browser per terminal discipline": first poller holds a
  30 s renewable lease, others get `suspended` + zero events + a visible
  message. Small, testable, no loss. Adopt.
- **Q5 — `event_id` idempotency** (`UniqueConstraint(terminal_id, event_id)`,
  `duplicates` counter, client LRU): makes daemon retries harmless — the
  piece both my plan and kimi's lack. Adopt (server side at minimum).
- **Q6 — wrong-device guards (§4.2).** The catastrophic failure mode of a
  grab daemon is silently disabling the *human* keyboard system-wide. The
  four guards + `--allow-any` opt-in (the `--any-port` doctrine) + always
  logging the grabbed name is the right defense. My plan had VID/PID only.
- **Q7 — decoder robustness (§5):** `O_NONBLOCK` + `select` (interruptible
  for heartbeat/shutdown), partial-read reassembly, `KEY_KPENTER` (96→40 — a
  real edge none of us others had), the **idle-discard guard** (150 ms
  silence with a non-empty buffer ⇒ discard the partial: a truncated burst
  must never be delivered as a barcode) and `MAX_SCAN_LEN`. My plan's
  "unmapped keycode → reset buffer" covered one corruption path; qwen
  covers the truncation path too.
- **Q8 — header separation + `feedFetch()`** so the feed's 401s can never
  trip `api()`'s global logout cleanup. My plan's `api(path,{token,raw})`
  foreign-token form is in fact safe (the 401 cleanup checks `!foreign`,
  and `raw:true` bypasses it) — but qwen's separation is more obviously
  safe and pinnable. Adopt (either is defensible; prefer the explicit one).
- **Q9 — at-most-once cursor (persist on receipt, before processing).** My
  plan chose at-least-once (advance after `drainScanQueue()`), arguing a
  silent loss is worse than a duplicate. Qwen's argument is better, and my
  claim was unsound anyway: `drainScanQueue` *toasts* per-scan API errors
  and drops the scan regardless, so my "at-least-once" did not actually
  redeliver — it just added cursor-write latency for nothing. A lost scan is
  re-scannable and visible at checkout; a duplicate line is a data error;
  and with Q5's idempotency the duplicate vector is otherwise closed. Switch
  my plan to at-most-once.
- **Q10 — invariant list (§8), fallback matrix (§10), hardware checklist
  (§9), smoke-test round trip in CI (T7).** The checklist is mapped to
  invariants (including the kill -9 typing-resumes proof and the
  browser-unfocused badge login — the round's headline); the fallback matrix
  is the task's fallback question answered as a table. Adopt all three.
- **Q11 — non-US layout byte-exactness**: with the grab, decoding is
  US-HID-semantics regardless of the X layout — a genuine improvement over
  the wedge path (which mangles `!`/`-`/`:` on non-US layouts today). Worth
  the README line and checklist item 11.

### Weaknesses / risks

- **Q-W1 — scope.** Seven tasks carrying a lease, a probe-404 state machine,
  visibility-based cadence, a client-side LRU dedup, three custom headers,
  `--no-grab`, `--release-on-server-down` (their own Q6 says don't ship it),
  and optional healthz counters (Q7). Trim: **drop the client LRU dedup**
  (the server's unique constraint + per-tab cursor + lease already prevent
  every duplicate the LRU would catch), **drop the probe-404 protocol**
  (a first poll that 404s *is* the unpaired signal), keep the lease (small,
  closes a real correctness bug), keep visibility cadence (three lines with
  `document.visibilityState`). Every kept ornament must earn its test count.
- **Q-W2 — anonymous terminal registration.** Anyone who can reach the
  server can mint terminal rows (bounded only by row-reaping). On the
  default loopback bind this is moot, but `run.sh` explicitly supports
  `HCRM_HOST=0.0.0.0`. My plan's loopback gate was better posture; the fix
  that keeps qwen's bootstrap story intact: **registration accepts either a
  loopback client OR any authenticated user** (one small dependency). The
  logged-out loopback kiosk self-provisions exactly as qwen designed; a LAN
  kiosk needs one member login once, ever. Qwen's Q2 staff-gated *pairing
  mint* can stay an optional hardening flag on top.
- **Q-W3 — client-generated `queue_token` is a credential chosen by the
  client.** Acceptable (it is high-entropy, stored hashed, and the
  anti-hijack 409 prevents stealing an existing terminal), but worth stating
  as an accepted risk rather than presenting self-provision as unqualified
  virtue.
- **Q-W4 — R6 overclaims**: "the wedge capture swallows bursts …
  `event_id` dedup" — the wedge path carries no `event_id`; a scan split
  across the grab↔wedge transition is bounded by round-3's shape gates, not
  by dedup. Minor wording fix; the risk itself was in my plan too.
- **Q-W5 — `terminal_id` TEXT PK (client uuid)**: house style is int PK +
  unique string (`User.email`, `Item.sku`). Cosmetic; align.
- **Q-W6 — pairing code 6 digits + lockout**: ~20 bits in a 5-minute window
  with no transport security. The 5-wrong lockout helps but is itself a
  DoS knob on pairing. Prefer an 8-char Crockford-base32 code (~40 bits) as
  my plan had, with the lockout optional. (Both are defensible; 40 bits
  removes the need for the lockout.)
- **Q-W7 — 600 s TTL** is long for kimi's "must not pop up after lunch"
  invariant (I3) and short for nothing. 300 s splits the difference; the
  value is env-tunable anyway (`HCRM_SCANNER_EVENT_TTL_SECONDS`).

### Disagreements (qwen vs mine)

- **Cadence:** qwen (400 ms client-paced, gate-proven) over mine (1 s) —
  adopt qwen's.
- **Binding:** qwen's capability pair over my secret/token register scheme —
  adopt, plus my registration gate (Q-W2) and kimi's rotate (K2).
- **Cursor semantics:** qwen's at-most-once over my at-least-once (Q9).
- **Classification:** qwen/kimi's shared module + server re-derivation over
  my "SPA classifies, server stores verbatim" (K1/K6).
- **Naming:** concede `/api/scanner/*` (both peers) over my `/api/terminals`.

---

## 3. Adjudication of the five focus areas

### (a) Channel: cadence and read semantics

**Verdict: qwen.** Client-paced short poll (400 ms live / 2 s degraded/
unpaired / 5 s hidden), **broadcast reads with a per-tab cursor persisted on
receipt** (at-most-once), **server-side `event_id` idempotency** on ingest, a
**30 s renewable per-tab lease** for the two-tab case, and **no server-side
hold ever** (I7, pinned by contract test + ui_check wall-time criterion).
Kimi's long-poll is disqualified by M9 (measured red gate); kimi's
consume-on-read is disqualified by its two loss windows (K-W2) and the
missing idempotency (K-W3). My 1 s broadcast+cursor was the right family but
qwen's parameters, lease, and idempotency are strictly better; my at-least-
once cursor variant is withdrawn (Q9).

### (b) Counter-session binding

**Verdict: qwen's skeleton + two imports.** Browser-generated
`terminal_id`+`queue_token` (read), server-issued `daemon_token` (write),
browser-minted single-use pairing code consumed by the daemon's `--pair`.
Imports: **registration gated to loopback-or-authenticated** (mine, Q-W2) —
qwen's anonymous register otherwise opens row-minting on LAN binds; and
**staff list/rotate/delete + minimal admin UI** (kimi K2) as the recovery
and ops surface. Pairing code: 8-char base32 (Q-W6). Kimi's single shared
token is rejected (K-W4).

### (c) Badge-payload handling

**Verdict: all three plans independently chose variant 1a** (raw payload in
a TTL+cap-bounded queue; the SPA needs the raw payload to exchange at
`/qr-login`). Adopt the consensus hardening set: TTL 300 s + per-terminal
cap 200 with lazy reaping (I3: no scan may land in a later member's cart),
hash-everything-else (`queue_token`, `daemon_token`, pairing code via
`token_hash`), `redact_badge` in every daemon print path, **never echo a
code in any error envelope**, 401s with no existence leak. Qwen's variant 1b
(server resolves the badge, queues an `adopt_token`) stays a documented
deferred alternative with its cost written down (a device token that can
mint member sessions is a security *downgrade*, plus a second switch path) —
do not build it.

### (d) EVIOCGRAB mechanics / lifecycle / fallback

**Verdict: full consensus, plus qwen's guards.** All three plans agree on:
stdlib `fcntl.ioctl(fd, 0x40044590, 1)`, grab-lifetime == fd-lifetime
(kill -9 ⇒ typing resumes; no cleanup protocol), EBUSY ⇒ loud message,
re-discovery by VID/PID on replug (M2: never trust an `eventN` number or —
M3 — a `by-id` symlink), the one udev `input` line (keep my
`KERNEL=="event*"` narrowing), and the fallback = round-3
`scancapture.js` unchanged and still installed, with a status chip making
the live mode visible. Adopt additionally from qwen: wrong-device guards +
`--allow-any` (Q6), `O_NONBLOCK`+`select` + partial-read reassembly,
`KEY_KPENTER`, idle-discard + `MAX_SCAN_LEN` (Q7), `--dry-run`/`--diagnose`,
single-instance lockfile, and the layout-independence note (Q11). Adopt
kimi's dual-format struct testing (K3, with `i` not `I`). Daemon-down-
server-down: in-memory bounded buffer (drop-oldest, loud), event_id
idempotency makes retries safe; kimi's durable spool documented as the
upgrade (K4).

### (e) Task ordering

**Verdict: classification first (both peers), then the API contract, then
daemon pure-core → grab, then SPA, then ops/verification.** My original
order (API → daemon → ops → SPA → verification) got the contract-first
instinct right but missed the shared-classification prerequisite that both
peers lead with. Qwen's parallel DAG (T2/T3 ∥ T4) is correct as a dependency
graph but this repo's rounds are serial with cross-review; a linear order
with noted parallelism is more reviewable.

---

## 4. Final recommended architecture (synthesis)

- **Router** `app/routers/scanner.py`, prefix `/api/scanner` (concede naming
  to the peers). Endpoints: `POST /terminals` (register+heartbeat; loopback
  **or** authenticated user; anti-hijack 409), `POST
  /terminals/pairing-code` (queue-token auth; 8-char code, single-use,
  5-min TTL), `POST /pair` (code → `daemon_token`, shown once),
  `POST /scans` (daemon-token; 1–32 events, `event_id`-idempotent,
  server re-classifies via `app/scanshapes.classify_scan`, unshaped
  rejected), `GET /scans?after=&limit=` (queue-token + session lease; no
  hold, ever), `POST /terminals/unpair` (queue-token; kills daemon_token),
  staff `GET /terminals`, `POST /terminals/{id}/rotate`, `DELETE
  /terminals/{id}`.
- **Tables** `ScannerTerminal` / `ScanEvent` (int PKs, house style; new
  tables ⇒ no migration entries — pinned by kimi's K7 boot test).
- **Queue semantics**: broadcast, cursor = `scan_events.id` per tab,
  persisted on receipt (at-most-once), TTL 300 s + cap 200 lazy-reaped,
  lease 30 s renewable, `daemon.online` from `last_seen` (10 s heartbeat,
  30 s window).
- **Daemon** `scripts/scanner_daemon.py`: pure decode layer (flat
  KEY→HID-usage table → synthetic boot reports → the *unmodified*
  `ScanAssembler`; KPENTER, idle-discard, MAX_SCAN_LEN, autorepeat ignored),
  sysfs discovery (root-injectable for tests, no by-id dependency),
  wrong-device-guarded grab (`O_NONBLOCK`+`select`), re-discovery on
  ENODEV with backoff, lockfile, `--pair`/`--dry-run`/`--diagnose`/
  `--allow-any`, bounded in-memory retry buffer, `redact_badge` everywhere,
  0600 token config, systemd unit + the udev `input` line.
- **SPA**: `static/scanfeed.js` (pure reducer, node-tested), adapter
  `installScanFeed()` beside `installScanCapture()` (which is NOT removed —
  the fallback), `feedFetch()` with non-Authorization headers, adaptive
  cadence 400 ms/2 s/5 s, `enqueueScan(code, kind)` as the only sink,
  status chip (`data-scanner-feed="live|off|unpaired"`), pairing panel on
  the login screen, unpaired hint (K5), minimal staff terminal panel.
- **Verification**: qwen's invariants I1–I9 (with I2 reading
  "at most once per session via lease+cursor+idempotency"), smoke-test
  hardware-free round trip in the CI `scripts` job, hardware checklist in
  `docs/plans/r4-checklist.md` once the scanner is re-attached (M1),
  README/NOTES sweep.

### Final task order

1. **T1 — shared scan shapes**: `app/scanshapes.py`, `auth.py` delegation,
   `scanner_bridge.redact_code` re-export, JS-side comment, parity test +
   shared vectors (kimi A1 / qwen T1). *Both peers' first task; my plan's
   missing piece.*
2. **T2 — queue API** (models, schemas, router, wiring; the contract both
   later halves test against). Includes the lease, idempotency,
   re-classification, TTL/cap, registration gate, staff rotate.
3. **T3 — daemon decode core** (pure, offline): keycode table, decoder,
   discovery-against-fake-sysfs, struct dual-format, classification hookup.
4. **T4 — grab + forwarding + packaging**: guards, lifecycle, lockfile,
   pairing, buffer, udev line, systemd unit, runbook.
5. **T5 — SPA feed**: scanfeed.js + adapter + pairing UI + chip + pins
   (including the no-hold contract test and the ui_check wall-time
   criterion).
6. **T6 — staff terminal panel + docs** (kimi D2, trimmed; README/NOTES/
   opticon docs).
7. **T7 — verification**: smoke round trip in CI, hardware checklist
   (blocked on M1), fallback matrix into the docs.

(T3 and T5 are independent after T2 if the round runs parallel tracks; the
repo's serial-with-review convention suggests the linear order.)

---

## 5. Changes to my own plan (r4plan-glm.md) — the honest ledger

1. **Withdrawn: at-least-once cursor (D4).** Switch to persist-on-receipt
   at-most-once (Q9) — my drain-based variant added latency without actually
   guaranteeing redelivery (`drainScanQueue` toasts and drops per-scan
   errors), so it was the worst of both.
2. **Withdrawn: my registration/pairing scheme (D3).** Replaced by qwen's
   browser-self-provision + browser-minted code + `daemon_token`, plus my
   loopback-or-authenticated registration gate (the one piece of mine that
   survives) and kimi's staff rotate.
3. **Withdrawn: "SPA classifies, server stores verbatim" (D2).** Replaced by
   the shared `app/scanshapes.py` + server re-derivation + parity pins
   (K1/K6).
4. **Withdrawn: `/dev/input/by-id` fast path** (D5 discovery) — M3: it does
   not exist on this host. Sysfs walk only; by-id opportunistic at most.
5. **Withdrawn: `/api/terminals` naming** — concede to `/api/scanner`
   (both peers).
6. **Adopted from qwen**: no-hold invariant + contract test + wall-time
   criterion (M9/M10); adaptive poll cadence; two-tab lease; `event_id`
   idempotency; wrong-device guards; `O_NONBLOCK`+`select`; partial-read
   reassembly; `KEY_KPENTER`; idle-discard + `MAX_SCAN_LEN`;
   `--dry-run`/`--diagnose`; lockfile; feedFetch header separation;
   invariants/fallback-matrix/hardware-checklist/smoke-round-trip; layout-
   independence note; "never echo a code in an error envelope".
7. **Adopted from kimi**: staff terminal management incl. rotate (+ trimmed
   UI); dual-format struct testing (with `i`, not `I`); the `create_all`
   boot pin; the unpaired-kiosk hint; durable spool documented as the
   upgrade over the in-memory buffer.
8. **Kept from mine (surviving peer pressure)**: loopback-or-authed
   registration gate; 8-char pairing code; `KERNEL=="event*"` in the udev
   line; TTL 300 s (between kimi's 120 s and qwen's 600 s); the
   purge/hashing/never-log doctrine (consensus anyway); the general
   task-review shape (now re-ordered per §4).
9. **Corrected fact**: my plan asserted the by-id symlink was usable
   "as a fast path" and leaned on `event14` from the round-3 record; M2/M3
   show both are unreliable on this host today.
