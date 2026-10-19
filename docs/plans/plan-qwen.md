# Plan (qwen3.8-max) — M-10 scanner: QR-badge login semantics + barcode-to-cart

**Phase-2 revision** (final, self-contained). Incorporates the review of
`docs/plans/plan-glm.md` and `docs/plans/plan-kimi.md`; §7 records exactly what
was adopted/rejected from each peer and why. Derived from the repo at HEAD
`bfd09fe`.

Scope: (1) QR-code login — a badge is auto-generated per new account; scanning
your own badge while logged in shows a logoff screen with a **30-second
countdown** that logs off on expiry; scanning someone else's badge **saves the
current user's work, then switches**. (2) Barcode shopping — a scanned item
barcode is looked up and added to the logged-in user's cart; an unknown
barcode opens a name+price screen with closest-match suggestions from the
barcode system; a scan with nobody logged in asks which account the item
belongs to.

---

## 0. What already exists (do not rebuild)

| Piece | Where | Status |
|---|---|---|
| M-10 driver stack (USB-COM `065A:A002`, 9600 8N1, scan-stream callback, command channel, verified discovery) | `app/scanner/{transport,protocol,m10}.py`, `tests/test_scanner.py` | done, unit-testable without hardware |
| Bridge: staff login, exact-identity lookup (`analyze_scan` → canonical GTIN or exact SKU, **never guesses** — REVIEW-m10 P1), atomic stock-adjust, `--fuzzy` opt-in, per-scan `ApiError` resilience, `--relogin`, badge scans → `POST /api/auth/qr-login` + print token | `scripts/scanner_bridge.py`, `tests/test_scanner_bridge.py` | done |
| QR badge credential: `HCRM1:<43-char token>`, 256-bit, SHA-256-hash-only at rest, inline-SVG render (vendored Nayuki encoder) | `app/qrbadge.py`, `app/vendor/qrcodegen.py`, `tests/test_qr.py` | done |
| Badge login `POST /api/auth/qr-login` (prefixed + raw token, uniform 401, respects the `must_change_password` gate) | `app/routers/auth.py` | done — but it always *creates a session*; it has no notion of "already logged in as X" |
| Badge issue/rotate/revoke: self-service + staff-for-member (admin accounts protected; rotate = old badge dies) | `/api/auth/qr-badge`, `/api/members/{id}/qr-badge` | done, **opt-in only** |
| Login-page *QR badge* field: the M-10 in **USB-HID mode (`A001`) types the payload + CR and submits**; Account/Members views generate + print badges | `static/app.js` (`doBadgeLogin`, `generateMyBadge`, `memberBadge`), `static/index.html` | done (logged-out case only) |
| Barcode identity: EAN-13/8, UPC-A/E(+preamble), GTIN-14, check digits, GS1 prefix/region/usage classification, `analyze_scan` verdicts (`gtin`/`restricted`/`invalid-gtin`/`opaque`) | `app/barcodes.py`, `tests/test_barcodes.py`, `docs/barcodes.md` | done |
| Catalogue: `GET /api/items?barcode=` (exact GTIN, 422 on misread), `q=` keyword ILIKE (includes barcode substring), `/api/items/vector-search` (503 when embeddings degraded), atomic `POST /api/items/{id}/stock-adjust` with `clamped` reporting, staff-only `POST /api/items` (409 on duplicate SKU/barcode) | `app/routers/items.py` | done |
| HID capture without window focus, CSV/JSON pipelines, hardware survey | `scripts/scanner_hid.py`, `scan_intake.py`, `opticon_detect.py`, `usb_host_check.py`, `m10_scan_test.py` | done |
| Append-only migrations (`COLUMN_MIGRATIONS`/`INDEX_MIGRATIONS`); `create_all()` adds **new tables** automatically | `app/main.py`, `app/database.py`, `tests/test_migrations.py` | done |
| Purchase list | `static/app.js` — **`localStorage["hcrm_purchase"]`, client-side only, one list per browser shared across accounts, not cleared on logout, checkout is a placeholder toast** | **the fundamental gap** |

**Hardware reality** (`SCANNER-AGENT-SYNC.md`, `docs/m10-unreachable-2026-10-10.md`):
the dev unit currently enumerates as the **USB-HID keyboard personality
(`A001`)** — it types scans into the focused field and works *today* with zero
setup (five-second proof: click any text field and scan). The USB-COM
personality (`A002`, needed by `scanner_bridge.py`) is unverified since the
xHCI controller `0000:c3:00.3` died; `/dev/hidraw1` is `root:root 0600` (udev
rule not installed). **Consequence: the HID path must be a first-class,
ship-first transport (all three peer plans agree), and every server behaviour
must be testable with `TestClient` and no hardware.**

## 1. What is missing (the actual gaps)

1. **No server-side cart.** "Save whatever had been done in the current
   user's account" and "add to *that user's* cart" are impossible with a
   browser-local, user-agnostic list (which today even leaks across users on
   one browser, since `logout()` never clears `PURCHASE_KEY`).
2. **No auto-badge at account creation.** `register()` and `create_member()`
   leave `qr_badge_hash = NULL`; badges are opt-in afterwards.
3. **No "scan while logged in" at all.** Badge scans work only on the login
   page; there is no global scan listener, no same-user logoff screen, no
   30 s countdown, no switch flow, no scan-to-cart, no unknown-barcode
   intake, no attribution flow.
4. **No barcode-system suggestions.** `--fuzzy` is first-keyword-hit (opt-in,
   forbidden with stock adjust); nothing does GS1 company-prefix similarity.
