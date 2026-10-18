# Task C proposal (glm) — Checkout + purchase history (backend only)

Coordinator2 decision 2 + Task C. A purchase is an **explicit checkout
action**: `POST /api/orders` snapshots the cart into an immutable receipt,
decrements stock with **clamp-and-report**, and clears the cart — all in one
atomic transaction. History (`GET /api/orders/mine`, staff
`GET /api/members/{id}/orders`) reads the snapshots, immune to later price
edits and item deletion. Cart adds stay stock-neutral (decision 6 unchanged).

Grounding verified: `adjust_stock` (`app/routers/items.py`) — the raw-
connection `BEGIN IMMEDIATE` pattern with `isolation_level=None`,
busy_timeout via the pool's connect event, 503-on-locked, the guarded
read-modify-write justification, and `StockAdjustOut {requested_delta,
applied_delta, clamped}` (exact-to-zero is NOT a clamp); `app/routers/cart.py`
(`_cart_out` reaps orphan lines on read; `_get_line`/`_upsert_line`; DELETE
`/api/cart` clears); models: `Item(stock, sku, name, price_cents, barcode)`,
`CartItem(user_id, item_id, qty)`, weak-ref precedent
`scan_requests.resolved_item_id`; `delete_member`'s same-transaction cleanup
loops; new tables via `create_all()` (no migration entries).

---

## 0. The clamp mechanism — a deviation from the stated preference, with the analysis

The tasking prefers "a single-statement `UPDATE items SET stock =
MAX(0, stock - :qty) … RETURNING` over BEGIN IMMEDIATE". **That statement
cannot report `applied` correctly**: SQLite's `RETURNING` sees only
post-update values (verified empirically this pass: stock 3, qty 5 →
`RETURNING stock` yields 0; the old stock 3 is unrecoverable), and
`applied_qty = min(qty, old_stock)` needs the old stock. Reading the old
stock *before* the update (a plain SELECT) is exactly the stale-read hazard
the write lock exists to close: a competing checkout committing between the
read and the update makes the recorded `applied` wrong (SQLite would
upgrade the deferred transaction on a stale snapshot — the
`SQLITE_BUSY_SNAPSHOT` class `adjust_stock`'s docstring already documents).
A read *after* the update yields the new stock, not the old.

**Proposed: the `adjust_stock` precedent, extended to the whole cart** —
ONE `BEGIN IMMEDIATE` transaction on one raw connection covering the entire
checkout: read cart + items under the write lock (reads guaranteed current),
compute `applied` per line, apply **clamped single-statement updates**
(`stock = MAX(0, stock - :qty)` — kept in SQL as defence-in-depth even
though the lock already guarantees the arithmetic), insert the purchase +
snapshot lines, delete the cart lines, COMMIT. Race-free by the lock;
`applied` trivially correct; `busy_timeout` + 503-on-locked reuse the
existing machinery verbatim.

*Rejected alternative (noted for the coordinator):* lock-free CAS —
`UPDATE … WHERE id = :id AND stock = :old_read` per line, retry on miss.
Correct and avoids BEGIN IMMEDIATE, but adds retry loops, a new failure
mode (item deleted mid-retry), and no repo precedent, for zero benefit at
shop concurrency (two kiosks). The single-statement preference's original
virtue ("no guard race") is fully preserved by the lock; only the
mechanical form differs.

## 1. Models (`app/models.py` — new tables, no migration entries)

```python
class Purchase(SQLModel, table=True):
    """A completed checkout (Task C): an immutable receipt. The cart is
    intent; THIS is the sale — the only member-facing flow that touches
    stock (decision 2)."""
    __tablename__ = "purchases"
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    total_cents: int = 0          # Σ price_cents × applied_qty (what was paid)
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)


class PurchaseLine(SQLModel, table=True):
    """One line of a purchase receipt — a DENORMALISED SNAPSHOT: what was
    actually bought, at the checkout-time price. History must never rewrite
    when the catalogue changes (price edits) or disappear (item deletion)."""
    __tablename__ = "purchase_lines"
    id: int | None = Field(default=None, primary_key=True)
    purchase_id: int = Field(foreign_key="purchases.id", index=True)
    # Plain int, deliberately NOT a FK: history survives item deletion (the
    # scan_requests.resolved_item_id weak-ref precedent). NULL never occurs
    # in practice (orphan lines are skipped, never snapshotted) but the
    # schema tolerates it.
    item_id: int | None = None
    sku: str
    name: str
    price_cents: int             # the price AT CHECKOUT
    requested_qty: int           # what the member wanted
    applied_qty: int             # what was actually deliverable (clamped at stock)
    line_total_cents: int        # price_cents × applied_qty
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)
```

`clamped` is derived (`requested_qty != applied_qty`) in the Out schema —
one source of truth, nothing to drift (the StockAdjustOut doctrine).

## 2. Schemas (`app/schemas.py`)

```python
class OrderLineOut(BaseModel):
    item_id: int | None
    sku: str
    name: str
    price_cents: int
    requested_qty: int
    applied_qty: int
    clamped: bool
    line_total_cents: int

