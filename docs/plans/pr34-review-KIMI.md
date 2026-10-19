# PR #34 Review — independent reviewer (KIMI)

**Branch:** `feature/ux-fixes-and-barcode` · **Base:** `main`
**Commits reviewed:** `03c5ccd` (UI + cart UX), `8e34461` (barcode_render tool),
`ddbe26e` (fix: address PR #34 review findings — landed on the branch *while this
review was in progress*; findings below mark which commit they apply to).

Method: `git diff main...HEAD`, line-by-line read of the touched files,
cross-check of the symbology tables against GS1/ISO 15420, a known-answer
encode check, and `pytest tests/test_barcode_render.py tests/test_frontend_contract.py
tests/test_items.py` (38 passed).

---

## 1. Requested verifications

### 1a. EAN-13 L/R/G + PARITY tables vs GS1/ISO 15420 — ✅ CORRECT
- `_L` (`scripts/barcode_render.py:44-55`) matches the spec's odd-parity left-hand
  table digit-for-digit.
- R = complement(L), G = reverse(R) are the spec's own identities; spot-checked
  G(0)=`0100111`, R(9)=`1110100` etc. against the spec — all match.
- `_PARITY` (`:71-82`) matches the first-digit parity table for all ten digits.
- Structure: EAN-13 = 95 modules with centre guard at bits[45:50]; EAN-8 = 67,
  guard at [31:36]; UPC-A = 95, guard at [45:50] — all correct.
- Known-answer: `ean13_bits("5901234123457")` reproduces the canonical 95-bit
  reference string exactly. Quiet zone of 11 modules both sides is ≥ the spec
  minimums (11/7 EAN-13, 7 EAN-8, 9 UPC-A), so scannability is fine.
- `check_digit` is reused from `app.barcodes` (`:230-242`), so the script cannot
  drift from API validation. The strengthened pins in `ddbe26e`
  (`tests/test_barcode_render.py`: literal R/G/PARITY tables + the
  4006381333931 known-answer string) close the tautology hole the original
  `test_r_and_g_are_derived_from_l` had (it tested `br._R` using the same
  `br._complement` the script derives with — a broken complement would have
  passed both).

### 1b. #/purchases loads on direct load AND navigation — ✅ CORRECT
- Navigation: `currentView` watcher (`static/app.js:393-396`) calls
  `loadMyOrders()` + `loadCalendar()`.
- Direct load / refresh: `mounted()` (`static/app.js:2457`) does the same when
  `this.user` restores from localStorage. Post-checkout redirect (`:1133-1136`)
  routes to `#/purchases` and pre-expands the receipt; the watcher supplies data.
- The profile sub-tab removal is clean: no remaining code sets
  `profileTab = "purchases"`, so the account view can never land on the deleted
  panel.

### 1c. Semantic search vs keyword pagination / explicit-limit callers — ✅ CORRECT
- Keyword branch (`static/app.js:729-738`) is untouched: `PAGE_SIZE` limit/offset,
  `hasMore = page.length === PAGE_SIZE` — pagination intact.
- `suggest_items` passes an explicit `limit` (`app/routers/items.py:273-274`);
  `openDetail` passes `&limit=5` (`static/app.js:752-756`). Both still get top-N.
- No-limit path correctly drops `LIMIT/OFFSET` from the SQL (`items.py:227-235`),
  and `ddbe26e` adds a 422 for `offset` without `limit` (`items.py:148-152`),
  closing the silent-ignore hole. Category filter still applied in SQL before
  ordering — correct.

### 1d. `_announceCartRestored` gating — ✅ CORRECT
- Called only from `afterLogin` (`app.js:467`) and `switchAccount` (`app.js:1771`).
  `loadCart` itself never announces; the page-restore path in `mounted()`
  (`app.js:2452`) calls `loadCart` without it. Routine qty re-syncs are silent.
- `ddbe26e` additionally suppresses the announce when the cart was just filled
  by the legacy import (`app.js:790-797`), fixing the toast clobber.

---

## 2. Findings already fixed by `ddbe26e` (were real bugs in `03c5ccd`/`8e34461`)

Recorded for completeness — no action needed:

