"""scanner_daemon — the evdev decode core of the round-4 grab daemon.

The M-10 in USB-HID keyboard mode (065a:a001) "types" each scan into
whatever window has OS focus. Round 4 grabs the scanner at the OS level:

* T2 (docs/plans/r4t2-FINAL.md), the decode core — ``discover_event_device``
  (find ``/dev/input/eventN`` by VID/PID in sysfs), ``parse_input_event``
  (unpack ``struct input_event``), ``EvdevDecoder`` / ``read_scans``
  (EV_KEY events → scan strings),
* T3 (docs/plans/r4t3-FINAL.md), the grab + lifecycle — ``GrabbedDevice``
  (EVIOCGRAB context manager with the human-keyboard guard), the
  re-discovery ``run_loop``, and the ``DaemonLock`` single-instance
  lockfile,
* T5 (docs/plans/r4t5-FINAL.md), forwarding + packaging — ``main()``
  (``--config``/``--register``/``--forward``/``--dry-run``), the 0600
  terminal config, the spooled :class:`Forwarder` (a server outage never
  stops the grab and never drops a scan), redaction on every print path,
  and SIGTERM/SIGINT clean exit.

**The pairing fallback (``--find-scanner``).** A stale three-way identity
mismatch — config secret of a FOREIGN terminal, offer file naming an
UNREGISTERED one, kiosk pill answering 401 while ``--register`` answers
409 — strands every scan in a queue no local browser can read
(2026-11-17 incident). ``--find-scanner`` is the read-only diagnosis for
that class: device/node present? grab held? another daemon running (said
in plain words, never a lock error)? does the config terminal exist
server-side, which OTHER terminals do, and does the config secret belong
to one of them? offer file consistent? It writes NOTHING, never takes the
single-instance lock or keeps a grab, and ends in exactly ONE actionable
recovery ladder (exact operator commands, printed — never executed).
Registration failures get the same treatment: :func:`_registration_recovery`
turns a 409/401/403/unreachable :class:`PostError` into one ladder instead
of a raw error dump. ``save_config`` stays honest by construction: it is
only ever reached after the server ACCEPTED the registration.

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
with an exclusive grab, every key from this device is a scan key, and
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

import argparse
import errno
import fcntl
import hashlib
import json
import math
import os
import secrets
import signal
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

# Allow `python scripts/scanner_daemon.py` from anywhere (the
# scanner_bridge bootstrap pattern): the repo root makes `scripts.*` and
# `app.*` imports resolve.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
    on EOF. The open/EVIOCGRAB/lifecycle is :class:`GrabbedDevice` below —
    this takes an already open fd (int or file object with ``fileno()``),
    so tests can drive it from an ``os.pipe`` with synthetic events and no
    hardware.
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


# --- T3: the exclusive grab ----------------------------------------------------

# EVIOCGRAB = _IOW('E', 0x90, int):
#   (IOC_WRITE << 30) | (sizeof(int) << 16) | ('E' << 8) | 0x90
#   = 0x40044590 — pinned by tests/test_scanner_daemon_grab.py.
EVIOCGRAB = 0x40044590

# evdev read errors that mean "the device is gone" (unplug, usbipd detach,
# the xHCI controller dying again — docs/m10-unreachable-2026-10-10.md):
# transient by policy; the re-discovery loop retries, never exits.
_LOST_ERRNOS = frozenset({errno.ENODEV, errno.EIO, errno.ENOENT, errno.ENXIO})


class GrabError(RuntimeError):
    """Base of the grab contract's failures (loud, actionable messages)."""


class WrongDeviceError(GrabError):
    """The node is not (or cannot be verified as) the scanner.

    The human-keyboard guard: EVIOCGRAB on the wrong node would silently
    disable typing system-wide, so a node is grabbed only after sysfs
    confirms its VID/PID — including at RE-grab time, because event node
    numbers are recycled across replugs (r4rev M2).
    """


class GrabBusyError(GrabError):
    """Another process already holds the grab (EBUSY)."""


class GrabPermissionError(GrabError):
    """No permission to open/grab the node (EACCES/EPERM)."""


class DeviceLostError(GrabError):
    """The device disappeared (ENODEV/EIO/EOF) — transient: re-discover."""


class InstanceAlreadyRunning(GrabError):
    """A second daemon instance (the lockfile is held)."""


def _verify_node(node: str, vid_pid: tuple[int, int],
                 sysfs_root: str | Path) -> None:
    """Raise WrongDeviceError unless sysfs proves `node` is the scanner.

    Accepts a full ``/dev/input/eventN`` path or a bare ``eventN`` name.
    An unreadable/absent id (device vanished, garbage sysfs) is ALSO a
    refusal — an unverifiable device is never grabbed. ENOENT-style
    absence is indistinguishable from a recycled node here, so the safe
    direction is always "refuse".
    """
    name = os.path.basename(node.rstrip("/"))
    ident = Path(sysfs_root) / name / "device" / "id"
    vid, pid = vid_pid
    found = (_read_sysfs_int(ident / "vendor"), _read_sysfs_int(ident / "product"))
    if found != vid_pid:
        seen = "unreadable" if found[0] is None else f"{found[0]:04x}:{found[1]:04x}"
        raise WrongDeviceError(
            f"refusing to grab {node}: sysfs does not verify it as "
            f"{vid:04x}:{pid:04x} (says: {seen}) — the human-keyboard guard; "
            "never grab an unverified device"
        )


def _open_readonly(node: str) -> int:
    """The default opener: O_RDONLY is sufficient for EVIOCGRAB (the ioctl
    path checks no write permission) and for reading the grabbed stream."""
    return os.open(node, os.O_RDONLY)


class GrabbedDevice:
    """Open + exclusively grab one event device (context manager).

    Contract (docs/plans/r4t3-FINAL.md):

    1. open the node ``O_RDONLY``;
    2. VERIFY it is the scanner (VID/PID via sysfs) — never grab an
       unverified node; the ioctl is not even attempted otherwise;
    3. ``ioctl(fd, EVIOCGRAB, 1)`` — from here the kernel delivers this
       device's events to us ONLY: nothing is typed into any window.

    Failure mapping: ``EACCES``/``EPERM`` → :class:`GrabPermissionError`;
    ``EBUSY`` → :class:`GrabBusyError` (another grabber holds it);
    ``ENODEV``/``EIO``/``ENOENT`` → :class:`DeviceLostError` (transient —
    the re-discovery loop handles those, everything else is loud).
    Exiting the context — normally, on exception, on crash — closes the fd,
    and closing the fd RELEASES the grab (kernel guarantee), so the
    scanner reverts to keyboard-wedge with zero operator action.

    ``opener``/``ioctl``/``closer`` are injected per the T2 doctrine
    (offline tests, no hardware): the defaults are ``os.open`` (O_RDONLY),
    ``fcntl.ioctl`` and ``os.close``.
    """

    def __init__(self, node, *, vid_pid: tuple[int, int] = DEFAULT_VID_PID,
                 sysfs_root: str | Path = _SYSFS_INPUT_CLASS,
                 opener=None, ioctl=None, closer=None) -> None:
        self.node = node
        self.vid_pid = vid_pid
        self._sysfs_root = sysfs_root
        self._open = _open_readonly if opener is None else opener
        self._ioctl = fcntl.ioctl if ioctl is None else ioctl
        self._close = os.close if closer is None else closer
        self.fd = None
        self._grabbed = False

    def __enter__(self) -> "GrabbedDevice":
        try:
            self.fd = self._open(self.node)
        except OSError as exc:
            raise self._map_open_error(exc) from exc
        try:
            # Verify BEFORE grabbing: the ioctl must never touch an
            # unverified node (node numbers are recycled across replugs).
            _verify_node(self.node, self.vid_pid, self._sysfs_root)
            self._ioctl(self.fd, EVIOCGRAB, 1)
            self._grabbed = True
        except GrabError:
            self._release()
            raise
        except OSError as exc:
            self._release()
            raise self._map_grab_error(exc) from exc
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._release()
        return False  # never swallow

    def _release(self) -> None:
        """Un-grab (best-effort, only if the grab was taken) and close —
        the close itself releases the grab even if the ioctl is refused,
        which is why it is unconditional."""
        fd, self.fd = self.fd, None
        if fd is None:
            return
        if self._grabbed:
            try:
                self._ioctl(fd, EVIOCGRAB, 0)
            except OSError:
                pass  # the close below releases the grab regardless
        self._close(fd)

    def _map_open_error(self, exc: OSError) -> GrabError | OSError:
        if exc.errno in (errno.EACCES, errno.EPERM):
            return GrabPermissionError(self._permission_message(exc))
        if exc.errno in _LOST_ERRNOS:
            return DeviceLostError(f"{self.node} disappeared before open: {exc}")
        return exc

    def _map_grab_error(self, exc: OSError) -> GrabError | OSError:
        if exc.errno == errno.EBUSY:
            return GrabBusyError(
                f"another process holds the scanner grab on {self.node} — "
                "a second scanner_daemon, evtest, or an input remapper?"
            )
        if exc.errno in (errno.EACCES, errno.EPERM):
            return GrabPermissionError(self._permission_message(exc))
        if exc.errno in _LOST_ERRNOS:
            return DeviceLostError(f"{self.node} disappeared during grab: {exc}")
        return exc

    def _permission_message(self, exc: OSError) -> str:
        return (
            f"no permission to open/grab {self.node} ({exc}) — install the "
            "udev rule (scripts/udev/99-opticon-scanner.rules), or run in "
            "the input/plugdev group, or use the systemd unit "
            "(scripts/systemd/scanner-daemon.service)"
        )


# --- T3: the single-instance lockfile -------------------------------------------

