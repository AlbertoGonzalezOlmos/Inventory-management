# Task 1 proposal (kimi-k3): Server-side per-user cart

Coordinator plan reference: `docs/plans/COORDINATOR.md` Task 1. Binding
decisions honoured here: cart is per-user and durable; adding to cart never
mutates stock (UI warns when qty > stock, no hard clamp); cart rows are
cleaned up explicitly on item/user delete (PRAGMA foreign_keys is OFF —
FKs are declarative only). Scope is Task 1 only; scanner endpoints, the
SPA, and switch flows are later tasks, but the API shape below is designed
so Task 5's save-then-switch needs nothing more.

Derived against repo HEAD `bfd09fe`.

---

## 1. Data model

New model `CartItem` in `app/models.py` (alongside `User`/`Item`/`AuthToken`):

```python
class CartItem(SQLModel, table=True):
    """One line in a user's durable shopping cart."""

    __tablename__ = "cart_items"
    __table_args__ = (
        UniqueConstraint("user_id", "item_id", name="uq_cart_items_user_item"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    item_id: int = Field(foreign_key="items.id", index=True)
    qty: int = Field(default=1)                      # >= 1; validated in schemas
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)
```

Design notes:

- **Table name `cart_items`**, matching the snake_case plural convention
  (`users`, `items`, `auth_tokens`, `app_meta`).
- **Unique `(user_id, item_id)`** is the load-bearing invariant: one row per
  (user, item) pair is what makes "already in cart" an increment rather than
  a duplicate line, and it gives the concurrency story a race-free authority
  (same doctrine as `items.barcode` / `items.sku` uniqueness — the pre-check
  is a courtesy, the constraint is the law).
- `user_id` indexed: every cart read is `WHERE user_id = ?`. `item_id`
  indexed: cleanup on item delete is `WHERE item_id = ?`, and with the
  foreign_keys pragma OFF nothing else would use that column efficiently.
- `created_at`/`updated_at` mirror `Item`'s pair (naive UTC via the existing
  `utcnow()`, `sa_type=DateTime`). `updated_at` gives the UI a stable
  sort/merge key for the legacy-import path.
- **No `price_cents` snapshot column** — see §3 (live price decision).

### Migration story

