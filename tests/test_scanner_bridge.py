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


# --- P2 follow-up: a stalled server must not wedge the reader thread ---------
#
# urllib's default is no timeout; the bridge runs its HTTP calls on the
# scanner's reader thread, so a hung connection stopped the port being read
# (and scans being lost) with no error at all. Bounded now.


def test_api_passes_a_bounded_timeout(monkeypatch):
    import scripts.scanner_bridge as bridge

    captured = {}

    class _FakeResponse:
        status = 200

        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)
    assert bridge.api("/api/healthz") == (200, None)
    assert captured["timeout"] == bridge.HTTP_TIMEOUT
    assert bridge.HTTP_TIMEOUT and bridge.HTTP_TIMEOUT > 0


def test_api_timeout_is_reported_as_an_apierror(monkeypatch):
    import scripts.scanner_bridge as bridge

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ApiError) as exc:
        bridge.api("/api/healthz")
    assert exc.value.status == 0
    assert "cannot reach" in str(exc.value)


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
