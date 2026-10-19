# Plan-2 review (qwen3.8-max) — peer plans for profile / re-viewable badges / auto-detect

Reviews `plan2-glm.md` and `plan2-kimi.md` against `plan2-qwen.md`. The three
plans converge on ~80 % (checkout-as-history, snapshot lines with weak
`item_id`, password-gated email change with uniqueness-409, phone
normalisation without libraries, auto-detect ≈ shipped UX work, key
management = env override + auto-generated 0600 file in the data dir,
`#/account` becomes the profile hub, docs doctrine rewrite). The three
contested decisions the coordinator flagged split 1-1-1 or 2-1 in
interesting ways; they get the deep analysis first.

---

## 1. Contested decision A — badge re-viewability: derivation vs encryption

Positions: **mine + kimi** = HMAC-derived payloads (`HMAC(key, user_id:gen)`,
a generation column); **glm** = encrypt-at-rest (stdlib HMAC-CTR keystream +
encrypt-then-MAC, `qr_badge_encrypted` column). All three keep
`qr_badge_hash` as the login authority and identical key management.

### The analysis that decides it: what a KEY-ONLY leak yields

Both designs are equal under a DB-only leak (nothing printable) and under a
DB+key leak (every current badge). They differ under a **key-only leak**
(env var copied, keyfile exfiltrated, DB intact):

