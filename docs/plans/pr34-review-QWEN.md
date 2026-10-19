# PR #34 — independent review (QWEN)

**Scope reviewed:** `git diff main...HEAD` at `ddbe26e` (three commits: `03c5ccd` UI + cart UX,
`8e34461` barcode_render, `ddbe26e` review fixes).
**Verdict: REQUEST CHANGES** — the barcode symbology is correct and well pinned; the UI half has
two demonstrated defects (an unrecoverable stale-render regression and a cross-user cart leak)
plus a removed response-size ceiling.

---

## 0. Process note — read this first

**The branch moved while this review was running.** When I started, HEAD was `8e34461` and five PR
files were dirty in the working tree. Mid-review those were committed as `ddbe26e`
"fix: address PR #34 review findings", and HEAD moved. Everything below is against `ddbe26e`;
`git status` was clean of tracked modifications when I finished.

`ddbe26e` already fixes six things I had independently reproduced against `8e34461`, so they are
**not** re-raised here: the un-caught `flushCart()` rejection that could abort `logout()` and
strand a user; the `finishLogoff` toast clobber; the legacy-import / cart-restored toast clobber;
`offset` being silently ignored without a `limit`; the 12-digit UPC-A-vs-EAN-13 ambiguity (now
warns on stderr); and the tautological R/G/PARITY test pins (now hardcoded literals + a
known-answer vector).

**Action:** the PR description says "two commits" — it is stale. Re-pull before merging.

**Also:** `tests/test_metadata_and_ops.py::test_shipped_artifacts_never_name_a_git_branch` is
currently **RED** on this checkout. Two sibling review artifacts under `docs/plans/` name the
branch slug, and that test scans `git ls-files --others`, so *untracked* review files count as
shipped artifacts. This file deliberately avoids the slug; the other two need the same treatment
or the suite stays red. (Full suite otherwise: **693 passed, 1 failed** — that one.)

---

## 1. What I verified as CORRECT (evidence, not assertion)

### 1.1 The EAN-13 L/R/G and first-digit PARITY tables are right

This was the highest-risk item (a wrong table = unscannable barcode), so I checked it against an
independent oracle rather than against my recollection of the spec.

**Table-level cross-check** vs `python-barcode` 0.16.1's own GS1 transcriptions
(`barcode/charsets/ean.py` → `CODES["A"/"B"/"C"]`, `LEFT_PATTERN`; `barcode/charsets/upc.py` →
`CODES["L"/"R"]`):

| `barcode_render.py` | oracle | result |
|---|---|---|
| `_L` (left, odd) | `ean.CODES["A"]`, `upc.CODES["L"]` | exact match, all 10 |
| `_G` (left, even) | `ean.CODES["B"]` | exact match, all 10 |
| `_R` (right, even) | `ean.CODES["C"]`, `upc.CODES["R"]` | exact match, all 10 |
| `_PARITY` | `ean.LEFT_PATTERN` (A→L, B→G) | exact match, all 10 first digits |
| `_START`/`_CENTER`/`_END` | `EDGE`/`MIDDLE`/`EDGE` | match |

The two derivation identities the script relies on both hold against the oracle's independent
values: `R == complement(L)` and `G == reverse(R)`. So deriving R and G from a single transcribed
L table is sound, not a shortcut that hides an error.

**Bit-level cross-check:** 2,442 encoded bit strings compared byte-for-byte with
`python-barcode`'s `build()` — 1,950 EAN-13 (every parity row × every left-hand position × every
digit, every right-hand position × every digit, plus 400 randoms), 237 EAN-8, 255 UPC-A.
**Zero mismatches.**

### 1.2 The rendered output is actually scannable

Bit tables being right does not prove the *renderer* is right, so I wrote an independent GS1
decoder, rendered real SVGs via the CLI, and decoded them back from the emitted `<rect>` geometry:

- 40 EAN-13 (all 10 first digits + 30 randoms) and 20 EAN-8 → **60/60 exact round-trips**.
- Lengths 95 / 67 modules; start/end guards `101`, centre guard `01010` at the correct offsets;
  run-merging in `_render_svg` reconstructs the exact bit string; quiet zone 11 modules each side
  (≥ the GS1 minimum of 11 left / 7 right).
- PNG path exercised under Pillow 10.2.0: renders fine, `draw.text(anchor="ma")` with
  `ImageFont.load_default()` does **not** raise (I checked because that combination breaks on
  older Pillow bitmap fonts).

### 1.3 The new known-answer vector is genuine

