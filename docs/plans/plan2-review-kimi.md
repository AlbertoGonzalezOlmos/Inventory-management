# Plan 2 review (kimi-k3): profiles, re-viewable badges, auto-detect login

Reviews of `plan2-glm.md` and `plan2-qwen.md` vs my own `plan2-kimi.md`.
All three converge on the skeleton: derived/encrypted badge recoverability
with the hash kept as login authority, an explicit-checkout purchase model
with snapshot lines and weak-ref item ids, phone+email profile endpoints,
a profile tab on the SPA, and "auto-detect is mostly shipped — UX +
verification". The real adjudications are the three tensions the round
asked about.

---

## 1. The badge mechanism: derivation (me/qwen) vs encryption (glm)

**Derivation wins, decisively — and qwen's version is the best-engineered.**

GLM's encrypt-at-rest design is honest and well-reasoned (key outside the
DB, hash stays the login authority, graceful 409 on key loss), but it pays
for the same exposure class as derivation with **hand-rolled AEAD**
(HMAC-CTR keystream + encrypt-then-MAC, nonce and key-rotation machinery).
"Standard construction" is exactly what people say before shipping a
crypto bug; the repo is stdlib-crypto by doctrine and adding
`cryptography` breaks the offline culture. Derivation needs *one HMAC
call and no per-record state* to achieve the identical exposure profile
(DB + key ⇒ badges). GLM's own fallback note ("Fernet is a drop-in swap
if a dep is ever acceptable") concedes the point.

**Adopt from qwen — four refinements my derivation design lacked:**

1. **Verify-on-re-view.** `GET /api/auth/qr-badge` re-derives *and checks
   the derived hash against the stored hash* before returning. Without
   this (my design), a lost/swapped keyfile silently shows the member a
   **badge that does not work** — the worst possible failure mode for a
   re-view feature. With it, key mismatch becomes a clean structured 409
   (`reason: "no-key"|"key-mismatch"`), and logins are provably unaffected
   (the hash needs no key). This is the single most important delta of the
   round.
2. **Derive from `user_id`, never email** — so Task B's email change is
   badge-safe *by construction*. Qwen states the consequence and pins it
   with a test; my design used user_id too, but without calling out why it
   is load-bearing.
3. **Legacy = `version NULL`, structured 409, explicit rotate** — qwen's
   `GET` answers `{re_viewable: false, reason: "legacy"}` and the UI
   offers rotation. My "auto-migrate on first view" silently kills the
   printed card *as a side effect of peeking* — the exact failure GLM
   rejected "view = regenerate" for. Qwen's is honest; adopt (bulk
   re-issue of legacy badges: no, all three agree).
4. **Operational key handling**: keyfile `0600` in the data dir with an
   env override, startup announcement (the dev-admin loud pattern),
   **`backup_db.sh` must copy `badge.key` and docs must say backups are
   now credential-bearing** (I missed this entirely — the key lives beside
   the DB by default, so "someone copied `data/`" now includes the key),
   `healthz` reports `badge_derivation` (the `insecure_dev_admin`
   loud-visibility precedent), and a mass-rotation runbook for key
   compromise.

**Keep from mine:** single-transaction issuance — `add → session.flush()`
(assigns `user.id`, no commit) → derive + set hash → commit. Qwen's
two-phase issuance (commit user, then a second commit for the badge) has a
badgeless-account failure window it heals via rotation; the flush makes
the window nonexistent (a failed commit rolls the id back too). The
IntegrityError catch just has to wrap the flush as well as the commit.

## 2. Checkout stock handling: clamp (me/qwen) vs reject (glm)

**Clamp-and-report wins on house consistency.** GLM rejects the whole
checkout with a 409 naming the short item (BEGIN-IMMEDIATE guarded),
arguing a clamp "silently under-delivers". But qwen's clamp is not silent:
per-line `requested`/`applied`/`clamped` flags — **exactly the shipped
`stock-adjust` semantics** ("a sale beyond available stock is reported,
never silently dropped", `StockAdjustOut.clamped`). Rejecting also clashes
with decision 6's warn-never-clamp cart: the shop lets a member build an
oversold cart with warnings, then refuses the sale — a worse UX than
selling what exists and reporting the clamp. And qwen's implementation
point is right: a SQL-side `stock = MAX(0, stock - qty)` needs **no
BEGIN-IMMEDIATE ritual** (the guard race GLM's pattern exists for does not
exist when the clamp lives in the statement) — the cart-upsert cap
precedent, one transaction, read-back only for the report.

GLM's reject is recorded as the coordinator-flippable alternative (if
"purchase" must mean "fully fulfilled", reject is the honest shape) — but
the default should match the doctrine the repo already shipped.

## 3. Email change: revoke other sessions (qwen) vs keep (me/glm)

**Adopt qwen's revoke-others.** GLM and I argued the current-password
gate already re-authenticates and the credential is unchanged — true, but
qwen's uniformity with `change-password` (verify current password + revoke
every *other* session, current survives) is the stronger, more
documentable posture: all identity changes sign out other devices. The UX
cost is one re-login on the member's other devices; the security gain is
real against the "attacker has a second session AND the password somehow"
residual. Flagged as coordinator-flippable; GLM's/my position recorded.

