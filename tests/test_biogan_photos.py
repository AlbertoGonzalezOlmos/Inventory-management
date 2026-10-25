"""Offline CLI tests: fake opener, synthetic CSV, no network and no real data/."""

from __future__ import annotations

import io
import json
import os
import stat
import time
import urllib.error
from pathlib import Path
from urllib.parse import quote_plus

import pytest

import scripts._biogan as bio
import scripts.biogan_photos as mod

FIXTURES = Path(__file__).parent / "fixtures" / "biogan"
BASE = "https://shop.test"
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 64
HTML404 = b"<html><body>404 not found</body></html>"


def read_fixture(name: str) -> str:
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


def ean13(body12: str) -> str:
    """A valid EAN-13: the 12-digit body plus its check digit."""
    total = sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(body12))
    return body12 + str((10 - total % 10) % 10)


def make_row_csv(tmp_path: Path, rows: list[tuple[str, str]], *,
                 photo: dict[str, str] | None = None) -> Path:
    """A CRLF master CSV with one row per (sku, name) and generated EANs."""
    photo = photo or {}
    lines = ["Varenummer,Varenavn,EAN-nummer,Antal pr colli,Sp2-Engros,"
             "Sp3-Vejl.Pris,URL to photo"]
    for i, (sku, name) in enumerate(rows, start=1):
        lines.append(f'{sku},{name},{ean13(f"57000000{i:04d}")},1,10.00,'
                     f'20.00,{photo.get(sku, "")}')
    path = tmp_path / "rows.csv"
    path.write_bytes("\r\n".join(lines).encode("utf-8"))
    return path


def search_url(keyword: str) -> str:
    return f"{BASE}{bio.SEARCH_PATH}?keyword={quote_plus(keyword)}"


def product_page(product_id: str, name: str, img: str) -> str:
    """A one-product result page in the site's shape (for probe pages)."""
    return ('<html><body><ul class="ProductList_Custom_UL"><li>'
            f'<input type="hidden" name="ProductID" value="{product_id}">'
            f'<div class="image"><img src="{img}"></div>'
            f'<div class="name"><a href="/shop/{product_id}-p.html" '
            f'title="{name}">{name}</a></div>'
            f'<div class="productnumber">{product_id}</div>'
            '<div class="price">10,00 DKK</div></li></ul></body></html>')


def prefilled_csv(tmp_path: Path, cell: str) -> Path:
    """The fixture CSV with row 1134's prefilled cell replaced by `cell`."""
    csv_path = make_csv(tmp_path)
    csv_path.write_bytes(csv_path.read_bytes().replace(PREFILLED.encode(),
                                                       cell.encode()))
    return csv_path


def image_url(path: str, *, big: bool = True) -> str:
    thumb = bio.canonical_image_url(path, BASE + bio.SEARCH_PATH)
    return bio.big_variant(thumb) if big else thumb


NAME_10005 = bio.clean_name("Mosaic kobberflaske 900ml, 900 ml")
NAME_1509B = bio.clean_name("LOPPEFRØSKALLER/psyllium  fintmalede 200g ØKO")
P_10005 = image_url("/images/10005 Mosaik flaske-t.jpg")
T_10005 = image_url("/images/10005 Mosaik flaske-t.jpg", big=False)
P_1509B = image_url("/images/1509b_300ppi-t.png")
P_1134 = image_url("/images/produktbilleder/1134_web-t.jpg")
PREFILLED = "https://shop.biogan.dk/images/produktbilleder/1134_web-p.jpg"
# the -t sibling of the prefilled cell (same host as the cell, not BASE)
PREFILLED_T = "https://shop.biogan.dk/images/produktbilleder/1134_web-t.jpg"


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200,
                 ctype: str = "text/html; Charset=UTF-8", url: str = ""):
        self._body = body
        self.status = status
        self.headers = {"Content-Type": ctype, "ETag": '"etag"'}
        self._url = url
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._body[self._pos:]
            self._pos = len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        return None


class FakeOpener:
    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.calls.append(url)
        route = self.routes.get(url)
        if route is None:
            raise urllib.error.HTTPError(url, 404, "missing",
                                         {"Content-Type": "text/html"},
                                         io.BytesIO(HTML404))
        body, status, ctype = route
        if isinstance(body, str):
            body = body.encode("utf-8")
        if status >= 400:
            raise urllib.error.HTTPError(url, status, "error",
                                         {"Content-Type": ctype},
                                         io.BytesIO(body))
        return FakeResponse(body, status, ctype, url)


def run(tmp_path: Path, routes: dict, *args: str, csv_path: Path | None = None,
        images: Path | None = None, opener=None):
    """Run the CLI with F12 injection: opener/sleep/clock go through main()."""
    fake = opener if opener is not None else FakeOpener(routes)
    code = mod.main([
        "--csv", str(csv_path or tmp_path / "masterdata.csv"),
        "--images-dir", str(images or tmp_path / "images"),
        "--base-url", BASE, "--delay", "0", *args,
    ], opener=fake, sleep=lambda _s: None, clock=time.monotonic)
    return code, fake


def cell(csv_path: Path, sku: str) -> str:
    md = bio.read_masterdata(csv_path)
    return next(r.photo for r in md.rows if r.sku == sku)


def clean_routes() -> dict:
    return {
        search_url(NAME_10005): (read_fixture("search_10005.html"), 200, "text/html"),
        search_url(NAME_1509B): (read_fixture("search_1509b.html"), 200, "text/html"),
        P_10005: (JPEG, 200, "image/jpeg"),
        P_1509B: (PNG, 200, "image/png"),
        PREFILLED: (JPEG, 200, "image/jpeg"),
    }


