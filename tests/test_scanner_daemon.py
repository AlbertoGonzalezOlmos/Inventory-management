"""Tests for scripts/scanner_daemon.py — the round-4 T2 decode core.

Fully offline (qwen M1: the scanner is not guaranteed to be attached — CI
has none): synthetic ``struct.pack`` event streams drive the decoder, a
fake sysfs tree in ``tmp_path`` drives discovery, and ``read_scans`` is
exercised through an ``os.pipe`` (an already-open fd, exactly the contract
T3 will call it with).

The load-bearing test is the parity property: every usage in
``scanner_hid._USAGE_MAP`` must decode to the same character through the
evdev path (shifted and unshifted) — the executable form of "reuse the
assembler, do not duplicate it".
"""

import os
import struct

import pytest

from scripts.scanner_daemon import (
    EV_KEY,
    EV_SYN,
    KEYCODE_TO_USAGE,
    KEY_0,
    KEY_ENTER,
    KEY_KPENTER,
    KEY_LEFTSHIFT,
    KEY_LINEFEED,
    KEY_RIGHTSHIFT,
    NATIVE_TV_SIZE,
    EvdevDecoder,
    discover_event_device,
    discover_event_devices,
    event_size,
    parse_input_event,
    read_scans,
)
from scripts.scanner_hid import _USAGE_MAP

KEY_TAB = 15  # an unmapped-for-scanners key (input-event-codes.h)

# char -> (keycode, shifted?), built from the shared tables — the inverse
# of what the daemon does, so a mistake in either direction fails a test.
CHAR_TO_KEY = {}
for _kc, _usage in KEYCODE_TO_USAGE.items():
    if _usage in _USAGE_MAP:
        _plain, _shifted = _USAGE_MAP[_usage]
        CHAR_TO_KEY[_plain] = (_kc, False)
        CHAR_TO_KEY[_shifted] = (_kc, True)


def pack_event(type_, code, value, tv_size=16):
    """One input_event as bytes. tv_size 16 (64-bit arch) or 8 (32-bit)."""
    fmt = {16: "=qqHHi", 8: "=iiHHi"}[tv_size]
    return struct.pack(fmt, 0, 0, type_, code, value)


def scan_stream(text, terminator=KEY_ENTER, tv_size=16):
    """Realistic event bytes for typing `text` + one terminator press:
    press/release pairs per key, shift pressed/released around shifted keys
    (exactly what a HID keyboard sends)."""
    events = []
    for ch in text:
        keycode, shifted = CHAR_TO_KEY[ch]
        if shifted:
            events.append((EV_KEY, KEY_LEFTSHIFT, 1))
        events.append((EV_KEY, keycode, 1))
        events.append((EV_KEY, keycode, 0))
        if shifted:
            events.append((EV_KEY, KEY_LEFTSHIFT, 0))
    events.append((EV_KEY, terminator, 1))
    events.append((EV_KEY, terminator, 0))
    return b"".join(pack_event(*e, tv_size=tv_size) for e in events)


def decode(events, on_unmapped=None):
    """Feed (type, code, value) tuples to a fresh decoder; return scans."""
    decoder = EvdevDecoder(on_unmapped=on_unmapped)
    return [s for e in events if (s := decoder.feed(*e)) is not None]


# --- decode: the shape of a scan ---------------------------------------------

def test_full_streams_digits_letters_punctuation():
    for text in ("8412345678905", "EX-007", "sku-42.5/l", "A1B2C3"):
        assert decode(_unpack(scan_stream(text))) == [text]


def test_shifted_characters():
    # '!' = shift+1, ':' = shift+semicolon, '_' = shift+minus, 'A' = shift+a
    assert decode(_unpack(scan_stream("!:_A"))) == ["!:_A"]


def test_right_shift_works_too():
    events = [(EV_KEY, KEY_RIGHTSHIFT, 1), (EV_KEY, 30, 1),  # KEY_A
              (EV_KEY, KEY_RIGHTSHIFT, 0), (EV_KEY, KEY_ENTER, 1)]
    assert decode(events) == ["A"]


def test_full_badge_payload_round_trips():
    token = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_01234"
    assert len(token) == 43
    assert decode(_unpack(scan_stream("HCRM1:" + token))) == ["HCRM1:" + token]


