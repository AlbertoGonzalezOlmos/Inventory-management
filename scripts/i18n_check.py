"""The i18n completeness checker (item 4 of the Danish-UI roadmap) — a RATCHET.

`i18n/strings.csv` is the only hand-edited copy source; this script proves the tree
still honours that. It is READ-ONLY: it opens files for reading and prints, and the
only "write" in the repo's i18n loop is `scripts/build_i18n.py --write`.

Five passes
-----------
1. ``csv``       — CSV integrity. Delegated to ``scripts/_i18n.py`` (never re-implemented);
                   the CSV must parse and validate before anything else can be trusted.
2. ``reference`` — Reference resolution. Every id referenced from *production* source
                   (``static/index.html`` and ``static/*.js``, excluding ``vendor/`` and the
                   generated ``static/i18n.messages.js``) must exist in the CSV — or be a
                   plural base whose ``.one``/``.other`` pair exists. Recognised call
                   shapes: ``t('id')``, ``tp('id', …)``, ``this.t(…)``, descriptor
                   ``key: "id"`` literals, and ``labelKey:``/``titleKey:``/``messageKey:``
                   literals. Comments are not references; string literals ARE (a
                   reference is itself a string literal — blanking its contents made
                   every ``this.t("…")`` call site invisible).
3. ``dead-row``  — Dead rows. Every CSV row must be referenced from production (pass 2),
                   from the tests (the seeded plumbing rows are pinned only by tests until
                   their slice lands), or be covered by a declared ``DYNAMIC_FAMILIES``
                   prefix or by the plural-suffix rule (``<base>.one|.other`` is covered by
                   a reference to ``<base>``). Each declared family must have at least one
                   row — a family with no rows is an error (the list cannot rot).
4. ``hardcoded`` — Hardcoded-copy scan. ``index.html``: every text node outside
                   script/style must be interpolation-only (after removing ``{{ … }}``);
                   an attribute's LITERAL value is copy unless its name is machine data
                   (``MACHINE_ATTRS``; ``value`` is a payload except on a button, and ARIA
                   is state except its text-bearing attributes), so ``value=``,
                   ``aria-description=``, ``data-label=``, ``on*=`` and any new attribute
                   are caught by default; copy inside template/bound expressions (single
                   words and backticks included) and inside inline ``<script>`` bodies is
                   scanned too (the scanner pill's label/title and a future inline script
                   live there). Event handlers (``@event``/``v-on:``/bare ``on*``) are JS
                   and get the FULL JS copy scan — item 5's ``showToast``/``confirm``
                   literals live there — while ``v-if``/``v-show``/``v-model`` expressions
                   get only the JS call scanners and the sentence heuristic (their
                   ``'login'``/``'admin'`` comparisons are boilerplate, not copy). ``static/*.js``: literal arguments to
                   ``showToast``/``confirm``/``alert`` (quoted or template), literal
                   ``label:``/``title:``/``placeholder:``/``aria-label:`` values,
                   sentence-shaped English literals, and multi-word template messages.
                   Comments are not copy.
5. ``ratchet``   — Scope. Pass-4 findings are INFO until their namespace is migrated:
                   ``i18n/migrated.txt`` lists migrated namespace prefixes (one per line,
                   e.g. ``app.``); a finding whose literal matches a CSV cell in a listed
                   namespace is an ERROR. If the file is ABSENT nothing is exempt — every
                   pass-4 finding is an error (this is how item 7 removes the ratchet).
                   A listed namespace with no CSV row is stale → error.
                   ``i18n/copy-allowlist.txt`` exempts findings by CONTENT —
                   ``<path>:<kind>:<escaped literal>`` — so an entry survives a line
                   move and only goes stale when the copy itself changes. Unused entries
                   are errors, so the list can only shrink.

Tests are a *reference source* for the dead-row pass — comments and docstrings are stripped
first (a mention in a comment must not keep a dead row alive) — but their ids are never
required to resolve: the suite deliberately calls ``translate()`` with synthetic ids (``ghost.row``,
``demo.only``) to pin the missing-key marker, so "a test references a missing id" cannot
be an error without exempting exactly the probes it exists to exercise. For the same
reason the runtime's own ``translate()``/``hasMessage()`` probes are not required
references: ``errorText`` deliberately probes ``error.*`` rows that arrive with item 6.

Exit codes: 0 clean · 1 violations (including stale namespaces and unused allowlist
entries) · 2 cannot run (bad flags/root, missing CSV, or a CSV that fails validation —
the same contract as ``build_i18n.py``). ``--quiet`` prints only the summary; ``--json``
prints the findings machine-readably (the same findings as the text output).

Usage:
    uv run python scripts/i18n_check.py [--quiet|--json] [--root PATH]
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
import _i18n  # noqa: E402  (the shared loader/validator: pass 1 is never re-implemented)

DISPLAY_CSV = "i18n/strings.csv"
DISPLAY_MIGRATED = "i18n/migrated.txt"
DISPLAY_ALLOWLIST = "i18n/copy-allowlist.txt"
DISPLAY_SCRIPT = "scripts/i18n_check.py"

ID_RE = _i18n.ID_RE
COPY_ATTRS = ("placeholder", "title", "aria-label", "alt")
ALLOWLIST_KINDS = ("text", "attr", "expr", "attr-expr", "call", "key", "template",
                   "sentence")
#: Attribute names whose literal values are MACHINE data, never copy. The rule is
#: INVERTED: any other literal attribute value containing a letter is copy (so a new
#: `value`/`aria-description`/`data-label`/`on*` cannot quietly bypass the gate).
#: Vue syntax (`:x`, `v-bind:x`, `@event`, `v-*`, `#slot`) is a JS expression, not
#: copy; the copy-bearing ones among them are scanned by the expression path.
MACHINE_ATTRS = frozenset({
    "class", "id", "href", "src", "srcset", "sizes", "type", "name", "for", "form",
    "list", "rel", "target", "lang", "dir", "charset", "content", "http-equiv",
    "autocomplete", "inputmode", "pattern", "method", "action", "enctype", "accept",
    "autofocus", "checked", "disabled", "readonly", "required", "multiple", "selected",
    "min", "max", "step", "minlength", "maxlength", "rows", "cols", "wrap", "novalidate",
    "formaction", "formmethod", "formnovalidate", "tabindex", "style", "hidden", "inert",
    "slot", "part", "is", "nonce", "draggable", "spellcheck", "contenteditable",
    "enterkeyhint", "decoding", "loading", "referrerpolicy", "integrity", "crossorigin",
    "media", "role", "focusable", "preserveAspectRatio", "xmlns", "version",
    "viewBox", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin",
    "fill-rule", "clip-rule", "d", "cx", "cy", "r", "rx", "ry", "x", "y", "x1", "x2",
    "y1", "y2", "points", "transform", "opacity", "offset", "stop-color", "width",
    "height",
})
#: ARIA attributes that carry human-readable copy (the rest are state/config).
COPY_ARIA_ATTRS = frozenset({"aria-label", "aria-description", "aria-placeholder",
                             "aria-roledescription", "aria-valuetext"})
#: `data-*` is machine by convention; these suffixes are the exception.
COPY_DATA_SUFFIXES = frozenset({"label", "title", "text", "message", "tooltip",
                                "placeholder"})
#: Bound attributes whose VALUE is copy (a Vue binding is an expression, not copy).
EXPR_COPY_ATTRS = frozenset(COPY_ATTRS) | COPY_ARIA_ATTRS
#: Literals inside a template/bound expression: single/double quotes AND backticks.
EXPR_LITERALS = re.compile(r"(['\"`])((?:\\.|(?!\1)[^\\\n])*)\1")
#: A bare inline event handler (`onclick`, `onsubmit`, …) is JS, not an attribute value.
BARE_ON = re.compile(r"^on[a-z]+$")
CALLS = re.compile(r"\b(showToast|confirm|alert)\s*\(\s*$")
KEY_LITERAL = re.compile(r"\b(label|title|placeholder|aria-label)\s*:\s*$")
REF_KEY = re.compile(
    r"\b(?:this\.)?t\(\s*(['\"])([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)\1"
    r"|\btp\(\s*(['\"])([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)\3"
    r"|\b(?:labelKey|titleKey|messageKey|key)\s*:\s*"
    r"(['\"])([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)\5")
STR = re.compile(r"(['\"])((?:\\.|(?!\1)[^\\\n])*)\1")
INTERP = re.compile(r"\{\{(.*?)\}\}", re.S)
PLACEHOLDER = re.compile(r"\$\{[^{}]*\}")
NS_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*\.$")
COMMENT_LINE = re.compile(r"^\s*#")

#: Ids produced by computation are exempt from the dead-row pass. Each entry maps a
#: prefix to (line in THIS file, producer comment). RULE: a declared family must match
#: at least one CSV row — a family with no rows is an error. Today no computed family
#: has rows yet (they arrive with items 5-6), so this list is empty: adding a
#: speculative entry would be exactly the rot the rule forbids. The one computed shape
#: that already exists — plural pairs — is covered by the suffix rule.
DYNAMIC_FAMILIES: dict[str, tuple[int, str]] = {}


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    pass_name: str
    severity: str
    kind: str
    message: str
    literal: str = ""   # the copy payload (used by the ratchet's namespace match)

    def text(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file
        return f"{where}: {self.severity} {self.pass_name}: {self.message}"

    def as_dict(self) -> dict:
        return {"file": self.file, "line": self.line, "pass": self.pass_name,
                "severity": self.severity, "kind": self.kind, "message": self.message}


# ---------------------------------------------------------------------------
# read-only helpers
# ---------------------------------------------------------------------------

def strip_html_comments(text: str) -> str:
    """Blank <!-- … --> keeping newlines (so line numbers survive)."""
    return re.sub(
        r"<!--.*?-->",
        lambda m: "".join("\n" if c == "\n" else " " for c in m.group(0)),
        text, flags=re.S)


def _starts_regex(src: str, i: int) -> bool:
    """True when the '/' at i starts a regex literal rather than a division.

    Division is always preceded by a value (identifier, digit, ``)``/``]``); a
    regex follows an operator or an opening bracket. This repo's regexes are all
    call arguments (``.replace(/…/, …)``), so the operator heuristic is enough.
    """
    j = i - 1
    while j >= 0 and src[j] in " \t\n\r":
        j -= 1
    if j < 0:
        return True
    if src[j] in "(,=:[!&|?{};":
        return True
    word = ""
    k = j
    while k >= 0 and (src[k].isalnum() or src[k] == "_"):
        word = src[k] + word
        k -= 1
    return word in ("return", "typeof", "case")


def scan_js(src: str, blank_strings: bool = True) -> tuple[str, list[tuple[str, int, str]]]:
    """Return (comment/string-blanked source, [(kind, offset, content)]).

    kind is 'str' (single/double) or 'tpl' (backtick). With `blank_strings` (the
    production default) string contents are blanked so a call/key context can never
    be faked by a string; with it off the strings are preserved and only comments are
    blanked — what the dead-row pass needs to read a TEST file's real references
    without trusting its comments.
    """
    clean = list(src)
    tokens: list[tuple[str, int, str]] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                clean[k] = " "
            i = j
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if src[k] != "\n":
                    clean[k] = " "
            i = j
        elif c == "/" and _starts_regex(src, i):
            # A regex literal (``s.replace(/"/g, '""')``): skip it whole. Treating
            # its quote characters as strings would swallow the rest of the file.
            j = i + 1
            in_class = False
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "[":
                    in_class = True
                elif src[j] == "]":
                    in_class = False
                elif src[j] == "/" and not in_class:
                    j += 1
                    break
                elif src[j] == "\n":
                    break
                j += 1
            while j < n and src[j].isalpha():
                j += 1
            for k in range(i, j):
                if src[k] != "\n":
                    clean[k] = " "
            i = j
        elif c in "'\"`":
            close = c
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == close:
                    j += 1
                    break
                j += 1
            tokens.append(("tpl" if c == "`" else "str", i, src[i + 1:j - 1]))
            if blank_strings:
                for k in range(i + 1, max(i + 1, j - 1)):
                    if clean[k] != "\n":
                        clean[k] = " "
            i = j
        else:
            i += 1
    return "".join(clean), tokens


def has_letter(text: str) -> bool:
    return any(ch.isalpha() for ch in text)


def line_of(src: str, offset: int) -> int:
    return src.count("\n", 0, offset) + 1


def id_prefix(rid: str) -> str:
    """The ratchet namespace of a row id: its first segment + '.', e.g. 'app.'."""
    return rid.split(".", 1)[0] + "."


def reference_names(ids: set[str]) -> set[str]:
    """Row ids plus the plural bases that address them (``tp(base, n)``)."""
    names = set(ids)
    for rid in ids:
        if rid.endswith(".one") and rid[:-4] + ".other" in ids:
            names.add(rid[:-4])
    return names


def ids_in_test_text(text: str, names: set[str]) -> set[str]:
    """Quoted literals in a test file that exactly name a row (or plural base)."""
    return {value for _quote, value in EXPR_LITERALS.findall(text) if value in names}


def python_test_references(text: str, names: set[str]) -> set[str]:
    """Reference names in a Python test file, ignoring comments and DOCSTRINGS.

    A comment like ``# 'zzz.unused' is the dead row`` must not keep that row alive:
    the checker's own guidance lives in comments. ``ast`` sees only real string
    constants; docstrings (the first statement of a module/class/function) are
    excluded explicitly.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:   # a broken test file is not a reference source
        return ids_in_test_text(text, names)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", [])
            first = body[0] if body else None
            value = getattr(first, "value", None) if isinstance(first, ast.Expr) else None
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                docstrings.add(id(value))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings and node.value in names:
            found.add(node.value)
    return found


