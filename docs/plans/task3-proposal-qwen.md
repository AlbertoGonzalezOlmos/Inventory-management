# Task 3 proposal (qwen3.8-max) — Scan resolution core, `/api/cart/scan`, suggestions, ScanRequest

Backend only (no frontend). Implements coordinator Task 3 under the binding
decisions: **one shared scan-resolution core** (no bridge/server drift,
decision 8); **suggestions = GS1 company-prefix siblings + keyword tokens,
semantic degrades to empty on 503** (decision 7); **staff-only catalogue
creation — members file a `ScanRequest`** (decision 1); **cart adds never
mutate stock** (decision 6). Proposal only; grounded in the current tree
(Task 1 merged as `1a742d9`, Task 2 in working tree).

**Verified ground truth this pass** (not assumed):

- `analyze_scan(barcode, symbology="") -> ScanAnalysis{raw, symbology, kind
  ∈ {gtin, restricted, invalid-gtin, opaque, empty}, key (gtin14 or raw; ""
  for invalid-gtin), addon, info: BarcodeInfo|None}`; `BarcodeInfo{gtin14,
  format ∈ GTIN-8/12/13/14, indicator, prefix (3-digit GS1), region, usage,
  globally_unique, notes[], in_store}`. Pure stdlib.
- `scripts/scanner_bridge.py::find_item(get, code, fuzzy=False,
  symbology="") -> (item, how, analysis)`, `how ∈ {"barcode","sku","search",
  None}`. Its tests **pin the exact HTTP call sequence**: GTIN → exactly one
  `GET /api/items?barcode=<gtin14>`; opaque → exactly one
  `GET /api/items?q=<code>&limit=100` whose results are scanned for exact
  (case-insensitive) SKU equality and reused as the fuzzy first-hit source;
  `invalid-gtin` → **zero** calls (`test_misread_gtin_is_never_looked_up`).
  Any refactor must reproduce these sequences byte-for-byte.
- `app/routers/cart.py`: `_upsert_line(session, user_id, item_id, qty) ->
  bool` (race-free `INSERT…SELECT FROM items…ON CONFLICT`, caps at
  `QTY_CAP`, commits) and `_cart_out(session, user) -> CartOut` — the
  coordinator says reuse them, and they fit unchanged.
- `app/routers/items.py`: `list_items` escapes LIKE wildcards then ORs
  `ilike` over name/description/sku/barcode; `vector_search` raises **503**
  when `embed_text` returns None or `vector_init` fails; `create_item`
  rejects with **503 when embeddings are unavailable** and answers duplicate
  barcode with **409 string detail** `"Barcode already assigned to item
  {sku}"`.
- New tables come from `create_all()`; no migration entries (Task-1 pattern;
  `test_every_migration_the_app_currently_needs_is_listed` pins the exact
  migration set and must stay untouched).

---

## 1. Shared core — `app/scanlookup.py`

A **pure** module (stdlib + `app.barcodes` only; no DB, no HTTP, no FastAPI)
holding the exact-identity decision table, parameterised by lookup callbacks
so both worlds plug in their own I/O. The doctrine it encodes is REVIEW-m10
P1: *identity or nothing — never guess*.

```python
# app/scanlookup.py
from dataclasses import dataclass
from typing import Any, Callable

from app.barcodes import ScanAnalysis, analyze_scan

@dataclass(frozen=True)
class ScanVerdict:
    analysis: ScanAnalysis
    item: Any | None          # whatever the callbacks return (dict | Item)
    how: str | None           # "barcode" | "sku" | "search" | None

def resolve_scan(
    code: str,
    symbology: str = "",
    *,
    by_gtin: Callable[[str], Any | None] | None = None,     # canonical GTIN-14 → item
    by_sku: Callable[[str], Any | None] | None = None,      # exact, case-insensitive SKU
    by_keyword: Callable[[str, int], list] | None = None,   # q-semantics, for --fuzzy only
    allow_fuzzy: bool = False,
) -> ScanVerdict:
    analysis = analyze_scan(code, symbology)
    if analysis.kind in ("gtin", "restricted"):
        item = by_gtin(analysis.key) if by_gtin else None
        return ScanVerdict(analysis, item, "barcode" if item else None)
    if analysis.kind in ("invalid-gtin", "empty"):
        return ScanVerdict(analysis, None, None)            # NEVER looked up
    # opaque: exact SKU identity only …
    item = by_sku(analysis.key) if by_sku else None
    if item is not None:
        return ScanVerdict(analysis, item, "sku")
    # … and the opt-in keyword fallback (the bridge's --fuzzy; the server
    # never enables it — suggestions are shown, never auto-added).
    if allow_fuzzy and by_keyword:
        hits = by_keyword(analysis.key, 100)
        if hits:
            return ScanVerdict(analysis, hits[0], "search")
    return ScanVerdict(analysis, None, None)
```

