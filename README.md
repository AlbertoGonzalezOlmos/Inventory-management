# HCRM — Shop Catalogue & Members

> ⚠ **Known-stale sections (round 5):** this document still describes some
> pre-round-5 behaviour — the `BEGIN IMMEDIATE` last-admin claim (the DB triggers
> enforce it now), the "strict" CSP wording, and offline `/docs`. The rewrite is
> tracked as W6.1 in `PLAN-v2.md` (§1 lists every stale claim). Until then, trust
> `PLAN-v2.md` over this file where they disagree.

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
remaining admin cannot be demoted or deleted** — enforced at two levels: the
API checks run inside `BEGIN IMMEDIATE` transactions (no check-then-write
race), and database triggers on `users` reject any UPDATE/DELETE that would
leave zero admins, even for hand-written SQL.

## Quick start (Linux)

```bash
# If you don't have uv yet:
# curl -LsSf https://astral.sh/uv/install.sh | sh

./scripts/run.sh         # uv sync + uvicorn on 127.0.0.1:8000
```

Then open http://localhost:8000 — no database server to start: everything is
embedded in `data/hcrm.db`. To expose the server on your network:
`HCRM_HOST=0.0.0.0 ./scripts/run.sh` (only do this on a trusted network).

On first run the app seeds:

- a default admin: `admin@shop.local` / `changeme` ← the account is
  **blocked from the API until you change it** (first login redirects to
  Account → Change password; only logout/me work before that)
- 12 example items (SKUs `EX-001`…`EX-012`) with embeddings, so semantic
  search and the table view can be tested immediately.

> The embedding model (`BAAI/bge-small-en-v1.5`, 384 dims, local & offline) is
> downloaded on first use (~80 MB) and cached.

## Configuration

| Environment variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///<repo>/data/hcrm.db` | SQLite database location |
| `HCRM_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed model for embeddings (must produce 384-dim vectors; checked at startup) |
| `HCRM_HOST` / `HCRM_PORT` | `127.0.0.1` / `8000` | bind address used by `scripts/run.sh` |
| `HCRM_EMBED_STRICT` | unset | when `1`, abort startup if the embedding model is unusable (default: degrade to keyword search) |
| `HCRM_EMBED_RETRY_SECONDS` | `60` | after a model load failure, retry at most this often (a transient network blip must not disable semantic search until restart) |

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
│   └── routers/
│       ├── auth.py       # /api/auth/*      register, login, logout, me
│       ├── items.py      # /api/items/*     browse, vector-search, input
│       └── members.py    # /api/members/*   account management
├── static/               # Vue 3 SPA (index.html, app.js, style.css)
├── data/                 # SQLite database (created at runtime)
├── scripts/
│   ├── run.sh            # one-command startup
│   ├── smoke_test.py     # end-to-end API test (run with server up)
│   └── load_test.py     # availability check: concurrent logins must not lock the DB
└── pyproject.toml        # uv-managed dependencies
```

## API summary

- `POST /api/auth/register` — member self-signup → token
- `POST /api/auth/login` / `POST /api/auth/logout` (revokes only the current
  session) / `GET /api/auth/me`
- `POST /api/auth/change-password` — self-service password change (verifies
  the current password, revokes all other sessions, clears
  `must_change_password`)
- `GET /api/healthz` — liveness probe: `{ok, vector, embeddings}`. The
  extension check is cached at startup (never re-probed under load) and the
  model state is reported without triggering a load
  (`ready` / `failed` / `not_loaded`)
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
- Accounts flagged `must_change_password` (seeded default admin, admin-issued
  resets) are blocked from the API (403) until they change it via
  `/api/auth/change-password` — enforced server-side, not just in the UI.

Interactive docs: http://localhost:8000/docs

## Security notes

- All responses carry baseline hardening headers (`X-Content-Type-Options`,
  `X-Frame-Options`, `Referrer-Policy`, and a strict CSP: scripts are
  same-origin only — everything is vendored; inline style *attributes* are
  allowed for Vue's reactive `:style` bindings).
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
uv run pytest                        # unit/API test suite (per-test fresh DB,
                                     # fake embeddings — fast and offline)

./scripts/run.sh &                   # or another terminal
uv run python scripts/smoke_test.py  # end-to-end test with real embeddings

# once you changed the default admin password:
HCRM_ADMIN_PASSWORD=<new> uv run python scripts/smoke_test.py

# availability regression check (concurrent logins must not lock the DB):
uv run python scripts/load_test.py
```

The pytest suite covers the privilege model (staff cannot touch admin
accounts, last-admin protection including the SQL-level trigger backstop and
**concurrent** demotion/delete races), auth/session handling (hashed token
storage, must-change-password enforcement, self-reset takeover rejection),
password change and reset, validation (unknown fields, explicit nulls,
LIKE-wildcard escaping), embedding-outage behaviour and cooldown retry, and
vector search. The smoke test covers semantic search ranking, listing,
auto-embedding on create/update, and permissions against a live server. The
load test asserts 60 concurrent bogus logins + 20 concurrent reads produce
zero 5xx and sub-second read latency (regression test for the global
BEGIN IMMEDIATE incident).

## Next steps (barebones placeholders)

- Real checkout/order endpoint for the purchase list (currently client-side only)
- Refresh tokens / proper session expiry handling
- Image uploads (only URLs are supported now)
- Quantized ANN scans (`vector_quantize`) for very large catalogues
- Server-side streaming exports for very large tables
- Production deployment: reverse proxy (nginx/caddy) + HTTPS
