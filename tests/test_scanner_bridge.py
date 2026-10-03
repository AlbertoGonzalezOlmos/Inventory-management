"""Tests for scripts/scanner_bridge.py — the M-10 → catalogue bridge.

Offline: the HTTP layer is injected (`find_item` takes a `get(path)` callable),
so the whole identity/decision table is pinned without a server. These exist
because the two defects they cover were *demonstrated live* in the review
(REVIEW-m10.md P1, P2) rather than reasoned about:

* P1 — `find_item()` used to fall back to `items[0]` of a keyword search, and
  `--stock-out` applied to whatever came back. Scanning the string `water`
  decremented *Insulated Water Bottle*. Every "must not match" case below is
  that incident in miniature.
* P5 — identity now comes from `app.barcodes.analyze_scan` + `?barcode=`, so a
  UPC-E scan finds the item filed under its EAN-13 GTIN. The old "put the
  barcode in the SKU field" convention could never do that.
"""

import pytest

import urllib.error

from scripts.scanner_bridge import ApiError, api, find_item, resolve_port

# A catalogue as the API returns it: one item filed under a canonical GTIN-14,
# one under an opaque store SKU.
BOTTLE = {"id": 1, "sku": "BTL-001", "name": "Insulated Water Bottle",
          "stock": 60, "price_cents": 2500, "barcode": "08412345678905"}
# The Wikipedia UPC-A/UPC-E pair also pinned in tests/test_barcodes.py:
# UPC-A 042100005264 == UPC-E 425261, canonical GTIN-14 00042100005264.
TIN = {"id": 4, "sku": "TIN-001", "name": "Canned product",
       "stock": 12, "price_cents": 350, "barcode": "00042100005264"}
LABEL = {"id": 2, "sku": "EX-007", "name": "Store-label product",
         "stock": 3, "price_cents": 100, "barcode": None}


def fake_api(table):
    """A `get(path)` that mimics ?barcode= (exact GTIN) and ?q= (substring)."""
    calls = []

    def get(path):
        calls.append(path)
        if "barcode=" in path:
            gtin = path.split("barcode=")[1].split("&")[0]
            return 200, [i for i in table if i.get("barcode") == gtin]
        needle = path.split("q=")[1].split("&")[0].lower()
        return 200, [i for i in table
                     if needle in i["sku"].lower() or needle in i["name"].lower()]

    get.calls = calls
    return get


# --- P5: identity, not strings ------------------------------------------------


def test_gtin_matches_on_the_canonical_form():
    get = fake_api([BOTTLE, LABEL])
    item, how, analysis = find_item(get, "8412345678905")
    assert how == "barcode" and item["id"] == BOTTLE["id"]
    assert analysis.kind == "gtin" and analysis.key == "08412345678905"
    assert get.calls == ["/api/items?barcode=08412345678905"]


def test_upce_scan_finds_the_item_filed_under_its_upca_gtin():
    """The case the barcode-in-SKU convention could never handle — provided the
    scanner reports the symbology, which is the only honest way to know a bare
    6-digit payload is a zero-suppressed UPC-E."""
    get = fake_api([BOTTLE, LABEL, TIN])
    item, how, analysis = find_item(get, "425261", symbology="UPC-E")
    assert how == "barcode" and item["id"] == TIN["id"]
    assert analysis.key == "00042100005264"


def test_bare_six_digits_are_never_guessed_into_a_gtin():
    """app.barcodes expands UPC-E only on the scanner's word, because the
    expansion computes its own check digit — so any 6 digits would 'validate'.
    The bridge must not undermine that by trying both number systems."""
    get = fake_api([BOTTLE, LABEL, TIN])
    item, how, analysis = find_item(get, "425261")
    assert item is None and how is None
    assert analysis.kind == "opaque"
    assert get.calls == ["/api/items?q=425261&limit=100"]   # SKU equality only


def test_opaque_label_matches_on_exact_sku_only():
    get = fake_api([BOTTLE, LABEL])
    item, how, analysis = find_item(get, "ex-007")
    assert how == "sku" and item["id"] == LABEL["id"]
    assert analysis.kind == "opaque"


