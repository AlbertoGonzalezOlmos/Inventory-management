"""Tests for the round-4 T5 forwarding/pairing/config layer
(scripts/scanner_daemon.py: main, Forwarder, Spool).

No network, no device: ``post``/the grab triple (opener/ioctl/closer, the
T3 fixture pattern) and ``scan_reader`` are all injected, and ``sysfs_root``
points at a fake tree. The loop is stopped the way real life stops it —
SIGTERM (via ``signal.raise_signal``, mid-reader) and KeyboardInterrupt —
asserting the clean-exit contract: exit 0, fd closed, grab released.

The invariants under test (docs/plans/r4t5-FINAL.md): register prints the
single-use pairing code and writes the 0600 config; every scan is POSTed
with the secret; a failed POST retries and NEVER kills the grab loop;
``--dry-run`` issues zero HTTP; badge payloads are redacted on every print
path; a server outage spools and replays with per-entry dedupe.
"""

import json
import os
import signal
import stat
import tempfile

import pytest

from scripts.scanner_daemon import (
    EVIOCGRAB,
    Forwarder,
    PostError,
    Spool,
    main,
)
from tests.test_scanner_daemon import _make_event_node, _input_class
from tests.test_scanner_daemon_grab import FakeGrab

BADGE = "HCRM1:" + "A" * 43
RAW_TOKEN = "B" * 43
PAIRING_CODE = "7Q2MB4XW"  # the 8-char Crockford shape the API mints


@pytest.fixture()
def scanner_sysfs(tmp_path):
    root = _input_class(tmp_path)
    _make_event_node(root, "event3", "065a", "a001")
    return root


class PostScript:
    """Injectable `post`: scripted outcomes, full call recording.

    Each call pops one outcome — an exception (a failure) or a dict (a
    success); once the script is exhausted, calls succeed with ``{}``.
    """

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, url, headers, body):
        self.calls.append({"url": url, "headers": dict(headers),
                           "body": dict(body)})
        outcome = self.outcomes.pop(0) if self.outcomes else {}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def codes(self):
        """The codes POSTed to the events endpoint, in order."""
        return [c["body"]["code"] for c in self.calls
                if c["url"].endswith("/api/scanner/events")]


def reader_yielding(*codes, then=KeyboardInterrupt):
    """scan_reader stand-in: yield the codes, then stop the daemon the way
    SIGINT does (the reader runs inside main's with-blocks, so the
    exception unwinds exactly the real stop path: fd close → release)."""

    def reader(fd, on_unmapped=None):
        yield from codes
        if then is not None:
            raise then()
        return

    return reader


def run(args, post, reader, scanner_sysfs):
    """Drive main() once with everything injected; return its exit code."""
    fake = FakeGrab()
    lock = os.path.join(tempfile.mkdtemp(prefix="hcrm-t5-"), "daemon.lock")
    return main(args, post=post, opener=fake.opener, ioctl=fake.ioctl,
                closer=fake.closer, scan_reader=reader, lock_path=lock,
                sysfs_root=scanner_sysfs, sleep=lambda s: None)


def write_config(tmp_path, terminal_id="counter-1", secret=None):
    secret = secret or "S" * 43
    cfg = tmp_path / "scanner.json"
    cfg.write_text(json.dumps({"terminal_id": terminal_id, "secret": secret}))
    return cfg


# --- register + config ---------------------------------------------------------

def test_register_prints_pairing_code_and_writes_0600_config(
        tmp_path, scanner_sysfs, capsys):
    cfg = tmp_path / "scanner.json"
    post = PostScript([{"pairing_code": PAIRING_CODE}])
    code = run(["--config", str(cfg), "--register",
                "--terminal-id", "counter-1"],
               post, reader_yielding(), scanner_sysfs)
    assert code == 0
    out = capsys.readouterr().out
    assert PAIRING_CODE in out
    # The instruction must point at the header-pill flow (the login-screen
    # pairing panel is gone — login-screen cleanup, PR #35).
    assert "'no scanner' pill" in out

    assert cfg.exists()
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    saved = json.loads(cfg.read_text())
    assert saved["terminal_id"] == "counter-1"
    assert len(saved["secret"]) >= 32  # the API's register schema floor