# ---------------------------------------------------------------------------
# pass 1 — CSV integrity (delegated)
# ---------------------------------------------------------------------------

def pass_csv(csv_path: Path) -> tuple[list[_i18n.Row], list[Finding]]:
    rows, problems = _i18n.read_rows(csv_path)
    findings = []
    for problem in problems:
        match = re.match(r"^i18n/strings\.csv:(\d+): (.*)$", problem)
        if match:
            findings.append(Finding(DISPLAY_CSV, int(match.group(1)), "csv", "ERROR",
                                    "csv", match.group(2)))
        else:
            findings.append(Finding(DISPLAY_CSV, 0, "csv", "ERROR", "csv",
                                    problem.replace(DISPLAY_CSV + ": ", "", 1)))
    return rows, findings


# ---------------------------------------------------------------------------
# pass 2 — reference resolution (production sources only)
# ---------------------------------------------------------------------------

def production_sources(root: Path) -> tuple[list[tuple[str, str]], list[Finding]]:
    sources: list[tuple[str, str]] = []
    problems: list[Finding] = []
    html = root / "static" / "index.html"
    if html.is_file():
        sources.append(("static/index.html", html.read_text(encoding="utf-8")))
    else:
        problems.append(Finding("static/index.html", 0, "reference", "ERROR", "missing",
                                "static/index.html is missing"))
    for path in sorted((root / "static").glob("*.js")):
        if path.name == "i18n.messages.js":
            continue   # generated data, not a caller
        sources.append((f"static/{path.name}", path.read_text(encoding="utf-8")))
    return sources, problems


