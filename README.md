# HCRM — Shop Catalogue & Members

Barebones infrastructure for a shop: staff input items into a catalogue, members
create accounts, browse the catalogue (keyword **and AI semantic search**), build
a purchase list, and explore the item data in a table view with export/analysis.

## Stack

| Layer     | Technology                                        |
|-----------|---------------------------------------------------|
| Runtime   | Python 3.12, managed with **uv**                   |
| Database  | **SQLite + sqlite-vector** (embedded, no server)   |
| Backend   | **FastAPI** + SQLModel                             |
| Frontend  | **Vue 3** SPA (vendored JS, no Node toolchain, fully offline) served by FastAPI |
| Search    | Semantic vector search via **fastembed** (local ONNX model, no API keys) + keyword search |
| Exports   | CSV & PDF (jsPDF), client-side                    |
| Auth      | Bearer tokens, PBKDF2 password hashing (stdlib)    |

Roles: **member** (browse + purchase list), **staff** (input/edit items, create
member accounts, table view), **admin** (everything, incl. deleting accounts).
Staff can never modify admin accounts or grant the admin role, and the **last
remaining admin cannot be demoted or deleted**. That invariant is enforced by
**database triggers** on `users`, which reject any UPDATE/DELETE that would leave
zero admins — even for hand-written SQL, and without any check-then-write race
(SQLite serialises writers, and a trigger's `WHEN` subquery is evaluated at write
time). The API checks run first only to produce friendly 400s.

