"""W4.0 — dev admin mode (`admin` / `admin`, owner directive "until further
notice").

The mode is read at import time (`app.security.DEV_ADMIN`), and the suite runs
with `HCRM_DEV_ADMIN=0` (tests/conftest.py) so every other security test keeps
exercising the secure default. The ON-path therefore runs in a subprocess with
its own scratch database — the same shape as the `HCRM_DOCS=0` test (W5.5).

Guarantees pinned here:
- mode ON: `admin`/`admin` logs in, is an admin, is NOT flagged, and reaches the
  API; `/api/healthz` reports `insecure_dev_admin: true`; seeding is idempotent
  and never rewrites an existing account's password.
- mode OFF: the secure default is untouched (`admin`/`admin` → 401, seeded
  `admin@shop.local`/`changeme` still flagged and blocked).
- `scripts/run.sh` refuses a non-loopback bind while the mode is on.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_BOOT_SNIPPET = """
import json
from fastapi.testclient import TestClient
from app.main import app

out = {}
with TestClient(app) as c:                      # runs the lifespan (seeding)
    r = c.post("/api/auth/login", json={"email": "admin", "password": "admin"})
    out["login_status"] = r.status_code
    if r.status_code == 200:
        d = r.json()
        out["role"] = d["user"]["role"]
        out["flagged"] = d["user"]["must_change_password"]
        h = {"Authorization": "Bearer " + d["token"]}
        out["items"] = c.get("/api/items", headers=h).status_code
        out["members"] = c.get("/api/members", headers=h).status_code
        out["accounts"] = [
            (u["email"], u["role"], u["must_change_password"])
            for u in c.get("/api/members", headers=h).json()
        ]
    r2 = c.post("/api/auth/login",
                json={"email": "admin@shop.local", "password": "changeme"})
    out["legacy_login"] = r2.status_code
    if r2.status_code == 200:
        out["legacy_flagged"] = r2.json()["user"]["must_change_password"]
    out["healthz"] = c.get("/api/healthz").json()
print("JSON>>" + json.dumps(out))
"""


def _boot(dev_admin: str, extra_env: dict | None = None, data_dir: str | None = None):
    """Boot the app in a subprocess on a scratch DB and return its JSON report."""
    data_dir = data_dir or tempfile.mkdtemp(prefix="hcrm-devadmin-")
    env = {
        **os.environ,
        "PYTHONPATH": REPO_ROOT,
        "HCRM_DATA_DIR": data_dir,
        "HCRM_DEV_ADMIN": dev_admin,
        "HCRM_EMBED_MODEL": "bogus/model",  # fail fast, no 80 MB download
        "HCRM_PBKDF2_ITERATIONS": "1000",
    }
    env.update(extra_env or {})
    proc = subprocess.run(
        [sys.executable, "-c", _BOOT_SNIPPET],
        capture_output=True, text=True, timeout=240, cwd=REPO_ROOT, env=env,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = next(l for l in proc.stdout.splitlines() if l.startswith("JSON>>"))
    return json.loads(line[len("JSON>>"):]), data_dir


def test_dev_admin_mode_on():
    report, _ = _boot("1")
    assert report["login_status"] == 200, report
    assert report["role"] == "admin"
    # The credential must be usable: a flagged account is blocked from the API.
    assert report["flagged"] is False
    assert report["items"] == 200
    assert report["members"] == 200
    assert report["healthz"]["insecure_dev_admin"] is True
    emails = [a[0] for a in report["accounts"]]
    assert emails.count("admin") == 1, report["accounts"]
    # The secure default still exists alongside it, still flagged.
    assert report["legacy_login"] == 200
    assert report["legacy_flagged"] is True


def test_dev_admin_mode_off_keeps_the_secure_default():
    report, _ = _boot("0")
    assert report["login_status"] == 401, report      # admin/admin must not work
    assert report["healthz"]["insecure_dev_admin"] is False
    assert report["legacy_login"] == 200
    assert report["legacy_flagged"] is True


def test_dev_admin_seeding_is_idempotent_and_non_destructive():
    """A second boot must not duplicate the account or reset its password."""
    data_dir = tempfile.mkdtemp(prefix="hcrm-devadmin-idem-")
    first, _ = _boot("1", data_dir=data_dir)
    assert first["login_status"] == 200

    snippet = """
import json
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from app.database import engine
from app.main import _ensure_dev_admin, app
from app.models import User

out = {}
with TestClient(app) as c:
    tok = c.post("/api/auth/login",
                 json={"email": "admin", "password": "admin"}).json()["token"]
    h = {"Authorization": "Bearer " + tok}
    # operator changes the dev password
    r = c.post("/api/auth/change-password", headers=h,
               json={"current_password": "admin", "new_password": "newpassword1"})
    out["change"] = r.status_code
    # seeding runs again (as it does on every boot)
    with Session(engine) as s:
        _ensure_dev_admin(s)
        _ensure_dev_admin(s)
        out["dev_accounts"] = len(s.exec(
            select(User).where(User.email == "admin")).all())
    out["old_pw"] = c.post("/api/auth/login",
                           json={"email": "admin", "password": "admin"}).status_code
    out["new_pw"] = c.post("/api/auth/login",
                           json={"email": "admin", "password": "newpassword1"}).status_code
