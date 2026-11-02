"""Loader + validator for the copy source `i18n/strings.csv` (item 2 of the roadmap).

Why this module exists, and why it is strict:

* The CSV is the **only** hand-edited copy source; `scripts/build_i18n.py` compiles it
  into the served `static/i18n.messages.js`. A malformed row must therefore fail the
  build, never render — and the owner edits this file in Excel, so the *reader*
  tolerates a BOM-less or CRLF file while the **committed** file is pinned canonical
  by `tests/test_i18n_csv.py` (the same split the repo uses for source vs. artifact).
* Validation **reports every problem**, in file order, as
  ``i18n/strings.csv:<line>: <message>`` — one build tells the owner everything that is
  wrong, instead of one error per round trip.
* Nothing here writes: this module reads, validates and renders bytes. Only
  `scripts/build_i18n.py --write` touches the artifact, and nothing but the owner
  touches the CSV (rows are never silently re-sorted: order is an error).

The complete rule list (every problem is reported, in file order, as
``i18n/strings.csv:<line>: <message>`` — and ``<line>`` is the physical line where
the RECORD starts, so a quoted multi-line cell points at its opening line, where
the fix goes):

read-level (the file itself)
    unreadable file · not UTF-8 · empty file · header exactly ``id,en,da`` ·
    blank row · exactly 3 columns per record · strict CSV quoting (an undoubled
    or stray quote is a line-addressed parse error, never silently absorbed)
per row
    id grammar ``lower.dot.snake`` with at least one dot · no duplicate id ·
    ids in sorted order (never silently re-sorted) · no empty cell (a
    whitespace-only cell is empty) · no leading or trailing whitespace in a
    cell · no embedded newline · no control characters (C0/C1, tab, NUL, DEL —
    the row-break LF and the file's leading BOM are the only escapes) · no
    ``<``/``>`` markup · no euro sign · no BOM inside a cell · braces only as
    well-formed ``{token}``s
cross-row
    the same placeholder MULTISET in ``en`` and ``da`` (repeat counts included) ·
    plural pairs ``<base>.one``/``<base>.other`` both present and agreeing on the
    same placeholder multiset per language · ``money.code`` present, identical
    in ``en``/``da``, and exactly ``DKK`` — a currency change edits this rule
    and the CSV together (money never changes shape with the language)
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = REPO_ROOT / "i18n" / "strings.csv"
ARTIFACT_PATH = REPO_ROOT / "static" / "i18n.messages.js"

DISPLAY = "i18n/strings.csv"
HEADER = ("id", "en", "da")
LANGUAGES = ("en", "da")
ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")
# C0/C1 controls are not copy. LF is handled separately as an embedded row break;
# CR is normalized to LF while reading. Everything else (including tab/NUL) fails.
CONTROL_RE = re.compile(r"[\x00-\x09\x0b\x0c\x0e-\x1f\x7f-\x9f]")
OLD_SYMBOL = chr(0x20AC)   # the currency glyph the app no longer renders
BOM_CHAR = "\ufeff"             # legal only as the file's leading byte-order mark
MONEY_CODE_ID = "money.code"
BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class Row:
    line: int
    id: str
    en: str
    da: str

    def cell(self, lang: str) -> str:
        return self.en if lang == "en" else self.da


def placeholders(cell: str) -> list[str]:
    """Sorted placeholder names, retaining repeats for multiset comparison."""
    return sorted(PLACEHOLDER_RE.findall(cell))


def _brace_problem(cell: str) -> bool:
    """True when a brace survives after removing every well-formed {token}."""
    stripped = PLACEHOLDER_RE.sub("", cell)
    return "{" in stripped or "}" in stripped


def read_rows(path: Path | str = CSV_PATH) -> tuple[list[Row], list[str]]:
    """Parse + validate. Returns (rows, problems); problems is empty when clean."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [], [f"{DISPLAY}: cannot read: {exc.strerror or exc}"]
    try:
        text = raw.decode("utf-8-sig")  # BOM tolerated (and required in the repo copy)
    except UnicodeDecodeError:
        return [], [f"{DISPLAY}: expected UTF-8 (with optional BOM)"]
    # Excel saves CRLF; a lone CR is normalised too and then shows up as an
    # embedded newline if it was inside a quoted cell (rule below).
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows: list[Row] = []
    problems: list[str] = []
    try:
        try:
            header = next(reader)
        except StopIteration:
            return [], [f"{DISPLAY}:1: empty file (expected the header row 'id,en,da')"]
        if tuple(header) != HEADER:
            return [], [
                f"{DISPLAY}:1: header must be exactly 'id,en,da' "
                f"(got '{','.join(header)}')"
            ]

        # reader.line_num is the physical line at the END of a record. Keep the
        # START as well so an owner editing a quoted multi-line cell is sent to
        # the line where that record begins, not to the following record.
        start_line = reader.line_num + 1
        for cells in reader:
            line = start_line
            start_line = reader.line_num + 1
            if not cells:
                problems.append(f"{DISPLAY}:{line}: blank row")
                continue
            if len(cells) != 3:
                problems.append(f"{DISPLAY}:{line}: expected 3 columns, got {len(cells)}")
                continue
            rows.append(Row(line, cells[0], cells[1], cells[2]))
    except csv.Error as exc:
        return [], [f"{DISPLAY}:{reader.line_num}: CSV parse error: {exc}"]

    problems.extend(validate(rows))
    return rows, problems


