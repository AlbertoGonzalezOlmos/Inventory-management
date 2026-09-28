"""W2.3 — parser fixtures for scripts/ui_check.py's console detector.

These run under plain `pytest` with **no browser installed**, which is the
point: the detector is the one component whose failure mode is "silently
green", so its parsing must be regression-tested independently of any
browser being present (PLAN-v2 §9.8 gate).

Every fixture below is a real shape captured from a real browser, or the
adversarial case that separates a correct parser from a blindly-truncating
one (§9.3).
"""

import importlib.util
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_ui_check():
    path = os.path.join(REPO_ROOT, "scripts", "ui_check.py")
    spec = importlib.util.spec_from_file_location("ui_check_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


uic = _load_ui_check()

# --- real captured shapes ---------------------------------------------------- #
# (Raw strings: these are verbatim stderr bytes, and the messages contain both
# single and double quotes. Escaping them by hand is how syntax errors happen.)

# Chrome for Testing 131: `:CONSOLE(n)]` + `, source: <url> (n)`
CHROME_131_EVAL_ERROR = r"""[36707:36707:0928/121305.660100:INFO:CONSOLE(9)] "Uncaught EvalError: Refused to evaluate a string as JavaScript because 'unsafe-eval' is not an allowed source of script in the following Content Security Policy directive: \"script-src 'self'\".", source: http://127.0.0.1:8151/ (9)
"""

# Chrome 150: `:CONSOLE:n]`, and this record has NO `, source:` suffix at all.
CHROME_150_EVAL_ERROR = r"""[36707:36707:0928/121305.660100:INFO:CONSOLE:9] "Uncaught EvalError: Evaluating a string as JavaScript violates the following Content Security Policy directive because 'unsafe-eval' is not an allowed source of script: script-src 'self'".
"""

# The recorded /docs failure: three records, MIXED shapes in one stream
# (`:CONSOLE:6]`, `:CONSOLE(0)]`, `:CONSOLE:15]`).
CHROME_150_DOCS_VIOLATIONS = r"""[36938:36938:0928/121315.439250:INFO:CONSOLE:6] "Loading the stylesheet 'https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css' violates the following Content Security Policy directive: \"style-src 'self' 'unsafe-inline'\". The action has been blocked.", source: http://127.0.0.1:8137/docs (6)
[36938:36938:0928/121315.439394:INFO:CONSOLE(0)] "Loading the script 'https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js' violates the following Content Security Policy directive: \"script-src 'self'\". The action has been blocked.", source: http://127.0.0.1:8137/docs (0)
[36938:36938:0928/121315.439417:INFO:CONSOLE:15] "Executing inline script violates the following Content Security Policy directive 'script-src 'self''. Either the 'unsafe-inline' keyword, a hash ('sha256-QOOQu4W1oxGqd2nbXbxiA1Di6OHQOLQD+o+G9oWL8YY='), or a nonce ('nonce-...') is required to enable inline execution. The action has been blocked.", source: http://127.0.0.1:8137/docs (15)
"""

# The benign advisory that used to be allowlisted; the markup now carries
# autocomplete attributes so it no longer appears (W4.0).
BENIGN_ADVISORY = r"""[38540:38540:0928/121856.047436:INFO:CONSOLE(0)] "[DOM] Input elements should have autocomplete attributes (suggested: \"current-password\"): (More info: https://goo.gl/9p2vKq) %o", source: http://127.0.0.1:8138// (0)
"""

# The adversarial case (PLAN-v2 §9.3): a FAIL pattern AFTER an inner double
# quote. A capture-group regex with an optional source suffix yields 'Blocked '
# and reports is_error False — a real violation, missed.
FAIL_AFTER_INNER_QUOTE = r"""[1:1:0928/120000.000000:INFO:CONSOLE(1)] "Blocked \"foo\" Refused to connect", source: http://x/ (1)
"""

MULTILINE = '[1:1:0928/120000.000000:INFO:CONSOLE(2)] "line one\nline two Refused to load", source: http://x/ (2)\n'

NON_CONSOLE_NOISE = (
    "[36707:36707:0928/121305.543676:ERROR:dbus/object_proxy.cc(572)] Failed to "
    "call method: org.freedesktop.DBus.Properties.GetAll: object_path= "
    "/org/freedesktop/UPower/devices/DisplayDevice\n"
)


def test_both_real_browser_formats_parse():
    for label, stderr in (("chrome-131", CHROME_131_EVAL_ERROR),
                          ("chrome-150", CHROME_150_EVAL_ERROR)):
        assert uic.record_count(stderr) == 1, label
        messages = uic.console_records(stderr)
        assert len(messages) == 1, label
        assert messages[0].startswith("Uncaught EvalError"), (label, messages)
        assert uic.is_error(messages[0]) is True, label


def test_missing_source_suffix_still_parses():
    """Chrome 150 emits records without `, source: …`; a regex that requires it
    matched nothing at all (the original F1 blind spot)."""
    stderr = '[1:1:0:INFO:CONSOLE:9] "Uncaught TypeError: x is not a function".\n'
    messages = uic.console_records(stderr)
    assert messages == ["Uncaught TypeError: x is not a function"]
    assert uic.is_error(messages[0]) is True


def test_fail_pattern_after_an_inner_quote_is_not_truncated():
    messages = uic.console_records(FAIL_AFTER_INNER_QUOTE)
    assert len(messages) == 1
    assert "Refused to connect" in messages[0], messages
    assert messages[0].count('"') == 2, messages          # inner quotes preserved
    assert uic.is_error(messages[0]) is True               # …and still classified


def test_benign_advisory_parses_completely_but_is_not_an_error():
    messages = uic.console_records(BENIGN_ADVISORY)
    assert len(messages) == 1
    assert "current-password" in messages[0], messages     # not cut at the quote
    assert "goo.gl/9p2vKq" in messages[0], messages        # tail intact
    assert uic.is_error(messages[0]) is False


def test_multiline_message_survives():
    messages = uic.console_records(MULTILINE)
    assert len(messages) == 1
    assert "line one\nline two Refused to load" == messages[0]
    assert uic.is_error(messages[0]) is True


def test_three_docs_violations_all_detected_regardless_of_format():
    """The recorded /docs failure had two `:CONSOLE:n]` records and one
    `:CONSOLE(n)]` record in the same stderr — mixed shapes in one stream."""
    assert uic.record_count(CHROME_150_DOCS_VIOLATIONS) == 3
    messages = uic.console_records(CHROME_150_DOCS_VIOLATIONS)
    assert len(messages) == 3
    assert all(uic.is_error(m) for m in messages), messages
    joined = "\n".join(messages)
    assert "swagger-ui.css" in joined and "swagger-ui-bundle.js" in joined
    assert "sha256-QOOQu4W1oxGqd2nbXbxiA1Di6OHQOLQD+o+G9oWL8YY=" in joined


def test_non_console_stderr_is_ignored_and_the_guard_stays_silent():
    assert uic.record_count(NON_CONSOLE_NOISE) == 0
    assert uic.console_records(NON_CONSOLE_NOISE) == []
    assert uic.parser_liveness_failure(NON_CONSOLE_NOISE) is None
    assert uic.parser_liveness_failure("") is None


def test_liveness_guard_fires_when_records_exist_but_none_parse(monkeypatch):
    """The guard is belt-and-braces for a future parser regression: it cannot
    fire with today's splitter (both use the same record regex), so simulate the
    regression it exists to catch."""
    stderr = '[1:1:0:INFO:CONSOLE(1)] "Uncaught EvalError: boom", source: http://x/ (1)\n'
    assert uic.parser_liveness_failure(stderr) is None          # healthy today
    monkeypatch.setattr(uic, "console_records", lambda _stderr: [])
    message = uic.parser_liveness_failure(stderr)
    assert message is not None and "0 of 1 record(s)" in message


def test_benign_allowlist_is_narrow_and_documented():
    # Only messages that WOULD match a fail pattern need allowlisting; the
    # advisory above is benign by classification, not by allowlist.
    assert uic.is_error("[Deprecation] something failed to load resource") is False
    assert uic.is_error("Third-party cookie will be blocked") is False
    assert uic.is_error("Failed to load resource: 404") is True
    for entry in uic.BENIGN_PREFIXES:
        assert not entry.startswith("[DOM] Input elements"), (
            "the autocomplete advisory was fixed in the markup (W4.0); the "
            "allowlist must shrink, not grow")


def test_detector_probe_page_defeats_truncation():
    """The probe payload must contain an inner quote AND a newline, otherwise it
    validates emission but not capture (§9.2 refinement 2)."""
    page = uic.PROBE_PAGE.format(token="deadbeef")
    assert "inner-quoted" in page and "second-line" in page
    assert "deadbeef" in page
    assert "console.error" in page


def test_exit_codes_are_distinct():
    assert len({uic.EXIT_PASS, uic.EXIT_CHECK_FAILED, uic.EXIT_UNREACHABLE,
                uic.EXIT_NO_BROWSER, uic.EXIT_DETECTOR_BLIND}) == 5
    assert uic.EXIT_DETECTOR_BLIND == 4 and uic.EXIT_NO_BROWSER == 3
