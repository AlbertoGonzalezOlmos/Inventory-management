"""Item 2 media route — ``GET /catalogue-images/{name}`` (FINAL-APPROACH-2 §A).

Serves one crawled catalogue image per request, exactly as stored by the
item-1 crawler: a file named ``<EAN13>.<jpg|jpeg|png|webp>`` inside the images
root.  The root is resolved per request as
``$HCRM_CATALOGUE_IMAGES_DIR`` -> ``$HCRM_DATA_DIR/Catalogue-images`` ->
``<repo>/data/Catalogue-images`` — the same rule as
``scripts/_biogan.py::default_images_dir`` (used verbatim when ``scripts`` is
importable; its 3-line twin below is the fallback so the app can never fail to
boot when the repo root is not on sys.path).

Contract highlights (all pinned by ``tests/test_catalogue_media.py``):

- no auth (D3); ``app.main`` includes this router BEFORE the catch-all
  ``StaticFiles`` mount, so this route wins over static serving;
- ``include_in_schema=False``: the route is not part of the OpenAPI schema;
- everything that is not a legal in-root file named
  ``<EAN13>.<jpg|jpeg|png|webp>`` — bad name or extension, traversal, NUL,
  symlink escaping the root, missing images dir, missing/zero-byte/
  content-magic-mismatched file, ``.bak``/``.lock``/``.part-*`` — gets ONE
  uniform 404 (never 403, no existence leak).  Nothing is ever created on
  disk: a missing images dir is a 404, never a ``mkdir``;
- files stream via ``FileResponse`` (never buffered with ``read_bytes``) with
  ``ETag`` and ``Cache-Control: public, max-age=3600``; a conditional GET
  whose ``If-None-Match`` matches the ETag is answered 304.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from stat import S_ISREG

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

router = APIRouter()

# Exactly the legal local-image name (FINAL-APPROACH-2 §A.3): the 13-digit
# EAN-13 stem plus one allowlisted extension.  ``re.ASCII`` pins ``\d`` to the
# EAN's ASCII digits (a plain ``\d`` also matches Unicode digits);
# ``re.IGNORECASE`` is the pinned "case-insensitive extension match" — the
# crawler writes lowercase, but e.g. an externally added ``<EAN>.JPG`` must
# still serve (it is discovered by ``find_image`` and the importer's image_url
# policy requires this route to resolve whatever that helper selects).
MEDIA_NAME_RE = re.compile(r"\d{13}\.(?:jpg|jpeg|png|webp)", re.IGNORECASE | re.ASCII)

CACHE_CONTROL = "public, max-age=3600"
NOT_FOUND = "Catalogue image not found"

_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
# Content-magic class each allowlisted extension must carry (the sniff tier of
# scripts/_biogan.py::sniff_image): .jpeg files carry JPEG magic.
_MAGIC_CLASS = {".jpg": ".jpg", ".jpeg": ".jpg", ".png": ".png", ".webp": ".webp"}
_SNIFF_HEAD = 64  # enough for every magic signature sniff_image recognises
_MIN_IMAGE_BYTES = 12  # twin of scripts/_biogan.py::MIN_IMAGE_BYTES

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _bio():
    """``scripts._biogan`` when importable (repo root on sys.path), else None."""
    try:
        import scripts._biogan as bio
    except ImportError:  # packaged deployment without the scripts/ tree
        return None
    return bio


def _default_images_dir() -> Path:
    """Twin of ``scripts/_biogan.py::default_images_dir`` (fallback only)."""
    env = os.environ.get("HCRM_CATALOGUE_IMAGES_DIR")
    if env:
        return Path(env)
    data = os.environ.get("HCRM_DATA_DIR")
    if data:
        return Path(data) / "Catalogue-images"
    return _REPO_ROOT / "data" / "Catalogue-images"


def _magic_ext(head: bytes) -> str | None:
    """Content-magic extension of a file head, twin of sniff_image's magic tier."""
    bio = _bio()
    if bio is not None:
        return bio.sniff_image(head, None)[0]
    if len(head) < _MIN_IMAGE_BYTES:
        return None
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\x89PNG"):
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[4:12] in (b"ftypavif", b"ftypavis"):
        return ".avif"
    return None


def images_root() -> Path:
    """The images root — resolved per request, never at import time."""
    bio = _bio()
    return Path(bio.default_images_dir()) if bio is not None else _default_images_dir()


def _not_found() -> HTTPException:
    """The single uniform 404 — same status and body for every failure class."""
    return HTTPException(status_code=404, detail=NOT_FOUND)


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.removeprefix("W/").removeprefix("w/") == etag:
            return True
    return False


@router.get("/catalogue-images/{name}", include_in_schema=False)
def catalogue_image(name: str, request: Request) -> Response:
    # Validation before any filesystem access (§A.3): allowlist fullmatch,
    # single-segment belt-and-braces, then resolved-parent containment.
    if not MEDIA_NAME_RE.fullmatch(name) or Path(name).name != name:
        raise _not_found()
    suffix = Path(name).suffix.lower()
    root = images_root()
    try:
        root_real = root.resolve()
        real = (root / name).resolve()
        if real.parent != root_real:
            raise _not_found()  # symlink (or ..) escaping the images root
        st = real.stat()
    except OSError:
        raise _not_found()  # missing dir or missing file — never a mkdir
    if not S_ISREG(st.st_mode) or st.st_size == 0:
        raise _not_found()
    with open(real, "rb") as fh:
        head = fh.read(_SNIFF_HEAD)
    if _magic_ext(head) != _MAGIC_CLASS[suffix]:
        raise _not_found()  # non-magic / lying content (§A.7)

    etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(
            status_code=304, headers={"etag": etag, "cache-control": CACHE_CONTROL}
        )
    return FileResponse(
        real,
        media_type=_MIME[suffix],
        stat_result=st,
        headers={"etag": etag, "cache-control": CACHE_CONTROL},
    )
