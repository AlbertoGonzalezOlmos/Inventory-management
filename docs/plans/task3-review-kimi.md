# Task 3 proposal review (kimi-k3)

Reviews of `task3-proposal-glm.md` and `task3-proposal-qwen.md` against my
own `task3-proposal-kimi.md`, plus the recommended standard shape.

All three agree on the skeleton: shared pure decision core with injected
lookups; bridge `find_item` becomes a delegating adapter with unchanged
signature/tests; `POST /api/cart/scan` in `cart.py` reusing `_upsert_line`
(badge→400, misread→422, hit→200+`CartOut`, miss→404 structured detail);
suggestions = prefix siblings + keyword with semantic confined to a GET
endpoint that degrades to `[]`; `ScanRequest` with staff-only
list/resolve/dismiss and resolve-via-existing-`POST /api/items`.

---

## 1. Review: glm

### Strengths (adopt)
- **`qty` in `CartScanIn`** (ge=1, le=CART_QTY_MAX) — my proposal hardcoded
  qty=1; multi-unit scans (a case of 12) are real. Qwen has it too. Adopt.
- **Misread 422 carries NO suggestions** — "a misread must never be
  suggested around". I had 422 but didn't pin the absence of suggestions;
  explicit pin adopted (qwen has it too).
- **Delegation spy test**: assert the bridge's `find_item` actually calls
  `scanlookup.resolve_scan`, so the delegation cannot be silently
  re-inlined later. Cheap, unique to GLM, adopt.
- **409 on filing a request for an already-catalogued code, with the
  matched `ItemOut` in the detail** — better than my "create anyway +
  `existing_item` field": a request for a catalogued item is noise; the UI
  gets the item to offer "add that instead". Qwen independently chose 409
  (`already_catalogued`). Adopt.
- **`resolved_item_id` as a plain int, no rewrite on item delete** —
  resolved requests are an audit trail; tolerate the dangling id with a
  defensive/outer join. Qwen agrees (weak ref). My "UPDATE … SET NULL on
  item delete" rewrites history — reject my own approach, adopt theirs.
- **Required `name` (1..255) + `price_cents` (≥0, default 0) at filing** —
  the task's intake screen collects name and price; optional fields (my
  proposal) invite content-free requests. Qwen agrees. Adopt.
- **`add_to_cart` on resolve (default true)**: the requester's cart gains
  the item via `_upsert_line(session, request.user_id, …)` — closes the
  loop "member scanned it *wanting* it" with Task-1 machinery, one line.
  Unique to GLM. Adopt, flagged as coordinator-flippable.

### Weaknesses / risks
- **`by_keyword` strips LIKE wildcards instead of escaping them**
  (`q.replace('%','')…`) — a *different* keyword dialect from
  `list_items`' escaped-ILIKE: the exact drift the shared-core task exists
  to prevent, one level down. Qwen's (and my) approach — extract
  `keyword_filter(q)` from `list_items` and reuse it — is the correct one.
  Reject.
- **No duplicate-open protection on ScanRequest POST** — a kiosk
  double-submit files two identical open requests. Qwen's 409
  `duplicate_open` covers it; GLM's is a gap.
- **Bridge adapter without memoisation**: `by_sku` does a `?q=` fetch and
  `by_keyword` does another — under `fuzzy=True` that is TWO HTTP calls
  where today's bridge makes ONE, and qwen verified the existing bridge
  tests **pin the exact call sequences**. GLM's adapter would break the
  very suite it promises to keep green. Qwen's memoising closure is the
  correct adapter. Reject GLM's.
- **`prefix_candidates` validates with `assert`** — disabled under
  `python -O`; mine raised `ValueError`, qwen's rank function validates
  properly. Minor, but don't standardise the assert.
- **`by_sku` on `analysis.raw`**: qwen's `analysis.key` (addon-stripped,
  normalised) is the canonical identity `analyze_scan` computed — use
  that. (Both are whitespace-stripped; `.key` is the doctrine-consistent
  choice.)

### Disagreements vs. my proposal
- 409-already-catalogued (GLM/qwen) vs my create-with-`existing_item`:
  theirs is better — adopted.
- Weak-ref `resolved_item_id` (GLM/qwen) vs my null-on-delete: theirs is
  better — adopted.
- Required name/price (GLM/qwen) vs my optional: theirs is better —
  adopted.
- Everything else (core shape, 404 detail with prefix+keyword, misread
  422, badge 400, staff-only lifecycle) is agreement.

