# Coordinator synthesis #2 — Member profile, re-viewable QR, auto-detect login

Merged from `plan2-{glm,kimi,qwen}.md` + `plan2-review-{glm,kimi,qwen}.md`.

## Coordinator decisions (resolving the flagged choices)

1. **Badge re-viewability = HMAC-derived badges** (kimi's design, all three
   converge): the payload is *derived*, not stored —
   `HCRM1:` + `b64(HMAC(badge_secret, "badge:{user_id}:{generation}")[:32])`.
   - Master secret: `HCRM_BADGE_SECRET` env or auto-generated `data/badge.key`
     (0600, git-ignored). Back it up with the DB; documented rotation runbook.
   - `qr_badge_hash` stays the login/verify authority (untouched); re-view
     recomputes on demand (`GET /api/auth/qr-badge`, repeatable).
   - Rotation = `generation++` (self-rotate + staff regenerate).
   - Legacy random badges (Task 2) keep verifying by hash; auto-migrate on
     first view, or structured 409 if the key is missing/rotated.
   - **Trade-off accepted by owner's request**: a full server compromise can
     now derive badges (was: DB-leak-safe). Documented, not hidden.
2. **Purchase history = explicit Checkout.** One atomic action snapshots cart
   lines into `purchases`/`purchase_lines` (denormalised name/price snapshot,
   weak-ref `item_id`), decrements stock with **clamp-and-report**
   (`stock = MAX(0, stock - qty)`, per-line `requested`/`applied`/`clamped`),
   and clears the cart. Cart adds stay stock-neutral.
3. **Phone**: `users.phone` nullable column (migration), optional `+`, 6–15
   digits, `""`-clears. `POST /api/auth/change-phone` (session-only, no
   password). Staff `MemberPatch.phone`.
4. **Email**: `POST /api/auth/change-email`, current password required,
   uniqueness + `IntegrityError`→409, **revokes other sessions** (change-password
   parity). Staff may never edit email.
5. **Profile**: keep the `#/account` route, retitle "My profile", tabbed layout
   (identity/email/phone, security/password, My QR badge with re-view +
   isolated print, purchase history, My scan requests). No route churn (the
   `must_change_password` redirect keeps working).
6. **Login auto-detect**: the global capture already routes logged-out badge
   scans to login. Remaining work = copy, demote the badge field to a collapsed
   manual fallback, a busy double-fire guard, and harness/checklist cases.

## Prioritized plan (order of execution)

- **A** — Derived badges + re-view (backend): key bootstrap, derivation in
  `app/qrbadge.py`, `generation` migration, flush-then-derive issuance,
  `GET /api/auth/qr-badge` + staff `GET /api/members/{id}/qr-badge`, legacy
  auto-migration, backup + healthz.
- **B** — Phone + email change (backend): `users.phone` migration, normaliser,
  `change-email` / `change-phone`, staff patch, `UserOut` phone.
- **C** — Purchases (backend): models, checkout endpoint, history reads,
  member-delete cleanup, clamp/concurrency/snapshot tests.
- **D** — SPA profile tab (+ checkout button, copy-doctrine fix: "shown only
  once" → "keep it private"; Members tab gains "View QR").
- **E** — Auto-detect login UX (small): copy, collapsed fallback, busy guard,
  harness cases.
- **F** — Closing: README (env row + security trade-off paragraph),
  opticon-m10 §9/§10, NOTES, migrations exact-set, smoke kiosk-loop extension,
  hardware checklist.

Each task lands as its own PR (propose → cross-review → decide → implement →
review×2 → consensus → branch/commit/PR/merge), same as the first feature.
