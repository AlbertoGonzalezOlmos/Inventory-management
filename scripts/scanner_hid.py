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
"""

from __future__ import annotations

import argparse
import csv
import glob
import io
import json
import os
import re
import sys
import time

DEFAULT_VID_PID = (0x065A, 0xA001)  # Opticon USB Barcode Reader (HID mode)

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


def run_hidraw(node: str, on_scan) -> None:
    """Blocking read loop on a hidraw node; calls on_scan(text) per barcode.

    hidraw always prepends the report ID (0x00 for unnumbered reports), so
    the boot-keyboard payload starts at byte 1.
    """
    fd = os.open(node, os.O_RDONLY)
    assembler = ScanAssembler()
    try:
        while True:
            report = os.read(fd, 64)
            if not report:
                continue
            scan = assembler.feed(report[1:])
            if scan is not None and on_scan(scan) is False:
                return
    finally:
        os.close(fd)


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
            # Like hidraw, hidapi's first byte is the report ID (0x00 for
            # unnumbered boot-keyboard reports).
            scan = assembler.feed(bytes(report)[1:])
            if scan is not None and on_scan(scan) is False:
                return
    finally:
        dev.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


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
                sys.exit(
                    f"no /dev/hidraw* for {vid:04x}:{pid:04x} — is the scanner "
                    "attached? (WSL2: usbipd attach --wsl …; survey the host "
                    "with: uv run python scripts/opticon_detect.py)"
                )
            node = matches[0]["node"]
        print(f"# listening on {node} (Ctrl-C to stop)", file=sys.stderr)
        try:
            run_hidraw(node, emit)
        except KeyboardInterrupt:
            pass
        except PermissionError:
            sys.exit(f"permission denied on {node} — run with sudo once, or "
                     "install the udev rule (docs/opticon-hardware.md §4)")
    else:
        print("# listening via hidapi (Ctrl-C to stop)", file=sys.stderr)
        try:
            run_hidapi((vid, pid), emit)
        except KeyboardInterrupt:
            pass

    if args.json:
        print(json.dumps({"device_id": None, "count": len(records),
                          "scans": records}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
