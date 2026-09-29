"""QR badge login: payload format, badge lifecycle, permissions.

The QR matrix rendering itself is validated against the Nayuki reference
encoder (vendored in app/vendor/qrcodegen.py); here we assert the payload
contract and the API behaviour.
"""

from app.qrbadge import (
    BADGE_PREFIX,
    badge_svg,
    new_badge_payload,
    parse_badge_payload,
)
from tests.conftest import auth, create_user


# --- payload unit tests ----------------------------------------------------

def test_payload_format():
    payload = new_badge_payload()
    assert payload.startswith(BADGE_PREFIX)
    token = payload[len(BADGE_PREFIX):]
    assert len(token) == 43  # token_urlsafe(32)
    assert parse_badge_payload(payload) == payload


def test_parse_rejects_non_badges():
    assert parse_badge_payload("5901234123457") is None  # product EAN
    assert parse_badge_payload("HCRM2:whatever") is None  # wrong version
    assert parse_badge_payload(BADGE_PREFIX + "short") is None  # truncated
    assert parse_badge_payload(BADGE_PREFIX + "A" * 44) is None  # too long
    assert parse_badge_payload(BADGE_PREFIX + "!" * 43) is None  # bad chars
    assert parse_badge_payload("") is None


def test_badge_svg_is_well_formed():
    svg = badge_svg(new_badge_payload())
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg
    assert "<path" in svg and "<rect" in svg  # quiet zone + modules
    assert "<script" not in svg  # CSP-safe markup only


# --- self-service badge lifecycle ------------------------------------------

def test_self_badge_lifecycle(client, admin_token):
    user, token = create_user(client, admin_token)
    assert user["has_qr_badge"] is False

    # Generate a badge.
    r = client.post("/api/auth/qr-badge", headers=auth(token))
    assert r.status_code == 201, r.text
    badge = r.json()["payload"]
    assert badge.startswith(BADGE_PREFIX)
    assert r.json()["svg"].startswith("<svg")
    assert client.get("/api/auth/me", headers=auth(token)).json()["has_qr_badge"] is True

    # Log in with it (full payload, and the raw token without prefix).
    for presented in (badge, badge[len(BADGE_PREFIX):]):
        r = client.post("/api/auth/qr-login", json={"token": presented})
        assert r.status_code == 200, r.text
        session_token = r.json()["token"]
        assert r.json()["user"]["email"] == user["email"]
        me = client.get("/api/auth/me", headers=auth(session_token))
        assert me.status_code == 200

    # Regenerate: the old badge dies, the new one works.
    r = client.post("/api/auth/qr-badge", headers=auth(token))
    new_badge = r.json()["payload"]
    assert new_badge != badge
    assert client.post("/api/auth/qr-login", json={"token": badge}).status_code == 401
    assert client.post("/api/auth/qr-login", json={"token": new_badge}).status_code == 200

    # Revoke: nothing works anymore.
    assert client.delete("/api/auth/qr-badge", headers=auth(token)).status_code == 204
    assert client.post("/api/auth/qr-login", json={"token": new_badge}).status_code == 401
    assert client.get("/api/auth/me", headers=auth(token)).json()["has_qr_badge"] is False


def test_qr_login_rejects_garbage(client):
    for bad in ("not-a-badge", BADGE_PREFIX + "A" * 43, "5901234123457"):
        r = client.post("/api/auth/qr-login", json={"token": bad})
        assert r.status_code == 401, (bad, r.text)


def test_badge_endpoints_require_auth(client):
    assert client.post("/api/auth/qr-badge").status_code in (401, 403)
    assert client.delete("/api/auth/qr-badge").status_code in (401, 403)


# --- staff-side badge management -------------------------------------------

def test_staff_manages_member_badge(client, admin_token):
    member, _ = create_user(client, admin_token, role="member")
    # create_user hands back a ready token: since W4.2 a staff-issued account
    # is flagged must_change_password and the helper performs that change, so
    # the original "password123" no longer logs in. This test predates that
    # contract (it was written against the pre-W4.2 conftest).
    staff, staff_token = create_user(client, admin_token, role="staff")

    # Staff generates a badge for the member; the member can log in with it.
    r = client.post(f"/api/members/{member['id']}/qr-badge", headers=auth(staff_token))
    assert r.status_code == 201, r.text
    badge = r.json()["payload"]
    r = client.post("/api/auth/qr-login", json={"token": badge})
    assert r.status_code == 200
    assert r.json()["user"]["email"] == member["email"]

    # And revokes it again.
    r = client.delete(f"/api/members/{member['id']}/qr-badge", headers=auth(staff_token))
    assert r.status_code == 204
    assert client.post("/api/auth/qr-login", json={"token": badge}).status_code == 401


def test_staff_cannot_touch_admin_badge(client, admin_token):
    staff, staff_token = create_user(client, admin_token, role="staff")
    admins = client.get("/api/members", headers=auth(admin_token)).json()
    admin_id = [u for u in admins if u["role"] == "admin"][0]["id"]

    assert client.post(
        f"/api/members/{admin_id}/qr-badge", headers=auth(staff_token)
    ).status_code == 403
    assert client.delete(
        f"/api/members/{admin_id}/qr-badge", headers=auth(staff_token)
    ).status_code == 403
    # Admins themselves can.
    assert client.post(
        f"/api/members/{admin_id}/qr-badge", headers=auth(admin_token)
    ).status_code == 201


def test_members_cannot_use_badge_management(client, admin_token):
    member, member_token = create_user(client, admin_token, role="member")
    other, _ = create_user(client, admin_token, role="member")
    assert client.post(
        f"/api/members/{other['id']}/qr-badge", headers=auth(member_token)
    ).status_code == 403
    # ...but can always manage their own badge (covered by the lifecycle test).
    assert client.post("/api/auth/qr-badge", headers=auth(member_token)).status_code == 201


def test_badge_management_unknown_member(client, admin_token):
    assert client.post("/api/members/9999/qr-badge", headers=auth(admin_token)).status_code == 404


def test_member_list_shows_badge_status(client, admin_token):
    member, _ = create_user(client, admin_token, role="member")
    client.post(f"/api/members/{member['id']}/qr-badge", headers=auth(admin_token))
    members = client.get("/api/members", headers=auth(admin_token)).json()
    entry = [m for m in members if m["id"] == member["id"]][0]
    assert entry["has_qr_badge"] is True


def test_deleted_members_badge_dies_with_account(client, admin_token):
    member, _ = create_user(client, admin_token, role="member")
    badge = client.post(
        f"/api/members/{member['id']}/qr-badge", headers=auth(admin_token)
    ).json()["payload"]
    assert client.post("/api/auth/qr-login", json={"token": badge}).status_code == 200
    client.delete(f"/api/members/{member['id']}", headers=auth(admin_token))
    assert client.post("/api/auth/qr-login", json={"token": badge}).status_code == 401
