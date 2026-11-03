"""Hardware-free end-to-end: the run.sh scanner chain, minus the grab.

Boots a real scratch server (``scripts/_hcrm.self_host`` — bogus embed
model, temp data dir, free loopback port), then runs the REAL
``scripts/scanner_daemon.py`` in ``--register-only`` mode: registration +
config + the one-click offer file, and **no device touch**, so the test is
safe on a host with the M-10 attached (the suite must never EVIOCGRAB the
operator's scanner). It then walks exactly the chain the "no scanner" pill
drives: loopback ``POST /api/scanner/pair/auto`` consumes the 0600 offer,
the minted token polls ``GET /api/scanner/events``, and a second daemon run
with the saved config re-registers nothing and does not resurrect a
consumed offer.
"""

import json
import os
import sqlite3
import stat
import subprocess
import sys
import urllib.request

from scripts._hcrm import self_host

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _request(method, base, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base.rstrip("/") + path, method=method, data=data,
        headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read()
        return response.status, json.loads(raw) if raw else {}


# Env vars that would silently change where the daemon/server resolve the
# pairing state — a developer with one exported must not break the parity
# this e2e exists to pin.
_STRIP_ENV = ("HCRM_SCANNER_DAEMON", "HCRM_SCANNER_PAIRING_FILE",
              "HCRM_SCANNER_REGISTER_WAIT", "HCRM_SCANNER_CONFIG",
              "HCRM_ALLOW_INSECURE_BIND", "HCRM_SCRATCH")


def _run_daemon(server):
    """The real daemon, exactly as run.sh starts it: same HCRM_DATA_DIR as
    the server (so BOTH sides resolve the same default pairing-file path —
    the parity the one-click flow depends on), loopback forward, retry
    budget, and --register-only so no device is ever touched."""
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    return subprocess.run(
        [sys.executable, "scripts/scanner_daemon.py",
         "--config", os.path.join(server.data_dir, "scanner.json"),
         "--forward", server.base,
         "--register-wait", "30",
         "--register-only"],
        capture_output=True, text=True, timeout=90, cwd=REPO_ROOT,
        env={**env, "HCRM_DATA_DIR": server.data_dir},
    )


def test_register_only_pairs_the_kiosk_browser_end_to_end(monkeypatch):
    # The self-hosted server inherits OUR env too — scrub the same vars so
    # both subprocesses resolve the default pairing path the way run.sh
    # would on a clean host.
    for var in _STRIP_ENV:
        monkeypatch.delenv(var, raising=False)
    with self_host(pbkdf2_iterations=1000) as server:
        # The default offer path both sides resolve from HCRM_DATA_DIR.
        offer = os.path.join(server.data_dir, "scanner-pairing.json")

        # First boot: config absent -> the daemon self-registers and writes
        # the single-use offer file, then exits without grabbing.
        proc = _run_daemon(server)
        assert proc.returncode == 0, proc.stderr[-500:]
        assert os.path.exists(offer), proc.stdout + proc.stderr
        assert stat.S_IMODE(os.stat(offer).st_mode) == 0o600
        with open(offer, encoding="utf-8") as fh:
            saved = json.load(fh)
        assert set(saved) == {"terminal_id", "pairing_code"}
        assert len(saved["pairing_code"]) == 8  # the Crockford shape

        # The pill's click: one-shot pairing from loopback, JSON content
        # type (the CSRF gate), offer consumed on success.
        status, payload = _request("POST", server.base, "/api/scanner/pair/auto",
                                   body={})
        assert status == 200, payload
        token = payload["token"]
        assert token
        assert not os.path.exists(offer)

        # The paired browser polls the feed with its terminal token.
        status, payload = _request(
            "GET", server.base, "/api/scanner/events?after=0",
            headers={"X-Scanner-Token": token})
        assert status == 200, payload

        # Second boot: the config exists, so no re-registration, and the
        # consumed offer is NOT regenerated.
        proc = _run_daemon(server)
        assert proc.returncode == 0, proc.stderr[-500:]
        assert not os.path.exists(offer)

        # Exactly one terminal was ever registered.
        db = os.path.join(server.data_dir, "hcrm.db")
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            terminals = con.execute(
                "SELECT COUNT(*) FROM terminals").fetchone()[0]
        finally:
            con.close()
        assert terminals == 1