def _default_lockfile_path() -> Path:
    """XDG_RUNTIME_DIR (``/run/user/$UID`` under systemd) or a tempdir
    fallback — a per-user default, so two users' daemons do not fight over
    one lockfile while the kernel grab (EBUSY) remains the real interlock."""
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / "hcrm-scanner-daemon.lock"


class DaemonLock:
    """flock-based single-instance lock (one daemon per user).

    flock is held by the open file description and RELEASED BY THE KERNEL
    when the process dies — there is no stale-lockfile recovery problem,
    which is why it is used instead of O_EXCL. A held lock raises
    :class:`InstanceAlreadyRunning`; the holder's PID is written into the
    file for diagnostics only.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else _default_lockfile_path()
        self._fd: int | None = None

    def acquire(self) -> "DaemonLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise InstanceAlreadyRunning(
                    f"another scanner_daemon instance is already running "
                    f"(lockfile: {self.path})"
                ) from exc
            raise
        self._fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        return self

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # releases the flock
            self._fd = None

    def __enter__(self) -> "DaemonLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False


# --- T3: the re-discovery run loop ----------------------------------------------

def _log(message: str) -> None:
    """The default lifecycle log: stderr, one line (journald picks it up)."""
    print(f"scanner_daemon: {message}", file=sys.stderr)


def _pump(dev: GrabbedDevice, on_scan, scan_reader, on_unmapped) -> None:
    """Consume the grabbed stream until the device is lost.

    EOF and ENODEV/EIO both mean "gone" (evdev signals removal either way);
    anything else from the reader propagates (a callback bug must not look
    like a device loss).
    """
    iterator = scan_reader(dev.fd, on_unmapped=on_unmapped)
    while True:
        try:
            scan = next(iterator)
        except StopIteration:
            raise DeviceLostError(f"{dev.node}: event stream ended (EOF)")
        except OSError as exc:
            if exc.errno in (errno.ENODEV, errno.EIO):
                raise DeviceLostError(f"{dev.node}: read failed: {exc}") from exc
            raise
        on_scan(scan)


def run_loop(on_scan, *, node=None, vid_pid: tuple[int, int] = DEFAULT_VID_PID,
             sysfs_root: str | Path = _SYSFS_INPUT_CLASS,
             opener=None, ioctl=None, closer=None,
             scan_reader=read_scans, on_unmapped=None,
             regrab_delay: float = 2.0, sleep=time.sleep,
             stop=None, log=_log) -> None:
    """The daemon's device lifecycle: discover → grab → decode → on_scan.

    On device loss (unplug, controller death, EOF) the loop closes
    (releasing the grab), sleeps, and re-discovers by VID/PID — never a
    remembered ``eventN`` (node numbers are recycled, r4rev M2) and never
    an exit: a transient unplug must not turn an always-on daemon off. A
    pinned ``node`` is used for the FIRST attempt only; after any loss the
    loop trusts discovery. When no device is found at all it keeps retrying
    (the scanner may not be attached yet — qwen M1).

    The loud failures — :class:`GrabBusyError`, :class:`GrabPermissionError`,
    :class:`WrongDeviceError` — PROPAGATE: they are operator problems, not
    transients, and T5's entrypoint turns them into a loud exit. ``stop()``
    is polled after sleeps and scans and ends the loop cleanly (T5 wires it
    to SIGTERM); ``sleep``/``scan_reader``/the grab injectables keep the
    whole loop offline-testable.
    """
    first = True
    while True:
        target = node if (node is not None and first) else \
            discover_event_device(vid_pid, sysfs_root)
        first = False
        if target is None:
            vid, pid = vid_pid
            log(f"no {vid:04x}:{pid:04x} event device — retrying in "
                f"{regrab_delay:g}s")
            sleep(regrab_delay)
            if stop is not None and stop():
                return
            continue
        try:
            with GrabbedDevice(target, vid_pid=vid_pid, sysfs_root=sysfs_root,
                               opener=opener, ioctl=ioctl, closer=closer) as dev:
                log(f"grabbed {target}")
                _pump(dev, on_scan, scan_reader, on_unmapped)
        except DeviceLostError as exc:
            log(f"device lost ({exc}); re-discovering in {regrab_delay:g}s")
            sleep(regrab_delay)
            if stop is not None and stop():
                return
            continue


# --- T5: forwarding, pairing, config, packaging ---------------------------------

# Reused, not re-implemented: the bridge's redaction is the ONE masking rule
# (T1 delegated it to the shared shapes), so badge payloads cannot be printed
# raw here even by accident of a copied-but-stale table.
from scripts.scanner_bridge import redact_code  # noqa: E402


class PostError(Exception):
    """One failed HTTP POST. ``status`` 0 = transport (unreachable/timeout);
    anything else is the HTTP status. Transient failures (transport, 5xx,
    429) are spooled and retried; permanent ones (401 = the terminal was
    deleted / secret rotated, 409, …) are spooled and reported loudly — the
    daemon keeps grabbing either way (stopping the typing is the product;
    delivery is best-effort with a visible backlog)."""

    def __init__(self, status: int, detail: str = ""):
        super().__init__(f"POST failed ({status or 'network'}): {detail}")
        self.status = status

    @property
    def transient(self) -> bool:
        return self.status == 0 or self.status >= 500 or self.status == 429


def _http_post(url, headers, body, timeout=10.0):
    """The default transport: one JSON POST, stdlib only.

    urllib is imported LAZILY so ``--dry-run`` (and the offline test suite)
    touch no network code at all. Raises :class:`PostError`, never a raw
    traceback (the bridge doctrine: no failure of the environment may look
    like a crash).
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url, method="POST", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        raise PostError(exc.code, detail) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise PostError(0, str(exc)) from exc


class Spool:
    """The ordered delivery backlog (server down, daemon restarted mid-outage).

    Entries are FIFO and per-physical-scan: two scans of the same barcode
    are two entries (a re-scan is a quantity, not a duplicate) — "dedupe"
    means the FORWARDER never delivers one entry twice, not that identical
    codes collapse. Bounded (drop-oldest past ``cap``) and optionally
    file-backed (``0600``; a spool can briefly hold badge payloads, the
    same at-rest trade-off as the server's scan_events TTL, stated in
    README's security notes). The whole file is rewritten on change —
    spools are tiny and correctness beats cleverness here.
    """

    def __init__(self, path=None, cap=1000, log=None):
        self.path = Path(path) if path is not None else None
        self.cap = cap
        self._log = log or (lambda message: None)
        self._entries: list[str] = []
        if self.path is not None and self.path.exists():
            try:
                self._entries = [line for line in
                                 self.path.read_text().splitlines() if line]
            except OSError as exc:
                self._log(f"spool unreadable ({exc}) — starting empty")

    def pending(self) -> list[str]:
        return list(self._entries)

    def add(self, code: str) -> None:
        self._entries.append(code)
        while len(self._entries) > self.cap:
            dropped = self._entries.pop(0)
            self._log(f"spool full — oldest scan dropped ({redact_code(dropped)})")
        self._persist()

    def remove_oldest(self) -> None:
        self._entries.pop(0)
        self._persist()

    def _persist(self) -> None:
        if self.path is None:
            return
        if not self._entries:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, ("\n".join(self._entries) + "\n").encode())
        finally:
            os.close(fd)


class Forwarder:
    """Deliver scans to POST /api/scanner/events, in order, without ever
    stopping the grab loop.

    Every scan enters the spool FIRST and is then drained head-first: a
    successful POST removes its entry before the next is tried, so a crash
    can at worst re-POST the head (never skip or reorder), and a failed POST
    leaves the backlog for the next scan/startup to retry — the loop keeps
    listening the whole time. Permanent rejections (401: the terminal was
    deleted server-side) are logged loudly and kept spooled; recovery is
    DELETE + ``--register``.
    """

    def __init__(self, post, url, secret, spool, log=_log):
        self._post = post
        self._url = url
        self._secret = secret
        self.spool = spool
        self._log = log

    def handle(self, code: str) -> None:
        """run_loop's on_scan: enqueue + one drain attempt."""
        self.spool.add(code)
        self.drain()

    def drain(self) -> int:
        """Deliver the backlog head-first; stop at the first failure.
        Returns the number of entries delivered in this pass."""
        delivered = 0
        while self.spool.pending():
            code = self.spool.pending()[0]
            try:
                self._post(self._url, {"X-Scanner-Secret": self._secret},
                           {"code": code})
            except PostError as exc:
                if not exc.transient:
                    self._log(f"forwarding REJECTED ({exc}) — scans spooled; "
                              "if this persists, the terminal was deleted "
                              "server-side: DELETE it and re-run --register")
                else:
                    self._log(f"server unreachable ({exc}) — scan spooled "
                              f"({len(self.spool.pending())} waiting)")
                return delivered
            self.spool.remove_oldest()
            delivered += 1
            self._log(f"forwarded {redact_code(code)}")
        return delivered


# --- config (the daemon's identity: {terminal_id, secret}, 0600) ----------------

def load_config(path) -> dict | None:
    """Read {terminal_id, secret}, or None when absent/unparseable."""
    try:
        cfg = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict) or "terminal_id" not in cfg or "secret" not in cfg:
        return None
    return cfg


def derive_machine_key(config: dict | None = None, *,
                       machine_id_path: str = "/etc/machine-id") -> str:
    """The laptop's stable identity: sha256(/etc/machine-id)[:16].

    Never derived from the hostname or the network. When the machine-id file
    is absent (macOS/dev), fall back to a persisted random 16-hex value in the
    config; with neither, mint a fresh one (the caller persists it via
    save_config). Injectable path so tests run with no /etc/machine-id.
    """
    try:
        raw = Path(machine_id_path).read_text().strip()
    except OSError:
        raw = ""
    if raw:
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
    existing = (config or {}).get("machine_key")
    if (isinstance(existing, str) and len(existing) == 16
            and all(c in "0123456789abcdef" for c in existing)):
        return existing
    return secrets.token_hex(8)


