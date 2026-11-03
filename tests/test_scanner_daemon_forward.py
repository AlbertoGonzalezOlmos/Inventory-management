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
    _Stopped,
    default_pairing_file,
    main,
    save_pairing_file,
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


def run(args, post, reader, scanner_sysfs, *, sleep=lambda s: None,
         clock=None):
    """Drive main() once with everything injected; return its exit code.

    ``sleep`` defaults to a no-op (the grab loop's re-discovery naps must
    never really sleep); retry tests pass a recorder (and, for budget
    exhaustion, a ``clock`` the recorder advances — a monotonic deadline
    cannot be faked with real time and a no-op sleep).
    """
    fake = FakeGrab()
    lock = os.path.join(tempfile.mkdtemp(prefix="hcrm-t5-"), "daemon.lock")
    return main(args, post=post, opener=fake.opener, ioctl=fake.ioctl,
                closer=fake.closer, scan_reader=reader, lock_path=lock,
                sysfs_root=scanner_sysfs, sleep=sleep, clock=clock)


def write_config(tmp_path, terminal_id="counter-1", secret=None):
    secret = secret or "S" * 43
    cfg = tmp_path / "scanner.json"
    cfg.write_text(json.dumps({"terminal_id": terminal_id, "secret": secret}))
    return cfg


# --- register + config ---------------------------------------------------------

def test_register_writes_the_one_click_pairing_file(tmp_path, scanner_sysfs,
                                                    capsys):
    """Register writes the 0600 offer file and does NOT print the code.

    The code is the file's business now (the kiosk browser pairs by
    clicking the pill); printing it by default would put a single-use
    credential in the journal/scrollback — the unit file's own NOTE calls
    that the thing to avoid. --print-code opts in (next test).
    """
    cfg = tmp_path / "scanner.json"
    offer = tmp_path / "scanner-pairing.json"
    post = PostScript([{"pairing_code": PAIRING_CODE}])
    code = run(["--config", str(cfg), "--register",
                "--terminal-id", "counter-1", "--pairing-file", str(offer)],
               post, reader_yielding(), scanner_sysfs)
    assert code == 0
    out = capsys.readouterr().out
    assert PAIRING_CODE not in out            # off by default
    assert str(offer) in out                  # the operator is pointed at it
    # The PR-#35 pin intent survives: the instruction names the pill.
    assert "'no scanner' pill" in out

    assert stat.S_IMODE(offer.stat().st_mode) == 0o600
    saved_offer = json.loads(offer.read_text())
    assert saved_offer == {"terminal_id": "counter-1",
                           "pairing_code": PAIRING_CODE}

    assert cfg.exists()
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    saved = json.loads(cfg.read_text())
    assert saved["terminal_id"] == "counter-1"
    assert len(saved["secret"]) >= 32  # the API's register schema floor


def test_register_print_code_opt_in(tmp_path, scanner_sysfs, capsys):
    cfg = tmp_path / "scanner.json"
    offer = tmp_path / "scanner-pairing.json"
    post = PostScript([{"pairing_code": PAIRING_CODE}])
    code = run(["--config", str(cfg), "--register",
                "--terminal-id", "counter-1", "--pairing-file", str(offer),
                "--print-code"],
               post, reader_yielding(), scanner_sysfs)
    assert code == 0
    assert PAIRING_CODE in capsys.readouterr().out


def test_register_pairing_file_path_precedence(tmp_path, scanner_sysfs,
                                                monkeypatch, capsys):
    """--pairing-file > $HCRM_SCANNER_PAIRING_FILE > the data-dir default."""
    cfg = tmp_path / "scanner.json"
    via_env = tmp_path / "via-env.json"
    via_flag = tmp_path / "via-flag.json"
    post = PostScript([{"pairing_code": PAIRING_CODE}])

    monkeypatch.setenv("HCRM_SCANNER_PAIRING_FILE", str(via_env))
    code = run(["--config", str(cfg), "--register",
                "--terminal-id", "counter-1", "--pairing-file", str(via_flag)],
               post, reader_yielding(), scanner_sysfs)
    assert code == 0
    out = capsys.readouterr().out
    assert str(via_flag) in out and str(via_env) not in out
    assert via_flag.exists() and not via_env.exists()

    # env when no flag (second register round: DELETE happened server-side)
    cfg2 = tmp_path / "scanner2.json"
    post = PostScript([{"pairing_code": PAIRING_CODE}])
    monkeypatch.setenv("HCRM_SCANNER_PAIRING_FILE", str(via_env))
    code = run(["--config", str(cfg2), "--register",
                "--terminal-id", "counter-2"],
               post, reader_yielding(), scanner_sysfs)
    assert code == 0
    assert json.loads(via_env.read_text())["terminal_id"] == "counter-2"


