#!/usr/bin/env python3
"""scanner_hid — receive barcodes from a USB-HID (keyboard-mode) scanner.

For Opticon scanners that enumerate as a HID keyboard instead of a virtual
COM port — by default VID 065A, PID A001, bus-reported name "Opticon USB
Barcode Reader". That PID is what this project's dev machine reports; its
identity as the M-10's USB-HID personality is the leading hypothesis but is
NOT documented by Opticon (see the personality table and its provenance in
docs/opticon-hardware.md). Keyboard-mode scanners "type" each scan followed
by Enter; this tool reads the raw HID keyboard reports directly (no window
focus needed) and emits one line per barcode:

    2026-09-29 10:12:03,HID-Keyboard,8412345678905

i.e. `timestamp,symbology,barcode` with no header — the shape the repo's scan
pipelines consume. `--json` emits a `{device_id, count, scans[]}` document
instead. Which Opticon personalities exist, and which tool receives from
each, is answered by `uv run python scripts/opticon_detect.py`.

Backends (auto-detected, or forced with --backend):

* ``hidraw``  — Linux/WSL2. Stdlib only: reads /dev/hidraw* and matches the
  device by VID/PID in sysfs. On WSL2 the scanner must first be attached from
  Windows with usbipd-win (docs/opticon-hardware.md §2); the stock WSL2
  kernel has CONFIG_HIDRAW=y, so /dev/hidrawN appears as soon as it is.
* ``hidapi``  — Windows/macOS. Needs the `hid` package at runtime:
      uv run --with hid python scripts/scanner_hid.py
  On Windows this claims the scanner from the HID class driver — while it
  runs, scans no longer reach the focused window (that is the point: the
  tool owns the stream).

HID boot-keyboard report parsing is stdlib and offline-tested
(tests/test_scanner_hid.py): standard 8-byte reports
[modifier, reserved, k1..k6] with the HID usage table for letters/digits/
punctuation, plus shift handling and rollover tolerance.

What one hidraw frame actually is gets DISCOVERED, not assumed: hidraw
prefixes the report number (`0x00` when the device uses unnumbered reports,
so a boot frame arrives as 9 bytes) but some kernel/back-end combinations hand
back the 8 payload bytes alone. Guessing wrong is silent — the first keycode
AND the Enter terminator shift out of the parsed slots, so no scan is ever
emitted — which is why `boot_report()` accepts both and the first frame's
shape is announced on stderr.

The tool only READS: it opens the node `O_RDONLY`, writes no file, and never
touches `./data`. Failure modes are separate answers, not one silent hang
(`ui_check` doctrine: "a gate that cannot run must fail"):

| exit | meaning |
|------|---------|
| 0 | the requested `--count` was captured (or Ctrl-C) |
| 2 | usage (argparse) |
| 3 | cannot receive: no node, the node vanished mid-capture, or permission |
| 4 | the node is open and readable but the DEVICE transmitted nothing |

A running `scanner_daemon` holds the scanner's evdev node exclusively
(EVIOCGRAB) but not hidraw — hidraw sits below the input layer, so this tool
still receives while the daemon runs. It says so, and it never takes the
daemon's lockfile: it reports, it does not repair.
"""

from __future__ import annotations

import argparse
import csv
import glob
import io
import json
import os
import re
import select
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_VID_PID = (0x065A, 0xA001)  # Opticon USB Barcode Reader (HID mode)

#: Exit codes (see the module docstring). 3 = "cannot run", 4 = "ran, heard
#: nothing" — never 0 for either, so a wrapper script can tell them apart.
EXIT_OK = 0
EXIT_CANNOT_RUN = 3
EXIT_SILENT = 4

#: How long to wait for a frame before declaring the device silent.
SILENCE_TIMEOUT_DEFAULT = 60.0

# HID usage id -> (unshifted, shifted) for the printable keyboard page (0x07).
# Boot-protocol reports use this page for every key a barcode scanner emits.
# Usages 30..39 are the digits '1'..'9','0' (shifted: '!'..'(' , ')').
# Usage 40 is Enter (terminates a barcode; no printable text).
_USAGE_MAP = {
    **{i: (chr(ord("a") + i - 4), chr(ord("A") + i - 4)) for i in range(4, 30)},
    **{i: (str(i - 29), ")!@#$%^&*("[i - 29]) for i in range(30, 39)},
    39: ("0", ")"),
    44: (" ", " "), 45: ("-", "_"), 46: ("=", "+"),
    47: ("[", "{"), 48: ("]", "}"), 49: ("\\", "|"),
    51: (";", ":"), 52: ("'", '"'), 53: ("`", "~"), 54: (",", "<"),
    55: (".", ">"), 56: ("/", "?"),
}
_USAGE_ENTER = 40  # carriage return terminates a barcode