**Bridge side** — `find_item(get, code, fuzzy, symbology)` keeps its
signature and `(item, how, analysis)` return contract (its 12+ existing
tests pass **unchanged** and thereby become the parity guard), and becomes a
thin adapter with a **memoising closure** so the pinned one-call-per-scan
HTTP sequences hold:

```python
def find_item(get, code, fuzzy=False, symbology=""):
    cache: dict[tuple[str, int], list] = {}
    def by_keyword(text, limit):
        if (text, limit) not in cache:
            _, items = get(f"/api/items?q={urllib.parse.quote(text)}&limit={limit}")
            cache[(text, limit)] = items
        return cache[(text, limit)]
    def by_sku(raw):                      # exact SKU equality over the q= page
        for it in by_keyword(raw, 100):
            if it["sku"].lower() == raw.lower():
                return it
        return None
    def by_gtin(key):
        _, items = get(f"/api/items?barcode={urllib.parse.quote(key)}")
        return items[0] if items else None
    v = resolve_scan(code, symbology, by_gtin=by_gtin, by_sku=by_sku,
                     by_keyword=by_keyword, allow_fuzzy=fuzzy)
    return v.item, v.how, v.analysis
```

Call-sequence check against the pinned tests: GTIN → one `?barcode=` call ✓;
opaque → one `?q=` call (SKU match and fuzzy first-hit share the memoised
page — exactly today's behaviour) ✓; misread → zero calls ✓; `--fuzzy`
refusal with stock adjust stays in `main()` untouched.

**Server side** (used by `POST /api/cart/scan`, §2): `by_gtin` =
`SELECT Item WHERE barcode = :gtin14`; `by_sku` =
`SELECT Item WHERE lower(sku) = lower(:code)` (direct SQL — strictly more
faithful to the *semantics* than the bridge's 100-row page scan, same
decision table); `by_keyword=None`, `allow_fuzzy=False` — **the cart path
never guesses**; unknown codes surface suggestions (§3) instead.

## 2. `POST /api/cart/scan`

Lives in `app/routers/cart.py` (it mutates the caller's cart; reuses
`_upsert_line`/`_cart_out`). Auth: `get_current_user` — any role (members
scan too); the `must_change_password` 403 gate applies automatically.

**Request** (`app/schemas.py`):

```python
class CartScanIn(_Stripped):
    code: str = Field(min_length=1, max_length=128)   # "" / whitespace → 422
    symbology: str = Field(default="", max_length=32) # scanner-reported; guides UPC-E etc.
    qty: int = Field(default=1, ge=CART_QTY_MIN, le=CART_QTY_MAX)
```

**Flow** (order matters):

1. `parse_badge_payload(code)` hit → **400**, detail
   `{"status": "badge", "message": "QR badge payload — badges log in via
   POST /api/auth/qr-login, they are not catalogue items"}`. (Invariant all
   three plans agreed on. A *prefix-less* raw 43-char badge token is
   deliberately not special-cased: it is indistinguishable from an opaque
   label, and the `HCRM1:` prefix is the contract — same stance as the
   bridge.)
2. `resolve_scan(code, symbology, by_gtin=…, by_sku=…)` →
   - **hit** (`how` = `"barcode"` or `"sku"`): `_upsert_line(session,
     user.id, item.id, payload.qty)` → **200**:
     ```json
     {"status": "added", "match": "barcode",
      "item": {ItemOut}, "qty": 3, "cart": {CartOut}}
     ```
     (full cart in the response — Task-1 contract: one payload, no
     read-after-write. `qty` is the resulting line quantity; the `QTY_CAP`
     clamp shows in `cart`, never as an error. Stock untouched — decision 6.)
   - **`invalid-gtin`** → **422**, detail
     `{"status": "misread", "message": "check digit fails — re-scan, do not
     file", "analysis": {ScanAnalysisOut}}`. Nothing stored, nothing
     suggested (a misread must not masquerade as a product — the
     `GET /api/items?barcode=` 422 precedent; status code matches it).
   - **miss** (`gtin`/`restricted`/`opaque` with no identity) → **404** with
     the structured detail that drives the later name+price UI:
     ```json
     {"status": "unknown",
      "message": "valid GTIN, not in the catalogue — new-entry candidate",
      "analysis": {"kind": "gtin", "code": "8412345000123",
                    "gtin14": "08412345000123", "format": "GTIN-13",
                    "prefix": "841", "region": "Spain & Andorra",
                    "usage": "retail", "globally_unique": true,
                    "notes": [...], "addon": ""},
      "catalogable": true,
      "suggestions": {"by_prefix": [ItemOut…], "by_keyword": [ItemOut…]}}
     ```
     - `message` reuses the bridge's verdict strings verbatim (parity of
       *language* too), including the opaque 6–8-digit hint "if this is a
       UPC-E, the scanner must transmit its symbology ID" — an HID capture
       cannot say UPC-E, and `analyze_scan` refuses to guess (pinned by
       `test_bare_six_digits_are_never_guessed_into_a_gtin`).
     - `catalogable`: `kind in {"gtin", "restricted"}` (a normalised identity
       exists to prefill); `false` for opaque (staff decide the SKU/label
       story) — the Task-5 form reads this.
     - `analysis` = new `ScanAnalysisOut` schema (a serialisable view of
       `ScanAnalysis`+`BarcodeInfo`; also reused by ScanRequest responses).
     - No `by_semantic` in the 404: embedding a digit string is noise;
       semantic suggestions belong to the *typed name* flow via §3's
       endpoint.
   - **`empty`**: unreachable through the API (`min_length=1` after
     `_Stripped`); the core still handles it (bridge guard) — 422 if it ever
     surfaces.

