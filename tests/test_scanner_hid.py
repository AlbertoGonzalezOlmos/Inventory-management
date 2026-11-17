"""Tests for scripts/scanner_hid.py — HID boot-keyboard report parsing.

Offline: the pure parsing/grouping functions are exercised with synthetic
reports shaped exactly like the ones a keyboard-mode barcode scanner emits
(8-byte boot reports: [modifier, reserved, usage x6]).
"""

import pytest

from scripts.scanner_hid import (
    ScanAssembler,
    format_csv_row,
    group_scans_from_reports,
    parse_keyboard_report,
)


def report(*usages, modifier=0x00):
    """Build an 8-byte boot-keyboard report (report ID already stripped)."""
    keys = list(usages) + [0] * (6 - len(usages))
    return bytes([modifier, 0x00, *keys[:6]])


USAGE = {c: 4 + ord(c.lower()) - ord("a") for c in "abcdefghijklmnopqrstuvwxyz"}
# HID usages 30..38 = '1'..'9', usage 39 = '0'
USAGE.update({str((d + 1) % 10): 30 + d for d in range(10)})
DIGITS_SHIFTED = ")!@#$%^&*("


@pytest.mark.parametrize("char,usage", [
    ("a", USAGE["a"]), ("z", USAGE["z"]),
    ("0", USAGE["0"]), ("9", USAGE["9"]),
    ("5", USAGE["5"]),
])
def test_single_keys(char, usage):
    assert parse_keyboard_report(report(usage)) == (char, False)


def test_shifted_characters():
    # Shift+'1' = '!' ; Shift+'a' = 'A' ; Shift+'0' (usage 39) = ')'
    assert parse_keyboard_report(report(USAGE["1"], modifier=0x02))[0] == "!"
    assert parse_keyboard_report(report(USAGE["a"], modifier=0x20))[0] == "A"
    assert parse_keyboard_report(report(USAGE["0"], modifier=0x02))[0] == ")"


def test_digit_usages_are_hid_standard():
    # 30..38 = '1'..'9', 39 = '0' (HID Usage Tables, keyboard page)
    assert USAGE["1"] == 30 and USAGE["9"] == 38 and USAGE["0"] == 39
    assert parse_keyboard_report(report(39))[0] == "0"


def test_enter_terminates():
    text, enter = parse_keyboard_report(report(40))
    assert (text, enter) == ("", True)


def test_comma_usage_is_supported():
    # Usage 54 is ','; barcodes are not restricted to GTIN characters.
    assert parse_keyboard_report(report(54)) == (",", False)


def test_report_with_text_and_enter_keeps_the_text():
    # A scanner may pack the final character and its CR suffix into one report;
    # the character must not be dropped (previously it was).
    assert parse_keyboard_report(report(USAGE["5"], 40)) == ("5", True)
    assert group_scans_from_reports(
        [report(USAGE["4"]), report(USAGE["5"], 40)]
    ) == ["45"]


def test_scan_assembler_is_the_shared_runtime_path():
    # The live hidraw/hidapi loops feed this same object, so this test covers
    # the code that actually runs (not just a parallel copy of it).
    assembler = ScanAssembler()
    assert assembler.feed(report(USAGE["a"])) is None
    assert assembler.feed(report(40)) == "a"
    assert assembler.feed(report(40)) is None  # empty Enter emits nothing


def test_rollover_and_padding_ignored():
    # ErrorRollOver (0x01 in every slot — phantom state) yields nothing.
    assert parse_keyboard_report(bytes([0, 0, 1, 1, 1, 1, 1, 1])) == ("", False)
    # All-zero report (key released) yields nothing.
    assert parse_keyboard_report(bytes(8)) == ("", False)


def test_multi_key_report():
    assert parse_keyboard_report(report(USAGE["a"], USAGE["b"])) == ("ab", False)


def test_short_and_empty_reports_are_safe():
    assert parse_keyboard_report(b"") == ("", False)
    assert parse_keyboard_report(b"\x00\x00") == ("", False)


