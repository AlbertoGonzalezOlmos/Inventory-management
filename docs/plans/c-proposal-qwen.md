# Task C proposal (qwen3.8-max) — Checkout + purchase history (backend)

Implements `docs/plans/COORDINATOR2.md` decision 2 + Task C, per the tasking
prompt (authoritative for routes: `POST /api/orders`, `GET /api/orders/mine`,
staff `GET /api/members/{id}/orders`; models `Purchase`/`PurchaseLine`;
empty cart → **400**; orphans skipped with a note; race-free clamp
preferred over the BEGIN IMMEDIATE ritual).

**Verified this pass:** `delete_member` already chains explicit cleanups
(`auth_tokens` → `cart_items` → `scan_requests` → user, same transaction) —
purchases join that chain; `StockAdjustOut {requested_delta, applied_delta,
clamped}` is the clamp-report idiom ("a sale beyond available stock is
reported, never silently dropped", REVIEW-m10 P6); `adjust_stock`'s
`BEGIN IMMEDIATE` raw-connection ritual exists because its *guard* must read
the latest committed stock under the write lock; `cart_items` has the unique
`(user_id, item_id)` constraint and `_cart_out`'s lazy orphan reap; new
tables arrive via `create_all()` (no migration entries; the Task-6
legacy-tables test is the pin to extend); SQLite here is **3.45.1** (so
`UPDATE … RETURNING` exists — and §2 shows why it is still not enough).

---

## 1. Models (`app/models.py`, new tables ⇒ `create_all()` only)

```python
class Purchase(SQLModel, table=True):
    """One completed checkout — an immutable receipt.

    Recorded when the member presses checkout (decision 2): the cart is
    intent, the purchase is the sale. Lines snapshot everything the receipt
    needs (name/sku/price), so history survives price edits AND item
    deletion; stock movement is reported per line (clamp doctrine), never
    a veto on the sale.
    """
    __tablename__ = "purchases"
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    total_cents: int                       # Σ line_total_cents, frozen at checkout
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)


class PurchaseLine(SQLModel, table=True):
    """One receipt line: the snapshot + what the stock clamp actually did."""
    __tablename__ = "purchase_lines"
    id: int | None = Field(default=None, primary_key=True)
    purchase_id: int = Field(foreign_key="purchases.id", index=True)
    # WEAK reference (plain int, deliberately NOT a FK — the
    # scan_requests.resolved_item_id precedent): history outlives the
    # catalogue entry; read paths never join on it, they render the
    # snapshots. NULL is never written (orphans are skipped at checkout),
    # but readers tolerate it.
    item_id: int | None = None
    sku: str                               # snapshot at checkout
    name: str                              # snapshot at checkout
    price_cents: int                       # snapshot — LIVE price at checkout time
    qty: int                               # what the member bought (requested)
    line_total_cents: int                  # price_cents × qty, frozen arithmetic
    stock_applied: int                     # what stock actually absorbed (≤ qty)
    clamped: bool                          # stock_applied != qty (reported, never silent)
```

No `updated_at` anywhere (receipts are immutable); no status column
(refunds/voids are the successor tracked issue, out of scope); indexes only
where queries filter (`user_id`, `purchase_id`).

## 2. The race-free clamp mechanism (the prompt's core question)

**Why single-statement `UPDATE … MAX(0, stock-:qty) … RETURNING stock` is
not sufficient, even on SQLite 3.45:** RETURNING yields only the NEW stock,
and `applied`/`clamped` cannot be reconstructed from it at the boundary —
`new == 0` is ambiguous between `old == qty` (applied = qty, **not**
clamped) and `old < qty` (applied = old, clamped). The clamp itself is
race-free in one statement, but decision 2's *report* (`requested/applied/
clamped` per line, the `StockAdjustOut` idiom) needs the pre-value.

**Chosen mechanism — one plain ORM transaction that takes the write lock
first, then reads:**

```
1. read the caller's cart lines            (autocommit SELECT — no lock yet)
2. valid = lines whose item still exists   (orphans → skipped[], see §3)
   if not valid → 400 "Cart is empty"      (nothing written)
3. INSERT the Purchase header              ← FIRST DML: SQLite's single-
                                              writer lock is now held by
                                              this transaction (busy_timeout
                                              makes a rival checkout WAIT,
                                              not fail; WAL readers proceed)
4. per valid line, INSIDE the transaction:
     old = SELECT items.stock WHERE id     (stable: no other writer can
                                            commit while the lock is held)
     applied = min(qty, old); new = old - applied; clamped = applied != qty
     UPDATE items SET stock = :new, updated_at = :now WHERE id = :id
     INSERT PurchaseLine(snapshots from the item row read in the same txn,
                         qty, applied, clamped, line_total)
5. total_cents = Σ line_total → set on the header (or compute before INSERT
   and insert once — implementation detail; one commit either way)
6. DELETE FROM cart_items WHERE id IN (:snapshotted_line_ids)   ← by ID, not
   by user: a line added by a second tab mid-checkout SURVIVES (it was never
   purchased); the Task-1 last-writer-wins doctrine, narrowed to the rows
   this receipt actually covers
7. commit; return CheckoutOut
```

Properties: **no `BEGIN IMMEDIATE` raw-connection ritual** — the lock is
acquired by the transaction's own first DML (step 3), which is exactly the
guarantee the ritual exists to obtain; the guarded read (step 4's SELECT)
runs *under* that lock, so the read-modify-write cannot lose to a rival
(the rival is parked on `busy_timeout` and then reads our committed stock —
the same serialisation `adjust_stock` pays 40 lines of raw-DBAPI code for).
Two concurrent checkouts of *different* users racing for the last unit:
writer 2 waits, then reads stock 0 → `applied 0, clamped true` → **both
sales are recorded** (decision 2: clamp-and-report, never reject). The
endpoint docstring must state this reasoning (the `adjust_stock` and
`_upsert_line` docstrings are the house precedent for explaining *why* a
write path is race-free).

## 3. `POST /api/orders` (checkout)

New `app/routers/orders.py` (`APIRouter(prefix="/api/orders",
tags=["orders"])`, registered in `app/main.py`). Auth: `get_current_user` —
any role with a cart (members shop; staff can too); the
`must_change_password` gate applies automatically (not exempt).

Schemas (`app/schemas.py`):

```python
class PurchaseLineOut(BaseModel):          # receipt line (+ clamp report)
    item_id: int | None
    sku: str
    name: str
    price_cents: int
    qty: int
    line_total_cents: int
    stock_applied: int
    clamped: bool

class PurchaseOut(BaseModel):
    id: int
    total_cents: int
    created_at: datetime
    lines: list[PurchaseLineOut]

class SkippedCartLineOut(BaseModel):       # the orphan "note"
    item_id: int
    qty: int
    reason: str                            # "item no longer exists"

class CheckoutOut(BaseModel):              # one payload, no read-after-write
    purchase: PurchaseOut
    skipped: list[SkippedCartLineOut]      # orphan cart lines, skipped+noted
    cart: CartOut                          # the post-checkout cart (usually
                                           # empty; a mid-checkout add survives)
```

Behaviour:
- **Empty cart (or all-orphan) → 400** `"Cart is empty"` (prompt-specified
  status; nothing written; orphan rows are left to `_cart_out`'s existing
  lazy reap — checkout does not double as a reaper, one mechanism per job).
- Orphans (item deleted since the line was added): skipped, listed in
  `skipped`, never snapshotted; the valid lines still checkout (partial
  success is the honest outcome — the member sees exactly what was skipped).
- Prices: `price_cents` is read **in the checkout transaction** — "live at
  checkout time" (the cart's `line_total_cents` from add-time is ignored;
  the receipt reflects what the shop charges at the sale).
- Stock: §2 clamp-and-report; **never a veto**, never negative.
- Cart: deleted **by snapshotted row ids** (§2 step 6).

## 4. History reads

- **`GET /api/orders/mine`** (any user): own purchases, newest first
  (`id DESC`), `limit` (default 20, `ge=1 le=100`) + `offset` (the
  `list_items` pagination idiom) → `list[PurchaseOut]` with lines embedded
  (receipts are small; no separate lines endpoint).
- **`GET /api/members/{user_id}/orders`** (in `app/routers/members.py`,
  its prefix; shared `PurchaseOut` assembly imported from `orders.py` — the
  `scan_requests → cart._upsert_line` cross-router precedent):
  `require_staff` + **`_get_manageable_member`** (staff never list an
  admin's orders; admins list anyone's; unknown id 404) — the badge-view
  privilege shape, same pagination params.
- No `GET /api/orders/{id}` (not in the prompt; the list embeds lines —
  add it when a receipt deep-link is actually needed).
- No staff-wide order table (deferred by all plan-2 proposals; noted).

## 5. Cleanup (`app/routers/members.py::delete_member`)

Extend the existing chain, same transaction, before `session.delete(user)`:

```python
    purchase_ids = [p.id for p in session.exec(
        select(Purchase).where(Purchase.user_id == user_id)).all()]
    if purchase_ids:
        for line in session.exec(select(PurchaseLine).where(
                PurchaseLine.purchase_id.in_(purchase_ids))).all():
            session.delete(line)
    for purchase in …: session.delete(purchase)
```

(Operational data dies with the account — the cart/scan_requests doctrine.
An alternative "keep receipts, anonymise the owner" is a records-retention
policy the repo does not have; flagged as an open question, not built.)
**`delete_item` touches nothing** — lines are snapshots with a weak
`item_id`; history must survive catalogue churn (tested).

## 6. Edge-case matrix

| Case | Behaviour |
|---|---|
| Empty cart / all lines orphan | **400**, nothing written |
| Some lines orphan | 201-equivalent success: purchase with the valid lines, `skipped` notes the rest |
| Two checkouts, same user (double-click / two tabs) | SQLite serialises: first wins; second sees the snapshotted rows gone → its valid-line set is empty → **400** (exactly one purchase — the concurrency test pins the row count) |
| Two users race for the last unit | Both purchases recorded; loser gets `stock_applied: 0, clamped: true`; stock floors at 0 |
| `old == qty` boundary | `applied == qty`, `clamped == false` (the case RETURNING-alone could not report — pinned) |
| Stock already 0 | Sale recorded, `applied 0, clamped true` (decision 2: report, don't reject) |
| Price edited between add and checkout | Receipt carries the checkout-time price |
| Price edited after checkout | History frozen (snapshot pin) |
| Item deleted after checkout | History intact (weak ref + snapshots) |
| Cart line added mid-checkout (second tab) | Survives (delete-by-snapshotted-id); returned in `CheckoutOut.cart` |
| Item deleted between cart-read and stock-update (same txn) | Impossible to interleave: the write lock is held from step 3; a delete committed *before* step 3 makes the line an orphan (skipped) |
| Flagged account / anonymous | 403 / 401 (gate inherited) |
| `total_cents` | Σ `line_total_cents`, computed once at checkout, stored |

## 7. Tests — new `tests/test_orders.py` (~18 cases)

Fixtures/helpers from conftest (`client`, `admin_token`, `create_user`,
`auth`); items via the staff API (fake embeds); the `threading.Barrier`
pattern from `test_cart.py`/`test_stock_adjust.py`.

1. **Happy checkout**: 2-line cart → 200 `CheckoutOut`; receipt snapshots
   (sku/name/price), `line_total_cents` per line, `total_cents` = Σ; cart
   empty afterwards (`GET /api/cart`); `items.stock` decremented exactly per
   line; `skipped == []`.
2. **Price-snapshot pins**: staff PATCH price after checkout → history
   unchanged; a second checkout of the same item receipts the NEW price
   (live-at-checkout).
3. **Empty cart → 400**, nothing written (no purchase rows).
4. **Clamp**: stock 3, qty 5 → `qty 5, stock_applied 3, clamped true`,
   `items.stock == 0`, sale NOT rejected, `line_total = 5 × price` (the
   receipt is what was ordered; the clamp is stock's business).
5. **Boundary**: stock == qty → `applied == qty, clamped false`.
6. **Zero stock**: stock 0, qty 2 → `applied 0, clamped true`, purchase
   succeeds.
7. **Orphans**: raw-DB cart row with a dead `item_id` + one valid line →
   purchase has the valid line, `skipped` names the orphan `{item_id, qty}`;
   all-orphan cart → 400.
8. **Concurrent double-checkout (same user)**: 2 threads, one barrier →
   exactly ONE purchase row, exactly one 200, the other 400; cart empty.
9. **Concurrent last-unit race (two users)**: stock 1, both qty 1 → both
   200; one `applied 1 clamped false`, the other `applied 0 clamped true`;
   final stock 0 (order-agnostic assertion: the multiset of applieds is
   {1, 0}).
10. **Weak-ref survival**: delete the item post-checkout → `GET /orders/mine`
    renders the receipt identically (snapshots), no 500.
11. **mine**: own-only (B never sees A's), newest-first, limit/offset bounds
    (limit 101 → 422, limit 0 → 422).
12. **Staff member-orders**: staff→member 200 (same shape); staff→admin 403
    (manage rule); admin→any 200; member caller 403; unknown id 404.
13. **delete_member cleanup**: purchases+lines gone (direct DB), another
    user's purchases untouched; **delete_item leaves purchases** (the twin
    pin).
14. **Gates**: unauthenticated 401 (all three endpoints); flagged 403.
15. **Mid-checkout add survives** (sequential approximation): add A,
    snapshot semantics — add B *after* issuing checkout is not testable
    without true races; pin the delete-by-id contract instead: checkout
    removes exactly the snapshotted rows (direct-DB row ids before/after
    with a concurrent-style manual insert between read and commit is out of
    reach in TestClient — assert the *implementation contract* via a second
    line added immediately after checkout → it is still in the cart).
16. **`CheckoutOut.cart`** equals a follow-up `GET /api/cart` (mutator
    contract).
17. **total arithmetic** on a 3-line mixed-qty cart.
18. **`test_migrations.py`**: extend `test_new_tables_land_on_a_legacy_database`
    with `purchases` + `purchase_lines` (create_all path; no COLUMN/INDEX
    entries — exact-set test untouched).

Suite hygiene: full `uv run pytest` green; `test_cart.py`/`test_items.py`
untouched (cart endpoints unchanged — checkout is additive); no SPA changes
(Task D wires the button).

## 8. Files touched

| File | Change |
|---|---|
| `app/models.py` | `Purchase`, `PurchaseLine` (weak-ref + immutability comments) |
| `app/schemas.py` | `PurchaseLineOut`, `PurchaseOut`, `SkippedCartLineOut`, `CheckoutOut` |
| `app/routers/orders.py` | **new** — `POST ""` (checkout, §2 mechanism), `GET /mine`, shared `_purchase_out` assembler |
| `app/main.py` | `include_router(orders.router)` (no migration entries) |
| `app/routers/members.py` | `GET /{user_id}/orders` (manage rule) + `delete_member` cleanup chain extension |
| `tests/test_orders.py` | **new** (~18 cases) |
| `tests/test_migrations.py` | legacy-tables test += the two tables |
| *(untouched)* | cart router, items router (no stock-adjust changes), deps, security, SPA |

## 9. Naming note + risks / open questions

1. **Routes say "orders", models say "purchases"** — the prompt specifies
   both (`POST /api/orders`; `Purchase`/`PurchaseLine`, matching
   COORDINATOR2's `purchases`/`purchase_lines` table names). Followed
   literally; schemas follow the models (`PurchaseOut`). If the coordinator
   wants surface consistency, renaming the *routes* to `/api/purchases` is
   mechanical and compatibility-free (nothing consumes them yet — Task D
   lands after). Flagged, not decided here.
2. **Oversell is policy**: clamped sales mean the shop can sell what it
   doesn't have (recorded + reported). That is decision 2 verbatim and
   matches `stock-adjust`'s doctrine; the alternative (reject) was glm's
   plan-2 position, already adjudicated.
3. **The cart re-read window** (step 1 reads outside the lock): narrowed by
   delete-by-snapshotted-id (a mid-checkout add survives) and by SQLite's
   writer serialisation for everything inside the transaction; full
   isolation would need `BEGIN IMMEDIATE` — explicitly not worth it for a
   single-member cart (Task-1's documented last-writer-wins class).
4. **No refunds/voids/status** — receipts are append-only; a refund is the
   successor tracked issue (stock restored via the existing staff
   `stock-adjust`, a new purchase with negative totals, or a status column —
   coordinator's future call).
5. **delete_member erases purchase history** (doctrine parity with
   carts/requests). If the owner ever wants receipts kept for accounting,
   that is an anonymisation feature (null the user link), not a cleanup —
   flagged, not built.
6. **`GET /api/orders/mine` embeds lines** — fine at shop scale (a receipt
   is a handful of rows); pagination caps the list, not the lines.
7. **RETURNING was evaluated and rejected for the report** (§2) though
   SQLite 3.45 supports it — recorded so a future reviewer does not
   "optimise" the two-statement clamp into a one-statement version that
   silently loses the `old == qty` boundary (test 5 is the guard).
