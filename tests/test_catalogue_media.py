"""W6.1 (Item 2, slice A): the catalogue media route contract (FINAL-APPROACH-2 §A).

Load-bearing details pinned here:

- happy path serves a tmp images dir chosen by ``$HCRM_CATALOGUE_IMAGES_DIR``
  with the explicit MIME map and ``Cache-Control: public, max-age=3600``,
  unauthenticated (D3: no auth on the media route);
- conditional GET: ``If-None-Match`` matching the served ETag -> 304;
- one uniform 404 body for every failure class (bad name/extension, traversal,
  NUL, ``.bak``/``.lock``/``.part-*``, zero-byte/non-magic content, missing
  file/dir, symlink escaping the root) — never 403, no existence leak;
- a missing images dir is a 404 and is never created;
- images-root resolution order: ``$HCRM_CATALOGUE_IMAGES_DIR`` ->
  ``$HCRM_DATA_DIR/Catalogue-images`` -> ``<repo>/data/Catalogue-images``,
  equal to ``scripts/_biogan.py::default_images_dir`` under the same env;
- the route is registered before the catch-all ``StaticFiles`` mount and wins
  over it, while ``/app.js`` and ``/`` keep being served statically.

All tests run offline against tmp dirs; nothing depends on real ``data/``.
"""

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount

import scripts._biogan as bio
from app.main import app
from app.routers import media

client = TestClient(app)

EAN = "5742000000369"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 60
MARKER = b"SECRET-MARKER-NOT-SERVED"


def _put(root, name: str, body: bytes = JPEG):
    p = root / name
    p.write_bytes(body)
    return p


def _use_root(monkeypatch, path):
    monkeypatch.setenv("HCRM_CATALOGUE_IMAGES_DIR", str(path))


def _assert_uniform_404(r):
    """The one 404 of the route: same status, same body, no existence leak."""
    assert r.status_code == 404
    assert r.json() == {"detail": media.NOT_FOUND}


# --- happy path / headers ---------------------------------------------------

def test_happy_path_jpeg_served_with_mime_cache_etag_no_auth(monkeypatch, tmp_path):
    _use_root(monkeypatch, tmp_path)
    _put(tmp_path, f"{EAN}.jpg")
    r = client.get(f"/catalogue-images/{EAN}.jpg")  # no auth headers at all
    assert r.status_code == 200
    assert r.content == JPEG
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "public, max-age=3600"
    assert r.headers["etag"].startswith('"')  # FileResponse ETag, streamed body
    assert "www-authenticate" not in r.headers  # D3: no auth on this route


def test_mime_map_png_webp_jpeg(monkeypatch, tmp_path):
    _use_root(monkeypatch, tmp_path)
    for name, body, ctype in (
        (f"{EAN}.png", PNG, "image/png"),
        (f"{EAN}.webp", WEBP, "image/webp"),
        (f"{EAN}.jpeg", JPEG, "image/jpeg"),
    ):
        _put(tmp_path, name, body)
        r = client.get(f"/catalogue-images/{name}")
        assert r.status_code == 200, name
        assert r.headers["content-type"] == ctype, name


def test_extension_match_case_insensitive_serves_case_exact(monkeypatch, tmp_path):
    # §A.3: case-insensitive extension match; the case-exact name is served.
    _use_root(monkeypatch, tmp_path)
    _put(tmp_path, f"{EAN}.JPG")
    r = client.get(f"/catalogue-images/{EAN}.JPG")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


def test_conditional_get_etag_roundtrip_304(monkeypatch, tmp_path):
    _use_root(monkeypatch, tmp_path)
    _put(tmp_path, f"{EAN}.jpg")
    etag = client.get(f"/catalogue-images/{EAN}.jpg").headers["etag"]
    for inm in (etag, f"W/{etag}", "*"):
        r = client.get(f"/catalogue-images/{EAN}.jpg", headers={"If-None-Match": inm})
        assert r.status_code == 304, inm
        assert r.headers["etag"] == etag
        assert r.content == b""
    r = client.get(f"/catalogue-images/{EAN}.jpg", headers={"If-None-Match": '"nope"'})
    assert r.status_code == 200


# --- the allowlist and the uniform 404 --------------------------------------

def test_bad_names_uniform_404(monkeypatch, tmp_path):
    _use_root(monkeypatch, tmp_path)
    _put(tmp_path, f"{EAN}.gif")  # exists but .gif is not on the allowlist
    bad = [
        "abc.jpg",                       # wrong stem
        f"{EAN[:-1]}.jpg",               # 12 digits
        f"{EAN}0.jpg",                   # 14 digits
        f"{EAN}.gif",                    # gif/avif are IMAGE_EXTS but not route-legal
        f"{EAN}.avif",
        f"{EAN}.EXE",
        f"{EAN}",                        # no extension
        f"{EAN}.",                       # empty extension
        f"{EAN}.jpegx",                  # suffix of an allowed ext is not the ext
        f"{EAN}.jpg.bak",                # backups are never images
        f"{EAN}.bak",
        f"{EAN}.lock",
        f"{EAN}.jpg.part-42",
        "Biogan - masterdata - Sheet.csv.bak",
        "%2e%2e",  # bare ".." is collapsed to "/" by HTTP clients — not reachable
    ]
    for name in bad:
        _assert_uniform_404(client.get(f"/catalogue-images/{name}"))


