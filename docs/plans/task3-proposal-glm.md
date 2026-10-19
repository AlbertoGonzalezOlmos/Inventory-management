# Task 3 proposal (glm) — Scan resolution core, `/api/cart/scan`, suggestions, member ScanRequest

Coordinator Task 3 (`docs/plans/COORDINATOR.md`): backend only. Binding
decisions: **one shared scan-resolution core** (`app/scanlookup.py`, extracted
from the bridge's tested `find_item` — no drift between bridge and server);
suggestions = **GS1 company-prefix siblings + keyword**, semantic degrades to
`[]` on 503; **staff-only catalogue creation**, members file a `ScanRequest`
for unknown items; adding to cart **never mutates stock**.

Ground truth verified this pass: `app/barcodes.py` —
`analyze_scan(barcode, symbology) -> ScanAnalysis{raw, symbology, kind
(gtin|restricted|invalid-gtin|opaque|empty), key, addon, info:
BarcodeInfo{gtin14, format, indicator, prefix, region, usage,
globally_unique, notes}}` (pure stdlib, frozen dataclasses);
`scripts/scanner_bridge.py::find_item(get, code, fuzzy=False, symbology="")`
→ `(item, how, analysis)` with `how ∈ {"barcode","sku","search",None}`, the
`get(path)` HTTP-injection seam, and the never-guess doctrine
(REVIEW-m10.md P1); `app/routers/items.py` (`?barcode=` 422-on-misread,
`?q=` escaped-ILIKE, `/vector-search` 503-on-degradation);
`app/routers/cart.py::_upsert_line(session, user_id, item_id, qty) -> bool`
(Task 1, commits, race-free); `app/models.py` (Item.sku/barcode/…; new
tables via `create_all()`, no migration entries).

---

## 1. Shared core: `app/scanlookup.py`

### The shape

The decision table needs exactly three queries — *item by canonical GTIN*,
*item by exact SKU*, and (fuzzy opt-in only) *keyword hits*. So the core
takes a **resolver** with those three methods and owns the table once:

```python
# app/scanlookup.py  (new)
"""The one scan-resolution decision table, shared by the API and the bridge.

Extracted from scripts/scanner_bridge.py::find_item (whose behaviour and
tests are preserved verbatim): a scanned code resolves to a catalogue item
by EXACT identity only — the canonical GTIN for a retail barcode, the exact
SKU for an opaque label — and a code that identifies nothing is reported as
identifying nothing. There is deliberately no silent fallback to "the first
search hit" (REVIEW-m10.md P1). Both callers inject a resolver: the bridge
an HTTP-backed one (its tested `get` seam), the server a Session-backed
one. Because both literally call resolve_scan(), the two paths cannot
drift on identity.
"""

from dataclasses import dataclass
from typing import Any, Protocol

from app.barcodes import ScanAnalysis, analyze_scan


class ItemResolver(Protocol):
    """The three lookups the decision table needs. `by_keyword` is only
    ever called with fuzzy=True (bridge opt-in); the server never passes it."""

    def by_barcode(self, gtin14: str) -> Any: ...
    def by_sku(self, sku: str) -> Any: ...
    def by_keyword(self, q: str, limit: int = 100) -> list[Any]: ...


@dataclass(frozen=True)
class ScanResolution:
    item: Any | None        # whatever the resolver returns (ORM Item | dict)
    match: str | None       # "barcode" | "sku" | "search" | None
    analysis: ScanAnalysis


def resolve_scan(code: str, resolver: ItemResolver, *,
                 symbology: str = "", fuzzy: bool = False) -> ScanResolution:
    analysis = analyze_scan(code, symbology)

    if analysis.kind in ("gtin", "restricted"):
        # A GTIN is a global product identity: exact canonical match only.
        item = resolver.by_barcode(analysis.key)
        return ScanResolution(item, "barcode" if item is not None else None, analysis)

    if analysis.kind in ("invalid-gtin", "empty"):
        # Misread / nothing: NEVER looked up — a scanning error must not
        # become a phantom identity or a wrong match.
        return ScanResolution(None, None, analysis)

    # Opaque payload (Code 128 SKU label, internal code): exact SKU equality
    # only, case-insensitive. A short all-digit payload is NOT guessed into
    # a GTIN (app.barcodes refuses without the scanner's word).
    item = resolver.by_sku(analysis.raw)
    if item is not None:
        return ScanResolution(item, "sku", analysis)
    if fuzzy:
        hits = resolver.by_keyword(analysis.raw, limit=100)
        if hits:
            return ScanResolution(hits[0], "search", analysis)
    return ScanResolution(None, None, analysis)
```

(Uses `analysis.raw` for the opaque path so the addon-stripped, trimmed
payload is what gets compared — matching the bridge's use of `code` after
`analyze_scan` had already normalised… note: the bridge compares the raw
`code` argument, and `analyze_scan` strips/normalises into `.raw`; using
`.raw` is the faithful equivalent and slightly more correct for
whitespace-padded input.)

### The two resolvers

**Server** (in `app/scanlookup.py` or `app/routers/cart.py` — propose
`scanlookup.py`, it is the core's companion):

```python
class SessionItemResolver:
    """Resolver over the ORM session — the server side of the shared table."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def by_barcode(self, gtin14: str) -> Item | None:
        return self.session.exec(
            select(Item).where(Item.barcode == gtin14)
        ).first()

    def by_sku(self, sku: str) -> Item | None:
        # Case-insensitive exact equality. The bridge achieves this via
        # ?q=<code> + an exact lower()== filter client-side; a direct
        # lower() comparison is the same semantics without depending on
        # the search endpoint's escaping. Small catalogues: no functional
        # index needed (documented).
        return self.session.exec(
            select(Item).where(func.lower(Item.sku) == sku.strip().lower())
        ).first()

    def by_keyword(self, q: str, limit: int = 100) -> list[Item]:
        # Never called by the server (fuzzy stays a bridge opt-in); exists
        # to satisfy the Protocol and keep the table complete.
        needle = f"%{q.replace('%', '').replace('_', '')}%"
        return self.session.exec(
            select(Item).where(or_(Item.name.ilike(needle), Item.sku.ilike(needle),
                                   Item.description.ilike(needle)))
            .limit(limit)
        ).all()
```

**Bridge** — `find_item` becomes a thin adapter that keeps its exact
signature and return contract, so **`tests/test_scanner_bridge.py` passes
unchanged** (that suite pinning the adapter IS the parity guarantee, since
the adapter now delegates to the same `resolve_scan` the server calls):

```python
# scripts/scanner_bridge.py
class _HttpResolver:            # adapter over the existing `get` seam
    def __init__(self, get): self._get = get
    def by_barcode(self, gtin14):
        _, items = self._get(f"/api/items?barcode={urllib.parse.quote(gtin14)}")
        return items[0] if items else None
    def by_sku(self, sku):
        _, items = self._get(f"/api/items?q={urllib.parse.quote(sku)}&limit=100")
        for item in items:
            if item["sku"].lower() == sku.lower():
                return item
        return None
    def by_keyword(self, q, limit=100):
        _, items = self._get(f"/api/items?q={urllib.parse.quote(q)}&limit={limit}")
        return items

def find_item(get, code, fuzzy=False, symbology=""):
    """Unchanged contract (item, how, analysis); now delegates to the shared
    app.scanlookup.resolve_scan — the drift-proof core the server uses too."""
    resolution = resolve_scan(code, _HttpResolver(get),
                              symbology=symbology, fuzzy=fuzzy)
    return resolution.item, resolution.match, resolution.analysis
```

The bridge's per-path docstring/verdict logic (the "new-entry candidate"
strings, the UPC-E `--symbology` hint) stays in `on_scan`, untouched — only
the identity decision moves into the core.

**What happens to the bridge's `find_item`:** it survives with an identical
signature, return shape and tests; its body shrinks to the adapter +
delegation. The decision table itself (currently ~30 lines inside
`find_item`) is deleted from the bridge and lives once in `scanlookup.py`.

## 2. `POST /api/cart/scan` (in `app/routers/cart.py`)

### Request schema (`app/schemas.py`)

```python
class CartScanIn(_Stripped):
    """One scanned code, resolved against the catalogue and added to the
    caller's cart on an exact identity match."""
    code: str = Field(min_length=1, max_length=64)   # fits HCRM1: payloads (49)
    symbology: str = Field(default="", max_length=32)  # scanner-reported, optional
    qty: int = Field(default=1, ge=1, le=CART_QTY_MAX)
```

### Resolution flow

1. **Badge guard first** (the bridge's invariant: badges never reach the
   catalogue): `parse_badge_payload(code)` is not None → **400**
   `{"detail": "QR badge payload — use POST /api/auth/qr-login"}`. (A raw
   43-char badge token without the prefix falls through as an opaque code
   and simply misses — harmless.)
2. `resolution = resolve_scan(code, SessionItemResolver(session),
   symbology=payload.symbology)` — **fuzzy is never passed**; the API path
   never guesses (P1).
3. Outcomes:

| Outcome | Status | Body |
|---|---|---|
| Match (`match` = barcode/sku) | **200** | `{"status": "added", "match": …, "item": ItemOut, "qty": <resulting line qty>, "cart": CartOut}` — the line added via `_upsert_line(session, user.id, item.id, payload.qty)` (Task 1's race-free upsert; commits; stock untouched per decision 6) and, per Task 1's contract, the full updated `CartOut` so the SPA re-renders from one payload. `item` + resulting `qty` are for the toast. |
| Valid GTIN / restricted / opaque, no match | **404** | Structured detail (below) — drives the Task-5 name+price modal. |
| `invalid-gtin` (misread) | **422** | `{"detail": {"message": "Misread barcode (check digit fails) — re-scan, do not file", "code": …, "analysis": <serialized>}}`. **No suggestions** — a misread must never be suggested around (mirrors `GET /api/items?barcode=`'s 422-on-misread). |
| Badge payload | **400** | flat string detail as above. |
| Bad input (empty >64 chars, qty bounds, extra fields) | **422** | pydantic, as everywhere. |
| Unauthenticated / `must_change_password` | 401 / 403 | inherited from `get_current_user`. |

### The 404 structured detail (the name+price UI's data)

```json
{
  "detail": {
    "message": "No catalogue match for this code",
    "code": "5901234123457",
    "symbology": "",
    "analysis": {
      "kind": "gtin", "key": "05901234123457", "addon": "",
      "info": {"gtin14": "05901234123457", "format": "GTIN-13",
               "indicator": null, "prefix": "590", "region": "Poland",
               "usage": "retail", "globally_unique": true, "notes": []}
    },
    "suggestions": {
      "by_prefix":  [ItemOut, …],   // GS1 company-prefix siblings (§3); GTIN scans only
      "by_keyword": [ItemOut, …]    // keyword hits on the code string (the useful
                                     // group for opaque labels: SKU fragments)
      // by_semantic is NOT embedded: a barcode carries no text to embed.
    }
  }
}
```

`analysis`/`info` are serialised from the frozen dataclasses by a small
`_analysis_dict(analysis)` helper (dataclasses.asdict-ish, explicit fields —
`notes` list included). The UI uses `info.region`/`usage` for the
"barcode system" context line, `suggestions` for the pick-list, and posts
`code`+`symbology` back with the typed name/price (§4).

## 3. Suggestions — "closest matching items via the barcode system"

### `prefix_candidates` (pure, in `app/barcodes.py`)

```python
def prefix_candidates(gtin14: str) -> list[str]:
    """Progressively shorter GS1 company-prefix patterns for a canonical
    GTIN-14, longest (most specific: same company AND same packaging
    family) first: lengths 13 down to 7. Digits only by construction
    (gtin14 is validated); assert defensively — a wildcard must never
    reach the LIKE."""
    assert gtin14.isdigit() and len(gtin14) == 14
    return [gtin14[:n] for n in range(13, 6, -1)]
```

Same company prefix ⇒ same manufacturer ⇒ the usual substitute — that *is*
"closest via the barcode system". Ranking: longest prefix first; cap the
merged list at ~5.

### Where suggestions are computed — one function, two surfaces

```python
# app/routers/items.py (or a small app/suggest.py used by both routers)
def suggest_items(session, *, barcode: str = "", q: str = "", limit: int = 5
                  ) -> dict[str, list[ItemOut]]:
    """by_prefix: items whose canonical barcode LIKE '<candidate>%' (GTIN scans
    only; empty when the code is not a GTIN). by_keyword: the existing
    escaped-ILIKE q= semantics on the given text. by_semantic: vector search
    on the text — DEGRADES TO [] when the embedding model/vector extension is
    unavailable (a 503 from the search path is a suggestion miss, never a
    scan-flow failure)."""
```

- **Surface 1 — `GET /api/items/suggest?barcode=&q=&limit=`**
  (authenticated, `get_current_user`; sits in `items.py` next to its
  siblings): response `{"by_prefix": [ItemOut…], "by_keyword": [ItemOut…],
  "by_semantic": [ItemOut…]}`. This is what the Task-5 modal calls *live*,
  as the user types the name (`q=`), and what re-queries prefix siblings
  (`barcode=`). All groups are plain `ItemOut` for a uniform pick-list
  (similarity scores are noise for suggestions — noted as a deliberate
  simplification).
- **Surface 2 — embedded in the 404 detail** (§2): `by_prefix` (from the
  scanned code, when it is a GTIN) + `by_keyword` (from the code string —
  for opaque labels this is the group that finds SKU-fragment matches).
  `by_semantic` is not embedded (no text yet). Same single `suggest_items`
  implementation — no second suggestion brain.

The semantic degrade: wrap the embed+vector-scan in the same try/except-503
shape `/vector-search` already uses; on any unavailability return `[]`
rather than raising. With the conftest's fake embeddings, tests exercise
the real path; a monkeypatched `embed_text → None` pins the degrade.

Prefix matching is SQL `LIKE '…%'` on the indexed `barcode` column — a
prefix LIKE can use the index; shop scale is trivial for it (documented).

## 4. Member `ScanRequest` — model, endpoints, lifecycle

### Model (`app/models.py`, new table → `create_all()`, no migration entries)

```python
class ScanRequest(SQLModel, table=True):
    """A member's 'unknown item' report: a scanned code the catalogue does
    not know, with the name/price the member proposed. Staff resolve it by
    creating the catalogue item (POST /api/items, staff-only — coordinator
    decision 1) and linking it back; the member never writes the catalogue."""

    __tablename__ = "scan_requests"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)   # who filed it
    code: str = Field(max_length=64, index=True)               # raw scanned code
    symbology: str = ""
    # Snapshot of the classification at filing time (server-computed, never
    # trusted from the client): kind + canonical key.
    analysis_kind: str = ""
    gtin14: str | None = None
    # The member's proposal (what the Task-5 modal collected).
    name: str
    price_cents: int = Field(default=0, ge=0)
    status: str = Field(default="open", index=True)   # "open" | "resolved" | "dismissed"
    # Plain int, deliberately NOT a FK: a resolved request is a historical
    # record and must survive the item's later deletion (no cleanup path,
    # unlike the user_id FK which delete_member cleans — see below).
    resolved_item_id: int | None = None
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)
```

`delete_member` (`app/routers/members.py`) gains the explicit cleanup loop
for `scan_requests.user_id` — same D4 pattern as the Task-1 cart cleanup,
same transaction. Item deletion does **not** touch resolved requests
(historical).

### Endpoints (new `app/routers/scanrequests.py`, prefix `/api/scan-requests`, tag "scan-requests")

| Endpoint | Auth | Body | Success | Errors |
|---|---|---|---|---|
| `POST ""` | any authenticated user | `{code, symbology?, name, price_cents}` (`ScanRequestIn`, `_Stripped`, name 1..255, price ≥ 0) | **201** `ScanRequestOut` | 401/403 (gate); **409** if the code *does* resolve to a catalogue item (server re-runs `resolve_scan`; detail carries the matched `ItemOut` so the UI offers "add that instead"); 422 validation |
| `GET ""?status=open` | **staff** (`require_staff`) | — | 200 `list[ScanRequestOut]` (with requester name/email joined for the admin view) | 401/403; 422 bad status filter |
| `POST "/{id}/resolve"` | **staff** | `{item_id, add_to_cart?}` (`ScanRequestResolveIn`, `add_to_cart: bool = True`) | 200 `ScanRequestOut` (status=resolved, resolved_item_id set; when `add_to_cart`, `_upsert_line(session, request.user_id, item_id, 1)` — the requester's cart gains the item, closing the loop "member scanned it wanting it") | 404 unknown request; 409 already resolved/dismissed; 404 unknown item_id; 401/403 |
| `POST "/{id}/dismiss"` | **staff** | — | 200 `ScanRequestOut` (status=dismissed) | 404; 409 not open; 401/403 |

`ScanRequestOut`: `{id, user: {id, name, email}, code, symbology,
analysis_kind, gtin14, name, price_cents, status, resolved_item_id,
created_at, updated_at}`.

### Lifecycle

unknown scan → `POST /api/cart/scan` → **404** with analysis + suggestions →
(member's modal) → `POST /api/scan-requests {code, symbology, name,
price_cents}` → appears in the staff admin list → staff member creates the
catalogue item through the normal staff-only path (`POST /api/items` with
the barcode — existing GTIN canonicalisation, duplicate-barcode 409,
embedding) → `POST /api/scan-requests/{id}/resolve {item_id}` → requester's
cart gains the item (default; stock untouched — decision 6) → status
resolved. Dismissal is the staff "not a real product / duplicate junk" exit.
Nothing in this flow lets a member write to `items`.

## 5. Tests

**`tests/test_scanlookup.py`** (new — the decision table with a recording
fake resolver, no HTTP, no DB):
- gtin/restricted → exactly one `by_barcode(key)` call, match="barcode" on
  hit, None on miss; **no** `by_sku` call.
- invalid-gtin → **zero resolver calls** (never looked up — the P1 pin at
  unit level); empty → zero calls.
- opaque → one `by_sku(raw)` call, case-insensitive hit; miss → no match.
- fuzzy=True + opaque miss → `by_keyword` called, first hit → match="search";
  fuzzy=False → `by_keyword` **never** called (the server's contract).
- `find_item` adapter: signature/return parity — the existing
  `tests/test_scanner_bridge.py` suite (all 11 `find_item` cases) passes
  **unchanged** through the delegation; add one explicit test that the
  bridge module's `find_item` calls `scanlookup.resolve_scan` (spy) so the
  delegation cannot be silently re-inlined.

**`tests/test_barcodes.py`** (extend): `prefix_candidates` — correct lengths
(13→7, 7 entries), longest-first, digits-only assertion, and (via
`pytest.raises`) rejection of non-digit input.

**`tests/test_cart_scan.py`** (new):
- 401 unauthenticated; 403 `must_change_password`.
- badge payload → 400 and nothing added; raw 43-char token → ordinary
  opaque miss (404, no explosion).
- known GTIN → 200 `status:"added"`, match "barcode", cart line qty, full
  `cart` consistent with `GET /api/cart`.
- **UPC-E scan finds the EAN-13-filed item** (canonical-key identity, the
  case that motivated the whole normalisation layer).
- re-scan increments (never 409); `qty: 3` adds 3.
- opaque label → exact SKU match, case-insensitive; unknown opaque → 404
  with `by_keyword` suggestions (SKU fragment) and no `by_prefix`.
- **misread (bad check digit) → 422**, `analysis.kind == "invalid-gtin"`,
  nothing added, no suggestions key.
- unknown valid GTIN → 404; detail carries `analysis.info.region/usage`;
  `by_prefix` contains the same-company-prefix sibling and **not** an
  unrelated-prefix item (the anti-guess pin).
- stock-untouched: add qty 20 for stock-3 item → 200, stock still 3.

**`tests/test_items_suggest.py`** (new):
- `by_prefix` ranked longest-first; empty for a non-GTIN `barcode` param;
- `by_keyword` hits (escaped wildcards: `q="50%"` literal);
- `by_semantic` returns results with (fake) embeddings and **degrades to
  `[]`** when `embed_text` is monkeypatched to `None` (the 503 path never
  raises out of the endpoint);
- 401 unauthenticated; limit bounds.

**`tests/test_scan_requests.py`** (new):
- member files → 201, server-snapshotted `analysis_kind`/`gtin14` (client
  cannot forge — post with a valid EAN, assert snapshot);
- code that matches the catalogue → **409 with the matched item** in detail;
- staff lists (member → 403); status filter;
- resolve: links item, requester's cart gains it (default) / not
  (`add_to_cart: false`); unknown item → 404; double resolve → 409;
- dismiss; unknown request id → 404;
- **member deletion cleans their scan_requests** (direct DB check, D4 pin);
- a resolved request **survives item deletion** (historical record).

**Suite hygiene:** full `uv run pytest`; `tests/test_scanner_bridge.py`
green **without edits** (the adapter contract); `test_migrations.py`
untouched (no migration entries — new tables only).

## 6. Risks / open questions

1. **`by_sku` semantics parity** — the bridge pre-filters via `?q=` then
   exact-compares; the server compares `lower(sku) ==` directly. Equivalent
   (both case-insensitive exact), and the server version doesn't depend on
   the search endpoint's LIKE escaping; the unchanged bridge suite pins the
   bridge side. Flagged so a reviewer looks at both once.
2. **400 vs 422 for the badge guard** — 400 chosen (semantically "wrong
   endpoint for this payload"); misread is 422 to mirror
   `GET /api/items?barcode=`. Reviewer taste; one-line flip.
3. **`resolve`'s `add_to_cart` default `true`** — the loop-closing choice
   (the member scanned it *wanting* it); if the coordinator prefers
   resolve to be purely administrative, flip the default. Flagged.
4. **`resolved_item_id` as a plain int (no FK)** — deliberate: resolved
   requests are historical and must survive item deletion; the alternative
   (FK + cleanup on item delete) destroys the audit trail. Flagged as a
   D4-adjacent judgement call.
5. **`GET /api/items/suggest` auth** — `get_current_user` (any role), like
   every other catalogue read. If suggestions are deemed enumerable
   business data, tightening is a one-line change.
6. **Suggestion quality** — prefix siblings need catalogue density to be
   useful; empty is fine (the modal still works). Keyword-on-code can
   surface odd substring hits for opaque codes; capped at 5 and labelled as
   suggestions, never auto-added.
7. **`code` max_length 64** — fits GTIN+addon (19), internal SKUs, and
   `HCRM1:` payloads (49); a longer scan is almost certainly garbage → 422.
8. **Symbology trust** — the server accepts the client's `symbology` (as
   the bridge accepts `--symbology`); HID mode sends `""` and gets the
   content-based classification. Same trust model as today, documented.
9. **Payload size of the 404** — two extra queries (prefix LIKE + keyword)
   per unknown scan; indexed prefix LIKE, shop scale ⇒ trivial. If it ever
   matters, the 404 can drop the embedded suggestions and let the modal
   fetch `/api/items/suggest` — the shape supports both.