def test_clean_run_fills_cells_and_downloads(tmp_path):
    csv_path = make_csv(tmp_path)
    code, fake = run(tmp_path, clean_routes(), "--report",
                     str(tmp_path / "report.json"))
    assert code == 0
    assert cell(csv_path, "10005") == P_10005
    assert cell(csv_path, "1134") == PREFILLED
    assert cell(csv_path, "1509b") == P_1509B
    images = tmp_path / "images"
    assert (images / "5901234123457.jpg").read_bytes() == JPEG
    assert (images / "4006381333931.jpg").read_bytes() == JPEG
    assert (images / "4260563290218.png").read_bytes() == PNG
    assert len(fake.calls) == 5
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["requests"] == 5
    assert {h["status"] for h in report["hits"]} == {"downloaded"}
    assert len(report["hits"]) == 3


def test_resume_makes_zero_requests(tmp_path):
    csv_path = make_csv(tmp_path)
    assert run(tmp_path, clean_routes())[0] == 0
    before = csv_path.read_bytes()
    code, fake = run(tmp_path, {})
    assert code == 0
    assert fake.calls == []
    assert csv_path.read_bytes() == before


def test_prefilled_cell_downloads_without_search(tmp_path):
    csv_path = make_csv(tmp_path)
    code, fake = run(tmp_path, {PREFILLED: (JPEG, 200, "image/jpeg")},
                     "--only-sku", "1134")
    assert code == 0
    assert fake.calls == [PREFILLED]
    assert cell(csv_path, "1134") == PREFILLED
    assert (tmp_path / "images" / "4006381333931.jpg").exists()


def test_p_falls_back_to_t(tmp_path):
    csv_path = make_csv(tmp_path)
    routes = {
        search_url(NAME_10005): (read_fixture("search_10005.html"), 200, "text/html"),
        T_10005: (JPEG, 200, "image/jpeg"),
    }
    code, fake = run(tmp_path, routes, "--only-sku", "10005", "--report",
                     str(tmp_path / "report.json"))
    assert code == 0
    assert fake.calls == [search_url(NAME_10005), P_10005, T_10005]
    assert cell(csv_path, "10005") == T_10005
    hit = json.loads((tmp_path / "report.json").read_text())["hits"][0]
    assert hit["photo_url"] == T_10005
    assert hit["detail"] == "p_missing_used_thumb"


def test_html_body_is_never_saved_as_an_image(tmp_path):
    csv_path = make_csv(tmp_path)
    routes = {
        search_url(NAME_10005): (read_fixture("search_10005.html"), 200, "text/html"),
        P_10005: (HTML404, 200, "text/html"),
        T_10005: (HTML404, 200, "text/html"),
    }
    code, _fake = run(tmp_path, routes, "--only-sku", "10005", "--report",
                      str(tmp_path / "report.json"))
    assert code == 1
    assert not list((tmp_path / "images").glob("*"))
    assert cell(csv_path, "10005") == P_10005
    hit = json.loads((tmp_path / "report.json").read_text())["hits"][0]
    assert hit["status"] == "not_an_image"


def test_sku_search_rescues_a_name_hit_without_the_exact_id(tmp_path):
    csv_path = make_csv(tmp_path)
    routes = {
        search_url(NAME_1509B): (read_fixture("search_1134.html"), 200, "text/html"),
        search_url("1509b"): (read_fixture("search_1509b.html"), 200, "text/html"),
        P_1509B: (PNG, 200, "image/png"),
    }
    code, _fake = run(tmp_path, routes, "--only-sku", "1509b", "--report",
                      str(tmp_path / "report.json"))
    assert code == 0
    hit = json.loads((tmp_path / "report.json").read_text())["hits"][0]
    assert hit["match_via"] == "sku_search"
    assert cell(csv_path, "1509b") == P_1509B


def test_first_result_is_flagged_and_fails(tmp_path):
    csv_path = make_csv(tmp_path)
    routes = {
        search_url(NAME_1509B): (read_fixture("search_1134.html"), 200, "text/html"),
        search_url("1509b"): (read_fixture("search_1134.html"), 200, "text/html"),
        P_1134: (JPEG, 200, "image/jpeg"),
    }
    code, _fake = run(tmp_path, routes, "--only-sku", "1509b", "--report",
                      str(tmp_path / "report.json"))
    assert code == 1
    hit = json.loads((tmp_path / "report.json").read_text())["hits"][0]
    assert hit["match_via"] == "first_result"
    assert "first_result flagged" in hit["review"]
    assert hit["site_id"] == "1134"


def test_f2_bad_ean_never_reaches_the_network_or_a_filename(tmp_path):
    csv_path = make_csv(tmp_path)
    csv_path.write_bytes(csv_path.read_bytes().replace(b"4260563290218",
                                                       b"4260563290219"))
    code, fake = run(tmp_path, {}, "--only-sku", "1509b",
                     "--report", str(tmp_path / "report.json"))
    assert code == 1
    assert fake.calls == []                    # no search, no image request
    assert cell(csv_path, "1509b") == ""
    assert not list((tmp_path / "images").glob("*"))
    hit = json.loads((tmp_path / "report.json").read_text())["hits"][0]
    assert hit["status"] == "bad_ean"
    code, fake = run(tmp_path, {}, "--only-sku", "1509b", "--dry-run")
    assert code == 1 and fake.calls == []      # gated on dry-run too


def test_f5_garbage_prefilled_cell_is_bad_cell_and_untouched(tmp_path):
    csv_path = make_csv(tmp_path)
    csv_path.write_bytes(csv_path.read_bytes().replace(
        PREFILLED.encode(), b"javascript:alert(1)"))
    code, fake = run(tmp_path, {}, "--only-sku", "1134",
                     "--report", str(tmp_path / "report.json"))
    assert code == 1
    assert fake.calls == []
    assert cell(csv_path, "1134") == "javascript:alert(1)"   # never touched
    hit = json.loads((tmp_path / "report.json").read_text())["hits"][0]
    assert hit["status"] == "bad_cell"
    assert hit["match_via"] == "prefilled"
    before = csv_path.read_bytes()
    code, fake = run(tmp_path, {}, "--only-sku", "1134", "--dry-run",
                     "--no-download")
    assert code == 1 and fake.calls == []
    assert csv_path.read_bytes() == before


