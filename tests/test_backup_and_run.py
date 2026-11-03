"""W1.4 — live-data safety: scripts/backup_db.sh and run.sh's data-dir handling.

The failure mode these pin (PLAN-v2 §8.2 F5 / §9.1, observed on the real
`./data`): with WAL enabled the main `hcrm.db` file can be *behind* the
database, so `cp hcrm.db backup/` silently produces a rolled-back snapshot —
in the observed case a pre-migration schema with the `changeme` admin password
and three revoked tokens. The backup script must checkpoint first and verify
the copy; `HCRM_SCRATCH=1` must keep experiments off the live directory.
"""

import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

import pytest

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


def _uv_shim(dirpath: str, log: str, uvicorn_exit: int | None = None) -> str:
    """A fake `uv` that records the HCRM_DATA_DIR it was invoked with.

    ``uvicorn_exit`` makes shimmed *uvicorn* invocations exit with that code
    (everything else still exits 0), so a test can pin run.sh's exit-code
    propagation without a real server. Shimmed uvicorns also sleep briefly
    first: run.sh backgrounds the scanner daemon after uvicorn and shuts it
    down once uvicorn exits, so without the grace period the daemon's log
    line races that TERM (and loses ~half the time) — no assertion on the
    daemon's argv would be deterministic.
    """
    shim = os.path.join(dirpath, "uv")
    uv_exit = f"exit {uvicorn_exit}" if uvicorn_exit is not None else ":"
    with open(shim, "w", encoding="utf-8") as fh:
        fh.write("#!/usr/bin/env bash\n"
                 f'echo "DATA_DIR=$HCRM_DATA_DIR ARGS=$*" >> "{log}"\n'
                 f'case "$*" in *uvicorn*) sleep 0.3; {uv_exit};; esac\n'
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
        # Strip every variable that would change which database run.sh announces
        # or exports — with DATABASE_URL exported the script prints it instead of
        # the sqlite path and the test fails for a reason unrelated to run.sh.
        env = {k: v for k, v in os.environ.items()
               if k not in ("HCRM_DATA_DIR", "DATABASE_URL", "HCRM_SCRATCH")}
        proc = subprocess.run(
            [BASH, "scripts/run.sh"],
            capture_output=True, text=True, timeout=60, cwd=REPO_ROOT,
            env={**env, "PATH": shim_dir + os.pathsep + env["PATH"], "HCRM_PORT": "8199"},
        )
        # Compare REAL paths, not strings: run.sh prints bash's $PWD (which
        # keeps or resolves symlinks depending on how it was invoked) while
        # REPO_ROOT comes from Python — under a symlinked checkout (macOS maps
        # /tmp -> /private/tmp) the two spellings legitimately differ.
        live = os.path.realpath(os.path.join(REPO_ROOT, "data"))
        announced = re.search(r"^database:  sqlite:///(.+)/hcrm\.db$",
                              proc.stdout, re.M)
        assert announced, proc.stdout
        assert os.path.realpath(announced.group(1)) == live, proc.stdout
        # The resolved dir is exported, so parent and child cannot disagree.
        exported = re.search(r"^DATA_DIR=(\S+) ",
                             open(log, encoding="utf-8").read(), re.M)
        assert exported, open(log, encoding="utf-8").read()
        assert os.path.realpath(exported.group(1)) == live
        # …and the operator is told whether that is live data or a fresh file.
        if os.path.exists(os.path.join(live, "hcrm.db")):
            assert "LIVE DATA" in proc.stdout, proc.stdout
        else:
            assert "first boot will create" in proc.stdout, proc.stdout


# --- run.sh as the two-process kiosk start (uvicorn + scanner daemon) --------


def _call_index(calls, needle):
    """Index of the first recorded uv call containing `needle`."""
    for i, call in enumerate(calls):
        if needle in call:
            return i
    raise AssertionError(f"{needle!r} not in recorded calls: {calls}")


# Env vars that would silently change what run.sh starts: a developer with
# any of these exported must still see every test pass (the tests set the
# defaults they intend explicitly).
_STRIP_ENV = ("HCRM_DATA_DIR", "DATABASE_URL", "HCRM_SCRATCH",
              "HCRM_SCANNER_DAEMON", "HCRM_SCANNER_PAIRING_FILE",
              "HCRM_SCANNER_REGISTER_WAIT", "HCRM_SCANNER_CONFIG",
              "HCRM_ALLOW_INSECURE_BIND")


def _run_sh_with_shim(td, log, *, uvicorn_exit=None, **extra_env):
    """Run scripts/run.sh against the uv shim; return (proc, calls)."""
    shim_dir = _uv_shim(td, log, uvicorn_exit=uvicorn_exit)
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({"PATH": shim_dir + os.pathsep + env["PATH"],
                "HCRM_PORT": "8199"})
    env.update(extra_env)
    proc = subprocess.run(
        [BASH, "scripts/run.sh"], capture_output=True, text=True,
        timeout=60, cwd=REPO_ROOT, env=env)
    calls = []
    if os.path.exists(log):
        calls = open(log, encoding="utf-8").read().splitlines()
    return proc, calls


def test_run_sh_starts_scanner_daemon_with_app():
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-scan-") as td:
        data = os.path.join(td, "data")
        proc, calls = _run_sh_with_shim(
            td, os.path.join(td, "calls.log"), HCRM_DATA_DIR=data)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        # The boot gates still run first, before EITHER process is spawned.
        # (uvicorn and the daemon are both backgrounded, so their two log
        # lines race — only "gates before both" is deterministic.)
        gates = _call_index(calls, "build_i18n.py --check")
        server = _call_index(calls, "run uvicorn")
        daemon = _call_index(calls, "scripts/scanner_daemon.py")
        assert gates < server and gates < daemon, calls
        daemon_call = calls[daemon]
        assert f"--config {data}/scanner.json" in daemon_call, daemon_call
        assert "--forward http://127.0.0.1:8199" in daemon_call, daemon_call
        assert f"--pairing-file {data}/scanner-pairing.json" in daemon_call
        assert "--register-wait 300" in daemon_call, daemon_call
        assert "scanner:   daemon pid " in proc.stdout, proc.stdout


def test_run_sh_scratch_mode_skips_the_scanner_daemon():
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-scratch-") as td:
        proc, calls = _run_sh_with_shim(
            td, os.path.join(td, "calls.log"), HCRM_SCRATCH="1")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not any("scanner_daemon.py" in c for c in calls), calls
        assert "daemon skipped" in proc.stdout, proc.stdout


def test_run_sh_scanner_daemon_opt_out():
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-optout-") as td:
        data = os.path.join(td, "data")
        proc, calls = _run_sh_with_shim(
            td, os.path.join(td, "calls.log"),
            HCRM_SCANNER_DAEMON="0", HCRM_DATA_DIR=data)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not any("scanner_daemon.py" in c for c in calls), calls
        assert any("run uvicorn" in c for c in calls), calls
        assert "daemon disabled" in proc.stdout, proc.stdout


def test_run_sh_propagates_the_uvicorn_exit_code():
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-exit-") as td:
        data = os.path.join(td, "data")
        proc, calls = _run_sh_with_shim(
            td, os.path.join(td, "calls.log"),
            uvicorn_exit=3, HCRM_DATA_DIR=data)
        assert proc.returncode == 3, (proc.returncode, proc.stdout,
                                      proc.stderr)
        # the daemon was started and still did not turn into a hang
        assert any("scanner_daemon.py" in c for c in calls), calls


def test_run_sh_scanner_env_overrides_flow_into_the_daemon_argv():
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-env-") as td:
        offer = os.path.join(td, "offer.json")
        proc, calls = _run_sh_with_shim(
            td, os.path.join(td, "calls.log"),
            HCRM_DATA_DIR=os.path.join(td, "data"),
            HCRM_SCANNER_PAIRING_FILE=offer,
            HCRM_SCANNER_REGISTER_WAIT="5")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        daemon_call = calls[_call_index(calls, "scripts/scanner_daemon.py")]
        assert f"--pairing-file {offer}" in daemon_call, daemon_call
        assert "--register-wait 5" in daemon_call, daemon_call


def test_run_sh_ipv6_only_bind_forwards_to_ipv6_loopback():
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-v6-") as td:
        proc, calls = _run_sh_with_shim(
            td, os.path.join(td, "calls.log"),
            HCRM_DATA_DIR=os.path.join(td, "data"), HCRM_HOST="::1")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        daemon_call = calls[_call_index(calls, "scripts/scanner_daemon.py")]
        assert "--forward http://[::1]:8199" in daemon_call, daemon_call
        assert "--host ::1" in calls[_call_index(calls, "run uvicorn")]


def test_run_sh_non_loopback_bind_skips_the_daemon_with_a_warning():
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-lan-") as td:
        proc, calls = _run_sh_with_shim(
            td, os.path.join(td, "calls.log"),
            HCRM_DATA_DIR=os.path.join(td, "data"), HCRM_HOST="192.168.1.5",
            HCRM_DEV_ADMIN="0")  # a non-loopback bind + dev admin would refuse
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not any("scanner_daemon.py" in c for c in calls), calls
        assert any("run uvicorn" in c for c in calls), calls
        assert "gives the daemon no 127.0.0.1/[::1] listener" in proc.stdout, proc.stdout
        assert "HCRM_HOST=192.168.1.5" in proc.stdout, proc.stdout


def test_run_sh_refusal_spawns_no_processes():
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-refuse-") as td:
        log = os.path.join(td, "calls.log")
        proc, calls = _run_sh_with_shim(
            td, log, HCRM_HOST="0.0.0.0", HCRM_DEV_ADMIN="1")
        assert proc.returncode == 1, (proc.returncode, proc.stderr[-500:])
        assert "refusing to bind" in proc.stderr
        # the guard fires before ANY uv invocation — nothing is spawned
        assert calls == [], calls


def _uv_hold_shim(dirpath: str, log: str) -> str:
    """A uv shim whose two app children wait for signals and record their PIDs."""
    shim = os.path.join(dirpath, "uv")
    with open(shim, "w", encoding="utf-8") as fh:
        fh.write("#!/usr/bin/env bash\n"
                 f'echo "DATA_DIR=$HCRM_DATA_DIR ARGS=$*" >> "{log}"\n'
                 'case "$*" in\n'
                 f'  *uvicorn*) echo $$ > "{dirpath}/server.pid"; exec sleep 30 ;;\n'
                 f'  *scanner_daemon.py*) echo $$ > "{dirpath}/daemon.pid"; exec sleep 30 ;;\n'
                 "esac\n"
                 "exit 0\n")
    os.chmod(shim, os.stat(shim).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return os.path.dirname(shim)


def _pid_when_ready(proc, path: str) -> int:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with open(path, encoding="ascii") as fh:
                return int(fh.read().strip())
        except (FileNotFoundError, ValueError):
            pass
        if proc.poll() is not None:
            raise AssertionError(f"run.sh exited before creating {path}: {proc.returncode}")
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


@pytest.mark.parametrize(("sig", "expected"), [
    (signal.SIGTERM, 143),
    (signal.SIGINT, 130),
])
def test_run_sh_signal_reaps_both_children_and_preserves_signal_status(sig, expected):
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-signal-") as td:
        log = os.path.join(td, "calls.log")
        shim_dir = _uv_hold_shim(td, log)
        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        env.update({"PATH": shim_dir + os.pathsep + env["PATH"],
                    "HCRM_DATA_DIR": os.path.join(td, "data"),
                    "HCRM_PORT": "8199"})
        proc = subprocess.Popen(
            [BASH, "scripts/run.sh"], cwd=REPO_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children = []
        try:
            children = [_pid_when_ready(proc, os.path.join(td, "server.pid")),
                        _pid_when_ready(proc, os.path.join(td, "daemon.pid"))]
            proc.send_signal(sig)
            out, err = proc.communicate(timeout=15)
            assert proc.returncode == expected, (proc.returncode, out, err)
            for pid in children:
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=10)
            for pid in children:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_python_scripts_are_syntactically_valid():
    """Cheap tripwire: a syntax error in a script is a broken gate, not a warning."""
    for rel in ("scripts/ui_check.py", "scripts/load_test.py", "scripts/smoke_test.py",
                "scripts/_i18n.py", "scripts/build_i18n.py", "scripts/i18n_check.py"):
        path = os.path.join(REPO_ROOT, rel)
        if not os.path.exists(path):
            continue
        proc = subprocess.run([sys.executable, "-m", "py_compile", path],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (rel, proc.stderr)


def test_backup_includes_the_badge_key_and_warns():
    """Task A: database + badge.key together derive every member's QR badge —
    the backup must carry the key (or re-viewing is lost on restore) and say
    loudly that the backup is credential-bearing."""
    with tempfile.TemporaryDirectory(prefix="hcrm-badgekey-") as td:
        _make_wal_db(td)   # creates <td>/hcrm.db (+WAL) — td IS the data dir
        # A badge key, as the app would auto-generate it.
        with open(os.path.join(td, "badge.key"), "w") as f:
            f.write("k" * 43)
        dest = os.path.join(td, "backup-dest")
        proc = subprocess.run(
            [BASH, "scripts/backup_db.sh", dest],
            capture_output=True, text=True, timeout=120, cwd=REPO_ROOT,
            env={**os.environ, "HCRM_DATA_DIR": td},
        )
        assert proc.returncode == 0, proc.stderr
        copied = open(os.path.join(dest, "badge.key")).read()
        assert copied == "k" * 43
        # The loud warning: a credential-bearing backup must announce itself.
        assert "credential-bearing" in proc.stderr
        # …and the restore hint mentions the key.
        assert "badge.key" in proc.stdout


def test_backup_succeeds_without_a_badge_key():
    """Deployments that keep the key in env (never on disk) back up unchanged."""
    with tempfile.TemporaryDirectory(prefix="hcrm-nokey-") as td:
        _make_wal_db(td)   # td IS the data dir
        dest = os.path.join(td, "backup-dest")
        proc = subprocess.run(
            [BASH, "scripts/backup_db.sh", dest],
            capture_output=True, text=True, timeout=120, cwd=REPO_ROOT,
            env={**os.environ, "HCRM_DATA_DIR": td},
        )
        assert proc.returncode == 0, proc.stderr
        assert not os.path.exists(os.path.join(dest, "badge.key"))
        assert "credential-bearing" not in proc.stderr


# --- HCRM_SCRATCH: strictly parsed (1/0/true/false/yes/no/on/off) --------------


def test_run_sh_scratch_off_values_use_the_live_dir_and_start_the_daemon():
    for value in ("0", "false", ""):
        with tempfile.TemporaryDirectory(prefix="hcrm-runsh-scratchoff-") as td:
            data = os.path.join(td, "data")
            proc, calls = _run_sh_with_shim(
                td, os.path.join(td, "calls.log"),
                HCRM_DATA_DIR=data, HCRM_SCRATCH=value)
            assert proc.returncode == 0, proc.stdout + proc.stderr
            assert "scratch:   off" in proc.stdout, proc.stdout
            assert "(scratch" not in proc.stdout, proc.stdout
            # not a scratch run: the SAME resolved data dir reaches uvicorn
            # and the scanner daemon starts (item 1 keys off $SCRATCH).
            server_call = calls[_call_index(calls, "run uvicorn")]
            assert server_call.startswith(f"DATA_DIR={data} "), server_call
            assert any("scanner_daemon.py" in c for c in calls), calls


def test_run_sh_scratch_on_announces_and_skips_the_daemon():
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-scratchon-") as td:
        proc, calls = _run_sh_with_shim(
            td, os.path.join(td, "calls.log"), HCRM_SCRATCH="1")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "scratch:   on" in proc.stdout, proc.stdout
        assert "(scratch" in proc.stdout, proc.stdout
        assert not any("scanner_daemon.py" in c for c in calls), calls


def test_run_sh_scratch_unknown_value_refuses_to_boot():
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-scratchbad-") as td:
        log = os.path.join(td, "calls.log")
        proc, calls = _run_sh_with_shim(
            td, log, HCRM_DATA_DIR=os.path.join(td, "data"),
            HCRM_SCRATCH="maybe")
        assert proc.returncode == 1, (proc.returncode, proc.stdout,
                                      proc.stderr)
        assert "refusing to boot" in proc.stderr
        assert "HCRM_SCRATCH=maybe" in proc.stderr
        assert calls == [], calls  # nothing is spawned on a refused boot
