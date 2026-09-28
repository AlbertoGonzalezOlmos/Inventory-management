"""Opticon M-10 barcode scanner integration.

Hardware-facing, offline-capable, stdlib-only at its core:

- ``protocol``  — frame parsing and command construction (pure stdlib,
  transport-agnostic, fully unit-testable without hardware).
- ``transport`` — serial port opening (pyserial when installed, POSIX
  termios fallback otherwise).
- ``m10``       — the high-level :class:`OpticonM10` driver and port
  discovery.

See ``docs/opticon-m10.md`` for the full connection & interaction guide.
"""

from app.scanner.m10 import OpticonM10, find_scanner_ports
from app.scanner.protocol import (
    ACK,
    ESC,
    NAK,
    READ_DISABLE,
    READ_ENABLE,
    ScanParser,
    build_menu_command,
)

__all__ = [
    "ACK",
    "ESC",
    "NAK",
    "READ_DISABLE",
    "READ_ENABLE",
    "OpticonM10",
    "ScanParser",
    "build_menu_command",
    "find_scanner_ports",
]
