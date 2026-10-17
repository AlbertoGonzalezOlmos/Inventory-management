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
- the badge-code field is GONE (owner decision, login-screen cleanup): the
  global capture ("scan anywhere") is the one browser badge path, so no
  data-scan-sink may reappear — a sink takes its keys OUT of the capture
  bus and forks the badge flow.
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


def _method_body(header: str) -> str:
    """Extract a method body from static/app.js by brace matching.

    ``header`` must be the DEFINITION line's text ending in ``{`` (e.g.
    ``"handleSessionExpired() {"``) so a call site can never match. The
    honest alternative to unbounded ``\\{[\\s\\S]*?foo\\(`` regexes, which
    lazily match a *later definition* and false-pass when the pinned call
    is deleted (found by the round-2 review probe).
    """
    js = _read("app.js")
    assert header.endswith("{"), "header must include the opening brace"
    start = js.index(header)
    open_i = start + len(header) - 1
    depth = 0
    for i in range(open_i, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[start:i + 1]
    raise AssertionError(f"unbalanced braces after {header!r}")


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


def _html_section(view: str) -> str:
    """Extract one <section> block from index.html by its currentView guard
    (from the guard to the next <section tag) — for pins that a view's own
    markup must (not) carry."""
    html = _read("index.html")
    start = html.index(f"currentView === '{view}'")
    nxt = html.find("<section", start)
    assert nxt != -1, f"no closing <section> after view {view!r}"
    return html[start:nxt]


def test_badge_field_is_gone_scan_anywhere_is_the_path():
    """The collapsed badge-code field and its toggle were removed (owner
    decision, login-screen cleanup): the global capture is the ONE browser
    badge path. The capture still honours [data-scan-sink] defensively
    (app.js), but no such element may exist — a reintroduced sink would
    take its keys out of the capture bus and fork the badge flow."""
    html = _read("index.html")
    # "data-scan-sink is" appears in prose; the attribute form must not.
    assert not re.findall(r"data-scan-sink(?!\s+is)", html), (
        "a data-scan-sink element came back — the global capture returns "
        "early for sinks, so it would fork the badge flow")
    assert "Use a badge code instead" not in html
    assert "showBadgeField" not in html
    assert "doBadgeLogin" not in html
    app = _read("app.js")
    assert "async badgeLogin(code)" in app, (
        "the scan bus's badge login entry point must survive the field's "
        "removal")
    assert "logs you in automatically" in html, (
        "the scan-anywhere copy is the only badge affordance left")


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


# --- Round 4, T6: the scanner feed (scanfeed.js + the app.js adapter) -----------

def test_scanfeed_loads_before_app_js():
    """Same failure class as the capture pin: a reversed order is a silently
    dead feed (the adapter references the HcrmScanFeed global at mount)."""
    html = _read("index.html")
    feed = html.find('src="/scanfeed.js"')
    app = html.find('src="/app.js"')
    assert feed != -1, "index.html no longer loads /scanfeed.js"
    assert app != -1, "index.html no longer loads /app.js"
    assert feed < app, (
        "scanfeed.js must load before app.js — installScanFeed references "
        "the HcrmScanFeed global at mount time")


def test_wedge_capture_is_not_removed():
    """The round-3 keyboard capture IS the round-4 fallback: with no daemon
    holding the grab, it is the only scan path. The feed must install
    BESIDE it, never replace it (coordinator decision: keep, unchanged)."""
    app = _read("app.js")
    assert "this.installScanCapture();" in app, (
        "installScanCapture is no longer called in mounted() — the wedge "
        "fallback (and the pre-pairing scan path) would be gone")
    assert "this.installScanFeed();" in app, (
        "installScanFeed is not called in mounted() — the feed never boots")
    assert "HcrmScanCapture.initialState" in app, (
        "the capture adapter no longer drives the round-3 reducer")


def test_feed_poll_never_holds_the_server():
    """The no-hold rule (r4rev M9, measured): a server-side hold freezes
    ui_check's virtual-time DOM dump and turns the browser gate red. The
    feed's request carries ONLY the cursor — the pacing is a client-side
    setInterval, and no wait/hold parameter may appear in code (comments
    are stripped first — this very rule gets discussed in the sources)."""
    combined = _read("scanfeed.js") + _read("app.js")
    code = re.sub(r"/\*.*?\*/", "", combined, flags=re.DOTALL)
    code = re.sub(r"//[^\n]*", "", code)
    for forbidden in ("wait_ms", "waitMs", "longpoll", "long-poll"):
        assert forbidden not in code, (
            f"{forbidden!r} found in the feed code — a server-side hold "
            "would hang the browser gate (r4rev M9)")
    app = _read("app.js")
    assert re.search(r"\"/scanner/events\?after=\" \+ this\._feedState\.cursor", app), (
        "the feed poll URL must be exactly /scanner/events?after=<cursor> — "
        "any added parameter risks reintroducing a hold")


def test_feed_poll_interval_is_the_client_paced_second():
    """POLL_INTERVAL_MS is pinned here AND in the Node suite (the
    SCAN_DEFER_MS precedent): 1 s paced, and the adapter must use the
    module constant, not a private number that can drift."""
    src = _read("scanfeed.js")
    m = re.search(r"^const POLL_INTERVAL_MS = (\d+);", src, re.MULTILINE)
    assert m, "POLL_INTERVAL_MS not found in scanfeed.js"
    assert int(m.group(1)) == 1000
    assert "HcrmScanFeed.POLL_INTERVAL_MS" in _read("app.js"), (
        "the adapter must pace the poll with the reducer's exported "
        "constant, not a private setInterval number")


def test_feed_sink_is_the_existing_scan_bus():
    """The feed's ONLY sink must be enqueueScan(code, kind) — the same bus
    the wedge capture feeds — so badge login / logoff / save-then-switch /
    cart-scan / intake run identically whichever path delivered the scan.
    Any other sink would fork the flows (the round-4 architecture's core
    decision)."""
    app = _read("app.js")
    assert "this.enqueueScan(effect.code, effect.kind);" in app, (
        "the feed adapter no longer routes scans through enqueueScan")


def test_scanner_feed_pill_and_pairing_markup_present():
    """The pill (data-scanner-feed) renders on every view (the topbar), so
    the operator can always see which scan path is live. It is a BUTTON
    now: the pairing affordance (terminal id + 8-char code form, and the
    Unpair recovery) opens from it in a popover — the login screen's
    pairing panel is gone (owner decision, login-screen cleanup)."""
    html = _read("index.html")
    assert 'data-scanner-feed' in html, (
        "the data-scanner-feed pill binding is missing from index.html")
    assert re.search(r'<button[^>]*class="feed-pill"', html), (
        "the pill must be a button — it opens the pairing popover")
    assert "pairOpen" in html, "the popover toggle binding is missing"
    assert "pairTerminal" in html, (
        "the pairing form is missing its submit handler")
    assert "unpairTerminal" in html, (
        "the unpair affordance is missing (recovery for a stale binding)")
    # the LOGIN section itself must no longer carry the pairing form nor
    # the removed badge field — they moved to the pill / were deleted
    login = _html_section("login")
    for absent in ("Terminal id", "Pair this terminal", "Pairing code",
                   "Scan your QR badge here", "Use a badge code instead"):
        assert absent not in login, (
            f"{absent!r} must not be on the login screen (moved to the "
            "pill popover / removed)")


def test_terminal_token_key_is_the_persisted_binding():
    """The terminal token's localStorage key: one name, used for read and
    write, so a future rename cannot strand a paired kiosk."""
    app = _read("app.js")
    assert 'const TERMINAL_TOKEN_KEY = "hcrm_terminal_token"' in app
    for call in ('localStorage.getItem(TERMINAL_TOKEN_KEY)',
                 'localStorage.setItem(TERMINAL_TOKEN_KEY',
                 'localStorage.removeItem(TERMINAL_TOKEN_KEY)'):
        assert call in app, f"missing {call} — the binding would not persist"


def test_purchases_route_wiring_is_pinned():
    """The #/purchases tab needs five cooperating pieces (nav link, view
    mapping, section, watcher load, mounted load); drop any one and the
    page renders EMPTY rather than erroring — exactly the silent shape
    this suite exists to pin. The watcher/mounted pair is the piece a
    well-meaning "de-duplication" eats, killing either deep links or
    navigation."""
    html = _read("index.html")
    assert 'href="#/purchases"' in html
    assert "currentView === 'purchases'" in html
    watcher = _method_body("currentView(view) {")
    assert "loadMyOrders()" in watcher, \
        "the currentView watcher must load purchase history (navigation)"
    mounted = _method_body("mounted() {")
    assert "loadMyOrders()" in mounted, \
        "mounted() must load purchase history (direct deep link)"


def test_similar_items_fetch_pins_explicit_limit():
    """openDetail's "similar items" fetch must keep its explicit limit=5:
    the vector-search default is now rank-everything, so dropping the
    parameter would silently fetch the whole catalogue per item click."""
    js = _read("app.js")
    assert re.search(
        r'"/items/vector-search\?q="[\s\S]{0,300}?&limit=5', js), \
        "the similar-items fetch must carry an explicit limit"


def test_session_expired_clears_the_cart_view():
    """BOTH 401 teardown paths must reset this.cart exactly like logout()
    does: the next user can log in flagged (must_change_password skips
    loadCart), so a lingering cart would display — and be mutable — under
    the new token (a cross-user leak). The reset lives in the ONE
    canonical handleSessionExpired(), and the event handler delegates to
    it (api()'s throwing path) — the two paths cannot drift apart."""
    js = _read("app.js")
    body = _method_body("handleSessionExpired() {")
    assert "this.cart = { lines: [], total_qty: 0, total_cents: 0 }" in body
    assert "_cartImportedLegacy = false" in body
    m = re.search(r'"hcrm-session-expired", \(\) => \{([\s\S]*?)\}\);', js)
    assert m, "the hcrm-session-expired handler must exist"
    assert "handleSessionExpired()" in m.group(1), \
        "the event handler must delegate to the canonical teardown"


def test_stale_search_responses_are_generation_guarded():
    """reloadItems must bump a request generation and loadMore must drop
    superseded responses on BOTH the resolve and the reject path: without
    the reject-path refetch, an erroring superseded request abandons the
    fresh reload and the grid silently stays empty (round-2 review). The
    interleavings themselves are behaviour-tested in tests/js/
    loadmore.test.mjs; this pins the mechanism's presence."""
    js = _read("app.js")
    assert "_itemsGen = (this._itemsGen || 0) + 1" in js
    body = _method_body("async loadMore() {")
    assert body.count("gen !== this._itemsGen) continue") == 2, \
        "both the resolve and the reject path must refetch when superseded"
