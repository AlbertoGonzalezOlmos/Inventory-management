# HCRM — Engineering Notes (repository orientation)

> Written after a full read of the code, docs and git history (round 5c,
> `3776405`, then refreshed at `e28b7ec` / round 5b merge of W4.0). Purpose:
> any engineer joining this repo can understand the system,
> run it, test it, and — critically — avoid re-breaking the invariants that past
> rounds paid for in bugs. Read this together with `README.md` (product view) and
> `PLAN-v2.md` (work plan / history of findings).

---

## 1. What this is

**HCRM** = *Shop Catalogue & Members*. A small shop's internal system:

- **Members** browse a catalogue (keyword **and** AI semantic search), build a
  client-side purchase list.
- **Staff** input/edit catalogue items, create member accounts, use a
  table/data-explorer view with CSV/PDF export and analysis.
- **Admins** do everything, including account deletion and password resets.

Everything is embedded — no external services: SQLite (+ the `sqlite-vector`
extension) for storage, a local ONNX embedding model (fastembed) for semantic
search, a vendored Vue 3 SPA (no Node build step) served by FastAPI.

### Stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.12, managed with **uv** (`uv sync`, `uv run …`) |
| DB | SQLite + **sqlite-vector** extension, WAL mode, `busy_timeout=5000` |
| Backend | FastAPI + SQLModel |
| Frontend | Vue 3 full build (vendored `static/vendor/vue.global.prod.js`), hash routing |
| Search | `fastembed` with `BAAI/bge-small-en-v1.5` (384-dim, ~80 MB, downloaded on first use, cached under `$TMPDIR/fastembed_cache` or `FASTEMBED_CACHE_PATH`) |
| Exports | jsPDF + autotable (vendored), client-side |
| Auth | Bearer tokens (stored **SHA-256-hashed** in DB), PBKDF2 password hashing (stdlib, 600k iterations) |

### Roles & privilege model (enforced in `app/routers/members.py` + DB triggers)

- `member` — browse + purchase list only.
- `staff` — item CRUD, create member/staff accounts, table view. **Cannot**
  touch admin accounts, grant admin, or reset passwords.
- `admin` — everything. **Cannot** demote/delete the *last* admin (400).
- The last-admin rule is backed by **SQLite triggers**
  (`app/database.py::TRIGGER_DDL`) so even hand-written SQL or a racy API path
  cannot leave the system admin-less. API checks map the trigger abort back to
  a friendly 400.

---

## 2. Running it

```bash
./scripts/run.sh                    # uv sync + uvicorn on 127.0.0.1:8000
```

- First boot seeds: default admin `admin@shop.local` / `changeme`
  (**flagged `must_change_password` → blocked from the API until changed**) and
  12 example items (`EX-001`…`EX-012`) with embeddings.
- DB lives in `data/hcrm.db` (gitignored; the directory is created on demand).
- **Golden rule from the team's process: never run experiments against `./data`.**
  Always use a scratch dir:
  ```bash
  HCRM_DATA_DIR=$(mktemp -d) HCRM_EMBED_MODEL=bogus/model ./scripts/run.sh
  ```
  (`bogus/model` gives a deterministic, offline, ~6 s "degraded" boot with NULL
  embeddings — the hermetic-boot trick CI uses.)
- `HCRM_HOST=0.0.0.0` exposes to the network (trusted networks only).

### Environment variables

