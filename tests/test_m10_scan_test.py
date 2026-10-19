"""Tests for scripts/m10_scan_test.py — offline, known-answer.

The validation and rendering helpers are pure; the live-scan flow is driven
through a fake ``next_scan`` so the PASS/FAIL dispatch is exercised without
hardware (same trick the scanner tests use: inject the transport boundary).
"""

import pytest

from app.vendor.qrcodegen import QrCode
from scripts.m10_scan_test import (
    Results,
    build_qr_payload,
    classify_qr_content,
    qr_svg,
    render_ascii_qr,
    run_scan_tests,
    validate_1d,
    validate_qr,
)

# Known-good EAN-13 (prefix 590 = Poland): check digit verified by
# app.barcodes.is_valid_gtin at write time.
EAN13 = "5901234123457"
EAN13_BAD = "5901234123458"  # same digits, broken check digit


def make_qr(payload="HCRM-SCANTEST:AF12CD"):
    return QrCode.encode_text(payload, QrCode.Ecc.MEDIUM)


# --- 1D validation ----------------------------------------------------------

def test_validate_1d_valid_gtin():
    ok, detail = validate_1d(EAN13)
    assert ok, detail
    assert "GTIN-13" in detail
    assert "Poland" in detail
    assert "retail" in detail


def test_validate_1d_check_digit_failure_is_a_fail():
    """A misread must never pass: the catalogue treats these as MISREAD."""
    ok, detail = validate_1d(EAN13_BAD)
    assert not ok
    assert "check digit" in detail


def test_validate_1d_opaque_internal_label_passes():
    ok, detail = validate_1d("SKU-1234")
    assert ok, detail
    assert "opaque" in detail


def test_validate_1d_empty_fails():
    assert validate_1d("")[0] is False
    assert validate_1d("   ")[0] is False


# --- QR validation ----------------------------------------------------------

def test_validate_qr_exact_match():
    ok, detail = validate_qr("HCRM-SCANTEST:AF12CD", "HCRM-SCANTEST:AF12CD")
    assert ok, detail
    assert "exact match" in detail


def test_validate_qr_wrong_payload_fails():
    ok, detail = validate_qr("HCRM-SCANTEST:FFFFFF", "HCRM-SCANTEST:AF12CD")
    assert not ok
    assert "misdecode" in detail or "!=" in detail


def test_validate_qr_any_mode_classifies():
    ok, detail = validate_qr("https://example.com/x")
    assert ok, detail
    assert "URL" in detail


def test_validate_qr_any_mode_flags_gtin_shaped_payload():
    """An all-digit payload may be the item's 1D barcode scanned by mistake —
    not a failure (QRs can carry GTINs), but it must be flagged."""
    ok, detail = validate_qr(EAN13)
    assert ok
    assert "1D barcode" in detail


def test_validate_qr_empty_fails():
    assert validate_qr("", "expected")[0] is False
    assert validate_qr("   ")[0] is False


# --- QR content classification ---------------------------------------------

@pytest.mark.parametrize("payload,kind", [
    ("https://hcrm.local/login", "URL"),
    ("HTTP://EXAMPLE.COM", "URL"),
    ("WIFI:T:WPA;S:ssid;P:pass;;", "Wi-Fi network config"),
    ("mailto:a@b.c", "contact/geo link"),
    ("geo:41.39,2.17", "contact/geo link"),
    ("MECARD:N:Doe,John;;", "contact card"),
    ('{"sku": "X1"}', "JSON object"),
    ("]d2010123456789012", "GS1 element string"),
    ("(01)01234567890128", "GS1 element string"),
    ("hello world", "plain text"),
])
def test_classify_qr_content(payload, kind):
    assert classify_qr_content(payload) == kind


# --- QR target generation and rendering -------------------------------------

def test_build_qr_payload_shape_and_uniqueness():
    a, b = build_qr_payload(), build_qr_payload()
    for p in (a, b):
        assert p.startswith("HCRM-SCANTEST:")
        assert len(p) == 20            # keeps the symbol at version 1
    assert a != b


def test_render_ascii_qr_geometry():
    """One row per module line, two cells per module, quiet zone included."""
    qr = make_qr()
    size = qr.get_size()
    art = render_ascii_qr(qr, border=4)
    rows = art.split("\n")
    assert len(rows) == size + 8                    # 2 * quiet zone
    assert all(len(r) == 2 * (size + 8) for r in rows)
    assert set(art) <= {"█", " ", "\n"}             # block/space only
    assert rows[0] == "  " * (size + 8)             # quiet zone is blank


def test_render_ascii_qr_contains_the_matrix():
    qr = make_qr()
    art = render_ascii_qr(qr, border=4)
    # First data row (row 4) must reflect module row 0, not just padding.
    row = art.split("\n")[4]
    expected = "".join("██" if qr.get_module(x, 0) else "  "
                       for x in range(qr.get_size()))
    assert row == "        " + expected + "        "  # 4-module quiet zone


def test_qr_svg_well_formed():
    svg = qr_svg(make_qr())
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg
    assert svg.count("<rect") >= 2                  # background + modules
    assert "<script" not in svg                     # CSP-safe, like qrbadge


# --- the live-scan flow, transport faked -------------------------------------

def test_run_scan_tests_all_pass():
    results = Results()
    scans = iter([EAN13, "HCRM-SCANTEST:AF12CD",
                  "HCRM-SCANTEST:AF12CD", "HCRM-SCANTEST:AF12CD"])
    run_scan_tests(lambda t: next(scans, None), results,
                   timeout=0.1, expected_qr="HCRM-SCANTEST:AF12CD", repeat=2)
    assert results.ok
    assert [name for name, _, _ in results.entries] == [
        "1D barcode read", "QR code read", "repeat consistency x2"]


def test_run_scan_tests_timeout_is_recorded_not_raised():
    results = Results()
    run_scan_tests(lambda t: None, results,
                   timeout=0.01, expected_qr=None, repeat=1)
    assert not results.ok
    assert all("nothing in" in detail for _, ok, detail in results.entries if not ok)


def test_run_scan_tests_repeat_detects_inconsistency():
    results = Results()
    scans = iter([EAN13, "HCRM-SCANTEST:AF12CD",
                  "HCRM-SCANTEST:AF12CD", "HCRM-SCANTEST:FFFF00"])
    run_scan_tests(lambda t: next(scans, None), results,
                   timeout=0.1,
                   expected_qr="HCRM-SCANTEST:AF12CD", repeat=2)
    repeat = [e for e in results.entries if e[0].startswith("repeat")]
    assert repeat and not repeat[0][1]
    assert "distinct payload" in repeat[0][2]
