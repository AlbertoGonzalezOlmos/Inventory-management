# PR #34 round-2 review — QWEN

**Scope:** `git diff ddbe26e..925e2ac` (single respin commit), `main...HEAD` as context.
**Method:** adversarial execution per the repo ground rules — barcode_render CLI battery with
exit codes printed (`$?`), independent SVG-rect and PNG-pixel decoders, live probes against a
hermetic uvicorn (`HCRM_DATA_DIR=$(mktemp -d)`, port 8931, seeded 12 items, real embedding
model), a faithful JS harness of the new `reloadItems`/`loadMore` generation loop
(`/tmp/genloop.mjs`, 6 interleavings incl. an edge-triggered IntersectionObserver model), the
full pytest suite, and a manual branch-name scan of everything the tripwire sees.
**Verdict: APPROVE** (one MEDIUM residual and two LOWs recorded for a follow-up; none blocking).

---

## §1 Round-1 findings (F1–F10) at `925e2ac`

| # | Status | Evidence (executed unless noted) |
|---|---|---|
| F1 stale-response pin | **FIXED** (residual edge → N1) | Harness S1: category change mid-flight on a slow semantic fetch → stale response dropped, **refetched with fresh state** (`category=Kitchen` in the refetch URL), grid = fresh results. S2 (semantic toggle mid-flight) and S5 (double reload: one commit, 2 fetches, no spin) also pass. S6: keyword paging intact (page 2 fetched at `offset=24`, 48 items committed). |
| F2 cart leak on 401 | **FIXED** | `static/app.js:2460-2470`: handler now resets `this.cart` and `_cartImportedLegacy` exactly like `logout()`; `tests/test_frontend_contract.py::test_session_expired_clears_the_cart_view` pins the reset inside the handler body (regex-scoped, not global). |
| F3 lost limit ceiling | **FIXED** | Live: `limit=501` → **422** (`le=500` in the detail), `limit=500` → **200**, `limit=0`/`-1`/`2.5`/`%20` → clean 422s, no-limit → all 12 items similarity-sorted desc, `offset=5` alone → 422, `limit=2&offset={0,2}` → disjoint pages, `category=Kitchen` → 3. `suggest?q=…` → `by_semantic` still 5. |
| F4 non-ASCII KeyError | **FIXED** | `٤٠٠٦٣٨١٣٣٣٩٣١` with and without `--force`, fullwidth `４０…`, and mixed `12٣٤` → one-line `barcode digits must be 0-9 (ASCII) only` on stderr, **rc=1**, no traceback (`scripts/barcode_render.py:256`). |
| F5 #/purchases test gap | **FIXED** | `test_purchases_route_wiring_is_pinned` asserts all five pieces (nav link, view mapping, section, watcher load, mounted load). |
| F6 search-contract test gap | **FIXED** | Three new `test_items.py` tests (rank-all vs `/api/items?limit=100` count, offset-without-limit 422, ceiling 422/200) + `test_items_suggest.py` slicing test; all verified against live behaviour above. |
| F7 deprecated constant | **FIXED** | `HTTP_422_UNPROCESSABLE_CONTENT` (`app/routers/items.py:150`); live server log clean of deprecation warnings after exercising the 422 path. |
| F8 `--check` swallows `--format` | **FIXED** | `--check --format svg` → report on stdout **and** file written, rc=0; plain `--check` → print-only with stderr notice, rc=0. (Residual edges → N2/N3.) |
| F9 SVG/PNG geometry | **FIXED** | Both canvases 110+24=134; PNG rect `y1=height-1` → bars exactly 110 rows (y=109 black, y=110 white); my independent decoders (SVG `<rect>` runs; PNG pixel row) reconstruct the exact encoder bit string for EAN-13, EAN-8 **and** UPC-A (95/67/95 modules) in both formats. |
| F10 flagged dead-end copy | **FIXED** | `static/index.html:445-452`: `v-if="user && user.must_change_password"` → "Change your password…" branch, else the original copy. |

Round-1 §0 process note also closed: the tripwire now passes **with** `docs/plans/` present
(full suite: **709 passed, 0 failed** — round 1 was 693/1).

## §2 New findings in the respin

**N1 — MEDIUM — an error on a *superseded* request leaves the catalogue stuck empty.**
`static/app.js:759-763` (catch), with `reloadItems` at `709-717`.

The refetch-on-superseded loop only runs when the stale request **resolves**. If it **rejects**,
the catch suppresses the toast (correct — it's stale) and `finally` clears `loading`, but the
fetch the reload asked for never happens: `reloadItems`' own `loadMore()` already early-returned
on `loading === true` (line 718). Result: `items=[], hasMore=true, loading=false`, no toast, no
pending fetch — a silently empty grid. The IntersectionObserver cannot rescue it: it fires only
on intersection *changes*, and the change provoked by emptying the grid lands while `loading` is
still true (the rejection typically follows the DOM update), so that firing is swallowed by the
same `loading` guard. Harness S3 (`node /tmp/genloop.mjs`):

```
S3 error-on-superseded: items = 0 | hasMore = true | loading = false | toasts = [] | fetches = 1
   -> STUCK EMPTY (no refetch, no toast, sentinel can't re-fire)
```

Reachability: state change (category/semantic/debounced search) during an in-flight request that
then errors — a network blip or server restart on kiosk wifi. Failure mode is an **empty** grid
(visible, recoverable on the next input or a view switch — the watcher re-runs
`reloadItems` on `items.length === 0`), not the silent wrong-data pin of round-1 F1, hence
MEDIUM, not blocking. Fix direction: on a stale rejection, re-arm instead of returning — e.g.
in the catch's else-branch, after `finally` clears `loading`, re-enter `loadMore()` (or set a
flag the finally acts on). No test pins this path (the new generation-guard test is
string-based and covers only the resolve path).

**N2 — LOW — `--check --force` on an invalid code prints INVALID but exits 0.**
`scripts/barcode_render.py:338-345` is skipped under `--force`, so flow reaches the print-only
branch and returns 0. Executed: `8412345678906 --check --force` → `check: 6 (INVALID)`,
**rc=0**. This contradicts the respin's own recorded judgement ("a validator must not report
success for an invalid code"); if force means "acknowledged", the report should say so. Untested.

**N3 — LOW — `--check -o <file>` on an invalid code silently ignores the explicit output.**
The CheckDigitError handler (`scripts/barcode_render.py:338-344`) returns 1 before the
file-writing logic and before any notice. Executed: `8412345678906 --check -o x.svg` → report,
rc=1, **no file, no notice** — the exact silent-swallow class the F8 fix targeted, on the
invalid path. (Valid + `-o` writes, so the asymmetry is invalid-only.)

**INFO (no action required):**
- `-o x.png` without `--format` writes SVG bytes into a `.png` filename — **pre-existing**
  (verified against the `ddbe26e` script: same behaviour), unchanged by the respin.
- `--check` on a body-length input (e.g. 12-digit `841234567890`) reports the *computed*
  13-digit code as "valid" — a verdict on a code the user didn't type, though the report does
  display the computed code, and there is genuinely no check digit to validate in a body.
  Likewise INVALID `--check` omits the "print-only mode" stderr notice the valid path prints.
- `app.js:1803` — dead `clearTimeout(this._greetingTimer)` (the switchAccount timer it cleared
  is gone; the 4 s timer at `app.js:548` belongs to the *login* greeting, a different message).
- **docs/plans tripwire exemption** (`tests/test_metadata_and_ops.py:208,223`): scoped tightly
  enough in the dimensions that matter — exact `dirname` match (a `docs/plans/sub/x.md` would
  *not* be exempt), the test still scans untracked files, and the comment documents the
  rationale. It is content-blind (any future file parked there escapes the tripwire
  regardless of name), which is the same trust level the PLAN/REVIEW/NOTES prefixes already
  grant. Acceptable.
- **No shipped artifact names a git branch**: zero tracked files contain
  `feature/ux-fixes-and-barcode`; the only tracked branch-naming files (`NOTES.md`,
  `REVIEW-branches.md`) are the long-standing prefix-exempt process notes; nothing outside
  `docs/plans/` that `git ls-files --cached --others` returns names the slug. Tripwire test
  passes (rc=0).

## §3 Test-honesty audit (tests added in `925e2ac`)

No tautologies found; spot-checked against broken implementations where cheap:

- `test_items.py` rank-all test compares against an independently fetched `/api/items?limit=100`
  count (not a hardcoded 12 — it asserts `total >= 12` so the fixture can't quietly shrink);
  the ceiling and offset-422 tests assert the status **and** the detail/loc. All three verified
  against live behaviour.
- `test_items_suggest.py` slicing test creates 8 matching items on top of the 12 fixtures and
  asserts exactly 5 — if slicing regressed to rank-all it would see ≥20 and fail. Honest.
- `test_barcode_render.py`: the 12-digit tests use hardcoded literals whose values I re-derived
  (`check_digit("0360002914")=="2"`); the non-ASCII test covers the `--force` bypass; the
  `--check` tests assert the stdout/stderr split and exit codes; the geometry test asserts
  *pixels* (y=109 black, y=110 white, exact canvas) — my independent decode agrees.
  `test_check_with_format_writes_the_file` also asserts the negative (plain `--check` leaves
  the directory unchanged). One pin it lacks: `--check --force` exit code (N2).
- `test_frontend_contract.py`: the new pins are string/regex-based. The two load-order regexes
  (`currentView\(view\) \{[\s\S]*?loadMyOrders\(\)` / `mounted\(\) \{…`) are anchored to unique
  single occurrences and would catch deletion or relocation; they would *not* catch the guard
  being widened (e.g. `loadMyOrders()` unconditionally) — inherent to contract tests, fine.
  `test_stale_search_responses_are_generation_guarded` pins two exact source strings: honest
  (fails if the guard is removed) but brittle to refactors and — as N1 shows — pins the
  resolve path only; the error path is unpinned.
- `test_metadata_and_ops.py` exemption change: verified it passes with the untracked
  `docs/plans/` present and would still fail for branch-named files in any other directory
  (the two tracked exempt-prefix files remain the only branch-namers).

## §4 Verdict

**APPROVE.**

Both round-1 blockers are genuinely closed and verified by execution, as are all eight
non-blocking findings; the respin's new tests are honest. The residual N1 (stuck-empty grid on
an erroring superseded request) is a real gap in the F1 fix's error path but fails visibly and
recovers on the next interaction — it should go into a small follow-up together with N2/N3
(all three are a few lines in `loadMore`'s catch and `main`'s CheckDigitError handler), plus a
test for the stale-rejection interleaving.
