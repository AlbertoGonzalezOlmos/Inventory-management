"""Tests for the round-4 T4 scanner feed API (app/routers/scanner.py).

The counter-session lifecycle end to end — register (loopback daemon) →
pair (browser, single-use code) → ingest (secret-authed daemon) → poll
(token-authed, logged-out-friendly SPA) → delete (staff/loopback recovery)
— plus the settled semantics: server-derived kinds, at-most-once delivery,
per-terminal isolation, TTL/consumed pruning, and the capability split
(secret writes, token reads, neither interchangeable).
"""

import json
import secrets
from datetime import timedelta

import pytest
from sqlmodel import Session, select

import app.routers.scanner as scanner_router
from app.database import engine
from app.models import ScanEvent
from app.scanshapes import BADGE_PREFIX
from tests.conftest import ADMIN_PASSWORD_AFTER_CHANGE, auth, create_user

BADGE = BADGE_PREFIX + "A" * 43
RAW_TOKEN = "B" * 43


def new_secret() -> str:
    return secrets.token_urlsafe(32)  # the daemon-side credential shape


@pytest.fixture()
def loopback(monkeypatch):
    """Admit the TestClient (host 'testclient') as a loopback caller."""
    monkeypatch.setattr(scanner_router, "_LOOPBACK_HOSTS",
                        frozenset({"testclient"}))


def register(client, terminal_id="counter-1", secret=None):
    secret = secret or new_secret()
    r = client.post("/api/scanner/terminals",
                    json={"terminal_id": terminal_id, "secret": secret})
    assert r.status_code == 201, r.text
    return secret, r.json()["pairing_code"]


def pair(client, terminal_id, code):
    return client.post("/api/scanner/pair",
                       json={"terminal_id": terminal_id, "code": code})


def offer_file(tmp_path, terminal_id="counter-1", code=None):
    """Point HCRM_SCANNER_PAIRING_FILE at a tmp file; return its Path.

    Written with the two keys the daemon writes — code defaults to None so
    a test can register first and pass the real single-use code in.
    """
    path = tmp_path / "scanner-pairing.json"
    if code is not None:
        path.write_text(json.dumps(
            {"terminal_id": terminal_id, "pairing_code": code}))
    return path


def auto_pair(client):
    # body {}: api()'s SPA form always attaches the JSON content-type; the
    # endpoint requires it (the CSRF gate), so the tests must too.
    return client.post("/api/scanner/pair/auto", json={})


def ingest(client, secret, code, *, header=True):
    headers = {"X-Scanner-Secret": secret} if header else {}
    return client.post("/api/scanner/events", json={"code": code},
                       headers=headers)


def poll(client, token, after=0):
    return client.get(f"/api/scanner/events?after={after}",
                      headers={"X-Scanner-Token": token})


def events_rows(terminal_id):
    """Direct DB access for prune assertions (the API must not expose them)."""
    with Session(engine) as session:
        return session.exec(
            select(ScanEvent).where(ScanEvent.terminal_id == terminal_id)
            .order_by(ScanEvent.seq)
        ).all()


def age_events(terminal_id, seconds):
    """Wind the clock on stored rows — deterministic TTL pruning."""
    with Session(engine) as session:
        for event in session.exec(
                select(ScanEvent).where(ScanEvent.terminal_id == terminal_id)).all():
            event.created_at -= timedelta(seconds=seconds)
            session.add(event)
        session.commit()


# --- the lifecycle ------------------------------------------------------------

def test_register_pair_ingest_poll_delete_lifecycle(client, loopback):
    secret, code = register(client)
    assert len(code) == 8

    r = pair(client, "counter-1", code)
    assert r.status_code == 200, r.text
    token = r.json()["token"]

    seqs = [ingest(client, secret, c).json()["seq"]
            for c in (BADGE, "8412345678905", RAW_TOKEN)]
    assert seqs == sorted(seqs)  # the cursor is monotonic

    r = poll(client, token)
    assert r.status_code == 200
    body = r.json()
    assert [e["code"] for e in body["events"]] == [BADGE, "8412345678905", RAW_TOKEN]
    assert body["latest_seq"] == seqs[-1]

    # Unpair (loopback caller, no user session) — then everything 401s.
    r = client.delete("/api/scanner/terminals/counter-1")
    assert r.status_code == 204
    assert poll(client, token).status_code == 401
    assert ingest(client, secret, "8412345678905").status_code == 401
    assert pair(client, "counter-1", code).status_code == 401
    assert events_rows("counter-1") == []  # the queue died with the terminal


