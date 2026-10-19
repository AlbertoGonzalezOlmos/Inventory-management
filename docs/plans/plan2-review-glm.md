# Plan 2 cross-review (glm) — of `plan2-kimi.md` and `plan2-qwen.md`

Reviewer context: my own plan is `docs/plans/plan2-glm.md`. All three plans
agree on the shape (six tasks: profile fields → badges → purchases → SPA →
login UX → docs) and on the load-bearing finding that **auto-detect login is
~80 % shipped** (Task 5's global capture already routes logged-out badge
scans to `badgeLogin` from any focus state). The review arbitrates the three
flagged tensions — and on the biggest one, both peers out-design me.

---

## 1. Kimi

**Strengths (adopt):**
- **HMAC-derived badges — the better mechanism, and I concede my
  encrypted-at-rest choice.** Derivation
  (`payload = HMAC(secret, user_id:generation)`) achieves the *identical*
  exposure trade-off (DB+key ⇒ all badges; DB alone ⇒ nothing) with strictly
  less machinery: no ciphertext column, no nonce/tag handling, no
  hand-rolled cipher — my HMAC-CTR + encrypt-then-MAC construction was a
  hand-rolled stream cipher, the weakest part of my design, and derivation
  eliminates it entirely. Re-view is one HMAC; rotation is
  `generation += 1`; the payload format is byte-identical so every parser,
  the bridge, and the SPA gates are untouched; and the login authority stays
  `qr_badge_hash` with zero hot-path change. Kimi's framing of the
  rejected alternative ("same master-secret exposure, strictly more
  machinery, zero gain") is exactly right.
- **Flush-based single-transaction issuance** (`add → session.flush()`
  assigns the id → derive → commit): derivation needs `user.id`, and the
  flush trick keeps the whole issuance in ONE transaction — the register
  409-race rollback still covers everything, no badgeless window. Better
  than qwen's two-phase commit.
- The 43-char-password-at-scanner-speed edge case, honestly documented.
- T4's plain-400-on-mismatch (the caller is authenticated; DUMMY_HASH
  timing parity is a login-path concern, not a profile one) — correct
  scoping of an existing doctrine.

**Weaknesses / rejected:**
- **Clamp-at-0 auto-migrating legacy badges on first view** — it *is* a
  rotation that silently kills the member's printed card because they
  clicked "view". Qwen's explicit rotate-to-enable (with the UI explaining)
  is the safer default; kimi's own risk #5 admits the footgun.
