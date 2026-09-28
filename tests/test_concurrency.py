"""Regression tests for the last-admin TOCTOU race (concurrent demotions and
deletes across two sessions).

Guarantees asserted:
- the number of admins never drops below one (the DB triggers are the
  race-free backstop: SQLite serialises writers, and each write transaction
  sees the latest committed state);
- the losing request gets a clean 4xx (400 from the trigger mapping, 401/403
  if authn/authz already saw the change) — never a 500 and never a
  "database is locked" error.

These tests would fail if someone reintroduces a global BEGIN IMMEDIATE
hook (finding 1 of the remediation review: unauthenticated DoS via
"database is locked" 500s) or removes the triggers.
"""

import threading

from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import auth, create_user

ROUNDS = 5


def _concurrently(fn_a, fn_b):
    """Run two zero-arg callables at the same time; return their results."""
    results = {}
    barrier = threading.Barrier(2)

    def run(key, fn):
        try:
            results[key] = fn(barrier)
        except Exception as exc:  # pragma: no cover - surfaced as a failure
            results[key] = ("EXC", repr(exc))

    threads = [
        threading.Thread(target=run, args=("a", fn_a)),
        threading.Thread(target=run, args=("b", fn_b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results["a"], results["b"]


def _leave_exactly_two_admins(client, admin_token, keep_emails):
    """Demote every admin except the two under test (fresh DB: just the seed)."""
    users = client.get("/api/members", headers=auth(admin_token)).json()
    for u in users:
        if u["role"] == "admin" and u["email"] not in keep_emails:
            r = client.patch(
                f"/api/members/{u['id']}", json={"role": "staff"}, headers=auth(admin_token)
            )
            assert r.status_code == 200, r.text


def _admin_count(client, admin_token):
    users = client.get("/api/members", headers=auth(admin_token)).json()
    return sum(1 for u in users if u["role"] == "admin")


def _assert_clean_result(result):
    """Loser outcomes: 400 (trigger), 401/403 (authn/authz saw the change).
    Never 500, never 'database is locked'."""
    status = result[0]
    assert status != 500, result
    if status >= 400:
        assert "locked" not in str(result[1]).lower(), result


def test_concurrent_cross_demotion_never_leaves_zero_admins(client, admin_token):
    actor_token = admin_token  # an admin that can create users; after each
    # round the surviving racer takes over (the seed admin gets demoted to
    # leave exactly two admins for the race).
    for round_no in range(ROUNDS):
        a, a_token = create_user(
            client, actor_token, role="admin", email=f"race-demote-a{round_no}@t.local"
        )
        b, b_token = create_user(
            client, actor_token, role="admin", email=f"race-demote-b{round_no}@t.local"
        )
        _leave_exactly_two_admins(client, actor_token, {a["email"], b["email"]})

        def demote(token, target_id):
            def go(barrier):
                # Own TestClient -> own DB session; barrier maximises overlap.
                with TestClient(app) as c:
                    barrier.wait()
                    r = c.patch(
                        f"/api/members/{target_id}",
                        json={"role": "member"},
                        headers=auth(token),
                    )
                    return r.status_code, r.text
            return go

        sa, sb = _concurrently(demote(a_token, b["id"]), demote(b_token, a["id"]))
        _assert_clean_result(sa)
        _assert_clean_result(sb)
        # Exactly one demotion can win; the other must be a clean 4xx.
        statuses = sorted([sa[0], sb[0]])
        assert statuses[0] == 200, (round_no, sa, sb)
        assert statuses[1] in (400, 401, 403), (round_no, sa, sb)
        assert _admin_count(client, actor_token) == 1, (round_no, sa, sb)
        # The survivor administers the next round.
        survivor = next(
            u["email"]
            for u in client.get("/api/members", headers=auth(actor_token)).json()
            if u["role"] == "admin"
        )
        actor_token = client.post(
            "/api/auth/login", json={"email": survivor, "password": "password123"}
        ).json()["token"]


def test_concurrent_cross_delete_never_leaves_zero_admins(client, admin_token):
    actor_token = admin_token
    for round_no in range(3):
        a, a_token = create_user(
            client, actor_token, role="admin", email=f"race-del-a{round_no}@t.local"
        )
        b, b_token = create_user(
            client, actor_token, role="admin", email=f"race-del-b{round_no}@t.local"
        )
        _leave_exactly_two_admins(client, actor_token, {a["email"], b["email"]})

        def delete(token, target_id):
            def go(barrier):
                with TestClient(app) as c:
                    barrier.wait()
                    r = c.delete(f"/api/members/{target_id}", headers=auth(token))
                    return r.status_code, r.text
            return go

        sa, sb = _concurrently(delete(a_token, b["id"]), delete(b_token, a["id"]))
        _assert_clean_result(sa)
        _assert_clean_result(sb)
        statuses = sorted([sa[0], sb[0]])
        assert statuses[0] == 204, (round_no, sa, sb)  # DELETE success is 204
        # Loser: 400 (trigger) or 401 (the actor's own account was deleted).
        assert statuses[1] in (400, 401, 403), (round_no, sa, sb)
        assert _admin_count(client, actor_token) == 1, (round_no, sa, sb)
        # The survivor administers the next round.
        survivor = next(
            u["email"]
            for u in client.get("/api/members", headers=auth(actor_token)).json()
            if u["role"] == "admin"
        )
        actor_token = client.post(
            "/api/auth/login", json={"email": survivor, "password": "password123"}
        ).json()["token"]
