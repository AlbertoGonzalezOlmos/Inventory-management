"""W5.3 — the script contract, as tests.

The two verification scripts had three defects that all failed in the direction
of "looks fine": the load phase's return value was discarded so a failing load
exited 0 (finding C); an unconditional password restore made a *passing* run
exit 1 (D) and re-armed the well-known `changeme` default with
must_change_password cleared (E); and the smoke test died with an uncaught
TypeError instead of printing [FAIL], leaving the admin password changed (N4).

One server is booted for the whole module (dev admin OFF, so the seeded
admin@shop.local/changeme is flagged and the bootstrap path is exercised) and
every assertion below is an exit code or a line of output — the same evidence
the PLAN's gates asked for, now permanent.
"""

import hashlib
import importlib.util
import json
import os
import re
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import _hcrm  # noqa: E402


def _load_script(name: str):
    path = os.path.join(REPO_ROOT, "scripts", f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"script_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


load_test = _load_script("load_test")
smoke_test = _load_script("smoke_test")


def _main(module, argv) -> int:
    """Run a script's main(); normalise die()'s SystemExit to its code."""
    try:
        return module.main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


@pytest.fixture(scope="module")
def server():
    """One server for the module: dev admin OFF (so the seeded
    admin@shop.local/changeme is flagged and the bootstrap path is exercised)
    and no embedding model (so the smoke test's degraded path is exercised)."""
    with _hcrm.self_host(model=_hcrm.NO_MODEL, pbkdf2_iterations=1000,
                         extra_env={"HCRM_DEV_ADMIN": "0"}) as srv:
        yield srv


@pytest.fixture()
def flagged_admin(server):
    """Reset the server's admin to a KNOWN flagged state before each test.

    Without this the module shares mutable credential state and the tests become
    order-dependent — running the file in reverse put the "second consecutive
    run" test before the first one and 6 tests failed. Each test now establishes
    its own precondition instead of inheriting the previous test's password.

    Writing straight to the scratch database is deliberate: W4.1 makes it
    impossible to reset an account to `changeme` through the API (that was the
    hole), so a test that needs a flagged-default admin has to plant one.
    """
    _reset_flagged_admin(server)
    return {"username": "admin@shop.local", "password": "changeme"}


def _reset_flagged_admin(server) -> None:
    """Plant a flagged admin@shop.local/changeme in the scratch database."""
    import sqlite3

    from app.security import hash_password

    db = os.path.join(server.data_dir, "hcrm.db")
    con = sqlite3.connect(db, timeout=15)
    try:
        con.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 1 "
            "WHERE email = 'admin@shop.local'", (hash_password("changeme"),))
        con.execute("DELETE FROM auth_tokens")
        con.commit()
    finally:
        con.close()
    # Preconditions asserted, not assumed.
    assert _hcrm.call(server.base, "/api/auth/login", "POST",
                      body={"email": "admin@shop.local",
                            "password": "changeme"})[0] == 200


@pytest.fixture()
def admin_env(flagged_admin, monkeypatch):
    monkeypatch.setenv("HCRM_ADMIN_USERNAME", flagged_admin["username"])
    monkeypatch.setenv("HCRM_ADMIN_PASSWORD", flagged_admin["password"])
    return flagged_admin


def _login(base, password):
    status, payload, _ = _hcrm.call(base, "/api/auth/login", "POST",
                                    body={"email": "admin@shop.local",
                                          "password": password})
    return status, payload


def test_usage_external_base_requires_force():
    assert _main(load_test, ["--base", "http://127.0.0.1:1"]) == 3
    assert _main(smoke_test, ["--base", "http://127.0.0.1:1"]) == 3
    # ...and the self-host-only flags are rejected for an external target.
    assert _main(load_test, ["--base", "http://127.0.0.1:1", "--force",
                             "--fast-hashing"]) == 3
    assert _main(smoke_test, ["--base", "http://127.0.0.1:1", "--force",
                              "--keep"]) == 3


