"""Shared helpers for the HCRM verification scripts (PLAN-v2 W3.1).

The contract these helpers exist to enforce — each clause is a regression that
actually shipped (PLAN.md P-1/P-2/P-3, PLAN-v2 §0 B5-B8/B13):

* **Never restore a password.** If the bootstrap had to change one, the final
  state is *reported*, not undone. Restoring re-armed the well-known `changeme`
  default with `must_change_password` cleared, i.e. the "security" scripts left
  the server less safe than they found it (finding E), and an unconditional
  restore made a passing load run exit 1 (finding D).
* **Self-host by default.** The default target is a throwaway server on a
  scratch `HCRM_DATA_DIR` and a free port, torn down afterwards, so a
  verification run cannot touch a real deployment. Targeting an existing server
  requires `--force` **and** explicit credentials from the environment (P-3).
* **A failing check must fail the process.** Every check goes through
  `Reporter`, which never raises on an unexpected payload shape; the exit code
  is the AND of all checks (finding C discarded it, finding N4 died with a
  `TypeError` instead of printing `[FAIL]`).
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A model name that fastembed rejects immediately: hermetic, offline boots with
# no 80 MB download. Embedding-dependent checks must then report [FAIL]/[SKIP]
# explicitly rather than hang or crash (see smoke_test.py).
NO_MODEL = "bogus/model"

EXIT_OK = 0
EXIT_CHECKS_FAILED = 1
EXIT_UNREACHABLE = 2
EXIT_USAGE = 3


def die(message: str, code: int = EXIT_USAGE) -> "NoReturn":  # noqa: F821
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def call(base: str, path: str, method: str = "GET", token: str | None = None,
         body=None, timeout: float = 30.0) -> tuple[int, object, float]:
    """One API call -> (status, parsed payload, elapsed seconds).

    Never raises for HTTP errors: a 4xx/5xx is a result the caller must be able
    to report, not an exception that skips the report (finding N4).
    """
    req = urllib.request.Request(
        base.rstrip("/") + path, method=method,
        headers={"Content-Type": "application/json"},
        data=None if body is None else json.dumps(body).encode(),
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
            return res.status, _decode(raw), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, _decode(raw), time.monotonic() - started
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        return 0, {"detail": f"connection failed: {exc}"}, time.monotonic() - started


def _decode(raw: bytes) -> object:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw.decode(errors="replace")[:500]}


def detail_of(payload: object) -> str:
    """Human-readable one-liner for any payload shape (dict / list / str / None)."""
    if isinstance(payload, dict):
        if "detail" in payload:
            return str(payload["detail"])[:200]
        if "raw" in payload:
            return str(payload["raw"])[:200]
        return json.dumps(payload)[:200]
    if isinstance(payload, list):
        return f"[{len(payload)} item(s)]"
    return str(payload)[:200]


def healthz(base: str, timeout: float = 5.0) -> dict | None:
    status, payload, _ = call(base, "/api/healthz", timeout=timeout)
    return payload if status == 200 and isinstance(payload, dict) else None


def wait_for_server(base: str, timeout: float = 90.0, poll: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if healthz(base, timeout=2.0) is not None:
            return True
        time.sleep(poll)
    return False


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --------------------------------------------------------------------------- #
# Self-hosted scratch server
# --------------------------------------------------------------------------- #

@dataclass
class Server:
    base: str
    data_dir: str
    log_path: str
    port: int
    proc: subprocess.Popen | None = None
    env: dict = field(default_factory=dict)

    def log_tail(self, lines: int = 25) -> str:
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as fh:
                return "".join(fh.readlines()[-lines:])
        except OSError:
            return "(no server log)"


@contextlib.contextmanager
def self_host(model: str = NO_MODEL, pbkdf2_iterations: int | None = None,
              extra_env: dict | None = None, keep: bool = False,
              startup_timeout: float = 120.0):
    """Boot a throwaway server; yield a `Server`; always tear it down.

    `pbkdf2_iterations=None` keeps the app's real (expensive) default, which is
    what makes a load test's latency numbers meaningful (finding N10: the old
    docstring claimed 600k iterations while nothing enforced it).
    """
    data_dir = tempfile.mkdtemp(prefix="hcrm-selfhost-")
    port = free_port()
    log_path = os.path.join(tempfile.gettempdir(), f"hcrm-selfhost-{port}.log")
    env = {
        **os.environ,
        "PYTHONPATH": REPO_ROOT,
        "HCRM_DATA_DIR": data_dir,
        "HCRM_EMBED_MODEL": model,
        "HCRM_DEV_ADMIN": "1",          # deterministic admin/admin bootstrap
        "HCRM_HOST": "127.0.0.1",
        "HCRM_PORT": str(port),
    }
    if pbkdf2_iterations is not None:
        env["HCRM_PBKDF2_ITERATIONS"] = str(pbkdf2_iterations)
    env.update(extra_env or {})

    log = open(log_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    server = Server(base=f"http://127.0.0.1:{port}", data_dir=data_dir,
                    log_path=log_path, port=port, proc=proc, env=env)
    try:
        if not wait_for_server(server.base, timeout=startup_timeout):
            tail = server.log_tail()
            _terminate(proc)
            log.close()
            die(f"self-hosted server did not become ready on {server.base}\n"
                f"--- server log ---\n{tail}", EXIT_UNREACHABLE)
        yield server
    finally:
        _terminate(proc)
        log.close()
        if not keep:
            _rmtree(data_dir)


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _rmtree(path: str) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #

DEV_USERNAME = "admin"
DEV_PASSWORD = "admin"


@dataclass
class Credentials:
    username: str
    password: str
    source: str


def credentials_for_self_hosted() -> Credentials:
    """The scratch server is booted with HCRM_DEV_ADMIN=1, so admin/admin exists."""
    return Credentials(
        username=os.environ.get("HCRM_ADMIN_USERNAME", DEV_USERNAME),
        password=os.environ.get("HCRM_ADMIN_PASSWORD", DEV_PASSWORD),
        source="self-hosted dev admin (override with HCRM_ADMIN_USERNAME/PASSWORD)",
    )


def credentials_for_external() -> Credentials:
    """Explicit env credentials only — no well-known default for a real server.

    PLAN.md P-3: a script must never assume `changeme`/`admin` against somebody
    else's deployment, and must never write a well-known password to one.
    """
    username = os.environ.get("HCRM_ADMIN_USERNAME") or os.environ.get("HCRM_ADMIN_EMAIL")
    password = os.environ.get("HCRM_ADMIN_PASSWORD")
    if not username or not password:
        die("targeting an existing server (--base/--force) requires explicit "
            "credentials: set HCRM_ADMIN_USERNAME and HCRM_ADMIN_PASSWORD. "
            "Well-known defaults are deliberately not used for external targets.",
            EXIT_USAGE)
    return Credentials(username=username, password=password,
                       source="environment (HCRM_ADMIN_USERNAME/PASSWORD)")


def bootstrap_admin(server_base: str, creds: Credentials,
                    verbose: bool = True) -> tuple[str, str, bool]:
    """Log in, satisfying a pending password change if the account is flagged.

    Returns (token, final_password, changed). The password is NEVER changed
    back: a successful run against a flagged account intentionally leaves the
    new random password active and the caller must report it (P-2).
    """
    import secrets

    status, payload, _ = call(server_base, "/api/auth/login", "POST",
                              body={"email": creds.username, "password": creds.password})
    if status != 200 or not isinstance(payload, dict) or "token" not in payload:
        die(f"cannot log in as {creds.username!r} on {server_base}: "
            f"HTTP {status} {detail_of(payload)} (credentials from: {creds.source})",
            EXIT_UNREACHABLE if status == 0 else EXIT_CHECKS_FAILED)

    token = payload["token"]
    user = payload.get("user") or {}
    if not user.get("must_change_password"):
        return token, creds.password, False

    new_password = "hcrm-" + secrets.token_hex(12)
    status, body, _ = call(server_base, "/api/auth/change-password", "POST", token=token,
                           body={"current_password": creds.password,
                                 "new_password": new_password})
    if status != 204:
        die(f"account is flagged must_change_password but the change failed: "
            f"HTTP {status} {detail_of(body)}", EXIT_CHECKS_FAILED)
    if verbose:
        print(f"  [..] account was flagged must_change_password; set a random "
              f"password to proceed (final state, NOT reverted): {new_password}")
    return token, new_password, True


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

class Reporter:
    """Check bookkeeping that cannot raise and cannot lose the exit code."""

    def __init__(self, title: str):
        self.title = title
        self.passed = 0
        self.failed: list[str] = []
        self.skipped: list[str] = []

    def section(self, label: str) -> None:
        print(f"== {label} ==")

    def check(self, label: str, condition: bool, extra: str = "") -> bool:
        if condition:
            self.passed += 1
            print(f"  [ OK ] {label} {extra}".rstrip())
        else:
            self.failed.append(label)
            print(f"  [FAIL] {label} {extra}".rstrip())
        return bool(condition)

    def fail(self, label: str, detail: str = "") -> None:
        self.check(label, False, detail)

    def skip(self, label: str, reason: str) -> None:
        self.skipped.append(label)
        print(f"  [SKIP] {label} — {reason}")

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self, noun: str = "CHECKS") -> int:
        print()
        print(f"{self.title}: {self.passed} passed, {len(self.failed)} failed, "
              f"{len(self.skipped)} skipped")
        if self.failed:
            print(f"{noun} FAILED:")
            for label in self.failed:
                print(f"  - {label}")
            return EXIT_CHECKS_FAILED
        print(f"{noun} PASSED")
        return EXIT_OK
