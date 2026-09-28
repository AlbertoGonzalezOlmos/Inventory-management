"""High-level driver for the Opticon M-10 presentation scanner.

Usage::

    from app.scanner import OpticonM10

    with OpticonM10("/dev/ttyACM0") as scanner:
        scanner.start_reading(print)   # callback per scanned barcode
        ...
        scanner.stop_reading()

Or let discovery find the USB-COM model (VID 065A / PID A002,
Specifications Manual §18.5)::

    scanner = OpticonM10.discover()

The command channel (USB-COM / RS-232C only — not USB-HID) accepts menu
commands and dedicated commands; the scanner answers with a single byte:
ACK / NAK / ESC (see ``app.scanner.protocol``).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

from app.scanner.protocol import (
    ACK,
    COMMAND_RESPONSES,
    DEFAULT_BAUDRATE,
    DEFAULT_DATA_BITS,
    DEFAULT_PARITY,
    DEFAULT_STOP_BITS,
    FACTORY_DEFAULT_LABEL,
    NAK,
    ESC,
    READ_DISABLE,
    READ_ENABLE,
    ScanParser,
    build_menu_command,
)
from app.scanner.transport import SerialOpenError, open_serial_port

logger = logging.getLogger("hcrm.scanner")

#: USB identity of the M-10 USB-COM model (Specifications Manual §18.5).
USB_VENDOR_ID = "065a"
USB_PRODUCT_ID = "a002"


def find_scanner_ports() -> list[str]:
    """Best-effort list of serial ports that could be an M-10.

    Linux: ``/dev/ttyACM*`` (USB-COM enumerates as CDC-ACM; the wiki's
    MDI3100 page confirms it appears as ``ttyACM0``), with ports whose
    sysfs USB IDs match the M-10 sorted first. If pyserial is installed,
    its cross-platform port list is used instead (Windows ``COMx``).
    """
    try:
        from serial.tools import list_ports  # type: ignore
    except ImportError:
        list_ports = None

    if list_ports is not None:
        matches, others = [], []
        for info in list_ports.comports():
            entry = info.device
            if (
                info.vid is not None
                and f"{info.vid:04x}" == USB_VENDOR_ID
                and info.pid is not None
                and f"{info.pid:04x}" == USB_PRODUCT_ID
            ):
                matches.append(entry)
            elif "ttyACM" in entry or "ttyUSB" in entry or entry.startswith("COM"):
                others.append(entry)
        return matches + others

    # Stdlib fallback: scan /dev for CDC-ACM devices (Linux).
    ports = []
    if os.path.isdir("/dev"):
        ports = sorted(
            os.path.join("/dev", name)
            for name in os.listdir("/dev")
            if name.startswith(("ttyACM", "ttyUSB"))
        )

    def _is_opticon(port: str) -> bool:
        # /sys/class/tty/ttyACM0/device is the USB interface; the parent
        # USB device directory holds idVendor/idProduct.
        dev = os.path.realpath(f"/sys/class/tty/{os.path.basename(port)}/device")
        for candidate in (dev, os.path.dirname(dev)):
            try:
                with open(os.path.join(candidate, "idVendor")) as f:
                    vid = f.read().strip().lower()
                with open(os.path.join(candidate, "idProduct")) as f:
                    pid = f.read().strip().lower()
                return vid == USB_VENDOR_ID and pid == USB_PRODUCT_ID
            except OSError:
                continue
        return False

    return sorted(ports, key=lambda p: (not _is_opticon(p), p))


class CommandRejected(RuntimeError):
    """The scanner answered NAK or ESC to a command."""


class OpticonM10:
    """Connection to an M-10 over USB-COM or RS-232C."""

    def __init__(
        self,
        port: str,
        baudrate: int = DEFAULT_BAUDRATE,
        data_bits: int = DEFAULT_DATA_BITS,
        parity: str = DEFAULT_PARITY,
        stop_bits: int = DEFAULT_STOP_BITS,
    ) -> None:
        # Line settings only matter for the RS-232C model (defaults match
        # the scanner's factory settings); USB-COM ignores them.
        self.port = port
        self.baudrate = baudrate
        self.data_bits = data_bits
        self.parity = parity
        self.stop_bits = stop_bits
        self._serial = None
        self._reader_thread: threading.Thread | None = None
        self._stop = threading.Event()

    @classmethod
    def discover(cls) -> "OpticonM10":
        """Open the first port that looks like an M-10 (see find_scanner_ports)."""
        ports = find_scanner_ports()
        if not ports:
            raise SerialOpenError(
                "no candidate scanner ports found — is the M-10 plugged in "
                "and in USB-COM mode? (see docs/opticon-m10.md)"
            )
        return cls(ports[0])

    # -- lifecycle ------------------------------------------------------------

    def open(self) -> "OpticonM10":
        if self._serial is None:
            self._serial = open_serial_port(
                self.port, self.baudrate, self.data_bits, self.parity, self.stop_bits
            )
            logger.info("M-10 opened on %s", self.port)
        return self

    def close(self) -> None:
        self.stop_reading()
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def __enter__(self) -> "OpticonM10":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._serial is not None

    # -- command channel --------------------------------------------------------

    def send_command(
        self, command: str | bytes, *, expect_response: bool = True, timeout: float = 2.0
    ) -> bool:
        """Send a command; optionally wait for the ACK/NAK/ESC byte.

        Returns True on ACK. Raises :class:`CommandRejected` on NAK/ESC and
        ``TimeoutError`` when no response arrives in time. With
        ``expect_response=False`` the command is fire-and-forget.
        """
        self._require_open()
        data = command.encode("ascii") if isinstance(command, str) else command
        self._serial.write(data)
        if not expect_response:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            byte = self._serial.read(1, timeout=max(0.0, deadline - time.monotonic()))
            if not byte:
                break
            value = byte[0]
            if value == ACK:
                return True
            if value in (NAK, ESC):
                raise CommandRejected(
                    f"scanner answered {COMMAND_RESPONSES[value]} to {data!r}"
                )
            # Scanned barcode data can interleave with command responses in
            # buffered mode; ignore bytes that are not command responses.
            logger.debug("ignoring non-response byte while awaiting ACK: %r", byte)
        raise TimeoutError(f"no response from scanner within {timeout:.1f}s")

    def send_menu_commands(self, *commands: str, timeout: float = 2.0) -> bool:
        """Send Universal Menu Book command codes (e.g. ``"ZZ"``) over serial."""
        return self.send_command(build_menu_command(*commands), timeout=timeout)

    def restore_factory_defaults(self, timeout: float = 2.0) -> bool:
        """Apply the published factory-default configuration (Spec Manual §18.1)."""
        return self.send_command(FACTORY_DEFAULT_LABEL, timeout=timeout)

    def trigger_on(self) -> bool:
        """Software trigger: start one read cycle (READ_ENABLE, ``Z1``)."""
        return self.send_command(READ_ENABLE)

    def trigger_off(self) -> bool:
        """Abort reading / disable the trigger (READ_DISABLE, ``Z2``)."""
        return self.send_command(READ_DISABLE)

    # -- scan stream ------------------------------------------------------------

    def start_reading(
        self,
        on_scan: Callable[[str], None],
        *,
        on_error: Callable[[Exception], None] | None = None,
        chunk_size: int = 256,
        poll_timeout: float = 0.2,
    ) -> threading.Thread:
        """Start a daemon thread invoking ``on_scan(barcode)`` per read.

        The thread reads chunks from the port and feeds a
        :class:`ScanParser`; decoding problems never kill the loop.
        """
        self._require_open()
        if self._reader_thread is not None and self._reader_thread.is_alive():
            raise RuntimeError("reader thread already running")
        self._stop.clear()

        def _loop() -> None:
            parser = ScanParser()
            while not self._stop.is_set():
                try:
                    chunk = self._serial.read(chunk_size, timeout=poll_timeout)
                except Exception as exc:  # unplugged device, etc.
                    if on_error is not None:
                        on_error(exc)
                    else:
                        logger.warning("scanner read failed: %s", exc)
                    return
                if not chunk:
                    continue
                try:
                    for frame in parser.feed(chunk):
                        on_scan(frame)
                except Exception as exc:  # a callback bug must not kill the loop
                    logger.warning("scan callback failed: %s", exc)

        self._reader_thread = threading.Thread(
            target=_loop, name="m10-reader", daemon=True
        )
        self._reader_thread.start()
        return self._reader_thread

    def stop_reading(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._reader_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    # -- internals --------------------------------------------------------------

    def _require_open(self) -> None:
        if self._serial is None:
            raise SerialOpenError("scanner is not open — use open() or a with-block")
