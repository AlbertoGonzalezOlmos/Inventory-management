"""Regression tests for the privilege model (the critical findings)."""

import os
import sqlite3

import pytest

from tests.conftest import auth, create_user, get_db_path


def _normalize_to_single_admin(client, admin_token):
    users = client.get("/api/members", headers=auth(admin_token)).json()
    admins = [u for u in users if u["role"] == "admin"]
    for extra in admins[1:]:
        r = client.patch(
            f"/api/members/{extra['id']}", json={"role": "member"}, headers=auth(admin_token)
        )
        assert r.status_code == 200
    return admins[0]


def _get_seed_admin_id(client, admin_token):
    users = client.get("/api/members", headers=auth(admin_token)).json()
    return next(u for u in users if u["email"] == "admin@shop.local")["id"]


def test_staff_cannot_modify_admin_accounts(client, admin_token):
    _, staff_token = create_user(client, admin_token, role="staff")
    admin_id = _get_seed_admin_id(client, admin_token)

    r = client.patch(f"/api/members/{admin_id}", json={"role": "member"}, headers=auth(staff_token))
    assert r.status_code == 403

    r = client.patch(f"/api/members/{admin_id}", json={"name": "Hacked"}, headers=auth(staff_token))
    assert r.status_code == 403


def test_staff_cannot_grant_admin_role(client, admin_token):
    _, staff_token = create_user(client, admin_token, role="staff")
    member, _ = create_user(client, admin_token, role="member")

    r = client.post(
        "/api/members",
        json={"name": "X", "email": "x-staff-admin@test.local", "password": "password123", "role": "admin"},
        headers=auth(staff_token),
    )
    assert r.status_code == 403

    r = client.patch(f"/api/members/{member['id']}", json={"role": "admin"}, headers=auth(staff_token))
    assert r.status_code == 403


def test_staff_cannot_reset_passwords(client, admin_token):
    _, staff_token = create_user(client, admin_token, role="staff")
    member, _ = create_user(client, admin_token, role="member")

    r = client.patch(f"/api/members/{member['id']}", json={"password": "newpassword1"}, headers=auth(staff_token))
    assert r.status_code == 403


def test_admin_can_grant_admin_role(client, admin_token):
    member, _ = create_user(client, admin_token, role="member")
    r = client.patch(f"/api/members/{member['id']}", json={"role": "admin"}, headers=auth(admin_token))
    assert r.status_code == 200
    assert r.json()["role"] == "admin"
    # restore
    client.patch(f"/api/members/{member['id']}", json={"role": "member"}, headers=auth(admin_token))


def test_last_admin_cannot_be_demoted(client, admin_token):
    last_admin = _normalize_to_single_admin(client, admin_token)
    r = client.patch(
        f"/api/members/{last_admin['id']}", json={"role": "member"}, headers=auth(admin_token)
    )
    assert r.status_code == 400
    assert "last admin" in r.json()["detail"].lower()
    # restore
    client.patch(f"/api/members/{last_admin['id']}", json={"role": "admin"}, headers=auth(admin_token))


def test_db_trigger_blocks_last_admin_removal_at_sql_level(client, admin_token):
    """Defense in depth: even bypassing the API entirely, the database itself
    refuses to leave zero admins (the TOCTOU backstop)."""
    _normalize_to_single_admin(client, admin_token)

    db = get_db_path()
    con = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="last admin"):
            con.execute("UPDATE users SET role='member' WHERE role='admin'")
        with pytest.raises(sqlite3.IntegrityError, match="last admin"):
            con.execute("DELETE FROM users WHERE role='admin'")
    finally:
        con.close()

    # The surviving admin is untouched.
    con = sqlite3.connect(db)
    try:
        count = con.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
        assert count == 1
    finally:
        con.close()


def test_member_cannot_use_member_management(client):
    reg = client.post(
        "/api/auth/register",
        json={"name": "Plain", "email": "plain-member@test.local", "password": "password123"},
    )
    token = reg.json()["token"]
    assert client.get("/api/members", headers=auth(token)).status_code == 403
    assert client.patch("/api/members/1", json={"role": "staff"}, headers=auth(token)).status_code == 403
    assert client.delete("/api/members/1", headers=auth(token)).status_code == 403
