"""Wire protocol for the Opticon M-10 (MDI3100 engine) — pure stdlib.

Facts verified against Opticon's published M-10 documents
(``docs/opticon-m10.md`` lists the sources per claim):

- Scanned data arrives as ASCII/UTF-8 bytes terminated by a configurable
  suffix; the factory default suffix is **CR** (Specifications Manual
  §18.3 "Added suffix value CR"). We accept CR, LF and CRLF so the parser
  also works when the suffix is reconfigured via the Universal Menu Book.
- RS-232C defaults: 9600 bps, 8 data bits, no parity, 1 stop bit, no
  handshaking (Specifications Manual §18.6). USB-COM is CDC-ACM and
  ignores these settings, but they must match for the RS-232C model.
- Configuration commands are Opticon "menu commands": the same
  ``@MENU_OPTO@<CMD>@<CMD>@OTPO_UNEM@`` strings printed as Code 128 labels
  in the Universal Menu Book can be sent over USB-COM / RS-232C. The
  factory-default label is published verbatim in the Specifications
  Manual (§18.1): ``@MENU_OPTO@ZZ@BAP@ZZ@OTPO_UNEM@``.
- Dedicated serial commands for the MDI3x00 family (read enable/disable)
  and the single-byte command responses (ACK/NAK/ESC) come from the
  "MDI3100 Serial Interface" document, which Opticon distributes via
  tech support. They are standard across the MDI3x00 product family;
  treat them as family-documented, and confirm against your unit's
  firmware if in doubt.
"""

from __future__ import annotations

# --- Serial line defaults (Specifications Manual §18.6) ----------------------

DEFAULT_BAUDRATE = 9600
DEFAULT_DATA_BITS = 8
DEFAULT_PARITY = "N"  # none
DEFAULT_STOP_BITS = 1

# --- Scanner -> host ---------------------------------------------------------

# Single-byte responses to host commands (MDI3x00 serial interface).
ACK = 0x06  # command accepted
NAK = 0x15  # command rejected (unknown/invalid command)
ESC = 0x1B  # command recognised but not executable in the current state

COMMAND_RESPONSES = {ACK: "ACK", NAK: "NAK", ESC: "ESC"}

# Factory default data suffix is CR; LF and CRLF are accepted too so the
# parser keeps working if the suffix is changed with the Universal Menu
# Book (e.g. to CRLF for terminal-friendly output).
FRAME_TERMINATORS = (b"\r", b"\n")

# --- Host -> scanner ---------------------------------------------------------

# Dedicated commands (MDI3x00 serial interface, family-documented).
READ_ENABLE = "Z1"  # start a read cycle (software trigger)
READ_DISABLE = "Z2"  # abort/disable reading

# Menu-command envelope (verified: the factory-default label published in
# the M-10 Specifications Manual §18.1 decomposes exactly this way).
_MENU_HEADER = "@MENU_OPTO@"
_MENU_TRAILER = "@OTPO_UNEM@"

#: Verbatim factory-default menu label from the Specifications Manual.
FACTORY_DEFAULT_LABEL = "@MENU_OPTO@ZZ@BAP@ZZ@OTPO_UNEM@"


def build_menu_command(*commands: str) -> str:
    """Assemble a menu-command string from individual command codes.

    ``build_menu_command("ZZ", "BAP", "ZZ")`` reproduces the published
    factory-default label :data:`FACTORY_DEFAULT_LABEL`. The same string
    can be printed as a Code 128 label for the scanner to read, or sent
    over the serial link.
    """
    if not commands:
        raise ValueError("at least one menu command is required")
    for cmd in commands:
        if not cmd or "@" in cmd:
            raise ValueError(f"invalid menu command code: {cmd!r}")
    return _MENU_HEADER + "@".join(commands) + _MENU_TRAILER


class ScanParser:
    """Incremental parser for scanned-barcode frames.

    Feed raw bytes as they arrive from the serial port; get back every
    complete barcode read so far. Frames are terminated by CR, LF or CRLF
    (see :data:`FRAME_TERMINATORS`); empty frames (e.g. a stray LF after
    a CR) are dropped. Decoding is UTF-8 with replacement so a damaged
    byte never raises inside the read loop.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[str]:
        """Add bytes to the buffer and return all complete frames."""
        frames: list[str] = []
        self._buf.extend(data)
        while True:
            # Find the earliest CR or LF.
            positions = [
                i
                for i in (self._buf.find(b"\r"), self._buf.find(b"\n"))
                if i != -1
            ]
            if not positions:
                break
            end = min(positions)
            frame = bytes(self._buf[:end])
            terminator = bytes(self._buf[end : end + 1])
            del self._buf[: end + 1]
            # Swallow the second half of a CRLF (or LFCR) pair so each
            # physical line yields exactly one frame.
            if self._buf[:1] in FRAME_TERMINATORS and bytes(self._buf[:1]) != terminator:
                del self._buf[:1]
            if frame:
                frames.append(frame.decode("utf-8", errors="replace").strip())
        return [f for f in frames if f]

    def flush(self) -> str | None:
        """Return any partial frame still buffered (e.g. on disconnect)."""
        if not self._buf:
            return None
        frame = bytes(self._buf).decode("utf-8", errors="replace").strip()
        self._buf.clear()
        return frame or None

    @property
    def pending_bytes(self) -> int:
        """Bytes buffered without a terminator yet (for diagnostics)."""
        return len(self._buf)
