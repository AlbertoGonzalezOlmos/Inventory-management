# PR #34 round-3 focused verification — QWEN

**Scope:** `git diff 925e2ac..26d1ed1` (round-2 residuals: QWEN N1–N3 + `-o` extension, KIMI N2).
**Method:** re-ran my round-2 adversarial repros against HEAD — my own JS harness re-transcribed
to the 26d1ed1 `loadMore` shape (`/tmp/genloop.mjs`, incl. the edge-triggered observer model),
the new permanent `node --test tests/js/loadmore.test.mjs`, and the barcode_render CLI battery
with `$?` printed. Full suite: **712 passed, 0 failed** (+ 5/5 node tests).

| Item | Status | Executed evidence |
|---|---|---|
| (1) N1 stale-REJECT refetch | **FIXED** | My harness S3: stale request rejects after a category change → loop **continues**, refetches with `category=Kitchen`, commits 3 items, no toast, `loading=false` — no stuck-empty grid. New S3b edge: the refetch itself then errors on the current gen → exactly one toast, `hasMore=false`, 2 fetches total, no retry spin. Permanent tests: 5/5 pass, and they genuinely eval the **shipped source** — `readFileSync(join(ROOT, "static", "app.js"))` + brace-matched extraction of `loadMore`/`reloadItems` into `new Function` (not a copy); the stale-reject test is `round-2 N1` by name and asserts the refetch URL + commit. |
| (2) N2 `--check --force` exit code | **FIXED** | `8412345678906 --check --force` → `check: 6 (INVALID)`, **rc=1** (was 0). Valid `--check` still rc=0; invalid+`--force`+`--format` also exits 1 after writing. |
| (3) N3 `--check -o` on invalid code | **FIXED** | `8412345678906 --check -o x.svg` → report + stderr `no file written (the code is invalid)`, rc=1, no file. Same for `--check --format svg` (the other explicit-output spelling). |
| (4) `-o x.png` without `--format` | **FIXED** | `-o out.png` → `file out.png`: **PNG image data, 351 x 134** (was SVG bytes). Controls: `-o out.svg` → SVG; `-o noext` → SVG default; `--format png -o y.txt` → explicit flag still wins (PNG). |
| (5) F2 cart leak through the refactor | **HOLDS — strengthened** | Single canonical `handleSessionExpired()` (`app.js:1566`) now carries the `this.cart` + `_cartImportedLegacy` reset; **all** 401 paths route through it (raw callers `app.js:970,1160,1518,1542,1726,1759,1849` and the `hcrm-session-expired` listener `app.js:2476`), so the two expiry paths can no longer drift — exactly the shape round-1 F2 wanted. Contract test updated to brace-matched `_method_body("handleSessionExpired() {")` extraction. |
| (5) F10 flagged-account copy | **HOLDS** | `static/index.html` untouched by 26d1ed1 (empty diffstat); `v-if="user && user.must_change_password"` branch still at `index.html:448`. |

Bonus hardening noticed: `test_frontend_contract.py` replaced the lazy `[\s\S]*?` regexes with
brace-matched method extraction (definition-anchored, call sites can't match) — closes the
false-pass shape I flagged in the round-2 honesty audit.

**Verdict: APPROVE.** All five items verified fixed at HEAD by execution; no new defects found
in the diff (the restructured `loadMore` error path behaves correctly on every interleaving I
could construct, including the stale-reject-then-current-error chain).
