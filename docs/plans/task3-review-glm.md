# Task 3 cross-review (glm) — of `task3-proposal-kimi.md` and `task3-proposal-qwen.md`

Reviewer context: my own proposal is `docs/plans/task3-proposal-glm.md`. All
three proposals converged on the architecture before this review (pure
`resolve_scan` core + injected lookups; bridge `find_item` as a
contract-preserving adapter; badge→400 / misread→422 / unknown→404-structured
in `POST /api/cart/scan`; prefix+keyword suggestions with semantic-only-in-
the-endpoint degrading to `[]`; member ScanRequest with staff-only
resolution). The review arbitrates the real deltas: the adapter's HTTP call
fidelity, the prefix-sibling algorithm, the 404 detail shape, and several
ScanRequest lifecycle choices.

**Fact checked during this review** (it decides §1):
`tests/test_scanner_bridge.py` asserts **exact HTTP call sequences** —
`get.calls == ["/api/items?barcode=…"]` (one call, GTIN path),
`get.calls == ["/api/items?q=…&limit=100"]` (one call, opaque path), and
`get.calls == []` (zero calls, misread). The opaque+fuzzy tests do *not*
assert call counts, but today's bridge fetches `?q=` **once** and shares the
page between the SKU filter and the fuzzy first-hit.

---

## 1. Review of `task3-proposal-kimi.md`

### Strengths (adopt)

- **The dual-backend parity suite** — the strongest testing idea on the
  table: one shared case list (canonical GTIN, UPC-E→EAN-13, bare-6-digits
  never guessed, opaque case variants, "water" never matches, fuzzy opt-in,
  misread never looked up, restricted matches) executed against **both** the
  DB backend and the bridge's `fake_api` adapter. "Same input ⇒ same
  verdict, both backends" makes the no-drift guarantee *executable*, not
  just structural. My proposal relied on the bridge suite passing
  unchanged; kimi's is strictly stronger. Adopt.
- **`item_search_filter(q)` extraction** from `list_items` — the anti-drift
  rule applied one layer down (keyword semantics shared by `?q=`,
  `DbLookup.search`, and the suggest endpoint). Qwen has the same idea plus
  a vector-search twin; adopt both.
- **`reason: "unknown_barcode" | "unknown_sku"`** — a clean discriminator
  for the 404 detail; folds naturally into a `status` key (see §3).
- **`GET /mine`** for the filer — cheap, and it powers the Task-5
  "your report was resolved" UX. Adopt (qwen has it too).
- **Format-family sibling note** (an EAN-13 and a UPC-A of the same brand
  pad differently, so siblings group by format family) — correct
  documentation of padded-prefix semantics; subsumed by qwen's ranking
  (below) but worth keeping in `docs/barcodes.md`.
- R6 (the parity case list must grow with future `analyze_scan` edge
  cases) — good drift-watch note.

### Weaknesses / risks

- **The adapter is not call-faithful.** Kimi's `_HttpLookup` gives `by_sku`
  and `search` separate `?q=` fetches; on the opaque+fuzzy path that is
  **two** fetches where today's bridge makes one. The asserted pins
  (one-call opaque, zero-call misread) still hold — I verified the fuzzy
  tests don't assert call counts — so the suite stays green, but it is an
  behavioural infidelity smuggled through a green test. Qwen's memoising
  closure is the fix; whatever interface shape wins, the `?q=` page must be
  fetched once and shared. (My own proposal has the same flaw — see §3.)
- **The LIKE-ladder prefix algorithm misses case codes.** Candidates are
  prefixes of the full 14-digit key, indicator digit included: a GTIN-14
  case/c pallet code (indicator 1–8) can never match its retail GTIN-13
  sibling at any candidate length, because they differ at digit 1. Qwen's
  indicator-stripped comparison fixes exactly this. Same flaw in my
  proposal.
- **`resolved_item_id` as a real FK + NULL-ing UPDATE in `delete_item`** —
  rewrites history (the request no longer records what it pointed at) and
  adds a third cleanup path to `delete_item`. The weak-ref approach (mine,
  qwen) preserves the audit record and needs no item-delete coupling.
  Reject.
- **Idempotent re-file (200, same row)** vs qwen's 409 `duplicate_open`:
  either is defensible, but the repo's uniform convention for "this
  already exists" is 409 (`register`, `create_item`, cart-replace
  duplicates) — consistency wins. Prefer qwen.
- `name`/`price_cents` optional at filing — the Task-5 modal collects both;
  a request with no name is noise for staff. Require name (mine/qwen).

### Disagreements with my proposal

