# Round-4 Remediation Plan (response to the two-cycle review)

Status: PLAN — not yet implemented. Every finding below was independently
reproduced before being accepted (evidence in "Verification log").

## Verification log (what I reproduced myself)

- **A (fatal): the shipped CSP blanks the entire SPA.** Headless Chrome against
  the current commit: `Uncaught EvalError: ... 'unsafe-eval' is not an allowed
  source of script: script-src 'self'`; DOM = 557 bytes, `v-cloak` still
  present, no `data-v-app`, nothing rendered. The frontend uses Vue's full
  build with an in-DOM template, so the runtime compiler emits `Function(...)`,
  which `script-src 'self'` forbids. **The product's only UI is dead.**
- **B: /docs is dead with three distinct CSP violations** (jsdelivr CSS,
  jsdelivr JS, and FastAPI's *inline* bootstrap script — hash
  `sha256-QOOQu4W1oxGqd2nbXbxiA1Di6OHQOLQD+e+G9oWL8YY=`), confirming that the
  1st-review fix sketch (add jsdelivr to script-src/style-src) is insufficient.
- **C: load_test.py cannot fail the build.** `_run_load()`'s return value is
  discarded; the exit code reflects only the password restore.
- **D: unconditional restore.** `temp_password` is always generated but only
  conditionally set, so an unflagged admin gets a failed restore → exit 1 on a
  passing run.
- **E: the scripts re-arm `changeme`.** Live probe on a scratch server:
  bootstrap → temp → "restore" to changeme → 204; then `changeme` logs in with
  `must_change_password=False` and full API access until the next boot.
- **F, G, H**: confirmed by code inspection (race tests run the lifespan per
  thread per round; `create_member` never sets the flag; nothing in the repo
  executes JavaScript).

## Critique of the reviews

### Conceded in full (no pushback)

1. **A is the round's headline and it is entirely my failure.** I shipped a
   total-UI-outage CSP in the same commit that claimed "verified CSP headers
   end-to-end" — the verification was `curl -I`. Worse: I had *just* rejected
   the reviewer's CSP suggestion for breaking `:style` bindings and external
   images, then shipped something that broke everything. Both teams made the
   identical error class — reasoning about CSP from spec knowledge instead of
   running a browser — and I made it one round *after* catching them making
   it. The correct response is not a better argument, it is a browser in the
   loop (their item 2), which is why it is first in the plan.
2. **B, C, D, E, F, G, H and both 🔵 minors: accepted as stated.** The
   evidence in both reviews is precise and reproduced.
3. Their ordering constraint — *"a CSP change without a browser check is how
   this happened twice"* — is the most important sentence in the plan and is
   adopted verbatim as a hard gate below.

### Pushbacks (4 substantive, 2 minor)

