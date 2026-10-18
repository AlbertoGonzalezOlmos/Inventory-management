#!/usr/bin/env python3
"""scanner_test — end-to-end functional test for the attached Opticon scanner.

Connection check first, then exercises whatever personality the scanner
presents, reusing the repo's existing building blocks:

* ``scripts/opticon_detect.py`` — the device survey / personality map.
* ``scripts/opn2001.py``        — the OPN-2001 RBBV protocol layer (frames,
                                  CRC, records, both serial and raw-USB
                                  backends).
* ``scripts/scanner_hid.py``    — the USB-HID keyboard-mode receiver.

    uv run python scripts/scanner_test.py                     # check + test
    uv run python scripts/scanner_test.py --wait 60           # wait for plug-in
    uv run python scripts/scanner_test.py --no-scan-test      # skip live scan
    uv run python scripts/scanner_test.py --clear             # wipe batch after

Dependencies are the same as the underlying tools and imported lazily:
pyserial for a /dev/ttyUSB* (COMx) port, pyusb for the raw-USB backend:

    uv run --with pyserial python scripts/scanner_test.py
    uv run --with pyusb    python scripts/scanner_test.py

Exit code 0 = every requested test passed; 1 = failure; 2 = no scanner found.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import select
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.opticon_detect import describe, survey  # noqa: E402
from scripts.opn2001 import (  # noqa: E402
    OPN2001,
    UsbStream,
    _default_port,
    _open_serial,
    detect_device,
)
from scripts.scanner_hid import ScanAssembler, find_hidraw  # noqa: E402

OPTICON_VID = 0x065A


# --------------------------------------------------------------------------
# Connection check
# --------------------------------------------------------------------------

def check_connection(wait_s: int = 0) -> dict:
    """Detect the scanner, optionally polling until it shows up."""
    report = detect_device()
    if report["found"] or wait_s <= 0:
        return report

    print(f"no Opticon device visible yet — waiting up to {wait_s}s "
          f"(re-plug the USB cable, watch for the scanner's LED)")
    print("tips: the OPN-2001 ships with a CHARGE-ONLY mini-B cable; a data "
          "cable is required for the host to see it. If it is in HID/keyboard "
          "mode it enumerates as 065A:A001, not 0009.")
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(2)
        report = detect_device()
        if report["found"]:
            print("scanner appeared!")
            return report
        sys.stdout.write(".")
        sys.stdout.flush()
    print()
    return report


def print_survey() -> None:
    """One-paragraph USB reality check, from the shared map."""
    print("USB survey (all Opticon devices, VID 065A):")
    devs = (survey().get("linux") or []) \
        if sys.platform.startswith("linux") else []
    devs = [d for d in devs if d.get("vid") == OPTICON_VID]
    if not devs:
        print("  - nothing — no 065A device is enumerated by this kernel")
    for d in devs:
        info = describe(d["pid"])
        print(f"  - 065A:{d['pid']:04X} {info['name']} "
              f"({'confirmed' if info['confirmed'] else 'observed, unconfirmed'}) "
              f"ttys={d.get('ttys')} hidraws={d.get('hidraws')}")


# --------------------------------------------------------------------------
# Test bookkeeping
# --------------------------------------------------------------------------

class Results:
    def __init__(self) -> None:
        self.entries: list[tuple[str, bool, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.entries.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}"
              + (f" — {detail}" if detail else ""))

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.entries)

    def summary(self) -> int:
        passed = sum(1 for _, ok, _ in self.entries if ok)
        total = len(self.entries)
        print(f"\n{passed}/{total} tests passed")
        return 0 if self.ok and total else 1


# --------------------------------------------------------------------------
# OPN-2001 (065A:0009) functional test
# --------------------------------------------------------------------------

def test_opn2001(backend: str, port: str | None, results: Results,
                 scan_test: bool, clear: bool) -> None:
    """Protocol round-trip + optional live scan against an OPN-2001."""
    if backend == "serial":
        if not port:
            port = _default_port()
        if not port:
            results.record("open serial port", False,
                           "no port (is the opticon usb-serial driver loaded? "
                           "try --backend usb)")
            return
        try:
            stream = _open_serial(port)
        except SystemExit as e:
            results.record(f"open serial port {port}", False, str(e))
            return
        except OSError as e:
            results.record(f"open serial port {port}", False, str(e))
            return
    else:
        try:
            stream = UsbStream()
        except SystemExit as e:
            results.record("open raw-USB (pyusb)", False, str(e))
            return
        except Exception as e:  # usb.core.USBError etc.
            results.record("open raw-USB (pyusb)", False, repr(e))
            return
    label = f"backend '{backend}'" + (f" on {port}" if port else "")
    results.record(f"open {label}", True)

    try:
        dev = OPN2001(stream)
        try:
            info = dev.interrogate()
        except Exception as e:
            results.record("interrogate (RBBV protocol)", False, repr(e))
            return
        results.record("interrogate (RBBV protocol)", True,
                       f"device {info['device_id']}, firmware {info['firmware']}")

        try:
            when = dev.get_time()
            host = time.localtime()
            drift = (when - dt.datetime.now()).total_seconds()
            results.record("read clock", True,
                           f"{when.isoformat(sep=' ')} "
                           f"({drift:+.0f}s vs host {host.tm_zone or 'local'})")
        except Exception as e:
            results.record("read clock", False, repr(e))

        try:
            _, scans = dev.get_data()
            results.record("read batch memory", True,
                           f"{len(scans)} scan(s) stored")
        except Exception as e:
            results.record("read batch memory", False, repr(e))
            scans = []

        if scan_test:
            before = len(scans)
            print(f"\nLIVE SCAN TEST: press the trigger and scan ANY barcode "
                  f"within 30 seconds... (stored scans so far: {before})")
            got: object = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and got is None:
                time.sleep(2)
                try:
                    _, now_scans = dev.get_data()
                except Exception:
                    continue
                if len(now_scans) > before:
                    got = now_scans[-1]
            if got is not None:
                results.record("live scan", True,
                               f"{got.barcode!r} ({got.symbology}, "
                               f"{got.timestamp.isoformat(sep=' ')})")
            else:
                results.record("live scan", False,
                               "no new scan stored in 30s — trigger works? "
                               "battery charged? aiming at a readable code?")

        if clear:
            try:
                dev.clear()
                _, remaining = dev.get_data()
                results.record("clear batch memory", len(remaining) == 0,
                               f"{len(remaining)} scan(s) left")
            except Exception as e:
                results.record("clear batch memory", False, repr(e))
    finally:
        stream.close()


# --------------------------------------------------------------------------
# USB-HID keyboard-mode (065A:A001) functional test
# --------------------------------------------------------------------------

def test_hid(results: Results, scan_test: bool, count: int = 1,
             timeout_s: int = 30) -> None:
    """Live scan capture from the HID keyboard-mode personality."""
    nodes = find_hidraw(vid_pid=(OPTICON_VID, 0xA001))
    if not nodes:
        results.record("find hidraw node", False,
                       "device enumerated but no /dev/hidraw* (CONFIG_HIDRAW)")
        return
    node = nodes[0]["node"]
    results.record("find hidraw node", True, node)

    if not scan_test:
        return

    print(f"\nLIVE SCAN TEST: scan {count} barcode(s) within {timeout_s}s "
          f"(HID keyboard mode — the codes arrive like keystrokes)")
    try:
        fd = os.open(node, os.O_RDONLY)
    except OSError as e:
        results.record("open hidraw node", False, str(e))
        return
    assembler = ScanAssembler()
    got: list[str] = []
    deadline = time.monotonic() + timeout_s
    try:
        while len(got) < count and time.monotonic() < deadline:
            remaining = max(0, deadline - time.monotonic())
            ready, _, _ = select.select([fd], [], [], min(1.0, remaining))
            if not ready:
                continue
            report = os.read(fd, 64)
            if not report:
                continue
            text = assembler.feed(report[1:])
            if text is not None:
                print(f"  scan: {text!r}")
                got.append(text)
    finally:
        os.close(fd)
    results.record("live scan", len(got) >= count,
                   f"{len(got)}/{count} barcode(s): {got}" if got
                   else f"nothing in {timeout_s}s — trigger/battery/code?")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Test the Opticon scanner connection and functionality")
    ap.add_argument("--wait", type=int, default=0, metavar="SEC",
                    help="poll for the device up to SEC seconds before giving up")
    ap.add_argument("--backend", choices=["auto", "serial", "usb"],
                    default="auto", help="OPN-2001 transport (default: auto)")
    ap.add_argument("--port", default=None,
                    help="serial port for --backend serial (default: "
                         "$OPN2001_PORT or /dev/ttyUSB0)")
    ap.add_argument("--no-scan-test", action="store_true",
                    help="skip the interactive live-scan step")
    ap.add_argument("--clear", action="store_true",
                    help="wipe the OPN-2001 batch memory after testing")
    args = ap.parse_args(argv)

    print("=" * 60)
    print("1) CONNECTION CHECK")
    print("=" * 60)
    report = check_connection(args.wait)
    print(f"platform: {report['platform']}  found: {report['found']}  "
          f"usable: {report['usable']}")
    print_survey()
    for g in report.get("guidance", []):
        print(f"hint: {g}")

    if not report["found"]:
        print("\nNo Opticon scanner is visible to this OS. Before it can be "
              "tested it must enumerate: check the cable is a DATA cable "
              "(the bundled OPN-2001 cable is charge-only), charge the "
              "scanner, and re-plug. Then run with --wait, e.g.: "
              "uv run python scripts/scanner_test.py --wait 60")
        return 2

    results = Results()
    print("\n" + "=" * 60)
    print("2) FUNCTIONAL TEST")
    print("=" * 60)
    backends = {c.get("backend") for c in report.get("candidates", [])}
    scan_test = not args.no_scan_test
    if "hidraw" in backends:
        test_hid(results, scan_test)
    elif "serial" in backends and any(
            c.get("vid", OPTICON_VID) == OPTICON_VID and c.get("pid") == 0x0009
            for c in report.get("candidates", [])):
        test_opn2001(args.backend, args.port, results, scan_test, args.clear)
    elif "usb" in backends:
        test_opn2001("usb", None, results, scan_test, args.clear)
    elif "serial" in backends:  # 065A:A002 — the M-10, plain serial line
        results.record("M-10 USB-COM detected", True,
                       "plain serial line, not RBBV — functional test for it "
                       "is scripts/scanner_bridge.py (see docs/opticon-hardware.md)")
    else:
        results.record("personality dispatch", False,
                       "Opticon device present but no testable backend")
    return results.summary()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