def _spell(text: str) -> list[bytes]:
    """Reports a keyboard-mode scanner emits to 'type' text (key + release)."""
    reports = []
    for ch in text:
        modifier = 0x02 if ch.isupper() else 0x00
        base = ch.lower()
        if base == "-":
            usage = 45
        else:
            usage = USAGE[base]
        reports.append(report(usage, modifier=modifier))
        reports.append(bytes(8))  # key release between presses (real devices)
    return reports


def test_group_scans_single():
    reports = _spell("8412345678905") + [report(40)]
    assert group_scans_from_reports(reports) == ["8412345678905"]


def test_group_scans_multiple_and_trailing_partial():
    reports = (
        _spell("EX-001") + [report(40)]
        + _spell("4006381333931") + [report(40)]
        + _spell("partial")            # never terminated → not emitted
    )
    assert group_scans_from_reports(reports) == ["EX-001", "4006381333931"]


def test_group_scans_empty_enter_ignored():
    # Enter with an empty buffer (double suffix, idle scanner) emits nothing.
    assert group_scans_from_reports([report(40), report(40)]) == []


def test_uppercase_uses_shift_modifier():
    # 'E' = shift + usage 0x08; scanners in "uppercase" mode emit it that way.
    reports = _spell("EX1") + [report(40)]
    assert group_scans_from_reports(reports) == ["EX1"]
    assert reports[0][0] == 0x02  # shift modifier set for the uppercase key


def test_csv_row_quotes_separators_and_quotes():
    # A barcode with a comma must stay one CSV field, not split into columns.
    assert format_csv_row("2026-09-29 10:12:03", "A,B") == \
        '2026-09-29 10:12:03,HID-Keyboard,"A,B"'
    assert format_csv_row("t", 'a"b') == 't,HID-Keyboard,"a""b"'
    # Ordinary GTINs are emitted unchanged (no gratuitous quoting).
    assert format_csv_row("t", "8412345678905") == "t,HID-Keyboard,8412345678905"


# --- item-1 fallback: the live receive path (F1-F4) ---------------------------
#
# Everything below injects the "hardware": a fake `os` (scripted
# open/read/close) and a fake `select`, both rebound on the module, so the
# SHIPPED run_hidraw/main run with no device, no root and no file written.

import errno
import json
import os as real_os

import scripts.scanner_hid as hidmod


class FakeOs:
    """`os` proxy with scripted open/read/close; everything else delegates."""

    def __init__(self, frames=(), *, read_error=None, open_error=None):
        self.frames = list(frames)
        self.read_error = read_error
        self.open_error = open_error
        self.opened = []            # (path, flags)
        self.closed = []
        self.reads = 0

    def __getattr__(self, name):     # environ, getpid, O_RDONLY, ...
        return getattr(real_os, name)

    def open(self, path, flags, *args, **kwargs):
        if self.open_error is not None:
            raise self.open_error
        self.opened.append((path, flags))
        return 7

    def read(self, fd, size):
        self.reads += 1
        if self.frames:
            item = self.frames.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        if self.read_error is not None:
            raise self.read_error
        raise AssertionError("run_hidraw read past the scripted frames")

    def close(self, fd):
        self.closed.append(fd)


def readable(*outcomes):
    """A select.select stand-in: one readiness answer per call."""
    answers = list(outcomes)

    def _select(rlist, wlist, xlist, timeout):
        if not answers:
            raise AssertionError("run_hidraw polled more often than scripted")
        ready = answers.pop(0)
        return (list(rlist) if ready else [], [], [])

    return _select


def frames_for(text, *, prefixed=True):
    """The reports a keyboard scanner sends for `text` + its Enter suffix."""
    frames = []
    for char in text:
        frame = report(USAGE[char])
        frames.append((b"\x00" + frame) if prefixed else frame)
    enter = report(40)
    frames.append((b"\x00" + enter) if prefixed else enter)
    return frames


