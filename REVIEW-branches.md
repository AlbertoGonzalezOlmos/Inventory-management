# Branch review — state and remaining work (all branches)

> Independent review of every branch in `origin`, verified against the actual
> trees (not just the branch's own docs). Method: read the diffs and the
> branch-local review/orientation documents, ran the full suite on every
> feature branch, checked ancestry and merge-base against `main`, and grepped
> for open items. Date of review: 2026-10-03.

## 0. Topology (measured)

`main` = `fbecbe9` (merge of PR #1, `round5-remediation`); it has not moved
since all scanner branches split from it. The four live feature branches form
one **linear, cumulative stack**:

```
main fbecbe9
  └── feature/opticon-hardware-layer      e512f0f   +1 commit
        └── feature/opn2001-scanner-connection  b37269f   +3 commits
              └── feature/opticon-m10-scanner    cbfad87   +10 commits
                    └── refactor/table-driven-migrations  788e7be   +1 commit
```

Because each branch contains its predecessor as an ancestor, **the only
conflict-free merge order is the stack order**. The `app/main.py` migration
anchor (three branches each appending an `if` block) was the reason the
`refactor/table-driven-migrations` branch exists; that branch now removes the
conflict by construction (see §5). `origin/archive/m10-qr-eeeeb4e` and the
tags `archive/m10-4755422`, `archive/opn2001-b2ed226` are superseded
pre-round-5 iterations (see §6).

### Test evidence (executed in throwaway worktrees, no `./data` touched)

| Branch | Collected | Result |
|---|---|---|
| `feature/opticon-hardware-layer` | 131 | 130 passed, 1 skipped |
| `feature/opn2001-scanner-connection` | 226 | 225 passed, 1 skipped |
| `feature/opticon-m10-scanner` | 291 | 290 passed, 1 skipped |
| `refactor/table-driven-migrations` | 298 | 297 passed, 1 skipped |

The one skip is `tests/test_scripts_contract.py:214` — "no ./data/hcrm.db in
this checkout" (it only runs where a live database file exists); no failures
anywhere.

A **trial merge in stack order** (executed in a throwaway clone) is
conflict-free at every step, and the resulting tree is **identical** to the
`refactor/table-driven-migrations` tip (`git diff` empty).

---

## 1. `round5-remediation` — already merged, nothing to do

- Tip `df76b86`, merge-base with `main` is its own tip; `0` ahead, `1` behind
  (the merge commit itself). PR #1 is merged, so this branch is finished.
- **Action:** none, other than deleting the branch if desired.

## 2. `feature/opticon-hardware-layer` — foundation, merge first

**Purpose.** One canonical home for all non-model-specific Opticon facts:
`scripts/opticon_detect.py` (the single VID:PID personality map + host/WSL2
discovery), `scripts/scanner_hid.py` (USB-HID reception),
`scripts/udev/99-opticon-scanner.rules`, `docs/opticon-hardware.md`, plus the
tripwire `tests/test_metadata_and_ops.py::test_shipped_artifacts_never_name_a_git_branch`.

**State.** Self-contained and correct; 130 passed. The `065A:A001` HID
personality is deliberately hedged as `confirmed: False` with a test that
fails if the hedge is stripped.

**What is left:**
1. **Merge this branch first** (PR). `opn2001` and `m10` both consume its map
   and HID tooling; out-of-order merging reintroduces the duplicated hardware
   facts the branch removed.
2. **Hardware step (not blocking merge):** scan the *USB COM Port* sheet on
   the physical `HID\VID_065A&PID_A001` device to prove it re-enumerates as
   `065A:A002`, then flip `confirmed` and record the dated result in
   `docs/opticon-hardware.md` §1/§6. Cheaper discriminator: a device that
   reads a QR code cannot be the 1D-laser OPN-2001.
3. No code work outstanding.

## 3. `feature/opn2001-scanner-connection` — OPN-2001 + catalogue identity

**Purpose.** The OPN-2001 (`065A:0009`) connector (`scripts/opn2001.py`:
serial + raw-libusb backends, RBBV protocol, `detect`), the device-agnostic
barcode/GTIN layer (`app/barcodes.py`, `items.barcode`,
`GET /api/items?barcode=`, API normalisation/uniqueness/validation), the
read-only intake classifier (`scripts/scan_intake.py`), `docs/barcodes.md`,
`docs/scanner-opn2001.md`, and the repository orientation `NOTES.md`.

**State.** 225 passed. Barcode logic is pinned by known-answer vectors;
`scan_intake` opens the DB read-only (`mode=ro` + `PRAGMA query_only`).

**What is left:**
1. **Merge after `opticon-hardware-layer`** (it is the PR the M-10 review
   calls "PR #2"; the M-10 branch's P5 fix depends on `app/barcodes`).
2. **Hardware verification (device-dependent):** the OPN-2001 guide flags that
   reference implementations disagree on the data-response CRC scope
   (`docs/scanner-opn2001.md` §4.4/§7). Capture a hex dump from a real unit and
   settle the CRC on-device; the code keeps records with a warning today.
3. **Optional dependency is documented, not declared:** `--backend usb`
   (the WSL2 path) needs `pyusb`, used via
   `uv run --with pyusb python scripts/opn2001.py …`. That keeps `uv.lock`
   frozen/offline by design — do not add it to `pyproject.toml` without a
   deliberate decision.
4. No known code defects.

## 4. `feature/opticon-m10-scanner` — M-10 driver, bridge, QR badge

**Purpose.** The bidirectional M-10 path (`app/scanner/` protocol/transport/
driver), `scripts/scanner_bridge.py`, `docs/opticon-m10.md`, QR-badge login
(`app/qrbadge.py`, vendored Nayuki encoder, `/api/auth/qr-login`,
`users.qr_badge_hash`), and `REVIEW-m10.md`.

**State.** 290 passed. The branch's own review found P1–P7; the follow-up
commits fix P1–P7 with tests:

- **P1** stock corruption from fuzzy match — fixed (exact GTIN/SKU only;
  `--fuzzy` refused with `--stock-in/--stock-out`).
- **P2** any HTTP error killed ingestion (zombie bridge) — fixed (`api()`
  returns errors, per-scan reporting, `_deliver()` catches `BaseException`,
  `--relogin` on 401).
- **P3** two readers racing one port / dropped interleaved scans — fixed
  (single reader thread, waiter demux; the old "drops scans" test was deleted
  and replaced).
- **P4** wrong-device hazard — fixed (strict `065A:A002` filter via the shared
  map; `--any-port` opt-in).
- **P5** "barcode in SKU" conflict — fixed (identity via `app.barcodes` +
  `?barcode=`).
- **P6** hygiene — mostly fixed (`--password` warning, stock-clamp reporting,
  termios restore + exclusive `flock`, `SerialOpenError` assertion).
- **P7** `A001` overclaim — fixed once, in the canonical hardware doc.
- The QR-badge commit was reviewed separately (no exploitable defect; four
  coverage gaps now pinned in `tests/test_qr.py`, 16 tests).

**What is left:**
1. **Merge after `opn2001`** (P5 is a semantic dependency). In stack order the
   merge is conflict-free (verified by trial merge, §0); the documented
   `app/main.py` migration-anchor conflict only appears on *out-of-order*
   merges or a rebase onto a `main` that already carries a sibling branch's
   migration block — the situation `refactor/table-driven-migrations`
   exists to eliminate.
2. **P6, still open by design:** `scanner_bridge` stock adjustment is
   read-modify-write (`GET` then `PATCH`), so two concurrent bridges can lose
   an increment. The fix is a dedicated
   `POST /api/items/{id}/stock-adjust` endpoint with SQL-side arithmetic
   (`stock = stock + ?`) — **no such endpoint exists on any branch**. Pair it
   with the OPN-2001 layer's planned `POST /api/stock-count`.
3. **P6, documented not changed:** `ScanParser` `.strip()`s frames, altering
   payloads with legal leading/trailing spaces (Code 128). Fine for GTINs;
   documented.
4. **Hardware Phase 4 (device + printed sheets):** confirm `Z1`/`Z2` ACK/NAK/
   ESC behavior, the menu-envelope ACK, the CR suffix, and buffered-mode
   interleaving during an ACK window on a real port; record as a dated
   addendum. Also settles §2's `A001→A002` question.
5. **Test gap found in this review's re-check:** `--relogin` (P2's one-retry
   on 401, `scanner_bridge.call()`) has **no test anywhere** — `git grep -i
   relogin` over `tests/` is empty. `REVIEW-m10.md` Phase 5 promised bridge
   unit tests for the "token-expiry path" and that is the one item of the
   phase that did not land (the find_item matrix and the callback-resilience
   pty tests did). The same applies to the bridge's QR-badge branch
   (`badge_login()` / `BADGE_PREFIX`, guide §6a): the *API* is tested in
   `tests/test_qr.py`, but the bridge's dispatch on `HCRM1:` payloads is not.
   **Add bridge unit tests for the 401-retry and the badge path** — both are
   monkeypatchable the same way `test_unreachable_server_…` is.

## 5. `refactor/table-driven-migrations` — migration shape fix

**Purpose.** Replaces the sequential `if`-block `_migrate_schema()` with an
append-only `COLUMN_MIGRATIONS` / `INDEX_MIGRATIONS` table, so three branches
adding columns no longer collide on the same lines. Adds a strict SQL
identifier guard and `tests/test_migrations.py` (7 tests against legacy SQLite
files, including a planted `bad; DROP TABLE users--` entry).

**State.** 297 passed. Behaviour is claimed preserved; the tests exercise
pre-scanner and pre-W4.0 databases, idempotency, missing tables, and the
identifier guard.

**What is left:**
1. **Documentation drift (found in this review):** `NOTES.md` §4.1 still says
   `_migrate_schema()` is a "hand-rolled column migration". The refactor commit
   changed only `app/main.py` + `tests/test_migrations.py`, so the orientation
   doc now describes the old shape — the exact shape that caused the repeated
   conflicts. **Add a NOTES.md §4.1 update** describing the table and the
   append-only rule.
2. **Process:** this branch is a superset of the whole stack. Either
   (a) merge §2–§4 first and rebase this to a clean `+2 files` cleanup, or
   (b) merge it as one integration PR, which collapses the layered history.
   The DAG makes (a) the intended shape. If `main` advances before this
   merges, its only textual conflict is `app/main.py`, which this commit
   already refactors.
3. No functional defects found in the migration table.

## 6. Archived branches/tags — superseded

`origin/archive/m10-qr-eeeeb4e` (`eeeeb4e`, based on pre-round-5 `3776405`,
16 behind `main`) and the tags `archive/m10-4755422`, `archive/opn2001-b2ed226`
are earlier diverging attempts. Their content was replayed onto the
round-5-merged stack (M-10 QR as `2071668`, OPN-2001/barcodes as `a0cebbf`/
`b37269f`). They are not merge candidates.

**Action:** delete the archived remote branch/tags, or leave as read-only
history. Do not base new work on them.

---

## 7. Repo-wide open items (from `PLAN-v2.md` "Still open", not branch-specific)

These are not tied to a scanner branch, but a merge plan should not pretend
the tree is "done" without listing them:

1. **W1.3 — GitHub Support purge** (owner action). The public remote still
   serves pre-rewrite blobs (old admin hash, three plaintext tokens), inert by
   design but present. Needs a "remove sensitive data" request; the gate is
   `git fetch origin 07d879e…` failing.
2. **W6.2 — `resetMemberPassword()` uses `prompt()`**, so an admin-issued
   temporary password is typed/displayed in plaintext. Replace with the
   existing modal + `type=password`.
3. **W7.2 — CSP endgame / vendor swagger-ui** (deferred): precompiled Vue
   render functions to drop `'unsafe-eval'`, and vendored swagger-ui so
   `/docs` works offline and can carry a CSP.
4. **D6 — `HCRM_EMBED_DISABLED=1`** interface, still deferred (CI uses
   `HCRM_EMBED_MODEL=bogus/model`).
5. **Runner-side CI** (`setup-uv`, puppeteer install, cache keys) can only be
   verified by the first PR run.
6. README "Tracked issues" remain open by decision: no login rate limiting,
   register enumeration oracle, tokens in `localStorage`, no refresh tokens,
   server-side checkout, `'unsafe-eval'`.

## 8. Recommended merge sequence

1. `feature/opticon-hardware-layer` → PR, merge.
2. `feature/opn2001-scanner-connection` → rebase (no-op today) → merge.
3. `feature/opticon-m10-scanner` → rebase → merge (conflict-free in this
   order; proven by trial merge).
4. `refactor/table-driven-migrations` → rebase → merge (plus the NOTES.md §4.1
   update from §5). Merging 1–4 in order reproduces the refactor tip's tree
   exactly.
5. Follow-ups: bridge tests for `--relogin` and the badge path (§4, item 5), the
   `stock-adjust`/`stock-count` endpoints (P6), hardware verification
   addenda, W6.2/W1.3/W7.2.

---

## 8a. Round-6 update (2026-10-03) — items completed on `round6/scanner-completion`

Three of this plan's code items are now done in **PR #3**
(`round6/scanner-completion` → `refactor/table-driven-migrations`), 316
passed / 1 skipped forward and reverse:

- §5, item 1 (NOTES.md §4.1 drift) — fixed; the doc now describes
  `COLUMN_MIGRATIONS`/`INDEX_MIGRATIONS` and the append-only rule.
- §4, item 5 (bridge `--relogin` + badge tests) — fixed; the closures moved
  to module level with injected collaborators and got 7 tests.
- §4, item 2 (`stock-adjust` endpoint, P6) — fixed;
  `POST /api/items/{id}/stock-adjust` applies the delta inside one targeted
  `BEGIN IMMEDIATE` (raw pooled connection, isolation level restored — the
  sanctioned exception to the global-hook ban, now recorded in NOTES.md §9),
  the bridge calls it, and a 4×25-writer race test pins the lost-update
  regression.

Still open and unchanged: hardware verification (§2/§3/§4, item 4), the
`stock-count` companion endpoint, `ScanParser`'s documented `.strip()`, and
the repo-level W1.3/W6.2/W7.2 items.

---

## 10. PR review (2026-10-03) — open PRs #2 and #3, with executed evidence

PR #1 (round-5) is merged. Two PRs are open: **#2**
(`feature/opn2001-scanner-connection` → `main`) and **#3**
(`round6/scanner-completion` → `refactor/table-driven-migrations`).

### F1 — MEDIUM, demonstrated: concurrent `PATCH /api/items/{id}` with a
duplicate barcode returns **500**, not 409 (PR #2's code, inherited by the
whole stack)

`create_item` maps the unique-index violation to 409 (`except IntegrityError`
→ "SKU or barcode already exists (concurrent create)") and `members.py` has
four such guards — but `update_item`, which PR #2 taught about the new
`items.barcode` unique index, has **none**. Reproduced against a live server:
two threads PATCH two different items to the same GTIN → `[200, 500]`, server
log `sqlalchemy.exc.IntegrityError: UNIQUE constraint failed: items.barcode`.
No corruption (the loser rolled back: its barcode stayed NULL), but the wrong
status class, a traceback in the log, and a client that cannot tell "resolve
the conflict" from "the server broke". Reachable without a tight race: under
WAL the request's earlier auth read can hold a snapshot where the other
item's barcode is not yet visible, so the app-level pre-check passes and the
commit fails. `tests/test_items_barcode.py::test_patch_barcode_conflict`
covers only the sequential pre-check path.

### F2 — PROCESS: PR #2's description is stale and its evidence is not from
its head

The body cites commits `033b556`, `b2ed226`, `f79ae74` — **none** is an
ancestor of the PR head (they are the archived pre-round-5 attempts;
`b2ed226` is literally tag `archive/opn2001-b2ed226`). The PR actually
contains `e512f0f`, `4dd31db`, `a0cebbf`, `b37269f`. It claims "161 passed
(91 new)"; the head collects 226 and runs **225 passed + 1 skipped**
(measured). This is the decay REVIEW-m10.md §5.4 warns about: mechanics
claims belong in a command, not a sentence.

### F3 — PROCESS: the documented merge plan cannot be executed as written

`feature/opticon-hardware-layer` has **no PR**, yet its tip `e512f0f` is an
ancestor of PR #2's head — so PR #2's diff already contains the entire shared
hardware layer (`opticon_detect.py`, `scanner_hid.py`,
`docs/opticon-hardware.md`, the udev rules). `feature/opticon-m10-scanner`
and `refactor/table-driven-migrations` have **no PRs either**, while PR #3's
*base* is the migrations branch. Net: the stack has PRs at layer 2 and layer
5 only, and PR #3 cannot merge until a base that has no PR of its own merges.

### F4 — LOW, latent, already fixed upstream: `_migrate_schema()` raises on a
database with no tables

`OperationalError: no such table: users` (measured). Pre-existing on `main`
(round-5 code); PR #2's `items.barcode` block repeats the missing
`if "<table>" in tables` guard that its own `auth_tokens` index block has.
Unreachable through `_startup` (`create_all()` runs first) — only a direct
call, e.g. a future ops script. `refactor/table-driven-migrations` fixes it
(measured: no exception; pinned by `test_migrations.py`).

### F5 — MEDIUM, mine, found and FIXED in this review (PR #3, `72cdddf`)

`adjust_stock` checked out a **second** pooled connection for the guarded
write while the auth dependency's session connection was still checked out:
2 of the pool's 15 per request (a plain GET holds 1). Measured: 16 concurrent
adjustments saturated all 15 — a 17th would block on the 30 s pool timeout
and 500. Fixed by releasing the auth read's connection first; measured peak
1 per request, 13 under 16-way concurrency, totals still exact. Pinned by
`test_adjust_holds_exactly_one_pooled_connection`, demonstrated **failing**
(peak == 2) with the fix removed.

### F6 — LOW, mine, fixed: a fix committed to a detached HEAD

`72cdddf` was made in a scratch worktree that was on a detached HEAD, so the
pushed branch — and PR #3 — still showed 3 commits. Caught by comparing
`origin/round6/scanner-completion` with the worktree HEAD; branch
fast-forwarded and pushed (PR #3 now 4 commits, +520/−41).

### F7 — LOW, residual (optional hardening)

If `BEGIN IMMEDIATE` itself times out after `busy_timeout` (5 s) under
sustained write contention, `adjust_stock` surfaces a 500 "database is
locked" — the class the repo already documents. Optionally map that
`OperationalError` to 503 with a retry hint.

### F8 — INFO

The bridge now *requires* the stock-adjust endpoint (against an older server
a stock scan yields a per-scan `ApiError` report, not a crash), and
`StockAdjustIn` bounds `delta` to ±100 000 — neither is stated in the guide.

### Verified good during this review (no action)

- Fresh-DB and migrated-DB schemas are **equivalent**: both produce
  `CREATE UNIQUE INDEX ix_items_barcode ON items (barcode)`.
- `scan_intake.py` reads a **live WAL** database with uncheckpointed data
  read-only and correctly: cross-format identity works (UPC-E `425261` →
  the item filed as GTIN-14 `00042100005264`), invalid check digit → exit 3.
- `opn2001.py read` is non-destructive by default (`--clear-after` opt-in).
- POST's duplicate-barcode race correctly returns 409.
- PR #3 after fixes: **317 passed + 1 skipped**; `smoke_test
  --allow-degraded --fast-hashing` → 17 passed, exit 0.

### Fix plan

| # | Action | Where | Effort |
|---|---|---|---|
| 1 | **F1**: wrap `update_item`'s commit in `except IntegrityError → rollback + 409` (mirror `create_item`); add a barrier-based concurrency regression test asserting `{200, 409}` and never 500 | `feature/opn2001-scanner-connection`, then rebase the stack so m10/migrations/round6 inherit it | ~30 min |
| 2 | **F3**: owner decision — layered PRs (repo rule "one workstream = one PR") with chained bases: hardware-layer→main, opn2001→hardware-layer, m10→opn2001, migrations→m10, round6→migrations; **or** one integration PR. Then open the missing ones | GitHub | ~15 min |
| 3 | **F2**: rewrite PR #2's body from the real head (commit list, measured 225+1, and an explicit "includes the shared hardware layer" note) | PR #2 | ~15 min |
| 4 | **F7/F8**: optional — 503 on lock timeout; document the ±100 000 bound and the bridge's endpoint requirement in `docs/opticon-m10.md` §6 | round6 branch | ~20 min |
| 5 | **F4**: none — fixed by the stack; just merge in order | — | — |
| 6 | After merges: delete `archive/m10-qr-eeeeb4e` + the three archive tags and the merged branches | GitHub | ~5 min |

---

## 9. Addendum — what the first pass of this review got wrong

Re-checked 2026-10-03 (trial merges executed, skip identity identified, test
coverage grepped). Two claims were corrected above; recorded here so the next
review does not re-make them:

1. **"Expect that one hunk" when merging the M-10 branch** — wrong for the
   in-order case. The branches are a linear stack on an unmoved `main`, so
   stack-order merges are clean and the final tree equals the refactor tip
   (measured, §0). The conflict is real only for out-of-order merges or
   rebases — the scenario the migration refactor removes. Mechanics belong in
   an executed command, not a prediction (the same lesson `REVIEW-m10.md` §5.4
   recorded for its own stale branch-mechanics section).
2. **"`--relogin` is covered by unit tests"** — false; there is no relogin
   test on any branch, and the bridge's badge dispatch is likewise untested
   at bridge level. Both are now §4, item 5 action items. The driver-level P2 fixes
   (callback raising `SystemExit`, broken `on_error`, interleaved delivery,
   exclusive open, termios restore) *are* pinned by pty tests in
   `tests/test_scanner.py` — that part of the original claim was correct.
---

## 11. The stacked-PR structure (created 2026-10-03)

The stack is now represented as **six PRs with chained bases**, so each PR's
"Files changed" is exactly one workstream and no PR's diff contains another's
work. The GitHub-reported counts were verified against `git diff --shortstat`
per layer: all six match the local measurement.

| Layer | PR | Head → base | Delta | Content |
|---|---|---|---|---|
| 0 | #4 | `docs/branch-review-record` → `main` | 1 file, +394 | this document |
| 1 | #5 | `feature/opticon-hardware-layer` → `main` | 8 files, +1 332 | shared Opticon hardware layer |
| 2 | #2 | `feature/opn2001-scanner-connection` → layer 1 | 14 files, +4 260/−5 | OPN-2001 + GTIN-14 identity + intake |
| 3 | #6 | `feature/opticon-m10-scanner` → layer 2 | 22 files, +4 190/−9 | M-10 driver, bridge, QR-badge login |
| 4 | #7 | `refactor/table-driven-migrations` → layer 3 | 2 files, +251/−43 | table-driven migrations |
| 5 | #3 | `round6/scanner-completion` → layer 4 | 9 files, +520/−41 | round-6 completion |

**Why chained bases rather than five PRs all targeting `main`.** With a common
base every PR's diff is cumulative — layer 5 would show ~10 000 changed lines —
so a reviewer cannot see what a layer actually did, and the review record that
belongs to a layer (`REVIEW-m10.md`, this file) cannot be matched against the
diff it describes. Chaining preserves the repo's "one workstream = one PR"
rule and makes the merge order explicit in the PR graph itself.

**Merge procedure** (repeated in every PR body):

1. Merge in layer order 1 → 5. Layer 0 is docs-only and independent.
2. After a parent merges, **retarget the child PR's base to `main`** (the
   *base* dropdown, or `PATCH /pulls/N` with `{"base":"main"}`). The child's
   diff does not change, because its parent is now in `main`.
3. **Do not delete a parent branch before its child has been retargeted** —
   deleting the base branch of an open PR closes that PR.
4. Delete branches (and the `archive/*` branch and tags, §6) only once the
   whole stack has merged.

**Also corrected while building the stack (finding F2):** PR #2's description
cited three commits that are not ancestors of its head (`033b556`, `b2ed226`,
`f79ae74` — the archived pre-round-5 attempts) and a test count (161) that
cannot have come from it. It is rewritten from the real head `b37269f` with
measured numbers (225 passed + 1 skipped), its base moved from `main` to
layer 1, and it now carries the F1 finding with its reproduction so the
defect is visible where the fix belongs.
