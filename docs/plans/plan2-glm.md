# Plan 2 (glm) — Member profile, re-viewable QR badge, login auto-detect

Three owner asks on top of the shipped M-10 feature: **(1)** a member
profile tab (purchase history, password/email/phone changes, QR view);
**(2)** QR linked to the account (done since Task 2) + **re-viewable**
(owner/user can see it again, admin regenerates); **(3)** login auto-detect
(scan a badge on the login screen with no field click).

Current state verified this pass: `User` has **no phone** column; `UserOut`
carries no phone; `MemberPatch` has no phone; badges are stored
**hash-only** (`qr_badge_hash`) with the payload shown exactly once
(`new_badge_for`, `POST /api/auth/qr-badge` rotate, `DELETE` revoke, staff
`/api/members/{id}/qr-badge`); admin regeneration **already exists**;
auto-issue at creation **already exists** (Task 2); there is **no
order/checkout concept** — only the live `cart_items` cart and the
`requestPurchase()` placeholder toast; **item 3's core already works**:
`handleBadgeScan` routes logged-out badge scans from the *global* HID
capture straight to `badgeLogin` (no field click needed) — the remaining
work there is UX copy and verification; `pyproject` has no `cryptography`
dep (the repo is stdlib-first/offline); `/data/` is git-ignored; the
migration machinery is the append-only `COLUMN_MIGRATIONS` + `create_all()`
for new tables.

---

## Design decisions for the three tensions

### D1 — Re-viewable QR vs hash-only-at-rest (the security-critical call)

Re-viewability **requires** the payload to exist server-side in
recoverable form — that is non-negotiable; the question is what a leak
yields. Today's doctrine (opticon-m10 §9.4): only the hash, so a *leaked
DB file* yields no printable badges.

**Proposal: encrypt-at-rest with a key that lives OUTSIDE the database.**
Keep `qr_badge_hash` as the login authority (unchanged hot path — a
hash-seek, no crypto), and add `qr_badge_encrypted` holding
`nonce ‖ tag ‖ ciphertext` of the payload, encrypted with a server key:

- **Key management**: `HCRM_BADGE_SECRET` env var if set; otherwise a
  256-bit key auto-generated on first boot to `data/badge.key` (0600,
  `/data/` is git-ignored, lives outside the DB). The property preserved:
  **a leaked DB alone still yields no badges**; the attacker needs the key
  file too. What is *given up*: a full-server compromise (DB + key) now
  yields every member's badge — the old design only gave that up if the
  payload had never been shown. The owner is accepting this trade for the
  feature; it must be written into §9.4.
