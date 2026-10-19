# Plan 2 (kimi-k3): Member profiles, re-viewable QR badges, login auto-detect

Three owner requests, planned as one ordered program. Grounded against the
current tree (M-10 feature shipped, Tasks 1–6; 499 tests green).

The three items, and where they already stand:
1. **Member profile tab** — the Account view exists (password change, badge
   rotate, my-scan-requests) but has no purchase history, no email/phone
   editing, and no re-viewable QR.
2. **QR linking + re-viewability** — badges are auto-issued at creation and
   linked via `users.qr_badge_hash`, but the payload is shown **once** and is
   then unrecoverable (SHA-256-only storage). Admin regeneration already
   exists (`POST /api/members/{id}/qr-badge`). The re-view is the real work.
3. **Login auto-detect** — **~80 % already shipped**: the global HID capture
   runs on the login view, `handleBadgeScan` routes logged-out badge scans
   straight to `badgeLogin()` (no click needed), a scan landing in the
   focused email/password field is buffered and **restored** before routing,
   and logged-in scans already drive logoff (30 s countdown) / switch. What
   remains is UX (the dedicated field must become an optional affordance,
   not a requirement) and verification.

---

## 0. Design tensions — decisions

### T1 — "See QR again and again" vs hash-only storage

The doctrine ("a leaked database must not yield printable badges") and
re-viewability are fundamentally in tension: a re-viewable badge must be
*recoverable* server-side. Two honest options:

**Chosen: derived badges (stateless, HMAC).** A badge payload becomes a
deterministic function of a server secret:

```
payload(user, gen) = "HCRM1:" + urlsafe_b64(HMAC_SHA256(badge_secret,
                     f"badge:{user.id}:{gen}")[:32])   # 43 chars, same shape
```

- **Storage**: `users.qr_badge_hash` keeps working **exactly as today** (the
  SHA-256 of the payload — the login lookup, `qr-identify`, and all Task 2
  tests are untouched). New columns: `users.badge_generation INTEGER NOT NULL
  DEFAULT 0` (0 = legacy random badge, ≥ 1 = derived). One appended
  `COLUMN_MIGRATIONS` entry.
- **Re-view**: `GET /api/auth/qr-badge` recomputes the payload from
  (secret, id, generation) and renders the SVG — works forever, costs no
  storage. Legacy badges (generation 0) **auto-migrate on first view**:
  rotate to generation 1 and show the new badge (the old printed one dies —
  surfaced in the UI).
- **Rotation** (own `POST /api/auth/qr-badge`, admin
  `POST /api/members/{id}/qr-badge`): `generation += 1` → new payload, new
  hash, old badge dies — same external contract as today.
- **Creation**: `register`/`create_member` currently call `new_badge_for`
  *before* `session.add`. Derivation needs `user.id`, so the flow becomes
  `add → session.flush() (assigns id, no commit) → derive gen-1 payload,
  set hash → commit`. The generate-before-commit invariant holds (flush is
  not a commit; a raced 409 rolls back everything).
- **Legacy verification needs no dual path**: old random badges still match
  by stored hash; anything newly issued/rotated is derived. Nothing else
  changes in `qr-login`.
- **The secret**: `HCRM_BADGE_SECRET` env, else an auto-generated 32-byte
  file `$HCRM_DATA_DIR/badge_secret` created `0600` on first boot (same
  trust tier as the database file itself; documented loudly).

**Trade-off (stated plainly, this is the price of re-viewability):** one
master secret now protects *every* badge — a leak of the env/file plus
knowledge of the scheme lets someone mint any user's badge; previously each
badge was independent randomness and even a full DB leak yielded nothing.
Residual risk is bounded (secret lives outside the DB file; rotating the
secret invalidates all derived badges at once — a documented break-glass).
For a small shop kiosk this is the right trade.

**Fallback (rejected alternative): encrypted payload at rest** — store
`qr_badge_enc` (payload encrypted with a server key) and decrypt to render.
Same master-secret exposure, strictly more machinery (ciphertext column,
key management, rotation re-encryption), zero gain over derivation. Mention
in the plan for the coordinator; recommend against.

