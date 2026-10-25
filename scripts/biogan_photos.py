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
from urllib.parse import quote_plus, urlsplit, urlunsplit

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
# F11: the status/match_via vocabularies pinned by the contract
# (FINAL-APPROACH-1.md); the report embeds them so a consumer can validate it.
STATUS_VOCABULARY = frozenset(OK_STATUSES | FAILING_STATUSES)
MATCH_VIA_VOCABULARY = frozenset(
    {"exact_id", "exact_name", "sku_search", "first_result", "prefilled", ""})


class FetchError(RuntimeError):
    """Network/5xx failure after retries."""


class Http404(Exception):
    """A missing resource; never retried."""


class TooLarge(RuntimeError):
    """A response body exceeded the per-image cap while streaming."""


class BudgetExhausted(RuntimeError):
    """The --max-requests budget ran out (F7); a dedicated abort -> exit 2."""


class LockHeld(RuntimeError):
    """Another crawler holds the CSV lock."""


# F8: a pidfile this young whose pid cannot be read may simply be mid-write by
# another acquirer; it is held (never broken) until the grace window passes.
LOCK_GRACE_SECONDS = 60.0


@dataclass
class Hit:
    # rec_index is the 1-based data-row number: md.records[rec_index] addresses
    # the row (records[0] is the header). F11 documents this base in the report.
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
        # F7: --max-requests, enforced BEFORE each request (retries included)
        # so the budget is never overshot mid-row; budget_exhausted tells
        # main() to abort with exit 2 once the current row state is flushed.
        self.max_requests: int | None = None
        self.budget_exhausted = False
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
            if (self.max_requests is not None
                    and self.requests >= self.max_requests):
                self.budget_exhausted = True
                raise BudgetExhausted(
                    f"request budget of {self.max_requests} exhausted")
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
    # G2: exact ProductID equality, no casefolding — a case-variant sibling is
    # a different product, so it may only ever be a flagged first_result.
    wanted = (sku or "").strip()
    if not wanted:
        return None
    for product in products:
        if (product.product_id or "").strip() == wanted:
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
    except BudgetExhausted as exc:
        hit.status = "error"
        hit.detail = str(exc)
        return hit
    except (ValueError, OSError) as exc:
        hit.status = "error"
        hit.detail = str(exc)
        return hit


_SMALL_VARIANT_RE = re.compile(r"-p(\.[A-Za-z0-9]+)$", re.I)


def _swap_to(url: str, size: str) -> str:
    """`url` adjusted to the requested size when it carries a p/t suffix.

    size "p" maps -t<ext> -> -p<ext>; size "t" maps -p<ext> -> -t<ext>. A URL
    without the suffix (or already at the requested size) is returned as is, so
    prefilled cells work here as well as resolved thumbs (G6).
    """
    if size == "p":
        return bio.big_variant(url)
    parts = urlsplit(url)
    head, _, seg = parts.path.rpartition("/")
    new_seg = _SMALL_VARIANT_RE.sub(r"-t\1", seg)
    if new_seg == seg:
        return url
    path = f"{head}/{new_seg}" if head else new_seg
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query,
                       parts.fragment))


def _attempts(hit: Hit, size: str) -> list[tuple[str, str, str]]:
    """(url, variant, detail) download attempts, first choice by `size`.

    The base URL is `photo_url` (set for resolved hits and for prefilled cells
    alike), so the first attempt honours --size and the second swaps -p/-t on
    the actual URL once — prefilled `download_only` rows therefore get the same
    404/not-an-image fallback as resolved ones (G6).
    """
    base = hit.photo_url or hit.thumb_url
    if not base:
        return []
    first = _swap_to(base, size)
    other = _swap_to(first, "t" if size == "p" else "p")
    attempts = [(first, size, "")]
    if other and other != first:
        if size == "p":
            attempts.append((other, "t", "p_missing_used_thumb"))
        else:
            attempts.append((other, "p", "t_missing_used_p"))
    return attempts