def test_f5_force_reresolves_a_garbage_prefilled_cell(tmp_path):
    csv_path = make_csv(tmp_path)
    csv_path.write_bytes(csv_path.read_bytes().replace(
        PREFILLED.encode(), b"javascript:alert(1)"))
    routes = {
        search_url("Kobber tungeskraber"):
            (read_fixture("search_1134.html"), 200, "text/html"),
        P_1134: (JPEG, 200, "image/jpeg"),
    }
    code, fake = run(tmp_path, routes, "--only-sku", "1134", "--force")
    assert code == 0                       # --force reads no cell, repairs it
    assert fake.calls == [search_url("Kobber tungeskraber"), P_1134]
    assert cell(csv_path, "1134") == P_1134


def test_sku_search_with_no_hits_keeps_the_name_first_hit(tmp_path):
    csv_path = make_csv(tmp_path)
    routes = {
        search_url(NAME_1509B): (read_fixture("search_1134.html"), 200, "text/html"),
        search_url("1509b"): (read_fixture("search_none.html"), 200, "text/html"),
        P_1134: (JPEG, 200, "image/jpeg"),
    }
    code, fake = run(tmp_path, routes, "--only-sku", "1509b",
                     "--report", str(tmp_path / "report.json"))
    assert code == 1                       # flagged, never a silent no_result
    assert fake.calls == [search_url(NAME_1509B), search_url("1509b"), P_1134]
    hit = json.loads((tmp_path / "report.json").read_text())["hits"][0]
    assert hit["status"] == "downloaded"
    assert hit["match_via"] == "first_result"
    assert hit["site_id"] == "1134"        # the rung-1 hit, not the empty rung 2
    assert "sku_search_no_hits" in hit["review"]
    assert "first_result flagged" in hit["review"]
    assert cell(csv_path, "1509b") == P_1134


def test_breaker_trips_on_consecutive_parse_failures(tmp_path):
    make_csv(tmp_path)
    unparseable = read_fixture("search_unparseable.html")
    routes = {
        search_url(NAME_10005): (unparseable, 200, "text/html"),
        search_url(NAME_1509B): (unparseable, 200, "text/html"),
    }
    code, fake = run(tmp_path, routes, "--only-sku", "10005",
                     "--only-sku", "1509b", "--abort-after", "2",
                     "--report", str(tmp_path / "report.json"))
    assert code == 2
    assert len(fake.calls) == 2
    hits = json.loads((tmp_path / "report.json").read_text())["hits"]
    assert [h["status"] for h in hits] == ["parse_failure", "parse_failure"]


def test_breaker_default_is_five_consecutive_hard_failures(tmp_path):
    rows = [(f"90{i:03d}", f"Product {i}") for i in range(1, 7)]
    csv_path = make_row_csv(tmp_path, rows)
    unparseable = read_fixture("search_unparseable.html")
    routes = {search_url(f"Product {i}"): (unparseable, 200, "text/html")
              for i in range(1, 7)}
    code, fake = run(tmp_path, routes, "--report",
                     str(tmp_path / "report.json"), csv_path=csv_path)
    assert code == 2
    assert len(fake.calls) == 5            # the 6th row is never fetched
    hits = json.loads((tmp_path / "report.json").read_text())["hits"]
    assert [h["status"] for h in hits] == ["parse_failure"] * 5


def test_breaker_resets_on_a_row_that_is_not_a_hard_failure(tmp_path):
    make_csv(tmp_path)
    (tmp_path / "images").mkdir()
    # row 1134 is complete (prefilled cell + a valid image) -> status skipped
    (tmp_path / "images" / "4006381333931.jpg").write_bytes(JPEG)
    unparseable = read_fixture("search_unparseable.html")
    routes = {
        search_url(NAME_10005): (unparseable, 200, "text/html"),
        search_url(NAME_1509B): (unparseable, 200, "text/html"),
    }
    code, fake = run(tmp_path, routes, "--abort-after", "2")
    assert code == 1                       # parse_failure, skipped, parse_failure
    assert len(fake.calls) == 2


def test_no_result_and_strict_match_leave_the_cell_empty(tmp_path):
    csv_path = make_csv(tmp_path)
    routes = {search_url(NAME_1509B): (read_fixture("search_none.html"), 200, "text/html")}
    code, fake = run(tmp_path, routes, "--only-sku", "1509b")
    assert code == 1
    assert fake.calls == [search_url(NAME_1509B)]
    assert cell(csv_path, "1509b") == ""
    routes = {
        search_url(NAME_1509B): (read_fixture("search_1134.html"), 200, "text/html"),
        search_url("1509b"): (read_fixture("search_1134.html"), 200, "text/html"),
    }
    code, _ = run(tmp_path, routes, "--only-sku", "1509b", "--strict-match")
    assert code == 1
    assert cell(csv_path, "1509b") == ""


def test_parse_failure_saves_html(tmp_path):
    csv_path = make_csv(tmp_path)
    routes = {search_url(NAME_10005): (read_fixture("search_unparseable.html"), 200, "text/html")}
    code, _ = run(tmp_path, routes, "--only-sku", "10005",
                  "--save-html", str(tmp_path / "html"))
    assert code == 1
    assert (tmp_path / "html" / "2-5901234123457.html").is_file()
    assert cell(csv_path, "10005") == ""


