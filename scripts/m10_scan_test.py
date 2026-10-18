#!/usr/bin/env python3
"""m10_scan_test — can the attached Opticon M-10 actually READ codes?

``scripts/scanner_test.py`` answers "is the scanner alive and talking?"
(protocol round-trip, clock, batch memory). This script answers the next
question: **can it read both code families it was bought for?**

* **1D test** — scan any retail barcode (EAN-13/UPC; any grocery item works).
  The payload is validated through the GS1 number system: check digit,
  prefix region, in-store vs global (``app/barcodes.py``, the same
  classifier the catalogue uses — so the test exercises the production
  path, not a private reimplementation).
* **QR test** — the script *generates* a QR target with the vendored Nayuki
  encoder (``app/vendor/qrcodegen.py``, the badge encoder), prints it in
  the terminal and knows the exact payload to expect, making the test
  content-exact rather than "something arrived". ``--any-qr`` scans a QR
  of your own instead (content classified, not compared).
* **Command channel** (USB-COM personality only) — Z1/Z2 software-trigger
  round-trip, each answered by ACK: proves the link is bidirectional.
* ``--repeat N`` — scan the same QR target N times; every read must return
  the identical payload (consistency: no dropped or duplicated bytes).

Personality dispatch comes from the shared map (``scripts/opticon_detect.py``,
``docs/opticon-hardware.md`` §1):

* ``065A:A002`` — M-10 in USB-COM mode: full test (``app.scanner`` driver).
* ``065A:A001`` — HID keyboard-mode personality: scan tests only (there is
  no host→scanner channel in HID mode).
* ``065A:0009`` — that is the OPN-2001, not an M-10: use
  ``scripts/scanner_test.py``.

Usage::

    uv run python scripts/m10_scan_test.py                    # full test
    uv run python scripts/m10_scan_test.py --wait 60          # wait for plug-in
    uv run python scripts/m10_scan_test.py --timeout 45       # per-scan window
    uv run python scripts/m10_scan_test.py --repeat 3         # consistency check
    uv run python scripts/m10_scan_test.py --any-qr           # scan your own QR
    uv run python scripts/m10_scan_test.py --svg /tmp/qr.svg  # save the QR target

Exit codes: 0 = every test passed, 1 = at least one failure,
2 = no M-10-compatible scanner found.

Offline testability: the validation and rendering helpers are pure functions
with known-answer tests in ``tests/test_m10_scan_test.py``; only ``run_serial``
and ``run_hid`` touch hardware, and both are driven through an injectable
``next_scan(timeout)`` callable.
"""

from __future__ import annotations

import argparse
import os
import queue
import select
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.barcodes import analyze_scan  # noqa: E402
from scripts.opticon_detect import OPTICON_VID, describe, survey  # noqa: E402

#: PIDs this script can drive, in preference order (M-10 personalities first).
M10_PIDS = {0xA002: "USB-COM", 0xA001: "USB-HID keyboard"}


# --------------------------------------------------------------------------
# QR test target — generated, so the expected payload is known exactly
# --------------------------------------------------------------------------

def build_qr_payload() -> str:
    """Unique, compact payload for the QR test target.

    20 characters keeps the symbol at version 1 (21×21 modules): the sparsest
    possible QR, so a screen-rendered scan exercises the reader, not the
    user's aim. All characters are in the QR alphanumeric set.
    """
    return f"HCRM-SCANTEST:{secrets.token_hex(3).upper()}"


def render_ascii_qr(qr, border: int = 4) -> str:
    """Terminal rendering: two characters per module (terminal cells are
    roughly twice as tall as wide, so this keeps the symbol square).

    ``border`` is the quiet zone the QR specification requires — without it
    a screen scan reliably fails, and the failure looks like a scanner bug.
    """
    size = qr.get_size()
    total = size + 2 * border
    blank = "  " * total
    rows = [blank] * border
    for y in range(size):
        rows.append(
            "  " * border
            + "".join("██" if qr.get_module(x, y) else "  " for x in range(size))
            + "  " * border
        )
    rows.extend([blank] * border)
    return "\n".join(rows)


def qr_svg(qr, border: int = 4) -> str:
    """SVG rendering of the same matrix (same shape as ``app.qrbadge``)."""
    size = qr.get_size()
    total = size + 2 * border
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" '
        f'shape-rendering="crispEdges">',
        f'<rect width="{total}" height="{total}" fill="#ffffff"/>',
    ]
    for y in range(size):
        for x in range(size):
            if qr.get_module(x, y):
                parts.append(
                    f'<rect x="{x + border}" y="{y + border}" width="1" height="1" '
                    f'fill="#000000"/>')
    parts.append("</svg>")
    return "".join(parts)


