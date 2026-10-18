# PR #34 round-2 review — KIMI

**Scope:** `git diff ddbe26e..925e2ac` (respin "fix: address PR #34 round-2 review findings"), with `main...HEAD` as context.
**Method:** line-by-line read of the diff and touched files; `uv run pytest` full suite (709 passed, exit 0); the barcode script exercised end-to-end in `/tmp/brtest` (8 CLI invocations, exit codes printed); the SPA state machine exercised via a Node harness (`/tmp/brtest/harness.js`) that `eval`s the **actual** `reloadItems`/`loadMore` source out of `static/app.js` against a scripted mock `api()` — 7 interleavings (S1–S7) with logged fetch order and settled state; honesty probes that delete the pinned lines and re-run the new tests' regexes. Nothing touched `./data`.

**Verdict: REQUEST CHANGES** — one HIGH (N2: the session-expiry cart reset was applied to only one of the two 401 teardown paths, leaving the exact cross-user leak the respin claims to fix), plus one MEDIUM (N1: an error on a superseded search request silently abandons the fresh reload). Both are small, targeted fixes in the respin's own new code.

---

## §1 Round-1 findings (F1–F6) at `925e2ac`

**F1 (stale semantic response pins the grid) — FIXED** for the success paths, with one residual hole now filed as N1 below.
Executed (harness, real source): category change mid-semantic-fetch (S1), double reload gen-chase 1→2→3 (S4), semantic→keyword toggle mid-flight (S5 — refetch correctly hits `/items?limit=12&offset=0&q=…`), and sentinel `loadMore()` during the in-flight refetch (S6 — early-returns on `loading`, no duplicate fetch, fresh page commits, `hasMore` correct). In every case the superseded response is dropped (`gen !== this._itemsGen → continue`, `app.js:747`) and the fresh state is fetched with the *current* term/category/branch and a reset offset. No spin (every `continue` follows a completed `await`), no deadlock of `loading` (the `for(;;)` always exits via `break` or throw; `finally` releases the flag).

**F2 ("persistent" banner auto-dismissed at 6 s) — FIXED.** `app.js:1795-1804`: the `setTimeout` is gone; the retained `clearTimeout(this._greetingTimer)` correctly kills the 4 s "Hi" timer that `saveSession` (`app.js:549`) started at switch step 4 — the banner is now genuinely persistent and click-dismissible (`index.html:36-38`). Verified by grep: line 549 is the only `_greetingTimer = setTimeout` in the file.

**F3 (12-digit auto-detect rendered a different GTIN) — FIXED**, exactly as suggested. `barcode_render.py:262-274`: a 12-digit input whose UPC-A check digit validates now renders as UPC-A. Executed: `036000291452 --check` → `symbology: upca … check: 2 (valid)`, stderr hint to pass `--symbology ean13` for the other reading; `841234567890` (bad UPC-A check) still resolves to EAN-13 body `8412345678905`; `--symbology ean13` on 12 digits still appends.

**F4 (`--check` could never print INVALID) — FIXED.** `CheckDigitError` (`barcode_render.py:227-243`) is raised by `resolve` and caught in `main` (`:338-349`): `--check` prints the structured report (`check: 6 (INVALID)`) on stdout and exits 1; without `--check/--analyze` it stays the one-line stderr error. Executed both, rc=1 each. The new `--format` default `None` semantics also verified: plain `--check` is print-only (notice on stderr, no file); `--check --format png` writes the file.

**F5 (four test gaps) — FIXED**, and honestly (see §3): rank-all no-limit with independent item count + non-increasing similarity (`test_items.py:test_vector_search_no_limit_ranks_every_item`, asserts `total >= 12` so the fixture is load-bearing), the 422 for `offset` without `limit`, the EAN-8/UPC-A round-trips and both 12-digit branches (`test_barcode_render.py`), and the `#/purchases` wiring + similar-items `limit=5` + session-expiry cart + generation-guard pins (`test_frontend_contract.py`), plus a suggest-slice test. All executed (part of the 709).

**F6 (unbounded `limit`) — FIXED** with the suggested shape: `le=500` on `limit`, default unchanged (`items.py:136`); executed (501→422, 500→200). Residual INFO: the *no-limit* default path still feeds every row id into `Item.id.in_(…)` (`items.py:170-175`) — fine on the bundled SQLite 3.45 (32766-var limit; verified `sqlite3.sqlite_version` = 3.45.1), only a concern on ancient ≤999-var builds. Acceptable for kiosk scale.

## §2 New findings in the respin