- **Derivation collapses.** Payloads become a pure function of
  (key, user_id, generation) — a *low-entropy, guessable input space*
  (ids 1…N × gens 1…few). An attacker with the key needs no DB: they mint
  candidates and try them against `POST /api/auth/qr-login`, which — by
  explicit, documented doctrine (`docs/opticon-m10.md` §9.4: "badges are
  unguessable (256-bit), so qr-login carries **no rate limit**") — happily
  answers. A few thousand online probes own every account. The derivation
  design silently voids the premise an existing shipped security decision
  rests on. (A per-issuance random salt stored in the DB would close this —
  but then you are storing per-badge material again and have rebuilt
  encryption with extra steps.)
- **Encryption degrades gracefully.** Payloads stay per-badge
  `secrets.token_urlsafe(32)` randomness; the ciphertexts live in the DB.
  Key alone decrypts nothing; the 256-bit-unguessability premise (and the
  no-rate-limit decision) survives every leak short of DB+key.

### The second, structural argument: encryption preserves the Task-2 issuance invariant

Both derivation plans must restructure account creation because derivation
needs the committed/flushed `user.id`: mine went **two-phase commit** (a
documented "account exists badgeless" failure window), kimi's went
`add → flush() → derive → commit` (clever — and the flush trick is sound —
but it still rewrites the Task-2-tested creation flow and moves the
IntegrityError from `commit()` to `flush()`, touching the race-path tests).
**Encryption has no dependency on `user.id`**: `new_badge_for` keeps
generate-random-payload-before-add, sets hash *and* ciphertext, and the
existing single-commit + IntegrityError-rollback flow ships **byte-identical**
— the whole Task-2 atomicity story (and its tests) is untouched. That is a
large hidden cost-saver neither derivation plan accounted for.

### Verdict: adopt glm's encrypt-at-rest — with three refinements

1. **Key separation** (glm's spec omits it): derive `enc_key =
   HMAC(master, "hcrm-badge-enc")` and `mac_key = HMAC(master,
   "hcrm-badge-mac")` — never the same key for keystream and MAC. Tag
   compare via `hmac.compare_digest`. Random 16-byte nonce per encryption
   (rotation re-encrypts with a fresh nonce); stored blob = `nonce ‖ tag ‖
   ciphertext` (b64, one column).
2. **Legacy badges: explicit rotation, NOT auto-migrate-on-view.** kimi's
   "first view silently rotates" is a **state mutation on a GET** that kills
   a printed card as a side effect of rendering a page (kimi's own risk 5
   admits the sharp edge). My structured 409 `{re_viewable: false, reason:
   "legacy"|"no-key"|"mac-failed"}` + a user-pressed *Regenerate* button is
   safe, and glm independently rejects view-side-effects too ("view =
   regenerate … silently invalidates the printed card" — their reason for
   rejecting it as the *primary* mechanism applies equally to kimi's
   auto-migrate).
3. **Ship-blocker docs** (glm's insistence, adopted): §9.1/§9.4 doctrine
   rewrite lands in the same PR — the change from "DB leak yields nothing"
   to "DB **+ key** yields every badge; backups are credential-bearing"
   must not be discoverable only by reading code. Plus my two key-ops items
   that **both peers missed**: `backup_db.sh` copies `badge.key` (and warns),
   and `/api/healthz` reports `badge_retrievable: true|false` (loud-visibility
   doctrine, cf. `insecure_dev_admin`).

Hand-rolled-crypto concern (glm's own risk 2, kimi's "strictly more
machinery"): real, but bounded — HMAC-CTR + EtM over a ≤49-byte message is
the textbook PRF construction, the repo already assembles PBKDF2 from
`hashlib` primitives by doctrine ("stdlib only, no external crypto deps"),
and the test list (round-trip / every-field tamper → MAC fail → 409 / wrong
key → 409 / login unaffected throughout) pins the failure modes that matter.
`cryptography`/Fernet stays the documented drop-in if the dep doctrine ever
relaxes. Note for the coordinator: this reverses my plan's own recommendation
— the rate-limit-premise argument is what moved me, and it was absent from
all three plans until this review.

## 2. Contested decision B — checkout stock: reject (glm) vs clamp-and-report (mine + kimi)

**Verdict: clamp-and-report, 2-of-1 + repo precedent.** glm rejects a
checkout whose line exceeds stock (409 naming the item, nothing decremented,
member adjusts). Against: (a) the repo's own stock-adjust doctrine is
verbatim the opposite — "a sale beyond available stock is **reported, never
silently dropped**" (REVIEW-m10 P6, `StockAdjustOut.clamped`); (b) the stock
count is demonstrably best-effort in this shop (no reservations, staff
PATCH corrections, coordinator decision 6 already chose warn-not-block for
carts) — rejecting a sale at the counter because a *count* is stale stops
the queue behind a kiosk; (c) glm's design then needs the guarded
`BEGIN IMMEDIATE` raw-connection ritual (their plan says so) to make the
reject race-free, while the SQL-side `stock = MAX(0, stock - qty)` clamp is
**race-free by construction** (the guard is in the statement — the same
reason the cart upsert's cap needed no ritual). Oversell is recorded,
reported per line (`requested/applied/clamped`), and staff fix stock after.
glm's reject semantics stay documented as the owner-flippable alternative
(their risk 3 asks for confirmation anyway). Table naming: `purchases` /
`purchase_lines` (2-of-1, matches the owner's "purchase history" and the
existing "purchase list" UI vocabulary); line snapshots include **sku**
(mine+glm; kimi omits it — receipts want it). `GET /api/purchases` (own,
paginated) + `GET /api/purchases/{id}` (own-or-staff — counter support;
staff-wide listing stays deferred, all three agree). Concurrency test
adapted: double-checkout race → exactly one purchase, loser gets 409
"cart is empty" (kimi's shape — glm's last-unit race test doesn't apply
under clamp semantics).

## 3. Contested decision C — email change: revoke other sessions (mine) vs not (glm + kimi)

**Verdict: do NOT revoke — I concede (2-of-1, and the threat model says the
peers are right).** The revoke-others doctrine exists on `change-password`
because the *credential itself* changed: pre-existing sessions may be the
attacker's, and the new password defines a new trust boundary. Email change
requires the current password (all three plans), tokens bind to `user_id`
(not email), and an attacker who holds the password can change the password
anyway — which already revokes everything. Revoking on email change
therefore adds zero attacker-cost and pure friction (the owner's phone
session dies because they fixed a typo on the kiosk). Keep: password
required (400 on mismatch), normalise+lower, uniqueness pre-check +
`IntegrityError` → 409 (register's pattern), gate inherited (not exempt),
**decision documented** (glm's risk 4 / kimi's risk 3 both pre-flag the
owner flip — one loop if ever wanted). Related adoption: glm's rule that
**staff can never change a member's email** (login identifier; a takeover
vector we don't open) — `MemberPatch` stays email-less, documented; staff
*may* set phone (mine+glm 2-of-1 against kimi's deferral — staff create
most accounts at the counter, and phone-without-email is not an identity
risk).

## 4. Per-plan notes (beyond the three contests)

### glm — strengths also adopted
- The `PATCH /api/auth/profile` consolidated endpoint is *rejected*
  (2-of-1: separate `change-email` / `change-phone` verb endpoints match
  the auth router's house style — `change-password`, `logoff-schedule`,
  `qr-login` — and keep the conditional-password rule out of one
  multi-field validator), but glm's **conditional rule itself** (password
  required *iff* the identity field changes; name/phone need none) is
  exactly the semantics the separate endpoints implement.
- Test-list details adopted: MAC-failure never yields a partial payload;
  key auto-generation stable across boots; "login unaffected while
  decryption degraded" as the load-bearing pin; email-change *other
  sessions survive* pinned (now that non-revocation is the decision, pin
  it so a future hand doesn't "fix" it silently).
- Their D4 scoping of auto-detect ("the remaining work is UX copy and
  verification") matches all three plans — no disagreement.

### glm — weaknesses
- W1: derivation-blind-spot n/a (they chose encryption) — but their
  encryption spec omits key separation and constant-time tag compare
  (refinement 1 above).
- W2: reject-checkout + BEGIN IMMEDIATE (contest B).
- W3: `phone: str = ""` NOT NULL DEFAULT "" — nullable is the better
  house shape (2-of-1: `""`-clears maps to NULL, `UserOut` renders null
  honestly; the barcode precedent).
- W4: no backup-script or healthz handling of the keyfile (both peers
  missed; mine had both).
- W5: phone digit floor 6 vs kimi's strict E.164 `+`-required 7–15: glm's
  optional-`+` 6–15 is the pragmatic winner (a village shop's "555 1234"
  must be storable); adopted.

### kimi — strengths also adopted
- **S1: the flush-before-commit observation** (even though encryption
  makes it unnecessary here, it is the correct technique if any future
  derivation-style feature needs an id pre-commit — record it in the plan
  as the rejected-but-noted alternative).
- **S2: the honest master-secret trade-off paragraph** ("one secret now
  protects every badge … rotating the secret invalidates all derived
  badges — a documented break-glass") — its encryption-adapted form (key
  rotation ⇒ re-encrypt-on-next-rotate, break-glass = mass rotation
  runbook) goes into §9.4.
- **S3: `#/account` → profile with the tabbed layout** (tabs adopted; the
  route *rename* + alias rejected — 2-of-1 with glm keeping the route;
  the `must_change_password` redirect and muscle-memory links keep
  working, zero alias machinery).
- **S4: the documented auto-detect edge** — a 43-char password typed at
  scanner speed matches the raw-badge gate → harmless 401 toast, field
  restored. The best statement of the residual false-positive class;
  adopt into docs + checklist.
- **S5: smoke-test extension** (checkout + re-view in the kiosk loop) —
  matches mine; glm omitted.
- **S6: `CheckoutOut` = purchase + resulting empty `CartOut`** — the SPA
  re-renders from one payload, the Task-1 mutator contract applied to
  checkout. Adopt (my `PurchaseCreateOut` was the same idea, theirs is
  better specified).

### kimi — weaknesses
- W1: auto-migrate-on-view (contest A refinement 2 — GET with a
  credential-killing side effect).
- W2: derivation (contest A) — with the rate-limit-premise collapse they
  didn't examine (their trade-off paragraph weighs DB+key but not
  key-only).
