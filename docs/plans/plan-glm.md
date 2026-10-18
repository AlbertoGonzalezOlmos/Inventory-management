# Plan (peer: glm, **final v2** — reconciled with plan-kimi and plan-qwen)

Scope of the assigned task: use the Opticon M-10 for (1) QR-code login — a QR
badge is auto-generated per account; scanning your own badge while logged in
offers a logoff screen with a **30-second countdown**; scanning someone else's
badge **saves the current user's work, then switches** to that account — and
(2) barcode scanning for the shop — a scanned item barcode is looked up in the
catalogue and added to the logged-in user's cart; an unknown barcode opens a
name+price entry screen with closest-match suggestions from the barcode
system; a scan with nobody logged in asks which account the item belongs to.

> **v2 changes** (Phase 2 reconciliation): adopted the peers' role-aware
> unknown-item intake (staff create / member request), server-enforced lazy
> logoff expiry (qwen's idea, simplified onto `AuthToken`), polling instead of
> SSE, kimi's token-revocation and staging ideas, qwen's suggestion endpoint
> and cart-cleanup paths. Full adopt/reject ledger in §7.

---

## 1. What already exists (do not rebuild)

| Capability | Where | Status vs. the task |
|---|---|---|
| M-10 serial driver (one reader thread, command/scan demux, verified 065A:A002 discovery) | `app/scanner/{m10,protocol,transport}.py` | **Done.** No changes needed. |
| QR badge credential: `HCRM1:<43-char token>`, SHA-256-hash-only storage, SVG rendering | `app/qrbadge.py`, `app/vendor/qrcodegen.py` | **Done.** |
| Badge login `POST /api/auth/qr-login` (uniform 401, raw token also accepted, `must_change_password` honoured) | `app/routers/auth.py` | **Done** — but it always *creates a new session*; it has no notion of "you are already logged in as X". |
| Badge self-service + staff management (`POST/DELETE /api/auth/qr-badge`, `/api/members/{id}/qr-badge`) | `app/routers/auth.py`, `app/routers/members.py` | **Done**, but **opt-in only**. The task requires a badge for *every new account* — `register()` and `create_member()` currently create accounts with `qr_badge_hash = NULL`. |
| Bridge: exact-identity catalogue lookup (canonical GTIN / exact SKU), stock adjust, badge→session exchange, JSON mode | `scripts/scanner_bridge.py` | **Partially covers** (2): it *looks up* a scanned barcode but has no cart concept and no UI delivery; its badge path prints a token to stdout instead of driving a browser session. |
| GTIN analysis/normalisation, symbology classification, GS1 prefix regions/usage | `app/barcodes.py` (+ `docs/barcodes.md`) | **Done** — the "closest items using the barcode system" suggestions build on `analyze_scan` + GS1 company-prefix structure. |
| `GET /api/items?barcode=` exact GTIN lookup (422 on misread); keyword + semantic search | `app/routers/items.py` | **Done** — reused by the scan path and the live name suggestions. |
| Login page badge field (USB-HID types into it) | `static/index.html`, `static/app.js` (`doBadgeLogin`) | **Done** for the logged-out case only. |
| Purchase list | `static/app.js` — **client-side only, in `localStorage` (`hcrm_purchase`), not per-user, not cleared on logout** | **Fundamental gap.** It is user-agnostic *and* leaks to the next user of the same browser. "Save whatever had been done in the current user's account" is impossible until the cart is server-side and per-user. |
| Migration machinery (append-only `COLUMN_MIGRATIONS` / `INDEX_MIGRATIONS`; new tables via `create_all`) | `app/main.py` | Done — new tables need no entry; new columns must be appended to the tuple. |
| Tests | `tests/test_qr.py`, `test_scanner_bridge.py`, `test_barcodes.py`, `test_items_barcode.py`, `test_scanner.py`, `test_concurrency.py`, … | Good harness (`client`, `admin_token`, `create_user` in `conftest.py`); everything must stay green with `TestClient` and **zero hardware**. |

**Hardware reality** (from `SCANNER-AGENT-SYNC.md`, `docs/m10-unreachable-2026-10-10.md` —
adopted from qwen): the dev unit currently enumerates as the **USB-HID
keyboard personality (`A001`)** — it types scans + Enter into the focused
element and works today with zero setup. The USB-COM personality (`A002`,
needed by `scanner_bridge.py`) is unverified since the xHCI controller
`0000:c3:00.3` died, and `/dev/hidraw1` is root-only until the udev rule is
installed. **Consequence: the browser must work standalone in HID mode (no
bridge, no server push); the relay path is secondary and must be fully
testable without hardware.**

**Key architectural observation.** The task's flows (30-second logoff
countdown, save-then-switch, scan-to-cart while browsing) are **UI behaviours
driven by scans that arrive while the user is anywhere in the app**. So the
frontend needs a **scan input bus** that works on every route; everything else
hangs off it. All three peer plans independently converged on: server-side
per-user cart + HID global capture as the primary scan source + one
server-side scan-resolution brain.

