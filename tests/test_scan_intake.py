"""Tests for scripts/scan_intake.py — the scanner→catalogue bridge.

Offline: the catalogue is a throwaway SQLite file with the columns the tool
reads; scans come from fixtures in the exact shapes the download tools emit
(opn2001.py --json / CSV, scanner_hid.py CSV).
"""

import json
import os
import sqlite3

import pytest

from scripts.scan_intake import (
    ScanRecord,
    analyze_intake,
    build_report,
    load_catalogue,
    load_csv,
    load_json,
    render_text,
)


@pytest.fixture()
def catalogue_db(tmp_path):
    """A minimal read-only-compatible catalogue with one GTIN and one SKU item."""
    path = tmp_path / "hcrm.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, sku TEXT, name TEXT, "
        "barcode TEXT, stock INTEGER)"
    )
    conn.execute(
        "INSERT INTO items VALUES (1, 'OLIVE-OIL-1L', 'Olive oil 1L', "
        "'08412345678905', 4)"
    )
    conn.execute(
        "INSERT INTO items VALUES (2, 'EX-001', 'Example One', NULL, 7)"
    )
    conn.commit()
    conn.close()
    return str(path)


def _records(*triples):
    return [ScanRecord(*t) for t in triples]


def test_load_json_shape(tmp_path):
    doc = {"device_id": 1, "count": 1,
           "scans": [{"barcode": "8412345678905", "symbology": "EAN-13",
                      "timestamp": "2026-09-29T10:00:00"}]}
    p = tmp_path / "scans.json"
    p.write_text(json.dumps(doc))
    scans = load_json(str(p))
    assert scans == [ScanRecord("8412345678905", "EAN-13", "2026-09-29T10:00:00")]


def test_load_csv_skips_comments_and_handles_quoting(tmp_path):
    p = tmp_path / "scans.csv"
    p.write_text(
        "# 3 scan(s) from device 4711\n"
        "2026-09-29 10:00:00,EAN-13,8412345678905\n"
        '2026-09-29 10:00:01,Code 128,"BOX, LARGE"\n'
    )
    scans = load_csv(str(p))
    assert [s.barcode for s in scans] == ["8412345678905", "BOX, LARGE"]


def test_load_catalogue_indexes(catalogue_db):
    by_gtin, by_sku = load_catalogue(catalogue_db)
    assert by_gtin["08412345678905"].sku == "OLIVE-OIL-1L"
    assert by_sku["EX-001"].id == 2
    # SKU lookup is case-insensitive
    assert by_sku["ex-001".upper()] is by_sku["EX-001"]


def test_load_catalogue_missing_db_is_not_fatal(tmp_path, capsys):
    by_gtin, by_sku = load_catalogue(str(tmp_path / "nope.db"))
    assert by_gtin == {} and by_sku == {}
    assert "warning" in capsys.readouterr().err


def test_same_item_by_gtin_and_duplicates_count(catalogue_db):
    by_gtin, by_sku = load_catalogue(catalogue_db)
    groups = analyze_intake(
        _records(("8412345678905", "EAN-13", "t1"),
                 ("8412345678905", "EAN-13", "t2"),
                 ("8412345678905", "EAN-13", "t3")),
        by_gtin, by_sku,
    )
    assert len(groups) == 1
    g = groups[0]
    assert g.status == "same-item" and g.count == 3
    assert g.item.sku == "OLIVE-OIL-1L"
    assert (g.first_seen, g.last_seen) == ("t1", "t3")


def test_cross_format_match_upce_hits_upca_entry(catalogue_db):
    # The whole point of canonicalisation: a UPC-E scan of the product filed
    # under its UPC-A/EAN-13 GTIN is the SAME item.
    conn = sqlite3.connect(catalogue_db)
    conn.execute("INSERT INTO items VALUES (3, 'UPC-THING', 'x', '00042100005264', 1)")
    conn.commit()
    by_gtin, by_sku = load_catalogue(catalogue_db)
    groups = analyze_intake(
        _records(("04252614", "UPC-E", "t1")), by_gtin, by_sku)
    assert groups[0].status == "same-item"
    assert groups[0].item.sku == "UPC-THING"


def test_same_item_by_sku_for_opaque_labels(catalogue_db):
    by_gtin, by_sku = load_catalogue(catalogue_db)
    groups = analyze_intake(_records(("EX-001", "Code 128", "t1")), by_gtin, by_sku)
    assert groups[0].status == "same-item"
    assert groups[0].item.id == 2


def test_new_item_gets_number_system_and_suggested_sku(catalogue_db):
    by_gtin, by_sku = load_catalogue(catalogue_db)
    groups = analyze_intake(
        _records(("4006381333931", "EAN-13", "t1")), by_gtin, by_sku)
    g = groups[0]
    assert g.status == "new-item" and g.kind == "gtin"
    assert g.suggested_sku == f"BC-{g.key[6:]}"   # derived from the GTIN
    assert g.key == "04006381333931"
    assert g.analysis.info.region == "Germany"


def test_restricted_code_is_flagged_not_global(catalogue_db):
    by_gtin, by_sku = load_catalogue(catalogue_db)
    groups = analyze_intake(
        _records(("2412345678901", "EAN-13", "t1")), by_gtin, by_sku)
    g = groups[0]
    assert g.status == "new-item" and g.kind == "restricted"
    assert not g.analysis.info.globally_unique
    report = build_report(groups, "test")
    text = render_text(report)
    assert "NOT globally unique" in text


def test_invalid_check_digit_never_becomes_new_item(catalogue_db):
    by_gtin, by_sku = load_catalogue(catalogue_db)
    groups = analyze_intake(
        _records(("8412345678906", "EAN-13", "t1")), by_gtin, by_sku)
    assert groups[0].status == "invalid"
    assert groups[0].suggested_sku is None


def test_opaque_new_label_suggests_itself_as_sku(catalogue_db):
    by_gtin, by_sku = load_catalogue(catalogue_db)
    groups = analyze_intake(
        _records(("NEW-STUFF-42", "Code 128", "t1")), by_gtin, by_sku)
    g = groups[0]
    assert g.status == "new-item" and g.kind == "opaque"
    assert g.suggested_sku == "NEW-STUFF-42"


def test_report_summary_and_json_roundtrip(catalogue_db):
    by_gtin, by_sku = load_catalogue(catalogue_db)
    groups = analyze_intake(
        _records(("8412345678905", "EAN-13", "t1"),
                 ("8412345678905", "EAN-13", "t2"),
                 ("4006381333931", "EAN-13", "t3"),
                 ("8412345678906", "EAN-13", "t4")),
        by_gtin, by_sku,
    )
    report = build_report(groups, "fixture")
    s = report["summary"]
    assert (s["total_scans"], s["unique_codes"]) == (4, 3)
    assert (s["same_item"], s["new_item"], s["invalid"]) == (1, 1, 1)
    assert s["units_of_known_items"] == 2
    # JSON must survive a roundtrip (scan_intake --json consumers)
    assert json.loads(json.dumps(report))["summary"] == s
    text = render_text(report)
    assert "[SAME-ITEM] ×2" in text and "[NEW-ITEM] ×1" in text
    assert "re-scan" in text


def test_database_is_opened_read_only(catalogue_db):
    # The tool must never write to the catalogue DB; simulate by making the
    # file read-only at the OS level — sqlite must still open it (mode=ro).
    os.chmod(catalogue_db, 0o444)
    try:
        by_gtin, _ = load_catalogue(catalogue_db)
        assert len(by_gtin) == 1
    finally:
        os.chmod(catalogue_db, 0o644)