def classify_qr_content(payload: str) -> str:
    """Best-effort content kind for an arbitrary QR payload."""
    p = (payload or "").strip()
    low = p.lower()
    if low.startswith(("http://", "https://")):
        return "URL"
    if low.startswith("wifi:"):
        return "Wi-Fi network config"
    if low.startswith(("mailto:", "tel:", "smsto:", "geo:")):
        return "contact/geo link"
    if low.startswith(("mecard:", "begin:vcard")):
        return "contact card"
    if p.startswith("{") and p.endswith("}"):
        return "JSON object"
    if p.startswith("]d2") or "(01)" in p:
        return "GS1 element string"
    return "plain text"


# --------------------------------------------------------------------------
# Payload validation — pure, shared by every transport
# --------------------------------------------------------------------------

def validate_1d(payload: str) -> tuple[bool, str]:
    """Did the 1D test read a well-formed retail barcode?

    Uses ``app.barcodes.analyze_scan`` (no symbology context: neither the
    USB-COM frames nor HID keystrokes carry a symbology ID unless the
    scanner is reconfigured to transmit one), so classification is
    content-based — exactly the path the catalogue takes for a raw scan.
    """
    analysis = analyze_scan(payload)
    if analysis.kind == "empty":
        return False, "empty payload"
    if analysis.kind == "invalid-gtin":
        return (False,
                "digits of GTIN length but the check digit fails — a misread, "
                "or a non-GS1 numeric label; re-scan a retail EAN-13/UPC code")
    if analysis.kind in ("gtin", "restricted"):
        info = analysis.info
        detail = (f"valid {info.format}"
                  + (f" + add-on {analysis.addon}" if analysis.addon else "")
                  + f", key {analysis.key}, {info.region or 'unknown GS1 region'}"
                    f" ({info.usage})")
        if analysis.kind == "restricted":
            detail += " — restricted circulation (in-store) number"
        return True, detail
    # "opaque": an internal Code 128/39-style identifier. The read itself is
    # what this test checks; opaque payloads are legitimate (our own SKU
    # labels print exactly those).
    return True, f"opaque payload (internal Code 128/39-style identifier)"


def validate_qr(payload: str, expected: str | None = None) -> tuple[bool, str]:
    """Did the QR test read the right thing?

    With ``expected`` (the generated target) the match is exact — decoding
    integrity, not just arrival. Without it (``--any-qr``), any non-empty
    decodable payload passes and gets classified.
    """
    if not (payload or "").strip():
        return False, "empty payload"
    if expected is not None:
        if payload.strip() == expected:
            return True, "exact match with the generated target"
        return (False,
                f"payload {payload!r} != generated target {expected!r} — "
                "misdecode or the wrong code was scanned")
    detail = f"decoded {classify_qr_content(payload)} payload"
    if payload.strip().isdigit() and len(payload.strip()) <= 14:
        detail += (" (all digits, GTIN length: a QR can legitimately carry a "
                   "GTIN, but this may have been the item's 1D barcode)")
    return True, detail


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
# Live scan tests — transport-independent, fed through next_scan(timeout)
# --------------------------------------------------------------------------

def run_scan_tests(next_scan, results: Results, *, timeout: float,
                   expected_qr: str | None, repeat: int) -> None:
    """The 1D and QR read tests, given a ``next_scan(timeout) -> str|None``."""

    print(f"\nLIVE TEST 1 — 1D barcode: scan any RETAIL product barcode "
          f"(EAN-13/UPC — any grocery item works) within {timeout:.0f}s")
    code = next_scan(timeout)
    if code is None:
        results.record("1D barcode read", False,
                       f"nothing in {timeout:.0f}s — code close enough? "
                       "scanner awake? (move it / present the code again)")
    else:
        ok, detail = validate_1d(code)
        results.record("1D barcode read", ok, f"{code!r}: {detail}")

    target = "the QR target above" if expected_qr else "any QR code you have"
    print(f"\nLIVE TEST 2 — QR code: scan {target} within {timeout:.0f}s")
    code = next_scan(timeout)
    if code is None:
        results.record("QR code read", False,
                       f"nothing in {timeout:.0f}s — terminal font big enough "
                       "for the target? try --svg and print it, or --any-qr")
    else:
        ok, detail = validate_qr(code, expected_qr)
        results.record("QR code read", ok, f"{code!r}: {detail}")

    if repeat > 1:
        print(f"\nLIVE TEST 3 — repeat consistency: scan the SAME QR target "
              f"{repeat} times (every read must be identical)")
        reads = [next_scan(timeout) for _ in range(repeat)]
        if any(r is None for r in reads):
            results.record(f"repeat consistency x{repeat}", False,
                           f"only {sum(r is not None for r in reads)}/{repeat} "
                           "scans arrived")
        else:
            unique = set(reads)
            ok = len(unique) == 1 and (expected_qr is None
                                       or reads[0] == expected_qr)
            results.record(f"repeat consistency x{repeat}", ok,
                           f"{len(unique)} distinct payload(s) in {repeat} reads"
                           + (f": {sorted(unique)!r}" if not ok else ""))


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------

