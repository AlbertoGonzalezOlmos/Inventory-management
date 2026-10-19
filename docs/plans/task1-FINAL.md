# Task 1 FINAL spec — Server-side per-user cart

Authoritative decision merged from the three proposals + three reviews
(`task1-proposal-{glm,kimi,qwen}.md`, `task1-review-{glm,kimi,qwen}.md`).
Implementer follows THIS spec.

## Data model (`app/models.py`)

New `CartItem` model → table `cart_items`:

- `id: int | None = Field(default=None, primary_key=True)`
- `user_id: int = Field(foreign_key="users.id", index=True)`
- `item_id: int = Field(foreign_key="items.id", index=True)`
- `qty: int`
- `created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)`
- `updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)`
- `__table_args__` = `UniqueConstraint("user_id", "item_id", name="uq_cart_items_user_item")`

- **No** DB CHECK on qty (it would fight the atomic decrement-then-delete path).
- **No** price snapshot (live prices only).
- FKs are declarative only (`PRAGMA foreign_keys` is OFF) → explicit cleanup.
- New table → `create_all()` creates it everywhere. **No** `COLUMN_MIGRATIONS`
  / `INDEX_MIGRATIONS` entries. The existing exact-set migration test
  (`test_every_migration_the_app_currently_needs_is_listed`) stays unchanged.

## Schemas (`app/schemas.py`)

Follow existing style: `extra="forbid"`, bounded qtys (1..10000).

- `CartAddIn`: `item_id: int`, `qty: int = 1`
- `CartSetIn`: `qty: int` (0 = remove)
- `CartReplaceLineIn`: `item_id: int`, `qty: int`
- `CartReplaceIn`: `lines: list[CartReplaceLineIn]` (≤ 200 lines)
- `CartLineOut`: `item: ItemOut`, `qty: int`, `line_total_cents: int`,
  `created_at: datetime`, `updated_at: datetime`
- `CartOut`: `lines: list[CartLineOut]`, `total_qty: int`, `total_cents: int`

## Router (`app/routers/cart.py`, prefix `/api/cart`)

Every endpoint behind `get_current_user` (any role; inherits 401 and the
`must_change_password` 403). Mutators return full `CartOut`.

1. `GET /api/cart` → `CartOut`. Lazily reaps orphan lines (item_id no longer
   exists): skip them in the response and delete them.
2. `POST /api/cart/items` (`CartAddIn`) → add-or-increment, single statement
   upsert (below). `rowcount == 0` → 404 "Item not found". Full `CartOut`.
3. `PATCH /api/cart/items/{item_id}` (`CartSetIn`) → absolute set; `qty == 0`
   removes the line; line absent → 404 "Item not in cart". Never creates.
   Full `CartOut`.
4. `DELETE /api/cart/items/{item_id}` → line absent → 404. Full `CartOut`.
5. `PUT /api/cart` (`CartReplaceIn`) → all-or-nothing replace. Unknown item_id
   → 404 and the cart is left byte-identical. Duplicate item_id in the body →
   422. ≤ 200 lines. Full `CartOut`.
6. `DELETE /api/cart` → idempotent clear. Full `CartOut` (empty).

## Concurrency-safe upsert (the one write path that matters)

Shared helper `_upsert_line(session, user_id, item_id, qty)` used by
`POST /api/cart/items` (and reusable by later tasks):

```sql
INSERT INTO cart_items (user_id, item_id, qty, created_at, updated_at)
SELECT :uid, id, :qty, :now, :now FROM items WHERE id = :iid
ON CONFLICT(user_id, item_id)
DO UPDATE SET qty = min(qty + excluded.qty, :cap), updated_at = :now
```

- Folds the item-existence check into the write (no check-then-write orphan
  window, no lost updates at any concurrency).
- `rowcount == 0` → item did not exist → 404.
- `cap = 10_000` (min() clamps where accumulation happens; no silent drop of
  the reported quantity — the response still shows the resulting line).

## Behaviour

- **Live prices**: totals computed at read (`item.price_cents * qty`).
- **No stock mutation**; `qty > stock` is allowed (the UI warns from
  `line.item.stock` in a later task).
- **Cleanup hooks**: `delete_item` (items router) and `delete_member`
  (members router) delete `cart_items` rows in the same transaction,
  mirroring the existing `AuthToken` cleanup.
- `updated_at` bumped on every write path.

## Tests (`tests/test_cart.py`)

- Auth gating: 401 unauthenticated; 403 `must_change_password`.
- Add/increment (re-scan increments, never 409); 404 unknown item.
- Set absolute (incl. `qty == 0` removes); 404 "not in cart" (not 403).
- Delete line (404 when absent); clear idempotent.
- Replace atomicity: unknown id → 404 with cart byte-identical; duplicate ids
  → 422.
- Live-price pin (total follows `item.price_cents`).
- Stock-untouched pin.
- Per-user isolation (another user's line → 404, no existence leak).
- Cleanup on item delete and on member delete (direct DB check).
- Orphan reap (GET skips + removes lines for deleted items).
- Unique-constraint behavioural test (duplicate `(user_id, item_id)` insert
  raises IntegrityError).
- Concurrency: 8-thread barrier add-same-item → `qty == N`; plus a 3-thread
  contention test (no lost updates, no flake at N≥3).
- Leave `tests/test_migrations.py` exact-set test unchanged.

## Out of scope for Task 1 (later tasks)

`POST /api/cart/assign` (attribution), scan endpoints, `exceeds_stock` flag,
price snapshots, checkout, the SPA.
