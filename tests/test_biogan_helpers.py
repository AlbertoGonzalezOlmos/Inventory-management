"""Offline tests for the pure Biogan helpers (no network, no real data/)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

import scripts._biogan as bio

FIXTURES = Path(__file__).parent / "fixtures" / "biogan"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_csv(tmp_path: Path, *, eol: str = "\r\n", eof: bool = False) -> Path:
    raw = (FIXTURES / "masterdata.csv").read_bytes()
    if eol == "\n":
        raw = raw.replace(b"\r\n", b"\n")
    if eof and not raw.endswith(eol.encode()):
        raw += eol.encode()
    path = tmp_path / "masterdata.csv"
    path.write_bytes(raw)
    return path


@pytest.mark.parametrize("eol,eof", [("\r\n", False), ("\n", True),
                                     ("\r\n", True), ("\n", False)])
def test_snapshot_detects_eol_and_eof(tmp_path, eol, eof):
    path = make_csv(tmp_path, eol=eol, eof=eof)
    md = bio.read_masterdata(path)
    assert md.snap.eol == eol
    assert md.snap.eof_newline is eof
    assert md.snap.had_bom is False
    assert md.snap.n_records == len(md.records) == 5
    assert md.snap.mode == stat.S_IMODE(path.stat().st_mode)
    original = [r[:] for r in md.records]
    original[0][6] = ""  # ensure_photo_column renamed the empty 7th header
    assert bio.serialise(original, md.snap) == md.snap.raw


def test_read_masterdata_shapes(tmp_path):
    md = bio.read_masterdata(make_csv(tmp_path))
    assert md.photo_idx == 6
    assert md.column_appended is False
    assert md.records[0][6] == bio.IMAGE_COLUMN
    assert [r.is_blank for r in md.rows] == [True, False, False, False]
    assert md.rows[1].sku == "10005"
    assert md.rows[2].name == "Kobber tungeskraber "
    assert md.rows[2].photo.startswith("https://shop.biogan.dk/")


def test_self_check_rejects_a_perturbed_record(tmp_path):
    md = bio.read_masterdata(make_csv(tmp_path))
    md.records[2][1] = md.records[2][1] + "x"
    with pytest.raises(bio.CsvFidelityError):
        bio.self_check(md)


def test_ensure_photo_column_names_empty_seventh():
    records = [["a", "b", "c", "d", "e", "f", ""], ["1", "2", "3", "4", "5", "6", ""]]
    assert bio.ensure_photo_column(records) == (6, False)
    assert records[0][6] == bio.IMAGE_COLUMN


def test_ensure_photo_column_reuses_named():
    records = [["a", "b", "c", "d", "e", "f", bio.IMAGE_COLUMN], ["1", "2", "3", "4", "5", "6", ""]]
    assert bio.ensure_photo_column(records) == (6, False)


def test_ensure_photo_column_appends_when_foreign():
    records = [["a", "b", "c", "d", "e", "f", "foreign"], ["1", "2", "3", "4", "5", "6", ""]]
    assert bio.ensure_photo_column(records) == (7, True)
    assert records[0][7] == bio.IMAGE_COLUMN
    assert all(len(r) == 8 for r in records)


def test_write_sets_only_the_photo_cell_and_preserves_shape(tmp_path):
    path = make_csv(tmp_path)
    md = bio.read_masterdata(path)
    url = "https://shop.biogan.dk/images/10005%20Mosaik%20flaske-p.jpg"
    md.records[2][md.photo_idx] = url
    bio.write_masterdata_atomic(path, md, md.records)
    after = bio.read_masterdata(path)
    assert after.records[2][after.photo_idx] == url
    assert after.snap.eol == md.snap.eol
    assert after.snap.eof_newline is md.snap.eof_newline
    assert after.snap.mode == md.snap.mode
    assert after.records[3][after.photo_idx] == md.records[3][md.photo_idx]
    assert after.records[1] == md.records[1]


def test_write_aborts_before_replace_on_a_short_write(tmp_path, monkeypatch):
    path = make_csv(tmp_path)
    original = path.read_bytes()
    md = bio.read_masterdata(path)
    monkeypatch.setattr(bio, "serialise", lambda records, snap: b"Varenummer\n")
    with pytest.raises(bio.CsvFidelityError):
        bio.write_masterdata_atomic(path, md, md.records)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp-*"))


def test_write_verifies_the_eol_shape_of_the_written_bytes(tmp_path, monkeypatch):
    path = make_csv(tmp_path)                      # CRLF, no final newline
    original = path.read_bytes()
    md = bio.read_masterdata(path)
    records = [list(r) for r in md.records]
    records[2][md.photo_idx] = "https://shop.test/x.jpg"
    real = bio.serialise
    monkeypatch.setattr(
        bio, "serialise",
        lambda recs, snap: real(recs, snap).replace(b"\r\n", b"\n"))
    with pytest.raises(bio.CsvFidelityError):
        bio.write_masterdata_atomic(path, md, records)
    assert path.read_bytes() == original           # LF never replaces a CRLF file
    assert not list(tmp_path.glob("*.tmp-*"))


def test_write_serialises_from_a_copy_and_keeps_the_baseline_pristine(
        tmp_path, monkeypatch):
    path = make_csv(tmp_path)
    md = bio.read_masterdata(path)
    baseline = [list(r) for r in md.records]
    records = [list(r) for r in md.records]
    url = "https://shop.test/photo-p.jpg"
    records[2][md.photo_idx] = url
    real = bio.serialise

    def serialise_then_tamper(recs, snap):
        data = real(recs, snap)
        recs[2][1] = "tampered after serialisation"
        return data

    monkeypatch.setattr(bio, "serialise", serialise_then_tamper)
    bio.write_masterdata_atomic(path, md, records)
    monkeypatch.undo()                             # the tamper must not follow us
    assert md.records == baseline                  # baseline never mutated
    assert records[2][1] == baseline[2][1]         # caller's copy untouched
    written = bio.read_masterdata(path)
    assert written.records[2][written.photo_idx] == url
    assert written.records[2][1] == baseline[2][1]


def test_read_rejects_duplicate_ean_and_ragged(tmp_path):
    raw = (FIXTURES / "masterdata.csv").read_bytes()
    dup = raw.replace(b"4006381333931", b"5901234123457", 1)
    p = tmp_path / "dup.csv"
    p.write_bytes(dup)
    with pytest.raises(bio.CsvShapeError):
        bio.read_masterdata(p)
    p2 = tmp_path / "ragged.csv"
    p2.write_bytes(raw.replace(b"10005,\"", b"10005,x,\"", 1))
    with pytest.raises(bio.CsvShapeError):
        bio.read_masterdata(p2)


def test_read_rejects_missing_required_header(tmp_path):
    raw = (FIXTURES / "masterdata.csv").read_bytes().replace(b"Varenavn", b"Name", 1)
    p = tmp_path / "bad.csv"
    p.write_bytes(raw)
    with pytest.raises(bio.CsvShapeError):
        bio.read_masterdata(p)
    with pytest.raises(bio.CsvShapeError):
        bio.read_masterdata(tmp_path / "missing.csv")


def test_clean_name_parse_price_and_gtin():
    assert bio.clean_name("  Kobber   tungeskraber  ") == "Kobber tungeskraber"
    assert bio.parse_price_cents("118") == 11800
    assert bio.parse_price_cents("108.1") == 10810
    assert bio.parse_price_cents("404.48") == 40448
    for bad in ("1,5", "", "-1", "1.234", "abc"):
        with pytest.raises(ValueError):
            bio.parse_price_cents(bad)
    assert bio.ean_to_gtin14("4260563290218") == "04260563290218"
    with pytest.raises(ValueError):
        bio.ean_to_gtin14("4260563290219")


def test_canonical_image_url_is_idempotent():
    base = "https://shop.biogan.dk/shop/search-1.html"
    assert bio.canonical_image_url("/images/10005 Mosaik flaske-t.jpg", base) == \
        "https://shop.biogan.dk/images/10005%20Mosaik%20flaske-t.jpg"
    assert bio.canonical_image_url("/images/miljø.jpg", base) == \
        "https://shop.biogan.dk/images/milj%C3%B8.jpg"
    assert bio.canonical_image_url("/images/a%20b.jpg", base) == \
        "https://shop.biogan.dk/images/a%20b.jpg"
    assert bio.canonical_image_url("https://cdn.test/x.jpg", base) == \
        "https://cdn.test/x.jpg"
    with pytest.raises(ValueError):
        bio.canonical_image_url("javascript:alert(1)", base)


def test_big_variant_last_segment_only():
    assert bio.big_variant("https://x/images/10005 Mosaik flaske-t.jpg") == \
        "https://x/images/10005 Mosaik flaske-p.jpg"
    assert bio.big_variant("https://x/produktbilleder/1134_web-t.jpg") == \
        "https://x/produktbilleder/1134_web-p.jpg"
    assert bio.big_variant("https://x/2134 FORSIDE 300 ppi-t.png") == \
        "https://x/2134 FORSIDE 300 ppi-p.png"
    assert bio.big_variant("https://x/2968_300ppi -t.jpg") == \
        "https://x/2968_300ppi -p.jpg"
    assert bio.big_variant("https://x/no-variant.jpg") == "https://x/no-variant.jpg"


def test_sniff_image_precedence():
    jpeg = b"\xff\xd8\xff\xe0" + b"0" * 16
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    assert bio.sniff_image(jpeg, "image/jpeg") == (".jpg", None)
    assert bio.sniff_image(png, "image/jpeg") == (".png", "content_type_mismatch:image/jpeg")
    assert bio.sniff_image(b"not an image", "text/html") == (None, "not_an_image")
    assert bio.sniff_image(b"no magic bytes here", "image/png") == (".png", "no_magic")
    assert bio.sniff_image(png, "image/png") == (".png", None)


def test_sniff_image_rejects_short_and_markup_bodies():
    jpeg = b"\xff\xd8\xff\xe0" + b"0" * 16
    # empty / truncated bodies are never images, whatever the header claims
    assert bio.sniff_image(b"", "image/jpeg") == (None, "not_an_image")
    assert bio.sniff_image(b"\xff\xd8\xff\xe0", "image/jpeg") == (None, "not_an_image")
    # a lying image/* Content-Type cannot turn markup into an image
    assert bio.sniff_image(b"<!DOCTYPE html><html>x</html>", "image/jpeg") == \
        (None, "not_an_image")
    assert bio.sniff_image(b"  \r\n<html><body>x</body></html>", "image/png") == \
        (None, "not_an_image")
    # magic still wins over a lying CT (and is flagged), and needs no CT at all
    assert bio.sniff_image(jpeg, "text/html") == (
        ".jpg", "content_type_mismatch:text/html")
    assert bio.sniff_image(jpeg, None) == (".jpg", None)


def test_find_image_preference_and_duplicates(tmp_path):
    (tmp_path / "5901234123457.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "5901234123457.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    best, all_files = bio.find_image(tmp_path, "5901234123457")
    assert best is not None and best.suffix == ".jpg"
    assert len(all_files) == 2


def test_parse_products_exact_product():
    products, no_result = bio.parse_products(fixture("search_10005.html"))
    assert no_result is False
    assert len(products) == 1
    p = products[0]
    assert p.product_id == "10005"
    assert p.number == "10005"
    assert p.name == "Mosaic kobberflaske 900ml"
    assert p.images[0] == "/images/10005 Mosaik flaske-t.jpg"
    assert p.price_text == "404,48 DKK"
    assert p.href.endswith("2230p.html")


def test_parse_products_prefix_collision_and_pick():
    products, _ = bio.parse_products(fixture("search_1134.html"))
    assert [p.product_id for p in products] == ["1134", "1134C", "1134b"]
    assert bio.pick(products, "1134C", "x")[1] == "exact_id"
    assert bio.pick(products, "nope", "no name")[1] == "first_result"


def test_parse_products_inverted_order_picks_exact():
    products, _ = bio.parse_products(fixture("search_1509b.html"))
    assert [p.product_id for p in products] == ["1510b", "1509b"]
    chosen, via = bio.pick(products, "1509b", "LOPPEFRØSKALLER/psyllium  fintmalede 200g ØKO")
    assert chosen is not None and chosen.product_id == "1509b"
    assert via == "exact_id"
    assert bio.pick(products, "nope", "LOPPEFRØSKALLER/psyllium 200g ØKO")[1] == "exact_name"


def test_parse_products_chrome_image_is_not_the_photo():
    products, _ = bio.parse_products(fixture("search_2134.html"))
    assert products[0].images == ("/images/2134 FORSIDE 300 ppi-t.png",)
    assert products[0].name == "DADELSIRUP 100% dadler, 330g ØKO"


def test_no_result_vs_parse_failure():
    assert bio.parse_products(fixture("search_none.html")) == ([], True)
    assert bio.parse_products(fixture("search_unparseable.html")) == ([], False)


def test_bom_is_tolerated_and_not_rewritten(tmp_path):
    raw = b"\xef\xbb\xbf" + (FIXTURES / "masterdata.csv").read_bytes()
    path = tmp_path / "bom.csv"
    path.write_bytes(raw)
    md = bio.read_masterdata(path)
    assert md.snap.had_bom is True
    md.records[2][md.photo_idx] = "https://shop.test/x.jpg"
    bio.write_masterdata_atomic(path, md, md.records)
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
