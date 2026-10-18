# Task 3 proposal (kimi-k3): Scan resolution core + `/api/cart/scan` + suggestions + member ScanRequest

Coordinator plan reference: `docs/plans/COORDINATOR.md` Task 3 (backend only).
Binding decisions honoured: **one shared scan-resolution core** (bridge and
server never drift on identity); suggestions = **GS1 company-prefix siblings
+ keyword tokens**, semantic degrades to empty on 503; **staff-only**
catalogue creation — members file a **ScanRequest**; adding to cart **never
mutates stock** (Task 1's `_upsert_line` is the only write path).

Verified against the working tree (Tasks 1–2 landed): `app/barcodes.py::
analyze_scan`, `scripts/scanner_bridge.py::find_item` + its injected-`get`
test seam in `tests/test_scanner_bridge.py`, `app/routers/items.py`
(`?barcode=`/`?q=`/`vector-search`), `app/routers/cart.py::_upsert_line`,
`app/models.py`, `create_all()` migration story.

---

## 1. Shared core: `app/scanlookup.py`

### What must not drift

The doctrine lives in the *decision table*, not in the fetch mechanics:
(a) GTIN/`restricted` → exact canonical-barcode lookup only; (b)
`invalid-gtin`/`empty` → **never looked up** (misread: re-scan, never file);
(c) `opaque` → exact **case-insensitive SKU equality** only; (d) a keyword
hit is a match ONLY under an explicit `fuzzy` opt-in, and never silently
(REVIEW-m10.md P1 — the incident where scanning "water" decremented the
water bottle's stock). The bridge already shares `analyze_scan` with the
server; this module extends the same sharing one level up.

### Shape

```python
# app/scanlookup.py
"""Exact-identity scan resolution: ONE decision table, two backends.

The `Lookup` protocol is the fetch seam; `resolve_scan` is the doctrine.
"""
from dataclasses import dataclass
from typing import Any, Protocol

from app.barcodes import ScanAnalysis, analyze_scan


class Lookup(Protocol):
    """Backend-specific exact fetches. `item` is backend-opaque (a dict for
    the bridge's HTTP client, an Item row for the server)."""
    def by_barcode(self, gtin14: str) -> Any | None: ...   # exact canonical GTIN-14
    def by_sku(self, code: str) -> Any | None: ...         # EXACT, case-insensitive
    def search(self, text: str) -> list[Any]: ...          # keyword candidates (fuzzy only)


@dataclass(frozen=True)
class ScanVerdict:
    analysis: ScanAnalysis
    item: Any | None          # the matched item (backend's type), else None
    how: str | None           # "barcode" | "sku" | "search" | None


def resolve_scan(lookup: Lookup, code: str, symbology: str = "",
                 fuzzy: bool = False) -> ScanVerdict:
    analysis = analyze_scan(code, symbology)

    if analysis.kind in ("gtin", "restricted"):
        item = lookup.by_barcode(analysis.key)
        return ScanVerdict(analysis, item, "barcode" if item is not None else None)

    if analysis.kind in ("invalid-gtin", "empty"):
        return ScanVerdict(analysis, None, None)      # never looked up

    # opaque: exact SKU equality only; keyword search is an explicit opt-in.
    item = lookup.by_sku(code)
    if item is not None:
        return ScanVerdict(analysis, item, "sku")
    if fuzzy:
        hits = lookup.search(code)
        if hits:
            return ScanVerdict(analysis, hits[0], "search")
    return ScanVerdict(analysis, None, None)
```

### Server backend (`app/scanlookup.py`, continued)

```python
class DbLookup:
    def __init__(self, session: Session): ...
    def by_barcode(self, gtin14):  # select(Item).where(Item.barcode == gtin14)
    def by_sku(self, code):        # select(Item).where(func.lower(Item.sku) == code.strip().lower())
    def search(self, text):        # same escaped-ILIKE filter as list_items? q=
```

The `?q=` ILIKE filter is factored out of `items.py::list_items` into a
reusable `item_search_filter(q)` (column expression) so `DbLookup.search`
and the endpoint cannot drift on wildcard-escaping either (small,
mechanical refactor; existing item tests pin it).

### Bridge side — what happens to `find_item`

`scripts/scanner_bridge.py::find_item` becomes a **thin adapter**, keeping
its signature and its injected-`get` seam (its offline tests are the P1
regression suite — they must pass unchanged):

```python
def find_item(get, code, fuzzy=False, symbology=""):
    lookup = _HttpLookup(get)     # by_barcode: GET ?barcode= ; by_sku: GET ?q=
                                  #   + client-side exact case-insensitive filter
                                  #   (API has no exact-SKU query); search: GET ?q=
    verdict = resolve_scan(lookup, code, symbology, fuzzy)
    return verdict.item, verdict.how, verdict.analysis
```

The exact-SKU filter on the bridge stays client-side (the API offers
substring search, not exact-SKU) — that is fetch mechanics, not doctrine;
the *requirement* "exact, case-insensitive" is stated on the `Lookup`
protocol and pinned for **both** backends by the parity suite (§5): one
shared case list (canonical GTIN, UPC-E→EAN-13, bare-6-digits never
guessed, opaque SKU case variants, "water" never matches, fuzzy opt-in,
misread never looked up, restricted matches exactly) executed against the
bridge's `fake_api` adapter AND against `DbLookup` on a test DB. Same
input, same verdict — that is the no-drift guarantee made executable.

## 2. `POST /api/cart/scan`

In `app/routers/cart.py` (it is a cart operation; keeps `_upsert_line` and
`_cart_out` private to one module).

### Request schema

```python
class CartScanIn(_Stripped):
    code: str = Field(min_length=1, max_length=64)
    symbology: str = Field(default="", max_length=32)
```

### Flow

1. **Badge gate**: `parse_badge_payload(payload.code)` → **400**
   `"QR badges are login credentials, not catalogue items"`. (Prefixed
   form only — same gate as the bridge's `startswith(BADGE_PREFIX)`. A raw
   43-char token is treated as an opaque label here, matching the bridge;
   the badge parser's strictness exists to keep product barcodes out of
   *login*, not vice versa.)
2. `verdict = resolve_scan(DbLookup(session), code, symbology)` — `fuzzy`
   is **not exposed**: the cart path never guesses (P1; the bridge's
   `--fuzzy` stays a back-office CLI opt-in).
3. **`invalid-gtin` → 422** `"misread — check digit fails; re-scan, do not
   file"` (mirrors `GET /api/items?barcode=`'s 422-on-invalid: a misread is
   a client error, never an empty result, never a filing). `empty` cannot
   occur (schema `min_length=1`).
4. **Match** → `_upsert_line(session, user.id, item.id, 1)` (Task 1's
   single-statement upsert — concurrent scans cannot lose an increment;
   stock untouched) → **200**:
   ```python
   class CartScanOut(BaseModel):
       cart: CartOut          # full cart, scanner-cadence contract
       item: ItemOut          # what was added (the toast)
       match: str             # "barcode" | "sku"
   ```
5. **No match** → **404** with a **structured detail object** (FastAPI
   serialises a dict `detail` verbatim) — the payload that drives the later
   name+price intake UI in one round-trip:
   ```json
   {"detail": {
     "reason": "unknown_barcode",            // or "unknown_sku" for opaque
     "code": "<raw scan>",
     "analysis": {"kind": "gtin|restricted|opaque", "key": "<gtin14|raw>",
                  "addon": ""},
     "info": {"gtin14": "…", "format": "GTIN-13", "prefix": "841",
              "region": "Spain & Andorra", "usage": "retail",
              "globally_unique": true, "notes": ["…"]},   // null for opaque
     "suggestions": [ItemOut, …]             // §3, prefix+keyword, ≤5
   }}
   ```

**Why 404-with-structured-detail and not a 200 status-union**: the repo
already answers 404 for "item not found" (`get_item`) and 422 for
"misread" (`?barcode=`); a scan that identifies nothing *is* a 404, and
the UI branches on status, not on parsing a 200 envelope. (`info` is
`BarcodeInfo.__dict__` minus internals, or None for opaque.)

Auth: `get_current_user` — 401 unauthenticated, 403 while
`must_change_password` (inherits the gate, as the rest of the cart).

## 3. Suggestions — "closest matching items using the barcode system"

### `prefix_candidates(gtin14)` in `app/barcodes.py` (pure, stdlib)

```python
def prefix_candidates(gtin14: str, min_len: int = 7) -> list[str]:
    """GS1 company-prefix LIKE candidates, longest first.

    Stored barcodes are canonical zero-padded GTIN-14, so prefix matching
    compares (format family + company prefix): items filed under the same
    leading digits are, with high GS1 probability, the same brand/vendor —
    the shop's usual substitute. Digit-only input ⇒ no LIKE-escape hazard.
    """
    if not (gtin14.isdigit() and len(gtin14) == 14):
        raise ValueError("prefix_candidates expects a 14-digit GTIN")
    return [gtin14[:n] for n in range(13, min_len - 1, -1)]
```

(Padding note, documented in the docstring and `docs/barcodes.md`: an
EAN-13 and a UPC-A from the same company pad to *different* leading digits,
so siblings group by format family too — that is a feature: "closest via
the barcode system", not a fuzzy text guess.)

### Shared assembler (server, in `app/routers/items.py` or `scanlookup.py`)

```python
def suggest_items(session, *, gtin14: str | None, q: str = "", limit: int = 5,
                  semantic: bool = False) -> dict:
    # by_prefix: for each candidate (longest first), Item.barcode.startswith(p),
    #   dedupe, rank longest-match-first, stop at limit. Only when gtin14 given.
    # by_keyword: item_search_filter(q) when q non-empty, ≤ limit.
    # by_semantic: only when explicitly requested AND q non-empty: wrap the
    #   vector-search internals; ANY failure (extension missing, model
    #   unavailable, 503) → []. Suggestions must never fail a scan flow.
```

### Two surfaces, one helper

- **Embedded in the `/api/cart/scan` 404** (`suggestions` above):
  `by_prefix` for the scanned GTIN, topped up with `by_keyword` on the raw
  code if prefix yields < limit. **Semantic is NOT run here** — embedding a
  query per unknown scan is wasted cost (there is no name to embed yet).
- **`GET /api/items/suggest?barcode=&q=&limit=`** (auth required): returns
  `{by_prefix, by_keyword, by_semantic}` — this is the live
  suggest-as-you-type endpoint for the intake form's name field (semantic
  included here, degrading to `[]` on 503/unavailable). 422 on an invalid
  GTIN argument (same rule as `?barcode=`).

## 4. Member ScanRequest

### Model (`app/models.py`, new table — `create_all()`, no migration entries)

```python
class ScanRequest(SQLModel, table=True):
    """A member-filed 'unknown item' request for staff to resolve."""

    __tablename__ = "scan_requests"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)   # who filed
    code: str                            # raw scan as received
    symbology: str = ""
    kind: str                            # "gtin" | "restricted" | "opaque"
    gtin14: str | None = Field(default=None, index=True)  # canonical key when GTIN
    name: str = ""                       # member's proposal (optional)
    price_cents: int | None = Field(default=None)
    status: str = Field(default="open", index=True)  # open|resolved|dismissed
    resolved_item_id: int | None = Field(default=None, foreign_key="items.id")
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)
```

FKs declarative only ⇒ **cleanup**: `delete_member` also deletes the user's
scan requests (extend the Task-1 cleanup block); item delete does **not**
touch requests (a resolved request keeps `resolved_item_id` as history —
if the item is later deleted, the row stays; staff list displays tolerate
a dangling id by joining defensively… simpler: item delete sets
`resolved_item_id = NULL` via a targeted UPDATE — one extra statement in
`delete_item`'s existing cleanup block. Take the UPDATE; history
survives, no dangling references).

### Endpoints — new `app/routers/scanrequests.py`, `prefix="/api/scan-requests"`

| Method & path | Auth | Body | Success | Errors |
|---|---|---|---|---|
| `POST ""` | any user | `ScanRequestIn {code, symbology="", name="", price_cents: int\|None}` | **201** `ScanRequestOut`; **200** with the existing row if the same user already has an **open** request for the same code/gtin14 (idempotent re-scan) | 401, 403 (gate), 422 misread (`invalid-gtin`) / badge payload (400) |
| `GET "/mine"` | any user | — | `list[ScanRequestOut]` (own, newest first) | 401 |
| `GET ""` | staff | `?status=open` | `list[ScanRequestOut]` | 401, 403 member |
| `POST "/{id}/resolve"` | staff | `{item_id}` | 200 `ScanRequestOut` (status→resolved, links item) | 404 request/item, 409 already resolved/dismissed |
| `POST "/{id}/dismiss"` | staff | — | 200 `ScanRequestOut` (status→dismissed) | 404, 409 not open |

Validation on file: `parse_badge_payload` → 400 (badges are credentials);
`analyze_scan(code, symbology)`: `invalid-gtin`/`empty` → 422 (misreads
are never filed — the intake doctrine); `gtin`/`restricted` store
`gtin14=key`; `opaque` stores raw `code`. **If the barcode is already in
the catalogue** (race: item created after the scan 404'd), the POST still
succeeds — staff resolve is where duplicates are handled (the resolve
screen in Task 5 shows the existing match); flagged as a cheap
enhancement: the POST response includes `existing_item: ItemOut | None`
when the GTIN now matches, so the UI can short-circuit. Include it — one
query.

`ScanRequestOut`: id, code, kind, gtin14, name, price_cents, status,
resolved_item_id, created_at + `filer: {id, name}` for the staff list
(public fields only).

### Lifecycle

unknown scan → member files request (with optional name/price) → staff
sees it in the open list → staff creates the catalogue item via the
**existing** `POST /api/items` (barcode pre-filled from `gtin14`; the
schema's GTIN validation and duplicate-barcode 409 apply unchanged) →
`POST /{id}/resolve {item_id}` closes the loop (or `/dismiss` for junk).
No new catalogue-write path is created — the staff-only invariant is
untouched.

## 5. Tests

**`tests/test_scanlookup.py`** (new — the parity suite):
- One shared case list mirroring `test_scanner_bridge.py`'s P1/P5 pins
  (canonical GTIN hit; UPC-E finds EAN-13-filed item; bare 6 digits never
  guessed; opaque exact-SKU incl. case variants; "water" partial word →
  no match; same with `fuzzy=True` → first hit; misread → verdict with
  `item None`, **lookup never called** — assert with a counting Lookup;
  restricted-circulation code matches exactly) executed against (a)
  `DbLookup` on the test DB and (b) the bridge's `_HttpLookup` over the
  existing `fake_api` table. Same input ⇒ same verdict, both backends.
- `prefix_candidates`: shape (lengths 13→7, longest-first), digit-check
  422-class error, no wildcard characters possible.

**`tests/test_cart_scan.py`** (new):
- 401 unauthenticated; 403 `must_change_password` (reuse Task 1's
  `_flagged_token` helper — move it to `conftest.py` so both files share).
- Badge payload → 400, cart untouched.
- Known GTIN adds (200 `CartScanOut`, `match="barcode"`); re-scan → qty 2
  (upsert); UPC-E payload finds EAN-13-filed item; opaque SKU adds
  (`match="sku"`).
- `invalid-gtin` → 422, cart untouched; `empty`/whitespace → 422 (schema).
- Unknown GTIN → 404: detail has `reason`, `analysis.kind="gtin"`, `info`
  (region/usage), and `suggestions` — prefix siblings ranked
  longest-first, unrelated items excluded, ≤5; unknown opaque → 404
  `reason="unknown_sku"`.
- Stock-untouched pin (scan qty>stock item; `items.stock` unchanged).

**`tests/test_items_suggest.py`** (new):
- `by_prefix` ranking/dedup/limit; `by_keyword` escaped-ILIKE parity with
  `?q=`; `by_semantic` returns results with the conftest fake embeddings
  and `[]` when the model is patched unavailable (503-degradation);
  invalid `barcode=` → 422; 401.

**`tests/test_scan_requests.py`** (new):
- Member files (201; gtin14 stored for GTIN codes; opaque stores raw);
  re-file same open code → 200 same row (idempotent); misread → 422;
  badge → 400; `existing_item` short-circuit when the GTIN is already
  filed.
- `/mine` lists only own; member `GET ""` → 403; staff list filtered by
  status.
- Resolve: staff creates item (existing endpoint) → resolve links it →
  **lifecycle closes**: `POST /api/cart/scan` of that code now adds to
  cart; resolve unknown item_id → 404; double-resolve → 409; dismiss →
  200, further resolve → 409.
- Cleanup: `delete_member` removes the user's requests (direct DB check);
  item delete NULLs `resolved_item_id` (direct DB check).

**Existing suites**: `tests/test_scanner_bridge.py` must pass **unchanged**
(adapter preserves `find_item`'s exact semantics — the P1/P2 pins are the
contract); `tests/test_items.py` green through the `item_search_filter`
refactor; `tests/test_migrations.py` exact-set test untouched (new tables
only — assert `scan_requests` appears after `create_all()` on a
legacy-shape DB, same style as the Task-1 cart case).

## 6. Risks / open questions (Task-3-scoped)

- **Q1 — 404-with-dict-detail** is my recommendation over a 200
  status-union (repo precedent: `get_item` 404, `?barcode=` 422; UI
  branches on status). If the coordinator prefers the union, only the
  endpoint envelope changes — the flow and schemas are identical.
- **Q2 — Padded-prefix sibling semantics** (§3): grouping is
  (format-family + company), so an EAN-13 scan will not suggest UPC-A
  filings of the same brand. Acceptable and arguably correct ("closest via
  the barcode system"); the alternative (strip padding, compare
  significant digits) cross-matches formats at the cost of false siblings
  across number systems. Flag for confirmation.
- **R1 — Semantic bucket availability in tests**: conftest patches
  embeddings with deterministic fakes, so `by_semantic` is exercised
  cheaply; the degradation case patches `embed_text` to None. No new
  infrastructure.
- **R2 — `existing_item` short-circuit in ScanRequest POST** adds one
  query to a member-facing endpoint; bounded, indexed (`items.barcode`
  unique index). Fine.
- **R3 — Suggestion cost on 404**: prefix LIKEs on the indexed `barcode`
  column, ≤8 probes, no embedding call. Documented catalogue-scale note
  (fine for a shop; not a general search engine).
- **R4 — `resolve_scan` is sync and pure**; `DbLookup` does one query per
  call. The scan endpoint thus issues ≤2 queries before the upsert — no
  transaction-mode changes, no new pool pressure (the `stock-adjust`
  machinery remains unjustified here, same argument as Task 1).
- **R5 — opaque `code` length cap 64** matches `QrLoginIn.token`; Code 128
  labels longer than 64 chars would 422 — acceptable for shop labels,
  noted in the schema docstring.
- **R6 — drift watch**: the parity suite is the contract, but it only
  runs cases someone wrote down. When a future symbology/edge case is
  added to `analyze_scan`, the parity case list must grow with it —
  one-line note in `app/scanlookup.py`'s docstring.

## 7. Files touched (review checklist)

| File | Change |
|---|---|
| `app/scanlookup.py` | **new** — `Lookup`, `ScanVerdict`, `resolve_scan`, `DbLookup` |
| `app/barcodes.py` | + `prefix_candidates` |
| `app/routers/cart.py` | + `POST /scan` (badge gate, verdict flow, `_upsert_line`) |
| `app/routers/items.py` | factor `item_search_filter`; + `GET /suggest`; `delete_item` cleanup += NULL `resolved_item_id` |
| `app/routers/scanrequests.py` | **new** — file/mine/list/resolve/dismiss |
| `app/models.py` | + `ScanRequest` |
| `app/schemas.py` | + `CartScanIn`, `CartScanOut`, `ScanRequestIn`, `ScanRequestOut`, `ScanResolveIn`, suggest out |
| `app/main.py` | + `include_router(scanrequests.router)` |
| `app/routers/members.py` | `delete_member` cleanup += scan requests |
| `scripts/scanner_bridge.py` | `find_item` → adapter over `resolve_scan` (+ `_HttpLookup`); behaviour unchanged |
| `tests/` | `test_scanlookup.py`, `test_cart_scan.py`, `test_items_suggest.py`, `test_scan_requests.py` (new); `conftest.py` (+`_flagged_token`); bridge/items/migrations suites green unchanged |
| `docs/barcodes.md` | suggestion rule paragraph (prefix semantics + misreads never suggested) |
