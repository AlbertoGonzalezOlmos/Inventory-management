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
    markup must (not) carry. Anchored on the <section tag itself via regex:
    the bare `currentView === '<view>'` string also appears in the nav's
    :class bindings (catalogue/table/admin/account/purchases), which would
    silently slice from the nav instead of the section.

    The end bound is whichever comes first of the next <section and </main>:
    the LAST section (admin) has no following <section, so a next-tag-only
    bound would make the helper unusable — and loudly misleading — for it."""
    html = _read("index.html")
    m = re.search(rf"<section[^>]*currentView === '{view}'", html)
    assert m, f"no <section> guard for view {view!r}"
    bounds = [b for b in (html.find("<section", m.end()), html.find("</main>", m.end()))
              if b != -1]
    assert bounds, f"no following <section> or </main> after view {view!r}"
    return html[m.start():min(bounds)]


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
    the operator can always see which scan path is live. It is a BUTTON,
    and since the one-click change the MANUAL pairing form must be gone
    EVERYWHERE — the unpaired pill pairs from the daemon's offer file in
    one click, so any reintroduced terminal-id/code form would fork the
    flow (the whole-file tightening of the old login-section-only pin:
    the form's last home was the pill popover, not the login screen)."""
    html = _read("index.html")
    app = _read("app.js")
    assert 'data-scanner-feed' in html, (
        "the data-scanner-feed pill binding is missing from index.html")
    assert re.search(r'<button[^>]*class="feed-pill"', html), (
        "the pill must be a button — it pairs / manages the pairing")
    assert "unpairTerminal" in html, (
        "the unpair affordance is missing (recovery for a stale binding)")
    # the manual pairing form must not come back ANYWHERE (whole file, not
    # just the login section — replaces the login-section absence entries
    # for these three strings)
    for absent in ("Terminal id", "Pairing code", "pair-form",
                   "Pair this terminal"):
        assert absent not in html, (
            f"{absent!r} must not be anywhere in index.html — the manual "
            "pairing form is gone; the pill pairs in one click")
    assert not re.search(r"pairForm|pairError", app), (
        "the manual pairing form's state must not come back (pairForm/"
        "pairError) — the one-click flow has no form state")
    # (?<!un): unpairTerminal is the intended survivor
    assert not re.search(r"(?<!un)pairTerminal", app), (
        "the manual pairing submit must be gone — autoPair is the path")
    # the LOGIN section keeps its own absence pin for the removed badge
    # field (the login-screen cleanup, PR #35)
    login = _html_section("login")
    for absent in ("Scan your QR badge here", "Use a badge code instead"):
        assert absent not in login, (
            f"{absent!r} must not be on the login screen (removed, "
            "login-screen cleanup)")


def test_pill_is_one_click_pair_when_unpaired():
    """The unpaired pill is a one-click PAIR BUTTON: @click routes through
    onPillClick (a method, so these _method_body pins have a body to
    read), which branches to autoPair() when unpaired. autoPair must call
    the auto endpoint with raw: true (a 401 stale offer must never run
    api()'s global session-expiry teardown) and carry the pairBusy
    double-click guard — as a JS guard, NOT :disabled (disabling the
    focused pill drops focus to <body> and greys a status indicator)."""
    html = _read("index.html")
    pill = re.search(r'<button[^>]*class="feed-pill"[^>]*>', html)
    assert pill, "the feed pill button is missing"
    assert '@click="onPillClick"' in pill.group(0), (
        "the pill must route its click through onPillClick")
    assert ":disabled" not in pill.group(0), (
        "the pill must not carry :disabled — the busy guard is JS-side "
        "(pairBusy), not a UA-disabled status pill")
    app = _read("app.js")
    click_body = _method_body("onPillClick() {")
    assert 'this.scannerFeed === "unpaired"' in click_body \
        and "autoPair()" in click_body, (
            "onPillClick must branch to autoPair() when unpaired")
    auto = _method_body("async autoPair() {")
    assert '"/scanner/pair/auto"' in auto, (
        "autoPair must call the one-click endpoint")
    assert re.search(r'method: "POST", body: \{\}, raw: true,', auto), (
        "autoPair must call the endpoint with raw: true (the exact call-"
        "site form — a bare `raw: true` assert is satisfied by the comment "
        "above the call, mutation-proven) — a 401 stale offer is not a "
        "session expiry and must never log the kiosk out")
    assert re.search(r"if \(this\.pairBusy\) return;", auto), (
        "autoPair must carry the pairBusy double-click guard")


