# Task D proposal (qwen3.8-max) — SPA: the profile tab, re-viewable badge, checkout (frontend only)

Implements `docs/plans/COORDINATOR2.md` decisions 5–6 + Task D on
`static/app.js` / `index.html` / `style.css` (Vue 3 options API, vendored
full build, in-DOM template, no Node toolchain, no JS runner — established).
Backend (Tasks A–C) is merged: badge re-view with the status envelope,
change-phone/change-email returning `UserOut`, `POST /api/orders` →
`CheckoutOut`, `GET /api/orders/mine`, `GET /api/scan-requests/mine`
(already wired).

**Verified this pass:** the account view is `index.html:222` (`<h1>Account</h1>`,
inline password form, "QR badge login" section at :244 with
generate/revoke + inline `badgeSvg` display, "My scan requests" table added
in Task 5); the checkout placeholder is `index.html:212`
(`@click="requestPurchase"` "Request items" → toast); the members tab's
per-member `QR badge` button (`showMemberBadge`, :539) opens the
`memberBadge` modal; the **one-shot copy to fix** is the `badgePrint` modal
at :714–719 ("Shown only once — print or write it down now. … It cannot be
retrieved again."); the tab pattern to copy is `admin-tabs` (:392);
`changePassword` establishes the success-note + `USER_KEY`-refresh idiom;
`api()` since Task 5 throws with `err.status`/`err.detail` attached (the
structured-409/404 branching this task needs) and accepts `{token}`/`{raw}`;
`badgePrint = {title, subtitle, svg, payload}` drives the isolated print
modal (`printing-badge` body class via watcher); `_closeOverlays()` nulls
`badgePrint` on logout/switch/session-expiry/navigation.

---

## 1. `#/account` → "My profile", tabbed (no route churn)

Keep the route (`#/account`), the `currentView === 'account'` computed and
every redirect that targets it (`must_change_password` after login/switch,
the nav link) — **only the presentation changes** (decision 5's point: the
gate redirect keeps working with zero logic edits).

- Nav label `Account` → `Profile`; `<h1>Account</h1>` → `<h1>My profile</h1>`
  (the identity line `name · email · role · phone` stays under it).
- New data prop `profileTab: "details"`, tab bar reusing the `.admin-tabs`
  markup/CSS class verbatim (house pattern, no new CSS concept):
  **Details · Security · My QR badge · Purchases · Scan requests**.
- Existing blocks move under `v-show`/`v-if` per tab *unchanged* (password
  form → Security; badge section → My QR badge; my-scan-requests table →
  Scan requests). `v-if` (not `v-show`) so hidden tabs make no requests.
- **Tab default + the flagged flow:** `currentView`'s existing watcher
  (which calls `loadMyRequests` on account) becomes: if
  `user.must_change_password` → `profileTab = "security"` (the gate's
  destination is the password form), else `"details"`. Tab buttons for
  403-gated data stay visible but their loaders no-op while flagged (the
  banner already says why; `loadMyOrders`/`loadMyRequests` catch silently —
  the existing `loadMyRequests` pattern).
- Lazy loading per tab activation: `@click="profileTab = 'purchases';
  loadMyOrders()"` style (one small `selectProfileTab(tab)` method so the
  logic isn't repeated in markup).

## 2. Details tab — identity & contact

New data: `emailForm {new_email: "", current_password: ""}`, `emailError`,
`emailOk`, `phoneForm {phone: ""}`, `phoneError`, `phoneOk`.

- **Change email** form (new email + current password — mirrors the
  password form's two-field shape): `POST /api/auth/change-email` →
  on 200: `this.user = r` (full `UserOut`) + `localStorage.setItem(USER_KEY,
  …)` (the `changePassword`/`generateMyBadge` state-refresh idiom), clear
  form, `emailOk = "Email updated — other sessions were signed out"`
  (**the revocation toast decision-4 requires**, phrased exactly like the
  password form's existing note); errors → `emailError = e.message`
  (400 wrong password / no-op, 409 taken — `api()`'s message extraction
  already renders the string details). Prefill `new_email` with the current
  one for convenience.
- **Change phone** form: single input + [Save] + [Clear] (Clear = submit
  `""`, which the backend normalises to NULL — the `""`-clears contract;
  one code path, no special case). `POST /api/auth/change-phone` → 200
  `UserOut` → same state refresh; show the **normalised** value from the
  response in the input afterwards (server truth: `"+34 600 123 456"`
  displays as `+34600123456`); 422 messages surface verbatim
  (`api()` renders pydantic's `field: msg`). `phoneOk` note. No password
  field — and the UI says why ("contact info — no password needed").
- The identity line under the h1 gains `user.phone` when set.

## 3. My QR badge tab — re-view + the envelope handling

Section content: status line (`has_qr_badge`), **[Show my QR]** (primary,
new), existing [Regenerate/Generate] and [Revoke] buttons, and the inline
`badgeSvg` display **removed** (the isolated print modal supersedes
printing-the-whole-page; Task 6's `printAccountBadge` indirection is
replaced by the re-view flow — `badgeSvg`/`badgePayload` data and
`printAccountBadge` method are deleted, `_closeOverlays` loses two fields).

```js
async showMyBadge() {
  try {
    const view = await api("/auth/qr-badge");           // 200: QrBadgeViewOut
    this.badgePrint = {
      title: "Your QR login badge",
      subtitle: view.migrated
        ? "Your badge was just UPGRADED to a re-viewable one — the previously printed code stopped working. Print this one."
        : "You can re-view this badge here at any time.",
      svg: view.svg, payload: view.payload,
    };
  } catch (e) {
    if (e.status === 404) { this.badgeState = "none"; return; }        // → [Generate] CTA
    if (e.status === 409 && e.detail && e.detail.status === "no_key")
      { this.badgeState = "no_key"; return; }
    if (e.status === 409 && e.detail && e.detail.status === "key_mismatch")
      { this.badgeState = "key_mismatch"; return; }
    this.showToast(e.message);
  }
}
```

- `badgeState` (null | "none" | "no_key" | "key_mismatch") drives an inline
  panel in the tab: `none` → "No active badge" + [Generate];
  `no_key` → "Re-viewing is unavailable on this server right now (logins
  still work)"; `key_mismatch` → "The server's badge key changed —
  regenerate to re-enable viewing" + [Regenerate]. Both 409 states keep
  [Regenerate] as the healing CTA (rotation always works: it re-issues
  under the current key, or random-legacy when keyless — the backend's
  documented degradation).
- **Self-view needs no confirm** (own credential; the migration case is the
  user's own upgrade) — but a `migrated: true` response gets the loud
  subtitle above **plus** a toast ("Your old badge stopped working — print
  the new one").
- The `badgePrint` modal itself is unchanged (isolated print, payload line,
  `printing-badge` class) except its **warning copy** (§6).

## 4. Purchases tab — history + receipts

- `myOrders: []`, `ordersOffset`, `ordersHasMore`, `expandedOrder: null`.
- `loadMyOrders(more = false)`: `GET /api/orders/mine?limit=20&offset=…` →
  append/replace; [Load more] when a full page came back (the catalogue's
  `hasMore` idiom). Silent catch while flagged.
- List rows: date · total · `N item(s)` · a **clamp chip** when any line has
  `clamped` ("shortfall recorded") · expand toggle → the receipt lines:
  `name (sku)` · `price × qty` · when `clamped`: "only `applied_qty` of
  `qty` in stock — you were charged for `applied_qty`" (the applied-basis
  total explained where the member sees it) · line totals. Expand =
  `expandedOrder = (expandedOrder === p.id ? null : p.id)` (one open
  receipt, no state array).
- Empty state: "No purchases yet — add items to your purchase list and
  press **Complete purchase**."

## 5. Checkout — "Complete purchase" (replaces `requestPurchase`)

- `index.html:212`: button text → **Complete purchase**,
  `:disabled="purchase.length === 0 || busy"`, `@click="checkout"`.
- ```js
  async checkout() {
    if (!confirm(`Complete this purchase (${this.cart.total_qty} item(s), ` +
                 `${this.formatPrice(this.purchaseTotalCents)})?`)) return;
    this.busy = true;
    try {
      const r = await api("/orders", { method: "POST" });   // 201 CheckoutOut
      this.cart = r.cart;                                   // emptied — one payload
      this.lastReceipt = r.purchase;                        // receipt modal
      const clamped = r.purchase.lines.filter(l => l.clamped).length;
      const skipped = r.skipped.length;
      this.showToast("Purchase complete"
        + (clamped ? ` — ${clamped} line(s) limited to available stock` : "")
        + (skipped ? ` — ${skipped} stale item(s) skipped` : ""));
      this.myOrders = []; this.ordersOffset = 0;            // invalidate history cache
    } catch (e) { this.showToast(e.message); }              // 400 empty race, etc.
    finally { this.busy = false; }
  }
  ```
- **Receipt modal** (`lastReceipt`): the `PurchaseOut` rendered like the
  Purchases-tab receipt (same partial markup), with clamp/skip notes and a
  [Done] — the member's proof-of-purchase screen; `data["status"] == 201`
  body is the truth (no read-after-write, Task-1 mutator contract).
- Cart panel keeps its live `clamped`-free display; the stock warnings on
  lines (`qty > stock`, Task 5) already pre-announce what checkout will
  clamp — consistent story, no new panel logic.
- The old `requestPurchase()` method is deleted (its placeholder toast is
  the tracked-issue text Task F removes from README).

## 6. Copy-doctrine fix (decision: badges are re-viewable now)

Every "shown once / cannot be retrieved" claim becomes false the moment
Task A shipped — fixed here (SPA surface; README/docs are Task F):

- `badgePrint` modal (:714–719): **"Shown only once — print or write it
  down now. … It cannot be retrieved again."** →
  **"Keep it private — whoever holds this code can log in.** You can
  re-view this badge in your profile at any time; regenerating revokes it.
  Print it now if you need a physical copy." (Keeps the bearer-credential
  warning — that part of the doctrine is unchanged — and drops only the
  unrecoverability claim.)
- Register / member-create flows keep their "print it now" urgency (the
  *moment* is still the only guaranteed sighting for a brand-new account —
  the copy says "print it now", not "cannot be retrieved").
- Account badge section muted text (:246): unchanged (still true) + one
  sentence: "You can re-view your badge here at any time."
- `memberBadge` modal copy ("previous badge … was just revoked") unchanged —
  rotation semantics didn't change.

## 7. Members tab — staff "View" beside "QR badge"

- Per-member button row gains **[View]** (staff/admin, same manage-rule
  visibility the backend enforces): **`confirm()` first** — "View this
  member's badge? If it predates re-viewable badges, viewing replaces it
  and any printed copy stops working." Staff act on *someone else's*
  credential and the endpoint auto-migrates legacy badges, so pre-consent
  is the right side; the confirm is skipped for self-view (§3). (Smarter
  pre-checking would need `generation` in `UserOut` — flagged as a
  follow-up, not needed: confirm-once is cheap and honest.)
- `GET /api/members/{id}/qr-badge` → same envelope handling as §3 →
  `badgePrint = {title: "QR badge — " + m.name, subtitle: migrated-warning
  or "Re-viewable at any time", …}`; 404/409 states surface as toasts here
  (staff has the [QR badge] regenerate button right beside [View] as the
  healing action). Existing `showMemberBadge` (rotate) and
  `revokeMemberBadge` untouched.

## 8. Verification (no JS runner — established; none added)

- **`scripts/ui_check.py`** — `count()`-only additions, logged-out surface
  (all that's reachable): existing pins stay (`data-scan-capture="on"`,
  badge-field placeholder, "Scanner ready", "Log in", register copy, zero
  console errors on `/` and `/#/register` **with the new data props/methods
  present** — the real regression value: a typo in ~250 new lines of app.js
  breaks mount or logs, and these pins catch it). New copy pins: none
  possible for the profile (authenticated) — stated honestly. Parser
  untouched ⇒ `test_ui_check_parser.py` green.
- **`uv run pytest` green** (no backend change) + `node --check
  static/app.js`.
- **Manual checklist** (rides in the PR, folded into Task F's hardware
  doc): tabs render + flagged default-to-Security; email change → note +
  second tab/browser lands on login (revocation visible); phone
  set/normalise/clear; [Show my QR] twice → identical payload, print
  yields the card alone; legacy account (simulated: direct-DB gen-0 badge)
  → migrated warning + old payload 401 on scan; no_key/key_mismatch panels
  (simulate via env); checkout happy (cart empties, receipt modal, history
  gains it); clamp receipt (stock 3 + qty 5 → "only 3 … charged for 3",
  total reflects applied); history frozen across a staff price edit;
  [View] member badge confirm + modal; copy sweep — `grep -ri "only once\|
  cannot be retrieved" static/` returns nothing.

## 9. Data/methods delta (review surface)

**data:** `profileTab`, `emailForm/emailError/emailOk`, `phoneForm/
phoneError/phoneOk`, `badgeState`, `myOrders/ordersOffset/ordersHasMore/
expandedOrder`, `lastReceipt`. **Removed:** `badgeSvg`, `badgePayload`,
`pwOk` stays. **computed:** none new (the `purchase`/`purchaseTotalCents`
Task-5 wiring is reused). **methods:** `selectProfileTab`,
`changeEmail`, `changePhone`, `clearPhone` (submits `""`), `showMyBadge`,
`viewMemberBadge`, `loadMyOrders`, `toggleOrder`, `checkout`;
**deleted:** `generateMyBadge`'s inline-svg path reworked (badge shown via
`badgePrint` after rotate too — one modal for all sightings),
`printAccountBadge`, `requestPurchase`. **watch:** `badgePrint` watcher
unchanged (print class); `_closeOverlays` drops the two removed fields.
**index.html:** nav label, h1, tab bar, five tab blocks (three are moved
markup), email/phone forms, badge-state panels, members [View] button,
receipt modal, checkout button text/handler, copy fixes (§6).
**style.css:** `.clamp-chip`, `.receipt` line styles, `.badge-state` panel
— reusing `.admin-tabs`, `.status-chip`, `.modal` tokens; ~30 lines.

## 10. Risks & open questions

1. **Staff [View] confirm is a blanket confirm** (can't know legacy-ness
   client-side without `generation` in `UserOut`): acceptable friction
   (one click, only in the Members tab); exposing `badge_generation` on
   `UserOut` is a backend one-liner if the coordinator wants the confirm
   limited to legacy accounts — flagged, not assumed.
2. **Moved markup risk**: three existing account blocks relocate into tabs;
   the diff must be reviewed as *moves* (whitespace-normalised diff
   recommended in the PR) so logic changes stay visible.
3. **Other-tab revocation UX**: after change-email, other tabs die on their
   next 401 → the global handler routes them to login (existing machinery);
   the note only appears in the tab that made the change — by design,
   documented.
4. **Receipt modal vs logoff overlay stacking**: `lastReceipt` is a
   `.modal-backdrop` — `_modalOpen()` (scan discipline) must include it, and
   `_closeOverlays` must null it (two one-line additions; listed here
   because forgetting them reopens the Task-5 stranding class).
5. **Checkout race** (cart emptied elsewhere between confirm and POST):
   backend 400 → toast; no state corruption (the response is the truth).
6. **No pagination for receipts' lines** (a receipt renders whole) and
   history pagination is offset-based — fine at shop scale, matches
   `list_items`.
7. **Open question — [Load more] vs infinite scroll for history**: proposed
   [Load more] (the sentinel observer is catalogue-specific; scroll
   pagination inside a tab is fiddly). Flip is contained to `loadMyOrders`.
