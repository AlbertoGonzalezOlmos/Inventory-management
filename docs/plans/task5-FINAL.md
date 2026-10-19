# Task 5 FINAL spec — SPA: cart-on-API, global scan capture, all kiosk flows

Merged from `task5-proposal-{glm,kimi,qwen}.md` + reviews. Implementer follows THIS.
Frontend only: `static/app.js`, `static/index.html`, `static/style.css` (Vue 3
options API, vendored, no build step).

## 0. api() change

`api(path, {method, body, raw})`. When `raw: true`, return `{status, data}`
without throwing (for the scan path's status branching). Default path still
throws, but attach `err.status` and `err.detail` (parsed JSON body) so
structured 404s reach the flows.

## 1. Cart on the API (5a)

- Replace the localStorage purchase list (`PURCHASE_KEY`) with the server cart.
  Load via `GET /api/cart` on login (`loadCart()`) — **skipped** while
  `must_change_password` (avoid 403 spam). Mutations go through
  `/api/cart/items` / `PUT /api/cart` / `DELETE /api/cart`.
- **One-time legacy import**: if `PURCHASE_KEY` exists, `PUT /api/cart` with its
  lines once (on first cart load), then `localStorage.removeItem(PURCHASE_KEY)`.
- `flushCart()` awaits any in-flight cart mutation — this is the
  save-then-switch flush (every mutation is already a committed server write).
- Cart line `qty > item.stock` → render a stock warning (no client clamp).

## 2. Global scan capture (5b)

- One document-level `keydown` listener (capture phase), always on.
- **Focus rule**: never capture from editable targets (input/textarea) except a
  dedicated visible **scan field**. In non-sink editable fields, buffer without
  `preventDefault`, record the pre-burst value, and on a shape-gated Enter
  restore the field and route the scan (no character eating). The login badge
  field keeps its exact current behaviour.
- **Timing gate**: < 50 ms inter-key, ≥ 4 chars, Enter-terminated.
- **Shape gate (routing only — the server classifies)**: `HCRM1:` prefix OR a
  bare 43-char `[A-Za-z0-9_-]` token → badge; barcode-ish (digits / known
  symbology-ish) → barcode; else ignore (no server call).
- Scans are queued and drained one at a time. While any modal is open (except
  the logoff overlay) barcodes are dropped with a toast; badges always process.
  A scan-status pill and the visible scan field give a deterministic path.

## 3. handleScan() bus

- **Badge, logged out** → `doBadgeLogin(code)` (refactor the existing badge
  login for reuse).
- **Badge, logged in** → `POST /api/auth/qr-identify {token}`:
  - `same_user` → `POST /api/auth/logoff-schedule`; show the logoff overlay
    counting down from the returned `logoff_deadline` (server time; `Z`-safe
    parse). Cancel → `logoff-cancel` + close. "Log off now" → `logout()`.
    Expiry → `logout()`. Re-scan same badge → re-schedule (restart, one overlay
    instance, one interval). Different badge during countdown → cancel then
    switch.
  - different user → **switch**: `flushCart()` → `POST /api/auth/qr-login`
    (new session first) → best-effort `logoff-cancel` → `saveSession(new)` →
    revoke old token via `POST /api/auth/logout` with a `{token}` override →
    `loadCart()` + catalogue reload → `must_change_password` branch. Abort
    loudly (toast) if qr-login fails, leaving the old session intact.
  - flagged account (identify 403) → toast; the UI already routes flagged
    accounts to change-password. No mint-and-revoke fallback.
- **Barcode, logged in** → `POST /api/cart/scan {code, symbology}` with
  `raw: true`:
  - 200 → "added" toast (+ stock warning when `qty > item.stock`).
  - 422 → "misread — re-scan" toast.
  - 404 → intake modal: classification line from `analysis`, suggestion groups
    (`by_prefix`/`by_keyword`), live `GET /api/items/suggest` as the name is
    typed; role-aware submit:
    - **staff** → editable SKU prefilled from `analysis.gtin14 || code`, barcode
      pre-check, create via `POST /api/items` (409 → fetch-and-offer), add to
      cart, best-effort resolve matching open ScanRequests.
    - **member** → `POST /api/scan-requests` with 409-already/duplicate
      handling.
- **Barcode, logged out** → stage (localStorage-backed, cap 50, no secrets) +
  banner on the login card; after any login replay sequentially through
  `/api/cart/scan`; unknowns go to an `intakeQueue` (skip = drop) re-opening
  the intake modal one at a time.

## 4. One-shot badge print

- Shared `badgePrint` modal (reuse the member-badge markup pattern): `v-html`
  SVG + the payload shown once (never persisted to component state, cleared on
  close). Shown after `register()` and staff `create_member()` (both 201s now
  carry `qr_badge`).
- Add the repo's first `@media print` block isolating the badge card (today the
  print button prints the whole page).

## 5. Staff queue + account list

- Admin "Requests" tab: list ScanRequests, resolve with an `add_to_cart`
  checkbox (default on), dismiss. Account "My scan requests" (`/mine`) list.

## 6. Verification

- `scripts/ui_check.py`: extend via the existing `count()` mechanism ONLY —
  `data-scan-capture="on"` attribute pin (bus installed), badge-input
  regression pin, zero console errors. No parser changes, no interaction layer.
- `uv run pytest` stays green (backend unchanged except the api() surface is
  client-side).
- A precise manual M-10 hardware checklist (countdown cancel/restart/expiry,
  switch with cart restore, intake both roles, staging replay,
  typing-never-hijacked, close-tab-mid-countdown).

## Landing

Implement all of 5a → 5b → 5c in the working tree; keep the three logical
slices separable in the diff if practical (not required).
