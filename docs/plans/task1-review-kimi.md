# Task 1 proposal review (kimi-k3)

Reviews of `task1-proposal-glm.md` and `task1-proposal-qwen.md` against my
own `task1-proposal-kimi.md`, plus a final recommended shape for the
coordinator to standardise on.

All three proposals agree on the fundamentals (new `cart_items` table with a
unique `(user_id, item_id)`, no migration entries, per-user CRUD behind
`get_current_user`, live prices, zero stock interaction, explicit delete
cleanup, full-cart `PUT` for flush/replay, a threaded lost-update test).
The differences are in the concurrency mechanism, endpoint inventory, and
a handful of details that matter.

---

## 1. Review: glm

### Strengths (adopt)
- **Single-statement upsert for add/increment.** `INSERT … ON CONFLICT DO
  UPDATE` executed once, with the accumulation inside the write statement —
  race-free by construction under SQLite's single-writer serialisation, no
  retry loop. This is strictly better than my SELECT → INSERT/UPDATE →
  IntegrityError-rollback-retry dance: fewer statements, no exception path
  on the hot path, and the concurrent-increment test passes by construction
  rather than by recovery. (Qwen independently chose the same mechanism; §3.)
- **Explicit reasoning about why *not* the `BEGIN IMMEDIATE` raw-connection
  ritual** from `stock-adjust` (no guarded read-modify-write here). Good
  review guidance — the two idioms must not be conflated.
- **The migration-guard test idea**: asserting `COLUMN_MIGRATIONS` gained
  no cart entry protects the "new table ⇒ no migration" invariant against
  future copy-paste.
- **422 on duplicate `item_id` in a `PUT` payload** — my proposal validated
  item existence but missed duplicate-id rejection; without it a duplicated
  line would hit the unique constraint mid-transaction and 500 (or
  silently win twice). Genuine catch.
- Shared `add_lines()` helper factored so Task 3's `/api/cart/scan` cannot
  reinvent a racy add — matches my "one upsert path" intent, stated more
  concretely.

### Weaknesses / risks
- **`POST /api/cart/assign` is scope creep.** The coordinator's Task 1
  boundary is cart CRUD + cleanup + migration + concurrency; attribution
  (decision D5) lives in the Task 3/5 neighbourhood. GLM half-concedes this
  ("can slip to Task 3 with zero rework") — it should simply not be here.
  Including it also drags a members-router privilege question (staff→admin
  403) into a PR that should be mechanically reviewable.
- **Raw `text()` SQL for the upsert.** Correct, but it hand-writes column
  lists and timestamps as strings; a future model change silently desyncs
  it. Qwen's `sqlalchemy.dialects.sqlite.insert` form is typed against the
  model and equally single-statement — prefer it (§3).
- **`exceeds_stock` convenience flag** duplicates data already in
  `CartLineOut.item.stock`. Harmless but it is a second source of truth for
  the UI to disagree with; drop it and let the SPA compute `qty >
  item.stock` (one comparison, Task 5).
- **`/lines` path naming** (`POST /api/cart/lines` while the collection is
  `/api/cart`) — reads oddly; `/api/cart/items` is more consistent with the
  domain language and with my/qwen's add path. Minor.
- Migration test placed in `tests/test_migrations.py` — fine, but see §3:
  qwen's point that the exact-set pinning test must stay *unchanged* is the
  sharper formulation.

### Disagreements vs. my proposal
- **Upsert mechanism**: GLM (single-statement) wins over mine (retry).
  Adopted.
- **`assign`**: I excluded it; GLM includes it. Mine is right for the Task 1
  boundary; the coordinator should route it to Task 3 or 5.
- Otherwise cosmetic (`lines` vs `items`, `exceeds_stock`, `total_qty` vs my
  `item_count` — all taste; recommendation in §3).

---

## 2. Review: qwen

### Strengths (adopt)
- **Typed dialect upsert**: `sqlite_insert(CartItem).on_conflict_do_update(
  index_elements=["user_id","item_id"], set_={qty: qty + excluded.qty, …})`
  — single-statement like GLM's but bound to the model, with the conflict
  target named explicitly. Best formulation of the three.