def _write_image(images_dir: Path, ean: str, body: bytes,
                 ext: str) -> Path:
    dest = images_dir / f"{ean}{ext}"
    # F9: the contract's part-file name <ean>.<ext>.part-<pid>; the legacy
    # dot-prefixed form of older runs is cleaned up alongside it.
    tmp = images_dir / f"{ean}{ext}.part-{os.getpid()}"
    try:
        with open(tmp, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        # G5: land the new image first; only then drop the stale siblings, so a
        # failed replace can never leave the row with no image at all.
        os.replace(tmp, dest)
        for stale in list(images_dir.glob(f"{ean}.*")):
            if stale != dest and stale.suffix.lower() in bio.IMAGE_EXTS:
                stale.unlink(missing_ok=True)
        for pattern in (f"{ean}.*.part-*", f".{ean}.*.part-*"):
            for stale in list(images_dir.glob(pattern)):
                if stale != tmp:
                    stale.unlink(missing_ok=True)
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
    for url, variant, fallback_detail in _attempts(hit, size):
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
        except BudgetExhausted as exc:
            # F7: keep hit.photo_url — the resolved URL is knowledge and the
            # row is still flushed before main() aborts with exit 2.
            hit.status, hit.detail = "error", str(exc)
            return hit
        ext, note = bio.sniff_image(body, ctype)
        if ext is None and note == "no_magic" and not ctype:
            # G7: the contract's third tier (URL suffix), applied here because
            # sniff_image is pinned to two parameters. Only reachable when the
            # server claimed nothing at all and the body is not markup.
            suffix = Path(urlsplit(url).path).suffix.lower()
            if suffix in bio.IMAGE_EXTS:
                ext = ".jpg" if suffix == ".jpeg" else suffix
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
    """A pidfile beside the CSV; stale means the pid is dead or too old.

    F8: an unreadable pidfile inside the grace window is treated as a live
    acquirer mid-write and held; the pidfile is re-read immediately before
    the unlink so a lock replaced under us is never broken; every break is
    recorded in ``break_reason`` for the report.
    """

    def __init__(self, path: Path, stale_after: float = 7200,
                 grace: float = LOCK_GRACE_SECONDS) -> None:
        self.path = Path(path)
        self.stale_after = stale_after
        self.grace = grace
        self._owned = False
        # F8: the reason string of the stale lock this acquisition broke, or
        # None; main() copies it into the report as ``lock_broken``.
        self.break_reason: str | None = None

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

    def _read(self) -> tuple[int | None, str, float]:
        """(pid or None if unreadable, raw content, age in seconds)."""
        age = time.time() - self.path.stat().st_mtime
        try:
            content = self.path.read_text(encoding="utf-8")
        except OSError:
            return None, "", age
        try:
            return int(content.splitlines()[0]), content, age
        except (ValueError, IndexError):
            return None, content, age

    def _stale_reason(self, pid: int | None, age: float) -> str | None:
        """Why this lock is stale, or None while it must be held."""
        if pid is None:
            if age < self.grace:
                return None  # possibly mid-write: hold until the grace passes
            return f"unreadable pidfile, {age:.0f}s old"
        if not self._alive(pid):
            return f"dead pid {pid}"
        if age > self.stale_after:
            return f"pid {pid} alive but {age:.0f}s old"
        return None

    def __enter__(self) -> "Lock":
        for _ in range(3):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                             0o644)
            except FileExistsError:
                try:
                    pid, content, age = self._read()
                except FileNotFoundError:
                    continue  # released between the open attempt and the read
                reason = self._stale_reason(pid, age)
                if reason is None:
                    if pid is None:
                        raise LockHeld(
                            f"{self.path} held (pidfile unreadable, "
                            f"{age:.0f}s old, within {self.grace:.0f}s grace)")
                    raise LockHeld(
                        f"{self.path} held by pid {pid} ({age:.0f}s old)")
                # F8: re-read before the unlink — another acquirer may have
                # replaced the file between our read and this decision.
                try:
                    if self.path.read_text(encoding="utf-8") != content:
                        continue
                except OSError:
                    continue
                print(f"breaking stale lock {self.path} ({reason})",
                      file=sys.stderr)
                self.break_reason = reason
                try:
                    self.path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(f"{os.getpid()}\n{datetime.now(timezone.utc).isoformat()}\n"
                             f"{' '.join(sys.argv)}\n")
            except BaseException:
                # G9: the pidfile exists but carries no owner; leaving it would
                # block every later crawl until the grace window passes. Close
                # the fd (if still open) and remove the file before re-raising.
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    self.path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            self._owned = True
            return self
        raise LockHeld(f"{self.path} could not be acquired")

    def __exit__(self, *exc) -> None:
        if self._owned:
            self.path.unlink(missing_ok=True)
            self._owned = False


def _check_report_path(report: Path, images_dir: Path,
                       csv_path: Path | None = None) -> None:
    report, images = report.resolve(), images_dir.resolve()
    if report == images or images in report.parents:
        raise ValueError("report path must be outside the images directory")
    # G1: a report is JSON; refusing other suffixes keeps a mistyped --report
    # from landing on an unrelated file (e.g. the master CSV itself).
    if report.suffix.lower() != ".json":
        raise ValueError(f"report path must end in .json: {report}")
    if csv_path is not None:
        csv = Path(csv_path).resolve()
        reserved = {csv, csv.with_name(csv.name + ".bak"),
                    csv.with_name(csv.name + ".lock")}
        if report in reserved:
            # G1: writing the report over the CSV would destroy every row this
            # run just crawled, and over the .bak would destroy the recovery
            # copy. Both are usage errors (exit 3) before any work starts.
            raise ValueError(
                f"report path {report} would overwrite the master CSV, its "
                "backup or its lock")


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
    _check_report_path(path, Path(meta["images_dir"]),
                       Path(meta["csv"]) if meta.get("csv") else None)
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


class _UsageParser(argparse.ArgumentParser):
    """G3: CLI usage errors are exit 3, never argparse's default 2.

    Exit 2 is the *aborted* code (lock/breaker/budget/interrupt), so a mistyped
    flag must not be confusable with a real abort.
    """

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def _build_parser() -> argparse.ArgumentParser:
    parser = _UsageParser(description=__doc__)
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


