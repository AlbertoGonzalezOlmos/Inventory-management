"""W5.4 / W5.5 / W4.5 — metadata, kill-switches, repo hygiene and the token index.

These are the cheap tripwires for things that were fixed by hand once and would
silently drift back:

* the version was 0.1.0 in pyproject.toml and 0.2.0 in the app (Eng3 #3);
* `HCRM_DOCS=0` is read at import time, so nothing in-process can catch it
  rotting — it needs a subprocess;
* the runtime database and the `*copy*` duplicates were committed (N1);
* `_issue_token()` purges expired tokens on every login, which was a full table
  scan until `expires_at` got an index (N8);
* the README claimed the last-admin check runs "inside BEGIN IMMEDIATE
  transactions" — the exact thing app/database.py forbids after a real incident.
"""

import json
import os
import re
import subprocess
import sys
import tempfile

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- W5.4: metadata consistency ---------------------------------------------

def test_version_is_consistent_everywhere():
    import tomllib

    from app.main import app

    with open(os.path.join(REPO_ROOT, "pyproject.toml"), "rb") as fh:
        pyproject = tomllib.load(fh)
    assert pyproject["project"]["version"] == app.version, (
        f"pyproject says {pyproject['project']['version']}, app says {app.version}")
    assert pyproject["project"]["readme"] == "README.md"
    assert os.path.exists(os.path.join(REPO_ROOT, "README.md"))


def test_readme_is_not_a_stub():
    """README.md used to be 91 bytes while the real doc sat in 'README copy.md'."""
    text = open(os.path.join(REPO_ROOT, "README.md"), encoding="utf-8").read()
    assert len(text) > 4000, f"README.md is only {len(text)} bytes"
    for heading in ("## Quick start", "## Configuration", "## Security notes",
                    "## Testing"):
        assert heading in text, heading


def test_readme_has_no_known_false_claims():
    text = open(os.path.join(REPO_ROOT, "README.md"), encoding="utf-8").read()
    # The last-admin invariant is enforced by DB triggers, NOT by transaction
    # mode: a global BEGIN IMMEDIATE hook caused an unauthenticated DoS
    # ("database is locked" 500s) and app/database.py forbids reintroducing it.
    # The phrase may still appear as a warning ("do NOT do this"), but the stale
    # claim that the API checks run inside such transactions must not.
    assert "database triggers" in text, "the trigger-based invariant must be described"
    for stale in ("run inside `BEGIN IMMEDIATE`",
                  "inside `BEGIN IMMEDIATE` transactions",
                  "BEGIN IMMEDIATE` transactions (no check-then-write"):
        assert stale not in text, f"stale claim: {stale}"
    assert "Do **not**" in text and "BEGIN IMMEDIATE" in text, (
        "keep the explicit warning against reintroducing the hook")
    # The CSP carries 'unsafe-eval'; calling it strict is how the round started.
    assert "strict CSP" not in text
    assert "unsafe-eval" in text, "the CSP's real cost must be documented"
    # /docs pulls swagger-ui from jsdelivr, so "fully offline" needs a caveat.
    assert "jsdelivr" in text or "HCRM_DOCS" in text


# --- W5.5: the HCRM_DOCS kill-switch (import-time env -> subprocess) ---------

_DOCS_SNIPPET = """
import json
from fastapi.testclient import TestClient
from app.main import app

with TestClient(app) as c:
    out = {p: c.get(p).status_code
           for p in ("/docs", "/redoc", "/openapi.json", "/", "/api/healthz")}
    out["csp_on_root"] = "content-security-policy" in {
        k.lower() for k in c.get("/").headers}
print("JSON>>" + json.dumps(out))
"""


def _boot_with(env_overrides: dict) -> dict:
    env = {
        **os.environ,
        "PYTHONPATH": REPO_ROOT,
        "HCRM_DATA_DIR": tempfile.mkdtemp(prefix="hcrm-docsflag-"),
        "HCRM_EMBED_MODEL": "bogus/model",       # fail fast, no 80 MB download
        "HCRM_PBKDF2_ITERATIONS": "1000",
        **env_overrides,
    }
    proc = subprocess.run([sys.executable, "-c", _DOCS_SNIPPET],
                          capture_output=True, text=True, timeout=240,
                          cwd=REPO_ROOT, env=env)
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = next(l for l in proc.stdout.splitlines() if l.startswith("JSON>>"))
    return json.loads(line[len("JSON>>"):])