`tests/test_barcode_render.py::test_ean13_known_answer_bit_string` pins a 95-char literal for
`4006381333931`. I confirmed that literal equals **both** `br.ean13_bits(...)` **and**
python-barcode's output (95 modules), and that `check_digit("400638133393") == 1`. The Stabilo
code really is documented in-tree at `docs/barcodes.md:69,236,256`, so the test's citation is
accurate.

### 1.4 `#/purchases` loads on BOTH direct load and navigation ✅

- **Direct load:** `route` initialises from `location.hash` (`static/app.js:107`), `currentView`
  maps `#/purchases` → `"purchases"` (`app.js:243`), and `mounted()` loads the data
  (`app.js:2465`: `if (this.currentView === "purchases") { this.loadMyOrders(); this.loadCalendar(); }`),
  correctly inside the `if (this.user)` guard so a logged-out deep link can't fire a 401.
- **Navigation:** the `currentView` watcher loads the same pair (`app.js:393-396`).
- **No double-fetch either way:** Vue's watcher doesn't fire on initial render, and `mounted`
  doesn't re-run on hash change.
- **Post-checkout landing is correct:** `app.js:1142-1143` assigns `route` and `location.hash` in
  one synchronous turn, so the `hashchange` handler's `navigated` guard (`app.js:2431`) stays
  false and `_closeOverlays()` does not kill the follow-up toast; `expandedOrders = [pid]` is set
  synchronously, and Vue flushes the watcher in a microtask, so the receipt is pre-expanded before
  `myOrders` resolves.
- No dangling `profileTab === 'purchases'` references survive the tab removal
  (`selectProfileTab` branch and the sub-tab button are both gone; `profileTab` defaults to
  `"profile"`, so the account view can't render an empty panel).

### 1.5 Keyword pagination and the explicit-limit callers are unaffected ✅

- `suggest_items` passes `limit=limit` explicitly (`app/routers/items.py:278-279`, default 5) —
  verified live: `GET /api/items/suggest?q=something` still returns exactly **5** `by_semantic`.
- `openDetail`'s "similar items" passes `&limit=5` (`static/app.js:753-756`).
- `scripts/smoke_test.py:144` passes `&limit=5`; `smoke_test.py:198` omits `limit` but only asserts
  a 401, so the new default can't break it.
- Keyword pagination verified live and untouched: `?limit=2&offset=0` → ids `[12,11]`,
  `?limit=2&offset=2` → `[10,9]`, disjoint.
- Rank-all default verified live: `?q=something` → 200 with **all 12** fixture items, sorted by
  descending `similarity`, `min similarity == 0.0` (i.e. genuinely no threshold), and the
  `category` filter still applies in SQL (`category=Kitchen` → 3).
- `scripts/ui_check.py --self-host` → **UI CHECK PASSED**.

### 1.6 `_announceCartRestored` is correctly gated to session entry ✅

Exactly two call sites, both immediately after a `loadCart()` at a session-entry funnel, and never
inside `loadCart` itself:

- `app.js:467` — `afterLogin` (the shared funnel for password login, badge login and register).
- `app.js:1771` — `switchAccount`.

The routine `loadCart()` calls that must stay silent are all untouched: the qty re-syncs at
`app.js:1103` and `app.js:1112`, and the page-load restore at `app.js:2461`. The empty-cart guard
means a fresh registration doesn't toast, and the `_cartImportedLegacy` flag (set in `loadCart`,
consumed and cleared in `_announceCartRestored`) stops it clobbering the one-time
"Imported N items" toast. `flushCart()` (`app.js:771-777`) is now genuinely best-effort
(`.catch(() => {})`), which matters because `_cartOp` (`app.js:766-769`) propagates rejections
into `_cartChain`.

---

## 2. Findings

### F1 — HIGH — semantic search: a stale response now pins the grid permanently

`static/app.js:713-728` (with `reloadItems` at `706-711`, `category` watcher at `377`)

`reloadItems()` is **not** guarded by `this.loading`, and the `category` watcher calls it directly
with no debounce (only `search` is debounced, `app.js:374-376`). So if a semantic request is in
flight when the category changes: `reloadItems()` resets `items/offset/hasMore`, calls `loadMore()`,
which returns immediately at `app.js:714` because `loading === true`; then the **stale** response
lands and executes `this.items = page` (726) and `this.hasMore = false` (728).

The regression is line 728. I reproduced both versions in a faithful JS harness with a slow
semantic endpoint and a 40-match catalogue:

```
[old] after stale response: n=48 hasMore=true  | sentinel re-fetch -> q=milk|Kitchen | SELF-HEALS: true
[new] after stale response: n=40 hasMore=false | sentinel re-fetch -> q=milk         | SELF-HEALS: false
```

