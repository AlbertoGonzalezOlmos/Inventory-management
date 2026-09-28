"""Serial transport for the scanner — pyserial when available, POSIX
termios fallback otherwise.

The HCRM app itself is dependency-frozen (uv.lock, offline installs), so
pyserial is an *optional* convenience: on Linux/macOS the stdlib fallback
drives ``/dev/ttyACM*`` (USB-COM model) or ``/dev/ttyS*``/USB-RS232
adapters directly. On Windows you need ``pyserial`` (``uv pip install
pyserial``) — there is no stdlib way to open ``COMx``.

Both implementations expose the same minimal interface used by
:class:`app.scanner.m10.OpticonM10`:

- ``read(n, timeout)`` -> ``bytes`` (may return fewer than ``n`` bytes;
  empty on timeout)
- ``write(data: bytes)`` -> ``int``
- ``close()``
- ``name`` attribute
"""

from __future__ import annotations

import os
import time

from app.scanner.protocol import (
    DEFAULT_BAUDRATE,
    DEFAULT_DATA_BITS,
    DEFAULT_PARITY,
    DEFAULT_STOP_BITS,
)


class SerialOpenError(OSError):
    """The serial port could not be opened or configured."""


def open_serial_port(
    port: str,
    baudrate: int = DEFAULT_BAUDRATE,
    data_bits: int = DEFAULT_DATA_BITS,
    parity: str = DEFAULT_PARITY,
    stop_bits: int = DEFAULT_STOP_BITS,
):
    """Open a serial port, preferring pyserial, falling back to POSIX stdlib."""
    try:
        import serial  # type: ignore
    except ImportError:
        serial = None

    if serial is not None:
        parity_map = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}
        stop_map = {1: serial.STOPBITS_ONE, 2: serial.STOPBITS_TWO}
        try:
            # timeout=0 → non-blocking reads; OpticonM10 does its own
            # deadline handling on top of read(n, timeout).
            raw = serial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=data_bits,
                parity=parity_map[parity],
                stopbits=stop_map[stop_bits],
                timeout=0,
                write_timeout=5,
            )
        except Exception as exc:  # serial.SerialException or OSError
            raise SerialOpenError(f"cannot open {port}: {exc}") from exc
        return _PyserialAdapter(raw)

    if os.name != "posix":
        raise SerialOpenError(
            "pyserial is required on this platform "
            "(uv pip install pyserial); the stdlib fallback only works on POSIX"
        )
    return _PosixSerialPort(port, baudrate, data_bits, parity, stop_bits)


class _PyserialAdapter:
    """Give pyserial's ``read(size)`` the uniform ``read(n, timeout)`` shape."""

    def __init__(self, raw):
        self._raw = raw
        self.name = raw.name

    def read(self, n: int, timeout: float | None = None) -> bytes:
        # Deadline-based: poll the non-blocking port until data arrives or
        # the deadline lapses. pyserial's own read timeout is left at 0.
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            data = self._raw.read(n)
            if data or deadline is None or time.monotonic() >= deadline:
                return data
            time.sleep(0.005)

    def write(self, data: bytes) -> int:
        return self._raw.write(data)

    def close(self) -> None:
        self._raw.close()


class _PosixSerialPort:
    """Minimal stdlib serial port (termios + select), raw 8N1-style I/O."""

    def __init__(self, port, baudrate, data_bits, parity, stop_bits):
        import termios

        try:
            self._fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise SerialOpenError(f"cannot open {port}: {exc}") from exc
        self.name = port
        try:
            attrs = termios.tcgetattr(self._fd)
            # Raw mode: no echo, no canonical processing, no CR/NL
            # translation, no signals — the scanner's bytes pass through
            # unchanged (this is what makes CR-suffixed frames parseable).
            attrs[0] = 0  # iflag
            attrs[1] = 0  # oflag (no OPOST)
            attrs[3] = 0  # lflag

            baud = getattr(termios, f"B{baudrate}", None)
            if baud is None:
                raise SerialOpenError(f"unsupported baud rate: {baudrate}")
            attrs[4] = attrs[5] = baud  # ispeed / ospeed

            cflag = termios.CREAD | termios.CLOCAL
            cflag |= {5: termios.CS5, 6: termios.CS6, 7: termios.CS7, 8: termios.CS8}[data_bits]
            if parity in ("E", "O"):
                cflag |= termios.PARENB
                if parity == "O":
                    cflag |= termios.PARODD
            if stop_bits == 2:
                cflag |= termios.CSTOPB
            attrs[2] = cflag
            # VMIN/VTIME: return immediately with whatever is available;
            # read() implements its own deadline via select.
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self._fd, termios.TCSANOW, attrs)
            termios.tcflush(self._fd, termios.TCIOFLUSH)
        except Exception:
            os.close(self._fd)
            raise

    def read(self, n: int, timeout: float | None = None) -> bytes:
        import select

        deadline = None if timeout is None else time.monotonic() + timeout
        out = bytearray()
        while len(out) < n:
            wait = None
            if deadline is not None:
                wait = max(0.0, deadline - time.monotonic())
                if wait == 0.0 and out:
                    break
            ready, _, _ = select.select([self._fd], [], [], wait)
            if not ready:
                break  # timeout
            try:
                chunk = os.read(self._fd, n - len(out))
            except BlockingIOError:
                continue
            if not chunk:
                break  # device went away
            out.extend(chunk)
            # One chunk is enough for the streaming read loop; only loop
            # again if the caller needs more bytes than delivered.
            if out:
                break
        return bytes(out)

    def write(self, data: bytes) -> int:
        return os.write(self._fd, data)

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass
