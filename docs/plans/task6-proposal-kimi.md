# Task 6 proposal (kimi-k3): Security review, docs, migrations check, hardware checklist

Coordinator reference: `docs/plans/COORDINATOR.md` Task 6 (closing pass).
The feature landed across Tasks 1–5; this pass audits it, fixes what is
genuinely wrong, documents what is accepted, and closes out docs,
migrations, and hardware verification. Every audit point below was
re-verified against the working tree this pass (not carried over from
review memory).

---

## 1. Security & edge-case audit

### 1a. FIX — the scan pill displays badge payloads in cleartext

`enqueueScan` (app.js:789) sets `this.scanFlash = "scan: " + code` for
*every* scan — including badge scans. The pill renders the full `HCRM1:…`
bearer credential on the kiosk screen for 2 seconds: a shoulder-surfable
secret, against the repo's badge doctrine (hashes at rest, shown once at
issue). Barcodes in the pill are fine (not secrets).

**Fix (static/app.js, ~4 lines):** mask badge-shaped codes in the pill:

```js
const shown = (BADGE_FULL_RE.test(code) || BADGE_RAW_RE.test(code))
  ? "scan: badge received" : "scan: " + code;
this.scanFlash = shown;
```

(`BADGE_RAW_RE` covers a prefix-stripped presentation — mask on shape,
not on prefix.) No test harness for JS exists; verification is the
manual checklist (badge scan → pill shows no payload) plus a ui_check
code-shape comment. Cheap, clearly right.

### 1b. FIX — session-expired path can strand a modal over the login screen

My Task-5 `_closeOverlays()` covers `logout()` and `switchAccount`, but
the two *other* session-death paths were missed: the
`hcrm-session-expired` window handler (app.js:1621, the throwing `api()`
path) and `handleSessionExpired` (the raw-mode path). A token that dies
while the intake modal (or any modal) is open leaves that user-specific
dialog floating over the login view.

**Fix (static/app.js, 2 lines):** call `this._closeOverlays()` in both
handlers (next to the existing `clearLogoff()`).

### 1c. Verified clean — document as accepted risk (no code change)

| Point | Verdict |
|---|---|
| Badge payload transport | POST bodies only; never in URLs, query strings, or logs (no logging near badge code paths in any router). `badgePrint.payload` is rendered **once** in the print modal (index.html:712), held in memory, cleared on close — that *is* the intended "shown exactly once". **Accept + document** (opticon-m10 §11: kiosk screens are trusted surfaces; body-logging middleware must exclude `/auth/*` and `/cart/scan`). |
| Enumeration — `qr-login` | Uniform 401, always runs the hash lookup (no timing oracle beyond qr-identify's explicit boolean). **Accept.** |
| Enumeration — `qr-identify` | Authenticated; uniform `{same_user:false}` for garbage/EAN/other badge; one bit about the caller's *own* hash; zero side effects (DB-pinned). A stolen session learns nothing it can't already do (rotate the badge). **Accept.** |
| Deadline rows of vanished browsers | Inert (every presentation 401s + self-deletes); reaped by the next login's `expires_at` purge (≤ 7 d). Deliberate (Task-4 review: no purge extension, smallest diff). **Accept + document** in §11. |
| Cart races | Server side: single-statement upsert + atomic PUT, 8-thread pins. Client side: `_cartOp` chain serialises; two tabs of one session are documented last-writer-wins. **Accept.** |
| ScanRequest lifecycle | Server re-classifies at filing (badge 400 / misread 422 / already 409 / duplicate 409); non-open transitions 409; weak-ref `resolved_item_id` survives item deletion; member-delete cleanup. **Accept.** |
| Staged scans | `hcrm_staged_scans` holds codes + symbology only (never badge payloads — badge scans route to the badge flow, never to staging), capped at 50. Attribution = "whoever logs in next" — the documented, intended semantics (the banner says exactly that). **Accept + document.** |
| `api()` token handling | Foreign `{token}` calls never trip the global 401 redirect; raw-mode 401s are handled at every call site (the four `handleSessionExpired` guards). **Accept.** |
| `@media print` isolation | Scan pill and toast are `#app` children (not `.modal-backdrop`) → hidden while `printing-badge` is set; only the badge card prints. The **account page's inline own-badge** print has no isolation (prints the account page: name/email/role + the badge — nothing beyond the badge itself is sensitive). **Accept + one doc line.** |
| Legacy purchase import | Browser-global list maps to the first logger-in on that browser — the only available semantics; key removed after processing; per-line fallback for deleted items. **Accept + document.** |
| CSP / `v-html` badge SVG | Server-rendered, pinned inert by `test_badge_svg_is_safe_for_the_v_html_sink`; no new inline script/style in Task 5. **Accept.** |
| `create_item` embeddings-down 503 during staff intake | The intake modal surfaces the server's message; ScanRequests queue until embeddings recover. **Accept + doc line in §11.** |

## 2. Docs (file-by-file)

### `docs/opticon-m10.md` — new §10 "Counter/kiosk flows", renumber References to §11
(§9 ends badge login; §10 is currently References — insert before it and
bump; update the §-references in the header if any.) Content:
- **Personalities**: HID (A001 — works today, no setup; the kiosk path) vs
  USB-COM (A002 — bridge/Phase B; currently blocked, §checklist).
- **Scan routing**: global capture (timing/shape gates, sinks, visible
  scan field), badge vs barcode, queue, modal discipline.
- **Badge flows**: logged-out login; own badge → 30 s overlay (server
  deadline, `HCRM_LOGOFF_GRACE_SECONDS`, lazy expiry — the dead-browser
  property); other badge → save-then-switch (ordering + old-token revoke).
- **Barcode flows**: scan-to-cart; unknown → intake (staff create /
  member ScanRequest; suggestions incl. degradation); logged-out →
  staging + attribution.
- **Operations notes**: accepted risks from §1c (inert deadline rows,
  staging semantics, legacy-import semantics, account-page print,
  body-logging exclusion).
### `README.md` — env table gains `HCRM_LOGOFF_GRACE_SECONDS` (default 30,
clamped ≥ 1; kiosk logoff grace) next to the existing rows (line ~91);
one feature-bullet line for scan-to-cart in the scanner bullet.
### `docs/barcodes.md` — extend §8 ("How this is wired into HCRM") with a
"Suggestions" subsection: `rank_prefix_siblings` (indicator-stripped,
leading-zeros-stripped, ≥ 6 significant shared digits, name tie-break —
and *why*: GEPIR is a lookup service, observed shared length is the
offline proxy); keyword = the single escaped-ILIKE dialect; semantic =
vector search that **degrades to `[]`** (a suggestion miss never fails a
scan flow); misreads are never suggested around.
### `NOTES.md` — one-paragraph refresh of the now-stale "client-side
purchase list" description (server-side per-user cart; kiosk flows;
pointer to opticon-m10 §10).