def run_serial(port: str, results: Results, *, expected_qr: str | None,
               timeout: float, repeat: int, command_channel: bool) -> None:
    """M-10 in USB-COM mode (065A:A002) — the full test, bidirectional."""
    from app.scanner import OpticonM10
    from app.scanner.m10 import CommandRejected
    from app.scanner.transport import SerialOpenError

    try:
        scanner = OpticonM10(port).open()
    except SerialOpenError as exc:
        results.record(f"open {port} (065A:A002, USB-COM)", False, str(exc))
        return
    results.record(f"open {port} (065A:A002, USB-COM)", True,
                   "CDC-ACM virtual serial port, 9600 8N1")
    try:
        if command_channel:
            try:
                # Z1 (read enable) / Z2 (disable) / Z1 again: leave the
                # scanner in the enabled state we found it heading for.
                # Each dedicated command must be answered by a single ACK
                # byte — this is the only test of host→scanner traffic.
                scanner.trigger_on()
                scanner.trigger_off()
                scanner.trigger_on()
                results.record("command channel Z1/Z2 round-trip", True,
                               "software trigger on/off/on, ACK each time")
            except (CommandRejected, TimeoutError) as exc:
                results.record("command channel Z1/Z2 round-trip", False,
                               repr(exc))

        scans: queue.Queue = queue.Queue()
        scanner.start_reading(
            scans.put,
            on_error=lambda exc: print(f"scanner error: {exc!r}",
                                       file=sys.stderr, flush=True))

        def next_scan(t: float):
            try:
                return scans.get(timeout=max(0.1, t))
            except queue.Empty:
                return None

        run_scan_tests(next_scan, results, timeout=timeout,
                       expected_qr=expected_qr, repeat=repeat)
    finally:
        scanner.close()


def run_hid(node: str, results: Results, *, expected_qr: str | None,
            timeout: float, repeat: int) -> None:
    """HID keyboard-mode personality (065A:A001) — receive-only by design."""
    from scripts.scanner_hid import ScanAssembler

    try:
        fd = os.open(node, os.O_RDONLY)
    except PermissionError:
        results.record(f"open {node} (065A:A001, HID keyboard mode)", False,
                       "permission denied — install the udev rules once: "
                       "sudo cp scripts/udev/99-opticon-scanner.rules "
                       "/etc/udev/rules.d/ && sudo udevadm control --reload-rules, "
                       "then re-plug the scanner")
        return
    except OSError as exc:
        results.record(f"open {node} (065A:A001, HID keyboard mode)", False,
                       str(exc))
        return
    results.record(f"open {node} (065A:A001, HID keyboard mode)", True,
                   "scans arrive as boot-keyboard reports")
    assembler = ScanAssembler()
    try:
        def next_scan(t: float):
            deadline = time.monotonic() + t
            while time.monotonic() < deadline:
                remaining = max(0.0, deadline - time.monotonic())
                ready, _, _ = select.select([fd], [], [], min(1.0, remaining))
                if not ready:
                    continue
                report = os.read(fd, 64)
                if not report:
                    continue
                text = assembler.feed(report[1:])
                if text is not None:
                    return text

        run_scan_tests(next_scan, results, timeout=timeout,
                       expected_qr=expected_qr, repeat=repeat)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# Connection check + dispatch
# --------------------------------------------------------------------------

def opticon_devices(report: dict) -> list[dict]:
    return [d for d in report.get("devices", [])
            if d.get("vid") == OPTICON_VID]


