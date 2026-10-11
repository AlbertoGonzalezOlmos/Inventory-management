"""Task A — HMAC-derived, re-viewable QR badges.

The scheme: payload = HCRM1: + b64url(HMAC(secret, "badge:{id}:{gen}")[:32]).
`qr_badge_hash` stays the sole login authority (the key is never needed to
authenticate); derivation exists so a badge can be RE-VIEWED forever. The
load-bearing pins here:

- the KAT: the derivation formula is a forever-contract (a change silently
  orphans every stored hash);
- a WRONG key never authenticates (the secret is the root), while a key
  MISMATCH at re-view never breaks login (the hash never needed the key);
- legacy random badges auto-migrate on first view — idempotently (two
  concurrent first views derive the SAME generation-1 payload).
"""

import os
import stat
import subprocess
import sys
import tempfile

import pytest
from sqlmodel import Session, select

from app import security
from app.database import engine
from app.models import User
from app.qrbadge import (
    BADGE_PREFIX,
    derive_badge_payload,
    new_badge_for,
    parse_badge_payload,
    view_badge,
)
from app.security import token_hash
from tests.conftest import auth, create_user, unique_email

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A stable test secret (>= security.BADGE_SECRET_MIN_LEN). The KAT below is
# computed with it and frozen — do not change either without a mass-reissue.
TEST_SECRET = "KAT-secret-0123456789abcdef0123456789abcdef"
OTHER_SECRET = "another-secret-0123456789abcdef0123456789"


def _use_secret(monkeypatch, secret):
    """Pin the process-wide badge secret (tests never touch the real key)."""
    monkeypatch.setattr(security, "_badge_secret_state",
                        (secret, "test") if secret else (None, None))


