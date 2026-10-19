# Round 3 plan (qwen3.8-max) — scan interception, QR-while-logged-in, greeting, checkout review, history calendar

> **⚠ Stale-tree notice (added after the audit).** Round 3 was **implemented and
> merged while this plan was being written**: HEAD is now `484c1c3`
> (`c17ad42` grace+calendar endpoint, `945fb71` scan interception,
> `1a2f907` greeting+review+calendar UI, `501e85a` docs). Every `file:line`
> citation below was verified against **`4e831e2`** and is stale for
> `static/app.js` (+517 lines), `static/index.html` (+177),
> `static/style.css` (+93) and `app/routers/orders.py` (+72); the new files
> `static/scancapture.js`, `tests/js/scancapture.test.mjs` and
> `tests/test_frontend_contract.py` did not exist when this was written. Read
> this file as **design rationale**, not as a work order. The audit of what
> actually landed — including five places the implementation is better than all
> three plans, one finding I raised and then refuted by measurement, and the four
> items still open — is **`docs/plans/r3rev-qwen.md` §9**.
>
> **Rev 2 (post cross-review).** Cross-reviewed against `r3plan-glm.md` and
> `r3plan-kimi.md`; the record (per-peer verdicts, adopted designs, measured
> corrections, and my own rev-1 defect ledger) is in **`docs/plans/r3rev-qwen.md`**.
> This file supersedes rev 1. What changed: the defer timer is **re-armed on
> every held key** (glm's catch — rev 1 left the BURST state able to either
> flush mid-payload or hang forever); the WARM exception is split into **E1
> (bounded, WARM-only)** and **E2 (unbounded, inherent, mode-independent)**;
> credential fields get a **no-WARM** rule (`SCAN_NO_WARM_SELECTOR`) so no part
> of a badge payload can ever paint on the login screen; `type=number` is back to
> **never held** (rev 2's append-replay could corrupt human input at a mid-value
> caret); the item-1 motivation is corrected — it is a **credential-exposure**
> fix (`index.html:55-56` autofocuses a `type="text"` field that today paints the
> 49-char bearer payload in plaintext), not a cosmetic one; T3's evidence gate is
> **hardware first** (measured: no Opticon device is attached, but the wedge path
> needs no privileges and the udev rule is already installed); T1's copy sweep
> gains `auth.py:271`; T2 gains the single-writer proof that makes the range
> query safe; T4's greeting uses a **persistent** `aria-live` region; T5 gains
> three verified notes (`badgeLogin`'s silent `busy` drop, the qty-box snap-back,
> the modal-drop case being unchanged).

Proposal only — nothing here is implemented. Grounded in the tree at `4e831e2`
(`main`), read this pass; every claim below carries a `file:line` so a reviewer
can check it without re-reading the repo.

## 0. What the tree actually does today (verified)

**Scan capture** — `static/app.js:929-1010`. One document-level `keydown`
listener in capture phase (`installScanCapture`, called from `mounted`,
`app.js:1889`). Constants at `app.js:12-21`: `SCAN_MAX_GAP_MS = 50`,
`SCAN_MIN_LEN = 4`, badge shapes (`HCRM1:` + 43 chars, or a bare 43-char
token), barcode shapes (`^[0-9]{4,14}$` or an opaque label containing ≥1
digit). The state machine (`_onScanKey`, `app.js:942`):

1. `[data-scan-sink]` targets are skipped entirely (`app.js:944-948`) — the
   login badge field (`index.html:81-90`) and the catalogue scan field
   (`index.html:129-137`) keep their own Enter flows.
2. modifier keys → reset (`app.js:949`).
3. `Enter` → if `_scanBuf.length >= 4` and `_scanShape(buf)` matches: restore
   the snapshot (`restore.el.value = restore.value` + synthetic `input`),
   `preventDefault()`, `enqueueScan` (`app.js:951-975`).
4. printable char → if the buffer is empty **or** the gap was ≥50 ms, reset and
   start a new burst with this char, snapshotting an editable target's
   `{el, value}` first; otherwise append. **Deliberately no `preventDefault`**
   (`app.js:976-1005`, with the long comment explaining why the first char must
   be buffered).

So today every scanned character physically lands in the focused field and is
only rolled back at Enter. That is the round-3 defect: the whole payload is
visible for the burst's duration (~50-500 ms), and the rollback itself is lossy
(§0.1).

**And on the login screen the payload is a credential.** The login email input is
`type="text"` **with `autofocus`** (`index.html:55-56`), so the most-scanned
screen in a kiosk paints the full 49-character `HCRM1:…` **bearer credential in
plaintext** for the burst's duration, and `loginForm.email` holds it in the Vue
model meanwhile (visible to any password-manager/autofill extension watching the
field). That flatly contradicts the doctrine written 600 lines below in the same
file — `enqueueScan` masks badges in the pill because *"NEVER render the raw
badge payload on screen: it is a bearer credential, shoulder-surfable for the
pill's 2 s lifetime"* (`app.js:1027-1031`). **Item 1 is therefore a
credential-exposure fix, not a cosmetic one**, and T3's PR should lead with that.
It is also what settles the strict-vs-WARM question for credential fields
specifically (§D1c).

This was a *deliberate* round-2 decision — `docs/plans/task5-FINAL.md` §2:
"In non-sink editable fields, buffer without `preventDefault` … on a
shape-gated Enter restore the field and route the scan (no character eating)".
Round 3 item 1 supersedes the *mechanism* (buffer-then-restore) but must keep
the *invariant* (no character eating). The plan below is written around that:
**defer-then-replay replaces buffer-then-restore, and "nothing is ever lost"
stays provable.**

### 0.1 Three latent defects in the current rollback (fix them in T3, not later)

| # | Defect | Where | Effect |
|---|--------|-------|--------|
| a | `restore.el.value = restore.value` on a `<select>`, plus an `input` dispatch the directive does not listen to | `app.js:964-967`; `editable` includes `SELECT` at `app.js:990-991` | Two compounding bugs. (i) Assigning a badge string to `select.value` matches no option → the value collapses to `""`. (ii) Native select type-ahead fires **`change`** per keystroke, and Vue's `vModelSelect` binds `change` and reads `el.options`/`el.selected` (decoded from `static/vendor/vue.global.prod.js`) — so the `category` watcher (`app.js:313-315`) fires `reloadItems()` **once per scanned character**, and the restore's `input` dispatch is *not* what the directive listens to, so DOM and model are left disagreeing. Net: scanning while the category `<select>` (`index.html:155`) is focused resets the filter to "All categories" **and** storms the catalogue endpoint. Fix needs both parts: restore `selectedIndex` **and** dispatch `change`. |
| b | The same assignment on `<input type="number">` | `app.js:965`; admin price/stock (`index.html:562,565`), intake price (`index.html:794`) | A non-numeric payload sets `.value` to `""` → **the field's number is wiped**. |
| c | Caret position is not restored | `app.js:964-967` | Setting `.value` puts the caret at the end. A scan into a field whose caret was mid-text moves the caret; the next human keystroke appends in the wrong place. |

Also missing today: **IME** and **auto-repeat** guards. `e.isComposing`
(`keyCode 229`) events are single "keys" with `e.key` often `"Process"` (length
≠ 1 → reset, so mostly harmless today) — but any defer/`preventDefault` design
*must* exclude composition explicitly, and it must do so for a second reason:
Vue's `vModelText` handler begins `if (t.target.composing) return`, so a
synthetic `input` dispatched **during** a composition updates the DOM value but
not the model — a silent desync (§3.3b). And `e.repeat` (holding a key down)
currently produces ≥50 ms-fast gaps, i.e. it looks exactly like a scanner burst.
Both are handled in T3.

One more verified fact that de-risks a global capture listener: `index.html`
contains exactly **one** keyboard handler in the whole template
(`@keydown.enter.prevent="scanFieldSubmit"`, `index.html:136` — itself a sink),
so `preventDefault` on printable keys cannot break any app shortcut.

**Auth / logoff** — `LOGOFF_GRACE_SECONDS = max(1, int(env or "30"))`
(`app/security.py:166-168`); `POST /api/auth/logoff-schedule` reads the *module
attribute* at call time so tests can monkeypatch it, clamps to the token's
expiry, and returns `{logoff_deadline, grace_seconds}`
(`app/routers/auth.py:257-289`, schema `app/schemas.py:152-159`).
`POST /api/auth/logoff-cancel` is idempotent (`auth.py:292-310`).
`POST /api/auth/qr-identify` is a pure, enumeration-safe `{same_user}` predicate
(`auth.py:313-336`). The SPA side: `handleBadgeScan` (`app.js:1062-1082`) →
`armLogoff()` on same-user (`app.js:1141-1160`, counts down from the **server**
deadline, 250 ms tick) or `switchAccount(code)` on different-user
(`app.js:1188-1225`: flush cart → `qr-login` the incoming user → cancel a
pending countdown → save the new session → `_closeOverlays` → revoke the OLD
token with a foreign-token call → route to `#/catalogue`). Item 2's
"save-then-switch" therefore already exists; only the number and the copy
change. Note `qr-login` (`auth.py:143-161`) mints a session for any matching
badge and never rejects "already logged in" — multiple sessions per user are
the design (`change-password`/`change-email` revoke *other* sessions
explicitly). See open question Q2.

