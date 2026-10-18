# Task 3 FINAL spec — Scan resolution, /api/cart/scan, suggestions, ScanRequest

Merged from `task3-proposal-{glm,kimi,qwen}.md` + reviews. Implementer follows THIS.

## Shared core (`app/scanlookup.py`, new)

- `ItemLookup` Protocol with exactly three methods:
  `by_barcode(gtin14) -> list`, `by_sku(sku) -> list`, `search(q) -> list`.
- `resolve_scan(lookup, code, *, symbology="", fuzzy=False) -> ScanVerdict`
  (ScanVerdict: `item | None`, `match: "barcode"|"sku"|"search"|None`,
  `analysis: ScanAnalysis`). Doctrine is preserved from the bridge's
  `find_item`: gtin/restricted → exact canonical barcode only; invalid-gtin /
  empty → **never** looked up (zero resolver calls); opaque → exact
  case-insensitive SKU only; keyword hit only under explicit `fuzzy=True`.
- **Bridge**: `find_item` becomes a **memoising adapter** over its injected
  `get` seam calling `resolve_scan`. Its existing signature/return contract and
  all `tests/test_scanner_bridge.py` cases pass **unchanged** (that suite is the
  adapter parity guarantee). Memoise the adapter's lookups so the opaque+fuzzy
  path does not double-fetch `?q=`.
- **Parity suite** (in `tests/test_scanlookup.py`): one shared case list run
  against both a DB-backed resolver and a fake-HTTP backend — same input ⇒ same
  verdict. Plus a delegation-spy test and a "zero resolver calls on misread"
  test.

## `POST /api/cart/scan` (`app/routers/cart.py`)

- `CartScanIn {code: str(max 64), symbology: str = "", qty: int = 1 (1..10000)}`.
- Badge payload (`HCRM1:` prefix) → **400** `{status:"badge"}`.
- `resolve_scan(..., fuzzy=False)` — never guess.
  - hit → **200** `{status:"added", match, item: ItemOut, qty, cart: CartOut}`
    via Task 1's `_upsert_line` (stock untouched).
  - invalid-gtin → **422** `{status:"misread"}` (mirrors `?barcode=` 422; **no**
    suggestions).
  - unknown → **404** `{status:"unknown", message (verbatim bridge verdict
    string incl. UPC-E symbology hint), analysis{kind, gtin14, format, prefix,
    region, usage, globally_unique, notes, info}, catalogable: bool,
    suggestions{by_prefix, by_keyword}}`. The message/analysis/catalogable/
    suggestions fully drive the Task-5 name+price UI (one round-trip).

## Suggestions

- `app/barcodes.py`: `rank_prefix_siblings(gtin14, items) -> list` — rank by
  longest shared prefix on the **indicator-stripped GTIN-13 view**, with leading
  zeros stripped on both sides before comparison; require ≥ 6 significant
  shared digits; tie-break by name. (Avoids the pure-padding EAN-8 false
  sibling.)
- Extract `keyword_filter(statement, q)` (from `list_items`) and
  `_vector_search_rows(...)` (from `vector_search`) — leave tested behaviour
  byte-identical; one keyword dialect for all consumers.
- `GET /api/items/suggest?barcode=&q=&limit=` → `{by_prefix, by_keyword,
  by_semantic}`. `by_semantic` uses `_vector_search_rows` and **degrades to []**
  on any embedding/extension unavailability — the suggest endpoint itself never
  returns 503. The 404 detail and the endpoint share one assembler.

## ScanRequest (`app/models.py` + `app/routers/scan_requests.py`)

- New table `scan_requests` (create_all; no migration entries):
  `id`, `requester_id: int = FK users.id index`, `code: str`, `symbology: str`,
  `kind: str`, `gtin14: str | None`, `name: str`, `price_cents: int`,
  `status: str` (open|resolved|dismissed), `resolved_item_id: int | None`
  (**plain int weak ref** — no FK, audit trail survives item deletion),
  `resolved_by: int | None`, `resolved_at`, `created_at`, `updated_at`.
- `POST /api/scan-requests` (any authenticated, `ScanRequestIn {code,
  symbology="", name, price_cents}`): server re-runs `analyze_scan`; badge →
  400; misread → 422; already-catalogued code → **409** `{status:"already",
  item}`; duplicate-open (same user+code) → **409** `{status:"duplicate",
  request}`. Members never write the catalogue.
- `GET /api/scan-requests/mine` (any auth) — own requests.
- `GET /api/scan-requests?status=` (**staff**) — list.
- `POST /api/scan-requests/{id}/resolve` (**staff**, `{item_id,
  add_to_cart: bool = false}`): mark resolved (resolved_item_id, resolved_by,
  resolved_at); if `add_to_cart` → `_upsert_line` into the requester's cart.
- `POST /api/scan-requests/{id}/dismiss` (**staff**). Non-open transitions →
  409.
- Cleanup: `delete_member` removes the member's scan_requests in the same txn.

## Tests

- `tests/test_scanlookup.py` (decision table + parity + spy + zero-calls-on-misread).
- `tests/test_cart_scan.py` (gates, badge 400, misread 422, UPC-E→EAN-13
  identity, both match kinds, 404 shape incl. no-suggestions-on-misread,
  stock-untouched, per-user isolation, qty).
- `tests/test_items_suggest.py` (ranking + floor + indicator-strip, wildcard
  escaping, keyword parity, semantic degrade-to-empty).
- `tests/test_scan_requests.py` (full lifecycle, 409 already/duplicate,
  resolve+dismiss+409-on-non-open, add_to_cart, cleanup, weak-ref tolerance).
- `tests/test_barcodes.py` additions; `test_scanner_bridge.py` unchanged and green.

## Out of scope

Frontend (Task 5), badge login flows, the USB-COM relay (Phase B).
