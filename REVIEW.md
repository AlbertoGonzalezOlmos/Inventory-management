# Code Review — "HCRM Issue Fixes" Work Summary

Verification of the claims, with every finding reproduced against the actual code
(all demonstrations runnable; see "Evidence" notes).

Overall: the core privilege-escalation fixes (staff vs. admin, role grants,
password resets) are correct on the happy paths and well tested. However, one of
the two "critical" guarantees is broken by a race condition, one headline claim
("fail fast") is not actually implemented as described, the documented test
command does not work, and several smaller bugs remain.

---

## 🔴 High severity — a "critical fix" that is not actually fixed

### 1. Last-admin protection has a TOCTOU race — the system CAN end up admin-less

**Claim:** "The last remaining admin cannot be demoted or deleted (400), so the
system can never end up admin-less."

**Reality:** `_admin_count()` is read in a *different transaction state* than
the subsequent write. Two concurrent demotions both read `count == 2`, both pass
the check, both commit → **zero admins**. Same flaw applies to `delete_member`.

**Evidence (reproduced):** with two admins A and B, interleaving
`SELECT count(admins)` → 2 in both sessions, then committing both demotions:

```
countA 2 countB 2
A committed demotion
B committed demotion
admins remaining: 0
>>> ZERO ADMINS — protection broken
```

**Fix (pick one, ideally the first two combined):**
- Take a write lock *before* the count check so the read and the write are
  serialized: run the whole check-then-act inside `BEGIN IMMEDIATE`
  (e.g. `session.execute(text("BEGIN IMMEDIATE"))` after getting the user, or
  use `with engine.begin()` around the guarded operation).
- Add a database-level enforcement that cannot be raced, e.g. a SQLite trigger
  on `users` that raises whenever an UPDATE/DELETE would leave zero rows with
  `role='admin'`. Defense in depth — protects against future code paths too.
- Re-check the admin count immediately before `commit()` while holding the
  write lock.

A regression test should be added that exercises the concurrent case
(e.g. two threads + `BEGIN IMMEDIATE` semantics, or a unit test that opens two
sessions and reproduces the interleaving after the fix).

---

## 🟠 Misreported / broken claims

### 2. The documented test command fails

**Claim:** "25/25 pytest tests pass. Run with `uv run pytest`" (also in README).

**Reality:**
```
$ uv run pytest
ImportError while loading conftest '.../tests/conftest.py':
ModuleNotFoundError: No module named 'app'
```
Only `uv run python -m pytest` works (cwd on `sys.path`). The verification
statement could not have been produced with the documented command.

**Fix:** add to `pyproject.toml`:
```toml
[tool.pytest.ini_options]
pythonpath = ["."]
```
(and keep `uv run pytest` as the documented entry point).

### 3. "Startup probe validates the model's output dimension, fails fast with a clear message" — it does not fail fast

In `app/embeddings.py::_get_model()` the probe `RuntimeError` is swallowed by
the broad `except Exception`, sets `_model_failed = True`, logs a warning, and
startup continues exactly as before (graceful degradation). The only change is
a clearer log message. "Fail fast" is misreported.

Combined with issue #4 this is worse than before in one scenario: a
misconfigured `HCRM_EMBED_MODEL` lets the app start, and item edits then
silently strip embeddings.

**Fix:** either actually abort startup when the configured model's dimension
≠ `EMBEDDING_DIM` (raise out of `_startup()`), or change the claim and accept
degradation — but then guard #4 below.

### 4. PATCH item while the embedding model is unavailable silently destroys the stored embedding

**Reproduced:** with the model patched to unavailable,
`PATCH /api/items/{id} {"description": "new text"}` → `200 OK` and
`has_embedding: false`. A transient model failure permanently degrades search
quality for that item until the next startup backfill.

**Fix:** in `update_item`, if `embed_text_blob(...)` returns `None` while the
item previously had an embedding, either (a) keep the stale embedding (it is
still a better approximation than nothing) and log, or (b) reject the update
with 503, or (c) return a warning field so staff knows to re-embed later.
Same consideration for `create_item` (there returning 503 is safer than a
silently un-searchable item).

---

## 🟡 Functional bugs remaining

### 5. LIKE wildcard injection in keyword search

`list_items` interpolates `q` into `%{q}%` without escaping. `q=%` or
`q=_______` matches **every** item (reproduced: both return all 12 seed items).
Not a security hole, but broken search semantics and an easy DoS-ish
foot-gun for the `%`-heavy queries.

