#!/usr/bin/env bash
# Back up the HCRM SQLite database — safely.
#
# WHY THIS EXISTS (PLAN-v2 §8.2 F5 / §9.1): with WAL enabled, the main file
# `hcrm.db` can be *behind* the database. Everything written since the last
# checkpoint lives in `hcrm.db-wal`. A plain `cp data/hcrm.db backup/` therefore
# produces a snapshot that silently rolls the database back — in the observed
# case it resurrected a pre-migration schema, the `changeme` admin password and
# three revoked session tokens. This script refuses to do that:
#
#   1. `PRAGMA wal_checkpoint(TRUNCATE)` folds the WAL into the main file, so
#      the copy is the whole truth;
#   2. all three files are copied when present (never the main file alone);
#   3. the copy is verified by opening it and reporting what is inside — a
#      backup nobody can open is not a backup.
#
# Usage:
#   scripts/backup_db.sh [destination-dir]     (default: data/backups/<timestamp>)
#   HCRM_DATA_DIR=/some/where scripts/backup_db.sh
#
# Safe to run while the server is up; if a writer holds the database the
# checkpoint reports SQLITE_BUSY and the script exits non-zero WITHOUT leaving
# a misleading partial backup behind.
set -euo pipefail
cd "$(dirname "$0")/.."

DATA_DIR="${HCRM_DATA_DIR:-$PWD/data}"
DB="$DATA_DIR/hcrm.db"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${1:-$DATA_DIR/backups/$STAMP}"

if [ ! -f "$DB" ]; then
    echo "no database at $DB (nothing to back up; the app creates it on first boot)" >&2
    exit 2
fi

PY="$(command -v python3 || true)"
if [ -z "$PY" ] && [ -x .venv/bin/python ]; then PY=.venv/bin/python; fi
if [ -z "$PY" ]; then echo "no python3 found (needed for the checkpoint)" >&2; exit 3; fi

echo "database: $DB"
echo "   main file: $(wc -c < "$DB") bytes, wal: $([ -f "$DB-wal" ] && wc -c < "$DB-wal" || echo 0) bytes"

# 1. Checkpoint first — this is the step a naive copy skips.
if ! "$PY" - "$DB" <<'PYEOF'
import sqlite3, sys
db = sys.argv[1]
con = sqlite3.connect(db, timeout=10)
try:
    # TRUNCATE: fold every WAL frame into the main file and reset the WAL to 0
    # bytes, so a file copy afterwards is a complete, self-contained snapshot.
    busy, log, ckpt = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if busy:
        print(f"checkpoint BLOCKED (busy={busy}, wal={log}, checkpointed={ckpt})", file=sys.stderr)
        sys.exit(4)
    print(f"   checkpoint ok (wal frames={log}, checkpointed={ckpt})")
    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    print(f"   journal_mode={mode}")
finally:
    con.close()
PYEOF
then
    echo "refusing to continue: the WAL could not be checkpointed (a writer is" >&2
    echo "active?). Stop the server or retry — copying now would roll the DB back." >&2
    exit 4
fi

# 2. Copy every file of the database (never the main file alone).
mkdir -p "$DEST"
for suffix in "" "-wal" "-shm"; do
    if [ -f "$DB$suffix" ]; then
        cp -p "$DB$suffix" "$DEST/hcrm.db$suffix"
    fi
done

# 3. Verify the copy by opening it.
"$PY" - "$DEST/hcrm.db" "$DB" <<'PYEOF'
import sqlite3, sys
backup, original = sys.argv[1], sys.argv[2]

def summary(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0] if "users" in tables else -1
        items = con.execute("SELECT COUNT(*) FROM items").fetchone()[0] if "items" in tables else -1
        cols = [r[1] for r in con.execute("PRAGMA table_info(users)")] if "users" in tables else []
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        return users, items, cols, integrity
    finally:
        con.close()

bu, bi, bcols, bint = summary(backup)
ou, oi, ocols, oint = summary(original)
print(f"   backup : users={bu} items={bi} integrity={bint} must_change_password={'must_change_password' in bcols}")
print(f"   original: users={ou} items={oi} integrity={oint} must_change_password={'must_change_password' in ocols}")
if bint != "ok":
    print("BACKUP FAILED integrity_check", file=sys.stderr); sys.exit(5)
if (bu, bi) != (ou, oi):
    print("BACKUP DOES NOT MATCH the original (row counts differ)", file=sys.stderr); sys.exit(5)
PYEOF

echo "backup written to: $DEST"
ls -la "$DEST" | sed 's/^/   /'
echo "restore with: stop the server, then copy $DEST/hcrm.db* back into $DATA_DIR/"
