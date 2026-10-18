# PR #34 round-3 focused verification — GLM

Scope: `git diff 925e2ac..26d1ed1` (scripts/barcode_render.py, static/app.js,
tests/js/loadmore.test.mjs, tests/test_barcode_render.py,
tests/test_frontend_contract.py). Method: CLI behaviour executed with `$?`
printed (outputs in /tmp, nothing touched `./data`); the node harness run
against HEAD, against the round-2 `app.js` (`git show 925e2ac:static/app.js`),
and against a hand-made mutation; `_method_body` boundaries checked; targeted
(37 passed) and full suite (**712 passed, 0 failed**); harness CI wiring
checked (.github/workflows/ci.yml:59, glob form).

## N1 — `--check --force` exit code: **FIXED** (executed)
- `8412345678906 --check --force` → INVALID report on stdout, **rc=1**
  (was 0). Valid code + `--check --force` still rc=0.
- `--check --force --format svg` on the invalid code → file
  `8412345678906.svg` **is written** (that is --force's purpose) and rc=1 —
  the exit code follows the verdict, not the render (barcode_render.py:362,
  397). Pinned by `test_check_force_invalid_still_exits_nonzero`.
- Bonus hardening verified: `--check -o x.svg` on an invalid (unforced) code
  → rc=1, "no file written" on stderr, no file — the explicit-file swallow
  class is closed on this path too (barcode_render.py:348–353).

## N2 — format inferred from `--output` extension: **FIXED** (executed)
barcode_render.py:363–371: explicit `--format` wins; else `.svg`/`.png`
inferred (case-insensitive); unknown extension → svg default.
- `--output label.png` (no --format) → rc=0, PNG magic `\x89PNG\r\n\x1a\n`
  (was SVG bytes).
- `--output odd.png --format svg` → SVG bytes in odd.png (explicit wins).
- `--output plain.txt` → SVG bytes (documented fallback).
Pinned by `test_output_extension_infers_format` (magic-byte + `<svg`
assertions — cannot pass against the round-2 behaviour).

## N3 — superseded-request REJECT path refetches: **FIXED**
static/app.js:717–770: the fetch moved inside a per-iteration try/catch;
a stale rejection hits `if (gen !== this._itemsGen) continue` → refetch
with fresh state; a current rejection toasts + `hasMore = false` + break.
The old outer catch (swallow-and-strand) is gone. Verified **behaviourally**
by the node harness (below), not just by source.

## §4 Test-honesty audit of the new tests

- **tests/js/loadmore.test.mjs — honest, and genuinely behavioural.** It
  brace-extracts the *real* `reloadItems`/`loadMore` from static/app.js and
  evals them against a scripted deferred `api()` — no reimplementation to
  drift. Independently re-verified it cannot pass against the round-2
  defect: run against `925e2ac:static/app.js`, exactly "stale REJECT also
  refetches" fails (4 pass / 1 fail, rc=1). Extra mutation probe: a variant
  that refetches but *toasts* on the stale error also fails test 2 (the
  `toasts == []` assertion discriminates). All 5 pass against HEAD. The
  "double reload" test pins exactly-one-refetch (no spin/duplicate commit);
  the toggle test pins the keyword-path refetch with `offset=0`. Wired into
  CI via `node --test tests/js/*.test.mjs` (glob form; the bare-dir form
  fails on Node 22 — the workflow comment already notes this).
- **`_method_body()` brace matching — sound for what it pins.** Header must
  be the definition text ending in `{` (call sites can't match); extracted
  boundaries verified sane for all five pinned methods (plausible lengths,
  each ends at its own closing brace; `loadMore` contains exactly the
  asserted 2 gen-checks). Residual (INFO, non-blocking): the counting is
  brace-naive — a `}` inside a string/regex/template literal in a future
  pinned body would over-capture into a superset and could false-pass;
  none of the current bodies contains one. The `hcrm-session-expired`
  handler still uses the old unbounded `[\s\S]*?\}\);` regex (short body,
  low risk) — same note.
- **test_barcode_render.py additions — honest**: exit codes, real file
  existence, byte-magic checks, and the no-file notice all execute real
  `main()` calls; each fails against the round-2 behaviour it targets.

## Verdict

**APPROVE.** N1, N2, N3 all fixed with executed evidence; the new tests are
behavioural where it matters (the node harness demonstrably fails against
both the round-2 defect and a subtle mutation), and the remaining regex/
brace notes are INFO-level.