# --- registration gate ----------------------------------------------------------

def test_register_is_loopback_gated(client):
    # The TestClient's host is 'testclient' — not loopback.
    r = client.post("/api/scanner/terminals",
                    json={"terminal_id": "counter-1", "secret": new_secret()})
    assert r.status_code == 403
    assert r.json()["detail"]["status"] == "loopback_only"


def test_register_env_override_opens_the_gate(client, monkeypatch):
    monkeypatch.setenv("HCRM_SCANNER_REGISTER_ANY", "1")
    r = client.post("/api/scanner/terminals",
                    json={"terminal_id": "counter-1", "secret": new_secret()})
    assert r.status_code == 201


def test_re_register_conflicts(client, loopback):
    secret, _ = register(client)
    r = client.post("/api/scanner/terminals",
                    json={"terminal_id": "counter-1", "secret": secret})
    assert r.status_code == 409
    assert r.json()["detail"]["status"] == "already_registered"


def test_register_rejects_a_secret_already_in_use(client, loopback):
    # Ingest auth looks the terminal up BY secret hash: a secret shared by
    # two terminals (a copied daemon config) would misroute every scan of
    # both kiosks into the first queue — refused loudly at register.
    secret, _ = register(client, "counter-1")
    r = client.post("/api/scanner/terminals",
                    json={"terminal_id": "counter-2", "secret": secret})
    assert r.status_code == 409
    assert r.json()["detail"]["status"] == "secret_in_use"


def test_register_rejects_weak_secrets_and_odd_ids(client, loopback):
    for body in (
        {"terminal_id": "counter-1", "secret": "short"},                 # < 32
        {"terminal_id": "bad id!", "secret": new_secret()},              # charset
        {"terminal_id": "", "secret": new_secret()},                     # empty id
        {"terminal_id": "counter-1"},                                    # no secret
    ):
        r = client.post("/api/scanner/terminals", json=body)
        assert r.status_code == 422, body


# --- pairing ---------------------------------------------------------------------

def test_pair_wrong_code_and_unknown_terminal_are_indistinguishable(client, loopback):
    _, code = register(client)
    assert pair(client, "counter-1", "XXXXXXXX").status_code == 401
    assert pair(client, "no-such-terminal", code).status_code == 401
    # Both carry the same envelope — no enumeration oracle.
    a = client.post("/api/scanner/pair",
                    json={"terminal_id": "counter-1", "code": "XXXXXXXX"}).json()
    b = client.post("/api/scanner/pair",
                    json={"terminal_id": "no-such-terminal", "code": code}).json()
    assert a["detail"]["status"] == b["detail"]["status"] == "bad_pairing"


def test_pair_twice_conflicts_even_with_the_correct_code(client, loopback):
    _, code = register(client)
    assert pair(client, "counter-1", code).status_code == 200
    r = pair(client, "counter-1", code)
    assert r.status_code == 409
    assert r.json()["detail"]["status"] == "already_paired"


def test_pairing_code_is_single_use_by_construction(client, loopback):
    _, code = register(client)
    token = pair(client, "counter-1", code).json()["token"]
    # The FIRST token keeps working; the code can never mint a second.
    assert poll(client, token).status_code == 200


