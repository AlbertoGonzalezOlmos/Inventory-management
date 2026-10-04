"""Unit tests for the OPN-2001 protocol implementation (scripts/opn2001.py).

Everything here runs offline: the CRC/frame known-answer vectors were
cross-verified against three independent implementations of the Opticon
"RBBV" serial protocol (PyOPN, open-opn, OPN-Device-application — see
docs/scanner-opn2001.md §Provenance), so a regression in our CRC or framing
fails these tests without any hardware attached.
"""

import datetime as dt
import io
import struct

import pytest

from scripts.opn2001 import (
    OPN2001,
    OP_CLEAR,
    OP_GET_DATA,
    OP_GET_PARAM,
    OP_GET_TIME,
    OP_INTERROGATE,
    OP_POWER_DOWN,
    OP_SET_TIME,
    Scan,
    build_frame,
    crc16,
    csv_line,
    pack_timestamp,
    parse_time_payload,
    read_frame,
    read_scans,
    set_time_payload,
    unpack_timestamp,
)


# --- CRC-16 known-answer vectors (raw frames published by the reference
# implementations; verified byte-for-byte) ----------------------------------

KNOWN_FRAMES = {
    OP_INTERROGATE: "0102009FDE",  # wake / interrogate
    OP_CLEAR: "0202009F2E",  # clear all scans
    OP_GET_DATA: "0702009E3E",  # download all scans
    OP_GET_TIME: "0A02005DAF",  # read the clock
    OP_POWER_DOWN: "0502005E9F",  # switch the scanner off
}


@pytest.mark.parametrize("opcode,expected_hex", sorted(KNOWN_FRAMES.items()))
def test_build_frame_matches_reference_vectors(opcode, expected_hex):
    assert build_frame(opcode).hex().upper() == expected_hex


def test_crc16_known_answers():
    # CRC over the three header bytes of each reference frame.
    assert crc16(bytes.fromhex("010200")) == 0x9FDE
    assert crc16(bytes.fromhex("070200")) == 0x9E3E
    assert crc16(bytes.fromhex("0A02005DAF"[:-4])) == 0x5DAF


def test_build_frame_set_time_reference_vector():
    """PyOPN documents a set-time frame ending in the CRC bytes 34 83."""
    # 09 02 06 | 00:01:07 02-03-(20)00 | 00 | 34 83
    frame = build_frame(OP_SET_TIME, bytes([0, 1, 7, 2, 3, 0]))
    assert frame.hex().upper() == "090206000107020300003483"


def test_build_frame_with_payload_includes_pad_and_crc():
    frame = build_frame(OP_GET_PARAM, bytes([0x02]))
    # 08 02 | len=1 | 02 | pad 00 | crc
    assert frame == bytes.fromhex("0802010200266B")
    assert len(frame) == 3 + 1 + 1 + 2


def test_build_frame_rejects_oversized_payload():
    with pytest.raises(ValueError):
        build_frame(0x03, b"x" * 32)


def test_csv_line_quotes_comma_barcode():
    # scan_intake.py reads these lines with csv.reader; a Code 128/Data Matrix
    # payload may contain a comma, which must stay one field.
    assert csv_line("2026-09-29 10:00:00", "Code 128", "BOX, LARGE") == \
        '2026-09-29 10:00:00,Code 128,"BOX, LARGE"'
    assert csv_line("2026-09-29 10:00:00", "EAN-13", "8412345678905") == \
        "2026-09-29 10:00:00,EAN-13,8412345678905"


# --- Frame round-trip --------------------------------------------------------


class _Loopback(io.RawIOBase):
    """A scripted stream: writes are captured, reads drain a fixed buffer
    (like a serial port, a read() may return fewer bytes than requested —
    here one byte at a time to exercise _read_exact's reassembly)."""

    def __init__(self, responses: list[bytes]):
        self.sent = b""
        self._buffer = b"".join(responses)

    def write(self, data):
        self.sent += data
        return len(data)

    def read(self, n=-1):
        if not self._buffer:
            return b""
        if n is None or n < 0:
            n = len(self._buffer)
        chunk, self._buffer = self._buffer[:n], self._buffer[n:]
        return chunk


def test_read_frame_roundtrip_and_crc_check():
    payload = bytes([5, 30, 14, 15, 6, 25])  # 2025-06-15 14:30:05
    response = build_frame(0x06, payload)
    stream = _Loopback([response])
    opcode, received = read_frame(stream)
    assert opcode == 0x06
    assert received == payload


def test_read_frame_rejects_corrupted_crc():
    response = bytearray(build_frame(0x06, bytes([5, 30, 14, 15, 6, 25])))
    response[-1] ^= 0xFF
    stream = _Loopback([bytes(response)])
    from scripts.opn2001 import ProtocolError

    with pytest.raises(ProtocolError, match="CRC mismatch"):
        read_frame(stream)


