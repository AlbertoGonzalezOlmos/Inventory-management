"""Tests for authentication, sessions, and password management."""

from tests.conftest import ADMIN_PASSWORD_AFTER_CHANGE, auth, create_user, unique_email


def test_register_login_me(client):
    email = unique_email()
    r = client.post(
        "/api/auth/register",
        json={"name": "Alice", "email": email, "password": "password123"},
    )
    assert r.status_code == 201
    token = r.json()["token"]
    assert r.json()["user"]["role"] == "member"

    me = client.get("/api/auth/me", headers=auth(token))
    assert me.status_code == 200
    assert me.json()["email"] == email


def test_invalid_email_rejected(client):
    r = client.post(
        "/api/auth/register",
        json={"name": "X", "email": "not-an-email", "password": "password123"},
    )
    assert r.status_code == 422


def test_whitespace_only_name_rejected(client):
    r = client.post(
        "/api/auth/register",
        json={"name": "   ", "email": unique_email(), "password": "password123"},
    )
    assert r.status_code == 422


def test_duplicate_email_conflict(client):
    email = unique_email()
    body = {"name": "Dup", "email": email, "password": "password123"}
    assert client.post("/api/auth/register", json=body).status_code == 201
    assert client.post("/api/auth/register", json=body).status_code == 409
    # Case-insensitive duplicates are also rejected.
    body2 = {**body, "email": email.upper()}
    assert client.post("/api/auth/register", json=body2).status_code == 409


def test_wrong_password_login(client):
    email = unique_email()
    client.post("/api/auth/register", json={"name": "W", "email": email, "password": "password123"})
    r = client.post("/api/auth/login", json={"email": email, "password": "wrong-password"})
    assert r.status_code == 401


def test_logout_revokes_only_current_session(client):
    email = unique_email()
    client.post("/api/auth/register", json={"name": "L", "email": email, "password": "password123"})
    t1 = client.post("/api/auth/login", json={"email": email, "password": "password123"}).json()["token"]
    t2 = client.post("/api/auth/login", json={"email": email, "password": "password123"}).json()["token"]

    assert client.post("/api/auth/logout", headers=auth(t1)).status_code == 204
    assert client.get("/api/auth/me", headers=auth(t1)).status_code == 401
    # The other session of the same user must survive.
    assert client.get("/api/auth/me", headers=auth(t2)).status_code == 200


