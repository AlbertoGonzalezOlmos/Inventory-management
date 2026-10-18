# Task 5 proposal (qwen3.8-max) — SPA: cart-on-API, global HID capture, scan flows

Frontend only: `static/app.js` (853 lines, Vue 3 options API, vendored full
build, in-DOM template, no Node toolchain), `static/index.html`,
`static/style.css`, plus `scripts/ui_check.py` gate extensions. Implements
coordinator Task 5 ("lands incrementally: cart UI first, then capture+flows,
then modals") on the Task 1–4 backend.

**Verified ground truth this pass** (not assumed):
- `api()` throws `Error(detail)` where detail extraction handles *string*
  and *pydantic-list* shapes — **dict details (the scan-surface envelopes)
  fall through to "Request failed (404)"**, so `api()` needs a small
  extension (§2). It also hard-codes the localStorage token and whitelists
  `/auth/login` + `/auth/qr-login` from the global 401→relogin redirect.
- Purchase list: `purchase` data (array of `{item, qty}`) + deep watcher →
  `PURCHASE_KEY` localStorage; `purchaseTotalCents` computed; mutators
  `addToPurchase` (blocks qty ≥ stock client-side today), `inc/dec` (stock-
  clamped), `removeFromPurchase`, `clearPurchase`, `requestPurchase`
  (placeholder toast). `logout()` removes TOKEN/USER keys but **not**
  PURCHASE_KEY (the cross-user leak).
- **`CartLineOut` entries are `{item: ItemOut, qty, …}` — the exact shape the
  template already binds** (`e.item.id`, `e.item.name`, `e.qty`), so a
  computed `purchase()` returning `cart.lines` keeps the panel markup
  untouched.
- Login view: password form + badge form (`badgeCode` v-model, CR submits
  `doBadgeLogin`); modals are `.modal-backdrop`/`.modal` divs driven by data
  props (`detailItem`, `memberBadge`, `pwResetMember`); print is
  `@click="() => window.print()"` with **no `@media print` CSS at all**
  (badge printing currently prints the whole page — fixed here, §6).
- `mounted()`: hashchange + `hcrm-session-expired` listeners; `if (this.user)`
  boot block loads catalogue data.
- `ui_check.py`: one-shot headless browser per URL (`--dump-dom`-style DOM on
  stdout, console records on stderr); checks are `data-v-app` present,
  `v-cloak` gone, ≥2 inputs/≥1 button, an expected copy string, and **zero
  console errors**; targets are `/` (login), `/#/register`, `/docs`. No
  interaction capability; unauthenticated dumps only ever see login/register
  views. `tests/test_ui_check_parser.py` pins the console parser — untouched
  by this task.
- Backend contracts (Tasks 1–4, all merged): full-`CartOut`-returning cart
  mutators (`GET/PUT/DELETE /api/cart`, `POST /api/cart/items`,
  `PATCH|DELETE /api/cart/items/{id}` — PATCH-set with qty 0 removes, **no
  delta endpoint exists**); `POST /api/cart/scan` (200 `{status:"added",
  match, item, qty, cart}` / 422 `{status:"misread"}` / 400 `{status:"badge"}`
  / 404 `{status:"unknown", message, analysis, catalogable,
  suggestions{by_prefix, by_keyword}}` — dict details!);
  `GET /api/items/suggest?barcode=&q=` (+`by_semantic`, degrades, never 503);
  `POST /api/scan-requests` (+`/mine`, staff `GET ?status=`,
  `POST /{id}/resolve {item_id, add_to_cart}`, `/{id}/dismiss`);
  `POST /api/auth/qr-identify {token}` → `{same_user}` (**403 while
  must_change_password** — not exempt); `POST /api/auth/logoff-schedule` →
  `{logoff_deadline, grace_seconds}` and `/logoff-cancel` → 204 (both gated);
  `register`/`create_member` responses carry one-shot `qr_badge {payload,
  svg}`. **`/api/cart/assign` does not exist** (folded into resolve's
  `add_to_cart`); staff-only `POST /api/items` 503s when embeddings are down
  and 409s (string detail) on duplicate SKU/barcode.

---

## 1. Cart-on-API (increment 5a)

**State:** replace `purchase: JSON.parse(localStorage…)` with
`cart: {lines: [], total_qty: 0, total_cents: 0}`; add computed
`purchase() { return this.cart.lines; }` (template bindings unchanged — the
line shape is identical) and rewire `purchaseTotalCents` →
`this.cart.total_cents`. **Delete** the deep `purchase` watcher (the
localStorage persistence it existed for is gone — the server is the store).

**Mutators** — every one is an `await`ed API call whose **response is the
new cart** (Task-1 contract: one payload, no read-after-write), funneled
through a serialization chain so fast clicks/scans cannot interleave
client-side qty arithmetic (PATCH inc/dec computes from `this.cart`, which
is only fresh after the previous response):

```js
_enqueueCart(op) {                      // promise chain, one flight at a time
  this._cartChain = (this._cartChain || Promise.resolve())
    .then(op, op);                      // keep the chain alive on errors
  return this._cartChain;
},
async loadCart()  { this.cart = await api("/cart"); },
addToPurchase(item) { return this._enqueueCart(async () => {
  this.cart = await api("/cart/items", {method:"POST",
    body:{item_id: item.id, qty: 1}});
  this._stockWarn(item);                // decision 6: warn, never block
  this.showToast(item.name + " added to your purchase list");
});},
incPurchase(id) / decPurchase(id)       // PATCH {qty: line.qty ± 1}; dec at 1 → DELETE line
removeFromPurchase(id)                  // DELETE /cart/items/{id}
clearPurchase()                         // DELETE /cart → empty CartOut
```

- **Stock policy flip (decision 6):** `addToPurchase` no longer refuses
  out-of-stock or over-stock adds; after each mutation `_stockWarn` toasts
  "Only N in stock" (warning tone) when `line.qty > line.item.stock`. The
  server clamps nothing; the UI warns.
- `requestPurchase()` stays the placeholder toast, now reading server truth.
- `loadCart()` is called: on boot when `this.user` exists (page reload with a
  live session), after every login/register/badge-login/switch, and skipped
  (silently) when `user.must_change_password` — the cart endpoints 403 under
  the gate and the user is headed to the change-password screen anyway.

**One-time legacy import** (fixes the cross-user leak and rescues existing
browsers), inside the first authenticated `loadCart()`:

```js
const legacy = JSON.parse(localStorage.getItem(PURCHASE_KEY) || "[]");
if (legacy.length && this.cart.lines.length === 0) {
  const lines = legacy.filter(e => e?.item?.id && e.qty >= 1)
                      .map(e => ({item_id: e.item.id, qty: Math.min(e.qty, 10000)}));
  try    { this.cart = await api("/cart", {method:"PUT", body:{lines}}); }
  catch  { /* 404 = a deleted item poisons the atomic PUT: fall back to
              per-entry POSTs, skipping individual 404s (deleted items) */ }
  … per-entry fallback when the PUT 404'd …
}
localStorage.removeItem(PURCHASE_KEY);   // always, once processed (or empty)
```

PUT-first honours the coordinator's "PUT /api/cart" wording and is one
atomic request in the common case; the per-entry `POST /cart/items` fallback
exists because the PUT is all-or-nothing (404 leaves the cart untouched) and
legacy entries may reference deleted items — parsing ids out of the 404
string detail would be brittle, re-POSTing per entry is not. Import maps the
browser-global legacy list onto *whoever logs in first on that browser* —
the only available semantics, documented in a comment. `logout()` also drops
the (now vestigial) key defensively.

**Save-then-switch flush:** with server-side carts, "save whatever had been
done" is *durable by construction*; the SPA's `flushCart()` before a switch
is exactly `await this._cartChain` (let in-flight mutations land) plus the
legacy-import check above. No other upload exists — anonymous *staged scans*
(§3.4) are replayed after the switch's login step, not flushed into the
outgoing account (they were never that user's).

**`api()` extensions (5a, small and additive):**
1. `{token}` option — use an explicit bearer instead of localStorage's.
   Load-bearing for the switch flow: after `saveSession(newAuth)` the stored
   token is the NEW one, but D3 requires revoking the OLD one
   (`POST /auth/logout` with `{token: oldToken}`), best-effort.
2. `{raw: true}` option — resolve to `{status, data}` instead of throwing on
   ≥400, **after** still running the global 401 handler. Required by
   `/cart/scan`, whose 404/422/400 bodies are structured *dict details* the
   flow must branch on (the throwing path flattens them to
   "Request failed (404)"). Everything else keeps using the throwing mode.

## 2. Global HID scan capture (increment 5b)

**Decision: document-level capture-phase keydown accumulator, always on,
plus a visible scan field as the deterministic no-heuristics path.** A
focus-stealing hidden input was rejected: it fights every real form on the
page (the repo's login/register/item forms are input-dense), and re-focus
loops are exactly the "swallow real typing" failure class. The visible field
("Scan items or badge here", in the catalogue header and on the counter
strip) has no length/shape gate at all — explicit operator intent — and
submits on Enter via the same `handleScan()`; it is also the answer for
payloads the global gate deliberately ignores (<6-char internal codes).

**The accumulator** (installed in `mounted()`, `document.addEventListener
("keydown", …, {capture: true})`):

```js
const SCAN_MAX_GAP_MS = 50;          // scanner inter-key; humans sustain ~80ms+
const SCAN_MIN_LEN    = 6;           // opaque floor (EAN-8 and up); badge shape exempt
onScanKey(ev) {
  if (ev.target.closest?.("[data-scan-sink]")) return;   // dedicated fields own their keys
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return this._scanReset();
  const now = performance.now();
  if (ev.key === "Enter") {
    const buf = this._scanBuf; this._scanReset();
    if (buf.length && this._scanShaped(buf)) { ev.preventDefault(); this.handleScan(buf); }
    return;                                            // unshaped burst: let Enter behave normally
  }
  if (ev.key.length !== 1) return this._scanReset();   // modifiers/nav keys abort a burst
  const fast = (now - this._scanLast) < SCAN_MAX_GAP_MS;
  this._scanLast = now;
  if (!fast && this._scanBuf.length === 0) return;     // first slow key: human, ignore
  if (!fast) { this._scanReset(); return; }            // burst broken: human caught up, ignore
  if (this._scanBuf.length >= 1) ev.preventDefault();  // from the 2nd fast key: don't pollute the focused field
  this._scanBuf += ev.key;
}
```

Guards, layered (each answers a concrete false-positive class):
1. **`data-scan-sink` stand-down** — the login-page badge field and the
   visible scan field mark themselves; their own `@keydown.enter` handlers
   call `handleScan`/`doBadgeLogin`. No double-processing, and the
   login-page badge flow keeps its exact shipped behaviour (regression-
   sensitive — pinned in the manual checklist).
2. **Timing gate** — buffering *starts* only on a fast key and *aborts* the
   moment a slow key arrives, so normal typing (even fast typing that
   pauses) never becomes a scan. The first keystroke of a burst is not
   preventDefault-able (speed unknown yet): at most **one character leaks**
   into a focused text field before suppression kicks in — accepted,
   documented, and invisible for search boxes (the shape gate means no scan
   fires; the stray char is ordinary search input).
3. **Shape gate at Enter** (`_scanShaped`): badge = `^HCRM1:[A-Za-z0-9_-]{43}$`
   **or** bare `^[A-Za-z0-9_-]{43}$` (qr-login/qr-identify accept both
   forms; the SPA routes both to the badge flow — a 43-char opaque label is
   not a realistic SKU); barcode = `^[0-9]{6,14}$` (GTIN family incl.
   misreads — the *server* judges validity) or opaque
   `^[A-Za-z0-9._/-]{6,64}$`. Fails → buffer discarded silently, Enter keeps
   its default behaviour (a fast-typed word + Enter must still submit forms).
4. **Modal suspension** (Task-4-arbitrated semantics): while the logoff
   overlay or any modal is open, **barcode** scans are dropped with a toast
   ("finish the current dialog first"); **badge** scans still process
   (same-badge restarts the countdown; different-badge cancels it and
   switches) — a second person at the counter is never ignored.
5. A `_scanIndicator` flash ("⌁ scanned") so silent misroutes are visible
   (kimi's scan-received affordance).

## 3. `handleScan(code)` — the one bus, all flows

```js
async handleScan(code) {
  code = (code || "").trim(); if (!code) return;
  this.lastScan = { code, at: Date.now() };
  if (BADGE_RE.test(code)) return this.handleBadgeScan(code);
  return this.handleBarcodeScan(code);
}
```

### 3.1 Badge, logged out
Refactor `doBadgeLogin` into `loginWithBadge(payload)` (the existing
`badgeCode` form calls it unchanged). On success: `saveSession` →
`must_change_password` branch (existing) → `loadCart()` + legacy import →
**replay staged scans** (§3.4) → catalogue.

### 3.2 Badge, logged in — same user → 30 s logoff overlay
```
POST /api/auth/qr-identify {token: code}
  200 {same_user:true} → armLogoff()
  200 {same_user:false} → §3.3 (switch)
  403 (flagged account — identify is NOT exempt) → FALLBACK: qr-login, compare
      user.id with this.user.id; same → logout(mintedToken) immediately (the
      spare-session revoke), then armLogoff(); different → keep the new
      session and continue as a switch (§3.3 step 2 onward). This is the only
      path that mints-and-revokes; it exists because flagged kiosk sessions
      must still be able to scan.
  401/network → the global session-expired handler already routed to login;
      re-run as §3.1 (badge login from logged-out state)
```
`armLogoff()`:
```js
const s = await api("/auth/logoff-schedule", {method:"POST"});
this.logoff = { name: this.user.name,
                endsAt: Date.now() + s.grace_seconds * 1000,  // count from RECEIPT:
                deadline: s.logoff_deadline };                // skew/latency-proof
if (!this.logoff.timer) this.logoff.timer = setInterval(() => this.tickLogoff(), 250);
```
- Overlay (full-screen, above modals): "You scanned your own badge — log off
  **{name}**?", huge remaining-seconds (recomputed from `endsAt` every tick —
  timestamps, never tick counts), **[Stay logged in]**, **[Log off now]**,
  Esc = stay.
- `tickLogoff()` at ≤0 → `finishLogoff()`: clear timer, `await
  this.logout()` (existing method: server-revoke + local clear + route
  login). If the server already killed the token (deadline passed, browser
  was throttled), `logout`'s api call 401s → the global handler clears the
  session — same end state; **the server deadline is the truth, the overlay
  is presentation** (Task-4 doctrine).
- **[Stay logged in]/Esc** → `POST /auth/logoff-cancel` (best-effort; a 401
  means it already expired — let the global handler land) → close overlay,
  clear timer.
- **Re-scan same badge while open** → `qr-identify` true → `armLogoff()`
  again: single `logoff` object, fresh `endsAt` (restart), never a stacked
  overlay, never a second timer.
- **Different badge while open** → cancel (best-effort) → §3.3.
- Timer hygiene: one interval, cleared on close/`finishLogoff`/route change
  (watcher) and on `hcrm-session-expired` (the handler also nulls `logoff`).

### 3.3 Badge, logged in — different user → save-then-switch
Order is load-bearing (**never log out first and fail second**):
```js
1. await this.flushCart();                              // _cartChain + legacy import
2. let auth;
   try { auth = await api("/auth/qr-login", {method:"POST", body:{token: code}}); }
   catch (e) { this.showToast("Switch failed: " + e.message); return; }   // STAY logged in
3. const oldToken = localStorage.getItem(TOKEN_KEY);
4. if (this.logoff) { await api("/auth/logoff-cancel", {token: oldToken, raw:true}); }  // best-effort
5. this.saveSession(auth);                              // new session secured FIRST
6. await api("/auth/logout", {method:"POST", token: oldToken, raw:true});   // D3: revoke OLD
                                                         // best-effort; failures only console.warn
7. if (auth.user.must_change_password) { route #/account + toast; skip cart load (403-gated) }
   else { await this.loadCart(); this.reloadItems(); this.loadCategories();
          toast: `Switched to ${auth.user.name} — ${oldName}'s list was saved`; }
8. this.replayStagedScans();                            // §3.4 (anonymous leftovers, if any)
```
Step 6 uses the `{token}` override (the stored token is already the new one)
and `{raw:true}` (a 401 there means the old token was already dead — fine).
Logout semantics guarantee the user's *other* devices survive (server-tested).

### 3.4 Barcode, logged out → attribution ("which account?")
No `/api/cart/assign` exists and none is invented: **badge-scan is the
primary answer** (coordinator D5) and staging+replay is the mechanism.
- Stage the code: `stagedScans` (component state) mirrored to
  `STAGING_KEY = "hcrm_staged_scans"` (localStorage: codes+timestamps only —
  no secrets; survives refresh; capped at 50, oldest dropped).
- Show the **attribution banner** on the login/register cards: "N scanned
  item(s) waiting — scan a badge or log in to assign them", with a [Discard]
  that clears the stash. (This is the on-screen form of "ask which account
  the scanned item should be registered to"; the *answer* is the login
  itself. No member picker — enumeration, and `GET /members` is staff-only.)
- **Replay after any login** (§3.1/§3.3 step 8/register): sequentially
  `POST /api/cart/scan` each staged code; `added` → cart toast; `misread` →
  toast + drop; `unknown` → push to `intakeQueue` (§3.5, modals open one at
  a time, each skippable). Clear the stash when the queue empties or the
  user discards. Staff-assisted alternative (member without a badge): staff
  logs in at the kiosk, replay lands in *staff's* cart or intake queue, and
  staff can resolve via the ScanRequest queue (§3.6) — no new endpoint
  needed.

### 3.5 Barcode, logged in → scan-to-cart + intake modal
```js
const {status, data} = await api("/cart/scan", {method:"POST",
    body:{code, symbology:"", qty:1}, raw:true});
200 → this.cart = data.cart; toast «name» added; _stockWarn(data.item, data.qty)
422 (detail.status==="misread") → error toast detail.message ("re-scan"); nothing else
400 (badge) → unreachable via the shape gate; toast + ignore defensively
404 (detail.status==="unknown") → this.intakeQueue.push(detail); open next intake modal
other/pydantic-422 → toast (api raw still surfaces the shape)
```
**Intake modal** (one at a time from `intakeQueue`): header = code +
classification line from `detail.analysis` (`region · usage · format`,
notes, "restricted circulation" warning when `!globally_unique`); suggestion
groups **Same barcode family** (`by_prefix`) and **Similar code/name**
(`by_keyword`) — clicking one `POST /cart/items {item_id}`, closes, toasts,
marks a matching open ScanRequest resolved-by-pick is *not* attempted (keep
it simple); live search as the operator types a name (≥2 chars, 300 ms
debounce → `GET /items/suggest?barcode=<gtin14?>&q=<name>` — the
`by_semantic` group appears here as "Similar meaning"); form: name, price €
(→ cents via the existing `Math.round(x*100)` idiom), and for **staff only**
a SKU field prefilled with `analysis.gtin14 || code` (ItemIn requires a SKU).
Submit, role-aware (decision 1):
- **staff/admin**: if `catalogable` → pre-check `GET /items?barcode=<gtin14>`;
  a hit offers "Already filed as {sku} — add that instead" (avoids the
  string-detail 409 entirely — the Task-3-review resolution). Else
  `POST /items {sku, name, price_cents, category:"general", stock:0,
  barcode: catalogable ? gtin14 : null}` → on 201: `POST /cart/items`
  (staff's own cart when staff scanned it) + **best-effort queue closure**:
  `GET /scan-requests?status=open`, any request with the same `code` →
  `POST /{id}/resolve {item_id}` (default `add_to_cart:false`; the requester
  picks it up on their next scan). Embeddings-down 503 → toast "cannot
  create items right now" (existing server message).
- **member**: `POST /scan-requests {code, symbology:"", name, price_cents}`
  → 201 toast "Reported to staff — scan it again once it's in the
  catalogue"; 409 `already` → offer "add that instead"; 409 `duplicate` →
  toast "already reported". The member's typed name/price are preserved on
  the request either way.

### 3.6 Queues & one-shot badges
- **Admin view: "Requests" tab** (`adminTab` addition, staff-gated like the
  view): open-count chip, list (created, requester, code/kind/gtin14,
  proposed name/price), actions **[Create & resolve]** (opens the intake
  modal pre-filled from the request, staff variant; on success resolves with
  `add_to_cart` as a checkbox → the D5 staff-assign path) and **[Dismiss]**.
- **Account view: "My scan requests"** (`GET /scan-requests/mine`): status
  chips (open/resolved/dismissed) — the "your report was resolved" view.
- **One-shot badge print** (Task-2 payloads): new `badgeOnce` data prop
  `{who, payload, svg}` set from `register`'s and `createMember`'s 201
  responses (`data.qr_badge`), rendered in a modal reusing the `memberBadge`
  markup pattern: `v-html` SVG (the sanctioned sink — server markup is
  pinned inert by `test_badge_svg_is_safe_for_the_v_html_sink`), the payload
  as copyable text, a red **"Shown only once — print or write it down now;
  it cannot be retrieved again"** warning, and Print. The payload lives in
  memory only — never localStorage (kimi R1). Closing nulls it. Existing
  self-serve/member badge modals gain the same print treatment.

## 4. index.html / style.css

- **index.html**: logoff overlay (`v-if="logoff"`, full-screen, huge
  countdown, two buttons); intake modal (`.modal-backdrop` pattern);
  attribution banner (login + register cards, `v-if="stagedScans.length"`);
  visible scan field (catalogue header + counter strip, `data-scan-sink`);
  `data-scan-sink` on the existing badge field; admin "Requests" tab;
  account "My scan requests" list; `badgeOnce` modal; login-view static hint
  line "Scanner ready — barcodes and badges are captured automatically"
  (always-rendered copy → the ui_check pin, §5).
- **style.css**: `.overlay-backdrop`/`.countdown` (z-index above modals,
  tabular-nums for the digits), `.scan-field`, `.suggestion-group`/
  `.suggestion-item` (extend the existing `.similar` styles), `.status-chip`
  (open/resolved/dismissed), `.badge-warn` (the once-only warning), and the
  **first `@media print` block in the repo**: a `printing-badge` body class
  (set while any badge modal is open) hides everything except the modal,
  drops backdrop/shadows, and hides `.no-print` (close/footer buttons) —
  badge cards print as badge cards instead of whole-page screenshots.
- **CSP**: no changes needed — no new inline `<script>`, Vue's runtime
  compiler already needs `'unsafe-eval'`, `:style` bindings are covered by
  `style-src 'unsafe-inline'`, SVG goes through the existing `v-html` sink
  (markup, no script). `tests/test_headers.py` untouched.

## 5. Verification (no JS runner exists; none is added)

**Layer 1 — semantics already server-tested.** Every flow's *truth* lives in
the 492-test pytest suite (cart, scan verdicts, suggestions, requests,
identify, deadline enforcement). The SPA is a renderer of those contracts.

**Layer 2 — `ui_check.py` gate (extended, interaction-free by nature):**
the harness can only load pages and inspect DOM/console, and only the
logged-out surface is reachable; the gate must assert, on `/` and
`/#/register` (existing targets, extended copy pins):
1. Vue mounts, `v-cloak` gone, zero console errors — **with the global
   keydown listener installed** (a syntax/runtime error in the capture code
   breaks mount or logs; this is the real regression value);
2. new static copy pin: "Scanner ready" (capture feature present);
3. existing pins stay: "Log in", badge-field placeholder "Scan your QR badge
   here" (login badge flow untouched), "Create your member account";
4. `/#/register` renders the register card (the badge-once modal is
   post-submit — not reachable one-shot; manual checklist covers it).
Parser (`console_records`) untouched → `tests/test_ui_check_parser.py` green.
Full `uv run pytest` green is still the PR gate (nothing backend changes
except zero — this task is JS/HTML/CSS + ui_check only).

**Layer 3 — manual hardware checklist** (the M-10 in HID mode works today
with no udev rule — SCANNER-AGENT-SYNC's five-second test): real scans on
the catalogue screen (global capture, no focus); typing in search/email/
password at human speed is never hijacked; the one-char burst leak is
cosmetic; login-page badge field unchanged; own-badge overlay: countdown,
Stay/Esc, expiry auto-logout, re-scan restart, **close the tab mid-countdown
→ next API use anywhere 401s (Task-4 property, observable via a second
tab)**; other-badge switch: old cart intact when switching back, old token
revoked (paste it into a curl → 401), flagged-badge fallback path;
unknown barcode: staff create+auto-resolve, member request, queue resolve
with add_to_cart reaching the member's cart; logged-out staging → badge
login → replay; register/member-create badge modal prints one card;
legacy-key import on a doctored browser profile.

## 6. Increment slicing (coordinator: "lands incrementally")

- **5a — cart-on-API**: state/mutators/watcher removal, `api()` extensions
  (`token`, `raw`), `loadCart` + legacy import + stock-warn flip.
  Independently shippable (scan capture absent; ui_check unchanged).
- **5b — capture + badge flows**: accumulator + shape gate + visible field,
  `handleScan` bus, identify/schedule/cancel, logoff overlay, switch flow
  (+flagged fallback), session-expired cleanup hooks.
- **5c — barcode flows + modals**: cart/scan wiring, intake modal
  (staff/member), attribution staging/replay/banner, Requests tab,
  /mine list, badge-once print modal + print CSS, ui_check copy pins.

## 7. Risks & open questions

1. **Capture false positives/negatives** (the riskiest pure-frontend piece):
   five layered guards (§2) + visible-field escape hatch; sustained
   sub-50 ms human typing *with* Enter *and* barcode shape is the only
   false-positive class left — accepted, documented, checklist-tested.
   Sub-6-char internal codes are invisible to the global gate by design
   (visible field covers them).
2. **One-char burst leak** into focused inputs before suppression starts —
   cosmetic; the alternative (delaying every keystroke to classify it) would
   degrade all typing. Accepted.
3. **Switch-flow token choreography** (steps 3–6): the `{token}` override on
   `api()` is load-bearing; a reviewer must check the old-token revoke uses
   it *after* `saveSession`. Failure modes are all best-effort-by-design
   (the server deadline + TTL bound any missed revoke).
4. **Flagged-session fallback mints-and-revokes** (§3.2, 403 branch) — the
   one place the identify design's purity is bent; it exists because Task 4
   (per spec) did not exempt qr-identify. If the coordinator later exempts
   it, the fallback deletes cleanly.
5. **Multiple tabs**: only the focused document receives HID keystrokes
   (natural); two tabs of one session share one server cart (last-writer-
   wins, Task-1 documented) and one armed deadline (Task-4 R5) — both
   already-adjudicated semantics, restated in a comment.
6. **`window.print()` idiom**: reused from the shipped memberBadge modal;
   the manual checklist verifies the print path (with the new `@media print`
   rules) since no automated coverage is possible.
7. **Legacy import mapping** (browser-global list → first logger-in) is the
   only semantics available; one-time, then the key is gone.
8. **Intake queue stacking**: replays can queue several unknowns — modals
   open strictly one at a time, each skippable; skipped codes return to the
   staged list? **Open question**: skip = drop (recommended, with toast) vs
   re-stage; recommend drop (the code is re-scannable; stashing invites
   zombie queues).
9. **`purchase()` computed name** keeps the template diff tiny but the name
   legacy; rename to `cartLines` in a follow-up cosmetic PR, not here.
10. **Open question — `qty` entry on the visible scan field**: a "qty"
    stepper next to the scan field (scan once, enter 5) is supported by
    `CartScanIn.qty` but not built here (YAGNI until the owner asks); the
    intake modal and PATCH cover corrections.