def test_default_pairing_file_matches_the_server_side(monkeypatch):
    """Daemon and server resolve the SAME default path (env and plain).

    The one-click flow dies silently if they drift: the daemon writes where
    the server never looks, and the first click 404s. The app/scanshapes.py
    ↔ scancapture.js cross-language parity-pin precedent.
    """
    from app.routers.scanner import _pairing_file

    assert str(default_pairing_file()) == str(_pairing_file())
    monkeypatch.setenv("HCRM_DATA_DIR", "/somewhere/else")
    assert str(default_pairing_file()) == str(_pairing_file())
    monkeypatch.setenv("HCRM_SCANNER_PAIRING_FILE", "/x/y.json")
    assert str(default_pairing_file()) == str(_pairing_file())


def test_failed_register_writes_no_pairing_file(tmp_path, scanner_sysfs):
    cfg = tmp_path / "scanner.json"
    offer = tmp_path / "scanner-pairing.json"
    post = PostScript([PostError("409: already registered")])
    code = run(["--config", str(cfg), "--register",
                "--terminal-id", "counter-1", "--pairing-file", str(offer)],
               post, reader_yielding(), scanner_sysfs)
    assert code != 0
    assert not offer.exists()   # a failed register leaves no offer behind


# --- --register-wait: bounded, transient-only registration retry -------------


def test_register_wait_retries_transient_then_succeeds(tmp_path, scanner_sysfs,
                                                        capsys):
    """Transient failures (server still booting) retry within the budget;
    the config + offer land exactly as with a first-try success."""
    cfg = tmp_path / "scanner.json"
    offer = tmp_path / "scanner-pairing.json"
    post = PostScript([PostError(0, "refused"), PostError(503, "boom"),
                       PostError(429, "busy"), {"pairing_code": PAIRING_CODE}])
    sleeps = []
    code = run(["--config", str(cfg), "--register", "--register-wait", "10",
                "--terminal-id", "counter-1", "--pairing-file", str(offer)],
               post, reader_yielding(), scanner_sysfs, sleep=sleeps.append)
    assert code == 0
    assert len(post.calls) == 4
    assert all(c["url"].endswith("/api/scanner/terminals") for c in post.calls)
    assert sleeps == [2.0, 2.0, 2.0]
    assert "retrying in 2s" in capsys.readouterr().err
    assert stat.S_IMODE(offer.stat().st_mode) == 0o600
    saved = json.loads(offer.read_text())
    assert saved == {"terminal_id": "counter-1", "pairing_code": PAIRING_CODE}


def test_register_wait_default_is_a_single_attempt(tmp_path, scanner_sysfs,
                                                   capsys):
    """No --register-wait (the default, and the systemd unit's contract):
    one attempt, today's loud failure — and no "budget" line, because the
    default path never had a budget to exhaust."""
    cfg = tmp_path / "scanner.json"
    offer = tmp_path / "scanner-pairing.json"
    post = PostScript([PostError(0, "refused"), {"pairing_code": PAIRING_CODE}])
    sleeps = []
    code = run(["--config", str(cfg), "--register",
                "--terminal-id", "counter-1", "--pairing-file", str(offer)],
               post, reader_yielding(), scanner_sysfs, sleep=sleeps.append)
    assert code == 1
    assert len(post.calls) == 1
    assert sleeps == []
    err = capsys.readouterr().err
    assert "registration failed" in err          # still exactly as loud
    assert "budget" not in err                    # ...but claims no budget
    assert not offer.exists()


