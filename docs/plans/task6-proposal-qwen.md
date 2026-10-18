# Task 6 proposal (qwen3.8-max) — closing pass: security audit, docs, migrations, hardware checklist

Final task per `docs/plans/COORDINATOR.md`. The feature (auto QR badges,
scan-to-cart, kiosk logoff, attribution, ScanRequest queue, SPA flows) is
implemented and green across Tasks 1–5 (493 pytest, ui_check passing, M-10
HID personality works today; USB-COM blocked on the host fixes in
`SCANNER-AGENT-SYNC.md`). Proposal only — grounded in the current tree.

**Freshly verified this pass:** `scripts/scanner_bridge.py:347` prints the
full session token to stdout in text mode after a badge scan (and its
docstring advertises it); `tests/test_scanner_bridge.py` pins the
`badge_login()` *record* shape (`{…, "token": …}`) but **not** the text-mode
emit line — so redacting the printout is test-safe while `--json` and the
record contract stay untouched. `README.md:400/406` still claims "The
purchase list is client-side only" — **false since Task 1/5** (the repo has a
literal "README truth" commit culture, `1245085`). No test anywhere asserts
that `cart_items` / `scan_requests` are created by `create_all()` on a
*legacy* database (the Task-1/3 shape pins were proposed but never landed;
`test_migrations.py` only covers COLUMN migrations + the Task-4 NULL pin).
`scripts/smoke_test.py` has **zero** cart/scan coverage. `static/app.js`
staging persists `{code, symbology}` only (no secrets); `app/routers/*` log
nothing (no logger calls near payloads); `docs/opticon-m10.md` ends at §10
(the §11 slot is free); `docs/barcodes.md` has no suggestion-rules section.

---

## Part 1 — Security & edge-case audit

Method: one PR, two **fixes** (below), everything else verified by
grep/test and recorded as a short audit table in `docs/opticon-m10.md` §11
(or accepted here). Each line states the expected verdict so the implementer
*proves or fixes*, never assumes.

### FIX-1 (genuine, small): redact the bridge's text-mode token printout

Since Task 2 **every** account carries a badge, so a counter bridge in its
documented badge mode now prints live 7-day bearer tokens to stdout —
terminal scrollback, `| tee`, journald, CI logs. The headless use case is
real (docstring line 25), so don't remove it — gate it:

- `scripts/scanner_bridge.py`: text-mode `emit()` prints
  `session token: 3f9a1c2d… (redacted — --show-token or --json for the full value)`;
  new flag `--show-token` restores the full print; **`badge_login()`'s record
  and `--json` output unchanged** (machine consumers are the point of JSON,
  and the pinned record contract stays byte-identical).
- Startup: when a badge scan first occurs without `--show-token`, one stderr
  note ("token redacted in text mode"). Loud, not silent — house style for
  anything credential-shaped (cf. `HCRM_DEV_ADMIN` announcements).
- `docs/opticon-m10.md` §6a/§11 + `--help` text updated.
- Tests (`tests/test_scanner_bridge.py`): emit-level test (capture stdout)
  — redacted by default, full with `--show-token`, `--json` carries the
  token; existing record-shape tests untouched.

### FIX-2 (README truth): correct the stale tracked-issues claim

`README.md` "Tracked issues": the row and bullet saying the purchase list is
client-side only are now false. Rewrite: *checkout* remains the tracked
non-goal (the list itself is server-side and per-user since Task 1; scans,
switch-save and attribution build on it). Same pass: feature-section updates
per Part 2.

### Audit table (verify each; expected verdict in brackets)

