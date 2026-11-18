"""Tests for the pairing-fallback diagnostics (scripts/scanner_daemon.py:
--find-scanner, find_scanner, probe_daemon_lock, probe_grab,
inspect_terminals_db, _registration_recovery via main).

The fixture set is the 2026-11-17 incident in miniature: a FOREIGN paired
terminal owning the config secret, an offer file naming an UNKNOWN terminal,
a running daemon holding the lock (and, via FakeGrab EBUSY, the exclusive
grab), and an absent device. Everything is offline: fake sysfs
(tests.test_scanner_daemon helpers), a real stdlib sqlite built in tmp_path
(read-only-opened by the code under test), scripted http_get, FakeGrab.

Doctrine under test: --find-scanner is READ-ONLY (no file created or
touched, no lock held afterwards, the probe grab released), it says "a
daemon is running and holds the grab" IN PLAIN WORDS instead of failing
with a lock error, secrets and hashes are never printed, destructive
recovery steps are printed as exact commands and NEVER executed, and the
exit code is 0 only when the delivery chain verifies usable.
"""

import errno
import hashlib
import json
import os
import sqlite3

import pytest

from scripts.scanner_daemon import (
    DaemonLock,
    PostError,
    default_config_file,
    default_db_file,
    find_scanner,
    inspect_terminals_db,
    main,
    probe_daemon_lock,
    probe_grab,
    save_pairing_file,
)
from tests.test_scanner_daemon import _input_class, _make_event_node
from tests.test_scanner_daemon_grab import FakeGrab

SECRET = "S" * 43
FOREIGN = "pentacosiarca-ThinkPad-T14s-Gen-4"
LOCAL = "hertha-shop"


@pytest.fixture()
def scanner_sysfs(tmp_path):
    root = _input_class(tmp_path)
    _make_event_node(root, "event3", "065a", "a001")
    return root


@pytest.fixture()
def empty_sysfs(tmp_path):
    return _input_class(tmp_path)


def make_db(tmp_path, rows):
    """A real sqlite file with the terminals schema. rows: (id, secret,
    paired, last_seen). Returns the path; the code under test may only ever
    open it mode=ro."""
    db = tmp_path / "hcrm.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE terminals (id INTEGER PRIMARY KEY, terminal_id TEXT "
        "UNIQUE, secret_hash TEXT, token_hash TEXT, pairing_code_hash TEXT, "
        "created_at TEXT, last_seen_at TEXT)")
    for i, (tid, secret, paired, seen) in enumerate(rows, 1):
        conn.execute(
            "INSERT INTO terminals VALUES (?,?,?,?,?,?,?)",
            (i, tid, hashlib.sha256(secret.encode()).hexdigest(),
             "T" * 64 if paired else None, "P" * 64, "2026-11-03", seen))
    conn.commit()
    conn.close()
    return db


def write_config(tmp_path, terminal_id=LOCAL, secret=SECRET, name="scanner.json"):
    cfg = tmp_path / name
    cfg.write_text(json.dumps({"terminal_id": terminal_id, "secret": secret}))
    return cfg


def find(tmp_path, *, cfg=None, offer=None, db=None, sysfs=None,
         lock_path=None, grab=None, http_status=401):
    """Drive find_scanner with everything injected; return (text, code)."""
    fake = grab or FakeGrab()
    lines, code = find_scanner(
        config_path=cfg, pairing_path=offer, db_path=db,
        base_url="http://127.0.0.1:8000",
        sysfs_root=sysfs if sysfs is not None else _input_class(tmp_path),
        lock_path=lock_path or (tmp_path / "daemon.lock"),
        opener=fake.opener, ioctl=fake.ioctl, closer=fake.closer,
        http_get=lambda url: http_status)
    return "\n".join(lines), code


# --- the healthy chain ---------------------------------------------------------

def test_find_scanner_healthy_chain_exits_zero(tmp_path, scanner_sysfs):
    cfg = write_config(tmp_path)
    offer = tmp_path / "scanner-pairing.json"
    save_pairing_file(offer, LOCAL, "7Q2MB4XW")
    db = make_db(tmp_path, [(LOCAL, SECRET, False, "2026-11-17")])
    text, code = find(tmp_path, cfg=cfg, offer=offer, db=db, sysfs=scanner_sysfs)
    assert code == 0
    assert "FAIL" not in text
    assert LOCAL in text
    assert "offer file consistent" in text


# --- BUG A: the stale three-way identity mismatch ------------------------------

