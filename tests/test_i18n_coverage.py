"""Planted-defect tests for the i18n completeness checker (item 4).

`scripts/i18n_check.py` is itself a gate, so the gate is tested: the current tree must
pass, the INFO count (hardcoded copy not yet migrated) is pinned as a shrinking number,
and six planted defects — one per failure class the checker claims to catch — must each
make it exit 1 at the planted `file:line`.

The planted cases run against a COPY of the real tree (i18n/, static/, tests/), so the
real checkout is never mutated. Pass-4 cases that are not about the ratchet delete the
copy's `i18n/migrated.txt`: with no file, nothing is exempt and every hardcoded-copy
finding is fatal (exactly the item-7 mode). The checker is read-only; one test asserts
that too.
"""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "i18n_check.py"
CSV = REPO_ROOT / "i18n" / "strings.csv"
ARTIFACT = REPO_ROOT / "static" / "i18n.messages.js"

#: Hardcoded-copy findings (INFO) on the current tree. This number may only SHRINK:
#: when a migration slice lands, lower it in the same commit. It must never grow
#: silently — a new hardcoded string is a new finding, which the checker will show.
PINNED_INFO_COUNT = 112

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "fixtures", "vendor")


def _run(*args, root: Path | None = None):
    cmd = [sys.executable, str(SCRIPT)]
    if root is not None:
        cmd += ["--root", str(root)]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                          cwd=str(REPO_ROOT))


def _findings(proc) -> list[dict]:
    assert proc.stdout, proc.stderr
    return json.loads(proc.stdout)["findings"]


def _copy_tree(tmp_path: Path) -> Path:
    """A private copy of the parts the checker reads (never vendor/fixtures)."""
    dest = tmp_path / "tree"
    for rel in ("i18n", "static", "tests"):
        shutil.copytree(REPO_ROOT / rel, dest / rel, ignore=IGNORE)
    return dest


def _insert_before(path: Path, needle: str, inserted: str) -> int:
    """Insert one line before `needle`; return the inserted line number."""
    text = path.read_text(encoding="utf-8")
    assert text.count(needle) == 1, f"anchor is not unique: {needle!r}"
    at = text.index(needle)
    path.write_text(text[:at] + inserted + "\n" + text[at:], encoding="utf-8")
    return text.count("\n", 0, at) + 1


