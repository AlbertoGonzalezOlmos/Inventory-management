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
import json
import os
import secrets
import signal
import socket
import struct
import sys
import tempfile
import time
from pathlib import Path

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


def save_config(path, terminal_id: str, secret: str) -> None:
    """Write the config 0600 — it holds a write credential for the API."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps({"terminal_id": terminal_id,
                                 "secret": secret}, indent=2).encode())
    finally:
        os.close(fd)


def register_terminal(post, base_url: str, terminal_id: str, secret: str) -> str:
    """POST /api/scanner/terminals → the single-use pairing code.

    Raises PostError (409 already-registered etc.): main turns that into a
    loud exit with the server's own recovery message.
    """
    body = post(base_url.rstrip("/") + "/api/scanner/terminals", {},
                {"terminal_id": terminal_id, "secret": secret})
    return body["pairing_code"]


class _Stopped(Exception):
    """Raised by the SIGTERM handler: unwinds the with-blocks (fd closes →
    the grab auto-releases) for a clean exit 0."""


def _stop_handler(signum, frame):  # pragma: no cover — signal plumbing
    raise _Stopped()


def _forward_events_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/api/scanner/events"


def main(argv=None, *, post=None, sleep=None, opener=None, ioctl=None,
         closer=None, scan_reader=None, lock_path=None,
         sysfs_root=None) -> int:
    """The daemon entrypoint (round 4, T5).

    Modes:
      scanner_daemon --config PATH [--register] [--forward URL]
        Register (first run, or ``--register``): POST the terminal, print
        the single-use pairing code, persist the 0600 config — then run.
        Run: lock → grab (T3) → decode (T2) → forward each scan to
        POST /api/scanner/events, spooled and retried so a server outage
        never stops the grab and never drops a scan.
      scanner_daemon --dry-run [--node N]
        Decode and print (redacted), ZERO HTTP — the hardware-checklist
        mode; no config, no lock, no network import.

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
                             "server-side DELETE) and print the pairing code")
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
    args = parser.parse_args(argv)
    post_fn = post or _http_post
    nap = sleep if sleep is not None else time.sleep
    grab_kwargs = {"node": args.node, "opener": opener, "ioctl": ioctl,
                   "closer": closer, "scan_reader": scan_reader or read_scans,
                   "sleep": nap}
    if sysfs_root is not None:
        grab_kwargs["sysfs_root"] = sysfs_root

    previous = {}
    installed = []
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

        if not args.config:
            parser.error("--config is required (or pass --dry-run)")

        with DaemonLock(lock_path):
            config = load_config(args.config)
            if config is None:
                terminal_id = args.terminal_id or socket.gethostname()
                secret = secrets.token_urlsafe(32)
            else:
                terminal_id, secret = config["terminal_id"], config["secret"]
            if args.register or config is None:
                code = register_terminal(post_fn, args.forward,
                                         terminal_id, secret)
                print(f"pair this terminal: enter {code} on the login screen",
                      flush=True)
                save_config(args.config, terminal_id, secret)
                _log(f"registered terminal {terminal_id}")

            spool = Spool(args.spool or (str(args.config) + ".spool"),
                          log=_log)
            forwarder = Forwarder(post_fn, _forward_events_url(args.forward),
                                  secret, spool, log=_log)
            if spool.pending():
                _log(f"replaying {len(spool.pending())} spooled scan(s)")
            forwarder.drain()  # a restart mid-outage replays the backlog
            try:
                run_loop(forwarder.handle, **grab_kwargs)
            except (_Stopped, KeyboardInterrupt):
                pass
            forwarder.drain()  # best-effort: flush what a stop left behind
        return 0
    except GrabError as exc:
        # The loud operator problems (EBUSY/EACCES/wrong device/409): one
        # clear line, exit non-zero — never a traceback, never a wedge.
        print(f"scanner_daemon: {exc}", file=sys.stderr)
        return 1
    except PostError as exc:
        print(f"scanner_daemon: registration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        for signum in installed:
            signal.signal(signum, previous[signum])


if __name__ == "__main__":
    sys.exit(main())
