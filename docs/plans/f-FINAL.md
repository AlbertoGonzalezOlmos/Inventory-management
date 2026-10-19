# Task F FINAL spec — Closing pass (docs, runbook, smoke, checklist)

## Docs

- `README.md`:
  - env table: add `HCRM_BADGE_SECRET` row (and confirm `HCRM_LOGOFF_GRACE_SECONDS`
    already present).
  - a short **security trade-off** paragraph: badges are now re-viewable because
    they are HMAC-derived from a server-side secret — a leaked DB no longer
    yields plaintext payloads but a *full server compromise* (DB + key) can now
    derive them. State it plainly.
  - feature bullets: member profile tab (phone/email/password/QR/purchase
    history), checkout, auto-detect QR login.
  - tracked-issues: remove the "checkout is a non-goal" row (checkout now
    exists); keep any successor non-goals (refunds/voids) if listed.
- `docs/opticon-m10.md`: finish the §9 badge section — a **key-rotation runbook**
  (rotate `HCRM_BADGE_SECRET` → existing derived badges answer `key_mismatch` on
  re-view and are healed by rotation; mass-rotate = staff regenerate each, or a
  documented bulk path) + a sentence in §10 (kiosk flows) noting the profile
  re-view and auto-detect login.
- `NOTES.md`: a short paragraph recording the profile/checkout/auto-detect work.

## Migrations

- Verify (and fix if needed) the exact-set migration test lists exactly the
  columns that now exist: `must_change_password`, `barcode`, `qr_badge_hash`,
  `qr_badge_generation`, `phone`, `logoff_deadline` (+ the index). The pinning
  test is the check.

## Smoke (`scripts/smoke_test.py`)

- Extend the kiosk loop with the new public flows: register → badge re-view
  (identical payload) → change-phone → change-email → checkout (add to cart,
  order, clamp) → `GET /api/orders/mine` history. Keep degraded-mode skips
  correct.

## Hardware checklist addendum (`docs/plans/task-f-checklist.md`)

- Track A (HID): profile re-view badge, checkout receipt (applied/clamped),
  purchase history, email change → other-session-revoked, auto-detect login
  (focus in email/password/nowhere), staff "View QR".
- Track B (USB-COM) remains [BLOCKED] with the same pre-flight.

## Gates

`uv run pytest` green; `scripts/ui_check.py --self-host` (if a browser is
available); `node --check static/app.js`; README false-claims test green.