- Stock clamp semantics: right answer (see §2), but kimi keeps the
  guarded-transaction framing without noticing that a SQL-side clamp makes
  the ritual unnecessary (qwen's catch).
- `change-phone` endpoint but staff phone editing "out of scope" — the
  counter workflow (staff updating a member's phone in person) is natural
  and one `MemberPatch.phone` field; qwen includes it.

## 2. Qwen

**Strengths (adopt):**
- **The SQL-side clamp insight** (T2): `UPDATE items SET stock =
  MAX(0, stock - :qty)` is a single statement — atomic under SQLite,
  race-free **by construction**, which dissolves the entire
  `BEGIN IMMEDIATE` question. My reject-on-shortage design was precisely
  what *forced* the guarded-read ritual (to reject you must read the latest
  stock under the lock); clamping in the statement needs no guard at all.
  Combined with the stock-adjust precedent (`applied_delta`/`clamped`,
  REVIEW-m10 P6) and decision 6 (carts may exceed stock by design), this
  settles the tension: **clamp-and-report, per line, with
  requested/applied/clamped on each line** — the history records what was
  requested AND what was actually deliverable. I withdraw my reject stance.
- **Verify-before-return on re-view** (`derived-hash == stored-hash` else a
  structured 409): a swapped or corrupt keyfile can never render a *wrong*
  badge — it fails cleanly into the rotate-to-re-issue path. The single
  best line in either peer plan.
- **The copy-doctrine catch**: once re-viewability ships, "Shown only once —
  cannot be retrieved" is a lie. The warning must become "anyone holding
  this code can log in — keep it private", and the creation modals keep
  their urgency but drop the false claim. Neither kimi nor I noticed our
  own Task-5 copy would become false.
- **`backup_db.sh` must copy the keyfile and warn that backups are now
  credential-bearing** — the key lives beside the DB by default, so
  "someone copied `data/`" silently changed meaning. A real operational
  catch; applies to either mechanism.
- `badge_derivation: true|false` in `/api/healthz` (the loud-visibility
  doctrine), the `busy` short-circuit in `badgeLogin`, the email-change
  ⇒ badge-unaffected pin (a *consequence* of deriving from `user_id`, worth
  testing), explicit-rotate for legacy badges, keeping `#/account` (no
  route churn — the `must_change_password` redirect keeps working), the
  smoke-test extension, and the mass-rotation runbook for key loss.

**Weaknesses / rejected:**
- **Two-phase issuance** (commit user → derive → commit): introduces a
  badgeless window and a phase-2-failure recovery path where kimi's flush
  needs neither. Adopt kimi's.
- **Email change revoking every other session** (mirrors change-password):
  over-conservative. The password — the actual credential — is verified and
  unchanged; other sessions hold valid tokens regardless. The realistic
  attacker (phished password + stolen session) is not stopped by revocation
  (they can just re-login), while the kiosk/multi-device member *is*
  inconvenienced. Me + kimi (2-of-3) keep sessions; documented. Flag for
  the coordinator as taste.
- Phone as `str | None` — the repo's string-field convention
  (`image_url`, `description`) is `str = ""` with `""`-clears (the
  `ItemPatch.barcode` precedent qwen itself cites); nullable adds
  NULL-handling for no gain.

## 3. Final recommended shape

**Badges (the core decision):** HMAC-derived payloads
(`HCRM1:` + 43 chars of `HMAC-SHA256(secret, "…:<user_id>:<version>")`),
`qr_badge_version` column (NULL = legacy random), login authority stays
`qr_badge_hash` (unchanged, indexed, uniform-401); issuance via kimi's
**flush-in-one-transaction**; re-view via `GET /api/auth/qr-badge` with
qwen's **hash-verification-before-return** and a structured 409
(`legacy | no-key | key-mismatch`); legacy badges re-viewable only after an
**explicit rotation** (UI explains); key = 32 bytes auto-generated to
`<data>/badge.key` (0600, startup-announced) or `HCRM_BADGE_KEY` env;
`healthz.badge_derivation`; `backup_db.sh` copies the key + warns;
copy-doctrine update (no more "cannot be retrieved"); mass-rotation runbook
in the docs. Optional staff `GET /api/members/{id}/qr-badge` (qwen).
Trade-off paragraph (DB+key ⇒ badges; DB alone still safe) in README
security notes + opticon-m10 §9.

**Purchases:** explicit checkout (`POST /api/purchases`); `purchases` +
`purchase_lines` with denormalised snapshots (`sku`, `name`, `price_cents`,
`qty` **requested and applied**, weak-ref `item_id`); **SQL-side clamp**
`stock = MAX(0, stock - :qty)` with per-line `clamped` reporting, no
BEGIN-IMMEDIATE ritual; empty cart → 409; cart cleared in the same commit;
`GET /api/purchases` (+`/{id}` own-or-staff); `delete_member` cleanup.
Checkout replaces the `requestPurchase()` placeholder.

**Profile:** `users.phone: str = ""` (""-clears, E.164-ish normalisation →
422); separate `POST /api/auth/change-phone` (no password — contact info)
and `POST /api/auth/change-email` (**current password required** — the
login identifier; 409 on conflict + race; **no session revocation**,
documented); staff `MemberPatch.phone`; `UserOut.phone`; the existing
`#/account` route retitled "My profile" with sections (identity/security/
QR badge/purchases/scan requests); checkout button + clamp surfacing in the
cart panel.

**Auto-detect:** already shipped — login copy ("scan anywhere, no click"),
the badge field demoted to a collapsed manual fallback (sink behaviour
unchanged), qwen's `busy` short-circuit, harness cases for
email-focused/password-focused/no-focus scans, hardware checklist entries.
Logged-in badge behaviour untouched (the countdown *is* "logs off the
current user").

**Sequencing:** A phone/email → B badges → C purchases → D SPA → E login
UX → F docs/migrations/smoke/checklist (all three plans converge; kimi's
A→B→C→D→E→F ordering adopted).

## 4. Changes I am making to my own plan as a result

1. **Drop encrypted-at-rest entirely; adopt HMAC derivation** (kimi's
   mechanism + flush issuance, qwen's verify-before-return, healthz flag,
   keyfile backup warning, copy-doctrine fix, mass-rotation runbook). My
   hand-rolled CTR+EtM was the plan's biggest liability; derivation
   delivers the same property with none of it.
2. **Drop reject-on-short-stock; adopt the SQL-side clamp** with
   requested/applied/clamped per line (qwen) — and drop my BEGIN IMMEDIATE
   ritual with it (the single statement makes the guard unnecessary).
3. **Keep: no session revocation on email change** (2-of-3; the credential
   is verified and unchanged) — flagged for the coordinator against
   qwen's conservative mirror of change-password.
4. Split my combined `PATCH /api/auth/profile` into the peers'
   `change-email` / `change-phone` verb endpoints (repo style); phone as
   `str = ""` with ""-clears (not nullable); add staff `MemberPatch.phone`.
5. Add: keep `#/account` (no route rename), the `busy` short-circuit, the
   email-change⇒badge-unaffected pin, legacy-explicit-rotation, the
   smoke-test extension, and the optional staff badge-view endpoint.
6. My Task 3's concurrency test survives in modified form: two checkouts
   racing the last unit now assert one purchase with a clamped line
   reported, not one rejection.
