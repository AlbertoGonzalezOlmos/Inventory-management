# REVIEW — `feature/opticon-m10-scanner` (1e03f93)

> Full review of the Opticon M-10 integration (app/scanner/*, scripts/scanner_bridge.py,
> docs/opticon-m10.md, tests/test_scanner.py) against `origin/main` @ e28b7ec.
> Method: complete code read, offline test run, live end-to-end demos against a scratch
> server (`HCRM_DATA_DIR=$(mktemp -d) HCRM_EMBED_MODEL=bogus/model`, per the repo golden
> rule), and independent verification of every cited Opticon source. Reviewer's context:
> author of the sibling branch `feature/opn2001-scanner-connection` (PR #2) and of the
> live hardware survey of this machine (WSL2; Opticon `065A:A001` attached via usbipd as
> `/dev/hidraw0`; a second device failing enumeration on Windows, problem 43).

**Verdict: merge-worthy after the P1/P2/P4 fixes; P3 and P5 before it is used for stock
changes at any real volume.** Nothing here questions the value of the branch — it is the
only bidirectional scanner path in the repo — but two failure modes were *demonstrated
live* and one design race is codified by a test.

---

## 1. Source verification (all independent, 2026-09-29)

Every publicly checkable claim in `docs/opticon-m10.md` was verified against Opticon's
own published files (note: `files.opticonusa.com`/`wiki.opticonusa.com` serve a broken
TLS chain — use `curl -k`; the content is real):

| Claim | Source checked | Result |
|---|---|---|
| "M-10 is a 2D presentation scanner containing our MDI3100 scan engine" | wiki `/techsupport/en/M-10` | ✅ verbatim |
| USB-COM = CDC-ACM, **VID 065A PID A002**, Opticon driver required | Specifications Manual (SS13063, 2014) §18.5, downloaded PDF | ✅ verbatim |
| Factory-default label `@MENU_OPTO@ZZ@BAP@ZZ@OTPO_UNEM@` | SS13063 §18.1 | ✅ verbatim |
| Default suffix **CR**; buffered mode | SS13063 §18.3 | ✅ verbatim |
| RS-232C default 9600 bps, no parity, 8 data, 1 stop | SS13063 §18.6 | ✅ verbatim |
| USB Full-speed 12 Mbps **(HID/COM)**, WVGA CMOS, ≤60 fps | M-10 datasheet PDF | ✅ |
| Linux: appears as `ttyACM0`, opens as any serial port | wiki `/techsupport/en/MDI3100` | ✅ |
| UMB / OptiConfigure / USB COM-Keyboard(HID)-RS232 restore sheets | wiki M-10 page | ✅ |
| `Z1`/`Z2`, ACK/NAK/ESC (0x06/0x15/0x1B) | "MDI3100 Serial Interface" — **tech-support-only** document (wiki confirms its existence and family scope) | ⚠️ not publicly verifiable; the code/doc label these *family-documented*, which is the correct epistemic status. Keep the label; verify on hardware (Phase 4). |

Tests: `uv run pytest` → **62 passed** as claimed. The commit message's own e2e claim
(bridge on a pty pair) reproduces.

## 2. Strengths (keep)

- **Provenance discipline** — per-claim citations with honest "family-documented"
  labels; verified accurate (§1). This is the standard the repo should keep.
- **Layering** — protocol (pure) / transport (pyserial-optional, termios fallback) /
  driver. Stdlib-first respects the frozen-`uv.lock` offline constraint; pyserial stays
  out of dependencies.
- **Tests** — 22 offline tests; the pty-based driver tests (NAK/ESC/timeout/interleave)
  exercise real serial semantics without hardware.
- **Self-aware docs** — §6 already flags the non-atomic stock adjustment and proposes
  `POST /api/items/{id}/stock-adjust` as the fix.

## 3. Findings

### P1 — HIGH · demonstrated: fuzzy match + stock adjust corrupts stock

`scanner_bridge.find_item()` falls back to `items[0]` of a `q=` ILIKE search
(name/description/sku substring), and `on_scan` applies `--stock-in/--stock-out` to
whatever came back — including `match="search"`.

**Live demo (this review, scratch server):** scanning the string `water` — not a
barcode, not a SKU — returned `{"found": true, "match": "search", ...}` and decremented
*Insulated Water Bottle* stock **60 → 59**. Any partial word, any mis-scan, any label
fragment silently mutates inventory of an unrelated item.

**Fix:**
1. Exact-identity matches only by default (SKU equality; GTIN equality via P5).
2. Fuzzy fallback behind an explicit `--fuzzy` flag, and **never** combined with stock
   adjustment (hard error if both are requested).
3. Unmatched codes report `found: false` (they already do — the fallback is the only
   leak).

### P2 — HIGH · demonstrated: any HTTP error silently kills ingestion (zombie bridge)

`scanner_bridge.api()` raises `SystemExit` on `HTTPError`. `SystemExit` is a
`BaseException`; `OpticonM10._loop` only catches `Exception` around `on_scan`, so the
**reader thread dies** while the main loop keeps sleeping in
`while True: time.sleep(3600)`.

**Live demo:** injecting `SystemExit` into `on_scan` → `scanner._reader_thread.is_alive()
== False`; the process still prints "Listening". A threading traceback goes to stderr,
easy to miss in a back office.

Triggers in real operation: **401 after the 7-day token TTL** (`TOKEN_TTL =
timedelta(days=7)`, `app/security.py`), any transient 500 (this repo has a documented
"database is locked" incident class), a 409. For an always-on counter bridge this is a
*when*, not an *if*.

**Fix:**
1. `api()` returns `(status, detail)` on HTTPError instead of raising `SystemExit`;
   `SystemExit` only from `main()`.
2. The scan callback catches per-scan errors, prints them loudly (stderr + JSON record
   `{"code":…, "error":…}`), and continues.
3. Harden `m10._loop`: catch `BaseException` (except `KeyboardInterrupt`) around
   `on_scan` and route to `on_error`; a callback must never be able to kill the loop.
4. Optional: `--relogin` on 401, or exit non-zero with a clear message (fail loudly) —
   but never fail silently.

### P3 — MEDIUM-HIGH · design: two readers race one port; scans dropped while awaiting ACK

`send_command()` reads the port directly while `start_reading()`'s thread may also be
reading it: either can steal the other's bytes (ACK eaten by the scan thread →
spurious `TimeoutError`; a frame eaten mid-bytes by `send_command` → corrupt/lost scan).
Worse, `send_command` **discards** every non-response byte it reads — i.e. scanned
barcode data arriving during the ACK window is silently lost (debug-log only), and
`test_interleaved_scan_data_ignored_while_awaiting_ack` *codifies the loss as expected
behaviour*. The device default is **buffered mode** (SS13063 §18.3) — interleaving is
the normal case, not the corner case.

**Fix (single-reader demux):** one loop owns the port; while a command is pending,
single bytes 0x06/0x15/0x1B resolve the waiter (`queue`/`Event`), everything else feeds
the `ScanParser`. `send_command` becomes "publish command + await result". Update the
interleave test to assert the scan is **delivered**, not ignored.

### P4 — MEDIUM · wrong-device hazard on a machine that owns both scanners

`find_scanner_ports()`' fallback lists *any* `ttyACM*/ttyUSB*/COM*` (non-M10 ports
included, merely sorted last), and the bridge auto-opens `candidates[0]`. This shop's
other scanner is an **OPN-2001** (`065A:0009`, serial 9600 **8O1**, binary RBBV
protocol): with it on `/dev/ttyUSB0` and no M-10 attached, the bridge opens the wrong
device at the wrong parity and interprets binary protocol bytes as "barcodes".

**Fix:** strict PID filter by default (only `065A:A002`), `--any-port PORT` as explicit
opt-in; refuse auto-pick with a clear message when a *different* Opticon personality is
detected. Better: share one detection utility with the opn2001 branch's `detect`
(it already maps `0009`/`A001`/`A002` and the WSL2/usbipd specifics) — one hardware
map for the repo instead of two.

### P5 — MEDIUM · strategy conflict with PR #2: "put the barcode in the SKU field"

Doc §6 and the bridge design assume the product's EAN/UPC lives in `items.sku`. PR #2
(`feature/opn2001-scanner-connection`, open against main) adds the proper home:
`items.barcode` (canonical **GTIN-14**, unique, migrated), `app/barcodes.py`
(check-digit validation, UPC-E→UPC-A→GTIN-14 normalisation, GS1 classification),
`GET /api/items?barcode=` exact lookup, and `scripts/scan_intake.py` (same-item /
new-item / misread classification). The SKU convention actively breaks under that model:

- same product's UPC-E and EAN-13 forms become **two different SKUs → two entries**
  (GTIN normalisation exists precisely to prevent this);
- no check-digit validation — a misread becomes a phantom SKU (PR #2: 422/quarantine);
- SKU namespace pollution: internal identifiers and global identities conflated.

**Fix:** merge PR #2 first; rebase this branch; bridge lookups go through
`app.barcodes.analyze_scan` + `?barcode=` (GTINs) with SKU equality as the opaque-label
path; delete the "barcode in SKU" convention from doc §6 and the troubleshooting row
that references it. Unknown GTINs should surface as "new entry" candidates (reuse
`scan_intake`'s reporting).

### P6 — LOW · hygiene

- `--password` on the command line (process list / shell history); default to the
  getpass prompt, warn when the flag is used.
- Token lifetime: no re-login/refresh story for an always-on bridge (compounds P2).
- `stock-out` clamps at 0 silently (`max(0, old+adjust)`) — a sale beyond stock looks
  like a no-op; report the clamp in the record/stderr.
- `_PosixSerialPort` never restores the previous termios state on close; no exclusive
  open (two bridges can fight over one port).
- `ScanParser` `.strip()`s frames — payloads with meaningful leading/trailing spaces
  (legal in Code 128) get altered; fine for GTINs, document it.
- `test_commands_require_open` asserts `pytest.raises(Exception)` — assert
  `SerialOpenError`.
- `import time` inside the bridge's while loop (style).
- `stock` read-modify-write race is documented (§6) — acceptable for now; the proposed
  `stock-adjust` endpoint stays the right long-term fix (pair it with PR #2's future
  `POST /api/stock-count`).

### P7 — LOW · cross-branch doc accuracy (reviewer's own branch)

`docs/scanner-opn2001.md` (PR #2) labels `065A:A001` as "M-10 family, HID personality".
A001 appears in **no** public Opticon document checked (SS13063 lists only A002 for
COM; §18.4 USB-HID lists no PID). It is what the physically attached device reports.
Relabel in PR #2 as "observed live; *likely* the M-10 HID personality — proving it is
one hardware step: scan the *USB COM Port* sheet and check the device re-enumerates as
`065A:A002`".

### Branch mechanics

> ⚠ **This subsection was superseded before it was ever acted on** — it
> described the branch as of `3776405`, and `main` has since absorbed the
> whole round-5 remediation (PR #1). Both of its claims turned out to be
> false at rebase time. Kept verbatim as the record; see **§5 Addendum** for
> the measured replacement.

- Based on `3776405`: **2 commits behind main** (misses `0d99984` W4.0 dev-admin,
  `e28b7ec`). Footprint is README-only overlap → rebase expected clean.
- No collision with PR #2's files; P5 is a semantic, not textual, conflict.

## 4. Proposed plan

| Phase | Content | Depends on | Effort |
|---|---|---|---|
| **0. Sequence** | Merge PR #2; rebase this branch onto main | — | minutes |
| **1. Safety** | P1 exact-match-only bridge (+`--fuzzy` opt-in, banned with adjust); P2 `api()` returns errors, callback resilience, `_loop` BaseException guard, loud failure; P4 strict `A002` filter + shared detection | 0 | ~0.5 day |
| **2. Driver** | P3 single-reader demux; command-waiter queue; flip the interleave test to assert delivery | 1 | ~1 day |
| **3. Integration** | P5 bridge on `app/barcodes` + `?barcode=` + new-entry reporting via `scan_intake`; docs cross-reference `scanner_hid.py` for the HID personality (the physically attached device!); P7 relabel in PR #2 docs | 0–2 | ~1 day |
| **4. Hardware verification** | With the physical unit: scan the *USB COM Port* sheet → expect re-enumeration `A001→A002` (also settles P7); then on a real port: `Z1`/`Z2` ACK behaviour, menu-envelope ACK, CR suffix, buffered-mode interleave during an ACK window; record results as a dated addendum in doc §9 | device + printed sheets | ~1 h |
| **5. Tests/docs** | Bridge unit tests (find_item matrix incl. fuzzy-off, callback raising SystemExit, token-expiry path), README/NOTES touch-ups | 1–3 | ~0.5 day |

**Pushbacks, explicitly:**
1. Do not ship `--stock-in/--stock-out` with the fuzzy fallback (P1) — demonstrated
   stock corruption.
2. Do not keep the "barcode in the SKU field" convention (P5) — PR #2's `items.barcode`
   supersedes it; adopting both creates two conflicting identities per product.
3. The interleave test's expectation is wrong (P3): losing scans is not behaviour to
   pin, it is the bug to fix.

---

## 5. Addendum (2026-09-30) — what the rebase actually cost, and status of each finding

Written when the branch was rebuilt on current `main` (`fbecbe9`, i.e. after
PR #1) on top of the restructured scanner work. Everything below is measured,
not predicted: the rebuild was run, and the suite was executed forward and in
reverse node-id order at every stage.

### 5.1 Scope of this review

This document reviewed **`1e03f93` only** — the driver, bridge, docs and their
22 tests. The QR-badge commit (`eeeeb4e`, preserved on
`archive/m10-qr-eeeeb4e` and replayed here as the branch's second commit) was
written *before* this review and was **not covered by it**: it adds two auth
endpoints, a bearer-credential lifecycle, a 907-line vendored QR encoder and
SPA changes. It is now explicitly in the review scope of this branch's PR, and
one defect from it is corrected in §5.3.

### 5.2 Branch mechanics, measured

| Claim above | Measured |
|---|---|
| "2 commits behind main" | **16** commits behind (`git rev-list --count <branch>..origin/main`) — round 5 landed in full |
| "Footprint is README-only overlap → rebase expected clean" | **4 files conflict** with `main`: `README.md` (project layout *and* the `healthz` payload — taking this branch's side would have re-documented `{ok, vector, embeddings}` and dropped `insecure_dev_admin`/`pbkdf2_iterations`, which `tests/test_metadata_and_ops.py` gates), `app/main.py` (`_migrate_schema`: three branches each append a migration at the same anchor), `app/routers/auth.py` and `app/routers/members.py` (import unions: `is_well_known_password` from W4.1 vs `app.qrbadge`/`token_hash`) |
| "No collision with PR #2's files" | False: both branches touch `app/main.py`, `app/models.py`, `app/schemas.py`; `app/main.py` **conflicts textually** (the `items.barcode` migration and the `users.qr_badge_hash` migration land on the same lines). Only `app/main.py` needs a human; the other two auto-merge. |

One semantic break that no merge tool can see, found by running the suite:
`tests/test_qr.py::test_staff_manages_member_badge` logged in with the
hardcoded `password123`, but W4.2 changed `conftest.create_user` to return a
ready token *after* performing the mandatory password change — so that login
now fails and the test dies with `KeyError: 'token'`. Fixed in the rebuild by
using the returned token. **The 62-test count quoted in §1 is stale**; the
rebuilt branch runs 259 passed / 1 skipped.

### 5.3 Finding status after the restructure

| Finding | Status |
|---|---|
| **P1** fuzzy match + stock adjust corrupts stock | **open, blocking.** Documented loudly in `docs/opticon-m10.md` §6 so no operator meets it unaware; the fix (exact-identity only, `--fuzzy` opt-in, hard error when combined with `--stock-*`) is the next commit on this branch. |
| **P2** any HTTP error kills ingestion (zombie bridge) | **open, blocking.** Same treatment: fix next (`api()` returns `(status, detail)`; per-scan error capture; `BaseException` guard in `_loop`). |
| **P3** two readers race one port; scans dropped while awaiting ACK | **open.** Single-reader demux + flipping `test_interleaved_scan_data_ignored_while_awaiting_ack` to assert *delivery*. Largest of the five; scheduled after P1/P2/P4. |
| **P4** wrong-device hazard on a machine that owns both scanners | **the shared prerequisite now exists**: `scripts/opticon_detect.py` is the repo's one personality map (`find_linux_usb_devices` is importable with no pyserial present, and `describe()` refuses to guess an unknown PID). The bridge's strict-`A002` filter + `--any-port` opt-in still has to be wired to it. |
| **P5** "barcode in the SKU field" vs PR #2's `items.barcode` | **sequencing done, code pending.** The GTIN-14 identity layer is underneath this branch now (`app/barcodes.py`, `?barcode=`, normalisation, 409/422), and §6 of the guide warns against the old convention. The bridge still queries `q=`/`sku`; switching it to `analyze_scan` + `?barcode=` is in the same follow-up commit as P1. |
| **P6** hygiene (`--password` on the command line, silent stock clamp at 0, termios not restored, `pytest.raises(Exception)`, …) | **open**, non-blocking; folded into the follow-up where cheap. |
| **P7** `A001` labelled as documented fact | **applied once, in the canonical home.** `docs/opticon-hardware.md` §1 carries the claim with its provenance, `scripts/opticon_detect.py` carries `confirmed: False`, and `tests/test_opticon_detect.py::test_unconfirmed_personality_keeps_its_hedge` fails if the hedge is removed without the hardware evidence. This guide no longer asserts it either (§5.3's own §2 correction). The experiment that would settle it is written down in both places. |

### 5.4 Two things this review got wrong, so the next one does not

1. **"No collision with PR #2's files"** — measured false (§5.2). The lesson is
   not that the reviewer miscounted, but that a review's *mechanics* section
   decays the moment `main` moves, and it reads like the rest of the review
   (which does not decay) because both are prose in one file. A stale mechanics
   claim was subsequently repeated, verbatim, in a fresh analysis of this
   branch. Mechanics belong in a command, not a sentence: the numbers above are
   reproducible with `git rev-list --count` and a trial merge.
2. **P7 was aimed only at the sibling branch.** The same overclaim was already
   in *this* branch's guide, added by the commit this review did not cover.
   When a finding is about a *fact* rather than a file, the fix has to be
   applied everywhere the fact is stated — which is the argument for giving the
   fact one canonical home.