# --- Timestamps ---------------------------------------------------------------


def test_pack_timestamp_known_value():
    # 2025-06-15 14:30:05 — expected bitfield computed longhand (independent
    # of pack_timestamp) so a bit-position regression cannot hide.
    expected = (
        25                      # year - 2000, bits 0-5
        + (6 << 6)              # month, bits 6-9
        + (15 << 10)            # day, bits 10-14
        + (14 << 15)            # hour, bits 15-19
        + (30 << 20)            # minute, bits 20-25
        + (5 << 26)             # second, bits 26-31
    )
    got = pack_timestamp(dt.datetime(2025, 6, 15, 14, 30, 5))
    assert got == expected == 367476121
    assert struct.pack(">I", got).hex().upper() == "15E73D99"


def test_timestamp_roundtrip_across_field_boundaries():
    for when in (
        dt.datetime(2000, 1, 1, 0, 0, 0),  # all-minimum
        dt.datetime(2063, 12, 31, 23, 59, 59),  # all-maximum (6-bit year)
        dt.datetime(2025, 6, 15, 14, 30, 5),
    ):
        assert unpack_timestamp(pack_timestamp(when)) == when


def test_pack_timestamp_rejects_out_of_range_year():
    with pytest.raises(ValueError):
        pack_timestamp(dt.datetime(1999, 1, 1))
    with pytest.raises(ValueError):
        pack_timestamp(dt.datetime(2064, 1, 1))


def test_time_payload_order_is_sec_min_hour_day_month_year():
    assert set_time_payload(dt.datetime(2025, 6, 15, 14, 30, 5)) == bytes(
        [5, 30, 14, 15, 6, 25]
    )
    assert parse_time_payload(bytes([5, 30, 14, 15, 6, 25])) == dt.datetime(
        2025, 6, 15, 14, 30, 5
    )


# --- Scan records -------------------------------------------------------------


def _record(symbology_id: int, barcode: str, when: dt.datetime) -> bytes:
    body = (
        bytes([symbology_id])
        + barcode.encode()
        + struct.pack(">I", pack_timestamp(when))
    )
    return bytes([len(body)]) + body


def test_read_scans_parses_records_and_terminator():
    when = dt.datetime(2025, 6, 15, 14, 30, 5)
    records = _record(0x03, "EX-001", when) + _record(0x0B, "4006381333931", when)
    device_id = 0x1122334455667788
    body = bytes([0x06, 0x02]) + struct.pack(">Q", device_id) + records + b"\x00"
    crc = struct.pack(">H", crc16(body))
    stream = _Loopback([body + crc])

    got_id, scans = read_scans(stream)
    assert got_id == device_id
    assert [s.barcode for s in scans] == ["EX-001", "4006381333931"]
    assert scans[0].symbology == "Code 128"
    assert scans[1].symbology == "EAN-13"
    assert scans[0].timestamp == when


def test_read_scans_empty_memory():
    device_id = 1
    body = bytes([0x06, 0x02]) + struct.pack(">Q", device_id) + b"\x00"
    crc = struct.pack(">H", crc16(body))
    stream = _Loopback([body + crc])
    got_id, scans = read_scans(stream)
    assert got_id == device_id
    assert scans == []


def test_scan_symbology_lookup_unknown_id():
    assert Scan("X", 0xFF, dt.datetime(2025, 1, 1)).symbology == "unknown (0xFF)"


# --- Device-level flows against a scripted stream -----------------------------


def test_interrogate_parses_device_id_and_firmware():
    payload = b"\x00" + struct.pack(">Q", 0xA1B2C3D4E5F60718) + b"RBBV1.00 "
    stream = _Loopback([build_frame(0x06, payload)])
    info = OPN2001(stream).interrogate()
    assert info["device_id"] == 0xA1B2C3D4E5F60718
    assert info["firmware"] == "RBBV1.00"
    assert stream.sent == build_frame(OP_INTERROGATE)


def test_clear_sends_reference_frame_and_reads_ack():
    stream = _Loopback([build_frame(0x06)])  # 5-byte ack
    OPN2001(stream).clear()
    assert stream.sent.hex().upper() == "0202009F2E"


def test_get_data_sends_reference_frame():
    when = dt.datetime(2025, 6, 15, 14, 30, 5)
    body = (
        bytes([0x06, 0x02])
        + struct.pack(">Q", 1)
        + _record(0x03, "EX-001", when)
        + b"\x00"
    )
    stream = _Loopback([body + struct.pack(">H", crc16(body))])
    _, scans = OPN2001(stream).get_data()
    assert stream.sent.hex().upper() == "0702009E3E"
    assert [s.barcode for s in scans] == ["EX-001"]


