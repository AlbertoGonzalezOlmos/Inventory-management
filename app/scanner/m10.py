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

**One loop owns the port.** :meth:`OpticonM10.open` starts a single reader
thread that demultiplexes everything the device sends: while a command is
pending, a lone ACK/NAK/ESC byte resolves that command's waiter and every other
byte is scan data (flushed to the parser in arrival order, so a scan that lands
inside the ACK window is *delivered*, not discarded). ``send_command`` never
reads the port itself — it registers a waiter and waits. That is a deliberate
design constraint, not an optimisation: the device ships in buffered mode, so
scans and responses genuinely interleave, and the previous two-reader design
lost scans while a command was awaiting its ACK.

Scan callbacks are invoked on that thread and may raise anything, including
``SystemExit``: the exception is routed to ``on_error`` and logged, because a
callback must never be able to kill the loop and leave a caller that believes
it is still listening.
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


def find_scanner_ports(include_unknown: bool = False, *,
                       dev_root: str = "/dev",
                       sysfs_root: str = "/sys/class/tty") -> list[str]:
    """Serial ports that are *verified* to be an M-10 in USB-COM mode.

    Only ``065A:A002`` matches (Specifications Manual §18.5). Linux:
    ``/dev/ttyACM*`` (USB-COM enumerates as CDC-ACM; the wiki's MDI3100 page
    confirms it appears as ``ttyACM0``), verified through sysfs. If pyserial is
    installed, its cross-platform port list is used instead (Windows ``COMx``),
    verified through the reported VID/PID.

    ``include_unknown=True`` appends any other serial port (``ttyACM*``,
    ``ttyUSB*``, ``COM*``) — but those are *candidates*, not matches, and
    auto-picking one is how a shop ends up talking to the wrong device: this
    scanner is 9600 **8N1** ASCII, the OPN-2001 that may well be plugged in
    next to it is 9600 **8O1** and speaks a binary protocol, so the wrong pick
    does not fail loudly — it produces garbage that looks like barcodes. Callers
    must opt in explicitly (the bridge's ``--any-port``) and say so out loud.
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
        return matches + others if include_unknown else matches

    # Stdlib fallback: scan the tty devices (Linux). ``dev_root``/``sysfs_root``
    # are injectable so the filter is unit-testable against a fake sysfs tree
    # with no hardware attached.
    ports = []
    if os.path.isdir(dev_root):
        ports = sorted(
            os.path.join(dev_root, name)
            for name in os.listdir(dev_root)
            if name.startswith(("ttyACM", "ttyUSB"))
        )

    def _is_opticon(port: str) -> bool:
        # <sysfs_root>/ttyACM0/device is the USB interface; the parent
        # USB device directory holds idVendor/idProduct.
        dev = os.path.realpath(f"{sysfs_root}/{os.path.basename(port)}/device")
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

    matches = [p for p in ports if _is_opticon(p)]
    if not include_unknown:
        return matches
    return matches + [p for p in ports if p not in matches]


class CommandRejected(RuntimeError):
    """The scanner answered NAK or ESC to a command."""


class _CommandWaiter:
    """One pending command's response slot, resolved by the reader thread."""

    __slots__ = ("event", "value")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.value: int | None = None

    def resolve(self, value: int) -> None:
        self.value = value
        self.event.set()


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
        # One loop owns the port (REVIEW-m10.md P3). Everything the device
        # sends — scan frames and single-byte command responses alike — arrives
        # on that one thread and is demultiplexed there, so a command can never
        # eat a barcode and a barcode can never eat an ACK.
        self._io_lock = threading.Lock()      # serialises writes + waiter handoff
        self._waiter: "_CommandWaiter | None" = None
        self._parser = ScanParser()
        self._on_scan: Callable[[str], None] | None = None
        self._on_error: Callable[[BaseException], None] | None = None

    @classmethod
    def discover(cls, *, allow_any_port: bool = False) -> "OpticonM10":
        """Open a port verified to be an M-10 in USB-COM mode (``065A:A002``).

        ``allow_any_port=True`` falls back to any serial port when no verified
        M-10 is attached. That is an explicit opt-in, not a default: see
        :func:`find_scanner_ports` for what goes wrong silently otherwise.
        """
        ports = find_scanner_ports()
        if ports:
            return cls(ports[0])
        candidates = find_scanner_ports(include_unknown=True)
        if candidates and allow_any_port:
            logger.warning(
                "no verified M-10 (065A:A002); opening unverified port %s "
                "because allow_any_port was requested", candidates[0])
            return cls(candidates[0])
        raise SerialOpenError(
            "no M-10 in USB-COM mode (065A:A002) found. "
            + (f"Unverified serial ports exist ({', '.join(candidates)}) but "
               "were not auto-picked: another Opticon personality uses "
               "different line settings and a binary protocol, so opening it "
               "here yields garbage rather than an error. Pass an explicit "
               "port (allow_any_port / the bridge's --any-port) if you are "
               "sure. " if candidates else "")
            + "Is the scanner plugged in, and in USB-COM mode? Survey the host "
              "with `uv run python scripts/opticon_detect.py`; switch "
              "personality by scanning the *USB COM Port* configuration sheet "
              "(docs/opticon-m10.md)."
        )

    # -- lifecycle ------------------------------------------------------------

    def open(self) -> "OpticonM10":
        if self._serial is None:
            self._serial = open_serial_port(
                self.port, self.baudrate, self.data_bits, self.parity, self.stop_bits
            )
            logger.info("M-10 opened on %s", self.port)
            self._stop.clear()
            self._reader_thread = threading.Thread(
                target=self._read_loop, name="m10-port-reader", daemon=True
            )
            self._reader_thread.start()
        return self

    def close(self) -> None:
        self.stop_reading()
        self._stop.set()
        thread = self._reader_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._reader_thread = None
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

        This method does **not** read the port. It registers a waiter and the
        single reader thread resolves it, which is what makes buffered mode
        safe: the device may emit scanned data between the command and its
        response, and those bytes belong to the scan stream, not to this call.
        The previous implementation read the port directly and *discarded*
        every non-response byte it saw, silently losing scans (P3).
        """
        self._require_open()
        data = command.encode("ascii") if isinstance(command, str) else command

        waiter = _CommandWaiter() if expect_response else None
        with self._io_lock:
            if waiter is not None:
                # Register BEFORE writing: a very fast ACK must not arrive
                # while nobody is listening for it.
                self._waiter = waiter
            try:
                self._serial.write(data)
            except BaseException:
                self._clear_waiter(waiter)
                raise

        if waiter is None:
            return True
        if not waiter.event.wait(timeout):
            self._clear_waiter(waiter)
            raise TimeoutError(f"no response from scanner within {timeout:.1f}s")
        self._clear_waiter(waiter)
        value = waiter.value
        if value == ACK:
            return True
        raise CommandRejected(
            f"scanner answered {COMMAND_RESPONSES.get(value, value)} to {data!r}"
        )

    def _clear_waiter(self, waiter: "_CommandWaiter | None") -> None:
        with self._io_lock:
            if waiter is not None and self._waiter is waiter:
                self._waiter = None

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
        on_error: Callable[[BaseException], None] | None = None,
        chunk_size: int = 256,
        poll_timeout: float = 0.2,
    ) -> threading.Thread:
        """Register ``on_scan(barcode)`` on the port's reader thread.

        The thread itself is started by :meth:`open` — one loop owns the port
        for the whole connection, whether or not a scan callback is registered
        (a command's ACK has to be read by somebody). Returns that thread.

        ``on_scan`` may raise anything, including :exc:`SystemExit`: it is
        caught, routed to ``on_error`` and logged, because a callback bug must
        never be able to kill the loop and leave a process that still prints
        "Listening" (P2).
        """
        self._require_open()
        if self._on_scan is not None:
            raise RuntimeError("a scan callback is already registered — "
                               "call stop_reading() first")
        self.chunk_size = chunk_size
        self.poll_timeout = poll_timeout
        self._on_error = on_error
        self._on_scan = on_scan
        return self._reader_thread

    def stop_reading(self, timeout: float = 2.0) -> None:
        """Unregister the scan callback. The reader thread stays with the port."""
        self._on_scan = None
        self._on_error = None

    @property
    def is_reading(self) -> bool:
        return self._on_scan is not None

    # -- the one loop that owns the port ---------------------------------------

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            serial = self._serial
            if serial is None:
                return
            try:
                chunk = serial.read(getattr(self, "chunk_size", 256),
                                    timeout=getattr(self, "poll_timeout", 0.2))
            except Exception as exc:  # unplugged device, etc.
                self._dispatch_error(exc)
                return
            if not chunk:
                continue
            try:
                self._demux(chunk)
            except Exception as exc:
                self._dispatch_error(exc)

    def _demux(self, chunk: bytes) -> None:
        """Split one read into command responses and scan frames, in order.

        While a command is pending, a single ACK/NAK/ESC byte resolves it;
        every other byte is scan data. Bytes accumulated *before* the response
        are flushed to the parser first, so a scan that arrived during the ACK
        window is delivered in the right order rather than dropped.
        """
        pending = bytearray()
        for byte in chunk:
            waiter = self._waiter
            if waiter is not None and byte in COMMAND_RESPONSES and not waiter.event.is_set():
                self._flush_scans(pending)
                pending = bytearray()
                waiter.resolve(byte)
                continue
            pending.append(byte)
        self._flush_scans(pending)

    def _flush_scans(self, data: bytearray) -> None:
        if not data:
            return
        for frame in self._parser.feed(bytes(data)):
            self._deliver(frame)

    def _deliver(self, frame: str) -> None:
        callback = self._on_scan
        if callback is None:
            logger.debug("scan with no callback registered: %r", frame)
            return
        try:
            callback(frame)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            # SystemExit included, on purpose: it is a BaseException, and the
            # old loop's `except Exception` let it through — which is how a
            # bridge became a zombie (P2).
            self._dispatch_error(exc)

    def _dispatch_error(self, exc: BaseException) -> None:
        handler = self._on_error
        if handler is not None:
            try:
                handler(exc)
                return
            except Exception:  # a broken error handler must not kill the loop
                logger.exception("on_error handler itself failed")
        logger.warning("scanner error: %s", exc)

    # -- internals --------------------------------------------------------------

    def _require_open(self) -> None:
        if self._serial is None:
            raise SerialOpenError("scanner is not open — use open() or a with-block")
        thread = self._reader_thread
        if thread is not None and not thread.is_alive():
            raise SerialOpenError(
                "the port reader thread has stopped (device unplugged or a read "
                "error) — reopen the scanner; see the on_error sink / logs for "
                "the cause")