def save_config(path, terminal_id: str, secret: str, *,
                machine_key: str | None = None,
                server_url: str | None = None,
                site_origin: str | None = None) -> None:
    """Write the config 0600 — it holds a write credential for the API.

    The legacy two keys stay byte-compatible; the portable path additionally
    stores machine_key/server_url/site_origin (each only when provided). The
    invite is NEVER written here. load_config tolerates the extra keys' absence.
    """
    cfg = {"terminal_id": terminal_id, "secret": secret}
    if machine_key is not None:
        cfg["machine_key"] = machine_key
    if server_url is not None:
        cfg["server_url"] = server_url
    if site_origin is not None:
        cfg["site_origin"] = site_origin
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(cfg, indent=2).encode())
    finally:
        os.close(fd)


#: The one-click pairing offer file: this daemon writes it after a
#: successful --register, the server (app/routers/scanner.py _pairing_file)
#: reads and consumes it. Same env name, SAME default — the two sides must
#: resolve the same path or the first click 404s (pinned by
#: test_default_pairing_file_matches_the_server_side).
_PAIRING_FILE_ENV = "HCRM_SCANNER_PAIRING_FILE"
_PAIRING_FILE_NAME = "scanner-pairing.json"


def default_pairing_file() -> Path:
    """The offer file's path: $HCRM_SCANNER_PAIRING_FILE, else
    <HCRM_DATA_DIR or <repo>/data>/scanner-pairing.json.

    Stdlib-only on purpose (no app.* import — this is a standalone
    kiosk script); the default mirrors app.database.DATA_DIR's, exactly
    like app/security.py's _badge_data_dir()."""
    data_dir = os.environ.get(
        "HCRM_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data"))
    return Path(os.environ.get(
        _PAIRING_FILE_ENV, os.path.join(data_dir, _PAIRING_FILE_NAME)))


def save_pairing_file(path, terminal_id: str, code: str, *,
                      expires_at: float | None = None) -> None:
    """Write the one-click pairing offer 0600, ATOMICALLY.

    A browser click can land at any moment, so a partially-written file
    must never be observable: write a 0600 tempfile in the same directory
    and os.replace() it into place (replace preserves the temp's mode).
    The legacy two keys stay byte-compatible; portable offers additionally
    carry ``expires_at`` (Unix seconds) for restart-safe TTL checks. Never a
    secret or token: the file is a single-use code, not a credential store.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)  # app.database does the same
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                                prefix=".scanner-pairing-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:  # mkstemp is 0600
            offer = {"terminal_id": terminal_id, "pairing_code": code}
            if expires_at is not None:
                offer["expires_at"] = expires_at
            out.write(json.dumps(offer, indent=2))
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_pairing_file(path):
    """Read the one-click offer file, or None when it cannot be read at all.

    Returns whatever JSON the file holds (a dict in every healthy case): a
    malformed-but-parseable offer — a list, an int, an object without
    ``terminal_id`` — must still be REPORTED by :func:`identity_findings`,
    never silently mistaken for an absent file (D6: the malformed path keeps
    the output it had before the refactor). None means absent / unreadable /
    not JSON, the same tolerance as the inline read this replaces. The
    pairing code inside is never printed.
    """
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


# --- loopback popup agent (P2) -----------------------------------------------

class NonceStore:
    """Bounded, in-memory, single-use popup nonces (120 s maximum)."""

    def __init__(self, *, ttl: float = 120.0, cap: int = 1024,
                 clock=time.monotonic):
        if not 0 < ttl <= 120:
            raise ValueError("nonce TTL must be in (0, 120]")
        self.ttl = float(ttl)
        self.cap = cap
        self.clock = clock
        self._items: dict[str, float] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float, *, for_issue: bool = False) -> None:
        expired = [nonce for nonce, deadline in self._items.items()
                   if deadline <= now]
        for nonce in expired:
            self._items.pop(nonce, None)
        if for_issue:
            while len(self._items) >= self.cap:
                oldest = min(self._items, key=self._items.get)
                self._items.pop(oldest, None)

    def issue(self) -> str:
        with self._lock:
            now = self.clock()
            self._prune(now, for_issue=True)
            nonce = secrets.token_urlsafe(16)
            while nonce in self._items:
                nonce = secrets.token_urlsafe(16)
            self._items[nonce] = now + self.ttl
            return nonce

    def consume(self, nonce: str) -> bool:
        with self._lock:
            now = self.clock()
            self._prune(now)
            deadline = self._items.pop(nonce, None)
            return deadline is not None and deadline > now


class OfferStore:
    """Memory-first pairing-code store with a TTL'd atomic file fallback."""

    def __init__(self, path, terminal_id: str, *, clock=time.monotonic,
                 wall_clock=time.time, ttl: float = 600.0):
        self.path = Path(path)
        self.terminal_id = terminal_id
        self.clock = clock
        self.wall_clock = wall_clock
        self.ttl = ttl
        self._code = None
        self._expires = None
        self._lock = threading.Lock()

    def put(self, code: str) -> None:
        if not isinstance(code, str) or len(code) != 8:
            raise ValueError("invalid pairing code shape")
        expires = self.clock() + self.ttl
        expires_wall = self.wall_clock() + self.ttl
        with self._lock:
            # Persist first: a failed atomic write never advertises a memory-only
            # offer that a restart could silently lose.
            save_pairing_file(self.path, self.terminal_id, code,
                              expires_at=expires_wall)
            self._code, self._expires = code, expires

    def get(self) -> str | None:
        with self._lock:
            if self._code is not None and self._expires > self.clock():
                return self._code
            offer = load_pairing_file(self.path)
            if not isinstance(offer, dict):
                return None
            if offer.get("terminal_id") != self.terminal_id:
                return None
            code = offer.get("pairing_code")
            if not isinstance(code, str) or len(code) != 8:
                return None
            expires = offer.get("expires_at")
            if expires is not None:
                try:
                    expired = float(expires) <= self.wall_clock()
                except (TypeError, ValueError):
                    return None
                if expired:
                    return None
            return code

    def available(self) -> bool:
        return self.get() is not None


