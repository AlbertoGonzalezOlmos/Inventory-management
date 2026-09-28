#!/usr/bin/env bash
# Start the HCRM server (installs dependencies on first run).
# Everything runs embedded: SQLite + the sqlite-vector extension — no
# external database server needed.
#
# Binds to localhost by default. To expose the server on the network:
#   HCRM_HOST=0.0.0.0 ./scripts/run.sh
set -euo pipefail
cd "$(dirname "$0")/.."

HOST="${HCRM_HOST:-127.0.0.1}"
PORT="${HCRM_PORT:-8000}"

# W4.0 guard: the dev admin credential (admin/admin, HCRM_DEV_ADMIN, on by
# default this round) must never be reachable from the network by accident.
DEV_ADMIN="${HCRM_DEV_ADMIN:-1}"
case "$DEV_ADMIN" in 0|false|FALSE|no|off) DEV_ADMIN="" ;; esac
case "$HOST" in ""|127.0.0.1|localhost|::1) LOOPBACK=1 ;; *) LOOPBACK="" ;; esac
if [ -n "$DEV_ADMIN" ] && [ -z "$LOOPBACK" ] && [ -z "${HCRM_ALLOW_INSECURE_BIND:-}" ]; then
    echo "refusing to bind $HOST: dev admin mode is ON, so username 'admin'" >&2
    echo "with password 'admin' would have full admin access on your network." >&2
    echo "Either keep the loopback bind, set HCRM_DEV_ADMIN=0, or override with" >&2
    echo "HCRM_ALLOW_INSECURE_BIND=1 (trusted network only)." >&2
    exit 1
fi

uv sync
exec uv run uvicorn app.main:app --host "$HOST" --port "$PORT"