def run_frames(monkeypatch, frames, **kwargs):
    fake = FakeOs(frames)
    monkeypatch.setattr(hidmod, "os", fake)
    scans = []

    def on_scan(text):
        scans.append(text)
        return False        # one scan is enough for the test

    outcome = hidmod.run_hidraw("/dev/hidraw9", on_scan,
                                select_fn=readable(*[True] * (len(frames) + 1)),
                                log=kwargs.pop("log", lambda message: None),
                                **kwargs)
    return outcome, scans, fake


def test_boot_report_accepts_prefixed_and_unprefixed_frames():
    bare = report(USAGE["8"])
    assert hidmod.boot_report(b"\x00" + bare) == bare       # hidraw's 9-byte shape
    assert hidmod.boot_report(bare) == bare                 # 8-byte shape
    odd = b"\x05" + bare                                    # numbered report, no 0x00
    assert hidmod.boot_report(odd) == odd
    assert hidmod.boot_report(b"") == b""


def test_frame_shape_label_names_both_shapes():
    assert "report ID 0x00" in hidmod.frame_shape_label(b"\x00" + report(40))
    assert "bare boot report" in hidmod.frame_shape_label(report(40))
    assert "unexpected" in hidmod.frame_shape_label(b"\x00" * 12)


def test_run_hidraw_decodes_prefixed_frames(monkeypatch):
    outcome, scans, _ = run_frames(monkeypatch, frames_for("841234", prefixed=True))
    assert (outcome, scans) == ("count", ["841234"])


def test_run_hidraw_decodes_unprefixed_frames(monkeypatch):
    # F1: the frame shape must be DISCOVERED. The pre-fix code stripped byte 0
    # unconditionally, which shifted the only keycode and the Enter terminator
    # out of the parsed slots and emitted nothing at all.
    outcome, scans, _ = run_frames(monkeypatch, frames_for("841234", prefixed=False))
    assert (outcome, scans) == ("count", ["841234"])


def test_run_hidraw_reports_a_vanished_node_without_a_traceback(monkeypatch, capsys):
    # F3: ENODEV mid-capture is the documented -71-then-dropout signature.
    frames = frames_for("84")[:1] + [OSError(errno.ENODEV, "No such device")]
    fake = FakeOs(frames)
    monkeypatch.setattr(hidmod, "os", fake)
    outcome = hidmod.run_hidraw("/dev/hidraw9", lambda text: None,
                                select_fn=readable(True, True))
    err = capsys.readouterr().err
    assert outcome == "gone"
    assert "vanished" in err and "dropped off the bus" in err
    assert "Traceback" not in err
    assert fake.closed == [7], "the node is always closed"


def test_run_hidraw_zero_length_read_is_not_a_busy_loop(monkeypatch):
    # F4: `continue` on b"" spun at ~1.8M reads/s and never exited.
    fake = FakeOs([b""])
    monkeypatch.setattr(hidmod, "os", fake)
    outcome = hidmod.run_hidraw("/dev/hidraw9", lambda text: None,
                                select_fn=readable(True))
    assert outcome == "gone"
    assert fake.reads == 1, "a zero-length read ends the loop immediately"


def test_run_hidraw_silence_is_loud(monkeypatch, capsys):
    # F2: attached-but-silent is its own answer, distinct from "gone".
    monkeypatch.setattr(hidmod, "os", FakeOs([]))
    outcome = hidmod.run_hidraw("/dev/hidraw9", lambda text: None,
                                silence_timeout=5, select_fn=readable(False))
    err = capsys.readouterr().err
    assert outcome == "silent"
    assert "no frames in 5s" in err and "not transmitting" in err


def test_run_hidraw_announces_the_frame_shape_once(monkeypatch):
    lines = []
    outcome, scans, _ = run_frames(
        monkeypatch, frames_for("84", prefixed=True),
        log=lines.append)
    assert scans == ["84"]
    assert sum(line.startswith("# frame shape:") for line in lines) == 1
    assert "report ID 0x00 + 8-byte boot report" in lines[0]


