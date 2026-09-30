"""Tests for app/barcodes.py — GTIN validation, normalisation, classification.

Known-answer vectors come from published worked examples:
* Wikipedia "International Article Number" (check digit): EAN-13 4006381333931
  (Stabilo Point 88) and EAN-8 7351353x / 96385074.
* Wikipedia "Universal Product Code": UPC-A 036000291452; UPC-E expansion
  examples 425261 → 042100005264 (figure caption) and 654321 → 065100004327
  / 165100004324 (number system 0/1).
* Wikipedia "Global Trade Item Number": the 14-digit padding table and the
  ISBN Bookland 978/979 rule.
* GS1 prefix ranges: Wikipedia "List of GS1 country codes" (mirrors the GS1
  company-prefix registry).
"""

import pytest

from app.barcodes import (
    BarcodeInfo,
    analyze_scan,
    check_digit,
    classify_gtin,
    is_valid_gtin,
    normalize_gtin,
    split_addon,
    upce_to_upca,
)


# --- GS1 modulo-10 check digit ---------------------------------------------

@pytest.mark.parametrize("data,expected", [
    ("400638133393", "1"),   # EAN-13, Stabilo Point 88 (Wikipedia example)
    ("9638507", "4"),        # EAN-8 96385074 (GS1 example)
    ("03600029145", "2"),    # UPC-A classic example
    ("978030640615", "7"),   # Bookland ISBN-13 9780306406157
    ("1400638133393", "8"),  # GTIN-14 with indicator 1
])
def test_check_digit_known_answers(data, expected):
    assert check_digit(data) == expected


def test_check_digit_rejects_non_digits():
    with pytest.raises(ValueError):
        check_digit("84A2")


@pytest.mark.parametrize("code", [
    "4006381333931", "96385074", "036000291452", "9780306406157",
    "14006381333938",  # GTIN-14
])
def test_is_valid_gtin(code):
    assert is_valid_gtin(code)


@pytest.mark.parametrize("code", [
    "4006381333932",   # check digit off by one
    "841234567890",    # GTIN-12 length with a failing check digit
    "123",             # too short
    "EX-001",          # not digits
    "",                # empty
])
def test_is_valid_gtin_rejects(code):
    assert not is_valid_gtin(code)


# --- UPC-E expansion ---------------------------------------------------------

def test_upce_expansion_wikipedia_vectors():
    # Figure caption: "UPC-A 042100005264 is equivalent to UPC-E 425261"
    assert upce_to_upca("425261") == "042100005264"
    # Body example: "UPC-E 654321 … UPC-A 065100004327 or 165100004324"
    assert upce_to_upca("654321", "0") == "065100004327"
    assert upce_to_upca("654321", "1") == "165100004324"


@pytest.mark.parametrize("payload,body11", [
    ("123450", "01200000345"),  # d6=0: NS d1d2 000 00 d3d4d5
    ("123451", "01210000345"),  # d6=1: NS d1d2 100 00 d3d4d5
    ("123452", "01220000345"),  # d6=2: NS d1d2 200 00 d3d4d5
    ("123453", "01230000045"),  # d6=3: NS d1d2d3 00 000 d4d5
    ("123454", "01234000005"),  # d6=4: NS d1d2d3d4 0 0000 d5
    ("123455", "01234500005"),  # d6=5..9: NS d1..d5 0000 d6
    ("123459", "01234500009"),
])
def test_upce_expansion_table(payload, body11):
    result = upce_to_upca(payload)
    assert result[:11] == body11
    assert is_valid_gtin(result)          # check digit recomputed
    assert len(result) == 12


def test_upce_8_digit_transmission_form():
    # NS + payload + check digit (scanner preamble parameter on)
    assert upce_to_upca("04252614") == "042100005264"
    with pytest.raises(ValueError):        # wrong check digit for preamble form
        upce_to_upca("04252615")
    with pytest.raises(ValueError):        # bad number system
        upce_to_upca("94252614")


# --- add-on supplements ------------------------------------------------------

def test_split_addon():
    assert split_addon("8412345678905 52495", "EAN-13+5") == ("8412345678905", "52495")
    assert split_addon("841234567890552495", "EAN-13+5") == ("8412345678905", "52495")
    assert split_addon("03600029145212", "UPC-A+2") == ("036000291452", "12")
    assert split_addon("4006381333931", "EAN-13") == ("4006381333931", "")
    # opaque payloads keep their spaces
    assert split_addon("BOX 12", "Code 128") == ("BOX 12", "")


# --- normalisation -----------------------------------------------------------

def test_normalize_pads_to_gtin14():
    assert normalize_gtin("4006381333931") == "04006381333931"  # EAN-13
    assert normalize_gtin("96385074") == "00000096385074"        # EAN-8
    assert normalize_gtin("036000291452") == "00036000291452"    # UPC-A
    assert normalize_gtin("14006381333938") == "14006381333938"  # GTIN-14