| Var | Default | Meaning |
|---|---|---|
| `HCRM_DATA_DIR` | `<repo>/data` | Where the SQLite DB lives |
| `DATABASE_URL` | `sqlite:///$HCRM_DATA_DIR/hcrm.db` | Override DB location |
| `HCRM_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed model (must be 384-dim; checked at load) |
| `HCRM_EMBED_STRICT` | unset | `1` → abort boot on model problems instead of degrading |
| `HCRM_EMBED_RETRY_SECONDS` | `60` | Cooldown before retrying a failed model load |
| `HCRM_HOST` / `HCRM_PORT` | `127.0.0.1` / `8000` | Used by `scripts/run.sh` |
| `HCRM_DOCS` | `1` | `0` disables `/docs`, `/redoc` **and** `/openapi.json` |
| `HCRM_PBKDF2_ITERATIONS` | `600000` | Password hashing cost (tests set 1000) |
| `HCRM_BASE`, `HCRM_ADMIN_EMAIL`, `HCRM_ADMIN_PASSWORD` | — | Used by `scripts/smoke_test.py` |

---

## 3. Repository layout

```
Inventory-management/
├── app/
│   ├── main.py        # FastAPI app, lifespan startup, seeding, migration,
│   │                  #   security-headers middleware, CSP, /api/healthz
│   ├── database.py    # engine, sqlite-vector loading, WAL, TRIGGERS (read the
│   │                  #   long comments — they document real incidents)
│   ├── models.py      # User, Item (embedding BLOB), AuthToken
│   ├── schemas.py     # Pydantic in/out schemas (_Stripped/_PatchIn bases)
│   ├── security.py    # PBKDF2 hash/verify, token creation + SHA-256 hashing
│   ├── deps.py        # get_current_user / require_staff / require_admin,
│   │                  #   must_change_password gate
│   ├── embeddings.py  # fastembed wrapper, cooldown retry, blob serialization
│   ├── barcodes.py    # GTIN/GS1 number systems: validate, normalise,
│   │                  #   classify (docs/barcodes.md); used by items API +
│   │                  #   scripts/scan_intake.py
│   ├── seed.py        # 12 example items + embedding backfill
│   └── routers/       # auth.py, items.py, members.py (all under /api)
├── static/            # Vue 3 SPA: index.html (in-DOM template), app.js, style.css
│   └── vendor/        # vue.global.prod.js, jspdf, jspdf-autotable (pinned)
├── docs/
│   ├── scanner-opn2001.md   # Opticon OPN-2001 scanner: connection + protocol
│   │                      #   (+ §3.4 WSL2, §3.5 HID-mode scanners)
│   └── barcodes.md        # GTIN/GS1 number systems; same-item vs new-entry rules
├── tests/             # pytest suite (~160 tests, ~25 s, no network/model;
│                      #   scanner tests run offline w/o hardware)
├── scripts/
│   ├── run.sh         # startup
│   ├── ui_check.py    # headless-Chrome browser gate (see §7)
│   ├── smoke_test.py  # end-to-end API test vs a live server (KNOWN FLAWS, §7)
│   ├── load_test.py   # concurrent login/read availability check (KNOWN FLAWS, §7)
│   ├── opn2001.py     # Opticon OPN-2001 connector: serial + raw-USB (pyusb)
│   │                  #   backends, detect (docs/scanner-opn2001.md)
│   ├── scanner_hid.py # live capture from USB-HID keyboard-mode scanners
│   ├── scan_intake.py # scans → catalogue verdicts (same/new/invalid, read-only)
│   └── udev/99-opticon-scanner.rules  # device-node permissions (one-time sudo)
├── PLAN.md            # round-4 remediation plan (historical; findings A–H, P-1…P-4)
├── PLAN-v2.md         # round-5+ plan — AUTHORITATIVE for status & next work
├── REVIEW.md          # round-3 code review (historical; most items since fixed)
└── README.md          # product doc (has a stale-sections banner; rewrite = W6.1)
```

---

## 4. Backend architecture — the load-bearing parts

### 4.1 Startup (`app/main.py::_startup`, run in a threadpool via lifespan)

1. `warm_up()` — load/validate the embedding model (aborts boot iff
   `HCRM_EMBED_STRICT=1`).
2. `create_all()` — tables + last-admin triggers.
3. `_migrate_schema()` — hand-rolled column migration (SQLModel's `create_all`
   never ALTERs; currently adds `users.must_change_password`).
4. Seed default admin (only if **no users at all** exist) — flagged
   `must_change_password=True`.
5. Seed 12 example items (only if no items) + backfill missing embeddings.
6. `_flag_default_password()` — re-flags the seed admin if the DB still matches
   `changeme`.
7. Cache the sqlite-vector probe into `_vector_status` so `/api/healthz` never
   touches the DB under load.

### 4.2 Database (`app/database.py`) — DO NOT "FIX" THESE

- **sqlite-vector is loaded into every pooled connection** via a `connect`
  event; `vector_init('items','embedding', …384/FLOAT32/COSINE)` runs per
  connection (idempotent; the search endpoint re-runs it anyway).
- **WAL + `busy_timeout=5000`** on every connection.
- **Transaction-mode comment is sacred**: the driver default (implicit BEGIN
  around DML only) is deliberate. An earlier revision added a global
  `BEGIN IMMEDIATE` hook and turned *every* request into a write-lock holder →
  unauthenticated DoS ("database is locked" 500s) under concurrent load. The
  last-admin invariant is enforced by **triggers**, not transaction mode. Do not
  re-add a `begin` event hook.
- **Foreign keys are OFF** (deliberate, documented as decision D4): every
  token-delete path does explicit cleanup; a table rebuild for a real FK was
  judged not worth it. Comment in the file explains.
- **Important gotcha**: `DATA_DIR`/`DATABASE_URL` are computed **at import
  time** — env vars must be set before any `app.*` import (see `conftest.py`,
  which uses `os.environ.setdefault` before importing).

### 4.3 Auth & sessions

- Bearer tokens: `secrets.token_urlsafe(32)`; only the **SHA-256 hash** is
  stored (`auth_tokens.token` is the hash). Lookup in `deps.get_current_user`
  is by hash of the presented token. TTL 7 days.
- Login always runs PBKDF2 against a `DUMMY_HASH` for unknown emails (timing
  side-channel mitigation).
- `must_change_password` gate (in `deps.get_current_user`): flagged users get
  **403 on everything except** `/api/auth/me`, `/logout`, `/change-password`
  (exempt list `MUST_CHANGE_EXEMPT_PATHS`). Server-side enforced, not just UI.
- `change-password` verifies the current password, clears the flag, revokes all
  *other* sessions (current one survives).
- Admin `PATCH /api/members/{id} {"password": …}`: admin-only, flags the target,
  revokes all its sessions; **rejected for self** (must use change-password —
  session-takeover protection).
- Register is public and reveals taken emails (409) — accepted trade-off,
  documented limitation.

### 4.4 Items & semantic search

- Embeddings: `f"{name}. {description}"` → 384-dim float vector → little-endian
  float32 BLOB in `items.embedding`.
- `GET /api/items` — keyword listing; `%`/`_`/`\` in `q` are **escaped** and
  matched literally (regression-tested).
- `GET /api/items/vector-search` — embeds the query **once**, exact cosine scan
  via `vector_full_scan`, category filter applied **in SQL before LIMIT/OFFSET**
  (pagination correctness), similarity clamped to [0,1]. Returns 503 if the
  model isn't loaded or the extension failed.
- Create/update policy when the embedding model is down: **503, nothing
  persisted** — an item that can't be embedded would be invisible to semantic
  search, and a PATCH must never null out an existing embedding (data-loss
  regression, both tested). Price/stock-only PATCHes still work during an
  outage.
- Validation: unknown fields → 422 (`extra="forbid"`); explicit `null` in PATCH
  bodies → 422 (`_PatchIn._reject_nulls`); whitespace-only names rejected.

### 4.5 Embeddings module (`app/embeddings.py`)

- Module-level singleton `_model` with failure state + cooldown
  (`RETRY_SECONDS`); `_get_model()` retries after the cooldown. `STRICT` only
  affects startup.
- `model_status()` → `ready` / `failed` / `not_loaded` (never triggers a load).
- Known open issues (PLAN-v2 N6, W4.3): the retry is inline (blocks a request
  thread) and has **no single-flight guard** — N concurrent requests at
  cooldown expiry can start N downloads. Fix planned (background daemon thread,
  `threading.Lock`, exactly-one-load test).

### 4.6 Security headers / CSP (`app/main.py`)

- Middleware adds `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy: same-origin` and a CSP to every response.
- **`script-src 'unsafe-eval'` is REQUIRED**: the SPA uses Vue's *full* build
  with an in-DOM template → runtime compiler → `new Function()`. A prior
  "stricter" CSP blanked the entire SPA (round-5 finding A). `tests/test_headers.py`
  pins this so a future tightening fails pytest, not just a browser.
- Docs routes (`/docs`, `/redoc`, `/openapi.json` — prefix match) are **exempt**
  from CSP (they load jsdelivr + an inline bootstrap script). Known open issue
  F3: the prefix match over-matches (e.g. a hypothetical `/docs.html` static
  file); fix planned in W2.4 (`_is_docs_path` predicate).
- `HCRM_DOCS=0` kills docs **and** `openapi_url` (all three must go together —
  nulling only docs_url leaves the schema public).
- Known open issue W2.5/F-Engineer2: an *unhandled* exception (500) bypasses
  the middleware — Starlette's `ServerErrorMiddleware` emits a bare response
  with no headers. Fix planned via an `@app.exception_handler(Exception)`.
- No built-in login rate limiting (documented; nginx snippet in README).

### 4.7 Frontend (`static/`)

- Single Vue app, hash-based routing (`#/catalogue`, `#/table`, `#/admin`,
  `#/account`, `#/login`, `#/register`). `currentView` computed guards
  `#/table` and `#/admin` with `isStaff`.
