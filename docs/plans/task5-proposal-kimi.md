# Task 5 proposal (kimi-k3): SPA — cart on API, global scan capture, badge & barcode flows

Coordinator reference: `docs/plans/COORDINATOR.md` Task 5 (lands
incrementally: cart UI first, then capture+flows, then modals). Backend
(Tasks 1–4) is in the tree; this proposal is frontend-only:
`static/app.js` (Vue 3 options API, vendored, no build), `static/index.html`,
`static/style.css`. Verified current code: `api()` wrapper (401 → broadcast
`hcrm-session-expired` + redirect), `saveSession`/`logout`, `PURCHASE_KEY`
localStorage list + watcher, badge login field (`badgeCode` + Enter),
`memberBadge` print modal (`.modal-backdrop`/`.modal` + `window.print()`),
toast pattern, `mounted()` listener registration, `scripts/ui_check.py`
(DOM/console gate — no scripted login, by design).

**State-shape luck:** the server's `CartLineOut` is `{item, qty,
line_total_cents, …}` — the existing panel template already renders
`entry.item.name` / `entry.item.price_cents` / `entry.qty`. The panel
markup survives almost unchanged.

---

## 1. Cart on the API

**Data:** replace `purchase: JSON.parse(localStorage…)` with
`cart: { lines: [], total_qty: 0, total_cents: 0 }`; **delete the
`purchase` watcher** (the localStorage write-back). `purchaseTotalCents`
computed is replaced by `cart.total_cents` (server-computed, live prices).

**Load** — one method, called from every session-entry point (`mounted`
when `this.user`, end of `doLogin`/`doBadgeLogin`/`doRegister`, and the
switch flow):

```js
async loadCart() {
  this.cart = await api("/cart");            // GET /api/cart
  await this.importLegacyPurchase();          // one-time, below
}
```

**One-time legacy import** (`PURCHASE_KEY`):
1. Key absent or `[]` → nothing.
2. Server cart non-empty → server wins; `localStorage.removeItem(PURCHASE_KEY)`.
3. Server cart empty and legacy list non-empty → try `PUT /api/cart
   {lines: legacy.map(e => ({item_id: e.item.id, qty: e.qty}))}` (atomic);
   on 404/422 (some legacy items were deleted from the catalogue — the
   all-or-nothing PUT rejects) fall back to per-line `POST /cart/items`,
   skipping failures silently (they are unreferenceable). Either way
   `removeItem(PURCHASE_KEY)` afterwards — the key dies on first contact,
   so the import can never double-run.

**Mutations** (mutators return the full `CartOut` — no read-after-write;
each chained onto a queue, below):

| UI action | Call |
|---|---|
| `addToPurchase(item)` | `POST /cart/items {item_id, qty: 1}` |
| `incPurchase` | `POST /cart/items {item_id, qty: 1}` (upsert +1) |
| `decPurchase` | `PATCH /cart/items/{id} {qty: e.qty - 1}` (0 removes) |
| `removeFromPurchase` | `DELETE /cart/items/{id}` |
| `clearPurchase` | `DELETE /cart` |

Each sets `this.cart = response`. The stock hint becomes
`v-if="entry.qty > entry.item.stock"` on the line ("Only N in stock"
class) — coordinator decision 6: warn, never clamp.

**Ordered mutation queue** (the save-then-switch flush primitive):

```js
this._cartQueue = Promise.resolve();
_cartOp(fn) { const p = this._cartQueue.then(fn); this._cartQueue = p.catch(() => {});
              return p; }
