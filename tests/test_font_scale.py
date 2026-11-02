"""Design refresh C2 pins: the device font-size stepper.

The BEHAVIOUR (grid, clamp, storage guards, the exact root px, the end guards
and the percentage hint) is node-tested against the real sources in
tests/js/fontscale.test.mjs. What pytest adds is the wiring a node run cannot
see cheaply:

* the markup is TWO native buttons in the `.userbox` cluster, immediately after
  the language toggle and before the scanner pill (whose popover anchors
  right), with no literal text node — the glyph is inline SVG, and every word
  (accessible name, percentage title) is a CSV binding;
* the stored scale is applied to the ROOT element BEFORE mount, with no write
  on that path (a first visit stays unwritten);
* the `fontScale` watcher is registered as a watcher, applies before it
  persists (a storage-blocked kiosk still scales), and it is the only writer;
* logout never clears the key (a device preference, like `hcrm_lang`), and the
  key itself lives only in the helper (one definition, no drift);
* the copy rows exist in both languages, name the ACTION, and the hint carries
  `{percent}` with the language's own percent spacing.
"""

import importlib.util
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(REPO_ROOT, "static", "app.js")
INDEX_HTML = os.path.join(REPO_ROOT, "static", "index.html")
STYLE_CSS = os.path.join(REPO_ROOT, "static", "style.css")
INTERACTION_JS = os.path.join(REPO_ROOT, "static", "interaction.js")
GLOSSARY = os.path.join(REPO_ROOT, "docs", "danish-terminology.md")


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _method_body(js, header):
    """Brace-match one method out of app.js (the contract-test idiom)."""
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


def _braced_block(text, header):
    """The whole `header ... { ... }` block, brace-matched."""
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


def _button_block(html, step):
    """The whole <button> carrying data-font-step="step" (open tag included)."""
    marker = html.index(f'data-font-step="{step}"')
    start = html.rindex("<button", 0, marker)
    end = html.index("</button>", start) + len("</button>")
    return html[start:end]