| # | Surface | Check | Expected |
|---|---|---|---|
| A1 | Badge payload exposure | grep every `qr_badge`/`payload` producer: register/create_member/rotate responses only; never logged (no logger/print in `app/routers/*`), never in a URL/query (POST bodies only — grep `params=`/`?token`), SPA holds it in memory only and nulls on close (`badgePrint`/`memberBadge` watchers) | clean; pin with the existing leak-inventory test (Task 2 T4) |
| A2 | Enumeration | `qr-login` uniform 401 (pinned); `qr-identify` one bit about the *caller's own* hash, uniform false for garbage/other badges (pinned); `suggest`/`items` reads authenticated | clean, tested |
| A3 | Logoff residue | reject-and-delete on presentation (pinned); never-presented armed rows are inert and bounded by the `expires_at` purge ≤7 d (Task-4 decision — **document**, don't extend the purge); no index on `logoff_deadline` (documented rationale) | accepted-as-designed |
| A4 | Cart races | single-statement capped upsert (8- and 3-thread pins); PUT all-or-nothing incl. overlapping-line regression; SPA serialises mutations through `_cartOp` and flushes before switch | clean, tested |
| A5 | ScanRequest lifecycle | 400 badge / 422 misread / 409 already / 409 duplicate-open / non-open 409s; weak-ref `resolved_item_id` survives item delete; `delete_member` cleanup same-txn; staff-only queue endpoints | clean, tested |
| A6 | Staging contents | `STAGING_KEY` holds `{code, symbology}` only — codes are not secrets (they were just scanned in public); cap 50; no tokens/user data | clean (grep-verified this pass) |
| A7 | `api()` token paths | `raw:true` never throws *and* never fires the global 401 cleanup; `token:` (foreign) never fires the 401 redirect — required by the switch flow's old-token revoke; default path attaches `err.status`/`err.detail`; 401 whitelist unchanged | clean; one manual trace of `switchAccount` step 5 with a dead old token (raw 401 → ignored) |
| A8 | `@media print` leak | `printing-badge` body class set/cleared by watchers on **both** badge modals; `.no-print` hides controls. **Known edge (Task-5 verdict observation):** `badgePrint` and `scanIntake` can be open simultaneously (register + staged-unknown replay) and both backdrops print. **FIX (3 lines, recommended):** when `badgePrint` is set while `scanIntake` is open, unshift `scanIntake` back onto `intakeQueue` and null it — the badge card (a credential artefact) gets print priority and the intake reopens on close. Otherwise: document | fix |
| A9 | CSP / v-html | headers untouched (test_headers green); the only sinks render server-generated SVG pinned inert (`test_badge_svg_is_safe_for_the_v_html_sink`) | clean |
| A10 | Gate coverage | every new endpoint behind `get_current_user` with the `must_change_password` 403 pinned (cart, scan, suggest, requests, identify, schedule/cancel — none added to the exempt set except as specified: none) | clean, tested |
| A11 | Query-string logs | no credential ever travels in a GET query (badge/token are POST bodies; `suggest?q=` carries only search text) | clean (grep) |
| A12 | Seeds | seeded admin + dev admin badge-less in both boot modes (pinned Task 2) | clean, tested |
| A13 | Bridge `--json` consumers | unchanged record contract (pinned) — the FIX-1 redaction is text-mode-only | clean after FIX-1 |

Anything the audit *finds* beyond FIX-1/2/A8: fix in the same PR if small,
else record in §11's "accepted risks" with a rationale (the repo documents
its risk acceptances inline — `app/database.py` D4 style).

## Part 2 — Docs

### `docs/opticon-m10.md` — new §11 "Kiosk / counter flows" (before References)

Outline (~60–80 lines, cross-linking §6a/§9 rather than repeating):
1. **Two transports**: USB-HID direct (works today, no setup: the SPA's
   global capture + the visible scan field + the login badge field) and
   USB-COM bridge (current modes: exact-identity lookup, stock adjust,
   badge→token with the new redaction; the *kiosk relay/counter-session*
   design is Phase B — **deferred, not built**; don't document what isn't
   there).
2. **Global capture contract**: <50 ms inter-key + Enter + shape gate
   (badge forms / 6–14 digits / ≥4-char opaque with a digit), sink fields
   stand down, editable targets are observe-only with pre-burst restore
   (nothing is ever eaten), modal discipline (barcodes dropped, badges
   always process), the visible field as the deterministic path.
3. **Badge flows**: same-user → `qr-identify` → `logoff-schedule` → 30 s
   countdown; **the server deadline is the truth** (lazy expiry in
   `get_current_user` kills the token even if the browser died — the
   countdown UI is presentation); cancel/restart semantics; flagged
   accounts (403 → change password first, by decision).
4. **Save-then-switch**: order (flush → qr-login → cancel → save →
   revoke old → load), why the new session is secured first (D3), carts
   are durable by construction.
5. **Barcode flows**: added / misread-422 / unknown-404 → intake modal
   (classification line, suggestion groups, staff create-and-resolve vs
   member ScanRequest), attribution staging + replay, the staff Requests
   tab, `/mine`.
6. **Config & ops**: `HCRM_LOGOFF_GRACE_SECONDS` (default 30, clamped ≥1,
   import-time); badge print isolation; bridge `--show-token`; the audit's
   accepted-risks table (A3 residue, PUT last-writer-wins, staging is
   codes-only, one-char burst leak into focused fields).
7. Pointer to the hardware checklist (Part 4) and `SCANNER-AGENT-SYNC.md`
   for the current host state.

### `README.md`

- **Env table**: `HCRM_LOGOFF_GRACE_SECONDS` row (default 30; kiosk logoff
  countdown; import-time; clamped ≥1).