async flushCart() { await this._cartQueue; }
```

Every mutation runs through `_cartOp` — rapid +/− clicks can't reorder
requests, and **"save whatever had been done in the current user's
account" is `await this.flushCart()`**: every mutation is already a
committed server write (Task 1), so the flush is draining the queue, not
persisting state. `PUT` is used only for the legacy import and the staged
replay (§3), not per switch.

`requestPurchase()` stays a toast (checkout is out of scope) but reads
`cart.total_cents`/`total_qty`.

## 2. Global HID scan capture

One document-level listener, installed in `mounted()`
(`document.addEventListener("keydown", this._onScanKey, true)`), state on
`this._scan = { buf: "", last: 0 }` (non-reactive internals, like
`this._observer`):

1. **Focus rule (the anti-hijack guard):** if `event.target` is
   `input`/`textarea`/`select`/`contenteditable`, return immediately —
   typed secrets and form input can never be swallowed. The login badge
   field keeps its existing v-model + Enter-submit behaviour untouched
   (regression-pinned by ui_check, §6).
2. **Timing rule:** a printable char with `now - this._scan.last > 50 ms`
   resets the buffer (human pause); chars within 50 ms append. Ignore
   anything with Ctrl/Alt/Meta.
3. **Terminator:** Enter while the buffer is non-empty **and** the burst
   was scanner-paced → `candidate = buf`, clear, `handleScan(candidate)`.
4. **Routing gate (badge vs barcode, not a validity check):** the server
   owns classification (Task 3); the client only needs to pick the flow —
   `candidate.startsWith("HCRM1:")` → badge, everything else → barcode.
   No GTIN/shape validation client-side: with the focus rule, a
   scanner-paced Enter-terminated burst on a non-editable target is a scan;
   a garbage one costs a 404/422 toast ("misread, re-scan" is the server's
   verdict string, surfaced verbatim). This deliberately lets Code 128 SKU
   labels work globally (they are the shop's own barcodes).
5. **Modal discipline:** scans are ignored while any modal is open —
   **except** the logoff overlay, where the listener stays live (same-badge
   rescan must restart; a different badge must cancel-and-switch, §3).
   During the overlay, product barcodes are ignored with a hint toast.
6. **Feedback:** every routed scan also toasts ("scan received …") —
   silent misroutes must be visible. Success/error toasts from the flows
   follow.

No visible capture field: the global listener covers every view; the
badge field remains only on the login page (logged-out badge login, its
current job). This reverses my phase-2 "capture field for opaque" hedge —
with the focus rule the false-positive surface is already closed, and a
field that must stay focused fights the SPA's own inputs.

## 3. `handleScan(code)` — the bus and the flows

One method; four branches on `(isBadge, !!this.user)`.

### 3a. Badge, logged out
Refactor the existing `doBadgeLogin` body into `badgeLogin(code)` (form
submit passes `this.badgeCode`, the bus passes the candidate) →
`POST /auth/qr-login` → `saveSession` → `loadCart()` →
`replayStagedScans()` (§3d) → `must_change_password` branch unchanged.

### 3b. Badge, logged in
`POST /auth/qr-identify {token: code}` →

- **`same_user: true` → logoff overlay.**
  `POST /auth/logoff-schedule` → `{logoff_deadline, grace_seconds}`;
  `this.logoff = { deadline, remaining, timer }` and the overlay shows.
  - Countdown: `setInterval(1 s)`;
    `remaining = Math.max(0, Math.ceil((Date.parse(deadline + "Z") - Date.now()) / 1000))`
    — computed from the **server deadline** (naive-UTC ISO ⇒ append `"Z"`
    when parsing), so poll latency and client clock skew don't matter.
  - `remaining === 0` → `performLogoff()`: `POST /auth/logout` inside
    try/ignore (the token is already dead server-side — the 401 path of
    `api()` also fires; both end at the same place), clear timer +
    session, route `#/login`, toast "Logged off".
  - **[Stay logged in]** → `POST /auth/logoff-cancel`, clear timer, close
    overlay, toast "Logoff cancelled".
  - **[Log off now]** → same as expiry, immediately.
  - **Same badge re-scanned while open** → `qr-identify` again →
    `same_user` → `logoff-schedule` **again** (server overwrite =
    restart), deadline/remaining reset from the new response. No stacked
    overlays (one `this.logoff` object).
  - **Different badge while open** → `qr-identify` → `false` →
    `logoff-cancel` → close overlay → fall into the switch flow below.
  - Timer hygiene: cleared on cancel / expiry / switch / route change /
    `hcrm-session-expired` (whose handler also nulls `this.logoff`).
  - The overlay has **no backdrop-click dismiss** and no ✕ — leaving is a
    deliberate act (button, expiry, or a badge decision).
