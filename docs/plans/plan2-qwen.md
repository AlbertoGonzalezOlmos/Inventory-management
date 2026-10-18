# Plan 2 (qwen3.8-max) — Member profile, re-viewable QR badges, purchase history, auto-detect login

New feature round after the shipped M-10 scanner stack (Tasks 1–6). Three
owner asks: **(1)** a member profile tab (purchase history; change password /
email / phone; see own QR code), **(2)** QR linked to the account, admin
regeneration, and **re-viewable** by the user, **(3)** login auto-detect —
scan a badge anywhere on the login screen, no click into a field first (and
an own-badge scan while logged in still logs off, per the shipped flows).

Grounded in the current tree (verified this pass): `User` has no phone;
`qr_badge_hash` is SHA-256-only and payloads are shown once (Task 2);
`cart_items` is a live cart with **no** order/history concept and
`requestPurchase()` is a placeholder toast (README tracked issue "Real
checkout/order endpoint"); the SPA already has the global HID capture with
observe-only mode on editable fields (Task 5 fix: first char buffered, shape
gate, Enter suppression, field restore), `handleBadgeScan` **already routes
logged-out badge scans to `badgeLogin` from anywhere on the page**, and the
logged-in same/different-badge flows (30 s countdown, save-then-switch) are
shipped; `change-password` establishes the "verify current password +
revoke every OTHER session" pattern (`auth.py:305`); `""`-clears-the-field
precedent exists (`ItemPatch.barcode`); `scripts/backup_db.sh` checkpoints
the WAL and copies the DB (no key material today); COLUMN_MIGRATIONS is
append-only and exact-set-pinned; seeds construct `User` directly.

---

## 0. The three design tensions, resolved up front

### T1 — Re-viewable badge vs hash-only doctrine → **deterministic derivation from a server key**

"View my QR again and again" *requires* recoverable badge material somewhere;
hash-only storage is mathematically incapable of it. The question is only
*what* becomes recoverable and *how*. Chosen mechanism:

- **`payload = "HCRM1:" + urlsafe_b64(HMAC-SHA256(server_key, "hcrm-badge-v1:<user_id>:<version>"))[:43]`**
  — a PRF over immutable inputs (user_id never changes; **email change is
  therefore badge-safe** — a consequence worth stating because deriving from
  email would have broken it). Format is byte-identical to today's
  `token_urlsafe(32)` output, so `parse_badge_payload`, the bridge, the SPA
  regexes and every existing test accept derived badges unchanged.
- New column `users.qr_badge_version: int | None` (NULL = legacy random
  badge, not derivable). Rotation/issuance sets `version = (version or 0)+1`,
  derives, stores the **hash** (login authority stays `qr_badge_hash` —
  unchanged, indexed, uniform-401 lookup; derivation is a *display-side*
  secret). `GET` re-derives and **verifies derived-hash == stored-hash**
  before returning: a swapped/corrupt keyfile yields a clean 503-style
  "cannot re-view — rotate to re-issue", never a wrong badge.
- **Key management**: 32 random bytes, auto-generated on first boot into
  `<data dir>/badge.key`, chmod 0600, announced in the startup log (the
  dev-admin announcement pattern); `HCRM_BADGE_KEY` (hex) env override for
  deployments that manage secrets externally. `/api/healthz` reports
  `badge_derivation: true|false` (loud-visibility doctrine). Losing the key
  **degrades gracefully**: logins keep working (hashes), re-viewing stops,
  rotation re-issues derivable badges.
- Issuance order changes: register/create_member currently derive-before-add;
  derivation needs the committed `user.id`, so issuance becomes **two-phase**
  (commit user → derive+store hash → commit), exactly the `_issue_token`
  second-commit precedent: if phase 2 fails the account exists badgeless and
  self-rotation (now in the profile) recovers it. Existing accounts
  (all Task-2 badges) stay `version=NULL` until first rotation — profile UI
  says "regenerate to make your badge re-viewable"; no silent mass re-issue
  (that would kill printed cards).
- **Trade-off, stated plainly** (goes into README security notes + §9 docs):
  today a leaked database yields nothing printable. After this change, a
  leaked database **plus** the keyfile/env yields every current badge —
  the two must be protected together (the keyfile lives beside the DB by
  default, so "someone copied `data/`" now includes the key: `backup_db.sh`
  must copy `badge.key` too, and the docs must say backups are now
  credential-bearing). Rejected alternative: **encrypted-payload column**
  (store AES-GCM-ish ciphertext) — same exposure class, but forces
  hand-rolled AEAD (the repo is stdlib-crypto by doctrine: "no external
  crypto deps"; adding `cryptography` for one column breaks the offline /
  minimal-deps culture) plus nonce and key-rotation machinery. Derivation
  is one HMAC call with no per-record state. Both designs are equally
  unrecoverable-without-the-key; derivation is the smaller, safer build.

### T2 — "Purchase history" needs a definition → **explicit checkout creates an immutable order**

A cart is intent; a **purchase** is a completed checkout. Minimal model:

- `purchases` (id, user_id idx, total_cents, created_at) +
  `purchase_lines` (id, purchase_id idx, item_id **weak ref**, sku/name/
  price_cents **snapshots**, qty). Snapshots are mandatory: prices change
  and items get deleted — the Task-3 weak-ref lesson applied from day one
  (history must never depend on the catalogue staying intact).
- **Recorded when**: the member presses **Complete purchase** (the
  `requestPurchase()` placeholder becomes real): `POST /api/purchases`
  reads the server cart (authoritative — the client sends no lines),
  rejects an empty cart (409), and in **one transaction**: inserts the
  purchase + snapshot lines, decrements stock per line with a SQL-side
  `stock = MAX(0, stock - qty)` clamp (**race-free by construction — the
  clamp is in the statement, so no BEGIN-IMMEDIATE ritual is needed**, the
  same doctrine as the cart upsert's cap; per-line `clamped` flags are
  reported, never silently dropped), clears the cart, commits. Concurrent
  double-checkout: SQLite serialises writers; the second sees an empty cart
  → 409. Stock is *not* a veto (barebones shop; oversell is reported).
- Not a scan log (scans ≠ purchases), not stock-adjust reuse (that is
  back-office receiving/POS deltas, staff-gated; checkout is member-facing
  and creates the history record).

### T3 — Auto-detect login → **mostly shipped; the task is UX + verification + demoting the field**

The Task-5 global capture already routes logged-out badge scans to
`qr-login` from any focus state (observe-only mode in the email/password
fields: buffer, shape-gate on Enter, restore the field, suppress the form
submit). What the owner is actually asking for is that the UI *stop
instructing* "click here and scan": (a) retitle/demote the dedicated badge
field to a collapsed "enter a badge code manually" fallback (it stays —
accessibility, slow >50 ms emulators, and the deterministic path); (b) login
copy becomes "Scan your badge — no need to click anything"; (c) prove the
no-focus and focused-field paths with harness cases + the hardware
checklist; (d) logged-in behaviour ("or logs off the current user") is the
shipped countdown/switch — untouched. One real guard to add: while
`busy` (a login in flight), `handleBadgeScan` must not double-fire
(`drainScanQueue` already serialises; add a `busy` short-circuit in
`badgeLogin`).

---

## Ordered tasks (each a green-suite PR)

### Task A — Derived badges + re-view endpoints (backend, T1)

Files: `app/qrbadge.py` (key load/generate, `derive_badge_payload(user_id,
version)`, `new_badge_for` becomes derivation-based with the version bump;
legacy `secrets.token_urlsafe` path deleted — issuance is always derived),
`app/models.py` (`qr_badge_version`), `app/main.py` (COLUMN_MIGRATIONS
append + key bootstrap at startup + healthz `badge_derivation`),
`app/routers/auth.py` (register/create… two-phase issuance; **new
`GET /api/auth/qr-badge`** → `{payload, svg, version}` or structured 409
`{re_viewable: false, reason: "legacy"|"no-key"|"key-mismatch"}`; rotate/
revoke keep working), `app/routers/members.py` (**new
`GET /api/members/{id}/qr-badge`** for staff/admin with the
`_get_manageable_member` rule; POST rotate unchanged in shape, now derived),
`scripts/backup_db.sh` (copy `badge.key` alongside the DB + warn that
backups are credential-bearing), `tests/test_backup_and_run.py` (keyfile
copy), README security notes.
Tests (`tests/test_qr.py` + new `tests/test_badge_derivation.py`): derived
payload format/parses/43-chars; determinism (same user+version ⇒ same
payload; version bump ⇒ different); **hash remains the login authority**
(derived payload qr-logins; a payload derived with a WRONG key does not);
re-view returns the same payload as issuance (register → GET → identical);
rotate → old dies, GET returns the new one; legacy badge (version NULL,
simulated by direct DB write) → GET 409 `legacy`, rotate fixes it; keyfile
absent/env mismatch → GET structured failure, login still works, healthz
`badge_derivation:false`; keyfile perms 0600 + auto-generation announced;
register phase-2 failure leaves a badgeless account that self-rotation
heals (monkeypatched commit failure); `must_change_password` gate applies
to GET (not exempt); staff GET mirrors the manage rule (member→403,
staff→admin 403); email change (Task B) does NOT change the badge.
Migration: exact-set test += `("users","qr_badge_version")`; legacy-DB
column-lands-NULL pin.

### Task B — Phone + email change (backend, profile fields)

Files: `app/models.py` (`phone: str | None`), `app/main.py` (migration
append), `app/schemas.py` (`_normalised_phone_or_none` following the
`_normalized_gtin_or_none` precedent: strip spaces/hyphens/dots/parens,
optional leading `+`, then 3–15 digits (E.164 max) else 422; `""` clears —
the barcode precedent; `UserOut` += `phone`; `ChangeEmailIn {new_email,
password}`, `ChangePhoneIn {phone}`; `MemberIn`/`MemberPatch` += optional
phone), `app/routers/auth.py` (**`POST /api/auth/change-email`**: verify
current password (change-password doctrine — a hijacked session must not
silently move the login identity), normalise+lower, uniqueness pre-check +
IntegrityError→409 (register's pattern), **revoke every OTHER session**
(mirrors change-password), update the returned `UserOut`; **`POST /api/auth
/change-phone`**: session-only (contact info, not an identity credential —
flagged as an open question if the owner wants password confirmation)),
`app/routers/members.py` (create/patch accept phone).
Tests: phone normalisation table (`"+34 944 123 456"` → `+34944123456`;
`"000"` → 422; 16 digits → 422; `""`/null clearing semantics per schema);
change-email happy path (login with new email works, old 401s); wrong
password → 400; taken email → 409 (and the IntegrityError race path);
other sessions revoked, current survives; **badge unaffected** (T1
consequence pin); flagged accounts gated (403 — not exempt); change-phone
without password, invalid values 422, clear-to-empty; members create/patch
with phone; migration exact-set += `("users","phone")` + legacy NULL pin;
`/me` and members list carry phone (and no new leak: phone is the owner's
own / staff-visible like email).

### Task C — Purchases: checkout + history (backend, T2)

Files: `app/models.py` (`Purchase`, `PurchaseLine`), `app/schemas.py`
(`PurchaseLineOut` with snapshots + `clamped`, `PurchaseOut {id, total_cents,
created_at, lines}`, `PurchaseCreateOut` = PurchaseOut + per-line clamp
report), new `app/routers/purchases.py` (`POST /api/purchases` checkout —
any authenticated, gate applies; `GET /api/purchases?limit&offset` own
history; `GET /api/purchases/{id}` own-or-staff), `app/main.py`
(include_router), `app/routers/members.py` (`delete_member` cleans
purchases+lines — same-transaction doctrine; history dies with the account
like carts/requests), `static` untouched here.
Checkout transaction (one session commit): re-read cart lines (empty →
409 "cart is empty"); insert purchase + lines with snapshots; per line
`UPDATE items SET stock = MAX(0, stock - :qty), updated_at = :now WHERE id =
:item_id` then read back for the clamp report; delete the cart rows;
**no stock-adjust endpoint reuse, no raw-connection ritual** (SQL-side
clamp = no guard race — documented in the endpoint docstring next to the
upsert-cap precedent). Item deleted between add and checkout: the cart's
orphan-reap already prevents stale lines; a race lands the line snapshot
with the weak `item_id` dangling — snapshots keep the receipt intact
(Task-3 lesson).
Tests (`tests/test_purchases.py`): checkout snapshots price/name/sku
(staff price-PATCH afterwards doesn't move history); cart emptied; total
arithmetic; empty cart → 409; stock decremented exactly, **clamp at 0
reported per line** (`requested`/`applied`/`clamped`), oversell allowed and
reported; concurrent double-checkout → one purchase, second 409 (thread
barrier, the Task-1 pattern); weak-ref: delete an item after purchase →
history line intact with snapshot fields, `GET /{id}` survives; member
delete cleans purchases (direct DB); own-only access (B reading A's
purchase → 404, staff → 200); pagination; gate 403; unauth 401;
migration: new tables via create_all on legacy DB (extend the Task-6
legacy-tables test).

### Task D — SPA: the profile tab (frontend)

Files: `static/app.js`, `static/index.html`, `static/style.css`,
`scripts/ui_check.py` (pins).
Account view becomes **"My profile"** (route `#/account` kept — no new
route churn; nav label changes) with sections:
1. **Identity**: name/email/role/phone display; *Change email* form
   (new email + current password; on success update `USER_KEY`/state —
   the pattern `changePassword` already uses for `must_change_password`);
   *Change phone* form (normalisation feedback, clear button).
2. **Security**: existing password form unchanged; a note that other
   sessions are signed out on email/password change.
3. **QR badge**: **[Show my QR]** → `GET /api/auth/qr-badge` → the
   `badgePrint` modal (svg + payload + Print, print isolation already
   built); legacy/no-key states render the structured 409 as "Regenerate
   to make your badge re-viewable" + the existing rotate/revoke buttons.
   **Copy doctrine change**: the "Shown only once — cannot be retrieved"
   warning is replaced by "Anyone holding this code can log in — keep it
   private" (re-viewability is the owner's explicit ask; the warning must
   not lie). Same for the register/member-create one-shot modals — they
   keep their urgency ("print it now") but drop "cannot be retrieved"
   for derived badges.
4. **Purchase history**: `GET /api/purchases` list (date, total, line
   count) + expand-to-lines (snapshot name, unit price × qty, clamp
   marks); empty state explains checkout.
5. **Checkout**: purchase panel's `requestPurchase()` becomes **Complete
   purchase** → confirm → `POST /api/purchases` → toast + receipt view
   (the just-returned purchase) + `loadCart()`; clamp report surfaces
   ("2 line(s) clamped to available stock").
Members admin tab: the existing per-member **QR badge** button now also
offers *View* (staff GET) alongside *Regenerate* — the owner's "admin can
regenerate" is the existing POST, re-verified and relabeled.
ui_check pins (DOM/console only, per the harness's reach): profile copy
renders on `#/account`… (authenticated views are out of ui_check's reach —
pins limited to the login-view changes in Task E; profile verified by the
manual checklist + zero-console-errors on the logged-out surface).

### Task E — Auto-detect login UX (frontend, small — T3)

Files: `static/index.html`, `static/app.js`, `static/style.css`.
- Login copy: "Scan your badge — no need to click anything." The badge
  field collapses into a `<details>` "Enter a badge code manually"
  fallback (kept: slow emulators, accessibility, determinism); its
  `data-scan-sink` behaviour unchanged.
- `badgeLogin` gains the `busy` short-circuit (no double-fire while a
  login is in flight); logged-out badge failures surface in `loginError`
  (already) **and** a toast (the capture path has no form context).
- Capture harness (`/tmp`-style node harness promoted to a documented
  manual-checklist appendix — no JS runner is added): new cases — badge
  burst with focus in email field, in password field (restored, never
  submitted), and with no focus; all route to `badgeLogin`.
- Logged-in flows untouched (countdown/switch ship as-is — the owner's
  "or logs off the current user" is the own-badge countdown).

### Task F — Docs, migrations check, checklist (closing)

- `README.md`: feature bullets (profile, history/checkout, re-viewable
  badges, scan-to-login); env table += `HCRM_BADGE_KEY`; **security notes
  += the T1 trade-off paragraph (DB+key ⇒ printable badges; backups are
  credential-bearing)**; tracked issues: "Real checkout/order endpoint"
  row **removed** (shipped, minimal form) — the false-claims test stays
  green (it pins other phrases; verify).
- `docs/opticon-m10.md` §9: re-viewable badges, derivation, key ops
  (rotate-on-key-loss), §10 login copy update (no-click scanning).
- `NOTES.md` paragraph; `tests/test_migrations.py` exact-set += both new
  columns (conscious edits) + legacy-tables test += `purchases`/
  `purchase_lines`.
- Hardware checklist addendum (HID): no-click badge login from the login
  page with focus in email, in password, and nowhere; own-badge logoff
  unchanged; profile re-view prints; checkout decrements stock (verify via
  staff table view) with a clamped line; email change on the kiosk keeps
  the badge working.
- Gates: `uv run pytest`, `ui_check --self-host`, `smoke_test
  --allow-degraded` (extend the kiosk loop: re-view GET, checkout,
  history), `node --check`.

## Risks & open questions

1. **T1 exposure shift is irreversible policy** — once derived badges ship,
   "DB-only leak is safe" is no longer true. If the owner wants the old
   guarantee back, the lever is rotation (version bump) — document a
   mass-rotation runbook (staff endpoint loop) in §9.
2. **Keyfile in the default data dir** = convenience vs "one `cp -r data/`
   leaks everything". Env override + backup warning + 0600 are the
   mitigations; a deployment guide line is mandatory (Task F).
3. **Legacy badges are second-class until rotated** (not re-viewable):
   accepted (silent mass re-issue would invalidate printed cards); the UI
   explains. Open question: offer staff a bulk "re-issue all legacy" action?
   Recommend no for now.
4. **change-phone without password** — chosen for friction; flip is one
   dependency if the owner disagrees.
5. **Checkout is minimal**: no refunds/voids/status field, no receipts
   beyond the on-screen purchase, stock clamped-not-blocked. Deliberate;
   the tracked-issues table keeps "payments/refunds" as the successor item.
6. **Profile = the existing account route** (retitled), not a new `#/profile`
   — fewer routes, the gate/redirect logic (`must_change_password` →
   `#/account`) keeps working untouched. Open to coordinator preference.
7. **Two-phase issuance** adds a small failure window (account exists,
   badge commit failed) — bounded, self-healing via rotation, mirrors the
   accepted `_issue_token` precedent.
8. **Auto-detect is verification-heavy, code-light** — the risk is
   regression of shipped capture behaviour; the harness cases + checklist
   are the gate, and the badge field stays as the deterministic fallback.
