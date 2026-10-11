"""Task B — phone + email change (the profile feature's identity/contact layer).

The security split (coordinator decisions 3-4):
- PHONE is contact info: session-only, no password, ""/null clears,
  no-op is an idempotent 200 (nothing to protect).
- EMAIL is the login identifier — the takeover surface: current password
  required (verified BEFORE the taken-email 409, so the enumeration oracle
  only serves credential-holders), no-op 400 (a no-op must never revoke
  sessions), every OTHER session revoked (change-password parity).
- The badge is NEVER touched by an email change: derivation keys on
  user_id (Task A) — pinned here at the endpoint level.
"""

from sqlmodel import Session, select

from app.database import engine
from app.models import AuthToken, User
from app.schemas import normalise_phone
from app.security import token_hash
from tests.conftest import USER_PASSWORD_AFTER_CHANGE, auth, create_user, unique_email


def _db_user(email):
    with Session(engine) as session:
        return session.exec(select(User).where(User.email == email)).first()


def _login(client, email, password=USER_PASSWORD_AFTER_CHANGE):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


# --- phone: normalisation ------------------------------------------------------


def test_phone_normalisation_table():
    # The pure helper: valid forms normalise; separators vanish; + kept.
    assert normalise_phone("+34 600 123 456") == "+34600123456"
    assert normalise_phone("(555) 123-4567") == "5551234567"
    assert normalise_phone("  +34-944-123-456 ") == "+34944123456"
    assert normalise_phone("600123456") == "600123456"
    assert normalise_phone("") is None
    assert normalise_phone(None) is None
    # Invalid shapes raise (→ 422 at the endpoints).
    import pytest
    for bad in ("CALL-ME", "12345", "1" * 16, "++555123", "555+1234",
                "12 34a", "+"):
        # ("-" alone strips to empty and therefore CLEARS, like "".)
        with pytest.raises(ValueError):
            normalise_phone(bad)
        with pytest.raises(ValueError):
            normalise_phone(bad)