def _insert_allow_entry(path: Path, entry: str) -> int:
    """Insert an allowlist entry in SORTED position; return its 1-based line.

    The checker errors on an out-of-order entry, and the real list already holds
    a permanent command entry, so a plain append would add an unrelated ERROR to
    the run under test. Keys are compared as the checker compares them
    (path, kind, literal); these fixtures use no escapable characters.
    """
    key = tuple(entry.partition("  #")[0].split(":", 2))
    lines = path.read_text(encoding="utf-8").splitlines()
    at = len(lines)
    for i, line in enumerate(lines):
        body = line.strip()
        if not body or body.startswith("#"):
            continue
        if tuple(body.partition("  #")[0].split(":", 2)) > key:
            at = i
            break
    lines.insert(at, entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return at + 1


def _assert_planted(proc, file: str, line: int, *, kind: str | None = None,
                    severity: str = "ERROR", contains: str | None = None) -> dict:
    assert proc.returncode == 1, (proc.returncode, proc.stdout[-2000:], proc.stderr)
    matches = [f for f in _findings(proc)
               if f["file"] == file and f["line"] == line and f["severity"] == severity]
    if kind is not None:
        matches = [f for f in matches if f["kind"] == kind]
    if contains is not None:
        matches = [f for f in matches if contains in f["message"]]
    assert matches, (file, line, kind, contains, _findings(proc)[:10])
    return matches[0]


# --- the real tree ---------------------------------------------------------

def test_current_tree_passes_and_the_info_count_is_pinned():
    proc = _run("--json")
    assert proc.returncode == 0, (proc.stdout[-3000:], proc.stderr)
    summary = json.loads(proc.stdout)["summary"]
    assert summary["errors"] == 0
    assert summary["info"] == PINNED_INFO_COUNT, (
        "the hardcoded-copy count moved: a slice landed (lower PINNED_INFO_COUNT) "
        "or new copy bypassed the CSV (fix it)")
    text = _run()
    assert text.returncode == 0
    assert re.search(r"^i18n check: 0 error\(s\), %d info\(s\)$" % PINNED_INFO_COUNT,
                     text.stdout, re.M), text.stdout[-500:]


def test_json_and_text_report_the_same_findings():
    as_json, as_text = _run("--json"), _run()
    findings = _findings(as_json)
    lines = [line for line in as_text.stdout.splitlines()
             if re.match(r"^\S+:\d+: (ERROR|INFO) [a-z-]+: ", line)]
    assert len(lines) == len(findings)
    parsed = [re.match(r"^(\S+):(\d+): (ERROR|INFO) ([a-z-]+): (.*)$", line).groups()
              for line in lines]
    expected = [(f["file"], str(f["line"]), f["severity"], f["pass"], f["message"])
                for f in findings]
    assert parsed == expected


def _git_status() -> str:
    proc = subprocess.run(["git", "status", "--porcelain"], cwd=str(REPO_ROOT),
                          capture_output=True, text=True, check=True)
    return proc.stdout


def test_the_checker_never_writes():
    """A tree write of ANY kind is caught: file bytes+mtime AND `git status`.

    `git status --porcelain` sees tracked edits and NEW untracked files; the only
    thing it does not see is ignored bytecode (`__pycache__/` is in .gitignore), which
    is exactly the cache the interpreter creates when the checker imports `_i18n` —
    not a repository write.
    """
    watched = [CSV, ARTIFACT, REPO_ROOT / "static" / "index.html",
               REPO_ROOT / "static" / "app.js", REPO_ROOT / "static" / "i18n.js",
               REPO_ROOT / "static" / "style.css", REPO_ROOT / "i18n" / "migrated.txt",
               REPO_ROOT / "i18n" / "copy-allowlist.txt"]
    before = {p: (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
              for p in watched}
    status_before = _git_status()
    assert _run().returncode == 0
    assert _run("--quiet").returncode == 0
    assert _run("--json").returncode == 0
    after = {p: (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
             for p in watched}
    assert before == after
    assert _git_status() == status_before, "a checker run changed the working tree"


# --- six planted defects ---------------------------------------------------

def test_planted_literal_text_node(tmp_path):
    """(a) A hardcoded text node whose text matches a migrated row."""
    tree = _copy_tree(tmp_path)
    line = _insert_before(tree / "static" / "index.html", "</main>",
                          "  <p>HCRM — Shop Catalogue</p>")
    _assert_planted(_run("--json", root=tree), "static/index.html", line, kind="text")


def test_planted_missing_reference(tmp_path):
    """(b) A t('…') call whose id has no CSV row."""
    tree = _copy_tree(tmp_path)
    line = _insert_before(tree / "static" / "index.html", "</main>",
                          "  <p>{{ t('nope.missing') }}</p>")
    _assert_planted(_run("--json", root=tree), "static/index.html", line,
                    kind="unknown-id", contains="nope.missing")


def test_planted_dead_row(tmp_path):
    """(c) A CSV row nothing references."""
    tree = _copy_tree(tmp_path)
    path = tree / "i18n" / "strings.csv"
    dead = "zzz" + ".unused"          # built, so this test file is not itself a reference
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines + [f"{dead},ZZZ copy,ZZZ tekst"]) + "\n",
                    encoding="utf-8")
    _assert_planted(_run("--json", root=tree), "i18n/strings.csv", len(lines) + 1,
                    kind="dead-row", contains=dead)


def test_planted_literal_show_toast(tmp_path):
    """(d) A literal showToast argument (strict mode: nothing exempt)."""
    tree = _copy_tree(tmp_path)
    (tree / "i18n" / "migrated.txt").unlink()
    line = _insert_before(tree / "static" / "app.js", "      this.toast = msg;",
                          '      showToast("Saved the report");')
    _assert_planted(_run("--json", root=tree), "static/app.js", line, kind="call")


def test_planted_literal_placeholder(tmp_path):
    """(e) A literal placeholder attribute (strict mode: nothing exempt)."""
    tree = _copy_tree(tmp_path)
    (tree / "i18n" / "migrated.txt").unlink()
    line = _insert_before(tree / "static" / "index.html", "</main>",
                          '  <input placeholder="Type a name" />')
    _assert_planted(_run("--json", root=tree), "static/index.html", line, kind="attr")


def test_planted_stale_allowlist_entry(tmp_path):
    """(f) An allowlist entry whose content matches no finding — the list shrinks."""
    tree = _copy_tree(tmp_path)
    path = tree / "i18n" / "copy-allowlist.txt"
    entry_line = _insert_allow_entry(path, "static/app.js:call:Saved the report"
                                    "  # stale — nothing matches this")
    _assert_planted(_run("--json", root=tree), "i18n/copy-allowlist.txt", entry_line,
                    kind="allowlist", contains="unused")


def test_allowlist_entry_is_content_addressed(tmp_path):
    """An allowlisted literal survives a LINE MOVE and dies when its TEXT changes.

    The entries are ``<path>:<kind>:<literal>``, not line-keyed: the five migration
    slices shift hundreds of lines in index.html, and a line-keyed allowlist would
    rot on every one of them. Only a change to the copy itself may stale an entry.
    """
    tree = _copy_tree(tmp_path)
    html = tree / "static" / "index.html"
    _insert_before(html, "</main>", "  <p>Switch to Danish</p>")
    allow = tree / "i18n" / "copy-allowlist.txt"
    _insert_allow_entry(allow, "static/index.html:text:Switch to Danish"
                               "  # test token; removed by item 7")

    # Suppressed by content (and the entry is used, not stale).
    assert _run("--json", root=tree).returncode == 0

    # A line move must NOT invalidate the entry.
    html.write_text("<!-- moved -->\n" + html.read_text(encoding="utf-8"), encoding="utf-8")
    assert _run("--json", root=tree).returncode == 0

    # Changing the copy makes the entry stale: no finding has that literal.
    html.write_text(html.read_text(encoding="utf-8")
                    .replace("Switch to Danish", "Switch to English"), encoding="utf-8")
    proc = _run("--json", root=tree)
    assert proc.returncode == 1
    unused = [f for f in _findings(proc)
              if f["file"] == "i18n/copy-allowlist.txt" and "unused" in f["message"]]
    assert unused, [f for f in _findings(proc) if f["file"] == "i18n/copy-allowlist.txt"]


# --- escape classes the first review found ---------------------------------

def test_planted_value_attribute(tmp_path):
    """An input's `value` is its visible label — not a machine payload."""
    tree = _copy_tree(tmp_path)
    (tree / "i18n" / "migrated.txt").unlink()
    line = _insert_before(tree / "static" / "index.html", "</main>",
                          '  <input value="Type a name" />')
    _assert_planted(_run("--json", root=tree), "static/index.html", line, kind="attr")


def test_planted_aria_description_attribute(tmp_path):
    """`aria-description` is copy (the other aria-* attributes are state/config)."""
    tree = _copy_tree(tmp_path)
    (tree / "i18n" / "migrated.txt").unlink()
    line = _insert_before(tree / "static" / "index.html", "</main>",
                          '  <div aria-description="Product details"></div>')
    _assert_planted(_run("--json", root=tree), "static/index.html", line, kind="attr")


def test_planted_click_handler_show_toast(tmp_path):
    """A @click handler is JS: its showToast literal must be caught (item 5's home)."""
    tree = _copy_tree(tmp_path)
    (tree / "i18n" / "migrated.txt").unlink()
    line = _insert_before(tree / "static" / "index.html", "</main>",
                          "  <div @click=\"showToast('Saved the report')\"></div>")
    _assert_planted(_run("--json", root=tree), "static/index.html", line, kind="call")


def test_planted_submit_handler_confirm(tmp_path):
    tree = _copy_tree(tmp_path)
    (tree / "i18n" / "migrated.txt").unlink()
    line = _insert_before(tree / "static" / "index.html", "</main>",
                          "  <form @submit=\"confirm('Delete it?')\"></form>")
    _assert_planted(_run("--json", root=tree), "static/index.html", line, kind="call")


def test_planted_bare_onclick_alert(tmp_path):
    tree = _copy_tree(tmp_path)
    (tree / "i18n" / "migrated.txt").unlink()
    line = _insert_before(tree / "static" / "index.html", "</main>",
                          "  <button onclick=\"alert('Hi there')\"></button>")
    _assert_planted(_run("--json", root=tree), "static/index.html", line, kind="call")


def test_plain_handler_without_literals_stays_clean(tmp_path):
    """The negative: `@click="toggleLang"` has no literal copy and must not be flagged."""
    tree = _copy_tree(tmp_path)
    (tree / "i18n" / "migrated.txt").unlink()
    line = _insert_before(tree / "static" / "index.html", "</main>",
                          '  <button @click="toggleLang"></button>')
    findings = _findings(_run("--json", root=tree))
    assert not [f for f in findings
                if f["file"] == "static/index.html" and f["line"] == line], (
        "a handler with no literal copy was flagged")


def test_planted_inline_script_is_scanned_as_js(tmp_path):
    """An inline <script> body is scanned by the JS scanners (absolute line)."""
    tree = _copy_tree(tmp_path)
    (tree / "i18n" / "migrated.txt").unlink()
    line = _insert_before(tree / "static" / "index.html", "</main>",
                          "  <script>showToast('Saved the report');</script>")
    _assert_planted(_run("--json", root=tree), "static/index.html", line, kind="call")


def test_planted_backtick_and_single_word_expression(tmp_path):
    """Backtick literals and 1-word literals in expressions are copy too."""
    tree = _copy_tree(tmp_path)
    (tree / "i18n" / "migrated.txt").unlink()
    html = tree / "static" / "index.html"
    first = _insert_before(html, "</main>", "  <p>{{ `Open` }}</p>")
    second = _insert_before(html, "</main>", '  <span :title="\'close\'"></span>')
    findings = _findings(_run("--json", root=tree))
    assert any(f["file"] == "static/index.html" and f["line"] == first
               and f["kind"] == "expr" for f in findings), findings[:10]
    assert any(f["file"] == "static/index.html" and f["line"] == second
               and f["kind"] == "attr-expr" for f in findings), findings[:10]


def test_planted_comment_only_reference_is_dead(tmp_path):
    """An id mentioned ONLY in a test comment must not count as a reference."""
    tree = _copy_tree(tmp_path)
    path = tree / "i18n" / "strings.csv"
    dead = "zzz" + "." + "comment"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines + [f"{dead},ZZZ copy,ZZZ tekst"]) + "\n",
                    encoding="utf-8")
    test_file = tree / "tests" / "test_lang_toggle.py"
    test_file.write_text(test_file.read_text(encoding="utf-8")
                         + f"\n# a comment naming {dead} must not count\n",
                         encoding="utf-8")
    _assert_planted(_run("--json", root=tree), "i18n/strings.csv", len(lines) + 1,
                    kind="dead-row", contains=dead)


