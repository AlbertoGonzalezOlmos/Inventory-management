"""P1 admission tests (qwen slice): staff invites + invite-gated registration.

Covers the R2 gate order (gate → rate limit → 409 checks → consume → insert),
the byte-identical bad_invite 403, the no-burn-on-409 guarantee, the
machine_key owner conflict, the R7 fixed-window rate limit, and the atomic
one-invite race (the conditional-UPDATE rowcount guard). Hardware-free:
TestClient + the scratch DB, the test_scanner_api.py style.
"""

import secrets
from datetime import timedelta

import pytest
from sqlmodel import Session, select, update

import app.routers.scanner as scanner_router
from app.database import engine
from app.models import ScannerInvite, Terminal, utcnow
from app.security import token_hash
from tests.conftest import auth, create_user

ALPHABET = set("23456789ABCDEFGHJKMNPQRSTVWXYZ")


def new_secret() -> str:
    return secrets.token_urlsafe(32)


@pytest.fixture()
def loopback(monkeypatch):
    monkeypatch.setattr(scanner_router, "_LOOPBACK_HOSTS",
                        frozenset({"testclient"}))


@pytest.fixture(autouse=True)
def _clean_rate(monkeypatch):
    """The rate counter is process state (the DB reset does not touch it)."""
    scanner_router._register_rate_hits.clear()
    monkeypatch.setattr(scanner_router, "_register_rate_clock", lambda: 1000.0)
    yield
    scanner_router._register_rate_hits.clear()


@pytest.fixture()
def clock(monkeypatch):
    holder = [1000.0]
    monkeypatch.setattr(scanner_router, "_register_rate_clock", lambda: holder[0])
    return holder


def mint(client, token=None):
    return client.post("/api/scanner/invites", json={},
                       headers=auth(token) if token else {})


def reg(client, terminal_id, *, secret=None, invite=None, machine_key=None):
    body = {"terminal_id": terminal_id, "secret": secret or new_secret()}
    if invite is not None:
        body["invite"] = invite
    if machine_key is not None:
        body["machine_key"] = machine_key
    return client.post("/api/scanner/terminals", json=body)


def _expire(code):
    with Session(engine) as s:
        s.exec(update(ScannerInvite)
               .where(ScannerInvite.invite_hash == token_hash(code))
               .values(expires_at=utcnow() - timedelta(seconds=1)))
        s.commit()


# --- invite minting ----------------------------------------------------------

