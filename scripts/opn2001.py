#!/usr/bin/env python3
"""opn2001 — connect to and interact with the Opticon OPN-2001 pocket memory scanner.

The protocol layer (frames, CRC-16, timestamps, barcode records) is stdlib-only
and unit-tested offline (tests/test_opn2001.py). pyserial / pyusb are imported
lazily, only when a subcommand actually opens the device, so the module stays
importable in any environment:

    uv run --with pyserial python scripts/opn2001.py info
    uv run --with pyserial python scripts/opn2001.py read --json

Two backends can carry the protocol:

* ``serial`` (default) — the kernel's opticon usb-serial driver exposes the
  scanner as ``/dev/ttyUSB*`` (Linux) or the Opticon driver as ``COMx``
  (Windows).
* ``usb`` — raw libusb via pyusb, reimplementing what the kernel driver does
  (writes as vendor control requests — the device has no bulk-out endpoint;
  bulk-in packets carry a 2-byte header). This is the backend for WSL2 and
  other kernels that ship without CONFIG_USB_SERIAL_OPTICON:

      uv run --with pyusb python scripts/opn2001.py --backend usb info

``scripts/opn2001.py detect`` finds the scanner and explains what stands
between it and a working connection on this machine.

Protocol background, hardware operation and troubleshooting live in
docs/scanner-opn2001.md. Every raw frame below is a known-answer vector
cross-verified against three independent implementations (PyOPN, open-opn,
OPN-Device-application) — see the doc's "Provenance" section.

Serial settings: 9600 baud, 8 data bits, ODD parity, 1 stop bit.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import struct
import sys
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

VID_PID = (0x065A, 0x0009)  # Opticon OPN-2001 (USB-VCP)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.opticon_detect import (  # noqa: E402  (repo root on sys.path)
    describe,
    find_linux_usb_devices,
    find_windows_ports,
    is_wsl,
    probe_windows_pnp,
    usbipd_attached,
    wsl_kernel_has_opticon,
)

DEFAULT_PORT = "/dev/ttyUSB0"  # Linux: the in-kernel `opticon` usb-serial driver
SERIAL_SETTINGS = dict(baudrate=9600, bytesize=8, parity="O", stopbits=1)

# Raw-USB backend constants — mirroring drivers/usb/serial/opticon.c:
# writes go out as vendor control requests (bmRequestType 0x41, bRequest 0x01,
# wValue/wIndex 0, the whole frame as the transfer buffer) because the device
# has no bulk-out endpoint; bulk-in transfers arrive with a 2-byte header
# (00 00 = data, 00 01 = CTS line-state change); open sequences send
# CONTROL_RTS(0) then RESEND_CTS_STATE(1) and clear the bulk-in halt.
USB_WRITE_REQUEST_TYPE = 0x41  # USB_DIR_OUT | USB_TYPE_VENDOR | USB_RECIP_INTERFACE
USB_WRITE_REQUEST = 0x01
USB_CONTROL_RTS = 0x02
USB_RESEND_CTS_STATE = 0x03
USB_BULK_PACKET_SIZE = 64  # full-speed bulk max packet; keeps header boundaries

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
# Raw-USB backend (pyusb) — for kernels without the `opticon` module (WSL2)
# --------------------------------------------------------------------------


def parse_bulk_packet(packet: bytes) -> tuple[str, bytes]:
    """Interpret one bulk-in transfer the way drivers/usb/serial/opticon.c does.

    Returns ("data", payload) for header 00 00, ("cts", level) for header
    00 01, or ("unknown", packet) for anything else / malformed short packets.
    """
    if len(packet) <= 2:
        return ("unknown", packet)
    if packet[0] == 0x00 and packet[1] == 0x00:
        return ("data", packet[2:])
    if packet[0] == 0x00 and packet[1] == 0x01:
        return ("cts", packet[2:])
    return ("unknown", packet)


class UsbStream:
    """pyserial-shaped stream (.read(n)/.write()/.close()) over raw libusb.

    Reimplements the kernel opticon driver's behaviour (see constants above):
    every host→scanner frame goes out as ONE vendor control transfer; every
    device→host bulk packet gets its 2-byte header stripped (CTS packets are
    tracked, not delivered). read(n) blocks until n bytes are available or
    the timeout expires, then returns whatever was accumulated — exactly the
    contract the protocol layer's _read_exact expects (b"" means timeout).
    """

    def __init__(self, vid_pid=VID_PID, timeout_ms: int = 3000):
        try:
            import usb.core
            import usb.util
        except ImportError:
            raise SystemExit(
                "pyusb is required for --backend usb:\n"
                "  uv run --with pyusb python scripts/opn2001.py --backend usb ...\n"
                "(also needs libusb-1.0 on the system: apt install libusb-1.0-0)"
            )
        self._usb_util = usb.util
        # Stash the exception classes so read() needs no repeated lazy import
        # (and tests can inject stand-ins when __init__ is bypassed).
        self._timeout_exc = usb.core.USBTimeoutError
        self._usb_error = usb.core.USBError
        dev = usb.core.find(idVendor=vid_pid[0], idProduct=vid_pid[1])
        if dev is None:
            raise SystemExit(
                f"no USB device {vid_pid[0]:04x}:{vid_pid[1]:04x} found — is the "
                "scanner plugged in and enumerated? Run: scripts/opn2001.py detect"
            )
        self.dev = dev
        self.timeout_ms = timeout_ms
        self.cts: bool | None = None
        self._buf = b""

        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
                self._detached = True
        except Exception:
            self._detached = False
        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass  # already configured
        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]
        self.interface = intf.bInterfaceNumber
        ep_in = next(
            e for e in intf
            if self._usb_util.endpoint_direction(e.bEndpointAddress)
            == self._usb_util.ENDPOINT_IN
        )
        self.ep_in = ep_in
        try:
            self._control_msg(USB_CONTROL_RTS, 0)  # clear RTS (as on open)
            try:
                dev.reset_endpoint(ep_in.bEndpointAddress)  # clear halt
            except (usb.core.USBError, NotImplementedError, AttributeError):
                pass
            self._control_msg(USB_RESEND_CTS_STATE, 1)  # ask for CTS state
        except usb.core.USBError as exc:
            raise SystemExit(f"USB handshake failed: {exc}")

    def _control_msg(self, request: int, val: int) -> None:
        """send_control_msg() from opticon.c: bmRequestType 0x41, bRequest =
        request, wValue = wIndex = 0, and the value travels in a 1-byte data
        stage (NOT in wValue)."""
        self.dev.ctrl_transfer(
            USB_WRITE_REQUEST_TYPE, request, 0, self.interface,
            bytes([val]), self.timeout_ms,
        )

    def write(self, data: bytes) -> int:
        """Send a whole frame as one vendor control request (opticon_write)."""
        self.dev.ctrl_transfer(
            USB_WRITE_REQUEST_TYPE, USB_WRITE_REQUEST, 0, self.interface,
            data, self.timeout_ms,
        )
        return len(data)

    def read(self, n: int) -> bytes:
        """Accumulate n payload bytes from header-stripped bulk packets."""
        out = b""
        while len(self._buf) + len(out) < n:
            try:
                packet = self.ep_in.read(USB_BULK_PACKET_SIZE, self.timeout_ms)
            except self._timeout_exc:
                break
            except self._usb_error:
                break
            kind, payload = parse_bulk_packet(bytes(packet))
            if kind == "data":
                out += payload
            elif kind == "cts":
                self.cts = bool(payload[:1] and payload[0])
        result = (self._buf + out)[:n]
        self._buf = (self._buf + out)[n:]
        return result

    def close(self) -> None:
        try:
            self._usb_util.release_interface(self.dev, self.interface)
            if getattr(self, "_detached", False):
                self.dev.attach_kernel_driver(self.interface)
        except Exception:
            pass
        self._usb_util.dispose_resources(self.dev)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# --------------------------------------------------------------------------
# Device detection — the OPN-2001 report
#
# The device-agnostic primitives (sysfs/COM/PnP discovery, the Opticon USB
# personality map, the WSL2 probes) live in scripts/opticon_detect.py so that
# every tool in this repo answers "what is attached?" from one map. What stays
# here is the OPN-2001-specific report: whether *this* model is reachable, and
# what to do about it (kernel module, --backend usb, usbipd, problem 43).
# --------------------------------------------------------------------------


def detect_device() -> dict:
    """Collect everything needed to reach the scanner on this machine."""
    report: dict = {"platform": sys.platform, "wsl": False, "found": False,
                    "usable": False, "candidates": [], "guidance": []}
    if sys.platform.startswith("linux"):
        report["wsl"] = is_wsl()
        devs = find_linux_usb_devices()
        opn = [d for d in devs if d["pid"] == VID_PID[1]]
        report["opn_enumerated"] = bool(opn)
        for d in opn:
            report["candidates"].append({"backend": "serial", **d})
            if d["ttys"]:
                report["found"] = report["usable"] = True
        if opn and not any(d["ttys"] for d in opn):
            report["found"] = True
            report["guidance"].append(
                "device enumerated but no /dev/ttyUSB*: the kernel lacks the "
                "opticon usb-serial driver — use --backend usb (pyusb) or a "
                "kernel with CONFIG_USB_SERIAL_OPTICON=m"
            )
        # Other Opticon personalities (the shared map owns their identity and
        # its provenance): HID keyboard (a001) / CDC-ACM (a002). This tool does
        # not speak their protocols — it reports them so the operator is not
        # left guessing why an OPN-2001 command times out.
        for d in devs:
            info = describe(d["pid"])
            if d["pid"] == 0xA001:
                report["found"] = True
                report["candidates"].append({"backend": "hidraw", **d})
                if d["hidraws"]:
                    report["usable"] = True
                    report["guidance"].append(
                        f"Opticon scanner in USB-HID keyboard mode on "
                        f"{', '.join(d['hidraws'])} — NOT an OPN-2001 (no RBBV "
                        f"protocol here); receive scans with "
                        f"scripts/scanner_hid.py. Identity "
                        f"{'documented' if info['confirmed'] else 'OBSERVED, unconfirmed'}"
                        f" — docs/opticon-hardware.md §1"
                    )
                else:
                    report["guidance"].append(
                        "Opticon HID-mode scanner enumerated but no hidraw "
                        "node (kernel lacks CONFIG_HIDRAW?)"
                    )
            elif d["pid"] == 0xA002:
                report["found"] = True
                report["candidates"].append({"backend": "serial", **d})
                if d["ttys"]:
                    report["usable"] = True
                    report["guidance"].append(
                        f"Opticon scanner in USB-COM (CDC-ACM) mode on "
                        f"{', '.join(d['ttys'])} — NOT an OPN-2001: a plain "
                        f"serial line, not the RBBV protocol "
                        f"(docs/opticon-hardware.md §1)"
                    )
            elif not info["known"]:
                report["found"] = True
                report["guidance"].append(
                    f"unrecognised Opticon device 065A:{d['pid']:04X} — not in "
                    f"the hardware map; do not guess a protocol for it"
                )
        if report["wsl"]:
            has_bus = usbipd_attached()
            report["usbipd_attached"] = has_bus
            report["wsl_kernel_opticon"] = wsl_kernel_has_opticon()
            if not has_bus:
                report["guidance"].append(
                    "WSL2 sees no USB devices: the scanner is attached to the "
                    "Windows host. Pass it through with usbipd-win (admin "
                    "PowerShell): usbipd bind --busid <id> ; then "
                    "usbipd attach --wsl --busid <id> — see docs/opticon-hardware.md §2"
                )
            elif report.get("opn_enumerated") and report["wsl_kernel_opticon"] is False:
                report["guidance"].append(
                    "this WSL2 kernel has no CONFIG_USB_SERIAL_OPTICON — the "
                    "attached device will not get a /dev/ttyUSB*; use "
                    "--backend usb (uv run --with pyusb …), which talks raw "
                    "libusb exactly like the kernel driver would"
                )
        pnp = probe_windows_pnp() if report["wsl"] else None
        if pnp is not None:
            report["windows_pnp"] = pnp
            if not report["found"] and pnp:
                report["found"] = True
                for entry in pnp:
                    status = (entry.get("Status") or "").lower()
                    name = entry.get("FriendlyName") or ""
                    if status == "error" or "Descriptor Request Failed" in name:
                        report["guidance"].append(
                            "Windows reports a FAILED USB enumeration "
                            f"({name!r}): the scanner is not talking to the "
                            "host at all. Usual causes: deeply-discharged "
                            "battery (leave it plugged in 30+ min; a charging "
                            "device shows a steady red LED) or a charge-only "
                            "mini-B cable (try a known-good DATA cable)"
                        )
                    elif "vid_065a" in (entry.get("InstanceId") or "").lower():
                        report["guidance"].append(
                            "scanner enumerated on Windows — install the "
                            "Opticon driver (docs §3.2) and use the COMx port, "
                            "or attach it to WSL with usbipd"
                        )
    elif sys.platform == "win32":
        ports = find_windows_ports(pid=VID_PID[1])
        report["candidates"] += [{"backend": "serial", **p} for p in ports]
        if ports:
            report["found"] = report["usable"] = True
        pnp = probe_windows_pnp()
        if pnp is not None:
            report["windows_pnp"] = pnp
            if any((e.get("Status") or "").lower() == "error" for e in pnp):
                report["found"] = True
                report["guidance"].append(
                    "a USB device is failing to enumerate (Device Manager → "
                    "'Unknown USB Device (Device Descriptor Request Failed)'): "
                    "charge the scanner 30+ min on a DATA cable and re-plug"
                )
        if not report["found"]:
            report["guidance"].append(
                "scanner not seen: check the cable/port, and install the "
                "Opticon USB driver BEFORE plugging in (docs §3.2; survey "
                "the host with scripts/opticon_detect.py)"
            )
    else:  # macOS and friends
        report["guidance"].append(
            "no vendor driver exists for the OPN-2001's vendor interface on "
            "macOS (docs §3.3) — use a Linux host/VM with USB passthrough"
        )
    if report.get("opn_enumerated") and not report["usable"] \
            and sys.platform.startswith("linux") and os.path.isdir("/dev/bus/usb"):
        # The OPN-2001 is enumerated (or usbipd-attached) but has no tty:
        # raw libusb can still drive it (--backend usb).
        report["usable"] = True
        report["candidates"].append({"backend": "usb", "vid_pid": "%04x:%04x" % VID_PID})
    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _open_serial(port: str):
    try:
        import serial  # pyserial, imported lazily
    except ImportError:
        sys.exit(
            "pyserial is required to talk to the scanner over a serial port:\n"
            "  uv run --with pyserial python scripts/opn2001.py ... "
            "(or: pip install pyserial)"
        )
    return serial.Serial(port=port, timeout=3, **SERIAL_SETTINGS)


def _open_stream(args):
    """Open the connection for the chosen backend ('auto' picks one)."""
    backend = args.backend
    if backend == "auto":
        if args.port and os.path.exists(args.port):
            backend = "serial"
        elif sys.platform == "win32" and args.port:
            backend = "serial"  # COMx: existence is not a file test
        else:
            backend = "usb"
    if backend == "serial":
        if not args.port:
            sys.exit("--backend serial needs --port (or $OPN2001_PORT)")
        return _open_serial(args.port)
    return UsbStream()


def _default_port() -> str | None:
    """Default port: $OPN2001_PORT, else /dev/ttyUSB0 if it exists, else None
    (None lets --backend auto fall through to raw USB)."""
    port = os.environ.get("OPN2001_PORT")
    if port:
        return port
    if sys.platform.startswith("linux") and os.path.exists(DEFAULT_PORT):
        return DEFAULT_PORT
    if sys.platform == "win32":
        ports = find_windows_ports(pid=VID_PID[1])
        if ports:
            return ports[0]["port"]
    return None


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


def csv_line(timestamp: str, symbology: str, barcode: str) -> str:
    """One CSV record ``timestamp,symbology,barcode``, quoted as needed.

    Uses ``csv.writer`` so a barcode containing a comma (legal in Code 128,
    Data Matrix, …) stays one field — ``scan_intake.py`` reads these lines
    with ``csv.reader`` and would otherwise split it into extra columns.
    """
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow([timestamp, symbology, barcode])
    return buf.getvalue().rstrip("\n")


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
            print(csv_line(s.timestamp.isoformat(sep=" "), s.symbology, s.barcode))
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


def cmd_detect(dev, args) -> int:
    """Report where the scanner is and what stands between us and it."""
    report = detect_device()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"platform: {report['platform']}" + (" (WSL2)" if report.get("wsl") else ""))
        if report["candidates"]:
            for c in report["candidates"]:
                if c["backend"] == "hidraw":
                    print(f"Opticon HID-mode scanner on {c['sysfs']} → "
                          f"{', '.join(c['hidraws']) or '(no hidraw node)'} "
                          f"(scripts/scanner_hid.py)")
                elif c["backend"] == "serial" and c.get("ttys"):
                    print(f"scanner on {c['sysfs']} → {', '.join(c['ttys'])} (serial backend)")
                elif c["backend"] == "serial" and "port" in c:
                    print(f"serial port candidate: {c['port']} ({c.get('description', '')})")
                elif c["backend"] == "serial":
                    print(f"scanner enumerated at {c['sysfs']} but no tty bound")
                else:
                    print(f"raw-USB candidate: {c.get('vid_pid', '')} (--backend usb)")
        else:
            print("no serial port / USB interface for 065a:0009 available yet")
        if "usbipd_attached" in report:
            print(f"usbipd USB bus visible in WSL: {report['usbipd_attached']}")
        if report.get("wsl_kernel_opticon") is not None:
            print(f"WSL kernel has opticon module: {report['wsl_kernel_opticon']}")
        for entry in report.get("windows_pnp") or []:
            print("windows PnP: [{Status}] {FriendlyName} ({InstanceId})".format(**entry))
        for line in report["guidance"]:
            print(f"→ {line}")
        print(f"found: {report['found']}  usable: {report['usable']}")
    return 0 if report["usable"] else (1 if report["found"] else 2)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Connect to an Opticon OPN-2001 pocket memory scanner "
        "(protocol: see docs/scanner-opn2001.md)"
    )
    parser.add_argument(
        "--port", default=_default_port(),
        help="serial device (default: %(default)s; not needed for --backend usb)",
    )
    parser.add_argument(
        "--backend", choices=("auto", "serial", "usb"), default="auto",
        help="serial = kernel/VCP COM port (pyserial); usb = raw libusb "
        "(pyusb, for WSL2 & kernels without the opticon module); "
        "auto picks serial when a port exists (default: %(default)s)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("detect", help="find the scanner and diagnose the connection path")
    p.add_argument("--json", action="store_true", help="machine-readable report")
    p.set_defaults(func=cmd_detect, no_device=True)

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
    if getattr(args, "no_device", False):
        return args.func(None, args)
    with _open_stream(args) as stream:
        dev = OPN2001(stream, timeout_desc=args.port or "usb")
        return args.func(dev, args)


if __name__ == "__main__":
    sys.exit(main())