- **No DB `CHECK` on `qty`, with the reason spelled out** (bounds live in
  pydantic/SQL, mirroring `items.stock`'s clamp-in-code precedent). Good
  doctrine-consistency note.
- **Orphan tolerance on read**: `GET` (and the shared `CartOut` assembler)
  skips lines whose item vanished and best-effort reaps them in the same
  request. Explicit cleanup covers the API delete paths; this covers
  everything else (hand-written SQL, the add-vs-item-delete race) and keeps
  repo decision D4 honest with a self-healing read instead of a silent
  inner-join drop. Cheap, white-box-testable. I missed this entirely —
  adopted.
- **`tests/test_migrations.py` exact-set test stays untouched.** Qwen is
  right that the repo already pins the migration set
  (`test_every_migration_the_app_currently_needs_is_listed`), so the
  schema-shape assertion (table + `uq_cart_items_user_item` exist after
  `create_all()`) belongs in `test_cart.py` — sharper than both my and
  GLM's "extend test_migrations.py".
- **404-not-403 for cross-user access** (no existence leak about other
  users' carts) — stated explicitly; my proposal had the behaviour but not
  the rationale.
- **PATCH does not create** ("adding is POST's job — no create-on-PATCH
  surprise") — same as mine, better justified.
- **The "why not stock-adjust machinery" analysis**, independently of GLM.
  Two plans arriving at it makes the review point robust.
- **Qty cap mirrored on `StockAdjustIn`'s 100 000 range** and the duplicate-
  id-422 in PUT (same catch as GLM).

### Weaknesses / risks
- **The delta endpoint (`POST /api/cart/lines {delta}`) is redundant
  surface.** Its only justification is a race-free decrement across two
  kiosks on one account — a case all three proposals already document as
  last-writer-wins and out of scope. Within one kiosk, the UI stepper's
  absolute PATCH is fine (the client just rendered the qty it is setting);
  the scanner's increment rides the upsert. Seven endpoints + a
  `CartLineDeltaIn` schema + a test group to buy insurance for a scenario
  we explicitly don't support is YAGNI. Recommend dropping it (one-line
  re-add later if Task 5 proves the need).
- **Silent SQL-side `min(qty, 100_000)` cap** — "the report *is* the cart
  we return" is a stretch; a qty frozen at the cap looks like a bug, not a
  report. But the whole scenario requires 100 000 increments — the cap is
  abuse theatre either way. Prefer the simpler per-request 422 bounds
  (mine/GLM) and no accumulation cap; flagged as a coordinator taste call.
- **`added_at` naming** diverges from the repo's `created_at` convention
  (`User`, `Item`). Descriptive, but consistency wins in a model file that
  reviewers pattern-match; recommend `created_at`.
- Minor: `CartLineOut` lacks a per-line total (mine and GLM's have
  `line_total_cents`); the SPA's existing panel shows line totals, so the
  server computing them keeps Task 5 dumber. Keep `line_total_cents`.

### Disagreements vs. my proposal
- **Upsert mechanism**: qwen's typed single-statement wins over my retry
  (same verdict as GLM). Adopted.
- **Delta endpoint**: qwen adds it, I omit it. Mine stands — see above.
- **Orphan reaping**: qwen adds it, I lacked it. Qwen wins — adopted.
- **Qty accumulation cap**: qwen caps in SQL, I bound per request only.
  Mine (simpler) recommended, low stakes.
- **`added_at` vs `created_at`**: mine (repo-consistent) recommended.

---

## 3. Final recommendation — the shape to standardise on

Take **qwen's proposal as the base** (it is the most complete and its
reasoning is the most explicit), with GLM's duplicate-id catch (which qwen
also has) and the following subtractions/adjustments:

### Data model (no disagreement worth having)
```python
class CartItem(SQLModel, table=True):
    __tablename__ = "cart_items"
    __table_args__ = (
        UniqueConstraint("user_id", "item_id", name="uq_cart_items_user_item"),
    )
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    item_id: int = Field(foreign_key="items.id", index=True)
    qty: int = Field(default=1)            # no CHECK; bounds in schemas/SQL
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime)
```
New table ⇒ `create_all()` covers every database; **`COLUMN_MIGRATIONS` /
`INDEX_MIGRATIONS` untouched** (and the exact-set pinning test proves it).