- `api()` fetch wrapper: attaches Bearer token from `localStorage`
  (`hcrm_token`); on any 401 (except login) clears the session and forces
  re-login; maps FastAPI validation-error lists to readable messages.
- Catalogue: infinite scroll (IntersectionObserver on `#sentinel`), category
  filter, semantic toggle (✨, requires ≥2 chars else falls back to keyword).
- Purchase list: entirely client-side (localStorage), quantities capped by
  stock, total at the bottom. Checkout is a placeholder toast.
- Table view: click/Shift/Ctrl selection of rows *and* columns, CSV export
  (UTF-8 BOM, formula-injection guard) and PDF export, stats panel
  (min/max/mean/median/σ, per-category breakdown).
- Admin view: item form (sku immutable on edit — PATCH payload is explicit),
  member creation (role member/staff; admin only via API), password reset
  currently uses `prompt()` (open issue N9/W6.2).

---

## 5. API summary

All under `/api`; auth = `Authorization: Bearer <token>`.

| Endpoint | Who | Notes |
|---|---|---|
| `POST /api/auth/register` | public | member signup → token (409 if email taken) |
| `POST /api/auth/login` | public | constant-time vs unknown email |
| `POST /api/auth/logout` | auth | revokes only the current session |
| `POST /api/auth/change-password` | auth | verifies current pw, revokes other sessions, clears flag |
| `GET /api/auth/me` | auth | survives the must-change gate |
| `GET /api/healthz` | public | `{ok, vector, embeddings}` — never loads the model |
| `GET /api/items?q=&category=&limit=&offset=` | auth | keyword search, wildcards escaped |
| `GET /api/items/vector-search?q=&category=&limit=&offset=` | auth | semantic, `q` min_length 2, 503 if model down |
| `GET /api/items/categories` | auth | distinct sorted categories |
| `GET /api/items/{id}` | auth | |
| `POST /api/items` · `PATCH /{id}` · `DELETE /{id}` | staff/admin | auto (re-)embed; 503 on model outage for text changes; SKU unique (409) |
| `GET /api/members` · `POST /api/members` | staff/admin | staff cannot create admins |
| `PATCH /api/members/{id}` | staff/admin | staff can't touch admins/roles-admin/passwords; admin reset flags + revokes |
| `DELETE /api/members/{id}` | admin | not self; not the last admin |

