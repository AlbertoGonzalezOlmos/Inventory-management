# PR #34 round-2 review — the respin (`925e2ac`)

Reviewer: GLM. Scope: `git diff ddbe26e..925e2ac` (single commit, tracked files
quiescent — `git status` shows only untracked scratch, unlike round 1's
moving tree). Method: full diff read; every barcode table re-verified
programmatically against independently transcribed GS1/ISO 15420 reference
tables (all 10 digits × L/R/G/PARITY, plus a 500-random-code cross-encode
against a spec-derived encoder, guards/centre/module counts, UPC-A↔EAN-13
identity); CLI behaviour executed with exit codes printed (`$?`); targeted
suite (72 passed) and full suite (**709 passed, 0 failed**). Nothing touched
`./data`; script outputs went to `/tmp`.

**Verdict: APPROVE** — all blocking round-1 findings are closed with executed
evidence; the new findings below are LOW/INFO and none block merge.

## §1 Round-1 findings (F1–F9) at `925e2ac`

- **F1 (HIGH, logout dead-end) — FIXED.** `flushCart` now swallows:
  `await (this._cartChain || Promise.resolve()).catch(() => {})`
  (static/app.js:792–798); both `logout()` (app.js:606) and `switchAccount`
  (app.js:1761) go through it, so a rejected chain can no longer abort the
  teardown before `localStorage.removeItem(TOKEN_KEY)`. (No JS runtime in
  the repo — verified by source; the mechanism is a one-line catch around
  the only await.)
- **F2 (MEDIUM, toast clobber) — FIXED.** `importLegacyPurchase` returns a
  flag on both import paths; `loadCart` stores it (app.js:804) and
  `_announceCartRestored` skips when set (app.js:815). Call sites still
  exactly the two session-entry points (app.js:467, 1792).
- **F3 (MEDIUM, 12-digit GTIN) — FIXED, exactly the suggested fix.**
  `resolve` now checks `check_digit(body[:-1]) == body[-1]` for 12-digit
  auto input (scripts/barcode_render.py:270). Executed: `036000291452` →
  renders UPC-A as typed (rc=0, informational stderr note);
  `841234567890` → EAN-13 body, check digit appended (rc=0, warning on
  stderr). Both stderr notes instruct the override.
- **F4 (LOW, offset ignored) — FIXED.** items.py:148–152 raises 422
  (`HTTP_422_UNPROCESSABLE_CONTENT`) when `limit is None and offset > 0`;
  executed via `test_vector_search_offset_without_limit_is_422` (422 +
  the sanctioned `limit=500&offset=0` → 200).