- W3: staff phone editing deferred (contest C aftermath — 2-of-1 against).
- W4: strict E.164 `+`-required phone (W5 above).
- W5: no healthz/backup keyfile handling (shared miss with glm).

## 5. Final recommended shape

**Task order (each a green-suite PR):**

1. **A — Badge re-viewability (encrypt-at-rest).** `app/qrbadge.py`:
   `encrypt_badge_payload`/`decrypt_badge_payload` (HMAC-CTR + EtM,
   separated subkeys, `compare_digest`, nonce‖tag‖ct blob); key loading in
   `app/security.py` (env `HCRM_BADGE_SECRET` hex, else auto-generated
   0600 `<data>/badge.key`, startup-announced); `users.qr_badge_encrypted`
   (one appended migration; NULL = legacy/none); `new_badge_for` stores
   hash + ciphertext (**creation flow otherwise untouched — single commit,
   Task-2 tests unchanged**); rotate stores both, revoke clears both;
   **new** `GET /api/auth/qr-badge` (self) and `GET /api/members/{id}/
   qr-badge` (staff, `_get_manageable_member` rule) → `QrBadgeOut` or
   structured 409 (`legacy` / `no-key` / `mac-failed` — each with the
   regenerate guidance); `healthz.badge_retrievable`; `backup_db.sh`
   copies the key + warns; §9.1/§9.4 doctrine rewrite **in the same PR**.
   Tests: crypto round-trip/tamper-every-field/wrong-key; re-view returns
   the identical payload repeatedly (the feature); rotate→re-view shows the
   new one, old 401s; **login unaffected while degraded** (the load-bearing
   pin); legacy badge → 409 `legacy`, explicit rotate fixes; key auto-gen
   0600 + stable across boots; wrong-secret subprocess pin (kimi's, adapted:
   ciphertext undecryptable, hash-login still works); staff GET mirrors the
   manage rule; gate 403 (not exempt); migrations exact-set +1.