**Greeting / routing** — `afterLogin` (`app.js:388-405`) already ends by
routing to the catalogue: `location.hash` and `this.route` are assigned in the
same synchronous turn (`app.js:400-401`), then `reloadItems()` +
`loadCategories()` — except the `must_change_password` branch, which routes to
`#/account` by design (`app.js:390-397`). Because both assignments land before
the queued `hashchange` task runs, that handler's `navigated` test is false and
`_closeOverlays()` does **not** fire on login (`app.js:1863-1878`).
There is **no greeting at all** on that path; the only "Welcome, <name>!" is
`doRegister`'s toast (`app.js:456`) and the register badge modal's subtitle
(`app.js:449`). `switchAccount` greets with `Switched to X — Y's list was saved`
(`app.js:1223`). So item 3 is: add a greeting to *every* session entry point —
and keep it out of `_closeOverlays()`, because `switchAccount` calls that on the
line right after `saveSession` (`app.js:1205-1206`) (§T4).

**Checkout** — `completePurchase` (`app.js:894-926`): `confirm(...)` at
`app.js:899-900`, then `busy = true`, then everything inside the `_cartOp`
serialization chain (`app.js:661-664`): `POST /api/orders` in `raw` mode, 401 →
`handleSessionExpired`, 201 → `cart = r.data.cart`, `lastReceipt = r.data`
(receipt modal, `index.html:842-873`), `lastCheckout = purchase.id`,
`loadMyOrders()`, `expandedOrders = [id]`; 400 → "Cart is empty". The button is
`index.html:227` inside the `.selection-panel` aside (`index.html:202-234`).
`POST /api/orders` (`app/routers/orders.py:76-204`) is a single
`BEGIN IMMEDIATE` transaction: snapshot lines, decrement stock with
clamp-and-report, delete the cart lines, `COMMIT`; returns
`{purchase, cart(empty), skipped[item_ids]}`. **No backend change is needed for
item 4** — the review screen is a pure frontend state addition, and every
review-time mutation already has an endpoint (`POST /api/cart/items`,
`PATCH /api/cart/items/{id}` with absolute qty where `0` removes
(`app/routers/cart.py:277-297`), `DELETE /api/cart/items/{id}`).

**Purchase history** — `GET /api/orders/mine?limit&offset` returns
`list[PurchaseOut]` = `{id, created_at, total_cents, lines[]}`, newest first,
`limit` default 20 / max 100 (`orders.py:206-214`, `app/schemas.py:535-541`).
SPA: `loadMyOrders` (`app.js:869-877`) with a single `ordersLimit` knob
(10 → 100, `loadMoreOrders`, `app.js:879-887`), rendered in the `purchases`
tab (`index.html:329-365`) as a flat list of collapsible receipts.
`created_at` is **naive UTC** (`app/models.py:9-11`, `Purchase.created_at`
`models.py:181`), serialised without a `Z`; `formatDate` (`app.js:350-352`)
parses it as *local* time — so the list already displays the stored UTC
wall-clock date. The calendar must bucket by the same rule (§T6, Q5).
`purchases.user_id` is indexed (`models.py:177`).

**Frontend verification today** — there is **no JS test harness at all**
(`node` 22 is installed on this host; no `package.json`, no `node_modules`,
vendored libs in `static/vendor/`). The only browser gate is
`scripts/ui_check.py`, which runs headless Chrome with
`--virtual-time-budget=6000 --dump-dom` (`ui_check.py:166-207`) against `/`
(copy "Log in") and `/#/register` (`ui_check.py:337-341`) — **one-shot, no
interaction**, so it can pin only what renders logged-out. Its DOM pins for the
scan bus are `data-scan-capture="on"` and the badge field's placeholder
(`ui_check.py:293-301`). CI (`.github/workflows/ci.yml`): `pytest` (forward +
reverse + hygiene) → `ui` (installs Chrome via `npx puppeteer`, runs the parser
fixtures + `ui_check --self-host`) → `scripts` (load + smoke). CSP is
`script-src 'self' 'unsafe-eval'` (`app/main.py:382`), so **adding another
self-hosted `<script src>` is fine** and `new Function` is allowed in the
browser too (not that we need it).

---

## 1. Decisions taken up front (each is a coordinator override point)

