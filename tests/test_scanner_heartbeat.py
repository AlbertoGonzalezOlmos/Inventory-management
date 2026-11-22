"""P1 reclaim + heartbeat + truthful agent liveness tests (hardware-free)."""

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

import app.routers.scanner as scanner_router
from app.database import engine
from app.models import Terminal, utcnow
from tests.test_scanner_api import ingest, new_secret, pair, poll, register


@pytest.fixture()
def loopback(monkeypatch):
    monkeypatch.setattr(scanner_router, "_LOOPBACK_HOSTS",
                        frozenset({"testclient"}))


def reclaim(client, terminal_id, secret=None, *, bearer=False):
    headers = {}
    if secret is not None:
        headers = ({"Authorization": f"Bearer {secret}"} if bearer
                   else {"X-Scanner-Secret": secret})
    return client.post(f"/api/scanner/terminals/{terminal_id}/reclaim",
                       json={}, headers=headers)


def heartbeat(client, terminal_id, secret=None, body=None):
    headers = {"X-Scanner-Secret": secret} if secret is not None else {}
    return client.post(f"/api/scanner/terminals/{terminal_id}/heartbeat",
                       json=body or {"device_status": "present"}, headers=headers)


def terminal_row(terminal_id):
    with Session(engine) as session:
        return session.exec(select(Terminal).where(
            Terminal.terminal_id == terminal_id)).one()


def test_reclaim_rotates_code_and_revokes_old_token(client, loopback):
    secret, first_code = register(client, "reclaim-me")
    old_token = pair(client, "reclaim-me", first_code).json()["token"]

    first = reclaim(client, "reclaim-me", secret)
    assert first.status_code == 200
    second_code = first.json()["pairing_code"]
    assert len(second_code) == 8 and second_code != first_code
    assert terminal_row("reclaim-me").pairing_code_expires_at is not None
    assert poll(client, old_token).status_code == 401  # SPA auto-unpairs here
    assert pair(client, "reclaim-me", first_code).status_code == 401
    second_token = pair(client, "reclaim-me", second_code).json()["token"]
    assert poll(client, second_token).status_code == 200

    # Repeat is the recovery path, not a staff operation: bearer also works,
    # and every call kills both the prior code and prior browser token.
    second = reclaim(client, "reclaim-me", secret, bearer=True)
    assert second.status_code == 200
    third_code = second.json()["pairing_code"]
    assert third_code != second_code
    assert poll(client, second_token).status_code == 401
    assert pair(client, "reclaim-me", second_code).status_code == 401
    assert pair(client, "reclaim-me", third_code).status_code == 200
    # Pairing spends the latest code and clears its deadline (R9).
    assert terminal_row("reclaim-me").pairing_code_expires_at is None


def test_reclaim_auth_and_json_failures_do_not_rotate(client, loopback):
    secret, code = register(client, "reclaim-guard")
    other_secret, _ = register(client, "other-terminal")
    token = pair(client, "reclaim-guard", code).json()["token"]
    before = terminal_row("reclaim-guard")

    wrong_type = client.post("/api/scanner/terminals/reclaim-guard/reclaim",
                             content="{}", headers={
                                 "Content-Type": "text/plain",
                                 "X-Scanner-Secret": secret})
    assert wrong_type.status_code == 415
    assert wrong_type.json()["detail"]["status"] == "json_required"
    assert wrong_type.headers["X-Error-Code"] == "json_required"

    responses = [reclaim(client, "reclaim-guard"),
                 reclaim(client, "reclaim-guard", new_secret()),
                 reclaim(client, "reclaim-guard", other_secret),
                 reclaim(client, "missing-terminal", secret)]
    assert all(r.status_code == 401 for r in responses)
    assert all(r.json()["detail"] == {
        "status": "unknown_terminal",
        "message": "Unknown terminal or bad credential"} for r in responses)
    after = terminal_row("reclaim-guard")
    assert after.pairing_code_hash == before.pairing_code_hash
    assert after.pairing_code_expires_at == before.pairing_code_expires_at
    assert poll(client, token).status_code == 200