def test_multiple_scans_in_order():
    stream = scan_stream("8412345678905") + scan_stream("EX-007")
    assert decode(_unpack(stream)) == ["8412345678905", "EX-007"]


def test_enter_on_empty_buffer_yields_nothing():
    assert decode([(EV_KEY, KEY_ENTER, 1), (EV_KEY, KEY_ENTER, 0)]) == []


# --- terminators: CR / LF / CRLF ---------------------------------------------

def test_cr_lf_and_keypad_enter_all_terminate():
    for terminator in (KEY_ENTER, KEY_LINEFEED, KEY_KPENTER):
        assert decode(_unpack(scan_stream("EX-007", terminator=terminator))) == ["EX-007"]


def test_crlf_suffix_yields_one_scan():
    # CR then LF: the first completes the scan; the second is an
    # Enter-only report on an empty buffer, absorbed by the assembler.
    stream = scan_stream("EX-007") + pack_event(EV_KEY, KEY_LINEFEED, 1)
    assert decode(_unpack(stream)) == ["EX-007"]


# --- event semantics ----------------------------------------------------------

def test_autorepeat_is_ignored():
    # value=2 is a repeat: a stuck/repeating key must not double a char.
    events = [(EV_KEY, 6, 1), (EV_KEY, 6, 2), (EV_KEY, 6, 2),
              (EV_KEY, KEY_ENTER, 1)]  # KEY_5
    assert decode(events) == ["5"]


def test_releases_and_non_key_types_are_ignored():
    events = [(EV_KEY, 2, 0),                # release without press
              (EV_SYN, 0, 0),                # EV_SYN
              (0x04, 2, 1),                  # EV_MSC
              (EV_KEY, 2, 1), (EV_KEY, 2, 0),
              (EV_KEY, KEY_ENTER, 1)]
    assert decode(events) == ["1"]


def test_shift_state_persists_across_keys_and_clears_on_release():
    events = [(EV_KEY, KEY_LEFTSHIFT, 1),
              (EV_KEY, 2, 1), (EV_KEY, 2, 0),   # '1' → '!'
              (EV_KEY, 3, 1), (EV_KEY, 3, 0),   # '2' → '@'
              (EV_KEY, KEY_LEFTSHIFT, 0),
              (EV_KEY, 2, 1), (EV_KEY, 2, 0),   # '1' plain again
              (EV_KEY, KEY_ENTER, 1)]
    assert decode(events) == ["!@1"]


def test_unmapped_key_resets_the_buffer_and_reports():
    seen = []
    stream = (scan_stream("AB")[:-2 * 24]           # drop AB's Enter pair
              + pack_event(EV_KEY, KEY_TAB, 1)
              + scan_stream("CD"))
    assert decode(_unpack(stream), on_unmapped=seen.append) == ["CD"]
    assert seen == [KEY_TAB]


def test_unmapped_release_does_not_report_twice():
    seen = []
    events = [(EV_KEY, KEY_TAB, 1), (EV_KEY, KEY_TAB, 0),
              (EV_KEY, 2, 1), (EV_KEY, KEY_ENTER, 1)]
    assert decode(events, on_unmapped=seen.append) == ["1"]
    assert seen == [KEY_TAB]


def test_overlapping_key_presses_decode_in_press_order():
    # Rollover at the source: a fast scanner can press the next key BEFORE
    # releasing the previous one (an N-key-rollover HID boot report would
    # carry both in separate slots; evdev decomposes that into individual
    # presses). Press order must be the character order, and the release
    # interleaving must not matter. (The HID ErrorRollOver phantom state
    # — usage 0x01 in every slot — is dropped by the kernel's hid-input
    # and never reaches evdev as keycodes, so there is nothing to absorb
    # at this layer; what must work is the overlap itself.)
    events = [(EV_KEY, 30, 1),   # KEY_A press
              (EV_KEY, 31, 1),   # KEY_S pressed before A is released
              (EV_KEY, 30, 0),   # A released
              (EV_KEY, 31, 0),   # S released
              (EV_KEY, KEY_ENTER, 1)]
    assert decode(events) == ["as"]


