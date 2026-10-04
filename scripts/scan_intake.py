#!/usr/bin/env python3
"""scan_intake — turn downloaded scanner data into catalogue decisions.

Reads scan records (from an OPN-2001, an HID-keyboard scanner capture, or a
previously saved file), classifies every barcode with the GS1/GTIN rules in
app/barcodes.py, matches them against the HCRM catalogue (READ-ONLY), and
reports per unique code:

* **same item** — the code matches an existing catalogue entry (by canonical
  GTIN, or by SKU for opaque labels). Duplicates within the batch are a stock
  count, not an error: the report shows the quantity.
* **new item**  — a valid, previously unseen code: a catalogue entry is
  needed. The report pre-fills what the entry can carry automatically (the
  normalised GTIN-14, number-system/region/usage classification, warnings for
  restricted-circulation codes).
* **invalid**   — GTIN-shaped but the check digit fails (misread or typo).
  Never file these; re-scan the item.

Usage:
    # batch already downloaded (opn2001.py read --json > scans.json)
    uv run python scripts/scan_intake.py --from-json scans.json

    # straight from the OPN-2001 (serial or raw-usb backend, see opn2001.py)
    uv run --with pyserial python scripts/scan_intake.py --from-device
    uv run --with pyusb    python scripts/scan_intake.py --from-device --backend usb

    # piped from the HID-keyboard listener (CSV lines on stdin: '-')
    uv run python scripts/scanner_hid.py --count 10 | \
        uv run python scripts/scan_intake.py --from-csv -

The database is opened read-only (file:...?mode=ro + PRAGMA query_only); the
report is a proposal for a human, creating/patching entries stays with the
API/UI (POST /api/items accepts the barcode field). docs/barcodes.md explains
the numbering rules; docs/scanner-opn2001.md §6 the workflow.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import sqlite3
import sys
from collections import OrderedDict
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.barcodes import analyze_scan  # noqa: E402  (repo root on sys.path)


# --------------------------------------------------------------------------
# Scan loading (all sources normalise to a list of ScanRecord)
# --------------------------------------------------------------------------


@dataclass
class ScanRecord:
    barcode: str
    symbology: str
    timestamp: str = ""


def load_json(path: str) -> list[ScanRecord]:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    return [
        ScanRecord(s.get("barcode", ""), s.get("symbology", ""),
                   s.get("timestamp", ""))
        for s in doc.get("scans", [])
    ]


def load_csv(source) -> list[ScanRecord]:
    """opn2001/scanner_hid CSV: timestamp,symbology,barcode (no header).

    `source` is a filename ('-' = stdin). Barcodes containing commas are
    quoted by the writers, so a real CSV reader is used (not str.split).
    """
    if source == "-":
        rows = csv.reader(sys.stdin)
    else:
        with open(source, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
    out = []
    for row in rows:
        if not row or row[0].startswith("#"):
            continue
        if len(row) >= 3:
            out.append(ScanRecord(row[2], row[1], row[0]))
        elif len(row) == 1:
            out.append(ScanRecord(row[0], ""))
    return out


def load_device(port: str | None, backend: str) -> list[ScanRecord]:
    from scripts.opn2001 import OPN2001, UsbStream, _open_serial

    if backend == "usb":
        stream = UsbStream()
    else:
        if not port:
            port = os.environ.get("OPN2001_PORT") or "/dev/ttyUSB0"
        stream = _open_serial(port)
    with stream:
        dev = OPN2001(stream, timeout_desc=port or "usb")
        dev.interrogate()  # wake the link
        _device_id, scans = dev.get_data()
    return [
        ScanRecord(s.barcode, s.symbology, s.timestamp.isoformat(sep=" "))
        for s in scans
    ]


# --------------------------------------------------------------------------
# Catalogue matching (read-only)
# --------------------------------------------------------------------------


@dataclass
class CatalogueEntry:
    id: int
    sku: str
    name: str
    barcode: str | None
    stock: int


def load_catalogue(db_path: str) -> tuple[dict[str, CatalogueEntry], dict[str, CatalogueEntry]]:
    """Return (by_canonical_gtin, by_upper_sku) indexes of the catalogue."""
    by_gtin: dict[str, CatalogueEntry] = {}
    by_sku: dict[str, CatalogueEntry] = {}
    if not os.path.exists(db_path):
        print(f"warning: no database at {db_path} — everything will be 'new'",
              file=sys.stderr)
        return by_gtin, by_sku
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT id, sku, name, barcode, stock FROM items"
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        entry = CatalogueEntry(*row)
        if entry.barcode:
            by_gtin[entry.barcode] = entry
        by_sku[entry.sku.strip().upper()] = entry
    return by_gtin, by_sku


# --------------------------------------------------------------------------
# Intake analysis
# --------------------------------------------------------------------------


@dataclass
class Group:
    """One unique scanned code and its catalogue verdict."""
    key: str                    # canonical GTIN-14 or raw opaque payload
    raw: str                    # first-seen raw payload
    symbology: str
    kind: str                   # ScanAnalysis.kind
    count: int
    first_seen: str
    last_seen: str
    status: str                 # same-item | new-item | invalid | ignored
    item: CatalogueEntry | None
    analysis: object            # app.barcodes.ScanAnalysis
    suggested_sku: str | None = None


def analyze_intake(scans: list[ScanRecord], by_gtin, by_sku) -> list[Group]:
    groups: "OrderedDict[str, Group]" = OrderedDict()
    for scan in scans:
        analysis = analyze_scan(scan.barcode, scan.symbology)
        if analysis.kind == "empty":
            continue
        if analysis.kind == "invalid-gtin":
            key = f"!invalid:{scan.barcode}"
        else:
            key = analysis.key
        g = groups.get(key)
        if g is None:
            item = None
            if analysis.kind in ("gtin", "restricted"):
                item = by_gtin.get(analysis.key)
            elif analysis.kind == "opaque":
                item = by_sku.get(analysis.key.strip().upper())
            if item is not None:
                status = "same-item"
            elif analysis.kind == "invalid-gtin":
                status = "invalid"
            else:
                status = "new-item"
            g = Group(
                key=key, raw=scan.barcode, symbology=scan.symbology,
                kind=analysis.kind, count=0,
                first_seen=scan.timestamp, last_seen=scan.timestamp,
                status=status, item=item, analysis=analysis,
                suggested_sku=_suggest_sku(analysis) if item is None and status == "new-item" else None,
            )
            groups[key] = g
        g.count += 1
        if scan.timestamp:
            g.last_seen = scan.timestamp
            g.first_seen = g.first_seen or scan.timestamp
    return list(groups.values())


def _suggest_sku(analysis) -> str | None:
    """SKU placeholder for a new entry: keep it derived from the identity.

    GTINs get BC-<last 8 digits of the GTIN-13 view> (short, stable, derived
    from the identity; not guaranteed collision-free — the API's SKU
    uniqueness check is the authority), opaque payloads get their own text
    (that IS the identity the label carries).
    """
    if analysis.kind in ("gtin", "restricted") and analysis.info is not None:
        return f"BC-{analysis.key[6:]}"
    if analysis.kind == "opaque" and analysis.key:
        return analysis.key[:64]
    return None


def build_report(groups: list[Group], source: str) -> dict:
    same = [g for g in groups if g.status == "same-item"]
    new = [g for g in groups if g.status == "new-item"]
    bad = [g for g in groups if g.status == "invalid"]
    return {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "summary": {
            "unique_codes": len(groups),
            "total_scans": sum(g.count for g in groups),
            "same_item": len(same),
            "new_item": len(new),
            "invalid": len(bad),
            "units_of_known_items": sum(g.count for g in same),
        },
        "groups": [
            {
                "key": g.key,
                "raw": g.raw,
                "symbology": g.symbology,
                "kind": g.kind,
                "count": g.count,
                "first_seen": g.first_seen,
                "last_seen": g.last_seen,
                "status": g.status,
                "suggested_sku": g.suggested_sku,
                "number_system": _number_system_dict(g),
                "matched_item": (
                    {"id": g.item.id, "sku": g.item.sku, "name": g.item.name,
                     "stock": g.item.stock}
                    if g.item else None
                ),
            }
            for g in groups
        ],
    }


def _number_system_dict(g: Group) -> dict | None:
    info = g.analysis.info
    if info is None:
        return None
    return {
        "gtin14": info.gtin14,
        "format": info.format,
        "gs1_prefix": info.prefix,
        "region": info.region,
        "usage": info.usage,
        "globally_unique": info.globally_unique,
        "notes": list(info.notes),
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_text(report: dict) -> str:
    out = io.StringIO()
    s = report["summary"]
    out.write(f"scan intake — {report['source']} ({report['generated']})\n")
    out.write(
        f"{s['total_scans']} scans, {s['unique_codes']} unique codes: "
        f"{s['same_item']} same-item (known), {s['new_item']} new-item "
        f"(need an entry), {s['invalid']} invalid (re-scan)\n"
    )
    order = {"new-item": 0, "same-item": 1, "invalid": 2}
    for g in sorted(report["groups"], key=lambda g: (order[g["status"]], g["key"])):
        ns = g["number_system"] or {}
        head = f"\n[{g['status'].upper()}] ×{g['count']}  {g['raw']}"
        if g["symbology"]:
            head += f"  ({g['symbology']})"
        out.write(head + "\n")
        if ns:
            out.write(
                f"  GTIN-14: {ns['gtin14']}  [{ns['format']}, GS1 prefix "
                f"{ns['gs1_prefix']}: {ns['region']}, {ns['usage']}]"
                + ("" if ns["globally_unique"] else "  ⚠ NOT globally unique")
                + "\n"
            )
            for note in ns["notes"]:
                out.write(f"    note: {note}\n")
        if g["matched_item"]:
            m = g["matched_item"]
            out.write(f"  catalogue: {m['sku']} — {m['name']} "
                      f"(id {m['id']}, stock {m['stock']})\n")
        if g["status"] == "new-item":
            if ns:  # GTIN family: file the canonical barcode
                out.write(f"  → create: POST /api/items {{\"sku\": \"{g['suggested_sku']}\", "
                          f"\"barcode\": \"{ns['gtin14']}\", ...}}\n")
            else:   # opaque label: the payload is the SKU, no barcode field
                out.write(f"  → create: POST /api/items {{\"sku\": \"{g['suggested_sku']}\", ...}}\n")
        if g["status"] == "invalid":
            out.write("  → do NOT create: check digit failed — misread or typo; re-scan\n")
    return out.getvalue()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def default_db_path() -> str:
    data_dir = os.environ.get(
        "HCRM_DATA_DIR",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"),
    )
    return os.path.join(data_dir, "hcrm.db")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Classify scanner downloads against the HCRM catalogue: "
        "same item vs new entry (see docs/barcodes.md)"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-json", metavar="FILE",
                        help="opn2001.py read --json output")
    source.add_argument("--from-csv", metavar="FILE|-",
                        help="CSV lines timestamp,symbology,barcode ('-' = stdin)")
    source.add_argument("--from-device", action="store_true",
                        help="download straight from an attached OPN-2001")
    parser.add_argument("--port", default=None,
                        help="serial port for --from-device (default: $OPN2001_PORT or /dev/ttyUSB0)")
    parser.add_argument("--backend", choices=("serial", "usb"), default="serial",
                        help="--from-device transport (usb = raw libusb, for WSL2)")
    parser.add_argument("--db", default=default_db_path(),
                        help="HCRM SQLite file, opened READ-ONLY (default: %(default)s)")
    parser.add_argument("--json", action="store_true", help="JSON report")
    args = parser.parse_args(argv)

    if args.from_json:
        scans, src = load_json(args.from_json), args.from_json
    elif args.from_csv:
        scans, src = load_csv(args.from_csv), (
            "stdin" if args.from_csv == "-" else args.from_csv)
    else:
        scans, src = load_device(args.port, args.backend), "device"

    by_gtin, by_sku = load_catalogue(args.db)
    groups = analyze_intake(scans, by_gtin, by_sku)
    report = build_report(groups, src)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(render_text(report))
    # Non-zero when any scan failed validation: scripts chaining this should
    # notice that a re-scan is needed.
    return 0 if report["summary"]["invalid"] == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