| Point | Kimi | Me | Verdict |
|---|---|---|---|
| Parity testing | dual-backend shared case list | bridge suite unchanged | **Kimi** — adopt his, keep mine as a subset |
| 404 discriminator | `reason` field | none (kind implied) | **Kimi** (folded into `status`) |
| `/mine` | yes | omitted | **Kimi** (with qwen) |
| Re-file duplicate | 200 idempotent | not handled | **neither** — qwen's 409 |
| resolved_item_id | FK + NULL on item delete | plain int weak ref | **Me** (with qwen) |

## 2. Review of `task3-proposal-qwen.md`

### Strengths (adopt)

- **The memoising closure adapter** — the review's decisive technical
  catch. Qwen is the only peer who read the bridge tests as *call-sequence
  contracts* (correctly: `get.calls` assertions at three points, verified
  above) and designed the adapter to reproduce them byte-for-byte,
  including the unasserted single-`?q=`-fetch on opaque+fuzzy. My and
  kimi's adapters both double-fetch there. Adopt verbatim.
- **Indicator-stripped shared-prefix ranking** — the best algorithmic idea
  in either proposal. Comparing on `gtin14[1:]` (13-digit view) makes a
  case/pallet GTIN-14 rank as the top sibling of its retail GTIN-13 (same
  product family — exactly what a substitute suggestion wants), which the
  full-key LIKE ladders (mine, kimi) structurally cannot do. Continuous
  shared-length ranking with a tunable floor (`SUGGEST_MIN_SHARED`) also
  beats a binary 7-probe ladder for quality, and one indexed-column fetch +
  Python compare is simpler than seven LIKEs. Adopt, replacing my
  `prefix_candidates` ladder entirely.
- **`catalogable` flag** in the 404 detail (`kind ∈ {gtin, restricted}`) —
  exactly what the Task-5 form needs to decide whether the barcode field is
  prefilled. Adopt.
- **Bridge verdict strings reused verbatim** as the 404 `message` — parity
  of *language* between bridge stdout and API detail; free, and it keeps
  the UPC-E symbology hint ("the scanner must transmit its symbology ID")
  in front of the user. Adopt.
- **`resolved_by` / `resolved_at`** audit columns on ScanRequest — cheap,
  and the staff queue is an operational record. Adopt.
