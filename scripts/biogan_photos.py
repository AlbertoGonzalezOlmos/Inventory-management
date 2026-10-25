"""Crawl shop.biogan.dk for catalogue photos; fill the CSV and download images.

The CSV is the resume state: after every row the whole file is rewritten
atomically, so a crash loses at most the in-flight row. A byte self-check at
load and a post-verification of every temp file make a corrupted rewrite
impossible without raising first.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import _biogan as bio
from scripts._hcrm import (EXIT_CHECKS_FAILED, EXIT_OK, EXIT_UNREACHABLE,
                           EXIT_USAGE)

DEFAULT_UA = "hcrm-biogan-photos/0.1 (+internal catalogue sync)"
FAILING_STATUSES = {
    "no_result", "no_image", "http_404", "too_large", "not_an_image",
    "error", "parse_failure", "bad_ean", "bad_cell",
}
OK_STATUSES = {"ok", "cached", "downloaded", "skipped"}
# The row-level breaker (F6) counts only these; every other outcome resets it.
HARD_FAILURE_STATUSES = {"error", "parse_failure"}


class FetchError(RuntimeError):
    """Network/5xx failure after retries."""


class Http404(Exception):
    """A missing resource; never retried."""


class TooLarge(RuntimeError):
    """A response body exceeded the per-image cap while streaming."""


class LockHeld(RuntimeError):
    """Another crawler holds the CSV lock."""


@dataclass
class Hit:
    rec_index: int
    sku: str = ""
    ean: str = ""
    name: str = ""
    query: str = ""
    rung: str = ""
    n_hits: int = 0
    status: str = "ok"
    match_via: str = ""
    thumb_url: str = ""
    photo_url: str = ""
    variant: str = ""
    site_id: str = ""
    site_name: str = ""
    site_price_raw: str = ""
    image_path: str = ""
    bytes: int = 0
    sha256: str = ""
    content_type: str = ""
    etag: str = ""
    detail: str = ""
    review: list[str] = field(default_factory=list)
    candidates: list[tuple[str, str]] = field(default_factory=list)


def _hit(row: bio.Row) -> Hit:
    return Hit(rec_index=row.rec_index, sku=row.sku, ean=row.ean,
               name=row.name)


def _valid_image(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        head = path.read_bytes()[:32]
    except OSError:
        return False
    return bio.sniff_image(head, None)[0] is not None


def row_state(cell: str, image_path: Path | None, force: bool) -> str:
    cell = (cell or "").strip()
    if force:
        return "resolve"
    if cell and image_path and _valid_image(Path(image_path)):
        return "skip"
    if cell:
        return "download_only"
    return "resolve"


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        try:
            return parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError):
            return None


class Fetcher:
    """The only network boundary; opener/sleep/clock are injectable for tests."""

    def __init__(self, opener=urllib.request.urlopen, *, delay: float = 1.0,
                 timeout: float = 30.0, retries: int = 3, jitter: float = 0.2,
                 user_agent: str = DEFAULT_UA, base_url: str = bio.SITE_BASE,
                 sleep=time.sleep, clock=time.monotonic) -> None:
        self.opener = opener
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.jitter = jitter
        self.user_agent = user_agent
        self.base_url = base_url.rstrip("/")
        self.sleep = sleep
        self.clock = clock
        self.requests = 0
        # Row-level breaker streak, owned by main(): +1 for an `error` or
        # `parse_failure` row and reset by every other row outcome. The
        # transport layer must never touch it — a single successful GET inside
        # a failing row would otherwise erase the streak (F6).
        self.consecutive_hard = 0
        self.max_image_bytes: int | None = None
        self._last: float | None = None

    def _throttle(self) -> None:
        if self._last is not None:
            wait = self.delay + random.uniform(0, self.jitter) - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        self._last = self.clock()

    def _backoff(self, attempt: int, retry_after: float | None) -> None:
        if retry_after is not None:
            wait = max(0.0, min(60.0, retry_after))
        else:
            wait = min(30.0, self.delay * (2 ** attempt))
        self.sleep(wait + random.uniform(0, self.jitter))

    def _request(self, url: str, *, limit: int | None = None) -> tuple[bytes, str | None, str | None, str]:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle()
            self.requests += 1
            request = urllib.request.Request(
                url, headers={"User-Agent": self.user_agent})
            try:
                response = self.opener(request, timeout=self.timeout)
                try:
                    status = getattr(response, "status", 200)
                    if status == 404:
                        raise Http404(url)
                    if status == 429 or status >= 500:
                        last = FetchError(f"HTTP {status} for {url}")
                        if attempt < self.retries:
                            self._backoff(attempt, _retry_after(
                                response.headers.get("Retry-After")))
                        continue
                    chunks: list[bytes] = []
                    total = 0
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if limit is not None and total > limit:
                            raise TooLarge(total)
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    ctype = response.headers.get("Content-Type")
                    etag = response.headers.get("ETag")
                    return body, ctype, etag, response.geturl() or url
                finally:
                    close = getattr(response, "close", None)
                    if close:
                        close()
            except Http404:
                raise
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise Http404(url) from exc
                if exc.code == 429 or exc.code >= 500:
                    last = FetchError(f"HTTP {exc.code} for {url}")
                    if attempt < self.retries:
                        self._backoff(attempt, _retry_after(
                            exc.headers.get("Retry-After")))
                    continue
                raise FetchError(f"HTTP {exc.code} for {url}") from exc
            except (urllib.error.URLError, TimeoutError, socket.timeout,
                    OSError) as exc:
                last = FetchError(f"{type(exc).__name__}: {exc}")
                if attempt < self.retries:
                    self._backoff(attempt, None)
        raise last or FetchError(url)

    @staticmethod
    def _decode(body: bytes, content_type: str | None) -> str:
        match = re.search(r"charset=([\w-]+)", content_type or "", re.I)
        for enc in (match.group(1) if match else None, "utf-8", "latin-1"):
            if not enc:
                continue
            try:
                return body.decode(enc)
            except (LookupError, UnicodeDecodeError):
                continue
        return body.decode("utf-8", "replace")

    def search(self, keyword: str) -> tuple[str, str]:
        url = f"{self.base_url}{bio.SEARCH_PATH}?keyword={quote_plus(keyword)}"
        body, ctype, _etag, final = self._request(url)
        return self._decode(body, ctype), final

    def get_bytes(self, url: str) -> tuple[bytes, str | None, str | None]:
        body, ctype, etag, _final = self._request(
            url, limit=self.max_image_bytes)
        return body, ctype, etag


def _exact(products: list[bio.SiteProduct], sku: str) -> bio.SiteProduct | None:
    wanted = (sku or "").strip().casefold()
    if not wanted:
        return None
    for product in products:
        if (product.product_id or "").strip().casefold() == wanted:
            return product
    return None


def resolve(fetcher: Fetcher, row: bio.Row, *, size: str,
            strict: bool, save_html: Path | None = None) -> Hit:
    hit = _hit(row)
    if row.is_blank:
        hit.status = "skipped"
        return hit
    if not row.sku:
        hit.status = "bad_cell"
        hit.detail = "empty SKU"
        return hit
    if not row.ean:
        hit.status = "bad_ean"
        hit.detail = "empty EAN"
        return hit
    html = ""
    try:
        query = bio.clean_name(row.name) or row.sku
        html, final = fetcher.search(query)
        products, no_result = bio.parse_products(html)
        if not products and not no_result:
            raise bio.ParseError("search page has no products and no no-result marker")
        hit.query, hit.rung, hit.n_hits = query, "name", len(products)
        hit.candidates = [(p.product_id or p.number, p.name) for p in products[:5]]
        chosen, via = bio.pick(products, row.sku, row.name)

        if via not in ("exact_id", "exact_name"):
            if not products:
                hit.status = "no_result"
                return hit
            html2, final2 = fetcher.search(row.sku)
            products2, no2 = bio.parse_products(html2)
            if not products2 and not no2:
                raise bio.ParseError("SKU search page has no products and no marker")
            if products2:
                exact = _exact(products2, row.sku)
                if exact is not None:
                    chosen, via = exact, "sku_search"
                else:
                    chosen, via = products2[0], "first_result"
                hit.query, hit.rung = row.sku, "sku"
                hit.n_hits = len(products2)
                hit.candidates = [(p.product_id or p.number, p.name)
                                  for p in products2[:5]]
                final = final2
            else:
                # F1: the SKU search found nothing, but the name search did.
                # Never downgrade a known hit to no_result: keep the rung-1
                # first hit (still flagged, so the run fails) and keep its
                # query/hits so the report says where the photo came from.
                chosen, via = products[0], "first_result"
                hit.review.append("sku_search_no_hits")

        if via == "first_result" and strict:
            hit.status = "no_result"
            hit.match_via = "first_result"
            hit.detail = "strict_match rejected first_result"
            return hit
        if chosen is None:
            hit.status = "no_result"
            return hit

        hit.match_via = via
        hit.site_id = chosen.product_id or chosen.number
        hit.site_name = chosen.name
        hit.site_price_raw = chosen.price_text
        if (chosen.product_id and chosen.number
                and chosen.product_id.casefold() != chosen.number.casefold()):
            hit.review.append("id/number mismatch")
        if via == "first_result":
            hit.review.append("first_result flagged")
        if not chosen.images:
            hit.status = "no_image"
            return hit
        thumb = bio.canonical_image_url(chosen.images[0], final)
        hit.thumb_url = thumb
        if size == "p":
            hit.photo_url, hit.variant = bio.big_variant(thumb), "p"
        else:
            hit.photo_url, hit.variant = thumb, "t"
        hit.status = "ok"
        return hit
    except bio.ParseError as exc:
        hit.status = "parse_failure"
        hit.detail = str(exc)
        if save_html and html:
            save_html.mkdir(parents=True, exist_ok=True)
            (save_html / f"{row.rec_index}-{row.ean}.html").write_text(
                html, encoding="utf-8")
        return hit
    except Http404:
        hit.status = "http_404"
        hit.detail = "search returned 404"
        return hit
    except FetchError as exc:
        hit.status = "error"
        hit.detail = str(exc)
        return hit
    except (ValueError, OSError) as exc:
        hit.status = "error"
        hit.detail = str(exc)
        return hit


def _attempts(hit: Hit) -> list[tuple[str, str, str]]:
    attempts = [(hit.photo_url, hit.variant, "")]
    if hit.variant == "p" and hit.thumb_url and hit.thumb_url != hit.photo_url:
        attempts.append((hit.thumb_url, "t", "p_missing_used_thumb"))
    elif hit.variant == "t" and hit.photo_url:
        big = bio.big_variant(hit.photo_url)
        if big != hit.photo_url:
            attempts.append((big, "p", "t_missing_used_p"))
    return attempts


def _write_image(images_dir: Path, ean: str, body: bytes,
                 ext: str) -> Path:
    dest = images_dir / f"{ean}{ext}"
    tmp = images_dir / f".{ean}{ext}.part-{os.getpid()}"
    try:
        with open(tmp, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        for stale in list(images_dir.glob(f"{ean}.*")):
            if stale != dest and stale.suffix.lower() in bio.IMAGE_EXTS:
                stale.unlink(missing_ok=True)
        for stale in list(images_dir.glob(f".{ean}.*.part-*")):
            if stale != tmp:
                stale.unlink(missing_ok=True)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()
    dest.chmod(0o644)
    return dest


def download(fetcher: Fetcher, hit: Hit, images_dir: Path, *,
             size: str, max_bytes: int, force: bool) -> Hit:
    if hit.status != "ok":
        return hit
    if not hit.ean:
        hit.status = "bad_ean"
        return hit
    images_dir = Path(images_dir)
    existing, candidates = bio.find_image(images_dir, hit.ean)
    if len(candidates) > 1:
        hit.detail = "duplicate_image:" + ",".join(p.name for p in candidates)
    if existing is not None and not force and _valid_image(existing):
        hit.status = "cached"
        hit.image_path = str(existing)
        hit.bytes = existing.stat().st_size
        hit.sha256 = hashlib.sha256(existing.read_bytes()).hexdigest()
        return hit

    last_status, last_detail = "http_404", "404"
    for url, variant, fallback_detail in _attempts(hit):
        try:
            body, ctype, etag = fetcher.get_bytes(url)
        except TooLarge:
            hit.status, hit.detail = "too_large", f">{max_bytes} bytes"
            return hit
        except Http404:
            last_status, last_detail = "http_404", "404"
            continue
        except FetchError as exc:
            hit.status, hit.detail = "error", str(exc)
            return hit
        ext, note = bio.sniff_image(body, ctype, url)
        if ext is None:
            last_status, last_detail = "not_an_image", note or ""
            continue
        try:
            path = _write_image(images_dir, hit.ean, body, ext)
        except OSError as exc:
            hit.status, hit.detail = "error", str(exc)
            return hit
        hit.status = "downloaded"
        hit.photo_url, hit.variant = url, variant
        hit.detail = fallback_detail or note or ""
        hit.image_path = str(path)
        hit.bytes = len(body)
        hit.sha256 = hashlib.sha256(body).hexdigest()
        hit.content_type = ctype or ""
        hit.etag = etag or ""
        return hit
    hit.status, hit.detail = last_status, last_detail
    return hit


class Lock:
    """A pidfile beside the CSV; stale means the pid is dead or too old."""

    def __init__(self, path: Path, stale_after: float = 7200) -> None:
        self.path = Path(path)
        self.stale_after = stale_after
        self._owned = False

    @staticmethod
    def _alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def __enter__(self) -> "Lock":
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                             0o644)
            except FileExistsError:
                try:
                    pid = int(self.path.read_text(encoding="utf-8").splitlines()[0])
                except (OSError, ValueError, IndexError):
                    pid = 0
                age = time.time() - self.path.stat().st_mtime
                if self._alive(pid) and age <= self.stale_after:
                    raise LockHeld(
                        f"{self.path} held by pid {pid} ({age:.0f}s old)")
                print(f"breaking stale lock {self.path} (pid {pid}, {age:.0f}s)",
                      file=sys.stderr)
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"{os.getpid()}\n{datetime.now(timezone.utc).isoformat()}\n"
                         f"{' '.join(sys.argv)}\n")
            self._owned = True
            return self
        raise LockHeld(f"{self.path} could not be acquired")

    def __exit__(self, *exc) -> None:
        if self._owned:
            self.path.unlink(missing_ok=True)
            self._owned = False


def _check_report_path(report: Path, images_dir: Path) -> None:
    report, images = report.resolve(), images_dir.resolve()
    if report == images or images in report.parents:
        raise ValueError("report path must be outside the images directory")


def _account_breaker(fetcher: Fetcher, hit: Hit, abort_after: int) -> bool:
    """F6: row-level breaker streak; True when the run must abort.

    Only an `error`/`parse_failure` row extends the streak. Every other
    outcome — including a skipped row or a data error such as `bad_ean` —
    proves the site answered, so it resets the streak.
    """
    if hit.status in HARD_FAILURE_STATUSES:
        fetcher.consecutive_hard += 1
    else:
        fetcher.consecutive_hard = 0
    return fetcher.consecutive_hard >= abort_after


def write_report(path: Path, hits: list[Hit], meta: dict) -> None:
    path = Path(path)
    _check_report_path(path, Path(meta["images_dir"]))
    payload = dict(meta)
    payload["hits"] = [asdict(h) for h in hits]
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def summarise(hits: list[Hit], *, requests: int, elapsed: float) -> str:
    counts = Counter(h.status for h in hits)
    parts = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    total = sum(h.bytes for h in hits)
    return (f"rows={len(hits)} {parts} requests={requests} "
            f"bytes={total} elapsed={elapsed:.1f}s")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default=str(bio.default_csv_path()))
    parser.add_argument("--images-dir", default=str(bio.default_images_dir()))
    parser.add_argument("--base-url", default=bio.SITE_BASE)
    parser.add_argument("--limit", type=int, default=0,
                        help="max rows that need work (0 = all)")
    parser.add_argument("--only-sku", action="append", default=[],
                        metavar="SKU")
    parser.add_argument("--skip-sku", action="append", default=[],
                        metavar="SKU")
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction,
                        default=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--strict-match", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-download", action="store_true")
    parser.add_argument("--size", choices=("p", "t"), default="p")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--abort-after", type=int, default=5)
    parser.add_argument("--max-requests", type=int, default=2000)
    parser.add_argument("--max-bytes", type=int, default=20_000_000)
    parser.add_argument("--user-agent", default=DEFAULT_UA)
    parser.add_argument("--report", default="")
    parser.add_argument("--save-html", default="")
    parser.add_argument("--lock", action=argparse.BooleanOptionalAction,
                        default=True)
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    started = time.monotonic()
    csv_path = Path(args.csv)
    images_dir = Path(args.images_dir)
    try:
        md = bio.read_masterdata(csv_path)
    except (bio.CsvShapeError, bio.CsvFidelityError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    known = {r.sku.casefold() for r in md.rows if not r.is_blank}
    only = {s.casefold() for s in args.only_sku}
    skip = {s.casefold() for s in args.skip_sku}
    unknown = sorted(only - known)
    if unknown:
        print(f"error: unknown --only-sku: {', '.join(unknown)}", file=sys.stderr)
        return EXIT_USAGE
    if args.report:
        try:
            _check_report_path(Path(args.report), images_dir)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE
    if not args.dry_run and not args.no_download:
        try:
            images_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE
        if not os.access(images_dir, os.W_OK):
            print(f"error: images dir not writable: {images_dir}", file=sys.stderr)
            return EXIT_USAGE

    selected = [
        r for r in md.rows
        if not r.is_blank
        and (not only or r.sku.casefold() in only)
        and r.sku.casefold() not in skip
    ]
    force = args.force or not args.resume
    # Working copy; md.records stays the pristine load-time baseline that
    # write_masterdata_atomic verifies the written temp against (F4).
    records = [list(rec) for rec in md.records]
    fetcher = Fetcher(opener=urllib.request.urlopen, delay=args.delay,
                      timeout=args.timeout, retries=args.retries,
                      user_agent=args.user_agent, base_url=args.base_url)
    fetcher.max_image_bytes = args.max_bytes
    hits: list[Hit] = []
    aborted = False
    work = 0
    lock: Lock | None = None
    try:
        if args.lock and not args.dry_run:
            lock = Lock(csv_path.with_name(csv_path.name + ".lock"))
            lock.__enter__()
        if not args.dry_run and not args.no_backup:
            bio.backup_once(csv_path)
        for row in selected:
            try:
                bio.ean_to_gtin14(row.ean)  # F2: before any filename/glob/request
            except ValueError as exc:
                hit = _hit(row)
                hit.status = "bad_ean"
                hit.detail = str(exc)
                hits.append(hit)
                _account_breaker(fetcher, hit, args.abort_after)  # resets
                if args.verbose and not args.quiet:
                    print(f"[bad_ean] {row.sku} {row.ean} {hit.detail}")
                continue
            if row.photo and not force:
                try:
                    # F5: a prefilled cell is trusted only after it validates;
                    # on failure the cell is left exactly as it is. `--force`/
                    # `--no-resume` re-resolve the row from scratch and never
                    # read the cell, so they may repair a garbage cell instead
                    # of being blocked by it.
                    bio.canonical_image_url(row.photo, fetcher.base_url)
                except ValueError as exc:
                    hit = _hit(row)
                    hit.status = "bad_cell"
                    hit.detail = str(exc)
                    hit.match_via = "prefilled"
                    hits.append(hit)
                    _account_breaker(fetcher, hit, args.abort_after)  # resets
                    if args.verbose and not args.quiet:
                        print(f"[bad_cell] {row.sku} {row.ean} {hit.detail}")
                    continue
            existing = bio.find_image(images_dir, row.ean)[0] if row.ean else None
            state = row_state(row.photo, existing, force)
            if args.limit and state != "skip":
                if work >= args.limit:
                    break
                work += 1
            if state == "skip":
                hit = _hit(row)
                hit.status = "skipped"
                hits.append(hit)
                _account_breaker(fetcher, hit, args.abort_after)  # resets
                continue
            if state == "download_only":
                hit = _hit(row)
                hit.status = "ok"
                hit.match_via = "prefilled"
                hit.photo_url = hit.thumb_url = row.photo
            else:
                hit = resolve(fetcher, row, size=args.size,
                              strict=args.strict_match,
                              save_html=Path(args.save_html)
                              if args.save_html and not args.dry_run else None)
            if hit.status == "ok" and not args.no_download and not args.dry_run:
                hit = download(fetcher, hit, images_dir, size=args.size,
                               max_bytes=args.max_bytes, force=force)
            new_cell = hit.photo_url or row.photo
            if records[row.rec_index][md.photo_idx].strip() != new_cell.strip():
                records[row.rec_index][md.photo_idx] = new_cell
                if not args.dry_run:
                    try:
                        bio.write_masterdata_atomic(csv_path, md, records)
                    except (bio.CsvShapeError, bio.CsvFidelityError, OSError) as exc:
                        print(f"error: cannot write CSV: {exc}", file=sys.stderr)
                        return EXIT_USAGE
            hits.append(hit)
            if args.verbose and not args.quiet:
                print(f"[{hit.status}] {row.sku} {row.ean} {hit.match_via} "
                      f"{hit.photo_url}")
            if _account_breaker(fetcher, hit, args.abort_after):
                print(f"error: {fetcher.consecutive_hard} consecutive hard "
                      f"failures; aborting", file=sys.stderr)
                aborted = True
                break
            if fetcher.requests >= args.max_requests:
                print("error: request budget exhausted", file=sys.stderr)
                aborted = True
                break
    except LockHeld as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE
    except KeyboardInterrupt:
        print("interrupted; completed rows are already flushed", file=sys.stderr)
        aborted = True
    finally:
        if lock is not None:
            lock.__exit__(None, None, None)

    elapsed = time.monotonic() - started
    if args.report and not args.dry_run:
        meta = {
            "csv": str(csv_path), "csv_sha256_before": md.snap.sha256,
            "eol": md.snap.eol, "eof_newline": md.snap.eof_newline,
            "mode": md.snap.mode, "size": args.size,
            "images_dir": str(images_dir), "requests": fetcher.requests,
            "counts": dict(Counter(h.status for h in hits)),
            "elapsed": round(elapsed, 3),
        }
        try:
            write_report(Path(args.report), hits, meta)
        except (OSError, ValueError) as exc:
            print(f"error: cannot write report: {exc}", file=sys.stderr)
            return EXIT_USAGE
    if not args.quiet:
        for hit in hits:
            if hit.status not in OK_STATUSES or hit.match_via == "first_result":
                print(f"[{hit.status}] {hit.sku} {hit.ean} "
                      f"{hit.match_via} {hit.detail} {hit.photo_url}".rstrip())
        print(summarise(hits, requests=fetcher.requests, elapsed=elapsed))

    if aborted:
        return EXIT_UNREACHABLE
    failed = any(h.status not in OK_STATUSES or h.match_via == "first_result"
                 for h in hits)
    return EXIT_CHECKS_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
