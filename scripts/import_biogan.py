#!/usr/bin/env python3
"""Idempotently import the Biogan master CSV into the item catalogue.

The supplier CSV is read-only. SKU and name are exact strings (never casefolded,
stripped, or passed through ``clean_name``), stock/description/created_at of an
existing item are preserved (and so is a stored barcode when the supplier row
states none), and all changes are applied in one transaction.

Unlike the crawler's input gate, this importer deliberately accepts blank EANs
and case-distinct SKUs: section B requires ``barcode=None`` for blank/invalid
EANs and treats ``1709c``/``1709C`` as different catalogue keys. The small CSV
reader below otherwise keeps the same byte-fidelity and shape checks.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import re
import stat
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from scripts import _biogan as bio
from scripts._hcrm import (
    EXIT_CHECKS_FAILED,
    EXIT_OK,
    EXIT_UNREACHABLE,
    EXIT_USAGE,
)
from app import embeddings
from app.models import CartItem, Item, utcnow
from app.seed import EXAMPLE_ITEMS

logger = logging.getLogger("hcrm")

MEDIA_NAME_RE = re.compile(r"\d{13}\.(?:jpg|jpeg|png|webp)", re.IGNORECASE | re.ASCII)
_MAGIC_CLASS = {".jpg": ".jpg", ".jpeg": ".jpg", ".png": ".png", ".webp": ".webp"}
_REQUIRED_HEADERS = ("Varenummer", "Varenavn", "EAN-nummer", "Sp3-Vejl.Pris")
_MANAGED_FIELDS = ("name", "category", "price_cents", "barcode", "image_url")


class UsageError(ValueError):
    """A CLI, input, schema, or output-path precondition failed."""


class _UsageParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def _nonnegative(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def _build_parser() -> argparse.ArgumentParser:
    parser = _UsageParser(description=__doc__)
    parser.add_argument("--csv", default=str(bio.default_csv_path()))
    parser.add_argument("--images-dir", default=str(bio.default_images_dir()))
    parser.add_argument("--db", default=str(bio.default_data_dir() / "hcrm.db"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-update", action="store_true")
    parser.add_argument("--category", default="Biogan")
    parser.add_argument("--stock", type=_nonnegative, default=0)
    parser.add_argument("--prune-demo", action="store_true")
    parser.add_argument("--crawl-report", default="")
    parser.add_argument("--report", default="")
    return parser


def _header_index(header: list[str], name: str) -> int:
    return [cell.strip() for cell in header].index(name)


def read_import_masterdata(path: Path) -> bio.MasterData:
    """Read CSV with crawler fidelity, while allowing blank EAN/case-variant SKUs."""
    path = Path(path)
    if not path.is_file():
        raise UsageError(f"master CSV not found: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise UsageError(f"cannot read master CSV {path}: {exc}") from exc
    try:
        text = raw.decode("utf-8-sig")
        records = list(csv.reader(io.StringIO(text, newline="")))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise UsageError(f"cannot parse master CSV {path}: {exc}") from exc
    if len(records) < 2:
        raise UsageError(f"{path}: fewer than two records")
    if len(records) != len(text.splitlines()):
        raise UsageError(f"{path}: embedded newline(s) in a quoted field")
    width = len(records[0])
    if any(len(record) != width for record in records):
        raise UsageError(f"{path}: records do not share one field count")
    header = [cell.strip() for cell in records[0]]
    for name in _REQUIRED_HEADERS:
        if name not in header:
            raise UsageError(f"{path}: missing required header {name!r}")

    sku_i = _header_index(records[0], "Varenummer")
    ean_i = _header_index(records[0], "EAN-nummer")
    seen_sku: set[str] = set()
    seen_ean: set[str] = set()
    for index, record in enumerate(records[1:], start=2):
        if all(not cell.strip() for cell in record):
            continue
        sku = record[sku_i]
        ean = record[ean_i].strip()
        if sku and sku in seen_sku:
            raise UsageError(f"{path}: duplicate SKU {sku!r} at record {index}")
        if ean and ean in seen_ean:
            raise UsageError(f"{path}: duplicate EAN {ean!r} at record {index}")
        if sku:
            seen_sku.add(sku)
        if ean:
            seen_ean.add(ean)

    eol = "\r\n" if b"\r\n" in raw else "\n"
    snap = bio.Snapshot(
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        mode=stat.S_IMODE(path.stat().st_mode),
        had_bom=raw.startswith(b"\xef\xbb\xbf"),
        eol=eol,
        eof_newline=raw.endswith(eol.encode("ascii")),
        n_records=len(records),
    )
    probe = bio.MasterData(records, [], snap, 0, False)
    try:
        bio.self_check(probe)
    except bio.CsvFidelityError as exc:
        raise UsageError(str(exc)) from exc
    photo_idx, appended = bio.ensure_photo_column(records)
    if appended:
        logger.warning("%s: foreign seventh header; appended URL to photo", path)

    name_i = _header_index(records[0], "Varenavn")
    price_i = _header_index(records[0], "Sp3-Vejl.Pris")
    rows = [
        bio.Row(
            rec_index=index,
            sku=record[sku_i],
            name=record[name_i],
            ean=record[ean_i].strip(),
            price=record[price_i].strip(),
            photo=record[photo_idx],
            is_blank=all(not cell.strip() for cell in record),
        )
        for index, record in enumerate(records[1:], start=1)
    ]
    return bio.MasterData(records, rows, snap, photo_idx, appended)


def _check_report_path(report: Path, images_dir: Path, csv_path: Path, db_path: Path) -> None:
    report = report.resolve()
    if report.suffix.lower() != ".json":
        raise UsageError("report path must end in .json")
    images = images_dir.resolve()
    if report == images or images in report.parents:
        raise UsageError("report path must be outside the images directory")
    csv_real = csv_path.resolve()
    reserved = {
        csv_real,
        csv_real.with_name(csv_real.name + ".bak"),
        csv_real.with_name(csv_real.name + ".lock"),
        db_path.resolve(),
    }
    if report in reserved:
        raise UsageError("report path would overwrite the CSV, its backup/lock, or the DB")


def write_report(path: Path, payload: dict) -> None:
    """Atomically write the import report as mode-0600 JSON."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def load_crawl_report(path: str) -> dict[str, dict]:
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        hits = payload["hits"]
        if not isinstance(hits, list):
            raise TypeError("hits is not a list")
        result = {}
        for hit in hits:
            if not isinstance(hit, dict) or not isinstance(hit.get("sku"), str):
                raise TypeError("each hit needs a string sku")
            result[hit["sku"]] = {
                "status": hit.get("status", ""),
                "match_via": hit.get("match_via", ""),
                "review": hit.get("review") or [],
            }
        return result
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise UsageError(f"invalid crawl report {path}: {exc}") from exc