def test_external_target_needs_explicit_credentials(server, monkeypatch):
    monkeypatch.delenv("HCRM_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("HCRM_ADMIN_PASSWORD", raising=False)
    # No well-known default is used against somebody else's server (P-3).
    assert _main(load_test, ["--base", server.base, "--force"]) == 3


def test_unreachable_target_is_not_a_pass(monkeypatch):
    """Exit 2 ("unreachable"), never 0 — a down server is not a passing run."""
    monkeypatch.setenv("HCRM_ADMIN_USERNAME", "admin@shop.local")
    monkeypatch.setenv("HCRM_ADMIN_PASSWORD", "whatever-it-takes")
    assert _hcrm.healthz("http://127.0.0.1:9", timeout=2.0) is None
    assert _main(load_test, ["--base", "http://127.0.0.1:9", "--force"]) == 2
    assert _main(smoke_test, ["--base", "http://127.0.0.1:9", "--force"]) == 2


def test_first_run_reports_final_state_and_second_run_reuses_it(server, admin_env,
                                                                 monkeypatch, capsys):
    """Findings D + E in one deterministic test.

    Run 1 hits a flagged admin, changes the password to proceed, exits 0 and
    reports the final state. E: `changeme` must be dead afterwards — the old
    restore step re-armed it with must_change_password cleared. D: run 2, fed
    run 1's password, must also exit 0 — the old unconditional restore made it
    print "LOAD TEST PASSED" and exit 1.
    """
    code = _main(load_test, ["--base", server.base, "--force",
                             "--logins", "12", "--reads", "6", "--json"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "LOAD TEST PASSED" in out
    assert "FINAL STATE" in out and "NOT reverted" in out

    summary = json.loads(out[out.index('{\n  "target"'):])
    assert summary["password_changed"] is True
    new_password = summary["final_password"]
    assert new_password and new_password != "changeme"

    assert _login(server.base, "changeme")[0] == 401           # E
    assert _login(server.base, new_password)[0] == 200

    monkeypatch.setenv("HCRM_ADMIN_PASSWORD", new_password)
    code2 = _main(load_test, ["--base", server.base, "--force",
                              "--logins", "12", "--reads", "6"])
    out2 = capsys.readouterr().out
    assert "LOAD TEST PASSED" in out2, out2
    assert code2 == 0, (code2, out2)                            # D
    # Still no restore anywhere: the default stays dead.
    assert _login(server.base, "changeme")[0] == 401


def test_load_test_failure_feeds_the_exit_code(server, admin_env, monkeypatch, capsys):
    """Finding C: run_load()'s return value used to be discarded, so a failing
    load phase still exited 0."""
    monkeypatch.setattr(load_test, "run_load", lambda *a, **k: False)
    code = _main(load_test, ["--base", server.base, "--force",
                             "--logins", "2", "--reads", "1"])
    out = capsys.readouterr().out
    assert code == 1, (code, out)
    assert "FAILED" in out


def test_smoke_reports_instead_of_raising(server, admin_env, capsys):
    """Finding N4: with the model unavailable the old script died with
    `TypeError: string indices must be integers`, printed no [FAIL] line, and
    left the admin password changed with no restore."""
    code = _main(smoke_test, ["--base", server.base, "--force"])
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "Traceback" not in out, out[-2000:]
    assert "[FAIL]" in out and "[SKIP]" in out, out[-2000:]
    assert code == 1, (code, out[-1500:])          # degraded coverage must fail
    assert "semantic coverage was exercised" in out


def test_smoke_allow_degraded_passes(server, admin_env, capsys):
    code = _main(smoke_test, ["--base", server.base, "--force", "--allow-degraded"])
    out = capsys.readouterr().out
    assert code == 0, out[-1500:]
    assert "SMOKE TEST PASSED" in out
    assert "rejected with 503" in out               # outage write policy asserted


def test_scripts_never_write_the_repo_data_dir(server, admin_env):
    """Ground rule 3, mechanically: running both scripts must not touch ./data."""
    live = os.path.join(REPO_ROOT, "data", "hcrm.db")
    if not os.path.exists(live):
        pytest.skip("no ./data/hcrm.db in this checkout")
    before = hashlib.md5(open(live, "rb").read()).hexdigest()
    before_stat = os.stat(live).st_mtime_ns

    # Each script bootstraps from a freshly planted flagged admin: the load run
    # changes the password (and never restores it), so the smoke run needs its
    # own precondition rather than inheriting the previous run's state.
    _reset_flagged_admin(server)
    assert _main(load_test, ["--base", server.base, "--force",
                             "--logins", "4", "--reads", "2"]) == 0
    _reset_flagged_admin(server)
    assert _main(smoke_test, ["--base", server.base, "--force",
                              "--allow-degraded"]) == 0

    after = hashlib.md5(open(live, "rb").read()).hexdigest()
    assert after == before, "./data/hcrm.db was modified by a verification script"
    assert os.stat(live).st_mtime_ns == before_stat


def test_smoke_cleanup_no_longer_deletes_arbitrary_skus():
    """Eng2 #5: the old cleanup deleted any item whose SKU was exactly "X"."""
    source = open(os.path.join(REPO_ROOT, "scripts", "smoke_test.py"),
                  encoding="utf-8").read()
    assert 'sku"] == "X"' not in source
    assert 'CLEANUP_PREFIX = "EX-TST-"' in source


def test_scripts_have_no_well_known_credential_fallback():
    """The restore-to-default step is gone, and so is any hard-coded default
    credential: the external path requires the environment (P-3)."""
    for name in ("load_test", "smoke_test"):
        source = open(os.path.join(REPO_ROOT, "scripts", f"{name}.py"),
                      encoding="utf-8").read()
        assert "NOT reverted" in source, name                 # contract documented
        assert "ADMIN_DEFAULT_PASSWORD" not in source, name   # old constant gone
        # No os.environ.get("HCRM_ADMIN_PASSWORD", "<fallback>") anywhere.
        assert 'HCRM_ADMIN_PASSWORD", "' not in source, name
        assert "HCRM_ADMIN_PASSWORD', '" not in source, name
        assert "credentials_for_external" in source, name