def test_existing_config_is_reused_no_register(tmp_path, scanner_sysfs):
    secret = "T" * 43
    cfg = write_config(tmp_path, secret=secret)
    post = PostScript([{}])  # one ingest; a register call needs a script entry
    code = run(["--config", str(cfg)], post,
               reader_yielding("8412345678905"), scanner_sysfs)
    assert code == 0
    # No register POST, and the ingest carried the CONFIG's secret.
    assert not any(c["url"].endswith("/terminals") for c in post.calls)
    assert post.calls[0]["headers"]["X-Scanner-Secret"] == secret


def test_register_conflict_exits_loud(tmp_path, scanner_sysfs, capsys):
    cfg = tmp_path / "scanner.json"
    post = PostScript([PostError(409, "already registered")])
    code = run(["--config", str(cfg), "--register",
                "--terminal-id", "counter-1"],
               post, reader_yielding(), scanner_sysfs)
    assert code == 1
    assert "409" in capsys.readouterr().err


def test_trailing_slash_forward_url_is_normalised(tmp_path, scanner_sysfs):
    # --forward http://host:8000/ must not produce //api/scanner/... — the
    # register and events URLs are normalised the same way.
    cfg = tmp_path / "scanner.json"
    post = PostScript([{"pairing_code": PAIRING_CODE}])
    code = run(["--config", str(cfg), "--register",
                "--forward", "http://x:8000/", "--terminal-id", "counter-1"],
               post, reader_yielding(), scanner_sysfs)
    assert code == 0
    assert post.calls[0]["url"] == "http://x:8000/api/scanner/terminals"


# --- forwarding ------------------------------------------------------------------

def test_scan_is_posted_with_the_secret(tmp_path, scanner_sysfs):
    cfg = write_config(tmp_path, secret="T" * 43)
    post = PostScript([{}, {}])
    code = run(["--config", str(cfg)], post,
               reader_yielding("8412345678905", "EX-007"), scanner_sysfs)
    assert code == 0
    assert post.codes() == ["8412345678905", "EX-007"]
    assert all(c["url"].endswith("/api/scanner/events") for c in post.calls)
    assert all(c["headers"]["X-Scanner-Secret"] == "T" * 43
               for c in post.calls)


def test_failed_post_retries_and_keeps_the_loop_alive(tmp_path, scanner_sysfs):
    cfg = write_config(tmp_path)
    # The server is down for A's first two attempts, then heals; B and C
    # arrive while the outage is ongoing and must still be decoded.
    post = PostScript([PostError(0, "refused"), PostError(0, "refused"),
                       {}, {}, {}])
    code = run(["--config", str(cfg)], post,
               reader_yielding("A", "B", "C"), scanner_sysfs)
    assert code == 0
    # A was retried head-first (fail, fail, then success) and B, C were
    # delivered in order — the loop never died during the outage.
    assert post.codes() == ["A", "A", "A", "B", "C"]


def test_permanent_rejection_is_loud_and_spooled(tmp_path, scanner_sysfs):
    cfg = write_config(tmp_path)
    # 401 on both the in-loop attempt and the final drain: the terminal
    # was deleted server-side. The daemon must SURVIVE (keep grabbing) and
    # keep the scan spooled, not drop it or spin.
    post = PostScript([PostError(401, "unknown terminal"),
                       PostError(401, "unknown terminal")])
    code = run(["--config", str(cfg)], post,
               reader_yielding("8412345678905"), scanner_sysfs)
    assert code == 0
    assert (tmp_path / "scanner.json.spool").read_text().splitlines() == \
        ["8412345678905"]


# --- dry-run ------------------------------------------------------------------------

