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

    def test_interleaved_scan_data_ignored_while_awaiting_ack(self, scanner_pair):
        # In buffered mode a scan can arrive between command and ACK.
        scanner, master = scanner_pair

        def play_scanner():
            self._read_master(master)
            os.write(master, b"EX-003\r" + bytes([ACK]))

        t = threading.Thread(target=play_scanner)
        t.start()
        assert scanner.trigger_on() is True
        t.join(2.0)

    def test_commands_require_open(self):
        with pytest.raises(Exception, match="not open"):
            OpticonM10("/dev/null-scan").trigger_on()


# --- Discovery ------------------------------------------------------------------


def test_find_scanner_ports_returns_list():
    # On a CI machine the result is usually empty; it must never raise.
    assert isinstance(find_scanner_ports(), list)