Interactive docs at `/docs` (dev tooling, CSP-exempt, can be disabled).

---

## 6. Testing

```bash
uv run pytest          # 40 tests, ~2.5 s, fully offline (verified green)
```

- **Per-test fresh DB**: the `client` fixture drops/recreates/reseeds. The
  session-scoped `_client` TestClient runs **without lifespan** (each test
  seeds its own data). The embedding model is NEVER loaded — `conftest.py`
  replaces embed calls with deterministic one-hot fake vectors and neutralises
  `warm_up`. Cheap PBKDF2 (1000 iterations) via env.
- Test files:
  - `test_auth.py` — register/login/logout/sessions/change-password/flag flows.
  - `test_security.py` — the privilege model + last-admin (incl. the
    SQL-trigger backstop via raw sqlite3, and concurrent demotion/delete races
    in `test_concurrency.py`).
  - `test_items.py` — listing, wildcards, vector search (single embed, category
    filter, clamping), model-outage 503 policies, unknown/null-field 422s,
    cooldown retry.
  - `test_headers.py` — CSP/hardening-header tripwires (see §4.6).
- **Fragility note**: conftest patches *module attributes*
  (`items_router.embed_text = …`). If you change routers to
  `from app.embeddings import embed_text` (direct name import), the fakes stop
  applying — keep the current `module.function` import style, or update conftest.