def test_dry_run_writes_nothing(tmp_path):
    csv_path = make_csv(tmp_path)
    before = csv_path.read_bytes()
    code, _fake = run(tmp_path, clean_routes(), "--dry-run",
                      "--only-sku", "10005")
    assert code == 0
    assert csv_path.read_bytes() == before
    assert not (tmp_path / "images").exists()
    assert not (tmp_path / "masterdata.csv.bak").exists()
    assert not (tmp_path / "masterdata.csv.lock").exists()


def test_lock_held_exits_2(tmp_path):
    csv_path = make_csv(tmp_path)
    lock = csv_path.with_name(csv_path.name + ".lock")
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
    try:
        code, fake = run(tmp_path, clean_routes(), "--only-sku", "10005")
        assert code == 2
        assert fake.calls == []
    finally:
        lock.unlink(missing_ok=True)


def test_stale_lock_is_broken(tmp_path):
    csv_path = make_csv(tmp_path)
    lock = csv_path.with_name(csv_path.name + ".lock")
    lock.write_text("0\n", encoding="utf-8")
    code, _fake = run(tmp_path, clean_routes(), "--only-sku", "10005")
    assert code == 0
    assert not lock.exists()


def test_abort_after_hard_failures_exits_2(tmp_path):
    csv_path = make_csv(tmp_path)
    routes = {
        search_url(NAME_10005): (HTML404, 500, "text/html"),
        PREFILLED: (HTML404, 500, "text/html"),
    }
    code, _fake = run(tmp_path, routes, "--retries", "0", "--abort-after", "2")
    assert code == 2
    assert csv_path.read_bytes() == (FIXTURES / "masterdata.csv").read_bytes()


def test_limit_counts_work_rows(tmp_path):
    csv_path = make_csv(tmp_path)
    code, fake = run(tmp_path, clean_routes(), "--limit", "1")
    assert code == 0
    assert cell(csv_path, "10005") == P_10005
    assert cell(csv_path, "1134") == PREFILLED
    assert cell(csv_path, "1509b") == ""
    assert len(fake.calls) == 2  # one search + one image


def test_only_sku_and_no_download(tmp_path):
    csv_path = make_csv(tmp_path)
    code, _fake = run(tmp_path, {search_url(NAME_10005):
                                  (read_fixture("search_10005.html"), 200, "text/html")},
                      "--only-sku", "10005", "--no-download")
    assert code == 0
    assert cell(csv_path, "10005") == P_10005
    assert not (tmp_path / "images").exists()


def test_lf_with_final_newline_is_preserved(tmp_path):
    csv_path = make_csv(tmp_path, eol="\n", eof=True)
    routes = {search_url(NAME_10005): (read_fixture("search_10005.html"), 200, "text/html"),
              P_10005: (JPEG, 200, "image/jpeg")}
    assert run(tmp_path, routes, "--only-sku", "10005")[0] == 0
    raw = csv_path.read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    assert cell(csv_path, "10005") == P_10005


def test_download_failure_keeps_the_cell_and_resume_downloads(tmp_path):
    csv_path = make_csv(tmp_path)
    routes = {search_url(NAME_10005): (read_fixture("search_10005.html"), 200, "text/html"),
              P_10005: (HTML404, 500, "text/html")}
    code, _ = run(tmp_path, routes, "--only-sku", "10005", "--retries", "0")
    assert code == 1
    assert cell(csv_path, "10005") == P_10005
    code, fake = run(tmp_path, {P_10005: (JPEG, 200, "image/jpeg")},
                     "--only-sku", "10005")
    assert code == 0
    assert fake.calls == [P_10005]
    assert (tmp_path / "images" / "5901234123457.jpg").exists()


def test_report_path_inside_images_dir_is_usage_error(tmp_path):
    make_csv(tmp_path)
    code, _ = run(tmp_path, {}, "--only-sku", "10005",
                  "--report", str(tmp_path / "images" / "r.json"))
    assert code == 3


def test_unknown_only_sku_is_usage_error(tmp_path):
    make_csv(tmp_path)
    code, _ = run(tmp_path, {}, "--only-sku", "NOPE")
    assert code == 3


def test_missing_csv_is_usage_error(tmp_path):
    code, _ = run(tmp_path, {}, csv_path=tmp_path / "absent.csv")
    assert code == 3


def test_max_bytes_guard(tmp_path):
    csv_path = make_csv(tmp_path)
    routes = {
        search_url(NAME_10005): (read_fixture("search_10005.html"), 200, "text/html"),
        P_10005: (JPEG + b"x" * 100, 200, "image/jpeg"),
    }
    code, _fake = run(tmp_path, routes, "--only-sku", "10005",
                      "--max-bytes", "10", "--report",
                      str(tmp_path / "report.json"))
    assert code == 1
    hit = json.loads((tmp_path / "report.json").read_text())["hits"][0]
    assert hit["status"] == "too_large"
    assert not list((tmp_path / "images").glob("*"))


def test_per_row_flush_then_resume_only_the_remainder(tmp_path):
    csv_path = make_csv(tmp_path)
    routes = {
        search_url(NAME_10005): (read_fixture("search_10005.html"), 200, "text/html"),
        P_10005: (JPEG, 200, "image/jpeg"),
        search_url(NAME_1509B): (HTML404, 500, "text/html"),
    }
    code, _fake = run(tmp_path, routes, "--only-sku", "10005",
                      "--only-sku", "1509b", "--retries", "0")
    assert code == 1
    assert cell(csv_path, "10005") == P_10005
    assert (tmp_path / "images" / "5901234123457.jpg").exists()
    assert cell(csv_path, "1509b") == ""
    routes2 = {
        search_url(NAME_1509B): (read_fixture("search_1509b.html"), 200, "text/html"),
        P_1509B: (PNG, 200, "image/png"),
    }
    code2, fake = run(tmp_path, routes2, "--only-sku", "1509b")
    assert code2 == 0
    assert fake.calls == [search_url(NAME_1509B), P_1509B]
    assert cell(csv_path, "1509b") == P_1509B


