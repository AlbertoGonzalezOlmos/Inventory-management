# Task 6 proposal (glm) — Closing pass: security/edge audit, docs, migrations, hardware checklist

Coordinator Task 6: the feature (QR-badge login flows + barcode scan-to-cart,
Tasks 1–5 merged) gets its closing review. This proposal is the concrete
file-by-file plan: what to **fix** (two small items found by this audit), what
to **document as accepted-as-risk** (the ledger), the docs updates, the
migrations check (+ one missing test), and the hardware checklist.

Audit grounding (verified in the current tree this pass): no
`console.log`/token logging anywhere in `static/app.js`; badge material
appears only in the two creation responses, the two rotate endpoints, and
the one-shot `badgePrint` modal (payload text included — own credential,
cleared on close); staging stores `{code, symbology}` only; the print CSS
isolates the `badgePrint`/`memberBadge` modals but **not** the account
view's inline badge; `tests/test_migrations.py` has **no coverage of the
new tables (`cart_items`, `scan_requests`) on a legacy database** — its
`_run()` helper only calls `_migrate_schema()`, never `create_all()` (the
real startup path adds new tables there); README's false-claims test pins
only BEGIN-IMMEDIATE/CSP/jsdelivr phrases (safe to edit around);
`docs/opticon-m10.md` ends at §10 References (no §11 yet);
`HCRM_LOGOFF_GRACE_SECONDS` is absent from README and the M-10 guide.

---

## 1. Security & edge-case audit — findings

### FIX (two items, both small)

**F1 — Account-view badge print is not isolated** (`static/app.js` +
`static/index.html`). The `@media print` machinery (Task 5) toggles the
`printing-badge` body class only for the `badgePrint` and `memberBadge`
*modals*. The Account view shows a freshly rotated badge **inline**
(`badgeSvg`) with its own Print button — printing there yields the whole
account page around the credential. Not a leak (it is the user's own,
on-screen badge; printing it is the point) but it is inconsistent clutter
and the one path where the repo's print isolation does not apply. **Fix:**
a tiny `printAccountBadge()` method — add `printing-badge` to
`document.body`, call `window.print()`, remove the class in a `finally`
(print is modal/synchronous in every supported browser) — plus give the
account badge block the `badge-card` class so the existing CSS rule
covers it, and wire the account Print button to the method. ~10 lines; no
new risk surface.

**F2 — Missing legacy-upgrade coverage for the new tables**
(`tests/test_migrations.py`). Tasks 1 and 3 added `cart_items` and
`scan_requests` as *new tables* — created by `create_all()` at startup,
correctly with **no** migration entries. But the migration suite's
`_legacy_db()`/`_run()` helper never calls `create_all()`, so nothing
proves an old database actually gains those tables (with the
`uq_cart_items_user_item` constraint) on upgrade. This was recommended in
the Task 1 review and never landed. **Fix:** one test — build the legacy
DB, run the real startup sequence (`create_all()` + `_migrate_schema()`,
matching `app/main.py::_startup`), assert `cart_items` and `scan_requests`
exist, `uq_cart_items_user_item` is present (`PRAGMA index_list`), a
duplicate `(user_id, item_id)` insert raises `IntegrityError`, and re-running
is idempotent. Also update the exact-set test's docstring if it still says
"four columns, one index" (it does — correct, but assert it matches the
tuple literally, which it already does).

### DOCUMENT — the accepted-as-risk ledger

Each of these is a deliberate, already-adjudicated decision; Task 6's job
is to write them down in one place (the new §11 + README where noted), not
to change them:

| # | Item | Status / rationale |
|---|---|---|
| D1 | **Deadline-past tokens linger ≤ 7 days** if never presented again (purge not extended to `logoff_deadline`) | Inert by construction: any presentation deletes; never-presented rows die at the `expires_at` purge. Chosen in Task 4 over login-path scan growth. |
| D2 | **No rate limit on `qr-login` / `qr-identify`** | 256-bit unguessable payloads (opticon-m10 §9.4 doctrine); a rate limiter would add state for no adversary. |
| D3 | **The bridge prints session tokens to stdout** in badge-login mode | Documented headless/kiosk use (§6a/§9.3); the bridge is a trusted back-office process, like `--password` on its own CLI. |
| D4 | **`api()` raw mode skips the global 401 cleanup** (and foreign tokens never trip the redirect) | By design: the switch flow's best-effort old-token calls must not nuke the new session; raw callers own their 401s (`handleSessionExpired` mirrors the cleanup). |
| D5 | **`PUT /api/cart` last-writer-wins** under concurrent kiosks on one account | Documented in Task 1; line-level ops are race-free (8-thread pinned). |
| D6 | **Staged scans live in `localStorage`** (codes + symbology only — no secrets) | Visible to anyone with browser profile access; accepted for a kiosk (same trust as the session token in the same store). |
| D7 | **Badge payload rendered as text in the one-shot print modal** | Own credential, one-shot, never persisted; the text form is the manual-entry fallback for the login badge field. |
| D8 | **Intake items created with `stock: 0`** and cart lines may exceed stock (warn, never clamp) | Coordinator decision 6; checkout/reservation is a tracked non-goal. |
| D9 | **`resolved_item_id` is a weak reference** | Resolved requests survive item deletion (audit trail); read paths tolerate the dangling id. |
| D10 | **`@media print` without the `printing-badge` class prints everything** | Only reachable from the account view — closed by F1. |
| D11 | **HID capture heuristics** (50 ms / ≥4 chars / shape gate) | Worst case is a dropped scan (visible scan field is the fallback) or a harmless 404 toast; the server is the classification authority. |
| D12 | **`qr-identify` tells a stolen session whether a badge is that session's own** | Negligible next to what a stolen session can already do (`/me`, cart, badge rotation); noted in the Task 4 review. |

Also verified clean (no action): badge payload never in URLs (all POST
bodies), `autocomplete="off"` on the badge field, hash-at-rest for both
tokens and badges, uniform 401s (qr-login) / uniform `false`
(qr-identify), member-never-writes-catalogue (ScanRequest path),
`delete_member`/`delete_item` cleanup, the SPA's `_closeOverlays` on
session end, and the CSP-safe `v-html` SVG sink (pinned by
`test_badge_svg_is_well_formed` + the v-html sink test).

## 2. Docs — file by file

**`docs/opticon-m10.md`** — new **§11 "Counter & kiosk flows"** (inserted
before the current References section, which renumbers to §12; cross-links
to §6a and §9):
- HID global capture: the timing/shape/sink rules, the visible scan field,
  modal suspension (barcodes drop, badges process), the scan pill;
- badge flows: logged-out scan → login; own badge → `qr-identify` →
  **30-second logoff countdown** (cancel / log-off-now / expiry → logout;
  re-scan restarts; *server-enforced lazy expiry* — a closed browser still
  logs off, next API use 401s); different badge → **save-then-switch**
  (durable cart → new session first → old kiosk token revoked);
- barcode flows: scan-to-cart, misread → re-scan, unknown → the intake
  modal (suggestions, staff-create vs member-request), the staff
  ScanRequest queue with `add_to_cart`;
- attribution: staged scans + replay after login (badge-scan primary);
- `HCRM_LOGOFF_GRACE_SECONDS` (default 30, clamped ≥ 1);
- the accepted-as-risk ledger items that belong in user-facing docs
  (D1–D3, D6, D11);
- a pointer that the USB-COM counter-session relay is **Phase B, deferred**
  (the bridge today exchanges badges for tokens itself, §6a).

**`README.md`**:
- env table row: `HCRM_LOGOFF_GRACE_SECONDS` | `30` | kiosk logoff
  countdown (server-enforced; clamped ≥ 1);
- feature bullets: the purchase list is now **per-account and durable
  (server-side)**; scan-to-cart with the unknown-item intake; QR-badge
  kiosk flows (countdown, switch) — replacing the now-stale "purchase
  list" phrasing where it implies client-side only (line ~122 area);
- keep every phrase the false-claims test pins (BEGIN IMMEDIATE warning,
  unsafe-eval, jsdelivr/HCRM_DOCS) — verified compatible.