- **Cipher (stdlib, per the repo's no-new-deps culture)**:
  HMAC-SHA256-CTR keystream (`block_i = HMAC(key, nonce ‖ i)`) with
  encrypt-then-MAC (`tag = HMAC(key, nonce ‖ ciphertext)`). A ~49-char
  payload is one or two blocks. Hand-rolled crypto is a real cost —
  mitigated by: the construction is standard, it is
  authentication-first (tampered rows fail the MAC, never decrypt), and
  the tests pin round-trip/tamper/wrong-key. *Alternative rejected*:
  adding the `cryptography` package (Fernet) — a new runtime dep against
  the vendored/offline doctrine; the construction above is a drop-in
  replacement for it if that ever changes.
- **Degradation semantics** (must be explicit): if the key is missing or a
  row fails the MAC (rotated key), **login is unaffected** (the hash needs
  no key) and re-viewing answers a clear 409 "re-generate the badge to
  re-view it" — which is exactly the *other* design:
- **Alternative design, rejected as primary**: "view = regenerate"
  (revoke + re-issue on every view — zero new storage). Rejected because
  it silently invalidates the member's printed card every time they peek
  online; kept as the automatic fallback when decryption is impossible.

### D2 — What "purchase history" is (minimal model)

A purchase is an **explicit checkout action**, not a scan log (a scan
isn't a sale) and not a stock-adjustment (that is staff receiving). This
finally gives the tracked-non-goal checkout real, minimal semantics:

- **`orders`** (`id`, `user_id` FK indexed, `total_cents`, `created_at`) +
  **`order_lines`** (`id`, `order_id` FK indexed, `item_id` **plain int —
  weak ref like `scan_requests.resolved_item_id`**, plus the denormalised
  snapshot `sku`, `name`, `price_cents`, `qty`). The snapshot is the
  point: history shows *what was paid*, immune to later price edits and
  item deletion.
- **When recorded**: `POST /api/orders` (the checkout button on the cart
  panel — today's `requestPurchase` placeholder). One transaction, the
  guarded `BEGIN IMMEDIATE` raw-connection pattern from
  `stock-adjust` (this is exactly its class: a guarded
  read-modify-write on `items.stock`): re-read stock under the write lock,
  **any line short of stock → 409 naming the item** (a sale beyond stock
  is rejected, not clamped — a clamp would silently under-deliver; the UI
  asks the member to adjust), decrement, insert order + lines, **clear the
  cart**, commit.
- **Reads**: `GET /api/orders/mine` (own orders, newest first, lines
  embedded). Staff-wide order views are *not* built (out of scope; noted).

### D3 — Phone, email change, and the profile endpoints

- **Phone**: new `users.phone: str = ""` (empty = not set; not unique —
  family shares numbers). Normalisation in the schema validator: strip
  spaces/dashes/parentheses/dots, optional leading `+`, then 6–15 digits
  required else 422. Stored normalised (E.164-ish, no library).
- **Email change is the account-takeover surface** (email is the login
  identifier): `PATCH /api/auth/profile` accepts `{name?, phone?, email?,
  current_password?}` where **`current_password` is required iff `email`
  is present and differs** (403 otherwise — a stolen session must not be
  able to swap the login identifier). Conflict → 409 (pre-check +
  `IntegrityError` race catch, the register pattern). **Other sessions are
  NOT revoked** (unlike change-password): the credential (password) is
  unchanged and was verified; documented explicitly. Name/phone changes
  need no password (same sensitivity as today's staff-side edits).
- Staff keep their existing member-edit path: `MemberPatch` gains `phone`
  (staff editing a member's phone; email stays member-only via the profile
  endpoint — staff changing a member's *login identifier* without the
  member present is a takeover vector we don't open).

### D4 — Login auto-detect (mostly already built — scope honestly)

Task 5's global capture already routes logged-out badge scans to
`badgeLogin` with **no field click**: the document-level listener works
with focus anywhere (bursts landing in the email/password fields are
buffered and restored — nothing is eaten). The remaining work is small:
login-page copy that says "just scan your badge" instead of implying the
field must be clicked, the dedicated field demoted to an optional fallback
(kept — it is the deterministic path and the manual-entry surface),
one manual hardware check, and a ui_check copy pin. Logged-in badge scans
already do logoff-countdown / switch (Task 4/5) — untouched.

---

## Ordered tasks

### Task 1 — Phone column + profile endpoints (backend)
Files: `app/models.py` (+`phone`, migration entry
`("users", "phone", "VARCHAR(32)", ())`), `app/schemas.py`
(`ProfilePatchIn`, phone validator, `UserOut.phone`, `MemberPatch.phone`),
`app/routers/auth.py` (`PATCH /api/auth/profile`), `app/routers/members.py`
(staff phone edits), `app/main.py` (migration append),
`tests/test_migrations.py` (exact-set + legacy case).
Tests (`tests/test_profile.py` new): name/phone happy path; phone
normalisation table (`+34 600 123 456` → `+34600123456`; `(555) 123-4567`
→ `5551234567`); invalid phone 422 (letters, too short/long); email change
without password → 403; wrong password → 400; email conflict → 409;
`IntegrityError` race → 409; `/me` reflects; staff PATCH member phone;
member cannot use staff PATCH (403); other sessions survive an email
change (pinned).

### Task 2 — Badge re-viewability (backend, security-critical)
Files: `app/qrbadge.py` (key load/generate, `encrypt_badge_payload` /
`decrypt_badge_payload` — CTR + EtM; `new_badge_for` stores both hash and
ciphertext), `app/models.py` (+`qr_badge_encrypted`, migration entry),
`app/routers/auth.py` (`GET /api/auth/qr-badge` → `{payload, svg}`,
repeatable; 409 with the regenerate message when undecryptable/revoked),
`app/main.py` (migration), `tests/test_migrations.py`.
Tests (`tests/test_qr.py` + new `tests/test_badge_crypto.py`): encrypt
round-trip; tampered ciphertext → MAC failure → the 409 path, never a
partial payload; wrong/rotated key → graceful 409 while `qr-login` still
works (the hash is independent — the load-bearing pin); key file
auto-generation (0600, stable across boots); `GET` returns the *same*
payload repeatedly; after rotate, `GET` returns the new one; register/
creation still carries the one-shot display; deletion cleans the row;
`has_qr_badge` semantics unchanged. Docs: opticon-m10 §9.1/§9.4 doctrine
rewrite (the trade-off, key handling, rotation consequences) — this task
does not ship without it.

### Task 3 — Checkout + purchase history (backend)
Files: `app/models.py` (`Order`, `OrderLine` — new tables, no migration
entries), `app/schemas.py`, new `app/routers/orders.py`
(`POST /api/orders`, `GET /api/orders/mine`, `GET /api/orders/{id}` own-only),
`app/routers/members.py` (delete_member cleanup), `app/main.py` (router).
Tests (`tests/test_orders.py` new): happy checkout (stock decremented,
cart cleared, snapshot + totals correct); empty cart → 409; short stock →
409 naming the item, nothing decremented (atomicity pin); concurrent
checkouts racing the last unit → exactly one succeeds (the
`BEGIN IMMEDIATE` pin, `test_concurrency.py` style); price edit after
order → history unchanged; item deleted after order → history intact
(weak-ref pin); `/mine` per-user isolation; member deletion cleanup;
flagged-account 403.

### Task 4 — Profile tab + checkout button (SPA)
Files: `static/app.js`, `index.html`, `style.css`.
The Account view becomes a tabbed profile: **Profile** (name/phone/email
form — the password field revealed/enforced when the email is being
changed; client-side mirrors the server rule), **Purchase history**
(orders list with dates, line snapshots, totals; the cart panel's
"Request items" button becomes **"Complete purchase"** → `POST
/api/orders` → success toast + history refresh; 409-short-stock → toast
naming the item and highlighting the line), **My QR badge**
(`GET /api/auth/qr-badge` → inline SVG + Print via the existing isolated
badge-print modal + regenerate/revoke buttons), **My scan requests**
(the existing `/mine` list, folded in), **Change password** (existing
form, folded in).
Verify: `node --check`, `ui_check.py` (existing pins + any new static
copy), manual checklist (profile saves, email-change password gate,
checkout flow, QR re-view + print isolation, history after price edits).

### Task 5 — Login auto-detect polish (SPA, small)
Files: `static/index.html` (login card copy: "Scan your QR badge anywhere
on this page — no need to click"; the badge field labelled "or type/paste
it"), `scripts/ui_check.py` (copy pin), `static/app.js` only if
verification finds a gap.
Verification: confirm the logged-out global-capture path
(`handleBadgeScan` → `badgeLogin`) works with focus in the password field
(buffer+restore), on the page background, and from the visible scan
field; one manual HID check. No backend work.

### Task 6 — Docs + security review (closing pass)
`README.md` (profile/checkout features, `HCRM_BADGE_SECRET` env row,
phone field, tracked-issues: checkout moves from non-goal to shipped —
the *list* semantics note), `docs/opticon-m10.md` (§9.1/§9.4 re-view
doctrine + key handling; §10 pointer), `NOTES.md`. Security review:
key-file permissions and rotation runbook, the email-change takeover
analysis, order/stock atomicity, the accepted-risk ledger update
(server-compromise-yields-badges trade-off).

---

## Risks / open questions (for the owner/coordinator)

1. **The doctrine change is the owner's call, made explicit**: re-viewable
   badges trade "only a server *and* key compromise leaks badges" for the
   feature. Key file outside the DB + git; if `HCRM_BADGE_SECRET` is set
   in prod, ops must not lose it (loss = re-generate-to-re-view, logins
   unaffected).
2. **Hand-rolled crypto**: HMAC-CTR + EtM is standard but hand-rolled;
   the tests pin round-trip/tamper/wrong-key, and Fernet remains a drop-in
   swap if a dep is ever acceptable. If the coordinator prefers, Task 2
   can add `cryptography` instead — say so before implementation.
3. **Checkout stock semantics**: reject-not-clamp chosen (a silent
   under-delivery is worse than a "adjust your list" toast). Confirm.
4. **Email change does not revoke other sessions** (password verified,
   credential unchanged) — confirm the owner is comfortable, or flip to
   change-password's revoke-others behaviour (one loop).
5. **Staff-wide order views deferred** — only `/mine` ships; a staff
   orders table is a natural follow-up.
6. **Phone is stored normalised, not verified** (no SMS/OTP in scope);
   validation is shape-only.
7. **Order lines are denormalised snapshots by design** — history must
   not rewrite when the catalogue changes; `item_id` is a weak reference
   (the `scan_requests.resolved_item_id` precedent).