def test_csv_fidelity_error_maps_to_exit_3(tmp_path, monkeypatch):
    make_csv(tmp_path)
    routes = {
        search_url(NAME_10005): (read_fixture("search_10005.html"), 200, "text/html"),
        P_10005: (JPEG, 200, "image/jpeg"),
    }

    def boom(*_args, **_kwargs):
        raise bio.CsvFidelityError("deliberate")

    monkeypatch.setattr(bio, "write_masterdata_atomic", boom)
    code, _fake = run(tmp_path, routes, "--only-sku", "10005")
    assert code == 3
    # F4: a post-verify/write failure still releases the lock
    assert not (tmp_path / "masterdata.csv.lock").exists()


def test_no_resume_reresolves_even_when_cached(tmp_path):
    make_csv(tmp_path)
    assert run(tmp_path, clean_routes())[0] == 0
    code, fake = run(tmp_path, clean_routes(), "--no-resume", "--only-sku",
                     "10005", "--report", str(tmp_path / "report.json"))
    assert code == 0
    # F9: --no-resume re-resolves the row (the search happens) but KEEPS the
    # magic-checked download cache: no second image GET, status "cached".
    assert fake.calls == [search_url(NAME_10005)]
    hit = json.loads((tmp_path / "report.json").read_text())["hits"][0]
    assert hit["status"] == "cached"
    assert (tmp_path / "images" / "5901234123457.jpg").read_bytes() == JPEG


def test_f7_budget_never_overshoots_mid_row(tmp_path):
    """F7: the budget is enforced inside the fetcher, before each request."""
    csv_path = make_csv(tmp_path)
    routes = {
        PREFILLED: (JPEG, 200, "image/jpeg"),
        search_url(NAME_1509B): (read_fixture("search_1134.html"), 200, "text/html"),
        search_url("1509b"): (read_fixture("search_1509b.html"), 200, "text/html"),
        P_1509B: (PNG, 200, "image/png"),
    }
    code, fake = run(tmp_path, routes, "--only-sku", "1134", "--only-sku",
                     "1509b", "--max-requests", "3", "--report",
                     str(tmp_path / "report.json"))
    assert code == 2
    assert len(fake.calls) == 3                  # the 4th request is refused
    assert cell(csv_path, "1509b") == P_1509B    # the resolved URL is knowledge
    hits = json.loads((tmp_path / "report.json").read_text())["hits"]
    assert hits[-1]["status"] == "error"
    assert "budget" in hits[-1]["detail"]


def test_f8_fresh_unreadable_lock_is_held_not_broken(tmp_path):
    """F8: a pidfile inside the grace window is never broken (mid-write race)."""
    make_csv(tmp_path)
    lock = tmp_path / "masterdata.csv.lock"
    lock.write_text("", encoding="utf-8")        # an acquirer that is mid-write
    code, fake = run(tmp_path, clean_routes(), "--only-sku", "10005")
    assert code == 2
    assert fake.calls == []
    assert lock.exists()                         # not broken, not emptied


def test_f8_stale_break_reason_lands_in_the_report(tmp_path):
    make_csv(tmp_path)
    lock = tmp_path / "masterdata.csv.lock"
    lock.write_text("0\n2020-01-01T00:00:00+00:00\nold argv\n", encoding="utf-8")
    routes = {
        search_url(NAME_10005): (read_fixture("search_10005.html"), 200, "text/html"),
        P_10005: (JPEG, 200, "image/jpeg"),
    }
    code, _fake = run(tmp_path, routes, "--only-sku", "10005", "--report",
                      str(tmp_path / "report.json"))
    assert code == 0
    assert not lock.exists()
    meta = json.loads((tmp_path / "report.json").read_text())
    assert "dead" in meta["lock_broken"]         # the reason is reported


