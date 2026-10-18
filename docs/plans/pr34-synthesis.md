# PR #34 review synthesis — respin of the review findings

**Round-2 addendum (below §“Round 2”) — verdicts: GLM APPROVE, QWEN
APPROVE, KIMI REQUEST CHANGES; KIMI's two blockers + the converged LOWs
are fixed in the round-2 follow-up commit.**

Input: `pr34-review-{GLM,KIMI,QWEN}.md` (three independent REQUEST-CHANGES
reviews of `ddbe26e`, the first fix commit). All three verified the symbology
tables, the `#/purchases` wiring, the explicit-limit callers and the
announce gating as correct; the outstanding findings below are the respin.

## Fixed in this respin

| Finding (reviewer) | Fix |
|---|---|
| Stale semantic response pins the grid, `hasMore=false` kills the sentinel (KIMI F1, QWEN F1, HIGH) | `reloadItems` bumps a request generation (`_itemsGen`); `loadMore` loops: a superseded response is dropped and **re-fetched with fresh state** (not merely discarded — reload during an in-flight request left nothing to re-trigger the fetch). Errors only toast when still current. |
| Cross-user cart leak on the session-expiry path (QWEN F2, HIGH) | `hcrm-session-expired` handler now resets `this.cart` (+`_cartImportedLegacy`) exactly like `logout()`/`switchAccount`'s flagged branch. |
| Vector-search lost its `le=100` ceiling (QWEN F3, KIMI F6, GLM F8) | Explicit `limit` is bounded again (`ge=1, le=500`). The **no-limit default stays rank-everything by design** (kiosk-scale catalogue — the PR's headline feature); the Query description states it, and a test pins both the ceiling and the no-limit contract. |
| 12-digit auto renders a different GTIN (GLM F3, KIMI F3) | Disambiguation: 12 digits with a valid UPC-A check digit → `upca` (renders exactly what was typed, informational stderr note); otherwise EAN-13 body + warning as before. |
| Non-ASCII digits → raw `KeyError` traceback (QWEN F4) | `body.isascii() and body.isdigit()` guard (also under `--force`). |
| `--check` could never print INVALID; `--format` silently swallowed (KIMI F4, QWEN F8) | `CheckDigitError(ValueError)` carries body/symbology/expected; `--check/--analyze` print the structured INVALID report (stdout) and still exit 1. `--format` default is now `None`: an explicit `--format`/`--output` writes the file, only the flag-less case is print-only (with a stderr notice). |
| SVG/PNG geometry disagree (QWEN F9) | Both canvases `height + 24`; PNG bars `y1 = height - 1` (PIL rects are corner-inclusive). Test pins pixel interchangeability. |
| Deprecated `HTTP_422_UNPROCESSABLE_ENTITY` (QWEN F7) | → `HTTP_422_UNPROCESSABLE_CONTENT`. |
| "Persistent" banner auto-dismissed after 6 s (KIMI F2) | Timer removed — the save-then-switch banner is now genuinely persistent (click-dismissible), as its comment always claimed. |
| Flagged account dead-end copy on `#/purchases` (QWEN F10) | Empty state now says "change your password" for flagged accounts instead of instructing an impossible purchase. |

## Test gaps closed

- `test_items.py`: no-limit rank-all contract (every item, similarity-sorted);
  `offset`-without-`limit` 422 (+ sanctioned `limit&offset` 200); ceiling
  422 at 501 / 200 at 500.
- `test_items_suggest.py`: `by_semantic` still slices to 5 (>5 matches).
- `test_frontend_contract.py`: `#/purchases` five-piece wiring (nav link,
  view mapping, section, watcher load, mounted load); explicit `limit=5` on
  the similar-items fetch; cart reset in the session-expired handler; the
  generation guard itself.
- `test_barcode_render.py`: 12-digit disambiguation both ways; EAN-8/UPC-A
  round-trips; non-ASCII rejection (also under `--force`); INVALID report
  via `--check`; `--check --format` writes the file / plain `--check`
  doesn't; SVG/PNG geometry agreement.

## Judgement calls (recorded)

- **Frontend no-limit stays.** QWEN suggested `limit=200` on the SPA fetch;
  rejected because rank-all is the feature's point and the catalogue is
  kiosk-scale. The backend ceiling bounds *explicit* requests; if the shop
  outgrows this, the fix is a cap on the no-limit path, not the frontend.
- **`--check` exits 1 on INVALID.** The report is the point, but a validator
  must not report success for an invalid code (KIMI left the exit code open).
- **`docs/plans/` exempted from the branch-name tripwire**
  (`test_metadata_and_ops.py`). The PR-34 review files parked there name
  `feature/ux-fixes-and-barcode` in prose; the directory is process-notes
  (council proposals/reviews, coordinator syntheses) — the same "exists to
  talk about the work" class as PLAN*/REVIEW*/NOTES, never shipped docs.

## Not acted on (accepted as-is)

- GLM F7 (no deterministic path for <4-char internal codes after the scan-box
  removal): documented tradeoff; the search box still finds the SKU.
- KIMI F6 note about SQLite 999-bind-var limit: the fixture build links a
  modern SQLite; revisit only if a hosted deployment appears.

---

# Round 2 (`pr34-r2-review-{GLM,KIMI,QWEN}.md`, reviews of `925e2ac`)

Verdicts: **GLM APPROVE · QWEN APPROVE · KIMI REQUEST CHANGES** (one HIGH,
one MEDIUM). All three confirmed every round-1 finding closed by execution
(harnesses, live servers, CLI batteries, independent spec transcriptions).

## Round-2 findings → fixes (second follow-up commit)

| Finding (reviewer) | Fix |
|---|---|
| **HIGH** raw-mode 401 path (`handleSessionExpired()`, 7 call sites) did not reset the cart — the cross-user leak persisted there (KIMI N2) | `handleSessionExpired()` is now THE canonical 401 teardown (tokens + user + cart + `_cartImportedLegacy` + overlays + route); the `hcrm-session-expired` event handler delegates to it, so the two expiry paths cannot drift. |
| **MEDIUM** an error on a *superseded* request abandoned the fresh reload — silent empty grid, dead sentinel (KIMI N1, QWEN N1, GLM N3) | try/catch moved inside the generation loop: a stale rejection `continue`s (refetches fresh); only a *current* error toasts and stops. Pinned by a real behaviour harness, `tests/js/loadmore.test.mjs` (evals the shipped source; demonstrated failing against the round-2 defect). |
| LOW `--check --force` printed INVALID but exited 0 (GLM N1, QWEN N2) | `_print_report` returns validity; exit code follows the verdict, force or not (invalid + explicit `--format` still writes the file — that is force's purpose — but exits 1). |
| LOW `--check -o file` on an invalid code silently wrote nothing (QWEN N3) | stderr notice "no file written (the code is invalid)". |
| LOW `-o x.png` without `--format` wrote SVG bytes (GLM N2, pre-existing) | format inferred from the output extension; explicit `--format` wins; unknown extension falls back to svg. |
| Defeatable pin: the watcher regex lazily matched a later definition (KIMI §3, GLM §3) | brace-matched `_method_body()` extraction in pytest (probe-verified: deleting the watcher's `loadMyOrders()` now fails the pin). |

Disagreement resolved: QWEN called the retained
`clearTimeout(this._greetingTimer)` in switchAccount dead code; KIMI is
right — it kills the 4 s "Hi" timer started by `saveSession` (which
switchAccount calls at step 4) and would otherwise wipe the persistent
banner. Kept.

Accepted as-is (INFO, no action): flagged-switch path skips the
save-reassurance banner (KIMI N3, pre-existing); `--check` on a body input
reports the computed full code (displayed as such); ≤999-bind-var SQLite
builds (KIMI F6 residual); `docs/plans/` tripwire exemption scope
(GLM N4/QWEN: tight enough, content-blind by the same trust as the
PLAN/REVIEW/NOTES prefixes).

## Round-2 test additions

- `tests/js/loadmore.test.mjs` (node --test, CI-gated): 5 interleavings
  against the real `reloadItems`/`loadMore` source — stale resolve,
  stale reject, current error, semantic toggle mid-flight, double reload.
- `tests/test_barcode_render.py`: `--check --force` exit code,
  invalid-with-output notice, extension→format inference.
- `tests/test_frontend_contract.py`: brace-matched method extraction
  (honest pins), session-expiry delegation pin, both-path generation
  guard pin.

---

# Round 3 (`pr34-r3-review-{GLM,KIMI,QWEN}.md`, focused verification of `26d1ed1`)

**Verdicts: GLM APPROVE · KIMI APPROVE · QWEN APPROVE — council converged,
PR #34 is review-clean at `26d1ed1`.**

All five verification streams closed by execution: KIMI's two blockers
(canonical 401 teardown shared by both expiry paths; stale-reject refetch),
GLM's CLI residuals (exit-code-follows-verdict under `--force`,
extension→format inference), QWEN's adversarial repros (all re-run, incl. a
new stale-reject-then-current-error chain — exactly one toast, no retry
spin), and F2/F10 hold-through-refactor. The reviewers independently
re-verified the new Node harness fails against the round-2 defect (GLM ran
it against `925e2ac:static/app.js`: 4 pass / 1 fail) and that the
brace-matched pins are deletion-sensitive (KIMI probes A–D).

Remaining INFO notes (recorded, no action): brace-naive `_method_body`
counting (no current pinned body contains a brace in a string); duplicate
"Session expired" toast on the throwing path (identical text, single-slot
toast — invisible); flagged-switch path skips the save-reassurance banner
(pre-existing, adjacent).

---

# Close-out

PR #34 merged into `main` as `da56674` (merge commit, GitHub API) after:
three-round council review (final: 3× APPROVE at `26d1ed1`), CI green on the
head commit (pytest forward+reverse, browser gate, self-hosted load+smoke),
and the review record posted to the PR
(https://github.com/AlbertoGonzalezOlmos/Inventory-management/pull/34#issuecomment-6233714588).
Post-merge verification on local `main`: 712 passed, `node --test` 47 passed.