- **`same_user: false` → save-then-switch** (coordinator decisions 2–3):
  1. `await this.flushCart()` — the outgoing user's work is durable
     (already committed writes; the queue drains).
  2. `POST /auth/qr-login {token: code}` — **the new session first**;
     on failure (garbage badge) stay exactly as we are, error toast, done.
  3. New session secured ⇒ `POST /auth/logout` **with the old token
     still in localStorage** (call before `saveSession`), errors ignored
     (an already-dead old token 401s the wrapper into clearing state —
     handled: saveSession afterwards regardless).
  4. `saveSession(new)` → `loadCart()` (incoming user's list restored) →
     route catalogue → toast "Switched to ‹name› — your list was saved".
  5. `must_change_password` on the new account → the existing
     forced-change redirect (same branch as `badgeLogin`).
  6. `busy` guards the whole flow against double-scans/clicks.

### 3c. Barcode, logged in
`POST /cart/scan {code}` (catch by status, reading the structured
`detail` objects — `api()` currently stringifies details, so the scan
path uses a small `apiRaw` variant that returns `{status, json}`; the
existing `api()` is untouched for everything else):

- **200** → `this.cart = data.cart`; toast "‹name› added (qty N)" +
  stock warning when the line exceeds stock.
- **422** (`status:"misread"`) → toast the server's misread message; no
  modal, nothing staged.
- **400** (`status:"badge"`) → unreachable from correct routing; toast.
- **404** (`status:"unknown"`) → open the **intake modal**
  (`this.unknownScan = detail`):
  - Header: the code, `message` (bridge-verbatim verdict incl. the UPC-E
    hint), and the barcode-system line from `analysis`
    (`format`/`region`/`usage`/`notes`).
  - **Suggestions** (`by_prefix` + `by_keyword`, re-queried live as the
    name is typed via `GET /items/suggest?q=…&barcode=…`, debounced 300 ms
    — that's where `by_semantic` appears): each row a button →
    `POST /cart/items {item_id}` → cart update, close, toast.
  - **Form**: name, price (€→cents), and for staff a SKU field prefilled
    with `analysis.gtin14` when `catalogable` (unique and meaningful),
    empty+required for opaque codes.
    - **Staff submit** → `POST /api/items {sku, name, price_cents,
      barcode: catalogable ? analysis.gtin14 : null, stock: 0}` → 201 ⇒
      `POST /cart/items` add, close, toast. 409 (duplicate barcode — filed
      meanwhile) ⇒ `GET /items?barcode=` fetch the existing item, add
      *that*, toast "already filed as ‹sku› — added instead" (the
      pre-check-by-fetch pattern; no server change).
    - **Member submit** → `POST /scan-requests {code, symbology: "",
      name, price_cents}` → 201 ⇒ toast "Sent to staff — they'll add it".
      409 `already` ⇒ add `detail.item` to cart instead. 409 `duplicate`
      ⇒ toast "already reported — staff know".
    - **Cancel** → close; the scan is dropped (re-scanning starts fresh).

### 3d. Barcode, logged out — attribution (staged replay)
Decision: **in-memory staging + replay after login** — the simplest flow
that is correct with the *existing* endpoints. (`POST /api/cart/assign`
was never built — noted as a Phase-B/option; an anonymous member picker is
a rejected enumeration leak; the badge scan IS the primary attribution
answer: it identifies the account and logs in.)

- Scan while logged out → push onto `stagedScans: []`; the login view
  shows a banner: **"N item(s) scanned — scan your QR badge or log in to
  add them to your list."** (In-memory only: a page reload drops them —
  documented limitation, kiosk-acceptable.)
- `replayStagedScans()` after any login/switch: for each staged code,
  `POST /cart/scan`; successes add (and toast once, summarised); codes
  that 404 are re-staged, and after the pass the intake modal opens for
  the first unknown; the banner persists while `stagedScans.length`.
- `PUT` is not needed here (per-code scan POSTs preserve the unknown-
  handling); `PUT` stays the legacy-import primitive only.

## 4. One-shot badge print (register + staff member-create)

Generalise the existing `memberBadge` modal into one
`badgePrint: { title, subtitle, svg } | null` modal (same
`.modal-backdrop`/`.modal`/`.qr-badge` markup + `window.print()` button):

- **`doRegister`**: the 201 now carries `qr_badge` → after `saveSession`
  open `badgePrint` with a "shown only once — print it now" subtitle, then
  continue to the catalogue behind the modal. **The `payload` string is
  never stored in component state** — only the SVG markup (in-memory,
  cleared on close; nothing touches localStorage).
- **Staff member-create** (`createMember` in the admin view): the 201
  carries `qr_badge` → open the same modal instead of requiring a second
  "QR badge" click. The existing rotate buttons (Account, Members) migrate
  to the shared modal (mechanical; same markup it already is).
- Copy: "whoever holds this code can log in — hand it over privately"
  (existing memberBadge text, reused).

## 5. `index.html` / `style.css`

- **Purchase panel**: render `cart.lines` (fields align, §1); add the
  `stock-warn` line class when `qty > item.stock`; totals from
  `cart.total_cents`/`total_qty`.
- **New markup** (all Vue bindings, no inline script/style — CSP-safe;
  badge SVG via the existing `v-html` sink, server-rendered markup only):
  - logoff overlay: `.modal-backdrop.logoff` (higher `z-index` than the
    other modals) — account name, `.countdown` (4 rem tabular digits),
    [Stay logged in] / [Log off now]; no ✕, no backdrop dismiss.
  - intake modal: code + verdict + analysis line; suggestion list buttons;
    name/price(/sku) form; staff-vs-member submit label ("Add to
    catalogue" vs "Send to staff").
  - staged banner on the login view (`v-if="stagedScans.length"`).
  - shared badge-print modal (replaces `memberBadge`).
- **style.css**: `.countdown`, `.stock-warn`, `.staged-banner`,
  `.suggestion-list` rows, `.modal-backdrop.logoff { z-index: 30 }` —
  additive, reusing existing tokens.

**Suggested landing slices** (coordinator's "incrementally"): S1 cart-on-
API (+ legacy import, panel) · S2 global capture + badge flows (logoff
overlay, switch) · S3 barcode flows (intake modal, staging) · S4 badge
print + ui_check extension. Each leaves the SPA loadable (ui_check green).

## 6. Verification (no JS runner — by repo rule, none is added)

- **`scripts/ui_check.py`** stays DOM/console-gated, no scripted login
  (its documented scope). Extend `_check_targets`' app assertions to add,
  via the existing `count()` mechanism only (no parser changes ⇒
  `tests/test_ui_check_parser.py` untouched):
  1. a `data-scan-capture="on"` attribute rendered on the app root (set by
     the listener's installer — a cheap DOM proof the bus is live);
  2. the login-page badge `<input>` still rendered (capture must never
     regress the HID field);
  3. expected login copy still present; zero console errors (existing).
- **Manual hardware checklist** (the real gate for this task; HID mode
  works today): badge login via field and via page-level scan · own badge
  → overlay appears with 30→0 countdown; cancel keeps session; re-scan
  restarts; expiry logs off (and after closing the laptop mid-countdown,
  the next use is logged out — Task-4 property, observed end-to-end) ·
  second badge → switch: outgoing cart saved, incoming cart restored ·
  known barcode adds + increments · UPC-E scan finds the EAN-13 item ·
  misread → re-scan toast, nothing added · unknown GTIN → modal with
  prefix siblings; suggestion click adds; staff create flow; member
  request flow · logged-out barcode → banner → badge login → replay ·
  normal typing in every form is never hijacked (negative test).
- Backend contracts are already pinned by the Task 1–4 pytest suites; the
  SPA consumes them read-only, so no new pytest is owed (full
  `uv run pytest` stays green — no backend change in this task).

## 7. Risks / open questions

- **R1 — Capture false positives** (fast human typing on the page
  background): mitigated by focus rule + 50 ms timing + Enter terminator;
  worst case is a 404 toast. The inverse — a slow USB-HID emulator (>50 ms
  inter-key) — is *not* captured globally; the badge field still works.
  Manual checklist covers both.
- **R2 — Naive-UTC deadline parsing**: forgetting the `"Z"` append makes
  the countdown timezone-wrong; pin with a code comment and the manual
  countdown check. (Alternative: server could send `Z`-suffixed ISO — a
  one-line backend nicety, not required.)
- **R3 — `hcrm-session-expired` during the overlay**: the wrapper clears
  the session and redirects; the overlay's timer must die in that handler
  (listed in §3b hygiene) or it fires `logout` against a dead session —
  harmless but noisy.
- **R4 — Switch while cart ops in flight**: `flushCart` drains the queue
  *before* `qr-login`; the queue also serialises the incoming `loadCart`
  after `saveSession` (ordered through the same chain) — no interleave.
- **R5 — Staged scans are memory-only**: reload loses them. Acceptable
  (the banner is honest); `sessionStorage` is the cheap upgrade if the
  coordinator wants persistence — flagged, not built.
- **R6 — Modal-on-modal**: intake modal during an armed countdown is
  possible (scan → 404 → modal, own badge scanned earlier). The scan
  listener ignores scans while a non-logoff modal is open, and the
  countdown keeps running underneath (server-enforced) — expiry still
  logs off mid-modal, which is the desired safety property.
- **R7 — `apiRaw` variant**: the scan path needs status + structured
  `detail`; adding a second fetch helper is a small duplication of
  `api()`'s auth handling. Alternative: teach `api()` to attach
  `err.status`/`err.detail` to thrown errors — cleaner, touches the shared
  helper (all callers keep working since `Error.message` is unchanged).
  Recommend the latter; flagged for implementer choice.
- **Q1 — SKU prefill for staff intake**: gtin14-as-SKU for catalogable
  scans (proposal) vs a `SCN-` prefix vs free-form. Coordinator call;
  free-form-with-prefill is what the modal does regardless.
- **Q2 — `add_to_cart` on staff resolve** (backend default false): the
  staff queue UI is out of this task's scope (admin view); when it lands,
  default the checkbox to on — the member scanned it wanting it. Noted
  for the follow-up, not built here.