def test_heartbeat_updates_agent_and_device_without_touching_queue(client, loopback):
    secret, code = register(client, "heartbeat-fields")
    token = pair(client, "heartbeat-fields", code).json()["token"]
    before = terminal_row("heartbeat-fields")
    assert before.agent_seen_at is None and before.device_status is None

    absent = heartbeat(client, "heartbeat-fields", secret,
                       {"device_status": "absent", "personality": None})
    assert absent.status_code == 204 and absent.content == b""
    first = terminal_row("heartbeat-fields")
    assert first.agent_seen_at is not None
    assert first.last_seen_at == first.agent_seen_at
    assert first.device_status == "absent" and first.personality is None

    present = heartbeat(client, "heartbeat-fields", secret,
                        {"device_status": "present", "personality": "hid-a001"})
    assert present.status_code == 204 and present.content == b""
    second = terminal_row("heartbeat-fields")
    assert second.agent_seen_at >= first.agent_seen_at
    assert second.device_status == "present"
    assert second.personality == "hid-a001"
    body = poll(client, token).json()
    assert body["agent"] == {"online": True, "device_status": "present",
                             "personality": "hid-a001"}
    assert body["events"] == []


def test_heartbeat_auth_json_and_validation_leave_state_unchanged(client, loopback):
    secret, _ = register(client, "heartbeat-guard")
    other_secret, _ = register(client, "heartbeat-other")
    wrong_type = client.post("/api/scanner/terminals/heartbeat-guard/heartbeat",
                             content='{"device_status":"present"}', headers={
                                 "Content-Type": "text/plain",
                                 "X-Scanner-Secret": secret})
    assert wrong_type.status_code == 415
    assert wrong_type.json()["detail"]["status"] == "json_required"

    for response in (heartbeat(client, "heartbeat-guard"),
                     heartbeat(client, "heartbeat-guard", new_secret()),
                     heartbeat(client, "heartbeat-guard", other_secret),
                     heartbeat(client, "missing-terminal", secret)):
        assert response.status_code == 401
        assert response.json()["detail"]["status"] == "unknown_terminal"
    for body in ({"device_status": "weird"},
                 {"device_status": "present", "personality": "HID A001"},
                 {"device_status": "present", "personality": "x" * 33},
                 {"personality": "hid-a001"}):
        response = heartbeat(client, "heartbeat-guard", secret, body)
        assert response.status_code == 422
    row = terminal_row("heartbeat-guard")
    assert row.agent_seen_at is None
    assert row.device_status is None and row.personality is None


def test_poll_agent_never_uses_registration_or_ingest_as_liveness(client, loopback):
    secret, code = register(client, "never-heartbeat")
    token = pair(client, "never-heartbeat", code).json()["token"]
    expected = {"online": False, "device_status": None, "personality": None}
    assert poll(client, token).json()["agent"] == expected
    assert ingest(client, secret, "EX-007").status_code == 201
    body = poll(client, token).json()
    assert body["agent"] == expected
    assert [event["code"] for event in body["events"]] == ["EX-007"]


def test_poll_agent_tracks_fresh_stale_and_future_heartbeats(
        client, loopback, monkeypatch):
    secret, code = register(client, "heartbeat-clock")
    token = pair(client, "heartbeat-clock", code).json()["token"]
    base = datetime(2026, 11, 21, 12, 0, 0)
    monkeypatch.setattr(scanner_router, "HEARTBEAT_FRESH_SECONDS", 90)
    monkeypatch.setattr(scanner_router, "utcnow", lambda: base)
    assert heartbeat(client, "heartbeat-clock", secret).status_code == 204
    assert poll(client, token).json()["agent"]["online"] is True

    monkeypatch.setattr(scanner_router, "utcnow",
                        lambda: base + timedelta(seconds=91))
    stale = poll(client, token).json()["agent"]
    assert stale == {"online": False, "device_status": "present",
                     "personality": None}

    with Session(engine) as session:
        row = session.exec(select(Terminal).where(
            Terminal.terminal_id == "heartbeat-clock")).one()
        row.agent_seen_at = utcnow() + timedelta(seconds=1)
        session.add(row)
        session.commit()
    future = poll(client, token).json()["agent"]
    assert future["online"] is False


def test_absent_heartbeat_keeps_agent_seen_and_browser_binding(client, loopback):
    secret, code = register(client, "heartbeat-absent")
    token = pair(client, "heartbeat-absent", code).json()["token"]
    assert heartbeat(client, "heartbeat-absent", secret).status_code == 204
    first_seen = terminal_row("heartbeat-absent").agent_seen_at
    response = heartbeat(client, "heartbeat-absent", secret,
                         {"device_status": "absent"})
    assert response.status_code == 204
    row = terminal_row("heartbeat-absent")
    assert row.agent_seen_at >= first_seen
    assert row.device_status == "absent"
    body = poll(client, token).json()
    assert body["agent"]["online"] is True
    assert body["agent"]["device_status"] == "absent"
