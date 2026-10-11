"""QR badge login: payload format, badge lifecycle, permissions.

The QR matrix rendering itself is validated against the Nayuki reference
encoder (vendored in app/vendor/qrcodegen.py); here we assert the payload
contract and the API behaviour — including the automatic badge issued at
account creation (register / staff member creation): it is returned
exactly once, only its hash is stored, and no other response ever carries
badge material.
"""

import json

from sqlmodel import Session, select

from app.database import engine
from app.models import User
from app.qrbadge import (
    BADGE_PREFIX,
    badge_svg,
    new_badge_payload,
    parse_badge_payload,
)
from app.security import token_hash
from tests.conftest import auth, create_user, unique_email


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
    # Task 2: account creation auto-issues a badge, so a freshly created
    # user already HAS one (this test predates that and asserted False).
    # The creation badge itself is covered by the tests below; this one
    # exercises the self-service rotate/revoke lifecycle on top of it.
    assert user["has_qr_badge"] is True

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

# --- review pass over the QR commit itself ---------------------------------
#
# REVIEW-m10.md reviewed the driver commit (1e03f93) only; the QR-badge commit
# was written before that review and was never covered by it. These are the
# gaps that review pass found, each pinned so it cannot regress silently.


def test_a_session_token_is_not_a_badge(client, admin_token):
    """The two credential namespaces must stay separate.

    A session token and a badge token are both `secrets.token_urlsafe(32)` —
    43 characters of the same alphabet — and `/api/auth/qr-login` accepts a
    bare 43-char token for scanners that strip the prefix. So a stolen *session*
    token is syntactically a valid badge presentation. It must not authenticate:
    the lookup is on `users.qr_badge_hash`, never on `auth_tokens.token`.
    """
    _user, token = create_user(client, admin_token)
    assert len(token) == 43, "precondition: same shape as a badge token"
    assert client.post("/api/auth/qr-login", json={"token": token}).status_code == 401
    assert client.post("/api/auth/qr-login",
                       json={"token": BADGE_PREFIX + token}).status_code == 401


def test_badge_login_does_not_bypass_the_password_change_gate(client, admin_token):
    """W4.1/W4.2 gate a flagged account at token *use*, so a badge session must
    be gated exactly like a password session — otherwise QR login would be a way
    around the well-known-password block."""
    email = unique_email()
    r = client.post("/api/members",
                    json={"name": "Flagged Member", "email": email,
                          "password": "initial-password-1", "role": "member"},
                    headers=auth(admin_token))
    assert r.status_code == 201, r.text
    assert r.json()["must_change_password"] is True          # W4.2
    member_id = r.json()["id"]

    badge = client.post(f"/api/members/{member_id}/qr-badge",
                        headers=auth(admin_token)).json()["payload"]
    login = client.post("/api/auth/qr-login", json={"token": badge})
    assert login.status_code == 200, "the badge itself is valid"
    sess = auth(login.json()["token"])

    assert client.get("/api/auth/me", headers=sess).status_code == 200   # exempt
    assert client.get("/api/items", headers=sess).status_code == 403     # gated
    # …and a flagged account cannot mint its own badge either:
    assert client.post("/api/auth/qr-badge", headers=sess).status_code == 403


def test_only_the_hash_is_persisted(client, admin_token):
    """A leaked database file must not yield printable badges."""
    import sqlite3

    from app.security import token_hash
    from tests.conftest import get_db_path

    user, token = create_user(client, admin_token)
    badge = client.post("/api/auth/qr-badge", headers=auth(token)).json()["payload"]

    rows = sqlite3.connect(get_db_path()).execute(
        "SELECT qr_badge_hash FROM users WHERE email = ?", (user["email"],)
    ).fetchall()
    stored = rows[0][0]
    assert stored == token_hash(badge)
    assert stored != badge and badge[len(BADGE_PREFIX):] not in stored


