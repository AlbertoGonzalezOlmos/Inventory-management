"""W4.1 / W4.2 / W5.2 — well-known passwords and staff-issued accounts.

The two holes these pin, both reproduced live before the fix (PLAN-v2 §0):

* B10: `PATCH /api/members/{id} {"password":"changeme"}` returned **200**, and
  `POST /api/auth/change-password` with `changeme -> changeme` returned **204
  and cleared must_change_password** — leaving an account on a password that is
  printed in this repository's README with full API access.
* B9: a staff-created account ("Temporary password" in the UI) was created with
  `must_change_password=False`, so it worked indefinitely on a password somebody
  else chose and knows.

Plus N5: the boot-time scan only inspected `admin@shop.local`, so any *other*
account on a published password was never flagged.
"""

from sqlmodel import Session, select

from app.database import engine
from app.main import WEAK_SCAN_KEY, _flag_well_known_passwords
from app.models import AppMeta, User
from app.security import hash_password
from tests.conftest import USER_PASSWORD_AFTER_CHANGE, auth, create_user, unique_email

# Both are published by this repo, and both are rejected — but by DIFFERENT
# layers, which is the point: `changeme` is 8 characters so it reaches the
# router's well-known-password check (400), while `admin` is 5 characters and is
# stopped earlier by the schemas' min_length=8 (422). Neither can be set through
# any endpoint, which is why the W4.0 dev credential is seed-only by
# construction.
WELL_KNOWN = {"changeme": 400, "admin": 422}


def _login(client, email, password):
    return client.post("/api/auth/login", json={"email": email, "password": password})


# --- W4.1: no endpoint may set a published password --------------------------

def test_change_password_to_a_well_known_password_is_rejected(client):
    email = unique_email()
    client.post("/api/auth/register",
                json={"name": "C", "email": email, "password": "password123"})
    token = _login(client, email, "password123").json()["token"]

    for weak, expected in WELL_KNOWN.items():
        r = client.post("/api/auth/change-password",
                        json={"current_password": "password123", "new_password": weak},
                        headers=auth(token))
        assert r.status_code == expected, (weak, r.status_code, r.text)
        if expected == 400:
            assert "published" in r.json()["detail"]

    # Nothing changed: the old password still works and the session survives.
    assert client.get("/api/auth/me", headers=auth(token)).status_code == 200
    assert _login(client, email, "password123").status_code == 200
    for weak in WELL_KNOWN:
        assert _login(client, email, weak).status_code == 401


def test_change_password_cannot_be_used_to_clear_the_flag_and_keep_default(client):
    """The exact B10 sequence, now impossible: the seeded admin is flagged, and
    `changeme -> changeme` used to return 204 and clear the flag."""
    token = _login(client, "admin@shop.local", "changeme").json()["token"]
    r = client.post("/api/auth/change-password",
                    json={"current_password": "changeme", "new_password": "changeme"},
                    headers=auth(token))
    assert r.status_code == 400, r.text
    # Still flagged, still blocked from the API.
    assert client.get("/api/auth/me", headers=auth(token)).json()["must_change_password"] is True
    assert client.get("/api/items", headers=auth(token)).status_code == 403


def test_change_password_to_the_same_password_is_rejected_and_keeps_the_flag(
        client, admin_token):
    """The exact round-5d F1 probe: a staff-issued account on `temporary123`
    calling change-password with `temporary123 -> temporary123` used to return
    204 and clear must_change_password — B9 through the back door, leaving the
    account on a password somebody else chose and knows, indefinitely."""
    email = unique_email()
    r = client.post("/api/members",
                    json={"name": "Temp", "email": email,
                          "password": "temporary123", "role": "member"},
                    headers=auth(admin_token))
    assert r.status_code == 201, r.text
    assert r.json()["must_change_password"] is True

    token = _login(client, email, "temporary123").json()["token"]
    r = client.post("/api/auth/change-password",
                    json={"current_password": "temporary123",
                          "new_password": "temporary123"},
                    headers=auth(token))
    assert r.status_code == 400, r.text
    assert "differ" in r.json()["detail"]
    # The flag survives and the account is still blocked from the API.
    me = client.get("/api/auth/me", headers=auth(token)).json()
    assert me["must_change_password"] is True
    assert client.get("/api/items", headers=auth(token)).status_code == 403


def test_change_password_to_the_same_password_is_rejected_for_anyone(client):
    """The no-op rejection is not specific to flagged accounts: a self-registered
    user cannot "change" their password to itself either."""
    email = unique_email()
    client.post("/api/auth/register",
                json={"name": "S", "email": email, "password": "password123"})
    token = _login(client, email, "password123").json()["token"]
    r = client.post("/api/auth/change-password",
                    json={"current_password": "password123",
                          "new_password": "password123"},
                    headers=auth(token))
    assert r.status_code == 400, r.text
    # Nothing changed: the same password still works, the session survives.
    assert _login(client, email, "password123").status_code == 200
    assert client.get("/api/auth/me", headers=auth(token)).status_code == 200


def test_admin_reset_to_a_well_known_password_is_rejected(client, admin_token):
    member, _ = create_user(client, admin_token)
    for weak, expected in WELL_KNOWN.items():
        r = client.patch(f"/api/members/{member['id']}", json={"password": weak},
                         headers=auth(admin_token))
        assert r.status_code == expected, (weak, r.status_code, r.text)
    # The member's own password is untouched and their session survives.
    assert _login(client, member["email"], USER_PASSWORD_AFTER_CHANGE).status_code == 200


