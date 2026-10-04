"""Tests for scripts/scanner_hid.py — HID boot-keyboard report parsing.

Offline: the pure parsing/grouping functions are exercised with synthetic
reports shaped exactly like the ones a keyboard-mode barcode scanner emits
(8-byte boot reports: [modifier, reserved, usage x6]).
"""

import pytest

from scripts.scanner_hid import (
    ScanAssembler,
    format_csv_row,
    group_scans_from_reports,
    parse_keyboard_report,
)


def report(*usages, modifier=0x00):
    """Build an 8-byte boot-keyboard report (report ID already stripped)."""
    keys = list(usages) + [0] * (6 - len(usages))
    return bytes([modifier, 0x00, *keys[:6]])


USAGE = {c: 4 + ord(c.lower()) - ord("a") for c in "abcdefghijklmnopqrstuvwxyz"}
# HID usages 30..38 = '1'..'9', usage 39 = '0'
USAGE.update({str((d + 1) % 10): 30 + d for d in range(10)})
DIGITS_SHIFTED = ")!@#$%^&*("


@pytest.mark.parametrize("char,usage", [
    ("a", USAGE["a"]), ("z", USAGE["z"]),
    ("0", USAGE["0"]), ("9", USAGE["9"]),
    ("5", USAGE["5"]),
])
def test_single_keys(char, usage):
    assert parse_keyboard_report(report(usage)) == (char, False)


def test_shifted_characters():
    # Shift+'1' = '!' ; Shift+'a' = 'A' ; Shift+'0' (usage 39) = ')'
    assert parse_keyboard_report(report(USAGE["1"], modifier=0x02))[0] == "!"
    assert parse_keyboard_report(report(USAGE["a"], modifier=0x20))[0] == "A"
    assert parse_keyboard_report(report(USAGE["0"], modifier=0x02))[0] == ")"


def test_digit_usages_are_hid_standard():
    # 30..38 = '1'..'9', 39 = '0' (HID Usage Tables, keyboard page)
    assert USAGE["1"] == 30 and USAGE["9"] == 38 and USAGE["0"] == 39
    assert parse_keyboard_report(report(39))[0] == "0"


def test_enter_terminates():
    text, enter = parse_keyboard_report(report(40))
    assert (text, enter) == ("", True)


def test_comma_usage_is_supported():
    # Usage 54 is ','; barcodes are not restricted to GTIN characters.
    assert parse_keyboard_report(report(54)) == (",", False)


def test_report_with_text_and_enter_keeps_the_text():
    # A scanner may pack the final character and its CR suffix into one report;
    # the character must not be dropped (previously it was).
    assert parse_keyboard_report(report(USAGE["5"], 40)) == ("5", True)
    assert group_scans_from_reports(
        [report(USAGE["4"]), report(USAGE["5"], 40)]
    ) == ["45"]


def test_scan_assembler_is_the_shared_runtime_path():
    # The live hidraw/hidapi loops feed this same object, so this test covers
    # the code that actually runs (not just a parallel copy of it).
    assembler = ScanAssembler()
    assert assembler.feed(report(USAGE["a"])) is None
    assert assembler.feed(report(40)) == "a"
    assert assembler.feed(report(40)) is None  # empty Enter emits nothing


def test_rollover_and_padding_ignored():
    # ErrorRollOver (0x01 in every slot — phantom state) yields nothing.
    assert parse_keyboard_report(bytes([0, 0, 1, 1, 1, 1, 1, 1])) == ("", False)
    # All-zero report (key released) yields nothing.
    assert parse_keyboard_report(bytes(8)) == ("", False)


def test_multi_key_report():
    assert parse_keyboard_report(report(USAGE["a"], USAGE["b"])) == ("ab", False)


def test_short_and_empty_reports_are_safe():
    assert parse_keyboard_report(b"") == ("", False)
    assert parse_keyboard_report(b"\x00\x00") == ("", False)


def _spell(text: str) -> list[bytes]:
    """Reports a keyboard-mode scanner emits to 'type' text (key + release)."""
    reports = []
    for ch in text:
        modifier = 0x02 if ch.isupper() else 0x00
        base = ch.lower()
        if base == "-":
            usage = 45
        else:
            usage = USAGE[base]
        reports.append(report(usage, modifier=modifier))
        reports.append(bytes(8))  # key release between presses (real devices)
    return reports


def test_group_scans_single():
    reports = _spell("8412345678905") + [report(40)]
    assert group_scans_from_reports(reports) == ["8412345678905"]


def test_group_scans_multiple_and_trailing_partial():
    reports = (
        _spell("EX-001") + [report(40)]
        + _spell("4006381333931") + [report(40)]
        + _spell("partial")            # never terminated → not emitted
    )
    assert group_scans_from_reports(reports) == ["EX-001", "4006381333931"]


def test_group_scans_empty_enter_ignored():
    # Enter with an empty buffer (double suffix, idle scanner) emits nothing.
    assert group_scans_from_reports([report(40), report(40)]) == []


def test_uppercase_uses_shift_modifier():
    # 'E' = shift + usage 0x08; scanners in "uppercase" mode emit it that way.
    reports = _spell("EX1") + [report(40)]
    assert group_scans_from_reports(reports) == ["EX1"]
    assert reports[0][0] == 0x02  # shift modifier set for the uppercase key


def test_csv_row_quotes_separators_and_quotes():
    # A barcode with a comma must stay one CSV field, not split into columns.
    assert format_csv_row("2026-09-29 10:12:03", "A,B") == \
        '2026-09-29 10:12:03,HID-Keyboard,"A,B"'
    assert format_csv_row("t", 'a"b') == 't,HID-Keyboard,"a""b"'
    # Ordinary GTINs are emitted unchanged (no gratuitous quoting).
    assert format_csv_row("t", "8412345678905") == "t,HID-Keyboard,8412345678905"