def pass_reference(sources: list[tuple[str, str]],
                   names: set[str]) -> tuple[list[Finding], set[str]]:
    findings: list[Finding] = []
    resolved: set[str] = set()
    for rel, raw in sources:
        # Keep STRING literals here (comments are still blanked): a reference IS a
        # string literal — ``this.t("auth.role.staff")`` — and blanking its contents
        # made every JS call site invisible (the HTML path always kept them).
        text = (strip_html_comments(raw) if rel.endswith(".html")
                else scan_js(raw, blank_strings=False)[0])
        for match in REF_KEY.finditer(text):
            rid = match.group(2) or match.group(4) or match.group(6)
            if rid in names:
                resolved.add(rid)
                continue
            if rid + ".one" in names and rid + ".other" in names:
                resolved.add(rid)   # a plural base: the pair is the row
                continue
            findings.append(Finding(
                rel, line_of(raw, match.start()), "reference", "ERROR", "unknown-id",
                f"unknown id {rid!r} — no such row in {DISPLAY_CSV}"))
    return findings, resolved


# ---------------------------------------------------------------------------
# pass 3 — dead rows
# ---------------------------------------------------------------------------

def family_findings(rows: list[_i18n.Row],
                    families: dict[str, tuple[int, str]] | None = None) -> list[Finding]:
    """Each declared dynamic family must match at least one row (no rotting list)."""
    families = DYNAMIC_FAMILIES if families is None else families
    findings = []
    for prefix, (line, producer) in sorted(families.items()):
        if not any(row.id.startswith(prefix) for row in rows):
            findings.append(Finding(
                DISPLAY_SCRIPT, line, "dead-row", "ERROR", "stale-family",
                f"DYNAMIC_FAMILIES entry {prefix!r} has no CSV row ({producer}) — "
                f"remove it or add the row"))
    return findings