def _fmt_set(names: list[str]) -> str:
    return "{" + ", ".join(names) + "}" if names else "{}"


def validate(rows: list[Row]) -> list[str]:
    """Every rule, in file order. Empty list means the copy is clean."""
    problems: list[str] = []
    seen: dict[str, int] = {}
    previous_id: str | None = None

    def add(line: int, message: str) -> None:
        problems.append(f"{DISPLAY}:{line}: {message}")

    for row in rows:
        rid = row.id
        if not ID_RE.match(rid):
            add(row.line, f"invalid id '{rid}' (lowercase, dot-separated, e.g. 'auth.greeting')")
        if rid in seen:
            add(row.line, f"duplicate id '{rid}' (first seen on line {seen[rid]})")
        else:
            seen[rid] = row.line
        if previous_id is not None and rid < previous_id:
            add(row.line, f"id '{rid}' is out of order (previous id '{previous_id}')")
        previous_id = rid

        for lang in LANGUAGES:
            cell = row.cell(lang)
            if not cell.strip():
                add(row.line, f"empty '{lang}' cell for id '{rid}'")
                continue
            if cell != cell.lstrip():
                add(row.line, f"leading whitespace in '{lang}' for id '{rid}'")
            if cell != cell.rstrip():
                add(row.line, f"trailing whitespace in '{lang}' for id '{rid}'")
            if "\n" in cell:
                add(row.line, f"embedded newline in '{lang}' for id '{rid}'")
            control = CONTROL_RE.search(cell)
            if control:
                add(row.line, f"control character U+{ord(control.group()):04X} "
                              f"in '{lang}' for id '{rid}'")
            if "<" in cell or ">" in cell:
                add(row.line, f"markup character in '{lang}' for id '{rid}' (copy is text, never HTML)")
            if OLD_SYMBOL in cell:
                add(row.line, f"euro sign in '{lang}' for id '{rid}' (the app renders DKK)")
            if BOM_CHAR in cell:
                add(row.line, f"unexpected BOM in '{lang}' for id '{rid}' "
                              "(only the leading file BOM is allowed)")
            if _brace_problem(cell):
                add(row.line, f"unbalanced or malformed braces in '{lang}' for id '{rid}'")

        en_set, da_set = placeholders(row.en), placeholders(row.da)
        if en_set != da_set:
            add(row.line,
                f"placeholder mismatch for id '{rid}': en={_fmt_set(en_set)} da={_fmt_set(da_set)}")

    # Plural pairs: every `.one` needs its `.other` and vice versa, and the siblings
    # must interpolate the same names (a translator dropping `{n}` is a rendering bug).
    for row in rows:
        if row.id.endswith(".one"):
            sibling = row.id[:-len(".one")] + ".other"
        elif row.id.endswith(".other"):
            sibling = row.id[:-len(".other")] + ".one"
        else:
            sibling = ""
        if not sibling:
            continue
        other = next((r for r in rows if r.id == sibling), None)
        if other is None:
            add(row.line, f"id '{row.id}' has no '{sibling}' sibling")
            continue
        for lang in LANGUAGES:
            if placeholders(row.cell(lang)) != placeholders(other.cell(lang)):
                add(row.line,
                    f"plural siblings disagree on placeholders in '{lang}': "
                    f"'{row.id}'={_fmt_set(placeholders(row.cell(lang)))} "
                    f"'{other.id}'={_fmt_set(placeholders(other.cell(lang)))}")

    money = next((r for r in rows if r.id == MONEY_CODE_ID), None)
    if money is None:
        problems.append(f"{DISPLAY}: missing required row '{MONEY_CODE_ID}'")
    else:
        if money.en != money.da:
            add(money.line,
                f"'{MONEY_CODE_ID}' must be identical in en and da "
                f"(got '{money.en}' / '{money.da}')")
        # DKK is the only supported currency for now. A future currency change
        # must deliberately update this rule and the CSV in the same commit.
        if money.en != "DKK" or money.da != "DKK":
            add(money.line,
                f"'{MONEY_CODE_ID}' must be 'DKK' in en and da "
                f"(got '{money.en}' / '{money.da}')")

    return problems


def quote_cell(cell: str) -> str:
    """RFC 4180 minimal quoting: quote only when the cell needs it."""
    if any(ch in cell for ch in ',"\n'):
        return '"' + cell.replace('"', '""') + '"'
    return cell


def canonical_csv_bytes(rows: list[Row]) -> bytes:
    """The exact bytes the artifact's provenance hash covers: BOM, LF, given order."""
    lines = [",".join(HEADER)]
    for row in rows:
        lines.append(",".join([row.id, quote_cell(row.en), quote_cell(row.da)]))
    return BOM + ("\n".join(lines) + "\n").encode("utf-8")


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