5. **The bridge cannot feed a browser.** In USB-COM mode scans end at stdout;
   no channel exists from the bridge process to a logged-in SPA session.

## 2. Architecture (reconciled)

**One server-side brain for scan resolution; two transports; UI owns the
session in the core phase.**

- **Brain**: a single pure decision table — extracted from the bridge's
  tested `find_item` into `app/scanlookup.py` (glm's idea, adopted) — used by
  both `POST /api/cart/scan` (server) and `scripts/scanner_bridge.py`
  (process), so the API, the bridge and `scan_intake.py` can never drift.
  Identity rules stay exact (canonical GTIN → `items.barcode`; opaque →
  exact SKU; `invalid-gtin` → misread, never looked up; unknown → never
  auto-added, only suggested).
- **Transport 1 (primary, ships first): USB-HID keyboard wedge captured in
  the SPA.** A global keystroke accumulator with scanner-timing detection
  (inter-key < ~50 ms, Enter-terminated, focus/shape guards) plus a visible
  capture field as the deterministic fallback. Works with the hardware as it
  stands today, needs no bridge, no udev rule, no server channel.
- **Transport 2 (phase B, USB-COM deployments): server-mediated counter
  session.** The bridge becomes a dumb relay (`--relay`); the *server*
  resolves badges by hash and barcodes by the shared decision table, owns a
  per-channel counter session, and the SPA polls **non-secret** events and
  adopts the session with an opaque `terminal_id` capability. No credential
  ever rides the bus (§2.2 for why this beat the peers' alternatives).

### 2.1 Core-phase semantics (UI-owned session, both transports converge)

One `handleScan(code, source)` in the SPA classifies with the `HCRM1:` prefix
(the SPA already must recognise it to call qr-login) and routes:

**Badge scan:**
- **Nobody logged in** → existing `doBadgeLogin` path (already merged; only
  wiring).
- **Same user** (call `POST /api/auth/qr-login`, compare `user.id`; then
  *immediately revoke the just-issued spare token* via `POST /api/auth/logout`
  with it — no new endpoint, no lingering session row) → **logoff screen**:
  full-screen modal "You scanned your own badge — log off {name}?", big
  remaining-seconds display, `LOGOFF_COUNTDOWN_S = 30`, 1 s `setInterval`
  driven by a deadline timestamp (`Date.now() + 30_000`, not 30 blind ticks —
  survives tab throttling):
  - **[Stay logged in]**, Esc, or route change → cancel, interval cleared;
  - **expiry** → existing `logout()` (server-revokes the presented token,
    clears `TOKEN_KEY`/`USER_KEY`, routes to `#/login`);
  - **[Log off now]** → same as expiry;
  - **re-scan of the same badge during the countdown restarts the timer**
    (never stacks modals or double-logs-out);
  - **a different badge during the countdown cancels it and performs the
    switch** (below);
  - **barcode scans are suspended while the modal is open** (queued scan is
    dropped with a toast — peer disagreement resolved: kimi suspends
    everything, glm restarts on same-badge; I take suspend-for-barcodes +
    restart-for-same-badge + switch-for-other-badge, which is the union that
    keeps both peers' guarantees);
  - interval cleanup on dismiss/route change is mandatory (stray-timer guard).
  The countdown is a **UX affordance, not a security control** (both peers'
  framing, adopted): closing the tab abandons it, the token lives on until
  its 7-day TTL or an explicit logout. The cart is safe regardless because
  every mutation is a server write. Phase B restores server-side enforcement
  (§2.2).
- **Different user** → **save-then-switch**. With the server-side cart,
  "save whatever had been done" holds *by construction* (every add/qty change
  was persisted as it happened); the flow only needs to (i) await in-flight
  cart requests and flush any staged/debounced state (`PUT /api/cart`),
  (ii) `POST /api/auth/qr-login` with the scanned badge → **new token in
  hand first**, (iii) honour `must_change_password` on the new account
  exactly like `doBadgeLogin` does today (redirect + toast), (iv)
  `saveSession(new)`, (v) **best-effort revoke the OLD kiosk token** via
  `POST /api/auth/logout` — safe because logout revokes *only the presented
  token* (`test_logout_revokes_only_current_session`): the user's phone/laptop
  sessions survive; kiosk sessions just must not accumulate, (vi)
  `GET /api/cart` + reload catalogue/categories, toast "Switched to {name} —
  your list was saved". **Order is load-bearing: never log out first and fail
  second** — if step (ii) fails (badge rotated mid-flow), toast the error and
  stay logged in as the current user.

**Barcode scan:**
- **Logged in** → `POST /api/cart/scan {code, symbology?}`:
  - hit → row upserted (qty+1), toast «name» added, cart badge count
    refreshes; qty clamped to stock **with the clamp reported** (repo idiom
    from `stock-adjust`: `clamped: true`, never silently dropped);
  - `invalid-gtin` → 422 misread verdict → toast "misread — re-scan", nothing
    stored, nothing suggested (a misread must not masquerade as a product);
  - valid-but-unknown (`gtin`/`restricted`) → **404 with structured detail**
    `{kind, gtin14, region, usage, notes, suggestions[]}` (kimi's shape,
    adopted) → **unknown-item screen**;
  - opaque non-SKU → same 404 treatment (no GTIN prefill).
  - A badge payload posted here is **rejected 400** (invariant shared by all
    three plans: badges never reach the catalogue path).
- **Unknown-item screen** (modal): read-only scanned code + its barcode-system
  classification (region/usage/notes), the suggestion list (click one → adds
  *that* item to the cart and closes), live keyword/semantic suggestions as
  the operator types a name (existing `q=` / `vector-search` endpoints), and
  a name + price (€ → cents) + category form. Submit is **role-aware** (all
  plans agree the staff-only catalogue-write invariant must not be weakened
  by a scanner):
  - staff/admin → existing `POST /api/items` prefilled with the normalised
    GTIN; **409 duplicate-barcode responses gain a structured detail carrying
    the conflicting item** (glm's improvement, adopted) so the UI offers
    "already filed as {sku} — add that instead"; on success the new item is
    added to the cart;
  - member → `POST /api/scan-requests {code, name, price_cents}` persisted in
    a small **`ScanRequest`** table (kimi's design, adopted over glm's
    any-role `items/quick`): `GET /api/scan-requests` (staff) + resolve/
    dismiss + a counter badge in the Admin view. The member sees "reported to
    staff" (their name/price captured, nothing lost).
- **Nobody logged in** → **"which account is this for?" screen**: primary
  path "scan the member's QR badge or log in" (badge both identifies *and
  proves presence*; all plans reject an anonymous member-name/email picker —
  enumeration leak, and `GET /api/members` is rightly staff-only). The
  pending code + any anonymous scans are staged in SPA state and an
  anonymous `localStorage` staging list (kimi), **replayed after login**:
  staged lines merged via `PUT /api/cart`, pending codes re-POSTed to
  `/api/cart/scan`; secondary staff-assisted path `POST /api/cart/assign`
  (staff-only, glm D5 adopted): staff adds a resolvable item to a named
  member's cart at the counter. Staged codes live in component state, never
  in a URL. Phase B moves this staging server-side (`pending_scans`) so it
  survives browser restarts and scans taken with no kiosk page open.

### 2.2 Phase B (USB-COM bridge deployments): server-mediated counter session

Design C from my phase-1 plan, kept after comparing with glm's
SSE-plus-shared-kiosk-key relay and kimi's "defer entirely":

- `POST /api/scanner/scans` (bridge `--relay`, staff token): the **server**
  classifies the code (`parse_badge_payload` / `app/scanlookup.py`). Badge →
  resolve owner **by hash, server-side**; no badge payload or session token is
  ever queued, broadcast, or persisted in the bus. Barcode → add to the
  counter user's cart, or park in a persisted `pending_scans` row when nobody
  is bound.
- `CounterSession` table (channel PK, `user_id`, `terminal_id`,
  `pending_logoff_at`, `adopt_tokens` JSON = hashes of tokens issued via
  adopt). Channel defaults to `"counter"`.
- SPA subscribes with an opaque browser-generated `terminal_id` (128-bit
  random in `localStorage`) and **polls** `GET /api/scanner/channels/{ch}/
  events?cursor=N&terminal=T` (~700 ms, only while the counter view is open).
  Events carry only non-secret data (verdicts, item ids/names, counter-user
  public fields, deadlines). Polling instead of SSE because `EventSource`
  cannot send an `Authorization` header (glm's design forces the kiosk key
  into a query string → access logs) and SSE has proxy-buffering caveats
  (glm's own risk 6); polling reuses the existing `api()` wrapper.
- `terminal_id` is a **capability**: `POST …/adopt` exchanges "the counter
  session has a user" into a real `AuthOut` (via the existing `_issue_token`)
  only for the terminal that holds it; wrong terminal → 403. `POST …/bind`
  is the reverse (a normal password/HID login on the kiosk declares itself
  the counter user).
- **Server-side 30 s truth**: same-user badge → `pending_logoff_at =
  now + LOGOFF_GRACE (30 s, env `HCRM_LOGOFF_GRACE_SECONDS` for tests)` +
  `logoff_prompt` event; SPA shows the same countdown modal, cancel calls
  `…/cancel-logoff`. **Lazy expiry** (no scheduler needed): any touch of the
  channel (`scans`, `events`, `adopt`…) with an overdue deadline clears the
  counter user, deletes the recorded `adopt_tokens` rows from `auth_tokens`,
  and emits `logged_off` — so a browser closed mid-countdown is still logged
  off, which the pure-client timers of both peers cannot guarantee.
- **Switch**: owner ≠ counter user → the old user's cart is already durable
  (server rows), their adopt-tokens are revoked, counter user := V,
  `user_switched` event; kiosk adopts V. Same save-then-switch contract as
  §2.1, enforced server-side.
- Single uvicorn worker (as `run.sh` deploys) ⇒ the event queue is an
  in-memory per-channel deque (bounded, last ~500); `pending_scans` (the
  durable, non-secret part) is a table. Multi-worker would need a DB-backed
  bus — documented constraint, out of scope.
- Attribution upgrades: anonymous `events` responses include `pending_count`
  (login-page banner: "N scanned item(s) waiting"); after login/adopt,
  `GET …/pending` + `POST …/pending/claim` bulk-add the open rows to the
  claimer's cart (unknowns surface the item-entry screen); staff get
  resolve/dismiss and an open-list endpoint.

**Why not glm's relay** (`POST /api/scans` + SSE broadcast behind a shared
`HCRM_KIOSK_KEY`, processing stays in the UI): it broadcasts **raw badge
payloads** — bearer credentials — to every holder of a static shared secret
that must also be shipped to (or typed into) each browser; a key embedded in
JS is public, a per-kiosk typed key is an ops burden, and the key travels in
a query string. It also cannot answer "which account does this item belong
to" durably (pending lives in SPA state) and has no server-side logoff. My
phase B keeps the server as the only component that ever sees the badge
secret after the scanner, at the cost of ~2 tables and one router — which the
coordinator can still trade for glm's simpler relay if the threat model is
deemed LAN-trusted; the core phase is unaffected either way.

**Why not kimi's "defer the USB-COM channel entirely"**: the task says to use
the M-10 for both purposes; the M-10's *recommended integration* personality
is USB-COM (`docs/opticon-m10.md` §3), the bridge already exists and is
tested, and a headless counter (browser closed / no focus) is exactly what a
shop terminal is. Deferring is fine for sequencing (I agree it ships *after*
the HID core — adopted), not for scope.

## 3. Ordered implementation steps

Each step ends green (`uv run pytest`), is separately reviewable, and needs
no hardware. Suggested PR slicing in §4.

### Step 1 — Server-side per-user cart (foundation)
Files: `app/models.py`, `app/schemas.py`, new `app/routers/cart.py`,
`app/main.py`, `app/routers/items.py` + `app/routers/members.py` (cleanup),
new `tests/test_cart.py`.

- `CartItem` table: `id`, `user_id` (indexed), `item_id`, `qty ≥ 1`,
  `created_at`, `updated_at`; **unique `(user_id, item_id)`**. New table ⇒
  `create_all()` covers existing DBs; no `COLUMN_MIGRATIONS` entry (that
  tuple is for new columns on existing tables); the unique index follows the
  idempotent-DDL style. FKs stay declarative (repo decision D4) ⇒ explicit
  cleanup: deleting an item (`DELETE /api/items/{id}`) or a user
  (`DELETE /api/members/{id}`) removes their cart rows, mirroring the
  `auth_tokens` convention.
- Schemas (`_Stripped`/`extra="forbid"` conventions): `CartLineOut {item:
  ItemOut, qty, clamped?}`, `CartOut {lines, total_cents}`, `CartAddIn
  {item_id, qty=1}`, `CartLineDeltaIn {delta}`, `CartQtyIn {qty}` (0 =
  remove), `CartPutIn {lines:[{item_id, qty}]}`.
- Endpoints (all `get_current_user`, any role — a cart is not role-gated):
  - `GET /api/cart`;
  - `POST /api/cart/items {item_id, qty=1}` — add/bump;
  - `POST /api/cart/lines {item_id, delta}` — **atomic increment/decrement**
    (kimi's delta API, adopted: the natural shape for repeated scans;
    implemented as upsert-under-unique-key in one transaction; SQLite
    serialises writers, so two rapid scans of the same item both land —
    tested, per glm's review-pass item);
  - `PATCH /api/cart/items/{item_id} {qty}` — set (0 deletes);
  - `PUT /api/cart` — **replace whole cart** (kimi, adopted: the switch-flush
    and the anonymous-staging replay path; validates ids, transactional
    delete+insert; two kiosks on one account = documented last-writer-wins);
  - `DELETE /api/cart/items/{item_id}`, `DELETE /api/cart`.
  - **Stock policy**: qty clamps to `item.stock` with `clamped: true`
    reported in the response (glm's parity with today's UI clamps + the
    repo's "report, never silently drop" idiom from `StockAdjustOut`).
    Adding to cart does **not** decrement stock (all three plans agree;
    checkout stays a tracked non-goal — kimi Q4 resolved).
- Tests (`tests/test_cart.py`): auth required; add/inc/dec/set/remove/clear;
  `PUT` replace semantics; qty validation (422 on negative, 404 unknown item);
  clamping reported; **per-user isolation** (Alice never sees/mutates Bob's);
  rows die with the item and with the user; `must_change_password` gate
  applies; **concurrency**: two parallel `POST /lines {delta:+1}` both
  survive (modelled on `tests/test_concurrency.py`).

### Step 2 — Shared scan-resolution module, `/api/cart/scan`, suggestions, member requests
Files: new `app/scanlookup.py`, `app/barcodes.py`, `app/routers/cart.py`,
`app/routers/items.py`, `scripts/scanner_bridge.py` (refactor only),
`app/models.py` + `app/schemas.py` (`ScanRequest`), `app/main.py`;
tests: new `tests/test_scanlookup.py`, `tests/test_cart_scan.py`,
`tests/test_items_suggest.py`, `tests/test_scan_requests.py`; extend
`tests/test_barcodes.py`; `tests/test_scanner_bridge.py` stays green.

- **`app/scanlookup.py`** (adopted from glm Task 5): extract the bridge's
  `find_item` decision core into a **pure** module — no DB, no HTTP; it takes
  injected lookup callbacks (`gtin_lookup(key) -> item|None`,
  `sku_lookup(code) -> item|None`) plus `(code, symbology)` and returns the
  verdict `(item|None, how, analysis)`. `scanner_bridge.find_item` becomes a
  thin wrapper over it keeping its injected-`get` HTTP seam (existing bridge
  tests pass unchanged); the server implements the callbacks with SQLModel
  queries. One implementation, three consumers, zero drift.
- **`POST /api/cart/scan {code, symbology?}`** (authenticated, any role):
  rejects `HCRM1:` payloads with 400 (badges never reach the catalogue);
  runs `scanlookup`; hit → cart upsert (Step 1) → `200 {status:"added",
  item, qty, clamped, cart}`; misread → `422 {status:"misread", analysis}`;
  unknown → `404 {status:"unknown", kind, gtin14, region, usage, notes,
  suggestions[]}` (kimi's structured detail). This is the endpoint the SPA
  calls for HID-captured scans *and* the resolution core Phase B ingest
  reuses.
- **Suggestions — "closest matching items using the barcode system"**:
  - new pure helper `prefix_candidates(gtin14) -> list[str]` in
    `app/barcodes.py`: progressively shorter GS1 company-prefix LIKE patterns
    over the zero-padded GTIN-14 (longest prefix = closest sibling; same
    prefix ⇒ same manufacturer/brand ⇒ the usual substitute);
  - `GET /api/items/suggest?barcode=&q=&limit=` →
    `{by_prefix:[ItemOut…], by_keyword:[…], by_semantic:[…]}`;
    `by_semantic` wraps `vector_search` and **degrades to `[]` on 503** —
    suggestions must never fail the scan flow;
  - the 404 detail from `/cart/scan` embeds `by_prefix` (+ keyword hits on
    the code); the unknown-item form adds live `q=`/vector-search as the
    operator types a name (existing endpoints, no new work).
  - Cap ~5 per group; never auto-added, clearly labelled (REVIEW-m10 P1).
  - Scale note: LIKE on the indexed `barcode` column, fine for shop
    catalogues; documented.
- **`ScanRequest`** (kimi, adopted): table `id`, `user_id`, `code`,
  `symbology`, `name`, `price_cents`, `status(open|resolved|dismissed)`,
  `resolution_item_id?`, timestamps. `POST /api/scan-requests` (any
  authenticated user, from the unknown-item screen), `GET /api/scan-requests`
  (staff), `POST /api/scan-requests/{id}/resolve` (staff; may reference the
  item they created) / `…/dismiss`. Admin-view badge count in Step 4.
- Tests: `test_scanlookup.py` — decision-table parity (GTIN any
  representation incl. UPC-E→EAN-13, opaque exact-SKU-only, bare 6 digits
  never guessed, misread never looked up, restricted-circulation matches);
  `test_cart_scan.py` — added/increment-on-rescan, clamp reporting, misread
  422 stores nothing, unknown 404 + prefix-ordered suggestions excluding
  unrelated items, badge payload → 400, unauthenticated → 401;
  `test_items_suggest.py` — prefix ranking, keyword, semantic degradation
  with embeddings off, 401; `test_scan_requests.py` — member creates, staff
  lists/resolves, member cannot list; `test_barcodes.py` — `prefix_candidates`
  shapes (GTIN-14 in, prefix floor, no LIKE-wildcard injection).

### Step 3 — Auto QR badge at account creation
Files: `app/routers/auth.py`, `app/routers/members.py`, `app/schemas.py`;
tests: extend `tests/test_qr.py`, `tests/test_auth.py`, member tests.

- `register()`: generate the badge **in the same transaction**
  (`new_badge_payload()` → store `token_hash`). Response schema
  **`RegisterOut(AuthOut)` with `badge: QrBadgeOut`** (kimi's subclass —
  cleaner than my phase-1 optional field on `AuthOut`, because `AuthOut` is
  also the *login* response and login must never carry badge material;
  glm's "new schemas, don't touch `UserOut`" principle, adopted).
- `create_member()`: same generation; response `MemberCreatedOut(UserOut +
  badge: QrBadgeOut)` (additive superset; existing field-wise assertions keep
  passing — verify `test_metadata_and_ops.py`'s OpenAPI expectations).
- **Badge material appears exactly once, in the creation response** — never
  in `/me`, member listings, or later logins (asserted in tests; final
  security pass greps `QrBadgeOut` usages).
- Seed/dev-admin accounts: deliberately **no** auto-badge (a payload nobody
  can see is dead weight; printing secrets to logs is worse — all three
  plans agree). No bulk backfill for existing accounts: impossible by design
  (payload shown once); they keep the existing generate/rotate buttons.
  Documented in README + `app/seed.py` docstring.
- Tests: register → response badge parses, hash stored, `has_qr_badge`
  true, **that badge logs in via `qr-login`**; staff-created member likewise;
  rotate kills the creation badge; badge absent from `/me`, `GET
  /api/members`, and subsequent `login` responses; `must_change_password`
  gate still applies to badge login of flagged accounts (existing test
  pattern).

### Step 4 — SPA core flows (HID-primary; the heart of the task)
Files: `static/app.js`, `static/index.html`, `static/style.css`,
`scripts/ui_check.py` scenarios; no new backend.

- **Cart becomes server-backed**: `purchase` state loads from `GET /api/cart`
  after login/switch; +/−/remove/clear call the Step-1 endpoints (keep the
  look and the stock hints); **one-time legacy migration**: on first
  authenticated fetch, if the server cart is empty and `hcrm_purchase`
  exists, `PUT` the still-valid entries and delete the key (also fixes the
  cross-user leak). Anonymous staging list for the logged-out case (§2.1)
  reuses the same key, replayed on login.
- **Global scan capture** (kimi Task 4 heuristics + glm D2.1 focus rules,
  adopted): capture-phase `keydown` accumulator — buffer while inter-key
  delay < ~50 ms, flush on Enter; ignore slow human typing; never hijack a
  focused input/textarea **unless** scanner timing matches; belt-and-braces
  shape gate before treating a buffer as a scan (`HCRM1:` prefix, GTIN-ish
  digits, or plausible SKU length) so a fast-typed password can never be
  submitted as a scan (glm risk 4 mitigation). The login-page badge field
  keeps its exact current behaviour (regression-sensitive). Plus a **visible
  "Scan items / badge here" capture field** on catalogue/counter views
  (autofocus, re-focus on blur unless another input is focused) — the
  deterministic path when heuristics misfire — and a scan-received indicator
  toast so silent misroutes are visible. All captures feed one
  `handleScan(code, source)`.
- **Badge flows**: implement §2.1 exactly — same-user detection via
  `qr-login` + immediate revoke of the spare token; countdown modal
  (deadline-driven, restart-on-same-badge, switch-on-other-badge,
  barcodes suspended with a toast, interval cleanup on dismiss/route
  change); save-then-switch in the mandated order (flush → qr-login new →
  `must_change_password` branch → saveSession → best-effort revoke old →
  reload cart; abort loudly if qr-login fails, never log out first).
- **Barcode flows**: `POST /api/cart/scan` → toast/refresh on added;
  misread toast; 404 → **unknown-item modal** (classification block,
  grouped suggestions — click adds that item; name/price/category form;
  staff submit → `POST /api/items` with 409-conflict "add that instead";
  member submit → `POST /api/scan-requests` + "reported to staff" toast);
  **attribution view** when logged out ("Which account is this for? Scan a
  badge or log in"), staging + post-login replay, staff `POST
  /api/cart/assign` helper from the counter view.
- **Register/member-create screens**: show the returned one-shot badge SVG
  in the existing print modal with a "print/acknowledge now — shown only
  once" warning (kimi's over-the-shoulder note, adopted).
- **Admin view**: `ScanRequest` counter badge + resolve/dismiss list.
- Verification: the repo has no JS test runner and must not gain one — keep
  JS changes small and isolated; semantics live server-side and are
  pytest-covered; add `scripts/ui_check.py` scenarios where cheap (badge
  shown after register; capture field present; countdown modal renders with
  a stubbed scan) + the manual hardware checklist (§Step 7). Extend
  `scripts/smoke_test.py` with cart + `/api/cart/scan` against the live
  subprocess server.

### Step 5 — Phase B: counter sessions, event bus, bridge relay (USB-COM path)
Files: `app/models.py` (`CounterSession`, `PendingScan`), new
`app/scannerbus.py`, new `app/routers/scanner.py`, `app/main.py`,
`scripts/scanner_bridge.py` (`--relay`), `static/*` (counter view);
tests: new `tests/test_scanner_api.py`, bridge relay additions.

Server surface (per §2.2): `POST /api/scanner/scans` (staff; badge →
server-side hash resolution + same/other/none counter-session transitions;
barcode → cart add or `pending_scans`; lazy `_expire_due` before processing;
returns the verdict for bridge `--json` output), `GET …/events?cursor&
terminal` (public read — events are non-secret by construction; includes
`counter` snapshot + `pending_count`), `POST …/bind`, `POST …/adopt`
(terminal_id capability → `AuthOut` via `_issue_token`, hash recorded in
`adopt_tokens`), `POST …/cancel-logoff`, `POST …/logoff`, `GET …/pending`,
`POST …/pending/claim`, staff `pending/{id}/resolve|dismiss`. Constants:
`LOGOFF_GRACE_SECONDS = 30` (env `HCRM_LOGOFF_GRACE_SECONDS`).

- **30 s logoff, server truth**: same-user badge sets `pending_logoff_at`;
  `logoff_prompt {deadline}` event drives the same SPA countdown modal;
  cancel clears; **lazy expiry** on any channel touch clears the counter
  user, revokes recorded adopt tokens, emits `logged_off` — the closed-tab
  case the pure-client timers cannot cover.
- **Switch**: cart already durable; revoke old adopt tokens *for this
  channel* (the user's other devices keep their sessions — same principle as
  `logout`); set counter user; `user_switched`; kiosk adopts **before**
  replacing the local session; adopt failure ⇒ stay as-is + toast.
- **Attribution**: `pending_count` on the anonymous events poll powers the
  login-page banner; after bind/adopt the attribution modal offers
  [Add all to my cart] (`pending/claim`; unknowns surface the item-entry
  screen) or [Leave for staff].
- **Bridge `--relay [CHANNEL]`**: mutually exclusive with
  `--stock-in/--stock-out/--fuzzy` (all decisions move server-side); relays
  *every* scan verbatim — badges included, **not** exchanged locally (avoids
  double sessions); reuses `call_with_relogin` (P2 contract) and the bounded
  `api()`; prints the server verdict per scan.
- Tests (TestClient, `HCRM_LOGOFF_GRACE_SECONDS=1` or monkeypatched
  `utcnow`): ingest classification parity with `scanlookup`; badge with no
  counter user → set + `user_switched`; adopt with right/wrong terminal
  (200/403); adopted token works on `/me`; **same-user badge → `logoff_prompt`
  deadline ≈ now+30 s; cancel clears; expiry → counter user None, adopt
  token revoked (`/me` 401), `logged_off`**; **different-user badge → old
  user's cart rows intact (the save assertion), new adopt yields the new
  user's empty cart, old adopt token revoked, other-device token of the old
  user unaffected**; same-badge re-scan restarts the deadline; different
  badge during countdown switches; no-user barcode → `pending_count` visible
  unauthenticated, claim lands rows in the claimer's cart, claim is
  idempotent, staff dismiss/resolve, member cannot resolve; ingest requires
  staff (member → 403); channel isolation; restart-safety note (in-memory
  events lost on restart — durable state is in tables; re-scan recovers).
  Bridge: relay posts verbatim, badge not consumed locally, `ApiError` per
  scan keeps the listener alive, flag exclusions are a startup `SystemExit`,
  `--json` shape.

### Step 6 — Migrations check
`tests/test_migrations.py`: `create_all()` on an "old" DB gains `cart_items`,
`scan_requests`, `counter_sessions`, `pending_scans`; append-only tuples
untouched (the round-5 collision lesson).

### Step 7 — Docs, security review pass, hardware checklist
- `docs/opticon-m10.md` new §11 "Counter/kiosk mode": personalities (HID
  direct vs USB-COM relay), capture heuristics, 30 s logoff semantics (client
  UX + phase-B server truth), save-then-switch order, attribution,
  `HCRM_LOGOFF_GRACE_SECONDS`; cross-link §6a/§9. `docs/barcodes.md`: the
  company-prefix suggestion rule and why misreads are never suggested.
  `README.md`: features, endpoint table (cart/scan/scanner), env vars,
  tracked-issues update (purchase list no longer client-only; checkout still
  open). `NOTES.md`/`PLAN-v2.md`: pointer entries only (historical records).
- **Security & edge-case review pass** (glm Task 9, adopted): badge payload
  appears only in creation/rotate responses (grep `QrBadgeOut`); no badge
  payload or token in bus events / `pending_scans` / logs; phase-B
  comparisons of `terminal_id` use `hmac.compare_digest`; events responses
  `Cache-Control: no-store`; countdown intervals cleaned up on route change;
  cart race test present; quick paths cannot patch existing items.
- **Hardware checklist** (blocked on the xHCI fix / udev rule from
  `SCANNER-AGENT-SYNC.md`): HID five-second typing test → SPA capture;
  USB-COM: `opticon_detect.py` → `scanner_bridge.py --relay` end-to-end with
  two printed badges (same-user countdown incl. closing the tab mid-count,
  different-user switch with cart persistence check) and one unknown barcode
  (staff create + member request).

## 4. Merge/PR slicing

Steps 1 → 2 → 3 are backend-only, independent except 2 depends on 1; each
leaves `uv run pytest` green. Step 4 lands incrementally (cart UI first, then
capture + flows, then modals). Step 5 is a self-contained stack the
coordinator may reorder, swap for glm's relay (§2.2 trade-off), or defer —
the core task is satisfied by Steps 1–4 on today's HID hardware. Steps 6–7
close out. Migration-touching changes stay append-only.

## 5. Risks

- **R1 Hardware**: USB-COM unverified (dead xHCI `0000:c3:00.3`, unit in HID
  mode, hidraw root-only). Mitigation: HID-first core needs no bridge/udev;
  all server logic TestClient-tested; relay unit-tested with injected HTTP.
  Owner actions: install `scripts/udev/99-opticon-scanner.rules`, revive the
  controller, run the Step-7 checklist.
- **R2 HID capture false positives** (fast human typing read as a scan —
  the riskiest pure-frontend piece, glm risk 4): timing threshold + Enter
  terminator + focus rules + payload shape gate + visible capture field as
  the deterministic fallback; login-page badge field untouched.
- **R3 Bearer credentials**: badges are password-equivalent. Phase A never
  exposes them beyond the existing qr-login call; Phase B keeps payloads
  server-side (no bus broadcast, hash-only lookups, `terminal_id`
  capability). Phase-B anonymous `events` read reveals *which codes* are
  scanned at the counter to a LAN peer — non-secret by classification,
  documented.
- **R4 30 s timer**: Phase A countdown is UX, not a security control — a
  closed tab skips the logoff and the token lives up to its 7-day TTL (both
  peers accept this; cart is safe regardless). Phase B closes it with lazy
  server expiry. Optional extra: shorter TTL for adopt-issued tokens.
- **R5 Single-worker bus** (Phase B): in-memory events assume one uvicorn
  worker (matches `run.sh`); multi-worker needs a DB-backed bus — documented.
- **R6 Cart concurrency**: same account on two kiosks → `PUT` is
  last-writer-wins, line deltas are atomic; documented, no CRDTs (kimi).
  Adding does not reserve stock; two carts can exceed stock (checkout is a
  non-goal) — clamp reports, never blocks a future checkout design.
- **R7 Response-shape changes**: `RegisterOut`/`MemberCreatedOut` are
  additive; items 409 detail becomes structured (keep the human string in
  `detail` for compatibility); `test_metadata_and_ops.py` OpenAPI snapshot
  may need a refresh.
- **R8 Legacy import** may reference deleted items — skip unknown ids, then
  clear the key.
- **R9 Register response carries a printable secret** (once): print/
  acknowledge UX, never logged (kimi).
- **R10 `must_change_password` mid-switch**: the new account may be gated to
  the password-change view; reuse the existing branch + say so in the switch
  toast (kimi risk, adopted).

## 6. Open questions for the coordinator/owner

1. **Member-created catalogue entries**: all three plans keep `POST
   /api/items` staff-only; glm alone proposed an any-role `items/quick`
   (rejected here — §7). Confirm the `ScanRequest` route is acceptable, or
   decide whether a member-run counter should auto-provision staff (recommend
   against).
2. **Switch = revoke old kiosk token?** This plan (with kimi) revokes the
   *presented* old token after the new one is secured; glm preferred dropping
   it unrevoked. Revocation is safe for other devices (logout semantics are
   tested) and stops kiosk-session accumulation — recommend keeping.
3. **Phase B relay design**: server-mediated counter session (this plan) vs
   SSE + shared kiosk key (glm) vs defer (kimi). Recommendation: build Phase
   B as specified here; if the coordinator judges the LAN fully trusted,
   glm's simpler relay is an acceptable downgrade — the core phase does not
   depend on the choice.
4. **Channel binding granularity** (Phase B): one channel per physical
   counter, latest bind wins, previous terminal gets a "taken over" event and
   stops polling — confirm vs per-tab binding.
5. **Stock clamping in carts**: adopted (parity with today's UI, reported via
   `clamped`); confirm the owner doesn't want overfill purchase lists.

## 7. Peer review: adopted / rejected

### From plan-glm
**Adopted**: shared extraction of the bridge's `find_item` decision core into
`app/scanlookup.py` (replaces my phase-1 parallel-helper idea — one
implementation, no drift); duplicate-barcode 409 returning the conflicting
item so the UI can offer "add that instead"; D5 attribution shape — badge
scan primary, staff-assisted `POST /api/cart/assign` secondary, no anonymous
picker; HID global capture as the ship-first primary transport (D2.1) with
focus rules and shape gating; cart qty clamped to stock (with the repo's
report-don't-drop idiom); the Task-9 security/edge-case review pass (folded
into Step 7) incl. the cart concurrency test; new response schemas instead of
mutating shared ones; explicit "badge payload on the cart-scan endpoint is
rejected" invariant; `ui_check.py` scenarios as the frontend verification
story.
**Rejected**: (a) **D2.2 SSE relay behind `HCRM_KIOSK_KEY`** — broadcasts raw
badge payloads (bearer credentials) to every holder of a secret that must
reach browsers (static key in JS ≈ public; typed key = ops burden), pushes
the key into query strings because `EventSource` cannot send `Authorization`,
inherits SSE proxy-buffering caveats (glm's own risk 6), and leaves pending
scans and the 30 s logoff without durable/server-side answers; Phase B uses
server-side resolution + non-secret polling events + `terminal_id` capability
instead. (b) **D4a `POST /api/items/quick` for any role** — weakens the
deliberate staff-only catalogue invariant; physical possession of an item is
not a privilege grant; `ScanRequest` (kimi) covers the member flow. (c)
**`POST /api/auth/qr-identify`** — a new unauthenticated probe endpoint
(enumeration/timing surface) for something `qr-login` + immediate revoke of
the spare token already solves with tested code. (d) **Flow C "old token
dropped, not revoked"** — logout revokes only the presented token (tested),
so revoking the kiosk's old token cannot hurt the user's other devices, and
unrevoked kiosk sessions accumulate; revoke-after-new-secured instead.
(e) **D6 client-only 30 s timer as the whole story** — accepted for Phase A,
but Phase B keeps server-side lazy expiry so a closed tab still ends in
logoff.

### From plan-kimi
**Adopted**: HID-first sequencing with the USB-COM channel *after* the core
(but in scope — see rejected); global-capture heuristics (< ~50 ms inter-key,
Enter terminator, slow-typing and focused-input exemptions, login-field
non-regression); `PUT /api/cart` replace + `POST /api/cart/lines {delta}`
shapes and the documented last-writer-wins stance; 404-with-structured-detail
(`kind/gtin14/region/usage/notes/suggestions`) from `/cart/scan`; the
`ScanRequest` table + staff list/resolve + Admin badge for member unknown-item
reports; `RegisterOut(AuthOut)` subclass so login responses never carry badge
material; countdown modal suspends barcode handling (merged with glm's
restart-on-same-badge — §2.1); anonymous staging list + post-login replay for
the logged-out attribution flow; explicit resolutions of Q2 (expiry
auto-logs-off, cancel button) and Q4 (no stock decrement on add);
`must_change_password` handling in the switch flow; register-badge
show-once/print UX; "timer is UX not security" framing for Phase A.
**Rejected**: (a) **Task 8 "defer the USB-COM kiosk channel to maybe-never"**
— the task mandates the M-10 for both purposes, USB-COM is the documented
integration personality, the bridge exists and is tested, and a headless
counter is the real shop deployment; deferred in *sequence* (agreed), kept in
*scope* as Phase B. (b) **SPA-state/localStorage as the only memory for
pending attribution** — lost on refresh/close and blind to scans taken while
no kiosk page runs (the exact case the bridge relay exists for); kept for
Phase A staging, superseded by server-side `pending_scans` in Phase B.
(c) Delta-only line API without a reported stock clamp — clamping with
`clamped: true` (glm parity + repo idiom) chosen over both silent clamping
and unbounded carts.

### Three-way agreements (coordinator fast-path)
Server-side per-user cart as the foundation; auto-badge at `register` +
`create_member` with one-time payload return, seeds excluded, no backfill;
exact-identity resolution via `analyze_scan` with misreads never matched and
unknowns never auto-added; GS1 company-prefix + keyword + semantic
suggestions; staff-only catalogue writes; no anonymous account picker
(enumeration); no stock decrement on cart-add; badge flows reuse the merged
`qr-login` endpoint verbatim.

## 8. Explicitly out of scope

Real checkout/orders and stock reservation; multi-worker event bus;
WebSockets/SSE; a JS test runner/Node toolchain; badge rate-limiting (256-bit
payloads, `docs/opticon-m10.md` §9.4 rationale); PNG badges; any behaviour
change to the existing bridge modes (`--stock-in/--stock-out/--fuzzy`) or the
OPN-2001 tooling.