## 3. Migrations check

Current exact set: `COLUMN_MIGRATIONS` = users.must_change_password,
items.barcode, users.qr_badge_hash, auth_tokens.logoff_deadline;
`INDEX_MIGRATIONS` = auth_tokens.expires_at. Verified against
`test_every_migration_the_app_currently_needs_is_listed` (updated in
Task 4) — correct and complete.

**Gap found:** no test asserts that a *legacy* (pre-Task-1) database
gains the **new tables** `cart_items` and `scan_requests` on boot —
`create_all()` covers them (and the AppMeta precedent says it works), but
nothing pins it, and the whole feature depends on it.

**Test (tests/test_migrations.py, one case):** reuse the file's
legacy-DB fixture (`test_all_migrations_land_on_a_pre_scanner_database`'s
pattern): boot against a pre-scanner schema snapshot → assert
`sqlite_master` contains `cart_items` (with `uq_cart_items_user_item`)
and `scan_requests`, and that re-running `_migrate_schema()` is
idempotent. No production-code change expected.

## 4. Hardware checklist (docs/opticon-m10.md §10 appendix + PR evidence)

**Blocked items marked [BLOCKED]** per `SCANNER-AGENT-SYNC.md`: the xHCI
controller `0000:c3:00.3` (USB-A buses 1/2) is dead — zero attach events
since the suspend/resume crash — and `/dev/hidraw*` is root-only until
`scripts/udev/99-opticon-scanner.rules` is installed. HID personality
works *if* the scanner is on a live bus (it is, today).

**HID (A001) — runnable now:**
1. `./scripts/run.sh` + open the SPA; scan the login badge field →
   passwordless login (regression).
2. Page-level scan (no field focused) of a known GTIN (and a UPC-E) →
   toast + sidebar line; qty increments on re-scan; stock warning when
   qty > stock.
3. Own-badge scan → overlay counts down from 30 (server deadline);
   [Stay logged in] cancels; re-scan restarts; let one expire → logged
   out. Close the tab mid-countdown, wait 30 s → any API use from
   another tab with that token 401s (Task-4 property).
4. Second user's badge → "switched" toast; outgoing user's cart intact on
   switching back; old token revoked (curl with it → 401).
5. Unknown GTIN → intake modal (region/usage + prefix siblings);
   suggestion click adds; staff create+add (+ auto-resolve); member
   report → appears in staff Requests tab and account /mine.
6. Logged-out barcode → banner; badge login → replay adds it. Badge scan
   → pill shows "badge received", **no payload** (the 1a fix).
7. Negative: fast human typing in search/password fields is never
   hijacked; a burst landing in the search box restores the box and
   routes.
8. Register → badge modal prints one card; reload → never shown again.

**USB-COM (A002) — [BLOCKED] on hardware/udev:**
9. [BLOCKED] `uv run python scripts/opticon_detect.py` sees `065A:A002`
   (needs a live bus — revive the controller or use another host port
   bus; install `scripts/udev/99-opticon-scanner.rules` for hidraw
   permissions).
10. [BLOCKED] `uv run python scripts/scanner_bridge.py --email …
    --json`: known GTIN → JSON match record; badge scan → badge_login
    record with token (existing bridge behaviour, unchanged by Tasks 1–6
    — the relay/counter-session mode is deferred Phase B).
11. [BLOCKED] `scripts/m10_scan_test.py` port sanity.

## 5. Files touched

| File | Change |
|---|---|
| `static/app.js` | Fix 1a (pill masking, ~4 lines), Fix 1b (2 lines) |
| `tests/test_migrations.py` | +1 legacy-DB new-tables case |
| `docs/opticon-m10.md` | new §10 (+ References → §11), checklist appendix |
| `README.md` | env-table row + feature line |
| `docs/barcodes.md` | §8 suggestions subsection |
| `NOTES.md` | cart/kiosk paragraph refresh |
| *(untouched)* | all backend routers/models (audit found nothing to change), `scripts/ui_check.py` (pins already in place), `tests/test_ui_check_parser.py` |

**Verification:** full `uv run pytest` green (incl. the new migrations
case) + `scripts/ui_check.py --self-host` passes (the app.js edits are
small, but the gate is cheap) + the HID checklist run and its output
recorded in the PR; [BLOCKED] items recorded as blocked with the
`SCANNER-AGENT-SYNC.md` pointer.

## 6. Explicitly not in this pass

Phase B (USB-COM counter sessions, bridge `--relay`), checkout/orders,
stock reservation, a JS test runner, CDP-driven ui_check interaction.
Each is a deliberate deferral already recorded in the coordinator plan.