def test_traversal_absolute_and_nul_404_without_leak(monkeypatch, tmp_path):
    root = tmp_path / "img"
    root.mkdir()
    _use_root(monkeypatch, root)
    _put(tmp_path, "secret.jpg", JPEG + MARKER)  # outside the served root
    names = [
        # Single-segment attempts land on the route -> uniform JSON 404.
        "%2e%2e",
        "..%5Csecret.jpg",
        f"{EAN}%00.jpg",
    ]
    for name in names:
        _assert_uniform_404(client.get(f"/catalogue-images/{name}"))
    # Encoded-slash / raw-dot attempts may be decoded into extra segments or
    # normalised before routing (bare ".." is even collapsed to "/" by the
    # HTTP client and is therefore untestable here); whichever layer answers,
    # it is a 404 and never leaks bytes from outside the root.
    for name in (
        "../secret.jpg",
        "..%2Fsecret.jpg",
        "%2e%2e%2fsecret.jpg",
        "%2Fetc%2Fpasswd",
    ):
        r = client.get(f"/catalogue-images/{name}")
        assert r.status_code == 404, name
        assert MARKER not in r.content, name
        assert b"root:" not in r.content, name


def test_missing_dir_is_404_and_never_created(monkeypatch, tmp_path):
    absent = tmp_path / "absent-images"
    _use_root(monkeypatch, absent)
    _assert_uniform_404(client.get(f"/catalogue-images/{EAN}.jpg"))
    assert not absent.exists()  # never a mkdir


def test_missing_file_404(monkeypatch, tmp_path):
    _use_root(monkeypatch, tmp_path)
    _assert_uniform_404(client.get(f"/catalogue-images/{EAN}.jpg"))


def test_zero_byte_non_magic_and_mismatched_magic_404(monkeypatch, tmp_path):
    _use_root(monkeypatch, tmp_path)
    _put(tmp_path, f"{EAN}.jpg", b"")                          # zero-byte
    _put(tmp_path, "1111111111111.jpg", b"<html>404 body</html>")  # non-magic
    _put(tmp_path, "2222222222222.jpg", PNG)                   # PNG magic in .jpg
    for name in (f"{EAN}.jpg", "1111111111111.jpg", "2222222222222.jpg"):
        _assert_uniform_404(client.get(f"/catalogue-images/{name}"))


def test_symlink_escaping_root_404(monkeypatch, tmp_path):
    root = tmp_path / "img"
    root.mkdir()
    _use_root(monkeypatch, root)
    outside = _put(tmp_path, "outside.jpg", JPEG + MARKER)
    (root / f"{EAN}.jpg").symlink_to(outside)
    r = client.get(f"/catalogue-images/{EAN}.jpg")
    assert r.status_code == 404
    assert MARKER not in r.content


# --- wiring: route before the catch-all static mount ------------------------

def test_route_before_static_mount_wins_but_static_still_serves(monkeypatch, tmp_path):
    _use_root(monkeypatch, tmp_path)
    # This FastAPI version wraps included routers in _IncludedRouter objects;
    # flatten first so the registration order against the Mount is observable.
    def _flatten(routes):
        flat = []
        for r in routes:
            inner = getattr(r, "original_router", None)  # FastAPI's _IncludedRouter
            if inner is not None:
                flat.extend(_flatten(inner.routes))
            else:
                flat.append(r)
        return flat

    flat = _flatten(app.routes)
    idx_media = next(
        i for i, r in enumerate(flat)
        if isinstance(r, APIRoute) and r.path == "/catalogue-images/{name}"
    )
    idx_mount = next(i for i, r in enumerate(flat) if isinstance(r, Mount))
    assert idx_media < idx_mount
    assert flat[idx_media].include_in_schema is False

    assert client.get("/").status_code == 200
    assert client.get("/app.js").status_code == 200
    # A legal-but-absent name is answered by the route's JSON 404 (this exact
    # body), not by StaticFiles — proving the route wins over the catch-all.
    _assert_uniform_404(client.get(f"/catalogue-images/{EAN}.jpg"))


# --- images-root resolution --------------------------------------------------

def test_images_root_env_precedence(monkeypatch, tmp_path):
    a, d = tmp_path / "A", tmp_path / "D"
    monkeypatch.setenv("HCRM_CATALOGUE_IMAGES_DIR", str(a))
    monkeypatch.setenv("HCRM_DATA_DIR", str(d))
    assert media.images_root() == a
    monkeypatch.delenv("HCRM_CATALOGUE_IMAGES_DIR")
    assert media.images_root() == d / "Catalogue-images"
    monkeypatch.delenv("HCRM_DATA_DIR")
    assert media.images_root() == media._REPO_ROOT / "data" / "Catalogue-images"


def test_images_root_equals_canonical_default_images_dir(monkeypatch, tmp_path):
    # The route resolves the root via scripts/_biogan.py::default_images_dir;
    # pin that its rule and the fallback twin never drift apart.
    a, d = tmp_path / "A", tmp_path / "D"
    for env in (
        {"HCRM_CATALOGUE_IMAGES_DIR": str(a), "HCRM_DATA_DIR": str(d)},
        {"HCRM_DATA_DIR": str(d)},
        {},
    ):
        for key in ("HCRM_CATALOGUE_IMAGES_DIR", "HCRM_DATA_DIR"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        assert media.images_root() == bio.default_images_dir(), env


def test_route_works_without_scripts_importable(monkeypatch, tmp_path):
    # Packaged deployments may lack scripts/: the fallback twin must serve.
    _use_root(monkeypatch, tmp_path)
    _put(tmp_path, f"{EAN}.jpg")
    _put(tmp_path, "3333333333333.jpg", b"")
    monkeypatch.setattr(media, "_bio", lambda: None)
    r = client.get(f"/catalogue-images/{EAN}.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    _assert_uniform_404(client.get("/catalogue-images/3333333333333.jpg"))