LEFT_SHIFT, RIGHT_SHIFT = 0x02, 0x20


def parse_keyboard_report(report: bytes) -> tuple[str, bool]:
    """Decode one HID boot-keyboard input report.

    Returns (text, enter_pressed). `text` holds the characters the report
    adds (usually 0 or 1 — scanners report one key per report). ErrorRollOver
    (usage 0x01 in all key slots) yields nothing. Multi-key reports are
    concatenated in slot order.
    """
    if len(report) < 3:
        return "", False
    modifier = report[0]
    keys = report[2:8] if len(report) >= 8 else report[2:]
    keys = [k for k in keys if k not in (0x00, 0x01)]  # no event / rollover
    shifted = bool(modifier & (LEFT_SHIFT | RIGHT_SHIFT))
    text = "".join(
        pair[1] if shifted else pair[0]
        for pair in (_USAGE_MAP.get(k) for k in keys)
        if pair is not None
    )
    return text, _USAGE_ENTER in keys


def boot_report(frame: bytes) -> bytes:
    """One hidraw/hidapi frame → the 8-byte boot-keyboard report.

    hidraw prefixes the report number (``0x00`` when the device uses
    unnumbered reports), so a boot-keyboard frame arrives as 9 bytes; some
    kernel/back-end combinations hand back the 8 payload bytes alone, and
    hidapi differs per backend on unnumbered reports. Discover the shape
    instead of assuming it, because guessing wrong is SILENT: dropping a
    byte that is really the modifier shifts the key slots, so the first
    keycode AND the Enter terminator fall out of the parsed range and no scan
    is ever emitted (that is exactly the "enumerated but no scans" symptom).

    The two shapes are unambiguous by length: a boot keyboard's input report
    is 8 payload bytes, and hidraw adds exactly one report-ID byte.
    """
    if len(frame) == 9 and frame[0] == 0x00:
        return frame[1:]
    return frame


def frame_shape_label(frame: bytes) -> str:
    """Describe a frame's shape for the one-time stderr announcement."""
    if len(frame) == 9 and frame[0] == 0x00:
        return f"{len(frame)} bytes: report ID 0x00 + 8-byte boot report"
    if len(frame) == 8:
        return f"{len(frame)} bytes: bare boot report (no report-ID prefix)"
    return f"{len(frame)} bytes: unexpected shape (parsed as a bare report)"