---

## 2. Review: qwen

### Strengths (adopt — several are decisive)
- **The memoising bridge adapter.** Qwen verified the existing
  `tests/test_scanner_bridge.py` pins the *exact HTTP call sequences*
  (opaque: ONE `?q=` page shared by the exact-SKU scan and the fuzzy
  first-hit; misread: ZERO calls). The memoising closure preserves those
  byte-for-byte; without it the adapter breaks the pinned suite (GLM's
  oversight, and mine — I never addressed call-sequence parity). **This is
  the single most important technical detail in the round.** Adopt.
- **Callback-form `resolve_scan`** (`by_gtin`/`by_sku`/`by_keyword`
  callables, `allow_fuzzy` flag) vs my/GLM's `Lookup` Protocol class —
  functionally equivalent; callables make the memoising closure natural
  and keep the module stdlib-pure. Either acceptable; slight edge to
  qwen's for the adapter ergonomics. Adopt (taste-level).
- **`catalogable: bool` in the 404 detail** (`kind in {gtin, restricted}`)
  — tells the Task-5 form whether a canonical identity exists to prefill
  (`barcode=gtin14`) or staff must decide the SKU story. Small, precise,
  adopt.
- **Bridge verdict strings reused verbatim as 404 `message`s**, including
  the opaque 6–8-digit UPC-E-symbology hint — parity of language as well
  as logic, and the hint is operationally important (HID captures cannot
  say UPC-E). Adopt.
- **`ScanAnalysisOut` schema** — one serialisable view of
  `ScanAnalysis`+`BarcodeInfo`, reused by the scan endpoint *and*
  ScanRequest responses. Beats my ad-hoc dict. Adopt.
- **409 `duplicate_open`** (same user, same code, open request) — kiosk
  double-submit is noise, not data; detail carries the existing request.
  Better than my idempotent-200 (which hides the double-submit) and GLM's
  gap. Adopt (qwen flags it as flippable — fine).
- **`resolved_by` / `resolved_at` on resolve** — audit fields I lacked.
  Adopt.
- **`rank_prefix_siblings` + `common_prefix_len` in `barcodes.py`** —
  comparison on the **indicator-stripped 13-digit view** (a GTIN-14 case
  code ranks as the top sibling of its retail GTIN-13), `min_shared=6`
  floor with the honest "GEPIR is a service, observed shared length IS
  the heuristic" rationale, stable tie-break, NULL-barcode tolerance.
  Strictly better than my/GLM's padded-GTIN-14 LIKE ladder (which
  mis-groups case codes). O(n) Python at shop scale, LIKE-ladder
  documented as escape hatch. Adopt; my `prefix_candidates` is withdrawn.
- **`keyword_filter(q)` extracted from `list_items`** (one house keyword
  search, escaping included) and **`_vector_search_rows` factored so
  `vector_search` keeps its 503 byte-for-byte while `/suggest` degrades
  to `[]`** — surgical about not perturbing tested behaviour. Adopt both.
- **`code` max_length=128** (vs my/GLM's 64) — long Code 128 labels
  shouldn't 422. Adopt.
- **409 on `create_item` stays a string detail; Task 5 pre-checks with
  `GET ?barcode=`** — zero contract churn, the UX still gets "already
  filed as {sku}". Adopt.
- **PR sequencing** (core+adapter → barcodes helpers → scan endpoint →
  suggest extractions → ScanRequest → docs), each step green with the
  existing suite as the acceptance gate. Adopt as the implementation
  order.

### Weaknesses / risks
- Little of substance. `GET /mine` is a self-flagged scope addition —
  keep it (one scoped query; Task 5's "your report was resolved" UI needs
  it; coordinator can drop it at no cost).
- Callback parameters default to `None` with silent no-lookup semantics
  (`by_gtin=None` → miss) — convenient for tests but a typo'd call site
  silently never matches. Mitigate in review by requiring the two server
  call sites to pass all callbacks explicitly (they do); not worth
  changing the signature.
- `add_to_cart`-on-resolve is absent (qwen's resolve is purely
  administrative). GLM's version closes the member's loop; adopt GLM's
  flag on top of qwen's resolve.

### Disagreements vs. my proposal
- Suggestion algorithm: qwen's indicator-stripped rank-with-floor beats
  my padded LIKE ladder — adopted, mine withdrawn.
- Duplicate-open 409 (qwen) vs my idempotent-200 — adopted qwen's.
- `analysis.key` for the opaque SKU lookup (qwen) vs my raw `code` —
  adopted.
- Everything else (404 structured detail, semantic-only-in-GET, staff-only
  lifecycle, `_upsert_line` reuse, badge 400, misread 422) is agreement.

---

## 3. Final recommendation — the shape to standardise on

**Base: qwen's proposal**, plus two GLM additions (`add_to_cart` on
resolve, delegation spy test) and these reconciliations:

1. **Core**: `app/scanlookup.py` — pure, `resolve_scan(code, symbology, *,
   by_gtin, by_sku, by_keyword, allow_fuzzy)` → `ScanVerdict{analysis,
   item, how}`; opaque lookup uses `analysis.key`; misread/empty never
   call any callback. **Bridge adapter**: `find_item` keeps its
   signature/returns and gains the **memoising `by_keyword` closure** so
   the pinned one-call-per-scan HTTP sequences hold byte-for-byte; add
   GLM's delegation spy test.
2. **`POST /api/cart/scan`**: `CartScanIn{code(≤128), symbology, qty=1}`;
   badge → 400; misread → 422 (`status:"misread"`, **no suggestions**);
   hit → `_upsert_line(…, qty)` → 200 `{status:"added", match, item, qty,
   cart}`; miss → 404 with `{status:"unknown", message (bridge verdict
   string), analysis: ScanAnalysisOut, catalogable, suggestions:
   {by_prefix, by_keyword}}` (no semantic in the 404).
3. **Suggestions**: `common_prefix_len` + `rank_prefix_siblings`
   (indicator-stripped view, `min_shared=6` module constant, stable
   tie-break) in `app/barcodes.py`; `keyword_filter(q)` extracted from
   `list_items`; `GET /api/items/suggest?barcode=&q=&limit=` →
   `{by_prefix, by_keyword, by_semantic}` with `_vector_search_rows`
   factored (503 behaviour of `/vector-search` untouched; suggest
   degrades to `[]`). One `suggest_items` helper feeds both the endpoint
   and the 404 embed.
4. **ScanRequest**: table with required `name`, `price_cents≥0`,
   server-captured `kind`/`gtin14`, `status(open|resolved|dismissed)`
   indexed, `resolved_item_id` as a **plain int weak ref** (no rewrite on
   item delete; tolerant outer-join serialisation), `resolved_by` +
   `resolved_at`; member `POST` (400 badge / 422 misread / **409
   already_catalogued with item** / **409 duplicate_open with existing**),
   `GET /mine`, staff `GET ?status=`, `POST /{id}/resolve {item_id,
   add_to_cart=true}` (upserts into the **requester's** cart),
   `POST /{id}/dismiss`; `delete_member` cleanup; item delete untouched.
5. **Tests**: qwen's five test groups + GLM's delegation spy + the
   add_to_cart pin; existing bridge/items/migrations suites green
   unchanged; `test_migrations.py` exact-set untouched.
6. **Sequencing**: qwen's ⑥-step order.

Naming: `resolved_item_id` (matches `status="resolved"`; over qwen's
`resolution_item_id` — cosmetic, coordinator can flip).

## 4. Changes to my own proposal

1. Adopt the **memoising bridge adapter** (call-sequence parity — the
   detail I missed).
2. Withdraw my `prefix_candidates` LIKE ladder → adopt
   `rank_prefix_siblings` (indicator-stripped, floor, stable order).
3. ScanRequest POST: my "create anyway + `existing_item`" → **409
   already_catalogued**; my idempotent-200 → **409 duplicate_open**.
4. `resolved_item_id`: my null-on-delete UPDATE → **weak ref, no rewrite**,
   tolerant join.
5. Required `name`/`price_cents` at filing (was optional).
6. Add `qty` to `CartScanIn`; add `catalogable` + bridge-verdict
   `message` + `ScanAnalysisOut` to the 404 shape; pin **no suggestions on
   misread**; `code` cap 128.
7. Adopt `keyword_filter`/`_vector_search_rows` extractions (replacing my
   `item_search_filter` sketch), `analysis.key` for the opaque lookup,
   GLM's `add_to_cart`-on-resolve and delegation spy test, and qwen's PR
   sequencing.
8. Keep from mine (no conflict): `suggest_items` shared helper feeding
   both surfaces (qwen has the same), `/mine` (qwen also has), the
   `_flagged_token` → `conftest.py` move for shared 403 tests.