def test_find_scanner_reproduces_the_three_way_mismatch(tmp_path, scanner_sysfs):
    """The live incident: config carries a FOREIGN paired terminal's identity,
    the offer names a terminal the server does not know → the pill 401s and
    scans land in a queue no local browser can read. The report must name
    both identities and print the exact recovery commands (never run them)."""
    cfg = write_config(tmp_path, terminal_id=FOREIGN)
    offer = tmp_path / "scanner-pairing.json"
    save_pairing_file(offer, LOCAL, "7Q2MB4XW")
    db = make_db(tmp_path, [(FOREIGN, SECRET, True, "2026-11-03 05:56:54")])
    text, code = find(tmp_path, cfg=cfg, offer=offer, db=db, sysfs=scanner_sysfs)
    assert code == 1
    assert FOREIGN in text and LOCAL in text
    # the offer's terminal is unknown to the server → the pill's 401 named
    assert "bad_pairing" in text
    # the paired-elsewhere trap is said in plain words
    assert "WHICHEVER browser paired first" in text
    # ONE ladder with the exact destructive commands, printed not executed
    assert "recovery plan" in text
    assert f"curl -X DELETE http://127.0.0.1:8000/api/scanner/terminals/{FOREIGN}" in text
    assert "--register" in text
    assert "'no scanner' pill" in text
    # the DB was only read: the foreign terminal still exists
    assert inspect_terminals_db(db) is not None
    assert [r["terminal_id"] for r in inspect_terminals_db(db)] == [FOREIGN]


def test_find_scanner_flags_a_secret_owned_by_another_terminal(tmp_path,
                                                               scanner_sysfs):
    """Config terminal_id unknown + its secret owned by a DIFFERENT registered
    terminal (a copied config): both findings, and the DELETE names the
    secret's owner."""
    cfg = write_config(tmp_path, terminal_id="ghost", secret=SECRET)
    db = make_db(tmp_path, [(FOREIGN, SECRET, True, None)])
    text, code = find(tmp_path, cfg=cfg, offer=tmp_path / "none.json", db=db,
                      sysfs=scanner_sysfs)
    assert code == 1
    assert "FOREIGN IDENTITY" in text
    assert "UNKNOWN to the server" in text
    assert f"terminals/{FOREIGN}" in text


# --- the device, the grab and the running daemon --------------------------------

def test_find_scanner_device_absent_is_loud_and_points_at_hardware(
        tmp_path, empty_sysfs):
    cfg = write_config(tmp_path)
    db = make_db(tmp_path, [(LOCAL, SECRET, True, None)])
    text, code = find(tmp_path, cfg=cfg, offer=tmp_path / "none.json", db=db,
                      sysfs=empty_sysfs)
    assert code == 1
    assert "NOT ATTACHED" in text
    assert "usb_host_check.py" in text
    assert "error -71" in text          # the journal signature to grep for
    assert "hardware:" in text          # and the ladder's hardware step


def test_find_scanner_detects_running_daemon_and_held_grab(tmp_path,
                                                           scanner_sysfs):
    """A live lock holder + EBUSY from the grab probe are FINDINGS in plain
    words (PID named, kernel-design silence explained) — not a lock error,
    and not a failure by themselves: a healthy identity + a running daemon
    is normal operation → exit 0."""
    cfg = write_config(tmp_path)
    offer = tmp_path / "scanner-pairing.json"
    save_pairing_file(offer, LOCAL, "7Q2MB4XW")
    db = make_db(tmp_path, [(LOCAL, SECRET, False, None)])
    lock_path = tmp_path / "daemon.lock"
    lock = DaemonLock(lock_path)
    lock.acquire()
    try:
        grab = FakeGrab(grab_error=OSError(errno.EBUSY, "Device or resource busy"))
        text, code = find(tmp_path, cfg=cfg, offer=offer, db=db,
                          sysfs=scanner_sysfs, lock_path=lock_path, grab=grab)
    finally:
        lock.release()
    assert code == 0
    assert "IS RUNNING" in text and str(os.getpid()) in text
    assert "EXCLUSIVE grab (EVIOCGRAB) is held" in text
    assert "sees NOTHING" in text
    assert grab.closed                     # the probe fd was closed again


def test_find_scanner_grab_permission_denied_is_a_warning(tmp_path,
                                                          scanner_sysfs):
    cfg = write_config(tmp_path)
    db = make_db(tmp_path, [(LOCAL, SECRET, True, None)])
    grab = FakeGrab(open_error=OSError(errno.EACCES, "Permission denied"))
    text, code = find(tmp_path, cfg=cfg, offer=tmp_path / "none.json", db=db,
                      sysfs=scanner_sysfs, grab=grab)
    assert code == 0                       # a permission WARN, not a blocker
    assert "no permission to open/grab" in text
    assert "udev" in text