def test_docs_kill_switch_disables_all_three_endpoints():
    """openapi_url must be nulled too: FastAPI gates docs on it, so nulling only
    docs_url/redoc_url leaves the full endpoint schema public."""
    out = _boot_with({"HCRM_DOCS": "0"})
    assert out["/docs"] == 404, out
    assert out["/redoc"] == 404, out
    assert out["/openapi.json"] == 404, out
    # The product still works.
    assert out["/"] == 200, out
    assert out["/api/healthz"] == 200, out
    assert out["csp_on_root"] is True, out


def test_docs_enabled_by_default():
    out = _boot_with({})
    assert out["/docs"] == 200, out
    assert out["/redoc"] == 200, out
    assert out["/openapi.json"] == 200, out
    # …and the docs routes are the ones exempt from the CSP, not the SPA.
    assert out["csp_on_root"] is True, out


# --- W4.5: the expired-token purge must be index-backed -----------------------

def test_auth_tokens_expiry_is_indexed(client):
    from sqlalchemy import text

    from app.database import engine

    with engine.connect() as conn:
        indexes = [r[0] for r in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='auth_tokens'"))]
        assert "ix_auth_tokens_expires_at" in indexes, indexes
        plan = " ".join(
            str(row) for row in conn.execute(text(
                "EXPLAIN QUERY PLAN SELECT token FROM auth_tokens "
                "WHERE expires_at < '2000-01-01'")))
        assert "ix_auth_tokens_expires_at" in plan, plan


# --- repo hygiene (N1: the runtime DB and the *copy* files were committed) ----

def _git(*args):
    proc = subprocess.run(["git", *args], capture_output=True, text=True,
                          timeout=60, cwd=REPO_ROOT)
    return proc


def _in_git_checkout() -> bool:
    # `.git` is a FILE, not a directory, in a linked worktree (`git worktree
    # add`) — exactly the setup a reviewer uses — so gating on isdir('.git')
    # silently skipped these tripwires there. Ask git instead.
    return _git("rev-parse", "--git-dir").returncode == 0


def test_repo_hygiene():
    if not _in_git_checkout():
        pytest.skip("not a git checkout")
    tracked = _git("ls-files").stdout.splitlines()
    assert tracked, "git ls-files returned nothing"

    copies = [f for f in tracked if " copy" in f or f.endswith("copy.md")]
    assert not copies, f"duplicate artifacts are tracked: {copies}"

    data_files = [f for f in tracked if f.startswith("data/")]
    assert not data_files, (
        f"the runtime database must never be tracked (it held the admin password "
        f"hash and plaintext session tokens): {data_files}")

    # …and the ignore rule that keeps it out must actually match.
    assert _git("check-ignore", "-q", "data/hcrm.db").returncode == 0
    assert _git("check-ignore", "-q", "data/hcrm.db-wal").returncode == 0

    # The agent coordination lock files (agent_note.py creates <file>.lock
    # beside its .md targets) must stay untracked — and the rule must stay
    # SCOPED. --no-index on all three: git check-ignore consults the index
    # by default, and uv.lock IS tracked, so without --no-index the
    # assertion passes under ANY rule (measured: `*.lock` still reports
    # "not ignored" for a tracked path) — a vacuous pin. --no-index tests
    # the RULES, which is what "scoped on purpose" means.
    assert _git("check-ignore", "-q", "--no-index",
                "SCANNER-AGENT-SYNC.md.lock").returncode == 0
    assert _git("check-ignore", "-q", "--no-index",
                "notes.md.lock").returncode == 0
    assert _git("check-ignore", "-q", "--no-index", "uv.lock").returncode != 0, (
        "the lock rule must stay *.md.lock — a blanket *.lock would keep a "
        "NEW lock file (poetry.lock, a regenerated uv.lock after a delete) "
        "out of git status and out of `git add -A` forever")


#: Vendored third-party assets: rewriting an upstream minified bundle to
#: satisfy a style pin is a worse diff than the missing byte. A future
#: vendored asset OUTSIDE this prefix needs the list extended.
NEWLINE_EXEMPT_DIRS = ("static/vendor/",)

#: In-tree path citations extracted from shipped files that deliberately do
#: NOT resolve — each entry needs a reason (the ui_check.BENIGN_PREFIXES
#: doctrine: a shrinking allowlist is the healthy direction).
CITED_PATH_ALLOWLIST = {
    # app/main.py:365 cites it as the MEASURED counter-example of the /docs
    # CSP boundary ("a static/docs.html was served with no CSP header at
    # all", PLAN-v2 §8.2 F3). The probe file was deliberately never
    # committed — PLAN-v2:849 says do not create it in the real tree.
    "static/docs.html",
}

#: A citation worth pinning carries its directory: one of these prefixes.
#: Extending the list is a deliberate edit, not a cleanup.
_CITED_PREFIXES = ("app", "docs", "scripts", "static", "tests")

_CITED_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./:-])"                   # not the tail of a URL or a
    r"\b(?:app|docs|scripts|static|tests)/"    # longer path: kills https://…/docs/x.md
    r"[A-Za-z0-9._/-]*[A-Za-z0-9]"             # path chars ONLY — no < > { } * $
    r"\.[A-Za-z0-9]{1,8}\b")                   # must end in a real extension


