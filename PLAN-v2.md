# Round-5 Consolidated Work Plan

Supersedes the *work ordering* of `PLAN.md` (round 4), which stays in the tree as the
historical record and as the source of findings A–H / P-1…P-4. This document merges
`PLAN.md` with the three engineer reviews and my own verification pass, corrects the
claims that did not survive contact with the code, and turns everything into
assignable items with executable gates.

**Round-5 review verdict: ACCEPTED**, with four refinements (R1–R4) and six decisions
(D1–D6), all folded in below. The reviewer independently reproduced every
load-bearing claim before accepting (see §0). Assignments confirmed: **A = W1 + W2**,
**B = W3 + W5.3**, **C = W4.1 + W4.2 + W5.2**, **D = W4.3 + W4.4 + W4.5**; reviews
rotate A↔C, B↔D; W6/W7 pair-review across A/B. B's browser artifacts (B2/B4/B14
reproductions) stand as the independent reproduction for C's review of A's W2 PR.
Process notes: this file is committed as its own commit (ground rule 4), and a
mid-session working-tree regression (`README.md` found deleted — likely a rename
rehearsal leak) was caught and restored during the review pass; keep the tree clean
between commits.

Ground rules for this round (adopted from PLAN.md, strengthened):

1. **No claim about browser-facing behaviour is "verified" without an executed
   artifact** (`scripts/ui_check.py`, or a headless-Chrome run pasted into the PR).
   `curl -I` is not a UI check. Findings A–D were all verification theater.
2. **No claim about a script's exit code is "verified" without printing `$?`**, and
   every script must be demonstrated failing (not just passing) at least once.
3. **Nothing runs against `./data`** — always a scratch `HCRM_DATA_DIR`. The runtime
   DB is currently tracked in git (§W1), so any local run otherwise dirties the tree.
4. One workstream = one PR = one review pair. No mixed hygiene/feature diffs.

---

## 0. Verified baseline (measured, not read — do not re-litigate)

Everything below was executed against `main` @ `07d879e` on this machine.