def test_probe_grab_releases_a_successful_probe(tmp_path, scanner_sysfs):
    """A free node: the probe grabs AND un-grabs AND closes — the report
    never keeps what it probed."""
    fake = FakeGrab()
    assert probe_grab("/dev/input/event3", opener=fake.opener,
                      ioctl=fake.ioctl, closer=fake.closer) == "free"
    grabs = [c for c in fake.ioctl_log if c[2] == 1]
    releases = [c for c in fake.ioctl_log if c[2] == 0]
    assert len(grabs) == 1 and len(releases) == 1
    assert fake.closed


def test_probe_daemon_lock_never_creates_the_lockfile(tmp_path):
    lock_path = tmp_path / "never-created.lock"
    result = probe_daemon_lock(lock_path)
    assert result["running"] is False and result["pid"] is None
    assert not lock_path.exists()


def test_probe_daemon_lock_reports_holder_and_stale_pid(tmp_path):
    lock_path = tmp_path / "daemon.lock"
    lock = DaemonLock(lock_path)
    lock.acquire()
    try:
        held = probe_daemon_lock(lock_path)
        assert held["running"] is True and held["pid"] == os.getpid()
    finally:
        lock.release()
    free = probe_daemon_lock(lock_path)
    assert free["running"] is False and free["pid"] == os.getpid()  # stale content


# --- read-only doctrine ----------------------------------------------------------

def test_find_scanner_is_read_only(tmp_path, scanner_sysfs):
    """No file created, none modified, the lock left free, no traceback:
    the report mode must be runnable against a LIVE ./data-equivalent."""
    cfg = write_config(tmp_path, terminal_id=FOREIGN)
    offer = tmp_path / "scanner-pairing.json"
    save_pairing_file(offer, LOCAL, "7Q2MB4XW")
    db = make_db(tmp_path, [(FOREIGN, SECRET, True, None)])
    lock_path = tmp_path / "daemon.lock"
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns)
              for p in (cfg, offer, db)}
    listing_before = sorted(str(p) for p in tmp_path.rglob("*"))
    text, code = find(tmp_path, cfg=cfg, offer=offer, db=db,
                      sysfs=scanner_sysfs, lock_path=lock_path)
    assert code == 1
    assert "Traceback" not in text
    for p, (blob, mtime) in before.items():
        assert p.read_bytes() == blob and p.stat().st_mtime_ns == mtime
    assert sorted(str(p) for p in tmp_path.rglob("*")) == listing_before
    # the probe left the lock free (acquirable) — and created no lockfile
    assert not lock_path.exists()


def test_find_scanner_never_prints_secrets_or_hashes(tmp_path, scanner_sysfs):
    cfg = write_config(tmp_path, terminal_id=FOREIGN)
    offer = tmp_path / "scanner-pairing.json"
    save_pairing_file(offer, LOCAL, "7Q2MB4XW")   # the code must not leak either
    db = make_db(tmp_path, [(FOREIGN, SECRET, True, None)])
    text, _ = find(tmp_path, cfg=cfg, offer=offer, db=db, sysfs=scanner_sysfs)
    assert SECRET not in text
    assert hashlib.sha256(SECRET.encode()).hexdigest() not in text
    assert "7Q2MB4XW" not in text


# --- server + database degradation ------------------------------------------------

def test_find_scanner_server_unreachable_is_a_blocker(tmp_path, scanner_sysfs):
    cfg = write_config(tmp_path)
    db = make_db(tmp_path, [(LOCAL, SECRET, True, None)])
    text, code = find(tmp_path, cfg=cfg, offer=tmp_path / "none.json", db=db,
                      sysfs=scanner_sysfs, http_status=0)
    assert code == 1
    assert "UNREACHABLE" in text and "run.sh" in text


def test_find_scanner_without_local_db_is_unverified_but_usable(
        tmp_path, scanner_sysfs):
    """Split-host deployment: no readable DB → the identity is UNVERIFIED
    (WARN), and with device + server + config present and no contradiction
    the chain still exits 0."""
    cfg = write_config(tmp_path)
    text, code = find(tmp_path, cfg=cfg, offer=tmp_path / "none.json",
                      db=tmp_path / "missing.db", sysfs=scanner_sysfs)
    assert code == 0
    assert "UNVERIFIED" in text


def test_find_scanner_missing_config_is_a_blocker(tmp_path, scanner_sysfs):
    db = make_db(tmp_path, [(LOCAL, SECRET, True, None)])
    text, code = find(tmp_path, cfg=tmp_path / "absent.json",
                      offer=tmp_path / "none.json", db=db, sysfs=scanner_sysfs)
    assert code == 1
    assert "no usable terminal config" in text