- Run order-independence is a stated guarantee (fresh DB per test); the team
  verifies suites in reverse node-id order too.

---

## 7. Scripts

### `scripts/ui_check.py` — the browser gate (run against a scratch server)

Headless Chrome loads `/`, `/#/register`, `/docs`; fails (exit 1) on console
*error text* (classified by message content, never severity — fatal errors log
as `INFO:CONSOLE`), missing `data-v-app`, lingering `v-cloak`, missing inputs,
or a docs DOM < 20 KB. Exit 3 = no browser found. Exists because a CSP change
was twice "verified" with `curl -I` while it blanked the UI.

**Known open defects (PLAN-v2 §8–9, W2.3 is the fix)**: the console regex only
matches Chrome-131-style `:CONSOLE(n)] "…", source: …` lines — Chrome 150 logs
a different shape, so the detector returns 0 messages and **fails open** (F1);
browser discovery sorts by mtime and can pick a blind binary (F2). The planned
fix: split on record headers, a liveness guard (records > 0 but parsed == 0 →
fail), a per-binary self-probe with a truncation-defeating payload, exit 4 for
"detector blind", and parser unit tests without a browser.

### `scripts/smoke_test.py` — end-to-end vs a live server

Covers semantic ranking, listing, auto-embedding on create/update, permissions.
Env: `HCRM_BASE`, `HCRM_ADMIN_EMAIL/PASSWORD`. **Known flaws (W3.3, N4)**: a
failed model or error body turns into an uncaught `TypeError` instead of
`[FAIL]`; the restore-to-default-password step is part of the old contract that
W4.1 (reject `changeme` as a new password) will make impossible; cleanup deletes
`sku == "X"` leftovers. Do not rely on it as-is for gate decisions.

### `scripts/load_test.py` — availability regression check

60 concurrent bogus logins + 20 concurrent reads must produce zero 5xx and
sub-second read p50 (regression test for the global-BEGIN-IMMEDIATE incident).
**Known flaws (W3.2, findings C/D/E)**: `_run_load()`'s return value is
discarded in `main()` (a failing load still exits 0), and it restores the admin
password to `changeme` at the end (re-arming the default). Planned rewrite:
self-hosted by default, no restore, exit code wired, `--force` for external
targets.

---

## 8. Process, conventions, and the plan documents

This repo is developed in **review rounds** with heavy emphasis on *verified
claims*. From `PLAN-v2.md` (ground rules, adopted team-wide):

1. **No claim about browser-facing behaviour is "verified" without an executed
   artifact** (`ui_check.py` or a headless-Chrome run). `curl -I` is not a UI
   check.
2. **No claim about a script's exit code is verified without printing `$?`**,
   and every script must be demonstrated failing at least once.
3. **Nothing runs against `./data`** — always a scratch `HCRM_DATA_DIR`.
4. One workstream = one PR = one review pair; no mixed hygiene/feature diffs.

### Document authority

- `PLAN-v2.md` — **authoritative** for current status, decisions (D1–D7),
  findings (N1–N11, F1–F7), and remaining work (§3 workstreams, §9.8 board).
- `README.md` — product doc, carries a stale-sections banner until the W6.1
  rewrite (trust PLAN-v2 where they disagree).
- `PLAN.md` / `REVIEW.md` — historical (rounds 3–4); almost all findings there
  are fixed; read for context on *why* the current code looks the way it does.

### Current status (as of `e28b7ec` on `origin/main`; check `git log` — this
section ages quickly)

**Done**: W1 repo hygiene (README/`pyproject`/app versions aligned at 0.3.0, no
`*copy*` files, `data/` untracked + history rewritten locally), W2.1 ui_check,
W2.2 CSP fix + docs exemption + `HCRM_DOCS` kill-switch, W5.1 header tests,
**W4.0** dev admin (`admin`/`admin` behind `HCRM_DEV_ADMIN`, default ON this
round — see `app/security.py` and `tests/test_dev_admin.py`).

