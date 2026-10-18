# Task 1 review (qwen3.8-max) — peer proposals for the server-side per-user cart

Reviews `docs/plans/task1-proposal-glm.md` and `docs/plans/task1-proposal-kimi.md`
against `docs/plans/task1-proposal-qwen.md`, the coordinator's binding
decisions (`docs/plans/COORDINATOR.md` Task 1, decisions 6 + D4/D5), and the
repo's actual write semantics (`app/database.py`: WAL, `busy_timeout=5000`,
driver-default transaction mode — SELECTs run in autocommit, the first DML
statement opens the write transaction; PRAGMA `foreign_keys` OFF).

§4 gives the single recommended API + data-model shape; §5 lists the changes
I make to my own proposal as a result.

---

## 1. Review of `task1-proposal-glm.md`

### Strengths (adopt)

- **S1 — Single-statement `ON CONFLICT` upsert for add/increment, with an
  explicit rejection of the `BEGIN IMMEDIATE` ritual.** Correctly identifies
  that the check-then-insert window is where concurrent adds would race into
  a unique-constraint 500, and that stock-adjust's heavier machinery exists
  for a *guarded* read-modify-write, which this is not. This matches my §3.1
  reasoning exactly; the three-way agreement is solid ground for the
  coordinator.
- **S2 — `exceeds_stock` flag on the line output.** Encodes the coordinator's
  decision-6 warning rule (`qty > item.stock`) server-side in one place, so
  Task 5's UI is a dumb renderer and the rule cannot drift per client. Cheap,
  no behavioural coupling. Adopt.
- **S3 — `total_qty` on `CartOut`.** Task 5 needs a header badge count; a
  server-side sum is one less client computation. Adopt (naming: `total_qty`,
  symmetric with `total_cents`).
- **S4 — "Remove does not re-check item existence."** Deleting (or
  patching) a line whose item vanished from the catalogue concurrently must
  still succeed — the line belongs to the caller, and the catalogue is not
  the gate for removing it from *your own* cart. My proposal did not state
  this; adopt explicitly.
- **S5 — The shared `add_lines(session, user_id, [(item_id, qty), …])`
  helper**, factored precisely so Task 3's `/api/cart/scan` and Task 5's
  replay "cannot reinvent a racy version". Adopt (my proposal had the same
  intent via `_cart_out`/router reuse but glm names it and states the
  anti-drift rationale better).
- **S6 — Migration test via `PRAGMA index_list('cart_items')`** on a
  legacy-DB fixture, plus the *negative* assertion that `COLUMN_MIGRATIONS`
  gained no entry. The negative assertion is a good copy-paste guard. Adopt.
- **S7 — `item_id: int = Field(ge=1)`** in input schemas — rejects 0/negative
  ids with a clean 422 instead of a pointless 404 round-trip. My proposal
  missed this; adopt.

### Weaknesses / risks / bugs (avoid or fix)

- **W1 — Risk #6 is wrong: the add/delete race is real, and glm dismisses
  it.** The claim "cart rows for items deleted concurrently with a `GET` —
  impossible within one request" addresses the wrong operation. The actual
  window is in **add**: the existence check (`SELECT item`) runs in
  autocommit *before* the upsert; `delete_item` can commit (item + cart-row
  cleanup) between the two; the upsert then inserts a row for a
  now-nonexistent item. SQLite serialises the *writers*, but the stale
  pre-read is exactly the P6 bug class this repo has a regression culture
  about. Worse consequence, which none of the three proposals states: SQLite
  `INTEGER PRIMARY KEY` without `AUTOINCREMENT` **reuses rowids** (next id =
  max(existing)+1), so an orphaned `(user, item_id=42)` row can later
  "resurrect" as a phantom line for a *different* item that happens to take
  id 42 — invisible to any join-based cleanup because the row now joins
  fine. **Fix (see §4):** make the existence check part of the write
  statement — `INSERT … SELECT … FROM items WHERE items.id = :iid ON
  CONFLICT … DO UPDATE` — so the item's existence is evaluated at
  write-serialisation time; `rowcount == 0` ⇒ 404. This closes the window
  with *no* extra transaction machinery. Lazy reap on `GET` remains as
  defence-in-depth for non-API deletions (raw SQL), per D4's documented
  limitation class.
