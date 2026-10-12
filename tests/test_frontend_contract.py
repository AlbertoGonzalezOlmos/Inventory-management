"""Static contract pins for the SPA's scan-capture wiring (round 3, Task B).

The capture BEHAVIOUR lives in static/scancapture.js (a pure reducer,
node-tested by tests/js/scancapture.test.mjs) plus the app.js adapter. What
pytest can meaningfully pin is the wiring a DOM dump cannot see — and the
one arithmetic the whole design stands on:

- SCAN_DEFER_MS > SCAN_MAX_GAP_MS: if the defer window ever drops below the
  burst gate, a scan's second key arrives after the human-verdict flush and
  the payload paints into the focused field (the exact defect round 3 item 1
  exists to fix). Pinned here AND in the Node suite.
- script order: scancapture.js must load before app.js — the adapter
  references the HcrmScanCapture global at install time, so a reversed
  order is a silently dead scan bus (ui_check would only catch it as a
  console error, and only on a page that logs it).
- the two [data-scan-sink] fields: the collapsed badge-code fallback and
  the visible scan field must keep their dedicated Enter paths (the global
  capture must never regress them).
- no isTrusted check: the M-10's USB host is dead
  (docs/m10-unreachable-2026-10-10.md), so console-dispatched synthetic
  KeyboardEvents are the only capture-verification path — a handler that
  skipped untrusted events would disable the simulator.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC = REPO_ROOT / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_scan_defer_exceeds_max_gap():
    """The load-bearing inequality, read from the shipped source."""
    src = _read("scancapture.js")
    defer = re.search(r"^const SCAN_DEFER_MS = (\d+)", src, re.MULTILINE)
    gap = re.search(r"^const SCAN_MAX_GAP_MS = (\d+)", src, re.MULTILINE)
    assert defer and gap, "SCAN_DEFER_MS / SCAN_MAX_GAP_MS not found in scancapture.js"
    assert int(defer.group(1)) > int(gap.group(1)), (
        f"SCAN_DEFER_MS ({defer.group(1)}) must exceed SCAN_MAX_GAP_MS "
        f"({gap.group(1)}): a defer window shorter than the burst gate lets a "
        "scan's second key arrive after the human-verdict flush and the "
        "payload paints into the focused field")


def test_scancapture_loads_before_app_js():
    """A reversed script order = `HcrmScanCapture is not defined` = dead bus."""
    html = _read("index.html")
    capture = html.find('src="/scancapture.js"')
    app = html.find('src="/app.js"')
    assert capture != -1, "index.html no longer loads /scancapture.js"
    assert app != -1, "index.html no longer loads /app.js"
    assert capture < app, (
        "scancapture.js must load before app.js — the adapter references the "
        "HcrmScanCapture global at install time")


def test_both_scan_sinks_still_exist():
    """The badge fallback field and the visible scan field own their keys.

    The global capture returns early for [data-scan-sink] targets; losing
    either attribute would route the M-10's keystrokes into BOTH the sink's
    handler and the capture bus (double-fire) — or break the login fallback
    entirely.
    """
    html = _read("index.html")
    assert html.count("data-scan-sink") >= 2, (
        "expected both data-scan-sink fields (the collapsed badge-code "
        "fallback on the login view and the visible scan field)")


def test_capture_never_checks_is_trusted():
    """Synthetic (isTrusted: false) keydowns must stay on the capture path.

    The M-10 hardware is dead, so the DevTools dispatchEvent simulator is
    the only browser-side verification path; a trust filter would silently
    disable it.
    """
    combined = _read("scancapture.js") + _read("app.js")
    assert "isTrusted" not in combined, (
        "the scan capture must not consult isTrusted — the simulator-based "
        "verification path dispatches untrusted events by construction")
