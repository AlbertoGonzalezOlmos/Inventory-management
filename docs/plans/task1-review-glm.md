# Task 1 review (glm) — of `task1-proposal-kimi.md` and `task1-proposal-qwen.md`

Reviewer context: my own proposal is `docs/plans/task1-proposal-glm.md`.
All three proposals were written independently against the same coordinator
brief; the data model has already converged almost completely, so this review
focuses on the deltas: endpoint shape, the concurrency idiom, orphan
handling, and scope boundaries.

---

## 1. Review of `task1-proposal-kimi.md`

### Strengths (adopt)

- **Crispest conventions argument of the three.** The 404-vs-422 split
  (unknown item → 404 mirrors `get_item`; bad qty → 422 from the schema),
  the DELETE-single-line-404 vs clear-all-204-idempotent distinction, and
  "no 409 on the happy path; the unique constraint is the law, the
  pre-check is a courtesy" are exactly the repo's doctrine, stated plainly.
- **`line_total_cents` on `CartRowOut`** — cheap, and it spares the SPA the
  per-line arithmetic it does today. Worth keeping in the merged shape.
- **PUT atomicity framed correctly for the switch flow**: "a 404/422
  mid-validation leaves the old cart untouched — the flush before an
  account switch must never destroy the outgoing user's saved cart." That
  is the *actual* requirement behind PUT, stated better than I did.
- **OpenAPI-snapshot check** (`test_metadata_and_ops.py`) in the test list —
  I had it in my phase-2 plan but dropped it from my Task 1 proposal; kimi
  kept it. Adopt.
- Clean scope: no `assign`, no scanner concerns; README endpoint-table row
  included in the same PR (right call — it belongs with the feature).

### Weaknesses / risks

- **The IntegrityError-retry add path is more code for the same guarantee.**
  SELECT → INSERT-or-UPDATE → on `IntegrityError`: rollback, re-SELECT,
  UPDATE, commit. It works (WAL makes the committed row visible after
  rollback; UPDATEs serialise), and it mirrors `register`/`create_item` —
  but those endpoints are check-then-insert *because they owe the client a
  friendly 400/409*; here the friendly path and the race path have no reason
  to differ, since increment-on-conflict is the intended semantics anyway.
  The single-statement upsert (mine/qwen) collapses three write paths into
  one. Kimi's own R1 ("remember to bump `updated_at` in the UPDATE *and*
  the retry path") is the smell: the upsert has exactly one place to forget.
- **No orphan tolerance.** Rows orphaned by non-API deletion (hand-written
  SQL — the exact D4 scenario `app/database.py` documents) or by the
  add/delete race sit forever; GET silently drops them via the join and
  nobody reaps. Qwen's lazy reaping (below) is better.
- `rows` as the collection key (vs `lines` in qwen/me) — cosmetic, but
  2-of-3 use `lines` and `CartOut.lines` reads better next to
  `CartLineOut`.
- `PUT` cap of 500 rows vs 200 elsewhere — cosmetic; pick one number.

### Disagreements with my proposal

| Point | Kimi | Me | Verdict |
|---|---|---|---|
| Add concurrency | ORM + IntegrityError retry | single-statement `ON CONFLICT` upsert | **Me** (with qwen) — one code path, no retry state, R1-class bugs impossible |
| POST path | `/api/cart/items` | `/api/cart/lines` | **Kimi** (with qwen) — 2-of-3, and `PATCH/DELETE /api/cart/items/{id}` reads naturally |
| `line_total_cents` | yes | no (only `exceeds_stock`) | **Kimi** — adopt; drop my `exceeds_stock` as redundant (UI compares `qty > item.stock` in one expression, `ItemOut` already carries stock) |
| Response | full `CartOut` from mutators | same | converged, no dispute |

---

## 2. Review of `task1-proposal-qwen.md`

### Strengths (adopt)

- **Lazy orphan reaping on read (§4.4)** — the best single idea in either
  peer proposal. Explicit cleanup on the API delete paths (all three
  proposals have it) still leaves two holes: hand-written SQL deletions
  (the documented D4 limitation) and the add/delete race in §3.1. Skipping
  *and deleting* unresolved rows in the `CartOut` assembler makes the
  limitation self-healing at bounded cost, and reusing one
  orphan-tolerant assembler across GET and all mutators is the right
  factoring. Test M (white-box: insert an orphan via raw session → GET
  omits it and the row is gone) is exactly how to pin it. Adopt verbatim.