def test_shipped_files_cite_only_in_tree_paths():
    """Every in-tree path cited by a shipped file must be IN GIT.

    The defect class (it happened): tracked files cited 8 docs/plans/…
    paths that existed on disk but were never committed — every local gate
    stayed green for a whole round, and the citations dangled on every
    fresh clone. Therefore resolution is against `git ls-files`, NEVER
    `os.path.exists()`: a filesystem check would have passed then and is
    worthless here.

    The extractor is deliberately NARROW, because narrow and honest beats
    broad and noisy: a citation must carry one of the pinned directory
    prefixes and a real extension. What that excludes by construction, and
    why that is right: bare filenames (unresolvable by name — the M-10
    round's bare-name sweep produced 18 hits, mostly tmp_path fixtures);
    globs (`tests/js/*.test.mjs` — a glob is not one tracked path); URLs
    (the negative lookbehind rejects a match preceded by `/` or `:`);
    placeholders (`<repo>/…`, `{bdf}` — no `<>`/`{}` in the path class);
    bare directory mentions (no extension). Measured on this tree: 73
    distinct citations, 0 accidental matches, 1 deliberate allowlist entry.

    Exemptions, reusing test_shipped_artifacts_never_name_a_git_branch's
    exact sets (same rationale — process notes talk about the work,
    including paths that were renamed or never landed): docs/plans/** (the
    archive cites never-implemented filenames and contains a typo — its
    dirname match is EXACT, so a docs/plans/sub/ would NOT be exempt; the
    archive must stay flat) and basenames PLAN*/REVIEW*/NOTES*. Measured
    cost of the basename exemption: zero — no unresolved citation is cited
    only by those files.
    """
    if not _in_git_checkout():
        pytest.skip("not a git checkout")
    tracked = _git("ls-files").stdout.splitlines()
    assert tracked, "git ls-files returned nothing"
    in_git = set(tracked)
    exempt_prefixes = ("PLAN", "REVIEW", "NOTES")
    exempt_dirs = ("docs/plans",)
    text_exts = (".py", ".md", ".sh", ".rules", ".service", ".css", ".html",
                 ".mjs", ".js", ".json", ".toml", ".yml")
    offenders = []
    for path in tracked:
        if not path.endswith(text_exts):
            continue
        if path.startswith(NEWLINE_EXEMPT_DIRS):
            continue
        if os.path.basename(path).startswith(exempt_prefixes):
            continue
        if os.path.dirname(path) in exempt_dirs:
            continue
        try:
            text = open(os.path.join(REPO_ROOT, path), encoding="utf-8",
                        errors="replace").read()
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in _CITED_PATH_RE.finditer(line):
                cited = m.group(0).rstrip(".,;:)]}'\"")
                if cited in in_git or cited in CITED_PATH_ALLOWLIST:
                    continue
                offenders.append(f"{path}:{lineno}: {cited}")
    assert not offenders, (
        "shipped files cite paths that are not in git (dangling on every "
        "fresh clone — the M-10 defect class):\n  " + "\n  ".join(offenders))