def test_badge_svg_is_safe_for_the_v_html_sink():
    """The SPA renders the badge with `v-html`, so the SVG must be inert markup.

    The payload is always server-generated today; this pins that the renderer
    cannot become an injection sink if that ever changes.
    """
    import re

    payload = new_badge_payload()
    svg = badge_svg(payload)
    low = svg.lower()
    assert "<script" not in low
    assert "javascript:" not in low
    assert not re.search(r"\son[a-z]+\s*=", low), "no inline event handlers"
    assert payload not in svg, "the secret is encoded, never echoed as text"
    tags = set(re.findall(r"<(/?[a-zA-Z]+)", svg))
    assert tags <= {"svg", "rect", "path", "/svg", "/rect", "/path"}, tags


# --- automatic badge at account creation ------------------------------------

def _register(client, name="New User"):
    email = unique_email()
    r = client.post(
        "/api/auth/register",
        json={"name": name, "email": email, "password": "password123"},
    )
    assert r.status_code == 201, r.text
    return email, r.json()


def _create_member(client, admin_token, role="member"):
    email = unique_email()
    r = client.post(
        "/api/members",
        json={"name": "Created User", "email": email,
              "password": "temporary123", "role": role},
        headers=auth(admin_token),
    )
    assert r.status_code == 201, r.text
    return email, r.json()


def test_register_returns_a_one_time_badge(client):
    email, body = _register(client)
    # Registration remains a login: the session is issued alongside.
    assert body["token"]
    assert body["user"]["email"] == email
    assert body["user"]["has_qr_badge"] is True
    badge = body["qr_badge"]
    assert parse_badge_payload(badge["payload"]) == badge["payload"]
    assert badge["svg"].startswith("<svg")
    # The badge logs in immediately — in both forms (prefixed and raw token).
    for presented in (badge["payload"], badge["payload"][len(BADGE_PREFIX):]):
        r = client.post("/api/auth/qr-login", json={"token": presented})
        assert r.status_code == 200, r.text
        assert r.json()["user"]["email"] == email


def test_create_member_returns_a_working_badge(client, admin_token):
    for role in ("member", "staff", "admin"):
        email, body = _create_member(client, admin_token, role=role)
        assert body["email"] == email
        assert body["has_qr_badge"] is True
        badge = body["qr_badge"]
        assert parse_badge_payload(badge["payload"]) is not None
        assert badge["svg"].startswith("<svg")
        r = client.post("/api/auth/qr-login", json={"token": badge["payload"]})
        assert r.status_code == 200, r.text
        assert r.json()["user"]["email"] == email


def test_creation_badge_respects_the_password_gate(client, admin_token):
    # A staff-created account is flagged must_change_password: its creation
    # badge logs in, but the session is gated until the change; afterwards
    # the SAME badge works fully (the gate is about the password, not the
    # badge).
    email, body = _create_member(client, admin_token)
    badge = body["qr_badge"]["payload"]
    r = client.post("/api/auth/qr-login", json={"token": badge})
    assert r.status_code == 200, r.text
    gated_token = r.json()["token"]
    assert client.get("/api/items", headers=auth(gated_token)).status_code == 403
    assert client.get("/api/auth/me", headers=auth(gated_token)).status_code == 200

    login = client.post(
        "/api/auth/login", json={"email": email, "password": "temporary123"}
    )
    assert login.status_code == 200, login.text
    assert client.post(
        "/api/auth/change-password",
        json={"current_password": "temporary123",
              "new_password": "ownpassword1"},
        headers=auth(login.json()["token"]),
    ).status_code == 204

    r = client.post("/api/auth/qr-login", json={"token": badge})
    assert r.status_code == 200, r.text
    assert client.get("/api/items", headers=auth(r.json()["token"])).status_code == 200


def test_creation_badge_only_hash_at_rest(client, admin_token):
    # Register path: the stored value is exactly the hash of the shown
    # payload — the payload itself appears nowhere in the row.
    email, body = _register(client)
    payload = body["qr_badge"]["payload"]
    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == email)).first()
        assert user is not None
        assert user.qr_badge_hash == token_hash(payload)
        assert payload not in (user.email, user.name, user.password_hash)

    # Staff-create path: same property.
    email, body = _create_member(client, admin_token)
    payload = body["qr_badge"]["payload"]
    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == email)).first()
        assert user is not None
        assert user.qr_badge_hash == token_hash(payload)


