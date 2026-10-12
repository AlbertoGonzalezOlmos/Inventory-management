"""Server-enforced kiosk logoff (Task 4): the lazy deadline on AuthToken.

The security property: once a countdown is armed, the token dies at its
deadline EVEN IF THE BROWSER NEVER COMES BACK — get_current_user enforces it
on the token's next use anywhere (reject-and-delete, uniform 401). The SPA's
countdown is presentation; this is the truth.

No test sleeps: expiry is exercised either by monkeypatching the grace to 0
(the boundary is inclusive — deadline == now already kills) or by writing a
past deadline directly into the row.
"""

import subprocess
import sys
from datetime import datetime, timedelta

from sqlmodel import Session

from app.database import engine
from app.models import AuthToken
from app.security import token_hash
from tests.conftest import USER_PASSWORD_AFTER_CHANGE, auth, create_user

REPO_ROOT = __file__.rsplit("/tests/", 1)[0]


def _second_session(client, email, password=USER_PASSWORD_AFTER_CHANGE):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _get_token_row(raw_token: str) -> AuthToken | None:
    with Session(engine) as session:
        return session.get(AuthToken, token_hash(raw_token))


def _schedule(client, token):
    return client.post("/api/auth/logoff-schedule", headers=auth(token))


def _cancel(client, token):
    return client.post("/api/auth/logoff-cancel", headers=auth(token))


def _grace(monkeypatch, seconds):
    # The module attribute at call time — the router reads
    # security.LOGOFF_GRACE_SECONDS precisely so this override works.
    from app import security
    monkeypatch.setattr(security, "LOGOFF_GRACE_SECONDS", seconds)


def _deadline_from_response(body: dict) -> datetime:
    """The response's deadline arrives as a JSON ISO string."""
    return datetime.fromisoformat(body["logoff_deadline"])


# --- gates ---------------------------------------------------------------------


def test_unauthenticated_is_401(client):
    assert _schedule(client, None or "").status_code == 401
    assert _cancel(client, None or "").status_code == 401


def test_flagged_account_is_403(client, admin_token):
    from tests.test_cart import _flagged_token
    token = _flagged_token(client, admin_token)
    assert _schedule(client, token).status_code == 403
    assert _cancel(client, token).status_code == 403
    # The escape hatch is unaffected: logout stays exempt.
    assert client.post("/api/auth/logout", headers=auth(token)).status_code == 204


# --- schedule ------------------------------------------------------------------


def test_schedule_arms_only_the_presented_token(client, admin_token):
    user, token_a = create_user(client, admin_token)
    token_b = _second_session(client, user["email"])

    r = _schedule(client, token_a)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"logoff_deadline", "grace_seconds"}
    assert body["grace_seconds"] == 60  # the default
    from app.models import utcnow
    deadline = _deadline_from_response(body)
    delta = (utcnow() + timedelta(seconds=60) - deadline).total_seconds()
    assert abs(delta) < 5, deadline  # server-computed now+60

    row_a = _get_token_row(token_a)
    row_b = _get_token_row(token_b)
    assert row_a.logoff_deadline is not None
    assert row_b.logoff_deadline is None  # per-token, like logout
    # Both sessions still work inside the window.
    assert client.get("/api/auth/me", headers=auth(token_a)).status_code == 200
    assert client.get("/api/auth/me", headers=auth(token_b)).status_code == 200


def test_schedule_restarts_on_reschedule(client, admin_token, monkeypatch):
    user, token = create_user(client, admin_token)
    _grace(monkeypatch, 5)
    first = _deadline_from_response(_schedule(client, token).json())
    _grace(monkeypatch, 300)
    second = _deadline_from_response(_schedule(client, token).json())
    assert second > first  # overwrite = restart, never a 409
    assert _get_token_row(token).logoff_deadline == second


def test_deadline_is_clamped_to_token_expiry(client, admin_token, monkeypatch):
    user, token = create_user(client, admin_token)
    _grace(monkeypatch, 10**9)  # an absurd grace must not arm past the TTL
    r = _schedule(client, token)
    assert r.status_code == 200, r.text
    row = _get_token_row(token)
    assert row.logoff_deadline == row.expires_at


# --- cancel --------------------------------------------------------------------


def test_cancel_disarms_and_is_idempotent(client, admin_token, monkeypatch):
    user, token = create_user(client, admin_token)
    _grace(monkeypatch, 1)
    assert _schedule(client, token).status_code == 200

    assert _cancel(client, token).status_code == 204
    assert _get_token_row(token).logoff_deadline is None
    # Cancel — not time — decides: advance the checker's clock past the
    # would-be deadline and the token is still alive (no sleep: deps.utcnow
    # is the only clock the deadline check reads).
    import app.deps
    from app.models import utcnow as real_utcnow
    monkeypatch.setattr(
        app.deps, "utcnow", lambda: real_utcnow() + timedelta(seconds=60))
    assert client.get("/api/auth/me", headers=auth(token)).status_code == 200
    # Idempotent: cancelling an unarmed token is the desired end state.
    assert _cancel(client, token).status_code == 204


# --- the lazy expiry (the security property) -------------------------------------


def test_expired_deadline_401s_deletes_row_uniform_detail(client, admin_token):
    user, token = create_user(client, admin_token)
    _schedule(client, token)  # default 60s: still armed, not passed

    # Push the deadline into the past directly (no sleep, no monkeypatch).
    from app.models import utcnow
    with Session(engine) as session:
        row = session.get(AuthToken, token_hash(token))
        row.logoff_deadline = utcnow() - timedelta(seconds=1)
        session.add(row)
        session.commit()

    r = client.get("/api/auth/me", headers=auth(token))
    assert r.status_code == 401
    # Uniform: indistinguishable from a naturally expired token.
    assert r.json()["detail"] == "Token invalid or expired"
    # Reject-AND-delete: the row is gone, so the kill is durable.
    assert _get_token_row(token) is None
    assert client.get("/api/auth/me", headers=auth(token)).status_code == 401