- **Features**: bullets for scan-to-cart (server-side per-user cart,
  M-10 HID capture anywhere on the page), kiosk logoff countdown,
  save-then-switch, unknown-item intake + member ScanRequest queue,
  auto-badge at account creation (extend the existing Task-2 bullet).
- **Tracked issues**: FIX-2 rewrite (checkout still open; the list itself is
  server-side now) + remove the stale "client-side only" bullet.
- Quick-start/usage: one paragraph "Scanning at the counter" pointing at
  §11.

### `docs/barcodes.md` — new section "Closest-item suggestions"

The ranking rule as implemented (`rank_prefix_siblings`): indicator-stripped,
leading-zero-stripped 13-digit view; shared significant prefix ≥ 6; longest
first, name tie-break; **why** (case/pallet GTIN-14 unites with its retail
GTIN-13; padding never creates siblings; true company-prefix length needs
GEPIR, so observed shared length *is* the offline heuristic); keyword group
= the house escaped-ILIKE `?q=` dialect (one implementation,
`keyword_filter`); semantic group degrades to `[]` (never 503 — decision 7)
and never runs on the raw-code 404 path; misreads are **never** suggested
around (422, no groups). Cross-ref `app/barcodes.py` docstrings.

### `docs/scanner-opn2001.md` / `docs/opticon-hardware.md`

No changes expected; one cross-link each to §11 if the audit finds drift
(cheap grep during the pass).

## Part 3 — Migrations check