print("JSON>>" + json.dumps(out))
"""
    env = {
        **os.environ,
        "PYTHONPATH": REPO_ROOT,
        "HCRM_DATA_DIR": data_dir,
        "HCRM_DEV_ADMIN": "1",
        "HCRM_EMBED_MODEL": "bogus/model",
        "HCRM_PBKDF2_ITERATIONS": "1000",
    }
    proc = subprocess.run([sys.executable, "-c", snippet], capture_output=True,
                          text=True, timeout=240, cwd=REPO_ROOT, env=env)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(
        next(l for l in proc.stdout.splitlines() if l.startswith("JSON>>"))[6:]
    )
    assert out["change"] == 204, out
    assert out["dev_accounts"] == 1, out          # idempotent
    assert out["old_pw"] == 401, out              # password NOT reset by seeding
    assert out["new_pw"] == 200, out


def test_dev_admin_not_flagged_by_the_well_known_password_scan():
    """The broadened scan (N5) must never block the dev credential."""
    snippet = """
import json
from fastapi.testclient import TestClient
from sqlmodel import Session
from app.database import engine
from app.main import WEAK_SCAN_KEY, _flag_well_known_passwords, app
from app.models import AppMeta

out = {}
with TestClient(app) as c:
    with Session(engine) as s:
        # force the (marker-gated) scan to actually run, twice
        s.delete(s.get(AppMeta, WEAK_SCAN_KEY)); s.commit()
        _flag_well_known_passwords(s)
        s.delete(s.get(AppMeta, WEAK_SCAN_KEY)); s.commit()
        _flag_well_known_passwords(s)
    tok = c.post("/api/auth/login",
                 json={"email": "admin", "password": "admin"}).json().get("token")
    out["login"] = 200 if tok else 401
    if tok:
        h = {"Authorization": "Bearer " + tok}
        out["items"] = c.get("/api/items", headers=h).status_code
print("JSON>>" + json.dumps(out))
"""
    env = {
        **os.environ,
        "PYTHONPATH": REPO_ROOT,
        "HCRM_DATA_DIR": tempfile.mkdtemp(prefix="hcrm-devadmin-flag-"),
        "HCRM_DEV_ADMIN": "1",
        "HCRM_EMBED_MODEL": "bogus/model",
        "HCRM_PBKDF2_ITERATIONS": "1000",
    }
    proc = subprocess.run([sys.executable, "-c", snippet], capture_output=True,
                          text=True, timeout=240, cwd=REPO_ROOT, env=env)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(
        next(l for l in proc.stdout.splitlines() if l.startswith("JSON>>"))[6:]
    )
    assert out["login"] == 200, out
    assert out["items"] == 200, out               # not blocked by the flag


def _run_sh(extra_env: dict):
    env = {**os.environ, **extra_env}
    # `uv` is hidden so the script cannot actually sync/start a server: exit 127
    # proves it got PAST the bind guard, exit 1 + "refusing" proves it did not.
    # bash must be resolved BEFORE PATH is emptied, hence the absolute path.
    bash = shutil.which("bash") or "/bin/bash"
    env["PATH"] = "/nonexistent"
    return subprocess.run(
        [bash, "scripts/run.sh"], capture_output=True, text=True,
        timeout=60, cwd=REPO_ROOT, env=env,
    )


def test_run_sh_refuses_network_bind_while_dev_admin_is_on():
    proc = _run_sh({"HCRM_HOST": "0.0.0.0", "HCRM_DEV_ADMIN": "1"})
    assert proc.returncode == 1, (proc.returncode, proc.stderr[-500:])
    assert "refusing to bind" in proc.stderr


def test_run_sh_allows_network_bind_when_dev_admin_is_off():
    proc = _run_sh({"HCRM_HOST": "0.0.0.0", "HCRM_DEV_ADMIN": "0"})
    assert "refusing to bind" not in proc.stderr
    assert proc.returncode == 127, (proc.returncode, proc.stderr[-500:])  # uv not found


def test_run_sh_allows_explicit_override():
    proc = _run_sh({
        "HCRM_HOST": "0.0.0.0", "HCRM_DEV_ADMIN": "1",
        "HCRM_ALLOW_INSECURE_BIND": "1",
    })
    assert "refusing to bind" not in proc.stderr
    assert proc.returncode == 127, (proc.returncode, proc.stderr[-500:])


def test_loopback_bind_never_refuses():
    proc = _run_sh({"HCRM_HOST": "127.0.0.1", "HCRM_DEV_ADMIN": "1"})
    assert "refusing to bind" not in proc.stderr
    assert proc.returncode == 127, (proc.returncode, proc.stderr[-500:])


# --- mode OFF in-process (the suite's default) --------------------------------

def test_suite_runs_with_dev_admin_disabled(client):
    """Guard the guard: if conftest ever stops disabling the mode, the whole
    security suite would silently test a different product."""
    from app.security import DEV_ADMIN

    assert DEV_ADMIN is False, "tests/conftest.py must keep HCRM_DEV_ADMIN=0"
    r = client.post("/api/auth/login", json={"email": "admin", "password": "admin"})
    assert r.status_code == 401
    assert client.get("/api/healthz").json()["insecure_dev_admin"] is False
