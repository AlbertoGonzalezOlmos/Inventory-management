# Task 5 proposal (glm) — The SPA flows: cart-on-API, scan bus, badge lifecycle, intake

Coordinator Task 5 (the heart): cart on the API (legacy localStorage one-time
import), global HID scan capture, `handleScan()` bus — badge flows (own badge
→ 30 s logoff countdown with cancel/restart; other badge → save-then-switch)
and barcode flows (add-to-cart; unknown → name+price modal with suggestions;
no-user → attribution) — plus one-shot badge print on register/member-create.

Ground truth verified this pass: `static/app.js` (853 lines, options API,
`api()` wrapper with the 401→relogin redirect and string-detail extraction,
`saveSession`/`logout`, `doBadgeLogin` reading `this.badgeCode`, purchase
panel bound to `purchase` entries `{item, qty}` — the same shape as the
server's `cart.lines`, so the template migration is mostly a rename);
`static/index.html` (login badge field, purchase sidebar at ~L156–183, admin
tabs, existing modal patterns: item detail, pw-reset, member badge);
`scripts/ui_check.py` — **important correction to the task brief: it is NOT
puppeteer** — it is headless Chrome `--dump-dom` + a console-record parser
with a self-proving detector. It loads URLs and asserts rendered markup and
zero console errors; it **cannot type, click, or execute page JS**. That
bounds what the automated gate can assert (see §6).

---

## 1. Cart-on-API

### State & load

- `data().purchase` (localStorage array) is replaced by
  `cart: {lines: [], total_qty: 0, total_cents: 0}` and `cartBusy: false`.
  The `watch: purchase` (deep → localStorage) is **deleted** — the server is
  the store now.
- `loadCart()`: `this.cart = await api("/cart")` — called from
  `saveSession()` (every login path funnels through it: password login,
  badge login, register, **and the switch flow**), and after every mutation.
- `purchaseTotalCents` computed → `this.cart.total_cents` (one line).
- Template: the sidebar binds `cart.lines` instead of `purchase`; each entry
  keeps `entry.item.*` / `entry.qty` (identical shape), gaining
  `entry.line_total_cents` for the per-line display. `v-if="purchase.length
  === 0"` → `cart.lines.length === 0`, etc.

### Mutations (each returns the full `CartOut` — set `this.cart` from the
response, no read-after-write)