def test_concurrent_pair_calls_mint_exactly_one_token(client, loopback):
    """Two browsers racing one pairing code: exactly one 200, one 409.

    The mint is a conditional UPDATE (``WHERE token_hash IS NULL``) — the
    database decides the winner. The read-then-write this replaced let both
    racers pass a NULL check and both commit; the loser then held a DEAD
    token whose polls 401 and the feed silently went unpaired. The barrier
    maximises the overlap; the assertion holds under any interleaving.
    (Bare ``TestClient(app)`` per worker — no lifespan in worker threads,
    the ``test_concurrency.py`` precedent.)
    """
    import threading

    from fastapi.testclient import TestClient

    from app.main import app

    _, code = register(client)
    barrier = threading.Barrier(2)
    outcomes = []

    def racer():
        c = TestClient(app)
        barrier.wait(timeout=60)
        r = c.post("/api/scanner/pair",
                   json={"terminal_id": "counter-1", "code": code})
        outcomes.append((r.status_code, r))

    threads = [threading.Thread(target=racer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
        assert not t.is_alive(), "pair racer still running after 120s"

    statuses = sorted(s for s, _ in outcomes)
    assert statuses == [200, 409], outcomes
    winner = next(r for s, r in outcomes if s == 200).json()["token"]
    loser = next(r for s, r in outcomes if s == 409).json()
    assert loser["detail"]["status"] == "already_paired"
    # The winner's token actually reads the queue; the race minted no dud.
    assert poll(client, winner).status_code == 200


# --- one-click pairing (/pair/auto) ---------------------------------------------


def _point_at(monkeypatch, path):
    monkeypatch.setenv("HCRM_SCANNER_PAIRING_FILE", str(path))


def test_auto_pair_happy_path_consumes_the_file(client, loopback, tmp_path,
                                                monkeypatch):
    _, code = register(client)
    path = offer_file(tmp_path, code=code)
    _point_at(monkeypatch, path)

    r = auto_pair(client)
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert poll(client, token).status_code == 200      # the token really reads
    assert not path.exists()                            # single-use file

    # The pairing code never appears in any response body.
    assert code not in r.text


def test_auto_pair_second_click_after_success_is_404(client, loopback,
                                                      tmp_path, monkeypatch):
    _, code = register(client)
    path = offer_file(tmp_path, code=code)
    _point_at(monkeypatch, path)
    assert auto_pair(client).status_code == 200
    assert auto_pair(client).status_code == 404


def test_auto_pair_absent_file_is_404(client, loopback, tmp_path, monkeypatch):
    _point_at(monkeypatch, tmp_path / "nope.json")
    r = auto_pair(client)
    assert r.status_code == 404
    assert r.json()["detail"]["status"] == "no_pairing_file"


def test_auto_pair_unreadable_file_is_500_not_404(client, loopback, tmp_path,
                                                   monkeypatch):
    # A permissions victim needs a DIFFERENT operator action than "absent"
    # (re-registering as root reproduces the failure) — hence 500. The
    # unreadable state is simulated with a directory at the file's path
    # (read_text raises IsADirectoryError, an OSError that is not
    # FileNotFoundError): chmod 000 is unreliable in CI — a root runner
    # reads straight through it — and patching pathlib.Path globally is
    # worse than the thing under test.
    path = offer_file(tmp_path)
    path.mkdir()
    _point_at(monkeypatch, path)
    r = auto_pair(client)
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["status"] == "pairing_file_unreadable"
    assert str(path) in detail["message"]


@pytest.mark.parametrize("body", [
    "not json at all",
    '["terminal_id", "pairing_code"]',
    '{"terminal_id": "counter-1"}',
    '{"pairing_code": "ABCD2345"}',
    '{"terminal_id": 7, "pairing_code": "ABCD2345"}',
    '{"terminal_id": "counter-1", "pairing_code": null}',
    '{"terminal_id": "   ", "pairing_code": "ABCD2345"}',
    '{"terminal_id": "counter-1", "pairing_code": "SHORT"}',
    '{"terminal_id": "counter-1", "pairing_code": "TOOLONGCODE9"}',
], ids=["not_json", "json_array", "missing_code", "missing_terminal",
       "non_string_terminal", "null_code", "blank_terminal",
       "code_too_short", "code_too_long"])
def test_auto_pair_malformed_file_is_422(client, loopback, tmp_path,
                                         monkeypatch, body):
    path = offer_file(tmp_path)
    path.write_text(body)
    _point_at(monkeypatch, path)
    r = auto_pair(client)
    assert r.status_code == 422, body
    assert r.json()["detail"]["status"] == "bad_pairing_file"
    assert path.exists()   # consumed on SUCCESS only


def test_auto_pair_wrong_code_is_byte_identical_to_pair(client, loopback,
                                                         tmp_path, monkeypatch):
    _, code = register(client)
    # A stale offer (code from an earlier registration round).
    path = offer_file(tmp_path, code="ABCD2345")
    _point_at(monkeypatch, path)
    a = auto_pair(client)
    b = pair(client, "counter-1", "ABCD2345")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()   # the indistinguishability that matters
    assert path.exists()          # not consumed on failure


def test_auto_pair_unknown_terminal_matches_pairs_401(client, loopback,
                                                       tmp_path, monkeypatch):
    _, code = register(client)
    path = offer_file(tmp_path, terminal_id="no-such-terminal", code=code)
    _point_at(monkeypatch, path)
    a = auto_pair(client)
    b = pair(client, "no-such-terminal", code)
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_auto_pair_already_paired_409_keeps_the_file(client, loopback,
                                                      tmp_path, monkeypatch):
    _, code = register(client)
    assert pair(client, "counter-1", code).status_code == 200
    path = offer_file(tmp_path, code=code)
    _point_at(monkeypatch, path)
    r = auto_pair(client)
    assert r.status_code == 409
    assert r.json()["detail"]["status"] == "already_paired"
    assert path.exists()   # success-only consumption: the evidence stays


def test_auto_pair_is_loopback_gated(client, tmp_path, monkeypatch):
    # No `loopback` fixture: the TestClient host is not in _LOOPBACK_HOSTS.
    # (No registration either — it is loopback-gated the same way; the
    # gate runs before the file, so a dummy offer is enough.)
    _point_at(monkeypatch, offer_file(tmp_path, code="ABCD2345"))
    r = auto_pair(client)
    assert r.status_code == 403
    assert r.json()["detail"]["status"] == "loopback_only"


def test_auto_pair_pair_any_env_admits_lan(client, tmp_path, monkeypatch):
    # Registration itself needs its own env off-loopback — the two flags
    # are set together here, but each is REQUIRED on its own merits below.
    monkeypatch.setenv("HCRM_SCANNER_REGISTER_ANY", "1")
    _, code = register(client)
    _point_at(monkeypatch, offer_file(tmp_path, code=code))
    monkeypatch.setenv("HCRM_SCANNER_PAIR_ANY", "1")
    assert auto_pair(client).status_code == 200


def test_auto_pair_register_any_does_not_open_pairing(client, tmp_path,
                                                       monkeypatch):
    # The two flags are independent by design: REGISTER_ANY opens terminal
    # MINTING, not the one-click claim of the read token.
    _point_at(monkeypatch, offer_file(tmp_path, code="ABCD2345"))
    monkeypatch.setenv("HCRM_SCANNER_REGISTER_ANY", "1")
    assert auto_pair(client).status_code == 403


def test_auto_pair_requires_json_content_type(client, loopback, tmp_path,
                                                monkeypatch):
    _, code = register(client)
    _point_at(monkeypatch, offer_file(tmp_path, code=code))
    r = client.post("/api/scanner/pair/auto", content="",
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    assert r.json()["detail"]["status"] == "json_required"
    # Nothing was minted and the offer survives (gates run before the file).
    assert pair(client, "counter-1", code).status_code == 200


def test_auto_pair_never_leaks_the_code_in_a_response(client, loopback,
                                                        tmp_path, monkeypatch):
    _, code = register(client)
    path = offer_file(tmp_path, code=code)
    _point_at(monkeypatch, path)
    for _ in range(2):   # 200 then 404 — every body must stay code-free
        r = auto_pair(client)
        assert code not in r.text


# --- ingest + server-side classification ------------------------------------------

def test_kinds_are_derived_server_side(client, loopback):
    secret, code = register(client)
    token = pair(client, "counter-1", code).json()["token"]
    # (sent, stored, expected kind): "   " strips to "" and classifies
    # unshaped — the queue carries what was scanned, verbatim.
    table = [
        (BADGE, BADGE, "badge"),            # full payload
        (RAW_TOKEN, RAW_TOKEN, "badge"),    # prefix stripped by a bridge
        ("8412345678905", "8412345678905", "barcode"),   # GTIN
        ("EX-007", "EX-007", "barcode"),   # opaque SKU
        ("   ", "", "unshaped"),            # whitespace-only
    ]
    for sent, _, _ in table:
        assert ingest(client, secret, sent).status_code == 201, sent
    got = [(e["code"], e["kind"]) for e in poll(client, token).json()["events"]]
    assert got == [(stored, kind) for _, stored, kind in table]


def test_ingest_rejects_bad_or_missing_secret(client, loopback):
    secret, _ = register(client)
    assert ingest(client, "not-the-secret", "8412345678905").status_code == 401
    r = client.post("/api/scanner/events", json={"code": "8412345678905"})
    assert r.status_code == 401
    assert r.json()["detail"]["status"] == "unknown_terminal"


def test_secret_may_ride_in_the_body(client, loopback):
    secret, _ = register(client)
    r = client.post("/api/scanner/events",
                    json={"code": "EX-007", "secret": secret})
    assert r.status_code == 201


def test_credentials_may_ride_authorization_bearer(client, loopback, admin_token):
    # The SPA's shared api() helper attaches foreign credentials ONLY via
    # Authorization: Bearer (its {token, raw} form, round 4 T6) — so both
    # endpoints accept that presentation. A USER bearer token simply
    # matches no terminal hash and 401s like any unknown credential.
    secret, code = register(client)
    token = pair(client, "counter-1", code).json()["token"]

    r = client.post("/api/scanner/events", json={"code": "EX-007"},
                    headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 201, r.text
    r = client.get("/api/scanner/events?after=0",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert [e["code"] for e in r.json()["events"]] == ["EX-007"]

    # A member session token is NOT a terminal credential — same 401 as a
    # garbage string (no leak, no accidental cross-auth).
    r = client.post("/api/auth/login", json={
        "email": "admin@shop.local",
        "password": ADMIN_PASSWORD_AFTER_CHANGE})
    member_token = r.json()["token"]
    assert client.get("/api/scanner/events?after=0",
                      headers={"Authorization": f"Bearer {member_token}"}).status_code == 401


def test_overlong_codes_are_rejected(client, loopback):
    secret, _ = register(client)
    assert ingest(client, secret, "A" * 129).status_code == 422


def test_empty_bearer_falls_back_to_the_body_secret(client, loopback):
    # "Authorization: Bearer " (empty) is no credential — the body secret
    # must still be honoured rather than 401ing a valid presentation.
    secret, _ = register(client)
    r = client.post("/api/scanner/events", json={"code": "EX-007",
                                                 "secret": secret},
                    headers={"Authorization": "Bearer "})
    assert r.status_code == 201


# --- polling: cursor, at-most-once, isolation ---------------------------------------

def test_cursor_and_at_most_once(client, loopback):
    secret, code = register(client)
    token = pair(client, "counter-1", code).json()["token"]
    seqs = [ingest(client, secret, c).json()["seq"] for c in ("A1", "B2", "C3")]

    first = poll(client, token).json()
    assert [e["code"] for e in first["events"]] == ["A1", "B2", "C3"]
    assert first["latest_seq"] == seqs[-1]

    # Second poll — same cursor or after=0 (the two-tab case): NOTHING.
    # The events were marked consumed in the first poll's commit.
    assert poll(client, token, after=seqs[-1]).json()["events"] == []
    assert poll(client, token, after=0).json()["events"] == []

    # A genuinely new event still arrives after the cursor.
    seq_new = ingest(client, secret, "D4").json()["seq"]
    body = poll(client, token, after=seqs[-1]).json()
    assert [e["code"] for e in body["events"]] == ["D4"]
    assert body["latest_seq"] == seq_new


def test_seq_is_monotonic_across_a_full_prune(client, loopback):
    # Regression: a plain INTEGER PRIMARY KEY restarts at 1 once pruning
    # empties the table, so a NEW event lands at/below a live client's
    # cursor and is never delivered. sqlite_autoincrement keeps seq
    # monotonic forever (sqlite_sequence high-water mark).
    secret, code = register(client)
    token = pair(client, "counter-1", code).json()["token"]
    seq = ingest(client, secret, "A1").json()["seq"]
    assert poll(client, token).json()["events"]          # consumes it
    ingest(client, secret, "B2")                         # touch: prunes A1
    assert events_rows("counter-1")[0].seq > seq         # B2 did NOT reuse 1
    seq_b = ingest(client, secret, "C3").json()["seq"]
    poll(client, token)                                   # empties the queue…
    poll(client, token)                                   # …and the next touch prunes ALL
    assert events_rows("counter-1") == []
    seq_d = ingest(client, secret, "D4").json()["seq"]   # table was empty!
    assert seq_d > seq_b
    assert [e["code"] for e in
            poll(client, token, after=seq_b).json()["events"]] == ["D4"]


def test_poll_rejects_bad_or_missing_token(client, loopback):
    _, code = register(client)
    assert pair(client, "counter-1", code).status_code == 200
    assert client.get("/api/scanner/events").status_code == 401
    r = client.get("/api/scanner/events", headers={"X-Scanner-Token": "nope"})
    assert r.status_code == 401
    assert r.json()["detail"]["status"] == "unknown_terminal"


def test_credentials_are_not_interchangeable(client, loopback):
    # The capability split, pinned: the SECRET writes, the TOKEN reads,
    # and neither works on the other's endpoint (a stolen read token
    # cannot inject scans; a stolen write secret cannot read the
    # badge-bearing queue).
    secret, code = register(client)
    token = pair(client, "counter-1", code).json()["token"]
    assert ingest(client, token, "8412345678905").status_code == 401
    r = client.get("/api/scanner/events", headers={"X-Scanner-Token": secret})
    assert r.status_code == 401


def test_per_terminal_isolation(client, loopback):
    secret_a, code_a = register(client, "counter-a")
    secret_b, code_b = register(client, "counter-b")
    token_a = pair(client, "counter-a", code_a).json()["token"]
    token_b = pair(client, "counter-b", code_b).json()["token"]

    ingest(client, secret_a, "8412345678905")
    ingest(client, secret_a, BADGE)

    assert poll(client, token_b).json()["events"] == []          # B sees nothing
    got = [e["code"] for e in poll(client, token_a).json()["events"]]
    assert got == ["8412345678905", BADGE]                       # A sees its own
    # A's SECRET cannot write into B's queue either (it identifies A).
    ingest(client, secret_a, "EX-007")
    assert poll(client, token_b, after=0).json()["events"] == []


# --- pruning --------------------------------------------------------------------------

def test_stale_events_are_pruned_not_delivered(client, loopback):
    secret, code = register(client)
    token = pair(client, "counter-1", code).json()["token"]
    ingest(client, secret, "8412345678905")
    age_events("counter-1", seconds=scanner_router.SCANNER_EVENT_TTL_SECONDS + 60)

    body = poll(client, token).json()
    assert body["events"] == []                    # too old to deliver
    assert events_rows("counter-1") == []          # and pruned from the table


def test_consumed_events_are_pruned_on_the_next_touch(client, loopback):
    secret, code = register(client)
    token = pair(client, "counter-1", code).json()["token"]
    ingest(client, secret, "8412345678905")
    assert poll(client, token).json()["events"]    # consumes it
    assert events_rows("counter-1")                # row lingers one touch…

    ingest(client, secret, "EX-007")               # …any touch prunes it
    rows = events_rows("counter-1")
    assert [r.code for r in rows] == ["EX-007"]    # only the fresh one remains


# --- deleting terminals: the auth matrix ----------------------------------------------

def test_loopback_delete_needs_no_user_auth(client, loopback):
    # The recovery path: a co-located operator un-registers without any
    # user session (the same trust boundary as register).
    register(client, "counter-1")
    assert client.delete("/api/scanner/terminals/counter-1").status_code == 204


def test_delete_auth_matrix_off_loopback(client, monkeypatch, admin_token):
    # Off loopback: anonymous → 401, member → 403, staff/admin → 204.
    # A distinct terminal per case: the 401/403 cases leave it registered.
    monkeypatch.setenv("HCRM_SCANNER_REGISTER_ANY", "1")  # register w/o loopback
    _, member_token = create_user(client, admin_token, role="member")

    register(client, "counter-1")
    assert client.delete("/api/scanner/terminals/counter-1").status_code == 401

    register(client, "counter-2")
    r = client.delete("/api/scanner/terminals/counter-2",
                      headers=auth(member_token))
    assert r.status_code == 403
    assert r.json()["detail"]["status"] == "staff_required"

    register(client, "counter-3")
    r = client.delete("/api/scanner/terminals/counter-3",
                      headers=auth(admin_token))
    assert r.status_code == 204


def test_delete_without_loopback_needs_a_user_token(client, admin_token):
    # No loopback fixture: anonymous delete is a 401, not a silent pass.
    r = client.delete("/api/scanner/terminals/counter-1")
    assert r.status_code == 401


def test_delete_unknown_terminal_404s(client, loopback, admin_token):
    r = client.delete("/api/scanner/terminals/nope",
                      headers=auth(admin_token))
    assert r.status_code == 404