**P-1. Plan items #3 and #4 conflict, and the plan does not reconcile them.**
Item #4 (reject the default password as a new password) makes the scripts'
"restore to default" step *impossible by design*. Therefore the 1st-review
sketch "gate the restore on `changed`" (echoed in plan #3) is obsolete: the
restore must be **removed**, not gated. The coherent script contract once #4
lands is: *if the bootstrap succeeded with the default password, the server
was in a pre-first-change state; the script sets a random password and never
writes the default back; a successful run against such a server intentionally
leaves the random password active and says so.* This single change resolves
C, D, and E together — the plan should say so instead of patching each
symptom.

**P-2. "Stop printing the temp password" is only half right.** Never printing
it creates an unrecoverable lockout: if the script dies between the change and
the end, the only admin account has an unknown random password. On a
throwaway/CI server a leaked temp password is harmless; on a real server the
operator *needs* it to recover. Correct contract: **never print on success
paths of a self-hosted run; print (loudly, as a final-state warning) whenever
the run aborts or the password is intentionally left changed.** Note that
under P-1 a successful external-target run against a flagged server *always*
leaves the password changed — so "stop printing" cannot mean "never print".

**P-3. "Refuse to run against a non-throwaway HCRM_DATA_DIR unless --force"
is unimplementable as stated.** When targeting `HCRM_BASE`, the script cannot
see the server's `HCRM_DATA_DIR`. The implementable equivalent, adopted here:
**load_test.py self-hosts by default** — it spawns uvicorn on a scratch
`HCRM_DATA_DIR` and a random port and tears it down afterwards, so the
default path cannot touch a real deployment. Targeting an external
`HCRM_BASE` requires `--force` plus `HCRM_ADMIN_PASSWORD` (no `changeme`
default for external targets).

**P-4. The CSP endgame (their #9) must be a tracked issue with a concrete
migration path, not just a decision.** The path exists and is well-defined:
`vue.runtime.global.prod.js` + precompiled render functions (commit-time
precompilation of the in-DOM template). It is deliberately out of scope — the
project's no-build-step philosophy is load-bearing — but "decide later"
issues that live only in review threads die. With no issue tracker in this
repo, the plan adds a **Tracked issues** section to the README so the
decision, the cost (`unsafe-eval` substantially negates the CSP's XSS
protection), and the migration path survive the round. Also: their verified
CSP should add `form-action 'self'` alongside `object-src 'none'` /
`base-uri 'self'` (cheap, closes form-hijack).

**Minor pushback on F:** the "coin flip on duplicate column name" framing is
overstated for the *current* test setup — the fresh test DB already contains
`must_change_password` (create_all creates it), so `_migrate_schema()` no-ops
in those threads today. The real problem is coupling and fragility (future
migrations, lifespan changes), which is still worth fixing since the fix is
trivial. Accepted, with corrected rationale.

**Minor pushback on G:** flagging `create_member` accounts (their first
option) is right — the UI form already says "Temporary password" — but the
reviews don't note the blast radius: every `create_user` in the test suite
then starts flagged, so the conftest helper must clear the flag, and the
concurrency tests' survivor re-login depends on the helper's password
contract. Plan item 6 includes the conftest change for that reason.

### One process observation that should be written down

Findings A–D share a single root cause: **verification theater** — headers
checked with curl, exit codes checked by reading print output, "end-to-end"
claims from a TestClient that executes no JavaScript. The reviews' remedy
(ui_check + CI, items 2 and 10) is correct; the plan adds one rule to it:
*no claim about browser-facing behavior may be marked verified without
scripts/ui_check.py (or an explicit browser reproduction) behind it.*

## Work plan

### P0 — ship blockers (the UI is currently dead)

**0. Repo hygiene (do first).** The repo was moved to `Inventory-management`
(the old `.venv` broke on absolute paths — rebuilt). Uncommitted operator
changes exist: replaced `.gitignore` (generic template; still ignores the db
files by name), `LICENSE`, `.vscode/`. Snapshot-commit these before any
remediation so the round has a clean diff.

**1. `scripts/ui_check.py` — the missing safety net (BEFORE touching the
CSP).** Dependency-free (stdlib + a Chrome binary):
- Browser discovery: `chrome-headless-shell` under `~/.cache/puppeteer`, then
  `chromium`/`google-chrome` in PATH, then the macOS Chrome app bundle.
  Exit 3 with install instructions when none is found (CI installs one).
- Checks, via `--headless --dump-dom --enable-logging=stderr
  --virtual-time-budget`:
  - `/` (and `#/register`): **zero console errors**, `data-v-app` present,
    `v-cloak` absent, login form rendered — this proves the Vue runtime
    compiler executed, i.e. exactly what regression A broke.
  - `/docs`: zero console errors, swagger-ui elements rendered (regression B).
  - No scripted login via CDP (out of scope); the login form rendering is
    the compiler canary.
- Exit non-zero on any failure.
- **Gate: must FAIL on the current commit** (A+B already reproduced above),
  and pass after item 2. Both runs recorded in the commit message.

**2. CSP fix (`app/main.py`).**
- App routes: `default-src 'self'; script-src 'self' 'unsafe-eval';
  style-src 'self' 'unsafe-inline'; img-src 'self' data: https:;
  connect-src 'self'; object-src 'none'; base-uri 'self';
  form-action 'self'; frame-ancestors 'none'` — with a comment stating why
  `unsafe-eval` is unavoidable (full Vue build + in-DOM template → runtime
  compiler → `Function()`), per the reviewer's verified fix.
- Docs routes (`/docs`, `/redoc`, `/openapi.json`) are **exempt from the CSP
  header** (dev); additionally `HCRM_DOCS=0` disables docs entirely
  (`FastAPI(docs_url=None, redoc_url=None)`) for production.
- Gate: ui_check passes; header shape verified with curl on `/` and absence
  on `/docs`.

**3. `load_test.py` repair + safety (fixes C, D, E per P-1/P-2/P-3).**
- `ok = _run_load(admin_token) and ok` — the load phase feeds the exit code.
- **No restore step at all.** If the bootstrap changed the password, the run
  ends by reporting the final state (random password left active, printed
  once as a warning); the default password is never written by the script.
- Self-host by default (scratch `HCRM_DATA_DIR` + random port + teardown).
  External `HCRM_BASE` requires `--force` and `HCRM_ADMIN_PASSWORD` (taken
  from env; no hardcoded `changeme` for external targets).
- Gates: (a) stubbed `_run_load` failure → exit 1; (b) two consecutive
  external-target runs → both exit 0 (second uses the password the first
  left, via env); (c) self-hosted default run never touches `./data`.

**4. `smoke_test.py`: same contract change** (no restore-to-default; env
creds, already present, are the supported path; leave-and-warn when the
bootstrap had to change the password). Gate: two consecutive scratch-server
runs, second fed the first's final password via env.

### P1 — close the app-side holes the scripts exposed

**5. Reject the well-known default as a new password (root fix for E).**
Move `DEFAULT_ADMIN_PASSWORD` to `app/security.py`; `change_password` and the
admin PATCH reset return 400 ("well-known default password; choose another")
when the new password equals it. Tests: change-to-changeme → 400 with the
flag unchanged; admin reset to changeme → 400. This is what makes item 3/4's
no-restore contract coherent (P-1).

**6. `create_member` sets `must_change_password = True` (G).** Conftest's
`create_user` clears the flag via a change-password call (reusing the same
password keeps the helper's "created users log in with password123" contract,
on which the concurrency tests' survivor re-login depends). New test:
staff-created account is flagged until changed. README wording updated to
"staff- or admin-issued passwords".

**7. Race tests share the lifespan-less `_client` fixture (F)** — no
`TestClient` context managers inside threads; the concurrency test no longer
depends on `_startup()` being safe to run concurrently.

**8. Embedding retry moves to a background daemon thread.** Extract
`_attempt_load()`; on failure, `_get_model()` schedules a single daemon
retry thread (sleep clamped to ≥ 1 s between attempts) and returns `None`
immediately — no request ever blocks on a model download after cooldown.
STRICT semantics unchanged (startup only). Cooldown test updated: no inline
retry; `_attempt_load()` recovery verified directly.

### P2 — docs, process, endgame

**9. README:** stop calling the CSP strict (document the `unsafe-eval` cost
and the tracked endgame per P-4); document the docs exemption + `HCRM_DOCS`;
add the upgrade note (first boot: column migration, all sessions invalidated
by token hashing, admin blocked until password change — will look like an
outage if unannounced); document the scripts' new no-restore contract and
the throwaway-server rule; add a **Tracked issues** section (CSP endgame,
register enumeration oracle, login rate limiting, refresh tokens,
server-side checkout).

**10. CI (`.github/workflows/ci.yml`):** uv setup; `uv run pytest`; boot a
temp-DB server; run smoke, load (self-hosted default), and ui_check (Chrome
installed by CI). All four artifacts now gate.

**11. Commit discipline:** snapshot first (item 0); ui_check evidence
(failing before / passing after) referenced in the CSP commit; one commit
per item group.

### Final verification matrix

- `uv run pytest` — multiple runs + reversed ordering (fresh per-test DB
  already guarantees order independence; re-verify after items 6–8).
- `scripts/ui_check.py` — fails on the pre-fix commit (evidence above),
  passes after item 2.
- `scripts/load_test.py` — self-hosted default (never touches `./data`);
  external `--force` twice consecutively; stubbed failure exits 1.
- `scripts/smoke_test.py` — twice consecutively against one scratch server.
- E-probe re-run — restore-to-changeme now returns 400, flag never clears
  while the hash matches the default.
- Race tests — repeated runs, zero flakes.
- Live-DB copy — migration + flagging + session invalidation re-verified
  after all changes (the real `data/hcrm.db` is left untouched; first boot
  performs the migration).

### Explicitly out of scope (tracked, not implemented)

- Vue template precompilation (the CSP endgame) — requires a Node build
  step, deliberately deferred as a tracked issue.
- Register enumeration oracle, login rate limiting (nginx snippet only),
  refresh tokens, server-side checkout — documented limitations.
