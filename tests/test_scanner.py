"""Tests for the Opticon M-10 integration (app.scanner).

No hardware required: the protocol layer is tested as pure functions, and
the driver is exercised over a POSIX pseudo-terminal pair, which behaves
like a real serial port (the stdlib transport configures it raw, exactly
as it would /dev/ttyACM0).
"""

import os
import threading
import time

import pytest

from app.scanner import (
    ACK,
    ESC,
    NAK,
    READ_DISABLE,
    READ_ENABLE,
    OpticonM10,
    ScanParser,
    build_menu_command,
    find_scanner_ports,
)
from app.scanner.m10 import CommandRejected
from app.scanner.transport import SerialOpenError
from app.scanner.protocol import FACTORY_DEFAULT_LABEL


# --- ScanParser -------------------------------------------------------------


class TestScanParser:
    def test_cr_terminated_frame(self):
        # Factory default suffix is CR (Spec Manual §18.3).
        assert ScanParser().feed(b"5901234123457\r") == ["5901234123457"]

    def test_lf_terminated_frame(self):
        assert ScanParser().feed(b"EX-001\n") == ["EX-001"]

    def test_crlf_yields_one_frame(self):
        parser = ScanParser()
        assert parser.feed(b"ABC\r\nDEF\r\n") == ["ABC", "DEF"]

    def test_partial_frame_across_feeds(self):
        parser = ScanParser()
        assert parser.feed(b"EX-0") == []
        assert parser.pending_bytes == 4
        assert parser.feed(b"1\r") == ["EX-01"]
        assert parser.pending_bytes == 0

    def test_empty_frames_dropped(self):
        assert ScanParser().feed(b"\r\n\rABC\r\n") == ["ABC"]

    def test_multiple_frames_one_chunk(self):
        assert ScanParser().feed(b"A\rB\rC\r") == ["A", "B", "C"]

    def test_whitespace_stripped(self):
        assert ScanParser().feed(b"  padded \r") == ["padded"]

    def test_invalid_utf8_never_raises(self):
        frames = ScanParser().feed(b"bad\xffbytes\r")
        assert frames == ["bad�bytes"]

    def test_flush_returns_partial(self):
        parser = ScanParser()
        parser.feed(b"left-over")
        assert parser.flush() == "left-over"
        assert parser.flush() is None


# --- Menu commands ------------------------------------------------------------


class TestMenuCommands:
    def test_factory_default_label_matches_spec_manual(self):
        # Reproducing the published default label (Spec Manual §18.1)
        # guards the envelope format against typos.
        assert build_menu_command("ZZ", "BAP", "ZZ") == FACTORY_DEFAULT_LABEL

    def test_envelope_shape(self):
        assert build_menu_command("ZZ") == "@MENU_OPTO@ZZ@OTPO_UNEM@"

    def test_rejects_empty_and_separator(self):
        with pytest.raises(ValueError):
            build_menu_command()
        with pytest.raises(ValueError):
            build_menu_command("Z@Z")


# --- Driver over a pseudo-terminal (POSIX only) --------------------------------

pty = pytest.importorskip("pty", reason="POSIX-only test")


@pytest.fixture()
def scanner_pair():
    """(scanner, master_fd): OpticonM10 on the slave end of a pty pair.

    The test plays the scanner on the master end. The pty defaults to a
    cooking ldisc, but the stdlib transport reconfigures the slave to raw
    mode on open, so bytes pass through unchanged both ways.
    """
    master, slave = pty.openpty()
    scanner = OpticonM10(os.ttyname(slave)).open()
    os.close(slave)
    yield scanner, master
    scanner.close()
    os.close(master)


