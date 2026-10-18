# Task 1 proposal (glm) — Server-side per-user cart

Implements coordinator Task 1: `CartItem` model + `/api/cart` CRUD, per-user
isolation, explicit cleanup on item/user deletion, migration story,
concurrency test. This is the foundation for save-then-switch (Task 5) and
scan-to-cart (Task 3).

Grounding: `app/models.py`, `app/schemas.py`, `app/routers/{items,auth,members}.py`,
`app/deps.py`, `app/main.py` (`COLUMN_MIGRATIONS`/`INDEX_MIGRATIONS`),
`app/database.py` (PRAGMA `foreign_keys` **off** — FKs declarative only;
`busy_timeout=5000` + WAL on every pooled connection),
`tests/conftest.py` (drops/creates all tables per test), `tests/test_concurrency.py`
(threaded barrier helper), `tests/test_migrations.py` (legacy-DB fixture).

---

## 1. Data model

New model in `app/models.py`, following the existing style (`utcnow()`,
`sa_type=DateTime`, `__tablename__`, declarative FK like `AuthToken.user_id`):

```python
class CartItem(SQLModel, table=True):
    """One line of a user's purchase list (server-side shopping cart)."""

    __tablename__ = "cart_items"

    __table_args__ = (
        UniqueConstraint("user_id", "item_id", name="uq_cart_items_user_item"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    item_id: int = Field(foreign_key="items.id", index=True)
    qty: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)
```

(imports: add `UniqueConstraint` to the existing `sqlalchemy` import line.)

Decisions:

- **No price snapshot.** The line stores only `(user_id, item_id, qty)`;
  prices, names, stock are read live from `items` at response time (see §3).
- **New table ⇒ no migration entries.** `create_all()` adds tables it does
  not find, on fresh *and* existing databases (same pattern as `AppMeta`).
  **Nothing is appended to `COLUMN_MIGRATIONS`/`INDEX_MIGRATIONS** — the
  append-only tables stay untouched (they exist for new columns/indexes on
  *existing* tables). The `UniqueConstraint` rides inside the new table's
  `CREATE TABLE`, so it needs no `INDEX_MIGRATIONS` entry either.
- **FKs are declarative only** (PRAGMA off, `app/database.py` D4): the unique
  constraint and the two indexes *are* created (they live in the table DDL),
  but referential integrity is not — hence the explicit cleanup in §3.
- Property helper for later tasks: `@property def line_total_cents(self)` is
  *not* added (needs the item; computed in the router instead).

## 2. API surface

New `app/routers/cart.py`, registered in `app/main.py` next to the other
`include_router` calls:

```python
router = APIRouter(prefix="/api/cart", tags=["cart"])
```

**Auth rule (all endpoints):** `user: User = Depends(get_current_user)` —
any role (member/staff/admin) shops. Existing dependency behaviour applies
for free: 401 unauthenticated/invalid/expired token; **403 while
`must_change_password`** (cart paths are not in `MUST_CHANGE_EXEMPT_PATHS`).
The staff-only `assign` endpoint uses `Depends(require_staff)`.

### Schemas (`app/schemas.py`, following `_Stripped`/`extra="forbid"` style)

```python
class CartLineIn(_Stripped):
    item_id: int = Field(ge=1)
    qty: int = Field(default=1, ge=1, le=10_000)

class CartQtyIn(_Stripped):
    qty: int = Field(ge=0, le=10_000)      # 0 removes the line (explicit)

class CartReplaceLine(_Stripped):
    item_id: int = Field(ge=1)
    qty: int = Field(ge=1, le=10_000)

class CartReplaceIn(_Stripped):
    lines: list[CartReplaceLine] = Field(min_length=0, max_length=200)

class CartLineOut(BaseModel):
    item: ItemOut                          # live catalogue data
    qty: int
    exceeds_stock: bool                    # convenience flag for the UI warning