2. **B — Phone + email change.** `users.phone` nullable (migration);
   `_normalised_phone_or_none` (strip spaces/dashes/dots/parens, optional
   `+`, 6–15 digits else 422; `""` clears — barcode precedent);
   `UserOut.phone`; `MemberIn`/`MemberPatch.phone` (staff counter signup;
   **never email** — documented); `POST /api/auth/change-email
   {new_email, current_password}` (400 mismatch, 409 duplicate incl.
   IntegrityError race, **no session revocation — pinned both ways**);
   `POST /api/auth/change-phone {phone}` (session-only). Tests incl. the
   normalisation table, other-sessions-survive pin, badge-unaffected pin
   (email is not a badge input), gate 403s, migrations exact-set +1.
3. **C — Purchases (checkout + history).** `purchases` + `purchase_lines`
   (snapshots sku/name/price_cents/qty/line_total, weak `item_id`,
   create_all only); `POST /api/purchases` — one transaction: cart re-read
   (empty → 409), snapshot lines, per-line `UPDATE items SET stock =
   MAX(0, stock - :qty)` + read-back for the clamp report, clear cart,
   commit → `CheckoutOut {purchase, cart(empty)}` with per-line
   `requested/applied/clamped`; `GET /api/purchases` (own, paginated),
   `GET /api/purchases/{id}` (own-or-staff); `delete_member` cleanup.
   Tests: snapshots freeze price/name across later edits; clamp reported
   and stock floored at 0; empty-cart 409; double-checkout race → one
   purchase + 409-empty; weak-ref survives item delete; isolation;
   cleanup; gate; legacy-tables migration test extended.
4. **D — SPA profile tab** (route `#/account` kept, retitled "My profile",
   tabbed): Details (email form with current-password field, phone form),
   Password (existing), **QR badge** ([Show my QR] → GET → the existing
   isolated print modal; legacy/no-key states render the structured 409 as
   a *Regenerate to make your badge re-viewable* CTA; **copy doctrine
   update**: "Shown only once — cannot be retrieved" becomes "Anyone
   holding this code can log in — keep it private", including on the
   register/member-create one-shot modals, which keep "print it now" but
   drop the now-false unrecoverability claim), **Purchases** (history list +
   expandable receipts with clamp marks), My scan requests (moved), and the
   cart panel's `requestPurchase()` → **Complete purchase** (confirm →
   `POST /api/purchases` → receipt + clamp surfacing + `loadCart()`);
   Members tab gains **View** beside the existing **Regenerate** (staff
   GET). `node --check`, ui_check green, manual checklist.
