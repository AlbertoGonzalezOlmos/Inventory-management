# Plan (kimi-k3, final): Opticon M-10 — QR-code login flows + barcode shopping

Scope: the two M-10 use-cases — (1) QR-badge login with same-user 30 s
logoff countdown and cross-account save-then-switch, and (2) barcode
scan-to-cart with unknown-barcode intake and account attribution.

This is the phase-2 revision: it reconciles with `plan-glm.md` and
`plan-qwen.md`. §7 lists what was adopted from and rejected in each.

---

## 1. What already exists (do not rebuild)

Backend:
- `app/qrbadge.py` — `HCRM1:<token>` badge payload format, strict parser,
  inline-SVG QR rendering. Merged and tested (`tests/test_qr.py`).
- `User.qr_badge_hash` (SHA-256 only, indexed) + `has_qr_badge`.
- `POST /api/auth/qr-login` (badge → bearer token, uniform 401, accepts the
  raw 43-char token); `POST/DELETE /api/auth/qr-badge` (self rotate/revoke);
  `POST/DELETE /api/members/{id}/qr-badge` (staff, admin accounts protected).
- `app/barcodes.py` — GTIN validation/normalisation/classification
  (`analyze_scan` → `gtin` / `restricted` / `invalid-gtin` / `opaque`).
- `GET /api/items?barcode=` exact GTIN-14 lookup (422 on misread);
  `GET /api/items?q=` keyword search (includes barcode substring);
  `GET /api/items/vector-search` semantic search (503 when degraded).
- Scanner stack: `app/scanner/m10.py` (USB-COM driver, one-reader loop);
  `scripts/scanner_bridge.py` (exact-identity lookup `find_item`, atomic
  stock-adjust, badge scans → qr-login with the token printed to stdout,
  per-scan ApiError resilience, `--relogin`); `scripts/scanner_hid.py`,
  `scan_intake.py`, `opticon_detect.py`.
- Append-only migrations (`COLUMN_MIGRATIONS`/`INDEX_MIGRATIONS`);
  `create_all()` adds *new tables* with no migration entry.

Frontend (vendored Vue 3, hash routing, **no JS test harness** — server
tests + `scripts/ui_check.py` + manual checklist are the verification story):
- Login-page badge field: the M-10 in USB-HID mode types payload + Enter →
  `doBadgeLogin()`; Account/Members views generate/print/revoke badges.
- "Purchase list" (`localStorage["hcrm_purchase"]`): client-side only,
  **not per-user, not cleared on logout** — the next user on the same
  browser inherits it. Checkout is a placeholder toast.

Hardware reality (from qwen, `SCANNER-AGENT-SYNC.md`,
`docs/m10-unreachable-2026-10-10.md`): the dev unit currently enumerates as
the **USB-HID personality (A001)** — types scans into the focused field,
works today with zero setup. USB-COM (A002) is unverified since the xHCI
controller died; `/dev/hidraw*` is root-only until the udev rule is
installed. **Consequence (adopted): every server-side behaviour must be
testable with `TestClient` and no hardware, and the SPA must work
standalone in HID mode with no bridge.** Both paths converge on one client
handler.

## 2. What is missing vs. the task

1. No server-side cart — save-then-switch and "add to *that user's* cart"
   are impossible with a browser-local, user-agnostic list.
2. No auto-badge at account creation (`register` / `create_member` leave
   `qr_badge_hash = NULL`).
3. No scan dispatch beyond the login page: no global/capture-field routing,
   no same-user logoff screen, no 30 s countdown, no switch flow.
4. No scan-to-cart endpoint; no unknown-barcode intake (name+price screen,
   closest-match suggestions); no attribution flow for anonymous scans.