@pytest.mark.skipif(os.name != "posix", reason="pty-based test needs POSIX")
class TestOpticonM10:
    def _read_master(self, master, timeout=2.0) -> bytes:
        import select

        ready, _, _ = select.select([master], [], [], timeout)
        return os.read(master, 256) if ready else b""

    def test_scan_callback_receives_barcode(self, scanner_pair):
        scanner, master = scanner_pair
        received = []
        done = threading.Event()

        def on_scan(code):
            received.append(code)
            done.set()

        scanner.start_reading(on_scan)
        os.write(master, b"EX-001\rEX-002\r")
        assert done.wait(2.0)
        scanner.stop_reading()
        assert received[:2] == ["EX-001", "EX-002"]

    def test_trigger_on_sends_read_enable_and_acks(self, scanner_pair):
        scanner, master = scanner_pair

        def play_scanner():
            assert self._read_master(master) == READ_ENABLE.encode()
            os.write(master, bytes([ACK]))

        t = threading.Thread(target=play_scanner)
        t.start()
        assert scanner.trigger_on() is True
        t.join(2.0)

    def test_trigger_off_sends_read_disable(self, scanner_pair):
        scanner, master = scanner_pair

        def play_scanner():
            assert self._read_master(master) == READ_DISABLE.encode()
            os.write(master, bytes([ACK]))

        t = threading.Thread(target=play_scanner)
        t.start()
        assert scanner.trigger_off() is True
        t.join(2.0)

    def test_nak_raises_command_rejected(self, scanner_pair):
        scanner, master = scanner_pair

        def play_scanner():
            self._read_master(master)
            os.write(master, bytes([NAK]))

        t = threading.Thread(target=play_scanner)
        t.start()
        with pytest.raises(CommandRejected, match="NAK"):
            scanner.trigger_on()
        t.join(2.0)

    def test_esc_raises_command_rejected(self, scanner_pair):
        scanner, master = scanner_pair

        def play_scanner():
            self._read_master(master)
            os.write(master, bytes([ESC]))

        t = threading.Thread(target=play_scanner)
        t.start()
        with pytest.raises(CommandRejected, match="ESC"):
            scanner.trigger_on()
        t.join(2.0)

    def test_command_timeout(self, scanner_pair):
        scanner, _master = scanner_pair  # scanner stays silent
        with pytest.raises(TimeoutError):
            scanner.send_command(READ_ENABLE, timeout=0.2)

    def test_restore_factory_defaults_sends_spec_label(self, scanner_pair):
        scanner, master = scanner_pair

        def play_scanner():
            data = self._read_master(master)
            assert data.decode() == FACTORY_DEFAULT_LABEL
            os.write(master, bytes([ACK]))

        t = threading.Thread(target=play_scanner)
        t.start()
        assert scanner.restore_factory_defaults() is True
        t.join(2.0)

    # --- P3: one loop owns the port; interleaved scans are DELIVERED ----------

    def test_interleaved_scan_data_is_delivered_while_awaiting_ack(self, scanner_pair):
        """In buffered mode a scan arrives between the command and its ACK.

        This test used to assert the opposite — that the scan is *ignored* —
        which pinned the loss as expected behaviour (REVIEW-m10.md P3). Buffered
        mode is the factory default, so interleaving is the normal case, not the
        corner case: dropping those scans is the bug, and the single-reader
        demux is the fix.
        """
        scanner, master = scanner_pair
        received = []
        done = threading.Event()
        scanner.start_reading(lambda code: (received.append(code), done.set()))

        def play_scanner():
            self._read_master(master)
            os.write(master, b"EX-003\r" + bytes([ACK]))

        t = threading.Thread(target=play_scanner)
        t.start()
        assert scanner.trigger_on() is True      # the ACK still resolves the command
        t.join(2.0)
        assert done.wait(2.0)
        scanner.stop_reading()
        assert received == ["EX-003"]            # …and the scan is not lost

    def test_scan_data_split_across_the_ack_is_reassembled_in_order(
            self, scanner_pair):
        """A frame interrupted by the response byte still parses, in order."""
        scanner, master = scanner_pair
        received = []
        got_two = threading.Event()
        scanner.start_reading(
            lambda code: (received.append(code),
                          got_two.set() if len(received) == 2 else None))

        def play_scanner():
            self._read_master(master)
            os.write(master, b"EX-00")            # half a frame …
            os.write(master, bytes([ACK]))        # … the ACK for our command …
            os.write(master, b"4\rEX-005\r")     # … then the rest, plus another

        t = threading.Thread(target=play_scanner)
        t.start()
        assert scanner.trigger_on() is True
        t.join(2.0)
        assert got_two.wait(2.0)
        scanner.stop_reading()
        assert received == ["EX-004", "EX-005"]

    def test_commands_work_while_a_scan_callback_is_registered(self, scanner_pair):
        scanner, master = scanner_pair
        scanner.start_reading(lambda code: None)

        def play_scanner():
            self._read_master(master)
            os.write(master, bytes([ACK]))

        t = threading.Thread(target=play_scanner)
        t.start()
        assert scanner.trigger_off() is True
        t.join(2.0)
        scanner.stop_reading()

    def test_start_reading_twice_is_refused(self, scanner_pair):
        scanner, _master = scanner_pair
        scanner.start_reading(lambda code: None)
        with pytest.raises(RuntimeError, match="already registered"):
            scanner.start_reading(lambda code: None)
        scanner.stop_reading()
        scanner.start_reading(lambda code: None)   # …and re-registering works
        scanner.stop_reading()
        assert scanner.is_reading is False

    # --- P2: a callback must never be able to kill the loop -------------------

    def test_callback_raising_systemexit_does_not_kill_the_reader(self, scanner_pair):
        """The zombie-bridge failure mode, at driver level.

        SystemExit is a BaseException, so the old `except Exception` around the
        callback let it through and the reader thread died while the process
        kept printing "Listening". Defence in depth: the bridge no longer raises
        SystemExit from a callback either.
        """
        scanner, master = scanner_pair
        errors = []
        seen = []
        second = threading.Event()

        def on_scan(code):
            if code == "BOOM":
                raise SystemExit("api error")
            seen.append(code)
            second.set()

        scanner.start_reading(on_scan, on_error=errors.append)
        os.write(master, b"BOOM\r")
        os.write(master, b"EX-009\r")
        assert second.wait(2.0), "the reader thread died on SystemExit"
        scanner.stop_reading()
        assert seen == ["EX-009"]
        assert isinstance(errors[0], SystemExit)
        assert scanner._reader_thread.is_alive()

    def test_callback_raising_a_plain_exception_is_reported_and_survived(
            self, scanner_pair):
        scanner, master = scanner_pair
        errors = []
        seen = []
        second = threading.Event()

        def on_scan(code):
            if code == "BAD":
                raise ValueError("callback bug")
            seen.append(code)
            second.set()

        scanner.start_reading(on_scan, on_error=errors.append)
        os.write(master, b"BAD\rEX-010\r")
        assert second.wait(2.0)
        scanner.stop_reading()
        assert seen == ["EX-010"]
        assert isinstance(errors[0], ValueError)

    def test_a_broken_error_handler_does_not_kill_the_loop_either(self, scanner_pair):
        scanner, master = scanner_pair
        seen = []
        second = threading.Event()

        def on_scan(code):
            if code == "BAD":
                raise ValueError("callback bug")
            seen.append(code)
            second.set()

        def on_error(exc):
            raise RuntimeError("error handler is broken too")

        scanner.start_reading(on_scan, on_error=on_error)
        os.write(master, b"BAD\rEX-011\r")
        assert second.wait(2.0)
        scanner.stop_reading()
        assert seen == ["EX-011"]

    def test_scans_with_no_callback_registered_do_not_crash_the_loop(
            self, scanner_pair):
        """Commands-only sessions still have a live reader; scans are logged."""
        scanner, master = scanner_pair
        os.write(master, b"EX-012\r")
        time.sleep(0.3)
        assert scanner._reader_thread.is_alive()

    def test_close_stops_the_reader_thread(self, scanner_pair):
        scanner, _master = scanner_pair
        scanner.start_reading(lambda code: None)
        thread = scanner._reader_thread
        scanner.close()
        assert not thread.is_alive()

    def test_commands_require_open(self):
        # P6: assert the specific error, not "some Exception".
        with pytest.raises(SerialOpenError, match="not open"):
            OpticonM10("/dev/null-scan").trigger_on()


