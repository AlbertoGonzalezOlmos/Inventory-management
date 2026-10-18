# PR #34 review — UX fixes + barcode_render (feature/ux-fixes-and-barcode)

Reviewer: GLM (independent review pass)
Scope reviewed: `git diff main...HEAD` — commits `03c5ccd` (UI + cart UX) and `8e34461` (barcode_render).
Method: full read of both commits' diffs, the touched files in context (`app/routers/items.py`, `static/app.js`, `static/index.html`, `static/scancapture.js`, `scripts/barcode_render.py`, both test files), programmatic verification of every barcode table against the GS1/ISO 15420 spec, and a full test-suite run.

**Verdict: REQUEST CHANGES** — one new correctness regression (F1), plus medium/low findings F2–F6. Several of these are already fixed by *uncommitted* working-tree edits that must land in this PR (see "Concurrent working-tree edits" at the end).

---

## The four specifically-requested verifications

### 1. EAN-13 L/R/G and first-digit parity tables — ✅ CORRECT

Verified programmatically against the GS1 General Specifications / ISO 15420 reference tables (all 10 digits, all four tables):

- `_L` (scripts/barcode_render.py:45–56): exact match with the spec's left-hand odd-parity table.
- `_R` = complement(L) (line 65): the derived values match the spec's R table digit-for-digit (e.g. R(0)=1110010, R(9)=1110100 …).
- `_G` = reverse(R) (line 67): matches the spec's G table digit-for-digit (e.g. G(0)=0100111, G(4)=0011101 …).
- `_PARITY` (lines 73–84): matches the standard first-digit → L/G pattern table for all ten digits (0=LLLLLL … 9=LGGLGL).
- Cross-symbology consistency: `ean13_bits("0" + upc_a_12)` is bit-identical to `upca_bits(upc_a_12)` — a zero-prefixed EAN-13 renders exactly as the UPC-A, as the spec requires.
- Structure: guards `101` / centre `01010` / `101` correct; 95 modules (EAN-13/UPC-A) and 67 (EAN-8) correct; quiet zone 11 modules ≥ the 9 required.

No unscannable-table defect. **However** the committed test suite does not pin the parity values — see F5.

### 2. `#/purchases` loads on direct load AND navigation — ✅ CORRECT

- Navigation: `currentView` watcher, static/app.js:393–396 → `loadMyOrders()` + `loadCalendar()`.
- Direct load (`#/purchases` with a stored token): the mounted hook, static/app.js:2449, calls both.
- The watcher is not `immediate`, so there is no double fetch on direct load. Checkout lands on `#/purchases` (app.js:1130–1133) and the watcher refreshes the list, so the new purchase (pre-expanded via `expandedOrders`) appears immediately.
- `loadMyOrders` (app.js:982–992) clears `myOrders` for flagged/absent users, so the previous user's history never flashes. Good.

### 3. Semantic-search change vs keyword pagination and explicit-limit callers — ✅ CORRECT (with F4/F6 caveats)

- Keyword path untouched: `list_items` still `limit=24, le=100` (app/routers/items.py:127–129); the frontend keyword branch still paginates with `limit=PAGE_SIZE&offset=` (static/app.js:729–737, HEAD).
- `suggest_items` passes its limit explicitly to `_vector_search_rows` (items.py:273–274, default `limit=5`) — unaffected by the new `None` default.
- `openDetail` "similar items" passes `&limit=5` explicitly (static/app.js:754) — unaffected.
- Frontend state machine: `reloadItems` resets `items/offset/hasMore` before the full fetch and the semantic branch sets `hasMore = false`, so repeated searches replace (not accumulate) and the scroll sentinel stops. Correct.
- Existing vector-search tests (`test_items.py`) still pass — the category-filter test uses an explicit `limit=2`.

### 4. `_announceCartRestored` gated to session entry — ✅ CORRECT (with F2 toast-clobber)

Exactly two call sites: afterLogin (static/app.js:467) and switchAccount (app.js:1758), both immediately after a session-entry `loadCart()`. The mounted page-load restore (app.js:2448), the checkout resyncs (app.js:1092, 1101) and the qty-revert re-syncs inside `_cartOp` callbacks do **not** announce. Gating is as claimed.

