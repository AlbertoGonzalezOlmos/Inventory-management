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


def _uv_shim(dirpath: str, log: str, uvicorn_exit: int | None = None, *,
             find_exit: int = 0, find_text: str | None = None,
             ready_exit: int = 0) -> str:
    """A fake `uv` that records the HCRM_DATA_DIR it was invoked with.

    ``uvicorn_exit`` makes shimmed *uvicorn* invocations exit with that code
    (everything else still exits 0), so a test can pin run.sh's exit-code
    propagation without a real server. Shimmed uvicorns also sleep briefly
    first: run.sh backgrounds the scanner daemon after uvicorn and shuts it
    down once uvicorn exits, so without the grace period the daemon's log
    line races that TERM (and loses ~half the time) — no assertion on the
    daemon's argv would be deterministic.

    The boot verdict (run.sh's read-only preflight) needs two more knobs:
    ``find_text`` is canned `--find-scanner` stdout and ``find_exit`` its
    status (0/1 are completed reports), and ``ready_exit`` fails the
    server-readiness poll so the "not ready → skip" path is testable. Both
    branches are matched BEFORE the generic exit 0, and the readiness probe
    is the only `python -c` run.sh makes.
    """
    shim = os.path.join(dirpath, "uv")
    find_file = os.path.join(dirpath, "find-scanner.out")
    if find_text is not None:
        with open(find_file, "w", encoding="utf-8") as fh:
            fh.write(find_text)
    uv_exit = f"exit {uvicorn_exit}" if uvicorn_exit is not None else ":"
    with open(shim, "w", encoding="utf-8") as fh:
        fh.write("#!/usr/bin/env bash\n"
                 f'echo "DATA_DIR=$HCRM_DATA_DIR ARGS=$*" >> "{log}"\n'
                 'case "$*" in\n'
                 f'  *uvicorn*) sleep 0.3; {uv_exit} ;;\n'
                 f'  *--find-scanner*) [ -f "{find_file}" ] && cat "{find_file}"; '
                 f'exit {find_exit} ;;\n'
                 f'  *"import socket"*) exit {ready_exit} ;;\n'
                 "esac\n"
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
              "HCRM_CATALOGUE_CSV", "HCRM_CATALOGUE_IMPORT",
              "HCRM_CATALOGUE_IMAGES_DIR",
              "HCRM_SCANNER_DAEMON", "HCRM_SCANNER_PAIRING_FILE",
              "HCRM_SCANNER_REGISTER_WAIT", "HCRM_SCANNER_CONFIG",
              "HCRM_SCANNER_REQUIRE_HOST_IDENTITY",
              "HCRM_ALLOW_INSECURE_BIND")


def _run_sh_with_shim(td, log, *, uvicorn_exit=None, find_exit=0,
                      find_text=None, ready_exit=0, **extra_env):
    """Run scripts/run.sh against the uv shim; return (proc, calls)."""
    shim_dir = _uv_shim(td, log, uvicorn_exit=uvicorn_exit,
                        find_exit=find_exit, find_text=find_text,
                        ready_exit=ready_exit)
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
                 # The boot preflight and the readiness poll are FOREGROUND
                 # calls: holding either would defer run.sh's signal trap and
                 # hang the signal test inside $( ) — they must never match the
                 # exec-sleep branches below.
                 '  *--find-scanner*|"*import socket"*) exit 0 ;;\n'
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


# --- the boot verdict (C3): a read-only preflight, never fatal ----------------
#
# run.sh prints --find-scanner's FAIL/WARN/verdict lines on the boot console
# BEFORE it spawns the daemon (so the report never describes our own lock/grab).
# The uv shim owns that verdict: a boot-verdict test must not need a real
# server, a real DB or a device.


def _scanner_data_dir(td):
    """A data dir with the identity files a real kiosk boot already has.

    Their presence is what makes the preflight run at all (a first boot has
    no config yet — the daemon is registering right then), so every verdict
    test creates them, and asserts afterwards that the boot left them alone.
    """
    data = os.path.join(td, "data")
    os.makedirs(data, exist_ok=True)
    cfg = os.path.join(data, "scanner.json")
    offer = os.path.join(data, "scanner-pairing.json")
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.write('{"terminal_id": "hertha-shop", "secret": "SECRETMARK"}')
    with open(offer, "w", encoding="utf-8") as fh:
        fh.write('{"terminal_id": "hertha-shop", "pairing_code": "CODE1234"}')
    return data, cfg, offer