def main(argv: list[str] | None = None, *, opener=None, sleep=None,
         clock=None) -> int:
    """Run the crawler; opener/sleep/clock are injectable for tests (F12)."""
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
            _check_report_path(Path(args.report), images_dir, csv_path)
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
    # F9: --no-resume re-resolves every row but KEEPS the magic-checked
    # download cache; only --force actually re-downloads over a valid image.
    force_resolve = args.force or not args.resume
    # Working copy; md.records stays the pristine load-time baseline that
    # write_masterdata_atomic verifies the written temp against (F4).
    records = [list(rec) for rec in md.records]
    fetcher = Fetcher(opener=opener or urllib.request.urlopen, delay=args.delay,
                      timeout=args.timeout, retries=args.retries,
                      user_agent=args.user_agent, base_url=args.base_url,
                      sleep=sleep or time.sleep, clock=clock or time.monotonic)
    fetcher.max_image_bytes = args.max_bytes
    fetcher.max_requests = args.max_requests  # F7: enforced before each request
    hits: list[Hit] = []
    aborted = False
    work = 0
    over_limit = 0
    lock: Lock | None = None
    try:
        # G10: a precondition failure here (read-only CSV directory, unwritable
        # lock/backup) is a usage-class exit (3), never a traceback.
        try:
            if args.lock and not args.dry_run:
                lock = Lock(csv_path.with_name(csv_path.name + ".lock"))
                lock.__enter__()
            if not args.dry_run and not args.no_backup:
                bio.backup_once(csv_path)
        except OSError as exc:
            print(f"error: cannot prepare the run: {exc}", file=sys.stderr)
            return EXIT_USAGE
        for row in selected:
            try:
                gtin = bio.ean_to_gtin14(row.ean)  # F2: before any glob/request
                ean_error = ""
            except ValueError as exc:
                gtin, ean_error = "", str(exc)
            existing = bio.find_image(images_dir, row.ean)[0] if gtin else None
            state = row_state(row.photo, existing, force_resolve)
            # G4: the --limit window sits above the data gates, so a row outside
            # it is never validated-and-reported and can never decide the exit
            # code; it is only counted in the report meta. F2 still holds: the
            # EAN is validated before the glob, and a bad EAN skips the glob.
            if state != "skip" and args.limit:
                if work >= args.limit:
                    over_limit += 1
                    continue
                work += 1
            if ean_error:
                hit = _hit(row)
                hit.status = "bad_ean"
                hit.detail = ean_error
                hits.append(hit)
                _account_breaker(fetcher, hit, args.abort_after)  # resets
                if args.verbose and not args.quiet:
                    print(f"[bad_ean] {row.sku} {row.ean} {hit.detail}")
                continue
            if row.photo and not force_resolve:
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
                               max_bytes=args.max_bytes, force=args.force)
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
            # F7: the fetcher refused a request against --max-requests; the
            # current row state is flushed, so abort now (exit 2). A run that
            # merely *reaches* the budget with only skip rows left does not
            # abort — nothing was refused.
            if fetcher.budget_exhausted:
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
            "mode": md.snap.mode,
            # F11: `size` is the CSV's size in bytes; the crawl variant is
            # `variant`. `mode` stays the int stat mode.
            "size": len(md.snap.raw), "variant": args.size,
            # F11: Hit.rec_index addresses md.records[rec_index] — data rows
            # are 1-based, records[0] is the header.
            "rec_index_base": ("1-based data rows "
                               "(records[0] is the header; "
                               "md.records[rec_index] addresses the row)"),
            "images_dir": str(images_dir), "requests": fetcher.requests,
            "counts": dict(Counter(h.status for h in hits)),
            # G4: rows that needed work but fell outside the --limit window.
            "skipped_by_limit": over_limit,
            # G8: top-level aggregates — match_via counts and the review notes
            # that flag rows a human must look at.
            "match_via": dict(Counter(h.match_via for h in hits)),
            "review": [
                {"rec_index": h.rec_index, "sku": h.sku, "notes": list(h.review)}
                for h in hits if h.review
            ],
            # F11: the pinned vocabularies, so a consumer can validate the
            # report against FINAL-APPROACH-1.md.
            "status_vocabulary": sorted(STATUS_VOCABULARY),
            "match_via_vocabulary": sorted(MATCH_VIA_VOCABULARY),
            "unresolved": [
                {"rec_index": h.rec_index, "sku": h.sku, "ean": h.ean,
                 "name": h.name, "status": h.status,
                 "match_via": h.match_via, "detail": h.detail}
                for h in hits
                if h.status in FAILING_STATUSES or h.match_via == "first_result"
            ],
            "elapsed": round(elapsed, 3),
        }
        # F8: the reason a stale lock was broken, when one was.
        if lock is not None and lock.break_reason:
            meta["lock_broken"] = lock.break_reason
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
