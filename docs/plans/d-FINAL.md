# Task D FINAL spec — SPA profile tab

Merged from `d-proposal-{glm,qwen}.md` (kimi unavailable).

## Structure

- Keep the `#/account` route (the `must_change_password` redirect depends on it);
  retitle the nav/tab to **"My profile"**. Convert the account section into
  **tabs**: *Profile* (identity/contact), *Security*, *My QR badge*, *Purchase
  history*, *My scan requests* (existing). A simple `profileTab` string on the
  component; no route churn.

## Tabs

- **Profile**: name (read-only or editable per current rules), email + phone edit
  forms → `POST /api/auth/change-email {new_email, current_password}` and
  `POST /api/auth/change-phone {phone}`. On email change success show a
  "you were signed out on other devices" toast (revocation). Phone `""` clears.
- **Security**: the existing change-password form.
- **My QR badge**: a "Show my QR" button → `GET /api/auth/qr-badge` → the
  existing `badgePrint` modal. Handle every envelope: `ok` → show; `migrated` →
  show + "your previous printed badge stopped working" warning; `none` → "you
  have no badge yet" + generate action; `no_key` / `key_mismatch` → guidance
  toast (admin must restore/rotate the badge key). Use `api(..., {raw:true})` to
  branch on the `status` discriminator.
- **Purchase history**: `GET /api/orders/mine` → newest-first list with
  expandable receipts (per line: name, qty, price, `applied_qty`, `clamped`
  marker). Empty state. A single "Load more"/pagination toggle contained to one
  method.
- **My scan requests**: existing `myRequests` block, kept.

## Checkout

- Replace the "Request items" placeholder with **"Complete purchase"** →
  `POST /api/orders` (raw mode). Show the result: total, per-line
  `applied_qty`/`clamped` ("3 of 5 in stock"), and `skipped` orphans; then reload
  the cart (now empty). Disable the button when the cart is empty. A backend
  400 (empty-cart race) → toast, no corruption.

## Copy-doctrine fix

- Replace every "shown only once / cannot be retrieved again" string (modal
  warning, its HTML comment, app.js construction sites) with re-viewable truth:
  "Anyone holding this code can log in as you — keep it private." Keep the
  "print it now" urgency on the creation/register path (without the false
  unrecoverability claim). A grep sweep for "only once|cannot be retrieved" is a
  checklist item.

## Staff "View QR"

- Members tab: a "View" action per member → staff `GET /api/members/{id}/qr-badge`
  → the same envelope handler + modal. The existing rotate-flow `memberBadge`
  modal is left untouched.

## Verification

- `ui_check.py` gains no new pins (the profile is authenticated and unreachable
  to the one-shot DOM dump); keep existing pins + zero-console green.
- `node --check static/app.js`; `uv run pytest` green (no backend change).
- Manual checklist: all badge envelopes (incl. simulated gen-0 migration),
  email-revocation cross-tab check, checkout variants (clamp receipt, orphan
  skip), frozen history across a price edit, print isolation.
- The last checkout result modal must join `_modalOpen()`/`_closeOverlays()` so
  it can't strand over a logout/switch (the Task-5 class of bug).