def _blocking_report(data):
    """Canned `--find-scanner` output: realistic markers, padding, em-dashes."""
    return "\n".join([
        "scanner_daemon --find-scanner: READ-ONLY report — nothing below was "
        "written, locked or kept grabbed",
        f"OK   config {data}/scanner.json: terminal_id='hertha-shop', secret "
        "present (43 chars — never printed)",
        "FAIL scanner 065a:a001 NOT ATTACHED — no /dev/input/eventN matches in sysfs.",
        "INFO another scanner_daemon IS RUNNING (lockfile /run/user/1000/x.lock, PID 4242)",
        "OK   server up at http://127.0.0.1:8199 (GET /api/scanner/events → 401)",
        "WARN it is already PAIRED: the read token went to WHICHEVER browser paired first",
        "FAIL offer file names terminal 'a' but the daemon config is 'b' — the pill "
        "would pair an identity this daemon does not hold",
        "FAIL verdict: scans CANNOT reliably reach a browser on this host "
        "(2 blocking finding(s): device-absent, offer)",
        "",
        "recovery plan (ONE ladder — printed for the operator, NOTHING here is executed):",
        "  1. hardware: the scanner is off the bus — re-plug it",
    ]) + "\n"


def _daemon_calls(calls):
    """The real daemon spawn(s) — never the preflight, which shares the path."""
    return [c for c in calls if "scanner_daemon.py" in c and "--find-scanner" not in c]


def _preflight_calls(calls):
    return [c for c in calls if "--find-scanner" in c]


def test_run_sh_prints_the_preflight_verdict_before_the_daemon_spawns():
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-verdict-") as td:
        data, cfg, offer = _scanner_data_dir(td)
        report = _blocking_report(data)
        before = (open(cfg, encoding="utf-8").read(),
                  open(offer, encoding="utf-8").read())
        proc, calls = _run_sh_with_shim(
            td, os.path.join(td, "calls.log"), HCRM_DATA_DIR=data,
            find_exit=1, find_text=report)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        # (1) the diagnosis, prefixed, on the operator's console
        assert "scanner:   FAIL scanner 065a:a001 NOT ATTACHED" in proc.stdout, proc.stdout
        assert "scanner:   WARN it is already PAIRED" in proc.stdout, proc.stdout
        assert ("scanner:   FAIL verdict: scans CANNOT reliably reach a browser"
                in proc.stdout), proc.stdout
        # …and ONLY FAIL/WARN/verdict: no header, no INFO, no ladder, no
        # OK chatter — and never key material from the report
        for excluded in ("READ-ONLY report", "INFO another scanner_daemon",
                         "recovery plan", "OK   config ", "OK   server up",
                         "SECRETMARK", "CODE1234"):
            assert excluded not in proc.stdout, (excluded, proc.stdout)
        # (5) preflight BEFORE the spawn, reusing the daemon's resolution
        daemons, preflights = _daemon_calls(calls), _preflight_calls(calls)
        assert len(daemons) == 1 and len(preflights) == 1, calls
        assert calls.index(preflights[0]) < calls.index(daemons[0]), calls
        for flag in ("--find-scanner", f"--config {cfg}",
                     f"--pairing-file {offer}",
                     "--forward http://127.0.0.1:8199"):
            assert flag in preflights[0], preflights[0]
        for forbidden in ("--register", "--print-code", "--dry-run"):
            assert forbidden not in preflights[0], preflights[0]
        # (4) the daemon argv is unchanged by the preflight
        assert f"--config {data}/scanner.json" in daemons[0], daemons[0]
        assert f"--pairing-file {data}/scanner-pairing.json" in daemons[0], daemons[0]
        assert "--register-wait 300" in daemons[0], daemons[0]
        assert "scanner:   daemon pid " in proc.stdout, proc.stdout
        # read-only: the boot left both identity files byte-identical
        assert (open(cfg, encoding="utf-8").read(),
                open(offer, encoding="utf-8").read()) == before


def test_run_sh_boot_survives_a_blocking_preflight_verdict():
    """(2) a non-zero verdict is the POINT, not a boot failure."""
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-nonfatal-") as td:
        data, _cfg, _offer = _scanner_data_dir(td)
        proc, calls = _run_sh_with_shim(
            td, os.path.join(td, "calls.log"), HCRM_DATA_DIR=data,
            find_exit=1, find_text=_blocking_report(data))
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert "scanner:   daemon pid " in proc.stdout, proc.stdout
        assert len(_daemon_calls(calls)) == 1, calls


def test_run_sh_preflight_does_not_mask_the_uvicorn_exit_code():
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-verdict-rc-") as td:
        data, _cfg, _offer = _scanner_data_dir(td)
        proc, _calls = _run_sh_with_shim(
            td, os.path.join(td, "calls.log"), HCRM_DATA_DIR=data,
            uvicorn_exit=3, find_exit=1, find_text=_blocking_report(data))
        assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)