**Verify (all currently true — re-prove, don't assume):**
- `COLUMN_MIGRATIONS` exact set = the four entries (`users.
  must_change_password`, `items.barcode`, `users.qr_badge_hash`,
  `auth_tokens.logoff_deadline`), append-only order, identifier-validated;
  `INDEX_MIGRATIONS` = `auth_tokens.expires_at` only; the exact-set test
  pins both (it *is* the check).
- New **tables** (`cart_items`, `scan_requests`, plus Task 1's shape) need no
  entries — `create_all()` creates them on fresh *and* legacy DBs.
- Legacy-column landing + "upgrade cannot log anyone off" NULL pins exist
  (Task 4); half-migration repair + idempotency exist (round 4).

**GAP → add one test** (`tests/test_migrations.py`, reusing its `_legacy_db`
+ `_run` machinery): `test_new_tables_land_on_a_legacy_database` — a
legacy-shaped DB (pre-cart/pre-scan tables) booted through `create_all()` +
`_migrate_schema()` gains **both** `cart_items` and `scan_requests`;
`PRAGMA index_list('cart_items')` shows `uq_cart_items_user_item`; a
duplicate `(user_id, item_id)` insert raises `IntegrityError` (behavioural
proof the upsert's conflict target exists on *upgraded* databases, not just
fresh ones — the Task-1 pin only covered fresh `create_all`). Idempotent on
re-run. This closes the last unpinned migration surface of the feature.

## Part 4 — Hardware checklist (write into §11; execute with the owner)

**Pre-flight (host, from `SCANNER-AGENT-SYNC.md` — all still outstanding):**
`scripts/usb_host_check.py` (xHCI `0000:c3:00.3` death); revive via
unbind/bind → **PCI remove+rescan** (no FLR: `reset_method=pm`) → reboot, or
the USB-C-port workaround; install `scripts/udev/99-opticon-scanner.rules`;
add the ModemManager ignore rule (`ENV{ID_MM_DEVICE_IGNORE}="1"` for
`065a`); set `power/control=on` for the controller (suspend/resume
prevention, §4 of the sync file).

**Track A — USB-HID (`A001`, works today, no sudo after the typing proof):**
1. Five-second proof: focus any text field, scan → digits + CR typed.
2. Login page: badge field scan logs in (regression pin — the dedicated
   sink must never break).
3. Catalogue, focus anywhere (search box, page background): known GTIN scan
   → toast + line + total; re-scan → qty 2; `qty > stock` → warning only.
4. UPC-E item scanned as its EAN-13 filing (symbology-transmitting setup)
   and a bare-6-digit scan without symbology → the 404's UPC-E hint.
5. Misread (digit flipped) → "MISREAD — re-scan" toast, nothing added.
6. Own badge on the kiosk → overlay 30→0; [Stay logged in] cancels (verify
   `logoff_deadline` NULL via a second session's API call); re-scan
   restarts; let one expire → logged out; **close the tab mid-countdown →
   after 30 s the token 401s anywhere** (curl or second tab — Task 4's
   property, end-to-end).
7. Other-user badge → "Switched to X — Y's list was saved"; switch back →
   Y's cart intact; old kiosk token revoked (curl → 401); the user's phone
   session (second login) survives.
8. Unknown barcode: staff → intake modal (region/usage line, prefix
   siblings), create → in catalogue + cart; member → ScanRequest filed →
   staff Requests tab → Create&resolve with add_to_cart → the member's next
   scan adds it; Dismiss path.
9. Logged out: two barcode scans → banner "2 waiting"; badge login → both
   replayed into the new session's cart; reload mid-staging → staging
   survives (localStorage).
10. Negative: type a sentence fast into search/email/password → never
    captured; held key never eaten; visible scan field handles a 3-char
    internal code the global gate ignores.
11. Register → badge modal → **Print yields the card alone** (print
    isolation); payload line legible; reload → never shown again.

**Track B — USB-COM (`A002`, after pre-flight):** `opticon_detect.py` shows
`065A:A002` on a ttyACM; `m10_scan_test.py` stream check;
`scanner_bridge.py --email … [--stock-in 1|--stock-out 1]` exact-identity +
stock-adjust e2e (incl. one misread and one unknown GTIN verdict); badge
scan → qr-login record with the **redacted** text output, full token only
under `--show-token`/`--json`; `--relogin` after a manual token revoke.
**Explicitly not on the checklist:** kiosk flows over USB-COM (the
counter-session relay is deferred Phase B — the SPA flows above are
HID-transport; noting this prevents "testing" a feature that doesn't
exist).

## Part 5 — Smoke test (small, recommended)

`scripts/smoke_test.py` has zero cart/scan coverage; it is the repo's
live-subserver e2e gate. Add ~8 checks against the running server: register
(response carries `qr_badge`, badge qr-logins) → `POST /api/cart/scan`
known seeded GTIN (`EX-…` barcode or a created item) → 200 added; unknown
valid GTIN → 404 structured; misread → 422; `GET /api/cart` totals;
identify same/other; schedule+cancel; cleanup deletes what it created.
Keeps the "smoke covers the kiosk loop end-to-end over real HTTP" property
the pytest TestClient suite approximates.

## File-by-file plan (one PR, ordered)

| # | File | Change | Kind |
|---|---|---|---|
| 1 | `scripts/scanner_bridge.py` | FIX-1 text-mode redaction + `--show-token` + stderr note + `--help`/docstring | fix |
| 2 | `tests/test_scanner_bridge.py` | emit-redaction tests (record/`--json` pins untouched) | test |
| 3 | `static/app.js` (+`index.html` if markup needed) | A8 print-precedence: opening `badgePrint` re-queues an open `scanIntake` | fix (3 lines) |
| 4 | `README.md` | FIX-2 tracked-issues truth + env row + feature bullets + scanning paragraph | docs |
| 5 | `docs/opticon-m10.md` | new §11 (Part-2 outline) incl. accepted-risks table + hardware checklist | docs |
| 6 | `docs/barcodes.md` | "Closest-item suggestions" section | docs |
| 7 | `tests/test_migrations.py` | `test_new_tables_land_on_a_legacy_database` (Part 3) | test |
| 8 | `scripts/smoke_test.py` | kiosk-loop smoke checks (Part 5) | test |
| — | Untouched | backend routers/models/deps, `COLUMN_MIGRATIONS` (nothing to add), CSP, conftest | — |

Gate: `uv run pytest` green (495+ expected), `ui_check.py --self-host`
green, `smoke_test.py` green, `node --check static/app.js`, and the audit
table above filled in with evidence (grep outputs / test names) in the PR
description.

## Risks & open questions

1. **FIX-1 scope**: redaction touches the bridge's human output — anyone's
   shell scripts parsing `session token: <t>` from text mode break. Judged
   acceptable (that parsing was always fragile; `--json` is the contract)
   and *safer by default* is the right side to err on with bearer tokens.
   Coordinator can downgrade FIX-1 to docs-only if the headless printout is
   load-bearing for the owner.
2. **A8 fix vs document**: the double-modal print edge is cosmetic; the
   3-line re-queue is recommended but droppable (then: document).
3. **§11 length discipline**: it must link, not duplicate, §6a/§9 and the
   code docstrings (the repo punishes stale duplication — README-truth
   commits); the audit table and checklist are the only net-new prose.
4. **Hardware availability**: Track B depends on the owner running the
   pre-flight (sudo/reboot). The checklist is written so Track A can be
   executed and signed off independently — the feature is usable and
   verifiable in HID mode alone (which is how the unit is configured today).
5. **Smoke-test additions** need embeddings (fake in pytest, real in smoke's
   subprocess): item creation in smoke already handles the model download
   path; the new checks reuse whatever the existing smoke harness does for
   `POST /api/items` (no new download surface).
6. **No backend behaviour changes** are expected from the audit — if one is
   found (beyond FIX-1/A8), it gets its own test in the same PR and a line
   in the PR description; nothing silent.