**`docs/barcodes.md`** — a "Suggestions" section: the GS1
company-prefix-sibling rule (indicator-stripped, zero-stripped significant
view; ≥ 6 shared significant digits; longest-first, name tie-break), the
keyword group (the shared `?q=` dialect), the semantic group (degrades to
`[]`, never 503), and the doctrine line: **misreads are never suggested
around**. One worked example (a Polish EAN suggesting its same-company
sibling, not the unrelated German one).

**`docs/opticon-hardware.md`** — only if needed: a pointer that HID mode
needs no udev rule (the global capture runs in the browser); USB-COM still
needs the rule + the xHCI fix.

## 3. Migrations check

- **Exact-set**: `COLUMN_MIGRATIONS` = 4 entries (`users.must_change_password`,
  `items.barcode`, `users.qr_badge_hash`, `auth_tokens.logoff_deadline`),
  `INDEX_MIGRATIONS` = 1 (`auth_tokens.expires_at`) — matches the shipped
  schema; `test_every_migration_the_app_currently_needs_is_listed` pins it
  (updated in Task 4). Verify the docstring says "four columns, one index".
- **Legacy column upgrade**: covered (`test_all_migrations_land_on_a_pre_scanner_database`
  asserts `logoff_deadline` lands NULL on a pre-existing token row — added
  in Task 4; idempotency and half-migration repair covered by existing
  cases).
- **Legacy NEW-TABLE upgrade: missing → F2 above** (one test through the
  real `create_all()` + `_migrate_schema()` startup sequence, asserting
  `cart_items` + its unique constraint and `scan_requests`).
- **No migration entries for new tables** — confirmed correct (create_all
  owns new tables); the exact-set test enforces that nobody "helpfully"
  adds one.

## 4. Hardware checklist (documented in §11; executed by the owner)

**HID personality (A001 — works today, no udev rule):** five-second typing
test → the scan pill flashes; per flow:
1. login-page badge field still logs in (regression);
2. logged-in known-barcode scan on the catalogue (no focus) → toast + cart
   line + total; UPC-E with symbology ID if configurable;
3. misread → "re-scan" toast, nothing added;
4. unknown GTIN → intake modal (region/usage line + prefix siblings);
   staff create+add; member report; queue Create-&-resolve with
   `add_to_cart` reaching the member's cart;
5. own badge → countdown 30→0 (server deadline); re-scan restarts; Stay
   cancels; expiry logs off; **close the tab mid-countdown → the token is
   dead from a second tab (the Task-4 property)**;
6. second user's badge → switch: outgoing cart intact on re-login, old
   token revoked (curl it → 401), flagged account lands on change-password;
7. logged-out barcode → banner; badge login → replay adds it;
8. typing in search/forms at human speed never hijacked (incl. held-key
   repeats); a scan into a focused field restores the field;
9. register / staff member-create → one-shot badge modal prints a clean
   card (the F1 fix's account-view print included); reload → never shown
   again.

**USB-COM personality (A002 — blocked)**: prerequisite the xHCI revival or
USB-C-port move + `scripts/udev/99-opticon-scanner.rules` install from
`SCANNER-AGENT-SYNC.md`; then: `scripts/opticon_detect.py` survey →
`scripts/m10_scan_test.py` round-trip → bridge modes unchanged
(`--stock-out` regression) → the Phase-B relay is *not* built (deferred) —
the checklist item is only that the bridge itself still works for
back-office use.

## 5. Tests

- `tests/test_migrations.py`: **+1** legacy-new-tables case (F2); exact-set
  docstring check.
- No backend behaviour changes → the 493-test suite stays green unchanged.
- F1 is frontend-only: covered by the manual checklist item 9 (no
  interaction harness exists — established).
- Docs edits: `test_readme_has_no_known_false_claims` stays green
  (verified phrase-compatible); no test pins barcodes.md/opticon-m10.md
  content.

## 6. Sequencing (one small PR)

1. F1 (frontend print isolation) + F2 (migration test) — the only code
   changes, both tiny.
2. Docs: opticon-m10 §11 (+renumber References), README env row + bullets,
   barcodes.md suggestions section.
3. Re-run: `uv run pytest`, `scripts/ui_check.py --self-host`,
   `node --check static/app.js`.
4. Hand the hardware checklist (§4) to the owner with the blocked-on notes.

Everything else in the audit is documented, not changed — the ledger in §1
is the deliverable for those.