def test_invite_mint_loopback_ok(client, loopback):
    r = mint(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert len(body["invite_code"]) == 16
    assert set(body["invite_code"]) <= ALPHABET
    assert body["expires_at"]
    with Session(engine) as s:
        row = s.exec(select(ScannerInvite).where(
            ScannerInvite.invite_hash == token_hash(body["invite_code"]))).one()
        assert row.created_by is None          # loopback mint
        assert row.consumed_at is None


def test_invite_mint_staff_ok_records_creator(client, admin_token):
    r = mint(client, admin_token)               # non-loopback, admin
    assert r.status_code == 201, r.text
    with Session(engine) as s:
        row = s.exec(select(ScannerInvite).where(
            ScannerInvite.invite_hash == token_hash(r.json()["invite_code"]))).one()
        assert row.created_by                   # names the staff user


def test_invite_mint_nonstaff_403(client, admin_token):
    _, member = create_user(client, admin_token, role="member")
    r = mint(client, member)
    assert r.status_code == 403
    assert r.json()["detail"]["status"] == "staff_required"
    assert r.headers["X-Error-Code"] == "invite_staff_only"


def test_invite_mint_anonymous_401(client):
    r = mint(client)                            # non-loopback, no bearer
    assert r.status_code == 401


def test_invite_mint_requires_json_415(client, loopback):
    r = client.post("/api/scanner/invites", content="{}",
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    assert r.headers["X-Error-Code"] == "json_required"


def test_invite_max_open_409(client, loopback, monkeypatch):
    monkeypatch.setenv("HCRM_SCANNER_INVITE_MAX_OPEN", "2")
    assert mint(client).status_code == 201
    assert mint(client).status_code == 201
    r = mint(client)
    assert r.status_code == 409
    assert r.headers["X-Error-Code"] == "invite_limit"


def test_invite_prunes_expired_on_mint(client, loopback):
    code = mint(client).json()["invite_code"]
    _expire(code)
    assert mint(client).status_code == 201
    with Session(engine) as s:
        assert s.exec(select(ScannerInvite).where(
            ScannerInvite.invite_hash == token_hash(code))).first() is None


def test_invite_ttl_env_honoured(client, loopback, monkeypatch):
    monkeypatch.setenv("HCRM_SCANNER_INVITE_TTL_SECONDS", "60")
    body = mint(client).json()
    with Session(engine) as s:
        row = s.exec(select(ScannerInvite).where(
            ScannerInvite.invite_hash == token_hash(body["invite_code"]))).one()
        assert row.expires_at - row.created_at <= timedelta(seconds=61)


# --- invite-gated registration ----------------------------------------------

def test_register_nonloopback_valid_invite_201(client, admin_token):
    code = mint(client, admin_token).json()["invite_code"]
    r = reg(client, "byo-laptop", invite=code)   # non-loopback, no env
    assert r.status_code == 201, r.text
    assert len(r.json()["pairing_code"]) == 8


def test_register_no_invite_nonloopback_still_403(client):
    r = reg(client, "counter-1")
    assert r.status_code == 403
    assert r.json()["detail"]["status"] == "loopback_only"


def test_bad_invite_unknown_consumed_expired_are_byte_identical(client, admin_token):
    unknown = reg(client, "u", invite="2" * 16)
    code = mint(client, admin_token).json()["invite_code"]
    assert reg(client, "first", invite=code).status_code == 201
    consumed = reg(client, "second", invite=code)
    expired_code = mint(client, admin_token).json()["invite_code"]
    _expire(expired_code)
    expired = reg(client, "third", invite=expired_code)
    assert unknown.status_code == consumed.status_code == expired.status_code == 403
    assert unknown.json() == consumed.json() == expired.json()
    assert unknown.json()["detail"]["status"] == "bad_invite"
    assert unknown.headers["X-Error-Code"] == "bad_invite"


def test_invite_not_burned_on_409_conflict(client, admin_token):
    with Session(engine) as s:                  # an existing terminal
        s.add(Terminal(terminal_id="taken", secret_hash=new_secret()))
        s.commit()
    code = mint(client, admin_token).json()["invite_code"]
    conflict = reg(client, "taken", invite=code)   # 409 BEFORE consume
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["status"] == "already_registered"
    # the SAME invite still registers a different terminal — never burned
    assert reg(client, "fresh", invite=code).status_code == 201


def test_machine_key_conflict_409_names_owner(client, loopback):
    key = "0123456789abcdef"
    assert reg(client, "laptop-a", machine_key=key).status_code == 201
    r = reg(client, "laptop-b", machine_key=key)
    assert r.status_code == 409
    assert r.json()["detail"]["status"] == "machine_key_in_use"
    assert r.headers["X-Error-Code"] == "machine_key_in_use"
    assert "laptop-a" in r.json()["detail"]["message"]
    assert r.headers["X-Error-Vars"]            # terminal/terminal_url published


def test_register_env_open_still_works_without_invite(client, monkeypatch):
    monkeypatch.setenv("HCRM_SCANNER_REGISTER_ANY", "1")
    assert reg(client, "lan-box").status_code == 201


# --- the one-invite race (conditional UPDATE rowcount guard) -----------------

def test_concurrent_register_one_invite_loser_403(client, admin_token, monkeypatch):
    """Simulate the race winner committing between our gate and our consume:
    the rate-limit step (which runs after the gate) spends the invite in a
    side session. Our conditional UPDATE then sees rowcount 0 → bad_invite,
    and NO terminal is inserted (the insert never runs)."""
    code = mint(client, admin_token).json()["invite_code"]
    real = scanner_router._register_rate_limit

    def steal(ip):
        with Session(engine) as s:
            s.exec(update(ScannerInvite)
                   .where(ScannerInvite.invite_hash == token_hash(code),
                          ScannerInvite.consumed_at.is_(None))
                   .values(consumed_at=utcnow()))
            s.commit()
        return real(ip)

    monkeypatch.setattr(scanner_router, "_register_rate_limit", steal)
    r = reg(client, "race-loser", invite=code)
    assert r.status_code == 403
    assert r.json()["detail"]["status"] == "bad_invite"
    with Session(engine) as s:
        assert s.exec(select(Terminal).where(
            Terminal.terminal_id == "race-loser")).first() is None


# --- rate limit (R7) ---------------------------------------------------------

def test_rate_limit_429_with_retry_after(client, monkeypatch, clock):
    monkeypatch.setenv("HCRM_SCANNER_REGISTER_ANY", "1")
    monkeypatch.setenv("HCRM_SCANNER_REGISTER_RATE_MAX", "3")
    for i in range(3):
        assert reg(client, f"rl-{i}").status_code == 201
    r = reg(client, "rl-over")
    assert r.status_code == 429
    assert r.headers["X-Error-Code"] == "register_rate_limited"
    assert int(r.headers["Retry-After"]) >= 1
    assert r.json()["detail"]["status"] == "rate_limited"


def test_rate_limit_loopback_exempt(client, loopback, monkeypatch):
    monkeypatch.setenv("HCRM_SCANNER_REGISTER_RATE_MAX", "2")
    for i in range(5):
        assert reg(client, "lb-{i}".format(i=i)).status_code == 201


def test_rate_limit_window_expires(client, monkeypatch, clock):
    monkeypatch.setenv("HCRM_SCANNER_REGISTER_ANY", "1")
    monkeypatch.setenv("HCRM_SCANNER_REGISTER_RATE_MAX", "2")
    monkeypatch.setenv("HCRM_SCANNER_REGISTER_RATE_WINDOW_SECONDS", "600")
    assert reg(client, "w-0").status_code == 201
    assert reg(client, "w-1").status_code == 201
    assert reg(client, "w-2").status_code == 429
    clock[0] += 601                              # the window ages out
    assert reg(client, "w-3").status_code == 201


def test_rate_limit_counts_only_admitted_nonloopback(client, monkeypatch, clock):
    # A gate-403 (no invite, no env, non-loopback) must NOT spend a slot.
    monkeypatch.setenv("HCRM_SCANNER_REGISTER_RATE_MAX", "1")
    for _ in range(5):
        assert reg(client, "denied").status_code == 403
    monkeypatch.setenv("HCRM_SCANNER_REGISTER_ANY", "1")
    assert reg(client, "admitted").status_code == 201   # slot still free


# --- no credential leakage ---------------------------------------------------

def test_invite_code_never_in_register_or_errors(client, admin_token):
    code = mint(client, admin_token).json()["invite_code"]
    ok = reg(client, "leak-ok", invite=code)
    assert code not in ok.text                  # register returns pairing_code
    bad = reg(client, "leak-bad", invite="3" * 16)
    assert code not in bad.text
    assert "3" * 16 not in bad.text