1. **`flushCart` could abort logout/switch teardown** — a rejected cart op
   (network error in `addToPurchase` etc., which don't catch) rejects
   `_cartChain`; the original `await this._cartChain` then threw out of
   `logout()` before the token was cleared, stranding the user. Fixed with
   `.catch(() => {})` (`app.js:771-778`).
2. **`finishLogoff` clobbered the new "list saved" toast** with an unconditional
   "Logged off" milliseconds later. Fixed via `logout()` returning `hadItems`
   (`app.js:618`, `:1716-1720`).
3. **Legacy-import toast clobber** by `_announceCartRestored` (see 1d).
4. **`offset` without `limit` silently ignored** in `_vector_search_rows`. Fixed
   with the 422 guard.
5. **Tautological R/G/parity tests** in the original `8e34461` suite. Fixed with
   literal spec tables + known-answer bit string.

---

## 3. Outstanding findings (current HEAD)

### F1 — Semantic search: stale response can replace fresh state and stick (moderate)
`static/app.js:713-728` (`loadMore`). Semantic requests are slow (an embedding
forward pass + full scan). Sequence: type "mil" → debounce → request A in flight
(`loading=true`) → type "milk" → debounce → `reloadItems()` clears `items`, but
its `loadMore()` early-returns on the `loading` guard → request A resolves and
runs `this.items = page; this.hasMore = false`. The grid now shows results for
"**mil**" while the box says "milk", and `hasMore=false` means nothing
re-triggers a fetch until the next keystroke. The PR's new *replace* semantics
(`this.items = page` instead of `push`) make the stale response fully clobber
state, and the no-limit semantic endpoint makes the race window wide.
**Fix:** capture the term before the await and discard stale replies, e.g.
`const term = this.search.trim(); … const page = await api(…); if (term !== this.search.trim()) return;`
(plus re-arm `hasMore`/`reloadItems` on mismatch), or use a request-generation
counter.

### F2 — "Persistent" save-then-switch banner auto-dismisses after 6 s (minor, doc-vs-behavior)
`static/app.js:1779-1781`. The comment says "surfaced on the **persistent**
greeting banner … so the save is legible and click-dismissible", and the PR
description repeats "persistent", but `setTimeout(…, 6000)` clears it — it is a
6-second banner, not persistent; on a kiosk the reassurance is gone before the
outgoing user reads it.
**Fix:** drop the `setTimeout` (the banner is already click-dismissible via
`dismissGreeting`) or correct the comment/description.

### F3 — 12-digit auto-detect still renders a *different GTIN*, now with only a stderr warning (minor, residual)
`scripts/barcode_render.py:231-240`. A full 12-digit UPC-A (e.g. `036000291452`,
valid check digit) in `--symbology auto` is treated as an EAN-13 *body*; a new
check digit is appended and the tool renders `0360002914522` — a different,
scannable product code. `ddbe26e` downgraded this from silent to a stderr
warning, but the artifact produced is still wrong for the number the operator
typed; warnings on stderr are easy to miss when batch-rendering.
**Fix:** in the 12-digit auto case, if `check_digit(body[:-1]) == body[-1]`
prefer `upca` (the digits already form a valid code); otherwise require
`--symbology` explicitly instead of guessing.

### F4 — `--check` can never print its `INVALID` verdict without `--force` (minor)
`scripts/barcode_render.py:286-290` + `resolve()` at `:252-258`. For a full code
with a bad check digit, `resolve` raises before `main` reaches the
`('valid' if … else 'INVALID')` line, so the advertised "validate the check
digit" mode only ever prints `valid` — invalid input exits 1 with an error
instead. The INVALID branch is dead unless `--force` is also passed.
**Fix:** when `args.check`/`args.analyze` is set, catch the `ValueError` from
`resolve` and print the `check: N (INVALID, expected M)` report instead of
erroring out.

### F5 — Test-coverage gaps (minor)
- No server-side test pins the *new* no-limit behaviour: seed >12 items, call
  `/items/vector-search?q=…` with no `limit`, assert all are returned, ranked
  (`tests/test_items.py` only exercises explicit limits or small fixtures).
- The new `offset`-without-`limit` 422 (`items.py:148-152`) is untested.
- `resolve()` round-trips for EAN-8 and UPC-A (body→check→full) and the
  12-digit ambiguity branch are untested (`tests/test_barcode_render.py` covers
  EAN-13 only).
- Nothing pins the `#/purchases` wiring (nav link + `currentView === 'purchases'`
  section + watcher/mounted loaders) the way `test_frontend_contract.py` pins
  the scan bus; a cheap HTML/JS substring pin in that suite would do.
**Fix:** add the four tests above (each is a few lines in the existing suites).

### F6 — Scalability risk note (non-blocking)
With `limit` omitted, every debounced keystroke triggers one embedding pass plus
a full-table SIMD scan, then `select(Item).where(Item.id.in_(…))` with N bind
params (`items.py:170-175`) — on SQLite builds with the old 999-variable limit a
catalog >999 items would 500 — and the Vue grid renders the entire catalog at
once. Fine for the current shop size; worth a server-side hard cap (e.g.
`le=500` on `limit`, default unchanged) or a code comment documenting the bound.

---

## 4. Verdict

**REQUEST CHANGES** — for F1 (a reproducible stale-results bug introduced by the
new replace semantics) and F2 (the "persistent" claim is false as shipped); F3's
warn-only default and the F5 test gaps should land in the same respin. The
symbology core, the routing, the search rework, and the cart-announce gating all
verified correct, and `ddbe26e` already resolved the most severe issues found in
the original two commits — this is a small, well-scoped respin, not a redesign.