- **W2 — Unbounded accumulated qty.** glm's upsert is `SET qty = qty + :q`
  with only a *per-request* cap (10 000): a stuck scanner bridge (their own
  risk #2 scenario!) hammering add in a loop grows a line without bound. My
  `min(qty + excluded.qty, cap)` inside the statement costs one expression
  and keeps the abuse bound where the accumulation happens. Minor, but free
  to fix.
- **W3 — `POST /api/cart/assign` is scope creep with design debt.** The
  coordinator scoped Task 1 as "model + CRUD + isolation + cleanup +
  migration + concurrency"; assign is the D5 *attribution* path, and glm
  themselves offer to slip it to Task 3 "with zero rework". Its shape also
  needs more thought than Task 1 should absorb: one `qty` applied to a list
  of `item_ids` is an odd contract, staff-writes-into-another-user's-cart
  deserves its own audit/privacy review, and the staff→admin 403 rule
  (imported from `_get_manageable_member`) is, as glm admits, arguably
  over-strict for "put an item in the boss's cart". **Recommendation: exclude
  from Task 1**, land it in Task 3 next to `ScanRequest`/attribution where
  the semantics get reviewed together. The `add_lines` helper (S5) is what
  makes this a zero-rework deferral.
- **W4 — Endpoint naming `POST /api/cart/lines` for *add*** collides with my
  proposal's use of `/lines` for a delta operation and with the `CartItem`
  model name. Not substantive, but the merged plan must pick one (§4 picks
  `/items`, 2-of-3 convergence, matches the model).
- **W5 — Raw `text()` upsert string** where the SQLite-dialect
  `insert(...).on_conflict_do_update(...)` gives the same SQL with
  metadata-derived table/column names and a typed `excluded` reference.
  Taste-level; `text()` matches `items.py` style, so either is acceptable —
  but note the W1 fix requires the `INSERT … SELECT` form, which is most
  clearly written as explicit SQL (see §4).
- **W6 — Test #2 hand-wave** ("skip the conftest helper's auto-change if it
  performs one"): `create_user` *does* perform the mandatory change, so the
  must_change_password test must create the member via `POST /api/members`
  directly and use the pre-change token (my §5 group A states this
  concretely). Small, but a test plan that can't name its fixture path
  wastes review time.

### Disagreements with my proposal, and verdicts

| Topic | glm | mine (phase-3) | Verdict |
|---|---|---|---|
| Add concurrency | raw `text()` upsert, no cap in SQL | dialect upsert, `min(…cap)` in SQL | **Merge**: `INSERT…SELECT` form (W1 fix) with `min()` cap; SQL style either, prefer explicit `text()` for the SELECT-source upsert |
| Assign endpoint | in Task 1 | Task 3 | **glm's deferral clause wins** — exclude from Task 1 (W3) |
| `exceeds_stock` / `total_qty` | present | absent (UI derives) | **glm wins** — adopt both (S2/S3) |
| Delta endpoint | absent | `POST /cart/lines {delta}` | Discussed under kimi (§2, D3) — **concede: drop** |
| Line output timestamps | absent | `added_at`/`updated_at` | **Mine wins (renamed)**: keep `created_at`/`updated_at` in the output — Task 5's legacy-import merge needs a stable ordering key (kimi's R1 makes the same point); glm's own model has the columns, omitting them from the response is gratuitous |
| item_id validation | `ge=1` | unbounded int | **glm wins** (S7) |

## 2. Review of `task1-proposal-kimi.md`

### Strengths (adopt)