def test_overlapping_shift_and_key_presses():
    # Same overlap across the shift boundary: the character is decided by
    # the shift state AT ITS PRESS, not at its release.
    events = [(EV_KEY, KEY_LEFTSHIFT, 1),
              (EV_KEY, 30, 1),   # 'A' (shifted)
              (EV_KEY, KEY_LEFTSHIFT, 0),
              (EV_KEY, 30, 0),   # A released AFTER shift — still 'A'
              (EV_KEY, 30, 1),   # 'a' (unshifted)
              (EV_KEY, 30, 0),
              (EV_KEY, KEY_ENTER, 1)]
    assert decode(events) == ["Aa"]


# --- parity with scanner_hid's usage map (the reuse pin) ----------------------

def test_every_usage_map_entry_decodes_identically_through_evdev():
    # Every printable usage in the shared table must be reachable via a
    # keycode and decode to the same character, shifted and unshifted —
    # the executable form of "reuse the assembler, don't duplicate it".
    covered = {u for u in KEYCODE_TO_USAGE.values() if u in _USAGE_MAP}
    assert covered == set(_USAGE_MAP), "a _USAGE_MAP entry lost its keycode"
    for keycode, usage in KEYCODE_TO_USAGE.items():
        if usage not in _USAGE_MAP:
            continue  # the three terminators (usage 40)
        for shift in (False, True):
            decoder = EvdevDecoder(on_unmapped=None)
            if shift:
                assert decoder.feed(EV_KEY, KEY_LEFTSHIFT, 1) is None
            assert decoder.feed(EV_KEY, keycode, 1) is None
            expected = _USAGE_MAP[usage][1 if shift else 0]
            got = decoder.feed(EV_KEY, KEY_ENTER, 1)
            assert got == expected, f"keycode {keycode} usage {usage} shift={shift}"


def test_digit_keycodes_are_linux_standard():
    # linux/input-event-codes.h: KEY_1=2 … KEY_9=10, KEY_0=11 — pinned so a
    # table typo cannot silently remap a digit.
    assert [KEYCODE_TO_USAGE[c] for c in range(2, 11)] == list(range(30, 39))
    assert KEYCODE_TO_USAGE[KEY_0] == 39
    assert KEYCODE_TO_USAGE[KEY_ENTER] == 40
    assert KEYCODE_TO_USAGE[30] == 4  # KEY_A
    assert KEYCODE_TO_USAGE[57] == 44  # KEY_SPACE


# --- struct parsing: 64/32-bit timeval ----------------------------------------

def test_parse_input_event_both_timeval_sizes():
    for tv_size in (16, 8):
        record = pack_event(EV_KEY, 30, 1, tv_size=tv_size)
        assert len(record) == event_size(tv_size) == tv_size + 8
        assert parse_input_event(record, tv_size=tv_size) == (EV_KEY, 30, 1)


def test_parse_input_event_default_matches_the_running_arch():
    assert NATIVE_TV_SIZE in (8, 16)
    assert event_size() == NATIVE_TV_SIZE + 8
    record = pack_event(EV_KEY, 28, 1, tv_size=NATIVE_TV_SIZE)
    assert parse_input_event(record) == (EV_KEY, 28, 1)


def test_parse_input_event_rejects_short_buffers():
    with pytest.raises(ValueError):
        parse_input_event(b"\x00" * 10, tv_size=16)


# --- discovery against a fake sysfs tree ---------------------------------------

def _make_event_node(root, name, vendor, product, with_id=True):
    entry = root / name
    if with_id:
        ident = entry / "device" / "id"
        ident.mkdir(parents=True)
        (ident / "vendor").write_text(vendor)
        (ident / "product").write_text(product)
    else:
        (entry / "device").mkdir(parents=True)
    return entry


def _input_class(tmp_path):
    root = tmp_path / "class" / "input"
    root.mkdir(parents=True)
    return root


def test_discover_finds_the_scanner(tmp_path):
    root = _input_class(tmp_path)
    _make_event_node(root, "event3", "065a", "a001")
    _make_event_node(root, "event0", "001f", "0001")  # the AT keyboard
    assert discover_event_device(sysfs_root=root) == "/dev/input/event3"
    assert discover_event_devices(sysfs_root=root) == ["/dev/input/event3"]


