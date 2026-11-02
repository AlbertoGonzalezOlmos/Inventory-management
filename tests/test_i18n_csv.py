"""The copy source: committed form + every validator rule (item 2).

`i18n/strings.csv` is the only hand-edited copy in the app, and the artifact the
browser loads is generated from it, so the CSV's *shape* is a contract. Two layers are
tested here:

* the **committed** file is canonical — BOM, LF, one trailing newline, sorted ids,
  minimal quoting (Excel writes CRLF and drops the BOM, so the loader tolerates that;
  the repo copy must not drift);
* the **validator** rejects each way the file can go wrong, with the line number and a
  message that names the fix. Every case below is a real editing mistake.

The euro glyph is built with ``chr`` so this file never contains the literal the token
sweep forbids (tests/test_money_dkk.py).
"""

import importlib.util
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(REPO_ROOT, "i18n", "strings.csv")
INDEX_HTML = os.path.join(REPO_ROOT, "static", "index.html")


def _load_i18n():
    path = os.path.join(REPO_ROOT, "scripts", "_i18n.py")
    spec = importlib.util.spec_from_file_location("_i18n_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


i18n = _load_i18n()

HEADER = "id,en,da\n"
OLD_SYMBOL = chr(0x20AC)   # the currency glyph the app no longer renders


def _write(tmp_path, body: str, *, bom: bool = True, crlf: bool = False):
    """A temp CSV. `body` is the data rows only (header is prepended)."""
    text = HEADER + body
    data = text.encode("utf-8")
    if crlf:
        data = data.replace(b"\n", b"\r\n")
    if bom:
        data = b"\xef\xbb\xbf" + data
    path = tmp_path / "strings.csv"
    path.write_bytes(data)
    return path


def _problems(tmp_path, body, **kwargs):
    rows, problems = i18n.read_rows(_write(tmp_path, body, **kwargs))
    return problems


# --- the committed file -----------------------------------------------------


def test_committed_csv_is_canonical():
    raw = open(CSV_PATH, "rb").read()
    assert raw.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM (Excel keeps æøå with it)"
    assert b"\r" not in raw, "LF line endings"
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n"), "exactly one trailing LF"
    rows, problems = i18n.read_rows(CSV_PATH)
    assert problems == []
    assert [row.id for row in rows] == sorted(row.id for row in rows), "ids sorted"
    assert raw == i18n.canonical_csv_bytes(rows), "canonical form (minimal quoting)"


SEED_IDS = (
    "app.title",
    "catalogue.search_placeholder",
    "common.item_count.one",
    "common.item_count.other",
    "lang.switch_to",
    "money.code",
)


def test_seed_rows_are_still_present():
    """The item-2/3 seed rows survive every migration slice (a SUBSET pin now —
    the slices legitimately add rows, but a seed cannot be renamed or dropped
    without this going red)."""
    rows, _ = i18n.read_rows(CSV_PATH)
    by_id = {row.id: row for row in rows}
    for rid in SEED_IDS:
        assert rid in by_id, f"seed row {rid!r} disappeared"
    assert by_id["money.code"].en == by_id["money.code"].da == "DKK"
    assert OLD_SYMBOL not in "".join(row.en + row.da for row in rows)


def test_the_static_title_is_the_language_neutral_brand():
    """The pre-JS <title> is the shop's proper noun, and it IS the CSV row.

    Decision 13: a static title cannot be language-specific (the tab must not
    print the wrong language before `applyLang` runs), so it carries `app.brand`
    — a proper noun, identical in both columns — while `app.title` (brand +
    page name, per language) is what `applyLang` writes a moment later.
    """
    rows, _ = i18n.read_rows(CSV_PATH)
    by_id = {row.id: row for row in rows}
    assert by_id["app.brand"].en == by_id["app.brand"].da == "Hertha's Butik"
    assert by_id["app.title"].en == "Hertha's Butik — Shop Catalogue"
    assert by_id["app.title"].da == "Hertha's Butik — Butikskatalog"
    index = open(INDEX_HTML, encoding="utf-8").read()
    assert f"<title>{by_id['app.brand'].en}</title>" in index, (
        "the static title must be the brand row, not a second copy of it")


def test_repair_hint_fragments_rebuild_the_sentence_in_both_languages():
    """The two-key exception, pinned as COPY: joining each language's
    fragments around the command must rebuild the original one-sentence
    hint. This is the half the `<code>` pin cannot see — the markup could
    keep the element while a translation drifts — and it keeps both rows
    placeholder-free (the `<code>` element, not a {command} token, carries
    the machine text)."""
    rows, _ = i18n.read_rows(CSV_PATH)
    by_id = {row.id: row for row in rows}
    command = "scanner_daemon.py --register"
    for lang, expected in (
        ("en", "To re-pair: DELETE the terminal (staff or loopback), re-run "
               f"{command}, then click the pill."),
        ("da", "For at parre igen: SLET terminalen (personale eller loopback), kør igen "
               f"{command}, og klik derefter på statusknappen."),
    ):
        for rid in ("scanner.repair_prefix", "scanner.repair_suffix"):
            assert i18n.placeholders(by_id[rid].cell(lang)) == [], (
                f"{rid} must stay placeholder-free — the command is <code>, "
                "never a {command} token")
        rebuilt = (by_id["scanner.repair_prefix"].cell(lang) + " " + command
                   + by_id["scanner.repair_suffix"].cell(lang))
        assert rebuilt == expected, f"{lang} fragments drifted: {rebuilt!r}"


def test_loader_tolerates_bom_less_and_crlf_input(tmp_path):
    """Excel re-saves are CRLF and may drop the BOM; the artifact must not change."""
    committed, _ = i18n.read_rows(CSV_PATH)
    path = tmp_path / "strings.csv"
    path.write_bytes(open(CSV_PATH, "rb").read().replace(b"\xef\xbb\xbf", b"").replace(b"\n", b"\r\n"))
    rows, problems = i18n.read_rows(path)
    assert problems == []
    assert rows == committed
    assert i18n.canonical_csv_bytes(rows) == i18n.canonical_csv_bytes(committed)


def test_non_utf8_input_is_rejected(tmp_path):
    # The build must never guess an encoding: mojibake copy is worse than a
    # refused build.
    path = tmp_path / "strings.csv"
    path.write_bytes(b"\xef\xbb\xbf" + b"id,en,da\napp.title,A\xff,B\n")
    rows, problems = i18n.read_rows(path)
    assert rows == []
    assert problems == ["i18n/strings.csv: expected UTF-8 (with optional BOM)"]


def test_unreadable_file_is_rejected(tmp_path):
    # Any OSError becomes a reported problem, never a traceback out of the
    # build (a directory stands in for an unreadable file, portably).
    rows, problems = i18n.read_rows(tmp_path)
    assert rows == []
    assert problems and problems[0].startswith("i18n/strings.csv: cannot read: ")


# test_no_ui_migration_has_started was DELETED in item 3 (acceptance note). Its
# one-stage replacement (an "exactly one binding" ratchet) was deleted by item 5's
# slice A; the migration slices are exactly the edits it would have forbidden.


# --- validator rules (one case per rule) ------------------------------------
# Every fixture keeps an untouched `money.code` row: it is required, and a mutated
# `en` cell there would also trip the "identical in both languages" rule, which
# would make each exact-message assertion test two rules at once.

MONEY = "money.code,DKK,DKK\n"


def test_header_must_be_exactly_id_en_da(tmp_path):
    path = tmp_path / "strings.csv"
    path.write_bytes(b"id,en\nmoney.code,DKK\n")
    _, problems = i18n.read_rows(path)
    assert problems == [
        "i18n/strings.csv:1: header must be exactly 'id,en,da' (got 'id,en')"]


def test_wrong_column_count(tmp_path):
    problems = _problems(tmp_path, "app.title,A\n" + MONEY)
    assert problems == ["i18n/strings.csv:2: expected 3 columns, got 2"]


def test_malformed_quoting_is_a_line_addressed_error(tmp_path):
    # strict=True: the csv module refuses an undoubled quote inside a quoted
    # field instead of silently dropping it from the copy. The problem names
    # the physical line, like every other rule.
    problems = _problems(tmp_path, 'app.title,"He said "Hi"",B\n' + MONEY)
    assert problems == [
        "i18n/strings.csv:2: CSV parse error: ',' expected after '\"'"]


def test_blank_row(tmp_path):
    problems = _problems(tmp_path, "\n" + MONEY)
    assert problems == ["i18n/strings.csv:2: blank row"]


def test_invalid_id(tmp_path):
    problems = _problems(tmp_path, "App.Title,A,B\n" + MONEY)
    assert problems == [
        "i18n/strings.csv:2: invalid id 'App.Title' "
        "(lowercase, dot-separated, e.g. 'auth.greeting')"]


def test_duplicate_id_names_the_first_line(tmp_path):
    problems = _problems(tmp_path, "app.title,A,B\napp.title,A,B\n" + MONEY)
    assert problems == ["i18n/strings.csv:3: duplicate id 'app.title' (first seen on line 2)"]


def test_empty_cells(tmp_path):
    problems = _problems(tmp_path, "app.title,,B\n" + MONEY)
    assert problems == ["i18n/strings.csv:2: empty 'en' cell for id 'app.title'"]
    problems = _problems(tmp_path, 'app.title,"A",\n' + MONEY)
    assert problems == ["i18n/strings.csv:2: empty 'da' cell for id 'app.title'"]


def test_trailing_whitespace(tmp_path):
    problems = _problems(tmp_path, 'app.title,"A ","B"\n' + MONEY)
    assert problems == ["i18n/strings.csv:2: trailing whitespace in 'en' for id 'app.title'"]


def test_leading_whitespace(tmp_path):
    # The mirror of the trailing rule: a stray leading space ships copy that
    # renders with an invisible offset in every language.
    problems = _problems(tmp_path, 'app.title," A","B"\n' + MONEY)
    assert problems == ["i18n/strings.csv:2: leading whitespace in 'en' for id 'app.title'"]


def test_whitespace_only_cell_is_empty(tmp_path):
    # Symmetry: a spaces-only cell gets ONE message — the clearer 'empty'
    # diagnosis — not a leading+trailing pair.
    problems = _problems(tmp_path, 'app.title,"  ",B\n' + MONEY)
    assert problems == ["i18n/strings.csv:2: empty 'en' cell for id 'app.title'"]


def test_embedded_newline(tmp_path):
    # The record spans physical lines 2-3; the problem cites the record's
    # STARTING line (2) — where the owner's fix goes, not the line the parser
    # finished on.
    problems = _problems(tmp_path, 'app.title,"A\nok",B\n' + MONEY)
    assert problems == ["i18n/strings.csv:2: embedded newline in 'en' for id 'app.title'"]


def test_markup_characters(tmp_path):
    problems = _problems(tmp_path, 'app.title,"<b>A</b>",B\n' + MONEY)
    assert problems == [
        "i18n/strings.csv:2: markup character in 'en' for id 'app.title' "
        "(copy is text, never HTML)"]


def test_euro_sign(tmp_path):
    problems = _problems(tmp_path, f'app.title,{OLD_SYMBOL},B\n' + MONEY)
    assert problems == [
        "i18n/strings.csv:2: euro sign in 'en' for id 'app.title' (the app renders DKK)"]


@pytest.mark.parametrize("ch, code", [
    ("\t", "0009"), ("\x00", "0000"), ("\x1b", "001B"), ("\x7f", "007F"),
])
def test_control_characters(tmp_path, ch, code):
    # Tab/NUL/ESC/DEL are not copy: they would flow verbatim into the served
    # JS string. LF as a row break and the file's leading BOM are the only
    # control escapes the format allows.
    problems = _problems(tmp_path, f'app.title,"A{ch}B",B\n' + MONEY)
    assert problems == [
        f"i18n/strings.csv:2: control character U+{code} in 'en' for id 'app.title'"]


def test_bom_inside_a_cell_is_rejected(tmp_path):
    problems = _problems(tmp_path, 'app.title,"A\ufeff",B\n' + MONEY)
    assert problems == [
        "i18n/strings.csv:2: unexpected BOM in 'en' for id 'app.title' "
        "(only the leading file BOM is allowed)"]


def test_quoted_comma_and_double_quote_round_trip(tmp_path):
    path = _write(tmp_path, 'app.title,"He said ""Hi, friend""",B\n' + MONEY)
    rows, problems = i18n.read_rows(path)
    assert problems == []
    assert rows[0].en == 'He said "Hi, friend"'
    assert b'"He said ""Hi, friend"""' in i18n.canonical_csv_bytes(rows)


@pytest.mark.parametrize("cell", ["{name", "name}", "{}", "{ n }", "{1bad}"])
def test_unbalanced_or_malformed_braces(tmp_path, cell):
    quoted = '"' + cell.replace('"', '""') + '"'
    problems = _problems(tmp_path, f'app.title,{quoted},B\n' + MONEY)
    assert any("unbalanced or malformed braces in 'en' for id 'app.title'" in p
               for p in problems), problems


def test_placeholder_mismatch_names_both_sets(tmp_path):
    problems = _problems(tmp_path, 'app.title,"Hi {name}","Hej {navn}"\n' + MONEY)
    assert problems == [
        "i18n/strings.csv:2: placeholder mismatch for id 'app.title': "
        "en={name} da={navn}"]


def test_placeholder_repeat_counts_matter(tmp_path):
    # Multiset, not set: a placeholder repeated in one language only would
    # silently render a literal '{n}' where the other language interpolates.
    problems = _problems(tmp_path, 'app.title,"{n} {n} items","{n} varer"\n' + MONEY)
    assert problems == [
        "i18n/strings.csv:2: placeholder mismatch for id 'app.title': "
        "en={n, n} da={n}"]


def test_plural_sibling_must_exist(tmp_path):
    problems = _problems(tmp_path, "app.title,A,B\ncommon.n.one,{n} one,{n} en\n" + MONEY)
    assert problems == ["i18n/strings.csv:3: id 'common.n.one' has no 'common.n.other' sibling"]
    problems = _problems(tmp_path, "app.title,A,B\ncommon.n.other,{n} many,{n} mange\n" + MONEY)
    assert problems == ["i18n/strings.csv:3: id 'common.n.other' has no 'common.n.one' sibling"]


def test_plural_siblings_must_agree_on_placeholders(tmp_path):
    problems = _problems(
        tmp_path,
        "app.title,A,B\n"
        "common.n.one,{n} one,{n} en\n"
        "common.n.other,{n} of {m}, {n} af {m}\n" + MONEY)
    assert any("plural siblings disagree on placeholders in 'en'" in p for p in problems), problems
    assert any("plural siblings disagree on placeholders in 'da'" in p for p in problems), problems


def test_plural_sibling_repeat_counts_matter(tmp_path):
    # The multiset rule again, across the pair: one '{n}' versus two is a
    # disagreement even though the name sets are equal.
    problems = _problems(
        tmp_path,
        "app.title,A,B\n"
        "common.n.one,{n} item,{n} vare\n"
        "common.n.other,{n} items,{n} {n} varer\n" + MONEY)
    assert problems == [
        "i18n/strings.csv:4: placeholder mismatch for id 'common.n.other': "
        "en={n} da={n, n}",
        "i18n/strings.csv:3: plural siblings disagree on placeholders in 'da': "
        "'common.n.one'={n} 'common.n.other'={n, n}",
        "i18n/strings.csv:4: plural siblings disagree on placeholders in 'da': "
        "'common.n.other'={n, n} 'common.n.one'={n}"]


def test_rows_must_be_sorted(tmp_path):
    problems = _problems(tmp_path, "money.code,DKK,DKK\napp.title,A,B\n")
    assert problems == [
        "i18n/strings.csv:3: id 'app.title' is out of order (previous id 'money.code')"]


def test_money_code_is_required(tmp_path):
    problems = _problems(tmp_path, "app.title,A,B\n")
    assert problems == ["i18n/strings.csv: missing required row 'money.code'"]


def test_money_code_must_be_identical(tmp_path):
    # A mismatch breaks two invariants at once (the columns differ AND the
    # value is not the supported currency); report-all names both.
    problems = _problems(tmp_path, "app.title,A,B\nmoney.code,DKK,kr.\n")
    assert problems == [
        "i18n/strings.csv:3: 'money.code' must be identical in en and da (got 'DKK' / 'kr.')",
        "i18n/strings.csv:3: 'money.code' must be 'DKK' in en and da (got 'DKK' / 'kr.')"]


def test_money_code_value_must_be_dkk(tmp_path):
    # Identical-but-wrong values are still rejected: the money formatter's
    # fixed shape (and the README's DKK claim) hangs on this one token. A
    # currency change edits this rule and the CSV in the same commit.
    problems = _problems(tmp_path, "app.title,A,B\nmoney.code,USD,USD\n")
    assert problems == [
        "i18n/strings.csv:3: 'money.code' must be 'DKK' in en and da (got 'USD' / 'USD')"]


def test_every_problem_is_reported_not_just_the_first(tmp_path):
    problems = _problems(
        tmp_path,
        'app.title,,B\napp.title,A,B\n' + MONEY + 'zz.bad,"x\ny","x"\n')
    assert len(problems) >= 3, problems
    assert any("empty 'en'" in p for p in problems)
    assert any("duplicate id" in p for p in problems)
    assert any("embedded newline" in p for p in problems)


def test_canonical_bytes_reproduce_the_committed_file():
    rows, _ = i18n.read_rows(CSV_PATH)
    assert i18n.canonical_csv_bytes(rows) == open(CSV_PATH, "rb").read()
    assert len(i18n.sha256_of(i18n.canonical_csv_bytes(rows))) == 64


def test_no_plural_suffix_idiom_anywhere_in_the_copy():
    """The glossary forbids '(s)' suffixes: a count is a .one/.other pair.

    Pinned over the whole CSV because the idiom is a copy rule, not a view
    rule — slice B removed the receipt's last two ('item(s)' line count,
    'scanned item(s)') and a new row must not smuggle it back.
    """
    rows, _ = i18n.read_rows(CSV_PATH)
    offenders = [f"{row.id} ({lang})" for row in rows for lang in i18n.LANGUAGES
                 if "(s)" in row.cell(lang)]
    assert offenders == [], f"'(s)' idiom found in: {offenders}"


def test_money_column_header_states_the_currency():
    """Item-1 decision 4: a money COLUMN HEADER names the currency in both
    languages; a form-field label uses the plain `common.price` row.

    `common.price_dkk` is the header row; keeping it separate is what lets
    slice E's form labels bind `common.price` ('Pris') without silently
    changing the header's meaning.
    """
    rows, _ = i18n.read_rows(CSV_PATH)
    by_id = {row.id: row for row in rows}
    assert by_id["common.price_dkk"].en == "Price (DKK)"
    assert by_id["common.price_dkk"].da == "Pris (DKK)"
    assert by_id["common.price"].en == "Price"
    assert by_id["common.price"].da == "Pris"


def test_calendar_date_labels_are_csv_rows_and_compact_in_danish():
    """Slice C: the calendar's weekday and month labels are CSV rows, not Intl
    output. The Danish cells are the glossary shorts so the 7-column header
    (and the month title) stay compact; the English month cells keep the long
    names the UI shipped with, so the English render is unchanged."""
    rows, _ = i18n.read_rows(CSV_PATH)
    by_id = {row.id: row for row in rows}
    ordered = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    assert [by_id[f"date.dow.{key}"].en for key in ordered] == [
        "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert [by_id[f"date.dow.{key}"].da for key in ordered] == [
        "ma", "ti", "on", "to", "fr", "lø", "sø"]
    assert all(len(by_id[f"date.dow.{key}"].da) <= 2 for key in ordered), \
        "the Danish weekday labels must fit the 7-column grid"
    assert sorted(rid for rid in by_id if rid.startswith("date.month.")) == \
        sorted(f"date.month.{n}" for n in range(1, 13))
    for n in range(1, 13):
        assert len(by_id[f"date.month.{n}"].da) <= 3, "Danish month labels stay short"
    assert by_id["date.month.3"].en == "March"
    assert by_id["date.month.3"].da == "mar"
    # Prose month names: the sentence form ("Brugt i marts 2026"), never a
    # grid short. English keeps the long names the UI shipped with.
    assert [by_id[f"date.month_long.{n}"].en for n in range(1, 13)] == [
        "January", "February", "March", "April", "May", "June", "July",
        "August", "September", "October", "November", "December"]
    assert [by_id[f"date.month_long.{n}"].da for n in range(1, 13)] == [
        "januar", "februar", "marts", "april", "maj", "juni", "juli",
        "august", "september", "oktober", "november", "december"]