---

## Findings (against the PR as committed, `main...HEAD`)

### F1 — HIGH, new regression: a rejected cart mutation makes logout silently do nothing
**static/app.js:606 (HEAD), with `_cartOp` 765–768 and `flushCart` 770–773.**

`_cartOp` chains via `.then(fn, fn)`; when the last enqueued operation's `api()` call rejects (network blip, 422, expired session), `this._cartChain` is left **rejected**. The new `await this.flushCart()` in `logout()` is not wrapped in try/catch (the try only covers the `/auth/logout` call on the next line), so `flushCart()` rethrows and `logout()` aborts **before** `localStorage.removeItem(TOKEN_KEY)` and the `#/login` redirect: on a shared kiosk, the logout button appears dead and the previous user stays fully logged in. Repro: take the network down, click "+1" on an item (error toast appears), restore the network, click Log out — nothing happens.

The same unguarded `await this.flushCart()` at app.js:1727 (`switchAccount`) is pre-existing on main, but this PR adds the logout variant.

**One-line fix:** `await this.flushCart().catch(() => {})` in `logout()` (and ideally `switchAccount`), or make `flushCart` itself swallow: `await (this._cartChain || Promise.resolve()).catch(() => {})`.

### F2 — MEDIUM: legacy-import toast is clobbered by the "restored" toast
**static/app.js:467 + 786–789 + importLegacyPurchase toast at ~819 (HEAD).**

On the first login of a user with a legacy localStorage list, `loadCart()` → `importLegacyPurchase()` shows "Imported N item(s) from your old purchase list", and `_announceCartRestored()` then immediately overwrites it with "Your purchase list was restored (N item(s))" — `showToast` is a single slot, so the import message is never legible. **Fix:** have `importLegacyPurchase()` return a flag and skip the announce when it just imported (exactly the shape of the uncommitted worktree fix).

### F3 — MEDIUM edge case: auto mode silently renumbers a full 12-digit UPC-A
**scripts/barcode_render.py:231 (HEAD, `elif len(body) in (12, 13): symbology = "ean13"`).**

A 12-digit input is ambiguous (complete UPC-A vs EAN-13 body). Auto resolves it as an EAN-13 *body* and appends a check digit: `036000291452` (a valid UPC-A) becomes EAN-13 `0360002914522` — a *different number* (the true EAN-13 equivalent is `0036000291452`). The rendered barcode is scannable but carries the wrong GTIN, silently. **Fix:** at minimum print the stderr warning (the uncommitted edit does); better, when the 12 digits pass the UPC check digit, resolve to `upca` instead, e.g. `symbology = "upca" if check_digit(body[:-1]) == body[-1] else "ean13"`.

### F4 — LOW: `offset` silently ignored when `limit` is omitted
**app/routers/items.py:135–137 and 221–226 (HEAD).**

When `limit is None` the SQL drops both `LIMIT` and `OFFSET`, but the endpoint still advertises `offset` — a client sending `?offset=24` (no limit) gets the identical full list; pagination *appears* to work but doesn't. **Fix:** raise 422 when `offset > 0 and limit is None` (the uncommitted worktree edit implements exactly this), or always emit `LIMIT -1 OFFSET :offset` when only offset is given.

### F5 — LOW, test gap: the parity table's *values* are not pinned
**tests/test_barcode_render.py:39–43 (HEAD, `test_parity_table_covers_all_first_digits`).**

The committed test only checks the table's keys, pattern length and L/G charset. A transposed entry (e.g. `_PARITY["4"] = "LGLGLG"` instead of `"LGLLGG"`) passes every committed test yet makes every barcode starting with 4 unscannable — precisely the class of drift the file's own docstring says the suite exists to prevent, and the one table the review was asked to trust. **Fix:** assert the full dict: `assert br._PARITY == {"0": "LLLLLL", "1": "LLGLGG", "2": "LLGGLG", "3": "LLGGGL", "4": "LGLLGG", "5": "LGGLLG", "6": "LGGGLL", "7": "LGLGLG", "8": "LGLGGL", "9": "LGGLGL"}` (the uncommitted test edit adds this — commit it; also keep its new known-answer bit-string test).