def _valid_local_image(path: Path, images_dir: Path) -> bool:
    """Match both the media allowlist and its content/containment checks."""
    try:
        if not MEDIA_NAME_RE.fullmatch(path.name) or Path(path.name).name != path.name:
            return False
        if not path.is_file() or path.stat().st_size == 0:
            return False
        if path.resolve().parent != images_dir.resolve():
            return False
        with open(path, "rb") as handle:
            head = handle.read(64)
        magic = bio.sniff_image(head, None)[0]
        return magic == _MAGIC_CLASS[path.suffix.lower()]
    except OSError:
        return False


def choose_image_url(row: bio.Row, images_dir: Path) -> tuple[str, str, list[str]]:
    """Return (image_url, source, review notes): valid local -> remote -> blank."""
    notes: list[str] = []
    if re.fullmatch(r"[0-9]{13}", row.ean or "", re.ASCII):
        candidate, _duplicates = bio.find_image(images_dir, row.ean)
        if candidate is not None:
            if _valid_local_image(candidate, images_dir):
                return f"/catalogue-images/{candidate.name}", "local", notes
            notes.append("invalid_local_image")
    if row.photo:
        return row.photo, "remote", notes
    notes.append("image_missing")
    return "", "none", notes


def _barcode(row: bio.Row) -> tuple[str | None, list[str]]:
    if not row.ean:
        return None, ["blank_ean"]
    try:
        return bio.ean_to_gtin14(row.ean), []
    except ValueError:
        # Section B: an invalid supplier EAN is not a reason to drop the item.
        return None, ["invalid_ean"]


