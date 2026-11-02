"""Design tokens: the palette, its measured contrast, and the brand typeface.

The palette lives in ONE block (`:root` in `static/style.css`) and every rule
reads the token names, so this file pins the block instead of the rules:

* the values are the cream/teal/orange set the design decided on, and
  `--font-brand` leads with Waldorff (no font file ships — the kiosk is
  offline; the CSS comment documents the one-block upgrade path);
* the WCAG ratios are RECOMPUTED from the shipped values, so a future "nicer
  shade" edit that quietly breaks body-text legibility fails here rather than
  in a customer's eyesight. Targets: body text >= 7:1, white on the primary
  and destructive fills >= 4.5:1, secondary text >= 4.5:1;
* the orange is a highlight only: no rule may use it as a text colour.

Nothing here renders a browser (that is `scripts/ui_check.py`'s job); these are
the arithmetic and token-shape claims a DOM dump cannot see.
"""

import re

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
STYLE = REPO_ROOT / "static" / "style.css"


def _css() -> str:
    return STYLE.read_text(encoding="utf-8")


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
        "--bg", "--surface", "--text", "--muted", "--accent", "--accent-dark",
        "--accent-2", "--danger", "--border", "--font-brand", "--radius",
        "--shadow",
    }


def test_the_palette_is_the_cream_teal_orange_set():
    tokens = _tokens()
    # Warm cream paper, not the old cool grey.
    assert tokens["--bg"].lower() == "#fbf6ed"
    assert tokens["--surface"].lower() == "#fffcf6"
    assert tokens["--accent"].lower() == "#0e6b62"        # teal, primary
    assert tokens["--accent-dark"].lower() == "#0a544d"   # deeper teal
    assert tokens["--accent-2"].lower() == "#e07a3c"      # orange, highlight
    assert tokens["--danger"].lower() == "#96271b"        # deep brick


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
    }
    misses = []
    for label, (fg, bg, target) in pairs.items():
        got = contrast(fg, bg)
        if got < target:
            misses.append(f"{label}: {got:.2f} < {target}")
    assert not misses, "contrast regression:\n  " + "\n  ".join(misses)


def test_the_orange_is_a_fill_never_text():
    """Decision 2: the orange highlights, it never writes on cream.

    `--text` on the orange fill is the sanctioned pairing (5.61:1); the orange
    itself as a `color:` would be 2.78:1 on cream, i.e. unreadable.
    """
    tokens = _tokens()
    assert contrast(tokens["--text"], tokens["--accent-2"]) >= 4.5, (
        "the badge fill must carry --text at >= 4.5:1")
    assert contrast("#ffffff", tokens["--accent-2"]) < 4.5, (
        "white on the orange is NOT a sanctioned pairing — the CSS comment says so")
    offenders = [line.strip() for line in _css().splitlines()
                 if re.search(r"(?<![-\w])color:\s*var\(--accent-2\)", line)]
    assert offenders == [], (
        "no rule may use --accent-2 as a text colour:\n  " + "\n  ".join(offenders))


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