| # | Measurement | Result |
|---|---|---|
| B1 | `uv run pytest -q` | **37 passed in 2.11 s** (REVIEW #2 `pythonpath` fix works) |
| B2 | Headless Chrome on `/` with the shipped CSP | DOM **557 B**, `v-cloak` present, **no** `data-v-app`, `Uncaught EvalError: … 'unsafe-eval' is not an allowed source of script: script-src 'self'`. With `[v-cloak]{display:none}` the page is **blank** → finding **A** confirmed |
| B3 | Headless Chrome on `/docs` | 3 violations: jsdelivr CSS, jsdelivr JS, inline bootstrap `sha256-QOOQu4W1oxGqd2nbXbxiA1Di6OHQOLQD+o+G9oWL8YY=` → finding **B** confirmed |
| B4 | Same browser check on a `/tmp` copy with the PLAN-item-2 CSP (`unsafe-eval`, `object-src 'none'`, `base-uri 'self'`, `form-action 'self'`, docs exempt) | `/` → DOM **1209 B**, `data-v-app` present, `v-cloak` gone, 2 inputs + button + login copy rendered; `/docs` → **50 865 B**, swagger-ui rendered, **0 console messages**; CSP header present on `/`, absent on `/docs`. **The proposed fix works** |
| B5 | `load_test.py` vs a scratch server, admin flagged | pass, exit **0**, restores `changeme` |
| B6 | same, admin **not** flagged (after B5) | prints `LOAD TEST PASSED`, exit **1** → finding **D** confirmed |
| B7 | same, `_run_load` stubbed to return `False` | prints failure, exit **0** → finding **C** confirmed (the discarded return is `scripts/load_test.py:71`) |
| B8 | Post-B5 probe: login `changeme` | 200, `must_change_password=False`, `/api/items` 200, `/api/members` 200 → finding **E** confirmed |
| B9 | Staff-created account ("Temporary password" in the UI) | `must_change_password=False`, `/api/items` 200 → finding **G** confirmed |
| B10 | `PATCH /api/members/{id} {"password":"changeme"}` | **200**. Then `change-password changeme→changeme` → **204 and clears the flag**; that account keeps full API access on the well-known default → the hole PLAN item 5 targets is reachable **without any script** |
| B11 | Boot on a copy of the committed `data/hcrm.db` | `must_change_password` column added, admin flagged, committed plaintext tokens → **401**, 12 items keep their embeddings, `/api/items` → 403 until change. **The upgrade path works** |
| B12 | `vector_full_scan` over NULL embeddings (raw SQL + extension 1.1.2) | all-NULL → `[]`; mixed → only the embedded row. **No error, no garbage** — this is *not* a bug, nobody needs to fix it |
| B13 | `smoke_test.py` vs a server whose model is unavailable | `TypeError: string indices must be integers` at `scripts/smoke_test.py:80`, no `[FAIL]` line, no restore → admin left on the random temp password (`changeme` → 401, temp → 200) |
| B14 | Chrome console severity, broken vs fixed build | the **fatal** `Uncaught EvalError` is logged as **`INFO:CONSOLE`**, and the **fixed** build emits a benign `INFO:CONSOLE` advisory (`[DOM] Input elements should have autocomplete attributes`) → severity-based filtering is inverted (see W2.1) |
| B15 | `git remote -v` | `git@github.com:AlbertoGonzalezOlmos/Inventory-management.git` — a **public** GitHub remote |
| B16 | fastembed cache location | `FASTEMBED_CACHE_PATH` or **`$TMPDIR/fastembed_cache`** (`fastembed/common/utils.py:64`) — *not* `~/.cache/fastembed` |
| B17 | swagger-ui-dist@5 sizes | bundle **1 585 988 B**, css **186 154 B** (standalone preset not needed by FastAPI) |

Independent re-verification (round-5 review — every row below confirmed by a second
engineer before acceptance): B15's remote is fetchable anonymously; the N1 blobs hold
the admin hash (`pbkdf2$600000$…`) and three plaintext 43-char tokens (exp 2026-10-02);
both last-admin triggers *are* in the committed DB; `git check-ignore data/hcrm.db` →
exit 1; `openapi_url=None` is necessary **and sufficient** (nulling only `docs_url`/
`redoc_url` leaves `/openapi.json` a 200); the B16 cache path; B13's `TypeError` at
line 80; and a live B14 repro on a hermetic boot — broken build: 557 B DOM,
`v-cloak`, no `data-v-app`, fatal `Uncaught EvalError` logged as `INFO:CONSOLE`; fixed
build with the W2.2 policy: `/` → 1211 B with `data-v-app` and only the benign
autocomplete advisory, `/docs` → 50 865 B (byte-exact with B4), 0 console messages.
Also dissolved: `chrome-headless-shell` v131 accepts `--headless=new` and works
without it, so W2.1's runner flags have no compatibility landmine.

Booting with `HCRM_EMBED_MODEL=bogus/model` gives a deterministic, offline,
~6 s degraded boot (warning logged, seeding proceeds with NULL embeddings). That is
the hermetic-boot trick CI can use today with zero code changes.

---

## 1. Fact-check of the three reviews

Corrections only; everything else in the three reviews is confirmed and folded into
the workstreams below.

| Claim | Verdict | Evidence |
|---|---|---|
| Eng1: "item 0 repo hygiene ✅ done (clean tree, copies committed)" | **Wrong label.** The *snapshot* happened; the *hygiene* did not. The tree is clean only because the junk was committed: `data/hcrm.db{,-shm,-wal}` are tracked, `.gitignore copy` / `LICENSE copy` / `README copy.md` are tracked duplicates, and `README.md` (what GitHub renders, and what `pyproject.toml:readme` points at) is a 91-byte stub | `git ls-files`, B15 |
| Eng2 #1: "the live `data/hcrm.db` … **no triggers**" | **Wrong.** Both triggers are present in the committed DB: `users_keep_last_admin_role`, `users_keep_last_admin_delete`. The rest of the claim (pre-migration schema, single admin, 12 embedded items) is correct, and B11 already exercised the whole upgrade path | `SELECT name FROM sqlite_master WHERE type='trigger'` on `data/hcrm.db` |
| Eng2 #3: "`.gitignore` … does not ignore `data/hcrm.db` (matches the plan's item-0 note)" | Right conclusion, wrong attribution: it **contradicts** PLAN item 0, which asserts the new `.gitignore` "still ignores the db files by name". Neither the old nor the new `.gitignore` has any `data/`/`*.db` rule (only `db.sqlite3`) | `git check-ignore -v data/hcrm.db` → exit 1 |
| Eng2 #4: "item 5 makes the scripts' restore a hard 400 → ordering dependency" | **Confirmed and strengthened.** After item 5 the restore is not merely "gated" — it is *impossible by design*, so items 3/4 must merge **before or with** item 5 or CI goes red on a healthy system. Encoded as DAG edge `W3 → W4.1` | B5–B8, PLAN P-1 |
| Eng2 #5: smoke cleanup deletes `sku == "X"` | Confirmed (`scripts/smoke_test.py:65`), and it is the *smaller* of that script's problems — see B13 and W3.3 |
| Eng3 #1/#2: item-0 claim stale, duplicate artifacts, `pyproject.readme` → stub | Confirmed, plus B15 (public remote) raises the severity | `git ls-files`, `pyproject.toml:5` |
| Eng3 #3: version drift `0.1.0` vs `0.2.0` | Confirmed: `pyproject.toml:3` = `0.1.0`, `app/main.py:113` = `0.2.0` | — |
| Eng3 #4: no model cache → ~80 MB first boot | Confirmed, and worse for CI than stated: the cache is `$TMPDIR/fastembed_cache` (B16), so the obvious `actions/cache` key (`~/.cache/fastembed`) would silently never hit | B16 |
| PLAN item 2: "`FastAPI(docs_url=None, redoc_url=None)` disables docs" | **Incomplete.** FastAPI gates all three on `self.openapi_url` (`fastapi/applications.py:1106,1121`), so with only `docs_url`/`redoc_url` nulled, **`/openapi.json` stays public** and leaks the full endpoint schema in "production" mode. Set `openapi_url=None` (which disables all three) | source read |
| PLAN item 1: ui_check asserts "zero console errors" | **Under-specified in a way that inverts the gate** — see B14 and W2.1 | B14 |
| `README copy.md` accuracy (Eng2 #2) | Confirmed, and it is false **twice** more: it still calls the CSP "strict … scripts are same-origin only" (today it is *broken*; after W2 it will carry `unsafe-eval`), and it advertises `/docs` while `/docs` requires jsdelivr — contradicting the same file's "fully offline" claim | B3, B4 |

---

## 2. New findings (not in PLAN.md, REVIEW.md or the three reviews)

| ID | Sev | Finding | Evidence | Item |
|---|---|---|---|---|
| N1 | 🔴 | **The runtime DB is committed to a public repo**: `data/hcrm.db` holds the admin's PBKDF2 hash and **3 plaintext 43-char bearer tokens** (exp 2026-10-02). They are inert today only because lookup is by SHA-256; any rollback of the hashing change makes them live sessions. `-shm`/`-wal` are tracked too | `git ls-files`, B15 | W1.2 |
| N2 | 🟠 | **Tracked WAL sidecars make the tree flap and risk stale-WAL replay.** Observed inside one session: clean → ` D data/hcrm.db-shm` / ` D data/hcrm.db-wal` → clean, from merely opening the DB read-only. "One clean diff per round" is impossible while these are tracked | B/§0, git status log | W1.2 |
| N3 | 🟠 | **ui_check's error rule is inverted as specified**: fatal CSP errors arrive as `INFO:CONSOLE`; the healthy build emits an `INFO:CONSOLE` advisory. Severity-based filtering → false negative on broken, false positive on fixed | B14 | W2.1 |
| N4 | 🟠 | **`smoke_test.py` cannot report its own failures**: an unavailable model or a failed create turns into an uncaught `TypeError`/`KeyError` instead of `[FAIL]`, and because the restore is not in a `finally`, the abort leaves the only admin on an unknown random password | B13 | W3.3 |
| N5 | 🟡 | **`_flag_default_password()` is scoped to `DEFAULT_ADMIN_EMAIL` only** — any *other* account sitting on `changeme` (reachable per B10) is never re-flagged at boot | `app/main.py:83-105`, B10 | W4.1 |
| N6 | 🟡 | **Embedding retry has no single-flight guard**: `_model_failed = False` is cleared *before* the load attempt, so when the cooldown lapses every concurrent request starts its own ~80 MB download in a request thread. PLAN item 8 fixes the blocking but not the herd | `app/embeddings.py:61-79` | W4.3 |
| N7 | 🟡 | `PRAGMA foreign_keys` is never enabled → `auth_tokens.user_id` is decorative; orphan tokens survive any delete path that forgets explicit cleanup | `app/database.py:47-56` | W4.5 |
| N8 | 🔵 | `_issue_token()` does an **unindexed full scan + write** over `auth_tokens` on every login (purge of expired rows) | `app/routers/auth.py:18-23` | W4.5 |
| N9 | 🔵 | `resetMemberPassword()` uses `prompt()` — admin-issued "temporary" passwords are typed and displayed in plaintext | `static/app.js:696` | W6.2 |
| N10 | 🔵 | `load_test.py`'s docstring claims "full-cost PBKDF2, 600k iterations" but never asserts the target's `HCRM_PBKDF2_ITERATIONS`; the 1 s p50 budget is trivially met by a misconfigured server (mine ran at 1000) | `scripts/load_test.py:1-12` | W3.2 |
| N11 | 🔵 | `/docs` depends on jsdelivr → the "fully offline" claim is false for docs. Vendoring costs ~1.77 MB (B17), comparable to the already-vendored jsPDF (366 KB) + Vue (158 KB) | B3, B17 | W7.2 (deferred) |
| — | ✅ | `vector_full_scan` over NULL embeddings is **safe** (B12). Recorded so nobody "fixes" a non-bug | B12 | none |

---

## 3. Workstreams

Size: **S** ≤ ~50 LOC, **M** ≤ ~200 LOC, **L** > 200 LOC or cross-cutting.
"Gate" is the executable acceptance test; "Evidence" is what must be pasted in the PR.

### W1 — Repo hygiene (must merge first, alone)

**W1.1 Reconcile the duplicate artifacts.** size S · deps: none
- `README copy.md` → `README.md` (then W6 rewrites its inaccurate parts); delete
  `.gitignore copy`, `LICENSE copy`. `pyproject.toml:readme` then points at the real doc.
- Until W6.1 lands, the moved README carries a one-line banner pointing at §1 for its
  known-stale claims (the `BEGIN IMMEDIATE` sentence, "strict" CSP, offline `/docs`)
  — a flagged falsehood is better than an unflagged one for the rounds in between.
- Sync the version: single source of truth. **D5 decided: `0.3.0` everywhere** — the
  upgrade note in W6.1 (all sessions invalidated, admin blocked until password change)
  is exactly the signal a version bump exists to send, and the committed tree already
  claims `0.2.0` with different behaviour (while `pyproject.toml` says `0.1.0`). Add a
  test asserting the two agree (W5.4) so the drift cannot return.
- Gate: `git ls-files | grep -c " copy"` → `0`; `test "$(python -c 'import tomllib,sys;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')" = "0.3.0"`.
- Evidence: `git show --stat`.

**W1.2 Untrack the runtime database (N1, N2).** size S · deps: none · **D1 decided: (b) rewrite history**
- **R4 (ignore rules):** append `/data/` and `/fastembed_cache/` **only** — no blanket
  `*.db`/`*.db-shm`/`*.db-wal`/`*.sqlite3` patterns, so a future *intentional* fixture
  DB (e.g. a demo seed) is never silently untracked. `git rm --cached` is superseded
  by the D1 rewrite, which drops `data/` from history entirely.
- Safe: B11/an empty scratch dir prove a fresh clone boots, migrates, seeds and
  flags without any committed DB.
- Keep `data/.gitkeep` if the directory must exist (`os.makedirs(..., exist_ok=True)`
  already handles it, so it is not required).
- History: **D1 protocol (decided)** — everyone pushes open work → one engineer (A,
  with W1) runs `git filter-repo --path data --invert-paths` → force-push → everyone
  else re-clones (never `pull --rebase` across the rewrite). Two commits, four
  collaborators: this is the cheapest it will ever be. **Back up and restore the
  working-tree `data/` across the rewrite** — it is live operator data; the rewrite
  must remove it from *history*, not from disk. Caveat for the PR record: GitHub
  retains cached views of the old blobs for a while, but the exposure is inert by
  design (hash-keyed token lookup; hash of a public default password) — hygiene, not
  incident response, and no rotation is needed.
- Gate: `git check-ignore -v data/hcrm.db data/x.db-wal` matches; `git status --porcelain` stays empty after a full local boot against `./data`.
- Evidence: the two commands above, plus a boot log showing seeding from scratch.

### W2 — The browser gate and the CSP fix (the ship blocker)

**W2.1 `scripts/ui_check.py`** [PLAN #1] size M · deps: W1 · must merge **before** W2.2
- Dependency-free: stdlib + a Chrome binary. Discovery order: `chrome-headless-shell`
  under `~/.cache/puppeteer`, `google-chrome`/`chromium`/`chromium-browser` on `PATH`,
  macOS app bundle. **Exit 3** with install instructions when none is found.
- Runner: `--headless=new --no-sandbox --disable-gpu --enable-logging=stderr
  --virtual-time-budget=6000 --dump-dom <url>`, capturing DOM + stderr.
- **Error rule (N3/B14) — this is the part that was wrong in PLAN item 1:**
  - Parse every `:CONSOLE:` line's *message text* (Chrome logs uncaught exceptions as
    `INFO:CONSOLE`, so severity is useless).
  - Fail on: `Uncaught`, `Refused to`, `violates the following Content Security
    Policy`, `Failed to load resource`, `net::ERR_`.
  - Allowlist known-benign advisories: `[DOM] Input elements should have autocomplete
    attributes`, `Third-party cookie`, deprecation notices. The allowlist is an
    explicit list in the file with a comment per entry — no regex wildcards.
- DOM assertions per route — **R1: every assertion counts OCCURRENCES** (`re.findall` /
  `grep -o | wc -l`), never lines: `--dump-dom` output is effectively one line, so a
  line-counted "≥ 2 `<input>`" always yields 1 and the compiler canary would be
  vacuously green:
  - `/` and `/#/register`: `data-v-app` present, `v-cloak` absent, `>= 2` `<input>`,
    `>= 1` `<button>`, expected copy ("Log in" / "Create your member account").
    This is the compiler canary for regression A.
  - `/docs`: `swagger-ui` present, DOM `> 20 000 B` (a blocked docs page is ~1 KB).
- Exit codes: `0` pass, `1` fail (print every offending console line + the failing
  assertion), `3` no browser.
- Optional `--base URL` (default `http://127.0.0.1:8000`), `--json` for CI annotations.
- Out of scope (say so in the docstring): no CDP, no scripted login.
- **Gate: must FAIL on the current tree and PASS after W2.2.** Both runs recorded.
  (I already have them: B2/B3 = fail, B4 = pass. Re-run locally so the artifact is
  reproducible from the committed script.)
- Evidence: the two runs' console lines + DOM sizes, and `$?` for each.

**W2.2 CSP fix** [PLAN #2] size S · deps: W2.1 · **D2 decided: exempt now, vendor in W7.2**
- (D2 rationale: coupling ~1.8 MB of vendoring to the ship blocker is exactly the
  mistake class this round eliminates; the exemption renders docs cleanly — 0 console
  messages, verified independently twice.)
- App routes: `default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self'
  'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'; object-src 'none';
  base-uri 'self'; form-action 'self'; frame-ancestors 'none'`, with a comment stating
  why `unsafe-eval` is unavoidable (full Vue build + in-DOM template → runtime
  compiler → `Function()`) and that it substantially negates the CSP's XSS value.
- Docs routes (`/docs`, `/redoc`, `/openapi.json`, `/docs/oauth2-redirect`) exempt from
  the CSP header (dev-only endpoints), prefix-matched in the middleware.
- `HCRM_DOCS=0` → `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`.
  **`openapi_url` must be included** (PLAN item 2 omits it; FastAPI gates docs on it,
  so without it the schema stays public — see §1).
- Gate: `scripts/ui_check.py` passes; `curl -sD- -o/dev/null /` shows the new header;
  `/docs` shows none; with `HCRM_DOCS=0`, `/docs`, `/redoc` and `/openapi.json` are
  all 404.
- Evidence: ui_check output + the three `curl` header dumps + the `HCRM_DOCS=0` 404s.

### W3 — Script contract: no restore, self-hosted, crash-safe

**W3.1 Shared bootstrap helper.** size S · deps: W1
- `scripts/_hcrm.py`: `call()`, `resolve_credentials()`, `bootstrap_admin()`
  (login → if flagged, change to a random password → return `(token, final_password,
  changed: bool)`), `self_host()` context manager (scratch `HCRM_DATA_DIR`, random
  free port, subprocess uvicorn, health-poll, teardown), `die(msg, code)`.
- Rationale: load and smoke currently duplicate ~40 lines and diverged (that is how
  C and D happened in one and not the other).

**W3.2 `load_test.py`** [PLAN #3, fixes C/D/E per P-1/P-2/P-3] size M · deps: W3.1 · **must merge before W4.1**
- `ok = _run_load(token) and ok` — the load phase feeds the exit code (C).
- **Delete the restore step entirely** (D, E). Final state is reported, never undone:
  if the bootstrap changed the password, print it **once, loudly, as the final-state
  warning** and exit 0 on a passing run (P-2: never print on a self-hosted success
  path; always print when the run aborts or intentionally leaves the password changed).
- **Self-host by default** (scratch data dir + random port + teardown) so the default
  path cannot touch a real deployment (P-3). External `HCRM_BASE` requires `--force`
  **and** `HCRM_ADMIN_PASSWORD` (no `changeme` default for external targets).
- Assert the target's hashing cost or stop claiming it (N10): read `/api/healthz` and
  print the effective mode; drop "600k iterations" from the docstring unless the
  server reports it. (Adding an `iterations` field to `healthz` is W4.5-optional.)
- Gates: (a) stubbed `_run_load → False` ⇒ **exit 1**; (b) two consecutive
  `--force` external runs ⇒ both exit 0 (second fed the first's final password via
  env); (c) a default self-hosted run leaves `./data` byte-identical
  (`git status --porcelain` empty + mtime/hash check); (d) `changeme` never
  authenticates after a run (B8's probe, now expected to fail).
- Evidence: all four, with `$?` printed.

**W3.3 `smoke_test.py`** [PLAN #4, fixes N4] size M · deps: W3.1 · **must merge before W4.1**
- Same no-restore contract as W3.2.
- **Never raise out of a check path**: wrap every response decode so a non-list/error
  body becomes `[FAIL] <label> (HTTP <code>: <detail>)`. B13's `TypeError` at line 80
  must become a FAIL line and a nonzero exit.
- Restore in `try/finally` is replaced by *no* restore; the abort path prints the
  final password (P-2).
- Cleanup: drop the `sku == "X"` branch (Eng2 #5), keep the `EX-TST-` prefix, and
  scope deletion to items the script created in this run where possible.
- Self-host by default; `--force` + env creds for external targets (same as W3.2).
- Gate: run against a server with `HCRM_EMBED_MODEL=bogus/model` ⇒ every semantic
  check prints `[FAIL] …(HTTP 503…)`, exit 1, **no traceback**, and the admin
  password state is reported; run against a real-model server ⇒ exit 0 twice in a row.
- Evidence: both transcripts + `$?`.

### W4 — App-side fixes the scripts exposed

**W4.1 Reject the well-known default as a new password** [PLAN #5, root fix for E] size S · deps: **W3.2 + W3.3 merged**
- Move `DEFAULT_ADMIN_EMAIL` / `DEFAULT_ADMIN_PASSWORD` from `app/main.py` to
  `app/security.py` (both routers and main need them; today they live in the
  entrypoint module).
- `POST /api/auth/change-password` and admin `PATCH {"password": …}` return **400**
  ("well-known default password; choose another") when the new password equals it
  (B10 shows both paths accept it today, and the change-password path *clears the flag*).
- **Extend the boot-time flagger (N5)**: `_flag_default_password()` must scan **all**
  users whose hash verifies against the default, not just `DEFAULT_ADMIN_EMAIL`.
  Otherwise an account already on `changeme` stays unflagged forever.
- Tests: change-to-`changeme` → 400 with the flag unchanged; admin reset to
  `changeme` → 400; a second account on the default gets flagged at boot (W5.2).
- Gate: `uv run pytest` green + the B10 probe now returns 400 twice.
- Evidence: pytest tail + the probe transcript.

**W4.2 `create_member` sets `must_change_password=True`** [PLAN #6, finding G] size S · deps: none (can run in parallel with W2/W3)
- `app/routers/members.py`: flag every staff/admin-issued account (the UI already says
  "Temporary password", `static/index.html:372`).
- **Blast radius (PLAN's own pushback is right)**: `tests/conftest.py::create_user`
  must clear the flag via a change-password call reusing the same password, so the
  suite's "created users log in with `password123`" contract (on which the concurrency
  tests' survivor re-login depends) survives.
- Gate: new test — staff-created account is flagged, blocked from `/api/items` (403),
  unblocked after change; full suite green.
- Evidence: pytest tail + the B9 probe now showing `must_change_password=True` and 403.

**W4.3 Embedding retry → single-flight background daemon** [PLAN #8 + N6] size M · deps: none
- Extract `_attempt_load()`; on failure `_get_model()` schedules **at most one** daemon
  retry thread and returns `None` immediately — no request ever blocks on a download
  after the cooldown. Sleep clamped to `>= 1 s`.
- **Single-flight, explicitly (N6)**: guard the thread hand-off with a `threading.Lock`
  and an `_retry_in_flight` flag; `if _retry_thread is None` alone is racy. Publish the
  loaded model with a single atomic assignment, and re-check under the lock so N
  concurrent requests cannot start N downloads.
- STRICT semantics unchanged (startup only).
- Gate: cooldown test rewritten (no inline retry; `_attempt_load()` recovery verified
  directly) + a new test that 20 concurrent `_get_model()` calls during a cooldown
  expiry start exactly **one** load attempt.
- Evidence: pytest tail + the new test's assertion output.

**W4.4 Race tests share the lifespan-less `_client` fixture** [PLAN #7, finding F] size S · deps: none
- `tests/test_concurrency.py`: drop `with TestClient(app)` inside the worker threads;
  use the session-scoped `_client` (no lifespan) so the suite no longer depends on
  `_startup()` being safe to run concurrently. Rationale correction from PLAN's
  pushback: today's fresh test DB already contains the column, so `_migrate_schema()`
  no-ops — the defect is coupling and fragility (16 lifespan runs per suite today:
  5×2 + 3×2), not a live coin flip.
- Gate: suite green over 10 consecutive runs **and** in reverse order — no
  randomization plugin is installed, so reverse means explicit node IDs:
  `uv run pytest $(uv run pytest --collect-only -q | grep '::' | tac)`. Zero flakes.
- Evidence: the 10-run loop output.

**W4.5 Small hardening batch** (N7, N8) size S · deps: none · optional this round
- `PRAGMA foreign_keys=ON` in the connect event + `ON DELETE CASCADE` semantics for
  `auth_tokens` — **D4 decided: document, don't rebuild.** Every token-delete path
  does explicit cleanup today (logout, change-password, admin reset, member deletion —
  verified in review); the FK is defense-in-depth for a hypothetical future path and
  is not worth a table rebuild this round. The W1 commit adds the explanatory comment
  to `app/database.py` so the next reader doesn't have to re-derive it.
- Index `auth_tokens.expires_at` and make the expired-token purge opportunistic
  (e.g. only on ~1% of logins, or on startup) instead of a full scan + write per login.
- Optional: expose `pbkdf2_iterations` in `/api/healthz` so W3.2 can assert the cost.
- Gate: suite green; `EXPLAIN QUERY PLAN` for the purge shows the index; login-burst
  p50 unchanged or better in `load_test.py`.

### W5 — Tests that lock the round's guarantees in

**W5.1 CSP regression test (Python-side).** size S · deps: W2.2 (ships inside the W2.2
PR — tests lock the change they accompany; the DAG edge is review coverage, not merge order)
- Assert the exact header on `/`, its **absence** on `/docs`, and that the policy
  contains `'unsafe-eval'` (so a future "tightening" that kills the SPA fails a test,
  not just a browser run). The browser remains the real gate (W2.1); this is the cheap
  tripwire that runs on every `pytest`.

**W5.2 Password-policy tests.** size S · deps: W4.1/W4.2 — the B10 and B9 probes as tests
(change-to-default 400, reset-to-default 400, non-admin account on the default flagged
at boot, staff-created account flagged then unblocked).

**W5.3 Script-contract tests.** size M · deps: W3 · **R2: not in the default suite**
- Live in `tests/integration/`, marked `integration`, deselected by default
  (`pyproject.toml`: `addopts = "-m 'not integration'"`); CI-3 runs
  `uv run pytest -m integration` explicitly. Reason: each hermetic boot is ~6–10 s,
  and the "37 tests in 2 s" property is what makes the default suite a usable
  pre-commit reflex — W5.3 must not cost it.
- A tiny in-process harness (or `subprocess` against a self-hosted server) asserting:
  stubbed-failure ⇒ exit 1 (C), unflagged admin ⇒ exit 0 (D), post-run `changeme`
  login ⇒ 401 (E), smoke degradation ⇒ `[FAIL]` lines and exit 1 with no traceback
  (N4). These are the assertions that make PLAN item 10's CI meaningful.

**W5.4 Metadata consistency.** size S · deps: W1.1 — `pyproject.version ==
app.main.app.version`; no tracked file matching `*copy*`; `data/` not tracked.

### W6 — Docs

**W6.1 `README.md` rewrite** [PLAN #9] size M · deps: W1.1, W2.2, W3, W4.1
- Stop calling the CSP strict: document `unsafe-eval`, *why* (no-build-step Vue full
  build), what it costs (injected-script protection is largely negated), and the
  tracked endgame (§W7.1).
- **Delete the `BEGIN IMMEDIATE` sentence** (Eng2 #2): it describes behaviour
  `app/database.py` explicitly forbids with a documented incident. Replace with the
  trigger-based invariant.
- Document the docs CSP exemption + `HCRM_DOCS=0` (and that `/openapi.json` goes away
  with it), the scripts' no-restore contract and the throwaway-server rule,
  `FASTEMBED_CACHE_PATH` (B16 — the model lives in `/tmp` by default and a tmp-cleaner
  wipes it), and the **upgrade note** (first boot migrates the column, invalidates all
  sessions by token hashing, and blocks the admin until the password changes — looks
  like an outage if unannounced).
- Add a **Tracked issues** section (PLAN P-4): CSP endgame, register enumeration
  oracle, login rate limiting (nginx snippet only), refresh tokens, server-side
  checkout, docs vendoring (N11), `prompt()` password reset (N9).

**W6.2 UI micro-fix** (N9) size S · optional — replace `prompt()` in
`resetMemberPassword()` with the existing modal pattern and a `type=password` input;
add `autocomplete` attributes to silence the Chrome advisory that ui_check allowlists
(if done, remove that allowlist entry — a shrinking allowlist is the healthy direction).

### W7 — CI and the deferred endgame

**W7.1 CI, staged** [PLAN #10] size M · deps: each gate turns on as its workstream lands
- `.github/workflows/ci.yml`, `astral-sh/setup-uv`, Python from `.python-version`
  (3.12), `uv sync --frozen`.
- **CI-1 (lands with W1):** `uv run pytest` + `scripts/ui_check.py --help` import check.
- **CI-2 (lands with W2):** boot a temp-`HCRM_DATA_DIR` server with
  `HCRM_EMBED_MODEL=bogus/model` (hermetic, ~6 s, no download — verified), install
  Chrome (`browser-actions/setup-chrome` or the runner's preinstalled one), run
  `ui_check.py`. Fail the job if the browser is missing rather than skipping
  (exit 3 must be a red build, or the gate is decorative).
- **CI-3 (lands with W3):** `load_test.py` self-hosted (default path, no `--force`) and
  `smoke_test.py` self-hosted. Both must leave the checkout clean — add a
  `git status --porcelain` assertion as the last step.
- Model policy (**D3 decided: out of the PR gate**): run the semantic-ranking smoke
  test in a separate `workflow_dispatch`/nightly job. **R3:** set
  `FASTEMBED_CACHE_PATH` to a *stable workspace path* (e.g.
  `$HOME/.cache/hcrm-fastembed`) and cache that — never `$TMPDIR/fastembed_cache`,
  which is per-job ephemeral on GitHub runners anyway (B16). Promote into the PR gate
  only after measuring the cached-download time in CI-2.
- Never `--force` an external target from CI.

**W7.2 CSP endgame (deferred, tracked)** [PLAN P-4]
- Path: `vue.runtime.global.prod.js` + build-time precompiled render functions, which
  removes `'unsafe-eval'`. Requires a Node build step → conflicts with the project's
  load-bearing no-build philosophy. Tracked in README, not implemented this round.
- Companion: **vendor swagger-ui-dist** (B17: 1 585 988 B + 186 154 B) and serve
  `get_swagger_ui_html(...)` with local `swagger_ui_bundle`/`swagger_ui_css`, then
  re-tighten docs to `script-src 'self' 'sha256-<bootstrap hash>'`. Makes `/docs` work
  offline (N11) and removes the CSP exemption. Deferred because it must not be coupled
  to the ship blocker; `ui_check.py` is what makes the hash-based version safe to adopt
  later (a FastAPI bump that changes the inline script fails the browser gate instead of
  silently blanking docs).

---

## 4. Dependency DAG and merge order

```
W1.1 ─┬─> W1.2 (decision D1)
      │
      ├─> W2.1 ui_check ──> W2.2 CSP fix ──> W5.1 CSP test ─┐
      │        (must FAIL on pre-fix tree)                   │
      ├─> W3.1 helper ─┬─> W3.2 load_test ─┐                 ├─> W7.1 CI-1/2/3
      │                └─> W3.3 smoke_test ─┴─> W5.3 ─────────┤
      │                                    │                  │
      │                (hard edge: W3.2+W3.3 BEFORE W4.1)      │
      ├─> W4.2 create_member flag ──> W5.2 ───────────────────┤
      ├─> W4.3 embedding retry (single-flight) ───────────────┤
      ├─> W4.4 race tests share _client ──────────────────────┤
      ├─> W4.1 reject default password ──> W5.2 ──────────────┤
      ├─> W4.5 hardening batch (optional) ────────────────────┤
      └─> W6.1 README (after W2/W3/W4 semantics are settled) ─┘
                                                    W7.2 deferred (tracked)
```

Hard constraints:

1. **W1 first and alone** — otherwise every later PR carries hygiene noise.
2. **W2.1 before W2.2** — the gate must be demonstrated failing on the tree it is
   meant to protect (PLAN's load-bearing process rule).
3. **W3.2 + W3.3 before W4.1** — after item 5 lands, a restore-to-default is a hard
   400; merging in the other order turns CI red on a healthy system (Eng2 #4).
4. **W6.1 last among code-affecting items** — the README describes behaviour; writing
   it before W4 settles guarantees a third stale doc.
5. CI stages turn on as their gate exists; a stage that cannot run (no browser) must
   fail, not skip.

Suggested split for four engineers (proposal, not an assignment): **A** = W1 + W2
(browser evidence already in hand, §0 B2–B4/B14), **B** = W3 + W5.3, **C** = W4.1 +
W4.2 + W5.2, **D** = W4.3 + W4.4 + W4.5. W6 and W7 pair-review across A/B. Reviews
rotate so nobody reviews their own dependency chain: A↔C, B↔D.

---

## 5. Final verification matrix

Every row must be executed and pasted (rule 1–2). Rows marked ▸ are already measured
in §0 and only need re-running against the merged code.

| Check | Command | Expected |
|---|---|---|
| Unit/API suite | `uv run pytest -q` | unit suite green in ~2 s (R2: integration tests live elsewhere); run 3× and in reverse node-id order |
| Integration suite | `uv run pytest -m integration` | green in CI-3 (W5.3) |
| ▸ Browser gate, pre-fix | `python scripts/ui_check.py --base …` on the pre-W2.2 tree | exit **1**, `EvalError … unsafe-eval` reported |
| Browser gate, post-fix | same, on `/`, `/#/register`, `/docs` | exit **0**, zero *error* console lines, `data-v-app` present, docs DOM > 20 KB |
| Docs kill-switch | `HCRM_DOCS=0` boot + `curl` | `/docs`, `/redoc`, `/openapi.json` → 404 |
| ▸ Upgrade path | boot on a copy of `data/hcrm.db` from `07d879e` | column added, admin flagged, old tokens 401, embeddings intact (B11) |
| load: failure propagates | stubbed `_run_load → False` | exit **1** |
| load: idempotent | two consecutive `--force` runs, second fed the first's password via env | both exit **0** |
| load: hermetic | default self-hosted run | `git status --porcelain` empty, `./data` untouched |
| ▸ default password dead | B8/B10 probes re-run | `changeme` login 401 after any script run; change/reset-to-`changeme` → **400** |
| smoke: reports, never raises | run vs `HCRM_EMBED_MODEL=bogus/model` server | `[FAIL]` lines, exit **1**, no traceback, final password state printed |
| smoke: idempotent | two consecutive self-hosted runs | both exit **0** |
| embeddings: single flight | 20 threads across a cooldown expiry | exactly **1** load attempt |
| races: no flake | 10 consecutive suite runs | 0 failures, no "database is locked" |
| hygiene | `git ls-files \| grep -E " copy|^data/"` | empty |
| CI | open a PR | pytest + ui_check + load + smoke all green; a deliberately broken CSP commit goes red |

---

## 6. Decisions — resolved in the round-5 review (outcomes inline)

- **D1 (blocks W1.2): public history containing credential material.** The admin hash
  is of `changeme` and the 3 plaintext tokens no longer authenticate, so the practical
  exposure is low — but the remote is public (B15). Options: (a) `git rm --cached` only,
  accept the history, note it in README; (b) rewrite history (2 commits — `git
  filter-repo --path data --invert-paths`) and coordinate a force-push window with all
  four of us; (c) recreate the repo. **Recommendation: (b)**, scheduled once, with
  everyone pushing local work first. Rotation is not needed (no live secret), but the
  admin password must still be changed on any real deployment.
  → **Resolved: (b), rewrite.** Protocol recorded in W1.2; no rotation needed
  (exposure inert by design).
- **D2 (W2.2): docs CSP strategy.** Exempt now (recommended, ships the fix) vs vendor
  swagger-ui + hash now (offline docs, tighter CSP, ~1.8 MB and coupling to the ship
  blocker). Recommendation: exempt now, W7.2 later.
  → **Resolved: exempt now, vendor in W7.2** (verified: exemption renders docs
  cleanly; `openapi_url=None` required for the kill-switch).
- **D3 (W7.1): does the PR gate download the 80 MB model?** Recommendation: no —
  hermetic degraded boots for the gate, real-model smoke in a nightly/`workflow_dispatch`
  job with `FASTEMBED_CACHE_PATH` cached. Measure the download in CI-2 before deciding
  to promote it into the gate.
  → **Resolved: keep it out of the gate**, with R3's stable-path cache; promote only
  after measuring the cached-download time in CI-2.
- **D4 (W4.5): enable `PRAGMA foreign_keys`?** SQLite cannot add an FK to the existing
  `auth_tokens` table without a rebuild. Options: table rebuild in `_migrate_schema()`,
  or keep the explicit token cleanup and document that FKs are off. Recommendation:
  document now, rebuild only if a real orphan path appears.
  → **Resolved: document now** (comment lands with W1); rebuild only if a real
  orphan path appears.
- **D5 (W1.1): version number for this round.** `0.2.0` everywhere (recommended) or
  `0.3.0` to mark the security/behaviour changes (default-password rejection, flag on
  staff-created accounts, docs kill-switch) — those *are* user-visible behaviour changes.
  → **Resolved: `0.3.0`** (weakly held by the reviewer, conceded by the author): the
  W6.1 upgrade note is precisely the signal a version bump exists to send, and the
  committed tree already claims `0.2.0` with different behaviour. W1.1 gate updated.
- **D6 (scope): optional `HCRM_EMBED_DISABLED=1`.** A first-class "no embeddings" mode
  would make hermetic boots explicit instead of relying on the bogus-model-name trick.
  Wrinkle: today a model outage makes `POST/PATCH /api/items` return **503**; in a
  *configured* disabled mode writes must succeed with `embedding = NULL` (and
  vector-search must return a clear 503/"disabled"), so the write policy has to branch on
  intent, not on model state. Deferred unless someone wants it this round.
  → **Resolved: defer.** Note W3.3's degraded gate exercises the *outage* path
  (503 on writes), which remains the correct semantics to test regardless of
  whether a configured-disabled mode ever exists.

---

## 7. Explicit non-goals this round

- Vue template precompilation / removing `'unsafe-eval'` (W7.2, tracked).
- Register enumeration oracle, login rate limiting (nginx snippet only), refresh tokens,
  server-side checkout, image uploads, quantized ANN scans.
- Any change to the transaction-mode policy in `app/database.py` — the comment there
  documents a real incident; the last-admin invariant is the triggers' job.
- "Fixing" NULL-embedding vector scans: verified safe (B12).