def pass_dead_rows(rows: list[_i18n.Row], production_refs: set[str],
                   test_refs: set[str]) -> list[Finding]:
    ids = {row.id for row in rows}
    referenced = set(production_refs) | set(test_refs)
    # A plural base reference covers both siblings (tp(base, n) picks one at runtime).
    for rid in list(referenced):
        if rid + ".one" in ids and rid + ".other" in ids:
            referenced |= {rid + ".one", rid + ".other"}
    findings = family_findings(rows)
    for row in rows:
        if row.id in referenced:
            continue
        if any(row.id.startswith(prefix) for prefix in DYNAMIC_FAMILIES):
            continue
        findings.append(Finding(
            DISPLAY_CSV, row.line, "dead-row", "ERROR", "dead-row",
            f"row {row.id!r} is referenced by nothing — no UI call site, no test, "
            f"no dynamic family"))
    return findings


# ---------------------------------------------------------------------------
# pass 4 — hardcoded-copy scan
# ---------------------------------------------------------------------------

class _HtmlScan(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.in_script = False
        self.texts: list[tuple[int, str]] = []
        self.scripts: list[tuple[int, str]] = []
        self.attrs: list[tuple[int, str, dict[str, str | None]]] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
            self.in_script = tag == "script"
        self.attrs.append((self.getpos()[0], tag, dict(attrs)))

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script and data.strip():
            self.scripts.append((self.getpos()[0], data))   # scanned as JS, not text
        elif not self.skip and data.strip():
            self.texts.append((self.getpos()[0], data))


def copy_shaped(literal: str) -> bool:
    """A quoted literal that reads like copy (not a token, route, id or CSS value).

    Heuristic: contains a letter, is not an i18n id, and either contains whitespace
    or starts with an uppercase letter. Single lowercase words (``'live'``, ``'off'``
    — state tokens) are deliberately not flagged; multi-word labels are.
    """
    if not has_letter(literal) or ID_RE.match(literal):
        return False
    return bool(re.search(r"\s", literal.strip())) or literal[:1].isupper()


def expression_copy(expr: str) -> str | None:
    """The first letter-bearing, non-id literal in a template/bound expression.

    Backticks are included (``{{ `Open` }}``) and a single word counts
    (``:title="'close'"`` must not escape because it has no space). Ids — the
    arguments of ``t('lang.switch_to')`` — are pass 2's job, not copy.
    """
    literals = [lit for _quote, lit in EXPR_LITERALS.findall(expr)
                if has_letter(lit) and not ID_RE.match(lit)]
    if not literals:
        return None
    return next((lit for lit in literals if copy_shaped(lit)), literals[0])


def is_copy_attribute(tag: str, name: str, attrs_map: dict[str, str | None]) -> bool:
    """The INVERTED attribute rule: a literal value is copy unless the name is machine.

    Vue syntax (``:x``/``v-bind:x``/``@event``/``v-*``/``#slot``) is a JS expression and
    is excluded here — its copy-bearing attributes are read by the expression path. A
    literal in ANY OTHER attribute is copy by default, so ``value``, ``aria-description``,
    ``data-label`` and ``on*`` cannot bypass the gate. ``value`` is the one contextual
    exception: a form payload (``<option value="open">``) is machine data, but on a
    button it is the visible label.
    """
    if name.startswith((":", "@", "v-", "#")):
        return False
    if name.startswith("aria-"):
        # ARIA is state/config, except the attributes that carry human-readable text.
        return name in COPY_ARIA_ATTRS
    if name in MACHINE_ATTRS:
        return False
    if name.startswith("data-"):
        return name[len("data-"):] in COPY_DATA_SUFFIXES
    if name == "value":
        # A form PAYLOAD on <option>/<button> (the visible label is the text node);
        # on an <input> it is the visible default text or button label. Evidence: every
        # static value in the app is an <option> payload; the one <input> value is bound.
        return tag == "input"
    return True   # unknown attribute: copy by default (the inversion)


def js_attribute_mode(name: str) -> str:
    """How an attribute's value must be read as JavaScript, if at all.

    ``@event``/``v-on:``/bare ``on*`` are handlers — scan them with the FULL JS copy
    rules (showToast/confirm/alert literals, key literals, template messages,
    sentence-shaped literals). ``v-if``/``v-show``/``v-model`` expressions are
    boilerplate comparisons, so only the JS CALL scanners and the sentence heuristic
    apply — never the generic literal rule (``'login'``/``'admin'`` are not copy).

    Item 5's confirm()/showToast() literals live in handlers, which is why a handler
    must never be a dead zone. Returns "full", "calls" or "".
    """
    if name.startswith("@") or name.startswith("v-on:") or BARE_ON.match(name):
        return "full"
    if name in ("v-if", "v-show") or name.startswith("v-model"):
        return "calls"
    return ""


def scan_html(raw: str) -> list[Finding]:
    parser = _HtmlScan()
    parser.feed(raw)
    findings: list[Finding] = []
    for line, data in parser.texts:
        rest = INTERP.sub("", data).strip()
        if has_letter(rest):
            findings.append(Finding(
                "static/index.html", line, "hardcoded", "INFO", "text",
                "hardcoded text node: " + " ".join(rest.split())[:80], rest))
        for match in INTERP.finditer(data):
            literal = expression_copy(match.group(1))
            if literal is not None:
                findings.append(Finding(
                    "static/index.html", line, "hardcoded", "INFO", "expr",
                    f"hardcoded copy in a template expression: {literal[:60]!r}",
                    literal.strip()))
    for line, tag, attrs_map in parser.attrs:
        for name, value in attrs_map.items():
            if value is None:
                continue
            mode = js_attribute_mode(name)
            if mode:
                # The handler/expression IS JavaScript: run the JS scanners on it.
                # A handler with no literal copy (``@click="toggleLang"``) stays clean.
                findings.extend(scan_js_copy("static/index.html", value,
                                             line_offset=line - 1,
                                             calls_only=(mode == "calls")))
                continue
            bound = name.startswith(":") or name.startswith("v-bind:")
            base = name[1:] if name.startswith(":") else name.removeprefix("v-bind:")
            if bound:
                # A Vue binding is a JS expression: read the copy-bearing ones only.
                is_copy_target = base in EXPR_COPY_ATTRS or (
                    base == "value" and tag == "input"
                    and (attrs_map.get("type") or "").lower() in
                    ("submit", "reset", "button", "image"))
                if not is_copy_target:
                    continue
                literal = expression_copy(value)
                if literal is not None:
                    findings.append(Finding(
                        "static/index.html", line, "hardcoded", "INFO", "attr-expr",
                        f"hardcoded copy in bound {base}: {literal[:60]!r}",
                        literal.strip()))
            elif is_copy_attribute(tag, name, attrs_map) and value.strip() \
                    and has_letter(value):
                findings.append(Finding(
                    "static/index.html", line, "hardcoded", "INFO", "attr",
                    f"literal {name}={value.strip()[:50]!r}", value.strip()))
    # Inline <script> bodies are a dead zone for the text/attribute scans; run the
    # SAME JS scanners over them (absolute lines), so showToast('…') cannot hide.
    for start_line, body in parser.scripts:
        findings.extend(scan_js_copy("static/index.html", body,
                                     line_offset=start_line - 1))
    return findings


def scan_js_copy(rel: str, raw: str, line_offset: int = 0,
                 calls_only: bool = False) -> list[Finding]:
    """Copy scan for one JS source (a static file, an inline <script>, a handler).

    `line_offset` maps a body-relative line to the containing file's line (inline
    scripts/handlers), so findings always point at the real file:line. `calls_only`
    keeps the showToast/confirm/alert and sentence rules but drops the key-literal and
    template-message rules (used for `v-if`/`v-show`/`v-model` boilerplate).
    """
    clean, tokens = scan_js(raw)
    findings: list[Finding] = []
    seen: set[tuple[str, int]] = set()

    def add(kind: str, line: int, message: str, literal: str) -> None:
        if (kind, line) in seen:
            return
        seen.add((kind, line))
        findings.append(Finding(rel, line, "hardcoded", "INFO", kind, message, literal))

    for kind, offset, content in tokens:
        if not has_letter(content):
            continue
        line = line_of(raw, offset) + line_offset
        before = clean[max(0, offset - 80):offset]
        call = CALLS.search(before)
        if call:
            add("call", line, f"literal {call.group(1)} argument: {content[:60]!r}", content)
            continue
        if not calls_only:
            key = KEY_LITERAL.search(before)
            if key:
                add("key", line, f"literal {key.group(1)}: {content[:60]!r}", content)
                continue
            if kind == "tpl":
                text = PLACEHOLDER.sub(" ", content)
                if len(text.split()) >= 3:
                    add("template", line,
                        f"template-literal message: {' '.join(text.split())[:60]!r}",
                        text.strip())
                continue
        words = content.split()
        if len(words) >= 3 and content[:1].isupper() and content.rstrip()[-1:] in ".!?:":
            add("sentence", line, f"sentence-shaped literal: {content[:60]!r}", content)
    return findings


def pass_hardcoded(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    html = root / "static" / "index.html"
    if html.is_file():
        findings.extend(scan_html(html.read_text(encoding="utf-8")))
    for path in sorted((root / "static").glob("*.js")):
        if path.name == "i18n.messages.js":
            continue
        findings.extend(scan_js_copy(f"static/{path.name}", path.read_text(encoding="utf-8")))
    return findings


# ---------------------------------------------------------------------------
# allowlist + pass 5 — the ratchet
# ---------------------------------------------------------------------------

def read_config_lines(path: Path, display: str) -> tuple[list[tuple[int, str]], Finding | None]:
    if not path.is_file():
        return [], Finding(display, 0, "ratchet", "ERROR", "missing",
                           f"{display} is missing (part of the i18n contract)")
    lines = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if stripped and not COMMENT_LINE.match(raw):
            lines.append((number, stripped))
    return lines, None


_ESCAPES = {"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "#": "\\#"}
_UNESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", "#": "#"}


def escape_literal(text: str) -> str:
    """Make a copy literal safe on ONE allowlist line (\\, newline, tab, '#').

    The entry key is content-addressed — ``<path>:<kind>:<escaped literal>`` — so a
    line move never invalidates it; only a change to the copy itself does.
    """
    return "".join(_ESCAPES.get(ch, ch) for ch in text)


def unescape_literal(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append(_UNESCAPES.get(nxt, "\\" + nxt))
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _split_allowlist_reason(line: str) -> tuple[str, str]:
    """Split at the first BARE '#' (an escaped '\\#' belongs to the literal)."""
    for i, ch in enumerate(line):
        if ch == "#" and (i == 0 or line[i - 1] != "\\"):
            return line[:i], line[i + 1:]
    return line, ""


def read_allowlist(path: Path) -> tuple[dict[tuple[str, str, str], int], list[Finding]]:
    """(allowlisted key -> entry line, config findings), keyed by content."""
    entries, problem = read_config_lines(path, DISPLAY_ALLOWLIST)
    findings = [problem] if problem else []
    keys: dict[tuple[str, str, str], int] = {}
    ordered: list[tuple[tuple[str, str, str], int]] = []
    for number, line in entries:
        body, reason = _split_allowlist_reason(line)
        body = body.strip()
        if not reason.strip():
            findings.append(Finding(DISPLAY_ALLOWLIST, number, "ratchet", "ERROR",
                                    "allowlist", "every entry needs a '# reason'"))
            continue
        parts = body.split(":", 2)
        if len(parts) != 3 or not parts[0] or parts[1] not in ALLOWLIST_KINDS \
                or not parts[2]:
            findings.append(Finding(DISPLAY_ALLOWLIST, number, "ratchet", "ERROR",
                                    "allowlist", f"malformed entry {body!r} (expected "
                                    f"<path>:<kind>:<escaped literal>)"))
            continue
        key = (parts[0], parts[1], unescape_literal(parts[2]))
        keys[key] = number
        ordered.append((key, number))
    for (previous, _pline), (current, line_no) in zip(ordered, ordered[1:]):
        if current < previous:
            findings.append(Finding(DISPLAY_ALLOWLIST, line_no, "ratchet", "ERROR",
                                    "allowlist", "entries must be sorted: "
                                    f"{escape_literal(current[2])} comes after "
                                    f"{escape_literal(previous[2])}"))
            break
    return keys, findings


def pass_ratchet(root: Path, hardcoded: list[Finding], rows: list[_i18n.Row]) -> list[Finding]:
    findings: list[Finding] = []
    allow_keys, allow_findings = read_allowlist(root / "i18n" / "copy-allowlist.txt")
    findings.extend(allow_findings)

    migrated_path = root / "i18n" / "migrated.txt"
    strict_all = not migrated_path.exists()
    namespaces: dict[str, int] = {}
    if not strict_all:
        entries, problem = read_config_lines(migrated_path, DISPLAY_MIGRATED)
        if problem:
            findings.append(problem)
        ids = {row.id for row in rows}
        for number, entry in entries:
            if not NS_RE.match(entry):
                findings.append(Finding(DISPLAY_MIGRATED, number, "ratchet", "ERROR",
                                        "namespace",
                                        f"malformed namespace {entry!r} "
                                        f"(expected e.g. 'app.')"))
                continue
            namespaces[entry] = number
            if not any(rid.startswith(entry) for rid in ids):
                findings.append(Finding(DISPLAY_MIGRATED, number, "ratchet", "ERROR",
                                        "stale-namespace",
                                        f"namespace {entry!r} has no CSV row — remove it "
                                        f"(a migrated namespace cannot be empty)"))

    # Every CSV cell -> the namespace of the row that owns it. A hardcoded literal
    # that matches a migrated row is exactly the regression this pass exists for.
    owners: dict[str, str] = {}
    for row in rows:
        owners.setdefault(row.en.strip(), id_prefix(row.id))
        owners.setdefault(row.da.strip(), id_prefix(row.id))

    used: set[tuple[str, str, str]] = set()
    for finding in hardcoded:
        key = (finding.file, finding.kind, finding.literal)
        if key in allow_keys:
            used.add(key)
            continue
        namespace = owners.get(finding.literal.strip(), "")
        strict = strict_all or (namespace != "" and namespace in namespaces)
        findings.append(Finding(finding.file, finding.line, "hardcoded",
                                "ERROR" if strict else "INFO", finding.kind,
                                finding.message, finding.literal))
    for key, entry_line in sorted(allow_keys.items()):
        if key in used:
            continue
        path, kind, literal = key
        findings.append(Finding(DISPLAY_ALLOWLIST, entry_line, "ratchet", "ERROR",
                                "allowlist",
                                f"unused allowlist entry "
                                f"{path}:{kind}:{escape_literal(literal)} — delete it "
                                f"(no finding has that content; the list may only shrink)"))
    return findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def emit(findings: list[Finding], args) -> None:
    errors = sum(1 for f in findings if f.severity == "ERROR")
    info = sum(1 for f in findings if f.severity == "INFO")
    if args.json:
        print(json.dumps({"findings": [f.as_dict() for f in findings],
                          "summary": {"errors": errors, "info": info}},
                         ensure_ascii=False, indent=2))
    elif not args.quiet:
        for finding in findings:
            print(finding.text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="i18n completeness checker: CSV ↔ source ↔ dead rows ↔ hardcoded "
                    "copy ↔ ratchet (read-only).")
    parser.add_argument("--quiet", action="store_true", help="print only the summary")
    parser.add_argument("--json", action="store_true", help="print findings as JSON")
    parser.add_argument("--root", default=str(SCRIPTS_DIR.parent),
                        help="tree to check (default: this checkout)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not (root / "i18n" / "strings.csv").is_file():
        print(f"i18n check: cannot run — {root / 'i18n' / 'strings.csv'} is missing",
              file=sys.stderr)
        return 2

    rows, findings = pass_csv(root / "i18n" / "strings.csv")
    if any(f.pass_name == "csv" for f in findings):
        # The source does not parse: coverage verdicts would be meaningless.
        emit(findings, args)
        print("i18n check: cannot run — fix i18n/strings.csv first", file=sys.stderr)
        return 2

    sources, source_problems = production_sources(root)
    findings.extend(source_problems)
    ids = {row.id for row in rows}
    ref_findings, production_refs = pass_reference(sources, reference_names(ids))
    findings.extend(ref_findings)

    # Tests are a reference source for dead rows — but only their REAL strings:
    # comments and docstrings must never keep a dead row alive.
    names = reference_names(ids)
    test_refs: set[str] = set()
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        for path in sorted(tests_dir.glob("*.py")):
            test_refs |= python_test_references(path.read_text(encoding="utf-8"), names)
        for path in sorted((tests_dir / "js").glob("*.mjs")):
            raw_test = path.read_text(encoding="utf-8")
            test_refs |= ids_in_test_text(scan_js(raw_test, blank_strings=False)[0], names)
    findings.extend(pass_dead_rows(rows, production_refs, test_refs))

    hardcoded = pass_hardcoded(root)
    findings.extend(pass_ratchet(root, hardcoded, rows))

    findings.sort(key=lambda f: (f.file, f.line, f.kind, f.pass_name, f.message))
    emit(findings, args)
    errors = sum(1 for f in findings if f.severity == "ERROR")
    info = sum(1 for f in findings if f.severity == "INFO")
    if not args.json:
        print(f"i18n check: {errors} error(s), {info} info(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
