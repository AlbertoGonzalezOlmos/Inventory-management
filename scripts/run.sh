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

uv sync
exec uv run uvicorn app.main:app --host "$HOST" --port "$PORT"
