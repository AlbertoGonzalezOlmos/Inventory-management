"""Offline tests for scripts.import_biogan (FINAL-APPROACH-2 §B)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import CartItem, Item, utcnow
from app.seed import EXAMPLE_ITEMS
from scripts import import_biogan as mod

FIXTURES = Path(__file__).parent / "fixtures" / "biogan"
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 64
HTML = b"<html><body>not an image</body></html>"


def ean13(number: int) -> str:
    body = f"57000000{number:04d}"
    total = sum(int(char) * (3 if index % 2 else 1)
                for index, char in enumerate(body))
    return body + str((10 - total % 10) % 10)


def supplier(sku: str, name: str | None = None, *, ean: str = "",
             price: str = "10.00", photo: str = "") -> dict:
    return {"sku": sku, "name": name if name is not None else f"Product {sku}",
            "ean": ean, "price": price, "photo": photo}


def write_csv(path: Path, rows: list[dict]) -> Path:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["Varenummer", "Varenavn", "EAN-nummer", "Antal pr colli",
                         "Sp2-Engros", "Sp3-Vejl.Pris", "URL to photo"])
        writer.writerow(["", "", "", "", "", "", ""])
        for row in rows:
            writer.writerow([row["sku"], row["name"], row["ean"], "1", "1.00",
                             row["price"], row["photo"]])
    return path


def make_engine(tmp_path: Path):
    return create_engine(f"sqlite:///{tmp_path / 'import.db'}",
                         connect_args={"check_same_thread": False})


def invoke(tmp_path: Path, engine, rows: list[dict], *extra: str) -> int:
    csv_path = write_csv(tmp_path / "master.csv", rows)
    argv = ["--csv", str(csv_path), "--images-dir", str(tmp_path / "images"),
            "--db", str(tmp_path / "import.db"), *extra]
    return mod.main(argv, engine=engine)


def items(engine) -> list[Item]:
    with Session(engine) as session:
        return list(session.exec(select(Item).order_by(Item.id)).all())


def report_at(tmp_path: Path, name: str = "report.json") -> dict:
    return json.loads((tmp_path / name).read_text(encoding="utf-8"))


def test_240_create_then_all_unchanged_and_226_local_14_blank(tmp_path, monkeypatch):
    rows = []
    for number in range(1, 241):
        photo = f"https://shop.biogan.dk/images/{number}-p.jpg" if number <= 226 else ""
        rows.append(supplier(f"S{number:04d}", ean=ean13(number), photo=photo))
    images = tmp_path / "images"
    images.mkdir()
    for number in range(1, 227):
        (images / f"{ean13(number)}.jpg").write_bytes(JPEG)
    engine = make_engine(tmp_path)

    assert invoke(tmp_path, engine, rows, "--report", str(tmp_path / "report.json")) == 0
    created = items(engine)
    assert len(created) == 240
    assert sum(bool(item.image_url) for item in created) == 226
    assert sum(item.image_url == "" for item in created) == 14
    assert all(item.image_url.startswith("/catalogue-images/")
               for item in created if item.image_url)
    first = report_at(tmp_path)
    assert first["counts"] == {"created": 240, "skipped_blank": 1}
    timestamps = {item.sku: (item.created_at, item.updated_at) for item in created}

    calls = []
    monkeypatch.setattr(mod.embeddings, "embed_text_blob",
                        lambda text: calls.append(text) or b"should-not-be-used")
    assert invoke(tmp_path, engine, rows, "--report", str(tmp_path / "report.json")) == 0
    second = report_at(tmp_path)
    assert second["counts"] == {"skipped_blank": 1, "unchanged": 240}
    assert calls == []
    assert {item.sku: (item.created_at, item.updated_at) for item in items(engine)} == timestamps


def test_verbatim_mapping_blank_invalid_ean_and_blank_rows(tmp_path):
    valid = ean13(50)
    rows = [
        supplier("A001", "  Kobber  tungeskraber ", price="108.1"),
        supplier("A002", "Invalid EAN", ean="not-an-ean", price="118"),
        supplier("A003", "Valid EAN", ean=valid, price="404.48"),
        supplier("A004", "   ", ean=ean13(51)),
    ]
    engine = make_engine(tmp_path)
    assert invoke(tmp_path, engine, rows, "--report", str(tmp_path / "report.json")) == 0
    by_sku = {item.sku: item for item in items(engine)}
    assert set(by_sku) == {"A001", "A002", "A003"}
    assert by_sku["A001"].name == "  Kobber  tungeskraber "
    assert by_sku["A001"].price_cents == 10810
    assert by_sku["A001"].barcode is None
    assert by_sku["A002"].price_cents == 11800
    assert by_sku["A002"].barcode is None
    assert by_sku["A003"].price_cents == 40448
    assert by_sku["A003"].barcode == "0" + valid
    assert report_at(tmp_path)["counts"]["skipped_blank"] == 2  # row 2 + blank name


def test_mixed_case_skus_are_distinct_exact_keys(tmp_path):
    rows = [supplier("1709c", ean=ean13(60)), supplier("1709C", ean=ean13(61))]
    engine = make_engine(tmp_path)
    assert invoke(tmp_path, engine, rows) == 0
    assert {item.sku for item in items(engine)} == {"1709c", "1709C"}


def test_gtin_conflict_refuses_whole_row_and_other_rows_commit(tmp_path):
    engine = make_engine(tmp_path)
    ean = ean13(70)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Item(sku="OWNER", name="Owner", barcode="0" + ean,
                         price_cents=1, embedding=b"owner"))
        session.commit()
    rows = [supplier("OTHER", ean=ean), supplier("GOOD", ean=ean13(71))]
    assert invoke(tmp_path, engine, rows, "--report", str(tmp_path / "report.json")) == 1
    by_sku = {item.sku: item for item in items(engine)}
    assert set(by_sku) == {"OWNER", "GOOD"}
    assert by_sku["OWNER"].name == "Owner"
    assert by_sku["OWNER"].price_cents == 1
    assert report_at(tmp_path)["conflicts"] == [{"sku": "OTHER", "owner_sku": "OWNER"}]


def test_image_fallback_valid_magic_remote_blank_and_allowlist(tmp_path):
    eans = [ean13(number) for number in range(80, 85)]
    images = tmp_path / "images"
    images.mkdir()
    (images / f"{eans[0]}.jpg").write_bytes(JPEG)
    (images / f"{eans[1]}.png").write_bytes(HTML)
    (images / f"{eans[2]}.jpg").write_bytes(b"")
    (images / f"{eans[3]}.gif").write_bytes(b"GIF89a" + b"g" * 20)
    (images / f"{eans[4]}.JPG").write_bytes(JPEG)
    rows = [
        supplier("L1", ean=eans[0], photo="https://remote/1.jpg"),
        supplier("L2", ean=eans[1], photo="https://remote/2.jpg"),
        supplier("L3", ean=eans[2]),
        supplier("L4", ean=eans[3], photo="https://remote/4.jpg"),
        supplier("L5", ean=eans[4]),
    ]
    engine = make_engine(tmp_path)
    assert invoke(tmp_path, engine, rows) == 0
    by_sku = {item.sku: item for item in items(engine)}
    assert by_sku["L1"].image_url == f"/catalogue-images/{eans[0]}.jpg"
    assert by_sku["L2"].image_url == "https://remote/2.jpg"
    assert by_sku["L3"].image_url == ""
    assert by_sku["L4"].image_url == "https://remote/4.jpg"  # gif is not route-legal
    assert by_sku["L5"].image_url == f"/catalogue-images/{eans[4]}.JPG"


def test_stock_description_created_at_and_no_update_are_safe(tmp_path):
    engine = make_engine(tmp_path)
    ean = ean13(90)
    created_at = utcnow().replace(year=2000)
    updated_at = utcnow().replace(year=2001)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Item(sku="KEEP", name="Old", description="counted shelves",
                         category="Old", price_cents=1, stock=7, barcode="0" + ean,
                         created_at=created_at, updated_at=updated_at,
                         embedding=b"old-embedding"))
        session.commit()
    changed = [supplier("KEEP", "New name", ean=ean, price="20"),
               supplier("NEW", ean=ean13(91))]
    assert invoke(tmp_path, engine, changed, "--stock", "99") == 0
    by_sku = {item.sku: item for item in items(engine)}
    assert by_sku["KEEP"].stock == 7
    assert by_sku["KEEP"].description == "counted shelves"
    assert by_sku["KEEP"].created_at == created_at
    assert by_sku["KEEP"].price_cents == 2000
    assert by_sku["KEEP"].updated_at > updated_at
    assert by_sku["NEW"].stock == 99

    before = {item.sku: (item.name, item.price_cents, item.updated_at)
              for item in items(engine)}
    changed_again = [supplier("KEEP", "Suppressed", ean=ean, price="30"),
                     supplier("NEW", ean=ean13(91))]
    assert invoke(tmp_path, engine, changed_again, "--no-update", "--report",
                  str(tmp_path / "report.json")) == 0
    after = {item.sku: (item.name, item.price_cents, item.updated_at)
             for item in items(engine)}
    assert after == before
    keep = next(row for row in report_at(tmp_path)["rows"] if row["sku"] == "KEEP")
    assert keep["action"] == "unchanged" and keep["would_update"] is True


def test_embedding_reuse_rename_and_null_backfill_left_alone(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    ean = ean13(100)
    calls = []

    def fake(text):
        calls.append(text)
        return f"blob:{text}".encode()

    monkeypatch.setattr(mod.embeddings, "embed_text_blob", fake)
    rows = [supplier("EMB", "Original", ean=ean)]
    assert invoke(tmp_path, engine, rows) == 0
    assert len(calls) == 1
    assert invoke(tmp_path, engine, rows) == 0
    assert len(calls) == 1  # exact no-op does not call the embedder

    assert invoke(tmp_path, engine, [supplier("EMB", "Original", ean=ean, price="99")]) == 0
    assert len(calls) == 1  # price-only update is not embedded text
    assert invoke(tmp_path, engine, [supplier("EMB", "Renamed", ean=ean, price="99")]) == 0
    assert len(calls) == 2
    item = items(engine)[0]
    assert item.name == "Renamed" and item.embedding == b"blob:Renamed."

    with Session(engine) as session:
        item.embedding = None
        session.add(item)
        session.commit()
    assert invoke(tmp_path, engine, [supplier("EMB", "Renamed", ean=ean, price="100")]) == 0
    assert len(calls) == 2  # NULL on unchanged text belongs to boot backfill
    assert items(engine)[0].embedding is None


def test_embedding_none_defers_create_and_preserves_rename(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    ean = ean13(110)
    monkeypatch.setattr(mod.embeddings, "embed_text_blob", lambda _text: None)
    assert invoke(tmp_path, engine, [supplier("NEW", ean=ean)],
                  "--report", str(tmp_path / "report.json")) == 0
    assert items(engine) == []
    assert report_at(tmp_path)["embedding_deferred"] == 1

    monkeypatch.setattr(mod.embeddings, "embed_text_blob", lambda text: b"good:" + text.encode())
    assert invoke(tmp_path, engine, [supplier("NEW", "Old name", ean=ean)]) == 0
    before = items(engine)[0]
    old_blob = before.embedding
    monkeypatch.setattr(mod.embeddings, "embed_text_blob", lambda _text: None)
    assert invoke(tmp_path, engine, [supplier("NEW", "New name", ean=ean, price="88")]) == 0
    after = items(engine)[0]
    assert after.name == "Old name" and after.price_cents == 1000
    assert after.embedding == old_blob


def test_prune_demo_exact_seed_set_and_cart_cleanup(tmp_path):
    engine = make_engine(tmp_path)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for spec in EXAMPLE_ITEMS:
            session.add(Item(**spec, embedding=b"demo"))
        custom = Item(sku="EX-999", name="Operator row", embedding=b"custom")
        session.add(custom)
        session.commit()
        demo = session.exec(select(Item).where(Item.sku == "EX-001")).one()
        session.add(CartItem(user_id=1, item_id=demo.id, qty=2))
        session.add(CartItem(user_id=1, item_id=custom.id, qty=3))
        session.commit()
    assert invoke(tmp_path, engine, []) == 0
    assert len(items(engine)) == 13  # default preserves every demo + custom
    assert invoke(tmp_path, engine, [], "--prune-demo") == 0
    remaining = items(engine)
    assert [item.sku for item in remaining] == ["EX-999"]
    with Session(engine) as session:
        carts = list(session.exec(select(CartItem)).all())
    assert len(carts) == 1 and carts[0].item_id == remaining[0].id


def test_dry_run_no_ddl_or_commit_but_explicit_report(tmp_path):
    engine = make_engine(tmp_path)
    ean = ean13(120)
    rows = [supplier("DRY", "Old", ean=ean, price="10")]
    assert invoke(tmp_path, engine, rows) == 0
    before = [(item.name, item.price_cents, item.updated_at) for item in items(engine)]
    changed = [supplier("DRY", "Planned", ean=ean, price="20")]
    assert invoke(tmp_path, engine, changed, "--dry-run", "--report",
                  str(tmp_path / "plan.json")) == 0
    assert [(item.name, item.price_cents, item.updated_at) for item in items(engine)] == before
    plan = report_at(tmp_path, "plan.json")
    assert plan["dry_run"] is True
    assert plan["counts"] == {"skipped_blank": 1, "updated": 1}
    assert not (tmp_path / "master.csv.lock").exists()

    missing_dir = tmp_path / "missing"
    missing_dir.mkdir()
    missing_engine = make_engine(missing_dir)
    write_csv(missing_dir / "master.csv", rows)
    code = mod.main(["--csv", str(missing_dir / "master.csv"), "--images-dir",
                     str(missing_dir / "images"), "--db", str(missing_dir / "import.db"),
                     "--dry-run"], engine=missing_engine)
    assert code == 3 and not (missing_dir / "import.db").exists()


def test_commit_integrity_error_rolls_back_entire_run(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    ean = ean13(130)
    assert invoke(tmp_path, engine, [supplier("OLD", "Before", ean=ean)]) == 0

    def fail_commit(self):
        raise IntegrityError("insert", {}, Exception("injected unique race"))

    monkeypatch.setattr(mod.Session, "commit", fail_commit)
    rows = [supplier("OLD", "After", ean=ean), supplier("NEW", ean=ean13(131))]
    assert invoke(tmp_path, engine, rows) == 2
    by_sku = {item.sku: item for item in items(engine)}
    assert set(by_sku) == {"OLD"} and by_sku["OLD"].name == "Before"


def test_bad_price_is_reported_without_blocking_good_rows(tmp_path):
    engine = make_engine(tmp_path)
    rows = [supplier("BAD", ean=ean13(140), price="1,00"),
            supplier("GOOD", ean=ean13(141))]
    assert invoke(tmp_path, engine, rows, "--report", str(tmp_path / "report.json")) == 1
    assert [item.sku for item in items(engine)] == ["GOOD"]
    assert report_at(tmp_path)["counts"]["bad_row"] == 1


def test_report_path_refusals_and_db_mismatch(tmp_path):
    engine = make_engine(tmp_path)
    csv_path = write_csv(tmp_path / "master.csv", [supplier("A", ean=ean13(150))])
    bad_paths = [csv_path, csv_path.with_name(csv_path.name + ".bak"),
                 csv_path.with_name(csv_path.name + ".lock"),
                 tmp_path / "report.txt", tmp_path / "import.db"]
    for path in bad_paths:
        assert invoke(tmp_path, engine, [], "--report", str(path)) == 3
    assert not (tmp_path / "import.db").exists()

    code = mod.main(["--csv", str(csv_path), "--images-dir", str(tmp_path / "images"),
                     "--db", str(tmp_path / "other.db")])
    assert code == 3 and not (tmp_path / "other.db").exists()


def test_crawl_report_provenance_and_expected_missing(tmp_path):
    engine = make_engine(tmp_path)
    crawl = str(FIXTURES / "crawl_report.json")
    rows = [supplier("10005", ean=ean13(160)), supplier("1134", ean=ean13(161)),
            supplier("1509b", ean=ean13(162)), supplier("1709c", ean=ean13(163))]
    assert invoke(tmp_path, engine, rows, "--crawl-report", crawl,
                  "--report", str(tmp_path / "report.json")) == 0
    report = report_at(tmp_path)
    assert report["expected_missing"] == ["1509b", "1709c"]
    by_sku = {row["sku"]: row for row in report["rows"]}
    assert by_sku["10005"]["crawl"]["status"] == "downloaded"
    assert by_sku["1134"]["crawl"]["status"] == "cached"
    assert by_sku["1509b"]["image_url"] == by_sku["1709c"]["image_url"] == ""

    bad = tmp_path / "bad-crawl.json"
    bad.write_text('{"hits": "not-a-list"}', encoding="utf-8")
    assert invoke(tmp_path, engine, [], "--crawl-report", str(bad)) == 3