### F6 — LOW, test gap: nothing pins `openDetail`'s explicit `limit=5`
**static/app.js:754.**

The endpoint default flip (12 → None) makes the explicit `&limit=5` on the similar-items fetch load-bearing: if that parameter were ever dropped, "similar items" would silently fetch the entire catalogue (payload + 3-item slice of mostly-noise). The frontend-contract suite pins scan-sink invariants but not this. **Fix:** add an assertion to `tests/test_frontend_contract.py` that the app.js similar-items fetch URL contains an explicit `limit=`.

### F7 — INFO, accepted-risk regression: no deterministic path left for short internal codes
**static/index.html (removed `.scan-field` block), static/scancapture.js:72, 84–87.**

The removed field's own comment called it "the deterministic path for payloads the global capture gate deliberately ignores (short internal codes)". With it gone, the global gate's `SCAN_MIN_CHARS = 4` + shape regexes mean a scanned internal code shorter than 4 chars (or digit-free) is no longer routed to `enqueueScan` from anywhere — it lands in whatever field has focus. Mitigation: typing it into the search box still finds the SKU (`keyword_filter`, app/routers/items.py:37–51, covers SKU). If short-code scanning matters operationally, keep a hidden sink or lower the gate; otherwise document the tradeoff in NOTES.md (the current NOTES entry does implicitly).

### F8 — INFO: `le=100` cap dropped; every debounced keystroke downloads the whole catalogue
**app/routers/items.py:136.**

`limit` now accepts any positive int, and the frontend's default semantic search returns the full ranked catalogue (full `ItemOut` payloads) per debounced query. Fine for a kiosk-scale catalogue and the PR's stated intent, but consider a sane ceiling (e.g. `le=1000`) or a documented expectation for large catalogues. Also cosmetic: `this.offset = page.length` in the semantic branch (app.js:726) is now meaningless.

### F9 — INFO: unconditional "Your list was saved" toast
**static/app.js:615–617.**

The logout reassurance fires even when the last cart mutation failed (that change was never saved — though the user did see the error toast at failure time). Wording nit: "Your list is saved on the server" is accurate in all cases; the current phrasing can be technically false for the last, failed edit.

---

## Test runs

- Targeted: `tests/test_barcode_render.py` + `tests/test_frontend_contract.py` + `tests/test_items.py` — 34 passed.
- Full suite (working tree, see below): 692 passed, 2 failed on the first run; one failure (`test_metadata_and_ops::test_shipped_artifacts_never_name_a_git_branch`) passed on re-run and one (`test_barcode_render.py::test_resolve_upca_requires_explicit_symbology`) failed against an intermediate file state and passes against the current one — both consistent with the concurrent edits below, not with the committed PR.

## Concurrent working-tree edits (important)

While this review was in progress, the working tree was **actively modified**: uncommitted changes appeared in `app/routers/items.py`, `static/app.js`, `scripts/barcode_render.py`, `tests/test_barcode_render.py` and `tests/test_frontend_contract.py` between tool calls (the barcode test file changed test set mid-review; an intermediate `resolve()` state failed its own new test). Those uncommitted edits implement fixes for **F1** (`flushCart` `.catch`), **F2** (legacy-import announce suppression), **F3** (stderr warning), **F4** (offset-requires-limit 422) and **F5** (value-pinned parity + known-answer test), plus a `finishLogoff` double-toast fix and comment updates.

None of that is in the PR as committed (`main...HEAD` still contains F1–F5). **Those edits must be committed (and re-reviewed for the F3 behaviour choice — warning vs UPC-check-digit disambiguation) before merge.** A branch that changes under the reviewer is itself a process finding: land a quiescent commit set.

## Summary verdict

The barcode encoding layer is correct against the GS1/ISO 15420 spec (all four tables verified), the `#/purchases` wiring is correct on both load paths, the explicit-limit callers are safe, and the announce gating is as claimed. The blocking items are the committed-PR bugs F1 (logout dead-ends after a failed cart mutation — a shared-kiosk session-leak) and F2, plus committing the already-written parity-table pin (F5) and the offset guard (F4).

**REQUEST CHANGES.**