class CartOut(BaseModel):
    lines: list[CartLineOut]
    total_cents: int                       # sum(item.price_cents * qty), live
    total_qty: int                         # sum(qty)

class CartAssignIn(_Stripped):
    email: str = Field(max_length=255)
    item_ids: list[int] = Field(min_length=1, max_length=100)
    qty: int = Field(default=1, ge=1, le=10_000)
```

### Endpoints

| Method & path | Auth | Request | Success | Errors |
|---|---|---|---|---|
| `GET /api/cart` | any user | — | `200 CartOut` | 401, 403 (pw change) |
| `POST /api/cart/lines` | any user | `CartLineIn` | `200 CartOut` | 401, 403, **404** unknown `item_id` ("Item not found"), 422 validation |
| `PATCH /api/cart/lines/{item_id}` | any user | `CartQtyIn` | `200 CartOut` (qty 0 → line removed) | 401, 403, **404** no such line in the caller's cart, 422 |
| `DELETE /api/cart/lines/{item_id}` | any user | — | `204` | 401, 403, 404 |
| `PUT /api/cart` | any user | `CartReplaceIn` | `200 CartOut` | 401, 403, **404** any unknown item_id (detail lists them), **422** duplicate item_id in payload |
| `DELETE /api/cart` | any user | — | `204` (already-empty is still 204; idempotent) | 401, 403 |
| `POST /api/cart/assign` | **staff/admin** | `CartAssignIn` | `204` | 401, 403 (member, or staff→admin target per `_get_manageable_member` rule), 404 unknown email / unknown item ids |

Notes on shape choices:

- **Mutations return the full `CartOut`** (not the affected line, and not
  201). Rationale: the SPA panel always needs lines+total, so one
  round-trip per mutation keeps it consistent with zero read-after-write;
  `stock-adjust` already establishes the "action-style POST → 200 with
  result" precedent. Deletes/clear stay `204` like `logout`/badge-revoke.
- **`POST /lines` never 409s on "already in cart"** — it increments (see §3).
- `assign` is included here because it is pure cart logic (no scan
  knowledge); it is the coordinator's D5 secondary attribution path. If the
  coordinator prefers, it can slip to Task 3 with zero rework — it uses the
  same internal helper (§3).

## 3. Behavior

### Add / already-in-cart / update / remove / list / clear

- **Add (`POST /lines`)** is an atomic **upsert**, one SQL statement:
  ```sql
  INSERT INTO cart_items (user_id, item_id, qty, created_at, updated_at)
  VALUES (:u, :i, :q, :now, :now)
  ON CONFLICT(user_id, item_id)
  DO UPDATE SET qty = qty + :q, updated_at = :now
  ```
  executed with `session.execute(text(...))` + `session.commit()` (the
  `text()` style of `items.py`). "Already in cart" therefore means
  *quantity increments* — scanning/adding the same item twice yields one
  line with qty 2, exactly like the current `+` button. A prior existence
  check is deliberately omitted: the check-then-insert window is where two
  concurrent adds would race into a unique-constraint 500; the single-statement
  upsert is race-free by construction (SQLite serialises writers;
  `busy_timeout=5000`, already set on every pooled connection in
  `app/database.py`, makes the loser wait, not fail). This avoids the
  heavier per-operation `BEGIN IMMEDIATE` dance of `adjust_stock` — that
  endpoint needs a guarded read-modify-write; here `ON CONFLICT` does it in
  one statement.
- **Update (`PATCH`)** sets absolute qty; `qty=0` deletes the line (the
  schema's `ge=0` + explicit removal keeps the UI's −-button-to-zero
  behaviour one round-trip). Unknown `(caller, item_id)` → 404.
- **Remove / clear** are plain deletes. The item's existence is *not*
  re-checked on delete (the line is the caller's; deleting a line whose
  item was removed from the catalogue in the meantime must still succeed —
  see cleanup).
- **List (`GET`)** joins `CartItem → Item` (`select(CartItem, Item).join(...)`
  or two queries like `vector_search` does), ordered by `CartItem.id`
  (stable, insertion order). Computes `total_cents` and the
  `exceeds_stock` flag (`qty > item.stock`) server-side from **live** data.

### Price snapshot vs live price — **live**

The cart stores no price. `CartLineOut.item` is the *current* `ItemOut` and
`total_cents` is computed at read time. Rationale: the current localStorage
UI shows the item object it stored, so prices silently go stale; a server
cart should quote what the shop charges *now* (a checkout flow, later, is
the point where a snapshot/lock would matter — out of scope). The
`updated_at` on the line records the last *cart* mutation only.

### Stock interaction — none (coordinator decision 6)

Cart operations never read-modify-write `items.stock`, never clamp
(`exceeds_stock` is informational, for the UI warning). Out-of-stock items
can sit in a cart; the existing UI hint ("Only N in stock") moves to the
`exceeds_stock` flag.

### Save-then-switch support (for Task 5)

- **Durability by construction:** every mutation is its own committed
  request; there is no client-held authoritative state to flush. The
  switch flow's "save" reduces to *awaiting in-flight requests* in the SPA.
- **`PUT /api/cart` is the explicit flush/replay primitive:** the Task 5
  flow uses it (a) to upload the legacy localStorage list once on first
  login, (b) to merge the anonymous pre-login staging list after
  attribution, and (c) as a belt-and-braces flush before an account switch
  if the SPA ever debounces. It is transactional delete-then-insert in one
  session/commit; concurrent line-adds during a PUT can be lost —
  **documented last-writer-wins** (two kiosks on one account is an edge
  case, not a requirement; no CRDTs).
- **Load** is `GET /api/cart` with the *new* user's token after the switch.
- Internal helper so Task 3's `/api/cart/scan` reuses the exact same upsert
  (no drift): `add_lines(session, user_id, [(item_id, qty), ...]) -> None`
  in `app/routers/cart.py` (or `app/cartops.py` if the router import would
  become circular later — start in the router module; it is importable).

### Cleanup on delete (FKs are off — must be explicit)

- `app/routers/items.py::delete_item`: before deleting the item, in the
  same session/commit, `delete from cart_items where item_id = :id`
  (via `session.exec(select(CartItem).where(...))` + deletes, matching the
  explicit-cleanup style of `delete_member`'s token loop — or one
  `session.execute(delete(CartItem).where(...))`; keep it in the same
  transaction so a failure rolls back together).
- `app/routers/members.py::delete_member`: extend the existing
  `auth_tokens` cleanup loop to also delete the user's `CartItem` rows.
- Without these, deleted items/users leave orphan lines that would make
  `GET /api/cart`'s join drop rows silently (inner join) or emit
  half-empty lines — the exact "orphan path" `app/database.py` D4 warns
  about.

## 4. Tests

### New `tests/test_cart.py` (uses `client`, `admin_token`, `create_user` from `conftest.py`)

Auth & gating:
1. Every endpoint unauthenticated → 401.
2. `must_change_password` account (create via staff, skip the conftest
   helper's auto-change if it performs one — otherwise build the state
   directly) → 403; after change-password → works.

CRUD round-trip:
3. Empty cart → `GET` → `{"lines": [], "total_cents": 0, "total_qty": 0}`.
4. Add new line → 200, one line, `total_cents` correct; add same item again
   → qty 2, still one line (**no 409**).
5. `PATCH` set qty 5 → 5; `PATCH` qty 0 → line gone; `PATCH` unknown
   item_id → 404; `PATCH` qty −1 → 422.
6. `DELETE /lines/{id}` → 204; repeat → 404. `DELETE /api/cart` → 204,
   idempotent on empty.
7. Add unknown item_id → 404 "Item not found".

Isolation:
8. A adds item X; B's `GET` shows nothing; B's `PATCH/DELETE` on X → 404
   (B has no such line); B adds X → two independent lines, both `GET`s
   correct.

Replace:
9. `PUT` with two lines replaces prior content entirely; unknown item_id →
   404 listing the bad ids; duplicate item_id in payload → 422; empty
   `lines` → clears the cart.

Live price / stock:
10. Add item, then staff `PATCH /api/items/{id}` raises the price → `GET
    /api/cart` reflects the new price and total (no snapshot).
11. Cart ops never touch stock: add qty 20 for an item with stock 5 →
    `exceeds_stock: true`, item stock still 5 via `GET /api/items/{id}`.

Cleanup:
12. Item in A's cart → admin deletes the item → A's `GET /api/cart` no
    longer shows it (and no 500).
13. User with cart lines → admin deletes the member → direct DB check
    (fresh `Session(engine)` or sqlite3, the `test_concurrency.py` pattern)
    that no `cart_items` rows remain for that `user_id`.

Assign:
14. Staff assigns to a member email → member's cart gains the lines;
    member calling assign → 403; staff assigning to an **admin** account →
    403 (mirrors `_get_manageable_member`); unknown email → 404.

Concurrency (reuse the `tests/test_concurrency.py` threaded-barrier helper):
15. N threads (e.g. 8) each `POST /lines {item_id, qty: 1}` for the same
    (user, item), released simultaneously → final `GET` shows exactly
    `qty == N` and one line — **no lost increments, no 500s** (this is the
    regression test for the upsert; a naive read-modify-write would fail it).

### Extend `tests/test_migrations.py`
16. Legacy-DB fixture (no `cart_items`) → run `create_all()` +
    `_migrate_schema()` → table exists with the unique index
    (`PRAGMA index_list('cart_items')` shows `uq_cart_items_user_item`);
    running twice is idempotent; **and** `COLUMN_MIGRATIONS` gained no new
    entry for this feature (guards the "new table needs no column
    migration" invariant against future copy-paste).

### Extend `tests/test_items.py` / member-deletion tests
17. Assert cart cleanup alongside the existing delete behaviour (or rely on
    cases 12–13 and just keep those tests green).

## 5. Risks & open questions

1. **Mutations return `CartOut` (not the affected line, not 201)** — a
   deliberate deviation from the create-returns-resource style, for
   one-round-trip UI consistency. Flag for reviewer taste; trivially
   changeable.
2. **`qty` upper bound 10 000 and 200-line `PUT` cap** are arbitrary but
   cheap guards against runaway loops (a stuck scanner bridge hammering
   `/api/cart/scan` in Task 3 inherits them via the shared helper).
   Confirm the numbers.
3. **`exceeds_stock` convenience flag** — the UI could compute it from
   `ItemOut.stock`; carrying it server-side keeps Task 5's toast logic
   dumb. Keep or drop, no behavioural impact.
4. **`PUT` last-writer-wins** under concurrent mutation is documented, not
   solved — acceptable for the kiosk flush/staging replay use case.
5. **`assign` scope**: included here as pure cart logic; if the coordinator
   wants Task 1 minimal it moves to Task 3 unchanged. Its staff→admin 403
   follows the members-router privilege rule for consistency — arguably
   over-strict for "put an item in the boss's cart", but consistency with
   `_get_manageable_member` is worth more than the edge-case convenience.
6. **Cart rows for items deleted concurrently with a `GET`** — impossible
   within one request (cleanup is transactional with the delete), and the
   join is per-request; no stale-read hazard beyond SQLite's normal
   snapshot semantics.
7. **`AuthToken`-style hash-at-rest concerns do not apply** (no secrets in
   the cart), but the `user_id`/`item_id` indexes keep the isolation
   filters and the cleanup deletes seeks, not scans.
8. **Later tasks plug in here**: Task 3's `/api/cart/scan` and Task 5's
   staging replay must go through `add_lines`/`PUT` respectively — the
   proposal factors the upsert into a helper precisely so those tasks
   cannot reinvent a racy version.