def test_pair_popover_is_wired_to_the_pill():
    """The PAIRED-state popover wiring: the pill's click toggle (via
    onPillClick), the backdrop's outside-click dismissal, the
    paired-only v-if mount (the unpaired pill is an action button, not a
    disclosure), the pairOpen state + focus-management watcher, and the
    _closeOverlays anti-stranding reset. A bare `pairOpen in html` is
    satisfied by an HTML comment — every pin binds to real markup or
    code."""
    html = _read("index.html")
    assert re.search(
        r'<button[^>]*class="feed-pill"[^>]*@click="onPillClick"',
        html), "the pill must route its click through onPillClick"
    assert '<div class="popover-backdrop" @click="pairOpen = false">' \
        in html, "the popover's outside-click backdrop is missing"
    assert '<template v-if="pairOpen && scannerFeed !== \'unpaired\'">' \
        in html, ("the popover must mount ONLY in the paired state — the "
                  "unpaired pill pairs in one click and opens nothing")
    app = _read("app.js")
    assert re.search(r"^\s+pairOpen: false,", app, re.M), (
        "the pairOpen data flag is missing")
    # The watcher must exist AND do the focus management: a body-less
    # `pairOpen(on) { return; }` satisfies an existence assert while the
    # popover opens with focus nowhere and closes by dropping a keyboard
    # user on <body> (the detached popover was the focused element).
    watcher = _method_body("pairOpen(on) {")
    assert ".pair-popover button" in watcher, (
        "the pairOpen watcher must focus the popover's Unpair button — its "
        "only focusable; the autofocus attribute only fires on initial "
        "page load, not on a v-if mount")
    assert ".feed-pill" in watcher, (
        "the pairOpen watcher must return focus to the pill when the "
        "popover closes")
    assert "this.pairOpen = false;" in _method_body("_closeOverlays() {"), (
        "_closeOverlays must close the popover with the other overlays")


def test_pair_popover_cannot_strand_under_a_modal():
    """The popover paints inside .topbar's stacking context (position:
    sticky + z-index: 10), so it AND its full-viewport dismiss backdrop sit
    below every .modal-backdrop (z-index: 50, all of them children of #app).
    An open modal therefore makes the popover's inputs unclickable and its
    outside-click backdrop unreachable — the popover can only be closed
    after the modal is. The two guards: a barcode scanned while the popover
    is open is dropped by the scan discipline ("finish the current dialog
    first"), and every path that raises an overlay closes the popover
    first. Losing any of them re-creates the stranded dropdown (PR #35
    review round 1: qwen #2 / kimi #3 / glm F1)."""
    app = _read("app.js")
    assert "this.pairOpen" in _method_body("_modalOpen() {"), (
        "_modalOpen() must count the pairing popover as an open dialog — "
        "otherwise a barcode scanned while it is open raises the intake "
        "modal over it and strands it")
    for header in ("openNextIntake() {", "async afterLogin(data) {",
                   "async armLogoff() {"):
        assert "this.pairOpen = false;" in _method_body(header), (
            f"{header} must close the pairing popover before raising its "
            "own overlay or navigating — the popover cannot survive under "
            "a modal (it paints below every .modal-backdrop)")
    # Since the one-click change the popover mounts ONLY in the paired
    # state: a server-side unpair (the poll's 401 — the terminal was
    # deleted) flips the pill to unpaired with the popover open, and an
    # INVISIBLE pairOpen=true still counts in _modalOpen() — every scan
    # would drop to "finish the current dialog" with nothing on screen.
    # The setPill effect is the one funnel every unpair passes through.
    feed = _method_body("_applyFeedEffect(effect) {")
    assert re.search(
        r'effect\.mode === "unpaired"[\s\S]*?this\.pairOpen = false;',
        feed), ("the setPill effect must close pairOpen when the pill goes "
                "unpaired — otherwise the invisibly-open popover strands "
                "the scan discipline")


def test_unpair_is_operator_gated_and_the_pill_discloses():
    """Unpair deletes the terminal token client-side and re-pairing needs a
    FRESH single-use offer written by `scanner_daemon.py --register` on the
    kiosk host — an operator-only recovery. The pill is on every view (the
    login-screen panel was logged-out-only), so a logged-in member must not
    get the button; `!user` preserves the logged-out first-install path.
    The disclosure semantics are pinned with it — STATE-CONDITIONAL since
    the one-click change: an unpaired pill is an action button and must
    not announce a popup it does not have (the attrs bind to null)."""
    html = _read("index.html")
    assert re.search(
        r'<button[^>]*v-if="!user \|\| isStaff"[^>]*@click="unpairTerminal"',
        html), ("Unpair must be gated to logged-out or staff — otherwise any "
                "logged-in member can disable the kiosk's scanner feed with "
                "two clicks, recoverable only with shell access to the host")
    assert re.search(r":aria-expanded=\"scannerFeed === 'unpaired' \? null", html), (
        "aria-expanded must be state-conditional — absent (null) when the "
        "pill is a one-click action button, present when it discloses")
    assert re.search(r":aria-haspopup=\"scannerFeed === 'unpaired' \? null", html), (
        "aria-haspopup must be state-conditional for the same reason")
    assert 'role="dialog" aria-label="Scanner pairing"' in html, (
        "the popover must be a labelled dialog")


def test_unpair_closes_the_popover_and_states_the_recovery():
    """Replaces the old focus-into-the-re-pair-form pin (the form is gone —
    the pill itself is the re-pair affordance now). Unpair must close the
    popover (the pill flips to the one-click state; an open paired-state
    popover over it would strand) and toast the recovery loop, so the
    operator knows the next action is --register, not a form."""
    body = _method_body("unpairTerminal() {")
    assert "this.pairOpen = false;" in body, (
        "unpairTerminal must close the popover — the pill becomes the "
        "one-click re-pair button and an open paired popover over it strands")
    assert "--register" in body, (
        "the unpair toast must name the recovery (re-run --register, then "
        "click the pill)")


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