### API surface — six endpoints
| Method & path | Body | Success | Errors |
|---|---|---|---|
| `GET /api/cart` | — | 200 `CartOut` (orphan-tolerant: skips + reaps dead lines) | 401, 403 |
| `POST /api/cart/items` | `{item_id, qty=1}` | 200 `CartOut` — **single-statement `sqlite_insert … on_conflict_do_update` upsert** | 401, 403, 404 unknown item, 422 |
| `PATCH /api/cart/items/{item_id}` | `{qty}` (absolute, required) | 200 `CartOut`; qty 0 removes; never creates | 401, 403, 404 line not in my cart, 422 |
| `DELETE /api/cart/items/{item_id}` | — | 204 | 401, 403, 404 |
| `PUT /api/cart` | `{lines: [{item_id, qty}], ≤200}` | 200 `CartOut`; all-or-nothing | 401, 403, 404 any unknown id (cart untouched), 422 duplicate ids |
| `DELETE /api/cart` | — | 204 idempotent | 401, 403 |

- All mutators return the full `CartOut` (unanimous across all three).
- `CartOut {lines, total_cents}`; `CartLineOut {item: ItemOut, qty,
  line_total_cents, created_at, updated_at}`. **Drop** `exceeds_stock`
  (GLM) and `total_qty`/`item_count` (trivially client-computed); **keep**
  `line_total_cents`.
- **No delta endpoint, no assign endpoint in Task 1.**
- Per-request qty bound 422 (`1 ≤ qty ≤ 10_000`; PUT lines ≤ 200); **no**
  SQL-side accumulation cap.
- Cross-user access → 404 (no existence leak); PATCH never creates; live
  prices; zero stock interaction.
- Cleanup: `delete_item` and `delete_member` delete cart rows in the same
  transaction (existing AuthToken-cleanup style), **plus** qwen's
  orphan-reap in the `CartOut` assembler.
- One shared `_upsert_line(session, user_id, item_id, qty)` helper in the
  router for Task 3's scan endpoint to reuse (GLM's `add_lines` idea).

### Tests
Qwen's groups A–N **minus group D (delta)**, i.e.: auth/gating, add/
increment (no 409), PATCH set/remove/no-create, line delete 204→404,
idempotent clear, PUT replace/atomicity/dup-422, live-price pin,
stock-untouched pin, per-user isolation (404-not-403), item/user delete
cleanup (both users' carts), threaded barrier lost-update regression
(exactly N, one row), orphan white-box, schema-shape (table + unique index
present after `create_all()`; `test_migrations.py`'s exact-set test left
**unchanged**), OpenAPI snapshot check.

## 4. Changes I would make to my own proposal

1. **Replace the IntegrityError-retry concurrency path** with the
   single-statement `sqlite_insert … on_conflict_do_update` upsert (qwen's
   typed form, not GLM's `text()`). My retry works, but it is more code on
   the exception path for a guarantee the single statement gives for free.
2. **Add orphan tolerance** (skip + best-effort reap dead lines in the
   `CartOut` assembler) — adopted from qwen; my explicit-cleanup-only story
   silently dropped lines on any non-API deletion.
3. **Add duplicate-`item_id` 422 to PUT validation** — adopted from GLM/
   qwen; my validation missed it.
4. **Move the schema-shape assertion into `test_cart.py`** and leave
   `tests/test_migrations.py`'s exact-set pinning test unchanged (qwen's
   sharper reading of the repo's own guard).
5. Keep from my original proposal, against the peers: no `assign` in
   Task 1 (GLM), no delta endpoint (qwen), `created_at` naming (qwen's
   `added_at`), per-request 422 bounds without a SQL accumulation cap
   (qwen), `/api/cart/items` path naming (GLM's `/lines`), and
   `line_total_cents` in the row schema (qwen omitted it).