class OrderOut(BaseModel):
    id: int
    created_at: datetime
    total_cents: int
    lines: list[OrderLineOut]

class SkippedLineOut(BaseModel):
    """A cart line skipped at checkout: its item no longer exists. The
    checkout proceeds for the rest; the receipt notes the skip."""
    item_id: int
    qty: int

class CheckoutOut(OrderOut):
    """The receipt of a checkout — the order plus what was skipped."""
    skipped: list[SkippedLineOut] = []

class OrderListOut(BaseModel):  # or plain list[OrderOut]; repo style: list
    ...
```

(Keep `GET` responses as plain `list[OrderOut]` — repo style.)

## 3. `POST /api/orders` — the checkout (`app/routers/orders.py`, new)

Auth: `get_current_user` (any role; members are the point). **Not exempt**
from the pw-change gate (403 while flagged). Empty cart → **400 "Cart is
empty"** (checked on the plain cart read before any transaction).

The transaction (one raw connection, the `adjust_stock` recipe verbatim —
`session.close()` to release the auth-read connection first,
`isolation_level=None`, `BEGIN IMMEDIATE`, 503-on-locked with Retry-After,
restore isolation in `finally`):

1. `BEGIN IMMEDIATE` — the write lock: every read below is current and
   competing checkouts serialise behind it (SQLite single-writer).
2. Read the caller's `cart_items` (user-scoped). Join `items` by id.
3. **Orphan lines** (item deleted): append to `skipped`, DELETE the line
   (the checkout-time analogue of `_cart_out`'s reap), continue.
4. If no purchasable lines remain → COMMIT the orphan cleanup, 400
   "Cart is empty".
5. Per line: read `old_stock` (current, lock-protected) →
   `applied = min(qty, old_stock)` →
   `UPDATE items SET stock = MAX(0, stock - :qty), updated_at = :now
   WHERE id = :id` (clamped in SQL as the guard; the lock guarantees the
   arithmetic anyway) → snapshot `sku/name/price_cents` from the row just
   read, `line_total = price × applied`.
6. INSERT `purchases` (`total_cents = Σ line_totals`) + the `purchase_lines`.
7. DELETE the caller's remaining cart lines.
8. COMMIT. Any failure → ROLLBACK (nothing partial: stock, receipt and
   cart move together or not at all).

Response **201 `CheckoutOut`** — the receipt: order + lines with
requested/applied/clamped + `skipped`. Stock is decremented **only** here
(cart adds were neutral); a sale beyond stock is **recorded and reported**,
never silently dropped (the `stock-adjust` clamp doctrine; checkout does not
veto — the barebones-shop decision, and the receipt is honest about the
shortfall).

## 4. Reads

- **`GET /api/orders/mine`** (`get_current_user`; `limit` default 20 ≤ 100,
  `offset`): own purchases, newest first, lines embedded (orders are small).
- **`GET /api/members/{user_id}/orders`** (`require_staff` +
  `_get_manageable_member` — staff never read an admin's history; 404
  unknown member): same shape. The owner's staff-side view.
- Lines load via one `purchase_id IN (…)` query (the `vector_search`
  pattern), assembled newest-first.

## 5. `delete_member` cleanup (`app/routers/members.py`)

Purchases and their lines die with the account (operational data, like
cart rows and scan requests — D4 doctrine, same transaction): delete the
user's `purchase_lines` (via their `purchase_id`s) then the `purchases`.
Item deletion does **not** touch history (weak ref by design).

## 6. Edge cases

| Case | Behaviour |
|---|---|
| Two concurrent checkouts (same user) | The lock serialises; the loser's transaction re-reads an EMPTY cart → 400; one receipt exists |
| Two carts racing the last unit (stock 1, qty 1 each) | One full, one clamped to `applied 0` — the line still appears (requested 1, applied 0, clamped), an honest receipt of the shortfall |
| qty == stock exactly | `applied == qty`, `clamped` FALSE (exhausted, not clamped — the StockAdjustOut boundary) |
| Total | Σ `price × applied` — never requested (you don't pay for undelivered goods) |
| Price edit after purchase | History unchanged (snapshot) |
| Item deleted after purchase | History intact (weak ref) |
| Orphan cart line at checkout | Skipped + noted + removed; the rest check out |
| Cart of only orphans | 400 + the orphans cleaned (committed housekeeping) |
| Empty cart | 400, no transaction opened |
| Flagged account / anonymous | 403 / 401 |

## 7. Tests (`tests/test_orders.py`, new; `test_migrations.py` extended)

1. **Happy path**: multi-line cart → 201; stock decremented per line;
   cart cleared (`GET /api/cart` empty); receipt exact (snapshots,
   requested == applied, clamped false, totals).
2. **Clamp**: stock 3, qty 5 → applied 3, clamped true, requested 5
   recorded, stock 0, line total 3 × price, total reflects applied.
3. **Exact-to-zero**: qty == stock → clamped FALSE (the boundary pin).
4. **Zero-applied line**: two carts racing the last unit → the loser's
   receipt shows requested 1 / applied 0 / clamped.
5. **Concurrency — double checkout**: barrier-parallel same-user checkouts
   → exactly one 201, one 400 "Cart is empty", stock decremented once
   (the BEGIN IMMEDIATE pin, `test_concurrency.py` style).
6. **Concurrency — last unit**: barrier-parallel different-user checkouts
   on stock 1 → receipts per case 4; final stock 0.
7. **Price snapshot**: checkout → staff PATCH price → history shows the
   old price; `/mine` unchanged.
8. **Item deletion after purchase**: history intact, lines render (weak ref).
9. **Orphan skip**: direct-DB cart line for a nonexistent item → checkout
   201 with `skipped`, the rest purchased, the orphan removed.
10. **Only orphans** → 400 + the cart is clean afterwards.
11. **`/mine` isolation + ordering + pagination**: A never sees B's; newest
    first; limit/offset respected.
12. **Staff history**: staff→member 200; staff→admin target 403 (manage
    rule); member on the staff endpoint 403; unknown id 404.
13. **`delete_member` cleanup**: purchases + lines gone (direct DB), other
    users' intact.
14. **Gates**: anonymous 401; flagged 403.
15. **Migrations**: extend the legacy new-tables case — `create_all()` on a
    legacy DB gains `purchases` + `purchase_lines` (the Task-6 pattern).

## 8. Files touched

| File | Change |
|---|---|
| `app/models.py` | `Purchase`, `PurchaseLine` (new tables) |
| `app/schemas.py` | `OrderLineOut`, `OrderOut`, `CheckoutOut`, `SkippedLineOut` |
| `app/routers/orders.py` | **new** — `POST /api/orders`, `GET /api/orders/mine` |
| `app/routers/members.py` | `GET /api/members/{id}/orders`; `delete_member` cleanup |
| `app/main.py` | router registration |
| tests | `test_orders.py` (new, ~15 cases), `test_migrations.py` |

*(Untouched: the cart router's endpoints — checkout composes the table, not
the endpoints; `adjust_stock`; the SPA.)*

## 9. Risks / flagged decisions

1. **The mechanism deviation (§0)** — the single-statement preference
   cannot report `applied`; BEGIN IMMEDIATE (the repo's own tested
   precedent) can. The clamp stays in SQL as defence-in-depth. Flagged for
   the coordinator with the empirical RETURNING analysis; the CAS
   alternative is documented if lock-free is ever required.
2. **Clamp-not-veto on shortfall** (coordinator decision 2's wording):
   a sale beyond stock is recorded with the shortfall reported. If the
   owner prefers rejection, the per-line `applied < qty` check becomes a
   ROLLBACK + 409 naming the item (the reject design from the plan-2
   review) — a two-line change, flagged.
3. **Zero-applied lines appear in receipts** (requested 1, applied 0): an
   honest receipt beats a silently missing line; the UI can grey them out
   (Task D).
4. **One BEGIN IMMEDIATE holds the lock for the whole cart** (a few
   statements): bounded by cart size (≤ the 200-line cap) and busy_timeout
   (5 s); identical in kind to `adjust_stock`, which the concurrency tests
   already guard.
5. **`skipped` appears only on the checkout response**, not in history
   reads (history shows what was purchased; the skip note is a one-time
   receipt detail).