def wait_for_scanner(wait_s: int) -> dict:
    """Survey now, then poll every 2s until the deadline if asked to wait."""
    report = survey()
    if opticon_devices(report) or wait_s <= 0:
        return report

    print(f"no Opticon device visible yet — waiting up to {wait_s}s "
          "(re-plug the USB cable, watch for the scanner's LED)")
    print("tips: the cable must be a DATA cable (the bundled OPN-2001 cable "
          "is charge-only); if it is in USB-HID mode it enumerates as "
          "065A:A001 with no serial port — that is testable too.")
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(2)
        report = survey()
        if opticon_devices(report):
            print("scanner appeared!")
            return report
        sys.stdout.write(".")
        sys.stdout.flush()
    print()
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Test the Opticon M-10 scanner: 1D barcode and QR reads, "
                    "plus the USB-COM command channel when available")
    ap.add_argument("--wait", type=int, default=0, metavar="SEC",
                    help="poll for the device up to SEC seconds before giving up")
    ap.add_argument("--timeout", type=float, default=30.0, metavar="SEC",
                    help="per-scan wait window (default: 30)")
    ap.add_argument("--repeat", type=int, default=1, metavar="N",
                    help="also re-scan the QR target N times and require "
                         "identical payloads (consistency check)")
    ap.add_argument("--any-qr", action="store_true",
                    help="scan any QR of your own instead of the generated "
                         "target (content classified, not compared)")
    ap.add_argument("--svg", metavar="PATH",
                    help="also save the generated QR target as an SVG file")
    ap.add_argument("--no-command-channel", action="store_true",
                    help="skip the Z1/Z2 command-channel test (USB-COM only)")
    args = ap.parse_args(argv)

    print("=" * 60)
    print("1) CONNECTION CHECK")
    print("=" * 60)
    report = wait_for_scanner(args.wait)
    for g in report.get("guidance", []):
        print(f"hint: {g}")
    devs = opticon_devices(report)
    if not devs:
        print("\nNo Opticon scanner is visible to this OS — nothing to test.\n"
              "  · check the cable is a DATA cable and the scanner is powered\n"
              "  · survey the host:  uv run python scripts/opticon_detect.py\n"
              "  · then re-run with --wait, e.g. "
              "uv run python scripts/m10_scan_test.py --wait 60")
        return 2

    # Prefer the M-10 personalities; anything else gets an honest pointer.
    devs.sort(key=lambda d: 0 if d["pid"] in M10_PIDS else 1)
    dev = devs[0]
    pid, info = dev["pid"], describe(dev["pid"])
    for other in devs[1:]:
        o = describe(other["pid"])
        print(f"note: a second Opticon device is attached (065A:"
              f"{other['pid']:04X}, {o['name']}) — testing only the first")
    if pid not in M10_PIDS:
        print(f"\nAttached: 065A:{pid:04X} — {info['name']}.\n"
              f"That is not an M-10 personality; its functional test is "
              f"{info.get('receive_with') or 'scripts/scanner_test.py'}.")
        return 2
    nodes = dev.get("nodes") or []
    print(f"\nAttached: 065A:{pid:04X} {info['name']} "
          f"({M10_PIDS[pid]} mode), nodes: {', '.join(nodes) or 'none'}")

    # -- QR test target -----------------------------------------------------
    expected_qr = None
    if not args.any_qr:
        from app.vendor.qrcodegen import QrCode

        expected_qr = build_qr_payload()
        qr = QrCode.encode_text(expected_qr, QrCode.Ecc.MEDIUM)
        print("\n" + "=" * 60)
        print("2) QR TEST TARGET")
        print("=" * 60)
        print("Scan this QR code from the screen for LIVE TEST 2. If the "
              "scanner cannot read it, enlarge the terminal/font or use "
              "--svg / --any-qr.\n")
        print(render_ascii_qr(qr))
        print(f"\nexpected payload: {expected_qr!r}")
        if args.svg:
            with open(args.svg, "w", encoding="utf-8") as f:
                f.write(qr_svg(qr))
            print(f"SVG copy saved to {args.svg}")

    results = Results()
    print("\n" + "=" * 60)
    print(f"{'3' if expected_qr or args.any_qr else '2'}) FUNCTIONAL TESTS")
    print("=" * 60)
    if not nodes:
        results.record("device node", False,
                       "enumerated but no kernel node bound — missing driver "
                       "or permissions (docs/opticon-hardware.md)")
        return results.summary()
    if pid == 0xA002:
        run_serial(nodes[0], results, expected_qr=expected_qr,
                   timeout=args.timeout, repeat=args.repeat,
                   command_channel=not args.no_command_channel)
    else:  # 0xA001 — HID
        run_hid(nodes[0], results, expected_qr=expected_qr,
                timeout=args.timeout, repeat=args.repeat)
    return results.summary()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