def test_discover_tolerates_0x_prefix_and_case(tmp_path):
    root = _input_class(tmp_path)
    _make_event_node(root, "event5", "0x065A", "0xA001")
    assert discover_event_device(sysfs_root=root) == "/dev/input/event5"


def test_discover_absent_returns_none(tmp_path):
    root = _input_class(tmp_path)
    _make_event_node(root, "event0", "001f", "0001")
    assert discover_event_device(sysfs_root=root) is None
    assert discover_event_devices(sysfs_root=root) == []


def test_discover_multiple_is_deterministic_and_numeric(tmp_path):
    root = _input_class(tmp_path)
    # A multi-interface device can expose several event nodes; numeric
    # order must win (string order would put event10 before event2).
    for name in ("event10", "event7", "event2"):
        _make_event_node(root, name, "065a", "a001")
    assert discover_event_devices(sysfs_root=root) == [
        "/dev/input/event2", "/dev/input/event7", "/dev/input/event10"]
    assert discover_event_device(sysfs_root=root) == "/dev/input/event2"


def test_discover_skips_unreadable_and_unidentified_nodes(tmp_path):
    root = _input_class(tmp_path)
    _make_event_node(root, "event4", "zzzz", "a001")          # garbage vendor
    _make_event_node(root, "event5", "065a", "a001", with_id=False)  # no id/
    assert discover_event_devices(sysfs_root=root) == []


def test_discover_ignores_non_event_class_entries(tmp_path):
    root = _input_class(tmp_path)
    # /sys/class/input also holds inputN, mice, mouseN — never grabbed.
    _make_event_node(root, "input9", "065a", "a001")
    _make_event_node(root, "mouse0", "065a", "a001")
    assert discover_event_devices(sysfs_root=root) == []


def test_discover_missing_class_dir_returns_empty(tmp_path):
    assert discover_event_devices(sysfs_root=tmp_path / "nowhere") == []
    assert discover_event_device(sysfs_root=tmp_path / "nowhere") is None


# --- read_scans through a real fd (os.pipe, no hardware) -----------------------

def test_read_scans_round_trip_through_a_pipe():
    payload = (scan_stream("8412345678905")
               + scan_stream("EX-007")
               + scan_stream("!:_A"))
    r, w = os.pipe()
    try:
        os.write(w, payload)
        os.close(w)
        assert list(read_scans(r)) == ["8412345678905", "EX-007", "!:_A"]
    finally:
        os.close(r)


def test_read_scans_reassembles_records_split_across_writes():
    stream = scan_stream("EX-007")
    r, w = os.pipe()
    try:
        os.write(w, stream[:10])       # a partial record
        os.write(w, stream[10:24])     # its remainder, in a second read
        os.write(w, stream[24:])       # the rest
        os.close(w)
        assert list(read_scans(r)) == ["EX-007"]
    finally:
        os.close(r)


def test_read_scans_32_bit_timeval_stream():
    r, w = os.pipe()
    try:
        os.write(w, scan_stream("EX-007", tv_size=8))
        os.close(w)
        assert list(read_scans(r, tv_size=8)) == ["EX-007"]
    finally:
        os.close(r)


def test_read_scans_reports_unmapped_keys_and_continues():
    seen = []
    stream = (scan_stream("AB")[:-2 * 24]
              + pack_event(EV_KEY, KEY_TAB, 1)
              + scan_stream("CD"))
    r, w = os.pipe()
    try:
        os.write(w, stream)
        os.close(w)
        assert list(read_scans(r, on_unmapped=seen.append)) == ["CD"]
        assert seen == [KEY_TAB]
    finally:
        os.close(r)


def test_read_scans_ends_cleanly_on_eof():
    r, w = os.pipe()
    os.close(w)
    try:
        assert list(read_scans(r)) == []
    finally:
        os.close(r)


# --- helpers -------------------------------------------------------------------

def _unpack(stream, tv_size=16):
    """input_event bytes → (type, code, value) tuples."""
    size = event_size(tv_size)
    assert len(stream) % size == 0, "test stream is not whole records"
    return [parse_input_event(stream[i:i + size], tv_size=tv_size)
            for i in range(0, len(stream), size)]