def _base_result(row: bio.Row, image_url: str, source: str, barcode: str | None,
                 price_cents: int | None, review: list[str], provenance: dict) -> dict:
    return {
        "rec_index": row.rec_index,
        "sku": row.sku,
        "ean": row.ean,
        "name": row.name,
        "action": "",
        "changed": [],
        "would_update": False,
        "image_url": image_url,
        "image_source": source,
        "barcode": barcode,
        "price_cents": price_cents,
        "owner_sku": None,
        "review": review,
        "crawl": provenance,
    }


def _plan_row(session: Session, row: bio.Row, args, images_dir: Path,
              crawl: dict) -> tuple[dict, Item | None, dict, bytes | None]:
    image_url, source, notes = choose_image_url(row, images_dir)
    provenance = crawl.get(row.sku, {})
    if row.is_blank or not row.sku.strip() or not row.name.strip():
        result = _base_result(row, image_url, source, None, None, notes, provenance)
        result["action"] = "skipped_blank"
        return result, None, {}, None
    barcode, barcode_notes = _barcode(row)
    notes.extend(barcode_notes)
    try:
        price_cents = bio.parse_price_cents(row.price)
    except ValueError as exc:
        result = _base_result(row, image_url, source, barcode, None, notes, provenance)
        result["action"] = "bad_row"
        result["review"].append(str(exc))
        return result, None, {}, None

    result = _base_result(row, image_url, source, barcode, price_cents, notes, provenance)
    if barcode is not None:
        owner = session.exec(
            select(Item).where(Item.barcode == barcode, Item.sku != row.sku)
        ).first()
        if owner is not None:
            result["action"] = "refused"
            result["owner_sku"] = owner.sku
            result["review"].append("gtin_owned_by_other_sku")
            return result, None, {}, None

    item = session.exec(select(Item).where(Item.sku == row.sku)).first()
    values = {
        "name": row.name,
        "category": args.category,
        "price_cents": price_cents,
        "barcode": barcode,
        "image_url": image_url,
    }
    if item is None:
        text = embeddings.item_embedding_text(row.name, "")
        blob = embeddings.embed_text_blob(text)
        if blob is None:
            result["action"] = "deferred"
            result["review"].append("embedding_unavailable")
            return result, None, {}, None
        result["action"] = "created"
        create_values = {"sku": row.sku, **values, "description": "", "stock": args.stock}
        return result, item, create_values, blob

    changed = [field for field in _MANAGED_FIELDS
               if not (field == "barcode" and values[field] is None)
               and getattr(item, field) != values[field]]
    if values["barcode"] is None and item.barcode:
        # A blank or GS1-invalid supplier EAN states nothing about the product:
        # never clear a stored GTIN (D7 "never destroy knowledge"). Deliberate
        # removal stays an API action (ItemPatch's ""-clears convention).
        result["review"].append("barcode_kept")
    result["changed"] = changed
    result["would_update"] = bool(changed)
    if args.no_update or not changed:
        result["action"] = "unchanged"
        return result, item, {}, None

    blob = None
    if "name" in changed:
        text = embeddings.item_embedding_text(row.name, item.description)
        blob = embeddings.embed_text_blob(text)
        if blob is None:
            # Do not assign any managed field: old name and old blob stay paired.
            result["action"] = "deferred"
            result["review"].append("embedding_unavailable")
            return result, item, {}, None
    result["action"] = "updated"
    return result, item, values, blob


def _prune_demo(session: Session, dry_run: bool) -> list[str]:
    demo_skus = {spec["sku"] for spec in EXAMPLE_ITEMS}
    pruned: list[str] = []
    for item in session.exec(select(Item).where(Item.sku.in_(demo_skus))).all():
        pruned.append(item.sku)
        if not dry_run:
            for line in session.exec(select(CartItem).where(CartItem.item_id == item.id)).all():
                session.delete(line)
            session.delete(item)
    return pruned