def _copy_rows():
    """The compiled-source rows, loaded through the real validator."""
    spec = importlib.util.spec_from_file_location(
        "_i18n_under_test_font_scale", os.path.join(REPO_ROOT, "scripts", "_i18n.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    rows, problems = module.read_rows()
    assert problems == []
    return {row.id: row for row in rows}, module


def test_markup_is_two_native_buttons_beside_the_language_toggle():
    html = _read(INDEX_HTML)
    assert 'class="font-scale no-print"' in html, (
        "the stepper is a .no-print cluster: a printed receipt shows no UI chrome")
    assert "data-font-scale-control" in html
    assert ':data-font-scale="fontScalePercent"' in html, (
        "the wrapper must expose the live percentage as machine data")

    down, up = _button_block(html, "down"), _button_block(html, "up")
    for step, block in (("down", down), ("up", up)):
        assert block.startswith("<button"), f"data-font-step={step} is not a native button"
        assert 'type="button"' in block, "the stepper must never submit a form"
        assert 'class="font-step"' in block
        assert f'data-font-step="{step}"' in block
        assert ':title="fontScaleHint"' in block, (
            "the title must show the current percentage from the CSV hint")
        assert block.count("<svg") == 1
        assert 'aria-hidden="true"' in block and 'focusable="false"' in block, (
            "the glyph is decorative; the accessible name is the aria-label")
        # No literal text node: an "A+"/"A-" couplet would be hardcoded copy.
        open_end = block.index(">") + 1
        inner = block[open_end:block.rindex("</button>")]
        inner = re.sub(r"<svg[\s\S]*?</svg>", "", inner).strip()
        assert inner == "", f"the {step} button contains a hardcoded text node: {inner!r}"

    assert '@click="smallerFont"' in down
    assert ':disabled="!canShrinkFont"' in down
    assert ":aria-label=\"t('ui.font_smaller')\"" in down
    assert '@click="largerFont"' in up
    assert ':disabled="!canGrowFont"' in up
    assert ":aria-label=\"t('ui.font_larger')\"" in up

    # Cluster position: after the language flag (the established leftmost
    # control) and before the scanner pill, whose popover anchors to the
    # userbox's right edge.
    userbox = html.index('class="userbox"')
    toggle = html.index('class="lang-toggle')
    scale = html.index('class="font-scale')
    pill = html.index('class="feed-pill"')
    assert userbox < toggle < scale < pill


def test_css_block_keeps_the_cluster_geometry():
    css = _read(STYLE_CSS)
    wrapper = _braced_block(css, ".font-scale {")
    for pin in ("display: inline-flex", "gap: 2px", "flex: 0 0 auto"):
        assert pin in wrapper, f".font-scale CSS lost {pin!r}"
    step = _braced_block(css, ".font-step {")
    for pin in ("width: 28px", "height: 28px", "flex: 0 0 auto", "padding: 0",
                "border: 1px solid var(--border)"):
        assert pin in step, f".font-step CSS lost {pin!r}"
    disabled = _braced_block(css, ".font-step:disabled {")
    assert "cursor: not-allowed" in disabled
    assert "opacity:" in disabled
    focus = _braced_block(css, ".font-step:focus-visible {")
    assert "outline: 2px solid var(--accent)" in focus
    assert "width: 20px" in _braced_block(css, ".font-step svg {")
    # The 900px reflow is grid-only; it must not squeeze or hide the stepper.
    media = _braced_block(css, "@media (max-width: 900px)")
    assert "font-scale" not in media and "font-step" not in media


def test_boot_applies_the_stored_scale_before_mount_and_never_writes():
    js = _read(APP_JS)
    assert "const bootFontScale = HcrmInteraction.readScale(localStorage);" in js
    assert "HcrmInteraction.applyScale(bootFontScale);" in js
    assert "fontScale: bootFontScale," in js, "data() must read the captured boot value"
    assert js.index("HcrmI18n.applyLang(boot.lang)") \
        < js.index("const bootFontScale") \
        < js.index("HcrmInteraction.applyScale(bootFontScale)") \
        < js.index("Vue.createApp({"), (
        "the font scale must be applied to the root before mount (and after the "
        "language, which the boot comments order)")
    # A first visit must not write: the boot path only reads.
    boot = js[js.index("const bootFontScale"):js.index("HcrmInteraction.applyScale(bootFontScale);")]
    assert "setItem" not in boot and "writeScale" not in boot, (
        "the boot path writes the preference — a first visit would be persisted")


def test_the_watcher_is_the_only_writer_and_survives_logout():
    js = _read(APP_JS)
    watcher = _method_body(js, "fontScale(next) {")
    assert "HcrmInteraction.applyScale(next);" in watcher
    assert "HcrmInteraction.writeScale(localStorage, next);" in watcher
    assert watcher.index("applyScale") < watcher.index("writeScale"), (
        "apply BEFORE persist: a storage-blocked kiosk must still scale this session")
    # Registered as a watcher (a body in methods would never fire).
    assert js.index("watch: {") < js.index("fontScale(next) {") < js.index("methods: {")

    for header, direction in (("smallerFont() {", "-1"), ("largerFont() {", "1")):
        body = _method_body(js, header)
        assert f"HcrmInteraction.stepScale(this.fontScale, {direction})" in body
        assert "localStorage" not in body, "the buttons must not bypass the watcher"
    assert js.count("this.fontScale =") == 2, (
        "only the two stepper methods may assign the scale")

    # The key lives in the helper only, and logout never touches it.
    assert "hcrm_font_scale" not in js
    assert "FONT_SCALE_KEY" not in js, (
        "app.js must go through readScale/writeScale, not reach for the key directly")
    logout = _method_body(js, "async logout() {")
    assert "fontScale" not in logout
    assert "FONT_SCALE_KEY" not in logout and "hcrm_font_scale" not in logout, (
        "logout must not clear the device's font preference")
    for line in js.splitlines():
        if "removeItem" in line:
            assert "font" not in line.lower() and "scale" not in line.lower(), (
                f"logout clears the font preference: {line.strip()}")
    assert "localStorage.clear(" not in js


def test_the_helper_constants_are_the_specified_grid():
    src = _read(INTERACTION_JS)
    pins = {
        "FONT_SCALE_KEY": '"hcrm_font_scale"',
        "FONT_SCALE_MIN": "0.85",
        "FONT_SCALE_MAX": "1.35",
        "FONT_SCALE_STEP": "0.1",
        "FONT_SCALE_DEFAULT": "1",
        "ROOT_FONT_PX": "16",
    }
    for name, value in pins.items():
        pattern = rf"^  const {name} = {re.escape(value)};"
        assert re.search(pattern, src, re.MULTILINE), f"interaction.js lost {name} = {value}"


def test_the_helper_loads_before_app_js():
    html = _read(INDEX_HTML)
    feed = html.index('src="/scanfeed.js"')
    interaction = html.index('src="/interaction.js"')
    app = html.index('src="/app.js"')
    assert feed < interaction < app, (
        "interaction.js must load after the scan adapters and before app.js — "
        "the boot read references HcrmInteraction at module scope")


def test_copy_rows_are_action_sentences_in_both_languages():
    by_id, i18n = _copy_rows()
    smaller = by_id["ui.font_smaller"]
    larger = by_id["ui.font_larger"]
    hint = by_id["ui.font_scale_hint"]

    assert smaller.en == "Make the text smaller"
    assert smaller.da == "Gør teksten mindre"
    assert larger.en == "Make the text larger"
    assert larger.da == "Gør teksten større"
    for row in (smaller, larger):
        assert i18n.placeholders(row.en) == i18n.placeholders(row.da) == [], (
            "the accessible name is an action, not a parameterised sentence")
        assert row.en.startswith("Make the text ") and row.da.startswith("Gør teksten "), (
            "verb-first in both languages (the Danish tone rule)")

    assert i18n.placeholders(hint.en) == i18n.placeholders(hint.da) == ["percent"]
    assert hint.en.replace("{percent}", "100") == "Text size: 100%"
    assert hint.da.replace("{percent}", "100") == "Tekststørrelse: 100 %", (
        "Danish puts a space before the percent sign; the row owns that, not the JS")
    # Sentence case, Danish and English alike: only the first word is capitalised.
    for cell in (hint.en, hint.da):
        words = cell.split()
        assert not any(word[:1].isupper() for word in words[1:] if word[:1].isalpha()), (
            f"not sentence case: {cell!r}")


def test_the_glossary_owns_the_new_terms():
    glossary = _read(GLOSSARY)
    for term in ("tekststørrelse", "Gør teksten større", "Gør teksten mindre"):
        assert term in glossary, (
            "the new UI terms must be added to docs/danish-terminology.md in the "
            "same commit as the rows that need them")