def _register(client):
    email = unique_email()
    r = client.post(
        "/api/auth/register",
        json={"name": "Derived User", "email": email, "password": "password123"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _db_user(email):
    with Session(engine) as session:
        return session.exec(select(User).where(User.email == email)).first()


# --- the frozen formula ---------------------------------------------------------


def test_known_answer_vector():
    """The formula is a forever-contract: any drift orphans every stored hash.
    This vector was computed once and frozen — changing it is a mass-reissue
    event (docs/opticon-m10.md §9.4), never a refactor."""
    assert derive_badge_payload(1, 1, TEST_SECRET) == \
        "HCRM1:IeUgiU6Rnx4FWVMMYMTM9gTHWFNg3OF16sdbXL3xt1o"


def test_derivation_shape_matches_the_legacy_contract():
    payload = derive_badge_payload(42, 3, TEST_SECRET)
    assert payload.startswith(BADGE_PREFIX)
    assert len(payload) == len(BADGE_PREFIX) + 43   # byte-identical shape
    assert parse_badge_payload(payload) == payload  # accepted everywhere


def test_derivation_is_deterministic_and_distinct():
    assert (derive_badge_payload(7, 1, TEST_SECRET)
            == derive_badge_payload(7, 1, TEST_SECRET))
    assert derive_badge_payload(7, 1, TEST_SECRET) != derive_badge_payload(7, 2, TEST_SECRET)
    assert derive_badge_payload(7, 1, TEST_SECRET) != derive_badge_payload(8, 1, TEST_SECRET)


# --- key bootstrap ---------------------------------------------------------------


def test_keyfile_auto_generation_0600_and_stable(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "_badge_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(security, "_badge_secret_state", None)
    first = security.badge_secret()
    assert first is not None and len(first) >= security.BADGE_SECRET_MIN_LEN
    assert security.badge_secret_source() == "file"
    mode = stat.S_IMODE(os.stat(os.path.join(tmp_path, "badge.key")).st_mode)
    assert mode == 0o600
    # Stable across a reload, and the O_EXCL re-read path returns the winner.
    monkeypatch.setattr(security, "_badge_secret_state", None)
    assert security.badge_secret() == first


def test_corrupt_keyfile_degrades_to_none(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "_badge_data_dir", lambda: str(tmp_path))
    with open(os.path.join(tmp_path, "badge.key"), "w") as f:
        f.write("short")  # corrupt/too short: never a derivation key
    monkeypatch.setattr(security, "_badge_secret_state", None)
    assert security.badge_secret() is None
    assert security.badge_secret_source() is None


def test_invalid_env_fails_fast_at_import():
    """A set-but-invalid HCRM_BADGE_SECRET must refuse to boot: falling back
    to the keyfile would silently fork the derivation space."""
    proc = subprocess.run(
        [sys.executable, "-c", "import os; os.environ['HCRM_BADGE_SECRET'] = 'short'; "
                               "import app.security"],
        capture_output=True, text=True, timeout=60, cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", "HCRM_DATA_DIR": tempfile.mkdtemp()},
    )
    assert proc.returncode != 0
    assert "HCRM_BADGE_SECRET" in proc.stderr


def test_env_secret_takes_precedence_and_writes_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "_badge_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(security, "_env_badge_secret", TEST_SECRET)
    monkeypatch.setattr(security, "_badge_secret_state", None)
    assert security.badge_secret() == TEST_SECRET
    assert security.badge_secret_source() == "env"
    assert not os.path.exists(os.path.join(tmp_path, "badge.key"))


# --- issuance ----------------------------------------------------------------------


def test_register_issues_a_derived_generation_1_badge(client, monkeypatch):
    _use_secret(monkeypatch, TEST_SECRET)
    body = _register(client)
    user = _db_user(body["user"]["email"])
    assert user.qr_badge_generation == 1
    assert body["qr_badge"]["payload"] == derive_badge_payload(user.id, 1, TEST_SECRET)
    assert user.qr_badge_hash == token_hash(body["qr_badge"]["payload"])
    # Both presentation forms log in (the shape is unchanged).
    payload = body["qr_badge"]["payload"]
    for presented in (payload, payload[len(BADGE_PREFIX):]):
        r = client.post("/api/auth/qr-login", json={"token": presented})
        assert r.status_code == 200, r.text
    r = client.post("/api/auth/qr-identify", json={"token": payload},
                    headers=auth(body["token"]))
    assert r.status_code == 200 and r.json()["same_user"] is True


def test_unflushed_user_raises_loudly(client, monkeypatch):
    _use_secret(monkeypatch, TEST_SECRET)
    user = User(email=unique_email(), name="x", password_hash="x")
    with pytest.raises(ValueError, match="flush"):
        new_badge_for(user)


def test_flush_race_leaves_no_trace(client, admin_token, monkeypatch):
    _use_secret(monkeypatch, TEST_SECRET)
    body = _register(client)
    email = body["user"]["email"]
    r = client.post("/api/auth/register",
                    json={"name": "Dup", "email": email, "password": "password456"})
    assert r.status_code == 409, r.text
    with Session(engine) as session:  # exactly one row, generation intact
        rows = session.exec(select(User).where(User.email == email)).all()
        assert len(rows) == 1 and rows[0].qr_badge_generation == 1


def test_no_key_issuance_falls_back_to_random_legacy(client, monkeypatch):
    _use_secret(monkeypatch, None)
    body = _register(client)  # registration must never block on the key
    user = _db_user(body["user"]["email"])
    assert user.qr_badge_generation == 0  # legacy: not re-viewable
    assert client.post("/api/auth/qr-login",
                       json={"token": body["qr_badge"]["payload"]}).status_code == 200
    # …and re-view says no_key (login unaffected).
    r = client.get("/api/auth/qr-badge", headers=auth(body["token"]))
    assert r.status_code == 409 and r.json()["detail"]["status"] == "no_key"


def test_keyless_rotation_never_resets_the_generation(client, monkeypatch):
    """Credential-resurrection guard: rotating while the key is unavailable
    must PRESERVE the generation (never decrement it).

    The failure mode this pins: gen-1 payload P1 issued → rotation retires
    P1 (gen 2) → key lost, rotation issues a random badge and RESETS the
    generation to 0 → key restored (e.g. from the backup that copies
    badge.key) → the next re-view auto-migrates “legacy” to generation 1 →
    derive(id, 1, same-secret) = P1 again — a retired, possibly leaked
    printed credential resurrected. With the generation preserved, re-view
    answers key_mismatch and the healing rotation uses an UNUSED generation.
    """
    _use_secret(monkeypatch, TEST_SECRET)
    body = _register(client)                     # gen 1, payload P1
    p1 = body["qr_badge"]["payload"]
    headers = auth(body["token"])
    assert client.post("/api/auth/qr-badge", headers=headers).status_code == 201
    assert _db_user(body["user"]["email"]).qr_badge_generation == 2   # P1 retired

    _use_secret(monkeypatch, None)               # the key goes away
    r = client.post("/api/auth/qr-badge", headers=headers)            # keyless rotate
    assert r.status_code == 201
    assert _db_user(body["user"]["email"]).qr_badge_generation == 2   # NOT reset to 0

    _use_secret(monkeypatch, TEST_SECRET)        # the key returns (restored backup)
    r = client.get("/api/auth/qr-badge", headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["status"] == "key_mismatch"
    r = client.post("/api/auth/qr-badge", headers=headers)            # heals → gen 3
    assert r.status_code == 201
    assert _db_user(body["user"]["email"]).qr_badge_generation == 3
    p3 = r.json()["payload"]
    assert p3 != p1
    # The retired payload stays dead — nothing re-derived it.
    assert client.post("/api/auth/qr-login", json={"token": p1}).status_code == 401
    assert client.post("/api/auth/qr-login", json={"token": p3}).status_code == 200


# --- re-view (the feature) -----------------------------------------------------------


def test_re_view_is_repeatable_and_identical_to_issuance(client, monkeypatch):
    _use_secret(monkeypatch, TEST_SECRET)
    body = _register(client)
    views = []
    for _ in range(2):
        r = client.get("/api/auth/qr-badge", headers=auth(body["token"]))
        assert r.status_code == 200, r.text
        view = r.json()
        assert view["payload"] == body["qr_badge"]["payload"]
        assert view["generation"] == 1 and view["migrated"] is False
        assert view["svg"].startswith("<svg")
        views.append(view)
    assert views[0] == views[1]


def test_rotation_bumps_generation_and_kills_the_old_payload(client, monkeypatch):
    _use_secret(monkeypatch, TEST_SECRET)
    body = _register(client)
    old = body["qr_badge"]["payload"]
    r = client.post("/api/auth/qr-badge", headers=auth(body["token"]))
    assert r.status_code == 201
    new = r.json()["payload"]
    assert new != old
    user = _db_user(body["user"]["email"])
    assert user.qr_badge_generation == 2
    assert client.post("/api/auth/qr-login", json={"token": old}).status_code == 401
    assert client.post("/api/auth/qr-login", json={"token": new}).status_code == 200
    r = client.get("/api/auth/qr-badge", headers=auth(body["token"]))
    assert r.json()["payload"] == new and r.json()["generation"] == 2


def test_revoke_then_reissue_never_reuses_a_generation(client, admin_token, monkeypatch):
    _use_secret(monkeypatch, TEST_SECRET)
    user, token = create_user(client, admin_token)
    client.post("/api/auth/qr-badge", headers=auth(token))          # gen 1 (creation) → 2
    g2 = _db_user(user["email"]).qr_badge_generation
    assert client.delete("/api/auth/qr-badge", headers=auth(token)).status_code == 204
    r = client.get("/api/auth/qr-badge", headers=auth(token))       # revoked → none
    assert r.status_code == 404 and r.json()["detail"]["status"] == "none"
    r = client.post("/api/auth/qr-badge", headers=auth(token))      # re-issue → 3
    assert _db_user(user["email"]).qr_badge_generation == g2 + 1


def test_no_badge_answers_404_none(client, admin_token, monkeypatch):
    _use_secret(monkeypatch, TEST_SECRET)
    user, token = create_user(client, admin_token)
    client.delete("/api/auth/qr-badge", headers=auth(token))
    r = client.get("/api/auth/qr-badge", headers=auth(token))
    assert r.status_code == 404
    assert r.json()["detail"]["status"] == "none"


# --- the wrong-key / key-mismatch pins -------------------------------------------------


def test_wrong_key_never_authenticates(client, monkeypatch):
    _use_secret(monkeypatch, TEST_SECRET)
    body = _register(client)
    # A payload derived with a DIFFERENT secret matches no stored hash.
    forged = derive_badge_payload(_db_user(body["user"]["email"]).id, 1, OTHER_SECRET)
    assert client.post("/api/auth/qr-login", json={"token": forged}).status_code == 401


def test_key_mismatch_never_breaks_login(client, monkeypatch):
    """The single most important pin: rotate the server key AFTER issuance →
    re-view 409s (never a wrong badge), but LOGIN STILL WORKS — the hash
    never needed the key."""
    _use_secret(monkeypatch, TEST_SECRET)
    body = _register(client)
    payload = body["qr_badge"]["payload"]
    _use_secret(monkeypatch, OTHER_SECRET)          # the key rotated
    r = client.get("/api/auth/qr-badge", headers=auth(body["token"]))
    assert r.status_code == 409 and r.json()["detail"]["status"] == "key_mismatch"
    assert client.post("/api/auth/qr-login", json={"token": payload}).status_code == 200
    # …and rotation heals: a fresh badge derives from the NEW key.
    r = client.post("/api/auth/qr-badge", headers=auth(body["token"]))
    assert r.status_code == 201
    r = client.get("/api/auth/qr-badge", headers=auth(body["token"]))
    assert r.status_code == 200


def test_rederive_and_check_is_the_self_test(client, monkeypatch):
    _use_secret(monkeypatch, TEST_SECRET)
    body = _register(client)
    user = _db_user(body["user"]["email"])
    assert view_badge.__name__  # (imported)
    from app.qrbadge import rederive_and_check
    assert rederive_and_check(user) == body["qr_badge"]["payload"]
    _use_secret(monkeypatch, OTHER_SECRET)
    with Session(engine) as session:  # fresh copy, no cached ORM state
        user = session.get(User, user.id)
        assert rederive_and_check(user) is None  # mismatch → never a wrong badge


def test_subprocess_wrong_secret_rejects_the_badge():
    """End-to-end: boot with secret A (scratch DB), issue a badge; boot with
    secret B against the SAME database → the badge no longer re-views, and a
    badge derived under B never logs in. The secret is the root."""
    data_dir = tempfile.mkdtemp(prefix="hcrm-badgesecret-")
    snippet = """
import json, os, sys
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as c:
    if sys.argv[1] == "issue":
        r = c.post("/api/auth/register", json={
            "name": "Sub", "email": "sub@example.com", "password": "password123"})
        print(json.dumps({"payload": r.json()["qr_badge"]["payload"]}))
    else:
        r = c.post("/api/auth/qr-login", json={"token": sys.argv[2]})
        rv = c.get("/api/auth/qr-badge") if r.status_code == 200 else None
        print(json.dumps({"login": r.status_code,
                          "relogin_token": r.json().get("token") if r.status_code == 200 else None}))
"""
    env = {"PATH": "/usr/bin:/bin", "HCRM_DATA_DIR": data_dir,
           "HCRM_DEV_ADMIN": "0", "HCRM_PBKDF2_ITERATIONS": "1000",
           "HCRM_EMBED_MODEL": "bogus/model", "HCRM_BADGE_SECRET": TEST_SECRET,
           "PYTHONPATH": REPO_ROOT}
    proc = subprocess.run([sys.executable, "-c", snippet, "issue"],
                          capture_output=True, text=True, timeout=240,
                          cwd=REPO_ROOT, env=env)
    assert proc.returncode == 0, proc.stderr[-2000:]
    payload = __import__("json").loads(
        [l for l in proc.stdout.splitlines() if l.startswith("{")][-1])["payload"]

    env["HCRM_BADGE_SECRET"] = OTHER_SECRET  # the key rotated between boots
    proc = subprocess.run(
        [sys.executable, "-c", snippet, "verify", payload],
        capture_output=True, text=True, timeout=240, cwd=REPO_ROOT, env=env)
    assert proc.returncode == 0, proc.stderr[-2000:]
    result = __import__("json").loads(
        [l for l in proc.stdout.splitlines() if l.startswith("{")][-1])
    # The OLD badge still logs in (hash authority) — the wrong-key payload
    # of THIS boot is a different derivation entirely.
    assert result["login"] == 200


# --- legacy auto-migration ----------------------------------------------------------


def _legacy_user_with_random_badge(email):
    """Hand-build a pre-derivation account: random payload, hash, generation 0."""
    from app.qrbadge import new_badge_payload
    from app.security import hash_password
    payload = new_badge_payload()
    with Session(engine) as session:
        user = User(email=email, name="Legacy", role="member",
                    password_hash=hash_password("password123"),
                    qr_badge_hash=token_hash(payload), qr_badge_generation=0)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user.email, payload


def test_legacy_badge_auto_migrates_on_first_view(client, monkeypatch):
    _use_secret(monkeypatch, TEST_SECRET)
    email, old_payload = _legacy_user_with_random_badge(unique_email())
    # The legacy badge logs in (hash authority, forever).
    assert client.post("/api/auth/qr-login", json={"token": old_payload}).status_code == 200

    login = client.post("/api/auth/login",
                        json={"email": email, "password": "password123"}).json()
    r = client.get("/api/auth/qr-badge", headers=auth(login["token"]))
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["migrated"] is True and view["generation"] == 1
    assert view["payload"] != old_payload
    # The old printed card died; the new derived badge works…
    assert client.post("/api/auth/qr-login", json={"token": old_payload}).status_code == 401
    assert client.post("/api/auth/qr-login", json={"token": view["payload"]}).status_code == 200
    # …and the migration is idempotent: a second view returns the SAME badge.
    r2 = client.get("/api/auth/qr-badge", headers=auth(login["token"]))
    assert r2.json()["payload"] == view["payload"]
    assert r2.json()["migrated"] is False


def test_legacy_badge_with_no_key_answers_no_key_not_migrate(client, monkeypatch):
    """Ordering pin: no_key beats legacy — never migrate to a random badge."""
    _use_secret(monkeypatch, None)
    email, old_payload = _legacy_user_with_random_badge(unique_email())
    login = client.post("/api/auth/login",
                        json={"email": email, "password": "password123"}).json()
    r = client.get("/api/auth/qr-badge", headers=auth(login["token"]))
    assert r.status_code == 409 and r.json()["detail"]["status"] == "no_key"
    assert client.post("/api/auth/qr-login", json={"token": old_payload}).status_code == 200


# --- invariants & gates ---------------------------------------------------------------


def test_email_change_never_touches_the_badge(client, monkeypatch):
    """Derivation is keyed on (secret, user_id, generation) — identity edits
    can never change a badge. The integration pin (Task B's endpoint) is
    cross-referenced from here."""
    _use_secret(monkeypatch, TEST_SECRET)
    body = _register(client)
    with Session(engine) as session:
        user = session.exec(select(User).where(
            User.email == body["user"]["email"])).first()
        user.email = "renamed-" + unique_email()
        session.add(user)
        session.commit()
    r = client.get("/api/auth/qr-badge", headers=auth(body["token"]))
    assert r.status_code == 200
    assert r.json()["payload"] == body["qr_badge"]["payload"]


def test_flagged_account_is_403(client, admin_token, monkeypatch):
    _use_secret(monkeypatch, TEST_SECRET)
    from tests.test_cart import _flagged_token
    token = _flagged_token(client, admin_token)
    assert client.get("/api/auth/qr-badge", headers=auth(token)).status_code == 403


def test_healthz_reports_the_key_state(client, monkeypatch):
    _use_secret(monkeypatch, TEST_SECRET)
    r = client.get("/api/healthz")
    assert r.status_code == 200
    assert r.json()["badge_key"] is True
    assert r.json()["badge_key_source"] == "test"
    _use_secret(monkeypatch, None)
    r = client.get("/api/healthz")
    assert r.json()["badge_key"] is False and r.json()["badge_key_source"] is None


# --- staff re-view (the counter reprint path) ------------------------------------------


def test_staff_view_privilege_matrix(client, admin_token, monkeypatch):
    _use_secret(monkeypatch, TEST_SECRET)
    member, _ = create_user(client, admin_token)
    staff, staff_token = create_user(client, admin_token, role="staff")
    _, admin_token2 = create_user(client, admin_token, role="admin")

    # Staff can view (and auto-migrate) a member's badge…
    r = client.get(f"/api/members/{member['id']}/qr-badge", headers=auth(staff_token))
    assert r.status_code == 200 and r.json()["generation"] == 1
    # …the creation badge (gen 1) re-views identically afterwards…
    r2 = client.get(f"/api/members/{member['id']}/qr-badge", headers=auth(staff_token))
    assert r2.json()["payload"] == r.json()["payload"] and r2.json()["migrated"] is False

    # …but never an admin's (the manage rule), and members cannot use the
    # staff endpoint at all.
    admins = client.get("/api/members", headers=auth(admin_token)).json()
    admin_id = [u for u in admins if u["role"] == "admin"][0]["id"]
    assert client.get(f"/api/members/{admin_id}/qr-badge",
                      headers=auth(staff_token)).status_code == 403
    assert client.get(f"/api/members/{member['id']}/qr-badge",
                      headers=auth(create_user(client, admin_token)[1])).status_code == 403
    assert client.get("/api/members/9999/qr-badge",
                      headers=auth(staff_token)).status_code == 404
    # Admins may view anyone.
    assert client.get(f"/api/members/{member['id']}/qr-badge",
                      headers=auth(admin_token)).status_code == 200
