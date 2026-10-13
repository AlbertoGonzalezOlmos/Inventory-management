"""Scan shapes — the ONE classification of a scanned string.

Round 4, T1 (docs/plans/r4t1-FINAL.md): every consumer of "is this scan a
badge, a barcode, or not scan-shaped at all" answers through this module —

* ``app/routers/auth.py`` (``_badge_from_scanned``: qr-login / qr-identify
  badge acceptance),
* ``scripts/scanner_bridge.py`` (``redact_code``: badge payloads are masked
  in every human-readable print path),
* ``app/qrbadge.py`` (``parse_badge_payload``: the DB-facing strict parse —
  it keeps its own validation but its prefix/length constants come from
  here),
* and, later this round, the scanner daemon's ingest endpoint (the server
  re-derives ``kind`` itself — never trusting the client's/daemon's label,
  the ``scan_requests`` doctrine).

The JavaScript mirror of the badge shapes is ``static/scancapture.js``
(``BADGE_FULL_RE`` / ``BADGE_RAW_RE`` — routing-only gates in the SPA's
burst interceptor). Python is the source of truth; the mirror is pinned by
the regex-source-equality test in ``tests/test_scanshapes.py``, which
compares the pattern TEXT on both sides of the language boundary (the
``test_frontend_contract.py`` drift-pin pattern).

What each verdict means:

* ``"badge"`` — the full ``HCRM1:<43-char token>`` payload, or the bare
  43-char token (a scanner/bridge may strip the prefix). A bearer
  credential: never printed in full, never a catalogue item.
* ``"barcode"`` — routing-only: any other non-empty, non-whitespace string.
  This is deliberately LOOSER than the SPA's burst gate (which also
  requires barcode-ish shapes before acting): classify_scan answers "is
  this a scan at all", not "is this a well-formed GTIN" — barcode
  validation stays in ``app.barcodes.analyze_scan``.
* ``"unshaped"`` — empty or whitespace-only: not a scan, ignore.
"""

import re

#: The badge payload prefix. Versions the format and lets scanners/bridges
#: tell a badge apart from an ordinary product barcode. ONE definition for
#: the whole repo: ``app/qrbadge.py`` re-exports it (its historical home),
#: and every other site imports it from here.
BADGE_PREFIX = "HCRM1:"

#: ``secrets.token_urlsafe(32)`` → 43 chars of [A-Za-z0-9_-]. The token
#: length is a shape constant (badge detection, redaction), not just a
#: qrbadge detail — pinned by tests/test_badge_derivation.py's known-answer
#: shape assertions.
BADGE_TOKEN_LEN = 43

# Anchored on purpose: the anchors are part of the pattern SOURCE, so the
# JS parity test can compare `pattern` against the literal in
# static/scancapture.js character-for-character. Match with .fullmatch()
# (a bare .match() would let "^...$" accept a trailing newline via $).
BADGE_FULL_RE = re.compile(
    rf"^{re.escape(BADGE_PREFIX)}[A-Za-z0-9_-]{{{BADGE_TOKEN_LEN}}}$"
)
BADGE_RAW_RE = re.compile(rf"^[A-Za-z0-9_-]{{{BADGE_TOKEN_LEN}}}$")


def classify_scan(code: str) -> str:
    """Classify one scanned string: ``"badge"`` | ``"barcode"`` | ``"unshaped"``.

    Routing only — authentication (badge → qr-login) and barcode validation
    (GTIN check digits, canonicalisation) live elsewhere; this function
    never touches the database. The input is classified as given (no
    stripping): callers strip when their source can carry surrounding
    whitespace (auth does; a daemon's decoded scan buffer cannot).
    """
    if BADGE_FULL_RE.fullmatch(code) or BADGE_RAW_RE.fullmatch(code):
        return "badge"
    if not code.strip():
        return "unshaped"
    return "barcode"