def import_masterdata(session: Session, md: bio.MasterData, args, *,
                      images_dir: Path, crawl: dict, dry_run: bool) -> dict:
    """Plan every row (including embeddings), then apply with one commit."""
    plans = [
        _plan_row(session, row, args, images_dir, crawl)
        for row in md.rows
    ]
    pruned = _prune_demo(session, dry_run) if args.prune_demo else []
    if not dry_run:
        for result, item, values, blob in plans:
            action = result["action"]
            if action == "created":
                session.add(Item(**values, embedding=blob))
            elif action == "updated":
                assert item is not None
                for field in result["changed"]:
                    setattr(item, field, values[field])
                if "name" in result["changed"]:
                    item.embedding = blob
                item.updated_at = utcnow()
                session.add(item)
        session.commit()
    else:
        session.rollback()

    rows = [result for result, _item, _values, _blob in plans]
    counts = Counter(result["action"] for result in rows)
    deferred = counts.get("deferred", 0)
    report = {
        "csv": str(args.csv_path),
        "csv_sha256": md.snap.sha256,
        "eol": md.snap.eol,
        "eof_newline": md.snap.eof_newline,
        "mode": md.snap.mode,
        "db": str(args.db_path),
        "images_dir": str(images_dir),
        "dry_run": dry_run,
        "no_update": args.no_update,
        "category": args.category,
        "stock_on_create": args.stock,
        "counts": dict(sorted(counts.items())),
        "embedding_deferred": deferred,
        "conflicts": [
            {"sku": result["sku"], "owner_sku": result["owner_sku"]}
            for result in rows if result["action"] == "refused"
        ],
        "expected_missing": sorted(
            sku for sku, provenance in crawl.items()
            if provenance.get("status") == "no_result"
        ),
        "demo_pruned": pruned,
        "review": [
            {"sku": result["sku"], "notes": result["review"]}
            for result in rows if result["review"]
        ],
        "rows": rows,
    }
    return report


def prepare_database(engine, dry_run: bool) -> None:
    """Bootstrap real runs only; dry-run requires an existing current schema."""
    if dry_run:
        inspector = inspect(engine)
        if not inspector.has_table("items"):
            raise UsageError("dry-run requires an existing items schema")
        required = {
            "sku", "name", "description", "category", "price_cents", "stock",
            "image_url", "barcode", "embedding", "created_at", "updated_at",
        }
        columns = {column["name"] for column in inspector.get_columns("items")}
        if not required.issubset(columns):
            raise UsageError("dry-run requires a migrated items schema")
    else:
        import app.database as database
        if engine is database.engine:
            database.create_all()
            from app.main import _migrate_schema
            _migrate_schema()
        else:
            # Explicit test/injection seam: create the current schema on a tmp engine.
            SQLModel.metadata.create_all(engine)
            with engine.begin() as connection:
                for ddl in database.TRIGGER_DDL:
                    connection.exec_driver_sql(ddl)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA busy_timeout=5000")


def _engine_for(args, injected):
    if injected is not None:
        return injected
    desired_url = f"sqlite:///{args.db_path}"
    if "app.database" in sys.modules:
        database = sys.modules["app.database"]
        if database.DATABASE_URL != desired_url:
            raise UsageError(
                "app.database is already bound to another DB; run the importer as a subprocess"
            )
        return database.engine
    os.environ["DATABASE_URL"] = desired_url
    import app.database as database
    return database.engine


def main(argv: list[str] | None = None, *, engine=None) -> int:
    args = _build_parser().parse_args(argv)
    args.csv_path = Path(args.csv).resolve()
    args.db_path = Path(args.db).resolve()
    images_dir = Path(args.images_dir).resolve()
    try:
        if args.report:
            _check_report_path(Path(args.report), images_dir, args.csv_path, args.db_path)
        if args.dry_run and not args.db_path.is_file():
            raise UsageError("dry-run requires an existing DB schema")
        md = read_import_masterdata(args.csv_path)
        crawl = load_crawl_report(args.crawl_report)
        db_engine = _engine_for(args, engine)
        prepare_database(db_engine, args.dry_run)
        with Session(db_engine) as session:
            try:
                report = import_masterdata(
                    session, md, args,
                    images_dir=images_dir, crawl=crawl, dry_run=args.dry_run,
                )
            except BaseException:
                session.rollback()
                raise
        if args.report:
            try:
                write_report(Path(args.report), report)
            except OSError as exc:
                raise UsageError(f"cannot write report: {exc}") from exc
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (IntegrityError, KeyboardInterrupt, Exception) as exc:
        print(f"error: import aborted; whole run rolled back: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE

    counts = report["counts"]
    print("rows=" + str(sum(counts.values())) + " " + " ".join(
        f"{key}={value}" for key, value in counts.items()
    ))
    if report["embedding_deferred"]:
        print(f"warning: {report['embedding_deferred']} row(s) deferred for embedding backfill",
              file=sys.stderr)
    failed = any(counts.get(action, 0) for action in ("refused", "bad_row"))
    return EXIT_CHECKS_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