**Fix:** escape `%`, `_` and `\` in `q` and add `escape="\\"` to the `ilike`
calls (`Item.name.ilike(needle, escape="\\")`).

### 6. `#/admin` renders for plain members

`currentView` guards `#/table` with `&& this.isStaff` but not `#/admin`:

```js
if (this.route.startsWith("#/admin")) return "admin";      // no isStaff check
if (this.route.startsWith("#/table") && this.isStaff) return "table";
```

A member navigating to `#/admin` sees the full admin UI shell (forms render;
submits then 403). Inconsistent with the table guard.

**Fix:** `if (this.route.startsWith("#/admin") && this.isStaff) return "admin";`
(and fall through to catalogue otherwise, mirroring `#/table`).

### 7. Admin resetting their own password via PATCH kills their current session

`update_member` deletes **all** of the target's tokens — including the calling
admin's own token when they reset their own password. `/api/auth/change-password`
deliberately preserves the current session; the PATCH path does not. The UI
hides the button for self (`m.id !== user.id`), but the API allows it and the
admin is silently logged out on the next request.

**Fix:** skip the presented token when `user.id == staff.id`, matching the
change-password semantics.

### 8. One-character semantic query → raw 422

Backend enforces `q: min_length=2`; the frontend doesn't, so typing one
character with ✨ Semantic on yields a toast "Request failed (422)" (the 422
detail is a list, so the message extraction falls back to the generic string).

**Fix:** disable/ignore semantic search for < 2 chars in `loadMore()`, or map
422 validation errors to a friendly message in `api()`.

### 9. Smoke test hardcodes the default admin credentials

`scripts/smoke_test.py` logs in as `admin@shop.local` / `changeme`, while the
work summary (correctly) instructs the operator to change that password. After
following the instructions the smoke test can no longer run — the verification
story is self-defeating.

**Fix:** read `HCRM_ADMIN_EMAIL` / `HCRM_ADMIN_PASSWORD` env overrides in the
smoke test (it already supports `HCRM_BASE`, so this is consistent).

---

## 🔵 Acknowledged-but-open items worth keeping visible

- **Live DB still uses `changeme`** — flagged as "action needed", still open.
  A `must_change_password` flag on first login would close this class of issue
  instead of relying on the operator remembering.
- **Login timing side-channel** (no dummy hash when user not found) —
  documented, unfixed; the fix is two lines (verify against a dummy hash).
- **Tokens in `localStorage`** (XSS-exfiltratable) and **no login rate
  limiting** — not addressed and not mentioned in the summary.
- **Test-suite quality:** session-scoped shared state makes tests
  order-dependent (e.g. `test_last_admin_cannot_be_demoted` demotes and
  restores "extra" admins); the conftest monkey-patches module globals which is
  fragile if imports change to `from app.embeddings import embed_text` style.
- **CSV export has no UTF-8 BOM** — Excel may mangle non-ASCII names/prices.
- No git repo (deliberately left to the owner — fine, but everything above
  should land in version control before further changes).

---

## ✅ Verified as correctly fixed

For balance, these claims checked out:

- Privilege rules on all single-request paths (staff cannot modify admins /
  grant admin / reset passwords; admin can grant admin; last-admin 400 on the
  non-racy path) — verified by tests and by manual probing.
- Change-password flow incl. revocation of other sessions (tested).
- Vector search: query embedded once; category filter applied in SQL before
  LIMIT/OFFSET (pagination correct); similarity clamped.
- 401 stale-UI handling in `api()` with the login exemption.
- Logout revokes only the current session; expired tokens purged on issue.
- WAL + `busy_timeout=5000` on every connection; IntegrityError → 409 for
  duplicate email/SKU (incl. case-insensitive email).
- CSV formula-injection guard; `verify_password` tolerance for malformed
  hashes (tested); whitespace-only name and bad email → 422 (tested).
- Vendored assets are genuine and pinned (Vue 3.5.13 header verified);
  `run.sh` binds 127.0.0.1; startup seeding in a threadpool; `from exc`
  chaining; `healthz` endpoint.
- 25 tests do pass — via `python -m pytest` (see issue #2).

## Recommended priority

1. Fix the last-admin race (#1) — it invalidates a "critical" guarantee.
2. Make `uv run pytest` work (#2) — otherwise the safety net is unusable as
   documented.
3. Stop silently destroying embeddings on model failure (#4) + decide the real
   fail-fast behavior (#3).
4. Items #5–#9 as a follow-up batch.