def test_change_password_flow(client):
    email = unique_email()
    client.post("/api/auth/register", json={"name": "P", "email": email, "password": "password123"})
    t1 = client.post("/api/auth/login", json={"email": email, "password": "password123"}).json()["token"]
    t2 = client.post("/api/auth/login", json={"email": email, "password": "password123"}).json()["token"]

    # Wrong current password
    r = client.post(
        "/api/auth/change-password",
        json={"current_password": "nope-nope", "new_password": "newpassword1"},
        headers=auth(t1),
    )
    assert r.status_code == 400

    # Correct change
    r = client.post(
        "/api/auth/change-password",
        json={"current_password": "password123", "new_password": "newpassword1"},
        headers=auth(t1),
    )
    assert r.status_code == 204

    # Old password no longer works, new one does
    assert client.post("/api/auth/login", json={"email": email, "password": "password123"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": email, "password": "newpassword1"}).status_code == 200
    # Other sessions revoked, the session that changed the password survives
    assert client.get("/api/auth/me", headers=auth(t2)).status_code == 401
    assert client.get("/api/auth/me", headers=auth(t1)).status_code == 200


def test_admin_password_reset(client, admin_token):
    member, member_token = create_user(client, admin_token, role="member")
    r = client.patch(
        f"/api/members/{member['id']}", json={"password": "resetpass99"}, headers=auth(admin_token)
    )
    assert r.status_code == 200
    # The reset revokes the member's existing sessions.
    assert client.get("/api/auth/me", headers=auth(member_token)).status_code == 401
    login = client.post(
        "/api/auth/login", json={"email": member["email"], "password": "resetpass99"}
    )
    assert login.status_code == 200
    # An admin-issued password is "temporary": the member must change it.
    assert login.json()["user"]["must_change_password"] is True
    new_token = login.json()["token"]
    assert client.get("/api/auth/me", headers=auth(new_token)).status_code == 200
    assert client.get("/api/items", headers=auth(new_token)).status_code == 403
    r = client.post(
        "/api/auth/change-password",
        json={"current_password": "resetpass99", "new_password": "memberpass1"},
        headers=auth(new_token),
    )
    assert r.status_code == 204
    assert client.get("/api/items", headers=auth(new_token)).status_code == 200


def test_self_password_reset_via_patch_rejected(client, admin_token):
    """Session-takeover regression: an admin must NOT be able to change their
    own password via PATCH. That path does not verify the current password,
    so allowing it (an earlier revision even spared the caller's session)
    would let a stolen session change the password and persist while locking
    out the real owner. Own-password changes must go through change-password.
    """
    users = client.get("/api/members", headers=auth(admin_token)).json()
    me = next(u for u in users if u["email"] == "admin@shop.local")

    r = client.patch(
        f"/api/members/{me['id']}", json={"password": "attackerpass1"}, headers=auth(admin_token)
    )
    assert r.status_code == 400
    assert "change-password" in r.json()["detail"]

    # Nothing changed: the session survives and the old password still works.
    assert client.get("/api/auth/me", headers=auth(admin_token)).status_code == 200
    assert client.post(
        "/api/auth/login",
        json={"email": "admin@shop.local", "password": ADMIN_PASSWORD_AFTER_CHANGE},
    ).status_code == 200
    assert client.post(
        "/api/auth/login", json={"email": "admin@shop.local", "password": "attackerpass1"}
    ).status_code == 401


def test_must_change_password_enforced(client):
    """The seeded default admin is blocked from the API until the password
    is changed (only me/logout/change-password are exempt)."""
    r = client.post(
        "/api/auth/login",
        json={"email": "admin@shop.local", "password": "changeme"},
    )
    assert r.status_code == 200
    assert r.json()["user"]["must_change_password"] is True
    token = r.json()["token"]

    assert client.get("/api/auth/me", headers=auth(token)).status_code == 200
    blocked = client.get("/api/items", headers=auth(token))
    assert blocked.status_code == 403
    assert "Password change required" in blocked.json()["detail"]
    assert client.post("/api/auth/logout", headers=auth(token)).status_code == 204

    # A second login gets a fresh token (logout revoked the first).
    token = client.post(
        "/api/auth/login",
        json={"email": "admin@shop.local", "password": "changeme"},
    ).json()["token"]
    r = client.post(
        "/api/auth/change-password",
        json={"current_password": "changeme", "new_password": "brand-new-pass1"},
        headers=auth(token),
    )
    assert r.status_code == 204
    # Unblocked, and the flag is cleared for future logins.
    assert client.get("/api/items", headers=auth(token)).status_code == 200
    login = client.post(
        "/api/auth/login",
        json={"email": "admin@shop.local", "password": "brand-new-pass1"},
    )
    assert login.json()["user"]["must_change_password"] is False


def test_tokens_stored_hashed(client):
    """Bearer tokens must not be persisted in plaintext: a leaked DB file
    must not yield usable sessions."""
    from sqlmodel import Session

    from app.database import engine
    from app.models import AuthToken
    from app.security import token_hash

    raw = client.post(
        "/api/auth/register",
        json={"name": "H", "email": "hashed@t.local", "password": "password123"},
    ).json()["token"]
    with Session(engine) as s:
        assert s.get(AuthToken, token_hash(raw)) is not None  # keyed by hash
        assert s.get(AuthToken, raw) is None  # raw secret not stored


def test_verify_password_tolerates_malformed_hashes():
    from app.security import verify_password

    assert verify_password("x", "garbage") is False
    assert verify_password("x", "pbkdf2_sha256$NaN$salt$digest") is False
    assert verify_password("x", "other_algo$1$s$d") is False