def test_dry_run_zero_http_and_redacted(tmp_path, scanner_sysfs, capsys):
    post = PostScript([])  # exhausted script still records; we assert none
    code = run(["--dry-run"], post,
               reader_yielding("8412345678905", BADGE, RAW_TOKEN),
               scanner_sysfs)
    assert code == 0
    assert post.calls == []               # ZERO HTTP
    out = capsys.readouterr().out
    assert "8412345678905" in out         # barcodes print raw
    assert "HCRM1:••••" in out            # badges never do
    assert "•••• (badge token)" in out    # …in either form
    assert "A" * 43 not in out and "B" * 43 not in out


def test_dry_run_needs_no_config(tmp_path, scanner_sysfs):
    code = run(["--dry-run"], PostScript([]),
               reader_yielding("EX-007"), scanner_sysfs)
    assert code == 0


# --- signals -------------------------------------------------------------------------

def test_sigterm_is_a_clean_exit_fd_closed(tmp_path, scanner_sysfs):
    cfg = write_config(tmp_path)

    def reader(fd, on_unmapped=None):
        yield "8412345678905"
        signal.raise_signal(signal.SIGTERM)  # delivered HERE, mid-loop
        return

    fake = FakeGrab()
    post = PostScript([{}])
    lock = os.path.join(tempfile.mkdtemp(prefix="hcrm-t5-"), "daemon.lock")
    code = main(["--config", str(cfg)], post=post, opener=fake.opener,
                ioctl=fake.ioctl, closer=fake.closer, scan_reader=reader,
                lock_path=lock, sysfs_root=scanner_sysfs,
                sleep=lambda s: None)
    assert code == 0
    assert fake.releases() == [(101, EVIOCGRAB, 0)]  # explicit un-grab…
    assert fake.closed == [101]                       # …and fd closed
    assert post.codes() == ["8412345678905"]          # the scan was delivered


# --- spool: outage → replay → dedupe ----------------------------------------------------

def test_spool_replays_across_a_restart_with_dedupe(tmp_path, scanner_sysfs):
    cfg = write_config(tmp_path)
    spool_path = tmp_path / "scanner.json.spool"

    # Run 1: the server is DOWN; two scans spool; SIGTERM stops the daemon.
    def sigterm_reader(fd, on_unmapped=None):
        yield "A"
        yield "B"
        signal.raise_signal(signal.SIGTERM)
        return

    down = PostScript([PostError(0, "refused"), PostError(0, "refused"),
                       PostError(0, "refused")])  # 2 scans + the final drain
    assert run(["--config", str(cfg)], down, sigterm_reader,
               scanner_sysfs) == 0
    assert spool_path.read_text().splitlines() == ["A", "B"]
    assert down.codes() == ["A", "A", "A"]  # head-first retry, in order

    # Run 2: the server is BACK; the restarted daemon replays the backlog
    # at STARTUP — each entry delivered exactly once (per-entry dedupe).
    up = PostScript([{}, {}])
    assert run(["--config", str(cfg)], up, reader_yielding(),
               scanner_sysfs) == 0
    assert up.codes() == ["A", "B"]
    assert not spool_path.exists()          # drained: the file is removed


def test_forwarder_two_identical_scans_are_two_entries():
    # "Dedupe" is per ENTRY, never per code: a re-scan of the same barcode
    # is another unit of goods, not a duplicate to collapse.
    post = PostScript([{}, {}])
    spool = Spool(path=None)
    forwarder = Forwarder(post, "http://x/api/scanner/events", "S" * 43,
                          spool, log=lambda m: None)
    forwarder.handle("8412345678905")
    forwarder.handle("8412345678905")
    assert post.codes() == ["8412345678905", "8412345678905"]
    assert spool.pending() == []


def test_spool_is_bounded_and_file_is_0600(tmp_path):
    path = tmp_path / "spool"
    spool = Spool(path=path, cap=3, log=lambda m: None)
    for code in ("1", "2", "3", "4"):
        spool.add(code)
    assert spool.pending() == ["2", "3", "4"]      # oldest dropped
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for _ in range(3):
        spool.remove_oldest()
    assert spool.pending() == []
    assert not path.exists()                       # empty → no file