# --- Discovery ------------------------------------------------------------------


def test_find_scanner_ports_returns_list():
    # On a CI machine the result is usually empty; it must never raise.
    assert isinstance(find_scanner_ports(), list)


# --- P4: discovery must not hand back a device it has not verified ------------


def _fake_tty(tmp_path, name: str, vid: str, pid: str) -> tuple[str, str]:
    """Build a fake /dev entry + sysfs USB identity; returns (dev_root, sysfs)."""
    dev_root = tmp_path / "dev"
    dev_root.mkdir(exist_ok=True)
    (dev_root / name).write_text("")
    sysfs = tmp_path / "sysfs"
    usbdev = sysfs / name / "device" / "usb1"
    usbdev.mkdir(parents=True, exist_ok=True)
    (usbdev / "idVendor").write_text(vid + "\n")
    (usbdev / "idProduct").write_text(pid + "\n")
    return str(dev_root), str(sysfs)


@pytest.mark.skipif(os.name != "posix", reason="fake sysfs layout is POSIX-shaped")
def test_discovery_refuses_an_unverified_opticon_personality(tmp_path, monkeypatch):
    """P4: an attached OPN-2001 (065a:0009, 9600 8O1, binary RBBV) must never
    be auto-picked by a driver that speaks M-10 ASCII at 9600 8N1 — the wrong
    pick does not fail loudly, it produces garbage that looks like barcodes."""
    monkeypatch.setattr("app.scanner.m10.os.path.realpath",
                        lambda p: str(tmp_path / "sysfs" / "ttyUSB0" / "device" / "usb1")
                        if p.endswith("ttyUSB0/device") else os.path.realpath(p))
    dev_root, sysfs = _fake_tty(tmp_path, "ttyUSB0", "065a", "0009")

    assert find_scanner_ports(dev_root=dev_root, sysfs_root=sysfs) == []
    # …but the opt-in still surfaces it, and says which ports are only candidates:
    loose = find_scanner_ports(include_unknown=True, dev_root=dev_root,
                               sysfs_root=sysfs)
    assert loose == [os.path.join(dev_root, "ttyUSB0")]