def test_text_files_end_with_exactly_one_newline():
    """Every tracked text file terminates with exactly one ``\n``.

    A missing final newline is invisible until it is not: every later diff
    of the last line carries ``\\ No newline at end of file``, and ``cat``
    concatenation mangles the file. Empty files are exempt (nothing to
    terminate — which also makes the two empty ``app/__init__.py`` cases
    natural, not special); vendored assets are exempt (we do not edit
    upstream bytes). Exactly ONE: a doubled trailing blank is the same
    class of drift in the other direction, and measured 0 files today.
    """
    if not _in_git_checkout():
        pytest.skip("not a git checkout")
    tracked = _git("ls-files").stdout.splitlines()
    assert tracked, "git ls-files returned nothing"
    exts = (".py", ".md", ".sh", ".rules", ".service", ".css", ".html",
            ".mjs", ".js", ".json", ".toml", ".yml")
    offenders = []
    for path in tracked:
        if not path.endswith(exts) or path.startswith(NEWLINE_EXEMPT_DIRS):
            continue
        data = open(os.path.join(REPO_ROOT, path), "rb").read()
        if not data:                       # empty: nothing to terminate
            continue
        if not data.endswith(b"\n") or data.endswith(b"\n\n"):
            offenders.append(path)
    assert not offenders, f"fix the EOF newline (exactly one): {offenders}"


def test_shipped_artifacts_never_name_a_git_branch():
    """A shipped file must point at in-tree paths, not at unmerged branches.

    This is the tripwire for the scanner-branch tangle: `scripts/opn2001.py`
    and `docs/scanner-opn2001.md` shipped four operator-facing strings of the
    form "see the `feature/…` branch docs". Merged into `main`, those
    pointers resolve to nothing — a reader of `main` has no such branch, and
    the file they are told to read does not exist there yet. Same class as a
    dangling `§3.5` cross-reference, except it also leaks in-flight branch
    topology into the product.

    Process/history documents are exempt on purpose: `PLAN*.md`, `REVIEW*.md`
    and `NOTES.md` exist to talk about the work, including its branches. The
    slug must contain a separator, so ordinary prose ("the backup/run
    scripts") is not a false positive.
    """
    if not _in_git_checkout():
        pytest.skip("not a git checkout")
    import re

    pattern = re.compile(
        r"\b(?:feature|archive|backup|hotfix|release|chore|refactor)"
        r"/[A-Za-z0-9][A-Za-z0-9._]*[-_.][A-Za-z0-9._-]*")
    exempt_prefixes = ("PLAN", "REVIEW", "NOTES")
    # docs/plans/ is the workstream's process-notes parking lot (council
    # proposals/reviews, coordinator syntheses, round checklists): the same
    # "exists to talk about the work, including its branches" class as the
    # exempt prefixes above — but never shipped operator documentation.
    exempt_dirs = ("docs/plans",)
    # --cached --others --exclude-standard: everything that would be part of
    # the next commit, including files this branch adds but has not committed
    # yet. `git ls-files` alone silently skips those — which is exactly when a
    # new script carrying a branch reference would slip through.
    tracked = _git("ls-files", "--cached", "--others", "--exclude-standard").stdout.splitlines()
    assert tracked, "git ls-files returned nothing"

    offenders = []
    for path in tracked:
        if not path.startswith(("app/", "scripts/", "docs/", "static/", "tests/")) \
                and path not in ("README.md",):
            continue
        if os.path.basename(path).startswith(exempt_prefixes):
            continue
        if os.path.dirname(path) in exempt_dirs:
            continue
        try:
            text = open(os.path.join(REPO_ROOT, path), encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue  # binary asset (vendored JS, fonts): not prose
        for n, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path}:{n}: {line.strip()[:100]}")

    assert not offenders, (
        "shipped artifacts reference a git branch instead of an in-tree path "
        "(if the target is not merged yet, describe it without naming the "
        "branch, and add the cross-link from the PR that owns the target):\n  "
        + "\n  ".join(offenders))


def test_no_database_artifacts_are_committed_in_history():
    """The rewritten history must stay clean; a re-added blob would be silent."""
    if not _in_git_checkout():
        pytest.skip("not a git checkout")
    objects = _git("rev-list", "--all", "--objects").stdout
    leaked = [l for l in objects.splitlines() if "hcrm.db" in l]
    assert not leaked, f"database blobs reachable from refs: {leaked[:5]}"