### T2 — What is a "purchase"?

Today there is only the live cart. Minimal honest model: **a purchase is
recorded at an explicit checkout action** (the current "Request items"
placeholder button becomes **Checkout**). Rules:

- `POST /api/purchases` (any authenticated user): one transaction —
  validate the cart is non-empty (**409** "Cart is empty"), read lines with
  live items (orphan lines are already reaped by `_cart_out`'s logic),
  **snapshot** each line (`name`, `price_cents`, `qty` — history must
  survive later item edits/deletions; `item_id` kept as a weak ref),
  **decrement stock clamped at 0** per line (same clamp doctrine as
  stock-adjust: a sale beyond stock is recorded, never silently dropped —
  the response reports clamped lines), create the purchase, delete the cart
  lines, commit. Response: the `PurchaseOut` (server-truth receipt).
- Stock decrement at checkout (not at cart-add) is the only coherent point:
  coordinator decision 6 ("cart adds never mutate stock") stays intact; the
  purchase is the sale. SQLite single-writer serialisation makes two
  simultaneous checkouts safe; the client `_cartOp` chain serialises per
  user anyway.
- Tables (new, `create_all()`; no migration entries):
  `purchases(id, user_id FK indexed, total_cents, created_at)` and
  `purchase_lines(id, purchase_id FK indexed, item_id NULL (weak ref —
  survives item deletion), name, price_cents, qty, line_total_cents)`.
- **Cleanup**: `delete_member` deletes the user's purchases+lines in the
  same transaction (operational data, like cart rows — D4 doctrine).
- `GET /api/purchases` (own history, newest first, lines included) — the
  profile tab's data. Staff viewing others' history: out of scope (flagged
  as a possible future admin endpoint).

### T3 — Phone number

`users.phone VARCHAR(32) NULL` (one appended `COLUMN_MIGRATIONS` entry).
Normalisation on write: strip spaces/dashes/parens/dots, then **E.164
shape** — leading `+`, 7–15 digits (schema validator → 422 otherwise).
Stored normalised; `UserOut` gains `phone` (it is the shop's own member
directory; staff seeing member phones via `/api/members` is accepted and
documented). Change endpoint: `POST /api/auth/change-phone {phone}` —
authenticated only; phone is not a credential, so no password re-entry
(same exposure class as changing it in person at the counter). `""` clears
it. Staff-side editing of member phones: out of scope for this pass.

### T4 — Email change

`POST /api/auth/change-email {new_email, current_password}`:
- **Requires the current password** (400 on mismatch — email is the login
  identifier; a stolen session must not be able to move the account).
- Normalise (`strip().lower()`), format-validate (existing
  `EMAIL_PATTERN`), uniqueness → **409** mirroring register (and the
  register-endpoint enumeration oracle is already an accepted trade-off in
  README's tracked issues — same class here).
- **Sessions are not revoked**: the bearer token is independent of the
  email (unlike a password change, no credential changed). Documented
  decision; the next *password* login simply uses the new email.
- Uniform error behaviour: verify the password against the stored hash
  regardless (timing parity with login's DUMMY_HASH pattern is unnecessary
  here — the caller is authenticated — a plain 400 on mismatch is fine).

### T5 — Login auto-detect

Mostly shipped (see above). Remaining work:
- **Demote the dedicated badge field** to an optional affordance: keep it
  (accessibility / slow-emulator path) but de-emphasise — replace the
  prominent form with a subtle line "Scan your QR badge **anywhere** on
  this page to log in — no click needed" and keep the field below it,
  collapsed/secondary. Keep `data-scan-sink` on it (its own Enter flow is
  the regression-safe path).
- **Verify the logged-out scan path end-to-end** (it ships already):
  page-background scan → login; scan with the **autofocused email field**
  focused → buffer, restore, route (the email field keeps its content);
  scan into the password field → same; a shaped-but-invalid badge →
  `qr-login` 401 surfaces as the login error, page state intact.
- **Already-built behaviours relied on**: logged-in own badge → 30 s
  logoff overlay (that *is* "logs off the current user"); different badge →
  save-then-switch; staged barcodes replay after the auto-login.
- One accepted edge (documented): a 43-character password typed at scanner
  speed ending in Enter matches the raw-badge shape gate → routed to
  `qr-login`, field restored, harmless 401 toast. Rate of occurrence ~zero;
  noted in docs.

---

## 1. Ordered tasks

Each task is one reviewable PR ending with `uv run pytest` green.

### Task A — Profile data: phone column + email/phone change endpoints
Files: `app/models.py` (`User.phone`), `app/main.py` (one
`COLUMN_MIGRATIONS` append), `app/schemas.py` (`UserOut.phone`,
`ChangeEmailIn`, `ChangePhoneIn` + E.164 validator), `app/routers/auth.py`
(`change-email`, `change-phone`).
Tests (`tests/test_profile.py`, new): phone normalisation (spaces/dashes →
E.164; 422 on bad shapes; `""` clears); email change happy path (login
with the new email works, old token still valid); wrong current password →
400; duplicate email → 409; unauthenticated → 401; flagged
(`must_change_password`) → 403 on both endpoints (not exempt);
`test_migrations.py` exact-set expectation gains `users.phone` (6 columns
total now, with Task B's entry).

### Task B — Purchase history (checkout)
Files: `app/models.py` (`Purchase`, `PurchaseLine`), `app/schemas.py`
(`PurchaseOut`, `PurchaseLineOut`, `CheckoutOut` = purchase + resulting
empty `CartOut`), new `app/routers/purchases.py` (`POST /api/purchases`,
`GET /api/purchases`), `app/main.py` (router registration),
`app/routers/members.py` (`delete_member` cleanup), `app/routers/cart.py`
(share the orphan-reap via a helper if needed — reuse `_cart_out` read
logic rather than duplicating).
Tests (`tests/test_purchases.py`, new): checkout snapshots lines and clears
the cart (GET /cart empty after; GET /purchases shows lines with
name/price frozen); empty cart → 409; stock decremented with clamp-at-0
reported (oversold line: stock 3, qty 5 → purchase records 5, stock 0,
`clamped: true` on that line); history survives item edit (rename item →
old purchase keeps old name) and item delete (weak-ref `item_id` → null
tolerant serialisation); per-user isolation; unauthenticated 401; flagged
403; two rapid checkouts (second → 409 empty); `delete_member` removes
purchases (direct DB check); `test_migrations.py` legacy-DB gains
`purchases`/`purchase_lines` (extend the Task-6 new-tables case).

### Task C — Re-viewable derived badges
Files: `app/security.py` (`BADGE_SECRET` loading: env or 0600 file in
DATA_DIR, `derive_badge_payload(user_id, generation)`), `app/qrbadge.py`
(doc: two payload provenances), `app/models.py` (`User.badge_generation`),
`app/main.py` (one `COLUMN_MIGRATIONS` append), `app/routers/auth.py`
(creation flow → add/flush/derive; `GET /api/auth/qr-badge` re-view with
legacy auto-migrate; rotate = generation++), `app/routers/members.py`
(admin regenerate = generation++ — same endpoint, new mechanism),
`app/schemas.py` (`QrBadgeViewOut` = reuse `QrBadgeOut`).
Tests (`tests/test_qr_derived.py`, new): derivation is deterministic and
43-char/urlsafe-shaped; register/member-create badges are derived
(generation 1) and log in; **re-view returns the same payload/SVG twice**
(the actual feature); rotate → generation 2, new payload logs in, old
payload 401s, re-view shows the new one; admin regenerate rotates the
member's badge; legacy badge (generation 0, hand-built) still logs in and
auto-migrates on view (old payload 401s after); wrong-secret derivation
fails (subprocess with a different `HCRM_BADGE_SECRET` → derived badge
401s, proving the secret is the root); `test_migrations.py` exact-set
gains `users.badge_generation`.

### Task D — SPA: Profile tab
Files: `static/app.js`, `static/index.html`, `static/style.css`.
- Rename the Account view → **Profile** (route `#/profile`; keep
  `#/account` as a redirect alias — external muscle memory + the
  `MUST_CHANGE_EXEMPT_PATHS` flows route there).
- Sections: **My details** (name read-only, email edit with current
  password, phone edit), **Password** (existing form), **QR badge** (view
  anytime via `GET /api/auth/qr-badge` → the isolated print modal;
  rotate button; legacy users see "viewing upgrades your badge" notice),
  **My purchases** (from Task B: date, lines, totals), **My scan
  requests** (existing, moved unchanged).
- Catalogue panel: "Request items" → **Checkout** → `POST /api/purchases`
  → receipt toast (+ link to Profile), cart re-rendered from the empty
  `CartOut` in the response, clamp warnings surfaced.
Tests: no JS runner (established) — `scripts/ui_check.py` copy pins for
the profile route is not reachable logged-out; keep to login/register pins
+ manual checklist; backend covered in A/B/C.

### Task E — Login auto-detect (UX + verification)
Files: `static/index.html` (login copy + field demotion),
`static/app.js` (copy constant only — no logic change expected),
`scripts/ui_check.py` (copy pin "anywhere" on the login view).
- Verify the four logged-out scan scenarios above behave as designed
  (manual checklist); adjust only if a gap appears (none expected — the
  mechanism shipped in Task 5).
- ui_check: add the new hint copy to the login-view assertions
  (`count()`-only); keep the badge-field presence pin.
- Manual hardware checklist updated (scan-with-email-focused, scan with
  page background focused, no-click login, logoff overlay re-scan).

### Task F — Docs, migrations check, checklist
Files: `docs/opticon-m10.md` (§10 additions: re-viewable derived badges +
the master-secret trade-off paragraph; auto-detect login note),
`README.md` (features: profile/checkout/re-viewable badge; env table:
`HCRM_BADGE_SECRET`; tracked issues: **remove "Server-side checkout"**,
update the badge bullet "shown once" → re-viewable), `NOTES.md`
(paragraph), `docs/barcodes.md` (no change needed).
Tests: exact-set migration test final state (6 columns), full suite,
`ui_check`, smoke (add checkout + re-view to the kiosk loop: ~4 checks).

## 2. Sequencing rationale

A (data/endpoints) and B (purchases) are independent and unblock D's
content; C (badge scheme) must land before D's QR section; D assembles
the profile tab; E is small and independent (can land any time); F closes.
Suggested PR order: **A → B → C → D → E → F**, each green.

## 3. Risks / open questions (for the coordinator)

1. **Master-secret trade-off (T1)** is the decision that needs an explicit
   owner sign-off: re-viewability *requires* recoverability; derivation is
   the cleanest recoverable form. If the owner instead prefers per-badge
   randomness, re-viewing is impossible by construction and item (2)
   reduces to "admin regenerates on request" (already shipped).
2. **Stock decrement at checkout** changes the long-standing "stock is
   never mutated by member flows" posture — it is the only way "purchase"
   means anything. Alternative: record-only purchases (no stock change)
   and keep stock strictly staff-managed; flag for decision (recommend
   decrement-with-clamp, reported).
3. **Email change without session revocation** — flag if the owner wants
   change-password-style revocation of other sessions instead (one-line
   change, worse kiosk UX).
4. **`#/account` → `#/profile` rename**: keep the alias (recommended) or
   hard-cut.
5. **Legacy-badge auto-migration on first view** silently kills the old
   printed badge — friendlier than a 409, but it *is* a rotation; the UI
   must say so plainly.
6. **Phone in `UserOut`** exposes it to staff via the members list —
   accepted for a shop directory; flag if staff visibility is unwanted.