- **S1 — Cleanest statement of the unique-constraint doctrine**: "the
  pre-check is a courtesy, the constraint is the law" (same doctrine as
  `items.barcode`/`sku`). Correct framing for review.
- **S2 — Behavioural migration test**: prove the unique constraint by
  attempting a duplicate `(user_id, item_id)` insert → `IntegrityError`,
  *in addition* to schema inspection. Stronger than glm's PRAGMA-only check
  (it would catch a constraint that exists in name but not in effect).
  Adopt both styles.
- **S3 — 404 detail wording split**: "Item not found" (catalogue miss) vs
  "Item not in cart" (line miss). Trivial cost, real debugging value, and it
  makes the two 404 classes distinguishable in tests. Adopt.
- **S4 — `line_total_cents` per row.** The SPA renders per-line subtotals
  today; server-side computation keeps one arithmetic owner. Adopt.
- **S5 — Explicit `PUT` atomicity guarantee tied to the switch flow**: "a
  404/422 mid-validation leaves the old cart untouched — the flush before an
  account switch must never destroy the outgoing user's saved cart." That is
  exactly the right *reason* for the all-or-nothing rule, better argued than
  in my proposal. Adopt the framing and pin it with the test (their #9 =
  my G).
- **S6 — R1 (`updated_at` must be bumped in *every* write path)** — a real
  review checklist item for the legacy-import merge ordering; adopt into the
  implementation notes (their own retry design is where this bites hardest,
  see W1).
- **S7 — Sequencing within the single PR** (§6) plus "README endpoint-table
  rows land with the feature, big docs in Task 6" — matches the repo's
  docs-with-code culture. Adopt.

### Weaknesses / risks / bugs (avoid or fix)