5. The bridge cannot feed a browser (prints tokens to stdout); naively
   relaying raw scans to the browser would put `HCRM1:` bearer credentials
   on a broadcast channel (qwen's decisive point — see §3).

## 3. Architecture (the load-bearing choices)

**A. Two scan-delivery paths, one client handler.**
- **HID-direct (primary, zero infrastructure — the only mode the current
  hardware supports).** The SPA captures scans itself (see §3.C) and calls
  existing/new authenticated endpoints directly (`/api/auth/qr-identify`,
  `/api/auth/qr-login`, `/api/cart/scan`). No bridge, no server bus.
- **Counter/relay (USB-COM deployments).** Adopted from qwen (design C):
  **server-mediated counter session; credentials never ride the event bus.**
  The bridge is a dumb relay: `POST /api/scanner/scans` (staff token)
  `{channel, code, symbology?}`; the *server* classifies the code and, for
  badges, resolves the owner by hash server-side and mutates the counter
  session — no badge payload or session token is ever queued, broadcast, or
  returned to subscribers. The SPA subscribes per channel with an opaque
  browser-generated `terminal_id` capability and **polls**
  `GET /api/scanner/channels/{ch}/events?cursor=&terminal=` (~700 ms, only
  on the kiosk view). Events carry only non-secret data (verdicts, item
  ids/names, public user fields, deadlines).

  Rejected alternatives: GLM's SSE relay of *raw* scans
  (`POST /api/scans` + `GET /api/scans/stream` behind a kiosk key) puts the
  raw badge payload — a bearer credential — on a channel every kiosk-key
  holder can read, against the repo's own doctrine (hashes at rest, payload
  shown exactly once). SSE also can't send an `Authorization` header
  (query-string tokens leak into logs; qwen's argument), which polling via
  the existing `api()` wrapper avoids. A headless bridge owning the session
  was rejected by all three plans (the required artefacts are screens).

**B. Server-side per-user cart (all three plans agree).** `cart_items`
table + `/api/cart` CRUD; every mutation is a server write, so "save
whatever had been done in the current user's account" is true *by
construction* at every instant. The switch flow then only awaits in-flight
requests. The legacy localStorage list is imported once on first
authenticated fetch (skipping deleted items) and retired — which also fixes
the cross-account leak.

**C. Scan capture in the SPA (reconciled).** GLM and I chose global
timing-based keystroke capture; qwen chose a dedicated always-visible
capture input (autofocus, refocus-on-blur). Final design — both, with a
strict shape gate:
- A visible **"Scan items / badge here" capture field** on catalogue and
  counter views (qwen): explicit context, anything scannable accepted,
  refocused on blur unless the user focused another input.
- **Global keydown capture** (GLM/me) for scans that arrive while focus is
  elsewhere: buffer keys with inter-key delay < ~50 ms, flush on Enter, and
  **only treat the buffer as a scan if it parses as a badge payload
  (`HCRM1:` + 43 urlsafe chars) or a valid GTIN shape (digits, valid length
  and check digit)**. Other buffered input (e.g. a fast-typed password) is
  dropped, never POSTed — this answers GLM's risk #4 (a misfire must not
  submit typed secrets as a "scan"). Opaque/SKU labels (Code 128) are NOT
  globally captured (anything would match); they go through the capture
  field. The login-page badge field keeps its exact current behaviour.

**D. Scan routing.** The SPA recognises the `HCRM1:` prefix itself;
everything else is a barcode. One pure decision core shared by server and
bridge: factor the bridge's `find_item` decision table into
`app/scanlookup.py` as `resolve_scan(code, symbology, fetch)` with an
injected fetch (GLM's idea; the bridge calls it with its HTTP `get`, the
server with a DB lookup). Qwen argued for a separate server-side helper to
avoid merging across the process boundary — rejected: both sides already
share `app.barcodes.analyze_scan` exactly this way, and the "never guess an
identity" doctrine (REVIEW-m10.md P1) is precisely the code that must not
be allowed to drift between two copies. `invalid-gtin` ⇒ misread, never
looked up, never filed.

**E. 30-second logoff: client countdown + server-authoritative deadline
(adopted from qwen).** My phase-1 plan had a client-only timer; qwen's
duality is strictly better and cheap:
- When a counter session exists (relay mode, or a HID kiosk that has bound
  a channel): the server sets `pending_logoff_at = now + 30 s`
  (`LOGOFF_GRACE_SECONDS = 30`, env-overridable for tests) and emits
  `logoff_prompt {deadline}`; the SPA counts down from the **server
  deadline** (survives poll latency); expiry is enforced **lazily** on the
  next channel touch (clear counter user, revoke adopt-issued tokens, emit
  `logged_off`) — the logoff completes even if the browser dies, with no
  background scheduler.
- A standalone HID session (no channel bound) keeps the client-side timer +
  `POST /api/auth/logout`; the server-side cart makes even an abandoned tab
  harmless.
- Timer semantics (qwen, better than my "suspend scans" idea): same-badge
  re-scan **restarts** the countdown; a **different** badge during the
  countdown cancels it and performs the switch; [Stay logged in] /
  navigating away cancels.