- **The "why this is *not* stock-adjust" analysis (§3)** — correct and well
  argued, and it independently matches my own reasoning: `BEGIN IMMEDIATE`
  exists for *guarded* read-modify-write; an increment has no guard, the
  accumulation happens inside the write statement, so the plain ORM session
  is already race-free. This pre-empts a reviewer asking us to cargo-cult
  the raw-connection ritual. Keep this text in the implementation PR's
  docstring.
- **Scope discipline:** `assign` explicitly deferred to Task 3 with the
  note that the scan endpoint will call the router's add-path helper. I
  put `assign` in Task 1; qwen's boundary is cleaner (it depends on the
  attribution design, and Task 1 loses nothing by waiting). Concede.
- **`test_every_migration_the_app_currently_needs_is_listed`** — qwen is
  the only one who noticed this existing test already pins the
  "no spurious migration entry" invariant; my proposed negative assertion
  is redundant with it, and the right instruction is "leave that test
  unchanged" (verified: `tests/test_migrations.py:109`).
- Mixed concurrent adds-vs-deltas test concept, and the schema-shape test
  (N) asserting the unique index exists after `create_all()` on a legacy
  DB — the conflict target of the upsert is load-bearing and deserves a
  pin.
- Files-touched table incl. the deliberate non-changes (migration tuples,
  `app/database.py`) — good review checklist.

### Weaknesses / risks

- **The seventh endpoint (`POST /api/cart/lines` with `CartLineDeltaIn`)
  is surface without a consumer.** Two POST endpoints with subtly
  different semantics — `/items {qty ≥ 1}` always-increment vs `/lines
  {delta ≠ 0, may remove}` — is a footgun for both the SPA author and
  Task 3's scan path (which one does the scanner hit?). The UI's − button
  is a single-kiosk, last-writer-wins PATCH; nothing in Tasks 3–5 needs a
  race-free decrement (scans only increment). If a race-safe decrement
  ever matters, add the endpoint then — the two-statement UPDATE+DELETE
  idiom in §3.2 is correct and can be lifted wholesale. Drop it from
  Task 1. (This also dissolves its subtle interaction with a hypothetical
  `CHECK (qty > 0)`: qwen's own argument for omitting the CHECK only
  exists because the delta path transiently writes `qty ≤ 0`.)
- **`added_at` instead of `created_at`** — the repo's every timestamped
  table (`users`, `items`, `app_meta`) uses `created_at`; the deviation
  buys nothing. Rename.
- **Silent `min(qty, 100_000)` clamp inside the upsert SQL.** A stuck
  scanner loop would saturate a line at the cap with no signal beyond
  "the number stopped moving" — for an *abuse* bound, 422-per-request
  bounds (all three proposals have them) are enough; if a cumulative cap
  is wanted, make it loud (reject), not silent. Judgement call, but the
  repo's own doctrine ("report, never silently drop") points the other
  way from qwen's reading of it.
- **`sqlite_insert().on_conflict_do_update`** vs my `text()` upsert —
  both fine; the dialect construct is typed and keeps the ORM mapping, and
  qwen documents the SQLite lock-in (R2) in the docstring, which is the
  right mitigation. No objection; marginally prefer the dialect construct
  for composability, and note the repo currently uses `text()` everywhere
  (verified: no `on_conflict` usage exists yet) so either is a first.
- No `line_total_cents` (client computes) — kimi's field is worth having;
  trivial to add.
- Missing from the test list: `must_change_password` gating *is* covered
  (group A), OpenAPI snapshot check is mentioned under "suite hygiene" —
  both fine. Minor: no test that mutator responses equal a subsequent GET
  (the "one contract" claim) — cheap to add, worth it.

### Disagreements with my proposal

| Point | Qwen | Me | Verdict |
|---|---|---|---|
| Endpoint count | 7 (separate delta POST) | 6 | **Me** (with kimi) — drop the delta endpoint; no consumer in Tasks 3–5 |
| `assign` in Task 1 | deferred to Task 3 | included | **Qwen** — cleaner boundary; my helper-based design means zero rework either way |
| Upsert style | dialect `on_conflict_do_update` + SQL cap | `text()` upsert, no SQL cap | split: adopt the dialect construct, **reject** the silent SQL cap |
| Orphan reaping | yes, on read | no (relied on join dropping) | **Qwen** — adopt |
| Timestamp name | `added_at` | `created_at` | **Me** (with kimi) — repo convention |
| `exceeds_stock` flag | no | yes | drop (redundant with `item.stock` in `ItemOut`) — Qwen/kimi implicitly right |