class ScanAssembler:
    """Accumulate boot-keyboard reports into Enter-terminated barcodes.

    This is the single implementation behind both the pure test helper
    (``group_scans_from_reports``) and the two live backends, so the logic the
    tests exercise is the logic that actually runs. It used to be written out
    three times — once per loop and once in the helper — which is how the
    combined report bug below survived the tests.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, report: bytes) -> str | None:
        """Add one report; return a completed barcode, or None.

        Text is appended *before* the terminator is handled: a scanner can
        pack the final character and its CR suffix into the same boot-keyboard
        report, and dropping it loses the last character of the barcode. An
        Enter-only report with an empty buffer emits nothing.
        """
        text, enter = parse_keyboard_report(report)
        self._buffer += text
        if not enter:
            return None
        scan, self._buffer = self._buffer, ""
        return scan or None


def group_scans_from_reports(reports: list[bytes]) -> list[str]:
    """Feed a sequence of reports; return the complete (Enter-terminated)
    payloads. Kept as a pure function so tests need no device."""
    assembler = ScanAssembler()
    out: list[str] = []
    for report in reports:
        scan = assembler.feed(report)
        if scan is not None:
            out.append(scan)
    return out


def format_csv_row(when: str, text: str) -> str:
    """One CSV record ``timestamp,symbology,barcode``, quoted only as needed.

    Uses ``csv.writer`` rather than f-string interpolation so a barcode
    containing a comma, quote or newline stays one field: the HID usage map
    includes ',' (usage 54) and Code 128 can carry the printable ASCII set.
    """
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow([when, "HID-Keyboard", text])
    return buf.getvalue().rstrip("\n")


# --------------------------------------------------------------------------
# hidraw backend (Linux / WSL2)
# --------------------------------------------------------------------------


def _hidraw_sysfs(node: str) -> dict | None:
    """VID/PID/name metadata for a /dev/hidrawN node (via sysfs)."""
    name = os.path.basename(node)
    dev = os.path.realpath(f"/sys/class/hidraw/{name}/device")
    hid_id = hid_name = ""
    for path in (os.path.join(dev, "uevent"),):
        try:
            with open(path) as fh:
                for line in fh:
                    if line.startswith("HID_ID="):
                        hid_id = line.split("=", 1)[1].strip()
                    elif line.startswith("HID_NAME="):
                        hid_name = line.split("=", 1)[1].strip()
        except OSError:
            return None
    m = re.search(r":([0-9A-Fa-f]{8}):([0-9A-Fa-f]{8})$", hid_id)
    if not m:
        return None
    return {"node": node, "vid": int(m.group(1), 16),
            "pid": int(m.group(2), 16), "name": hid_name}


def find_hidraw(vid_pid=None) -> list[dict]:
    """List hidraw nodes, optionally filtered to one VID/PID pair."""
    found = []
    for node in sorted(glob.glob("/dev/hidraw*")):
        info = _hidraw_sysfs(node)
        if info is None:
            continue
        if vid_pid is None or (info["vid"], info["pid"]) == vid_pid:
            found.append(info)
    return found


def run_hidraw(node: str, on_scan, *, raw: bool = False,
               silence_timeout: float | None = None, select_fn=None,
               log=None) -> str:
    """Poll a hidraw node; call on_scan(text) per barcode.

    Returns an outcome token so the caller can exit differently for each
    failure (the point of this tool in the fallback: "no node" and "attached
    but silent" must not look alike):

    * ``"count"``  — on_scan returned ``False`` (the ``--count`` budget is met)
    * ``"gone"``   — the node vanished: a zero-length read, ENODEV/EIO, or a
      descriptor that cannot be polled any more (the ``-71``-then-dropout
      signature this shop's host produces)
    * ``"silent"`` — nothing arrived within ``silence_timeout`` seconds while
      the node stayed open: the device is not transmitting (trigger, HID mode,
      or a dead link upstream of us)

    The frame shape is discovered per frame (``boot_report``), and the first
    frame's shape is announced once on stderr — so an operator transcript
    answers "9 bytes or 8?" without a second experiment. ``raw`` hexdumps every
    frame (the same answer, per scan, for a packet-level argument).

    Nothing here writes: the node is opened ``O_RDONLY`` and no file is
    created. ``KeyboardInterrupt`` is deliberately NOT caught — ``main`` treats
    Ctrl-C as a clean stop and still emits the ``--json`` document.
    """
    log = log or (lambda message: print(message, file=sys.stderr, flush=True))
    timeout = (SILENCE_TIMEOUT_DEFAULT if silence_timeout is None
               else float(silence_timeout))
    poll = select_fn or select.select
    fd = os.open(node, os.O_RDONLY)   # O_RDONLY only: this tool never writes
    assembler = ScanAssembler()
    announced = False
    try:
        while True:
            try:
                # timeout 0 means "wait forever" (an explicit operator choice)
                ready = poll([fd], [], [], timeout if timeout > 0 else None)[0]
            except OSError as exc:      # a node that cannot be polled is gone
                log(f"scanner_hid: {node} is no longer pollable ({exc}) — the "
                    "device is gone")
                return "gone"
            if not ready:
                log(f"scanner_hid: no frames in {timeout:g}s on {node} — the node "
                    "is open and readable but the DEVICE is not transmitting "
                    "(trigger, HID mode, or the -71/dropout signature). "
                    "Nothing was captured; diagnose with: "
                    "uv run python scripts/usb_host_check.py")
                return "silent"
            try:
                report = os.read(fd, 64)
            except OSError as exc:      # ENODEV/EIO/EPROTO: unplugged or dropped
                log(f"scanner_hid: the scanner vanished while reading {node} "
                    f"({exc}) — it dropped off the bus (the -71/dropout "
                    "signature). Re-plug it and re-run; diagnose with: "
                    "uv run python scripts/usb_host_check.py")
                return "gone"
            if not report:              # hidraw never returns 0 bytes for a report
                log(f"scanner_hid: zero-length read on {node} — the device is gone")
                return "gone"
            if not announced:
                announced = True
                log(f"# frame shape: {frame_shape_label(report)}")
            if raw:
                log(f"# raw {len(report)}B {report.hex(' ')}")
            scan = assembler.feed(boot_report(report))
            if scan is not None and on_scan(scan) is False:
                return "count"
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


# --------------------------------------------------------------------------
# hidapi backend (Windows / macOS)
# --------------------------------------------------------------------------


def run_hidapi(vid_pid, on_scan) -> None:
    try:
        import hid  # hidapi, imported lazily
    except ImportError:
        sys.exit(
            "the hidapi backend needs the `hid` package:\n"
            "  uv run --with hid python scripts/scanner_hid.py"
        )
    vid, pid = vid_pid
    matches = hid.enumerate(vid, pid)
    if not matches:
        sys.exit(f"no HID device {vid:04x}:{pid:04x} found")
    dev = hid.device()
    dev.open_path(matches[0]["path"])
    dev.setnonblocking(False)
    assembler = ScanAssembler()
    try:
        while True:
            report = dev.read(64)
            if not report:
                time.sleep(0.005)
                continue
            # hidapi's framing for UNNUMBERED reports differs per backend; the
            # same shape discovery as the hidraw loop applies.
            scan = assembler.feed(boot_report(bytes(report)))
            if scan is not None and on_scan(scan) is False:
                return
    finally:
        dev.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


#: The daemon's single-instance lockfile name. Kept in sync with
#: scripts/scanner_daemon.py::_default_lockfile_path (a drift means this tool
#: reports the wrong thing about a running daemon — pinned by
#: test_lockfile_path_matches_the_daemon, the pairing-file precedent).
_LOCKFILE_NAME = "hcrm-scanner-daemon.lock"


def _lockfile_path() -> Path:
    """The running daemon's lockfile path (``$XDG_RUNTIME_DIR`` or a tempdir)."""
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / _LOCKFILE_NAME


def running_daemon(path=None) -> int | None:
    """PID of a running ``scanner_daemon``, or None.

    Strictly READ-ONLY: the lockfile's PID is read and liveness is checked via
    ``/proc/<pid>``. The daemon holds an EXCLUSIVE flock plus an EVIOCGRAB on
    the scanner's evdev node; taking the lock here would fail noisily and
    killing the daemon is an operator decision, so this tool only reports.
    """
    if not sys.platform.startswith("linux"):
        return None
    lock = Path(path) if path is not None else _lockfile_path()
    try:
        pid = int(lock.read_text().strip() or "0")
    except (OSError, ValueError):
        return None
    if pid <= 0 or not Path(f"/proc/{pid}").exists():
        return None
    return pid


def _nonnegative_seconds(text: str) -> float:
    """argparse type for --silence-timeout: a finite, non-negative float."""
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if value != value or value < 0 or value == float("inf"):
        raise argparse.ArgumentTypeError("must be finite and >= 0")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Receive barcodes from a USB-HID keyboard-mode scanner "
        "(Opticon 065a:a001 by default; see docs/opticon-hardware.md §3)"
    )
    parser.add_argument("--vid", default=None, help="USB vendor id (hex, default 065a)")
    parser.add_argument("--pid", default=None, help="USB product id (hex, default a001)")
    parser.add_argument("--backend", choices=("auto", "hidraw", "hidapi"),
                        default="auto")
    parser.add_argument("--node", default=None,
                        help="explicit /dev/hidrawN (skip discovery)")
    parser.add_argument("--count", type=int, default=None,
                        help="exit after N barcodes (default: run forever)")
    parser.add_argument("--json", action="store_true",
                        help="emit one JSON document at the end (needs --count)")
    parser.add_argument("--list", action="store_true",
                        help="list candidate HID devices and exit")
    parser.add_argument("--raw", action="store_true",
                        help="hexdump every received frame to stderr — answers "
                             "\"9-byte prefixed or 8-byte bare?\" per scan")
    parser.add_argument("--silence-timeout", metavar="SECONDS",
                        type=_nonnegative_seconds,
                        default=SILENCE_TIMEOUT_DEFAULT,
                        help="exit 4 when no frame arrives for this long "
                             "(0 = wait forever; default "
                             f"{SILENCE_TIMEOUT_DEFAULT:g})")
    args = parser.parse_args(argv)

    vid = int(args.vid, 16) if args.vid else DEFAULT_VID_PID[0]
    pid = int(args.pid, 16) if args.pid else DEFAULT_VID_PID[1]

    if args.list:
        if sys.platform.startswith("linux"):
            for info in find_hidraw():
                mark = " <-- target" if (info["vid"], info["pid"]) == (vid, pid) else ""
                print(f"{info['node']}: {info['vid']:04x}:{info['pid']:04x} "
                      f"{info['name']}{mark}")
        else:
            try:
                import hid
                for d in hid.enumerate():
                    if d["vendor_id"] == 0x065A or d["vendor_id"] == vid:
                        print(f"{d['vendor_id']:04x}:{d['product_id']:04x} "
                              f"{d.get('product_string', '')} ({d['path']!r})")
            except ImportError:
                sys.exit("--list on this platform needs: uv run --with hid ...")
        return 0

    records: list[dict] = []
    count = [0]

    def emit(text: str):
        count[0] += 1
        when = time.strftime("%Y-%m-%d %H:%M:%S")
        if args.json:
            records.append({"barcode": text, "symbology": "HID-Keyboard",
                            "symbology_id": None, "timestamp": when})
        else:
            print(format_csv_row(when, text), flush=True)
        if args.count and count[0] >= args.count:
            return False
        return None

    backend = args.backend
    if backend == "auto":
        backend = "hidraw" if sys.platform.startswith("linux") else "hidapi"

    if backend == "hidraw":
        node = args.node
        if node is None:
            matches = find_hidraw((vid, pid))
            if not matches:
                # "cannot run" is not success (ui_check doctrine) — and say what
                # to do next, in the order that actually bears fruit here.
                print(
                    f"scanner_hid: no /dev/hidraw* for {vid:04x}:{pid:04x} — the "
                    "scanner is not on the bus.\n"
                    "  re-plug it (a marginal cable shows as repeated `device "
                    "descriptor read/64, error -71` in the journal, then the "
                    "device drops off: `journalctl -k | tail -40`)\n"
                    "  then:            uv run python scripts/scanner_hid.py --list\n"
                    "  host preflight:  uv run python scripts/usb_host_check.py\n"
                    "  node permissions: docs/opticon-hardware.md §4 (the udev rule)",
                    file=sys.stderr,
                )
                return EXIT_CANNOT_RUN
            node = matches[0]["node"]
        daemon_pid = running_daemon()
        if daemon_pid:
            # Read-only neighbour report: the live daemon owns the evdev node
            # (EVIOCGRAB) but not hidraw, and it is the operator's call to stop
            # it — so we neither take its lock nor signal it.
            print(
                f"# note: scanner_daemon is running (pid {daemon_pid}) and holds the "
                "scanner's evdev node exclusively.\n"
                "# hidraw sits below the input layer, so THIS tool still receives; "
                "the window-typing path does not.\n"
                f"# to give window typing back:  sudo systemctl stop scanner-daemon"
                f"   (or kill {daemon_pid})",
                file=sys.stderr,
            )
        print(f"# listening on {node} (Ctrl-C to stop)", file=sys.stderr)
        status = EXIT_OK
        try:
            outcome = run_hidraw(node, emit, raw=args.raw,
                                 silence_timeout=args.silence_timeout)
            # "gone"/"silent" already explained themselves on stderr; the exit
            # code is what a wrapper script branches on.
            status = {"gone": EXIT_CANNOT_RUN, "silent": EXIT_SILENT}.get(
                outcome, EXIT_OK)
        except KeyboardInterrupt:
            pass
        except PermissionError:
            print(f"scanner_hid: permission denied on {node} — run with sudo once, "
                  "or install the udev rule (docs/opticon-hardware.md §4)",
                  file=sys.stderr)
            return EXIT_CANNOT_RUN
        except OSError as exc:
            # The node disappeared between discovery and open (the racy
            # enumeration this host is known for): no traceback, just the fact.
            print(f"scanner_hid: cannot open {node} ({exc}) — the device is gone; "
                  "re-plug it and re-run (host preflight: "
                  "uv run python scripts/usb_host_check.py)", file=sys.stderr)
            return EXIT_CANNOT_RUN
    else:
        status = EXIT_OK
        print("# listening via hidapi (Ctrl-C to stop)", file=sys.stderr)
        try:
            run_hidapi((vid, pid), emit)
        except KeyboardInterrupt:
            pass

    if args.json:
        print(json.dumps({"device_id": None, "count": len(records),
                          "scans": records}, indent=2))
    return status


if __name__ == "__main__":
    sys.exit(main())