def test_no_badge_material_outside_creation_and_rotation(client, admin_token):
    email, body = _register(client)
    creation_badge = body["qr_badge"]["payload"]
    token = body["token"]

    # login: keys are exactly {token, user} — AuthOut did not grow the field.
    login = client.post(
        "/api/auth/login", json={"email": email, "password": "password123"}
    ).json()
    assert set(login.keys()) == {"token", "user"}
    # qr-login: same shape.
    qr = client.post(
        "/api/auth/qr-login", json={"token": creation_badge}
    ).json()
    assert set(qr.keys()) == {"token", "user"}

    # /me and the members list carry the FLAG, never the material.
    me = client.get("/api/auth/me", headers=auth(token)).json()
    assert me["has_qr_badge"] is True
    members = client.get("/api/members", headers=auth(admin_token)).json()
    entry = [m for m in members if m["email"] == email][0]
    assert entry["has_qr_badge"] is True

    # PATCH member: UserOut shape, no badge field.
    patched = client.patch(
        f"/api/members/{entry['id']}",
        json={"name": "Renamed"},
        headers=auth(admin_token),
    ).json()
    assert patched["name"] == "Renamed"

    # Rotate legitimately carries a NEW payload (different from the
    # creation one); revoke is a bodyless 204. Everywhere else, neither the
    # creation payload nor any HCRM1:/svg material may appear.
    rotated = client.post("/api/auth/qr-badge", headers=auth(token)).json()
    assert rotated["payload"] != creation_badge
    assert client.delete("/api/auth/qr-badge", headers=auth(token)).status_code == 204

    for response_body in (login, qr, me, members, patched):
        dumped = json.dumps(response_body)
        assert "HCRM1:" not in dumped
        assert '"svg"' not in dumped
        assert '"payload"' not in dumped


def test_register_conflict_leaves_no_badge_trace(client):
    email, first = _register(client)
    first_badge = first["qr_badge"]["payload"]

    r = client.post(
        "/api/auth/register",
        json={"name": "Other", "email": email, "password": "password456"},
    )
    assert r.status_code == 409, r.text
    assert "qr_badge" not in r.json()

    # The DB has exactly one row, still holding the FIRST registration's
    # badge hash (the raced insert — row and never-shown payload together —
    # rolled back), and the first badge still works.
    with Session(engine) as session:
        users = session.exec(select(User).where(User.email == email)).all()
        assert len(users) == 1
        assert users[0].qr_badge_hash == token_hash(first_badge)
    assert client.post(
        "/api/auth/qr-login", json={"token": first_badge}
    ).status_code == 200


def test_creation_badges_are_unique(client, admin_token):
    _, a = _register(client)
    _, b = _register(client)
    _, c = _create_member(client, admin_token)
    payloads = {a["qr_badge"]["payload"], b["qr_badge"]["payload"],
                c["qr_badge"]["payload"]}
    assert len(payloads) == 3


def test_creation_badge_rotates_and_revokes(client):
    # The auto-issued badge is an ordinary badge: rotate kills it, revoke
    # kills it — nothing about auto-issuance is special.
    _, body = _register(client)
    creation = body["qr_badge"]["payload"]
    token = body["token"]

    r = client.post("/api/auth/qr-badge", headers=auth(token))
    assert r.status_code == 201, r.text
    new_payload = r.json()["payload"]
    assert new_payload != creation
    assert client.post("/api/auth/qr-login", json={"token": creation}).status_code == 401
    assert client.post("/api/auth/qr-login", json={"token": new_payload}).status_code == 200

    assert client.delete("/api/auth/qr-badge", headers=auth(token)).status_code == 204
    assert client.post("/api/auth/qr-login", json={"token": new_payload}).status_code == 401


def test_seeded_admin_has_no_badge(client, admin_token):
    # admin_token IS the seeded default admin (post mandatory change).
    me = client.get("/api/auth/me", headers=auth(admin_token)).json()
    assert me["has_qr_badge"] is False
    with Session(engine) as session:
        admin = session.exec(
            select(User).where(User.email == "admin@shop.local")
        ).first()
        assert admin is not None
        assert admin.qr_badge_hash is None
        # Freshly seeded DB (HCRM_DEV_ADMIN=0 in the suite): NO account
        # carries a badge — pins the structural seed exclusion.
        badged = session.exec(
            select(User).where(User.qr_badge_hash.is_not(None))  # type: ignore[attr-defined]
        ).all()
        assert badged == []