Previously `hasMore = page.length === PAGE_SIZE` stayed `true` whenever a full page came back, so
the IntersectionObserver re-fired and the correct query/category was fetched. Now `hasMore` is
hard-set to `false`, the sentinel is dead, and the operator is left looking at results for a filter
they already changed — on a kiosk, silently adding the wrong items. Only editing the search box
again recovers.

**Fix:** add a generation token — `const gen = ++this._itemsGen;` before the `await` in `loadMore`
and `if (gen !== this._itemsGen) return;` immediately after it (both branches), so a superseded
response is dropped instead of committed.

### F2 — HIGH — cross-user cart leak on the session-expiry path

`static/app.js:2438-2444`

The `hcrm-session-expired` handler clears `user`, the logoff timer, the greeting and the overlays —
but **not** `this.cart`. Both other teardown paths do reset it: `logout()` at `app.js:612` and
`switchAccount`'s flagged branch at `app.js:1764` (whose comment is literally *"drop the OUTGOING
user's cart from the view so it can't linger in the UI"*). The 401 path is the one that was missed,
and it is the most likely one on a shared kiosk.

Reachable chain, every link verified in the source:

1. User A's token 401s → `app.js:2438-2444` → `user = null`, `this.cart` still holds A's lines.
2. User B logs in **flagged** → `afterLogin` (`app.js:455-465`) hits the `must_change_password`
   branch and `return`s *before* `loadCart()` — cart never overwritten.
3. B changes their password → `changePassword` (`app.js:622-640`) clears
   `must_change_password` but never calls `loadCart()`.
4. B opens the catalogue → `currentView` watcher (`app.js:385-390`) calls only `reloadItems()`.
5. The sidebar renders `purchase` = `this.cart.lines` (`app.js:256-258`, `index.html:290-320`) →
   **B sees A's purchase list and A's total.**

Worse than a display leak: pressing `+`/`−` on one of those lines issues a cart mutation carrying
A's `item_id` under B's token, writing A's items into B's durable server cart.

Step 2 is realistic, not exotic — staff-issued accounts start flagged (see the `create_user`
mandatory-change note in `tests/conftest.py:38-41`).

**Fix:** add `this.cart = { lines: [], total_qty: 0, total_cents: 0 };` to the
`hcrm-session-expired` handler (`app.js:2438-2444`), matching `logout()` and `switchAccount`.

### F3 — MEDIUM-HIGH — vector-search lost its response-size ceiling

`app/routers/items.py:136-138`

The rewrite changed `limit: int = Query(default=12, ge=1, le=100)` to
`limit: int | None = Query(default=None, ge=1, ...)` — dropping **`le=100`** along with the
default. So the endpoint now has no upper bound at all, from either direction. Verified live:

- `?q=something&limit=1000000` → **200** (previously a 422 from `le=100`).
- `?q=something` (no limit) → **all 12** fixture items, `min similarity 0.0`.

Downstream, `vector_search` builds `Item.id.in_([...])` over every returned row
(`items.py:172-177`), and `static/app.js:726` assigns the whole array to `this.items`, which the
catalogue grid renders with a plain `v-for` and no virtualisation — refetched on every
300 ms-debounced keystroke (`app.js:374-376, 428`). The fixture DB has 12 items, so nothing here
shows up in tests; at a few thousand SKUs this is an unbounded payload plus a few thousand DOM
nodes per keystroke.

**Fix:** restore a ceiling on the explicit path (`Query(default=None, ge=1, le=500)`) and cap the
frontend fetch (`params.set("limit", 200)` at `app.js:723`) — "rank everything" is still satisfied
for any realistic catalogue, and the sentinel can page the rest if you keep `hasMore` honest.

### F4 — MEDIUM — `resolve()` accepts non-ASCII digits, then dies with an uncaught `KeyError`

`scripts/barcode_render.py:224` (call sites `281-282` and `303`)

`if not body.isdigit()` uses `str.isdigit()`, which is `True` for Arabic-Indic, superscript and
other Unicode decimal digits. Those then flow into `_PARITY[...]` / `_L[...]` / `_R[...]`, which
are ASCII-keyed. Verified:

```
resolve('١٢٣٤٥٦٧', 'auto', False)  -> ('١٢٣٤٥٦٧0', 'ean8')     # no error raised
ean8_bits('١٢٣٤٥٦٧0')              -> KeyError('١')
main(['٤٠٠٦٣٨١٣٣٣٩٣١','--force'])  -> KeyError('٤')             # raw traceback
```