Also adopt from GLM (no conflict): **staff may edit member *phone* via
`MemberPatch` but never member *email*** — staff changing a member's login
identifier without the member present is a takeover vector. And from qwen:
separate `change-email`/`change-phone` endpoints (house style — GLM's
single `PATCH /profile` with conditional password is clever but unlike
`change-password`'s shape), phone as **nullable with `""`-clears**
(`ItemPatch.barcode` precedent) over GLM's `""`-default column.

## 4. Everything else worth adopting

- **Qwen — copy-doctrine fix**: the register/member-create modals'
  "Shown only once — it cannot be retrieved again" warning becomes *false*
  the day derived badges ship. Replace with "anyone holding this code can
  log in — keep it private" (urgency stays, lie goes). I missed this; it
  is mandatory, not cosmetic.
- **Qwen — profile stays on `#/account`, retitled "My profile"** (no route
  churn, the `must_change_password` redirect and exempt-path logic work
  untouched) over my `#/profile` + alias. Adopt.
- **Qwen — staff `GET /api/members/{id}/qr-badge`** with the
  `_get_manageable_member` rule (Members tab gets *View* next to the
  existing *Regenerate*) — the counter's "reprint a member's card" path,
  matching the owner's item (2).
- **Qwen — `badgeLogin` busy short-circuit** (no double-fire while a login
  is in flight) and badge failures surfacing in **`loginError` AND a
  toast** (the global-capture path has no form context). Small, correct,
  adopt for Task E.
- **Qwen — smoke-loop extension** (re-view GET, checkout, history) and
  the degraded-mode behaviour (creation 503 ⇒ checkout legs skip, like
  the Task-6 pattern).
- **GLM — explicit degradation semantics table** (key missing/MAC fail ⇒
  login unaffected, re-view answers a clear 409) — the right shape for the
  ops docs; folded into qwen's structured-409 design.
- **Both — ui_check scope honesty**: the profile tab is authenticated
  surface, unreachable by the interaction-free harness; its verification
  is backend tests + manual checklist + zero-console-errors on the
  logged-out surface. ui_check pins stay on the login-view copy.

## 5. Disagreement scoreboard

| Point | Me | GLM | Qwen | Verdict |
|---|---|---|---|---|
| Badge mechanism | derive | encrypt | derive + verify-on-view | **qwen** (verify-on-view is decisive) |
| Legacy badge on view | auto-migrate | — | structured 409 + rotate | **qwen** (no silent card-kill) |
| Issuance | flush (1 txn) | — | two-phase | **mine** (no failure window) |
| Checkout stock | clamp+report | reject 409 | clamp+report | **clamp** (stock-adjust doctrine) |
| Email-change sessions | keep | keep | revoke others | **qwen** (change-password parity) |
| Profile route | `#/profile`+alias | tabbed account | retitle `#/account` | **qwen** (no churn) |
| Staff email edit | (not addressed) | forbidden | (self-service only) | **GLM** (adopt) |
| Legacy mass re-issue | — | — | no | **no** (unanimous) |

## 6. Final recommended shape

**Base: qwen's plan**, with these swaps and additions:
1. Badge mechanism: derivation `"hcrm-badge-v1:<user_id>:<version>"`,
   `version NULL` = legacy, **verify-on-re-view**, structured 409 reasons
   (`legacy`/`no-key`/`key-mismatch`), keyfile 0600 + env override +
   startup announcement + `healthz.badge_derivation` + **`backup_db.sh`
   copies the key + "backups are credential-bearing" docs** + mass-rotation
   runbook. **Issuance via my flush()** (single transaction; wrap the
   IntegrityError catch around flush+commit).
2. Checkout: clamp-and-report, SQL-side `MAX(0, …)`, per-line
   `requested/applied/clamped`, no BEGIN-IMMEDIATE (documented next to the
   upsert-cap precedent), empty cart → 409, snapshots + weak-ref
   `item_id`, `delete_member` cleanup, second checkout → 409.
3. Profile endpoints: `change-email` (password-gated, 409 conflict,
   **revoke other sessions**), `change-phone` (session-only, nullable,
   `""` clears, flagged as flippable), `MemberPatch.phone` allowed /
   **staff email edit forbidden** (GLM).
4. SPA: `#/account` retitled "My profile" (identity / security /
   QR badge with *Show my QR* + structured-legacy state / purchase
   history / scan requests), "Complete purchase" with clamp-report
   surfacing, **copy-doctrine fix** in all badge modals, Members tab
   *View* + *Regenerate*.
5. Login auto-detect: copy change + collapsed manual-entry `<details>`
   fallback, `badgeLogin` busy guard, loginError+toast on capture-path
   failures, harness/manual-checklist cases (focus in email / password /
   nowhere), no backend change.
6. Order: A badges → B phone/email → C purchases → D SPA profile →
   E auto-detect UX → F docs/migrations/checklist. (Qwen's order; badges
   first because D's QR section and B's badge-safety pin depend on it.)

## 7. Changes to my own plan

1. Add **verify-on-re-view** (derived hash == stored hash; wrong key →
   structured 409, never a wrong badge) — my design's worst failure mode.
2. Legacy handling: auto-migrate-on-view → **structured 409 `legacy` +
   explicit rotate**.
3. Email change: keep-sessions → **revoke other sessions** (with the
   flippable flag).
4. Profile: `#/profile` + alias → **retitle `#/account`**.
5. `badge_generation` default 0 → **`version` NULL = legacy**.
6. Adopt qwen's ops package: keyfile 0600 + announcement + healthz flag +
   **backup_db.sh key copy** + credential-bearing-backup docs +
   mass-rotation runbook.
7. Adopt GLM's staff-email-edit ban and degradation table.
8. Adopt the copy-doctrine fix, `badgeLogin` busy guard, loginError+toast,
   staff *View* button, smoke extensions.
9. Keep from mine: derivation core, flush-based issuance, clamp checkout,
   separate endpoints, the accepted-edge note (43-char scanner-speed
   password).