**F. Badge flows while logged in.**
- *HID-direct path:* the SPA needs "does this badge belong to the current
  user?" without minting a session — adopt GLM's
  `POST /api/auth/qr-identify {token}`: with the caller's bearer token
  present, respond `{same_user: bool}` (uniform 401 for bad badges, no
  other user data; logged-out callers learn nothing beyond validity). Then:
  same user → logoff screen (§E); different user → `qr-login`, save-then-
  switch (below). My phase-1 "call qr-login and compare user.id" works but
  mints+discards a token on every same-user scan — qr-identify is cleaner.
- *Relay path:* the server already knows the badge owner from the ingest;
  it sets the deadline / performs the switch and the SPA mirrors it.
- **Save-then-switch** (both paths): (i) await in-flight cart requests;
  (ii) obtain the new session FIRST (HID: `qr-login`; relay: `POST
  …/adopt` with `terminal_id` → `AuthOut`) — never log out first and fail
  second (qwen's ordering rule); (iii) revoke the outgoing session:
  channel-scoped — adopt-issued tokens are revoked server-side on switch;
  the HID kiosk calls `/api/auth/logout` on its own token. The user's
  *other devices* are untouched (same doctrine as `logout`). GLM's
  "drop, don't revoke" is rejected: kiosk tokens are disposable, and
  revoking prevents session accumulation on a shared counter; (iv)
  `saveSession`, `GET /api/cart` for the incoming user, toast "Switched to
  <name> — your list was restored"; (v) honour `must_change_password`
  exactly like `doBadgeLogin` does today.

**G. Unknown-barcode intake — role-aware (qwen's position; GLM's (a)
rejected).** `POST /api/items` stays staff-only: the staff-only catalogue
invariant is deliberate, and a scanner must not become a privilege-escalation
path. Staff/admin get the create form pre-filled with the normalised GTIN
(409 duplicate-barcode returns the conflicting item so the UI offers "add
that instead" — GLM's detail, adopted); members record name+price as an
**item request** (on the `pending_scans` row in relay mode; a lightweight
`ScanRequest` table row in HID-direct mode) — "staff have been notified".
GLM's `POST /api/items/quick` for *any* authenticated user is rejected:
provenance of a scan does not confer catalogue-write trust (a printed
barcode is public); the item-request path costs the member one toast.

**H. Attribution of scans with nobody logged in.**
- HID-direct: the pending code is stashed in SPA state; the UI asks "which
  account? — scan a QR badge or log in"; after login the stash is replayed
  (`PUT /api/cart` merge + re-POST `/api/cart/scan`). No member-name picker
  (enumeration leak; all three plans agree).
- Relay: server persists `pending_scans` rows per channel; the login page
  shows "N scanned item(s) waiting" from an unauthenticated `pending_count`
  (counts carry no secrets); after login/adopt/bind the attribution screen
  offers [Add all to my cart] (claim endpoint; idempotent) / [Leave for
  staff] (staff resolve/dismiss endpoints). GLM's `POST /api/cart/assign`
  (staff types the member's email) is rejected as redundant scope — staff
  can claim/dismiss pending rows from the staff list instead.

**I. Suggestions (qwen's version adopted, superset of mine/GLM's).** New
pure helper `prefix_candidates(gtin14)` (progressively shorter GS1
company-prefix LIKE patterns, longest-first, no wildcard injection) and
`GET /api/items/suggest?barcode=&q=&limit=` returning `{by_prefix,
by_keyword, by_semantic}`: `by_prefix` = "closest matching items using the
barcode system" (same GS1 company prefix ⇒ same brand/vendor); `by_keyword`
reuses `q=` ILIKE; `by_semantic` wraps vector-search and **degrades to `[]`
on 503** — suggestions must never fail the scan flow. Suggestions are
labelled, capped ~5, never auto-added.

**J. Concurrency & cleanup (adopted details).** Cart line add/increment is
an atomic upsert under the unique `(user_id, item_id)` key; two kiosks on
one account are documented last-writer-wins for whole-cart `PUT`; add a
concurrency test modelled on `tests/test_concurrency.py` (GLM). Cart rows
must be explicitly cleaned up on `DELETE /api/items/{id}` and
`DELETE /api/members/{id}` — FKs are declarative only in this repo
(`app/database.py` D4; qwen's catch, my phase-1 plan missed it). Stock is
NOT decremented on cart add (checkout remains a tracked non-goal — my Q4,
both peers agree); server does not clamp qty to stock, the UI keeps the
existing stock-aware hints (open question R5).

## 4. Ordered tasks

Each task is independently reviewable and leaves `uv run pytest` green.

### Task 1 — Server-side per-user cart
Files: `app/models.py`, `app/schemas.py`, new `app/routers/cart.py`,
`app/main.py`, `app/routers/items.py` + `app/routers/members.py` (delete
cleanup); tests `tests/test_cart.py` (new), extend `tests/test_items.py` /
member-deletion tests, `tests/test_migrations.py`.
- `CartItem`: `id`, `user_id` (indexed FK), `item_id`, `qty` (≥1),
  `created_at`, `updated_at`; unique `(user_id, item_id)`. New table →
  `create_all()`; no column migration.
- Endpoints (any authenticated role): `GET /api/cart` (lines + total),
  `POST /api/cart/items {item_id, qty=1}` (upsert/increment),
  `PATCH /api/cart/items/{item_id} {qty}` (0 removes; 422 on negative),
  `DELETE /api/cart/items/{item_id}`, `DELETE /api/cart` (clear),
  `PUT /api/cart` (replace — switch-flush + staging merge).
- Explicit row cleanup in item-delete and member-delete endpoints.
- Tests: CRUD round-trip; qty rules; per-user isolation; cleanup on
  item/user delete; 401 unauthenticated; `must_change_password` gate;
  concurrent increment race (no lost update).

### Task 2 — QR badge generated at account creation
Files: `app/routers/auth.py`, `app/routers/members.py`, `app/schemas.py`,
`static/app.js` + `static/index.html` (print modal); tests
`tests/test_qr.py`, `tests/test_auth.py`, member-create tests.
- `register`: generate badge in the same transaction; `AuthOut` gains
  `qr_badge: QrBadgeOut | None = None` (additive; shown exactly once).
- `create_member`: same; new `MemberCreatedOut(UserOut + qr_badge)`.
- No backfill for existing/seeded accounts (the payload can only be shown
  once at generation; a stored hash nobody can print is dead weight) —
  documented; staff issue badges from Members as today.
- Frontend: register / member-create success shows the badge in the
  existing print modal with a "this is the only time the code is shown"
  warning.
- Tests: response badge parses and immediately works with `qr-login`;
  `has_qr_badge=True` afterwards; payload appears nowhere else (`/me`,
  member lists, later logins); existing field-wise assertions stay green
  (check `test_metadata_and_ops.py` OpenAPI snapshot — may need a refresh).

### Task 3 — Scan resolution + cart-scan endpoint + suggestions
Files: new `app/scanlookup.py` (shared decision core; bridge refactored to
use it), `app/barcodes.py` (`prefix_candidates`), `app/routers/cart.py`
(`POST /api/cart/scan`), `app/routers/items.py` (`GET /api/items/suggest`);
tests `tests/test_cart_scan.py`, `tests/test_items_suggest.py`,
`tests/test_barcodes.py` (prefix shapes), `tests/test_scanner_bridge.py`
(refactor parity).
- `POST /api/cart/scan {code, symbology?}`: badge payload → 400 (badges
  never reach the catalogue); found → upsert line, `200 {status:"added",
  item, qty}`; unknown → `{status:"unknown", analysis, suggestions}`;
  misread → `{status:"misread", analysis}` (422-class, nothing added).
- `GET /api/items/suggest`: `by_prefix` / `by_keyword` / `by_semantic`
  (503-degrading) per §3.I.
- Tests: UPC-E scan finds the EAN-13-filed item; opaque SKU label; badge →
  400; invalid check digit → misread, cart untouched; unknown GTIN →
  prefix-ranked suggestions, unrelated items excluded; semantic degrades to
  `[]` with embeddings disabled.

### Task 4 — SPA scan capture + HID-direct flows
Files: `static/app.js`, `static/index.html`, `static/style.css`;
`scripts/ui_check.py` scenario additions (capture field present, badge
modal after register); backend: `POST /api/auth/qr-identify` in
`app/routers/auth.py` + tests in `tests/test_qr.py`.
- Capture field + shape-gated global capture per §3.C feeding one
  `handleScan(code)`.
- Badge while logged in: `qr-identify` → same user ⇒ 30 s logoff modal
  (countdown from server/UTC deadline, [Stay logged in], [Log off now],
  restart/cancel semantics per §3.E); different user ⇒ save-then-switch per
  §3.F (new session first, then revoke the old, then load the new cart).
- Barcode while logged in: `/api/cart/scan` → toast + cart refresh; unknown
  → intake modal (read-only normalised GTIN, name+price, grouped
  suggestions; staff create-then-add with 409-offer; member item-request);
  misread → re-scan toast.
- Barcode while logged out: stash + "which account? scan a badge or log
  in"; replay after login; one-time legacy `hcrm_purchase` import on first
  authenticated cart fetch.
- Cart panel becomes API-backed (same look; server total).
- `qr-identify` tests: `{same_user:true/false}` with bearer; 401 bad badge;
  no user data to anonymous callers.

### Task 5 — Counter sessions + relay (USB-COM path)
Files: `app/models.py` (`CounterSession`, `PendingScan`), new
`app/scannerbus.py` (in-memory per-channel event list, bounded,
single-worker constraint documented), new `app/routers/scanner.py`,
`scripts/scanner_bridge.py` (`--relay [CHANNEL]`, mutually exclusive with
`--stock-in/--stock-out/--fuzzy`, badges relayed verbatim — never exchanged
locally in relay mode); tests `tests/test_scanner_api.py`,
`tests/test_scanner_bridge.py` additions, `tests/test_migrations.py`.
- Endpoints per §3.A/E/F/H: `POST /api/scanner/scans` (staff-only ingest,
  lazy logoff expiry first), `GET …/events` (public, non-secret),
  `POST …/bind`, `POST …/adopt {terminal_id}` (capability → `_issue_token`,
  hash recorded for scoped revocation), `POST …/cancel-logoff`,
  `POST …/logoff`, `GET …/pending`, `POST …/pending/claim` (idempotent),
  staff `GET /api/scanner/pending` + resolve/dismiss.
- Tests (all TestClient, `HCRM_LOGOFF_GRACE_SECONDS=1` or monkeypatched
  clock): ingest parity with the bridge decision table; adopt with
  correct/wrong `terminal_id` (403); same-user badge → `logoff_prompt` with
  deadline ≈ now+30 s; cancel; expiry → counter user cleared, adopt token
  revoked (`/me` 401), `logged_off`; different-user badge → U's cart rows
  intact (the "save" assertion), U's adopt token revoked, V's adopt yields
  V's cart; same-badge restart / different-badge-cancels-countdown;
  no-user barcode → `pending_count` visible unauthenticated; claim lands in
  claimer's cart; channel isolation; member token cannot ingest (403).
- SPA: `#/counter` view binds the channel, polls while visible, applies
  events through the same `handleScan`/modal code as Task 4.

### Task 6 — Bridge polish + docs
- `scanner_bridge.py --relay` end-to-end, `--help`, P2 contract (per-scan
  ApiError keeps the listener alive) preserved.
- `docs/opticon-m10.md`: new "Counter/kiosk mode" section (HID-direct vs
  relay, channel/terminal model, 30 s logoff, switch semantics, pending
  attribution, udev note). `README.md`: features + endpoint table +
  `HCRM_LOGOFF_GRACE_SECONDS`. `docs/barcodes.md`: suggestion rule.
  `NOTES.md`: cart/counter model pointer.
- Hardware checklist (blocked on xHCI/udev per `SCANNER-AGENT-SYNC.md`):
  HID typing test → capture field; two printed badges (same-user countdown,
  different-user switch); one unknown barcode end-to-end.

## 5. Risks

- **R1 Hardware**: only the HID personality currently works; hence
  HID-direct is the primary path and everything is TestClient-covered
  (qwen). Owner action: udev rule + controller fix, then §Task-6 checklist.
- **R2 Global-capture false positives**: mitigated by timing + the strict
  badge/GTIN shape gate (buffered non-scan input is dropped, never POSTed);
  opaque SKU labels require the capture field. The riskiest pure-frontend
  piece; manual hardware verification required.
- **R3 Bearer credentials on the bus**: eliminated by server-side badge
  resolution (no payloads/tokens in events); anonymous `events` read still
  exposes *which codes are scanned at the counter* to LAN peers — accepted,
  documented (qwen R2).
- **R4 Timer duality**: lazy server expiry lands on the next channel touch,
  not exactly at T+30, if the browser died; adopt tokens stay valid until
  then (optional: shorter TTL for adopt-issued tokens — open question).
- **R5 Cart vs stock**: no reservation; two carts can exceed stock. Open
  question: clamp server-side (GLM) or UI-warn only (qwen/me — default).
- **R6 Single-worker bus**: in-memory events assume one uvicorn worker
  (matches `run.sh`); multi-worker needs a DB-backed bus — out of scope.
- **R7 Register/member-create now return a printable secret** once; never
  logged; additive schema change — verify the OpenAPI snapshot test.
- **R8 Legacy purchase import** may reference deleted items — skipped, then
  the key is cleared.
- **R9 `must_change_password` after a badge switch** lands on the forced
  change screen mid-flow — existing behaviour; the switch toast must say so.

## 6. Open questions for the coordinator

1. R5: server-side stock clamp on cart qty? (default: no, UI warns)
2. R4: shorter TTL for adopt-issued kiosk tokens? (default: keep 7-day TTL,
   revocation is channel-scoped)
3. Counter channel granularity: one channel per physical counter, latest
   bind wins, previous terminal gets a "taken over" event (qwen's model) —
   confirm.
4. Confirm member item-request (not member item creation) — all-but-GLM
   position.

## 7. What I adopted from / rejected in each peer

**From qwen (adopted — the largest influence):**
- Design C: server-mediated counter session with server-side badge
  resolution — credentials never ride the event bus. This killed both my
  phase-2 "SPA polls raw scan events" idea and GLM's SSE relay (raw
  `HCRM1:` payloads on a broadcast channel are a credential harvest).
- Polling instead of SSE (EventSource can't send `Authorization`; query
  tokens leak).
- Server-authoritative 30 s deadline with lazy expiry + channel-scoped
  revocation of adopt-issued tokens; countdown rendered from the deadline.
  My phase-1 timer was client-only.
- Switch ordering: obtain the new session (adopt/qr-login) BEFORE dropping
  the old one.
- Countdown semantics: same-badge re-scan restarts; different badge cancels
  and switches (replaces my "suspend scans during the modal").
- Hardware reality (HID-only unit, udev) → HID-direct primary, everything
  TestClient-testable; capture-field as the explicit scan affordance.
- Explicit cart-row cleanup on item/user delete (FKs are declarative only —
  I missed this); `pending_scans` + `pending_count` attribution flow;
  semantic suggestions degrade to `[]` on 503; no badge backfill rationale;
  relay/stock-flag mutual exclusion; badge never exchanged locally in relay
  mode; OpenAPI snapshot risk.
- Rejected from qwen: a fully separate server-side scan-resolution helper
  duplicating the bridge's `find_item` — I share one injected-fetch core
  (`app/scanlookup.py`) so the no-guessing doctrine cannot drift (same
  sharing pattern as `app.barcodes`).

**From glm (adopted):**
- `POST /api/auth/qr-identify` (same-user check without minting a session)
  for the HID-direct path — cleaner than my compare-after-qr-login.
- Factoring the bridge's decision core into a shared `app/scanlookup.py`
  (see above).
- Global keydown capture with timing heuristics (kept, but gated on
  badge/GTIN shape — his own risk #4 — and paired with qwen's capture
  field).
- 409 duplicate-barcode returning the conflicting item so the UI can offer
  it; concurrency test modelled on `tests/test_concurrency.py`;
  `MemberCreatedOut` naming; polling/SSE docs caveats (moot — polling won).
- Rejected from glm: `POST /api/items/quick` for *any* authenticated user
  (weakens the deliberate staff-only catalogue invariant; a printed barcode
  is public provenance — members file item requests instead); SSE raw-scan
  relay behind a kiosk key (credential-on-the-bus, §3.A); "drop, don't
  revoke" the outgoing kiosk token on switch (kiosk tokens are disposable;
  channel-scoped revocation matches the logout doctrine and prevents
  session accumulation); `POST /api/cart/assign` staff-email picker
  (redundant with staff pending-row management).

**Kept from my phase-1 plan:** server-side per-user cart as the foundation;
HID as primary transport; unified server-side classification via
`analyze_scan`; role-aware intake; no stock mutation on cart add; no member
enumeration picker; no seed-account badges; PR slicing (cart → badge-on-
create → scan endpoint+suggestions → SPA → relay).

## 8. Sequencing rationale / PR slicing

Task 1 (cart) underpins every "save"; Task 2 is independent and small;
Task 3 is the shared scan brain; Task 4 ships the full HID-direct
experience (the only hardware-verifiable path today) on 1–3; Task 5 adds
the relay/counter machinery; Task 6 closes out. Reviewable as PRs:
(1) cart backend, (2) badge-on-create, (3) scan resolution + suggestions +
cart-scan, (4) SPA HID flows, (5) counter/relay, (6) bridge + docs.