5. **E — Auto-detect login UX.** Login copy: "Scan your badge — no need to
   click anything"; badge field demoted into a collapsed "enter manually"
   fallback (kept: accessibility, slow >50 ms emulators, determinism;
   `data-scan-sink` untouched); `busy` short-circuit in `badgeLogin`
   (mine — neither peer had it); capture-harness cases: badge burst with
   focus in email, in password (restored, never submitted), no focus;
   kimi's 43-char-fast-password edge documented; ui_check copy pin on the
   login view. Logged-in flows untouched (countdown = the owner's "logs
   off the current user").
6. **F — Closing.** README: features, env row `HCRM_BADGE_SECRET`,
   **security-notes trade-off paragraph** (DB+key ⇒ printable badges;
   backups credential-bearing; key-loss = rotate-to-recover, logins
   unaffected), tracked-issues checkout row removed (false-claims test
   re-verified); opticon-m10 §9 rewrite (shipped in A) + §10 auto-detect
   note; NOTES paragraph; migrations exact-set final (6 columns);
   smoke kiosk-loop += re-view + checkout + history (~4 checks); hardware
   checklist addendum (no-click login from all three focus states, re-view
   print, checkout clamp observed in the staff table, email change keeps
   the badge working).

**Coordinator decision points carried forward:** clamp-vs-reject is
recommended clamp (owner-flippable, one transaction branch); email-change
revocation recommended OFF (one loop to flip); `cryptography` dep remains
available as the crypto swap if the stdlib doctrine relaxes; staff-wide
order listing deferred; member self-name-edit not included (nobody asked;
staff can).

## 6. Changes to my own plan

1. **Derivation → encryption** (contest A): I was wrong on the decisive
   axis — derivation voids the shipped "256-bit ⇒ no rate limit" premise
   under key-only leak, and it forced my two-phase issuance (withdrawn:
   encryption leaves the Task-2 creation flow untouched). My legacy
   handling (explicit rotate, structured 409) and key-ops items
   (backup script, healthz) survive into the merged shape.
2. **Email-change revocation withdrawn** (contest C) — and pinned both
   ways in tests so the decision is executable, not prose.
3. Phone: `3–15` floor → glm's `6–15` with optional `+`; nullable column +
   `""`-clear kept; staff MemberIn/Patch phone kept (2-of-1).
4. Profile: adopt the **tabbed** layout (glm/kimi); keep route
   `#/account` (reject kimi's rename+alias); adopt kimi's `CheckoutOut`
   (purchase + empty cart in one payload).
5. Adopt kimi's flush-technique note (as the recorded alternative if a
   future feature needs pre-commit ids), their master-secret/break-glass
   framing (adapted to key rotation), their 43-char-password edge, and the
   smoke extension; adopt glm's §9-rewrite-as-ship-blocker, staff-never-
   edits-email rule, and the degraded-login-unaffected pin.
6. Keep uniquely mine: `busy` guard in `badgeLogin`, healthz flag,
   `backup_db.sh` key copy, `GET /purchases/{id}` own-or-staff, harness
   cases for the three focus states.

## 7. Verdict

No plan is adoptable whole: **glm wins the security-critical mechanism**
(encrypt-at-rest — vindicated by the rate-limit-premise analysis and the
preserved Task-2 issuance invariant) but loses both other contests
(reject-checkout, consolidated PATCH); **kimi contributes the best
trade-off prose, the flush technique, the tabbed profile, and the honest
auto-detect edge list** but its derivation choice shares the key-only-leak
collapse and its auto-migrate-on-view is a GET with a credential-killing
side effect; **mine contributes the legacy-handling discipline, the
key-ops items both peers missed (backup, healthz), and the clamp/checkout
shape** but was wrong on derivation and on email revocation. §5 is the
merged recommendation; the two owner-facing decision points (clamp vs
reject; revocation off) are stated with their flip costs.