Structured `detail` objects (vs pydantic's list-shaped 422) follow the
existing precedent (`list_items` raises string-detail 422s); clients branch
on `detail["status"]` when `detail` is a dict. Documented in the endpoint
docstring.

## 3. Suggestions — "closest matching items using the barcode system"

**Algorithm** (new pure helpers in `app/barcodes.py`, keeping its
stdlib-only doctrine):

```python
def common_prefix_len(a: str, b: str) -> int: ...

SUGGEST_MIN_SHARED = 6   # heuristics documented below; pinned by tests

def rank_prefix_siblings(gtin14: str, stored: Iterable[tuple[int, str]],
                         min_shared: int = SUGGEST_MIN_SHARED,
                         ) -> list[tuple[int, int]]:
    """Rank catalogue barcodes by shared GS1 prefix with the scanned GTIN-14.

    Comparison runs on the INDICATOR-STRIPPED 13-digit view (gtin14[1:]):
    a case/pallet GTIN-14 (indicator 1–8) then ranks as the top sibling of
    its retail GTIN-13 — same product family, which is exactly what a
    substitute-suggestion wants. Zero-padding keeps ranking sane *within*
    a format family (two GTIN-8s share the 5-digit pad, so real view digits
    decide order; cross-format pairs share ~0 and never suggest).
    min_shared=6 ⇒ at least ~5 real digits agree beyond padding — i.e. the
    same GS1 company-prefix neighbourhood (company prefixes are 6–12 digits;
    we cannot know the exact length offline — GEPIR is a lookup service —
    so observed shared length IS the heuristic). Restricted/in-store codes
    (2x…) get the most relevant suggestions from this: same store range.
    """
```

Computed server-side by loading `(id, barcode)` for `barcode IS NOT NULL`
(one indexed-column scan; O(n) Python compare — trivial at shop scale, the
documented ceiling; a SQL LIKE-ladder is the escape hatch if a catalogue
ever reaches 10⁵ rows). Rank: shared-length DESC, then `id` ASC (stable);
cap at `limit`.

**Endpoint** `GET /api/items/suggest` (in `app/routers/items.py`; any
authenticated user — the member-side form needs it):

```
?barcode=   any EAN/UPC form → normalise_input_gtin; invalid → 422 (the
            list_items?barcode= precedent). Absent/empty → by_prefix = [].
&q=         keyword tokens → by_keyword via the SAME escaped-ILIKE condition
            as list_items (extract `keyword_filter(q)` from list_items and
            reuse — one house keyword search, no second dialect).
&limit=     default 5, ge=1 le=25 (per group).
```

Response `SuggestOut {by_prefix: [ItemOut], by_keyword: [ItemOut],
by_semantic: [VectorSearchOut]}`:

- `by_semantic`: `q` with ≥ 2 chars → embed + vector scan, **reusing the
  endpoint internals factored into `_vector_search_rows(session, qvec, …)`**
  (the `vector_search` endpoint keeps its 503 behaviour byte-for-byte; only
  the SQL moves). Every degradation path returns `[]`, never 503 (decision
  7): `embed_text` → None, `vector_init` failure, scan exception — each
  caught and logged at debug.
- The 404 detail of §2 embeds `by_prefix` (+ `by_keyword` on the raw code —
  for opaque SKU-label near-misses) computed by the same
  `suggest_items(session, barcode=…, q=…, limit=…)` helper the endpoint
  calls. **One implementation, two consumers** — the anti-drift rule applied
  again below the router layer.

## 4. Member `ScanRequest` — unknown items without catalogue writes

Decision 1: `POST /api/items` stays staff-only; a member at the counter gets
the form, and filing it persists a request for staff.

**Model** (`app/models.py`; new table ⇒ `create_all()` only, **no migration
entries**):

```python
class ScanRequest(SQLModel, table=True):
    """A member's proposal for a catalogue entry from an unresolvable scan.

    Staff resolve it by creating the item through the normal staff-only
    path (POST /api/items, prefilled from here) and linking it, or dismiss
    it. Classification fields are captured AT FILING time so the request
    stays interpretable after barcode rules or catalogue change.
    """
    __tablename__ = "scan_requests"
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    code: str = Field(max_length=128)
    symbology: str = ""
    kind: str = ""               # analyze_scan kind at filing (gtin/restricted/opaque)
    gtin14: str | None = None    # canonical identity when one exists (prefill)
    name: str = Field(min_length=1, max_length=255)
    price_cents: int = Field(default=0, ge=0)
    status: str = Field(default="open", index=True)   # open | resolved | dismissed
    resolution_item_id: int | None = None             # weak ref (see cleanup)
    resolved_by: int | None = None
    resolved_at: datetime | None = Field(default=None, sa_type=DateTime)
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)
```

**Router** (new `app/routers/scan_requests.py`, prefix `/api/scan-requests`,
registered in `app/main.py`):

| Endpoint | Auth | Behaviour |
|---|---|---|
| `POST ""` → 201 `ScanRequestOut` | any user | Reject badge payloads (**400**, §2 invariant); re-run `analyze_scan` server-side (never trust a client `kind`): `invalid-gtin`/`empty` → **422** "misread — re-scan, do not file" (scan-intake doctrine). If the code *now resolves* (`resolve_scan` hit) → **409** `{"status": "already_catalogued", "item": ItemOut}` (staff beat them to it; UI offers "add that instead"). If the same user already has an **open** request for the same `code` → **409** `{"status": "duplicate_open", "request": ScanRequestOut}` (kiosk double-submit is noise, not data). Else create with captured classification. |
| `GET "/mine"` → `[ScanRequestOut]` | any user | Own requests, newest first, all statuses (lets Task 5 show "resolved by staff"). *(Small addition beyond the coordinator's minimal wording — droppable; it costs one scoped query.)* |
| `GET ""?status=open\|resolved\|dismissed\|all` → `[ScanRequestOut]` | **staff** | Default `open`; oldest first; joins requester name/email (staff may already list members) and the resolved item when present. |
| `POST "/{id}/resolve"` `{item_id}` → 200 | **staff** | Item must exist (404); request must exist (404) and be `open` (else **409** `{"status": "not_open"}`). Sets `status/resolution_item_id/resolved_by/resolved_at` in one commit. Two-step by design: item creation stays on the tested `POST /api/items` path (embeddings, duplicate guards, 409s) — no second creation code path. |
| `POST "/{id}/dismiss"` → 200 | **staff** | `open` → `dismissed` (409 otherwise). |

`ScanRequestOut` = model fields + `user_name`/`user_email` (staff views) +
`resolution_item: ItemOut | None` (tolerant join — see cleanup) +
`analysis: ScanAnalysisOut` recomputed-on-read is **not** included (filed
classification is the record; keeps responses cheap).

**Lifecycle:** scan → `/api/cart/scan` 404 (structured detail + suggestions)
→ Task-5 form: a suggestion click adds that item (request never filed);
staff submit → `POST /api/items` (prefilled `barcode=gtin14`, name, price)
→ `resolve` links it; member submit → `POST /api/scan-requests` → staff
queue → create+resolve or dismiss.

**Cleanup** (FKs declarative only — Task-1 doctrine):
- `members.delete_member`: also delete the user's `scan_requests` rows in
  the same transaction (parity with tokens/cart rows). Operational queue
  items die with the account; the *resolved* audit trail lives in the
  catalogue itself.
- `items.delete_item`: a resolved request's `resolution_item_id` dangles —
  left as a **weak reference** on purpose (deleting an item must not rewrite
  history); `ScanRequestOut.resolution_item` is `None` when the item is
  gone, and the staff list tolerates it (outer join). Documented in the
  model docstring.

## 5. Tests

- **`tests/test_scanlookup.py`** (pure, callback-injected; no DB/HTTP):
  decision-table parity — gtin/restricted call `by_gtin` with the canonical
  key and nothing else; opaque calls `by_sku` exact + case-insensitive
  (`"ex-007"` ≡ `"EX-007"`), fuzzy off → no keyword call and `how=None` on
  miss, fuzzy on → first hit + `how="search"`; `invalid-gtin`/`empty` →
  **zero callback invocations** (assert with raising stubs); UPC-E with
  symbology resolves via `by_gtin(expanded key)`; bare 6 digits without
  symbology stay opaque (never guessed); verdict carries `analysis` through
  untouched.
- **`tests/test_scanner_bridge.py`** — **unchanged and green**: the pinned
  `get.calls` sequences ARE the adapter-parity regression test. Add one
  explicit test that `find_item` delegates (memoised single `q=` fetch for
  opaque+fuzzy: one call, not two).
- **`tests/test_cart_scan.py`**: known GTIN → 200 `added`, `match:
  "barcode"`, line upserted, full `cart` echo, **stock unchanged**
  (decision-6 pin on the scan path); re-scan increments; UPC-E-with-
  symbology finds the EAN-13-filed item; opaque SKU label → `match: "sku"`;
  unknown valid GTIN → 404 with `status/kind/gtin14/region/usage/notes/
  catalogable:true/suggestions` and **cart unchanged**; restricted unknown →
  404 `catalogable:true` with the restricted note; unknown opaque → 404
  `catalogable:false` + the UPC-E symbology hint for 6–8-digit payloads;
  misread → 422 `status:"misread"`, nothing stored; badge payload → 400
  `status:"badge"`; empty/whitespace → 422 (pydantic); `qty` bounds;
  unauthenticated 401; `must_change_password` 403; isolation (adds land in
  the *caller's* cart only); `symbology` respected (Code 128 digit payload
  → opaque, never GTIN — `OPAQUE_SYMBOLOGIES` parity).
- **`tests/test_items_suggest.py`**: `by_prefix` ranks longest-shared-first
  (fixtures: shared-9, shared-7, shared-5→excluded-by-floor, no-barcode and
  NULL rows ignored); GTIN-14 case code ranks with its GTIN-13 sibling
  (indicator-stripped comparison pin); `by_keyword` matches the `q=`
  behaviour incl. wildcard escaping (`q="%"` literal); `by_semantic` with
  the conftest fake embeds; **degradation**: monkeypatch `embed_text →
  None` and `vector_init` failure → `by_semantic == []` and **200, never
  503**; invalid `barcode` param → 422; limit bounds; auth 401; member role
  allowed.
- **`tests/test_scan_requests.py`**: member files from an unknown GTIN →
  201 with `kind/gtin14` captured; misread → 422 (never filed); badge →
  400; already-catalogued code → 409 `already_catalogued` + item; duplicate
  open (same user+code) → 409 `duplicate_open`; different user, same code →
  separate requests (no cross-user dedup); staff list filters by status,
  member → 403 on the staff list, `/mine` scoped to the caller; resolve
  links item (`status/resolution_item_id/resolved_by/resolved_at`), resolve
  non-open → 409, resolve unknown item → 404, resolve unknown request →
  404; dismiss; member cannot resolve/dismiss (403); **cleanup**: member
  delete removes their requests (direct DB count, cart-test style); item
  delete leaves the resolved request with `resolution_item: null` in the
  staff list (weak-ref tolerance).
- **`tests/test_barcodes.py`** additions: `common_prefix_len`;
  `rank_prefix_siblings` floor/tie/order/indicator-strip rules; empty and
  NULL-barcode inputs.
- **`tests/test_migrations.py`**: untouched (exact-set pin stays green —
  `scan_requests` is a `create_all()` table). Suite-wide: `uv run pytest`
  green; `test_metadata_and_ops.py` has no schema snapshot (verified in
  Task 2 review) — new paths only add OpenAPI entries.

**PR sequencing** (each step green): ① `app/scanlookup.py` + bridge adapter
(existing bridge tests are the acceptance gate); ② `app/barcodes.py`
helpers + tests; ③ schemas + `POST /api/cart/scan` + tests; ④
`keyword_filter`/`_vector_search_rows` extractions (behaviour-identical —
existing items tests are the gate) + `GET /api/items/suggest` + tests;
⑤ `ScanRequest` model/router/cleanup + tests; ⑥ docs lines
(`docs/barcodes.md` suggestion rule incl. the indicator-strip + floor
rationale; `docs/opticon-m10.md` §6a pointer that the bridge and server now
share one decision core).

## 6. Risks & open questions (Task 3-specific)

1. **Prefix heuristic honesty.** True GS1 company-prefix *length* is not
   derivable offline (GEPIR is a service); observed shared-prefix length on
   the indicator-stripped view is the best offline proxy, with `min_shared=6`
   as a tunable floor. Low catalogue density ⇒ few/empty suggestions —
   harmless (the form still works). Pinned by tests, documented in
   `docs/barcodes.md`; the coordinator may retune the floor without code
   changes (module constant).
2. **O(n) Python ranking per suggest call** — fine at shop scale (one
   indexed-column fetch + string compares); escape hatch (SQL LIKE ladder)
   documented, not built.
3. **Prefix-less badge tokens** (raw 43-char) fall to the opaque path and can
   end up as junk ScanRequests — accepted: the `HCRM1:` prefix is the
   badge contract everywhere else too (bridge, qr-login's raw-token
   acceptance is login-side only).
4. **Mixed `detail` shapes** (pydantic list-422 vs our dict-422/404/409):
   precedent exists (`list_items` string-422); clients branch on
   `detail["status"]` when dict. Task-5 SPA code must know this — flagged
   for that PR.
5. **Duplicate-open policy**: 409-with-existing chosen over silent update
   (kiosk double-submit shows "already reported — staff know"). Flip is
   one branch if the coordinator prefers upsert semantics.
6. **`GET /mine`** is a small scope addition beyond the coordinator's
   "member files; staff list/resolve/dismiss" — justified by the Task-5
   "your report was resolved" UI; droppable without trace.
7. **`create_item`'s 409 stays a string detail** (tests + consumers assert
   it): the Task-5 staff form pre-checks conflicts with
   `GET /api/items?barcode=` *before* submitting, which delivers the
   "already filed as {sku} — add that instead" UX with zero server churn.
   (Withdraws my phase-2 "structured 409" adoption — pre-checking beats
   contract churn.)
8. **Embeddings-down interactions**: `suggest`/`cart/scan` never 503
   (decision 7), but the staff *resolution* step (`POST /api/items`) still
   503s when the model is unavailable (existing, deliberate policy) —
   ScanRequests simply queue until embeddings recover; noted for docs.
9. **`resolve_scan` callback contract** is now a shared surface: Task 5's
   Phase-B relay (deferred) and any future kiosk ingest must call it, not
   re-derive — the module docstring says so in terms (same wording culture
   as `_upsert_line`'s "do not reinvent a racy version").