- **409 `duplicate_open`** — matches repo conventions (see above). Adopt.
- **`_vector_search_rows` extraction** with "every degradation path returns
  `[]`, never 503, each caught and logged" — the most thorough treatment of
  decision 7. Adopt (with kimi's `keyword_filter` twin).
- **PR sequencing ①–⑥** — each step gated by the existing suite; good
  review slicing. Adopt.
- **R7** (withdrawing the structured-409 on `create_item` in favour of a
  client-side `?barcode=` pre-check) — correct scope discipline: don't churn
  a tested contract when the pre-check delivers the same UX.
- Weak-ref `resolution_item_id` with a tolerant outer join — matches my
  stance, better argued.

### Weaknesses / risks

- **The GTIN-8 padding caveat is looser than stated.** Two EAN-8 items pad
  to six leading zeros — shared length 6 ≥ `min_shared=6` — so *all* EAN-8
  catalogue items become mutual "siblings" on zero real digits. Qwen
  acknowledges the pad decides order *within* the format but not that the
  floor admits pure-pad matches. Either raise the floor to 7 (pad 6 + at
  least one real digit must agree) or document the EAN-8 mutual-suggestion
  noise as accepted. One constant + one test either way — flag for the
  coordinator, not a blocker.
- O(n) barcode load per suggest call — fine at shop scale and documented
  with an escape hatch; no action.
- `code` max_length 128 vs 64 (mine/kimi) — trivial; 64 suffices (fits
  `HCRM1:` payloads at 49); pick either.
- Qwen's `ScanAnalysisOut` recomputed-on-read is explicitly *not* included
  in ScanRequestOut — correct (filed classification is the record); agree.
- The callable-parameter interface (`by_gtin=`/`by_sku=`/`by_keyword=`)
  vs my/kimi's Protocol class: with the memoisation requirement, callables
  are the natural fit (closures share state); a Protocol class with a
  memoising implementation is equivalent. Standardise on qwen's shape since
  it is the one verified against the pins.

### Disagreements with my proposal

| Point | Qwen | Me | Verdict |
|---|---|---|---|
| Adapter fetches | memoised single `?q=` | separate `by_sku`/`by_keyword` fetches | **Qwen** — mine double-fetches on opaque+fuzzy (infidelity, not test-breaking; verified) |
| Prefix algorithm | indicator-stripped shared-length ranking | LIKE ladder on full key | **Qwen** — mine misses case-code siblings entirely |
| 404 detail | `status`/`catalogable`/verbatim messages | `message`/`analysis`/grouped suggestions | **Qwen** (+ kimi's barcode/sku distinction) |
| `add_to_cart` on resolve | no (administrative) | yes, default true | **Qwen** — concede: auto-adding to a member's cart after the fact is surprising; the member gets the item via re-scan / the already-catalogued 409 / `/mine`+Task-5 UI. Keep mine as a flagged option only. |
| Re-file duplicate | 409 `duplicate_open` | not handled | **Qwen** |
| Filing 409 already-catalogued | 409 + item | 409 + item | converged |

## 3. Final recommendation — the shape to standardise on

**Core** (`app/scanlookup.py`, pure, stdlib + `app.barcodes` only):
qwen's `resolve_scan(code, symbology="", *, by_gtin, by_sku, by_keyword=None,
allow_fuzzy=False) -> ScanVerdict{analysis, item, how}`.

**Bridge**: qwen's memoising adapter — `find_item` keeps its signature and
`(item, how, analysis)` contract; one `?q=` page shared by the SKU filter
and the fuzzy first-hit; existing `tests/test_scanner_bridge.py` unchanged
and green (the asserted pins hold), plus one new test pinning the
memoisation (opaque+fuzzy ⇒ exactly one `?q=` call).

**Parity**: kimi's dual-backend shared case list — the same cases against
the server's callables and the bridge's `fake_api`; same input ⇒ same
verdict.

**`POST /api/cart/scan`** (in `app/routers/cart.py`): `CartScanIn {code
(≤64), symbology, qty (1..CAP, default 1)}`; badge → **400** dict detail;
misread → **422** dict detail (both with a `status` key); hit → **200**
`{status:"added", match, item, qty, cart}` via `_upsert_line`; miss →
**404** dict detail: `{status: "unknown_barcode"|"unknown_sku", message
(bridge verdict strings verbatim), analysis (flat serialisation incl.
region/usage/notes), catalogable, suggestions: {by_prefix, by_keyword}}`.

**Suggestions**: qwen's `common_prefix_len` + `rank_prefix_siblings`
(indicator-stripped 13-digit view, tunable floor — coordinator to pick 6
vs 7 given the EAN-8 pad caveat), one `suggest_items()` helper, exposed as
`GET /api/items/suggest?barcode=&q=&limit=` (invalid `barcode` → 422,
`by_semantic` degrades to `[]` on every unavailability path) and embedded
(prefix + keyword-on-code) in the 404. `keyword_filter` and
`_vector_search_rows` extractions from `items.py`, gated by the existing
items tests.

**ScanRequest**: qwen's model + kimi's `/mine` — filing validates
(badge→400, misread→422, already-catalogued→409 with the item,
duplicate-open→409 with the existing request); staff-only list/resolve/
dismiss; `resolved_by`/`resolved_at`; weak-ref `resolution_item_id` with
tolerant join; `delete_member` cleanup; **no** `add_to_cart` on resolve
(kept as a flagged coordinator option); item deletion leaves resolved
requests intact.

**Tests**: the five files all three proposals agree on, plus kimi's shared
parity case list and qwen's memoisation pin; `test_migrations.py`
untouched; qwen's PR sequencing ①–⑥.

## 4. Changes I am making to my own proposal as a result

1. **Adapter memoisation** (qwen): the bridge adapter must share one `?q=`
   fetch between `by_sku` and `by_keyword` — my sketch double-fetches on
   the opaque+fuzzy path. My "bridge tests pass unchanged" claim was true
   but hid an infidelity; qwen checked the pins, I didn't.
2. **Replace my `prefix_candidates` LIKE ladder** with qwen's
   indicator-stripped shared-length ranking — my ladder cannot match
   case-code↔retail siblings at all (indicator digit included in every
   candidate).
3. **Drop `add_to_cart` from resolve** (default-false → gone; flagged
   option only) — both peers treat resolve as administrative, and the
   member's want is served by re-scan / already-catalogued 409 / `/mine`.
4. **Enrich the 404 detail** with qwen's `status` discriminator +
   `catalogable` + verbatim bridge messages, and kimi's
   `unknown_barcode`/`unknown_sku` split.
5. **Add** `/mine`, the 409 `duplicate_open`, `resolved_by`/`resolved_at`,
   the `keyword_filter`/`_vector_search_rows` extractions, kimi's
   dual-backend parity suite, and qwen's PR sequencing.
6. **Keep** (both peers agree or don't contradict): `qty` in `CartScanIn`,
   the direct `lower(sku)==` server lookup, weak-ref `resolved_item_id`,
   `delete_member` cleanup, suggest-endpoint auth level, code cap 64.