def test_register_wait_never_retries_permanent_failures(tmp_path, scanner_sysfs):
    """A 409 (terminal already registered) is an operator problem, not a
    transient one: one attempt, loud exit, regardless of the budget."""
    cfg = tmp_path / "scanner.json"
    offer = tmp_path / "scanner-pairing.json"
    post = PostScript([PostError(409, "already registered")])
    sleeps = []
    code = run(["--config", str(cfg), "--register", "--register-wait", "300",
                "--terminal-id", "counter-1", "--pairing-file", str(offer)],
               post, reader_yielding(), scanner_sysfs, sleep=sleeps.append)
    assert code == 1
    assert len(post.calls) == 1
    assert sleeps == []
    assert not offer.exists()


def test_register_wait_non_integer_status_is_permanent(tmp_path, scanner_sysfs):
    """Legacy callers pass string statuses; .transient's ``status >= 500``
    would TypeError on them — the guard must classify, never crash (this is
    what keeps test_failed_register_writes_no_pairing_file green)."""
    cfg = tmp_path / "scanner.json"
    offer = tmp_path / "scanner-pairing.json"
    post = PostScript([PostError("409: already registered")])
    code = run(["--config", str(cfg), "--register", "--register-wait", "300",
                "--terminal-id", "counter-1", "--pairing-file", str(offer)],
               post, reader_yielding(), scanner_sysfs, sleep=lambda s: None)
    assert code == 1
    assert len(post.calls) == 1
    assert not offer.exists()


def test_register_wait_budget_exhaustion_is_loud(tmp_path, scanner_sysfs,
                                                 capsys):
    """A always-transient outage exhausts the budget and re-raises: the
    daemon's exit-1-with-one-line contract, with the budget named."""
    cfg = tmp_path / "scanner.json"
    offer = tmp_path / "scanner-pairing.json"
    post = PostScript([PostError(0, "refused")] * 5)
    now = [0.0]
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds          # advance the fake clock with each nap

    code = run(["--config", str(cfg), "--register", "--register-wait", "6",
                "--terminal-id", "counter-1", "--pairing-file", str(offer)],
               post, reader_yielding(), scanner_sysfs,
               sleep=fake_sleep, clock=lambda: now[0])
    assert code == 1
    err = capsys.readouterr().err
    assert "registration failed" in err
    assert "budget of 6s exhausted after 4 attempt(s)" in err
    assert sleeps == [2.0, 2.0, 2.0]
    assert not offer.exists()
    assert not cfg.exists()


def test_register_wait_caps_final_sleep_to_the_remaining_budget(tmp_path,
                                                                 scanner_sysfs):
    """The last pause is shortened, not allowed to overrun the time budget."""
    cfg = tmp_path / "scanner.json"
    post = PostScript([PostError(0, "refused")] * 4)
    now = [0.0]
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    code = run(["--config", str(cfg), "--register", "--register-wait", "5"],
               post, reader_yielding(), scanner_sysfs,
               sleep=fake_sleep, clock=lambda: now[0])
    assert code == 1
    assert len(post.calls) == 4       # t=0, 2, 4 and the final attempt at t=5
    assert sleeps == [2.0, 2.0, 1.0]
    assert not cfg.exists()


@pytest.mark.parametrize("value", ["-1", "nan", "inf"])
def test_register_wait_rejects_invalid_budgets_before_any_post(
        tmp_path, scanner_sysfs, value):
    cfg = tmp_path / "scanner.json"
    post = PostScript([])
    with pytest.raises(SystemExit) as exc:
        run(["--config", str(cfg), "--register", "--register-wait", value],
            post, reader_yielding(), scanner_sysfs)
    assert exc.value.code == 2
    assert post.calls == []
    assert not cfg.exists()


def test_stopped_during_register_wait_exits_clean(tmp_path, scanner_sysfs):
    """SIGTERM while waiting out the retry budget: a clean exit 0, no
    traceback, and no half-written state."""
    cfg = tmp_path / "scanner.json"
    offer = tmp_path / "scanner-pairing.json"
    post = PostScript([PostError(0, "refused")])

    def stop_sleep(seconds):
        raise _Stopped()

    code = run(["--config", str(cfg), "--register", "--register-wait", "10",
                "--terminal-id", "counter-1", "--pairing-file", str(offer)],
               post, reader_yielding(), scanner_sysfs, sleep=stop_sleep)
    assert code == 0
    assert not cfg.exists()
    assert not offer.exists()