**N2 — HIGH: `handleSessionExpired()` does not reset the cart — the cross-user leak the respin fixes is still open on the raw-mode 401 path.** `static/app.js:1560-1572`. The new reset (`this.cart = {…}; this._cartImportedLegacy = false`, `app.js:2466-2470`) was added only to the `hcrm-session-expired` *event* handler (the `api()` throwing path). But raw-mode callers own their 401s and call `handleSessionExpired()` directly — at least six live sites (`app.js:964` badge view, `:1154` checkout purchase, `:1713` logoff-schedule, `:1746` logoff-cancel, `:1836` staged-scan replay). That path clears user/token/overlays/route but **not the cart**. The respin's own comment states the consequence: the next user can log in flagged, `afterLogin` returns before `loadCart()` (`app.js:458-464`), so the previous user's list displays — and once that user changes their password (unlocking cart endpoints; `changePassword` never reloads the cart), it is *mutable and purchasable* under their token. Realistic trigger: password change elsewhere ("other sessions were signed out", `app.js:644`) revokes the kiosk token mid-checkout → raw 401 → next badge scan is a flagged account.
**Fix:** mirror the two reset lines (or dispatch the same event) inside `handleSessionExpired()`.

**N1 — MEDIUM: an error on a superseded request silently abandons the fresh reload — blank catalogue, no toast, sentinel can stay dead.** `static/app.js:759-763`. Executed (harness S2): fetch#1 (`q=mil`, gen 1) in flight → user types "milk" → `reloadItems` (gen 2, items cleared, `hasMore=true`) → its `loadMore` early-returns on `loading` → fetch#1 **rejects** → catch sees `gen(1) !== _itemsGen(2)` and swallows → loop exits via the exception, `loading=false`. Settled state: `items=[], hasMore=true, loading=false`, zero fetches in flight, no toast. The design's recovery premise is that the in-flight loop refetches (true when the superseded fetch *succeeds*, S1/S4/S5) — but the error path never re-enters the loop. The IntersectionObserver only fires on intersection *change*: with a short grid (sentinel already visible — the small-catalogue kiosk case) it never re-fires, so nothing re-triggers until a view switch, keystroke, or scroll-away-and-back. Worse than pre-respin for this sub-case, where the error at least toasted. (Same hole if the *fresh* iteration errors while yet another gen bump lands between reject and catch.)
**Fix:** move the try/catch inside the `for(;;)` and `continue` on superseded errors, or have `reloadItems` re-drive `loadMore` once the in-flight one settles.

**N3 — INFO (pre-existing, adjacent):** the flagged-account switch path (`app.js:1782-1791`) early-returns before the persistent save-reassurance banner is set — the outgoing user's "your list was saved" message never appears on that branch (only a toast naming no save). Not introduced here; noting for completeness.

## §3 Test-honesty audit of tests added in `925e2ac`

- **`test_purchases_route_wiring_is_pinned` — one defeatable pin.** Executed probe: deleting the watcher's `loadMyOrders();` call still passes — the regex `currentView\(view\) \{[\s\S]*?loadMyOrders\(\)` lazily spans ~24 KB past the watcher and matches the `async loadMyOrders()` *definition* (`app.js:1014`). The `mounted()` pin is honest (deleting its call → no later occurrence → fails), as are the nav/section substring pins. **Fix:** bound the lazy match (e.g. `[\s\S]{0,400}?` or match to the watcher's closing context).
- **`test_session_expired_clears_the_cart_view` — honest** (the extracted handler body contains no `});` before its end, so the regex captures the full body; deleting the reset line fails it). Note it pins only the event handler — consistent with N2 being missed.
- **`test_similar_items_fetch_pins_explicit_limit`, `test_stale_search_responses_are_generation_guarded`** — honest source pins; they cannot catch N1, but they don't claim to.
- **Backend tests (`test_items.py`, `test_items_suggest.py`) — honest:** real endpoints, independent expected counts (`/items?limit=100` total; 8 created > 5 slice), rankedness, both 422 boundaries. No tautology found.
- **`test_barcode_render.py` additions — honest:** executed via suite and CLI; `--check` INVALID report, `--force` non-bypass of the ASCII gate, format/output file semantics, and the SVG/PNG geometry agreement (`height+24` both; PNG rows 0–109 black, 110 white) all assert observable output, not implementation echoes.
- **`test_metadata_and_ops.py` `docs/plans` exemption — tightly scoped:** exact `dirname(path) in ("docs/plans",)` — `docs/planning/…` or a subdir would not be exempt. Re-ran the tripwire test after writing this review (which itself names `feature/ux-fixes-and-barcode`): passes only via the exemption, confirming it is both needed and sufficient.

## §4 Verdict

**REQUEST CHANGES**, blocking:
1. **N2 (HIGH)** — add the cart/`_cartImportedLegacy` reset to `handleSessionExpired()` (`app.js:1560`); the cross-user leak the respin's own comment describes is still reachable through the raw-401 path.
2. **N1 (MEDIUM)** — re-drive the fetch when a superseded request *errors* (`app.js:759-763`); as shipped, that interleaving leaves a silent blank catalogue.

Non-blocking but cheap to land with them: the watcher-regex bound in `test_purchases_route_wiring_is_pinned` (§3). Everything else in the respin — the generation loop's success interleavings, the persistent banner, the UPC-A disambiguation, `--check` INVALID, the `--format` semantics, the SVG/PNG geometry, `le=500`, and the 422 — verified correct by execution.