`main()` only catches `ValueError` (line `282`), and `_bits_for` is called at line `303`, outside
that `try` — so the user gets a Python traceback instead of the script's otherwise-clean
`barcode_render: <message>` + exit 1. Reachable by pasting a code out of a PDF or a
localised label, and `--force` (the documented misread-testing hatch) makes it worse by bypassing
the check-digit comparison that would otherwise have rejected the input.

**Fix:** `if not (body.isascii() and body.isdigit()):` at line `224`.

### F5 — MEDIUM — test gap: nothing pins the `#/purchases` route wiring

`tests/test_frontend_contract.py` (whole file); targets `static/index.html:50,440`,
`static/app.js:243,393-396,2465`

The new tab needs **five** cooperating pieces, and dropping any one fails silently as an empty page
rather than an error: the nav link, the `currentView` mapping, the section, the watcher load, and
the `mounted` load. The watcher/`mounted` pair is precisely the duplicated-logic shape that
regresses (someone "de-duplicates" one and direct deep links, or navigation, quietly stops
fetching). This file already pins comparable structural invariants — scan-capture load order, the
`data-scan-sink` count, the absence of an `isTrusted` check — so the omission is inconsistent with
its own stated purpose. Confirmed by grep: no test in `tests/` references the `#/purchases` route
(the `test_purchases.py` hits are all backend table/endpoint tests).

**Fix:** add a contract test asserting `index.html` contains both `href="#/purchases"` and
`currentView === 'purchases'`, and that `app.js` loads `loadMyOrders()` in **both** the
`currentView` watcher and `mounted`.

### F6 — MEDIUM — test gap: the changed search contract is untested

`tests/test_items.py` (nearest: `:100`, which only exercises `limit=2`)

Three behaviours this PR introduces or changes have no coverage:

1. `limit` omitted → every item ranked, no threshold (the headline change).
2. `offset` without `limit` → **422**. This guard (`app/routers/items.py:148-152`) is brand new in
   `ddbe26e` and completely unexercised; I verified it works by hand, but nothing would notice if
   it regressed to the old silent-ignore.
3. `suggest`'s `by_semantic` still slices to `limit` (i.e. the default-`None` change did not leak
   into the suggestion path).

**Fix:** add those three assertions to `tests/test_items.py` beside the existing vector-search
tests.

### F7 — LOW — deprecated Starlette status constant in the new guard

`app/routers/items.py:150`

`status.HTTP_422_UNPROCESSABLE_ENTITY` emits a `StarletteDeprecationWarning` on every hit
(observed in the pytest run: *"Use `HTTP_422_UNPROCESSABLE_CONTENT` instead"*). The same file uses
`CONTENT` at lines `109` and `317`, and the repo is 6 `CONTENT` : 1 `ENTITY` — the new code is the
odd one out.

**Fix:** use `status.HTTP_422_UNPROCESSABLE_CONTENT`.

### F8 — LOW — `--check` / `--analyze` silently swallow `--format`

`scripts/barcode_render.py:300-302`

`if args.output is None and (args.check or args.analyze): return 0` short-circuits before any
rendering, so `barcode_render.py <code> --check --format png` prints the check digit, exits 0,
produces no PNG, and says nothing about it. Meanwhile `--analyze -o x.png` *does* write. The
asymmetry is undocumented (the module docstring's usage block shows `--check` and `--format` as
independent flags).

**Fix:** either print `barcode_render: no file written (recognition mode; pass --output)` before
the `return 0`, or only skip when `args.format` is the default.

### F9 — LOW — SVG and PNG geometry disagree

`scripts/barcode_render.py:146` vs `188`, and `197`

SVG canvas is `height + 24` (`label_y + 8`, `label_y = height + 16`); PNG canvas is `height + 28`.
PNG bars are one pixel taller than intended because `ImageDraw.rectangle` treats both corners as
inclusive: `[x, 0, x + w - 1, height]` fills `height + 1` rows, whereas the SVG rect is exactly
`height`. Neither affects scannability (both decode — see §1.2), but the two formats are not
pixel-interchangeable, which matters if anything ever diffs or caches them.

**Fix:** use `height - 1` as the PNG rect's `y1`, and make both canvases `height + 24`.

### F10 — LOW — a flagged account gets a misleading empty state on `#/purchases`

`static/index.html:445`; guards at `static/app.js:993-997` and `1005-1015`