@pytest.mark.skipif(os.name != "posix", reason="fake sysfs layout is POSIX-shaped")
def test_discovery_accepts_the_verified_usb_com_personality(tmp_path, monkeypatch):
    """The documented M-10 USB-COM identity (065A:A002, SS13063 §18.5)."""
    monkeypatch.setattr("app.scanner.m10.os.path.realpath",
                        lambda p: str(tmp_path / "sysfs" / "ttyACM0" / "device" / "usb1")
                        if p.endswith("ttyACM0/device") else os.path.realpath(p))
    dev_root, sysfs = _fake_tty(tmp_path, "ttyACM0", "065a", "a002")
    assert find_scanner_ports(dev_root=dev_root, sysfs_root=sysfs) == [
        os.path.join(dev_root, "ttyACM0")]


def test_discover_raises_rather_than_guessing(monkeypatch):
    """No verified M-10 → a loud, actionable error, never a guessed port."""
    monkeypatch.setattr("app.scanner.m10.find_scanner_ports",
                        lambda include_unknown=False: ["/dev/ttyUSB0"] if include_unknown else [])
    with pytest.raises(SerialOpenError) as exc:
        OpticonM10.discover()
    assert "A002" in str(exc.value) and "/dev/ttyUSB0" in str(exc.value)
    # …and the explicit opt-in does open it (the caller owns that risk):
    scanner = OpticonM10.discover(allow_any_port=True)
    assert scanner.port == "/dev/ttyUSB0"

# --- P6: the POSIX transport restores what it found, and refuses to share -----


@pytest.mark.skipif(os.name != "posix", reason="termios/flock transport is POSIX-only")
def test_second_open_of_the_same_port_is_refused():
    """Two bridges on one port interleave garbage and neither reports an error."""
    import pty as _pty

    from app.scanner.transport import open_serial_port

    master, slave = _pty.openpty()
    name = os.ttyname(slave)
    os.close(slave)
    try:
        first = open_serial_port(name)
        try:
            with pytest.raises(SerialOpenError, match="already open"):
                open_serial_port(name)
        finally:
            first.close()
        # …and after a clean close the port is available again (the lock was
        # released, not leaked with the descriptor):
        second = open_serial_port(name)
        second.close()
    finally:
        os.close(master)


@pytest.mark.skipif(os.name != "posix", reason="termios transport is POSIX-only")
def test_close_restores_the_line_discipline():
    """Leaving a port in raw mode outlives the process that set it."""
    import pty as _pty
    import termios

    from app.scanner.transport import open_serial_port

    master, slave = _pty.openpty()
    name = os.ttyname(slave)
    before = termios.tcgetattr(slave)
    os.close(slave)
    try:
        port = open_serial_port(name)
        port.close()
        check = os.open(name, os.O_RDWR | os.O_NOCTTY)
        try:
            after = termios.tcgetattr(check)
        finally:
            os.close(check)
        # iflag/oflag/lflag are what the transport zeroes to get raw mode.
        assert (after[0], after[1], after[3]) == (before[0], before[1], before[3])
    finally:
        os.close(master)