class _LocalThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def _origin_of(url: str) -> str | None:
    """Canonical scheme://host[:port] for exact Origin/Referer comparison."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return None
        host = parts.hostname
        if not host:
            return None
        port = parts.port
    except ValueError:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    default = 80 if parts.scheme == "http" else 443
    netloc = host if port in (None, default) else f"{host}:{port}"
    return f"{parts.scheme}://{netloc}"


def _script_json(value) -> str:
    """JSON safe to inline in a script element (not merely HTML-escape)."""
    return (json.dumps(value, separators=(",", ":"), ensure_ascii=True)
            .replace("&", "\\u0026").replace("<", "\\u003c")
            .replace(">", "\\u003e").replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


class _AgentRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        """Never put nonce-bearing URLs or offers into an access log."""

    def do_GET(self):
        self.server.agent._dispatch(self)


def _device_not_configured():
    """Mimo replaces this with Heartbeater.snapshot in main()."""
    raise RuntimeError("Heartbeater is not configured")


class LocalAgentServer:
    """Loopback-only popup/status server with origin-pinned offers."""

    def __init__(self, port: int, *, config: dict, offers: OfferStore,
                 nonces: NonceStore, device, post, stop_event=None, log=_log):
        self.requested_port = port
        self.port = port
        self.config = dict(config)
        self.offers = offers
        self.nonces = nonces
        self.device = device
        self.post = post
        self.stop_event = stop_event or threading.Event()
        self.log = log
        self._http = None
        self._thread = None

    def start(self) -> bool:
        try:
            self._http = _LocalThreadingHTTPServer(
                ("127.0.0.1", self.requested_port), _AgentRequestHandler)
        except OSError as exc:
            self.log(f"local popup port 127.0.0.1:{self.requested_port} "
                     f"is already in use ({exc.strerror or exc}); continuing "
                     "without local server (grab continues)")
            self._http = None
            return False
        self.port = self._http.server_address[1]
        self._http.agent = self
        self._thread = threading.Thread(
            target=self._http.serve_forever, name="scanner-local-http",
            daemon=True)
        self._thread.start()
        return True

    def close(self) -> None:
        http, thread = self._http, self._thread
        self._http = self._thread = None
        if http is not None:
            http.shutdown()
            http.server_close()
        if thread is not None:
            thread.join(timeout=2.0)

    @staticmethod
    def _send(handler, status: int, body: bytes, content_type: str,
              *, close=False, extra_headers=None):
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        if close:
            handler.send_header("Connection", "close")
            handler.close_connection = True
        for name, value in (extra_headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(body)

    def _json(self, handler, status: int, payload: dict, *, close=False,
              extra_headers=None):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self._send(handler, status, body, "application/json", close=close,
                   extra_headers=extra_headers)

    def _html(self, handler, status: int, html: str, *, nonce: str | None = None,
              close=False):
        headers = {"Referrer-Policy": "no-referrer"}
        if nonce is not None:
            headers["Content-Security-Policy"] = (
                f"default-src 'none'; script-src 'nonce-{nonce}'; "
                "connect-src 'self'; base-uri 'none'")
        self._send(handler, status, html.encode(),
                   "text/html; charset=utf-8", close=close,
                   extra_headers=headers)

    def _dispatch(self, handler) -> None:
        # Guard order (R1/R2): peer -> Host -> route -> Origin -> nonce.
        peer = handler.client_address[0]
        if peer not in ("127.0.0.1", "::1"):
            self._json(handler, 403, {"error": "loopback_only"}, close=True)
            return
        expected_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if handler.headers.get("Host", "").lower() not in expected_hosts:
            self._json(handler, 403, {"error": "bad_host"}, close=True)
            return
        path = urlsplit(handler.path).path
        if handler.command != "GET":
            self._json(handler, 405, {"error": "method_not_allowed"},
                       extra_headers={"Allow": "GET"})
            return
        if path == "/status":
            self._status(handler)
            return
        if path == "/pair":
            self._pair(handler)
            return
        self._json(handler, 404, {"error": "not_found"})

    def _snapshot(self, handler):
        try:
            return self.device()
        except (OSError, RuntimeError):
            self._json(handler, 503, {"error": "device_unavailable"})
            return None

    def _status(self, handler) -> None:
        device = self._snapshot(handler)
        if device is None:
            return
        self._json(handler, 200, {
            "ok": True,
            "terminal_id": self.config.get("terminal_id"),
            "server_url": self.config.get("server_url"),
            "device": device,
            "pairing_code_available": self.offers.available(),
            "agent_version": "1",
        })

    def _origin_allowed(self, handler) -> bool:
        expected = _origin_of(self.config.get("site_origin", ""))
        if expected is None:
            return False
        origin = handler.headers.get("Origin")
        if origin is not None:
            return origin == expected
        referer = handler.headers.get("Referer")
        return bool(referer and _origin_of(referer) == expected)

    def _pair(self, handler) -> None:
        if not self._origin_allowed(handler):
            self._html(handler, 403,
                       "<!doctype html><p>Origin refused — open pairing "
                       "from the configured site.</p>")
            return
        query = urlsplit(handler.path).query
        params = dict(part.split("=", 1) if "=" in part else (part, "")
                      for part in query.split("&") if part)
        nonce = params.get("nonce")
        if nonce is None:
            fresh = self.nonces.issue()
            self._send(handler, 307, b"", "text/plain; charset=utf-8",
                       extra_headers={"Location": "/pair?" + urlencode({
                           "nonce": fresh,
                           "reclaim": params.get("reclaim", "0")})})
            return
        if not self.nonces.consume(nonce):
            self._html(handler, 403,
                       "<!doctype html><p>Pairing link expired or already "
                       "used — press the pill again.</p>")
            return
        reclaim = params.get("reclaim", "0")
        if reclaim not in ("0", "1"):
            self._json(handler, 400, {"error": "bad_reclaim"})
            return
        device = self._snapshot(handler)
        if device is None:
            return
        code = self.offers.get() if reclaim == "0" else self._reclaim_code()
        reason = None if code else ("reclaim_failed" if reclaim == "1"
                                    else "no_offer")
        payload = {
            "type": "hcrm.agent.offer",
            "nonce": nonce,
            "terminal_id": self.config.get("terminal_id"),
            "pairing_code": code,
            "status": {"device": device["status"],
                       "personality": device["personality"],
                       "node": device["node"]},
            "reason": reason,
        }
        self._html(handler, 200, self._render_pair(payload), nonce=nonce)

    def _reclaim_code(self) -> str | None:
        terminal_id = self.config.get("terminal_id")
        secret = self.config.get("secret")
        base_url = self.config.get("server_url")
        if not terminal_id or not secret or not base_url:
            return None
        if self.stop_event.is_set():
            return None
        try:
            result = self.post(
                base_url.rstrip("/") + "/api/scanner/terminals/"
                + quote(terminal_id, safe="") + "/reclaim",
                {"X-Scanner-Secret": secret}, {})
            code = result["pairing_code"]
            self.offers.put(code)
            return code
        except (PostError, OSError, ValueError, KeyError, TypeError):
            return None

    def _render_pair(self, payload: dict) -> str:
        offer = _script_json(payload)
        origin = _script_json(self.config.get("site_origin"))
        nonce = payload["nonce"]
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; base-uri 'none'">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Scanner pairing</title></head>
<body><p id="message">Pair this laptop's scanner.</p><script nonce="{nonce}">
"use strict"; const OFFER = {offer}; const SITE_ORIGIN = {origin};
const message = document.getElementById("message");
if (!window.opener) {{ message.textContent = "Open this page from the site's scanner pill."; }}
else {{ window.opener.postMessage(OFFER, SITE_ORIGIN);
  if (!OFFER.reason) setTimeout(() => window.close(), 250);
  else message.textContent = OFFER.reason === "no_offer" ? "No offer is available." : "Reclaim failed."; }}
</script></body></html>"""


def _is_foreign_identity(config, hostname) -> bool:
    """True when the config forwards as an identity this host does not own.

    Compared verbatim, because the server resolves terminals with a
    case-sensitive equality (app/routers/scanner.py: register, pair and ingest
    all match ``Terminal.terminal_id`` with ``==``; app/schemas.py only
    constrains the pattern), so a case-only difference is a DIFFERENT terminal
    and is delivery-breaking. Absent or empty values never count as a mismatch
    (there is nothing to compare).
    """
    if not isinstance(config, dict):
        return False
    terminal_id = config.get("terminal_id")
    if not isinstance(terminal_id, str) or not terminal_id or not hostname:
        return False
    return terminal_id != hostname


def _id_is_known(terminal_id, terminal_ids) -> bool:
    """``terminal_id in terminal_ids`` that tolerates unhashable JSON values
    (a list/dict inside a malformed offer): those are never a known id."""
    try:
        return terminal_id in terminal_ids
    except TypeError:
        return False


#: Env override for --require-host-identity. Same convention as
#: app/security.py::_env_flag (1/true/yes/on, case- and whitespace-
#: insensitive; empty or unknown => default) — a local copy on purpose:
#: this script is standalone stdlib and must not import app.*.
_REQUIRE_HOST_IDENTITY_ENV = "HCRM_SCANNER_REQUIRE_HOST_IDENTITY"

#: The fatal-path line printed once, right before the gated exit 1.
_REFUSE_HOST_IDENTITY = (
    "require-host-identity: refusing to run — fix the identity above (the "
    "recovery ladder is: scripts/scanner_daemon.py --find-scanner)")


def _env_flag(name: str, default: bool = False) -> bool:
    """app.security._env_flag's contract, read at CALL time (never import)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def register_terminal(post, base_url: str, terminal_id: str, secret: str, *,
                      invite: str | None = None,
                      machine_key: str | None = None) -> str:
    """POST /api/scanner/terminals → the single-use pairing code.

    Raises PostError (409 already-registered etc.): main turns that into a
    loud exit with the server's own recovery message. invite/machine_key ride
    the body only on the portable path; the legacy body is unchanged.
    """
    body = {"terminal_id": terminal_id, "secret": secret}
    if invite is not None:
        body["invite"] = invite
    if machine_key is not None:
        body["machine_key"] = machine_key
    resp = post(base_url.rstrip("/") + "/api/scanner/terminals", {}, body)
    return resp["pairing_code"]


REGISTER_RETRY_DELAY = 2.0


def register_with_retry(post, base_url, terminal_id, secret, *,
                        wait=0.0, delay=REGISTER_RETRY_DELAY,
                        clock=time.monotonic, sleep=time.sleep, log=_log,
                        invite=None, machine_key=None):
    """register_terminal with a bounded, transient-only retry budget.

    The deadline is monotonic and includes the time a failing POST spent, so
    ``wait`` is wall-clock seconds, not an attempt count. Only transient
    failures (status 0 / >=500 / 429) are retried; a permanent status (or a
    non-integer one, which legacy callers pass) raises immediately, and so
    does exhaustion — main()'s loud exit-1 contract is unchanged. The budget
    is spent in ``delay`` steps so the log line can say what is left.
    """
    deadline = clock() + float(wait)
    attempt = 1
    while True:
        try:
            return register_terminal(post, base_url, terminal_id, secret,
                                     invite=invite, machine_key=machine_key)
        except PostError as exc:
            # isinstance FIRST: a non-integer status (legacy callers pass
            # strings) would make PostError.transient's ``status >= 500``
            # raise TypeError instead of classifying as permanent.
            transient = isinstance(exc.status, int) and exc.transient
            remaining = deadline - clock()
            if not transient or remaining <= 0:
                # "budget exhausted" is only claimed when one existed: the
                # default single-attempt path (wait=0) stays exactly as
                # loud as today, without naming a budget it never had.
                if transient and wait > 0:
                    log(f"--register-wait budget of {wait:g}s exhausted "
                        f"after {attempt} attempt(s)")
                raise
            pause = min(delay, remaining)
            log(f"registration not possible yet ({exc}) — retrying in "
                f"{pause:g}s ({remaining:g}s of budget left)")
            sleep(pause)
            attempt += 1


# --- pairing fallback: the read-only --find-scanner report ----------------------
#
# Doctrine (the 2026-11-17 stale-identity incident): diagnose read-only, print
# the exact operator command for anything destructive, and say "a daemon is
# running and holds the grab" in plain words instead of failing with a lock
# error or silently reading nothing. No app.* imports (standalone kiosk
# script): the DB probe re-implements app.security.token_hash's algorithm
# (sha256 hexdigest) locally and opens sqlite with a mode=ro URI, so it can
# never create or write the live database.

def default_config_file() -> Path:
    """The daemon config's conventional path: <HCRM_DATA_DIR or <repo>/data>/
    scanner.json — the file the live daemon runs with, so ``--find-scanner``
    works bare. Mirrors default_pairing_file()'s resolution."""
    data_dir = os.environ.get(
        "HCRM_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data"))
    return Path(os.path.join(data_dir, "scanner.json"))