def test_register_only_registers_without_grabbing(tmp_path, scanner_sysfs):
    """--register-only: config + offer written, the grab path never runs;
    a second run with the saved config registers nothing and leaves the
    (possibly consumed) offer alone."""
    cfg = tmp_path / "scanner.json"
    offer = tmp_path / "scanner-pairing.json"

    def reader_must_not_run(fd, on_unmapped=None):
        raise AssertionError("the grab path must not run under --register-only")

    post = PostScript([{"pairing_code": PAIRING_CODE}])
    code = run(["--config", str(cfg), "--register", "--register-only",
                "--terminal-id", "counter-1", "--pairing-file", str(offer)],
               post, reader_must_not_run, scanner_sysfs)
    assert code == 0
    assert len(post.calls) == 1
    assert stat.S_IMODE(offer.stat().st_mode) == 0o600
    saved = json.loads(offer.read_text())
    assert saved == {"terminal_id": "counter-1", "pairing_code": PAIRING_CODE}

    # Second run: the config exists, so no re-registration, offer untouched.
    post2 = PostScript([])
    code = run(["--config", str(cfg), "--register-only",
                "--terminal-id", "counter-1", "--pairing-file", str(offer)],
               post2, reader_must_not_run, scanner_sysfs)
    assert code == 0
    assert post2.calls == []
    assert json.loads(offer.read_text()) == saved


def test_re_register_overwrites_a_stale_pairing_file(tmp_path, scanner_sysfs):
    cfg = tmp_path / "scanner.json"
    offer = tmp_path / "scanner-pairing.json"
    offer.write_text("{garbage from an aborted round}")
    post = PostScript([{"pairing_code": PAIRING_CODE}])
    code = run(["--config", str(cfg), "--register",
                "--terminal-id", "counter-1", "--pairing-file", str(offer)],
               post, reader_yielding(), scanner_sysfs)
    assert code == 0
    assert json.loads(offer.read_text())["pairing_code"] == PAIRING_CODE


def test_unwritable_pairing_file_is_fatal(tmp_path, scanner_sysfs, capsys):
    """A failed offer-file write exits non-zero with the recovery text.

    With the code not printed, the file is its only copy — losing it
    silently would strand the registration (nothing here exits silently).
    Unwritable is simulated with a FILE where the parent directory should
    be (mkdir raises NotADirectoryError) — chmod tricks are unreliable
    under a root CI runner.
    """
    cfg = tmp_path / "scanner.json"
    blocker = tmp_path / "blocker"
    blocker.write_text("in the way")
    offer = blocker / "scanner-pairing.json"
    post = PostScript([{"pairing_code": PAIRING_CODE}])
    code = run(["--config", str(cfg), "--register",
                "--terminal-id", "counter-1", "--pairing-file", str(offer)],
               post, reader_yielding(), scanner_sysfs)
    assert code == 1
    err = capsys.readouterr().err
    assert "could not write the pairing file" in err
    assert "DELETE /api/scanner/terminals/counter-1" in err


def test_existing_config_is_reused_no_register(tmp_path, scanner_sysfs):
    secret = "T" * 43
    cfg = write_config(tmp_path, secret=secret)
    # An unconsumed offer from an earlier register must survive a normal
    # start — the pill stays clickable; only --register rewrites the file.
    offer = tmp_path / "scanner-pairing.json"
    offer.write_text(json.dumps({"terminal_id": "counter-1",
                                 "pairing_code": PAIRING_CODE}))
    post = PostScript([{}])  # one ingest; a register call needs a script entry
    code = run(["--config", str(cfg), "--pairing-file", str(offer)], post,
               reader_yielding("8412345678905"), scanner_sysfs)
    assert code == 0
    # No register POST, and the ingest carried the CONFIG's secret.
    assert not any(c["url"].endswith("/terminals") for c in post.calls)
    assert post.calls[0]["headers"]["X-Scanner-Secret"] == secret
    assert json.loads(offer.read_text())["pairing_code"] == PAIRING_CODE


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