---

## 3. Final recommendation — the shape the coordinator should standardise on

The three proposals agree on everything that matters structurally; the merged
shape is:

**Data model** (identical in all three, adopt as-is):
`cart_items` with `id` PK, `user_id` FK indexed, `item_id` FK indexed, plain
`qty` INTEGER (bounds at the schema layer, **no** `Field(ge=…)` on the table
model — SQLModel does not validate table models, and no existing repo model
uses Field bounds — and no SQL cap), `created_at`/`updated_at` via the
existing `utcnow()` convention, `UniqueConstraint("user_id", "item_id")`
declared from day one (it is the upsert's conflict target and cannot be added
to a live SQLite table later). **New table ⇒ no migration entries**; leave
`test_every_migration_the_app_currently_needs_is_listed` untouched.

**API** — kimi's six endpoints with qwen's internals:

```
GET    /api/cart                 → 200 CartOut          (orphan-tolerant, reaping)
POST   /api/cart/items           → 200 CartOut          (add-or-increment, single-statement upsert)
PATCH  /api/cart/items/{item_id} → 200 CartOut          (absolute set; 0 removes)
DELETE /api/cart/items/{item_id} → 204                  (absent → 404)
PUT    /api/cart                 → 200 CartOut          (atomic replace; flush/replay primitive)
DELETE /api/cart                 → 204                  (idempotent clear)
```

- All `get_current_user`, any role; 401/403-must-change-password inherited;
  user scoping in every WHERE (cross-user access → 404, never 403).
- `CartOut {lines: [CartLineOut], total_cents, total_qty}` with
  `CartLineOut {item: ItemOut, qty, line_total_cents}` — live prices, live
  stock, no snapshot, no `exceeds_stock` (UI computes `qty > item.stock`).
  Mutators all return the full `CartOut` (converged across all three).
- Error matrix: kimi's (404 unknown item / line-not-in-my-cart; 422 bounds,
  extra fields, duplicate ids or >200-line PUT; no 409 anywhere).
- Internals: qwen's single-statement upsert (dialect
  `on_conflict_do_update`, **without** the `min()` cap) + qwen's
  orphan-tolerant `_cart_out` assembler reused by every endpoint; PUT is
  validate-all-then-delete+insert in one transaction (kimi's framing).
- Explicit cleanup in `delete_item` / `delete_member` (all three agree).
- `assign` deferred to Task 3 (qwen's boundary); the add path exposed as a
  small helper so `/api/cart/scan` reuses the upsert (all three intend
  this; make it explicit).

**Tests** — union, deduplicated: kimi's cases 1–12 + qwen's M (orphan
white-box), N (schema shape / unique index on legacy DB), the mutator-GET
consistency check, the OpenAPI snapshot check, and the 8-thread same-line
concurrency regression (drop qwen's mixed adds-vs-deltas case with the delta
endpoint).

## 4. Changes I am making to my own proposal as a result

1. POST path `/api/cart/lines` → **`/api/cart/items`** (2-of-3, reads
   better with the sibling routes).
2. Drop `Field(ge=1)` on the table model's `qty` — it is a validation
   no-op for SQLModel table models and no repo model uses Field bounds;
   keep bounds in the schemas only.
3. Drop `exceeds_stock` from `CartLineOut`; add kimi's `line_total_cents`.
4. Adopt qwen's orphan-tolerant, reaping `CartOut` assembler across all
   endpoints (my proposal relied on the join silently dropping orphans).
5. Adopt the SQLAlchemy SQLite-dialect upsert over my `text()` version
   (typed, composable, documented lock-in note), but **without** the silent
   `min()` SQL cap — per-request pydantic bounds only.
6. Move `POST /api/cart/assign` out of Task 1 to Task 3 (qwen's scope
   boundary); keep the shared `add_lines` helper so it is zero rework.
7. Replace my redundant "no COLUMN_MIGRATIONS entry added" test with
   "leave `test_every_migration_the_app_currently_needs_is_listed`
   unchanged" (it already pins that invariant).
8. Keep (against both peers): six endpoints, no delta endpoint — the −
   button is a single-kiosk PATCH; add a race-safe delta later only if a
   real consumer appears.
