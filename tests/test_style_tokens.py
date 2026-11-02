"""Design tokens: the palette, its measured contrast, and the brand typeface.

The palette lives in ONE block (`:root` in `static/style.css`) and every rule
reads the token names, so this file pins the block instead of the rules:

* the values are the cream/teal/orange set the design decided on, including the
  four SOFT tints (teal/orange/brick/sand) and the danger border tint, so the
  rethemed components need no literal tint;
* `--font-brand` leads with Waldorff (no font file ships — the kiosk is
  offline; the CSS comment documents the one-block upgrade path);
* the WCAG ratios are RECOMPUTED from the shipped values, so a future "nicer
  shade" edit that quietly breaks body-text legibility fails here rather than
  in a customer's eyesight. Targets: body text >= 7:1, white on the primary
  and destructive fills >= 4.5:1, secondary/chip text >= 4.5:1, and the
  danger/teal non-text borders >= 3:1. The orange pill borders (2.51:1 on the
  soft fill, 2.78:1 on cream) are a SECONDARY state marker with the label
  carrying the state at 14.10:1/16.38:1 — reported, not raised to 3:1, because
  the design mandates var(--accent-2) there and orange text is forbidden.
* the orange is a FILL only: it is never a text colour on a light background,
  and it must be used by a real rule (the open status chip), not just defined;
* every rethemed component block uses tokens — no legacy hexes may return.

Nothing here renders a browser (that is `scripts/ui_check.py`'s job); these are
the arithmetic and token-shape claims a DOM dump cannot see.
"""

import re

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
STYLE = REPO_ROOT / "static" / "style.css"


def _css() -> str:
    return STYLE.read_text(encoding="utf-8")


def _braced_block(css: str, header: str) -> str:
    """The whole `header { ... }` block, brace-matched (one-liners included)."""
    start = css.index(header)
    open_at = css.index("{", start)
    depth = 0
    for i in range(open_at, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[start:i + 1]
    raise AssertionError(f"unbalanced braces after {header!r}")


def _tokens() -> dict[str, str]:
    """The `:root { --name: value }` block, parsed."""
    css = _css()
    block = css[css.index(":root {"):css.index("}", css.index(":root {"))]
    return {name: value.strip()
            for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block)}