# --- P1: nothing is guessed at ------------------------------------------------


def test_partial_word_does_not_match_anything():
    """The demonstrated incident: `water` used to resolve to the bottle."""
    get = fake_api([BOTTLE, LABEL])
    item, how, _ = find_item(get, "water")
    assert item is None and how is None


def test_partial_word_matches_only_under_explicit_fuzzy():
    get = fake_api([BOTTLE, LABEL])
    item, how, _ = find_item(get, "water", fuzzy=True)
    assert how == "search" and item["id"] == BOTTLE["id"]


def test_unknown_gtin_is_a_new_entry_candidate_not_a_match():
    get = fake_api([BOTTLE, LABEL])
    item, how, analysis = find_item(get, "4006381333931")  # valid, absent
    assert item is None and how is None
    assert analysis.kind == "gtin" and analysis.key == "04006381333931"


def test_misread_gtin_is_never_looked_up():
    """A bad check digit must not become a search string (phantom identity)."""
    get = fake_api([BOTTLE, LABEL])
    item, how, analysis = find_item(get, "8412345678906")  # check digit +1
    assert item is None and how is None
    assert analysis.kind == "invalid-gtin"
    assert get.calls == []          # not even one HTTP call


def test_restricted_circulation_code_still_matches_exactly():
    restricted = dict(BOTTLE, id=3, sku="IN-001", barcode="02012345678903")
    get = fake_api([restricted])
    item, how, analysis = find_item(get, "2012345678903")
    assert how == "barcode" and item["id"] == 3
    assert analysis.kind == "restricted"
    assert analysis.info.globally_unique is False


# --- P4: the port is verified or the bridge refuses to start ------------------


def test_resolve_port_prefers_an_explicit_port(monkeypatch):
    assert resolve_port("/dev/ttyACM3", None) == "/dev/ttyACM3"


def test_resolve_port_uses_a_verified_m10(monkeypatch):
    monkeypatch.setattr("scripts.scanner_bridge.find_scanner_ports",
                        lambda: ["/dev/ttyACM0"])
    assert resolve_port(None, None) == "/dev/ttyACM0"


def test_resolve_port_refuses_to_guess(monkeypatch):
    """An attached OPN-2001 must not be auto-opened by the M-10 bridge."""
    monkeypatch.setattr("scripts.scanner_bridge.find_scanner_ports", lambda: [])
    monkeypatch.setattr("scripts.scanner_bridge.describe_attached_opticon",
                        lambda: ["  065A:0009  OPN-2001 pocket memory scanner "
                                 "[/dev/ttyUSB0]"])
    with pytest.raises(SystemExit) as exc:
        resolve_port(None, None)
    msg = str(exc.value)
    assert "065A:0009" in msg and "/dev/ttyUSB0" in msg   # names what it found
    assert "--any-port" in msg and "opticon_detect" in msg  # and how to proceed


def test_resolve_port_any_port_is_an_explicit_opt_in(monkeypatch):
    monkeypatch.setattr("scripts.scanner_bridge.find_scanner_ports", lambda: [])
    assert resolve_port(None, "/dev/ttyUSB0") == "/dev/ttyUSB0"

# --- P2: an unreachable server is a report, not a traceback -------------------


def test_unreachable_server_raises_apierror_not_a_raw_traceback(monkeypatch):
    """Pointed at a dead port, api() must raise ApiError (which the login path
    turns into one clean message, and the scan path into a per-scan report that
    keeps the listener alive) — never URLError, which used to escape as a
    traceback, and never SystemExit, which kills the reader thread."""
    monkeypatch.setattr("scripts.scanner_bridge.BASE", "http://127.0.0.1:1")
    with pytest.raises(ApiError) as exc:
        api("/api/healthz")
    assert exc.value.status == 0
    assert "cannot reach" in str(exc.value)
    assert not isinstance(exc.value, SystemExit)
    assert not isinstance(exc.value, urllib.error.URLError)


# --- P2: the 401-retry (--relogin) --------------------------------------------
#
# The retry lived as a closure inside main() and was the one Phase-5 test the
# M-10 review promised that never landed — it is the path every always-on
# bridge exercises weekly, when the token outlives its 7-day TTL. It is now a
# module-level function (call_with_relogin) with both collaborators injected.

