"""Session-expiry control-flow pins (the uncaught-rejection defect).

The BEHAVIOUR is node-tested in tests/js/sessionexpired.test.mjs against the
shipped api(), the guard and loadCart. This file pins the WIRING a node run
cannot see:

* the 401 branch marks the thrown error with `sessionExpired: true` (and keeps
  the HTTP status + translated message);
* the narrow window guard is installed at module scope, before Vue mounts,
  and only ever calls preventDefault from the flag test;
* loadCart swallows ONLY that flag and rethrows real failures;
* the login entry points stop after the teardown instead of overriding the
  #/login route with #/catalogue.
"""

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _method_body(js, header):
    """Brace-match one function/method body out of app.js."""
    assert header.endswith("{"), "header must include the opening brace"
    start = js.index(header)
    open_at = start + len(header) - 1
    depth = 0
    for i in range(open_at, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[start:i + 1]
    raise AssertionError(f"unbalanced braces after {header!r}")


def test_the_401_signal_is_flagged_and_the_guard_is_narrow():
    js = _read(APP_JS)
    # The thrown error keeps status + translated message and gains the flag.
    api = _method_body(
        js, 'async function api(path, { method = "GET", body, raw = false, token } = {}) {')
    branch = api[api.index("if (res.status === 401"):]
    assert "Object.assign(new Error(tGlobal(\"auth.session_expired\"))" in branch
    assert "status: 401," in branch
    assert "sessionExpired: true," in branch

    # The predicate accepts ONLY the boolean true flag.
    predicate = _method_body(js, "function isSessionExpiredRejection(reason) {")
    assert "reason.sessionExpired === true" in predicate

    # The guard prevents the default for that reason and nothing else.
    guard = _method_body(js, "function installSessionExpiredGuard(target) {")
    assert 'target.addEventListener("unhandledrejection"' in guard
    assert "isSessionExpiredRejection(event.reason)" in guard
    assert "event.preventDefault()" in guard

    # Installed at module scope, before the app (and any boot/route load) mounts.
    assert "installSessionExpiredGuard(window);" in js
    assert js.index("installSessionExpiredGuard(window);") < js.index("Vue.createApp({")


def test_load_cart_swallows_only_the_session_signal():
    body = _method_body(_read(APP_JS), "async loadCart() {")
    assert 'this.cart = await api("/cart");' in body
    assert "if (isSessionExpiredRejection(e)) return;" in body
    assert "throw e;" in body, "a real failure must still surface"


def test_login_entry_points_stop_after_the_teardown():
    js = _read(APP_JS)
    after = _method_body(js, "async afterLogin(data) {")
    # The post-login tail lives in ONE shared helper (afterLogin and the
    # flagged password change both run it — two copies drift); the 401
    # guards moved with it, unchanged.
    assert "await this._enterCatalogue();" in after
    enter = _method_body(js, "async _enterCatalogue() {")
    assert re.search(r"await this\.loadCart\(\);\s*\n\s*if \(!this\.user\) return;", enter), (
        "afterLogin must not keep routing to the catalogue after a 401 load")
    assert re.search(
        r"await this\.replayStagedScans\(\);\s*\n\s*if \(!this\.user\) return;", enter), (
        "a 401 during staged-scan replay must also stop the routing")

    switch = _method_body(js, "async switchAccount(code) {")
    assert re.search(
        r"await this\.loadCart\(\);[^\n]*\n\s*if \(!this\.user\) return;", switch), (
        "switchAccount must not announce/route an incoming cart after a 401")
