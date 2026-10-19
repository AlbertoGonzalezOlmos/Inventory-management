# Task D proposal (glm) — The SPA profile tab, checkout, and the copy-doctrine fix

Coordinator2 decisions 5–6 + Task D. Frontend only: retitle `#/account` to
**My profile** with a tabbed layout (no route churn — `must_change_password`
keeps redirecting to `#/account`, landing on the security tab), wire the
Task A/B/C backend (badge re-view envelopes, email/phone change, checkout +
history), add the staff badge **View**, and fix the now-false "shown only
once / cannot be retrieved" copy everywhere.

Grounding verified: the current account section (`index.html` ~222: user
line, change-password form, badge section with inline `badgeSvg` +
`generateMyBadge`/`printAccountBadge`/`revokeMyBadge`, My-scan-requests
table); `requestPurchase()` (app.js ~706) is the placeholder toast; the
`badgePrint` modal (index.html ~704–724, `.badge-backdrop` print
precedence, the "shown only once … cannot be retrieved again" warning);
`api(path, {raw, token})` returns `{status, data}` in raw mode with
`data.detail = {status, message}` for the envelopes (Task 5); `adminTab`
is the tab pattern to reuse; `USER_KEY` refresh after mutations is the
`changePassword` pattern; `loadMyRequests` exists and loads on the account
view.

---

## 1. The tabbed layout (keep `#/account`)

`index.html` — the account section becomes (reusing the existing
`.admin-tabs` styles verbatim — zero new CSS for the tabs):

```html
<section v-else-if="currentView === 'account'" class="auth-card profile-card">
  <h1>My profile</h1>
  <p class="muted">{{ user.name }} · role: {{ user.role }} · {{ user.email }}</p>
  <p v-if="user.must_change_password" class="error"> …existing warning… </p>
  <div class="admin-tabs">
    <button :class="{active: profileTab === 'identity'}"  @click="selectProfileTab('identity')">Email &amp; phone</button>
    <button :class="{active: profileTab === 'security'}"  @click="selectProfileTab('security')">Password</button>
    <button :class="{active: profileTab === 'badge'}"     @click="selectProfileTab('badge')">My QR badge</button>
    <button :class="{active: profileTab === 'purchases'}" @click="selectProfileTab('purchases')">Purchases</button>
    <button :class="{active: profileTab === 'requests'}"  @click="selectProfileTab('requests')">My scan requests</button>
  </div>
  <div v-if="profileTab === 'identity'">  …§2… </div>
  <div v-else-if="profileTab === 'security'">  …existing password form, unchanged… </div>
  <div v-else-if="profileTab === 'badge'">  …existing badge block + Show-my-QR (§3)… </div>
  <div v-else-if="profileTab === 'purchases'">  …§4… </div>
  <div v-else-if="profileTab === 'requests'">  …existing table, moved unchanged… </div>
</section>
```

