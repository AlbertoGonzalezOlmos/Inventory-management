"""Offline CLI tests: fake opener, synthetic CSV, no network and no real data/."""

from __future__ import annotations

import io
import json
import os
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
        images: Path | None = None):
    fake = FakeOpener(routes)

    class TestFetcher(mod.Fetcher):
        def __init__(self, **kwargs):
            kwargs.pop("opener", None)
            kwargs.setdefault("jitter", 0)
            kwargs.setdefault("sleep", lambda _s: None)
            super().__init__(opener=fake, **kwargs)

    original = mod.Fetcher
    mod.Fetcher = TestFetcher
    try:
        code = mod.main([
            "--csv", str(csv_path or tmp_path / "masterdata.csv"),
            "--images-dir", str(images or tmp_path / "images"),
            "--base-url", BASE, "--delay", "0", *args,
        ])
    finally:
        mod.Fetcher = original
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
    code, fake = run(tmp_path, clean_routes(), "--no-resume", "--only-sku", "10005")
    assert code == 0
    assert fake.calls == [search_url(NAME_10005), P_10005]