---

## 2. Design decisions (reconciled; remaining flags for the coordinator)

### D1 — Server-side cart (all three plans agree; foundational)
New `cart_items` table (per-user, per-item, qty) + `/api/cart` CRUD. The SPA's
purchase panel keeps its look but is backed by the API. The legacy
`localStorage` list is (a) migrated up once on first login for existing
browsers, and (b) — adopted from kimi — kept as an **anonymous pre-login
staging area**: scans made while logged out accumulate there and are replayed
into the server cart after the attribution flow resolves. *Rejected
alternative:* namespacing localStorage per user — cannot survive a browser
change and cannot serve the bridge.

### D2 — Scan delivery: HID global capture primary; relay secondary, polled not pushed
1. **USB-HID global capture (primary, ships first — matches the hardware's
   current personality).** A global keystroke accumulator in `app.js`: buffer
   keys while inter-key delay < ~50 ms, flush on Enter (concrete threshold
   adopted from kimi), and **never hijack a focused input/textarea the user is
   typing in slowly** — the login-page badge field keeps its exact current
   behaviour (no regression). Candidate strings are only treated as scans if
   they start with `HCRM1:` or are GTIN-shaped, so a burst of human typing
   cannot trigger a flow.
2. **USB-COM bridge relay (secondary).** New bridge flag `--relay`: every scan
   is POSTed verbatim to `POST /api/scans`; the UI retrieves them by
   **polling** `GET /api/scans?cursor=N` (~700 ms, only while the counter view
   is open). **Polling, not SSE/WebSocket** — adopted from qwen, and I concur:
   `EventSource` cannot send an `Authorization` header, tokenized query
   strings leak, and polling reuses the existing `api()` wrapper and CSP
   untouched. Latency is irrelevant at counter speed.
   Both endpoints are gated by a shared kiosk secret (`HCRM_KIOSK_KEY`; both
   disabled/404 when unset — the HID path needs nothing). **Security posture
   (reconciled with qwen's objection):** in relay mode the badge payload does
   transit the bus, so `HCRM_KIOSK_KEY` is **credential-equivalent** — it must
   be protected like a password and only used on a trusted network, which the
   deployment already mandates (loopback default, `run.sh` bind guard). Qwen's
   heavier alternative — a server-side counter session where the server
   resolves the badge by hash and the browser "adopts" the session via a
   `terminal_id` capability, so the credential never rides the bus — is the
   right design for multi-terminal or untrusted-LAN deployments, and is
   **deferred as the documented evolution path**, not built now (see §7-R2).

### D3 — Auto-generated badge on account creation (all three plans agree)
`register()` and `create_member()` generate the badge in the creation
transaction and return payload+SVG **exactly once**, in the response only —
never in `/me`, login responses, or member listings (`has_qr_badge` stays the
only persisted signal). Seeded/dev admin accounts deliberately get **no**
auto-badge (the payload cannot be shown later; a hash nobody can print is dead
weight — staff issue via Members). Response shapes stay additive:
`AuthOut.badge: QrBadgeOut | None` and a new `MemberCreatedOut(UserOut +
badge)`.

### D4 — Unknown-barcode intake is role-aware (changed in v2; was "members may quick-create")
The task's "show a screen to enter the item name and price" does not say who
types it, but the repo's privilege model — `POST /api/items` is staff-only,
deliberately, with tests — should not be weakened by a scanner. Both peers
independently reached the same conclusion, and I now concur:
- **Staff/admin** see the create-item form pre-filled with the scanned
  (canonicalised) barcode → existing `POST /api/items` → add to cart. A 409
  (duplicate barcode) surfaces "already filed as <sku>" and offers to add that
  item instead.
- **Members** see the same screen but submission becomes an **item request**:
  a small `ScanRequest(user_id, code, name, price_cents, status, created_at)`
  table + `GET /api/scan-requests` (staff) + a badge/count in the Admin view.
  Toast: "unknown item reported — staff have been notified".
- *Flagged alternative for the coordinator:* a `POST /api/items/quick`
  (any authenticated user, restricted fields) if the owner wants true
  self-service intake; it is a one-endpoint change on top of this structure.

### D5 — "Which account should this item be registered to?" (unchanged, now with kimi's staging)
Listing member names/emails to an unauthenticated kiosk is an enumeration
leak (and `GET /api/members` is staff-only). Recommended flow:
- **Primary:** "Scan the member's QR badge" — the badge identifies the account
  and proves presence; the login it produces then replays the staged scans
  (D1b) into that user's cart.
- **Secondary (staff-assisted):** staff at the kiosk types the email; the
  assignment is sent with the *staff* token to a staff-only
  `POST /api/cart/assign` that adds the pending items to the named account's
  cart.
- No anonymous member picker. (Kimi's "log in or scan a badge" framing adopted;
  qwen's durable server-side `PendingScan` parking is noted as an optional
  hardening if staff want to review unattributed scans later — the default
  keeps pending scans in SPA state + localStorage staging, which is enough
  while the kiosk tab is open.)

### D6 — The 30-second logoff is UI presentation + **server-enforced lazy expiry** (strengthened in v2)
The countdown/cancel/expiry UX lives in the SPA, but a client-only timer dies
with the tab (my v1 accepted this; qwen did not, and qwen is right that it is
fixable cheaply). Adopted mechanism — **simplified from qwen's counter-session
token registry onto the token itself**:
- New nullable column `auth_tokens.logoff_deadline` (one `COLUMN_MIGRATIONS`
  entry, append-only).
- `POST /api/auth/logoff-schedule` (authenticated): sets
  `logoff_deadline = now + LOGOFF_GRACE_SECONDS` (constant, default 30, env
  override `HCRM_LOGOFF_GRACE_SECONDS` for tests/demo — adopted from qwen) on
  the **presented token only** — the user's other sessions are untouched, same
  principle as `logout`.
- `get_current_user` already loads the `AuthToken`; add: if
  `logoff_deadline` is set and past → delete the row and reject 401. That is
  the **lazy expiry**: no scheduler; the token is dead after the deadline even
  if the browser crashed mid-countdown, and it costs one comparison per
  authenticated request.
- `POST /api/auth/logoff-cancel` clears the deadline ("Stay logged in").
- The SPA calls `logoff-schedule` the moment the countdown modal opens, so the
  server clock — not a `setInterval(30)` — owns the truth; the modal's
  per-second display is computed from the returned deadline timestamp
  (deadline-driven countdown survives poll/tab latency — adopted from qwen).
This works identically for HID-initiated and relay-initiated badge scans and
needs no counter-session machinery.

### D7 — Cross-account switch semantics (refined in v2)
With the server-side cart, every mutation is durable as it happens, so "save
whatever had been done" holds by construction; the switch flow is:
1. `await` any in-flight cart request (flush the debounce); **a flush failure
   aborts the switch with a loud error** — never strand unsaved work silently
   (adopted from kimi).
2. `POST /api/auth/qr-login` with the scanned badge → **new session in hand
   before anything is dropped** (adopted from qwen's ordering: never log out
   first and fail second).
3. Revoke the old kiosk token via `POST /api/auth/logout` *with the old
   token* — this revokes **only that one token** (the endpoint's existing
   semantics), so kiosk sessions do not accumulate, and the user's other
   devices are unaffected. (Changed from my v1 "drop, don't revoke" — kimi was
   right; my v1 concern conflated per-token revocation with account-wide
   revocation.)
4. `saveSession`, `GET /api/cart`, reload catalogue/categories; toast
   "Saved — switched to <name>".
5. Honour `must_change_password` on the new account exactly like
   `doBadgeLogin` does today (redirect to Account, with an explanatory toast).
The countdown modal's rescan semantics (adopted from qwen, replacing my v1
"restart timer" and kimi's "suspend"): while the modal is open, a **same-badge
re-scan restarts the countdown** (re-schedule), a **different badge cancels it
and performs the switch**, any other scan is ignored, and Esc/the Stay button
cancels. Only one modal instance ever exists.

### D8 — Badge identity check without logging in
Knowing "does this badge belong to the current user" requires asking the
server without creating a session: new unauthenticated
`POST /api/auth/qr-identify` `{token}` → with a bearer token present:
`{same_user: true/false}`; without one: `{}` — a logged-out caller learns
nothing (uniform 401 for invalid badges, no enumeration). The UI calls this
on every badge scan while logged in. (Alternative rejected: logging in first
and comparing — creates a throwaway session per probe and races the countdown.)
Note: `qr-identify` + `qr-login` means two hash lookups per own-badge scan;
acceptable (indexed seek), and `qr-login` is only called on an actual switch.

### D9 — Suggestions ("closest matching items using the barcode system")
Adopted from qwen (richer than my v1 inline payload), plus kimi's category
fallback:
- New pure helper `app/barcodes.py::prefix_candidates(gtin14)` → progressively
  shorter GS1 company-prefix patterns (first 12, 11, … 7 digits of the padded
  GTIN-14); same company prefix ⇒ same brand/vendor ⇒ the usual substitute.
- New endpoint `GET /api/items/suggest?barcode=&q=&limit=` →
  `{by_prefix: [...], by_keyword: [...], by_semantic: [...]}` — prefix matches
  ranked longest-first, keyword reuses the `q=` ILIKE query, semantic wraps
  `vector-search` and **degrades to `[]` on 503** (suggestions must never
  break the scan flow). `by_prefix`/`by_keyword` also feed the unknown-item
  modal before the user types; live typing re-queries with `q=`.
- Misreads (`invalid-gtin`) are never suggested and never looked up (the
  repo's hard rule — REVIEW-m10.md P1).
- Same-category fallback (kimi) appended when prefix matches are empty.
Cap ~5 per group. Prefix matching is SQL LIKE on an indexed column — fine at
shop scale (documented).

### D10 — Scan resolution: server helper, bridge untouched (changed in v2)
My v1 proposed factoring the bridge's `find_item` into a shared
`app/scanlookup.py`. Qwen argued against merging across the process boundary
(bridge speaks HTTP with an injected `get`, already well-tested), and I accept
the argument: the shared-core interface gets awkward (dicts vs ORM), and the
decision table is ~30 lines. **Instead:** new `app/scanflow.py::resolve_scan`
implementing the same decision table server-side (GTIN → `Item.barcode`;
opaque → exact SKU; `invalid-gtin` → misread; unknown → not-found +
suggestions), plus an explicit **parity test** pinning `resolve_scan` and
`scanner_bridge.find_item` to the same verdict table so they cannot drift
silently.

### D11 — Cart concurrency (reconciled)
Per-line `POST /api/cart/lines {item_id, delta}` is an atomic single-row
upsert under the unique key — two kiosks on one account cannot lose an
increment (qwen/kimi agree). Whole-cart `PUT /api/cart` (adopted from kimi;
used by the switch flush and the staging replay) is transactional
delete+insert and documented **last-writer-wins** — no CRDTs. Keep my v1
concurrency test modelled on `tests/test_concurrency.py` for the line upsert.
Cart qty is **not** clamped to stock server-side (the UI keeps its stock-aware
hints); stock is **not** decremented on scan-to-cart — checkout remains a
tracked non-goal (kimi Q4 / qwen R5, agreed).

---

## 3. Ordered task list

Each task is independently reviewable, ends with `uv run pytest` green, and is
testable with `TestClient` and zero hardware. Steps 1–5 are backend; 6 is the
SPA; 7 closes out.

### Task 1 — Server-side per-user cart (backend)
- **New** `CartItem` model in `app/models.py`: `id`, `user_id` FK (indexed),
  `item_id` FK, `qty ≥ 1`, `created_at`, `updated_at`; unique
  `(user_id, item_id)`. New table → `create_all()` covers it; the unique
  index gets an `INDEX_MIGRATIONS` entry if `create_all` does not carry it to
  existing DBs (verify with `tests/test_migrations.py`).
- **New** `app/routers/cart.py` mounted in `app/main.py`, all
  `get_current_user` (any role):
  - `GET /api/cart` → lines (joined `ItemOut`) + `total_cents`;
  - `POST /api/cart/lines` `{item_id, delta}` → atomic add/increment
    (404 unknown item; 422 on bad delta);
  - `PUT /api/cart` → whole-cart replace (switch flush + staging replay;
    validates item ids, transactional);
  - `PATCH /api/cart/lines/{item_id}` `{qty}` (0 = remove), `DELETE
    /api/cart/lines/{item_id}`, `DELETE /api/cart` (clear);
  - `POST /api/cart/assign` `{email, item_ids[]}` — **staff-only** (D5
    secondary path).
- **Cleanup paths** (adopted from qwen — FKs are declarative only,
  `app/database.py`): `DELETE /api/items/{id}` and member deletion must also
  delete that item's / user's cart rows (mirror the `auth_tokens` cleanup
  convention).
- Schemas in `app/schemas.py` (`CartOut`, `CartLineOut`, `CartLineIn`,
  `CartReplaceIn`, `CartQtyIn`, `CartAssignIn`), `_Stripped`/`extra="forbid"`
  conventions.
- **Tests** `tests/test_cart.py`: CRUD round-trip; add/increment; PUT replace;
  clear; qty validation; per-user isolation; cart rows die with the item and
  with the user; assign is staff-only; unauthenticated → 401;
  `must_change_password` gate applies; concurrency test for the line upsert.
  Extend item/member deletion tests for cleanup.

### Task 2 — Auto-generate a QR badge at account creation (backend)
- `app/routers/auth.py::register` and `app/routers/members.py::create_member`:
  in the creation transaction, `payload = new_badge_payload()`,
  `user.qr_badge_hash = token_hash(payload)`; response carries
  `badge=QrBadgeOut(payload, svg)` (`AuthOut.badge` optional field; new
  `MemberCreatedOut` for members). Seeded/dev admins: no auto-badge (D3).
- **Tests**: extend `tests/test_qr.py`, `test_auth.py`, member tests —
  response badge immediately works via `qr-login`; `has_qr_badge` True
  afterwards; badge payload absent from `/me`, `GET /api/members`, and later
  logins; rotation/revoke still work. Check `test_metadata_and_ops.py`'s
  OpenAPI snapshot for the additive fields (qwen R7).

### Task 3 — Logoff schedule/cancel + qr-identify (backend, D6/D8)
- `app/models.py` + one `COLUMN_MIGRATIONS` entry:
  `("auth_tokens", "logoff_deadline", "DATETIME", ())`.
- `app/security.py`/`app/deps.py`: `LOGOFF_GRACE_SECONDS = int(env
  "HCRM_LOGOFF_GRACE_SECONDS", 30)`; `get_current_user` rejects + deletes
  tokens past their deadline.
- `app/routers/auth.py`: `POST /api/auth/logoff-schedule` (sets deadline on
  the presented token; returns `{deadline}`), `POST /api/auth/logoff-cancel`,
  `POST /api/auth/qr-identify` (D8 shape, uniform 401 on bad badges).
- **Tests** in `tests/test_qr.py` / new `tests/test_logoff.py`: schedule →
  token dies after the deadline (env override to 1 s or monkeypatched
  `utcnow`); cancel restores it; other sessions of the same user unaffected;
  `qr-identify` same_user true/false, anonymous `{}`, invalid badge 401.

### Task 4 — Scan resolution + suggestions (backend, D9/D10)
- `app/scanflow.py::resolve_scan(session, code, symbology)` — the server-side
  decision table; `app/barcodes.py::prefix_candidates`.
- `GET /api/items/suggest` (authenticated) per D9.
- `POST /api/cart/scan` `{code, symbology?}` (authenticated): badge payload →
  400 (badges never reach the catalogue); found → add + return item/qty;
  misread → `{status: "misread"}`; unknown → `{status: "unknown", analysis,
  suggestions}`. **Nothing is auto-added on unknown** (no guessing).
- **Tests**: `tests/test_scanflow.py` incl. the **parity test** against
  `scanner_bridge.find_item`; `tests/test_items_suggest.py` (prefix
  longest-first, keyword, semantic degradation with embeddings disabled,
  wildcard-injection safety in `prefix_candidates`); cart-scan cases (UPC-E →
  EAN-13-filed item; opaque SKU; misread never adds; badge → 400).

### Task 5 — Scan relay (backend + bridge, secondary path)
- `POST /api/scans` + `GET /api/scans?cursor=` (polled), both gated by
  `HCRM_KIOSK_KEY` (constant-time compare; 404 when unset). In-memory
  append-only per-channel ring (single-worker constraint documented —
  `run.sh` deploys one uvicorn worker); events carry the raw scan code only
  (and are therefore credential-bearing **only insofar as the kiosk key is
  protected** — documented as credential-equivalent, D2).
- `scripts/scanner_bridge.py --relay [CHANNEL]`: POSTs every scan verbatim
  (badges **not** consumed locally — the kiosk decides); mutually exclusive
  with `--stock-in/--stock-out/--fuzzy`; reuses `call_with_relogin` and the
  bounded-timeout `api()`; per-scan `ApiError` records keep the listener
  alive (P2 contract).
- **Tests**: `tests/test_scan_relay.py` (key gating, cursor semantics,
  channel isolation, disabled-when-unset); `tests/test_scanner_bridge.py`
  additions (relay posts verbatim; mutual-exclusion `SystemExit`; `--json`
  shape).

### Task 6 — Frontend: cart UI, scan bus, and all kiosk flows
- **Cart on the API** (`static/app.js`, `index.html`, `style.css`): purchase
  panel backed by `/api/cart`; one-time legacy import on first login
  (skip ids no longer in the catalogue, then clear `PURCHASE_KEY`); anonymous
  pre-login staging list in localStorage, replayed via `PUT /api/cart` after
  login/attribution (kimi). **Also clear the staging/local list on logout**
  (fixes the existing cross-user leak).
- **Scan bus**: HID global capture (D2.1: <50 ms inter-key, Enter terminator,
  focus rules, `HCRM1:`/GTIN-shape gate before acting; small "scan received"
  indicator so silent misroutes are visible — kimi) feeding one
  `handleScan(code)`; the relay poller feeds the same function when
  configured. Route: `HCRM1:` → badge flows; else `POST /api/cart/scan`.
- **Badge flows** (per D6/D7/D8):
  - nobody logged in → existing `doBadgeLogin` path, then replay staging;
  - own badge → `logoff-schedule` + **countdown modal**: big remaining
    seconds computed from the server `deadline`, [Stay logged in] →
    `logoff-cancel`, [Log off now] → `logout`, expiry → `logout`; rescan
    semantics per D7; only one modal instance; interval always cleared on
    dismiss/route change.
  - different user → save-then-switch per D7 (flush → qr-login → revoke old
    kiosk token → load new cart).
- **Barcode flows**:
  - found → toast + cart panel refresh;
  - misread → "misread, please re-scan" toast, nothing else;
  - unknown → **unknown-item modal**: read-only normalised barcode + its
    barcode-system info (region/usage), name + price form, suggestion groups
    *By barcode prefix / By name / Similar meaning* (click to add that item);
    submit is role-aware per D4 (staff → `POST /api/items` (+409 handling) →
    add to cart; member → `POST /api/scan-requests` + toast);
  - nobody logged in → **attribution screen**: "Which account is this for?
    Scan a badge or log in." Pending codes held in SPA state + staging list;
    after login/badge-login they are claimed (re-POSTed to
    `/api/cart/scan`/merged via `PUT`); staff-assisted email path calls
    `/api/cart/assign`. No member picker.
- **Register / member-create success views**: show the one-shot badge SVG in
    the existing print modal with a "print now — shown only once" warning.
- **Verification**: no JS test runner exists (do not add one — qwen's note,
  agreed); server tests own the semantics; add cheap `scripts/ui_check.py`
  assertions (badge after register, capture field present) plus a **manual
  hardware checklist** (adopted from qwen): HID typing test against the SPA;
  two printed badges (same-user countdown incl. browser-close-then-expiry,
  different-user switch) and one unknown barcode. USB-COM relay checklist
  blocked on the xHCI/udev fix from `SCANNER-AGENT-SYNC.md`.

### Task 7 — Docs, README, review pass
- `docs/opticon-m10.md` new §11 "Counter/kiosk mode": HID capture, relay +
  `HCRM_KIOSK_KEY` (credential-equivalence warning), 30 s logoff
  (client + server deadline), switch semantics, unknown-item intake, item
  requests, attribution; cross-link §6a/§9. Evolution path note for the
  hardened counter-session design (§7-R2).
- `README.md`: feature bullets; env table (`HCRM_KIOSK_KEY`,
  `HCRM_LOGOFF_GRACE_SECONDS`); tracked-issues update (purchase list no
  longer client-only; checkout still open).
- `docs/barcodes.md`: the suggestion rule (company-prefix similarity) and why
  misreads are never suggested.
- Security review pass (my v1 Task 9): one-time badge exposure (grep
  `QrBadgeOut` usages; `UserOut` untouched), `qr-identify` enumeration,
  constant-time key compare, `Cache-Control: no-store` on the relay,
  quick-check that no test asserts exact JSON equality on the changed
  response shapes, timer/interval cleanup, `ScanRequest` cannot mutate the
  catalogue.

---

## 4. PR slicing (for the coordinator)

1 (cart) → 3 (logoff/identify) → 4 (scan resolution + suggestions) are a
natural server stack; 2 (auto-badge) is independent; 5 (relay) depends on
nothing but pairs with 6; 6 (SPA) lands incrementally (cart UI → scan bus +
badge flows → barcode flows → attribution); 7 last. Every slice keeps the
suite green and appends to `COLUMN_MIGRATIONS` rather than editing shared
blocks (the round-5 collision lesson).

## 5. Risks

1. **HID capture false positives** — mitigated by timing + Enter + shape gate
   + focus rules; the login badge field must not regress (manual M-10 test;
   `scripts/m10_scan_test.py` where possible).
2. **Kiosk key is credential-equivalent** in relay mode — documented; trusted
   network only; unset = relay off. The hardened counter-session design is
   the escape hatch (§7-R2).
3. **`logoff_deadline` adds a check per authenticated request** — one
   comparison on an already-loaded row; negligible.
4. **Single-worker relay ring** — in-memory assumes one uvicorn worker
   (matches `run.sh`); multi-worker needs a DB-backed bus, documented as out
   of scope.
5. **Member item requests** add a table + staff UI surface — kept
   deliberately tiny; if the owner prefers self-service intake, swap in
   `POST /api/items/quick` (D4 flagged alternative).
6. **Suggestion quality** depends on catalogue density; harmless when empty.
7. **`must_change_password` mid-switch** — existing behaviour, surfaced with
   an explanatory toast (small UX task inside Task 6).
8. **Browser death mid-countdown** — the server deadline still kills the
   token lazily (D6); the *screen* obviously cannot.

## 6. Open questions for the coordinator

1. **D4**: staff-create + member-request (recommended, matches both peers) vs.
   member self-service quick-create (my v1 stance; one-endpoint swap).
2. **D5**: badge-scan attribution primary + staff email-assign secondary —
   confirm; qwen's durable server-side `PendingScan` parking is available if
   staff want deferred review.
3. **Switch token handling**: revoke the kiosk's own old token (recommended,
   kimi) — confirm this is not meant to log the previous user out of *all*
   their devices (it does not, by construction).
4. **Relay scope this round**: build the minimal polled relay (recommended,
   since USB-COM is currently untestable on this host) vs. defer entirely
   (kimi's phase-2 stance) vs. build qwen's full counter-session design now.
5. **Cart vs stock**: no clamping, no reservation (recommended) — confirm.

## 7. Reconciliation ledger (v2 — what I adopted / rejected, and why)

**From plan-kimi, adopted:**
- `PUT /api/cart` whole-cart replace (switch flush + staging replay).
- Anonymous localStorage staging list replayed after login (fixes
  attribution without new server state).
- Concrete HID timing heuristic (<50 ms inter-key + Enter) and the "scan
  received" indicator.
- **Revoking the kiosk's own token on switch** — my v1 "drop, don't revoke"
  was based on a wrong conflation of per-token and account-wide revocation;
  `logout` revokes only the presented token, so kimi is right and kiosk
  sessions should not accumulate.
- Loud abort of the switch when the cart flush fails.
- Role-aware unknown-item intake with a `ScanRequest` table (also qwen) —
  flipped my v1 recommendation; see below.
- "No stock changes on scan-to-cart" made explicit.

**From plan-kimi, rejected/modified:**
- Suspending the scan listener during the countdown — replaced by qwen's
  finer semantics (same badge restarts, different badge switches), which are
  strictly more useful and only marginally more code.
- Bridge relay deferred to "phase 2 optional" — kept in scope but secondary
  (Task 5); the hardware may return to USB-COM at any time and the endpoint
  is cheap once the scan bus exists.

**From plan-qwen, adopted:**
- The hardware-reality framing (unit is HID `A001`; USB-COM unverified since
  the xHCI death; everything must be hardware-free testable).
- **Server-enforced lazy logoff expiry** — the best idea in either peer plan.
  My v1 client-only timer had a real hole (browser death); qwen's fix is
  right. I simplified the mechanism: a `logoff_deadline` column on
  `auth_tokens` (checked in `get_current_user`) instead of qwen's
  `CounterSession.adopt_tokens` registry — same guarantee, no counter-session
  machinery, works in HID mode too (qwen's design only covers relay-adopted
  tokens).
- **Polling over SSE/WebSocket** — qwen's `EventSource`-can't-authenticate and
  CSP arguments are correct; I had planned SSE with a kiosk key and concede.
- Deadline-driven countdown (server timestamp, not a bare local timer) and
  `HCRM_LOGOFF_GRACE_SECONDS` for tests.
- Adopt-first-then-replace-session ordering on switch; "never log out first
  and fail second".
- `prefix_candidates` + a dedicated `GET /api/items/suggest` with
  by_prefix/by_keyword/by_semantic groups and semantic degradation to `[]`.
- Keeping `scanner_bridge.find_item` un-merged (server helper + parity test
  instead of my shared-module refactor across the process boundary).
- Cart cleanup on item/user deletion (FKs declarative only) — my v1 missed it.
- OpenAPI-snapshot check on additive schema changes (R7); manual hardware
  checklist; "do not add a JS test runner".
- No credential broadcast as a *design rule* — accepted as the rule; my
  minimal relay complies by making the kiosk key credential-equivalent and
  documenting it, rather than by qwen's heavier construction.

**From plan-qwen, rejected/modified:**
- **Design C (server-mediated counter session with `terminal_id` adopt/bind,
  `CounterSession` + `PendingScan` tables, staff-token scan ingest)** —
  rejected *for this round*: it is the right architecture for multi-terminal
  or untrusted-LAN deployments, but the actual hardware is HID-only, where
  scans arrive in the browser anyway and even qwen's plan falls back to
  client-side processing; the extra two tables, five endpoints and capability
  model would be built for a transport that cannot currently be tested.
  Kept as the documented evolution path (§7-R2 / D2), and its key insight
  (server-side expiry) is adopted in simplified form (D6).
- Public/unauthenticated event reads — my relay gates reads behind the kiosk
  key; qwen accepts anonymous reads of non-secret events, but scan codes at a
  counter are mildly sensitive and the key costs nothing.
- Cart qty clamping to stock — left unclamped server-side (UI hints only),
  matching qwen's own note and kimi's Q4.

**Where all three plans already agreed (no change):** server-side per-user
cart as the foundation; badge auto-generation at account creation with
one-time display; HID global capture as the primary scan source; misreads
never looked up or suggested; nothing auto-added on unknown barcodes (no
fuzzy guessing, REVIEW-m10 P1); suggestions anchored on GS1 company-prefix
similarity; countdown is UX, cart durability is the real safety net.