def test_zero_grace_boundary_is_inclusive(client, admin_token, monkeypatch):
    """grace 0 arms deadline == now; the very next request is already dead —
    the countdown reaching zero IS the logoff moment (>=, unlike expires_at's
    strict <)."""
    user, token = create_user(client, admin_token)
    _grace(monkeypatch, 0)
    r = _schedule(client, token)
    assert r.status_code == 200, r.text
    assert client.get("/api/auth/me", headers=auth(token)).status_code == 401
    assert _get_token_row(token) is None


def test_deadline_equal_to_now_is_already_dead(client, admin_token, monkeypatch):
    """The inclusive boundary itself, pinned with a frozen clock.

    The zero-grace test above cannot distinguish >= from > (the next real
    request's clock is always strictly later); this one freezes deps.utcnow
    at EXACTLY the stored deadline: >= kills the token, > would let it
    live. If a refactor ever flips the comparison, this is the test that
    fails — the spec's "deadline == now boundary (<=)" pin.
    """
    user, token = create_user(client, admin_token)
    _grace(monkeypatch, 30)
    assert _schedule(client, token).status_code == 200
    frozen = _get_token_row(token).logoff_deadline
    assert frozen is not None

    import app.deps
    monkeypatch.setattr(app.deps, "utcnow", lambda: frozen)
    r = client.get("/api/auth/me", headers=auth(token))
    assert r.status_code == 401, "deadline == now must already be dead (>=)"
    assert r.json()["detail"] == "Token invalid or expired"
    assert _get_token_row(token) is None


def test_expiry_kills_exempt_paths_too(client, admin_token, monkeypatch):
    user, token = create_user(client, admin_token)
    _grace(monkeypatch, 0)
    _schedule(client, token)
    for path in ("/api/auth/me", "/api/auth/logout", "/api/auth/change-password"):
        r = client.post(path, headers=auth(token)) if "change" in path or "logout" in path \
            else client.get(path, headers=auth(token))
        assert r.status_code == 401, path


def test_per_token_isolation(client, admin_token, monkeypatch):
    user, token_a = create_user(client, admin_token)
    token_b = _second_session(client, user["email"])
    _grace(monkeypatch, 0)
    _schedule(client, token_a)

    assert client.get("/api/auth/me", headers=auth(token_a)).status_code == 401
    assert client.get("/api/auth/me", headers=auth(token_b)).status_code == 200
    assert _get_token_row(token_b).logoff_deadline is None


def test_cancel_after_expiry_is_401(client, admin_token, monkeypatch):
    # A dead session cannot be resurrected.
    user, token = create_user(client, admin_token)
    _grace(monkeypatch, 0)
    _schedule(client, token)
    assert client.get("/api/auth/me", headers=auth(token)).status_code == 401
    assert _cancel(client, token).status_code == 401


# --- interactions with the existing token machinery --------------------------------


def test_logout_takes_the_armed_row_with_it(client, admin_token):
    user, token = create_user(client, admin_token)
    _schedule(client, token)
    assert client.post("/api/auth/logout", headers=auth(token)).status_code == 204
    assert _get_token_row(token) is None


def test_change_password_takes_other_armed_rows(client, admin_token):
    user, token_a = create_user(client, admin_token)
    token_b = _second_session(client, user["email"])
    _schedule(client, token_b)  # the OTHER session is armed

    r = client.post(
        "/api/auth/change-password",
        json={"current_password": USER_PASSWORD_AFTER_CHANGE,
              "new_password": "another-password-1"},
        headers=auth(token_a),
    )
    assert r.status_code == 204, r.text
    # Existing revocation: every other session's row (armed deadline and all)
    # is gone; the current session keeps its (unarmed) row.
    assert _get_token_row(token_b) is None
    assert _get_token_row(token_a) is not None
    assert _get_token_row(token_a).logoff_deadline is None


def test_fresh_logins_are_unarmed(client, admin_token):
    user, token = create_user(client, admin_token)
    fresh = _second_session(client, user["email"])
    assert _get_token_row(fresh).logoff_deadline is None  # NULL default


# --- the grace constant wiring --------------------------------------------------------


def test_grace_env_override_and_clamp():
    """HCRM_LOGOFF_GRACE_SECONDS is read at import and clamped >= 1 (a zero
    env value must not make scheduling an instant self-logout — tests that
    need zero monkeypatch the module attribute instead)."""
    for env_value, expected in (("0", 1), ("5", 5), ("120", 120), ("-9", 1)):
        proc = subprocess.run(
            [sys.executable, "-c",
             "import os; os.environ['HCRM_LOGOFF_GRACE_SECONDS'] = "
             f"{env_value!r}; "
             "from app.security import LOGOFF_GRACE_SECONDS; "
             "print(LOGOFF_GRACE_SECONDS)"],
            capture_output=True, text=True, timeout=60, cwd=REPO_ROOT,
            env={"PATH": "/usr/bin:/bin", "HCRM_DATA_DIR": "/tmp/hcrm-grace-test"},
        )
        assert proc.returncode == 0, proc.stderr
        assert int(proc.stdout.strip()) == expected, (env_value, proc.stdout)
