#!/usr/bin/env bash
# Start the HCRM server (installs dependencies on first run).
# Everything runs embedded: SQLite + the sqlite-vector extension — no
# external database server needed.
#
# Binds to localhost by default. To expose the server on the network:
#   HCRM_HOST=0.0.0.0 ./scripts/run.sh
#
# HCRM_SCRATCH=1 boots on a throwaway database instead of ./data — use it for
# any experiment, verification run or browser check (PLAN-v2 ground rule 3:
# nothing runs against ./data by accident).
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

# W1.4: a throwaway database for experiments, so ./data (the operator's live
# data) is never the default target of a verification run.
if [ -n "${HCRM_SCRATCH:-}" ]; then
    HCRM_DATA_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hcrm-scratch-XXXXXX")"
    export HCRM_DATA_DIR
fi

# Say out loud which database is about to be opened: silently migrating or
# mutating ./data is how the live DB got written mid-round (PLAN-v2 §8.2 F5).
# Exported so the server, the scripts and any child process agree on one path
# (DATABASE_URL, if set, still wins — see app/database.py).
RESOLVED_DATA_DIR="${HCRM_DATA_DIR:-$PWD/data}"
HCRM_DATA_DIR="$RESOLVED_DATA_DIR"
export HCRM_DATA_DIR
if [ -n "$DEV_ADMIN" ]; then DEV_ADMIN_LABEL="ON (admin/admin)"; else DEV_ADMIN_LABEL="off"; fi
echo "database:  ${DATABASE_URL:-sqlite:///$RESOLVED_DATA_DIR/hcrm.db}"
echo "bind:      http://$HOST:$PORT   dev admin: $DEV_ADMIN_LABEL"
if [ -n "${HCRM_SCRATCH:-}" ]; then
    echo "           (scratch — seeded fresh, safe to delete: $RESOLVED_DATA_DIR)"
elif [ ! -e "$RESOLVED_DATA_DIR/hcrm.db" ]; then
    echo "           (no database yet — first boot will create, migrate and seed it)"
else
    echo "           LIVE DATA — back it up first with scripts/backup_db.sh"
fi

exec uv run uvicorn app.main:app --host "$HOST" --port "$PORT"
