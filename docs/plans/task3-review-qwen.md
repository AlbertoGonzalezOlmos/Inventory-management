# Task 3 review (qwen3.8-max) — peer proposals for scan resolution, `/api/cart/scan`, suggestions, ScanRequest

Reviews `docs/plans/task3-proposal-glm.md` and `docs/plans/task3-proposal-kimi.md`
against `docs/plans/task3-proposal-qwen.md`, the coordinator's Task 3
decisions, and the current tree. Claims re-verified against code this pass
(not taken from any proposal):

- `tests/test_scanner_bridge.py` pins `get.calls == ["/api/items?barcode=…"]`
  (GTIN), `== ["/api/items?q=…&limit=100"]` (opaque, non-fuzzy) and
  `== []` (misread) — but the **fuzzy test asserts no call list**
  (`test_partial_word_matches_only_under_explicit_fuzzy` checks only
  `how`/`item`). A double-fetching adapter would pass the suite silently.
- `split_addon()` strips whitespace and splits supplements **only for the
  GTIN symbology family** ("an opaque payload (e.g. Code 128 `BOX 12`) may
  legitimately contain spaces"). Consequence: for `kind == "opaque"`,
  `analysis.key == analysis.raw == code.strip()` — always. The unstripped
  caller argument is the only variant that differs (whitespace-padded
  input), and today's bridge compares against exactly that (`code.lower()`).
- `scripts/scanner_bridge.py` docstring: "**Stdlib only**, except the
  optional pyserial" — a documented contract; the bridge today imports only
  `app.barcodes` and `app.scanner` (both stdlib-pure).
- `list_items`' `q=` filter escapes `\ % _` and ORs ilike over
  name/description/sku/**barcode**; `create_item` 503s when embeddings are
  down and 409s duplicate barcodes with a string detail.
- `app/routers/cart.py::_upsert_line(session, user_id, item_id, qty) ->
  bool` (commits, caps at `QTY_CAP`) and `_cart_out(session, user)`.

§4 is the merged recommendation; §5 the changes to my own proposal.

---

## 1. Review of `task3-proposal-glm.md`

### Strengths (adopt)

- **S1 — The `ItemResolver` Protocol + resolver-object shape.** A named
  three-method protocol (`by_barcode`/`by_sku`/`by_keyword`) with the
  doctrine ("`by_keyword` is only ever called with fuzzy=True; the server
  never passes it") stated on the protocol is more self-documenting than my
  keyword-callback signature, and the bridge adapter's memo cache fits
  naturally as resolver state. Adopt the shape (kimi converged on it
  independently — 2-of-3).
- **S2 — The delegation spy test**: "add one explicit test that the bridge
  module's `find_item` calls `scanlookup.resolve_scan` (spy) so the
  delegation cannot be silently re-inlined." Cheap and exactly the
  regression this repo's culture pins (the point of decision 8 is that the
  core is *the* implementation; a future bridge edit could quietly fork it
  back). Adopt.
- **S3 — Nested `analysis{…info{…}}` serialisation in the 404 detail**,
  mirroring the frozen dataclasses (`ScanAnalysis` → `BarcodeInfo`) with a
  small explicit-fields helper. Less mapping code and less invention than my
  flattened `ScanAnalysisOut`; the notes list rides along. Adopt (as a
  pydantic pair `ScanAnalysisOut{kind,key,addon,info: BarcodeInfoOut|None}`
  so responses stay schema-typed).
- **S4 — `resolve` grows an `add_to_cart` option** — this silently
  implements the coordinator's D5 *secondary attribution path* ("staff can
  assign via a secondary path") with **no extra endpoint**: staff resolving
  a request can drop the item into the filer's cart via Task 1's
  `_upsert_line`. Genuinely good scope economics. Adopt the flag — but
  **default `false`**, not glm's `true`: a staff action silently writing
  into a member's durable cart is a surprising side effect (the member may
  have left; the cart is per-user and persists), and Task 5's staff UI can
  offer an explicit checkbox. Coordinator flip is one default.
- **S5 — `SessionItemResolver.by_sku` via direct `lower(sku) ==` SQL** with
  the parity note ("equivalent to the bridge's q=-page exact compare,
  without depending on the search endpoint's escaping") and the risk-table
  entry asking a reviewer to look at both once. Correct semantics, honest
  flag. (My proposal made the same choice without the explicit parity note;
  adopt the note.)
- **S6 — Risk-table completeness**: 400-vs-422 rationale for the badge
  gate, symbology trust model ("HID sends `''` and gets content-based
  classification — same trust as the bridge's `--symbology`"), 404 payload
  cost (two indexed queries per unknown scan), and the escape hatch ("the
  404 can drop embedded suggestions and let the modal fetch `/suggest` —
  the shape supports both"). Adopt into the merged risk list.
- **S7 — `code` max_length 64** with the arithmetic (GTIN+addon 19,
  `HCRM1:` payload 49, `QrLoginIn.token` precedent 64). Beats my 128 —
  adopt.

### Weaknesses / risks / bugs (avoid or fix)

- **W1 — `_HttpResolver` double-fetches on the fuzzy path.** `by_sku` and
  `by_keyword` each issue their own `GET /api/items?q=…&limit=100`; an
  opaque miss with `--fuzzy` fires the *same* query twice. Today's bridge
  issues it once and reuses the page — and because the fuzzy test asserts no
  `get.calls` (verified above), **the suite would not catch the regression**;
  it would only show up as doubled HTTP on a production counter. Fix: the
  HTTP lookup must memoise per `(text, limit)` (my proposal's cache; make it
  resolver state per S1). Add a call-count assertion to the fuzzy parity
  case so this can never regress silently again.
- **W2 — `SessionItemResolver.by_keyword` diverges from the house `q=`
  semantics**: it strips `%`/`_` instead of backslash-escaping them (so
  `q="50%"` searches `"50"` rather than literal `50%`) and omits
  `Item.barcode` from the OR (the `?q=` contract deliberately includes
  barcode substring matches — a keyboard-mode scanner typing into search).
  It is dead code on the server (fuzzy never enabled) — which is exactly the
  problem: a divergent copy of house semantics sitting in the *shared core
  module* is a drift trap the whole task exists to prevent. Fix: extract
  `list_items`' filter into one helper (`keyword_filter(q)`, kimi's
  `item_search_filter`) and have every consumer — `list_items`, suggest,
  the server resolver — call it. One keyword dialect, zero copies.
- **W3 — `analysis.raw` rationale is wrong (conclusion harmless).** The
  parenthetical claims `.raw` is "the addon-stripped, trimmed payload …
  `analyze_scan` strips/normalises into `.raw`". Not so: `.raw` is the
  trimmed *input*; the addon-stripped base is `.key`. For opaque scans the
  two are provably equal (`split_addon` only splits the GTIN family —
  verified), so behaviour is unaffected, but a shared-core docstring that
  misdescribes the dataclass will mislead the next editor. Use `.key` (the
  semantically-named field) and state the equality.
- **W4 — `DbLookup`/`SessionItemResolver` inside `app/scanlookup.py` breaks
  the bridge's documented dependency contract.** The bridge must import
  `resolve_scan` from that module; if the module also hosts the ORM backend,
  the bridge transitively imports `sqlmodel` + `app.models` — against its
  "Stdlib only, except the optional pyserial" docstring (verified). It would
  *run* (project venv), which is why this slips through review; it is
  nonetheless a contract regression on the repo's most deployment-sensitive
  script. Fix: `scanlookup.py` stays stdlib-pure (core + Protocol only);
  the session backend lives server-side (§4: `app/scanflow.py`). kimi's
  proposal has the identical flaw.
- **W5 — No duplicate-open handling on `ScanRequest` POST.** A kiosk
  double-submit files two identical open requests; staff queue noise. Both
  my 409 and kimi's idempotent-200 beat silence; see §2 S5 for the verdict.
- **W6 — `assert` in `prefix_candidates`** (`assert gtin14.isdigit() …`):
  asserts are stripped under `python -O`; the house pattern for bad input
  in `app/barcodes.py` is `raise ValueError` (`classify_gtin`) or return
  `None` (`normalize_input_gtin`). kimi's ValueError version is correct.
- **W7 — The prefix ladder misses case-code/retail siblings** (shared with
  kimi; the full argument and worked example are in §2 W1 — it is the one
  algorithmic disagreement between the merged recommendation and *both*
  peers, so it is stated once there).
- **W8 — No `GET /mine`**: a member cannot see whether their filed request
  was resolved — Task 5's "reported to staff" UX then has no backend to
  show status. Mine and kimi both include it (2-of-3); cheap, scoped, keep.
- **W9 — ScanRequest lacks `updated_at`** (kimi and I both have it): status
  transitions without timestamps make the staff queue un-sortable by
  activity and the audit trail thinner. Minor; adopt kimi's pair.

### Disagreements with my proposal, and verdicts

| Topic | glm | mine | Verdict |
|---|---|---|---|
| Core API shape | Protocol resolver object | keyword callbacks | **glm/kimi** (S1) — I concede |
| HTTP adapter caching | none (W1 double-fetch) | memoised | **Mine**, as resolver state |
| Server keyword search | divergent inline copy (W2) | shared extracted filter | **Mine+kimi** |
| Backend module placement | inside `scanlookup.py` (W4) | callbacks inline in `cart.py` | **Neither** — merged: pure core + `app/scanflow.py` (§4) |
| `resolve` side effect | `add_to_cart=true` default | no flag | **Merge**: flag yes (S4), default **false** |
| 404 detail | nested analysis, no status discriminator | flat + `status`/`catalogable` | **Merge**: nested analysis (S3) + `status` + `catalogable` (§4) |
| code max length | 64 | 128 | **glm** (S7) |
| Already-catalogued at filing | 409 + item | 409 + item | agree (vs kimi's permissive — §2 W4) |

## 2. Review of `task3-proposal-kimi.md`

### Strengths (adopt)

- **S1 — The cross-backend parity suite is the review's best idea.** One
  shared case list (canonical GTIN, UPC-E→EAN-13, bare-6 never guessed,
  opaque case-variants, "water" no-match, fuzzy opt-in, misread
  never-looked-up, restricted exact) executed against **both** `DbLookup`
  on the test DB **and** the bridge's HTTP lookup over the existing
  `fake_api` — "same input, same verdict, both backends: the no-drift
  guarantee made executable." My "bridge tests stay green = parity" only
  pins the bridge side; glm's spy test only pins delegation. kimi's suite
  pins the *equivalence*, which is the actual coordinator decision. Adopt
  as the centrepiece of `tests/test_scanlookup.py` (parametrised), extended
  with glm's spy test and a whitespace-padded opaque case (see W2).
- **S2 — R6 drift-watch note**: the parity case list must grow whenever
  `analyze_scan` gains a symbology/edge case — one line in the
  `scanlookup.py` docstring. Adopt verbatim.
- **S3 — Lifecycle-closing integration test**: after staff create the item
  and resolve the request, `POST /api/cart/scan` of the original code now
  returns `added`. That single test pins the whole Task-3 story end to end.
  Adopt.
- **S4 — `resolved_item_id` NULLed by `delete_item`** ("take the UPDATE;
  history survives, no dangling references"): one statement in the existing
  cleanup block makes the reference *live* for every reader, while
  `status`/`resolved_by`/`resolved_at` preserve the audit trail. Better
  than my weak-reference + tolerant-join (which burdens every reader
  forever) and better than glm's dangling-int (which preserves an id nobody
  can display). Adopt — I concede this point.
- **S5 — Idempotent duplicate filing**: same user + same code with an open
  request → **200 with the existing row**, not an error. A kiosk
  double-submit is a no-op the UI needs zero handling for; my 409
  `duplicate_open` makes a benign repeat look like a failure. Adopt kimi's
  semantics (with the response distinguishable: 201 created vs 200
  existing).
- **S6 — `item_search_filter(q)` extraction** with the explicit reason ("so
  `DbLookup.search` and the endpoint cannot drift on wildcard-escaping
  either") — the same anti-drift doctrine applied *below* the decision
  table. Adopt (this is the fix for glm W2; naming: `keyword_filter`).
- **S7 — Migration-style assertion for the new table** (`scan_requests`
  appears after `create_all()` on a legacy-shape DB, Task-1 cart-test
  style). My proposal omitted it; cheap. Adopt.
- **S8 — `price_cents: int | None`** on the filing schema: `None` = member
  didn't provide a price, `0` = stated free — conflation-free. Adopt (keep
  `name` required, per the task's "enter the item name and price" screen).
- **S9 — `_flagged_token` moved to `conftest.py`** for reuse across
  test_cart/test_cart_scan/test_scan_requests. Adopt.
- **S10 — R4 query-budget note** (scan endpoint issues ≤ 2 queries before
  the upsert; the stock-adjust `BEGIN IMMEDIATE` machinery remains
  *unjustified* here, same argument as Task 1) — the right explicit
  non-parallel to draw for reviewers. Adopt into the merged docs.

### Weaknesses / risks / bugs (avoid or fix)

- **W1 — The prefix ladder misses the case-code/retail sibling** (the
  algorithmic disagreement with *both* peers). `prefix_candidates(gtin14)`
  LIKE-matches the **full 14-digit string**, whose first digit is the
  GTIN-14 *indicator*. Worked example: a shop files retail EAN-13
  `8412345000123` as `08412345000123`; goods-receiving scans the case code
  ITF-14 `18412345000129` (indicator 1 — same company `8412345`, same item
  reference). Ladder candidates all start `1841234…`; the stored barcode
  starts `0…` → **zero prefix suggestions** for the scan whose sibling the
  shop demonstrably stocks. Case codes at receiving are exactly what the
  bridge's `--stock-in` flow exists for, so this is a realistic miss, not a
  corner. Fix (mine, refined): rank by shared-prefix length on the
  **indicator-stripped 13-digit view** (`gtin14[1:]`) — the case code's view
  `8412345000129` shares **12 digits** with the retail view `8412345000123`
  → top-ranked sibling. This also **dissolves kimi's Q2 dilemma** (padded
  grouping "cross-matches formats or loses siblings" — false dichotomy):
  stripped views keep format-family separation (GTIN-8 views carry the
  5-digit pad, UPC-A views one pad zero, EAN-13 none — cross-family pairs
  share ~0 and never suggest) *and* unite packaging levels of the same
  product. Cost: ranking is Python over `(id, barcode)` pairs (one
  indexed-column fetch) instead of index-backed LIKEs — trivial at shop
  scale, escape hatch documented. Floor `min_shared=6` as a tunable
  constant; ties by `id`; pin all of it with tests (both peers' ladder
  tests translate 1:1 plus the case-code example).
- **W2 — The core passes the *unstripped caller argument* to `by_sku`**
  (`lookup.by_sku(code)`), replicating today's bridge whitespace bug:
  `" EX-007 "` from a padded scan never matches SKU `EX-007`. Since
  `analyze_scan` already computed the trimmed identity in `.key` (== `.raw`
  for opaque, proven above), the core should pass **`analysis.key`** — a
  strict improvement that is invisible for every clean input. Add the
  padded-whitespace case to the parity suite (S1) so both backends pin it.
- **W3 — `DbLookup` in `scanlookup.py`** — same bridge stdlib-footprint
  breach as glm W4; same fix (§4 module split).
- **W4 — Permissive filing of already-catalogued codes** (POST succeeds,
  response merely carries `existing_item`): every race between a member's
  404 and staff creating the item lands a request in the staff queue that
  staff must then dismiss — manufactured queue noise. A server-side
  `resolve_scan` at filing time already runs (kimi keeps the classification
  snapshot), so the hit is free: answer **409 `already_catalogued` + the
  matched item** (mine + glm, 2-of-3) and let the UI offer "add that
  instead" in one round-trip.
- **W5 — Flat merged `suggestions: [ItemOut…]`** ("prefix topped up with
  keyword") loses *why* each item was suggested; the Task-5 modal wants to
  label groups ("same brand" vs "name match"), and the groups have different
  trust levels (prefix siblings are identity-adjacent; keyword-on-code hits
  can be odd substrings — glm's risk 6). Keep grouped `by_prefix` /
  `by_keyword` (mine + glm, 2-of-3).
- **W6 — `CartScanIn` without `qty`** (always upsert 1): keypad flows
  ("scan once, enter 5") then need a second PATCH round-trip at the
  counter. Mine + glm include bounded `qty` (2-of-3); it costs one field
  and reuses `CART_QTY_*` bounds. Keep.
- **W7 — `ScanRequestOut.filer {id, name}` only for the staff list** —
  asymmetric shapes per consumer complicate the SPA; a single out-schema
  with flat `user_name`/`user_email` (populated for staff, self for /mine)
  is simpler. Minor; merged shape in §4.
- **W8 — String-detail 422 for misread** while the 404 is structured: the
  scan surface should speak one envelope language (`status` discriminator
  across added/unknown/misread/badge) so the SPA branches uniformly. Keep
  structured (mine + glm).

### Disagreements with my proposal, and verdicts

| Topic | kimi | mine | Verdict |
|---|---|---|---|
| Duplicate open filing | 200 idempotent | 409 `duplicate_open` | **kimi** (S5) — I concede |
| `resolved_item_id` lifecycle | FK + NULL on item delete | weak ref + tolerant join | **kimi** (S4) — I concede |
| Opaque SKU key | unstripped `code` (W2) | `analysis.key` | **Mine** |
| Already-catalogued filing | permissive + `existing_item` | 409 + item | **Mine+glm** (W4) |
| Suggestion grouping | flat merged | grouped | **Mine+glm** (W5) |
| `qty` on scan-in | absent | present | **Mine+glm** (W6) |
| Prefix algorithm | ladder on full gtin14 (W1) | stripped-view shared-prefix ranking | **Mine** — with kimi's ladder *tests* translated |
| Price nullability | `int \| None` | `int` required | **kimi** (S8) |
| Parity suite | both backends, one case list | bridge-tests-unchanged only | **kimi** (S1) — I concede, biggest test-design upgrade |

## 3. Three-way convergence (coordinator fast-path — no debate needed)

Pure `app/scanlookup.py` core owning the decision table (gtin/restricted →
exact canonical barcode; opaque → exact case-insensitive SKU; invalid-gtin/
empty → **never looked up**; fuzzy strictly opt-in and bridge-only — the
server never guesses); `find_item` reduced to a signature- and
return-identical adapter so `tests/test_scanner_bridge.py` passes
**unchanged** and thereby becomes the bridge-side parity pin; badge gate
**first** in `/api/cart/scan` (400, prefixed payloads only — a raw 43-char
token is an ordinary opaque miss, matching the bridge and `qr-login`'s
division of labour); misread → 422 with no suggestions; unknown → **404
with structured detail** (repo status-code precedent: `get_item` 404,
`?barcode=` 422) carrying the classification and suggestions; match →
200 with the **full `CartOut`** echo (Task-1 contract), `_upsert_line` as
the only cart write, **stock untouched** (decision 6); suggestions live in
**one** assembler used by both `GET /api/items/suggest` and the 404 (no
second brain); **semantic degrades to `[]`, never 503** (decision 7) while
`/api/items/vector-search` keeps its own 503 byte-for-byte; **no semantic
suggestions on the raw code** (digits carry no text to embed — semantic is
for the typed-name flow); `ScanRequest` = new `create_all()` table, **no
migration entries**, classification snapshotted **server-side at filing**
(never trusted from the client), misreads never filed (422), badges never
filed (400), staff-only list/resolve/dismiss with 409 on non-open
transitions, two-step resolution (item creation stays on the tested
staff-only `POST /api/items` path — no second creation pipeline);
`delete_member` cleans requests (D4 pattern); `conftest` gains nothing
except `_flagged_token` (kimi S9); `test_migrations.py` exact-set test
untouched; docs: `docs/barcodes.md` suggestion-rule paragraph +
`docs/opticon-m10.md` §6a pointer.

## 4. Final recommended shape (standardise on this)

**Modules** (the one structural decision neither peer got right):

- **`app/scanlookup.py` — pure core, stdlib + `app.barcodes` only** (the
  bridge's documented dependency contract stays intact): `Lookup` Protocol
  (`by_barcode(gtin14)`, `by_sku(code)` exact+case-insensitive,
  `search(text, limit)` fuzzy-only), `ScanVerdict(analysis, item, how)`,
  `resolve_scan(code, lookup, *, symbology="", fuzzy=False)`. The opaque
  path passes **`analysis.key`** (W2-fix; equals `.raw` for opaque, equals
  today's bridge behaviour for every clean input, and additionally fixes
  whitespace-padded scans). Docstring carries kimi's R6 drift-watch line
  and glm's P1 doctrine paragraph.
- **`app/scanflow.py` — server-side companion** (imports sqlmodel/models;
  imported by the routers, never by scripts): `SessionLookup`
  (`by_barcode` = `Item.barcode ==`; `by_sku` = `func.lower(Item.sku) ==
  code.lower()`; `search` = the extracted `keyword_filter`), the
  `keyword_filter(q)` extraction from `list_items` (escape semantics
  preserved exactly — existing item tests are the gate), `vector_search_rows`
  extracted from `vector_search` (endpoint keeps its 503 mapping; suggest
  catches everything → `[]`), `suggest_items(session, *, gtin14=None, q="",
  limit=5) -> {by_prefix, by_keyword, by_semantic}`, `analysis_out(analysis)`
  serialiser, and `scan_request_verdicts` helpers shared by cart-scan and
  ScanRequest filing. No router→router imports anywhere.
- **`app/barcodes.py`** (pure): `common_prefix_len(a, b)`,
  `SUGGEST_MIN_SHARED = 6`, `rank_prefix_siblings(gtin14, stored_pairs)` —
  **indicator-stripped** (`gtin14[1:]`) shared-prefix ranking, floor 6,
  shared-length DESC then id ASC (W1-fix; the case-code/retail example goes
  in the docstring and `docs/barcodes.md`). `prefix_candidates` (the peers'
  ladder) is **not** needed by this design; if retained for anything, it
  raises ValueError on bad input, never `assert` (glm W6).
- **`scripts/scanner_bridge.py`**: `find_item` → adapter with a
  **memoising `_HttpLookup`** (one `q=` fetch shared by `by_sku` and
  `search` — glm W1 fix; the memo lives on the lookup instance), returning
  `(verdict.item, verdict.how, verdict.analysis)`. Verdict strings, the
  UPC-E hint, and `--fuzzy`/stock-adjust refusal stay in `main()`/`on_scan`
  untouched.

**`POST /api/cart/scan`** (`app/routers/cart.py`, `get_current_user`, any
role; gate inherited): `CartScanIn(_Stripped) {code: 1..64, symbology: ""
(≤32), qty: 1..CART_QTY_MAX = 1}`. Order: badge → **400**;
`resolve_scan(code, SessionLookup(session), symbology=…)` (fuzzy never);
misread → **422**; match → `_upsert_line` → **200**; miss → **404**.
One envelope language — every body (including errors' `detail`) carries a
`status` discriminator:

- 200 `CartScanOut {status:"added", match:"barcode"|"sku", item:ItemOut,
  qty, cart:CartOut}`
- 404 `detail {status:"unknown", message (bridge verdict strings verbatim,
  incl. the UPC-E symbology hint for 6–8-digit opaques), code, symbology,
  analysis: ScanAnalysisOut{kind,key,addon,info: BarcodeInfoOut|null},
  catalogable: kind∈{gtin,restricted}, suggestions:{by_prefix:[ItemOut],
  by_keyword:[ItemOut]}}`
- 422 `detail {status:"misread", message, code, analysis}` — no suggestions,
  nothing stored
- 400 `detail {status:"badge", message}`
- pydantic 422 (list detail) for shape violations — clients branch on
  `isinstance(detail, dict) and detail.get("status")`; documented (Task 5
  needs to know).

**`GET /api/items/suggest?barcode=&q=&limit=`** (any authenticated user;
`limit` default 5, ≤ 25): invalid `barcode` → 422 (the `?barcode=`
precedent); response `{by_prefix:[ItemOut], by_keyword:[ItemOut],
by_semantic:[VectorSearchOut]}` — similarity kept on the semantic group
(existing schema, useful ordering signal; glm's "uniform ItemOut"
simplification rejected: it costs a schema and throws away data).
by_semantic requires `q` ≥ 2 chars and **degrades to `[]`** on every
failure path.

**`ScanRequest`** (`app/models.py`, table `scan_requests`, create_all only):
`id`, `user_id` FK indexed, `code` (≤64), `symbology`, `kind`, `gtin14|None`,
`name` (1..255, required), `price_cents: int|None` (kimi S8), `status`
("open"|"resolved"|"dismissed", indexed), `resolved_item_id: int|None`
(declared FK, **NULLed by `delete_item`** — kimi S4), `resolved_by`,
`resolved_at`, `created_at`, `updated_at`.

**`app/routers/scanrequests.py`** (prefix `/api/scan-requests`):

| Endpoint | Auth | Behaviour |
|---|---|---|
| `POST ""` | any user | badge → 400; `analyze_scan` server-side: invalid-gtin/empty → 422 ("misreads are never filed"); code resolves now → **409 `{status:"already_catalogued", item}`** (W4 verdict); same user + same code with an **open** request → **200 with the existing row** (idempotent, kimi S5); else 201, classification snapshotted |
| `GET "/mine"` | any user | own, newest first, all statuses |
| `GET ""?status=open\|resolved\|dismissed\|all` | **staff** | default `open`, oldest first; rows carry flat `user_name`/`user_email` (staff may list members anyway) and `resolved_item: ItemOut\|None` |
| `POST "/{id}/resolve"` `{item_id, add_to_cart=false}` | **staff** | 404 unknown request/item; 409 not open; sets status/`resolved_item_id`/`resolved_by`/`resolved_at`; `add_to_cart` → `_upsert_line(session, request.user_id, item_id, 1)` — the D5 staff-assign path folded in (glm S4, default flipped to false) |
| `POST "/{id}/dismiss"` | **staff** | 404; 409 not open |

Cleanup: `delete_member` += the user's requests (same transaction);
`delete_item` += `UPDATE scan_requests SET resolved_item_id = NULL WHERE
resolved_item_id = :id`.

**Tests** (merged superset; files as all three proposed):
`tests/test_scanlookup.py` — **the parity suite** (kimi S1): one shared
case list executed against `SessionLookup` (test DB) *and* `_HttpLookup`
(`fake_api`), cases = canonical GTIN, UPC-E→EAN-13, bare-6 never guessed,
opaque exact-SKU incl. case variants **and whitespace-padded input** (W2
pin), "water" no-match, fuzzy opt-in first-hit **with a one-fetch call-count
assertion** (glm W1 pin), misread/empty → zero lookup calls (raising
stubs), restricted exact; plus the **delegation spy** (glm S2).
`tests/test_scanner_bridge.py` — **unchanged, green**.
`tests/test_cart_scan.py` — badge 400 / raw-token opaque miss; added for
both match kinds with full-cart echo and re-scan increment; UPC-E identity;
misread 422 (nothing stored, no suggestions); unknown GTIN 404 (detail
shape, `catalogable:true`, region/usage, by_prefix contains the
same-company sibling and **excludes** unrelated prefixes, by_keyword for
opaque); **the case-code/retail sibling suggestion pin** (W1 example);
qty>1 and bounds; stock untouched; 401/403 (shared `_flagged_token` from
conftest, kimi S9); isolation.
`tests/test_items_suggest.py` — ranking/floor/tie/indicator-strip rules;
wildcard-escape parity with `?q=`; semantic with fake embeds; **degrade to
[]** on `embed_text→None` and on vector-init failure (never 503); invalid
barcode 422; limit bounds; auth; member role allowed.
`tests/test_scan_requests.py` — filing (snapshot fields server-computed;
client-forged `kind` ignored), misread 422, badge 400, already-catalogued
409+item, duplicate-open 200-existing, distinct users both file; `/mine`
scoping; staff list + status filter; member 403 on staff list; resolve
(links, `add_to_cart` both ways, 404s, 409 not-open, double-resolve 409);
dismiss; **lifecycle close: after resolve, `/api/cart/scan` of the code
returns `added`** (kimi S3); cleanup — member delete removes requests
(direct DB), item delete NULLs `resolved_item_id` and the staff list
survives; `scan_requests` appears via `create_all()` on a legacy-shape DB
(kimi S7).
`tests/test_barcodes.py` — `common_prefix_len`, `rank_prefix_siblings`
(floor, ties, ordering, stripped-view rules, NULL/empty inputs).
Suite hygiene: `test_migrations.py` exact-set test untouched;
`test_metadata_and_ops.py` green (no schema snapshot — verified in Task 2).

**PR sequencing** (each step green): ① core + bridge adapter + parity/spy
tests (existing bridge suite is the acceptance gate); ② barcodes ranking
helpers + tests; ③ `app/scanflow.py` extractions (`keyword_filter`,
`vector_search_rows` — behaviour-identical, existing item tests are the
gate); ④ `POST /api/cart/scan` + tests; ⑤ `GET /api/items/suggest` +
tests; ⑥ ScanRequest model/router/cleanups + tests; ⑦ docs (`docs/
barcodes.md` rule incl. indicator-strip rationale; `docs/opticon-m10.md`
§6a shared-core pointer).

## 5. Changes to my own proposal (self-review)

1. **Callbacks → `Lookup` Protocol object** (glm S1 / kimi) — conceded;
   better contract surface, and the memo cache becomes adapter state.
2. **Duplicate-open filing: 409 → 200-with-existing** (kimi S5) —
   conceded; benign kiosk repeats must not look like errors.
3. **`resolved_item_id`: weak ref → declared FK NULLed by `delete_item`**
   (kimi S4) — conceded; live invariant for readers, audit fields survive.
4. **`code` max_length 128 → 64** (glm S7); **`price_cents` → nullable**
   (kimi S8); **`updated_at` added**; **router file `scanrequests.py`**
   (peers' naming, house single-word style).
5. **404/analysis detail: nested `analysis{…info{…}}`** (glm S3) keeping my
   `status` discriminator and `catalogable` flag; grouped suggestions kept
   (kimi W5 rejected).
6. **Tests adopted**: cross-backend parity suite + whitespace case (kimi
   S1/W2), delegation spy (glm S2), lifecycle-close (kimi S3), legacy-DB
   table assertion (kimi S7), `_flagged_token` → conftest (kimi S9),
   fuzzy one-fetch call-count pin (new, from glm W1).
7. **`resolve` gains `add_to_cart: bool = false`** (glm S4, default
   flipped) — implements the D5 secondary assign path with no new endpoint.
8. **Module split made explicit**: my "callbacks inline in cart.py" becomes
   `app/scanflow.py` — needed because ScanRequest filing is a second server
   consumer (router→router imports avoided) and because both peers'
   "DbLookup in scanlookup.py" breaches the bridge's stdlib-only contract
   (W4/W3).
9. **Kept against peers**: `analysis.key` for the opaque SKU comparison
   (kimi W2 — with the proven `.key == .raw`-for-opaque note correcting glm
   W3); 409 already-catalogued at filing (kimi W4); `qty` on `CartScanIn`
   (kimi W6); structured dict envelope with `status` on every scan-surface
   error (kimi W8); similarity-carrying `by_semantic` (glm's uniform-ItemOut
   simplification rejected); indicator-stripped ranking over the ladder
   (both peers' W1/W7 — with the worked case-code example as the deciding
   argument).

## 6. Verdict

No proposal contains a shipping blocker, but each of the three would have
shipped something the merged shape fixes: **glm** a silent double-fetch and
a divergent keyword dialect in the shared module; **kimi** a whitespace bug
carried into the new core, queue-noise filings, and (with glm) the prefix
ladder that misses case-code/retail siblings — the exact goods-receiving
population the bridge exists for; **mine** a weaker parity-test design,
router-import smell, and three over-strict contract choices (duplicate 409,
weak ref, 128-char codes) that the peers correctly soften. The merged
specification in §4 — pure `scanlookup.py` + server-side `scanflow.py`,
memoised bridge adapter, `analysis.key` semantics, indicator-stripped
sibling ranking, one envelope language, idempotent filing with
already-catalogued 409, `add_to_cart=false` on resolve, and the
cross-backend parity suite as the executable no-drift guarantee — is what I
recommend the coordinator standardise on. Open choices genuinely left to
the coordinator: `add_to_cart` default (glm says true, I say false),
duplicate-filing status distinction (200-vs-201 body marker), and whether
`GET /mine` rides in Task 3 (recommend yes; two peers include it).