`app.js` — `data().profileTab = "identity"`; a `selectProfileTab(tab)`
method (lazy-loads: `purchases` → `loadMyOrders()`, `requests` →
`loadMyRequests()`); and in the `currentView` watcher: **arriving on the
account view with `must_change_password` auto-selects the security tab**
(redirect lands one click closer to the mandatory change; also set it in
`afterLogin`'s flagged branch so the hash-redirect and the tab agree).

## 2. Identity/contact — email + phone

```html
<div v-if="profileTab === 'identity'">
  <h2>Email</h2>
  <p class="muted">Your email is your login identifier. Changing it signs
    out your other sessions.</p>
  <form @submit.prevent="submitEmailChange">
    <label>New email <input type="email" v-model="emailForm.new_email" required /></label>
    <label>Current password
      <input type="password" autocomplete="current-password"
             v-model="emailForm.current_password" required /></label>
    <p v-if="emailError" class="error">{{ emailError }}</p>
    <p v-if="emailOk" class="ok">Email updated — other sessions were signed out.</p>
    <button class="btn block" :disabled="busy">Change email</button>
  </form>
  <h2 style="margin-top:2rem">Phone</h2>
  <form @submit.prevent="submitPhoneChange">
    <label>Phone (optional "+", 6–15 digits; empty clears)
      <input type="tel" v-model="phoneForm.phone" placeholder="+34 600 123 456" /></label>
    <p v-if="phoneError" class="error">{{ phoneError }}</p>
    <button class="btn block" :disabled="busy">Save phone</button>
  </form>
</div>
```

Methods (throwing `api()` — errors carry `err.status`/`err.detail` from
Task 5, so no raw mode needed here):

- `submitEmailChange()`: `POST /auth/change-email`. **400** (wrong password
  OR no-op — the server's two 400 messages are distinct and user-ready) →
  `emailError = e.message`; **409** taken → `emailError = e.message`;
  success → update `this.user.email`, persist to `USER_KEY` (the
  `changePassword` refresh pattern), `emailOk = true`, toast
  "Email updated — other sessions were signed out" (the revocation is a
  coordinator decision the user must be told about, mirroring the
  password-change copy).
- `submitPhoneChange()`: `POST /auth/change-phone`; success →
  `this.user.phone = res.phone`, `USER_KEY` refresh, toast; 422 (shape) →
  `phoneError = e.message` (the server's normaliser message names the
  expected shape). Prefill `phoneForm.phone = user.phone` when the tab
  opens (a `profileTab` watcher one-liner) so the field edits what's there
  and `""` visibly clears.

## 3. My QR badge — re-view + all four envelopes

The badge tab keeps the existing generate/revoke/print block and gains one
button:

```html
<button v-if="user.has_qr_badge" class="btn" :disabled="busy" @click="showMyBadge">
  Show my QR
</button>
```

`showMyBadge()` uses **raw mode** (the envelope is structured 404/409):
`const r = await api("/auth/qr-badge", {raw: true})` → one shared handler:

```js
_handleBadgeView(r, title) {
  if (r.status === 200 && r.data) {
    // ok | migrated — the modal carries the SVG + payload + a context
    // warning; migrated gets the loud "old card died" line.
    this.badgePrint = {
      title, subtitle: this.user ? this.user.name : "",
      svg: r.data.svg, payload: r.data.payload,
      warning: r.data.migrated
        ? "Your PREVIOUS badge stopped working when you viewed this one — print the new card now."
        : "Anyone holding this code can log in as you — keep it private.",
    };
    return true;
  }
  const s = r.data && r.data.detail && r.data.detail.status;
  if (r.status === 404 || s === "none")
    this.showToast("No badge on this account yet — generate one below");
  else if (s === "no_key")
    this.showToast("Badge viewing is unavailable (no key) — logins are unaffected; ask staff to regenerate");
  else if (s === "key_mismatch")
    this.showToast("The badge key changed — regenerate your badge to re-enable viewing");
  else this.showToast("Could not view the badge");
  return false;
}
```

`badgePrint` gains the `warning` field (replacing the hardcoded modal
warning — see §6); the print-isolation machinery (`.badge-backdrop`,
`@media print`, `_printBadge`) is untouched. Rotation
(`generateMyBadge`) keeps working and shows the fresh badge through the
same modal with the creation-flavoured warning.

## 4. Purchase history

`loadMyOrders()`: `this.myOrders = await api("/orders/mine?limit=50")`.
`data().myOrders = []`, `expandedOrders = []` (ids).

```html
<div v-else-if="profileTab === 'purchases'">
  <h2>Purchase history</h2>
  <p class="muted" v-if="myOrders.length === 0">
    Nothing purchased yet — complete a purchase from the catalogue's list.
  </p>
  <div v-for="order in myOrders" :key="order.id" class="order-card">
    <div class="order-head" @click="toggleOrder(order.id)">
      <strong>#{{ order.id }}</strong> · {{ formatDate(order.created_at) }}
      · {{ order.lines.length }} item(s)
      <strong>{{ formatPrice(order.total_cents) }}</strong>
      <span class="status-chip" v-if="order.id === lastCheckout">new</span>
    </div>
    <table class="table" v-if="expandedOrders.includes(order.id)">
      <thead><tr><th>Item</th><th>Qty</th><th>Price</th><th>Total</th></tr></thead>
      <tbody>
        <tr v-for="line in order.lines" :key="line.item_id">
          <td>{{ line.name }}<br /><small class="muted">SKU {{ line.sku }}</small></td>
          <td>
            <template v-if="line.clamped">
              <span class="status-chip open" title="requested {{ line.qty }}, only {{ line.applied_qty }} in stock">
                {{ line.applied_qty }} of {{ line.qty }}
              </span>
            </template>
            <template v-else>{{ line.qty }}</template>
          </td>
          <td>{{ formatPrice(line.price_cents) }}</td>
          <td>{{ formatPrice(line.line_total_cents) }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</div>
```

Clamped lines show "**3 of 5**" with the existing `.status-chip` styles and
a title tooltip explaining the shortfall — the honest-receipt rendering of
clamp-not-veto.

## 5. Checkout — replacing the placeholder

The cart panel's button (`index.html` ~178):

```html
<button class="btn block" @click="completePurchase"
        :disabled="!purchase.length || busy">Complete purchase</button>
```

`app.js` — `completePurchase()` (raw mode: empty-cart is a structured 400
even though the disabled button prevents it):

```js
async completePurchase() {
  const r = await api("/orders", { method: "POST", raw: true });
  if (r.status === 201 && r.data) {
    this.cart = r.data.cart;                     // the emptied cart (one payload)
    const p = r.data.purchase;
    const notes = [];
    const clampedLines = p.lines.filter(l => l.clamped);
    if (clampedLines.length)
      notes.push(clampedLines.length + " item(s) partially delivered (requested more than stock)");
    if (r.data.skipped.length)
      notes.push(r.data.skipped.length + " scanned item(s) no longer exist and were skipped");
    this.showToast("Purchase complete — " + this.formatPrice(p.total_cents)
      + (notes.length ? " · " + notes.join(" · ") : ""));
    this.lastCheckout = p.id;                    // highlight in history
    await this.loadMyOrders();
    this.expandedOrders = [p.id];                // pre-expand the new receipt
    return;
  }
  if (r.status === 400) { this.showToast("Cart is empty"); return; }
  this.showToast("Checkout failed (" + r.status + ")");
}
```

(The single-toast limitation is why clamped/skipped collapse into one
combined note; the pre-expanded receipt carries the detail.) The
`lastCheckout`/`expandedOrders` state makes the just-completed order land
open in the history tab. `requestPurchase` is deleted.

## 6. The copy-doctrine fix (re-viewability makes the old copy a lie)

Every "shown only once / cannot be retrieved" string, frontend-wide:

| Location | Now | Becomes |
|---|---|---|
| `index.html` ~716–718 (badgePrint modal warning) | hardcoded "Shown only once — print or write it down now. … It cannot be retrieved again." | `{{ badgePrint.warning }}` — context-set (§3): re-view/rotate → "Anyone holding this code can log in as you — keep it private."; creation (register/member-create) → the same + "Print it now while you have it." (urgency, no false claim) |
| `index.html` ~704 (the HTML comment "shown EXACTLY ONCE … closing this modal is final") | false for re-views | "The payload is a credential — the modal never persists it (re-views re-fetch via GET); the `.badge-backdrop` print-precedence hook…" |
| `app.js` 185, ~411, ~1560 area (comments + the register/member-create modal construction) | "cannot be retrieved again" | updated comments; the modal `warning` per context; the register toast "print it and keep it safe" is already correct |
| Account badge section copy | "treat it like a password" | unchanged (already correct) |

**Docs are Task F** — only the frontend strings change here.

## 7. Members tab (staff): the View action

Next to the existing per-member "QR badge" (rotate) button:

```html
<button v-if="m.has_qr_badge" class="btn tiny ghost" @click="viewMemberBadge(m)">View</button>
```

`viewMemberBadge(member)`: `api("/members/" + member.id + "/qr-badge",
{raw: true})` → the **same `_handleBadgeView(r, "QR badge — " +
member.name)`** handler (all envelopes handled identically; the migrated
case warns that the member's previous card died — staff should reprint it
for them). The rotate flow's existing `memberBadge` modal is left as-is
(zero-churn); `View` uses the generic `badgePrint` modal.

## 8. `style.css`

Minimal, reusing existing tokens: `.profile-card { max-width: 46rem }`
(the tabbed card is wider than the auth card), `.order-card` /
`.order-head` (clickable, hover state), the clamped chip reuses
`.status-chip`. ~20 lines.

## 9. Verification

- **`node --check`** + the full pytest suite (backend untouched —
  contract-compatibility only).
- **`ui_check.py`**: the logged-out surface (all the harness can reach) is
  unchanged by this task — the existing pins (login copy, badge-field
  regression, `data-scan-capture`, zero console errors) stay green; **no
  new pins are possible** (the profile/checkout surfaces are
  authenticated), stated honestly as before. No parser changes.
- **Manual checklist** (the real gate): tab navigation (5 tabs, lazy
  loads); the flagged-account redirect lands on the **Password** tab;
  email change (happy + wrong-password 400 + taken 409 + the
  other-sessions-signed-out toast, verified from a second tab); phone
  set/normalise-feedback/clear; **Show my QR** in all reachable envelopes
  (ok — twice, identical; migrated — via a pre-derivation account, with
  the old-card warning and the old badge 401ing; none — after revoke);
  staff **View** (member ok + admin-target 403 behaviour); checkout
  (happy → cart cleared + receipt pre-expanded in history; clamped line →
  "3 of 5" chip + combined toast; skipped orphan → note; disabled when
  empty); history expansion + the price-snapshot display (edit a price,
  history unchanged); print isolation still yields the badge card alone
  from the re-view modal.

## 10. Files touched

| File | Change |
|---|---|
| `static/app.js` | `profileTab` + `selectProfileTab` (lazy loads + flagged→security); `emailForm`/`phoneForm` + the two submit methods; `_handleBadgeView` + `showMyBadge` + `viewMemberBadge`; `myOrders`/`expandedOrders`/`lastCheckout` + `loadMyOrders`/`toggleOrder`; `completePurchase` (replacing `requestPurchase`); the copy-doctrine updates |
| `static/index.html` | the account section → tabbed My profile; the identity/contact forms; the Show-my-QR + staff View buttons; the purchases list; the checkout button swap; `badgePrint.warning` |
| `static/style.css` | `.profile-card`, `.order-card`/`.order-head` (~20 lines, existing tokens) |
| *(untouched)* | the scan bus, the kiosk flows, the login view, `scripts/ui_check.py` (existing pins stay green), all backend |

## 11. Risks / notes

1. **Single-toast limitation** on checkout multi-events — mitigated by the
   combined note + the pre-expanded receipt; a multi-toast stack is
   out of scope (nice Task-F polish).
2. **`USER_KEY` refresh correctness** after email/phone change (the SPA's
   cached `user` must not go stale) — the `changePassword` pattern,
   testable only manually.
3. **The migrated warning must be unmissable** (a card just died) — the
   modal's warning slot, in the warn style, above the fold.
4. **Prefilled phone + `""`-clears**: the form shows the stored value;
   clearing to `""` and saving must visibly empty it (a `profileTab`
   watcher refills on tab entry — deliberate, so the field always reflects
   the server truth).
5. `expandedOrders` as an array (not a Set) — fine at 50 orders;
   `includes()` per render.