# --- qr-identify (Task 4): "is this MY badge?" --------------------------------


def _register_with_badge(client):
    """Register (auto-badge, Task 2) → (email, token, badge payload)."""
    email = unique_email()
    r = client.post(
        "/api/auth/register",
        json={"name": "Ident User", "email": email, "password": "password123"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    return email, body["token"], body["qr_badge"]["payload"]


def _identify(client, token, scanned):
    return client.post("/api/auth/qr-identify", json={"token": scanned},
                       headers=auth(token))


def test_identify_own_badge_both_forms(client):
    email, token, badge = _register_with_badge(client)
    for presented in (badge, badge[len(BADGE_PREFIX):]):  # prefixed + raw
        r = _identify(client, token, presented)
        assert r.status_code == 200, r.text
        assert r.json() == {"same_user": True}


def test_identify_other_users_badge_is_false(client):
    _, token_a, _ = _register_with_badge(client)
    _, _, badge_b = _register_with_badge(client)
    r = _identify(client, token_a, badge_b)
    assert r.status_code == 200, r.text
    assert r.json() == {"same_user": False}


def test_identify_garbage_is_false_never_an_error(client):
    _, token, _ = _register_with_badge(client)
    for bad in ("not-a-badge", "5901234123457",  # a product EAN
                BADGE_PREFIX + "short", BADGE_PREFIX + "!" * 43):
        r = _identify(client, token, bad)
        assert r.status_code == 200, (bad, r.text)
        assert r.json() == {"same_user": False}  # uniform: no enumeration
    # An empty payload is a client bug, caught by the shared schema's
    # min_length (422) before the handler — same contract as qr-login.
    assert _identify(client, token, "").status_code == 422


def test_identify_badgeless_account_is_false(client, admin_token):
    user, token = create_user(client, admin_token)
    # Task 2: created accounts HAVE a badge — revoke it to get a badgeless one.
    assert client.delete("/api/auth/qr-badge", headers=auth(token)).status_code == 204
    other_email, _, other_badge = _register_with_badge(client)
    r = _identify(client, token, other_badge)
    assert r.json() == {"same_user": False}
    # And its own (now-revoked) badge is no longer its own.
    r = client.post("/api/auth/qr-badge", headers=auth(token))
    assert r.status_code == 201
    rotated = r.json()["payload"]
    assert _identify(client, token, rotated).json() == {"same_user": True}


def test_identify_follows_rotation(client):
    email, token, creation_badge = _register_with_badge(client)
    assert _identify(client, token, creation_badge).json() == {"same_user": True}
    r = client.post("/api/auth/qr-badge", headers=auth(token))
    assert r.status_code == 201
    new_badge = r.json()["payload"]
    assert _identify(client, token, creation_badge).json() == {"same_user": False}
    assert _identify(client, token, new_badge).json() == {"same_user": True}


def test_identify_has_no_side_effects(client):
    from app.models import AuthToken
    _, token, badge = _register_with_badge(client)
    with Session(engine) as session:
        before = len(session.exec(select(AuthToken)).all())
    for _ in range(3):
        assert _identify(client, token, badge).status_code == 200
    with Session(engine) as session:
        after = len(session.exec(select(AuthToken)).all())
    assert before == after  # no session minted, nothing armed, nothing revoked


def test_identify_response_is_exactly_one_bit(client):
    _, token, badge = _register_with_badge(client)
    r = _identify(client, token, badge)
    assert set(r.json().keys()) == {"same_user"}  # no user fields, no material


def test_identify_requires_auth(client, admin_token):
    _, _, badge = _register_with_badge(client)
    # Anonymous: 401, nothing probeable.
    r = client.post("/api/auth/qr-identify", json={"token": badge})
    assert r.status_code == 401
    # Flagged account: 403 (not exempt — change the password first).
    from tests.test_cart import _flagged_token
    flagged = _flagged_token(client, admin_token)
    assert _identify(client, flagged, badge).status_code == 403