def test_normalize_upce_needs_symbology():
    assert normalize_gtin("425261", "UPC-E") == "00042100005264"
    assert normalize_gtin("04252614", "UPC-E") == "00042100005264"
    # Without the scanner's word a bare 6-digit payload is NOT expanded
    # (expansion always self-validates, so guessing would invent GTINs).
    assert normalize_gtin("425261") is None


def test_normalize_addons_and_ai_prefix():
    assert normalize_gtin("4006381333931 51", "EAN-13+2") == "04006381333931"
    assert normalize_gtin("400638133393152495", "EAN-13+5") == "04006381333931"
    assert normalize_gtin("(01)04006381333931", "EAN-128") == "04006381333931"


@pytest.mark.parametrize("code", [
    "4006381333932",   # bad check digit
    "EX-001",          # SKU label
    "12345",           # implausible length
    "841234567890123", # 15 digits
    "",
])
def test_normalize_rejects(code):
    assert normalize_gtin(code) is None


# --- classification (number systems) -----------------------------------------

def _info(code: str, symbology: str = "") -> BarcodeInfo:
    gtin = normalize_gtin(code, symbology)
    assert gtin is not None, code
    return classify_gtin(gtin, orig_len=len(code))


def test_classify_regions():
    assert _info("4006381333931").region == "Germany"           # 400
    assert _info("8412345678905").region == "Spain & Andorra"   # 841
    assert _info("9780306406157").usage == "book"               # 978 Bookland
    assert _info("5901234123457").region == "Poland"            # 590
    info = _info("036000291452")
    assert info.format == "GTIN-12"
    assert "United States" in info.region                       # 00x UPC-A


def test_classify_restricted_circulation():
    # EAN-13 prefix 2x: in-store / region-restricted
    body = "241234567890"
    info = _info(body + check_digit(body))
    assert info.usage == "restricted-region"
    assert info.globally_unique is False and info.in_store is True
    # UPC-A number system 4: company-restricted (loyalty/in-store)
    body = "41234500000"
    info = _info(body + check_digit(body))
    assert info.usage == "restricted-company"
    assert not info.globally_unique
    # UPC-A number system 2: variable weight, region-restricted
    body = "21234500000"
    info = _info(body + check_digit(body))
    assert info.usage == "restricted-region"
    assert not info.globally_unique


def test_classify_special_usages():
    body = "977123456700"            # ISSN serial
    info = _info(body + check_digit(body))
    assert info.usage == "serial"
    body = "990123456789"            # coupon
    info = _info(body + check_digit(body))
    assert info.usage == "coupon" and not info.globally_unique


def test_classify_gtin8_and_rcn8():
    info = _info("96385074")
    assert info.format == "GTIN-8" and info.globally_unique
    body = "2012345"                 # RCN-8: store own-brand (first digit 2)
    info = _info(body + check_digit(body))
    assert info.format == "GTIN-8"
    assert info.usage == "restricted-company" and not info.globally_unique


def test_classify_gtin14_indicator():
    info = _info("14006381333938")
    assert info.format == "GTIN-14" and info.indicator == "1"
    body = "9400638133393"           # indicator 9: variable measure
    info = classify_gtin(body + check_digit(body), orig_len=14)
    assert info.indicator == "9"
    assert any("variable measure" in n for n in info.notes)


def test_classify_rejects_bad_input():
    with pytest.raises(ValueError):
        classify_gtin("123")


# --- whole-scan analysis -------------------------------------------------------

def test_analyze_scan_kinds():
    a = analyze_scan("4006381333931", "EAN-13")
    assert (a.kind, a.key) == ("gtin", "04006381333931")

    a = analyze_scan("2412345678901", "EAN-13")
    assert a.kind == "restricted" and not a.info.globally_unique

    a = analyze_scan("4006381333932", "EAN-13")
    assert a.kind == "invalid-gtin" and a.key == ""

    a = analyze_scan("EX-001", "Code 128")
    assert (a.kind, a.key) == ("opaque", "EX-001")

    # An opaque symbology wins over digit heuristics: a Code 128 payload that
    # happens to validate as a GTIN is still the printer's identifier, not GS1's.
    a = analyze_scan("4006381333931", "Code 128")
    assert a.kind == "opaque" and a.key == "4006381333931"

    # Unknown symbology + valid GTIN digits → still recognised as a GTIN.
    a = analyze_scan("4006381333931", "")
    assert a.kind == "gtin"

    assert analyze_scan("", "EAN-13").kind == "empty"


def test_analyze_scan_strips_addon_but_keeps_identity():
    a = analyze_scan("4006381333931 51", "EAN-13+2")
    assert a.kind == "gtin" and a.key == "04006381333931" and a.addon == "51"