def test_register_and_create_reject_well_known_passwords(client, admin_token):
    email = unique_email()
    for weak, expected in WELL_KNOWN.items():
        r = client.post("/api/auth/register",
                        json={"name": "R", "email": email, "password": weak})
        assert r.status_code == expected, (weak, r.status_code, r.text)
        r = client.post("/api/members",
                        json={"name": "R", "email": email, "password": weak,
                              "role": "member"},
                        headers=auth(admin_token))
        assert r.status_code == expected, (weak, r.status_code, r.text)
    # Neither password can be used to log in, and no account was created.
    for weak in WELL_KNOWN:
        assert _login(client, email, weak).status_code == 401


# --- W4.2: a staff-issued password is a temporary password -------------------

def test_staff_created_account_is_flagged_until_changed(client, admin_token):
    email = unique_email()
    r = client.post("/api/members",
                    json={"name": "Temp", "email": email,
                          "password": "temporary123", "role": "member"},
                    headers=auth(admin_token))
    assert r.status_code == 201, r.text
    assert r.json()["must_change_password"] is True          # was False (B9)

    token = _login(client, email, "temporary123").json()["token"]
    assert token
    # Blocked from the app until the holder changes it…
    blocked = client.get("/api/items", headers=auth(token))
    assert blocked.status_code == 403
    assert "Password change required" in blocked.json()["detail"]
    # …but the exempt paths still work, so they can change it.
    assert client.get("/api/auth/me", headers=auth(token)).status_code == 200
    r = client.post("/api/auth/change-password",
                    json={"current_password": "temporary123",
                          "new_password": "ownpassword1"},
                    headers=auth(token))
    assert r.status_code == 204, r.text
    assert client.get("/api/items", headers=auth(token)).status_code == 200
    assert _login(client, email, "ownpassword1").json()["user"]["must_change_password"] is False


def test_staff_created_staff_account_is_flagged_too(client, admin_token):
    _, staff_token = create_user(client, admin_token, role="staff")
    email = unique_email()
    r = client.post("/api/members",
                    json={"name": "New Staff", "email": email,
                          "password": "temporary123", "role": "staff"},
                    headers=auth(staff_token))
    assert r.status_code == 201, r.text
    assert r.json()["must_change_password"] is True


# --- N5: the scan covers every account, not just the seeded admin ------------

def _add_user_directly(email: str, password: str, flagged: bool = False) -> None:
    """Simulate a legacy row: written straight to the DB, bypassing the API."""
    with Session(engine) as session:
        session.add(User(email=email, name="Legacy", password_hash=hash_password(password),
                         role="member", must_change_password=flagged))
        session.commit()


def _reset_scan_marker() -> None:
    with Session(engine) as session:
        meta = session.get(AppMeta, WEAK_SCAN_KEY)
        if meta is not None:
            session.delete(meta)
            session.commit()


def test_scan_flags_any_account_on_a_published_password(client):
    _add_user_directly("legacy-changeme@t.local", "changeme")
    _add_user_directly("legacy-admin@t.local", "admin")
    _add_user_directly("legacy-strong@t.local", "a-strong-password-1")
    _reset_scan_marker()

    with Session(engine) as session:
        _flag_well_known_passwords(session)

    with Session(engine) as session:
        flags = {u.email: u.must_change_password
                 for u in session.exec(select(User)).all()}
    assert flags["legacy-changeme@t.local"] is True
    assert flags["legacy-admin@t.local"] is True
    assert flags["legacy-strong@t.local"] is False
    # The seeded default admin is flagged as before.
    assert flags["admin@shop.local"] is True


def test_scan_is_marker_gated_so_boot_cost_is_bounded(client):
    """Each candidate is a full PBKDF2 verification per account, so the scan runs
    once per policy, not once per boot."""
    _add_user_directly("scan-cost@t.local", "a-strong-password-1")
    _reset_scan_marker()
    with Session(engine) as session:
        _flag_well_known_passwords(session)
    with Session(engine) as session:
        assert session.get(AppMeta, WEAK_SCAN_KEY) is not None

    # A second call is a no-op: prove it by planting a weak password after the
    # marker was written — it is NOT flagged until the marker is reset.
    _add_user_directly("planted-after-scan@t.local", "changeme")
    with Session(engine) as session:
        _flag_well_known_passwords(session)
    with Session(engine) as session:
        user = session.exec(
            select(User).where(User.email == "planted-after-scan@t.local")).first()
        assert user.must_change_password is False

    _reset_scan_marker()
    with Session(engine) as session:
        _flag_well_known_passwords(session)
    with Session(engine) as session:
        user = session.exec(
            select(User).where(User.email == "planted-after-scan@t.local")).first()
        assert user.must_change_password is True


def test_flagged_legacy_account_is_blocked_at_the_api(client):
    _add_user_directly("legacy@t.local", "changeme")
    _reset_scan_marker()
    with Session(engine) as session:
        _flag_well_known_passwords(session)

    token = _login(client, "legacy@t.local", "changeme").json()["token"]
    assert client.get("/api/items", headers=auth(token)).status_code == 403
    r = client.post("/api/auth/change-password",
                    json={"current_password": "changeme", "new_password": "changeme"},
                    headers=auth(token))
    assert r.status_code == 400                     # cannot clear the flag with it
    r = client.post("/api/auth/change-password",
                    json={"current_password": "changeme", "new_password": "proper-pass-1"},
                    headers=auth(token))
    assert r.status_code == 204, r.text
    assert client.get("/api/items", headers=auth(token)).status_code == 200
