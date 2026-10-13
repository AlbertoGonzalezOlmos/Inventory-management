"""Tests for app/scanshapes.py — the ONE scan classification (round 4, T1).

Two layers, per docs/plans/r4t1-FINAL.md:

1. A classification table (badge full / badge raw / barcode / unshaped /
   empty / 43-char-but-wrong-charset → barcode).
2. A regex-source-equality parity suite, STRONGER than behaviour vectors:
   the delegated sites (auth's badge acceptance, the bridge's redaction,
   qrbadge's prefix/length constants) must reference the same compiled
   objects / the same ``classify_scan`` as ``app/scanshapes.py`` — and the
   JavaScript mirror in ``static/scancapture.js`` must carry the same
   pattern *text*. If any site re-grows a private copy, these fail on the
   identity/source check even when every behaviour vector still passes
   (the drift-pin doctrine of tests/test_frontend_contract.py, applied
   across the language boundary).

The existing behaviour suites (test_qr.py, test_auth.py,
test_scanner_bridge.py, test_cart_scan.py) are the no-behaviour-change
gate for the delegation itself and must pass unmodified.
"""

import re
import secrets
from pathlib import Path

import app.scanshapes as scanshapes
from app.qrbadge import new_badge_payload, parse_badge_payload
from app.routers.auth import _badge_from_scanned
from app.scanshapes import (
    BADGE_FULL_RE,
    BADGE_PREFIX,
    BADGE_RAW_RE,
    BADGE_TOKEN_LEN,
    classify_scan,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
JS_MIRROR = REPO_ROOT / "static" / "scancapture.js"


# --- classification table ---------------------------------------------------

def test_classify_table():
    token = "A" * BADGE_TOKEN_LEN
    assert classify_scan(BADGE_PREFIX + token) == "badge"       # badge, full
    assert classify_scan(token) == "badge"                      # badge, raw
    assert classify_scan("8412345678905") == "barcode"          # barcode (GTIN)
    assert classify_scan("EX-007") == "barcode"                 # barcode (opaque SKU)
    assert classify_scan("   ") == "unshaped"                   # whitespace
    assert classify_scan("") == "unshaped"                      # empty
    assert classify_scan("A" * 42 + "!") == "barcode"           # 43 chars, wrong charset


def test_classify_real_badge_payloads():
    payload = new_badge_payload()
    assert classify_scan(payload) == "badge"
    assert classify_scan(payload[len(BADGE_PREFIX):]) == "badge"
    # The payload's shape constants are the module's constants.
    assert len(payload) == len(BADGE_PREFIX) + BADGE_TOKEN_LEN


def test_classify_is_routing_only_deliberately_looser_than_the_js_gate():
    # classify_scan answers "is this a scan at all" — barcode validation
    # stays in app.barcodes.analyze_scan. So a digit-free word (typing,
    # which the SPA's scanShape gate ignores with `null`) is still a
    # "barcode" here, and near-miss badge shapes are not badges:
    assert classify_scan("water") == "barcode"
    assert classify_scan(BADGE_PREFIX + "A" * 42) == "barcode"  # truncated full form
    assert classify_scan(BADGE_PREFIX + "!" * 43) == "barcode"  # bad charset
    assert classify_scan("A" * 44) == "barcode"                 # not 43


# --- parity: the delegated sites share the classifier (no private copies) ---

def test_auth_delegates_to_classify_scan():
    from app.routers import auth

    assert auth.classify_scan is classify_scan
    assert auth.BADGE_PREFIX is BADGE_PREFIX
    # The private regex must not come back (drift's natural habitat).
    assert not hasattr(auth, "_RAW_TOKEN_RE")


def test_bridge_delegates_to_the_shared_shapes():
    import scripts.scanner_bridge as bridge

    assert bridge.BADGE_PREFIX is BADGE_PREFIX
    assert bridge.BADGE_RAW_RE is BADGE_RAW_RE
    assert bridge.BADGE_TOKEN_LEN is BADGE_TOKEN_LEN


def test_qrbadge_routes_through_the_shared_constants():
    import app.qrbadge as qrbadge

    assert qrbadge.BADGE_PREFIX is BADGE_PREFIX


def test_qrbadge_parse_agrees_with_the_shape_gate_on_ascii_samples():
    # parse_badge_payload keeps its own (DB-facing, unicode-lenient) charset
    # check but must agree with the shape gate on every ASCII sample:
    token = "A" * BADGE_TOKEN_LEN
    samples = {
        BADGE_PREFIX + token: True,
        BADGE_PREFIX + "short": False,
        BADGE_PREFIX + "A" * 44: False,
        BADGE_PREFIX + "!" * 43: False,
        token: False,           # raw token: not a FULL payload (auth adds the prefix)
        "5901234123457": False,
    }
    for stripped, is_full_badge in samples.items():
        assert (parse_badge_payload(stripped) is not None) == is_full_badge, stripped
        assert bool(BADGE_FULL_RE.fullmatch(stripped)) == is_full_badge, stripped


# --- parity: the JS mirror carries the same pattern text --------------------

def _js_regex_body(name: str) -> str:
    """The pattern text of `const NAME = /^…$/;` in static/scancapture.js."""
    source = JS_MIRROR.read_text()
    match = re.search(rf"const {name} = /\^(.+)\$/;", source)
    assert match, f"{name} literal not found in {JS_MIRROR}"
    return match.group(1)


def test_js_badge_regexes_match_the_python_pattern_sources():
    # Python carries the anchors (^…$) in the pattern source so the two
    # languages can be compared character-for-character.
    for name, pattern in (("BADGE_FULL_RE", BADGE_FULL_RE),
                          ("BADGE_RAW_RE", BADGE_RAW_RE)):
        assert pattern.pattern.startswith("^") and pattern.pattern.endswith("$")
        assert _js_regex_body(name) == pattern.pattern[1:-1], (
            f"{name} drifted between app/scanshapes.py and "
            f"static/scancapture.js"
        )


def test_no_private_badge_shape_copies_in_python_sources():
    # The literal charset fragment of the badge token must not appear in any
    # Python source outside app/scanshapes.py (which builds the pattern from
    # BADGE_TOKEN_LEN, so it does not even hold the literal). A re-grown
    # private copy — the thing the parity suite exists to prevent — fails here.
    fragment = "[A-Za-z0-9_-]{" + str(BADGE_TOKEN_LEN) + "}"
    for path in sorted(REPO_ROOT.glob("app/**/*.py")) + sorted(
            REPO_ROOT.glob("scripts/**/*.py")):
        assert fragment not in path.read_text(), (
            f"{path} re-defines the badge token shape; use app.scanshapes"
        )


# --- behaviour pins for the delegated auth rule (unchanged semantics) -------

def test_badge_from_scanned_forms():
    token = "A" * BADGE_TOKEN_LEN
    assert _badge_from_scanned(BADGE_PREFIX + token) == BADGE_PREFIX + token
    assert _badge_from_scanned(token) == BADGE_PREFIX + token  # prefix restored
    # Both forms are whitespace-tolerant (pre-T1 semantics, unchanged:
    # parse_badge_payload stripped too); the returned payload is the
    # stripped form the hash lookup expects.
    assert _badge_from_scanned(f"  {token}  ") == BADGE_PREFIX + token
    assert _badge_from_scanned(f"  {BADGE_PREFIX}{token}  ") == BADGE_PREFIX + token
    assert _badge_from_scanned(BADGE_PREFIX + "A" * 42) is None  # truncated
    assert _badge_from_scanned("5901234123457") is None          # product barcode
    assert _badge_from_scanned("") is None


def test_badge_from_scanned_accepts_any_token_shaped_string():
    # Session tokens are also 43 urlsafe chars: shape acceptance is NOT
    # authentication — the qr-login hash lookup rejects them (pinned in
    # tests/test_qr.py); this pin documents that the acceptance rule stays
    # purely shape-based (a 43-char string gets its prefix back either way).
    session_like = secrets.token_urlsafe(32)
    assert len(session_like) == BADGE_TOKEN_LEN
    assert _badge_from_scanned(session_like) == BADGE_PREFIX + session_like
