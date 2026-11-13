"""DKK source-contract pins (Danish/DKK roadmap, item 1).

The formatter's BEHAVIOUR is pinned byte-exactly by tests/js/money.test.mjs
(node) — including the real-method delegation and the mocked-jsPDF export
geometry. What pytest meaningfully adds is the wiring a node run or a DOM
dump cannot see: the forbidden-token sweep (the euro SYMBOL everywhere, the
old three-letter CODE only in shipped UI), `money()`'s own purity, the
script order, the CSV/PDF split in the source, and the documentation
repairs. Same discipline as tests/test_frontend_contract.py (the
_method_body helper is copied deliberately rather than imported across
test modules).
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC = REPO_ROOT / "static"

# Built dynamically (and named without the forbidden substrings) so this
# file can never match its own sweep below.
_SIGN = chr(0x20AC)          # the euro coin's glyph
_OLD = "E" + "UR"            # the old three-letter currency code
_MANGLED_ZERO = "DKK" + "0.00"


def _load_i18n():
    """The copy loader, imported by path (tests/test_i18n_csv.py's idiom)."""
    import importlib.util
    import sys

    scripts = str(REPO_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location(
        "_i18n_under_test", REPO_ROOT / "scripts" / "_i18n.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module   # dataclasses resolves cls.__module__ here
    spec.loader.exec_module(module)
    return module


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _method_body(header: str) -> str:
    """Extract a method body from static/app.js by brace matching.

    The header must be the DEFINITION line's text ending in ``{`` (e.g.
    ``"exportPDF() {"``) so a call site can never match — the honest
    alternative to unbounded regexes that lazily match a later definition
    and false-pass when the pinned call is deleted.
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


def _brace_block(src: str, header: str) -> str:
    """Brace-match one definition out of a JS source.

    ``header`` is the definition text ending in ``{`` (e.g.
    ``"function money(cents, code) {"``) so a call site can never match.
    """
    assert header.endswith("{"), "header must include the opening brace"
    start = src.index(header)
    open_i = start + len(header) - 1
    depth = 0
    for i in range(open_i, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unbalanced braces after {header!r}")


#: Where the old three-letter currency code is still forbidden: the shipped UI
#: and the docs that describe what it renders. `docs/` is deliberately OUT — the
#: i18n/migration items write notes there about the currency this item replaced,
#: and an archival note naming it is history, not a shipped claim (the same
#: carve-out tests/test_metadata_and_ops.py gives docs/plans/ for citations).
def _in_shipped_ui(path: str) -> bool:
    if path.startswith("static/"):
        return not path.startswith("static/vendor/")
    return path.startswith(("app/", "scripts/", "tests/")) or path == "README.md"


def test_no_euro_symbol_anywhere_and_no_eur_token_in_shipped_ui():
    """The euro SIGN is gone from the whole tree; the CODE from shipped UI.

    Two scopes on purpose:

    - U+20AC is forbidden in every tracked text file, `docs/` included: a euro
      sign anywhere (rendered copy, a label, a fixture) is requirement 1
      failing, and it has no legitimate use in this repo.
    - The three-letter code is forbidden only where money is defined or
      documented for the product: `static/` (minus the upstream vendor
      bytes), `app/`, `scripts/`, `tests/` and `README.md`.
    - The ba09ca7 sed artifact (the code glued to a bare zero) stays forbidden
      everywhere: it is nonsense in any file, historical docs included.

    The vendored libraries are upstream bytes and are never scanned.
    """
    text_exts = (".py", ".md", ".sh", ".rules", ".service", ".css", ".html",
                 ".mjs", ".js", ".json", ".toml", ".yml")
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True,
        check=True,
    ).stdout.splitlines()
    assert tracked, "git ls-files returned nothing"
    # Anti-vacuity: the tokens are built dynamically so this file cannot match
    # itself — which means a typo here would silently disable the whole sweep.
    assert _SIGN.encode("unicode_escape") == b"\\u20ac", (
        "the forbidden glyph must really be the euro sign (U+20AC)")
    assert _OLD and _MANGLED_ZERO, "the forbidden-token list must not be empty"
    offenders = []
    for path in tracked:
        if not path.endswith(text_exts) or path.startswith("static/vendor/"):
            continue
        text = (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
        if _SIGN in text:
            offenders.append(f"{path}: euro sign (U+20AC) in the tree")
        if _MANGLED_ZERO in text:
            offenders.append(f"{path}: mangled zero from the ba09ca7 sed")
        if _OLD in text and _in_shipped_ui(path):
            offenders.append(f"{path}: old currency code in shipped UI")
    assert not offenders, (
        "old-currency tokens remain where they may not be:\n  "
        + "\n  ".join(offenders))


def test_static_code_uses_no_platform_currency_formatter():
    """The formatter must stay hand-rolled: stable bytes, no NBSP, no ICU."""
    for name in ("app.js", "i18n.js", "index.html"):
        assert "Intl.NumberFormat" not in _read(name), (
            f"static/{name} still formats money through the platform "
            "currency formatter")


def test_format_price_delegates_to_the_i18n_runtime():
    body = _method_body("formatPrice(cents) {")
    assert "HcrmI18n.money(" in body, (
        "formatPrice must delegate to the shared runtime — the single choke "
        "point every rendered amount goes through")
    assert "NumberFormat" not in body, "the old formatter body must be gone"


def _strip_js_comments(src: str) -> str:
    """The code-only view of a JS file (comments removed).

    The house idiom before a substring scan is to strip comments first
    (tests/test_frontend_contract.py::test_feed_poll_never_holds_the_server):
    this runtime's header *explains* why the platform formatter is avoided,
    so scanning the raw file would fail on prose rather than on behaviour —
    and item 2, which grows this same file into the language runtime, would
    hit that on its first explanatory comment. ``//`` inside a string would
    be eaten too; this file has none, and a test-time loss there only makes
    the pin more forgiving, never less.
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"(?m)//[^\n]*$", "", src)


def test_i18n_runtime_is_dual_exported_and_clean():
    """The file stays importable both ways, names the token once, is clean."""
    src = _read("i18n.js")
    code = _strip_js_comments(src)
    assert re.search(r"\(function \([^)]*\) \{", code), (
        "must be IIFE-scoped so no helper leaks into the global namespace")
    assert "module.exports = HcrmI18n" in code, (
        "CommonJS guard missing — the node suite imports through it")
    assert "global.HcrmI18n = HcrmI18n" in code, "browser global missing"
    assert 'const MONEY_CODE = "DKK"' in code, "the single currency token"
    raw = (STATIC / "i18n.js").read_bytes()
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n"), (
        "exactly one trailing newline (tests/test_metadata_and_ops.py)")


def test_money_itself_is_dom_free_and_network_free():
    """money() must be pure: no DOM, no I/O, no platform currency formatter.

    Scoped to the FUNCTION BODY on purpose. Item 2 grows this same file into
    the language runtime, where applyLang legitimately touches
    documentElement/document.title — a file-wide `document.` ban would force
    item 2 to edit this item's test (or write around the pin). What must
    never appear ANYWHERE in the file is I/O or the platform's currency
    formatting, asserted below.
    """
    src = _read("i18n.js")
    body = _strip_js_comments(_brace_block(src, "function money(cents, code) {"))
    for banned in ("document.", "fetch(", "XMLHttpRequest", "Intl."):
        assert banned not in body, (
            f"money() must stay pure: {banned!r} in its body")
    for banned in ("Intl.NumberFormat", "fetch(", "XMLHttpRequest"):
        assert banned not in _strip_js_comments(src), (
            f"static/i18n.js must stay network-free and locale-free: {banned!r}")


def test_csv_export_stays_machine_numeric():
    body = _method_body("exportValue(row, key) {")
    assert "(row.price_cents / 100).toFixed(2)" in body, (
        "the CSV path must keep bare dot-decimal numbers — spreadsheets sum "
        "them and the formula-injection guard is written for plain values")
    assert "formatPrice" not in body, "exportValue must not format money"
    csv = _method_body("exportCSV() {")
    assert "csvEscape(this.exportValue(" in csv, (
        "CSV cells must route through exportValue + csvEscape")
    assert "\\ufeff" in csv, "the UTF-8 BOM prefix must stay"
    assert "items-export.csv" in csv


def test_pdf_export_declares_the_currency_conditionally():
    body = _method_body("exportPDF() {")
    assert 'cols.some((c) => c.key === "price_cents")' in body, (
        "the hasPrice probe is what keeps a stock-only export from "
        "claiming a currency it does not show")
    assert 'doc.text(this.t("export.pdf_prices"), 14, 28)' in body
    assert "startY: hasPrice ? 33 : 27" in body, (
        "the table must move down only when the currency note is printed — "
        "a no-price export keeps today's layout")
    assert "this.exportPdfValue(r, c.key)" in body, (
        "PDF body cells must route through exportPdfValue")
    assert "String(this.exportValue(r, c.key))" not in body, (
        "the PDF body must not fall back to the raw CSV values")
    # Slice E: no doc.text() argument is a literal any more — the title, the
    # summary and the currency note are CSV rows. jsPDF's standard fonts are
    # WinAnsi (CP1252), so every row that can be PRINTED must encode there
    # (the em dash is 0x97 and × is 0xD7; the analysis panel's σ is HTML-only).
    assert not re.findall(r'doc\.text\(\s*"', body), (
        "doc.text() must take a t()/CSV string, never a hardcoded literal")
    i18n = _load_i18n()
    rows, problems = i18n.read_rows()
    assert problems == []
    by_id = {row.id: row for row in rows}
    for rid in ("export.pdf_title", "export.pdf_prices", "export.summary"):
        for lang in i18n.LANGUAGES:
            by_id[rid].cell(lang).encode("cp1252")


def test_price_column_label_states_the_currency():
    """One label feeds the table header, the picker, the CSV and PDF heads —
    and since slice E that label is the `common.price_dkk` row, not a literal."""
    app = _read("app.js")
    assert '{ key: "price_cents", labelKey: "common.price_dkk" }' in app
    assert '{ key: "price_cents", label: ' not in app, (
        "the money column's label must come from the CSV, not a literal")


def test_ba09ca7_doc_hunks_are_repaired():
    """The blind euro-to-DKK sed produced nonsense; the six hunks now state
    what the app actually renders (repair, not revert — the definition of
    done is no old-currency token anywhere outside vendor/)."""
    repairs = {
        "docs/plans/r3plan-kimi.md": "month renders dotless with `0,00 DKK`",
        "docs/plans/r3plan-qwen.md": "`0,00 DKK` — never a blank hole.",
        "docs/plans/r3rev-qwen.md": "renders dotless with `0,00 DKK`.",
        "docs/plans/plan-qwen.md": "price in DKK (converted to `price_cents`)",
        "docs/plans/task5-proposal-kimi.md": "price in DKK (converted to `price_cents`)",
        "docs/plans/task5-proposal-qwen.md": "price in DKK",
    }
    for path, expected in repairs.items():
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
        assert expected in text, f"{path}: the repair is missing"
    assert "converted to `price_cents` via the existing `Math.round(x*100)` idiom" in (
        REPO_ROOT / "docs/plans/task5-proposal-qwen.md").read_text(encoding="utf-8")


def test_readme_claims_match_the_shipped_code():
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert f"still formats them as {_OLD}" not in text, (
        "the stale display-follow-up claim must be gone")
    assert f"Supplier prices displayed as {_OLD}" not in text, (
        "an implemented issue cannot stay in Tracked issues")
    assert "1.234,50 DKK" in text, "the shipped money contract must be stated"
    assert "static/i18n.js" in text, "the formatter's home must be named"


#: Money-carrying identifiers in the template. A line that mentions one must
#: render it through formatPrice().
_TEMPLATE_MONEY_IDENT = re.compile(
    r"(\bprice_cents\b|\bline_total_cents\b|\btotal_cents\b|\bvalueCents\b|"
    r"\bpurchaseTotalCents\b|\bcalTotalCents\b|\binventoryValueCents\b|"
    r"\bpriceMean\b|\bpriceMedian\b|\bpriceMin\b|\bpriceMax\b|\bpriceStd\b)")

#: Reviewed count of money-rendering bindings in static/index.html. Slice C
#: moved the calendar day-dot tooltip's price into calendarDayTitle() in
#: app.js (it still calls formatPrice), so the template count dropped 28 -> 27.
#: Bumping it is a deliberate, reviewed edit (item 5 re-points these bindings
#: at CSV keys and updates this number).
#: Item 2 (feat(cart)): 27 -> 29 — each panel's price cell now ALSO shows the
#: bare formatPrice(price_cents) next to the :title's cart.price_each call.
_TEMPLATE_MONEY_BINDINGS = 29

#: Words that make an interpolation money-bearing for the scaling check.
_MONEY_WORD = re.compile(r"cent|price|total|amount", re.I)


def test_template_renders_every_amount_through_format_price():
    """Requirement 1's completeness pin, inside the template.

    Every amount on screen must be the formatter's output: no raw cents, no
    kroner/øre rescaling, no manual decimals, no hand-written currency code —
    the class of defect that let a hard-coded currency label coexist with the
    wrong-currency formatter for a whole release. A new money surface is a
    deliberate edit (the binding count below), never a second formatter.
    """
    html = _read("index.html")
    offenders = []
    for lineno, line in enumerate(html.splitlines(), 1):
        if not _TEMPLATE_MONEY_IDENT.search(line):
            continue
        if "formatPrice(" not in line:
            offenders.append(f"index.html:{lineno}: money rendered without "
                             f"formatPrice(): {line.strip()}")
            continue
        if ".toFixed(" in line:
            offenders.append(f"index.html:{lineno}: manual number formatting "
                             f"next to a money field: {line.strip()}")
        if re.search(r"\b(price_cents|total_cents|line_total_cents|Cents)"
                     r"\s*[-+*/]\s*\d", line):
            offenders.append(f"index.html:{lineno}: arithmetic on a cents "
                             f"field: {line.strip()}")

    for expr in re.findall(r"\{\{(.*?)\}\}", html, flags=re.DOTALL):
        if not _MONEY_WORD.search(expr):
            continue
        scaled = re.search(r"[*/]\s*\d", expr)
        if "formatPrice(" in expr:
            if scaled or ".toFixed(" in expr:
                offenders.append("index.html: formatting/scaling inside a "
                                 f"money interpolation: {expr.strip()}")
        elif re.search(r"\b(price_cents|line_total_cents|total_cents|cents)\b",
                       expr, re.I):
            offenders.append("index.html: raw cents in an interpolation "
                             f"(no formatPrice): {expr.strip()}")
        elif scaled:
            offenders.append("index.html: money-word scaling in an "
                             f"interpolation: {expr.strip()}")
    assert not offenders, (
        "every amount on screen must be formatPrice() output:\n  "
        + "\n  ".join(offenders))

    count = html.count("formatPrice(")
    assert count == _TEMPLATE_MONEY_BINDINGS, (
        f"static/index.html has {count} money bindings, expected "
        f"{_TEMPLATE_MONEY_BINDINGS}: a new price surface is a reviewed change "
        "(and it must call formatPrice)")

    # A currency code never appears in the template: the two "Price (DKK)"
    # field labels ba09ca7 introduced are CSV rows now (common.price_dkk,
    # slice D), and a rendered amount takes its code from the formatter.
    assert html.count("DKK") == 0, (
        "no DKK literal in the template — the label comes from common.price_dkk "
        "and the amount's code from formatPrice")
    assert _SIGN not in html and "kr." not in html, (
        "no hand-written currency symbol in the template")