| UI action | Call |
|---|---|
| Click item / [Add] in detail modal | `POST /api/cart/items {item_id, qty: 1}` |
| [+] on a line | `POST /api/cart/items {item_id, qty: 1}` (the race-free increment; NOT PATCH — the server upserts) |
| [−] on a line | `PATCH /api/cart/items/{id} {qty: line.qty - 1}` (0 removes — matches the button's `qty <= 1` disable only cosmetically; the server is the authority) |
| [✕] remove | `DELETE /api/cart/items/{id}` |
| [Clear list] | `DELETE /api/cart` |
| Scan adds | `POST /api/cart/scan` (§3) |

The existing stock-aware hints stay client-side: after each mutation, warn
when `line.qty > line.item.stock` ("Only N in stock" toast). `requestPurchase`
keeps its placeholder-toast behaviour, now reading `cart`.

### One-time legacy import (and only one-time)

In `saveSession()`, after the first successful `loadCart()`:

```js
async migrateLegacyPurchase() {
  const raw = localStorage.getItem(PURCHASE_KEY);
  if (!raw) return;
  localStorage.removeItem(PURCHASE_KEY);        // removed regardless…
  if (this.cart.lines.length) return;           // …but imported only into an
  const entries = JSON.parse(raw);              //   empty server cart
  const lines = entries
    .filter(e => e.item && Number.isInteger(e.item.id))
    .map(e => ({item_id: e.item.id, qty: Math.max(1, e.qty | 0)}));
  if (lines.length) {
    this.cart = await api("/cart", {method: "PUT", body: {lines}});
    this.showToast(`Imported ${lines.length} item(s) from your old purchase list`);
  }
}
```

(Importing only into an empty cart means a returning user's durable server
cart is never clobbered by a stale browser copy; entries whose items were
deleted are dropped by the server's 404-whole-request semantics — pre-filter
by nothing, let the PUT validate, and on 404 retry with the intersection…
simplest correct: the PUT validates all ids first, so a 404 names the missing
ones; catch it, drop the named ids, retry once.)

### Save-then-switch's use of PUT/flush

With every mutation its own committed request, "save whatever had been done"
holds by construction — the switch flow has **nothing to flush** beyond
awaiting in-flight requests. The app is sequential per user action (every
button handler awaits its own call before returning), so the switch simply
proceeds; `PUT /api/cart` remains in the app's toolbox exactly where the
backend designed it: the legacy import (above) and the staged-anonymous
replay (§3, which uses `POST /api/cart/scan` per staged code rather than PUT,
since scans need server-side resolution). A defensive `await` of any pending
mutation promise is kept in the switch flow as a one-line guard.

## 2. Global HID scan capture

**Document-level keydown listener (capture phase), always on** — works on
every view, behind modals, logged in or out; a focus-dependent capture field
would break the moment a modal steals focus, which is exactly when scans must
still land.

```js
// constants
const SCAN_INTERKEY_MS = 50;    // scanners type ~10–20 ms/char; humans ≥ ~80
const SCAN_MIN_LEN = 4;

// in mounted():
this._scanBuf = ""; this._scanLast = 0;
document.addEventListener("keydown", this._onScanKey, true);   // capture
// (and removeEventListener in app teardown — the SPA never unmounts, but
//  register it bound once via a stable reference)

_onScanKey(e) {
  const t = e.target;
  const inField = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA"
                        || t.tagName === "SELECT" || t.isContentEditable);
  if (inField) { this._scanBuf = ""; return; }   // never hijack typing; the
                                                 // login badge field keeps
                                                 // its own dedicated flow
  if (e.key === "Enter") {
    const code = this._scanBuf.trim();
    this._scanBuf = "";
    if (code.length >= SCAN_MIN_LEN) this.enqueueScan(code);
    return;
  }
  if (e.key.length === 1) {
    const now = performance.now();
    if (now - this._scanLast > SCAN_INTERKEY_MS) this._scanBuf = "";
    this._scanLast = now;
    this._scanBuf += e.key;
  }
}
```

- **Timing gate**: a burst is a scan only if every inter-key gap stayed under
  50 ms AND it ends with Enter AND it is ≥ 4 chars. A fast human's stray
  burst that survives all three is then stopped by the shape gate.
- **Shape gate** (in `enqueueScan`, before anything else):
  `HCRM1:` prefix → badge; `/^[A-Za-z0-9_-]{4,64}$/` → barcode (GTIN digits
  or SKU-ish opaque — the server re-classifies authoritatively); anything
  else → ignored silently. The server remains the authority (`cart/scan`'s
  `analyze_scan`, `qr-login`/`qr-identify`'s parser), so the client gate only
  avoids nonsense calls.
- **Scan status pill**: a small fixed-position pill flashes
  `scan: <code>` for ~2 s on every capture — silent misroutes must be
  visible at a counter (phase-2 adoption). It is the only always-visible
  affordance; there is no capture field to focus.
- **Queue, not drop**: `enqueueScan(code)` pushes onto `scanQueue` and a
  drain loop processes one scan at a time (`scanBusy` + async drain) — a
  presentation scanner can fire faster than a round trip, and dropping
  scans is the failure mode the bridge was built to avoid.

## 3. `handleScan(code)` bus

```js
async handleScan(code) {
  if (code.startsWith(BADGE_PREFIX_CLIENT)) return this.handleBadgeScan(code);
  return this.handleBarcodeScan(code);
}
```

### Badge scans

**Logged out** → the existing `doBadgeLogin` path, refactored to take the
code as a parameter (the login-page field keeps calling it with
`this.badgeCode`): `POST /api/auth/qr-login` → `saveSession` →
`must_change_password` routing (existing) → **staged replay** (below) →
`reloadItems`/`loadCategories`.

**Logged in** → `POST /api/auth/qr-identify {token: code}`:

- **`same_user: true`** → the **logoff overlay**:
  1. `POST /api/auth/logoff-schedule` → `{logoff_deadline, grace_seconds}`.
  2. Show the overlay: "You scanned your own badge — log off,
     *<name>*?" with a **big per-second countdown computed from the server
     deadline** (`remaining = deadline - Date.now()`, one `setInterval`;
     the server timestamp is the truth — client clock skew and request
     latency never touch it). Buttons: **[Stay logged in]** and
     **[Log off now]**.
  3. `[Stay logged in]` → `POST /api/auth/logoff-cancel` → hide.
  4. `[Log off now]` or countdown reaching 0 → `this.logout()` (which
     calls `/api/auth/logout` for cleanliness — correctness does not depend
     on it: Task 4's lazy expiry kills the token regardless).
  5. **Restart on re-scan**: the document listener is live behind the
     overlay (it is not an input), so a same-badge re-scan flows through
     identify → schedule again → the overlay's deadline is simply updated —
     one overlay instance, never stacked. Interval hygiene: a single
     `this._logoffTimer` id, always `clearInterval`-ed before re-arming and
     on hide/logout/route change.
  6. **Different badge during the countdown** → the different-user branch
     below runs; its first step is a defensive `logoff-cancel` (Task 4's
     belt-and-braces: even without it, the armed deadline dies with the
     revoked old token).

- **`same_user: false`** → **save-then-switch** (coordinator decision 3:
  the new session is secured *before* the old token is dropped):
  1. Defensive `logoff-cancel` if a countdown is pending.
  2. `await` any in-flight cart mutation (the one-line guard).
  3. `const auth = await api("/auth/qr-login", {method: "POST", body:
     {token: code}})` — **new session in hand first**.
  4. `await api("/auth/logout", {method: "POST"})` — revoke the old kiosk
     token (the `api()` wrapper still sends the OLD token at this point,
     which is exactly the per-token revocation Task 4 designed).
  5. `this.saveSession(auth)` → `loadCart` (+ legacy migration — no-op after
     the first time) → `reloadItems`, `loadCategories`.
  6. `must_change_password` on the new user → route to `#/account` with an
     explanatory toast (existing pattern, plus a "switched to <name>"
     toast).

### Barcode scans

**Logged in** → `POST /api/cart/scan {code, symbology: ""}` (HID mode has no
symbology; the server's content-based classification applies):

- **200** → `this.cart = res.cart`; toast "«item.name» added to your
  purchase list"; the stock hint when `res.qty > res.item.stock`.
- **422 `detail.status === "misread"`** → toast the verbatim server message
  ("MISREAD — check digit fails; re-scan, do not file"). Nothing else.
- **404 `detail.status === "unknown"`** → the **intake modal** (below).
- **400 badge** (a raw 43-char token slipped past the client gate) → toast.

**Requires an `api()` change**: the current wrapper collapses non-string
`detail` to "Request failed (404)". Extend it to attach the parsed body to
the thrown error (`err.data = body; err.detail = body.detail`) while keeping
the message behaviour — every existing catch site is unaffected, the scan
flows get the structured 404.

### The intake modal (unknown barcode)

`data: scanIntake = null | {code, message, analysis, catalogable,
suggestions, name: "", price: "", busy: false}`. Rendered as a modal:

- **Context header**: the scanned code, `analysis.format / region / usage`
  (the "barcode system" line), and the verbatim `message` (including the
  UPC-E symbology hint for opaque 6–8-digit codes).
- **Suggestion rows** from `suggestions.by_prefix` / `by_keyword` — clicking
  one is "add that instead": `POST /api/cart/items {item_id}` → close.
- **The name+price form**, submit role-aware (coordinator decision 1):
  - **staff/admin**: `POST /api/items` with `sku` auto-generated
    (`scan-<gtin14>` when catalogable, else `scan-<slug of name>` —
    collision-checked by the server's 409), `barcode: analysis.gtin14` when
    `catalogable`, the typed name and `price_cents` → then
    `POST /api/cart/items` → close → toast. A **409
    "Barcode already assigned"** surfaces "already filed as <sku>" and
    offers to add the conflicting item instead.
  - **member**: `POST /api/scan-requests {code, symbology: "", name,
    price_cents}` → close → toast "unknown item reported — staff have been
    notified". **409 `already`** (detail.item) → offer "add that instead";
    **409 `duplicate`** → toast "you already reported this code".
- **Live suggestions** while typing the name: 300 ms debounce →
  `GET /api/items/suggest?q=<name>` → refresh the suggestion rows
  (`by_keyword`/`by_semantic` groups; the endpoint degrades semantic to []
  — the modal never breaks).
- After a successful staff create+add, the modal also **offers** to resolve
  any open ScanRequest for the same code (a `GET /api/scan-requests` staff
  lookup by code → one-click resolve) — optional convenience; the staff
  queue below is the primary path.

### Staff ScanRequest queue (admin tab)

A fourth admin tab "Scan requests": `GET /api/scan-requests?status=open`
(table: code, requester, proposed name/price, filed-at) with per-row
actions:
- **[Create & resolve]** → `POST /api/items` prefilled from the request
  (`barcode: gtin14`, name, price, generated sku) →
  `POST /api/scan-requests/{id}/resolve {item_id, add_to_cart: true}`
  (closing the member's loop — the item lands in the requester's cart,
  which is what they scanned for) → refresh list. A confirm() spells out
  what will happen.
- **[Dismiss]** → confirm() → `POST .../dismiss` → refresh.
- A status filter (open/resolved/dismissed/all) on the existing
  admin-tab pattern.

### Attribution (barcode while logged out) — stage and replay

Given the endpoints that exist (no `/api/cart/assign`), the simplest correct
approach is the coordinator's primary path, fully client-side:

- `data: stagedScans = []` (in-memory; the kiosk page stays open, and a
  reload simply loses un-attributed scans — acceptable, noted in §7).
- A **banner** (visible whenever `stagedScans.length`): "N scanned item(s)
  waiting — log in or scan your badge to assign them · [Add all] [Clear]".
- **After any successful login** (password, badge, register, switch),
  `replayStagedScans()` runs once: sequential `POST /api/cart/scan` per
  staged code — successes toast and leave the list; misreads toast; unknowns
  stay staged and toast "«code» is not in the catalogue — open it from the
  banner". Each remaining staged row keeps a **[Details]** button that opens
  the (now logged-in) intake modal for that code.
- The **badge-scan-primary** answer falls out: a logged-out badge scan logs
  in (the badge both identifies the account and proves presence), and the
  banner/replay then attributes the staged items to that account.
- **Staff-assisted assignment** (the coordinator's secondary path, without
  `/api/cart/assign`): degrades to "staff is logged in at the kiosk and uses
  the intake modal to create the item; the member's attribution happens via
  their own badge scan". Conscious scope decision, flagged in §7.

## 4. One-shot badge print (register & staff member-create)

- `doRegister()`: read `data.qr_badge` from the response and (instead of
  jumping straight to the catalogue) show the badge print modal first.
- `createMember()` (admin): same, from its response.
- The modal reuses the existing `memberBadge` markup pattern:
  `badgePrint = {svg, name, warning}` → card with `v-html="badgePrint.svg"`
  (the established CSP-safe pattern — markup-only SVG, no script), the
  account name, a **"print now — this code is shown only once"** warning,
  **[Print]** (`window.print()` with a `@media print` rule that isolates the
  badge card) and **[Done]**. The payload is never written to
  localStorage/sessionStorage; closing the modal is final. `has_qr_badge`
  in `UserOut` remains the only later signal.

## 5. index.html / style.css

All new UI follows the existing structural patterns (Vue directives inline
in `index.html`; one `style.css` append; modals reuse the
`modal-overlay`/card classes the item-detail, pw-reset and member-badge
modals already use):

| Addition | Markup | CSS |
|---|---|---|
| Scan pill | fixed-position `<div class="scan-pill" v-if="scanFlash">` | small fixed pill, fade-out animation |
| Staged banner | `<div class="staged-banner" v-if="stagedScans.length">` above the main content of catalogue & login views | amber banner |
| Logoff overlay | full-screen `modal-overlay` variant with the countdown `<div class="countdown">{{ logoffRemaining }}</div>`, [Stay logged in] / [Log off now] | large digits, centered card |
| Intake modal | modal card: context header, suggestion rows, name+price form, role-aware submit button label | reuse modal + list styles |
| Badge print modal | reuse `memberBadge` structure, plus the print-isolation class | `@media print` isolation rule |
| Admin "Scan requests" tab | table + filter + action buttons on the adminTab pattern | reuse admin table styles |
| Purchase sidebar | `purchase` → `cart.lines` renames; per-line total display | none |

## 6. Verification (no JS test runner — and an honest correction)

**Correction to the brief:** `scripts/ui_check.py` is **not puppeteer** — it
is headless Chrome `--dump-dom` plus a self-proving console-record parser. It
loads URLs and asserts rendered markup and zero console errors; it **cannot
type, click, or execute page JS**, and it cannot get past the login screen.
The automated gate therefore asserts what is reachable, and the interactive
flows get a precise manual checklist. This is the repo's established
verification story (server tests own the semantics — Tasks 1–4 already pin
every backend behaviour the SPA composes).

**ui_check.py gate (extended):**
- Existing checks stay green (login/register views render, docs, zero
  console errors) — this alone catches the biggest risk of a ~500-line
  app.js delta: a syntax/mount error blanks the app, and the rendered-
  element assertions fail.
- New assertions, on the reachable (logged-out) views: the login view shows
  the badge field (existing) *and* the staged-banner container's absence
  proves no unconditional render; the register view renders (the badge-print
  modal's `v-if` markup is absent until a registration happens — assert the
  app still mounts with the new code paths present); occurrence-counted
  `<div id="app"` / key elements per route (the existing `count()` helper).
- Console-error classification must stay clean across the new views —
  especially the `v-html` badge SVG path (CSP).

**Manual hardware checklist (executed with the real M-10, documented in the
PR; the USB-COM relay is Phase B — HID only):**
1. Login-page badge scan still logs in (no regression in the dedicated
   field).
2. Logged-in barcode scan of a known GTIN (any EAN form) → toast + sidebar
   line + total; stock warning when qty > stock.
3. Unknown GTIN → intake modal shows region/usage + prefix sibling
   suggestions; staff submit creates + adds; member submit reports.
4. Own-badge scan → overlay counts down from 30 (server deadline);
   re-scan restarts; [Stay logged in] cancels; let one expire → logged out;
   close the browser mid-countdown and wait 30 s → token dead (Task 4's
   property, observed from another tab).
5. Other-user badge scan → "Saved — switched to <name>", old cart intact on
   re-login as the first user, new user's cart loaded; a flagged account
   lands on the change-password screen.
6. Logged-out barcode scan → banner; badge login → replay adds it.
7. Fast human typing in the search box and in modal inputs → never captured
   as a scan; a real scan while a modal is open → still captured.
8. Register → badge modal prints; staff member-create → same; reload → the
   code is never shown again.

**What I deliberately do not build:** a JS unit runner, puppeteer/CDP
integration, or test-only hooks in the SPA (no `window.hcrmTestScan`
backdoors in production code). Flagged as an open question for the
coordinator (§7): a CDP-driven interaction layer for ui_check is the real
future upgrade.

## 7. Risks / open questions

1. **The 50 ms threshold** is a heuristic: too tight drops scans from a slow
   scanner (or a loaded kiosk); too loose catches fast typists. Mitigations:
   ≥4 chars + Enter + shape gate + the server as final authority; the
   constant is one line to retune after hardware testing.
2. **ui_check's reach** (per §6): the interactive flows are manual-gated
   this round. If the coordinator wants automated interaction coverage, a
   CDP layer in ui_check is a follow-up task of its own (websocket-driven
   input dispatch), not a rider on Task 5.
3. **Staged scans are in-memory**: a kiosk reload loses un-attributed scans.
   Acceptable (the physical items are still in hand; re-scan). Persisting
   them to sessionStorage is a two-line change if the owner disagrees.
4. **api() 401 during the switch flow**: `qr-login` and `logout` are already
   exempt from the wrapper's auto-redirect; the old token's `logout` call
   happens while it is still valid by construction. A naturally-expired
   token mid-session behaves exactly as today (re-login prompt).
5. **`window.print()` behaviour** varies by browser/kiosk; the
   `@media print` isolation rule keeps the badge card alone on the page —
   verified in the manual checklist, not automatable here.
6. **SKU auto-generation** for staff intake (`scan-<gtin14>` /
   `scan-<slug>`) can collide; the server's 409 surfaces it and the modal
   offers to add the existing item — no silent overwriting.
7. **One overlay, one intake modal, one drain loop**: all three singletons
   need disciplined show/hide (interval clear, busy flags) — the classic
   SPA-timer leak bugs; each has an explicit teardown path in the design
   above and a manual-checklist item.
8. **The `must_change_password` switch case**: the new account may be
   blocked to the change-password screen mid-kiosk-flow — the switch toast
   explains it (existing behaviour, now reachable via scan).
9. **Scan during `scanBusy`**: queued, not dropped (§2) — but the queue is
   unbounded in theory; a cap of ~10 with a "scanner is running ahead"
   toast is a one-line guard if ever needed.
