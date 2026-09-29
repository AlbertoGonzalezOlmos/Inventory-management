"""W5.1: security-header contract — the cheap pytest tripwire.

The REAL gate for browser-facing behaviour is scripts/ui_check.py (a
headless browser executing the SPA's JavaScript); these tests merely keep
the header shape from drifting unnoticed between browser runs.

Load-bearing details pinned here:
- 'unsafe-eval' in script-src is REQUIRED: the SPA uses Vue's full build
  with an in-DOM template, whose runtime compiler emits new Function().
  Removing it blanks the UI (PLAN-v2 §0 B2) — a future "tightening" must
  fail these tests, not a browser run in CI only.
- The docs routes are exempt from the CSP header (they load jsdelivr
  assets + an inline bootstrap script); everything else carries the policy.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_csp_present_on_app_routes():
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self' 'unsafe-eval'" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp
    assert "img-src 'self' data: https:" in csp
    assert "connect-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "form-action 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_docs_routes_exempt_from_csp():
    # ("/docs" exists exactly, without a trailing slash — Starlette does not
    # redirect-slash the docs route; the middleware exemption is prefix-based
    # anyway, so /docs/oauth2-redirect etc. are covered.)
    for path in ("/docs", "/redoc", "/openapi.json"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "Content-Security-Policy" not in r.headers, path


def test_hardening_headers_on_every_response_including_404():
    for path in ("/", "/docs", "/no/such/path"):
        r = client.get(path)
        assert r.headers["X-Content-Type-Options"] == "nosniff", path
        assert r.headers["X-Frame-Options"] == "DENY", path
        assert r.headers["Referrer-Policy"] == "same-origin", path


# --- W2.4: the docs exemption is a bounded predicate, not a bare prefix -------

def test_is_docs_path_is_bounded():
    from app.main import _is_docs_path

    for path in ("/docs", "/docs/", "/docs/oauth2-redirect", "/redoc", "/openapi.json"):
        assert _is_docs_path(path) is True, path
    # A bare startswith() exempted all of these from the CSP (PLAN-v2 §8.2 F3):
    # measured, a static /docs.html was served with no CSP header at all.
    for path in ("/docs.html", "/docsx", "/redocx", "/redoc.html", "/",
                 "/index.html", "/api/items", "/api/healthz", "/static/docs.html"):
        assert _is_docs_path(path) is False, path


def test_docs_like_static_paths_still_carry_the_csp(client):
    """End-to-end form of the predicate test: a real request for /docs.html
    (does not exist -> 404 from the static mount) must still be hardened."""
    for path in ("/docs.html", "/redocx.html"):
        r = client.get(path)
        assert r.status_code == 404, path
        assert "Content-Security-Policy" in r.headers, path


# --- W2.5: an unhandled 500 must be as hardened as a 200 ----------------------

@pytest.fixture()
def boom_route():
    """A route that raises, registered AHEAD of the StaticFiles mount.

    Order matters: a route appended after `app.mount("/", StaticFiles(...))` is
    unreachable (the mount swallows every path) and returns 404, not 500 — the
    mistake that made the first version of this probe report a false negative.
    """
    @app.get("/api/__test_boom")
    def _boom():  # pragma: no cover - the point is that it raises
        raise RuntimeError("deliberate unhandled failure (header test)")

    route = app.router.routes[-1]
    app.router.routes.remove(route)
    app.router.routes.insert(0, route)
    try:
        yield "/api/__test_boom"
    finally:
        app.router.routes.remove(route)


def test_unhandled_500_carries_hardening_headers(boom_route):
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get(boom_route)
    assert r.status_code == 500, r.status_code
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "same-origin"
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self' 'unsafe-eval'" in csp
    assert r.json() == {"detail": "Internal server error"}


# --- W5.5: the CSP on API responses, which the middleware docstring claims ----

def test_csp_present_on_api_responses(client):
    """The middleware docstring claims every API response carries the policy.

    (Note `/api/items?limit=99999` is 401, not 422: the auth dependency runs
    before query validation, so the 422 case uses an unauthenticated endpoint.)
    """
    for path, expected in [
        ("/api/healthz", 200),                    # public
        ("/api/items", 401),                      # unauthenticated
        ("/api/items?limit=99999", 401),          # auth wins over validation
        ("/api/nope", 404),                       # unknown API path
    ]:
        r = client.get(path)
        assert r.status_code == expected, (path, r.status_code)
        assert "script-src 'self' 'unsafe-eval'" in r.headers["Content-Security-Policy"], path
        assert r.headers["X-Content-Type-Options"] == "nosniff", path

    # A 422 from request validation, on a route with no auth dependency.
    r = client.post("/api/auth/login", json={"email": "x" * 300, "password": "y"})
    assert r.status_code == 422, r.status_code
    assert "script-src 'self' 'unsafe-eval'" in r.headers["Content-Security-Policy"]


def test_healthz_reports_mode_and_hashing_cost(client):
    """healthz is the machine-readable safety surface: dev-admin mode and the
    hashing cost a latency budget was measured at (PLAN-v2 §8.2 N10)."""
    from app.security import DEV_ADMIN, PBKDF2_ITERATIONS

    body = client.get("/api/healthz").json()
    assert body["insecure_dev_admin"] is DEV_ADMIN is False  # conftest disables it
    assert body["pbkdf2_iterations"] == PBKDF2_ITERATIONS
