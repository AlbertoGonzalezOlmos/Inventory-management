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