**D1 — Item 1 mechanism: defer the burst's first key, replay it if the burst
turns out to be human. Plus a bounded "human run" fast-path.**
The only way to guarantee "the text never appears" is to *not let the first
character land* — and at the first character you cannot yet know whether it is
a scan. So the first printable key after a ≥50 ms gap is `preventDefault`ed and
held for `SCAN_DEFER_MS = 60` ms (> the 50 ms gate + event-loop jitter). If a
second key arrives inside 50 ms → burst confirmed → everything stays held and
is routed at Enter (never painted). If the timer fires → the held characters
are spliced into the field at the recorded caret and a synthetic `input` is
dispatched (Vue's `v-model` picks it up) → **zero loss**.
Cost: the first character of a keystroke run in an editable field appears up to
60 ms late. To keep sustained typing native, a successful timeout flush marks
that element WARM for `HUMAN_RUN_MS = 400`: while warm, printable keys are
*not* prevented (zero latency, native IME/repeat/undo). A scan that starts
inside a warm window is still routed correctly — its first character is
retracted at burst-confirm time (value+caret snapshot taken on the warm key
that could have started a burst), so the visible artifact is **at most one
character for at most one frame**, and only when a scan begins <400 ms after a
human keystroke *in the same focused field*.
Alternative (strict mode): drop WARM → the absolute "never a pixel" guarantee,
at the cost of a 60 ms hold on *every* character and chunked rendering for
typists faster than 20 cps. Exposed as `SCAN_STRICT_DEFER = false` (one
constant) so the coordinator can flip it without a redesign.

**The exception taxonomy, stated precisely (rev 2 — rev 1 conflated these).**
There are two ways a scan can escape interception, and only one of them is
WARM's fault:

* **E1 — WARM-only, bounded.** The scan's first key arrives **≥50 ms** after a
  human keystroke but inside the 400 ms warm window. A snapshot exists (taken on
  that gap≥50 ms key), so burst-confirm retracts it: **one character painted for
  ≤1 frame**. `SCAN_STRICT_DEFER = true` closes E1 completely.
* **E2 — inherent, unbounded, identical in both modes, unchanged from today.**
  The scan's first key arrives **<50 ms** after a human keystroke in the same
  field. There is no burst boundary to detect, so the payload accumulates into
  `_buf` *mixed with the human's characters*; every char lands natively and the
  combined string fails the shape gate at Enter → the payload stays in the field
  and is not routed. **Strict mode does not help** (the mixed buffer fails the
  gate there too). E2 is the price of timing-based detection and is only removed
  by a sentinel prefix (§3.6) or by using the visible scan field.

So flipping STRICT buys E1 and nothing else — if the coordinator's reading of
"never appears" is absolute, the honest answer is "then we need the sentinel
prefix", not "then set STRICT".

**Recommendation: ship WARM + D1c, document E1/E2.**

**D1c — `SCAN_NO_WARM_SELECTOR`: credential fields never go WARM.**
`input[type=password], input[autocomplete="username"], input[autocomplete*="password"]`
are always strictly deferred (hold → replay on proof-of-human). Cost: a uniform
≤60 ms lag on the first character of a username/password field — invisible in a
password field, where the glyphs are dots. Benefit: **E1 is eliminated for the
only fields where a painted character is a credential fragment**, so the design
can claim "no part of a badge payload ever paints on the login screen" without a
caveat — which is exactly the exposure §0 documents (`index.html:55-56`,
contradicting `app.js:1027-1031`). ~5 lines (one `matches()` test in the
classifier) + 2 tests. This makes `SCAN_STRICT_DEFER` nearly moot: with D1c the
remaining WARM surface is the catalogue search box and staff forms, where a
1-char/1-frame artifact is a non-event.

**D2 — Extract the capture state machine into `static/scan-capture.js` and test
it with `node --test` (zero dependencies).**
The state machine is the risky part, and it is untestable while it lives inside
a 1900-line Vue options object. Extract it as a factory with injected deps
(`now`, `setTimeout`, `clearTimeout`, `onScan`, `sinkSelector`) so Node can
drive it with a ~80-line DOM stub (`tests/js/harness/dom.js`). The file is
dual-mode: `module.exports = {...}` when `typeof module !== "undefined"`,
else `window.HcrmScanCapture = {...}` — one file, loaded as a classic
`<script src="/scan-capture.js">` **before** `/app.js` (`index.html:886`), no
build step, no ESM/module-script migration (that would change script ordering
and strict-mode semantics for the whole SPA — out of scope). Same convention
for the calendar's pure math (`static/purchase-calendar.js`).
CI: a new `js` job (`actions/setup-node@v4`, `node --test tests/js/`). The
`pytest` job does **not** gain a node dependency; instead
`tests/test_frontend_contract.py` pins the *wiring* statically (§T0).

**D3 — Item 4's review screen is a route (`#/checkout`), not a modal, and it
reuses the catalogue section's markup.**
"Items can still be modified (qty/remove/**add**)" means the catalogue must
stay reachable, which a modal blocks. A hash route also makes "done → purchase
history" a plain navigation. To avoid duplicating the item grid, sentinel and
toolbar, `currentView === "checkout"` renders the *same* `<section>` as
`catalogue` (`v-else-if="currentView === 'catalogue' || currentView === 'checkout'"`,
`index.html:124`) and only the `<aside>` swaps between `.selection-panel` and
`.review-panel`. One grid ⇒ one `#sentinel` ⇒ the IntersectionObserver wiring
(`app.js:321-326`, `app.js:1906-1910`) keeps working unchanged. Scanning while
on the review screen already adds to the server cart for free
(`handleBarcodeScan` only blocks on `_modalOpen()`, `app.js:1089-1092`, and a
route is not a modal).

**D4 — Item 5 gets a small backend aggregate (`GET /api/orders/calendar`), and
the colour ramp stays client-side.**
Client-side bucketing from `/orders/mine` is *silently wrong* whenever the
month has more orders than the loaded page (the SPA caps at `limit=100`,
`app.js:879-887`), and it cannot render any month but the one you happen to
have loaded. One indexed range query (`user_id` + `created_at` window) returns
per-day totals for the requested month, complete and cheap. The **gradient is
presentation logic** → derived in the browser from the returned day values
(`min`/`max` over days), and unit-tested in Node.
Alternative considered and rejected: `?month=` on `/orders/mine` (muddies a
pinned contract) and pure client-side (above).

**D5 — After "done": navigate to purchase history, pre-expand the new receipt,
toast the total; keep the receipt *modal* only when the checkout reported
something the list cannot say (`skipped` orphans or any `clamped` line).**
Item 4 reads "done completes the purchase and goes to purchase history", i.e.
no blocking dialog. But `CheckoutOut.skipped` and `PurchaseLineOut.clamped`
exist precisely because "clamp-not-veto … reported, never silently dropped"
(`orders.py:18-21`, `schemas.py:519-532`) — dropping that report on the floor
would violate an existing doctrine. Compromise: the modal becomes
exception-only. **Flagged as Q4** because it is a visible behaviour change the
coordinator may want to overrule in either direction.

**D6 — The greeting is a timed banner, not a toast.**
`afterLogin` fires `loadCart` → `importLegacyPurchase` (toasts, `app.js:695,706`)
→ `replayStagedScans` (toasts per staged scan, `app.js:1259,1261,1266`) *before*
routing. A greeting on the toast channel (`showToast`, `app.js:353-357`, 3 s,
single slot) is overwritten within milliseconds on exactly the paths where it
matters most (a returning kiosk user with a saved cart). A dedicated
`.greeting` banner (own state, own 4 s timer, rendered above the view) cannot
be clobbered. Markup uses a **persistent** `aria-live="polite"` region with the
text conditional inside it — a live region that is *inserted* with its text
already present is announced inconsistently across screen readers (rev-1 defect
L5).

**D7 — `badgeLogin`'s silent `busy` drop becomes a toast.** `app.js:417-419`
returns without a word when a request is in flight, so a badge scanned during a
checkout is dropped silently (the pill fires, then nothing happens). T5
lengthens that window. One line: `this.showToast("One moment — finishing the
previous action")` before the `return`. The guard itself stays — it prevents
minting a throwaway session, as its comment says.

**D8 — The review screen's qty box visibly snaps to server truth.** `:value` +
`@change` (not `v-model`) means a concurrent scan or a rejected 422 re-renders
the input from `this.cart`. Correct (server truth wins) but stated here so
nobody "fixes" it into a `v-model` that fights the `_cartOp` chain.

**D9 — A scan routed while a modal is open is *unchanged*, not a regression.**
Checked because the new design looks like it loses the payload where the old one
recovered it: today's restore assigns the **pre-burst** value
(`app.js:964-967`), so it also removes the payload before `enqueueScan`.
"Finish the current dialog first" (`app.js:1089-1092`) drops the code in both
designs; the pill still shows a barcode for 2 s and the visible scan field
remains the deterministic re-entry path. No action — recorded so a reviewer does
not redo the check.

---

## 2. Ordered task breakdown

Serialisation rule: **T0-T7 all touch `static/app.js` / `index.html` /
`style.css` except T1/T2, so the frontend tasks are strictly serial** (one
writer per file). T1 and T2 are backend-only and can run in parallel with each
other and with T0.

| # | Task | Layer | Files | Size | Depends |
|---|------|-------|-------|------|---------|
| T0 | JS test harness + extraction convention (no behaviour change) | infra | `tests/js/harness/dom.js`, `tests/js/smoke.test.js`, `tests/test_frontend_contract.py`, `.github/workflows/ci.yml`, `static/scan-capture.js` (shape gates only), `static/app.js` (delegate `_scanShape`), `static/index.html` (script tag), `NOTES.md` | S | — |
| T1 | Logoff grace 30 s → 60 s + every copy/doc/test that says 30 | backend | `app/security.py:166-168`, `app/routers/auth.py:266`, `app/schemas.py:152-159`, `static/index.html:139`, `README.md:135`, `NOTES.md:21,279`, `tests/test_logoff.py:86-89` | S | — |
| T2 | `GET /api/orders/calendar` (per-day month aggregate) | backend | `app/routers/orders.py`, `app/schemas.py`, `tests/test_orders_calendar.py` (new) | M | — |
| T3 | **Item 1** — defer/replay/retract capture state machine | frontend | `static/scan-capture.js` (new body), `static/app.js:929-1010`, `tests/js/scan-capture.test.js` (new), `tests/test_frontend_contract.py` | **L** | T0 |
| T4 | **Item 3** — "Hi &lt;name&gt;" banner on every session entry + catalogue routing | frontend | `static/app.js` (`saveSession:464`, `afterLogin:388`, `doRegister:456`, `switchAccount:1223`, `logout:505`, `handleSessionExpired:1124`), `static/index.html`, `static/style.css` | S | T3 |
| T5 | **Item 4** — `#/checkout` review screen, no `confirm()` | frontend | `static/app.js` (`completePurchase:894`, `currentView:225`, watchers `app.js:309-341`, new `setLineQty`/`goToCheckout`/`donePurchase`), `static/index.html:124,202-234`, `static/style.css` | M | T4 |
| T6 | **Item 5** — history calendar + month total | frontend | `static/purchase-calendar.js` (new), `static/app.js` (`selectProfileTab:763`, `loadMyOrders:869`, new `loadCalendar`/`shiftCalendarMonth`), `static/index.html:329-365`, `static/style.css`, `tests/js/purchase-calendar.test.js` (new) | M | T0, T2, T5 |
| T7 | Docs + verification pass (README/NOTES/ui_check pins, hardware or simulated evidence) | docs | `README.md`, `NOTES.md`, `scripts/ui_check.py`, `docs/plans/round3-task.md` status | S | all |

### T0 — harness + convention (details)

* `static/scan-capture.js` starts as a **pure move** of the shape gates and
  constants out of `app.js:12-21,1006-1010` (`BADGE_FULL_RE`, `BADGE_RAW_RE`,
  `BARCODE_DIGITS_RE`, `BARCODE_OPAQUE_RE`, `SCAN_MIN_LEN`, `scanShape(code)`),
  exported dual-mode; `app.js` keeps only a thin delegating wrapper
  (`_scanShape(code) { return HcrmScanCapture.scanShape(code); }`) and its own
  `SCAN_MAX_GAP_MS` copy moves into the module with the rest of the timing
  constants. Behaviour identical → the PR is reviewable as a pure move, and it
  proves the loop (browser loads it, Node requires it, CI runs it).
* `tests/js/harness/dom.js`: `makeElement({tagName, type, value})` with
  `selectionStart/End`, `dispatchEvent` recording, `closest()`; `makeEvent({key,
  target, repeat, isComposing, ctrlKey…})` with a `preventDefault` spy;
  a manual clock (`now()`, `advance(ms)`) driving the injected
  `setTimeout/clearTimeout` so tests are deterministic and instant.
* `tests/js/smoke.test.js`: asserts the harness itself (a held char replays;
  a sink is never prevented) — i.e. the harness fails loudly if it drifts.
* `tests/test_frontend_contract.py` (no node needed; grep/regex over the static
  files, each pin with a comment naming the regression it catches):
  1. `<script src="/scan-capture.js">` occurs **before** `<script src="/app.js">`
     (a reversed order = `HcrmScanCapture is not defined` = the whole scan bus
     dead, and `ui_check` would only catch it as a console error).
  2. `data-scan-sink` count in `index.html` == 2 (the collapsed badge fallback +
     the catalogue scan field must both keep their dedicated Enter flows).
  3. `data-scan-capture` binding still present in `index.html` (the ui_check pin
     depends on it, `ui_check.py:293`).
  4. The constants the Node tests assert on are the *same* ones the SPA loads
     (no copy in the test file — `require("../../static/scan-capture.js")`).
* CI: new `js` job — `actions/checkout`, `actions/setup-node@v4` (node 22),
  `node --test tests/js/`. Added to the `needs:` of nothing (it is independent),
  and listed in the workflow's header comment. Gate-that-cannot-run doctrine:
  the job has no skip path.

### T1 — grace 30 → 60 (details)

* `app/security.py:166-168`: default `"30"` → `"60"` (keep `max(1, …)` clamp and
  the env override; keep the comment's "monkeypatch the module attribute" note).
* Copy sweep — **7 sites, all verified stale by grep**: `app/routers/auth.py:266`
  (`"log off in 30 seconds?"`), **`app/routers/auth.py:271`** (`"never from a
  client-side now+30"` — missed in rev 1, caught by glm),
  `app/schemas.py:154-155` (same phrase), `static/index.html:139`
  (`(30&nbsp;s countdown)`), `README.md:135`, `NOTES.md:21`, `NOTES.md:279`.
  **Do not touch `app/routers/items.py:466`** — its "30 s pool timeout" is the
  connection pool, unrelated; a blind `s/30/60/` sweep would corrupt it.
* `static/index.html:139` scan hint: `logs you off (30&nbsp;s countdown)` →
  `60&nbsp;s`. (Better: bind it — `{{ logoffGraceHint }}` is over-engineering;
  the number is a *copy* claim about the server default, so pin it in
  `tests/test_frontend_contract.py` against `app/security.py`'s parsed default
  — one source of truth, and the pin fails if either side drifts.)
* `tests/test_logoff.py:86` `== 30` → `== 60`; `:88-89` `now+30` delta → 60.
  Every monkeypatched test (`_grace(...)`, `:46-50`) is unaffected — that is
  exactly why the router reads the module attribute.
* New test: `test_default_grace_is_60` asserting the *unmonkeypatched* default
  and that the armed deadline is ~now+60 (±5 s), so a future "30" cannot slip
  back in through a different file.
* Nothing else changes: the SPA counts down from the server deadline
  (`app.js:1141-1160`), the overlay renders `logoff.remaining`
  (`index.html:748-760`), and expiry → `finishLogoff` → `logout`
  (`app.js:1167-1178`). Item 2's different-user half needs **no code** —
  `switchAccount` already flushes, switches, cancels a pending countdown and
  revokes the old token (`app.js:1188-1225`).
* Also check `TOKEN_TTL` ≫ 60 s so the clamp at `auth.py:283` is a no-op in
  practice (it is; the clamp test at `tests/test_logoff.py:111-118` stays).

### T2 — `GET /api/orders/calendar` (details)

```
GET /api/orders/calendar?year=2026&month=10        (auth: get_current_user)
200 → {
  "year": 2026, "month": 10,
  "days": { "2026-10-05": {"cents": 1250, "count": 2},
            "2026-10-06": {"cents":  480, "count": 1} },
  "total_cents": 1730,
  "purchase_count": 3
}
```

* `app/schemas.py`: `MonthDayOut {cents: int, count: int}`,
  `OrderCalendarOut {year: int, month: int, days: dict[str, MonthDayOut],
  total_cents: int, purchase_count: int}` (`extra="forbid"` house style,
  docstrings stating the UTC-day rule).
* `app/routers/orders.py`: range query, **no raw SQL** —
  `select(Purchase).where(Purchase.user_id == user.id, Purchase.created_at >=
  start, Purchase.created_at < end)` with `start = datetime(year, month, 1)` and
  `end = start + relativedelta(months=1)` computed without `dateutil`
  (`datetime(year + (month == 12), month % 12 + 1, 1)`), then bucket in Python
  by `p.created_at.date().isoformat()`. Uses the `user_id` index
  (`models.py:177`); a month of purchases is tens of rows, so Python-side
  aggregation is cheaper to reason about than `GROUP BY substr(created_at,…)`
  and stays backend-portable. **Day boundary = UTC**, matching how
  `created_at` is written (`orders.py:114-116`, one `utcnow()` read).
* **Why the bound-`datetime` range compare is safe (the proof a reviewer will
  ask for):** `purchases.created_at` has exactly **one** writer in the whole repo
  — the raw `INSERT` at `orders.py:159-161`, formatted with
  `strftime("%Y-%m-%d %H:%M:%S.%f")`, which is byte-identical to SQLAlchemy's
  SQLite `DATETIME` bind format. Verified: no `Purchase(...)` construction in
  `app/`, `scripts/` or `tests/` (grep), and `app/database.py` has no migration
  touching `purchases`. So every stored value is lexicographically ordered ==
  chronologically ordered, and no `substr()` hack is needed.
* Validation: `year: int = Query(ge=2000, le=2100)`, `month: int = Query(ge=1,
  le=12)` → 422 outside; default both to the current UTC month so the SPA can
  call it bare.
* Authorisation: `get_current_user` only — the same gate as `/mine`, so the
  `must_change_password` 403 and the staff-sees-only-their-own rule come free.
* Tests — `tests/test_orders_calendar.py` (new, `client`/`admin_token`/
  `create_user` fixtures from `tests/conftest.py`):
  1. two purchases on the same UTC day → one entry, summed cents, `count == 2`;
  2. a purchase on the last/first instant of the month → included; one second
     outside → excluded (the range is half-open);
  3. empty month → `days == {}`, `total_cents == 0`, `purchase_count == 0`
     (200, not 404 — the calendar renders an empty month);
  4. isolation: another user's purchases never appear (mirror
     `test_mine_isolation_ordering_and_pagination`, `test_purchases.py:272`);
  5. staff privilege matrix parity with `/mine` (`test_purchases.py:294`):
     an admin gets *their own* calendar, never a member's;
  6. `total_cents` is Σ `applied_qty × price` (i.e. equals Σ of the receipts'
     `total_cents`), not Σ of requested quantities — the clamp case from
     `test_purchases.py:87` must show the *paid* amount;
  7. gates: 401 anonymous, 403 while `must_change_password`, 422 for
     `month=13` / `year=1999`;
  8. `days` keys are exactly `YYYY-MM-DD` (a regex over the response) — the SPA
     indexes the map by a locally built key, so a format drift is a silent
     empty calendar.
* `scripts/smoke_test.py`: add one check next to the existing
  `GET /api/orders/mine` check (`smoke_test.py:371-374`).

### T3 — item 1: the capture state machine (the core of this round)

See §3 for the full design, invariants and test matrix. Deliverables:

* `static/scan-capture.js` — `createScanCapture(deps)` returning
  `{install, dispose, keydown, flush, snapshot}`; all timing via injected
  `now/setTimeout/clearTimeout`; all output via `onScan(code, kind)`; no Vue,
  no `document` access except through `deps.document` (so Node can stub it).
* `static/app.js:929-1010` — `installScanCapture()` becomes:
  `this._capture = HcrmScanCapture.createScanCapture({…, onScan: (c,k) =>
  this.enqueueScan(c,k), onState: s => (this.scanCaptureState = s)})` +
  `this._capture.install()`; `_scanReset`/`_onScanKey`/`_scanShape` are deleted
  (their behaviour now lives in the module) and `enqueueScan` (`app.js:1026`),
  the pill masking, the queue and `drainScanQueue` are **untouched** — the
  routing side of the bus does not change at all.
* `scanFieldSubmit` (`app.js:1012-1024`) unchanged; the two sinks unchanged.
* `tests/js/scan-capture.test.js` — the matrix in §3.4 (~30 cases).
* `tests/test_frontend_contract.py` gains: the two sinks still carry
  `data-scan-sink` (count == 2, so neither dedicated Enter flow can be dropped
  by a template edit), and the `SCAN_DEFER_MS > SCAN_MAX_GAP_MS` relation —
  imported from the module, not re-typed: **if the defer window ever drops below
  the burst gate, a scan's second key arrives after the flush and the payload
  lands in the field.** That inequality is the design's load-bearing wall, so it
  is pinned twice (Node + pytest). The sink *behaviour* ("never prevented") is
  pinned by name in the Node suite, not by a brittle regex over the source.
* Verification, **hardware first** (rev 2 — measured, see R2): re-plug the M-10
  and run the browser pass (a badge on the login screen, a GTIN into the
  catalogue with the search field focused, one into the review screen's qty box);
  the synthetic-event simulator is the **fallback**, not the only path. Either
  way the handler must never consult `isTrusted` (console-dispatched
  `KeyboardEvent`s are `isTrusted: false`, and a hardware pass does not conflict
  with that) — pinned by a contract test. The PR must state which path was used
  and must not claim the hardware is dead when it is merely unplugged.
* Node suite green; `ui_check --self-host` green (the `data-scan-capture="on"`
  pin proves the listener still installs and that no console error appeared).

### T4 — item 3: greeting + routing (details)

* State: `greeting: ""` + `_greetingTimer`. Set in **`saveSession`**
  (`app.js:464-468`) — the single funnel for password login, badge login and
  register (all via `afterLogin`, `app.js:389`) *and* for the badge switch
  (`app.js:1205`): `this.greeting = "Hi " + user.name;` + a 4 s auto-clear.
* **Do not clear it in `_closeOverlays()`** (`app.js:470-503`). `switchAccount`
  calls `saveSession(auth)` at `app.js:1205` and `_closeOverlays()` on the very
  next line (`app.js:1206`), so a greeting cleared there would be deleted on the
  one path that most needs it (a second person taking over the kiosk). Clear it
  explicitly in `logout` (`app.js:505-515`), `handleSessionExpired`
  (`app.js:1124-1137`) and by overwrite on the next `saveSession`. This is the
  subtlest line in T4 — it gets a comment saying why it is *not* in
  `_closeOverlays`.
* The `hashchange` handler is **not** a hazard here: `afterLogin` assigns
  `location.hash` and `this.route` in the same synchronous tick
  (`app.js:400-401`), so by the time the queued `hashchange` task runs,
  `navigated` is false and `_closeOverlays()` does not fire — the ordering the
  comment at `app.js:1865-1874` documents. Any *new* navigation added by T4/T5
  must keep that pattern (assign both in one tick) or it will strand/kill
  overlays.
* Markup: a **persistent** live region with the text conditional inside it —
  `<div class="greeting" aria-live="polite"><span v-if="greeting">{{ greeting
  }}</span></div>` rendered inside `<main>` above the view sections
  (`index.html:45`), `.greeting` collapsing visually when empty. A region that is
  *inserted* with its text already present (rev 1's `v-if` + `role="status"`) is
  announced inconsistently across screen readers; the persistent region is the
  reliable pattern.
* **D11 (documented, not fixed):** the banner lives above the view sections, so
  navigating away 1 s after login carries it into the next view for the rest of
  its 4 s. Harmless (non-blocking, no backdrop, `aria-live`) — stated here so a
  reviewer does not have to find it. Cheapest fix if anyone objects: clear it in
  the `hashchange` handler's `navigated` branch (`app.js:1863-1878`).
* Remove `doRegister`'s `showToast("Welcome, " + data.user.name + "!")`
  (`app.js:456`) — the banner now greets; keep the badge modal's subtitle
  (`app.js:449`) as-is so the print prompt still reads naturally.
* `switchAccount`'s toast (`app.js:1223`) becomes
  `"${oldName}'s list was saved"` (the banner carries the "Hi X"), keeping the
  save-then-switch evidence on screen.
* Routing: `afterLogin` **already** routes to the catalogue (`app.js:400-404`:
  hash + route + `reloadItems()` + `loadCategories()`), and so does
  `switchAccount` (`app.js:1221-1222`). Item 3's "then navigate to the
  catalogue" is therefore satisfied for every entry point except the
  `must_change_password` branch — verified, no routing change needed. What T4
  adds is the greeting, and the guarantee that nothing later in `afterLogin`
  (cart load, legacy import, staged-scan replay) can navigate away from the
  catalogue: it cannot, but the PR should state that it was checked.
* `must_change_password` keeps routing to `#/account` (`app.js:390-397`) — the
  greeting still shows (it is set in `saveSession`), which is correct: the
  user was greeted, then steered.
* Also in this task (D7): `badgeLogin`'s `if (this.busy) return;`
  (`app.js:417-419`) gains a toast, because T5 lengthens the `busy` window and a
  badge scanned mid-checkout is currently dropped in silence.
* Tests: contract pin (the persistent `aria-live="polite"` greeting region exists
  and its inner span is bound to `greeting`); a Node test only if we extract the
  greeting timer (we should not — it is 4 lines). Manual/browser evidence in the
  PR: password login, badge login, register, switch → each shows "Hi &lt;name&gt;"
  and lands on the catalogue with items loaded.

### T5 — item 4: the review screen (details)

See §4 for state design. Summary of the changes:

* `currentView` (`app.js:225-231`) gains `if (this.route.startsWith("#/checkout"))
  return "checkout";` **before** the catalogue fallback, and only when
  `this.user` (the `!this.user` branch at `app.js:226` already returns
  login/register, so a logged-out `#/checkout` deep link shows the login form —
  and `afterLogin` then routes to the catalogue, not back to checkout; that is
  the desired kiosk behaviour, noted in Q6).
* The `currentView` watcher (`app.js:320-336`) treats `checkout` like
  `catalogue` for data loading (items + `$nextTick(observeSentinel)`), plus:
  on entry, if `cart.lines.length === 0` → toast "Your purchase list is empty"
  and route back to `#/catalogue` (assign `this.route` in the same synchronous
  turn as `location.hash`, the `app.js:400-401` pattern — see §4.2).
* `completePurchase` (`app.js:894`) is **replaced**: the sidebar button becomes
  `@click="goToCheckout"` (`index.html:227`), which sets `location.hash` and
  `this.route` to `"#/checkout"` in the same turn. **No `confirm()`** — the only
  remaining `confirm(` calls are the destructive/irreversible ones
  (`app.js:575, 1682, 1752, 1793, 1817`), which is the doctrine the review
  screen now satisfies differently: the confirmation *is* the screen.
* `donePurchase()` = today's `completePurchase` body minus the `confirm`, i.e.
  still `busy`-guarded and still inside `_cartOp(...)` (the serialization that
  prevents an add/scan racing the checkout — `app.js:903-909`, follow-up
  finding 3 from round 2), then on 201: `cart = data.cart`,
  `lastCheckout = data.purchase.id`, `profileTab = "purchases"`,
  `this.route = "#/account"` **and** `location.hash = "#/account"` in the same
  synchronous turn (so the queued `hashchange` sees `navigated === false` and
  the receipt modal survives it), `await this.loadMyOrders()`, `expandedOrders = [id]`,
  `showToast("Purchase complete — " + formatPrice(total))`, and
  `lastReceipt = data` **only if** `data.skipped.length ||
  data.purchase.lines.some(l => l.clamped)` (D5/Q4).
* Review panel markup (`index.html`, inside the shared catalogue section):
  per line — name, unit price, `−`/`+` steppers (existing `decPurchase`/
  `incPurchase`), a **direct qty `<input type="number" min="1" max="10000">`**
  bound with `:value="entry.qty"` + `@change="setLineQty(entry.item.id,
  $event.target.value)"`, a `✕` remove, the line total and the existing
  over-stock warning (`index.html:211-216`, `_stockWarn` `app.js:713-717`);
  footer — grand total, "Keep shopping" (`#/catalogue`), "Done" (`donePurchase`,
  `:disabled="busy || !purchase.length"`).
* `setLineQty(id, raw)`: `_cartOp` → `n = parseInt(raw, 10)`; if
  `!Number.isFinite(n) || n < 0` → toast + `await this.loadCart()` (revert to
  server truth); `n === 0` → `PATCH {qty: 0}` (removes, `cart.py:290-292`) —
  or route 0 through `removeFromPurchase` for one code path; else
  `PATCH /cart/items/{id} {qty: n}` and `_stockWarn`. 422 (`qty > 10000`,
  `schemas.py:416`) → toast the server message + `loadCart()`.
  The `<input type="number">` is **never held** by the capture (§3.3, C1): a
  scan while it is focused is still routed, its first character is retracted from
  the `value` snapshot, and a human's typed quantity can never be corrupted by an
  append-replay. **D8:** because the box is `:value` + `@change` and not
  `v-model`, a concurrent scan or a rejected 422 re-renders it from `this.cart`
  (server truth wins) — intended, and the reason it must not become a `v-model`
  that fights the `_cartOp` chain.
* **D9 (verified, no action):** a barcode scanned while a modal is open is
  dropped with the "Finish the current dialog first" toast in both the old and
  the new design — today's restore assigns the *pre-burst* value
  (`app.js:964-967`), so it removes the payload too. Recorded so a reviewer does
  not have to re-derive it.
* Nav: `#/checkout` gets no topbar link (it is a transient step); the topbar's
  "Catalogue" active state uses `currentView === 'catalogue'`, so on the review
  screen nothing is highlighted — add `|| currentView === 'checkout'` to that
  binding (`index.html:27`) so the user can see where they are.
* `_closeOverlays` (`app.js:470`): nothing to add (the review screen is a
  route, and a user-initiated navigation away from it should close modals as
  before). But `logout` must not strand the user on `#/checkout` — it already
  forces `#/login` (`app.js:513-514`).
* Tests: contract pins (no `confirm(` inside the checkout path; `#/checkout`
  exists in both files; the "Done" button binds `donePurchase`); manual/browser
  evidence for the 6 flows in §4.3. No backend change ⇒ `tests/test_purchases.py`
  and `tests/test_cart.py` stay green untouched (that is the point of D3).

### T6 — item 5: the calendar (details)

See §5. Files: `static/purchase-calendar.js` (pure: `monthMatrix(year, month)`,
`bucketOrders(orders)` fallback, `dayColour(cents, min, max)`,
`monthSummary(days)`), `static/app.js` (state + `loadCalendar` +
`shiftCalendarMonth`), `static/index.html:329-365` (two-column layout),
`static/style.css` (`.history-layout`, `.calendar`, `.cal-dot`, `.cal-total`,
`@media (max-width: 900px)` collapse alongside `style.css:490-494`),
`tests/js/purchase-calendar.test.js`.

### T7 — docs + verification

* `README.md` (flows section around `:135`) and `NOTES.md` (`:21`, `:279`, the
  file-tree at `:133` gains `static/scan-capture.js` +
  `static/purchase-calendar.js` + `tests/js/`): the 60 s countdown, the
  "never painted" capture rule **with both exceptions named (E1 bounded and
  WARM-only; E2 inherent and mode-independent)**, the credential-exposure fix,
  the review screen, the calendar endpoint.
* `scripts/ui_check.py`: the login-view pins stay valid; add a pin that
  `/scan-capture.js` is served (200 + `content-type: application/javascript`) —
  a missing file is a *silent* dead scan bus in a `--dump-dom` run only if the
  console error is caught; `is_error` does catch it, but an explicit fetch pin
  is cheaper to diagnose. Also confirm the `--dump-dom` runs still show
  `data-scan-capture="on"` (T3 must not move the flag's binding,
  `index.html:16`).
* Evidence bundle per PR (repo norm, `PLAN.md:113-115`): Node suite output,
  `ui_check --self-host` output, `uv run pytest -q`, and — for T3 — a hardware
  transcript (preferred) or the simulator transcript of §6, with the PR stating
  which.

---

## 3. Item 1 in depth — the exact capture change

### 3.1 States

```
IDLE ──printable, gap≥50ms, holdable target───▶ HELD(1 char)  [preventDefault, arm 60ms]
HELD ──printable, gap<50ms─────────────────────▶ BURST         [preventDefault, accumulate,
                                                                RE-ARM the timer — L1]
HELD ──timer fires─────────────────────────────▶ replay → WARM(el,400ms)  [splice + input]
BURST ──timer fires (no key for 60ms)──────────▶ replay → IDLE  [NOT WARM: sub-50ms
                                                arrivals prove nothing about a human;
                                                this is the dead-source / lost-CR case]
HELD/BURST ──Enter & shape ok──────────────────▶ route (drop held; never painted)
HELD/BURST ──Enter & shape bad─────────────────▶ replay → IDLE   (Enter keeps its default)
HELD/BURST ──printable, gap≥50ms───────────────▶ replay, then HELD(new char)
HELD/BURST ──non-printable / modifier / repeat / composing / paste / focus / pointerdown──▶ replay → pass through
WARM(el, 400ms) ──printable────────────────────▶ native (no preventDefault); snapshot
                                                value+caret on gap≥50ms keys
WARM ──burst confirmed (2nd fast key)──────────▶ retract to snapshot → BURST   [E1]
NO-WARM targets (D1c: password / autocomplete=username) ── always the HELD path [E1 closed]
```

**Why the re-arm is safe (L1, glm's catch).** Rev 1 armed the timer only on
entering HELD and never said what became of it in BURST — both readings are
defects: a surviving timer fires ~60 ms after key #1, i.e. **mid-payload**,
flushing half a code into the field; a cancelled one leaves a dead-source burst
(scanner unplugged, CR suffix disabled) holding its characters indefinitely, to
reappear at some arbitrary later flush trigger. Re-arming on every held key
resolves both, and cannot fire during a live burst because
`SCAN_DEFER_MS (60) > SCAN_MAX_GAP_MS (50) > scanner inter-key (10-20 ms)`: a key
that arrives fast enough to extend the burst is always early enough to re-arm
past its own deadline.

`_buf` (the logical burst text) is maintained in **every** state, including
WARM, so the shape gate at Enter sees the same string as today. What differs
per state is only whether the characters physically land.

### 3.2 The three write paths (all spliced, all caret-preserving)

```js
// held chars → field (the "proved human" path)
function replay(held) {
  const {el, chars, value, start, end} = held;
  el.value = value.slice(0, start) + chars + value.slice(end);
  const caret = start + chars.length;
  try { el.setSelectionRange(caret, caret); } catch (_) {}   // type=email/number guards
  el.dispatchEvent(new Event("input", { bubbles: true }));    // what v-model reads
}
// landed chars → undone (the "warm burst" path); `snap` is the pre-key state
function retract(snap) {
  snap.el.value = snap.value;
  try { snap.el.setSelectionRange(snap.start, snap.end); } catch (_) {}
  snap.el.dispatchEvent(new Event("input", { bubbles: true }));
}
```
Plain `Event`, not `InputEvent` — that is what the current restore already
dispatches (`app.js:966`), all `v-model` needs is an `input` event whose
`target.value` it can read (verified in the vendored bundle: `vModelText` binds
`t ? "change" : "input"` where `t` is the **`lazy`** modifier, and `index.html`
has zero `.lazy`), and it keeps the Node harness free of another global.
Only **holdable** targets are ever spliced; `type=number` and `SELECT` are not
held at all (§3.3), so they need no `caret` variant — rev 2 of this plan briefly
held number inputs with an append-style replay, which is worse than the flash it
avoids (see L2 in `r3rev-qwen.md`: the caret is unreadable there, so a replay
would insert a human's character at the end regardless of where they clicked).

**Stale-snapshot guard.** The snapshot is taken at keydown and the replay can
happen up to 60 ms later, so anything that writes the field in between (a mouse
paste, browser autofill, another script) would be clobbered by a blind splice.
Two rules: (1) `paste`, `cut`, `drop` and `beforeinput` with an
`inputType !== "insertText"` **flush immediately** (§3.3b), which removes the
window for the common cases; (2) at replay time, if `el.value !== snap.value`
the held chars are inserted at the element's **current** caret
(`el.selectionStart`) instead of the snapshotted one. Both are one-liners and
both are tested.

Fixes §0.1c (caret) and, via the target classifier (§3.3), §0.1a/b.

### 3.3 Target classification (what may be prevented, what may be spliced)

| target | printable keys | burst confirmed | why |
|---|---|---|---|
| `INPUT` type ∈ {text, search, password, email, tel, url, ""} + `TEXTAREA` | **preventDefault + hold** (IDLE) / native (WARM) | keep holding | caret splice is well-defined |
| …of which `type=password`, `autocomplete=username`, `autocomplete*=password` | **always hold — never WARM** (`SCAN_NO_WARM_SELECTOR`, D1c) | keep holding | the only fields where a painted character is a credential fragment (§0): this closes E1 for the login screen entirely |
| `INPUT` type ∈ {number, date, time, month, week, color, range} | native, snapshot `value` only | preventDefault + restore `value` (no caret) | `selectionStart`/`setSelectionRange` throw `InvalidStateError` on these types, so neither a caret splice nor a faithful append-replay is possible; browsers also refuse non-numeric characters in `type=number`, so most payloads cannot even be represented there. Bounded cost: the burst's first character for ≤1 frame. Fixes §0.1b |
| `INPUT` type ∈ {checkbox, radio, file, button, submit} | native, no snapshot | preventDefault | nothing to paint; preventing stops a burst from toggling/submitting |
| `SELECT` | native, snapshot `selectedIndex` | preventDefault + restore `selectedIndex` + dispatch **`change`** | fixes §0.1a, both halves of it: `vModelSelect` binds `change` and derives the model from `el.options`/`el.selected` (verified in the vendored bundle), so an `input` dispatch would leave DOM and model disagreeing, and native type-ahead fires `change` per key → today a burst also storms `reloadItems()` via the `category` watcher (`app.js:313-315`) |
| `isContentEditable` | native, no snapshot | preventDefault only | no `.value`; a Range-based splice is not worth it — **no contentEditable exists in `index.html`** (verified: zero matches) |
| `[data-scan-sink]` ancestor | **untouched — early return before any of the above** | — | the collapsed badge field (`index.html:86`) and the catalogue scan field (`index.html:135`) keep their dedicated Enter flows; the ui_check placeholder pin (`ui_check.py:298`) and `doBadgeLogin` (`app.js:406-411`) are unchanged |
| non-editable (body, button, link) | native | preventDefault | no text can appear; preventing keeps the burst's Enter from activating a focused button |

### 3.3b Exclusions that make it no-hijack

* **`compositionstart` (document) and `e.isComposing || e.keyCode === 229`** →
  `flush()` (replay anything held) and return without preventing. Two reasons,
  not one: IME composition must never be intercepted, **and** Vue's `vModelText`
  handler begins `if (t.target.composing) return` (verified in the vendored
  bundle) — so a replay landing *during* a composition would update the DOM value
  but not the model, a silent desync. The `compositionstart` listener is what
  makes that unreachable: the flush happens before `el.composing` is ever set,
  which also covers platforms where the first composing `keydown` does not yet
  carry `isComposing`.
* `e.repeat` → `flush()` + clear WARM + return: auto-repeat is human by
  definition; today it is *classified as a scanner* (gaps ~30 ms), which is a
  false positive the new guard removes.
* `ctrl/meta/alt` → reset, return (unchanged, `app.js:949`) — shortcuts are
  never eaten. `e.key.length !== 1` → `flush()` then return **without**
  preventing: Backspace/Tab/Escape/arrows act natively, and the field's text is
  brought up to date first so the browser's own undo stack and Vue's model
  agree with what is on screen.
* `pointerdown` (document, capture) → `flush()` + reset `_buf`: a click means a
  human is present; held characters are written, never dropped.
* `paste` / `cut` / `drop` / non-`insertText` `beforeinput` → `flush()` first
  (the stale-snapshot guard, §3.2): a mouse-driven insertion must never be
  spliced over by a replay 60 ms later.
* `focusin` → `flush()` into the *snapshotted* element (not the newly focused
  one) and drop WARM if the element changed.

### 3.4 Invariants (each one is a test, not a comment)

1. **No keystroke is ever lost.** Every prevented printable char is either
   (a) inside a shape-gated burst that was routed, or (b) replayed. Tests:
   timeout flush; Backspace/Tab/Escape interrupt; unshaped Enter; focus change;
   pointerdown; paste-while-held; `repeat`; `isComposing` **and**
   `compositionstart`.
2. **A shape-gated burst never writes to any field.** Tests: badge into a
   focused `<input type="text">` with pre-existing text and a mid-text caret →
   value and caret byte-identical afterwards, and `input` never dispatched;
   same for `password`, `search`, `textarea`, and a non-editable target;
   `select` → `selectedIndex` preserved **and** a `change` (not `input`)
   dispatched; `number` → `value` preserved (its first character is retracted,
   not held).
2b. **A dead-source burst cannot hang (L1).** Test: 6 chars at 12 ms with **no**
   Enter → the timer fires 60 ms after the last key, all 6 chars are replayed in
   order, and the state is IDLE (not WARM).
2c. **A credential field never warms (D1c).** Test: a human-speed run in
   `input[type=password]` → every key is held-then-replayed (no native
   pass-through), so a scan arriving mid-run paints nothing at all.
3. **Human-speed typing is native after the first char of a run.** Test: keys at
   100 ms gaps → the first is held ≤60 ms then replayed, all later ones are not
   prevented (`preventDefault` call count == 1 for 20 keys).
4. **The collapsed badge-code fallback still works.** Test: a burst whose target
   is inside `[data-scan-sink]` → zero `preventDefault`, zero `onScan`, the
   element's own `keydown.enter` handler is what fires (assert the stub's
   handler ran and the value contains the payload).
5. **The visible scan field's explicit path is untouched** (`scanFieldSubmit`,
   `app.js:1012`): it accepts payloads the shape gate rejects (short internal
   codes). Test: a 3-char code into the sink reaches the sink's value; the same
   code into a normal field is ignored (no `onScan`) — the deterministic escape
   hatch is documented in the login/catalogue copy already.
6. **Unshaped bursts are inert**: no `onScan`, no server call (the "digit-free
   word is typing, not a scan" rule, `app.js:18-22`).
7. **`SCAN_DEFER_MS > SCAN_MAX_GAP_MS`** (pinned in Node *and* pytest).
8. **Routing is unchanged**: `onScan(code, kind)` receives exactly the strings
   today's `enqueueScan` receives for the recorded real payloads (`HCRM1:` + 43,
   bare 43, EAN-13, an opaque SKU with a digit), so `handleBadgeScan`/
   `handleBarcodeScan` (`app.js:1062,1085`) and the pill masking
   (`app.js:1026-1037`, badges never rendered raw) need no edits.
9. **Timing source is injected** — no test sleeps; the harness clock advances.
   (Same discipline as `tests/test_logoff.py:8-11`.)
10. **An unshaped Enter into a form still submits (D10).** Test: 3 fast chars
    into a focused field inside a `<form>` + Enter → the chars are replayed, the
    `input` dispatch updates the model **synchronously**, Enter is not prevented,
    and the form's submit handler then sees the replayed text. This is today's
    behaviour (a login attempt with scanned garbage → 401); pinning it stops the
    replay path from being "fixed" into swallowing the Enter.
11. **The exceptions are exactly E1 and E2, and nothing else.** Test E2
    explicitly (a human key followed 30 ms later by a full badge burst → the
    payload is *not* routed and *does* land, identically under
    `SCAN_STRICT_DEFER = true` and `false`) so the limit is documented in code
    rather than discovered in production.

### 3.5 Real-payload fixtures (from the repo, not invented)

`HCRM1:` + 43 chars (badge, `app/qrbadge.py` `BADGE_PREFIX`), a bare 43-char
token (`_RAW_TOKEN_RE`, `auth.py:126-127`), EAN-13 `4006381333931`-style
digits (`BARCODE_DIGITS_RE`), and the M-10's CR suffix (`app/scanner/protocol.py:7-9`
— factory default suffix CR; LF/CRLF also accepted, so `Enter` may arrive as
`key: "Enter"` from the wedge). Inter-key gaps: 10-20 ms per the existing
comment (`app.js:12`); tests use 12 ms and 20 ms, plus a 45 ms "worst-case
scanner" and a 55 ms "worst-case human" pair to pin the boundary on both sides
of the 50 ms gate.

### 3.6 Optional hardware hardening (out of scope, worth a follow-up ticket — **the only fix for E2**)

The ambiguity in §3 exists only because a keyboard wedge sends *exactly the same
events* as a human. The M-10 can be reconfigured (Universal Menu Book — see
`app/scanner/protocol.py:7-9`, which already anticipates a changed suffix) to
emit a **sentinel prefix** (e.g. `GS`/0x1D, or an unused function key) before
the payload. With a sentinel, interception becomes deterministic: swallow
everything from the sentinel to the CR — zero defer window, zero latency for
humans, zero paint, no shape gate needed for routing, **and E2 disappears** (a
scan starting 10 ms after a human keystroke is still recognisable, because the
sentinel — not the timing — is the marker). This plan must **not** depend on it
(any wedge, any kiosk, any misconfigured scanner), but it removes the heuristic
entirely for our own hardware. Recommend filing the round-3.5 ticket now:
config barcode + `SCAN_SENTINEL` support in the capture module +
`scripts/m10_scan_test.py` evidence.

---

## 4. Item 4 in depth — the review screen's state design

### 4.1 State

**No new cart state.** The server cart *is* the review state (`this.cart`,
refreshed by every mutation's response — the round-2 "one payload, no
read-after-write" contract, `schemas.py:443-446`). The screen adds exactly one
piece of UI state: the route (`#/checkout` → `currentView === "checkout"`).
Rationale: a local copy of the lines would reintroduce the desync class that
round 2's `_cartOp` chain and the durable-cart migration were built to kill
(`app.js:661-664`, `app.js:672-711`), and "add" from the review screen must be
visible to a concurrent scan from the same session.

Edit-in-place therefore means: every control calls an existing endpoint through
`_cartOp`, and `this.cart` is replaced by the response:

| control | call | endpoint |
|---|---|---|
| `+` / `−` | `incPurchase(id)` / `decPurchase(id)` (`app.js:730-750`) | `POST /cart/items`, `PATCH`/`DELETE /cart/items/{id}` |
| qty box | **new** `setLineQty(id, raw)` | `PATCH /cart/items/{id}` (absolute; `0` removes, `cart.py:290-292`) |
| `✕` | `removeFromPurchase(id)` (`app.js:751-755`) | `DELETE /cart/items/{id}` |
| add | `addToPurchase(item)` from the grid (`app.js:719-729`) / `handleBarcodeScan` (`app.js:1085`) | `POST /cart/items`, `POST /cart/scan` |
| "Clear list" (kept, ghost) | `clearPurchase()` (`app.js:756-760`) | `DELETE /cart` |
| **"Done"** | **new** `donePurchase()` | `POST /orders` |

`busy` still guards "Done" against a double submit (`app.js:895,901`), and
`donePurchase` still runs inside `_cartOp` so a scan landing mid-checkout cannot
interleave between the server-side cart read and the cart clear
(`app.js:903-909`).

### 4.2 Transitions

```
catalogue ──[Complete purchase]──▶ #/checkout (review)
#/checkout ──[Keep shopping]────▶ #/catalogue        (cart untouched — it is durable)
#/checkout ──[Done]──POST /orders──201──▶ #/account + profileTab="purchases"
                                          + expandedOrders=[new id] + "new" chip
                                          + toast (modal only if skipped/clamped, D5)
                              └─400 (empty)──▶ toast "Cart is empty" → #/catalogue
                              └─401──────────▶ handleSessionExpired() (app.js:1124)
                              └─503 (locked)─▶ toast "retry" (orders.py:105-111, Retry-After: 1)
#/checkout ──badge scan (own)──▶ logoff overlay counts down OVER the review screen
             (badge, other)────▶ switchAccount → #/catalogue as the NEW user
             (barcode)─────────▶ adds a line to the review list live
#/checkout ──page reload──────▶ deep link: user set? cart loaded? empty → catalogue
```
Every programmatic navigation assigns `this.route` **in the same synchronous
turn** as `location.hash` (the `afterLogin`/`switchAccount` pattern,
`app.js:400-401`, `app.js:1221-1222`), so the queued `hashchange` handler's
`navigated` test (`app.js:1863-1878`) is false and `_closeOverlays()` does not
kill the post-checkout receipt modal. A route written without the matching
`this.route` assignment is the bug this rule prevents — it would close the modal
before it ever rendered.

### 4.3 Flows to demonstrate in the PR (browser evidence)

1. 3 items → Complete purchase → review shows all 3 with totals; no dialog.
2. qty box 2 → 5: line total, grand total and the stock warning update; a value
   above stock warns but is allowed (clamp-not-veto is the server's job).
3. `✕` a line → the list and total update; "Done" with 0 lines is disabled and
   the empty-cart bounce fires if the last line is removed.
4. Add from the grid and by scanning while on the review screen → the new line
   appears in the review list.
5. "Done" → history view, the new receipt pre-expanded with the `new` chip
   (`index.html:339`), toast with the total; cart empty everywhere (the sidebar
   on `#/catalogue` shows the empty copy, `index.html:204-206`).
6. Clamp/skip case (stock 1, qty 3; and a deleted item still in the cart) → the
   receipt modal appears with `applied_qty`/`clamped`/`skipped` reported.
7. Own-badge scan on the review screen → the 60 s overlay; expiry logs off and
   the review screen is not left behind (`logout` forces `#/login`).

---

## 5. Item 5 in depth — the calendar

### 5.1 Data

`loadCalendar()` on entering the `purchases` tab (alongside `loadMyOrders`,
`app.js:763-768`) → `GET /api/orders/calendar?year&month` (T2). State:
```js
calYear: <current UTC year>, calMonth: <1-12>,
calDays: {},            // {"YYYY-MM-DD": {cents, count}}
calTotalCents: 0, calPurchaseCount: 0, calLoading: false,
```
`shiftCalendarMonth(±1)` re-fetches (prev/next month nav — free once the
endpoint exists, and the reason D4 beats client-side bucketing). Re-fetch after
a checkout (so a just-completed purchase dots today) and on
`selectProfileTab("purchases")`.

### 5.2 Grid + gradient (pure functions in `static/purchase-calendar.js`)

* `monthMatrix(year, month)` → `[{key: "2026-10-01", day: 1, inMonth: true,
  isToday: bool}…]`, Monday-first, with leading/trailing blanks so the grid is a
  clean 7×N. Built with `Date.UTC(y, m-1, d)` so it is timezone-independent
  (a `new Date(y, m-1, d)` local-midnight construction would shift the keys for
  users west of UTC — the whole map would miss by one day).
* `dayColour(cents, min, max)`:
  `t = max > min ? (cents - min) / (max - min) : 0` →
  `hsl(${120 * (1 - t)}, 73%, ${75 - 25 * t}%)`.
  `t = 0` ⇒ `hsl(120,73%,75%)` = CSS `lightgreen` (the spec's "first purchase
  is light green" falls out for free: a single purchase in the month has
  `min === max` ⇒ `t = 0`); `t = 1` ⇒ `hsl(0,73%,50%)` = red. The hue path
  passes through yellow/orange, which reads as a heat ramp; a 2-stop RGB lerp
  goes muddy brown in the middle, so HSL it is. `min`/`max` are over **day
  totals** (the dots), not over individual purchases — one dot per day is the
  spec, so one value per day is the scale.
* `monthSummary(days)` → `{min, max, total, count, busiest: {key, cents}}` —
  `min`/`max` from the day values; `total` cross-checked against the endpoint's
  `total_cents` in tests (they must agree; if they don't, the endpoint wins and
  a console warning fires — a cheap drift detector).
* Dot markup: `<span class="cal-dot" :style="{background: dayColour(...)}"
  :title="`${count} purchase(s) · ${formatPrice(cents)}`">` inside the day cell;
  the cell keeps its day number, `isToday` gets a ring, out-of-month cells are
  blank. Below the grid: `<div class="cal-total">Spent in {{ monthLabel }}:
  <strong>{{ formatPrice(calTotalCents) }}</strong></div>` (+
  `{{ calPurchaseCount }} purchase(s)`).
* Layout: `.history-layout { display: grid; grid-template-columns: 1fr 300px;
  gap: 1.5rem; }` inside the `purchases` tab (list left, calendar right),
  collapsing to one column in the existing `@media (max-width: 900px)` block
  (`style.css:490-494`) — mirroring `.catalogue-layout` (`style.css:136-141`).
* Empty month: the grid still renders (all cells dotless) and the total reads
  `€0.00` — never a blank hole.

### 5.3 Tests

* Node (`tests/js/purchase-calendar.test.js`): `monthMatrix` for a month
  starting on Monday/Sunday, a leap February, and a 31-day month (cell count,
  key format, no local-time off-by-one — assert against fixed UTC expectations
  with `TZ` set both ways in the harness); `dayColour` at t=0/0.5/1 and the
  degenerate `min === max` case (must be lightgreen, not NaN); `monthSummary`
  with one day, many days, and `{}`.
* pytest: T2's endpoint tests (§T2) + the contract pin that the purchases tab
  contains the calendar container and the month-total binding.
* Manual: seed a member with 3-4 purchases across a month (two on one day), open
  the tab → dots, ramp, total; next/prev month nav; a purchase made through the
  review screen dots today immediately.

---

## 6. Risks and open questions

**R1 (highest) — T3 is a rewrite of the repo's most subtle frontend code.**
`app.js:976-1005` carries a comment about an earlier revision that "corrupted
EVERY global capture" by skipping the first character. Mitigations: the state
machine moves to its own file with injected deps and ~30 Node tests (§3.4);
the routing half (`enqueueScan` → `drainScanQueue` → `handle*Scan`) is not
touched at all; the sink early-return stays the first statement; the
`data-scan-capture="on"` pin still proves the listener installed.

**R2 — Hardware status: measured, and it is *not* "dead".** Both peer plans
assert "the M-10 is dead → the simulator is the only verification path". Measured
on this host during this review:
* `lsusb` shows **no `065a` device on any bus** (present: root hubs, a Genesys
  hub, USI `10ab:9309`, Chicony camera) — the scanner is simply **not attached**,
  which is a different claim from a dead controller.
  `docs/m10-unreachable-2026-10-10.md` §8 records the controller as **revived**
  (unbind/bind + PCI rescan, no reboot), and `usb1` currently enumerates a hub +
  a device, so that bus is alive.
* `/dev/hidraw0` is the **Synaptics touchpad** (`06CB:CE67`, `hid-multitouch`),
  not a scanner — the "root-only hidraw" blocker from the sync doc is not even
  present here.
* `scripts/udev/99-opticon-scanner.rules` **is installed** in
  `/etc/udev/rules.d/` (dated Oct 10 23:02) and this user **is** in `plugdev`
  and `dialout` (`id`), so the HIDAPI path (`scripts/scanner_hid.py`,
  `scripts/m10_scan_test.py`) should work unprivileged after a re-plug. The sync
  doc's "one blocker remains" is stale.
* **Decisive for item 1:** the wedge path never needed any of that. The sync
  doc's last entry shows the HID personality as
  `/dev/input/by-id/usb-Opticon_Opticon_USB_Barcode_Reader-event-kbd → event14`
  and says "Test it in five seconds, **no sudo, no udev rule**": a root-only
  `/dev/input/eventN` blocks *our process* from reading evdev, not the X server,
  which is what delivers keystrokes to the focused browser window.
Consequence: T3's evidence gate is **re-plug and run the browser pass**,
with the synthetic-event simulator as the fallback — and the PR must say which it
used. The `isTrusted` rule stands independently (the Node suite and any
console-dispatched simulation need it; a hardware pass does not conflict).

**R3 — 60 ms of added latency on the first character of a keystroke run.**
Bounded, documented, and only in editable fields; WARM removes it for sustained
typing. If the coordinator prefers the absolute guarantee, flip
`SCAN_STRICT_DEFER = true` (D1) — noting that this closes **E1 only**, not E2
(§D1). Either way it is one constant, one test.

**R4 — Vue `v-model` vs synthetic `input` events (rev 2: premise corrected).**
Replay/retract set `.value` and dispatch `input`. Decoded from the vendored
`static/vendor/vue.global.prod.js`: `vModelText` binds
`t ? "change" : "input"` where **`t` is the `lazy` modifier**, and
`index.html` contains **zero** `.lazy` — so every text-ish field in this SPA
updates on `input`, and a synthetic `input` is sufficient. (Rev 1 of this plan,
and both peer plans, claimed `v-model.number` listens to `change`; it does not —
`.number` only selects a `looseToNumber` coercion, which Vue also applies
automatically when `props.type === "number"`.) The real caveats are: (a) Vue
ignores `input` while `el.composing` → hence the `compositionstart` flush
(§3.3b); (b) the browser's native undo stack does not know about a splice
(Ctrl+Z after a replayed scan may jump oddly). Both accepted and documented in
the module docstring.

**R5 — The 300 ms debounced search watcher** (`app.js:310-312`, `:358-361`)
fires once per replayed `input`. A replay is one `input` event for the whole
held chunk, so this is *fewer* reloads than native typing. No change needed.

**R6 — Item 5's month semantics.** "The current month" = the current **UTC**
month (the server's clock and the storage format), while `formatDate` in the
list renders the stored UTC wall-clock through a local-time parse
(`app.js:350-352`). Both agree on the *date label*, so a dot and its list entry
never disagree — but a purchase made at 23:30 UTC+2 shows under the UTC day.
Q5: do we (a) leave it (consistent, documented), (b) bucket by browser-local
day (dots would then need the client to re-bucket, and the endpoint's totals
would not match the dots), or (c) fix `formatDate` to append `Z` (correct
timestamps, but it changes every date shown in the app — out of scope for
round 3). **Recommendation: (a)**, with a one-line comment in both places.

**R7 — E2 is irreducible without a sentinel prefix (§D1, §3.6).** A scan whose
first character arrives <50 ms after a human keystroke *in the same focused
field* is indistinguishable from typing: the payload accumulates mixed with the
human's characters, fails the shape gate, and stays in the field. This is
**today's behaviour, unchanged**, and `SCAN_STRICT_DEFER` does not fix it. It is
named here so nobody discovers it in production and reads it as a regression
introduced by T3. Practical exposure is tiny (it requires a trigger pull within
50 ms of a keystroke in a focused field); the visible scan field and the sentinel
follow-up are the answers.

**R8 — A badge scanned while any request is in flight is silently dropped today**
(`app.js:417-419`), and T5 lengthens that window (the checkout POST holds
`busy`). D7 adds a toast; the guard itself must stay (it prevents minting a
throwaway session). Low risk, high operator-confusion value.

**Q1 — Strict vs WARM defer (D1).** Coordinator call; default WARM.
**Q2 — "not already logged in" in item 2.** `qr-login` mints a session
regardless of existing ones, and `qr-identify` answers only `same_user`. If the
incoming user already has a live session *on this kiosk* there is nothing to
detect (the SPA has one session at a time by construction). If the intent was
"show who is about to take over the kiosk", `QrIdentifyOut` would need the
incoming user's **name** — that is a small backend change with an
enumeration/privacy trade-off (it discloses that a badge belongs to a real
account and whose). **Recommendation: no change**; the switch toast already
names both users after the fact (`app.js:1223`). Ask the owner whether the
prompt should say "Switch to &lt;name&gt;?" *before* the switch.
**Q3 — Does the review screen need a "back" affordance in the topbar?** Default:
"Keep shopping" button only (D3).
**Q4 — Receipt modal after "Done"**: always / never / exception-only (D5).
Default exception-only.
**Q5 — Calendar month/timezone semantics** (R6). Default (a).
**Q6 — Logged-out deep link to `#/checkout`** → login form, then catalogue
(not back to checkout). Default: accept; the alternative (remember the intent
and return to checkout after login) needs a redirect-target field, which the
SPA has never had.
**Q7 — Should the calendar show only the *current* month, or navigate?**
The spec says "a calendar of the current month"; T2/T6 ship prev/next nav
because the endpoint makes it ~10 lines. Say the word and the nav buttons come
out (the endpoint stays).
**Q8 — Node in CI.** A new `js` job (D2) is the proposal; the alternative is
running `node --test` from pytest, which makes Node a prerequisite for
`uv run pytest` on every dev machine. Recommendation: separate job.
**Q9 (new, from the cross-review) — Adopt D1c / D7 / D11?** Recommendation:
**yes / yes / document-only**. D1c (no WARM for credential fields) is what makes
"no part of a badge payload ever paints on the login screen" literally true;
D7 is a one-line toast; D11 is a documented 4 s banner lifetime.
**Q10 (new) — File the round-3.5 sentinel-prefix ticket now?** Recommendation:
**yes**, out of scope for round 3. It is the only thing that removes E2, and it
is a scanner-configuration task (Universal Menu Book) plus a `SCAN_SENTINEL`
constant in the capture module — not a redesign.

---

## 7. Suggested PR sequence (each independently reviewable and green)

1. `feat(frontend): JS test harness + scan-capture extraction` (T0) — no
   behaviour change; CI job appears here.
2. `feat(auth): logoff grace 30s → 60s` (T1) — one constant + copy + tests.
3. `feat(orders): monthly calendar aggregate endpoint` (T2).
4. `feat(spa): intercept scan bursts without painting them` (T3) — the big one;
   leads with the credential-exposure fix (§0), carries the Node matrix and the
   hardware-or-simulator evidence, and names E1/E2 in the PR body.
5. `feat(spa): "Hi <name>" greeting on every session entry` (T4).
6. `feat(spa): checkout review screen replaces the confirm dialog` (T5).
7. `feat(spa): purchase-history calendar with spend heat dots` (T6).
8. `docs: round 3 — capture rule, review flow, calendar` (T7).

PRs 1-3 can be reviewed/merged in any order; 4-7 are serial (same three files).

---

## 8. Cross-review record

This is rev 2. The per-peer verdicts, the measured corrections to shared claims
(`v-model.number`, the SELECT `change` requirement, the hardware status, Vue's
composing guard, the single `purchases.created_at` writer, the autofocus
credential exposure) and my own five-entry rev-1 defect ledger are in
**`docs/plans/r3rev-qwen.md`**. Where this plan and a peer plan disagree, that
file says which is better and why; where all three converged, §1 of that file
records it as settled.
