#!/usr/bin/env python3
"""scanner_hid — receive barcodes from a USB-HID (keyboard-mode) scanner.

Companion to scripts/opn2001.py for Opticon scanners that enumerate as a HID
keyboard instead of a virtual COM port — e.g. the M-10 family in its USB-HID
personality (VID 065A, PID A001; bus-reported name "Opticon USB Barcode
Reader"; the same hardware in USB-COM mode is 065A:A002). Keyboard-mode
scanners "type" each scan followed by Enter; this tool reads the raw HID
keyboard reports directly (no window focus needed) and emits one line per
barcode:

    2026-09-29 10:12:03,HID-Keyboard,8412345678905

which is the same CSV shape `scripts/opn2001.py read` produces, so anything
downstream (scripts/scan_intake.py) consumes both sources identically.
`--json` emits the opn2001-compatible JSON document instead.

Backends (auto-detected, or forced with --backend):

* ``hidraw``  — Linux/WSL2. Stdlib only: reads /dev/hidraw* and matches the
  device by VID/PID in sysfs. On WSL2 the scanner must first be attached from
  Windows with usbipd-win (docs/scanner-opn2001.md §3.4); the stock WSL2
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
import glob
import json
import os
import re
import sys
import time
from dataclasses import dataclass

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


def group_scans_from_reports(reports: list[bytes]) -> list[str]:
    """Feed a sequence of reports; return the complete (Enter-terminated)
    payloads. Kept as a pure function so tests need no device."""
    buffer = ""
    out: list[str] = []
    for report in reports:
        text, enter = parse_keyboard_report(report)
        if enter:
            if buffer:
                out.append(buffer)
            buffer = ""
        else:
            buffer += text
    return out


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
    buffer = ""
    try:
        while True:
            report = os.read(fd, 64)
            if not report:
                continue
            text, enter = parse_keyboard_report(report[1:])
            if enter:
                if buffer and on_scan(buffer) is False:
                    return
                buffer = ""
            else:
                buffer += text
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
    buffer = ""
    try:
        while True:
            report = dev.read(64)
            if not report:
                time.sleep(0.005)
                continue
            # Like hidraw, hidapi's first byte is the report ID (0x00 for
            # unnumbered boot-keyboard reports).
            text, enter = parse_keyboard_report(bytes(report)[1:])
            if enter:
                if buffer and on_scan(buffer) is False:
                    return
                buffer = ""
            else:
                buffer += text
    finally:
        dev.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Receive barcodes from a USB-HID keyboard-mode scanner "
        "(Opticon 065a:a001 by default; see docs/scanner-opn2001.md §3.5)"
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
            print(f"{when},HID-Keyboard,{text}", flush=True)
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
                    "attached? (WSL2: usbipd attach --wsl …; see detect output "
                    "of scripts/opn2001.py)"
                )
            node = matches[0]["node"]
        print(f"# listening on {node} (Ctrl-C to stop)", file=sys.stderr)
        try:
            run_hidraw(node, emit)
        except KeyboardInterrupt:
            pass
        except PermissionError:
            sys.exit(f"permission denied on {node} — run with sudo once, or "
                     "add a udev rule (docs/scanner-opn2001.md §3.1)")
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
