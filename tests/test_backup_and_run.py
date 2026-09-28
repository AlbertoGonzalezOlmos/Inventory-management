"""W1.4 — live-data safety: scripts/backup_db.sh and run.sh's data-dir handling.

The failure mode these pin (PLAN-v2 §8.2 F5 / §9.1, observed on the real
`./data`): with WAL enabled the main `hcrm.db` file can be *behind* the
database, so `cp hcrm.db backup/` silently produces a rolled-back snapshot —
in the observed case a pre-migration schema with the `changeme` admin password
and three revoked tokens. The backup script must checkpoint first and verify
the copy; `HCRM_SCRATCH=1` must keep experiments off the live directory.
"""

import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Resolved once, up front: some tests replace PATH with a shim directory, after
# which `bash` would no longer be findable by name.
BASH = shutil.which("bash") or "/bin/bash"


def _make_wal_db(dirpath: str) -> str:
    """A database whose latest write is still only in the WAL.

    Mirrors what was observed on the real `./data` (PLAN-v2 §8.2 F5): schema and
    older rows are in the main file, the newest writes are in `hcrm.db-wal`, so
    copying the main file alone silently rolls the database back.
    """
    db = os.path.join(dirpath, "hcrm.db")
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, name TEXT)")
    con.execute("INSERT INTO users VALUES (1, 'old@x.local', 'before')")
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # fold the baseline in
    con.close()

    # Now write the new row and keep it in the WAL: a second connection holding
    # a read transaction blocks the automatic checkpoint-on-close.
    reader = sqlite3.connect(db)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM users").fetchone()
    writer = sqlite3.connect(db)
    writer.execute("INSERT INTO users VALUES (2, 'admin', 'Dev Administrator')")
    writer.commit()
    writer.close()

    wal = db + "-wal"
    assert os.path.exists(wal) and os.path.getsize(wal) > 0, "test setup: WAL must be non-empty"

    # Prove the premise: a main-file-only copy is a rolled-back snapshot.
    naive = os.path.join(dirpath, "naive-copy.db")
    with open(db, "rb") as src, open(naive, "wb") as dst:
        dst.write(src.read())
    probe = sqlite3.connect(f"file:{naive}?mode=ro", uri=True)
    rows = probe.execute("SELECT id, email FROM users").fetchall()
    probe.close()
    assert rows == [(1, "old@x.local")], f"test setup: naive copy should miss row 2, got {rows}"
    reader.close()
    return db


def test_backup_db_checkpoints_then_verifies():
    with tempfile.TemporaryDirectory(prefix="hcrm-backup-") as td:
        db = _make_wal_db(td)
        dest = os.path.join(td, "backup-dest")
        proc = subprocess.run(
            [BASH, "scripts/backup_db.sh", dest],
            capture_output=True, text=True, timeout=120, cwd=REPO_ROOT,
            env={**os.environ, "HCRM_DATA_DIR": td},
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "checkpoint ok" in proc.stdout
        assert "integrity=ok" in proc.stdout

        backed = os.path.join(dest, "hcrm.db")
        assert os.path.exists(backed)
        # The point of the whole exercise: opening the backup ALONE (no WAL
        # beside it) must show the post-checkpoint state, not a rollback.
        isolated = os.path.join(td, "isolated.db")
        with open(backed, "rb") as src, open(isolated, "wb") as dst:
            dst.write(src.read())
        con = sqlite3.connect(f"file:{isolated}?mode=ro", uri=True)
        rows = con.execute("SELECT id, email, name FROM users ORDER BY id").fetchall()
        con.close()
        assert rows == [(1, "old@x.local", "before"), (2, "admin", "Dev Administrator")], rows

        # The source database is now self-contained too (WAL truncated).
        assert os.path.getsize(db + "-wal") == 0
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
        con.close()


def test_backup_db_refuses_when_there_is_no_database():
    with tempfile.TemporaryDirectory(prefix="hcrm-nobackup-") as td:
        proc = subprocess.run(
            [BASH, "scripts/backup_db.sh"],
            capture_output=True, text=True, timeout=60, cwd=REPO_ROOT,
            env={**os.environ, "HCRM_DATA_DIR": td},
        )
        assert proc.returncode == 2, (proc.returncode, proc.stderr)
        assert "nothing to back up" in proc.stderr


def _uv_shim(dirpath: str, log: str) -> str:
    """A fake `uv` that records the HCRM_DATA_DIR it was invoked with."""
    shim = os.path.join(dirpath, "uv")
    with open(shim, "w", encoding="utf-8") as fh:
        fh.write("#!/usr/bin/env bash\n"
                 f'echo "DATA_DIR=$HCRM_DATA_DIR ARGS=$*" >> "{log}"\n'
                 "exit 0\n")
    os.chmod(shim, os.stat(shim).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return os.path.dirname(shim)


def test_run_sh_scratch_mode_points_away_from_the_live_data_dir():
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-") as td:
        log = os.path.join(td, "calls.log")
        shim_dir = _uv_shim(td, log)
        proc = subprocess.run(
            [BASH, "scripts/run.sh"],
            capture_output=True, text=True, timeout=60, cwd=REPO_ROOT,
            env={**os.environ, "PATH": shim_dir + os.pathsep + os.environ["PATH"],
                 "HCRM_SCRATCH": "1",
                 "HCRM_PORT": "8199", "HCRM_DATA_DIR": ""},
        )
        assert "scratch" in proc.stdout, proc.stdout
        recorded = [l for l in open(log, encoding="utf-8") if "run uvicorn" in l]
        assert recorded, open(log, encoding="utf-8").read()
        data_dir = recorded[-1].split("DATA_DIR=", 1)[1].split(" ", 1)[0]
        assert data_dir and data_dir != os.path.join(REPO_ROOT, "data"), data_dir
        assert os.path.isdir(data_dir), data_dir


def test_run_sh_announces_and_exports_the_resolved_data_dir():
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-live-") as td:
        log = os.path.join(td, "calls.log")
        shim_dir = _uv_shim(td, log)
        env = {k: v for k, v in os.environ.items() if k != "HCRM_DATA_DIR"}
        proc = subprocess.run(
            [BASH, "scripts/run.sh"],
            capture_output=True, text=True, timeout=60, cwd=REPO_ROOT,
            env={**env, "PATH": shim_dir + os.pathsep + env["PATH"], "HCRM_PORT": "8199"},
        )
        live = os.path.join(REPO_ROOT, "data")
        assert f"database:  sqlite:///{live}/hcrm.db" in proc.stdout, proc.stdout
        # The resolved dir is exported, so parent and child cannot disagree.
        assert f"DATA_DIR={live} " in open(log, encoding="utf-8").read()
        # …and the operator is told whether that is live data or a fresh file.
        if os.path.exists(os.path.join(live, "hcrm.db")):
            assert "LIVE DATA" in proc.stdout, proc.stdout
        else:
            assert "first boot will create" in proc.stdout, proc.stdout


def test_python_scripts_are_syntactically_valid():
    """Cheap tripwire: a syntax error in a script is a broken gate, not a warning."""
    for rel in ("scripts/ui_check.py", "scripts/load_test.py", "scripts/smoke_test.py"):
        path = os.path.join(REPO_ROOT, rel)
        if not os.path.exists(path):
            continue
        proc = subprocess.run([sys.executable, "-m", "py_compile", path],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (rel, proc.stderr)