from scripts.scanner_bridge import badge_login, call_with_relogin  # noqa: E402
import scripts.scanner_bridge as bridge  # noqa: E402


def _failing_api(status, detail="expired"):
    def call(path, method="GET", token=None, body=None):
        raise ApiError(status, f"{method} {path}", detail)
    return call


def test_relogin_retries_once_with_the_fresh_token():
    """401 + --relogin → one re-authentication, one retry, with the NEW token."""
    calls = []

    def api_call(path, method="GET", token=None, body=None):
        calls.append(token)
        if token == "STALE":
            raise ApiError(401, f"{method} {path}", "expired")
        return 200, {"ok": True}

    result, token = call_with_relogin(
        api_call, lambda: "FRESH", "/api/items", "STALE", relogin=True)
    assert result == (200, {"ok": True})
    assert token == "FRESH"
    assert calls == ["STALE", "FRESH"]


def test_without_relogin_a_401_is_just_an_error():
    """Same 401 without --relogin: ApiError propagates, login is never called."""
    relogins = []
    with pytest.raises(ApiError) as exc:
        call_with_relogin(
            _failing_api(401), lambda: relogins.append(1) or "FRESH",
            "/api/items", "STALE", relogin=False)
    assert exc.value.status == 401
    assert relogins == []


def test_a_second_401_does_not_loop():
    """If the fresh credentials are also rejected, the second 401 propagates —
    exactly one retry, never an infinite re-login loop."""
    calls = []

    def api_call(path, method="GET", token=None, body=None):
        calls.append(token)
        raise ApiError(401, f"{method} {path}", "expired")

    with pytest.raises(ApiError):
        call_with_relogin(api_call, lambda: "FRESH", "/api/items", "STALE",
                          relogin=True)
    assert calls == ["STALE", "FRESH"]


def test_non_401_errors_never_trigger_relogin():
    """A 500/409 is a per-scan report, not a re-authentication trigger."""
    relogins = []
    with pytest.raises(ApiError) as exc:
        call_with_relogin(
            _failing_api(500, "database is locked"),
            lambda: relogins.append(1) or "FRESH",
            "/api/items", "STALE", relogin=True)
    assert exc.value.status == 500
    assert relogins == []


# --- P2/§6a: badge scans are records, never fatal ------------------------------


def test_a_badge_scan_is_exchanged_for_a_session(monkeypatch):
    """badge_login posts the raw payload to /api/auth/qr-login and returns the
    session token and user in a record — headless/kiosk login (guide §6a)."""
    seen = {}

    def fake_api(path, method="GET", token=None, body=None):
        seen.update(path=path, method=method, body=body)
        return 200, {"token": "S1", "user": {
            "name": "Alice", "email": "alice@shop.local", "role": "member"}}

    monkeypatch.setattr(bridge, "api", fake_api)
    record = bridge.badge_login("HCRM1:abc123")
    assert seen == {"path": "/api/auth/qr-login", "method": "POST",
                    "body": {"token": "HCRM1:abc123"}}
    assert record == {"code": "HCRM1:abc123", "badge_login": True,
                      "user": {"name": "Alice", "email": "alice@shop.local",
                               "role": "member"},
                      "token": "S1"}


def test_a_rejected_badge_is_a_record_not_an_exception(monkeypatch):
    """An unknown/revoked badge is a failure record, so the bridge keeps
    listening instead of dying on the scanner's reader thread."""
    monkeypatch.setattr(bridge, "api", _failing_api(401, "unknown badge"))
    record = bridge.badge_login("HCRM1:stale")
    assert record["badge_login"] is False
    assert record["code"] == "HCRM1:stale"
    assert "401" in record["error"]


def test_an_unreachable_server_during_badge_login_is_also_a_record(monkeypatch):
    """Server down mid-shift: same contract — a record with the error."""
    monkeypatch.setattr(bridge, "api", _failing_api(0, "cannot reach"))
    record = bridge.badge_login("HCRM1:abc123")
    assert record["badge_login"] is False
    assert record["error"]
