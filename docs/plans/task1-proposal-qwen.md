# Task 1 proposal (qwen3.8-max) — Server-side per-user cart

Concrete implementation approach for Task 1 of `docs/plans/COORDINATOR.md`.
Proposal only — no code is changed by this document.

**Binding coordinator decisions honoured here:** cart is per-user and durable;
adding to cart **never mutates stock** and there is **no hard server-side
stock clamp** (the UI warns from live `item.stock` in Task 5); cart rows are
cleaned up **explicitly** when an item or user is deleted (PRAGMA
`foreign_keys` is OFF — repo decision D4, FKs are declarative only).

**Task boundary:** only the cart table + `/api/cart` CRUD + delete-path
cleanup + tests. `POST /api/cart/scan`, suggestions, `ScanRequest`,
`/api/cart/assign` are Task 3; the SPA rewire and localStorage import are
Task 5. Nothing in Task 1 depends on them, and nothing here pre-empts them
(the scan endpoint will simply call this router's add-path helper).

---

## 1. Data model

### Table: `cart_items` (new table, `app/models.py`)

Plural snake_case, matching `users` / `items` / `auth_tokens` / `app_meta`.

```python
class CartItem(SQLModel, table=True):
    """One line of a user's shopping cart: item + quantity.

    The cart is server-side and per-user so that (a) scans and browsers
    converge on one durable list and (b) the badge-driven account switch can
    guarantee "whatever had been done is saved" by construction. Stock is
    never touched here (coordinator decision 6); qty may legitimately exceed
    item.stock — the UI warns from the live stock carried in CartLineOut.
    FKs are declarative only (PRAGMA foreign_keys=OFF, decision D4), so the
    delete paths in items.py/members.py clean up rows explicitly, and
    GET /api/cart tolerates + lazily reaps orphan rows.
    """

    __tablename__ = "cart_items"
    __table_args__ = (
        UniqueConstraint("user_id", "item_id", name="uq_cart_items_user_item"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    item_id: int = Field(foreign_key="items.id", index=True)
    qty: int = Field(default=1, ge=1)
    added_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)
```

Column notes:

- `qty` — plain INTEGER, **no DB CHECK constraint**. A `CHECK (qty > 0)`
  would fight the atomic decrement path (§3, `POST /cart/lines`), which
  transiently writes `qty <= 0` and deletes the row in the same transaction.
  Bounds are enforced at the API (pydantic) and in the SQL statements, same
  as `items.stock` (which also has no CHECK and is clamped in endpoint code).
- **No price snapshot column.** Lines carry live catalogue prices (see §4.2
  for the decision and the checkout caveat).
- `added_at`/`updated_at` mirror `Item`'s naive-UTC `utcnow()` convention.
- Indexes: `ix_cart_items_user_id` (every query is user-scoped) and
  `ix_cart_items_item_id` (item-deletion cleanup); both created with the
  table by `Field(index=True)`.
- The **unique constraint on `(user_id, item_id)`** is load-bearing twice:
  it makes "one line per item" a DB invariant, and it is the conflict target
  for the atomic upsert in §3. Declare it from day one — SQLite cannot add a
  unique constraint to a live table later (the same reason `items.barcode`
  needed its own `CREATE UNIQUE INDEX` migration entry).

### Migration story

- **New table ⇒ no migration entries.** `create_all()` (run at startup and
  in `tests/conftest.py::_reset_db`) creates tables it does not find, on
  fresh *and* existing databases — the `AppMeta` precedent. `cart_items`
  arrives complete with its indexes and unique constraint.
- **`COLUMN_MIGRATIONS` / `INDEX_MIGRATIONS` stay untouched.** Those tuples
  are for new columns/indexes on *existing* tables. This is asserted by the
  repo itself: `tests/test_migrations.py::test_every_migration_the_app_currently_needs_is_listed`
  pins the exact set — adding a spurious cart entry would *fail* that test.
  If a later task adds a column to `cart_items`, it appends an entry then.
- One schema-shape test (§5, group N) records that the table + unique index
  exist after `create_all()` on a legacy-style database, so a future refactor
  cannot silently drop the upsert's conflict target.

## 2. API surface

New `app/routers/cart.py` — `APIRouter(prefix="/api/cart", tags=["cart"])`,
included in `app/main.py` next to the existing `include_router` calls.

**Auth (all endpoints):** `user: User = Depends(get_current_user)` — any
authenticated role (a cart is not role-gated; members are the point).
Consequences of reusing the existing dependency, for free:

- no/invalid/expired token → **401**;
- `must_change_password` account → **403** on every `/api/cart` path (not in
  `MUST_CHANGE_EXEMPT_PATHS`) — asserted in tests;
- every query is scoped `WHERE user_id = user.id`, so cross-user access is
  structurally impossible and surfaces as **404** (never 403 — no existence
  leak about other users' carts).

**Schemas** (in `app/schemas.py`, house conventions: `_Stripped` base,
`extra="forbid"`, pydantic bounds → 422):

```python
MAX_CART_QTY = 100_000          # abuse bound, mirrors StockAdjustIn's range

class CartLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    item: ItemOut               # live price_cents AND live stock, for the UI warning
    qty: int
    added_at: datetime
    updated_at: datetime

class CartOut(BaseModel):
    lines: list[CartLineOut]    # insertion order (id asc) — stable for UI diffing
    total_cents: int            # Σ live price_cents × qty, computed at read time

class CartAddIn(_Stripped):
    item_id: int
    qty: int = Field(default=1, ge=1, le=MAX_CART_QTY)

class CartQtyIn(_Stripped):     # absolute set; qty is required (not _PatchIn:
    qty: int = Field(ge=0, le=MAX_CART_QTY)   # single mandatory field; None → 422)

class CartLineDeltaIn(_Stripped):
    item_id: int
    delta: int = Field(ge=-MAX_CART_QTY, le=MAX_CART_QTY)
    # + non-zero validator, copied from StockAdjustIn: delta=0 is a client bug,
    # never a meaningful request, and must not return 200 looking like one.

class CartReplaceLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: int
    qty: int = Field(ge=1, le=MAX_CART_QTY)

class CartReplaceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lines: list[CartReplaceLineIn] = Field(max_length=200)  # a cart, not an import job
```

**Endpoints** — every mutator returns the **full updated `CartOut` (200)**,
one contract for the whole surface: at scanner cadence the client needs the
resulting state after each request, and returning it removes all client-side
merge arithmetic. (Deliberate deviation from `POST /api/items` → 201 +
resource: cart-add is usually an *increment*, not a creation, and the useful
result is the cart.)

| # | Endpoint | Body | Success | Purpose / semantics |
|---|---|---|---|---|
| 1 | `GET /api/cart` | — | 200 `CartOut` | Read; filters + lazily reaps orphan rows (§4.4) |
| 2 | `POST /api/cart/items` | `CartAddIn` | 200 `CartOut` | **Add-or-increment** (the scan path). Atomic upsert (§3.1). Unknown `item_id` → 404 |
| 3 | `PATCH /api/cart/items/{item_id}` | `CartQtyIn` | 200 `CartOut` | **Absolute set** (UI steppers, corrections); `qty: 0` removes the line. Line not in cart → 404 (adding is #2's job — explicit, no create-on-PATCH surprise) |
| 4 | `POST /api/cart/lines` | `CartLineDeltaIn` | 200 `CartOut` | **Relative delta** on an existing line (race-free decrement, §3.2); result ≤ 0 removes the line; line not in cart → 404; `delta: 0` → 422 |
| 5 | `DELETE /api/cart/items/{item_id}` | — | 204 | Remove one line; absent → 404 (repo convention: `delete_item`, `delete_member`) |
| 6 | `PUT /api/cart` | `CartReplaceIn` | 200 `CartOut` | **Whole-cart replace** (flush / replay / legacy import). All-or-nothing: any unknown `item_id` → 404 and the previous cart is untouched; duplicate `item_id` in the body → 422; `lines: []` = clear |
| 7 | `DELETE /api/cart` | — | 204 | Clear; **idempotent** (204 on an already-empty cart — the logout/clear-button path; contrast line-level 404: the collection always "exists") |

**Error matrix (Task 1 has no 409 path):**

| Case | Status |
|---|---|
| Missing/invalid/expired token | 401 (existing dependency) |
| `must_change_password` account | 403 (existing gate) |
| `item_id` not in catalogue (#2, #6) / line not in *my* cart (#3, #4, #5) — including another user's line | 404 |
| qty/delta out of bounds, `delta: 0`, extra fields, non-int, duplicate ids or >200 lines in `PUT`, explicit `null` | 422 |

## 3. Atomicity — what is single-statement, and why this is *not* stock-adjust

The repo has two write idioms and the choice between them is deliberate:

### 3.1 Add/increment (#2): single-statement SQLite upsert

```python
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

stmt = sqlite_insert(CartItem).values(
    user_id=user.id, item_id=payload.item_id, qty=payload.qty,
    added_at=utcnow(), updated_at=utcnow(),
)
stmt = stmt.on_conflict_do_update(
    index_elements=["user_id", "item_id"],       # the uq_cart_items_user_item target
    set_={"qty": func.min(CartItem.qty + stmt.excluded.qty, MAX_CART_QTY),
          "updated_at": utcnow()},
)
session.execute(stmt)
session.commit()
```

One statement ⇒ SQLite's single-writer serialisation makes concurrent
increments loss-free (two rapid scans of the same item both land — the exact
bug class stock-adjust was rebuilt for, REVIEW-m10 P6). The item-existence
check (`session.get(Item, …)` → 404) is a plain read *before* the write.

**Why not stock-adjust's `BEGIN IMMEDIATE` raw-connection ritual:** that
machinery exists because stock-adjust is a *guarded* read-modify-write where
the guard (clamp-at-0) must see the latest committed value, and stock is
money-adjacent. A cart increment has no guard to race: the accumulation
happens inside the write statement itself (`qty + excluded.qty`), so the
default ORM session (one pooled connection, implicit transaction on first
DML) is already race-free. The only residual race is *item deleted
concurrently with an add* → at worst an orphan row, which is self-healing
(§4.4) — cart rows are not stock. Copying the raw-connection pattern here
would double the pool-checkout surface for no guarantee gain; the two
idioms must not be conflated in review.

The `min(…, MAX_CART_QTY)` cap is an abuse bound, not policy: it is silent in
SQL but **visible in the returned `CartOut`** (qty stops growing at the
cap), consistent with "report, never silently drop" — the report *is* the
cart we return. (Open question Q1 below.)

### 3.2 Delta (#4): two writes, one implicit transaction

```sql
UPDATE cart_items SET qty = qty + :delta, updated_at = :now
 WHERE user_id = :uid AND item_id = :iid;      -- rowcount 0 → 404
DELETE FROM cart_items
 WHERE user_id = :uid AND item_id = :iid AND qty <= 0;
```

The first DML opens the write transaction (repo-documented driver default),
so the pair commits atomically and other writers serialise behind it; the
decrement can never lose to a concurrent increment. Floor-at-zero is
expressed as *removal* (an empty line is not a line), observable in the
returned `CartOut`.

### 3.3 Replace (#6)

Validate all `item_id`s exist first (404 before any write), reject duplicate
ids (422), then `DELETE` all of the user's rows + bulk `INSERT` in one
session transaction — all-or-nothing. Two kiosks `PUT`ting one account is
**last-writer-wins, documented** (kimi's stance, coordinator-accepted); no
ETag/versioning in Task 1.

## 4. Behaviour details

### 4.1 "Already in cart"
Increment, never 409. Re-scanning the same product is *the* dominant flow;
duplicates are a quantity, not an error (the rule `scan_intake.py` already
states for batches). `POST /api/cart/items {qty: n}` adds n at once.

### 4.2 Price: live, not snapshot
`CartLineOut` embeds the full `ItemOut`, so price and stock are read from the
catalogue at response time; `total_cents` is computed from live prices.
Rationale: checkout does not exist (README tracked issue — `requestPurchase()`
is a toast), so a snapshot column would be write-only data with a staleness
reconciliation problem and no consumer; the current localStorage list already
suffers from stale copied prices, and live data fixes that. **Caveat, flagged
for the future checkout task:** an order must snapshot prices at order time;
that belongs to checkout's schema, not the cart's. Test H pins the live-price
semantics so the choice is deliberate and visible.

### 4.3 Stock: zero server-side interaction (decision 6)
No read, no clamp, no decrement anywhere in this router. `qty > item.stock`
is a successful add; the SPA (Task 5) compares `line.qty` with
`line.item.stock` and warns. Test I asserts both halves (add succeeds;
`items.stock` unchanged by every cart op).

### 4.4 Deletion cleanup + orphan tolerance (FKs are declarative only)
- `app/routers/items.py::delete_item` — inside the same session/transaction
  that deletes the item: delete **all users'** `cart_items` rows with that
  `item_id` (mirrors how `delete_member` already deletes `auth_tokens`
  first; SQLite serialises writers, so no window).
- `app/routers/members.py::delete_member` — delete the user's cart rows
  alongside the existing token cleanup.
- `GET /api/cart` (and the `CartOut` assembly in mutators) resolves rows →
  items by id and **skips rows whose item no longer exists, deleting them
  best-effort in the same request**. This covers every non-API deletion path
  (hand-written SQL, the add/delete race in §3.1) and keeps the documented
  D4 limitation honest: explicit cleanup on the API paths, self-healing on
  read. Test M proves it white-box.

### 4.5 How this underpins save-then-switch (Tasks 4/5)
- **Save:** every mutation commits immediately ⇒ the cart is durable at
  every instant; "first save whatever had been done in the current user's
  account" needs *no flush protocol* for scans/steppers done through the
  API. `PUT /api/cart` exists for the one remaining case: replaying
  *staged* client state (anonymous scans, the one-time legacy localStorage
  import) in a single transaction before/after a switch.
- **Load:** after the SPA swaps tokens (badge switch or login), `GET
  /api/cart` returns the incoming user's list — per-user keying makes the
  old localStorage cross-user leak structurally impossible.
- **Switch itself is orthogonal:** tokens live in `auth_tokens`; nothing in
  this router references sessions, so Task 4's `logoff_deadline` work and
  Task 5's switch flow compose without cart changes.

## 5. Tests

**New file `tests/test_cart.py`** (fixtures/helpers reused verbatim:
`client`, `admin_token`, `create_user`, `auth`, seeded items `EX-001…012`;
staff-created items via `POST /api/items` where a controlled price/stock is
needed). Groups:

- **A. Auth & gating** — every endpoint 401 unauthenticated; a
  `must_change_password` token gets 403 on all seven (create a member via
  API and use the pre-change token, since `create_user` clears the flag).
- **B. Add/increment** — first add → line qty 1, `added_at`/`updated_at`
  present; re-add same item → qty 2 (no 409, `updated_at` advances);
  `{qty: 5}` adds 5; unknown `item_id` → 404; qty 0 / −1 / 100 001 → 422;
  extra field (`price_cent` typo) → 422; `total_cents` = price × qty.
- **C. PATCH set** — set qty up/down; `qty: 0` removes the line; PATCH on a
  line not in cart → 404 (does *not* create); bounds → 422.
- **D. Delta** — `+1`/`−1` on an existing line; decrement below zero removes
  the line; missing line → 404; `delta: 0` → 422 (StockAdjustIn parity);
  `|delta| > 100 000` → 422; negative-only delta on qty 1 → line gone, cart
  total updated.
- **E/F. Remove & clear** — `DELETE` line → 204, second → 404; `DELETE
  /api/cart` → 204 and empty; clear is idempotent (204 on empty); **clearing
  user A leaves user B's cart untouched**.
- **G. PUT replace** — replaces wholesale (old lines gone); `lines: []`
  clears; unknown id → 404 **and the previous cart is byte-identical
  afterwards** (atomicity); duplicate ids → 422; qty 0 → 422; 201 lines →
  422.
- **H. Live price** — staff `PATCH /api/items/{id} {price_cents}` → next
  `GET /api/cart` total reflects the new price (pins "no snapshot").
- **I. Stock untouched (decision 6)** — add with `qty > stock` → 200; after
  every mutator, `GET /api/items/{id}` shows unchanged `stock`; response
  line carries live `stock` for the UI warning.
- **J. Per-user isolation** — A and B each add distinct items; B's `GET`
  never contains A's lines; B `PATCH`/`DELETE`/delta on A's item → 404 and
  A's row unchanged (404-not-403: no existence leak).
- **K. Explicit cleanup** — staff `DELETE /api/items/{id}` → the line
  disappears from **both** A's and B's carts, their other lines survive;
  admin `DELETE /api/members/{id}` → the member's lines gone, another user's
  line for a shared item survives.
- **L. Concurrency (lost-update regression)** — modelled on
  `test_stock_adjust.py::test_concurrent_adjustments_lose_nothing`:
  `THREADS = 8` with a `threading.Barrier`, each firing
  `POST /api/cart/items {item_id, qty: 1}` for the same user+item → final
  qty **exactly 8**; companion case: 4 concurrent `+1` adds vs 4 concurrent
  `delta: −1` → final qty exactly 4 (or the line's removal accounting for
  floor-at-0), never a lost update, never a 500, never "database is locked".
- **M. Orphan tolerance (white-box)** — insert a `cart_items` row with a
  nonexistent `item_id` via a raw `Session` → `GET /api/cart` omits it and
  the row is gone from the table afterwards.
- **N. Schema shape** — after `create_all()`, `sqlite_master` contains
  `cart_items` and the `uq_cart_items_user_item` unique index (the upsert's
  conflict target can never silently vanish); plus the negative assertion
  that `COLUMN_MIGRATIONS` gained no cart entry
  (`test_every_migration_the_app_currently_needs_is_listed` already pins
  this — leave that test **unchanged**).
- **Suite hygiene** — run `tests/test_metadata_and_ops.py` (OpenAPI/route
  metadata): if it enumerates routes, refresh the expectation;
  `tests/test_items.py` / member-deletion tests stay green (cleanup is
  additive). Full `uv run pytest` green is the PR gate.

## 6. Files touched (review checklist)

| File | Change |
|---|---|
| `app/models.py` | + `CartItem` (with `UniqueConstraint` import from sqlalchemy) |
| `app/schemas.py` | + `MAX_CART_QTY`, `CartLineOut`, `CartOut`, `CartAddIn`, `CartQtyIn`, `CartLineDeltaIn`, `CartReplaceLineIn`, `CartReplaceIn` |
| `app/routers/cart.py` | **new** — 7 endpoints + a small `_cart_out(session, user)` assembler (orphan-tolerant) reused by all mutators |
| `app/main.py` | + `from app.routers import cart` / `app.include_router(cart.router)` |
| `app/routers/items.py` | `delete_item`: delete cart rows for the item in the same transaction |
| `app/routers/members.py` | `delete_member`: delete the user's cart rows alongside token cleanup |
| `tests/test_cart.py` | **new** — groups A–N above |
| *(untouched, deliberately)* | `app/main.py` migration tuples, `tests/test_migrations.py` exact-set test, `app/database.py` (no PRAGMA/transaction-mode changes — the incident comments stay true) |

## 7. Risks & open questions (Task 1-specific)

1. **Q1 — accumulation cap style.** Chosen: `min(qty, 100 000)` inside the
   upsert, silent in SQL but visible in the returned cart (the value stops
   growing). Alternative was a 409 on cap overflow — rejected as noise for an
   abuse-only boundary, but it is a judgement call the coordinator can flip
   cheaply (one `set_=` expression + one test).
2. **Q2 — mutators return the whole `CartOut`.** Slightly heavier payloads
   (~1 cart, not a page of items) in exchange for one uniform contract and
   zero client-side merge logic at scanner cadence. If reviewers prefer
   per-line responses (`CartLineOut` + `removed: bool` à la
   `StockAdjustOut`), the change is contained to the router.
3. **Q3 — `PUT` last-writer-wins.** Two kiosks on one account can clobber
   each other's replace. Accepted and documented (matches kimi/coordinator);
   an `If-Match`/version column is the future remedy if it ever bites — do
   not build it now.
4. **R1 — orphan-window.** The pre-write item-existence read can race a
   concurrent item deletion and leave an orphan row. Bounded harm: invisible
   in every response, reaped on next read, cleaned on API deletes. Same
   limitation class as `auth_tokens` under D4 — explicitly *not* "fixed" by
   enabling `PRAGMA foreign_keys` (repo decision: that needs a table rebuild
   and changes delete semantics globally).
5. **R2 — upsert dialect lock-in.** `sqlite_insert(...).on_conflict_do_update`
   is SQLite-dialect SQL. The repo is SQLite-only (engine, WAL, triggers,
   sqlite-vector), so acceptable; noted in the module docstring so a future
   Postgres move remembers this line.
6. **R3 — live prices** mean a cart total can change while the user shops
   (staff price edit). Correct pre-checkout behaviour, but the future
   checkout task must snapshot at order time — flagged in §4.2 and here so it
   cannot be forgotten.
7. **R4 — `must_change_password` gate** blocks carts for flagged accounts
   (403). That is the existing, intended gate behaviour (badge-login test
   precedent), but Task 5's switch flow must surface it rather than loop —
   already a Task 5 risk item (R10 in my plan); no Task 1 action.
8. **Open — line ordering.** `id ASC` (insertion order) chosen for stable UI
   diffing; recency-first (`updated_at DESC`) is a one-line flip if the owner
   prefers "last scanned on top" (Task 5 can also sort client-side).
