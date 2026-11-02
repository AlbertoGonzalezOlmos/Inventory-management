"""Wiring pins for the i18n machinery (item 2).

The behaviour lives in `static/i18n.js` and is pinned by `tests/js/i18n.test.mjs`; what
this file checks is the plumbing a node run cannot see: that `app.js` actually delegates
to the runtime (and never reaches into it directly), that the language is resolved and
applied *before* Vue mounts, that the runtime does not write a preference (item 3 owns
persistence) and that it never regrows a currency formatter or a network call.

`static/index.html`'s script order is pinned in tests/test_frontend_contract.py, with
the other order pins.
"""

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")
I18N_JS = os.path.join(REPO_ROOT, "static", "i18n.js")
INDEX_HTML = os.path.join(REPO_ROOT, "static", "index.html")

ITEM2_API = (
    "money", "moneyCode", "messagesFor", "hasMessage", "translate", "plural",
    "detectLang", "applyLang", "dateLocale", "errorText", "missingCount",
    # item 3: the switch — pure helpers; persistence stays in app.js
    "otherLang", "queryLang", "stripLangQuery", "bootPlan", "descriptorText",
)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _method_body(js: str, header: str) -> str:
    """Brace-match one method out of app.js (the contract-test idiom)."""
    assert header.endswith("{"), "header must include the opening brace"
    start = js.index(header)
    depth = 0
    for i in range(start + len(header) - 1, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[start:i + 1]
    raise AssertionError(f"unbalanced braces after {header!r}")


def test_app_js_exposes_the_i18n_helpers():
    js = _read(APP_JS)
    assert "HcrmI18n.translate(id, vars, this.lang)" in js
    assert "HcrmI18n.plural(base, n, vars, this.lang)" in js
    assert "HcrmI18n.dateLocale(this.lang)" in js
    for header in ("t(id, vars) {", "tp(base, n, vars) {", "formatLang() {"):
        body = _method_body(js, header)
        assert "HcrmI18n." in body, f"{header} must delegate to the runtime"


def test_format_price_injects_the_compiled_currency_code():
    body = _method_body(_read(APP_JS), "formatPrice(cents) {")
    assert "HcrmI18n.money(cents, HcrmI18n.moneyCode())" in body, (
        "the code comes from the compiled copy, not from a literal in app.js")
    assert "this." not in body, (
        "the money path must not depend on the Vue instance: the item-1 node harness "
        "calls the extracted method on a bare object")
    assert "Intl" not in body


def test_data_resolves_the_language_and_applylang_runs_before_mount():
    js = _read(APP_JS)
    assert "lang: boot.lang," in js, (
        "the reactive language must be the CAPTURED boot value — re-detecting "
        "after the ?lang strip loses it on a storage-blocked device")
    assert "lang: HcrmI18n.detectLang(" not in js
    plan = js.index("HcrmI18n.bootPlan(location.search, localStorage)")
    applied = js.index("HcrmI18n.applyLang(boot.lang)")
    mount = js.index("Vue.createApp({")
    assert plan < applied < mount, (
        "the boot plan runs first, <html lang> and the title are right on the "
        "first painted frame, and the strip cannot precede the application")


def test_static_title_is_language_neutral():
    """The pre-JS title carries only the brand: `applyLang` sets the real one from the
    compiled copy before mount, so a wrong-language title can never be printed."""
    html = _read(INDEX_HTML)
    assert "<title>HCRM</title>" in html
    assert "Shop Catalogue" not in html, (
        "the English title lives in i18n/strings.csv now (app.title)")


def test_runtime_exports_the_item2_api_and_never_writes_a_preference():
    src = _read(I18N_JS)
    exported = src[src.index("const HcrmI18n = {"):]
    for name in ITEM2_API:
        assert re.search(rf"\b{name}\b", exported), f"{name} must be exported"
    assert "setItem" not in src, "persistence is item 3; the runtime only reads"


def test_runtime_has_no_currency_formatter_or_network_call():
    src = _read(I18N_JS)
    for banned in ("Intl.NumberFormat", "fetch(", "XMLHttpRequest"):
        assert banned not in src, f"static/i18n.js must stay offline and Intl-free: {banned}"