`loadMyOrders` and `loadCalendar` both correctly return empty when `must_change_password` (and the
comment at `app.js:995` shows that's deliberate), but the section then renders
*"Nothing purchased yet — complete a purchase from your list in the catalogue"* — an instruction a
flagged account cannot follow, since every endpoint 403s until the password changes. Not a data
leak; just a dead-end message on the one route that bypasses the `#/account` steering.

**Fix:** gate that copy on `!user.must_change_password` and show the "change your password first"
line instead.

---

## 3. Things I checked that are NOT problems

Recorded so the next reviewer doesn't re-spend the time:

- **Guard-bar extension.** Real EAN-13 symbols drop the start/centre/end guards below the digit
  baseline. This renderer doesn't. That is cosmetic — ISO 15420 does not require it for decode, and
  my independent decoder reads all 60 rendered symbols correctly without it.
- **Quiet zone.** 11 modules each side; the GS1 minimum is 11 left / 7 right for EAN-13. Compliant.
- **`--force` rendering a bad check digit.** Intentional and documented (misread testing); the
  resulting symbol is scannable and reports the wrong digits, which is the point.
- **`_bits_for` on an unknown symbology.** Unreachable — `resolve` only ever returns one of the
  three, and `--symbology` is `choices`-constrained by argparse.
- **`this.offset = page.length` in the semantic branch** (`app.js:727`). Dead value: the semantic
  path never reads `offset`, and `reloadItems` zeroes it. Harmless.
- **Post-checkout `expandedOrders` / `lastCheckout` ordering** — correct (see §1.4).
- **`README.md` / `NOTES.md` updates** — both accurately describe the new optional-`limit`
  semantics and the removed scan field; no stale "Scan items or badges here" copy survives in
  `static/`. The only `scan-hint` left (`index.html:285`) belongs to the checkout panel, not the
  removed catalogue field.
- **`tests/test_frontend_contract.py`'s weakened sink assertion.** The rename to
  `test_badge_fallback_sink_still_exists` with `>= 1` is legitimate (the visible field really is
  gone), and `ddbe26e` tightened it to an exact count. Note the raw grep of `data-scan-sink` in
  `index.html` returns 2 hits because one is inside a comment at line `108` — the real attribute
  count is 1, so if the tightened assertion counts raw substrings it is asserting the wrong thing.
  Worth a glance.

---

## 4. Reproduction

```bash
# tables + bit strings vs an independent oracle (2,442 codes)
pip download python-barcode -d /tmp/x && mkdir -p /tmp/bclib \
  && unzip -oq /tmp/x/python_barcode-*.whl -d /tmp/bclib
PYTHONPATH=/tmp/bclib .venv/bin/python /tmp/verify_bars.py       # -> ALL MATCH

# render -> decode round trip (60 SVGs)
.venv/bin/python /tmp/decode_roundtrip.py                        # -> FAILURES: 0

# F1 stale-response race (old vs new loadMore)
node /tmp/race2.mjs                                              # -> old SELF-HEALS: true
                                                                 #    new SELF-HEALS: false

# F3 / F6 endpoint contract
#   ?q=something                -> 200, all 12 items, min similarity 0.0
#   ?q=something&offset=5       -> 422 (untested in the suite)
#   ?q=something&limit=1000000  -> 200 (the le=100 cap is gone)
#   /api/items/suggest?q=...    -> by_semantic == 5 (explicit limit honoured)

# F4
.venv/bin/python -c "import sys;sys.path.insert(0,'.');import scripts.barcode_render as br;\
print(br.main(['٤٠٠٦٣٨١٣٣٣٩٣١','--force','--no-label','-o','/tmp/u.svg']))"   # -> KeyError

# suite state
.venv/bin/python -m pytest tests/ -q      # 693 passed, 1 failed (branch-name lint, see §0)
.venv/bin/python scripts/ui_check.py --self-host   # UI CHECK PASSED
```

---

## 5. Verdict

**REQUEST CHANGES.**

Blocking: **F1** (demonstrated regression — a stale semantic response now permanently pins the
catalogue to the wrong filter, where it used to self-heal) and **F2** (a real cross-user cart leak
on a shared kiosk, in the one teardown path the PR's own fix missed). **F3** should go in the same
pass since it is a two-token change and F1's fix touches the same lines.

**F4–F6** are strongly recommended before merge (F4 is a one-line crash fix; F5/F6 are the tests
that would have caught F1-adjacent and F3-adjacent regressions). **F7–F10** are fine as follow-ups.

To be clear about the split: `scripts/barcode_render.py` is the strongest part of this PR. I tried
hard to break the symbology — table values, derivation identities, encode ordering, render geometry
— and it survived an exhaustive comparison against an independent GS1 implementation plus a
full render→decode round trip. That half is mergeable as it stands. The objections are all in the
SPA and the search endpoint.