def _luminance(hexcolor: str) -> float:
    raw = hexcolor.lstrip("#")
    assert len(raw) == 6, f"expected a 6-digit hex colour, got {hexcolor!r}"
    channels = (int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(channel) for channel in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG 2.1 relative contrast between two opaque colours."""
    la, lb = _luminance(a), _luminance(b)
    high, low = max(la, lb), min(la, lb)
    return (high + 0.05) / (low + 0.05)


def test_the_token_names_are_stable():
    """Every rule reads these names; a rename is a retheme-wide refactor, never a
    quiet improvement, so the set is pinned."""
    assert set(_tokens()) == {
        "--bg", "--surface", "--text", "--muted", "--muted-soft",
        "--accent", "--accent-dark", "--accent-soft", "--accent-2",
        "--accent-2-soft", "--danger", "--danger-soft", "--danger-border",
        "--border", "--font-brand", "--radius", "--shadow",
    }


def test_the_palette_is_the_cream_teal_orange_set():
    tokens = _tokens()
    # Warm cream paper, not the old cool grey.
    assert tokens["--bg"].lower() == "#fbf6ed"
    assert tokens["--surface"].lower() == "#fffcf6"
    assert tokens["--muted"].lower() == "#6b5d50"
    assert tokens["--muted-soft"].lower() == "#efe7db"     # sand tint
    assert tokens["--accent"].lower() == "#0e6b62"        # teal, primary
    assert tokens["--accent-dark"].lower() == "#0a544d"   # deeper teal
    assert tokens["--accent-soft"].lower() == "#e3f0ed"    # teal tint
    assert tokens["--accent-2"].lower() == "#e07a3c"      # orange, fill
    assert tokens["--accent-2-soft"].lower() == "#fbe8d9"  # orange tint
    assert tokens["--danger"].lower() == "#96271b"        # deep brick
    assert tokens["--danger-soft"].lower() == "#f7e3df"    # brick tint
    assert tokens["--danger-border"].lower() == "#ad6a5c"  # brick border tint


def test_the_measured_contrast_meets_the_design_targets():
    """The numbers the CSS comment states, recomputed from the shipped values."""
    t = _tokens()
    white = "#ffffff"
    pairs = {
        "--text on --bg (body copy, >= 7:1)": (t["--text"], t["--bg"], 7.0),
        "--text on --surface (>= 7:1)": (t["--text"], t["--surface"], 7.0),
        "--muted on --bg (secondary copy, >= 4.5:1)": (t["--muted"], t["--bg"], 4.5),
        "--muted on --surface (>= 4.5:1)": (t["--muted"], t["--surface"], 4.5),
        f"{white} on --accent (primary fill, >= 4.5:1)": (white, t["--accent"], 4.5),
        f"{white} on --accent-dark (hover, >= 4.5:1)": (white, t["--accent-dark"], 4.5),
        f"{white} on --danger (destructive fill, >= 4.5:1)": (white, t["--danger"], 4.5),
        "--accent on --bg (link/accent text, >= 4.5:1)": (t["--accent"], t["--bg"], 4.5),
        "--text on --accent-2 (open chip, >= 4.5:1)": (t["--text"], t["--accent-2"], 4.5),
        "--accent-dark on --accent-soft (>= 4.5:1)": (
            t["--accent-dark"], t["--accent-soft"], 4.5),
        "--muted on --muted-soft (>= 4.5:1)": (t["--muted"], t["--muted-soft"], 4.5),
        "--text on --accent-2-soft (>= 4.5:1)": (t["--text"], t["--accent-2-soft"], 4.5),
        "--danger on --danger-soft (>= 4.5:1)": (t["--danger"], t["--danger-soft"], 4.5),
        "--danger on --surface (countdown, >= 4.5:1)": (t["--danger"], t["--surface"], 4.5),
        "--danger-border on --danger-soft (border, >= 3:1)": (
            t["--danger-border"], t["--danger-soft"], 3.0),
        "--accent on --accent-soft (live border, >= 3:1)": (
            t["--accent"], t["--accent-soft"], 3.0),
    }
    misses = []
    for label, (fg, bg, target) in pairs.items():
        got = contrast(fg, bg)
        if got < target:
            misses.append(f"{label}: {got:.2f} < {target}")
    assert not misses, "contrast regression:\n  " + "\n  ".join(misses)


def test_the_orange_is_a_fill_never_text():
    """Decision 2: the orange fills; it never writes on a light background.

    `--text` on the orange fill is the sanctioned pairing (5.61:1); the orange
    itself as a `color:` would be 2.78:1 on cream (2.51:1 on its own tint),
    i.e. unreadable. The orange must do real work: it is the OPEN chip's fill
    and the off/unpaired pill's BORDER, never a label colour.
    """
    tokens = _tokens()
    css = _css()
    assert contrast(tokens["--text"], tokens["--accent-2"]) >= 4.5, (
        "the open-chip fill must carry --text at >= 4.5:1")
    assert contrast("#ffffff", tokens["--accent-2"]) < 4.5, (
        "white on the orange is NOT a sanctioned pairing — the CSS comment says so")
    # The token is not decorative dead weight: the open status chip fills with
    # it, and the scanner pill's state borders use it.
    open_chip = _braced_block(css, ".status-chip.open {")
    assert "background: var(--accent-2)" in open_chip
    assert "color: var(--text)" in open_chip
    off_pill = _braced_block(css, '.feed-pill[data-scanner-feed="off"] {')
    assert "border-color: var(--accent-2)" in off_pill
    assert "color: var(--text)" in off_pill
    unpaired = _braced_block(css, '.feed-pill[data-scanner-feed="unpaired"] {')
    assert "border-color: var(--accent-2)" in unpaired
    assert "color: var(--text)" in unpaired
    uses = [line for line in css.splitlines() if "var(--accent-2)" in line]
    assert len(uses) >= 3, (
        "--accent-2 must be used by real rules (chip fill + two pill borders)")
    # No rule may write with the orange, as a var OR as the literal hex.
    offenders = [line.strip() for line in css.splitlines()
                 if re.search(r"(?<![-\w])color:\s*(?:var\(--accent-2\)|#e07a3c)",
                              line, re.IGNORECASE)]
    assert offenders == [], (
        "no rule may use --accent-2 as a text colour:\n  " + "\n  ".join(offenders))


def test_the_rethemed_components_use_only_tokens():
    """The blocks this retheme touched carry no legacy tint literals."""
    css = _css()
    blocks = {
        ".status-chip.open {": ("background: var(--accent-2)", "color: var(--text)"),
        ".status-chip.resolved {": ("background: var(--accent-soft)",
                                     "color: var(--accent-dark)"),
        ".status-chip.dismissed {": ("background: var(--muted-soft)",
                                      "color: var(--muted)"),
        ".stock-warn {": ("background: var(--accent-2-soft)", "color: var(--text)",
                          "padding:", "border-radius:", "font-weight: 600"),
        '.feed-pill[data-scanner-feed="live"] {': (
            "background: var(--accent-soft)", "border-color: var(--accent)",
            "color: var(--accent-dark)"),
        '.feed-pill[data-scanner-feed="off"] {': (
            "background: var(--accent-2-soft)", "border-color: var(--accent-2)",
            "color: var(--text)"),
        '.feed-pill[data-scanner-feed="unpaired"] {': (
            "border-style: dashed", "border-color: var(--accent-2)",
            "color: var(--text)"),
        ".badge-warn {": ("background: var(--danger-soft)",
                          "border: 1px solid var(--danger-border)",
                          "color: var(--danger)"),
        ".countdown {": ("color: var(--danger)",),
        ".btn.danger:hover {": ("background: var(--danger)", "filter: brightness(.9)"),
    }
    misses = []
    for header, pins in blocks.items():
        block = _braced_block(css, header)
        for pin in pins:
            if pin not in block:
                misses.append(f"{header} lost {pin!r}")
        literals = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", block)
        if literals:
            misses.append(f"{header} still has literal colour(s): {literals}")
    assert not misses, "retheme regression:\n  " + "\n  ".join(misses)


def test_no_blue_slate_legacy_tints_survive():
    """The old blue/slate palette is gone from the stylesheet for good.

    These are the exact values the cream/teal/orange retheme replaced: the
    scan pill, suggestion items, order header/fallback, data-table selection,
    badge payload and the status chips. A reintroduced one fails the palette
    test rather than quietly re-cooling a warm surface.
    """
    css = _css().lower()
    patterns = {
        "#1e293b": r"#1e293b", "#e2e8f0": r"#e2e8f0",
        "#f1f5f9": r"#f1f5f9", "#2563eb": r"#2563eb",
        "#eff6ff": r"#eff6ff", "#e0ecff": r"#e0ecff",
        "#64748b": r"#64748b", "#475569": r"#475569",
        "#f8fafc": r"#f8fafc", "#f8fafd": r"#f8fafd",
        "rgba(37, 99, 235, …)": r"rgba?\(\s*37\s*,\s*99\s*,\s*235",
    }
    found = [name for name, pattern in patterns.items()
             if re.search(pattern, css)]
    assert not found, (
        "old blue-slate tint(s) returned to static/style.css: " + ", ".join(found))


def test_the_brand_typeface_is_the_brand_only():
    """`--font-brand` is a display face; body copy stays the system stack."""
    tokens = _tokens()
    family = tokens["--font-brand"]
    assert family.split(",")[0].strip().strip('"') == "Waldorff"
    for fallback in ('"Cormorant Garamond"', "Georgia", "serif"):
        assert fallback in family, fallback
    css = _css()
    brand = css[css.index(".brand {"):css.index("}", css.index(".brand {"))]
    assert "font-family: var(--font-brand)" in brand
    assert "font-weight: 600" in brand
    # ...and it is the ONLY user: a second surface with the display face would
    # need a design decision, not a drive-by.
    assert css.count("var(--font-brand)") == 1, (
        "only .brand may use the display face")
    # The upgrade path is documented where the value lives.
    assert "static/vendor/fonts/" in css and "@font-face" in css


def test_the_lockup_is_one_name_with_no_orphan_span_rule():
    """The header is `app.brand` as a single text node (design decision 1)."""
    html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "{{ t('app.brand') }}" in html
    assert "app.brand_shop" not in html, "the two-part lockup row is gone"
    assert not re.search(r"^\.brand span\s*\{", _css(), re.M), (
        "no markup carries a second brand span any more, so the rule would be dead")