def test_planted_js_reference_is_resolved(tmp_path):
    """A JS call site is a reference: `this.t("…")` in app.js must resolve too."""
    tree = _copy_tree(tmp_path)
    line = _insert_before(
        tree / "static" / "app.js",
        '        const data = await api("/auth/login", { method: "POST", body: this.loginForm });',
        '        this.msg(this.t("nope.missing_js"));')
    _assert_planted(_run("--json", root=tree), "static/app.js", line,
                    kind="unknown-id", contains="nope.missing_js")


# --- the family list, the exit codes ---------------------------------------

def _load_module():
    spec = importlib.util.spec_from_file_location("i18n_check_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_dynamic_family_with_no_rows_is_rejected():
    """A declared dynamic family must name at least one row (the list cannot rot)."""
    module = _load_module()
    rows, _problems = module.pass_csv(CSV)
    assert rows
    findings = module.family_findings(rows, {"zzz.": (7, "planted producer")})
    assert len(findings) == 1
    assert findings[0].pass_name == "dead-row" and findings[0].kind == "stale-family"
    assert "zzz." in findings[0].message and findings[0].line == 7
    # A family that does have rows is fine (common.* owns the seed rows).
    assert module.family_findings(rows, {"common.": (7, "seeded")}) == []


def test_usage_and_source_errors_exit_two(tmp_path):
    assert _run("--root", tmp_path / "no-such-tree").returncode == 2
    tree = _copy_tree(tmp_path)
    path = tree / "i18n" / "strings.csv"
    path.write_text(path.read_text(encoding="utf-8").replace(
        "app.title,HCRM — Shop Catalogue,HCRM — Butikskatalog",
        "app.title,HCRM — Shop Catalogue,HCRM — Butikskatalog\n"
        "app.title,Duplicate,Duplicate"), encoding="utf-8")
    proc = _run("--json", root=tree)
    assert proc.returncode == 2
    assert any(f["pass"] == "csv" and "duplicate" in f["message"] for f in _findings(proc))


def test_calendar_date_families_are_declared_dynamic():
    """Slice C's weekday/month labels are picked by index at render time, so the
    dead-row pass can only see their 19 rows through DYNAMIC_FAMILIES; losing a
    declaration would report every one of them as dead."""
    module = _load_module()
    assert set(module.DYNAMIC_FAMILIES) == {
        "date.dow.", "date.month.", "date.month_long."}
    rows, _problems = module.pass_csv(CSV)
    assert module.family_findings(rows) == []
