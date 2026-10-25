"""Shared, pure helpers for the Biogan catalogue crawler and importer.

Byte fidelity is the load-bearing contract: the master CSV is untracked, so a
bad rewrite has no git recovery. `read_masterdata` proves the round-trip once at
load and every atomic write re-reads its temp file before replacing the target.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import logging
import os
import re
import shutil
import stat
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

logger = logging.getLogger("hcrm")

REPO_ROOT = Path(__file__).resolve().parents[1]

IMAGE_COLUMN = "URL to photo"
SITE_BASE = "https://shop.biogan.dk"
SEARCH_PATH = "/shop/search-1.html"
NO_RESULT_MARKERS = ("Ingen varer fundet", "NoProcuctsFound")
SKIN_SRC_MARKERS = (
    "/images/skins/", "btn-favorite", "icon-not-in-stock", "logo", "trustpilot",
    "oeko-dansk", "Banner_", "favicon", "/images/design/",
)
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif",
}
MIN_IMAGE_BYTES = 12  # no real image header is shorter than this
REQUIRED_HEADERS = ("Varenummer", "Varenavn", "EAN-nummer", "Sp3-Vejl.Pris")


class CsvShapeError(ValueError):
    """The master CSV is missing, malformed, or has duplicate identities."""


class CsvFidelityError(ValueError):
    """A byte round-trip would not reproduce the input (or the written temp)."""


class ParseError(ValueError):
    """A search page has neither products nor a no-result marker."""


@dataclass(frozen=True)
class Row:
    rec_index: int
    sku: str
    name: str
    ean: str
    price: str
    photo: str
    is_blank: bool


@dataclass(frozen=True)
class Snapshot:
    raw: bytes
    sha256: str
    mode: int
    had_bom: bool
    eol: str
    eof_newline: bool
    n_records: int


@dataclass(frozen=True)
class MasterData:
    records: list[list[str]]
    rows: list[Row]
    snap: Snapshot
    photo_idx: int
    column_appended: bool


@dataclass(frozen=True)
class SiteProduct:
    product_id: str | None
    number: str
    name: str
    href: str
    images: tuple[str, ...]
    price_text: str


def default_data_dir() -> Path:
    return Path(os.environ.get("HCRM_DATA_DIR") or REPO_ROOT / "data")


def default_csv_path() -> Path:
    return default_data_dir() / "Biogan - masterdata - Sheet.csv"


def default_images_dir() -> Path:
    return Path(
        os.environ.get("HCRM_CATALOGUE_IMAGES_DIR")
        or default_data_dir() / "Catalogue-images"
    )


def clean_name(s: str) -> str:
    return " ".join((s or "").split())


def _header_index(header: list[str], name: str) -> int:
    return [c.strip() for c in header].index(name)


def read_masterdata(path: Path) -> MasterData:
    p = Path(path)
    if not p.is_file():
        raise CsvShapeError(f"master CSV not found: {p}")
    raw = p.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvShapeError(f"{p}: not UTF-8 ({exc})") from exc
    records = list(csv.reader(io.StringIO(text, newline="")))
    if len(records) < 2:
        raise CsvShapeError(f"{p}: fewer than two records")
    if len(records) != len(text.splitlines()):
        raise CsvShapeError(f"{p}: embedded newline(s) in a quoted field")
    width = len(records[0])
    if any(len(r) != width for r in records):
        raise CsvShapeError(f"{p}: records do not share one field count ({width})")
    header = [c.strip() for c in records[0]]
    for name in REQUIRED_HEADERS:
        if name not in header:
            raise CsvShapeError(f"{p}: missing required header {name!r}")

    sku_i = _header_index(records[0], "Varenummer")
    ean_i = _header_index(records[0], "EAN-nummer")
    seen_sku: set[str] = set()
    seen_ean: set[str] = set()
    for i, rec in enumerate(records[1:], start=1):
        if all(not c.strip() for c in rec):
            continue
        sku = rec[sku_i].strip()
        ean = rec[ean_i].strip()
        if not sku or not ean:
            raise CsvShapeError(f"{p}: record {i + 1}: empty SKU or EAN")
        # F10: SKUs are case-sensitive in the wild ("1134b" vs "1134B" would
        # collide on the site and in reports), so the duplicate check folds
        # case. EANs are digits; exact comparison stays correct for them.
        if sku.casefold() in seen_sku:
            raise CsvShapeError(f"{p}: duplicate SKU {sku!r}")
        if ean in seen_ean:
            raise CsvShapeError(f"{p}: duplicate EAN {ean!r} (filename collision)")
        seen_sku.add(sku.casefold())
        seen_ean.add(ean)

    eol = "\r\n" if b"\r\n" in raw else "\n"
    snap = Snapshot(
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        mode=stat.S_IMODE(p.stat().st_mode),
        had_bom=raw.startswith(b"\xef\xbb\xbf"),
        eol=eol,
        eof_newline=raw.endswith(eol.encode("ascii")),
        n_records=len(records),
    )
    md = MasterData(records=records, rows=[], snap=snap, photo_idx=0,
                    column_appended=False)
    self_check(md)
    photo_idx, appended = ensure_photo_column(records)
    if appended:
        logger.warning("%s: 7th header is foreign; appended %r as a new column",
                       p, IMAGE_COLUMN)
    rows = _build_rows(records, photo_idx)
    return MasterData(records=records, rows=rows, snap=snap,
                      photo_idx=photo_idx, column_appended=appended)


def _build_rows(records: list[list[str]], photo_idx: int) -> list[Row]:
    header = records[0]
    sku_i = _header_index(header, "Varenummer")
    name_i = _header_index(header, "Varenavn")
    ean_i = _header_index(header, "EAN-nummer")
    price_i = _header_index(header, "Sp3-Vejl.Pris")
    rows: list[Row] = []
    for i, rec in enumerate(records[1:], start=1):
        rows.append(Row(
            rec_index=i,
            sku=rec[sku_i].strip(),
            name=rec[name_i],
            ean=rec[ean_i].strip(),
            price=rec[price_i].strip(),
            photo=rec[photo_idx].strip(),
            is_blank=all(not c.strip() for c in rec),
        ))
    return rows


def ensure_photo_column(records: list[list[str]]) -> tuple[int, bool]:
    """D1: name the nameless 7th column; append defensively if foreign."""
    header = records[0]
    if len(header) == 7 and not header[6].strip():
        header[6] = IMAGE_COLUMN
        return 6, False
    for idx, cell in enumerate(header):
        if cell.strip() == IMAGE_COLUMN:
            return idx, False
    header.append(IMAGE_COLUMN)
    for rec in records[1:]:
        rec.append("")
    return len(header) - 1, True


def serialise(records: list[list[str]], snap: Snapshot) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator=snap.eol, quoting=csv.QUOTE_MINIMAL)
    writer.writerows(records)
    data = buf.getvalue().encode("utf-8")
    eol = snap.eol.encode("utf-8")
    if not snap.eof_newline and data.endswith(eol):
        data = data[: -len(eol)]
    return data


def self_check(md: MasterData) -> None:
    expected = md.snap.raw
    if md.snap.had_bom:
        expected = expected[len(b"\xef\xbb\xbf"):]
    if serialise(md.records, md.snap) != expected:
        raise CsvFidelityError(
            "CSV byte round-trip differs from the input (hand-edited/unsupported "
            "quoting); refusing to rewrite"
        )


def write_masterdata_atomic(path: Path, md: MasterData,
                            records: list[list[str]]) -> None:
    """Write `records` atomically, verified against the pristine baseline.

    The payload is serialised from a deep copy, and the temp file is re-read and
    compared cell-by-cell against `md.records` (the load-time baseline, which
    callers must leave untouched), so an in-memory drift in any non-photo cell
    raises before `os.replace`. The EOL/EOF shape of the bytes actually written
    is verified too, not only the parsed records.
    """
    path = Path(path)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    payload = [list(rec) for rec in records]
    data = serialise(payload, md.snap)
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        temp_raw = tmp.read_bytes()
        temp_text = temp_raw.decode("utf-8")
        temp_records = list(csv.reader(io.StringIO(temp_text, newline="")))
        eol = md.snap.eol.encode("utf-8")
        problems: list[str] = []
        if len(temp_records) != len(md.records):
            problems.append("record count")
        elif any(len(r) != len(md.records[0]) for r in temp_records):
            problems.append("record width")
        else:
            for i, (before, after) in enumerate(zip(md.records, temp_records)):
                for j, (a, b) in enumerate(zip(before, after)):
                    if j != md.photo_idx and a != b:
                        problems.append(f"cell {i}:{j}")
        if (b"\r\n" in temp_raw) != (md.snap.eol == "\r\n"):
            problems.append("EOL shape")
        if temp_raw.endswith(eol) != md.snap.eof_newline:
            problems.append("EOF newline")
        if problems:
            raise CsvFidelityError(
                "written CSV failed post-verification: " + ", ".join(problems)
            )
        os.chmod(tmp, md.snap.mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def backup_once(path: Path) -> Path | None:
    path = Path(path)
    bak = path.with_name(path.name + ".bak")
    if bak.exists():
        return None
    shutil.copy2(path, bak)
    return bak


def parse_price_cents(s: str) -> int:
    text = (s or "").strip()
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", text):
        raise ValueError(f"not a dot-decimal price: {s!r}")
    cents = Decimal(text) * 100
    return int(cents.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def ean_to_gtin14(ean: str) -> str:
    from app.barcodes import normalize_input_gtin

    gtin = normalize_input_gtin((ean or "").strip())
    if gtin is None:
        raise ValueError(f"invalid GTIN: {ean!r}")
    return gtin


def canonical_image_url(src: str, base: str) -> str:
    src = html.unescape((src or "").strip())
    url = urljoin(base, src)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"not an http(s) image URL: {src!r}")
    path = quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query,
                       parts.fragment))


def big_variant(url: str) -> str:
    parts = urlsplit(url)
    head, _, seg = parts.path.rpartition("/")
    new_seg = re.sub(r"-t(\.[A-Za-z0-9]+)$", r"-p\1", seg, flags=re.I)
    if new_seg == seg:
        return url
    path = f"{head}/{new_seg}" if head else new_seg
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query,
                       parts.fragment))


def is_skin_src(src: str) -> bool:
    low = (src or "").lower()
    return any(marker.lower() in low for marker in SKIN_SRC_MARKERS)


def _looks_like_markup(body: bytes) -> bool:
    head = body.lstrip().removeprefix(b"\xef\xbb\xbf").lstrip()
    return head[:1] == b"<"


def sniff_image(body: bytes, content_type: str | None) -> tuple[str | None, str | None]:
    """Decide the extension from the bytes, never from a claim.

    Pinned two-parameter surface (FINAL-APPROACH-1.md / G7): the URL-suffix
    tier lives in the caller (`biogan_photos.download`), which is the only
    place that knows the URL. A body with no magic and no Content-Type gets
    the note "no_magic" so that caller can apply the suffix as its last
    resort; a *present* non-image Content-Type is a firm "not_an_image".
    """
    body = body or b""
    ctype = (content_type or "").split(";")[0].strip().lower()
    ct_ext = EXT_BY_CONTENT_TYPE.get(ctype)
    if len(body) < MIN_IMAGE_BYTES:
        return None, "not_an_image"  # empty/truncated: never a real image
    ext: str | None = None
    if body.startswith(b"\xff\xd8\xff"):
        ext = ".jpg"
    elif body.startswith(b"\x89PNG\r\n\x1a\n") or body.startswith(b"\x89PNG"):
        ext = ".png"
    elif body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        ext = ".webp"
    elif body[:6] in (b"GIF87a", b"GIF89a"):
        ext = ".gif"
    elif body[4:12] in (b"ftypavif", b"ftypavis"):
        ext = ".avif"
    if ext:
        note = None
        if ctype and ct_ext != ext:
            note = f"content_type_mismatch:{content_type}"
        return ext, note
    if _looks_like_markup(body):
        return None, "not_an_image"  # a lying image/* CT cannot save markup
    if ct_ext:
        return ct_ext, "no_magic"
    if not ctype:
        return None, "no_magic"  # nothing claimed: the caller may consult the URL
    return None, "not_an_image"  # the server said it is not an image


def find_image(images_dir: Path, ean: str) -> tuple[Path | None, list[Path]]:
    folder = Path(images_dir)
    if not folder.is_dir() or not ean:
        return None, []
    candidates = [
        p for p in folder.glob(f"{ean}.*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    ]
    candidates.sort(key=lambda p: (IMAGE_EXTS.index(p.suffix.lower()), p.name))
    return (candidates[0] if candidates else None), candidates


class ProductListParser(HTMLParser):
    """Scoped to ul.ProductList_Custom_UL; one record per li.

    ProductID is authoritative; images are captured only while a div with class
    "image" is open, and chrome/skin sources are filtered out (the 2134
    news.png trap). Price text excludes nested spans (the b2b label).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.products: list[SiteProduct] = []
        self.no_results = False
        self._ul_depth = 0
        self._li: dict | None = None
        self._div_stack: list[str] = []
        self._image_depth = 0
        self._name_depth = 0
        self._number_depth = 0
        self._price_depth = 0
        self._span_depth = 0
        self._text: dict[str, list[str]] = {}

    def _reset_li(self) -> None:
        self._li = {"product_id": None, "images": [], "href": ""}
        self._div_stack = []
        self._image_depth = self._name_depth = 0
        self._number_depth = self._price_depth = 0
        self._span_depth = 0
        self._text = {"name": [], "number": [], "price": []}

    def _close_li(self) -> None:
        if self._li is None:
            return
        li = self._li
        self._li = None
        number = clean_name(" ".join(self._text["number"]))
        name = clean_name(" ".join(self._text["name"]))
        price = clean_name(" ".join(self._text["price"]))
        pid = li["product_id"]
        if not (pid or number or li["images"]):
            return
        self.products.append(SiteProduct(
            product_id=pid or None,
            number=number or (pid or ""),
            name=name,
            href=li["href"],
            images=tuple(li["images"]),
            price_text=price,
        ))

    @staticmethod
    def _has_class(cls: str, name: str) -> bool:
        return name in cls.lower().split()

    def handle_starttag(self, tag: str, attrs) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        cls = a.get("class", "")
        if tag == "ul" and "productlist_custom_ul" in cls.lower():
            self._ul_depth += 1
            return
        if self._ul_depth <= 0:
            if "noprocuctsfound" in cls.lower():
                self.no_results = True
            return
        if tag == "li":
            self._close_li()
            self._reset_li()
            return
        if self._li is None:
            return
        if tag == "input" and a.get("name", "").lower() == "productid":
            self._li["product_id"] = a.get("value", "").strip()
        elif tag == "div":
            self._div_stack.append(cls)
            depth = len(self._div_stack)
            if self._has_class(cls, "image"):
                self._image_depth = depth
            if self._has_class(cls, "productnumber"):
                self._number_depth = depth
            if self._has_class(cls, "name"):
                self._name_depth = depth
            if self._has_class(cls, "price"):
                self._price_depth = depth
        elif tag == "img" and self._image_depth and len(self._div_stack) >= self._image_depth:
            src = a.get("src", "").strip()
            if src and not is_skin_src(src):
                self._li["images"].append(src)
        elif tag == "a" and self._image_depth and len(self._div_stack) >= self._image_depth:
            href = a.get("href", "").strip()
            if href.endswith("p.html"):
                self._li["href"] = href
        elif tag == "span" and self._price_depth:
            self._span_depth += 1

    def handle_data(self, data: str) -> None:
        if any(m.lower() in data.lower() for m in NO_RESULT_MARKERS):
            self.no_results = True
        if self._li is None:
            return
        depth = len(self._div_stack)
        if self._number_depth and depth >= self._number_depth:
            self._text["number"].append(data)
        if self._name_depth and depth >= self._name_depth:
            self._text["name"].append(data)
        if (self._price_depth and depth >= self._price_depth
                and self._span_depth == 0):
            self._text["price"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "ul" and self._ul_depth > 0:
            self._ul_depth -= 1
            if self._ul_depth == 0:
                self._close_li()
            return
        if tag == "li" and self._li is not None:
            self._close_li()
            return
        if tag == "span" and self._span_depth > 0:
            self._span_depth -= 1
            return
        if tag == "div" and self._li is not None and self._div_stack:
            self._div_stack.pop()
            depth = len(self._div_stack)
            if self._image_depth and depth < self._image_depth:
                self._image_depth = 0
            if self._name_depth and depth < self._name_depth:
                self._name_depth = 0
            if self._number_depth and depth < self._number_depth:
                self._number_depth = 0
            if self._price_depth and depth < self._price_depth:
                self._price_depth = 0


def parse_products(html_text: str) -> tuple[list[SiteProduct], bool]:
    parser = ProductListParser()
    parser.feed(html_text)
    parser.close()
    parser._close_li()
    if parser.products:
        return parser.products, False
    return [], parser.no_results


def pick(products: list[SiteProduct], sku: str,
         name: str) -> tuple[SiteProduct | None, str]:
    # G2: ProductID identity is exact (contract [C4]); casefolding here would
    # accept the case-variant sibling (1134B for 1134b) as "exact_id" and write
    # its photo with a fully-resolved status. F10's casefold stays in the
    # duplicate-SKU check only.
    wanted = (sku or "").strip()
    if wanted:
        for product in products:
            if (product.product_id or "").strip() == wanted:
                return product, "exact_id"
    wanted_name = clean_name(name).casefold()
    if wanted_name:
        for product in products:
            if clean_name(product.name).casefold() == wanted_name:
                return product, "exact_name"
    if products:
        return products[0], "first_result"
    return None, "none"
