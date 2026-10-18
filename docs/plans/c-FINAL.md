# Task C FINAL spec — Checkout + purchase history

Merged from `c-proposal-{glm,qwen}.md` (kimi unavailable).

## Models (`app/models.py`, new tables via `create_all()`)

- `Purchase`: `id` PK, `user_id: int = Field(foreign_key="users.id", index=True)`,
  `total_cents: int`, `created_at`.
- `PurchaseLine`: `id` PK, `purchase_id: int = Field(foreign_key="purchases.id",
  index=True)`, `item_id: int | None` (**plain-int weak ref** — history survives
  item deletion), `name: str`, `sku: str`, `price_cents: int` (snapshot),
  `qty: int` (requested), `applied_qty: int` (actually sold), `clamped: bool`.

## `POST /api/orders` (checkout, authenticated member)

- Read the cart; **empty cart → 400**.
- On a dedicated connection, `BEGIN IMMEDIATE` (the `adjust_stock` precedent —
  a guarded read-modify-write needs the lock, and a single `UPDATE …
  RETURNING` **cannot** report `applied_qty` because SQLite's RETURNING only
  sees post-update values):
  - for each cart line: re-read the item; **orphan (item deleted) → skip** with
    a note; `old = item.stock`; `applied = min(qty, old)`; `clamped = qty > old`;
    `item.stock = max(0, old - qty)` (the SQL clamp is defence-in-depth);
    snapshot `name`/`sku`/`price_cents` (live at checkout time).
  - create `Purchase` + `PurchaseLine`s; `total_cents = Σ price_cents *
    applied_qty`; clear the cart (delete rows by id).
- Return `CheckoutOut {purchase, lines: [...], cart (post-checkout), skipped:
  [orphaned item_ids]}`. **Clamp-not-veto**: a shortfall sells what is available
  and the receipt records the ordered amount (`qty`) with `applied_qty`/`clamped`.

## History reads

- `GET /api/orders/mine` (member): own purchases, newest first, with lines.
- `GET /api/members/{id}/orders` (staff): any member's history.

## Cleanup

- `delete_member` deletes the member's `PurchaseLine`s and `Purchase`s in the
  same transaction (history is erased with the account — doctrine parity).

## Tests (`tests/test_purchases.py`)

~15 cases: happy checkout (snapshot + total + cart cleared); empty-cart 400;
clamp (stock 3, qty 5 → applied 3, clamped, sale recorded, receipt shows ordered
5); exact-to-zero boundary (applied == qty, clamped false — the guard against a
future RETURNING-only "optimisation"); zero-stock sale (applied 0); orphan skip +
all-orphan 400; concurrent double-checkout (barrier threads → exactly one
purchase, loser 400); two-user last-unit race (both sell, applied multiset
{1,0}, stock 0); weak-ref survival past item delete; mine isolation/ordering;
staff privilege matrix; cleanup twin pins; gates. Extend `test_migrations.py`
(legacy new-tables).