> Do **not** "fix" this with a global `BEGIN IMMEDIATE` connection hook: an
> earlier revision did exactly that and turned every request — reads included —
> into a write-lock holder, producing an unauthenticated DoS ("database is
> locked" 500s) under concurrent load. `app/database.py` documents the incident
> and `tests/test_concurrency.py` + `scripts/load_test.py` guard it.

## Quick start (Linux)

```bash
# If you don't have uv yet:
# curl -LsSf https://astral.sh/uv/install.sh | sh

./scripts/run.sh         # uv sync + uvicorn on 127.0.0.1:8000
```

Then open http://localhost:8000 — no database server to start: everything is
embedded in `data/hcrm.db`. `run.sh` prints the database it is about to open and
warns when that is live data; use `HCRM_SCRATCH=1 ./scripts/run.sh` for a
throwaway database when experimenting. To expose the server on your network:
`HCRM_HOST=0.0.0.0 ./scripts/run.sh` (only do this on a trusted network, and
never with the dev admin credential enabled — see below).

Back the database up with `scripts/backup_db.sh`: it checkpoints the WAL first
and then verifies the copy. **Never copy `data/hcrm.db` alone** — with WAL
enabled the main file can be behind the database, so a bare copy silently rolls
back to an older schema/passwords.

On first run the app seeds:

- **the dev admin (testing/debugging, owner directive "until further notice"):**
  username `admin`, password `admin`, full admin access, not flagged. It exists
  while `HCRM_DEV_ADMIN` is on (**the default**), is announced at startup and by
  `/api/healthz` (`insecure_dev_admin: true`), and `run.sh` refuses a
  non-loopback bind because of it. Turn it off with `HCRM_DEV_ADMIN=0`.
- a default admin: `admin@shop.local` / `changeme` ← the account is
  **blocked from the API until you change it** (first login redirects to
  Account → Change password; only logout/me work before that). No endpoint will
  accept `changeme` or `admin` as a *new* password.
- 12 example items (SKUs `EX-001`…`EX-012`) with embeddings, so semantic
  search and the table view can be tested immediately.

> **Upgrading an existing database:** the first boot adds the
> `must_change_password` column and the `app_meta` table, invalidates every
> existing session (tokens are stored as SHA-256 hashes now, so old plaintext
> tokens simply stop matching — everyone logs in once more), and blocks any
> account whose password is one this repo publishes until it is changed. That
> last part looks like an outage if nobody announced it: back up first
> (`scripts/backup_db.sh`), then expect a forced password change.

> The embedding model (`BAAI/bge-small-en-v1.5`, 384 dims, local & offline) is
> downloaded on first use (~80 MB) and cached in `$FASTEMBED_CACHE_PATH` — which
> defaults to **`/tmp/fastembed_cache`**, so a tmp-cleaner wipes it and the next
> boot re-downloads. Set `FASTEMBED_CACHE_PATH` to a stable path to avoid that.

## Configuration

| Environment variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///<repo>/data/hcrm.db` | SQLite database location |
| `HCRM_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed model for embeddings (must produce 384-dim vectors; checked at startup) |
| `HCRM_HOST` / `HCRM_PORT` | `127.0.0.1` / `8000` | bind address used by `scripts/run.sh` |
| `HCRM_EMBED_STRICT` | unset | when `1`, abort startup if the embedding model is unusable (default: degrade to keyword search) |
| `HCRM_EMBED_RETRY_SECONDS` | `60` | after a model load failure, retry at most this often (a transient network blip must not disable semantic search until restart) |
| `HCRM_DEV_ADMIN` | `1` (**this round**) | seed/ensure the well-known dev admin `admin`/`admin`. `0` restores the secure default (`admin@shop.local`/`changeme`, blocked until changed) |
| `HCRM_ALLOW_INSECURE_BIND` | unset | let `scripts/run.sh` bind a non-loopback `HCRM_HOST` while `HCRM_DEV_ADMIN` is on (trusted networks only) |
| `HCRM_SCRATCH` | unset | `1` makes `run.sh` boot on a throwaway database instead of `./data` |
| `HCRM_DOCS` | `1` | `0` disables `/docs`, `/redoc` **and** `/openapi.json` for production |
| `FASTEMBED_CACHE_PATH` | `$TMPDIR/fastembed_cache` | where the embedding model is cached — note this is `/tmp` by default, so a tmp-cleaner wipes it and the next boot re-downloads ~80 MB |
| `HCRM_BROWSER` | auto | browser binary for `scripts/ui_check.py` (it probes each candidate and refuses to run with one that cannot log console messages) |

## Vector search with sqlite-vector

[sqlite-vector](https://github.com/sqliteai/sqlite-vector) is a SQLite extension
that adds SIMD-accelerated vector search:

- item embeddings are stored as float32 **BLOBs in the ordinary `items.embedding`
  column** — no virtual tables, no preindexing
- the extension is loaded into every pooled connection (`app/database.py`)
- `vector_init('items', 'embedding', 'dimension=384,type=FLOAT32,distance=COSINE')`
  registers the column; the search endpoint re-runs it per request (idempotent)
- search uses `vector_full_scan(...)` — an **exact** cosine-distance scan.
  Measured on this machine: ~0.65 ms per 1,000 items per query, linear,
  single-threaded (1k → 0.6 ms, 10k → 6.6 ms, 50k → 33 ms) — i.e. roughly
  **650 ms per query at 1M items**. Comfortable up to ~100k items; beyond
  that, switch to the extension's quantized ANN scans (`vector_quantize` +
  `vector_quantize_scan`), which can be wired in without schema changes

## Features

### Catalogue (members)
- Infinite-scroll grid, category filter, keyword search
- **Semantic search toggle (✨)**: type e.g. *“something to keep my drinks warm
  on a hike”* and items are ranked by meaning-similarity to the description,
  with match percentages
- **Click an item to add it to the purchase list**; the ℹ button opens a
  details modal (with “Similar items” via vector neighbours and an
  “Add to purchase list” button)
- **Purchase list**: add/subtract quantities of the same item (+ / − buttons),
  remove items, and the **total amount shown at the bottom** of the panel

### Table view (staff) — `#/table`
- Table of all items in the database, paginated
- **Selection**: click a row/column, Shift-click for a range, Ctrl/Cmd-click to
  add or remove individual rows/columns
- **Export** the current selection (rows × columns) as **CSV** or **PDF**
- **Analysis** panel over the selection (or all shown rows): count, inventory
  value, price min/max/mean/median/σ, stock stats, per-category breakdown with
  bar chart

### Admin (staff)
- Item input form; descriptions are embedded automatically on create/update
  (an `AI ✓` column shows which items have embeddings)
- Member account creation (member or staff role), account management

### Barcode scanner hardware (back office)
- `scripts/opticon_detect.py` — one shared map of the Opticon USB
  personalities (`0009` OPN-2001 serial, `A001` USB-HID keyboard, `A002`
  USB-COM/CDC-ACM): what is attached, which kernel node it got, and which tool
  receives from it. Identities carry their provenance, and an
  observed-but-undocumented PID is reported as such rather than guessed.
- `scripts/scanner_hid.py` — live capture from a USB-HID (keyboard-mode)
  scanner without window focus: `hidraw` (stdlib) on Linux/WSL2, `hidapi` on
  Windows/macOS; emits `timestamp,symbology,barcode` CSV or JSON.
- `scripts/udev/99-opticon-scanner.rules` — one-time sudo install granting
  `plugdev` access to the tty/hidraw/usbfs nodes (WSL sessions have no seat,
  so the automatic `uaccess` ACL never fires).
- Connection, WSL2/`usbipd` passthrough, permissions and troubleshooting:
  **`docs/opticon-hardware.md`**.

### Barcode scanner (Opticon M-10)
- Hardware integration for the Opticon M-10 2D presentation scanner:
  stdlib-first serial driver (`app/scanner/`, pyserial optional — one reader
  thread owns the port, so a scan interleaved with a command ACK is delivered,
  not dropped) plus a bridge that matches scans by **exact identity**
  (canonical GTIN, or exact SKU for opaque labels) and can adjust stock
  (`scripts/scanner_bridge.py`). It never guesses: keyword matching is opt-in
  and refused outright when combined with a stock adjustment, and it will not
  open a serial port it has not verified as the M-10's `065A:A002`.
- **QR badge login**: every account can get a printable QR badge
  (`HCRM1:…` payload) that logs in instead of email+password — scan it into
  the badge field on the login page (USB-HID mode) or exchange it via
  `POST /api/auth/qr-login` (bridge/kiosk). Badges are bearer credentials:
  only the SHA-256 hash is stored, regenerating revokes the previous one.
- Full setup, protocol, QR-badge and troubleshooting guide:
  **`docs/opticon-m10.md`**.

## Project layout

```
├── app/
│   ├── main.py           # FastAPI app, static mounting, seeding on first run
│   ├── database.py       # SQLite engine + sqlite-vector loading & vector_init
│   ├── models.py         # User, Item (embedding as float32 BLOB), AuthToken
│   ├── schemas.py        # Pydantic request/response schemas
│   ├── security.py       # PBKDF2 hashing + token creation
│   ├── deps.py           # get_current_user / require_staff / require_admin
│   ├── embeddings.py     # fastembed wrapper + float32 BLOB serialization
│   ├── seed.py           # 12 example items + embedding backfill
│   ├── scanner/          # Opticon M-10 driver (protocol, transport, m10)
│   ├── qrbadge.py        # QR badge payloads + inline-SVG rendering
│   ├── vendor/           # vendored pure-Python deps (Nayuki QR encoder, MIT)
│   └── routers/
│       ├── auth.py       # /api/auth/*      register, login, QR badge, logout, me
│       ├── items.py      # /api/items/*     browse, vector-search, input
│       └── members.py    # /api/members/*   account management
├── static/               # Vue 3 SPA (index.html, app.js, style.css, vendored libs)
├── data/                 # SQLite database (runtime only — git-ignored)
├── docs/
│   ├── opticon-hardware.md # which Opticon scanner is attached, and how to reach it
│   └── opticon-m10.md    # Opticon M-10: connection, command protocol, guide
├── scripts/
│   ├── run.sh            # one-command startup (prints the DB it will open;
│   │                     # HCRM_SCRATCH=1 for a throwaway one)
│   ├── backup_db.sh      # WAL-checkpoint-then-verify backup (never copy .db alone)
│   ├── _hcrm.py          # shared script contract: self-hosting, credentials,
│   │                     # reporting, no password restore
│   ├── opticon_detect.py # the repo's one map of Opticon USB personalities
│   ├── scanner_hid.py    # receive scans from a USB-HID (keyboard-mode) scanner
│   ├── udev/             # one-time sudo install: device-node permissions
│   ├── ui_check.py       # browser gate: renders the SPA + /docs in headless
│   │                     # Chrome and fails on console errors / missing DOM
│   ├── smoke_test.py     # end-to-end API test (self-hosts by default)
│   ├── load_test.py      # availability check: concurrent logins must not lock the DB
│   └── scanner_bridge.py # Opticon M-10 → catalogue lookup / stock bridge
├── tests/                # pytest suite (per-test fresh DB, fake embeddings)
└── pyproject.toml        # uv-managed dependencies
```

## API summary

- `POST /api/auth/register` — member self-signup → token
- `POST /api/auth/login` / `POST /api/auth/logout` (revokes only the current
  session) / `GET /api/auth/me`
- `POST /api/auth/change-password` — self-service password change (verifies
  the current password, revokes all other sessions, clears
  `must_change_password`)
- `POST /api/auth/qr-login` — log in with a QR badge (`HCRM1:…` payload or
  raw token) → token; `POST`/`DELETE /api/auth/qr-badge` — generate/replace
  (returns payload + printable SVG, exactly once) or revoke your own badge;
  `POST`/`DELETE /api/members/{id}/qr-badge` — staff issue/revoke badges for
  members (admin accounts: admins only)
- `GET /api/healthz` — liveness probe:
  `{ok, vector, embeddings, insecure_dev_admin, pbkdf2_iterations}`. The
  extension check is cached at startup (never re-probed under load), the model
  state is reported without triggering a load (`ready` / `failed` /
  `not_loaded`), `insecure_dev_admin` exposes the dev credential mode, and
  `pbkdf2_iterations` is there so a load test's latency budget can be read
  against the hashing cost it was measured at
- `GET /api/items?q=&category=&limit=&offset=` — paginated keyword listing
  (LIKE wildcards `%`/`_` in `q` are escaped and matched literally)
- `GET /api/items/vector-search?q=&category=&limit=&offset=` — **semantic
  search** (embeds the query, exact cosine scan via sqlite-vector, optional
  category filter, returns `similarity` per item)
- `GET /api/items/categories` — distinct categories
- `POST /api/items` / `PATCH /api/items/{id}` / `DELETE /api/items/{id}` —
  staff only; create/update automatically (re-)embed the description. If the
  embedding model is down, item creation and text changes are rejected with
  **503** (nothing is persisted, and the existing embedding is never nulled
  out); price/stock updates still work. Unknown/typo'd fields are rejected
  with 422, as are explicit `null`s in PATCH bodies
- `POST /api/items/{id}/stock-adjust` — staff/admin; **atomic** stock delta
  for scanner/POS traffic (`scripts/scanner_bridge.py`). The read and the
  write run inside one targeted `BEGIN IMMEDIATE` — a per-operation write
  lock, not the global hook warned about above — so concurrent adjustments
  serialise and lose nothing. The response reports the applied delta and
  whether the adjustment clamped at 0
- `GET /api/members` / `POST /api/members` / `PATCH /api/members/{id}` — staff
  only; **admin accounts can only be modified by admins**, the admin role can
  only be granted by admins, and only admins can reset passwords
  (`PATCH {"password": ...}` — revokes the account's sessions and flags it
  `must_change_password`). Admins cannot reset *their own* password this
  way — that must go through change-password, which verifies the current
  password (session-takeover protection)
- `DELETE /api/members/{id}` — admin only

Sessions & passwords:

- Bearer tokens are stored in the DB as **SHA-256 hashes** — a leaked
  database file does not yield usable sessions. (Tokens issued before this
  change stop matching: everyone re-logs-in once.)
- Accounts flagged `must_change_password` are blocked from the API (403) until
  they change it via `/api/auth/change-password` — enforced server-side, not just
  in the UI. Accounts get flagged when they are **created by staff or an admin**
  (the UI calls it a "Temporary password"), when an admin **resets** their
  password, and at boot for any account still using a password this repo
  publishes.
- **Passwords published by this repo are rejected as new passwords** (`changeme`,
  and `admin` — the latter is also below the 8-character minimum, so the schemas
  refuse it first). Without that rule, an admin reset to `changeme` returned 200
  and a self-service `changeme → changeme` change returned 204 *while clearing
  the flag*, leaving a published credential with full API access.

Interactive docs: http://localhost:8000/docs — they load swagger-ui from the
jsdelivr CDN, so `/docs` needs network access even though the SPA does not, and
they are exempt from the CSP for that reason. Set `HCRM_DOCS=0` to disable
`/docs`, `/redoc` and `/openapi.json` entirely (recommended for a deployment).
Vendoring swagger-ui locally is a tracked issue.

## Security notes

- ⚠ **Dev admin mode is ON by default this round** (`HCRM_DEV_ADMIN=1`, owner
  directive): username `admin` / password `admin` has full admin access and is
  not flagged for a password change. It is intended for local testing and
  debugging only — `scripts/run.sh` refuses a non-loopback bind while it is on,
  `/api/healthz` reports `insecure_dev_admin`, and startup logs a SECURITY
  warning. Set `HCRM_DEV_ADMIN=0` for any deployment, and see the tracked item
  for making the well-known password un-settable via the API (W4.1).
- Every response carries baseline hardening headers (`X-Content-Type-Options`,
  `X-Frame-Options`, `Referrer-Policy`) — including 500s, which need an explicit
  exception handler because an exception propagates straight through
  `BaseHTTPMiddleware`. The CSP is:

  ```
  default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline';
  img-src 'self' data: https:; connect-src 'self'; object-src 'none';
  base-uri 'self'; form-action 'self'; frame-ancestors 'none'
  ```

  **This CSP is not "strict", and `'unsafe-eval'` is a real cost.** The frontend
  deliberately uses Vue's *full* build with an in-DOM template (no build step),
  so the runtime compiler emits `new Function(...)` — which `script-src 'self'`
  alone forbids. A policy without `'unsafe-eval'` was shipped once and blanked the
  entire SPA (`v-cloak` stayed on an emptied `#app`). `'unsafe-eval'` means an
  attacker who can inject script can also run it, so the CSP's XSS value is
  largely limited to *loading* foreign scripts. The endgame — precompiled render
  functions with `vue.runtime.global.prod.js`, which removes `'unsafe-eval'` — is
  a tracked issue; it needs a Node build step, which this project deliberately
  avoids. `style-src 'unsafe-inline'` is for Vue's reactive `:style` bindings
  (the analysis bar chart), not for scripts.
- The docs routes (`/docs`, `/redoc`, `/openapi.json`) are **exempt** from the
  CSP because swagger-ui comes from jsdelivr plus an inline bootstrap script. The
  exemption is bounded (`/docs` and paths under it, never `/docs.html`), and
  `HCRM_DOCS=0` removes the endpoints altogether.
- Any change to the CSP or to the frontend must be verified with
  `scripts/ui_check.py` — a real headless browser, not `curl -I`. Twice, a CSP
  change was declared verified from headers alone while the UI was dead.
- There is no built-in login rate limiting; put the app behind a reverse
  proxy and throttle there, e.g. nginx:

  ```nginx
  limit_req_zone $binary_remote_addr zone=hcrm_login:10m rate=10r/m;

  location = /api/auth/login {
      limit_req zone=hcrm_login burst=5 nodelay;
      proxy_pass http://127.0.0.1:8000;
  }
  ```

- `POST /api/auth/register` is open and reveals whether an email is taken
  (409) — an accepted trade-off for a shop signup page; restrict or disable
  it if that matters for your deployment.

## Testing

```bash
uv run pytest                          # per-test fresh DB + fake embeddings:
                                       # fast, offline, order-independent

# Browser gate (real headless Chrome; renders the SPA and /docs):
uv run python scripts/ui_check.py --self-host      # boots + tears down its own server
uv run python scripts/ui_check.py --base http://127.0.0.1:8000

# End-to-end against a live server. Both scripts SELF-HOST a throwaway server by
# default, so they cannot touch a real deployment:
uv run python scripts/smoke_test.py --allow-degraded          # no model: semantic checks skipped
uv run python scripts/smoke_test.py --model BAAI/bge-small-en-v1.5   # full, ~80 MB once
uv run python scripts/load_test.py                            # 60 logins + 20 reads

# Against an existing server you must opt in AND supply credentials:
HCRM_ADMIN_USERNAME=... HCRM_ADMIN_PASSWORD=... \
    uv run python scripts/load_test.py --base http://host:8000 --force

scripts/backup_db.sh                   # WAL-checkpoint-then-verify backup
```

`ui_check.py` exits 0 on pass, 1 on a check failure, 2 when the target is
unreachable, 3 when no browser is found and **4 when a browser is found but
cannot log console messages** (e.g. `chrome-headless-shell`). CI must treat 3 and
4 as failures, never as skips: a browser gate that silently detects nothing is
worse than no gate.

The scripts never restore a password. If an account had to change its password to
proceed, the run reports that final state (and `--json` carries it) and leaves it
— an earlier "restore the default afterwards" step re-armed `changeme` with the
must-change flag cleared, i.e. the verification tooling made the server *less*
safe than it found it.

The pytest suite covers the privilege model (staff cannot touch admin accounts,
last-admin protection incl. the SQL-level trigger backstop and **concurrent**
demotion/delete races), auth and sessions (hashed token storage,
must-change-password enforcement, self-reset takeover rejection), the password
policy (published passwords rejected on every write path, staff-issued accounts
flagged, the marker-gated boot scan), validation (unknown fields, explicit nulls,
LIKE-wildcard escaping), embedding-outage behaviour and the single-flight
background retry, vector search, security headers (CSP shape, docs exemption
bound, 500s), the dev-admin mode and its bind guard, the backup/run scripts, the
ui_check console parser (fixture-based, no browser needed), and the two
verification scripts' contract (exit codes, no-restore, no crashes, never
touching `./data`).