def test_run_hidraw_raw_hexdumps_every_frame(monkeypatch):
    lines = []
    run_frames(monkeypatch, frames_for("8", prefixed=True), raw=True,
               log=lines.append)
    raws = [line for line in lines if line.startswith("# raw ")]
    assert len(raws) == 2 and raws[0].startswith("# raw 9B 00 00 00 25")


def test_run_hidraw_opens_read_only(monkeypatch):
    _, _, fake = run_frames(monkeypatch, frames_for("8"))
    assert fake.opened == [("/dev/hidraw9", real_os.O_RDONLY)]


def test_main_no_node_exits_3_with_actions(monkeypatch, capsys):
    monkeypatch.setattr(hidmod, "find_hidraw", lambda vid_pid=None: [])
    code = hidmod.main(["--count", "1"])
    err = capsys.readouterr().err
    assert code == hidmod.EXIT_CANNOT_RUN == 3
    for needle in ("not on the bus", "--list", "usb_host_check.py",
                   "opticon-hardware.md §4", "-71"):
        assert needle in err, needle
    assert "Traceback" not in err


def test_main_silent_device_exits_4_and_still_reports_json(monkeypatch, capsys):
    monkeypatch.setattr(hidmod, "find_hidraw",
                        lambda vid_pid=None: [{"node": "/dev/hidraw9",
                                               "vid": 0x065A, "pid": 0xA001,
                                               "name": "Opticon USB Barcode Reader"}])
    monkeypatch.setattr(hidmod, "running_daemon", lambda path=None: None)
    monkeypatch.setattr(hidmod, "run_hidraw", lambda *a, **kw: "silent")
    code = hidmod.main(["--json", "--silence-timeout", "5"])
    out = capsys.readouterr().out
    assert code == hidmod.EXIT_SILENT == 4
    assert json.loads(out) == {"device_id": None, "count": 0, "scans": []}


def test_main_vanished_node_exits_3(monkeypatch, capsys):
    monkeypatch.setattr(hidmod, "find_hidraw",
                        lambda vid_pid=None: [{"node": "/dev/hidraw9",
                                               "vid": 0x065A, "pid": 0xA001,
                                               "name": "Opticon"}])
    monkeypatch.setattr(hidmod, "running_daemon", lambda path=None: None)
    monkeypatch.setattr(hidmod, "run_hidraw", lambda *a, **kw: "gone")
    assert hidmod.main(["--count", "1"]) == hidmod.EXIT_CANNOT_RUN


def test_main_warns_once_about_a_running_daemon(monkeypatch, tmp_path, capsys):
    lock = tmp_path / "hcrm-scanner-daemon.lock"
    lock.write_text(f"{real_os.getpid()}\n")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(hidmod, "find_hidraw",
                        lambda vid_pid=None: [{"node": "/dev/hidraw9",
                                               "vid": 0x065A, "pid": 0xA001,
                                               "name": "Opticon"}])
    monkeypatch.setattr(hidmod, "run_hidraw", lambda *a, **kw: "count")
    assert hidmod.main(["--count", "1"]) == 0
    err = capsys.readouterr().err
    assert err.count("scanner_daemon is running") == 1
    assert f"pid {real_os.getpid()}" in err
    assert "hidraw sits below the input layer" in err
    assert "systemctl stop scanner-daemon" in err


def test_running_daemon_ignores_a_stale_pid(monkeypatch, tmp_path):
    lock = tmp_path / "hcrm-scanner-daemon.lock"
    lock.write_text("999999999\n")          # not a live PID
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert hidmod.running_daemon() is None
    lock.unlink()
    assert hidmod.running_daemon() is None


def test_lockfile_path_matches_the_daemon(monkeypatch, tmp_path):
    # Two implementations (this tool must not import the daemon), one test:
    # a drift means the note above silently stops firing.
    from scripts.scanner_daemon import _default_lockfile_path

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert hidmod._lockfile_path() == _default_lockfile_path()
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert hidmod._lockfile_path() == _default_lockfile_path()