None needed. `cart_items` is a **new table**, so the existing
`create_all()` (`app/database.py`, called at lifespan startup in
`app/main.py:107`) creates it on every database, fresh or upgraded — the
same pattern the repo used for `AppMeta` ("A new table needs no migration —
create_all() adds tables it does not find"). The unique constraint and both
indexes are part of the CREATE TABLE DDL, so **no `COLUMN_MIGRATIONS` and
no `INDEX_MIGRATIONS` entries** (those tuples are append-only and only for
ALTERing *existing* tables; adding entries for a table create_all already
makes would be dead weight). `tests/test_migrations.py` gets a case
asserting an "old" database (created from a metadata snapshot without the
model, or simply: table dropped then app restarted) gains `cart_items` with
the unique constraint — matching how that file already validates the
`auth_tokens.expires_at` index story.

No backfill: there is no server-side cart data anywhere today; the legacy
browser `localStorage` import is a Task-5 (SPA) concern that talks to the
plain POST/PUT endpoints.

---

## 2. API surface

New router `app/routers/cart.py`, `prefix="/api/cart"`, `tags=["cart"]`,
registered in `app/main.py` next to the other three
(`app.include_router(cart.router)`). **Auth: every endpoint requires
`get_current_user`; any role** (member/staff/admin all shop). Consequences
inherited for free: 401 without a valid token; 403 while
`must_change_password` is set (`/api/cart*` is deliberately *not* added to
`MUST_CHANGE_EXEMPT_PATHS` — a flagged account must change its password
before shopping, same as before).

All schemas in `app/schemas.py`, following the existing conventions:
`_Stripped` base (strip whitespace, `extra="forbid"`), explicit bounds so a
garbage client gets 422, never a silent clamp.

```python
class CartAddIn(_Stripped):
    item_id: int = Field(ge=1)
    qty: int = Field(default=1, ge=1, le=10_000)

class CartQtyIn(_Stripped):
    qty: int = Field(ge=0, le=10_000)   # 0 = remove the line

class CartReplaceIn(_Stripped):
    # Whole-cart replace: staging merge after login, legacy localStorage
    # import, and the save-then-switch flush all use this one shape.
    rows: list[CartAddIn] = Field(max_length=500)

class CartRowOut(BaseModel):
    item: ItemOut            # live catalogue data incl. stock & price_cents
    qty: int
    line_total_cents: int
    added_at: datetime

class CartOut(BaseModel):
    rows: list[CartRowOut]
    item_count: int          # sum of qty
    total_cents: int         # sum of live line totals
```

| Method & path | Body | Success | Errors |
|---|---|---|---|
| `GET /api/cart` | — | `200 CartOut` (empty cart → `rows: []`) | 401 |
| `POST /api/cart/items` | `CartAddIn` | `200 CartOut` after upsert | 401, 404 unknown item, 422 bad qty |
| `PATCH /api/cart/items/{item_id}` | `CartQtyIn` | `200 CartOut` (qty 0 → line removed) | 401, 404 item/line unknown, 422 |
| `DELETE /api/cart/items/{item_id}` | — | `204` | 401, 404 line not in cart |
| `DELETE /api/cart` | — | `204` (idempotent: clearing an empty cart is fine) | 401 |
| `PUT /api/cart` | `CartReplaceIn` | `200 CartOut` (full replace, atomic) | 401, 404 any item unknown (whole request rejected), 422 |

Conventions honoured:

- **404 vs 422**: unknown `item_id` → 404 "Item not found" (mirrors
  `get_item`); syntactically bad qty → 422 from the schema. A PATCH for an
  existing item that simply isn't in *this* cart → 404 "Item not in cart".
- **DELETE single line → 404 when absent**, matching `delete_item`'s
  "DELETE of a missing resource is a client error" convention; **clear-all
  is 204-idempotent** because "the cart is empty" is the desired end state
  (and the SPA calls it fire-and-forget).
- No 409 on the happy path (the unique constraint is resolved by upsert);
  409 appears only as the internal retry signal of §3-concurrency, never in
  the API contract.
- `PUT` replaces inside one transaction (delete all rows, insert new) so a
  404/422 mid-validation leaves the old cart untouched — the flush before an
  account switch must never destroy the outgoing user's saved cart.

`CartRowOut.item` is the full `ItemOut`, so the SPA keeps its current
stock-aware UX (`qty > item.stock` → warning toast) with zero extra calls.

---

## 3. Behaviour

- **Add (`POST /items`)**: "already in cart" ⇒ `qty += payload.qty`
  (upsert); otherwise insert. Response is the whole `CartOut` so the SPA
  re-renders from one payload (and a racing second tab's changes are
  visible in the same response).
- **Update (`PATCH`)**: absolute set; `qty: 0` removes the line. Absolute
  (not delta) is right for the UI's +/−/direct-edit controls; the scanner's
  increment-by-one arrives via POST, not PATCH.
- **Remove / list / clear** as in the table. List orders by
  `created_at, id` (stable, oldest first — matches the current client-side
  list's append order).
- **Price: live, not snapshotted.** `CartRowOut` carries the current
  `Item.price_cents` and computed `line_total_cents`; totals are
  informational (checkout remains a tracked non-goal). A snapshot column
  would add a staleness question ("which price does the user pay?") the
  task doesn't ask us to answer, and the coordinator's decision list is
  silent on it — flagged as open question Q1.
- **Stock: untouched, unclamped** (coordinator decision 6). No UPDATE to
  `items.stock` anywhere in this router; no reservation. The UI warning
  needs `item.stock`, which `ItemOut` already includes.
- **Save-then-switch support (design for Task 5, no extra endpoints)**:
  because every mutation is already a server write, "save whatever had been
  done in the current user's account" is true at every instant; the switch
  flow is then exactly: await in-flight requests → (optional `PUT` flush of
  any debounced local diff) → secure the new session → revoke the old token
  → `GET /api/cart` to load the incoming user's cart. The atomicity of
  `PUT` (§2) is what guarantees a failed flush can't strand a half-saved
  cart.
- **Explicit cleanup** (coordinator decision; FKs are declarative only):
  - `DELETE /api/items/{item_id}` (`app/routers/items.py::delete_item`):
    delete `cart_items` rows `WHERE item_id = ?` in the same transaction as
    the item delete — mirrors the existing `AuthToken` cleanup loop in
    `members.py::delete_member`.
  - `DELETE /api/members/{user_id}` (`app/routers/members.py::delete_member`):
    delete rows `WHERE user_id = ?` next to the existing token cleanup.
- **Concurrency** (two rapid scans/increments of the same item, possibly two
  kiosks on one account): each POST is one transaction: SELECT the line;
  INSERT or UPDATE; commit. If two requests race the INSERT, the unique
  constraint makes one commit fail with `IntegrityError` → catch,
  `session.rollback()`, re-SELECT (WAL: the committed row is now visible),
  apply as UPDATE, commit. Both increments survive. Lock waits ride the
  existing `busy_timeout=5000`. This mirrors the repo's established
  IntegrityError-retry pattern (`register`, `create_item`, `update_item`)
  rather than introducing driver-level upsert SQL — same ORM style, same
  review surface. Whole-cart `PUT` is documented last-writer-wins (two
  kiosks replacing simultaneously — accepted, per plan).

---

## 4. Tests

**New file `tests/test_cart.py`** (fixtures `client`, `admin_token`,
`create_user`, `auth` from `conftest.py`; embeddings are already faked
there; items created via the staff API or directly through the session
helper the existing item tests use):

1. `test_unauthenticated` — all six endpoints → 401.
2. `test_must_change_password_gate` — flagged account → 403 on cart
   endpoints (reuse the pattern from `tests/test_auth.py`).
3. `test_add_and_list` — add item, GET shows one row with live price,
   `line_total_cents`, `total_cents`, `item_count`.
4. `test_add_existing_increments` — POST twice (qty 1 + qty 2) → one row,
   qty 3 (no duplicate line).
5. `test_add_unknown_item_404` / `test_add_bad_qty_422` (0, negative,
   > 10_000).
6. `test_patch_set_and_remove` — PATCH qty 5 → 5; PATCH qty 0 → line gone;
   PATCH unknown item → 404; PATCH item not in cart → 404; PATCH qty −1 →
   422; unknown JSON keys → 422 (`extra="forbid"`).
7. `test_delete_line` — 204 then 404 on second delete; deleting another
   user's line is impossible by construction (row is keyed to the caller).
8. `test_clear_idempotent` — clear non-empty → 204 + empty GET; clear again
   → 204.
9. `test_replace_atomic` — PUT two rows → exactly those rows; PUT with one
   unknown item_id → 404 **and the previous cart is unchanged**; PUT `[]` →
   empty.
10. `test_per_user_isolation` — Alice adds items; Bob's GET is empty; Bob
    cannot see/affect Alice's lines via any endpoint; totals differ
    correctly. (The core privacy regression test — the localStorage list
    leaked exactly this way.)
11. `test_live_price` — change an item's price via staff PATCH → cart GET
    reflects the new price (locks in the live-price decision).
12. `test_stock_untouched` — add qty 10 of a stock-3 item → `items.stock`
    still 3, no error; response carries `item.stock` for the UI warning
    (locks in coordinator decision 6).
13. `test_concurrent_increments` — modelled on `tests/test_concurrency.py`:
    N threads POST the same item concurrently → final qty == N, exactly one
    row. This is the test that justifies the IntegrityError-retry path; it
    fails deterministically without it.

**Extend existing files:**
- `tests/test_items.py` — deleting an item removes it from other users'
  carts (create two users with the item in both carts, staff DELETE
  `/api/items/{id}`, both carts empty of it; the item GET 404s).
- Member-deletion coverage (currently in `tests/test_security.py` /
  `tests/test_concurrency.py` where `DELETE /api/members/{id}` is
  exercised) — assert the deleted user's cart rows are gone (direct DB
  count via the test's session, the way `AuthToken` cleanup is asserted).
- `tests/test_migrations.py` — a database lacking `cart_items` gains it via
  startup `create_all()`, with the unique constraint present (attempt a
  duplicate (user_id, item_id) insert → IntegrityError).
- `tests/test_metadata_and_ops.py` — check the OpenAPI snapshot: new
  paths/schemas appear; update the snapshot if that file asserts one.

---

## 5. Risks / open questions (Task-1-scoped)

- **Q1 — Price snapshot vs live**: proposal ships live price (no snapshot
  column). If a later checkout task wants "price at time of add", it needs
  a new nullable column → an append-only `COLUMN_MIGRATIONS` entry then;
  deliberately not speculatively added now.
- **Q2 — Cart size caps**: `qty ≤ 10_000` per line and `PUT` ≤ 500 rows are
  pragmatic abuse guards; no per-cart row cap on incremental POSTs (a real
  shop cart won't approach it). Tighten if the coordinator wants a hard cap.
- **R1 — `updated_at` on upsert**: remember to bump it in both the UPDATE
  and the IntegrityError-retry path, or the legacy-import merge ordering
  drifts. Trivial, but exactly the kind of thing review should check.
- **R2 — Response size**: `CartOut` embeds full `ItemOut` per row; fine at
  shop scale (carts ≪ 500 rows). If that ever changes, paginate `GET` — not
  now (YAGNI, and the SPA renders the whole list today).
- **R3 — Deleting an item users hold in carts** silently drops lines. That
  matches the coordinator's cleanup decision and the catalogue's authority;
  noted because a future "item removed from your cart" UI hint would need a
  diff on GET — out of scope here.
- **R4 — PUT last-writer-wins** across two simultaneous kiosks on one
  account: accepted and documented; line-level POST/PATCH are the
  race-safe operations, PUT is for login-time flush/import only.

## 6. Sequencing within the task (single PR)

1. `app/models.py` (+`CartItem`), `app/schemas.py` (+5 schemas).
2. `app/routers/cart.py` + `app/main.py` registration.
3. Cleanup hooks in `items.py::delete_item` and `members.py::delete_member`.
4. `tests/test_cart.py`, extensions to `test_items.py` / member-deletion /
   `test_migrations.py`, OpenAPI snapshot check.
5. `uv run pytest` green; `README.md` endpoint table gets its `/api/cart`
   rows here (small, belongs with the feature) — bigger docs land in Task 6.