**Feature branch `feature/opn2001-scanner-connection`**: Opticon OPN-2001
scanner connection + documentation (`docs/scanner-opn2001.md`,
`scripts/opn2001.py`, `tests/test_opn2001.py`), extended with:
barcode number-system layer (`app/barcodes.py`, `docs/barcodes.md`,
`items.barcode` GTIN-14 column + migration + API normalisation/uniqueness/
lookup), device detection (`opn2001.py detect`), raw-libusb backend for
WSL2 kernels without `CONFIG_USB_SERIAL_OPTICON` (`--backend usb`),
HID-keyboard-mode capture (`scripts/scanner_hid.py`, Opticon `065A:A001`),
and the read-only intake classifier (`scripts/scan_intake.py`). Verified on
the real machine (WSL2 + usbipd-win): an `065A:A001` HID-mode Opticon
attaches and enumerates as `/dev/hidraw0`; a second USB device fails
enumeration on Windows (problem 43 — deep-discharge/cable signature,
docs §3.4 state log). Capture requires the one-time udev rule
(`scripts/udev/99-opticon-scanner.rules`, sudo).

**Open workstreams** (see PLAN-v2 §9.8 for the board and owners):
W2.3/W2.4/W2.5
(ui_check detector hardening, docs-exemption bound, 500-header handler),
W1.3 (GitHub-side purge of the old DB blobs — remote still serves them), W1.4
(backup script + run.sh scratch mode), W6.0 (README strike-throughs),
W3.1–W3.3 (shared script helper, load/smoke rewrite), W4.1 (reject well-known
default as new password), W4.2 (flag staff-created accounts), W4.3 (embedding
retry single-flight), W4.4 (race tests share the lifespan-less client),
W4.5 (token purge index etc.), W5.2–W5.5 (tests), W6.1 (README rewrite),
W7.1 (CI), W7.2 (CSP endgame / vendored swagger — deferred).

**Hard merge-order constraints** (from the DAG): scripts (W3.2/W3.3) must merge
**before** W4.1, or the no-restore contract breaks CI; W2.3 must land before
CI-2 is enabled; W1-style hygiene PRs go first and alone.

---

## 9. Gotchas checklist (things that have bitten before)

- **Env before import**: `HCRM_DATA_DIR`/`DATABASE_URL` are read at `app.database`
  import time. Set them before importing anything from `app`.
- **Never re-add a global `BEGIN IMMEDIATE`/`begin` hook** — documented incident.
- **Don't remove `'unsafe-eval'`** from the CSP without the W7.2 migration
  (precompiled render functions) — it blanks the SPA; `test_headers.py` will fail.
- **Don't "fix" NULL-embedding vector scans** — verified safe (B12 in PLAN-v2).
- **Keep the conftest monkey-patch style in mind** (§6 fragility note).
- **PATCH semantics**: omit a field to leave it unchanged; `null` is a 422;
  unknown/typo'd fields are 422 (never silently ignored).
- **Tokens are stored hashed** — you can't read a usable token out of the DB;
  the raw token exists only in the login response/client localStorage.
- **A migrated DB's changes may live only in the WAL** — copying `hcrm.db`
  alone loses them; checkpoint (`PRAGMA wal_checkpoint(TRUNCATE)`) before
  copying/backing up.
- **The public GitHub remote still serves pre-rewrite blobs** (old admin hash +
  plaintext tokens, inert by design but present) until W1.3's Support purge.
- `pyproject.toml` version and `app/main.py` `app.version` must stay in sync
  (0.3.0; W5.4 adds a test).

---

## 10. Quick reference

```bash
# Dev loop
uv sync
./scripts/run.sh                                   # server on :8000
uv run pytest                                      # full suite, offline, ~3 s

# Hermetic server (no model download, scratch data)
HCRM_DATA_DIR=$(mktemp -d) HCRM_EMBED_MODEL=bogus/model \
  uv run uvicorn app.main:app --port 8100 &

# Browser gate (needs a Chrome/Chromium on the machine)
uv run python scripts/ui_check.py --base http://127.0.0.1:8100

# End-to-end smoke (real model; set HCRM_ADMIN_PASSWORD after first change)
uv run python scripts/smoke_test.py

# Docs disabled
HCRM_DOCS=0 ./scripts/run.sh       # /docs, /redoc, /openapi.json → 404
```
