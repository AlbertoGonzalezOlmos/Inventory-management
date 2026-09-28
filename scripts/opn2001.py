#!/usr/bin/env python3
"""opn2001 — connect to and interact with the Opticon OPN-2001 pocket memory scanner.

The protocol layer (frames, CRC-16, timestamps, barcode records) is stdlib-only
and unit-tested offline (tests/test_opn2001.py). pyserial is imported lazily,
only when a subcommand actually opens the port, so the module stays importable
in any environment:

    uv run --with pyserial python scripts/opn2001.py info
    uv run --with pyserial python scripts/opn2001.py read --json

Protocol background, hardware operation and troubleshooting live in
docs/scanner-opn2001.md. Every raw frame below is a known-answer vector
cross-verified against three independent implementations (PyOPN, open-opn,
OPN-Device-application) — see the doc's "Provenance" section.

Serial settings: 9600 baud, 8 data bits, ODD parity, 1 stop bit.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import struct
import sys
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

VID_PID = (0x065A, 0x0009)  # Opticon OPN-2001 (USB-VCP)

DEFAULT_PORT = "/dev/ttyUSB0"  # Linux: the in-kernel `opticon` usb-serial driver
SERIAL_SETTINGS = dict(baudrate=9600, bytesize=8, parity="O", stopbits=1)

# Command opcodes (TX). The second byte of every frame is always 0x02.
OP_INTERROGATE = 0x01  # wake the link; reply carries device id + firmware
OP_CLEAR = 0x02  # delete ALL scans from scanner memory
OP_SET_PARAM = 0x03  # payload: [param_id, value]
OP_POWER_DOWN = 0x05  # switch the scanner off
OP_GET_DATA = 0x07  # download all scans
OP_GET_PARAM = 0x08  # payload: [param_id]
OP_SET_TIME = 0x09  # payload: [sec, min, hour, day, month, year-2000]
OP_GET_TIME = 0x0A

# Device parameters (see docs/scanner-opn2001.md §Parameters for the full table).
PARAMS = {
    "volume": 0x02,  # buzzer volume: 0=off .. 5=softest
    "reject_redundant": 0x04,  # 0=all allowed, 1=no two consecutive, 2=all unique
    "low_battery_indication": 0x07,
    "code128_enable": 0x08,
    "upc_enable": 0x09,
    "host_connect_beep": 0x0A,
    "host_complete_beep": 0x0B,
    "auto_clear": 0x0F,  # clear memory automatically after an upload
    "scanner_on_time": 0x11,  # 1–10 s in 100 ms units (e.g. 30 = 3 s)
    "code39_enable": 0x1F,
    "delete_enable": 0x21,  # bit0=clear-all, bit1=delete-one, bit2=factory-reset
    "max_barcode_length": 0x22,
    "store_rtc": 0x23,  # store a scan timestamp per barcode
    "scratch_pad": 0x26,
    "good_decode_led_duration": 0x1E,
    "toggle_buzzer": 0x55,  # whether a 10 s Scan press toggles the buzzer
    "buzzer_enable": 0x57,
}

# Symbology IDs as reported in downloaded scan records.
SYMBOLOGIES = {
    0x01: "Code 39",
    0x02: "Codabar",
    0x03: "Code 128",
    0x04: "D25",
    0x05: "IATA",
    0x06: "ITF",
    0x07: "Code 93",
    0x08: "UPC-A",
    0x09: "UPC-E",
    0x0A: "EAN-8",
    0x0B: "EAN-13",
    0x0C: "Code 11",
    0x0E: "MSI",
    0x0F: "EAN-128",
    0x10: "UPC-E1",
    0x11: "PDF-417",
    0x13: "Code 39 Full ASCII",
    0x15: "Trioptic Code 39",
    0x16: "Bookland",
    0x17: "Coupon",
    0x19: "ISBT-128",
    0x1B: "Data Matrix",
    0x1C: "QR code",
    0x1D: "Composite",
    0x1E: "Postnet (US)",
    0x20: "Code 32",
    0x21: "ISBT-128 concatenated",
    0x22: "Postal (Japan)",
    0x23: "Postal (Australia)",
    0x24: "Signature",
    0x26: "Postbar (Canada)",
    0x27: "Postal (UK)",
    0x28: "Macro PDF",
    0x30: "RSS-14",
    0x31: "RSS Limited",
    0x32: "RSS Expanded",
    0x48: "UPC-A+2",
    0x49: "UPC-E+2",
    0x4A: "EAN-8+2",
    0x4B: "EAN-13+2",
    0x50: "UPC-E1+2",
    0x88: "UPC-A+5",
    0x89: "UPC-E+5",
    0x8A: "EAN-8+5",
    0x8B: "EAN-13+5",
    0x90: "UPC-E1+5",
}


# --------------------------------------------------------------------------
# CRC-16 (Opticon "RBBV" stream)
# --------------------------------------------------------------------------
# poly 0xA001 (reflected 0x8005) · init 0xFFFF · no reflection of result
# (the table walk is already LSB-first) · final XOR 0xFFFF · sent big-endian.
# Known-answer vectors are pinned in tests/test_opn2001.py.

def _crc_table() -> list[int]:
    table = []
    for i in range(256):
        crc = 0
        byte = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (byte ^ crc) & 1 else crc >> 1
            byte >>= 1
        table.append(crc)
    return table


_CRC_TABLE = _crc_table()


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc = _CRC_TABLE[byte ^ (crc & 0xFF)] ^ (crc >> 8)
    return (~crc) & 0xFFFF


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------
# Request frame (all commands):
#     byte      opcode
#     byte      0x02 (constant)
#     byte      (flags << 5) | payload_length   (payload ≤ 31 bytes)
#     byte[len] payload
#     byte      0x00 pad (only when len > 0)
#     uint16be  CRC-16 of everything above
#
# Responses use the same shape, EXCEPT the reply to GET DATA (0x07), which
# carries: opcode, 0x02, 8-byte device id, then records until a 0-length
# record, then the CRC (no length byte, no pad — the terminator acts as one).

def build_frame(opcode: int, payload: bytes = b"") -> bytes:
    if len(payload) > 31:
        raise ValueError("payload longer than 31 bytes")
    data = bytes([opcode, 0x02, len(payload) & 0x1F]) + payload
    if payload:
        data += b"\x00"
    crc = crc16(data)
    return data + bytes([crc >> 8, crc & 0xFF])


class ProtocolError(Exception):
    pass


def _read_exact(stream, n: int, timeout_desc: str) -> bytes:
    """Read n bytes from a pyserial-like stream (has .read(n))."""
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            raise ProtocolError(
                f"timed out waiting for {timeout_desc} "
                f"({len(buf)}/{n} bytes received)"
            )
        buf += chunk
    return buf


def read_frame(stream) -> tuple[int, bytes]:
    """Read one length-prefixed response frame; returns (opcode, payload).

    Verifies the CRC and raises ProtocolError on a mismatch.
    """
    opcode, _const = _read_exact(stream, 2, "frame header")
    length = _read_exact(stream, 1, "length byte")[0] & 0x1F
    payload = _read_exact(stream, length, f"payload of frame 0x{opcode:02x}")
    if length:
        _read_exact(stream, 1, "pad byte")
    crc_rx = struct.unpack(">H", _read_exact(stream, 2, "CRC"))[0]
    body = bytes([opcode, _const, length]) + payload + (b"\x00" if length else b"")
    if crc16(body) != crc_rx:
        raise ProtocolError(
            f"CRC mismatch in response frame 0x{opcode:02x}: "
            f"received {crc_rx:04X}, computed {crc16(body):04X}"
        )
    return opcode, payload


# --------------------------------------------------------------------------
# Scan timestamps
# --------------------------------------------------------------------------
# Downloaded records carry a 4-byte big-endian packed timestamp:
#     bits 0-5   year - 2000     bits 15-19  hour (0-23)
#     bits 6-9   month (1-12)    bits 20-25  minute (0-59)
#     bits 10-14 day (1-31)      bits 26-31  second (0-59)

def unpack_timestamp(value: int) -> dt.datetime:
    try:
        return dt.datetime(
            2000 + (value & 0x3F),
            (value >> 6) & 0x0F,
            (value >> 10) & 0x1F,
            (value >> 15) & 0x1F,
            (value >> 20) & 0x3F,
            (value >> 26) & 0x3F,
        )
    except ValueError as exc:
        raise ProtocolError(f"invalid packed timestamp 0x{value:08X}: {exc}") from exc


def pack_timestamp(when: dt.datetime) -> int:
    if not (2000 <= when.year <= 2063):
        raise ValueError("OPN-2001 RTC range is 2000-2063")
    return (
        (when.year - 2000)
        | (when.month << 6)
        | (when.day << 10)
        | (when.hour << 15)
        | (when.minute << 20)
        | (when.second << 26)
    )


def set_time_payload(when: dt.datetime) -> bytes:
    """Set-time payload: six plain BCD-less bytes, sec first."""
    return bytes(
        [when.second, when.minute, when.hour, when.day, when.month, when.year - 2000]
    )


def parse_time_payload(payload: bytes) -> dt.datetime:
    sec, minute, hour, day, month, year = payload[:6]
    return dt.datetime(2000 + year, month, day, hour, minute, sec)


# --------------------------------------------------------------------------
# Scans
# --------------------------------------------------------------------------


@dataclass
class Scan:
    barcode: str
    symbology_id: int
    timestamp: dt.datetime

    @property
    def symbology(self) -> str:
        return SYMBOLOGIES.get(self.symbology_id, f"unknown (0x{self.symbology_id:02X})")


def read_scans(stream) -> tuple[int, list[Scan]]:
    """Read the reply to GET DATA: device id + records until a 0-length record.

    The trailing CRC is verified when present, but a mismatch degrades to a
    warning rather than an error: the published implementations disagree on
    whether the CRC covers the device id (see docs §Provenance).
    """
    opcode, _const = _read_exact(stream, 2, "data response header")
    device_id = struct.unpack(">Q", _read_exact(stream, 8, "device id"))[0]

    scans: list[Scan] = []
    while True:
        length = _read_exact(stream, 1, "record length")[0]
        if length == 0:
            break
        if length < 5:
            raise ProtocolError(f"record length {length} < minimum 5")
        record = _read_exact(stream, length, "scan record")
        symbology_id = record[0]
        barcode = record[1:-4].decode("ascii", errors="replace")
        timestamp = unpack_timestamp(struct.unpack(">I", record[-4:])[0])
        scans.append(Scan(barcode, symbology_id, timestamp))

    # Optional trailing CRC over the whole stream (header, id, records, 0x00).
    crc_rx = stream.read(2)
    if len(crc_rx) == 2:
        body = bytes([opcode, _const]) + struct.pack(">Q", device_id)
        for s in scans:
            rec = (
                bytes([s.symbology_id])
                + s.barcode.encode("ascii", errors="replace")
                + struct.pack(">I", pack_timestamp(s.timestamp))
            )
            body += bytes([len(rec)]) + rec
        body += b"\x00"
        expected = struct.unpack(">H", crc_rx)[0]
        if crc16(body) != expected:
            print(
                f"warning: data-response CRC mismatch (received {expected:04X}, "
                f"computed {crc16(body):04X}) — records kept",
                file=sys.stderr,
            )
    return device_id, scans


# --------------------------------------------------------------------------
# Device API (a live serial stream)
# --------------------------------------------------------------------------


class OPN2001:
    def __init__(self, stream, timeout_desc: str = "scanner"):
        self.stream = stream
        self.timeout_desc = timeout_desc

    def interrogate(self) -> dict:
        """Wake the link and read device id + firmware version."""
        self.stream.write(build_frame(OP_INTERROGATE))
        opcode, payload = read_frame(self.stream)
        # payload: 1 pad byte, 8-byte device id, 8-byte firmware string
        device_id = struct.unpack(">Q", payload[1:9])[0]
        firmware = payload[9:17].decode("ascii", errors="replace").strip()
        return {"device_id": device_id, "firmware": firmware}

    def get_time(self) -> dt.datetime:
        self.stream.write(build_frame(OP_GET_TIME))
        opcode, payload = read_frame(self.stream)
        return parse_time_payload(payload)

    def set_time(self, when: dt.datetime | None = None) -> dt.datetime:
        when = when or dt.datetime.now()
        self.stream.write(build_frame(OP_SET_TIME, set_time_payload(when)))
        opcode, payload = read_frame(self.stream)
        return parse_time_payload(payload)

    def get_param(self, param_id: int) -> int:
        self.stream.write(build_frame(OP_GET_PARAM, bytes([param_id])))
        opcode, payload = read_frame(self.stream)
        return payload[1]

    def set_param(self, param_id: int, value: int) -> int:
        self.stream.write(build_frame(OP_SET_PARAM, bytes([param_id, value])))
        opcode, payload = read_frame(self.stream)
        return payload[1]

    def get_data(self) -> tuple[int, list[Scan]]:
        self.stream.write(build_frame(OP_GET_DATA))
        return read_scans(self.stream)

    def clear(self) -> None:
        """Delete ALL scans from the scanner. Cannot be undone."""
        self.stream.write(build_frame(OP_CLEAR))
        read_frame(self.stream)  # 5-byte ack

    def power_down(self) -> None:
        self.stream.write(build_frame(OP_POWER_DOWN))
        try:
            read_frame(self.stream)
        except ProtocolError:
            pass  # the device may just switch off


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _open_port(port: str):
    try:
        import serial  # pyserial, imported lazily
    except ImportError:
        sys.exit(
            "pyserial is required to talk to the scanner:\n"
            "  uv run --with pyserial python scripts/opn2001.py ... "
            "(or: pip install pyserial)"
        )
    return serial.Serial(port=port, timeout=3, **SERIAL_SETTINGS)


def _find_port() -> str:
    """Default port: $OPN2001_PORT, else /dev/ttyUSB0 (Linux kernel driver)."""
    import os

    port = os.environ.get("OPN2001_PORT")
    if port:
        return port
    if sys.platform.startswith("linux"):
        return DEFAULT_PORT
    sys.exit(
        "No default port on this platform — pass --port (Windows: the COMx "
        "number from Device Manager; see docs/scanner-opn2001.md §Connecting)."
    )


def _resolve_param(name: str) -> int:
    if name in PARAMS:
        return PARAMS[name]
    if re.fullmatch(r"0x[0-9a-fA-F]{1,2}|\d{1,3}", name):
        return int(name, 0)
    sys.exit(f"unknown parameter {name!r} — known: {', '.join(sorted(PARAMS))}")


def cmd_info(dev: OPN2001, args) -> int:
    info = dev.interrogate()
    print(f"device id:    {info['device_id']}")
    print(f"firmware:     {info['firmware']}")
    print(f"scanner time: {dev.get_time().isoformat(sep=' ')}")
    print(f"scans stored: {len(dev.get_data()[1])}")
    return 0


def cmd_time(dev: OPN2001, args) -> int:
    print(dev.get_time().isoformat(sep=" "))
    return 0


def cmd_set_time(dev: OPN2001, args) -> int:
    when = dt.datetime.fromisoformat(args.datetime) if args.datetime else dt.datetime.now()
    print(f"scanner time set to: {dev.set_time(when).isoformat(sep=' ')}")
    print("(set store_rtc=1 to timestamp scans — see param command)")
    return 0


def cmd_read(dev: OPN2001, args) -> int:
    device_id, scans = dev.get_data()
    if args.json:
        print(
            json.dumps(
                {
                    "device_id": device_id,
                    "count": len(scans),
                    "scans": [
                        {
                            "barcode": s.barcode,
                            "symbology": s.symbology,
                            "symbology_id": s.symbology_id,
                            "timestamp": s.timestamp.isoformat(),
                        }
                        for s in scans
                    ],
                },
                indent=2,
            )
        )
    else:
        for s in scans:
            print(f"{s.timestamp.isoformat(sep=' ')},{s.symbology},{s.barcode}")
        print(f"# {len(scans)} scan(s) from device {device_id}", file=sys.stderr)
    if args.clear_after:
        dev.clear()
        print("# scanner memory cleared (--clear-after)", file=sys.stderr)
    return 0


def cmd_clear(dev: OPN2001, args) -> int:
    if not args.yes:
        confirm = input("Delete ALL scans from the scanner? [y/N] ")
        if confirm.strip().lower() != "y":
            print("aborted")
            return 1
    dev.clear()
    print("scanner memory cleared")
    return 0


def cmd_param(dev: OPN2001, args) -> int:
    param_id = _resolve_param(args.param)
    if args.value is None:
        print(f"{args.param} (0x{param_id:02X}) = {dev.get_param(param_id)}")
    else:
        value = int(args.value, 0)
        readback = dev.set_param(param_id, value)
        status = "ok" if readback == value else f"READBACK MISMATCH (got {readback})"
        print(f"{args.param} (0x{param_id:02X}) = {value}: {status}")
        if param_id == PARAMS["volume"] and value == 0:
            print("note: the buzzer can also be toggled by holding Scan for 10 s")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Connect to an Opticon OPN-2001 pocket memory scanner "
        "(protocol: see docs/scanner-opn2001.md)"
    )
    parser.add_argument(
        "--port", default=_find_port(), help="serial device (default: %(default)s)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="wake the link; show device id, firmware, clock, scan count") \
        .set_defaults(func=cmd_info)
    sub.add_parser("time", help="read the scanner clock").set_defaults(func=cmd_time)
    p = sub.add_parser("set-time", help="set the scanner clock (default: now)")
    p.add_argument("--datetime", help="ISO datetime, e.g. 2025-06-15T14:30:05")
    p.set_defaults(func=cmd_set_time)

    p = sub.add_parser("read", help="download all scans (does NOT clear by default)")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--clear-after", action="store_true",
                   help="clear scanner memory after a successful download")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("clear", help="delete ALL scans from the scanner")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("param", help="get or set a device parameter")
    p.add_argument("param", help="name (e.g. volume) or numeric id (e.g. 0x02)")
    p.add_argument("value", nargs="?", help="value to set (omit to read)")
    p.set_defaults(func=cmd_param)

    args = parser.parse_args(argv)
    with _open_port(args.port) as stream:
        dev = OPN2001(stream, timeout_desc=args.port)
        return args.func(dev, args)


if __name__ == "__main__":
    sys.exit(main())