## Tracked issues (decided, deliberately not implemented)

| Issue | Status / cost |
|---|---|
| **Remove `'unsafe-eval'` from the CSP** | Needs `vue.runtime.global.prod.js` + precompiled render functions, i.e. a Node build step — against the project's no-build philosophy. Until then the CSP cannot stop injected script from running. |
| **Vendor swagger-ui** (`swagger-ui-bundle.js` 1.59 MB + `swagger-ui.css` 186 KB) | Would make `/docs` work offline and let it carry a CSP (`script-src 'self' 'sha256-…'`). Today `/docs` needs the jsdelivr CDN and is CSP-exempt. |
| **Login rate limiting** | Not built in; use the nginx snippet above. |
| **`POST /api/auth/register` enumeration oracle** | Reveals whether an email is taken (409). Accepted trade-off for a shop signup page. |
| **Tokens in `localStorage`** | Exfiltratable by any script that runs — which `'unsafe-eval'` makes easier. HttpOnly cookies + CSRF tokens would fix it and change the API contract. |
| **Refresh tokens / session expiry UX** | Sessions simply expire after 7 days. |
| **Server-side checkout** | The purchase list is client-side only (`requestPurchase()` is a placeholder). |
| **`PRAGMA foreign_keys` is OFF** | `auth_tokens.user_id` is declarative only; enforcing it needs a table rebuild. The only user-deleting path removes tokens first. |
| **Quantized ANN scans** (`vector_quantize`) | Exact `vector_full_scan` is comfortable to ~100k items; see the measurements above. |

## Next steps (barebones placeholders)

- Real checkout/order endpoint for the purchase list (currently client-side only)
- Refresh tokens / proper session expiry handling
- Image uploads (only URLs are supported now)
- Server-side streaming exports for very large tables
- Production deployment: reverse proxy (nginx/caddy) + HTTPS, `HCRM_DEV_ADMIN=0`,
  `HCRM_DOCS=0`
- CI (`.github/workflows/ci.yml`): pytest → ui_check → load/smoke, each stage
  gated on the previous one's tooling being trustworthy
