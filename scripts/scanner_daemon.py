"""scanner_daemon — the evdev decode core of the round-4 grab daemon.

The M-10 in USB-HID keyboard mode (065a:a001) "types" each scan into
whatever window has OS focus. Round 4 grabs the scanner at the OS level:
this module is the DECODE-ONLY slice (docs/plans/r4t2-FINAL.md) —

* ``discover_event_device`` — find the scanner's ``/dev/input/eventN`` by
  VID/PID in sysfs (the grab itself is T3; T5 adds HTTP forwarding),
* ``parse_input_event`` — unpack the Linux ``struct input_event``,
* ``EvdevDecoder`` / ``read_scans`` — turn EV_KEY events into scan strings.

**The reuse bridge (do not duplicate the assembler).** evdev delivers Linux
keycodes (``KEY_A`` = 30), not HID usages ('a' = 4) — and the keycode space
is not an offset of the usage space (``KEY_A``=30 is HID '1'=30; letters
sit in two non-contiguous runs 30–38 and 44–50). So the decode is a flat
``KEYCODE_TO_USAGE`` table whose values are translated into SYNTHETIC
8-byte HID boot-keyboard reports — ``[modifier, 0, usage, 0, 0, 0, 0, 0]``
— fed to ``scripts.scanner_hid.ScanAssembler`` UNCHANGED. The shift table,
rollover tolerance and the "final character + terminator in one report"
fix therefore come from the code ``tests/test_scanner_hid.py`` already
pins; a second character table would be a second place for the
UPC-E-vs-``!`` class of bug to live. The reuse is enforced, not asserted:
``tests/test_scanner_daemon.py`` property-tests that EVERY usage in
``scanner_hid._USAGE_MAP`` decodes to the same character through the evdev
path, shifted and unshifted.

**No timing heuristics.** Round 3's browser capture had to distinguish
scanner bursts from human typing from timing alone; the daemon does not —
with an exclusive grab (T3), every key from this device is a scan key, and
the device identity is the classifier.

**Unmapped keys reset, never guess** (the misread doctrine): a keycode with
no translation (TAB, F-keys, keypad digits…) means the payload is not a
textual scan this decoder can represent; the partial buffer is discarded,
the code is reported through ``on_unmapped`` (stderr by default), and
decoding resumes with the next key. A barcode with a character we cannot
represent must be reported, never half-delivered.

**Terminators.** CR (``KEY_ENTER`` 28), the keypad Enter (``KEY_KPENTER``
96) and LF (``KEY_LINEFEED`` 101) all translate to HID usage 40; a CRLF
suffix is therefore two terminator reports, and the assembler's existing
"Enter-only report on an empty buffer emits nothing" rule absorbs the
second one — the same CR/LF/CRLF tolerance the hidraw path has.

**Struct parameterisation.** ``struct input_event`` is ``timeval + u16
type + u16 code + i32 value``: 24 bytes where ``timeval`` is two native
longs (64-bit), 16 bytes where it is two 32-bit ints. The timestamp is
irrelevant to decoding (see "no timing heuristics"), so ``parse_input_event``
skips it as raw bytes and unpacks only the tail — ``tv_size`` is a
parameter with a per-arch default, and synthetic 24- and 16-byte streams
are both testable offline. Import touches no device: this module imports
cleanly with nothing attached (CI has no scanner, and neither does this
host today — qwen M1).
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

# scripts.* imports work from the repo root (namespace package), exactly as
# tests/test_scanner_hid.py imports scripts.scanner_hid.
from scripts.scanner_hid import (
    LEFT_SHIFT,
    RIGHT_SHIFT,
    ScanAssembler,
    _USAGE_MAP,
)

# --- the scanner's USB identity (docs/opticon-hardware.md §1) ---------------

DEFAULT_VID_PID = (0x065A, 0xA001)  # Opticon USB Barcode Reader (HID mode)

# --- input_event parsing -----------------------------------------------------

EV_SYN = 0x00
EV_KEY = 0x01

# timeval = two native "long"s: 2x8 bytes on 64-bit, 2x4 on 32-bit. Kept as
# a module constant (not computed inline) so tests can pin it against the
# running arch, and so a surprising platform fails at the pin, not mid-decode.
NATIVE_TV_SIZE = struct.calcsize("@l") * 2

# "=HHi": standard (no native alignment/padding), 2+2+4 = 8 bytes.
_EVENT_TAIL_FMT = "=HHi"
_EVENT_TAIL_SIZE = struct.calcsize(_EVENT_TAIL_FMT)


def event_size(tv_size: int = NATIVE_TV_SIZE) -> int:
    """The full ``struct input_event`` size for a given timeval size."""
    return tv_size + _EVENT_TAIL_SIZE


def parse_input_event(buf: bytes, tv_size: int | None = None) -> tuple[int, int, int]:
    """Unpack one ``struct input_event`` → ``(type, code, value)``.

    The timeval bytes are skipped (decoding needs no timing). ``tv_size``
    defaults to the running arch's; pass 16 or 8 explicitly to decode a
    stream captured on the other arch. Raises ``ValueError`` on a short
    buffer — a truncated event must never be "decoded" from stale bytes.
    """
    tv = NATIVE_TV_SIZE if tv_size is None else tv_size
    if len(buf) < event_size(tv):
        raise ValueError(
            f"input_event buffer is {len(buf)} bytes; "
            f"{event_size(tv)} required for tv_size={tv}"
        )
    type_, code, value = struct.unpack_from(_EVENT_TAIL_FMT, buf, tv)
    return type_, code, value


# --- keycode → HID usage -----------------------------------------------------

# Linux KEY_* codes (linux/input-event-codes.h) → HID usages (HID usage
# tables, keyboard page 0x07) for every key a keyboard-mode scanner emits.
# Flat by necessity: the two spaces are not offset from each other and the
# letters are non-contiguous in keycode space (30–38, 44–50). Terminators
# (CR / keypad-Enter / LF) all map to usage 40 — see the module docstring.
KEY_ENTER, KEY_KPENTER, KEY_LINEFEED = 28, 96, 101
KEY_LEFTSHIFT, KEY_RIGHTSHIFT = 42, 54
KEY_0 = 11

KEYCODE_TO_USAGE: dict[int, int] = {
    # digits KEY_1..KEY_9 (2..10) → usages 30..38
    **{code: 30 + (code - 2) for code in range(2, 11)},
    # digits KEY_0 (11) → usage 39 ('0' closes the digit run)
    KEY_0: 39,
    # top letter row KEY_Q..KEY_P (16..25)
    16: 20, 17: 26, 18: 8, 19: 21, 20: 23, 21: 28, 22: 24, 23: 12, 24: 18, 25: 19,
    # home letter row KEY_A..KEY_L (30..38)
    30: 4, 31: 22, 32: 7, 33: 9, 34: 10, 35: 11, 36: 13, 37: 14, 38: 15,
    # bottom letter row KEY_Z..KEY_M (44..50)
    44: 29, 45: 27, 46: 6, 47: 25, 48: 5, 49: 17, 50: 16,
    # punctuation + space (usages 44–56, minus 50 which is not printable)
    12: 45, 13: 46, 26: 47, 27: 48, 43: 49, 39: 51, 40: 52, 41: 53,
    51: 54, 52: 55, 53: 56, 57: 44,
    # terminators → HID Enter (usage 40): CR, keypad Enter, LF
    KEY_ENTER: 40, KEY_KPENTER: 40, KEY_LINEFEED: 40,
}

# Every keycode must translate to a usage the shared assembler can decode
# (a _USAGE_MAP entry) or to the Enter terminator (usage 40). A table typo
# that invents an usage would otherwise decode as silence — fail at import.
_USAGE_ENTER = 40
assert all(usage in _USAGE_MAP or usage == _USAGE_ENTER
           for usage in KEYCODE_TO_USAGE.values()), "KEYCODE_TO_USAGE references an undecodable usage"


# Shift keys build the synthetic report's modifier byte (the same bits
# parse_keyboard_report tests: LEFT_SHIFT 0x02, RIGHT_SHIFT 0x20).
SHIFT_KEYS: dict[int, int] = {
    KEY_LEFTSHIFT: LEFT_SHIFT,
    KEY_RIGHTSHIFT: RIGHT_SHIFT,
}


def _warn_unmapped(code: int) -> None:
    """The default unmapped-key report: loud, non-fatal (stderr, one line)."""
    print(f"scanner_daemon: unmapped keycode {code} — partial scan discarded",
          file=sys.stderr)


class EvdevDecoder:
    """Stateful EV_KEY decoder: shift state + the shared ScanAssembler.

    ``feed(type_, code, value)`` applies one input_event and returns the
    completed scan string, or ``None``. Only presses (``value == 1``) of
    mapped keys emit synthetic reports; releases (0) update the shift mask;
    autorepeats (2) are ignored — a stuck or repeating key must not
    duplicate a character.
    """

    def __init__(self, on_unmapped=None) -> None:
        self._assembler = ScanAssembler()
        self._shift = 0
        self._on_unmapped = _warn_unmapped if on_unmapped is None else on_unmapped

    def feed(self, type_: int, code: int, value: int) -> str | None:
        if type_ != EV_KEY:
            return None  # EV_SYN / EV_MSC / EV_LED … — not scan data
        if code in SHIFT_KEYS:
            if value == 1:
                self._shift |= SHIFT_KEYS[code]
            elif value == 0:
                self._shift &= ~SHIFT_KEYS[code]
            return None
        if value != 1:
            return None  # release (0) or autorepeat (2)
        usage = KEYCODE_TO_USAGE.get(code)
        if usage is None:
            # A key this decoder cannot represent: the buffer cannot be a
            # complete textual scan. Report and reset — never guess.
            self._on_unmapped(code)
            self._assembler = ScanAssembler()
            return None
        report = bytes([self._shift, 0x00, usage, 0, 0, 0, 0, 0])
        return self._assembler.feed(report)


# --- discovery ----------------------------------------------------------------

_SYSFS_INPUT_CLASS = "/sys/class/input"


def _read_sysfs_int(path: Path) -> int | None:
    """Read a sysfs hex id (``id/vendor`` &co: ``065a``, ``0x065a`` …)."""
    try:
        text = path.read_text().strip().lower()
    except OSError:
        return None
    text = text.removeprefix("0x")
    try:
        return int(text, 16)
    except ValueError:
        return None


def _event_node_number(name: str) -> tuple[int, str]:
    """Sort key: numeric by node number (event2 < event10 < eventN-garbage)."""
    digits = name.removeprefix("event")
    return (int(digits) if digits.isdigit() else 1 << 30, name)


def discover_event_devices(
    vid_pid: tuple[int, int] = DEFAULT_VID_PID,
    sysfs_root: str | Path = _SYSFS_INPUT_CLASS,
) -> list[str]:
    """All ``/dev/input/eventN`` nodes whose device matches the VID/PID.

    Walks ``<sysfs_root>/event*/device/id/{vendor,product}`` (sysfs stores
    lowercase hex, with or without ``0x`` — both tolerated). Nodes without
    an ``id/`` directory (pseudo devices) are skipped. The root is
    injectable so tests can point at a fake tree in ``tmp_path``; the
    default is the real class directory. Does NOT rely on
    ``/dev/input/by-id`` (absent on this host — r4rev M3).
    """
    vid, pid = vid_pid
    root = Path(sysfs_root)
    matches = []
    try:
        entries = [e for e in root.iterdir() if e.name.startswith("event")]
    except OSError:
        return []
    for entry in sorted(entries, key=lambda e: _event_node_number(e.name)):
        ident = entry / "device" / "id"
        if _read_sysfs_int(ident / "vendor") == vid and \
                _read_sysfs_int(ident / "product") == pid:
            matches.append(f"/dev/input/{entry.name}")
    return matches


def discover_event_device(
    vid_pid: tuple[int, int] = DEFAULT_VID_PID,
    sysfs_root: str | Path = _SYSFS_INPUT_CLASS,
) -> str | None:
    """The scanner's event node, or ``None`` when it is not attached.

    Deterministic on multi-interface devices (a scanner may expose more
    than one input node): the lowest-numbered matching node wins here;
    T3's wrong-device guards refine the choice before any grab.
    """
    devices = discover_event_devices(vid_pid, sysfs_root)
    return devices[0] if devices else None


# --- the read loop -------------------------------------------------------------

def read_scans(fd, tv_size: int | None = None, on_unmapped=None):
    """Yield one scan string per terminator from an OPEN event-device fd.

    Blocking generator: reads arch-sized records (``tv_size`` overrides,
    e.g. for a stream captured on another arch), decodes presses, and
    yields each completed scan. Handles records split across reads (a
    partial record is held until its remainder arrives) and ends cleanly
    on EOF. The open/EVIOCGRAB/lifecycle is T3 — this takes an already
    open fd (int or file object with ``fileno()``), so tests can drive it
    from an ``os.pipe`` with synthetic events and no hardware.
    """
    tv = NATIVE_TV_SIZE if tv_size is None else tv_size
    size = event_size(tv)
    decoder = EvdevDecoder(on_unmapped=on_unmapped)
    fileno = fd.fileno() if hasattr(fd, "fileno") else fd
    buf = b""
    while True:
        chunk = os.read(fileno, 4096)
        if not chunk:
            return  # EOF: caller (T3) decides what that means
        buf += chunk
        while len(buf) >= size:
            record, buf = buf[:size], buf[size:]
            scan = decoder.feed(*parse_input_event(record, tv_size=tv))
            if scan is not None:
                yield scan