- **F5 (LOW, parity values unpinned) — FIXED.**
  `test_parity_table_matches_the_gs1_first_digit_table` now asserts the
  full dict, L/R/G likewise, plus a known-answer 95-module bit string
  (tests/test_barcode_render.py:59–89). I re-verified all four tables and
  the known answer against my own spec transcription — exact match
  (executed, rc=0; the file's tables were untouched by the respin).
- **F6 (LOW, limit=5 unpinned) — FIXED.**
  `test_similar_items_fetch_pins_explicit_limit` (test_frontend_contract.py)
  anchors on the `"/items/vector-search?q="` literal followed by `&limit=5`;
  `loadMore`'s fetch builds params programmatically, so the regex can only
  match `openDetail`'s fetch — correctly targeted.
- **F7 (INFO, short internal codes) — NOT FIXED, accepted.** As recorded in
  the synthesis: documented tradeoff, search box still covers SKU.
- **F8 (INFO, le cap) — FIXED.** `le=500` on the explicit limit
  (items.py:136), pinned by the 501→422 / 500→200 test. The no-limit
  rank-all default stays by design — the recorded judgement call, with the
  contract pinned by `test_vector_search_no_limit_ranks_every_item`.
- **F9 (INFO, unconditional "saved" toast) — NOT FIXED, accepted.**
  app.js:615–617 still toasts on `hadItems` regardless of the last
  mutation's outcome. Wording nit only; not in the synthesis, non-blocking.

Process note: round 1's "branch changed under the reviewer" complaint is
resolved — the respin is one quiescent commit and the tracked tree is clean.

## §2 New findings in the respin

- **N1 — LOW: `--check --force` prints INVALID but exits 0.**
  scripts/barcode_render.py:338–357. With `--force`, `resolve` returns the
  invalid code without raising, so `--check` falls into the normal path:
  report says `check: 6 (INVALID)` (recomputed in `_print_report`), then the
  print-only branch returns 0. Repro executed:
  `barcode_render.py 8412345678906 --check --force` → INVALID on stdout,
  **rc=0** (vs rc=1 without `--force`). Contradicts the respin's own
  contract ("a validator must not report success for an invalid code").
  No test covers the combination. Suggest: exit 1 whenever the report says
  INVALID, force or not.
- **N2 — LOW: `--output x.png` without `--format png` writes SVG bytes into
  a .png file.** scripts/barcode_render.py:361–371: `fmt = args.format or
  "svg"` and `out = args.output or f"{full}.{fmt}"` — the output name never
  feeds back into the format. Repro executed: `--output out.png` → rc=0,
  `file(1)` reports "SVG Scalable Vector Graphics image". Pre-existing
  shape (the old `default="svg"` behaved identically), but the respin
  rewrote exactly this resolution block; inferring from the extension (or
  warning) would close it.
- **N3 — LOW (static analysis, no browser repro executed): a superseded
  request that *rejects* strands the fresh query.** static/app.js:706–766.
  `reloadItems` during an in-flight fetch bails at loadMore's
  `if (this.loading || !this.hasMore) return` (app.js:718); the in-flight
  loop then refetches on success (`continue`, gen-refreshed — the designed
  self-heal) but on *rejection* the catch suppresses the toast (correctly —
  `gen` is stale) and nothing re-fetches: the IntersectionObserver
  (app.js:2497) only fires on intersection *change*, and the sentinel
  (`v-show="hasMore"`, still true over an empty grid) may already be
  intersecting. Grid silently empty until the next keystroke/category
  change. Explicitly checked and ruled out: no spin (each `continue`
  requires a gen bump = user action), no deadlock (`finally` clears
  `loading`), no stale commit (gen check and commit are one synchronous
  block — no microtask can interleave). Narrow trigger (superseded request
  must reject exactly then) + easy recovery ⇒ LOW.
- **N4 — INFO: exempt-dir tripwire scope.** tests/test_metadata_and_ops.py
  :208,223 exempts exactly `dirname(path) == "docs/plans"` — direct children
  only (a nested `docs/plans/sub/` is NOT exempt), tighter than a prefix
  match. Residual: a future genuinely-shipped doc parked there would bypass
  the tripwire; acceptable for a process-notes directory, worth a comment
  if it ever ships.

GS1/ISO 15420 re-verification (my assigned emphasis): **all four tables
exact**; R = complement(L), G = reverse(R) hold; guards `101`/`01010`/`101`
correct; 95/95/67 modules; quiet zone 11 ≥ 9; `ean13_bits("0"+upc12) ==
upca_bits(upc12)` bit-identical; 500 random codes cross-encoded against an
independently spec-derived encoder — all match. SVG/PNG geometry unified:
both canvases `height + 24` (SVG `label_y+8` = PNG literal), PNG bars
`y1 = height - 1` = exactly `height` rows = SVG rect height; executed PNG
(351,134) == SVG (351,134); boundary pixels black at y=0/109, white at
y=110 (pinned by test). `CheckDigitError` ordering (subclass caught before
`ValueError`) is correct; the INVALID report goes to stdout, exit 1, plain
mode stays a one-line stderr error (all executed, §repros above).

## §3 Test-honesty audit (tests added in `925e2ac`)

- `test_barcode_render.py`: **honest.** The 12-digit tests are
  complementary — an always-upca resolver fails the EAN-13-body test, an
  always-ean13 resolver fails the UPC-A test; round-trip tests use literal
  expected values; the `--check` tests assert rc + exact stdout/stderr
  split (`"expected 5" not in out`, empty stdout in plain mode); the
  format/file test asserts real file existence and an exact directory
  listing (the old swallow cannot pass); the geometry test checks exact
  canvas sizes and boundary pixels (an off-by-one in `y1` fails it). Ran
  with Pillow installed — no skips. One mild note: the EAN-13-body test
  computes its expected value via the same `check_digit` the impl uses —
  consistent-by-construction w.r.t. that function, but `check_digit` is
  independently pinned in the app suite, and the disambiguation branch's
  real pin (`("8412345678905", "ean13")` literal) is present.
- `test_items.py` / `test_items_suggest.py`: **honest** — real endpoint
  calls, exact status codes, and the suggest test creates 8 matches and
  asserts exactly 5 (a rank-all leak cannot pass). Minor fragility, safe
  direction: `no_limit_ranks_every_item` derives `total` from
  `/items?limit=100`, capped at the keyword endpoint's le=100 — fails
  loudly if the fixture ever exceeds 100 items.
- `test_frontend_contract.py`: **regression pins, not behaviour tests** —
  the `_itemsGen`/`session-expired`/`purchases-wiring` tests assert
  source strings and regexes. They catch deletion of the mechanism but not
  a subtly wrong guard (e.g. commit outside the gen check would still pass
  the string pins); with no JS harness in the repo this is the available
  tool, and §2-N3 is exactly the class they cannot see. One concrete
  fragility: `mounted\(\) \{[\s\S]*?loadMyOrders\(\)` is unbounded — it
  would false-pass if mounted's call were deleted and a `loadMyOrders()`
  appeared anywhere later in the file (none does today). Suggest bounding
  the span (e.g. `[^{]*`-style or a brace-count) when next touched.
- `test_metadata_and_ops.py` exemption: exercised (suite green with the
  round files present in `docs/plans/`).

## §4 Verdict

**APPROVE.** F1–F6, F8 closed with executed evidence; F7/F9 accepted INFOs.
New findings N1–N3 are LOW (N1/N2 have executed repros and one-line fixes —
good follow-ups, not merge blockers for a kiosk tool), N4 INFO. The barcode
layer is spec-exact and its new tests genuinely discriminate; the respin
also fixed the round-1 process problem by landing as one quiescent commit.
