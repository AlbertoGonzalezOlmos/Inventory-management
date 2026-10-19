# Round 4 T5 FINAL spec — daemon forwarding, pairing, config, packaging

## CLI (`scripts/scanner_daemon.py` gains `main()`)

```
scanner_daemon --config <path> [--forward URL] [--dry-run]
```

- **Config** (`--config`): JSON `{terminal_id, secret}`, written `0600` on first
  register. Secret is the daemon's write credential (stored hashed server-side).
- **Register**: if no config / `--register`, `POST /api/scanner/terminals
  {terminal_id, secret}` → print the returned **single-use pairing code** to
  stdout ("pair this terminal: enter <code> on the login screen"). Then persist
  the config.
- **Loop**: grab (T3) → `read_scans` (T2) → for each scan, `POST
  /api/scanner/events {code}` with the secret. **Retry keeps listening** — a
  failed POST is retried/queued; it never kills the grab loop. Optional spool
  file so a server outage doesn't drop scans; on replay, dedupe (the API's
  idempotency).
- **`--dry-run`**: print decoded scans to stdout, **zero HTTP** (no network
  import / call).
- **Redaction**: `redact_code` (from `app.scanshapes`/bridge) on every log/print
  path — never echo a badge payload.
- **SIGTERM/SIGINT**: stop the loop, close the fd (grab auto-releases), exit 0.

## Packaging

- `scripts/systemd/scanner-daemon.service` (from T3) now points at the real
  `ExecStart` with `--config /etc/hcrm/scanner.json --forward http://127.0.0.1:8000`.
- A `README`-level note (or `docs/opticon-m10.md` §) documenting install:
  udev rule + systemd unit + `--register` once to print the pairing code.

## Tests (`tests/test_scanner_daemon_forward.py`)

Injected `post`/`sleep`/`open` (no network, no device):
- register → config written 0600 + pairing code printed.
- a scan is POSTed with the secret; a failed POST retries and keeps the loop alive.
- `--dry-run` issues zero POST calls.
- badge payloads redacted in every print path (capture stdout).
- SIGTERM → clean exit (fd closed).
- spool: server-down scans are replayed with dedupe.

## Gates

`uv run pytest` green. Live forwarding is a manual step (documented), not CI.