def test_f9_part_files_use_the_contract_name_and_stale_parts_are_cleaned(
        tmp_path, monkeypatch):
    images = tmp_path / "images"
    images.mkdir()
    (images / "5901234123457.jpg.part-1").write_bytes(b"x")   # crashed predecessor
    (images / "5901234123457.png").write_bytes(PNG)            # stale other ext
    seen: list[str] = []
    real_replace = os.replace

    def spy_replace(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(mod.os, "replace", spy_replace)
    dest = mod._write_image(images, "5901234123457", JPEG, ".jpg")
    assert seen == [f"5901234123457.jpg.part-{os.getpid()}"]   # contract naming
    assert dest == images / "5901234123457.jpg"
    assert dest.read_bytes() == JPEG
    assert not (images / "5901234123457.jpg.part-1").exists()  # stale part gone
    assert not (images / "5901234123457.png").exists()         # stale ext gone


def test_f11_report_meta_unresolved_and_vocabulary(tmp_path):
    csv_path = make_csv(tmp_path)
    before = csv_path.read_bytes()
    routes = {
        search_url(NAME_1509B): (read_fixture("search_1134.html"), 200, "text/html"),
        search_url("1509b"): (read_fixture("search_1134.html"), 200, "text/html"),
        P_1134: (JPEG, 200, "image/jpeg"),
    }
    code, _fake = run(tmp_path, routes, "--only-sku", "1509b", "--report",
                      str(tmp_path / "report.json"))
    assert code == 1
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["size"] == len(before)                      # bytes, not --size
    assert report["variant"] == "p"
    assert isinstance(report["mode"], int)
    assert report["mode"] == stat.S_IMODE(csv_path.stat().st_mode)
    assert "1-based" in report["rec_index_base"]
    unresolved = report["unresolved"]
    assert [u["sku"] for u in unresolved] == ["1509b"]
    assert unresolved[0]["match_via"] == "first_result"
    assert unresolved[0]["rec_index"] == 4
    contract_statuses = {"ok", "cached", "downloaded", "no_result", "no_image",
                         "bad_ean", "bad_cell", "http_404", "too_large",
                         "not_an_image", "error", "parse_failure", "skipped"}
    contract_vias = {"exact_id", "exact_name", "sku_search", "first_result",
                     "prefilled", ""}
    assert set(report["status_vocabulary"]) == contract_statuses
    assert set(report["match_via_vocabulary"]) == contract_vias
    assert set(mod.STATUS_VOCABULARY) == contract_statuses
    assert set(mod.MATCH_VIA_VOCABULARY) == contract_vias
    for hit in report["hits"]:
        assert hit["status"] in contract_statuses
        assert hit["match_via"] in contract_vias
        assert "first_result flagged" in hit["review"]
    # G8: the top-level aggregates, with exact content
    assert report["match_via"] == {"first_result": 1}
    assert report["review"] == [
        {"rec_index": 4, "sku": "1509b", "notes": ["first_result flagged"]}
    ]


def test_f12_pinned_api_surface():
    import inspect
    from html.parser import HTMLParser
    assert issubclass(bio.ProductListParser, HTMLParser)     # a real subclass
    # G7: the complete pinned signature, not just its first two parameters
    params = list(inspect.signature(bio.sniff_image).parameters)
    assert params == ["body", "content_type"]
    assert bio.sniff_image(JPEG, "image/jpeg") == (".jpg", None)
    assert list(inspect.signature(mod.write_report).parameters) == [
        "path", "hits", "meta"]
    main_params = inspect.signature(mod.main).parameters
    assert {"opener", "sleep", "clock"} <= set(main_params)
    assert all(main_params[k].kind is inspect.Parameter.KEYWORD_ONLY
               for k in ("opener", "sleep", "clock"))


def test_g7_url_suffix_tier_is_the_callers_last_resort(tmp_path):
    """G7: sniff_image is pinned to two parameters, so tier 3 (URL suffix)
    lives in the caller. No magic and no Content-Type -> the suffix decides;
    a markup body with no Content-Type is still refused (no blank check)."""
    csv_path = make_csv(tmp_path)
    unknown = b"NO-MAGIC-BYTES" + b"x" * 32     # not markup, >= 12 bytes
    routes = {
        search_url(NAME_10005): (read_fixture("search_10005.html"), 200, "text/html"),
        P_10005: (unknown, 200, None),           # no Content-Type at all
    }
    code, _fake = run(tmp_path, routes, "--only-sku", "10005", "--report",
                      str(tmp_path / "report.json"))
    assert code == 0
    hit = json.loads((tmp_path / "report.json").read_text())["hits"][0]
    assert hit["status"] == "downloaded"
    assert hit["detail"] == "no_magic"
    assert (tmp_path / "images" / "5901234123457.jpg").read_bytes() == unknown

    sub = tmp_path / "m2"
    sub.mkdir()
    make_csv(sub)
    routes = {
        search_url(NAME_10005): (read_fixture("search_10005.html"), 200, "text/html"),
        P_10005: (HTML404, 200, None),            # markup, no Content-Type
        T_10005: (HTML404, 200, None),
    }
    code, _fake = run(sub, routes, "--only-sku", "10005")
    assert code == 1
    assert not list((sub / "images").glob("*"))  # never written


def test_g9_failed_pid_write_leaves_no_lock_file(tmp_path, monkeypatch):
    """G9: if writing the pid payload fails after os.open created the file,
    the pidfile is removed before the error propagates — no residue blocking
    every later crawl until the grace window passes."""
    lock_path = tmp_path / "m.csv.lock"

    def boom(*_args, **_kwargs):
        raise OSError("disk full while writing the pidfile")

    monkeypatch.setattr(mod.os, "fdopen", boom)
    with pytest.raises(OSError, match="disk full"):
        mod.Lock(lock_path).__enter__()
    monkeypatch.undo()
    assert not lock_path.exists()
    with mod.Lock(lock_path):                     # the next acquisition works
        assert lock_path.exists()
    assert not lock_path.exists()


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores directory mode bits")
def test_g10_lock_and_backup_oserror_is_usage_not_a_traceback(tmp_path):
    """G10: a read-only CSV directory makes the lock open (or backup_once)
    fail with OSError; that is a usage-class exit 3, never a traceback."""
    ro = tmp_path / "ro"
    ro.mkdir()
    csv_path = ro / "masterdata.csv"
    csv_path.write_bytes((FIXTURES / "masterdata.csv").read_bytes())
    images = tmp_path / "images"                 # writable, outside ro/
    ro.chmod(0o555)
    try:
        fake = FakeOpener(clean_routes())
        code = mod.main(["--csv", str(csv_path), "--images-dir", str(images),
                         "--base-url", BASE, "--delay", "0",
                         "--only-sku", "10005"],
                        opener=fake, sleep=lambda _s: None)
        assert code == 3                          # lock open fails: usage
        assert fake.calls == []
        assert not (ro / "masterdata.csv.lock").exists()
        fake = FakeOpener(clean_routes())
        code = mod.main(["--csv", str(csv_path), "--images-dir", str(images),
                         "--base-url", BASE, "--delay", "0",
                         "--only-sku", "10005", "--no-lock"],
                        opener=fake, sleep=lambda _s: None)
        assert code == 3                          # backup_once fails: usage
        assert fake.calls == []
    finally:
        ro.chmod(0o755)


def test_f13_dry_run_with_report_and_save_html_writes_nothing(tmp_path):
    csv_path = make_csv(tmp_path)
    before = csv_path.read_bytes()
    report = tmp_path / "report.json"
    html_dir = tmp_path / "html"
    routes = {search_url(NAME_10005):
              (read_fixture("search_unparseable.html"), 200, "text/html")}
    code, fake = run(tmp_path, routes, "--only-sku", "10005", "--dry-run",
                     "--report", str(report), "--save-html", str(html_dir))
    assert code == 1                                    # parse_failure stands
    assert fake.calls == [search_url(NAME_10005)]       # requests are allowed
    assert csv_path.read_bytes() == before
    assert not report.exists()
    assert not html_dir.exists()
    assert not (tmp_path / "images").exists()
    assert not (tmp_path / "masterdata.csv.bak").exists()
    assert not (tmp_path / "masterdata.csv.lock").exists()


def test_f13_lying_content_type_never_saves_html(tmp_path):
    csv_path = make_csv(tmp_path)
    routes = {
        search_url(NAME_10005): (read_fixture("search_10005.html"), 200, "text/html"),
        P_10005: (HTML404, 200, "image/jpeg"),
        T_10005: (HTML404, 200, "image/jpeg"),
    }
    code, _fake = run(tmp_path, routes, "--only-sku", "10005", "--report",
                      str(tmp_path / "report.json"))
    assert code == 1
    assert not list((tmp_path / "images").glob("*"))
    assert cell(csv_path, "10005") == P_10005           # the URL is knowledge
    hit = json.loads((tmp_path / "report.json").read_text())["hits"][0]
    assert hit["status"] == "not_an_image"


def test_f13_corrupt_and_zero_byte_cache_are_repaired(tmp_path):
    csv_path = make_csv(tmp_path)
    images = tmp_path / "images"
    images.mkdir()
    (images / "5901234123457.jpg").write_bytes(b"")       # zero-byte cache
    (images / "4260563290218.png").write_bytes(HTML404)   # HTML junk cache
    code, _fake = run(tmp_path, clean_routes(), "--report",
                      str(tmp_path / "report.json"), images=images)
    assert code == 0
    assert (images / "5901234123457.jpg").read_bytes() == JPEG
    assert (images / "4260563290218.png").read_bytes() == PNG
    hits = json.loads((tmp_path / "report.json").read_text())["hits"]
    assert {h["sku"]: h["status"] for h in hits} == {
        "10005": "downloaded", "1134": "downloaded", "1509b": "downloaded"}


def test_f13_interrupt_mid_run_flushes_and_exits_2(tmp_path):
    csv_path = make_csv(tmp_path)

    class InterruptingOpener(FakeOpener):
        def __init__(self, routes, after):
            super().__init__(routes)
            self.after = after

        def __call__(self, request, timeout=None):
            if len(self.calls) >= self.after:
                raise KeyboardInterrupt
            return super().__call__(request, timeout)

    code, fake = run(tmp_path, clean_routes(), "--only-sku", "10005",
                     "--only-sku", "1134",
                     opener=InterruptingOpener(clean_routes(), 2))
    assert code == 2
    assert len(fake.calls) == 2
    assert cell(csv_path, "10005") == P_10005             # completed row flushed
    assert (tmp_path / "images" / "5901234123457.jpg").read_bytes() == JPEG
    assert not (tmp_path / "masterdata.csv.lock").exists()  # lock released
    assert (tmp_path / "masterdata.csv.bak").exists()
    bio.read_masterdata(csv_path)                         # still a valid CSV


def test_f13_lock_released_on_write_and_report_errors(tmp_path, monkeypatch):
    make_csv(tmp_path)
    routes = {
        search_url(NAME_10005): (read_fixture("search_10005.html"), 200, "text/html"),
        P_10005: (JPEG, 200, "image/jpeg"),
    }

    def boom(*_args, **_kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(bio, "write_masterdata_atomic", boom)
    code, _fake = run(tmp_path, routes, "--only-sku", "10005")
    assert code == 3
    assert not (tmp_path / "masterdata.csv.lock").exists()
    monkeypatch.undo()
    code, _fake = run(tmp_path, routes, "--only-sku", "10005", "--report",
                      str(tmp_path / "missing-dir" / "r.json"))
    assert code == 3                                       # unwritable report
    assert not (tmp_path / "masterdata.csv.lock").exists()


def test_f14_cli_on_real_shape_changes_only_photo_cells(tmp_path):
    src = FIXTURES / "masterdata_real_shape.csv"
    csv_path = tmp_path / "masterdata.csv"
    csv_path.write_bytes(src.read_bytes())
    before = csv_path.read_bytes()
    mode_before = stat.S_IMODE(csv_path.stat().st_mode)
    code, _fake = run(tmp_path, clean_routes(), csv_path=csv_path)
    assert code == 0
    after = csv_path.read_bytes()
    assert after.endswith(b"\n") and b"\r" not in after    # LF + final newline
    assert stat.S_IMODE(csv_path.stat().st_mode) == mode_before
    a = before.decode("utf-8").split("\n")
    b = after.decode("utf-8").split("\n")
    assert len(a) == len(b)
    # only the 10005 and 1509b rows moved; header, blank row and the
    # prefilled 1134 row are byte-identical
    assert [i for i, (x, y) in enumerate(zip(a, b)) if x != y] == [2, 4]
    assert cell(csv_path, "1134") == PREFILLED           # prefilled cell kept


# --------------------------------------------------------------------------- #
# round-3 fixes (G1-G6, G11)
# --------------------------------------------------------------------------- #

def test_g1_report_path_cannot_overwrite_the_csv(tmp_path):
    """G1: --report on the CSV/bak/lock, or a non-.json path, is usage error 3."""
    csv_path = make_csv(tmp_path)
    before = csv_path.read_bytes()
    for bad in (csv_path, csv_path.with_name(csv_path.name + ".bak"),
                csv_path.with_name(csv_path.name + ".lock"),
                tmp_path / "report.txt"):
        code, fake = run(tmp_path, {}, "--report", str(bad))
        assert code == 3, bad
        assert fake.calls == []
    assert csv_path.read_bytes() == before          # the master CSV is intact
    assert not (tmp_path / "report.txt").exists()
    assert not csv_path.with_name(csv_path.name + ".bak").exists()


def test_g2_case_variant_product_id_is_not_exact_id(tmp_path):
    """G2: a '1509B' hit is never accepted as the identity of SKU '1509b'."""
    csv_path = make_csv(tmp_path)
    name_page = product_page("1134B", "Sibling Product", "/img/sib-t.jpg")
    sku_page = product_page("1509B", "Sibling Product", "/img/case-t.jpg")
    p_case = image_url("/img/case-t.jpg")
    routes = {
        search_url(NAME_1509B): (name_page, 200, "text/html"),
        search_url("1509b"): (sku_page, 200, "text/html"),
        p_case: (JPEG, 200, "image/jpeg"),
    }
    code, fake = run(tmp_path, routes, "--only-sku", "1509b", "--report",
                     str(tmp_path / "report.json"))
    assert len(fake.calls) == 3            # name search, SKU search, image
    assert code == 1                       # flagged first_result, never resolved
    hit = json.loads((tmp_path / "report.json").read_text())["hits"][0]
    assert hit["match_via"] == "first_result"
    assert hit["site_id"] == "1509B"       # the case variant, not an identity
    assert "first_result flagged" in hit["review"]


@pytest.mark.parametrize("extra", [["--size", "nope"], ["--no-such-flag"],
                                   ["--csv"]])
def test_g3_usage_errors_exit_3(tmp_path, extra):
    """G3: argparse failures are exit 3, never the aborted code 2."""
    make_csv(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        run(tmp_path, {}, *extra)
    assert excinfo.value.code == 3


def test_g4_limit_window_sits_above_the_data_gates(tmp_path):
    """G4: a bad-EAN row outside --limit must not report or change the exit."""
    rows = [("90001", "Alpha One"), ("90002", "Beta Two")]
    csv_path = make_row_csv(tmp_path, rows)
    second_ean = ean13("570000000002")
    csv_path.write_bytes(csv_path.read_bytes().replace(
        second_ean.encode(), b"4260563290219"))     # invalid check digit
    p_alpha = image_url("/img/alpha-t.jpg")
    routes = {
        search_url("Alpha One"): (product_page("90001", "Alpha One",
                                               "/img/alpha-t.jpg"),
                                  200, "text/html"),
        p_alpha: (JPEG, 200, "image/jpeg"),
    }
    code, fake = run(tmp_path, routes, "--limit", "1", "--report",
                     str(tmp_path / "report.json"), csv_path=csv_path)
    assert code == 0
    assert len(fake.calls) == 2
    report = json.loads((tmp_path / "report.json").read_text())
    assert [h["sku"] for h in report["hits"]] == ["90001"]
    assert report["skipped_by_limit"] == 1
    assert cell(csv_path, "90001") == p_alpha


def test_g5_failed_replace_keeps_the_previous_image(tmp_path, monkeypatch):
    """G5: the stale other-ext image is only dropped after the replace lands."""
    images = tmp_path / "images"
    images.mkdir()
    old = images / "5901234123457.png"
    old.write_bytes(PNG)

    def boom(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(mod.os, "replace", boom)
    with pytest.raises(OSError):
        mod._write_image(images, "5901234123457", JPEG, ".jpg")
    assert old.read_bytes() == PNG               # the old photo survived
    assert not (images / "5901234123457.jpg").exists()
    assert not list(images.glob("*.part-*"))     # part file cleaned up


def test_g6_prefilled_p_cell_falls_back_to_the_thumb(tmp_path):
    """G6: a prefilled -p cell whose -p URL 404s still tries -t."""
    csv_path = prefilled_csv(tmp_path, PREFILLED)
    routes = {PREFILLED_T: (JPEG, 200, "image/jpeg")}   # PREFILLED -> 404
    code, fake = run(tmp_path, routes, "--only-sku", "1134", "--report",
                     str(tmp_path / "report.json"))
    assert code == 0
    assert fake.calls == [PREFILLED, PREFILLED_T]
    hit = json.loads((tmp_path / "report.json").read_text())["hits"][0]
    assert hit["variant"] == "t" and hit["detail"] == "p_missing_used_thumb"
    assert cell(csv_path, "1134") == PREFILLED_T


def test_g6_prefilled_t_cell_honours_size_p(tmp_path):
    """G6: a -t cell re-downloaded with --size p asks for -p first."""
    csv_path = prefilled_csv(tmp_path, PREFILLED_T)
    routes = {PREFILLED: (JPEG, 200, "image/jpeg")}
    code, fake = run(tmp_path, routes, "--only-sku", "1134", "--size", "p",
                     "--report", str(tmp_path / "report.json"))
    assert code == 0
    assert fake.calls == [PREFILLED]              # not the -t cell URL
    hit = json.loads((tmp_path / "report.json").read_text())["hits"][0]
    assert hit["variant"] == "p"
    assert cell(csv_path, "1134") == PREFILLED


def test_g6_prefilled_t_cell_falls_back_to_big(tmp_path):
    """G6: a prefilled -t cell whose -t URL 404s still tries -p."""
    csv_path = prefilled_csv(tmp_path, PREFILLED_T)
    routes = {PREFILLED: (JPEG, 200, "image/jpeg")}     # -t -> 404
    code, fake = run(tmp_path, routes, "--only-sku", "1134", "--size", "t",
                     "--report", str(tmp_path / "report.json"))
    assert code == 0
    assert fake.calls == [PREFILLED_T, PREFILLED]
    hit = json.loads((tmp_path / "report.json").read_text())["hits"][0]
    assert hit["variant"] == "p" and hit["detail"] == "t_missing_used_p"
    assert cell(csv_path, "1134") == PREFILLED


def test_g11_max_image_bytes_is_assigned_once():
    """G11: the duplicated max_image_bytes assignment is gone."""
    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert src.count("max_image_bytes = args.max_bytes") == 1