- **W1 — BUG: the IntegrityError-retry concurrency design loses updates
  under three-way contention.** The proposed add path is: SELECT line;
  INSERT or UPDATE; on `IntegrityError` → rollback → **re-SELECT → compute
  `qty + delta` in Python → UPDATE** → commit. The retry path is itself a
  read-modify-write, and the re-SELECT runs in autocommit (per
  `app/database.py`'s documented transaction mode) with no write lock held
  across it. Interleaving: A inserts qty=1 (committed). B conflicts,
  re-selects qty=1, prepares UPDATE qty=2, commits. C *also* conflicted,
  re-selected qty=1 **before B committed**, and commits UPDATE qty=2 after
  B → final qty=2 where 3 increments happened. The unique constraint
  serialises *inserts*, not the retry's Python-side arithmetic. Their test
  #13 ("fails deterministically without it") would pass with 2 threads and
  flake with 3+. The claim that this "mirrors the repo's established
  IntegrityError-retry pattern" is misleading: `register`/`create_item`
  retry into a **409 report** (the conflict *is* the answer), never into a
  merge; there is no repo precedent for retry-into-update, because
  stock-adjust specifically avoided it (REVIEW-m10 P6) with SQL-side
  accumulation. **Fix: the single-statement upsert** (glm's S1 / my §3.1,
  refined per glm-W1 into `INSERT…SELECT … ON CONFLICT DO UPDATE SET qty =
  min(qty + excluded.qty, cap)`). One statement, no retry loop, no Python
  arithmetic, accumulation happens inside the serialised write.
- **W2 — Same add/delete orphan window as glm W1** (existence pre-check is
  a courtesy read before the write), same id-reuse phantom-line consequence,
  same fix (`INSERT…SELECT` source). Kimi at least acknowledges the join
  silently dropping orphan rows (§3 cleanup note) but, like glm, concludes
  no further hazard — the rowid-reuse resurrection defeats join-based
  invisibility.
- **W3 — `PUT` cap of 500 rows** vs 200 (mine/glm). Arbitrary either way;
  the legacy-localStorage import (its main consumer) is bounded by what one
  browser hoarded — 200 is ample and 2-of-3 convergence settles it.
  Non-issue, but standardise one number.
- **W4 — No `exceeds_stock`/warning support beyond embedding `ItemOut`.**
  Fine per se (the data is there), but glm's flag centralises decision 6's
  rule; kimi's shape pushes the comparison into every client. Prefer glm's.
- **W5 — Model `qty: int = Field(default=1)` without `ge=1`** ("validated in
  schemas"): acceptable, but glm's and my `ge=1` on the model field costs
  nothing and keeps direct-ORM writers (tests, scripts, future maintenance
  code) honest. Minor; adopt `ge=1`.
- **W6 — `CartReplaceIn.rows: list[CartAddIn]` reuses the add schema with
  `qty` defaulting to 1.** Convenient for imports, but it silently accepts
  `{item_id}` rows in a *replace* body where an explicit qty is the safer
  contract (a staging replay that lost its qty would silently re-add as 1).
  Borderline; I'd require qty in replace lines (my `CartReplaceLineIn`).

### Disagreements with my proposal, and verdicts

| Topic | kimi | mine (phase-3) | Verdict |
|---|---|---|---|
| Add concurrency | ORM retry loop | dialect upsert | **Mine (with glm)** — kimi's design is buggy under 3-way contention (W1); single-statement upsert wins |
| Delta endpoint | absent (PATCH-set only) | `POST /cart/lines {delta}` | **kimi wins — I concede.** No planned consumer needs it: scanners/`+` buttons add (POST upsert), steppers set (PATCH), flush/replay replaces (PUT). The race it protects against (two devices decrementing one cart concurrently) is the same last-writer-wins class the coordinator already accepted for PUT. Dropping it removes a schema, an endpoint, and three test groups; it remains a cheap additive later if multi-device decrement ever matters |
| Row naming (`rows` vs `lines`) | `rows` | `lines` | **Mine (+glm)** — 2-of-3, and `CartLineOut` already won on both peers' output naming |
| Output fields | `line_total_cents`, `added_at` | timestamps only | **Merge** — take kimi's `line_total_cents` + glm's `exceeds_stock`/`total_qty` + my timestamps |
| Max qty | 10 000 | 100 000 (stock-adjust parity) | **kimi/glm win** — 10 000 units in one cart line is already absurd; stock-adjust's 100 k bound fits stock events, not baskets. All inputs bounded at 10 000 |

## 3. Three-way convergence (coordinator fast-path — no debate needed)

All three proposals independently agree on: table `cart_items` with
`UniqueConstraint(user_id, item_id)` named `uq_cart_items_user_item` and
indexed FK columns; naive-UTC timestamp pair mirroring `Item`; **no price
snapshot — live prices**; **zero migration entries** (new table via
`create_all()`, with `test_every_migration_the_app_currently_needs_is_listed`
left untouched as the guard); no DB CHECK on qty; **no stock mutation or
clamp** (decision 6); any-role auth via `get_current_user` with the
`must_change_password` 403 gate inherited (cart deliberately *not* added to
`MUST_CHANGE_EXEMPT_PATHS`); mutators return the full cart with 200 (no 201,
no 409-on-duplicate — re-scan is an increment); `PATCH qty:0` removes the
line; DELETE-line 404-when-absent vs DELETE-cart idempotent-204; atomic
all-or-nothing `PUT` replace with documented last-writer-wins; explicit
cleanup in `delete_item` and `delete_member` inside the same transaction;
per-user isolation surfacing as 404 (never 403 — no existence leak); a
barrier-based N-thread concurrency regression test; per-line qty cap 10 000.

## 4. Final recommended shape (standardise on this)

**Model** (`app/models.py`) — kimi/glm's `CartItem` verbatim: `id` PK;
`user_id` FK indexed; `item_id` FK indexed; `qty: int = Field(default=1,
ge=1)`; `created_at`/`updated_at` (naive UTC, `sa_type=DateTime`);
`__table_args__ = (UniqueConstraint("user_id", "item_id",
name="uq_cart_items_user_item"),)`. No CHECK, no snapshot column, **no
migration entries**.

**Schemas** (`app/schemas.py`, `_Stripped`/`extra="forbid"`):

```python
MAX_CART_QTY = 10_000

class CartAddIn(_Stripped):
    item_id: int = Field(ge=1)
    qty: int = Field(default=1, ge=1, le=MAX_CART_QTY)

class CartQtyIn(_Stripped):                      # absolute set; 0 removes
    qty: int = Field(ge=0, le=MAX_CART_QTY)

class CartReplaceLineIn(_Stripped):              # qty required in replaces (kimi W6)
    item_id: int = Field(ge=1)
    qty: int = Field(ge=1, le=MAX_CART_QTY)

class CartReplaceIn(_Stripped):
    lines: list[CartReplaceLineIn] = Field(max_length=200)

class CartLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    item: ItemOut                # live price_cents AND live stock
    qty: int
    line_total_cents: int        # kimi S4
    exceeds_stock: bool          # glm S2 — decision-6 warning rule, server-side
    created_at: datetime
    updated_at: datetime

class CartOut(BaseModel):
    lines: list[CartLineOut]     # insertion order (id ASC)
    total_qty: int               # glm S3
    total_cents: int             # Σ live line totals
```

**Endpoints** (`app/routers/cart.py`, six — no delta endpoint, no assign):

| Method & path | Body | Success | Errors |
|---|---|---|---|
| `GET /api/cart` | — | 200 `CartOut` | 401, 403(gate) |
| `POST /api/cart/items` | `CartAddIn` | 200 `CartOut` | 404 "Item not found", 422 |
| `PATCH /api/cart/items/{item_id}` | `CartQtyIn` | 200 `CartOut` | 404 "Item not in cart", 422 |
| `DELETE /api/cart/items/{item_id}` | — | 204 | 404 |
| `PUT /api/cart` | `CartReplaceIn` | 200 `CartOut` | 404 (lists offending ids; old cart untouched), 422 (dupes/bounds/>200) |
| `DELETE /api/cart` | — | 204 idempotent | 401 |

**The one implementation detail the coordinator should mandate** (fixes both
peers' W1/W2 orphan window and kimi's lost-update bug in a single
statement) — add/increment as a race-free upsert whose *existence check is
inside the write*:

```sql
INSERT INTO cart_items (user_id, item_id, qty, created_at, updated_at)
SELECT :uid, :iid, :qty, :now, :now FROM items WHERE items.id = :iid
ON CONFLICT(user_id, item_id) DO UPDATE
  SET qty = min(cart_items.qty + excluded.qty, :max_qty),
      updated_at = :now
```

`result.rowcount == 0` ⇒ the item did not exist at write-serialisation time
⇒ 404. Properties: no lost updates under any N-way contention (accumulation
is SQL-side, one serialised writer); no check-then-write window against
`delete_item` (the `FROM items` source is evaluated under the write lock, so
the add/delete race cannot orphan a row); abuse cap enforced where
accumulation happens (glm W2); no `BEGIN IMMEDIATE` machinery, one pooled
connection (stock-adjust's ritual remains reserved for guarded stock
writes). PATCH/DELETE operate on the caller's row **without** re-checking
item existence (glm S4). `GET` skips + lazily reaps rows whose item has
vanished (defence-in-depth for raw-SQL deletes; D4 limitation class) — the
reap matters because rowid reuse can otherwise resurrect an orphan as a
phantom line for a future item.

**Cleanup**: `delete_item` deletes `cart_items WHERE item_id = :id` (all
users) in the same session transaction as the item delete; `delete_member`
deletes `WHERE user_id = :id` alongside the existing token loop. Shared
helper `add_lines(session, user_id, [(item_id, qty), …])` (glm S5) wraps the
upsert so Task 3's `/api/cart/scan` and Task 5's replay cannot reinvent a
racy version. `updated_at` is bumped in **every** write path (kimi S6/R1).

**Tests** — union of the three lists, deduplicated (all in new
`tests/test_cart.py` unless noted): auth 401s + `must_change_password` 403
(member created directly via `POST /api/members`, pre-change token — glm W6);
add/increment/no-409 + qty bounds + `item_id ≤ 0` 422 + extra-field 422;
PATCH set/0-removes/404-both-classes; DELETE line 204→404; clear idempotent;
PUT replace/empty-clears/unknown-id-404-**old-cart-unchanged**/dupes-422/
>200-422; live-price pin (staff price PATCH → totals move); stock untouched
+ `exceeds_stock` correctness; per-user isolation (404-not-403 on every
mutator); cleanup on item delete (two users' carts) and member delete
(direct DB row count, `AuthToken`-cleanup assertion style); **concurrency:
8-thread barrier on same (user,item) → exactly qty 8, one row, no 500s** —
plus a 3-thread variant (the case kimi's retry design would flake on);
orphan tolerance white-box (raw-insert a row with a nonexistent item_id →
GET omits and reaps it); schema-shape: legacy DB + `create_all()` →
`PRAGMA index_list` shows the unique index (glm S6) **and** a duplicate
`(user_id, item_id)` insert raises `IntegrityError` (kimi S2);
`COLUMN_MIGRATIONS` untouched (existing exact-set test); run
`test_metadata_and_ops.py` for the OpenAPI/route snapshot.

## 5. Changes to my own proposal (self-review)

1. **Drop `POST /api/cart/lines` (delta endpoint)** — kimi/glm convergence
   is right: no planned flow consumes it; PATCH-set + PUT-replace cover the
   UI and the flush; the concurrency case it served is already accepted as
   last-writer-wins elsewhere. Remains a cheap additive later.
2. **Adopt the `INSERT…SELECT … ON CONFLICT` upsert form** — this fixes a
   real (if narrow) orphan window in my own §3.1 pre-read design that the
   review of glm W1 surfaced; my lazy-reap was treating the symptom.
3. **Rename `added_at` → `created_at`** — repo convention (`User`, `Item`,
   `AppMeta`); I was the outlier.
4. **Adopt output conveniences**: `line_total_cents` (kimi), `exceeds_stock`
   + `total_qty` (glm).
5. **Adopt `item_id: Field(ge=1)`** (glm) and **qty cap 10 000** (both
   peers; my 100 k stock-adjust parity was the wrong analogue for a basket).
6. **Adopt the 404 detail split** ("Item not found" vs "Item not in cart",
   kimi) and **"line removal never re-checks the catalogue"** (glm S4).
7. **Adopt kimi's behavioural unique-constraint test** alongside my
   schema-shape test; adopt the 3-thread concurrency variant implied by W1.
8. **Confirm exclusions**: no `assign` in Task 1 (glm's own deferral clause;
   lands in Task 3 with attribution semantics); PUT cap stays 200.

## 6. Verdict

All three proposals converge on the same foundation (model, live prices,
zero migrations, no stock interaction, explicit cleanup, CartOut-returning
mutators), so the coordinator's risk is low whichever base is chosen.
**glm's proposal is the best base document** (correct upsert, best
behavioural nuances S2/S4/S5, honest risk list) **with two mandatory fixes**:
its add/delete race dismissal (W1 → the `INSERT…SELECT` upsert) and dropping
`assign` to Task 3 (W3). **kimi's proposal has the best test ideas**
(behavioural constraint proof, 404-detail split, PUT-atomicity framing) but
**its IntegrityError-retry concurrency design must not ship** — it loses
updates under 3-way contention, which is the exact bug class the concurrency
test exists to catch. My proposal contributes the upsert cap, orphan
lazy-reap + rowid-reuse analysis, and the delta endpoint — the first two
fold into the merged shape, the last I withdraw. §4 is the merged,
self-consistent specification I recommend the coordinator standardise on.