# --- Raw-USB backend (pyusb path for WSL2 kernels without `opticon`) ---------
#
# The wire behaviour these tests pin comes from drivers/usb/serial/opticon.c
# (fetched from kernel.org): writes are vendor control requests
# (bmRequestType 0x41, bRequest 0x01, wValue=wIndex=0, frame as data stage);
# bulk-in packets carry a 2-byte header (00 00 data / 00 01 CTS); open
# sequence sends CONTROL_RTS(0) and RESEND_CTS_STATE(1).

from scripts.opn2001 import (  # noqa: E402  (grouped with the section)
    USB_BULK_PACKET_SIZE,
    USB_CONTROL_RTS,
    USB_RESEND_CTS_STATE,
    USB_WRITE_REQUEST,
    USB_WRITE_REQUEST_TYPE,
    UsbStream,
    parse_bulk_packet,
)


def test_usb_constants_match_kernel_driver():
    # USB_DIR_OUT | USB_TYPE_VENDOR | USB_RECIP_INTERFACE
    assert USB_WRITE_REQUEST_TYPE == 0x41
    assert USB_WRITE_REQUEST == 0x01        # dr->bRequest in opticon_write
    assert USB_CONTROL_RTS == 0x02          # CONTROL_RTS
    assert USB_RESEND_CTS_STATE == 0x03     # RESEND_CTS_STATE
    assert USB_BULK_PACKET_SIZE == 64       # full-speed bulk max packet


def test_parse_bulk_packet_headers():
    assert parse_bulk_packet(b"\x00\x00ABC") == ("data", b"ABC")
    assert parse_bulk_packet(b"\x00\x01\x01") == ("cts", b"\x01")
    assert parse_bulk_packet(b"\x00\x02XYZ") == ("unknown", b"\x00\x02XYZ")
    assert parse_bulk_packet(b"\x00") == ("unknown", b"\x00")
    assert parse_bulk_packet(b"") == ("unknown", b"")


class _FakeTimeout(Exception):
    pass


class _FakeUsbError(Exception):
    pass


class _FakeEndpoint:
    """Stands in for a pyusb endpoint: yields queued bulk packets."""

    def __init__(self, packets):
        self.packets = list(packets)
        self.bEndpointAddress = 0x81

    def read(self, size, timeout=None):
        if not self.packets:
            raise _FakeTimeout()
        pkt = self.packets.pop(0)
        assert len(pkt) <= size, "reader must request full-speed packet size"
        return pkt


class _FakeDevice:
    def __init__(self):
        self.ctrl_calls = []

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex, data,
                      timeout=None):
        self.ctrl_calls.append((bmRequestType, bRequest, wValue, wIndex,
                                bytes(data)))


def _usb_stream_with(packets) -> UsbStream:
    """A UsbStream whose __init__ (device discovery) is bypassed; only the
    read/write paths under test are wired to the fakes."""
    stream = UsbStream.__new__(UsbStream)
    stream.dev = _FakeDevice()
    stream.ep_in = _FakeEndpoint(packets)
    stream.interface = 0
    stream.timeout_ms = 1
    stream.cts = None
    stream._buf = b""
    stream._timeout_exc = _FakeTimeout
    stream._usb_error = _FakeUsbError
    return stream


def test_usb_write_is_one_vendor_control_request():
    stream = _usb_stream_with([])
    frame = build_frame(OP_INTERROGATE)
    assert stream.write(frame) == len(frame)
    assert stream.dev.ctrl_calls == [
        (0x41, 0x01, 0, 0, frame),
    ]


def test_usb_read_strips_headers_spans_packets_and_tracks_cts():
    # A 23-byte interrogate reply arrives split across bulk packets, with a
    # CTS status packet interleaved — the protocol layer must never see the
    # headers or the CTS noise.
    payload = b"\x00" + struct.pack(">Q", 42) + b"RBBV1.00"
    response = build_frame(0x06, payload)
    chunks = [response[i:i + 7] for i in range(0, len(response), 7)]
    packets = [b"\x00\x00" + c for c in chunks]
    packets.insert(2, b"\x00\x01\x01")  # CTS went high mid-stream
    stream = _usb_stream_with(packets)
    info = OPN2001(stream).interrogate()
    assert info["device_id"] == 42
    assert info["firmware"] == "RBBV1.00"
    assert stream.cts is True


def test_usb_read_timeout_returns_empty_like_pyserial():
    stream = _usb_stream_with([])
    assert stream.read(5) == b""  # _read_exact turns this into ProtocolError