def test_inspect_terminals_db_matches_secrets_without_leaking_them(tmp_path):
    db = make_db(tmp_path, [(LOCAL, SECRET, True, None), ("other", "X" * 43, False, None)])
    rows = inspect_terminals_db(db, secret=SECRET)
    assert [r["terminal_id"] for r in rows] == [LOCAL, "other"]
    assert rows[0]["secret_matches"] is True and rows[0]["paired"] is True
    assert rows[1]["secret_matches"] is False and rows[1]["paired"] is False
    assert inspect_terminals_db(tmp_path / "absent.db") is None
    assert all("secret_hash" not in r for r in rows)   # only the bool leaves


# --- default paths + main() wiring -------------------------------------------------

def test_default_config_and_db_paths_follow_the_data_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("HCRM_DATA_DIR", str(tmp_path))
    assert default_config_file() == tmp_path / "scanner.json"
    assert default_db_file() == tmp_path / "hcrm.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/other.db")
    assert default_db_file() == tmp_path / "other.db"


def test_main_find_scanner_runs_bare_with_defaults(tmp_path, monkeypatch,
                                                   scanner_sysfs, capsys):
    """`--find-scanner` with NO --config/--pairing-file/--db flags: main
    probes the HCRM_DATA_DIR defaults, prints the report to stdout and
    returns the verdict as the exit code."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("HCRM_DATA_DIR", str(tmp_path))
    write_config(tmp_path)                       # <data>/scanner.json
    save_pairing_file(tmp_path / "scanner-pairing.json", LOCAL, "7Q2MB4XW")
    make_db(tmp_path, [(LOCAL, SECRET, False, None)])
    fake = FakeGrab()
    lock = DaemonLock(tmp_path / "live.lock")
    lock.acquire()                               # simulate the running daemon
    try:
        code = main(["--find-scanner"], opener=fake.opener, ioctl=fake.ioctl,
                    closer=fake.closer, lock_path=str(tmp_path / "live.lock"),
                    sysfs_root=scanner_sysfs, http_get=lambda url: 401)
    finally:
        lock.release()
    out = capsys.readouterr().out
    assert code == 0
    assert LOCAL in out and "READ-ONLY" in out
    assert "IS RUNNING" in out


def test_main_find_scanner_needs_no_device_and_takes_no_lock(tmp_path,
                                                             empty_sysfs, capsys):
    """The trap from the incident: a daemon holds the lock — --find-scanner
    must still run (no InstanceAlreadyRunning, exit code from the verdict)."""
    monkey_db = make_db(tmp_path, [(LOCAL, SECRET, True, None)])
    write_config(tmp_path)
    lock_path = tmp_path / "daemon.lock"
    lock = DaemonLock(lock_path)
    lock.acquire()
    try:
        code = main(["--find-scanner", "--config", str(tmp_path / "scanner.json"),
                     "--pairing-file", str(tmp_path / "none.json")],
                    opener=FakeGrab().opener, ioctl=FakeGrab().ioctl,
                    closer=FakeGrab().closer, lock_path=str(lock_path),
                    sysfs_root=empty_sysfs, db_path=str(monkey_db),
                    http_get=lambda url: 401)
    finally:
        lock.release()
    captured = capsys.readouterr()
    assert code == 1                      # device absent → loud
    assert "NOT ATTACHED" in captured.out
    # the held lock must never surface as a lock ERROR in this mode
    assert "instance is already running" not in captured.err
    assert captured.err == ""


# --- malformed offer files keep today's findings (D6, review fix) -----------
# A PARSED-but-malformed offer (a list, an empty object, a non-string or
# empty terminal_id) keeps the two FAIL findings the pre-refactor code
# printed — loud, never swallowed, never mistaken for an absent file.

@pytest.mark.parametrize(("blob", "offer_tid"), [
    ("[1, 2]", None),
    ("{}", None),
    ('{"terminal_id": 42}', 42),
    ('{"terminal_id": ""}', ""),
])
def test_find_scanner_malformed_offer_keeps_todays_findings(
        tmp_path, scanner_sysfs, blob, offer_tid):
    cfg = write_config(tmp_path)
    db = make_db(tmp_path, [(LOCAL, SECRET, True, None)])
    offer = tmp_path / "scanner-pairing.json"
    offer.write_text(blob)
    text, code = find(tmp_path, cfg=cfg, offer=offer, db=db,
                      sysfs=scanner_sysfs)
    assert (f"FAIL offer file names terminal {offer_tid!r} but the daemon "
            f"config is '{LOCAL}'") in text
    assert (f"FAIL offer file names {offer_tid!r}, which the server does NOT "
            "know") in text
    assert code == 1
    # an absent offer is a different, quieter thing — never the same finding
    text2, _ = find(tmp_path, cfg=cfg, offer=tmp_path / "absent.json",
                    db=db, sysfs=scanner_sysfs)
    assert "FAIL offer file names" not in text2
    assert "no offer file at" in text2
