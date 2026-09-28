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


def test_repo_hygiene():
    if not os.path.isdir(os.path.join(REPO_ROOT, ".git")):
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


def test_no_database_artifacts_are_committed_in_history():
    """The rewritten history must stay clean; a re-added blob would be silent."""
    if not os.path.isdir(os.path.join(REPO_ROOT, ".git")):
        pytest.skip("not a git checkout")
    objects = _git("rev-list", "--all", "--objects").stdout
    leaked = [l for l in objects.splitlines() if "hcrm.db" in l]
    assert not leaked, f"database blobs reachable from refs: {leaked[:5]}"
