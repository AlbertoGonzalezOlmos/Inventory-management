# Round 4 T4 FINAL spec — scan queue API + counter-session binding

## Models (`app/models.py`, new tables via `create_all()`)

- `Terminal`: `id` PK, `terminal_id: str` (unique, indexed), `secret_hash: str`
  (daemon write credential — SHA-256 like tokens), `token_hash: str` (browser
  read credential), `pairing_code_hash: str | None` (single-use 8-char code),
  `created_at`, `last_seen_at`.
- `ScanEvent`: `seq: int` PK (auto-increment = the polling cursor),
  `terminal_id: str` (indexed), `code: str` (the raw scan), `kind: str`
  (`badge`|`barcode`|`unshaped` — **server-derived** via `app.scanshapes.classify_scan`),
  `created_at` (TTL 300 s), `consumed_at: datetime | None`.

## Endpoints (`app/routers/scanner.py`, prefix `/api/scanner`)

1. `POST /api/scanner/terminals` — daemon registers a terminal. Body
   `{terminal_id, secret}`. **Loopback-gated by default** (only `127.0.0.1`/
   `::1`, overridable by env for LAN). Server stores `secret_hash`, mints an
   8-char single-use pairing code, returns `{pairing_code}` (the daemon prints
   it). Idempotent re-register → 409.
2. `POST /api/scanner/pair` — the browser pairs. Body `{terminal_id, code}`.
   Server verifies the code (constant-time), rotates it to one use, returns
   `{token}` (browser read credential, shown once, stored as `token_hash`).
   Wrong code → 401; already-paired → 409.
3. `POST /api/scanner/events` — daemon ingest. Auth: `secret` (header
   `X-Scanner-Secret` or body). Body `{code}`. Server `classify_scan`s, stores a
   `ScanEvent`, returns `{seq}`. Unknown terminal → 401.
4. `GET /api/scanner/events?after=<seq>` — SPA poll. Auth: `token`
   (`X-Scanner-Token`). Returns `{events: [{seq, code, kind}], latest_seq}`
   with `seq > after` for this terminal. **At-most-once**: returned events are
   marked `consumed_at` in the same commit (two tabs can't double-add). TTL
   prune (both consumed and stale) on each call. Empty poll → `{events: [], latest_seq}`.
5. `DELETE /api/scanner/terminals/{terminal_id}` — staff (or loopback) unpair.

## Rules

- Kinds are **never trusted from the wire** — `classify_scan` runs server-side.
- Badge payloads rest at most 300 s in `ScanEvent`, token-gated.
- Terminal-auth (secret/token), **not** user-auth — so the logged-out login
  screen can poll.
- Cursor is per-terminal; `latest_seq` = the terminal's highest seq.

## Tests (`tests/test_scanner_api.py`)

pairing→ingest→poll→unpair lifecycle; idempotent re-register 409; wrong-code 401;
double-pair 409; server-side re-classification (badge/barcode/unshaped table);
cursor + at-most-once (second poll returns nothing); TTL prune + consumed prune;
per-terminal isolation (terminal A's events never visible to B); 401 on bad
secret/token; loopback gate (non-loopback register rejected unless env override).
