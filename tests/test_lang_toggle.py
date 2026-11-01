"""Item 3 static pins: the language toggle, its wiring, and the t()-binding ratchet.

The BEHAVIOUR (boot plan, strip, toggle, watcher, descriptor rendering, the real-Vue
reactivity proof) is pinned by tests/js/langtoggle.test.mjs against the real sources.
What pytest adds is what a node run or a DOM dump cannot see cheaply: the markup and
CSS pins, the persistence wiring (one writer, never cleared on logout), the boot order,
the CSV row being an action sentence, and the ratchet that replaced
tests/test_i18n_csv.py::test_no_ui_migration_has_started.
"""

import importlib.util
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")
INDEX_HTML = os.path.join(REPO_ROOT, "static", "index.html")
STYLE_CSS = os.path.join(REPO_ROOT, "static", "style.css")
STATIC_DIR = os.path.join(REPO_ROOT, "static")


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


def _braced_block(text: str, header: str) -> str:
    """The whole `header { ... }` block, brace-matched (the _method_body
    discipline: a first-`}` slice reads one rule, not the block, and a rule
    added after it would go unseen)."""
    start = text.index(header)
    open_at = text.index("{", start)
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError(f"unbalanced braces after {header!r}")


def _toggle_block(html: str) -> str:
    start = html.index('class="lang-toggle')
    end = html.index("</button>", start)
    return html[start:end]


def _svgs(block: str) -> list[str]:
    """The button's inline SVGs, each wrapper kept WITH its own drawing, so a
    swapped drawing (right data-flag/viewBox, wrong art) is detectable."""
    spans, pos = [], 0
    while True:
        start = block.find("<svg", pos)
        if start < 0:
            return spans
        end = block.index("</svg>", start) + len("</svg>")
        spans.append(block[start:end])
        pos = end


def test_toggle_markup_pins():
    html = _read(INDEX_HTML)
    block = _toggle_block(html)
    # The sentinel polarity (ruling 1): current, target, and the static flags.
    assert ':data-lang="lang"' in block
    assert ':data-lang-toggle="nextLang"' in block
    assert '@click="toggleLang"' in block
    assert 'class="lang-toggle no-print"' in block
    # The accessible name is the ACTION in the current language, bound (not
    # static): it flips with the toggle.
    assert ":aria-label=\"t('lang.switch_to')\"" in block
    assert ":title=\"t('lang.switch_to')\"" in block
    # Leftmost in .userbox: before the feed pill, whose popover anchors right.
    userbox = html.index('class="userbox"')
    pill = html.index('class="feed-pill"')
    toggle = html.index('class="lang-toggle')
    assert userbox < toggle < pill


def test_toggle_flags_are_true_geometry_and_decorative():
    html = _read(INDEX_HTML)
    block = _toggle_block(html)
    svgs = _svgs(block)
    assert len(svgs) == 2, "the toggle must carry exactly two inline flags"
    da = next((s for s in svgs if 'data-flag="da"' in s), None)
    en = next((s for s in svgs if 'data-flag="en"' in s), None)
    assert da and en, "one STATIC data-flag per SVG (a swapped drawing must surface)"
    # Dannebrog: the `nextLang === 'da'` branch, official 37:28 proportions,
    # 4-unit white cross offset 12 from the hoist — and NONE of the Union
    # Jack's art (a content swap keeps both wrappers but draws the wrong flag).
    assert "v-if=\"nextLang === 'da'\"" in da.split(">")[0]
    assert 'viewBox="0 0 37 28"' in da and '#C8102E' in da
    assert '#012169' not in da and "<path" not in da, (
        "the Dannebrog SVG must not contain the Union Jack's art")
    assert '<rect x="12" y="0" width="4" height="28" fill="#fff"/>' in da
    assert '<rect x="0" y="12" width="37" height="4" fill="#fff"/>' in da
    # Union Jack: the v-else branch, true 2:1 ratio (60:30), union blue, and
    # NONE of the Dannebrog's cross; stroked paths carry fill="none" (a
    # default fill would black the saltire).
    assert "v-else" in en.split(">")[0]
    assert 'viewBox="0 0 60 30"' in en and '#012169' in en
    assert '<rect x="12" y="0" width="4"' not in en, (
        "the Union Jack SVG must not contain the Dannebrog's cross")
    paths = re.findall(r"<path[^>]*>", en)
    assert paths, "the Union Jack must draw its saltire with <path> strokes"
    for path in paths:
        assert 'fill="none"' in path, f"stroked path without fill=none: {path}"
    # Decorative: out of the tab order and the accessible name.
    assert block.count('aria-hidden="true"') == 2
    assert block.count('focusable="false"') == 2
    # Inline SVG only: no images, no network, no emoji flags anywhere in static/.
    assert "<img" not in block and "http" not in block
    for name in os.listdir(STATIC_DIR):
        if not name.endswith((".js", ".html", ".css")) or name.startswith("vendor"):
            continue
        text = _read(os.path.join(STATIC_DIR, name))
        assert not any(0x1F1E6 <= ord(ch) <= 0x1F1FF for ch in text), (
            f"emoji flag codepoint in static/{name}")