@pytest.mark.parametrize(("extra_env", "banner"), [
    ({"HCRM_SCRATCH": "1"}, "daemon skipped"),
    # the strong case: a config EXISTS in the data dir, so only the opt-out
    # can keep the preflight from running
    ({"HCRM_SCANNER_DAEMON": "0"}, "daemon disabled"),
    ({"HCRM_HOST": "192.168.1.5", "HCRM_DEV_ADMIN": "0"},
     "no 127.0.0.1/[::1] listener"),
])
def test_run_sh_skips_the_preflight_when_the_daemon_is_skipped(extra_env, banner):
    """(3) scratch / opt-out / unforwardable bind: no preflight call, no FAIL."""
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-nopre-") as td:
        data, _cfg, _offer = _scanner_data_dir(td)
        proc, calls = _run_sh_with_shim(
            td, os.path.join(td, "calls.log"), HCRM_DATA_DIR=data,
            find_exit=1, find_text=_blocking_report(data), **extra_env)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert _preflight_calls(calls) == [], calls
        assert banner in proc.stdout, proc.stdout
        assert "scanner:   FAIL" not in proc.stdout, proc.stdout


def test_run_sh_unreachable_server_skips_the_preflight_without_a_false_fail():
    """(6) a cold server must produce the bounded skip, never a FAIL."""
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-notch-") as td:
        data, _cfg, _offer = _scanner_data_dir(td)
        proc, calls = _run_sh_with_shim(
            td, os.path.join(td, "calls.log"), HCRM_DATA_DIR=data,
            ready_exit=1, find_exit=1, find_text=_blocking_report(data))
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert _preflight_calls(calls) == [], calls
        assert "not accepting connections within 10 s" in proc.stdout, proc.stdout
        # never a false FAIL from inside the script that starts the server,
        # and the boot still proceeded to the daemon
        assert "scanner:   FAIL" not in proc.stdout, proc.stdout
        assert "scanner:   daemon pid " in proc.stdout, proc.stdout


def test_run_sh_preflight_with_no_output_does_not_abort_the_boot():
    """The filter must survive grep's no-match exit under `pipefail`."""
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-silent-") as td:
        data, _cfg, _offer = _scanner_data_dir(td)
        proc, calls = _run_sh_with_shim(
            td, os.path.join(td, "calls.log"), HCRM_DATA_DIR=data,
            find_exit=0, find_text=None)
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert len(_preflight_calls(calls)) == 1, calls
        assert _preflight_calls(calls)[0].endswith(" --find-scanner --config "
            + f"{data}/scanner.json --pairing-file {data}/scanner-pairing.json "
            "--forward http://127.0.0.1:8199"), _preflight_calls(calls)[0]
        assert "scanner:   daemon pid " in proc.stdout, proc.stdout


def test_run_sh_preflight_forwards_the_ipv6_loopback_bracketed():
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-pre6-") as td:
        data, _cfg, _offer = _scanner_data_dir(td)
        proc, calls = _run_sh_with_shim(
            td, os.path.join(td, "calls.log"), HCRM_DATA_DIR=data,
            HCRM_HOST="::1", find_exit=1, find_text=_blocking_report(data))
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        preflights = _preflight_calls(calls)
        assert preflights, calls
        assert "--forward http://[::1]:8199" in preflights[0], preflights[0]


def test_run_sh_parses_the_forward_url_without_shell_colon_surgery():
    """`${X%%:*}` mangles `http://[::1]:8000`; the probe must use urllib."""
    text = open(os.path.join(REPO_ROOT, "scripts", "run.sh"),
                encoding="utf-8").read()
    assert "urlsplit" in text, "the readiness probe must parse the URL"
    assert "SCANNER_FORWARD%%:" not in text and "SCANNER_FORWARD%:" not in text


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
        # The unmissable banner says only what is unconditionally true: you
        # are in scratch mode, and this boot's data dir (TMPDIR-derived).
        assert "scratch:   ON — experiments (HCRM_SCRATCH=1); unset it to run the shop" in proc.stdout, proc.stdout
        assert "this boot's data dir: " in proc.stdout, proc.stdout
        scratch_dir = [l.split("data dir: ", 1)[1].strip()
                       for l in proc.stdout.splitlines()
                       if "this boot's data dir: " in l][0]
        assert os.path.isdir(scratch_dir), scratch_dir
        assert scratch_dir.startswith(tempfile.gettempdir()), scratch_dir
        assert not any("scanner_daemon.py" in c for c in calls), calls


def test_run_sh_scratch_banner_stays_true_under_the_surviving_overrides():
    """DATABASE_URL and HCRM_CATALOGUE_CSV still apply in scratch mode; the
    banner must stay true anyway (it names this boot's data dir and the shop
    switch, and leaves the DB/CSV to the lines that already report them)."""
    with tempfile.TemporaryDirectory(prefix="hcrm-runsh-scratch-ovr-") as td:
        proc, _ = _run_sh_with_shim(
            td, os.path.join(td, "calls.log"), HCRM_SCRATCH="1",
            DATABASE_URL="sqlite:////tmp/elsewhere.db",
            HCRM_CATALOGUE_CSV="/tmp/supplier.csv")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "scratch:   ON — experiments (HCRM_SCRATCH=1); unset it to run the shop" in proc.stdout
        assert "this boot's data dir: " in proc.stdout
        # the overrides are reported by their own lines, not by the banner
        assert "database:  sqlite:////tmp/elsewhere.db" in proc.stdout, proc.stdout


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