def test_change_phone_happy_path_and_me(client, admin_token):
    user, token = create_user(client, admin_token)
    r = client.post("/api/auth/change-phone",
                    json={"phone": "+34 600 123 456"}, headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["phone"] == "+34600123456"
    me = client.get("/api/auth/me", headers=auth(token))
    assert me.json()["phone"] == "+34600123456"
    assert _db_user(user["email"]).phone == "+34600123456"


def test_change_phone_invalid_is_422(client, admin_token):
    user, token = create_user(client, admin_token)
    for bad in ("CALL-ME", "12345", "1" * 16, "++555123"):
        r = client.post("/api/auth/change-phone", json={"phone": bad},
                        headers=auth(token))
        assert r.status_code == 422, (bad, r.text)


def test_change_phone_clears_with_empty_and_null(client, admin_token):
    # A single-field command body: both spellings clear (no omit-semantics).
    user, token = create_user(client, admin_token)
    client.post("/api/auth/change-phone", json={"phone": "600123456"},
                headers=auth(token))
    r = client.post("/api/auth/change-phone", json={"phone": ""},
                    headers=auth(token))
    assert r.status_code == 200 and r.json()["phone"] is None
    client.post("/api/auth/change-phone", json={"phone": "600123456"},
                headers=auth(token))
    r = client.post("/api/auth/change-phone", json={"phone": None},
                    headers=auth(token))
    assert r.status_code == 200 and r.json()["phone"] is None


def test_change_phone_noop_is_idempotent_200(client, admin_token):
    user, token = create_user(client, admin_token)
    client.post("/api/auth/change-phone", json={"phone": "600123456"},
                headers=auth(token))
    r = client.post("/api/auth/change-phone", json={"phone": "600 123 456"},
                    headers=auth(token))   # normalises to the same value
    assert r.status_code == 200 and r.json()["phone"] == "600123456"


def test_change_phone_gates_and_contract(client, admin_token):
    from tests.test_cart import _flagged_token
    assert client.post("/api/auth/change-phone",
                       json={"phone": "600123456"}).status_code == 401
    flagged = _flagged_token(client, admin_token)
    assert client.post("/api/auth/change-phone", json={"phone": "600123456"},
                       headers=auth(flagged)).status_code == 403
    # Session-only contract: the schema has NO password field (extra=forbid).
    r = client.post("/api/auth/change-phone",
                    json={"phone": "600123456", "current_password": "x"},
                    headers=auth(admin_token))
    assert r.status_code == 422


# --- phone: staff edits ----------------------------------------------------------


def test_staff_can_edit_member_phone_not_email(client, admin_token):
    member, _ = create_user(client, admin_token)
    # Set, clear (""), and the null rule — a PATCH document has omit-semantics.
    r = client.patch(f"/api/members/{member['id']}",
                     json={"phone": "+34 600 111 222"}, headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["phone"] == "+34600111222"
    r = client.patch(f"/api/members/{member['id']}",
                     json={"phone": ""}, headers=auth(admin_token))
    assert r.status_code == 200 and r.json()["phone"] is None
    r = client.patch(f"/api/members/{member['id']}",
                     json={"phone": None}, headers=auth(admin_token))
    assert r.status_code == 422  # _PatchIn: omit to leave unchanged
    r = client.patch(f"/api/members/{member['id']}",
                     json={"phone": "BAD"}, headers=auth(admin_token))
    assert r.status_code == 422
    # Staff can NEVER edit a member's email — the login identifier.
    r = client.patch(f"/api/members/{member['id']}",
                     json={"email": "newmail@example.com"},
                     headers=auth(admin_token))
    assert r.status_code == 422  # extra="forbid": the field does not exist
    assert _db_user(member["email"]).email == member["email"]


def test_member_creation_carries_phone(client, admin_token):
    email = unique_email()
    r = client.post("/api/members",
                    json={"name": "Phoned", "email": email,
                          "password": "password123", "role": "member",
                          "phone": "(555) 123-4567"},
                    headers=auth(admin_token))
    assert r.status_code == 201, r.text
    assert r.json()["phone"] == "5551234567"
    # …and without one: NULL, not "".
    email2 = unique_email()
    r = client.post("/api/members",
                    json={"name": "Unphoned", "email": email2,
                          "password": "password123", "role": "member"},
                    headers=auth(admin_token))
    assert r.json()["phone"] is None


# --- email change ------------------------------------------------------------------


def test_change_email_happy_path(client, admin_token):
    user, token = create_user(client, admin_token)
    new_email = unique_email()
    r = client.post("/api/auth/change-email",
                    json={"new_email": new_email.upper(),  # case-normalised
                          "current_password": USER_PASSWORD_AFTER_CHANGE},
                    headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["email"] == new_email          # normalised (lower/strip)
    assert client.post("/api/auth/login",
                       json={"email": new_email,
                             "password": USER_PASSWORD_AFTER_CHANGE}).status_code == 200
    assert client.post("/api/auth/login",
                       json={"email": user["email"],
                             "password": USER_PASSWORD_AFTER_CHANGE}).status_code == 401


def test_change_email_wrong_password_changes_nothing(client, admin_token):
    user, token = create_user(client, admin_token)
    r = client.post("/api/auth/change-email",
                    json={"new_email": unique_email(),
                          "current_password": "wrong-password"},
                    headers=auth(token))
    assert r.status_code == 400
    assert r.json()["detail"] == "Current password is incorrect"
    assert _db_user(user["email"]).email == user["email"]


def test_change_email_noop_is_400_before_any_revocation(client, admin_token):
    user, token = create_user(client, admin_token)
    second = _login(client, user["email"])          # a second session to watch
    # A case variant of the SAME email normalises to the current one.
    r = client.post("/api/auth/change-email",
                    json={"new_email": user["email"].upper(),
                          "current_password": USER_PASSWORD_AFTER_CHANGE},
                    headers=auth(token))
    assert r.status_code == 400
    # The no-op was rejected BEFORE the revocation loop: the other session
    # is still alive (a no-op must never revoke sessions).
    assert client.get("/api/auth/me", headers=auth(second)).status_code == 200


def test_change_email_revokes_other_sessions_presented_survives(client, admin_token):
    user, token = create_user(client, admin_token)
    second = _login(client, user["email"])
    new_email = unique_email()
    r = client.post("/api/auth/change-email",
                    json={"new_email": new_email,
                          "current_password": USER_PASSWORD_AFTER_CHANGE},
                    headers=auth(token))
    assert r.status_code == 200
    assert client.get("/api/auth/me", headers=auth(second)).status_code == 401
    assert client.get("/api/auth/me", headers=auth(token)).status_code == 200


def test_change_email_duplicate_is_409(client, admin_token):
    user, token = create_user(client, admin_token)
    other, _ = create_user(client, admin_token)
    r = client.post("/api/auth/change-email",
                    json={"new_email": other["email"],
                          "current_password": USER_PASSWORD_AFTER_CHANGE},
                    headers=auth(token))
    assert r.status_code == 409
    assert r.json()["detail"] == "Email already registered"
    assert _db_user(user["email"]).email == user["email"]


def test_taken_email_409_sits_behind_the_password_check(client, admin_token):
    """Oracle-narrowing pin: the taken-email 409 is an enumeration channel
    of register's accepted class, so it is served ONLY after the caller
    proved credential possession — a wrong password answers 400 even when
    the requested email is taken (never 409)."""
    user, token = create_user(client, admin_token)
    other, _ = create_user(client, admin_token)
    r = client.post("/api/auth/change-email",
                    json={"new_email": other["email"],
                          "current_password": "wrong-password"},
                    headers=auth(token))
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "Current password is incorrect"


def test_change_email_normalises_a_padded_email(client, admin_token):
    """Spec: "Normalise email (lower/strip)" — a padded/upper-cased address
    is normalised, not rejected (RegisterIn's _Stripped parity), while
    current_password keeps its raw semantics."""
    user, token = create_user(client, admin_token)
    new_email = unique_email()
    r = client.post("/api/auth/change-email",
                    json={"new_email": f"  {new_email.upper()} ",
                          "current_password": USER_PASSWORD_AFTER_CHANGE},
                    headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["email"] == new_email
    assert client.post("/api/auth/login",
                       json={"email": new_email,
                             "password": USER_PASSWORD_AFTER_CHANGE}).status_code == 200


def test_change_email_never_touches_the_badge(client, admin_token, monkeypatch):
    """The Task-A cross-pin, endpoint level: derivation keys on user_id, so
    moving the login identifier cannot change the badge."""
    from tests.test_badge_derivation import TEST_SECRET, _use_secret
    _use_secret(monkeypatch, TEST_SECRET)
    user, token = create_user(client, admin_token)
    before = client.get("/api/auth/qr-badge", headers=auth(token)).json()
    r = client.post("/api/auth/change-email",
                    json={"new_email": unique_email(),
                          "current_password": USER_PASSWORD_AFTER_CHANGE},
                    headers=auth(token))
    assert r.status_code == 200
    after = client.get("/api/auth/qr-badge", headers=auth(token)).json()
    assert after["payload"] == before["payload"]
    assert after["generation"] == before["generation"]
    assert client.post("/api/auth/qr-login",
                       json={"token": before["payload"]}).status_code == 200


def test_change_email_gates_and_schema(client, admin_token):
    from tests.test_cart import _flagged_token
    assert client.post("/api/auth/change-email",
                       json={"new_email": unique_email(),
                             "current_password": "x"}).status_code == 401
    flagged = _flagged_token(client, admin_token)
    assert client.post("/api/auth/change-email",
                       json={"new_email": unique_email(),
                             "current_password": "user-password-1"},
                       headers=auth(flagged)).status_code == 403
    # Schema contract: the password is REQUIRED (missing → 422) and unknown
    # fields are rejected (extra="forbid").
    user, token = create_user(client, admin_token)
    r = client.post("/api/auth/change-email",
                    json={"new_email": unique_email()}, headers=auth(token))
    assert r.status_code == 422
    r = client.post("/api/auth/change-email",
                    json={"new_email": unique_email(),
                          "current_password": USER_PASSWORD_AFTER_CHANGE,
                          "extra": 1},
                    headers=auth(token))
    assert r.status_code == 422
    r = client.post("/api/auth/change-email",
                    json={"new_email": "not-an-email",
                          "current_password": USER_PASSWORD_AFTER_CHANGE},
                    headers=auth(token))
    assert r.status_code == 422  # EMAIL_PATTERN


# --- the revoked session's tokens are really gone (not just 401-by-flag) ---------


def test_change_email_revocation_removes_token_rows(client, admin_token):
    user, token = create_user(client, admin_token)
    second = _login(client, user["email"])
    with Session(engine) as session:
        second_hash = token_hash(second)
        assert session.get(AuthToken, second_hash) is not None
    client.post("/api/auth/change-email",
                json={"new_email": unique_email(),
                      "current_password": USER_PASSWORD_AFTER_CHANGE},
                headers=auth(token))
    with Session(engine) as session:
        assert session.get(AuthToken, second_hash) is None  # deleted, not flagged
