"""Design refresh C3 pins: hold-to-confirm and the keyed double-tap guard.

The BEHAVIOUR — the 700 ms threshold, early release, release-at-threshold
without a frame, Escape, pointer/keyboard source discipline, exactly-once,
latched auto-repeat, and the 600 ms keyed guard (including async in-flight and
window expiry) — is node-tested against the real `static/interaction.js` in
tests/js/holdguard.test.mjs. What pytest adds is what a node run cannot see:

* the final purchase button has NO click handler (a quick click must only show
  the hint), starts on pointerdown/keydown, cancels on pointerup (± pointerleave
  / pointercancel / blur / Escape), paints a `--hold` fill, and is described by
  the VISIBLE `checkout.hold_hint` line (stable id, aria-describedby) — the
  duration is in the copy, not only in a title tooltip;
* the toast outcome is announced by a stable polite live region (the visible
  toast is aria-hidden so the sentence is not read twice), and the hold/font
  controls are print-hidden;
* `created()` wires the machine and the guard, and the document/window safety
  listeners are added only while a run is live;
* every money/record-changing handler carries a guard key, and the keys are
  distinct with the intended identity (per item / member / request / line);
* reduced motion removes the pulse but never the fill (the mechanism);
* the hold's copy and the visible instruction, including the "about one
  second" duration in both languages;
* the print hole and the rem-sized stepper (the 85 % floor stays >= 24 px).
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


def _hold_button(html):
    """The whole final-purchase <button> (open tag through </button>)."""
    marker = html.index('class="btn block hold-btn no-print"')
    start = html.rindex("<button", 0, marker)
    end = html.index("</button>", start) + len("</button>")
    return html[start:end]


def _copy_rows():
    """The compiled-source rows, loaded through the real validator."""
    spec = importlib.util.spec_from_file_location(
        "_i18n_under_test_hold_guard", os.path.join(REPO_ROOT, "scripts", "_i18n.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    rows, problems = module.read_rows()
    assert problems == []
    return {row.id: row for row in rows}, module


def test_the_button_is_hold_only_and_wired_for_pointer_and_keyboard():
    html = _read(INDEX_HTML)
    block = _hold_button(html)
    # A quick click must not commit: no click handler at all (the comment is
    # stripped first, because it explains the absence of @click).
    live = re.sub(r"<!--[\s\S]*?-->", "", block)
    assert "@click" not in live
    assert '@click="donePurchase"' not in html, (
        "the final purchase is hold-to-confirm; a click handler commits on a tap")
    for pin in ("@pointerdown=\"beginPurchaseHold\"",
                "@pointerup=\"endPurchaseHold('pointerup')\"",
                "@pointerleave=\"endPurchaseHold('pointerleave')\"",
                "@pointercancel=\"endPurchaseHold('pointercancel')\"",
                "@keydown=\"purchaseHoldKeydown\"",
                "@keyup=\"purchaseHoldKeyup\"",
                "@blur=\"cancelPurchaseHold('blur')\"",
                "@contextmenu.prevent"):
        assert pin in block, f"the hold button lost {pin}"
    assert ':disabled="busy || purchase.length === 0"' in block
    assert ":data-holding=\"holdActive ? 'on' : null\"" in block
    assert 'aria-describedby="checkout-hold-hint"' in block, (
        "the hold requirement must be PROGRAMMATICALLY associated, not title-only")
    assert ':title=' not in block, (
        "the visible hint + aria-describedby replace the unreliable tooltip")
    assert "no-print" in block, "the hold control is UI chrome and must not print"
    # The visible label stays checkout.done; the fill and label are separate
    # layers so the bar can render under the text.
    assert '<span class="hold-fill" aria-hidden="true"></span>' in block
    assert '<span class="hold-label">{{ t(\'checkout.done\') }}</span>' in block
    assert html.count('class="btn block hold-btn no-print"') == 1, (
        "only the FINAL confirm is a hold; goToCheckout stays a normal click")


def test_the_hold_instruction_is_visible_described_and_print_hidden():
    html = _read(INDEX_HTML)
    hint_at = html.index('id="checkout-hold-hint"')
    hint = html[html.rindex("<p", 0, hint_at):html.index("</p>", hint_at)]
    assert 'class="muted hold-hint no-print"' in hint
    assert "{{ t('checkout.hold_hint') }}" in hint, (
        "the visible line must carry the same CSV row as the toast")
    assert html.count('id="checkout-hold-hint"') == 1, "stable id, exactly once"
    # Under the button (the instruction explains the control above it) and
    # before the scan hint that follows the actions.
    block = _hold_button(html)
    assert html.index(block) < hint_at < html.index('class="muted scan-hint"')
    assert "@media print" in _read(STYLE_CSS)
    print_block = _braced_block(_read(STYLE_CSS), "@media print {")
    assert ".no-print { display: none !important; }" in print_block, (
        "the no-print class is what keeps the chrome off paper")


def test_the_toast_outcome_is_announced_by_a_stable_live_region():
    html = _read(INDEX_HTML)
    # The announcement region is permanent (not v-if): a region inserted with
    # its text is announced inconsistently, the greeting comment records it.
    assert '<div class="sr-only" role="status" aria-live="polite">{{ toastText }}</div>' in html
    # The visible toast is the same sentence for sighted users, hidden from
    # AT so it is not announced twice.
    toast = html[html.index('class="toast"'):html.index("</div>", html.index('class="toast"'))]
    assert 'v-if="toast"' in toast
    assert 'aria-hidden="true"' in toast
    assert "{{ toastText }}" in toast
    # .sr-only must keep the region in the accessibility tree.
    css = _read(STYLE_CSS)
    sr = _braced_block(css, ".sr-only {")
    assert "clip-path: inset(50%)" in sr and "display: none" not in sr
    assert "position: absolute" in sr


def test_the_hold_css_tracks_the_variable_and_survives_reduced_motion():
    css = _read(STYLE_CSS)
    button = _braced_block(css, ".hold-btn {")
    for pin in ("position: relative", "overflow: hidden", "touch-action: none",
                "user-select: none"):
        assert pin in button, f".hold-btn CSS lost {pin!r}"
    fill = _braced_block(css, ".hold-fill {")
    for pin in ("position: absolute", "inset: 0", "background: var(--accent-dark)",
                "transform: scaleX(var(--hold, 0))",
                "transform-origin: left center", "pointer-events: none"):
        assert pin in fill, f".hold-fill CSS lost {pin!r}"
    assert ".hold-label { position: relative; z-index: 1; }" in css, (
        "the label must sit ABOVE the fill")
    assert "@keyframes hold-pulse" in css
    assert '.hold-btn[data-holding="on"] { animation: hold-pulse' in css
    # Reduced motion removes ONLY the decorative pulse. The fill (the visible
    # mechanism) must remain: no display:none / no transform override there.
    reduced = _braced_block(css, "@media (prefers-reduced-motion: reduce) {")
    assert "animation: none" in reduced
    assert ".hold-fill" not in reduced and "display: none" not in reduced, (
        "reduced motion must not remove the progress mechanism")


def test_created_wires_the_machine_the_guard_and_the_safety_listeners():
    js = _read(APP_JS)
    created = _method_body(js, "created() {")
    for pin in ('this._guard = HcrmInteraction.createGuard();',
                'this._hold = HcrmInteraction.createHold({',
                'duration: HcrmInteraction.HOLD_MS,',
                'onProgress: (value) => this._paintHold(value),',
                'onState: (phase) => { this.holdActive = phase !== "idle"; },',
                'onFire: () => this.donePurchase(),',
                'onCancel: () => this.showToast({ key: "checkout.hold_hint" }),',
                'this._onHoldPointerUp = () => this.endPurchaseHold("pointerup");',
                'this._onHoldKeyUp = (ev) => this.purchaseHoldKeyup(ev);',
                'this._onHoldBlur = () => this.cancelPurchaseHold("blur");',
                'if (ev.key === "Escape") this.cancelPurchaseHold("escape");'):
        assert pin in created, f"created() lost {pin!r}"
    assert "holdActive: false," in js, "the reactive holding flag must be declared"

    bind = _method_body(js, "_bindHoldListeners(on) {")
    for pin in ('document[method]("pointerup"', 'document[method]("keydown"',
                'document[method]("keyup"', 'window[method]("blur"'):
        assert pin in bind, f"_bindHoldListeners lost {pin!r}"

    paint = _method_body(js, "_paintHold(value) {")
    assert 'el.style.setProperty("--hold", String(value))' in paint, (
        "the fill must be painted from the rAF progress, not a Vue re-render")


def test_the_pointer_and_keyboard_adapter_gates_the_run_correctly():
    js = _read(APP_JS)
    begin = _method_body(js, "beginPurchaseHold(ev) {")
    assert "ev.button !== undefined && ev.button !== 0" in begin, (
        "a right-click must not start a money-committing hold")
    assert "ev.isPrimary === false" in begin
    assert 'this._hold.start("pointer")' in begin

    down = _method_body(js, "purchaseHoldKeydown(ev) {")
    assert "ev.preventDefault()" in down, "Space must not scroll / synthesise a click"
    assert "if (ev.repeat) return;" in down, (
        "key auto-repeat must not restart the clock or fire twice")
    assert 'this._hold.start("key")' in down
    assert '"Enter" && key !== " " && key !== "Spacebar"' in down

    up = _method_body(js, "purchaseHoldKeyup(ev) {")
    assert "if (this._holdKey && key !== this._holdKey) return;" in up, (
        "another key's release must not end the held run")
    assert 'this._hold.release("key", "keyup")' in up

    end = _method_body(js, "endPurchaseHold(reason) {")
    assert 'this._hold.release("pointer", reason)' in end, (
        "the reason is what the cancel hint reports (pointerleave vs pointerup)")
    cancel = _method_body(js, "cancelPurchaseHold(reason) {")
    assert "this._hold.cancel(reason)" in cancel


def test_every_money_and_record_action_carries_a_distinct_guard_key():
    js = _read(APP_JS)
    guarded = (
        ("addToPurchase(item) {", 'this._guard.guarded("cart:add:" + item.id'),
        ("incPurchase(id) {", 'this._guard.guarded("cart:inc:" + id'),
        ("decPurchase(id) {", 'this._guard.guarded("cart:dec:" + id'),
        ("setLineQty(id, ev) {", 'this._guard.guarded("cart:qty:" + id + ":" + raw'),
        ("removeFromPurchase(id) {", 'this._guard.guarded("cart:remove:" + id'),
        ("clearPurchase() {", 'this._guard.guarded("cart:clear"'),
        ("goToCheckout() {", 'this._guard.guarded("checkout:go"'),
        ("async donePurchase() {", 'this._guard.guarded("purchase:confirm"'),
        ("async deleteItem(item) {", 'this._guard.guarded("item:delete:" + item.id'),
        ("async deleteMember(member) {", 'this._guard.guarded("member:delete:" + member.id'),
        ("async revokeMemberBadge(member) {",
         'this._guard.guarded("member:badge:revoke:" + member.id'),
        ("async revokeMyBadge() {", 'this._guard.guarded("account:badge:revoke"'),
        ("async dismissRequest(req) {", 'this._guard.guarded("request:dismiss:" + req.id'),
        ("discardStagedScans() {", 'this._guard.guarded("scan:discard"'),
    )
    for header, pin in guarded:
        body = _method_body(js, header)
        assert pin in body, f"{header} is not guarded with {pin!r}"
    # The keys are action-identity scoped, so different actions never block
    # each other (the helper's keying contract).
    keys = [pin.split('"')[1] for _, pin in guarded]
    assert len(keys) == len(set(keys)), f"duplicate guard keys: {keys}"
    # donePurchase's guard wraps the real body (the hold and the guard are two
    # independent gates); the body stays where the toast pins expect it.
    wrapper = _method_body(js, "async donePurchase() {")
    assert "this._donePurchase()" in wrapper
    assert "async _donePurchase() {" in js


def test_the_timing_constants_are_the_design_numbers():
    src = _read(INTERACTION_JS)
    assert re.search(r"^  const HOLD_MS = 700;", src, re.MULTILINE), (
        "the final purchase hold is 700 ms")
    assert re.search(r"^  const GUARD_MS = 600;", src, re.MULTILINE), (
        "the double-tap guard window is 600 ms")
    assert "function createHold(" in src and "function createGuard(" in src
    assert "createHold, createGuard," in src, "both helpers must be exported"


def test_hold_hint_copy_is_a_bilingual_action_sentence():
    by_id, i18n = _copy_rows()
    hint = by_id["checkout.hold_hint"]
    assert hint.en == "Hold the button to complete the purchase (about one second)."
    assert hint.da == "Hold knappen nede for at gennemføre købet (cirka et sekund)."
    assert i18n.placeholders(hint.en) == i18n.placeholders(hint.da) == [], (
        "the hint is a complete sentence — no placeholder left to fill")
    assert "about one second" in hint.en and "cirka et sekund" in hint.da, (
        "the duration must be discoverable in the copy, not only from the fill")
    assert hint.da.startswith("Hold "), "Danish is verb-first (the tone rule)"
    for cell in (hint.en, hint.da):
        words = cell.split()
        assert not any(w[:1].isupper() for w in words[1:] if w[:1].isalpha()), (
            f"not sentence case: {cell!r}")
    glossary = _read(GLOSSARY)
    assert "hold nede" in glossary, (
        "the new term must be in docs/danish-terminology.md with its first row")
    # The brand directive: the surface is Hertha's Butik (official spelling,
    # English possessive kept); HCRM is only a machine identifier now.
    assert "Hertha's Butik" in glossary
    assert "Herthas" in glossary, "the Danish-genitive note must be recorded"
    assert "proper noun" in glossary
    assert "machine identifier" in glossary
    assert "brand stays the language-neutral `HCRM`" not in glossary, (
        "the superseded table row still contradicts the shipped brand")