def portable_config_file() -> Path:
    """The PORTABLE config default (R6): $HCRM_SCANNER_CONFIG, else
    $XDG_CONFIG_HOME/hcrm/scanner.json, else ~/.config/hcrm/scanner.json.

    Only the new portable path (--register --invite) defaults here; the legacy
    same-host flow keeps --config / default_config_file() (the repo data dir).
    """
    env = os.environ.get("HCRM_SCANNER_CONFIG")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "hcrm" / "scanner.json"


def default_db_file() -> Path:
    """The server's sqlite file, resolved like app.database (DATABASE_URL's
    sqlite:/// form wins, else <HCRM_DATA_DIR or <repo>/data>/hcrm.db).
    Only ever opened mode=ro by :func:`inspect_terminals_db`."""
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("sqlite:///"):
        return Path(url[len("sqlite:///"):])
    data_dir = os.environ.get(
        "HCRM_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data"))
    return Path(os.path.join(data_dir, "hcrm.db"))


def _http_get(url, timeout=5.0) -> int:
    """GET `url` → the HTTP status, or 0 on any transport failure.

    The liveness probe for --find-scanner: GET /api/scanner/events WITHOUT a
    browser token always answers 401 on a healthy server (terminal-token
    auth), so 401 means "up". Stdlib-only, lazily imported (the --dry-run
    zero-HTTP doctrine), and it never raises — a dead server is a finding,
    not a traceback.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return 0


def probe_daemon_lock(lock_path=None) -> dict:
    """NON-acquiring single-instance probe → {"path", "running", "pid"}.

    The flock is the truth, the PID content is diagnostics: try
    LOCK_EX|LOCK_NB and release immediately. The lockfile is never created
    (no O_CREAT — a probe must not leave artifacts on a healthy host) and
    never left held, so --find-scanner works WHILE a daemon runs (that is
    its whole point: the running daemon is a finding, not a lock error).
    """
    path = Path(lock_path) if lock_path is not None else _default_lockfile_path()
    result = {"path": path, "running": False, "pid": None}
    try:
        raw = path.read_text().strip()
        result["pid"] = int(raw.splitlines()[0]) if raw else None
    except (OSError, ValueError):
        pass  # absent/unparseable PID content — the flock below decides
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return result  # no lockfile → no daemon
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        result["running"] = True
        return result
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    return result


def probe_grab(node, *, opener=None, ioctl=None, closer=None) -> str:
    """Transient EVIOCGRAB probe of `node` → "free"|"held"|"no-permission"|
    "absent"|"error (…)" — ALWAYS released before returning.

    EBUSY is the only reliable "another process (the running daemon, evtest,
    a remapper) holds the EXCLUSIVE grab" signal, and a held grab means every
    other evdev reader observes NOTHING by kernel design — the plain-words
    finding the coordinator's capture silently lacked. A successful probe
    un-grabs and closes immediately (try/finally): the report never keeps
    what it probed.
    """
    open_ = _open_readonly if opener is None else opener
    io = fcntl.ioctl if ioctl is None else ioctl
    close = os.close if closer is None else closer
    fd = None
    try:
        fd = open_(node)
        try:
            io(fd, EVIOCGRAB, 1)
        except OSError as exc:
            if exc.errno == errno.EBUSY:
                return "held"
            if exc.errno in (errno.EACCES, errno.EPERM):
                return "no-permission"
            if exc.errno in _LOST_ERRNOS:
                return "absent"
            return f"error ({exc})"
        try:
            io(fd, EVIOCGRAB, 0)
        except OSError:
            pass  # the close below releases the grab regardless
        return "free"
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM):
            return "no-permission"
        if exc.errno in _LOST_ERRNOS:
            return "absent"
        return f"error ({exc})"
    finally:
        if fd is not None:
            try:
                close(fd)
            except OSError:
                pass


def inspect_terminals_db(db_path, secret: str | None = None) -> list[dict] | None:
    """READ-ONLY enumeration of the server's terminals table, or None.

    No list-terminal API exists, and every HTTP existence probe can WRITE (a
    successful register creates state), so the server-side identity facts for
    --find-scanner come from the local sqlite file opened with a mode=ro URI
    (never created, never written). Per row: terminal_id, paired (a browser
    token exists), last_seen_at, and secret_matches — whether `secret`'s
    sha256 equals the stored secret_hash (the app.security.token_hash
    algorithm; only the bool leaves this function, never a hash or secret).
    Unreadable (split-host server, permissions, WAL state) → None: the report
    degrades to "UNVERIFIED", it never crashes.
    """
    path = Path(db_path)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT terminal_id, secret_hash, token_hash IS NOT NULL, "
                "last_seen_at FROM terminals").fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return None
    want = hashlib.sha256(secret.encode()).hexdigest() if secret else None
    return [{"terminal_id": tid,
             "paired": bool(paired),
             "last_seen_at": seen,
             "secret_matches": (None if want is None else shash == want)}
            for tid, shash, paired, seen in rows]


def identity_findings(config, offer, hostname,
                      *, terminal_ids=None) -> list[tuple[str, str]]:
    """PURE identity/offer consistency findings → [(marker, text), ...].

    Read-only by construction: no I/O, no network, no DB, no writes, and
    nothing secret in the output (both terminal ids are already printed by
    the report; the secret and the pairing code never are).

    The two call sites share it so they can never drift:
      * ``main()`` at daemon start-up — config + offer + hostname, with
        ``terminal_ids=None`` (no server knowledge at boot: an HTTP round
        trip before the grab would only delay the device).
      * ``find_scanner()`` — the same, plus the ids the local DB knows.

    Stable order (pinned): A offer-vs-config FAIL, C offer-unknown-to-server
    FAIL, D config-vs-host WARN. Every FAIL returned here is OFFER-family and
    find_scanner appends that failure key; the server row-level findings
    (config identity unknown, secret mismatch, FOREIGN IDENTITY,
    already-paired) stay in ``find_scanner``, which owns the facts
    (``paired``/``secret_matches``/``owner``) this helper deliberately lacks.

    Only D is DELIVERY-breaking (the daemon forwards as an identity this host
    does not own); A and C break the pairing paperwork only. A malformed but
    readable offer (a list, ``{}``, a non-string or empty ``terminal_id``)
    still names what it holds — the guard is the pre-refactor one, so those
    shapes keep the findings they always had.
    """
    findings: list[tuple[str, str]] = []
    terminal_id = config.get("terminal_id") if isinstance(config, dict) else None
    offer_tid = offer.get("terminal_id") if isinstance(offer, dict) else None
    # A/C — offer vs config and offer identity vs the server. `offer is None`
    # means the file is absent/unreadable/`null` (find_scanner has its own
    # "no offer file" finding for that, and main() has nothing to say); any
    # PARSED value counts, so a malformed-but-readable offer (a list, {}, a
    # non-string or empty terminal_id) keeps the findings it always had.
    if offer is not None:
        if terminal_id is not None and offer_tid != terminal_id:
            findings.append(("FAIL",
                f"offer file names terminal {offer_tid!r} but the daemon "
                f"config is {terminal_id!r} — the pill would pair an "
                "identity this daemon does not hold"))
        if terminal_ids is not None and not _id_is_known(offer_tid, terminal_ids):
            findings.append(("FAIL",
                f"offer file names {offer_tid!r}, which the server does NOT "
                "know — clicking the pill returns 401 bad_pairing (a stale "
                "offer; --register overwrites it atomically)"))
    # D — the config forwards as an identity this host does not own.
    if _is_foreign_identity(config, hostname):
        findings.append(("WARN",
            f"config identity {terminal_id!r} is NOT this host "
            f"({hostname!r}) — this daemon forwards as an identity this "
            "host does not own, and no browser here is necessarily "
            "paired to it; run scripts/scanner_daemon.py --find-scanner "
            "for the one recovery ladder"))
    return findings


def find_scanner(*, config_path=None, pairing_path=None,
                 base_url: str = "http://127.0.0.1:8000", db_path=None,
                 sysfs_root: str | Path = _SYSFS_INPUT_CLASS, lock_path=None,
                 vid_pid: tuple[int, int] = DEFAULT_VID_PID,
                 opener=None, ioctl=None, closer=None,
                 http_get=None, hostname=None) -> tuple[list[str], int]:
    """The read-only pairing-fallback report → ``(lines, exit_code)``.

    Answers, in order: device present? node grabbed/held by whom? another
    daemon running (plain words, with its PID)? server up? does the config
    terminal exist server-side and which OTHER terminals do? does the config
    secret belong to a FOREIGN terminal? offer file consistent? Ends in ONE
    actionable recovery ladder (exact commands, printed — never executed;
    destructive steps stay the operator's call). Writes nothing, takes no
    lock, keeps no grab. Exit 0 only when the whole delivery chain verifies
    usable (or is contradiction-free with the DB unverifiable), else 1 —
    the loud half of "diagnose loudly, never hide it".
    """
    get = _http_get if http_get is None else http_get
    report: list[str] = []
    failures: list[str] = []

    def say(marker: str, text: str) -> None:
        report.append(f"{marker:<4} {text}")

    report.append("scanner_daemon --find-scanner: READ-ONLY report — nothing "
                  "below was written, locked or kept grabbed")
    vid, pid = vid_pid

    # 1. the daemon's identity file (read-only).
    cfg_path = Path(config_path) if config_path else default_config_file()
    config = load_config(cfg_path)
    terminal_id = secret = None
    if config is None:
        say("FAIL", f"no usable terminal config at {cfg_path} — the daemon has "
                    "no identity; --register mints one (config + offer, 0600)")
        failures.append("no-config")
    else:
        terminal_id, secret = config["terminal_id"], config["secret"]
        say("OK", f"config {cfg_path}: terminal_id={terminal_id!r}, secret "
                  f"present ({len(secret)} chars — never printed)")

    # 2. device + node, and who holds the exclusive grab.
    nodes = discover_event_devices(vid_pid, sysfs_root)
    grab_state = None
    if not nodes:
        say("FAIL", f"scanner {vid:04x}:{pid:04x} NOT ATTACHED — no "
                    "/dev/input/eventN matches in sysfs. Hardware/cable until "
                    "proven otherwise: re-plug, then scripts/usb_host_check.py "
                    "and journalctl -k | grep -E 'error -71|USB disconnect' "
                    "(the -71-then-dropout signature, docs/opticon-hardware.md §5)")
        failures.append("device-absent")
    else:
        say("OK", f"device attached: {', '.join(nodes)}")
        grab_state = probe_grab(nodes[0], opener=opener, ioctl=ioctl,
                                closer=closer)
        if grab_state == "held":
            say("WARN", f"{nodes[0]}: the EXCLUSIVE grab (EVIOCGRAB) is held by "
                        "another process (a running scanner_daemon, evtest, a "
                        "remapper) — every OTHER evdev reader sees NOTHING "
                        "while it is held, by kernel design")
        elif grab_state == "no-permission":
            say("WARN", f"{nodes[0]}: no permission to open/grab — install the "
                        "udev rule (scripts/udev/99-opticon-scanner.rules) or "
                        "join the input group")
        elif grab_state == "free":
            say("OK", f"{nodes[0]}: free — no other process holds the grab "
                      "(the probe released it immediately)")
        else:
            say("WARN", f"{nodes[0]}: grab probe: {grab_state}")

    # 3. the single-instance lock: a running daemon is a FINDING, not an error.
    lock = probe_daemon_lock(lock_path)
    if lock["running"]:
        say("INFO", f"another scanner_daemon IS RUNNING (lockfile {lock['path']}, "
                    f"PID {lock['pid'] if lock['pid'] is not None else '?'}) — it "
                    "holds the single-instance lock and (device attached) the "
                    "exclusive grab; --register refuses until it is stopped")
    else:
        stale = (f" (stale PID {lock['pid']} in the file — the flock is the "
                 "truth, and it is free)") if lock["pid"] is not None else ""
        say("INFO", f"no other daemon holds the lockfile {lock['path']}{stale}")

    # 4. server liveness: an unauthenticated GET /events must answer 401.
    events_url = base_url.rstrip("/") + "/api/scanner/events"
    status = get(events_url)
    if status == 0:
        say("FAIL", f"server UNREACHABLE at {base_url} (GET {events_url}) — "
                    "start it (./scripts/run.sh) or fix --forward")
        failures.append("server-unreachable")
    elif status == 401:
        say("OK", f"server up at {base_url} (GET /api/scanner/events → 401 "
                  "without a browser token, the expected liveness answer)")
    else:
        say("WARN", f"server at {base_url} answered GET /api/scanner/events "
                    f"with {status} (expected 401 without a token)")

    # 5. server-side identity: config vs the terminals table (read-only).
    db = Path(db_path) if db_path is not None else default_db_file()
    terminals = inspect_terminals_db(db, secret)
    mine = owner = None
    identity_verified = False
    if terminals is None:
        say("WARN", f"cannot inspect the server database read-only at {db} "
                    "(split-host server or permissions) — server-side identity "
                    "UNVERIFIED; the HTTP liveness probe is all this host sees")
    else:
        if not terminals:
            say("INFO", f"server database {db}: the terminals table is EMPTY — "
                        "nothing is registered")
        for row in terminals:
            marks = []
            if row["terminal_id"] == terminal_id:
                marks.append("this config's terminal_id")
            if row["secret_matches"]:
                marks.append("owns THIS config's secret")
            marks.append("paired: a browser holds the read token" if row["paired"]
                         else "UNPAIRED: no browser can read its queue")
            say("INFO", f"server terminal {row['terminal_id']!r} (last_seen "
                        f"{row['last_seen_at'] or 'never'}) ← {'; '.join(marks)}")
        mine = next((r for r in terminals
                     if r["terminal_id"] == terminal_id), None)
        owner = next((r for r in terminals if r["secret_matches"]), None)
        if terminal_id is not None:
            if mine is None:
                say("FAIL", f"config terminal {terminal_id!r} is UNKNOWN to the "
                            "server — every forward would 401 and the pill would "
                            "answer 401 bad_pairing")
                failures.append("identity")
            elif mine["secret_matches"] is False:
                say("FAIL", f"config terminal {terminal_id!r} exists but its "
                            "server-side secret does NOT match this config — "
                            "forwards would 401")
                failures.append("identity")
            else:
                identity_verified = True
                if mine["paired"]:
                    say("OK", f"identity verified: {terminal_id!r} exists, the "
                              "secret matches, and a read token is minted")
                    say("WARN", "it is already PAIRED: the read token went to "
                                "WHICHEVER browser paired first — if that was "
                                "another host (a decommissioned laptop), no "
                                "browser HERE can read this queue; recovery is "
                                "the ladder below (DELETE + re-register + pair "
                                "from THIS host)")
                else:
                    say("WARN", f"identity verified but {terminal_id!r} is "
                                "UNPAIRED — the kiosk pill must complete pairing "
                                "(offer file checked below)")
            if owner is not None and owner["terminal_id"] != terminal_id:
                say("FAIL", f"FOREIGN IDENTITY: this config's secret belongs to "
                            f"terminal {owner['terminal_id']!r} — scans would "
                            "land in a queue only ITS paired browser can read "
                            "(a config copied from another host?)")
                failures.append("identity")

    # 6. the one-click offer file vs config vs server.
    offer_path = Path(pairing_path) if pairing_path else default_pairing_file()
    offer = load_pairing_file(offer_path)   # same tolerance, never prints it
    offer_tid = offer.get("terminal_id") if isinstance(offer, dict) else None
    offer_row = None
    # 6a. host-vs-config and offer-vs-config: the PURE helper main() also
    # runs at start-up, so the two surfaces can never drift. Row-level
    # findings (config unknown, secret/owner/paired) stay in section 5:
    # they need facts terminal_ids deliberately does not carry.
    host = socket.gethostname() if hostname is None else hostname
    for marker, text in identity_findings(
            config, offer, host,
            terminal_ids=None if terminals is None
            else {r["terminal_id"] for r in terminals}):
        say(marker, text)
        if marker == "FAIL":
            failures.append("offer")   # every helper FAIL is offer-family
    if offer is None:
        if identity_verified and mine is not None and not mine["paired"]:
            say("FAIL", f"no offer file at {offer_path} and {terminal_id!r} is "
                        "unpaired — the pill has nothing to consume; re-run "
                        "with --register to mint a fresh single-use code")
            failures.append("offer")
        else:
            say("INFO", f"no offer file at {offer_path} — only the one-click "
                        "pill of an UNPAIRED terminal needs it (--register "
                        "writes it)")
    elif terminals is not None:
        offer_row = next((r for r in terminals
                          if r["terminal_id"] == offer_tid), None)
        if offer_row is not None:   # an unknown one already FAILed in 6a
            if offer_row["paired"]:
                say("WARN", f"offer file for {offer_tid!r} is STALE — that "
                            "terminal is already paired; the pill would answer "
                            "409 already_paired")
            else:
                say("OK", f"offer file consistent: {offer_tid!r} is registered "
                          "and unpaired — the pill will mint the browser token")
    else:
        say("WARN", "offer file present, but its server-side consistency is "
                    "UNVERIFIED (no readable local DB)")

    # 7. verdict + exactly ONE recovery ladder.
    if not failures:
        say("OK", "verdict: the delivery chain is consistent — if scans still "
                  "do not arrive, the DEVICE is silent: capture with "
                  "scripts/scanner_hid.py (it distinguishes no-node / vanished "
                  "/ attached-but-silent)")
        return report, 0

    say("FAIL", f"verdict: scans CANNOT reliably reach a browser on this host "
                f"({len(failures)} blocking finding(s): "
                f"{', '.join(dict.fromkeys(failures))})")
    identity_family = {"identity", "offer", "no-config"} & set(failures)
    delete_target = None
    if terminals and identity_family:
        delete_target = (owner or mine or offer_row
                         or next(iter(terminals)))["terminal_id"]
    plan = []
    if lock["running"] and identity_family:
        plan.append("stop the daemon that holds the stale identity (it also "
                    "holds the lock and the exclusive grab): kill "
                    + (str(lock["pid"]) if lock["pid"] is not None
                       else f"$(cat {lock['path']})"))
    if "server-unreachable" in failures:
        plan.append(f"start the server: ./scripts/run.sh  (then re-check: "
                    f"curl -s -o /dev/null -w '%{{http_code}}' {events_url} "
                    f"→ expect 401)")
    if delete_target is not None:
        plan.append("delete the stale server-side terminal — VERIFY FIRST that "
                    f"it belongs to a decommissioned host: curl -X DELETE "
                    f"{base_url.rstrip('/')}/api/scanner/terminals/{delete_target}")
    if identity_family and config is not None:
        plan.append(f"move the stale config aside: mv {cfg_path} {cfg_path}.old-host")
    if identity_family:
        plan.append("re-register (writes config + one-click offer, both 0600): "
                    f"uv run python scripts/scanner_daemon.py --config {cfg_path} "
                    f"--register --forward {base_url.rstrip('/')}")
        plan.append("in the kiosk browser ON THIS HOST, click the 'no scanner' "
                    "pill — that mints THIS host's read token")
    if "device-absent" in failures:
        plan.append("hardware: the scanner is off the bus — re-plug it (a "
                    "known-good data cable, another jack), then run "
                    "scripts/usb_host_check.py and inspect journalctl -k for "
                    "the -71/dropout signature")
    report.append("")
    report.append("recovery plan (ONE ladder — printed for the operator, "
                  "NOTHING here is executed):")
    report.extend(f"  {i}. {step}" for i, step in enumerate(plan, 1))
    return report, 1


def _registration_recovery(exc: "PostError", terminal_id: str | None,
                           base_url: str, config_path) -> list[str]:
    """ONE actionable ladder for a failed registration (never a raw dump).

    The first line is the historical loud one ("registration failed: POST
    failed (…)" — pinned by tests); what follows is the single recovery plan
    for that status, with the exact operator commands (printed, never
    executed). 409 already_registered / secret_in_use, 401, 403
    loopback-only and transport failures each get their own ladder; anything
    else points at --find-scanner.
    """
    base = base_url.rstrip("/")
    text = str(exc)
    diagnose = ("diagnose read-only first: uv run python "
                "scripts/scanner_daemon.py --find-scanner")
    delete_cmd = (f"curl -X DELETE {base}/api/scanner/terminals/{terminal_id}"
                  if terminal_id else "curl -X DELETE "
                  f"{base}/api/scanner/terminals/<terminal-id>")
    register_cmd = ("re-run with --register: uv run python "
                    f"scripts/scanner_daemon.py --config {config_path} "
                    f"--register --forward {base}")
    pill = ("in the kiosk browser ON THIS HOST, click the 'no scanner' pill "
            "— that mints this host's read token")
    if exc.status == 409 and "machine_key_in_use" in text:
        steps = [diagnose,
                 "this laptop's machine key is already registered to another "
                 "terminal — press the pill again (the popup's reclaim reuses "
                 "it automatically), or delete the stale registration — "
                 f"VERIFY FIRST: {delete_cmd}",
                 register_cmd]
    elif exc.status == 409 and "secret_in_use" in text:
        steps = [diagnose,
                 "this config's secret belongs to ANOTHER terminal (a copied "
                 f"config) — move it aside: mv {config_path} {config_path}.old-host",
                 register_cmd,
                 pill]
    elif exc.status == 409:
        steps = [diagnose,
                 f"the server already knows terminal {terminal_id!r}. If THIS "
                 "config's secret still matches it, simply drop --register "
                 "(the daemon reuses the stored identity).",
                 "if it is a stale/foreign registration (e.g. an old host), "
                 f"delete it — VERIFY FIRST: {delete_cmd}",
                 register_cmd,
                 pill]
    elif exc.status == 401:
        steps = [diagnose,
                 "the server rejected the credentials (terminal deleted "
                 "server-side or secret rotated).",
                 f"delete any leftover registration: {delete_cmd}",
                 register_cmd,
                 pill]
    elif exc.status == 403 or "loopback_only" in text:
        steps = ["registration is loopback-gated: run this ON the server host, "
                 "or (trusted-LAN deployments only) start the server with "
                 "HCRM_SCANNER_REGISTER_ANY=1"]
    elif exc.status == 0:
        steps = [f"the server at {base_url} is unreachable — start it "
                 "(./scripts/run.sh) or fix --forward, then re-run with "
                 "--register (nothing was written; save_config only persists "
                 "a server-ACCEPTED credential)"]
    else:
        steps = [diagnose,
                 f"unexpected server answer ({text}) — check the server log"]
    return ([f"scanner_daemon: registration failed: {exc}",
             "scanner_daemon: recovery plan (ONE ladder — nothing is "
             "executed for you):"]
            + [f"  {i}. {step}" for i, step in enumerate(steps, 1)])


class _Stopped(Exception):
    """Raised by the SIGTERM handler: unwinds the with-blocks (fd closes →
    the grab auto-releases) for a clean exit 0."""


def _stop_handler(signum, frame):  # pragma: no cover — signal plumbing
    raise _Stopped()


def _forward_events_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/api/scanner/events"


def _port_number(text: str) -> int:
    try:
        port = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be in 0..65535")
    return port


def _nonnegative_seconds(text: str) -> float:
    """argparse type for --register-wait: a finite, non-negative float."""
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be finite and >= 0")
    return value


def main(argv=None, *, post=None, sleep=None, opener=None, ioctl=None,
         closer=None, scan_reader=None, lock_path=None,
         sysfs_root=None, clock=None, db_path=None, http_get=None,
         hostname=None, device=None, machine_id_path="/etc/machine-id") -> int:
    """The daemon entrypoint (round 4, T5).

    Modes:
      scanner_daemon --config PATH [--register] [--forward URL]
        [--register-wait SECONDS] [--register-only]
        Register (first run, or ``--register``): POST the terminal, persist
        the 0600 config, and write the one-click pairing offer file (0600,
        atomic) that the server's POST /api/scanner/pair/auto reads — the
        kiosk browser pairs by clicking the "no scanner" pill, no code to
        type. The code itself is NOT printed by default (it must never
        touch a journal); ``--print-code`` opts in for the manual
        POST /api/scanner/pair fallback. A failed offer-file write is
        FATAL: the code would be unrecoverable. Then run.
        Run: lock → grab (T3) → decode (T2) → forward each scan to
        POST /api/scanner/events, spooled and retried so a server outage
        never stops the grab and never drops a scan. Before the grab, a
        start-up identity check names a config identity that is not this host
        (WARN) and an offer file that disagrees with the config
        (FAIL). ``--require-host-identity`` makes the HOST mismatch fatal
        (exit 1) before register/grab; offer-file drift stays loud but is
        never fatal — a stale paperwork file must not take a healthy kiosk
        down.
      scanner_daemon --dry-run [--node N]
        Decode and print (redacted), ZERO HTTP — the hardware-checklist
        mode; no config, no lock, no network import.
      scanner_daemon --find-scanner [--config PATH] [--forward URL]
        The read-only pairing-fallback report (see find_scanner): the
        three-way identity check (config vs server DB vs offer file), the
        running-daemon/held-grab findings in plain words, and exactly ONE
        recovery ladder of printed operator commands. Never takes the lock,
        writes nothing; exit 0 iff the delivery chain verifies usable.

    Everything device/network-touching is injectable (``post``, the grab
    triple, ``scan_reader``, ``sleep``, ``lock_path``) so the test suite
    runs with no hardware and no network. SIGTERM/SIGINT stop the loop,
    close the fd (the grab auto-releases) and exit 0.
    """
    parser = argparse.ArgumentParser(
        description="Grab the Opticon M-10's evdev node (stop it typing "
                    "into windows) and forward its scans to the HCRM "
                    "scanner queue.")
    parser.add_argument("--config", metavar="PATH",
                        help="terminal config JSON {terminal_id, secret} "
                             "(written 0600 on first register)")
    parser.add_argument("--register", action="store_true",
                        help="register the terminal (first run, or after a "
                             "server-side DELETE) and write the one-click "
                             "pairing file")
    parser.add_argument("--forward", metavar="URL", default="http://127.0.0.1:8000",
                        help="HCRM base URL (default http://127.0.0.1:8000)")
    parser.add_argument("--dry-run", action="store_true",
                        help="decode and print scans, zero HTTP, no config")
    parser.add_argument("--terminal-id", metavar="ID", default=None,
                        help="terminal id at register time (default: hostname)")
    parser.add_argument("--node", metavar="NODE", default=None,
                        help="explicit event node for the FIRST grab "
                             "(re-discovery is always by VID/PID)")
    parser.add_argument("--spool", metavar="PATH", default=None,
                        help="delivery backlog file (default: <config>.spool)")
    parser.add_argument("--pairing-file", metavar="PATH", default=None,
                        help="one-click pairing offer file (default: "
                             "$HCRM_SCANNER_PAIRING_FILE or <repo>/data/"
                             "scanner-pairing.json — the SAME path the "
                             "server reads; override on BOTH sides if set)")
    parser.add_argument("--print-code", action="store_true",
                        help="also print the pairing code — the manual "
                             "POST /api/scanner/pair fallback for split-"
                             "host deployments; off by default so the "
                             "code never touches a journal or scrollback")
    parser.add_argument("--register-wait", metavar="SECONDS",
                        type=_nonnegative_seconds, default=0.0,
                        help="retry TRANSIENT registration failures for up to "
                             "SECONDS (0 = one attempt: today's behaviour)")
    parser.add_argument("--register-only", action="store_true",
                        help="register, write the config + the one-click offer "
                             "file, then exit 0 without grabbing a device")
    parser.add_argument("--find-scanner", action="store_true",
                        help="read-only pairing-fallback report: device/node, "
                             "who holds the exclusive grab, whether another "
                             "daemon is running (PID), config vs server vs "
                             "offer-file identity, and ONE recovery plan with "
                             "exact commands. Writes nothing, never takes the "
                             "lock; exit 0 only when the chain verifies usable")
    parser.add_argument("--require-host-identity", action="store_true",
                        help="make the start-up identity check fatal when the "
                             "config's terminal_id is not this host: exit 1 "
                             "before registering or grabbing (default: warn "
                             "and keep running). One-click offer-file drift "
                             "is reported but never fatal, gate or not. Also "
                             "HCRM_SCANNER_REQUIRE_HOST_IDENTITY=1")
    parser.add_argument("--local-port", type=_port_number, default=None,
                        help="serve the loopback popup on 127.0.0.1:PORT "
                             "(0 = ephemeral; off unless this or --site-origin "
                             "is given; portable default 47631)")
    parser.add_argument("--site-origin", metavar="ORIGIN",
                        help="site origin allowed to receive the popup offer "
                             "(scheme://host[:port]); also enables portable "
                             "popup serving")
    parser.add_argument("--server", metavar="URL", default=None,
                        help="portable registration/pairing/forward target "
                             "(default: --forward); one base_url source of truth")
    parser.add_argument("--invite", metavar="CODE", default=None,
                        help="staff-minted registration invite (portable path); "
                             "defaults the config to the XDG path; never logged")
    args = parser.parse_args(argv)
    if args.invite and not args.site_origin:
        parser.error("--site-origin is required with --invite")
    if args.site_origin and _origin_of(args.site_origin) is None:
        parser.error("--site-origin must be scheme://host[:port]")
    base_url = args.server or args.forward
    post_fn = post or _http_post
    nap = sleep if sleep is not None else time.sleep
    tick = clock if clock is not None else time.monotonic
    grab_kwargs = {"node": args.node, "opener": opener, "ioctl": ioctl,
                   "closer": closer, "scan_reader": scan_reader or read_scans,
                   "sleep": nap}
    if sysfs_root is not None:
        grab_kwargs["sysfs_root"] = sysfs_root

    previous = {}
    installed = []
    stop_event = threading.Event()
    local_server = None
    offers = None
    try:
        # Signals: raise _Stopped from anywhere (including a blocking read)
        # so the with-blocks unwind and the fd closes. Installed in BOTH
        # modes — a dry-run must stop as cleanly as the real daemon.
        for signum, handler in ((signal.SIGTERM, _stop_handler),):
            try:
                previous[signum] = signal.signal(signum, handler)
                installed.append(signum)
            except ValueError:  # not the main thread (embedded use)
                pass

        if args.dry_run:
            # Zero HTTP by construction: no post is built, no config is
            # read, and every decoded scan prints through redact_code.
            try:
                run_loop(lambda code: print(redact_code(code), flush=True),
                         **grab_kwargs)
            except (_Stopped, KeyboardInterrupt):
                pass
            return 0

        if args.find_scanner:
            # BEFORE the lock and the --config requirement on purpose: the
            # whole point is diagnosing a host where a daemon already holds
            # the lock and the grab. Read-only; destructive steps are PRINTED.
            find_kwargs = {"config_path": args.config or default_config_file(),
                           "pairing_path": args.pairing_file or default_pairing_file(),
                           "base_url": args.forward,
                           "db_path": db_path,
                           "lock_path": lock_path,
                           "opener": opener, "ioctl": ioctl, "closer": closer,
                           "http_get": http_get}
            if sysfs_root is not None:
                find_kwargs["sysfs_root"] = sysfs_root
            lines, code = find_scanner(**find_kwargs)
            for line in lines:
                print(line, flush=True)
            return code

        if not args.config and args.invite:
            args.config = str(portable_config_file())   # R6: XDG default, portable only
        if not args.config:
            parser.error("--config is required (or pass --dry-run / "
                         "--find-scanner / --register --invite)")

        terminal_hint = args.terminal_id
        with DaemonLock(lock_path):
            config = load_config(args.config)
            if config is None:
                terminal_id = args.terminal_id or (
                    hostname if hostname is not None else socket.gethostname())
                secret = secrets.token_urlsafe(32)
            else:
                terminal_id, secret = config["terminal_id"], config["secret"]
            terminal_hint = terminal_id
            # C2: the loud bring-up. A config copied from another host makes
            # the daemon forward under THAT host's identity while the server
            # answers 200 — the silent half of the 2026-11-17 incident. Warn
            # (or refuse, with --require-host-identity) before register/grab.
            # Suppressed only when an explicit --terminal-id AGREES with the
            # loaded config identity (a differing one cannot change what is
            # forwarded, so it must not silence the check).
            if config is not None and not args.register \
                    and not args.register_only \
                    and args.terminal_id != terminal_id:
                offer_path = (Path(args.pairing_file) if args.pairing_file
                              else default_pairing_file())
                host = (hostname if hostname is not None
                        else socket.gethostname())
                findings = identity_findings(
                    config, load_pairing_file(offer_path), host)
                for marker, text in findings:
                    _log(f"{marker}: {text}")
                # The gate is DELIVERY-breaking only: a config that forwards
                # as another host's identity refuses to run when asked to; a
                # stale one-click offer is pairing paperwork and must never
                # take a healthy kiosk down (it is still reported above).
                if _is_foreign_identity(config, host) and (
                        args.require_host_identity
                        or _env_flag(_REQUIRE_HOST_IDENTITY_ENV)):
                    _log(_REFUSE_HOST_IDENTITY)
                    return 1
            if args.register or config is None:
                portable = args.invite is not None
                machine_key = (derive_machine_key(
                    config, machine_id_path=machine_id_path)
                    if portable else None)
                try:
                    code = register_with_retry(
                        post_fn, base_url, terminal_id, secret,
                        invite=args.invite, machine_key=machine_key,
                        wait=args.register_wait, sleep=nap,
                        clock=tick, log=_log)
                except (_Stopped, KeyboardInterrupt):
                    # Stopped while waiting out the retry budget: a clean
                    # exit (fd/grab untouched — registration precedes both).
                    return 0
                if portable:
                    # R3: the offer lives BESIDE the XDG config, never in the
                    # repo data dir; OfferStore.put persists code+expiry atomically.
                    save_config(args.config, terminal_id, secret,
                                machine_key=machine_key, server_url=base_url,
                                site_origin=_origin_of(args.site_origin))
                    pairing_path = (Path(args.pairing_file) if args.pairing_file
                                    else Path(args.config).with_name(
                                        "scanner-pairing.json"))
                else:
                    save_config(args.config, terminal_id, secret)
                    pairing_path = args.pairing_file or default_pairing_file()
                # Write-then-announce, and FATAL on failure: with the code
                # no longer printed by default, the offer file is its only
                # copy — losing it silently would strand the registration.
                try:
                    if portable:
                        offers = OfferStore(pairing_path, terminal_id, clock=tick)
                        offers.put(code)
                    else:
                        save_pairing_file(pairing_path, terminal_id, code)
                except OSError as exc:
                    print(f"scanner_daemon: could not write the pairing "
                          f"file {pairing_path}: {exc}\n"
                          f"  The single-use code is unrecoverable — "
                          f"recovery: DELETE /api/scanner/terminals/"
                          f"{terminal_id} (staff or loopback), then re-run "
                          f"with --register", file=sys.stderr)
                    return 1
                msg = (f"pair this terminal: pairing file written to "
                       f"{pairing_path} (0600) — in the kiosk browser, "
                       f"click the 'no scanner' pill in the header")
                if args.print_code:
                    msg += f" (pairing code: {code})"
                print(msg, flush=True)
                _log(f"registered terminal {terminal_id}")

            if args.register_only:
                _log("registration complete (--register-only) — not grabbing")
                return 0

            # R2: local serving is opt-in, so every legacy invocation stays off.
            site_origin = (args.site_origin
                           or (config or {}).get("site_origin"))
            env_port = os.environ.get("HCRM_SCANNER_LOCAL_PORT")
            local_port = (args.local_port if args.local_port is not None
                          else _port_number(env_port) if env_port
                          else 47631 if site_origin else None)
            if local_port is not None:
                runtime_config = dict(config or {})
                runtime_config.update({
                    "terminal_id": terminal_id,
                    "secret": secret,
                    "server_url": (getattr(args, "server", None)
                                   or runtime_config.get("server_url")
                                   or args.forward),
                    "site_origin": (site_origin
                                    or runtime_config.get("site_origin")),
                })
                if args.site_origin:
                    offer_path = (Path(args.pairing_file) if args.pairing_file
                                  else Path(args.config).with_name(
                                      "scanner-pairing.json"))
                else:
                    offer_path = (Path(args.pairing_file) if args.pairing_file
                                  else default_pairing_file())
                local_server = LocalAgentServer(
                    local_port, config=runtime_config,
                    offers=offers if offers is not None else OfferStore(
                        offer_path, terminal_id, clock=tick),
                    nonces=NonceStore(clock=tick),
                    device=device or _device_not_configured,
                    post=post_fn, stop_event=stop_event, log=_log)
                local_server.start()
                grab_kwargs["stop"] = stop_event.is_set

            spool = Spool(args.spool or (str(args.config) + ".spool"),
                          log=_log)
            forwarder = Forwarder(post_fn, _forward_events_url(base_url),
                                  secret, spool, log=_log)
            if spool.pending():
                _log(f"replaying {len(spool.pending())} spooled scan(s)")
            forwarder.drain()  # a restart mid-outage replays the backlog
            try:
                run_loop(forwarder.handle, **grab_kwargs)
            except (_Stopped, KeyboardInterrupt):
                pass
            finally:
                stop_event.set()
                # mimo adds Heartbeater.join(2) here, before server_close.
                if local_server is not None:
                    local_server.close()
            forwarder.drain()  # best-effort: flush what a stop left behind
        return 0
    except GrabError as exc:
        # The loud operator problems (EBUSY/EACCES/wrong device/409): one
        # clear line, exit non-zero — never a traceback, never a wedge.
        print(f"scanner_daemon: {exc}", file=sys.stderr)
        if isinstance(exc, InstanceAlreadyRunning):
            # Plain words for the trap the 2026-11-17 incident set: the
            # holder ALSO has the exclusive grab, diagnosis still works, and
            # re-registering needs it stopped first.
            held = probe_daemon_lock(lock_path)
            who = f" (PID {held['pid']})" if held["pid"] is not None else ""
            print("scanner_daemon: the running daemon holds the single-instance "
                  f"lock AND the EXCLUSIVE evdev grab{who} — diagnosis still "
                  "works: re-run with --find-scanner (it never takes the "
                  "lock). To re-register, stop it first: kill "
                  + (str(held["pid"]) if held["pid"] is not None
                     else f"$(cat {held['path']})"), file=sys.stderr)
        return 1
    except PostError as exc:
        for line in _registration_recovery(
                exc, terminal_hint or socket.gethostname(), base_url,
                args.config):
            print(line, file=sys.stderr)
        return 1
    finally:
        for signum in installed:
            signal.signal(signum, previous[signum])


if __name__ == "__main__":
    sys.exit(main())