def test_toggle_css_block():
    css = _read(STYLE_CSS)
    block = css[css.index(".lang-toggle {"):css.index("/* ---------- buttons, forms")]
    for pin in ("width: 32px", "height: 28px", "flex: 0 0 auto",
                "outline: 2px solid var(--accent)", "width: 24px", "height: 16px"):
        assert pin in block, f".lang-toggle CSS lost {pin!r}"
    # The 900px query is grid-only and must stay untouched by this item. The
    # WHOLE block is read: a rule appended after the first one would otherwise
    # escape the pin (e.g. hiding the toggle on narrow screens).
    media = _braced_block(css, "@media (max-width: 900px)")
    assert "lang-toggle" not in media and "userbox" not in media


def test_persistence_has_one_writer_and_survives_logout():
    js = _read(APP_JS)
    # The runtime OWNS the storage key; app.js consumes it, so the writer and
    # detectLang can never read different keys.
    assert "const LANG_KEY = HcrmI18n.LANG_KEY;" in js
    assert 'const LANG_KEY = "hcrm_lang";' not in js
    # The watcher is the single post-boot writer, and syncs the document.
    watcher = _method_body(js, "lang(next) {")
    assert "localStorage.setItem(LANG_KEY, next)" in watcher
    assert "HcrmI18n.applyLang(next)" in watcher
    # The boot write happens only when ?lang was present.
    assert "if (boot.persist) { try { localStorage.setItem(LANG_KEY, boot.lang); }" in js
    # Logout must never clear the language: no removeItem mentions the key.
    for line in js.splitlines():
        if "removeItem" in line:
            assert "LANG_KEY" not in line and "hcrm_lang" not in line, (
                f"logout clears the language preference: {line.strip()}")
    # The strip rebuilds the URL from the plan's CLEANED search + the caller's
    # hash: dropping the hash would lose the SPA route.
    assert "location.pathname + boot.search + location.hash" in js
    # Order: plan -> apply -> strip, all before mount (the ?lang visit is
    # applied before it is consumed, so a storage-blocked device keeps it).
    assert js.index("HcrmI18n.bootPlan(") \
        < js.index("HcrmI18n.applyLang(boot.lang)") \
        < js.index("history.replaceState(") \
        < js.index("Vue.createApp({")
    # The watcher must be REGISTERED as a watcher: its body in `methods` would
    # never fire, and only a real click in a browser could notice.
    assert js.index("watch: {") < js.index("lang(next) {") < js.index("methods: {")
    # No `localStorage.clear()` anywhere: it would wipe the language (and the
    # kiosk's terminal token) behind the logout path's back.
    assert "localStorage.clear(" not in js
    # Never a reload: the strip must go through replaceState only.
    assert "location.href =" not in js


def test_toggle_row_is_an_action_sentence():
    """`lang.switch_to` names the ACTION in the current language, not a language."""
    spec = importlib.util.spec_from_file_location(
        "_i18n_under_test_toggle", os.path.join(REPO_ROOT, "scripts", "_i18n.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    rows, problems = module.read_rows()
    assert problems == []
    row = next(r for r in rows if r.id == "lang.switch_to")
    assert row.en == "Switch to Danish"
    assert row.da == "Skift til engelsk"
    for cell in (row.en, row.da):
        assert cell not in ("Danish", "English", "Dansk", "Engelsk", "da", "en"), (
            "the accessible name must be an action sentence, not a language name")


def test_only_the_toggle_binds_t_yet():
    """The ratchet that replaced test_no_ui_migration_has_started (item 3).

    Exactly ONE template binding exists at this stage: the toggle's label.
    The item-5 slices are the next edits allowed to add `t(` bindings.
    """
    html = _read(INDEX_HTML)
    # Count OCCURRENCES (not lines) so a reformat that puts both bindings on
    # one line cannot false-red, while a third binding still does.
    bound = re.findall(r'="t\(', html)
    assert len(bound) == 2, f"expected the toggle's two bindings, got {len(bound)}"
    for line in html.splitlines():
        if '="t(' in line:
            assert "lang.switch_to" in line, f"a second migration started: {line.strip()}"
    assert "{{ t(" not in html
    # The descriptor path is live but produces identical output today.
    assert "{{ toastText }}" in html and "{{ greetingText }}" in html
    assert "{{ toast }}" not in html and "{{ greeting }}" not in html
