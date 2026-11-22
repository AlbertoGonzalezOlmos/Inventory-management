"""P2 loopback popup agent tests (real loopback HTTP, fake everything else)."""

import http.client
import json
import re
import socket
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

import scripts.scanner_daemon as daemon
from tests.test_scanner_daemon import _input_class, _make_event_node
from tests.test_scanner_daemon_forward import PostScript, reader_yielding
from tests.test_scanner_daemon_grab import FakeGrab

CODE = "7Q2MB4XW"
SECRET = "S" * 43
SITE = "http://shop.example:8000"


class AgentFixture:
    def __init__(self, tmp_path):
        self.clock = [1000.0]
        self.wall = [2000.0]
        self.post = PostScript([])
        self.config = {"terminal_id": "counter-1", "secret": SECRET,
                       "server_url": SITE, "site_origin": SITE,
                       "machine_key": "0123456789abcdef"}
        self.offers = daemon.OfferStore(
            tmp_path / "offer.json", "counter-1",
            clock=lambda: self.clock[0], wall_clock=lambda: self.wall[0])
        self.offers.put(CODE)
        self.nonces = daemon.NonceStore(clock=lambda: self.clock[0])
        self.stop_event = threading.Event()
        self.server = daemon.LocalAgentServer(
            0, config=self.config, offers=self.offers, nonces=self.nonces,
            device=lambda: {"status": "present", "personality": "hid-a001",
                            "node": "/dev/input/event3"},
            post=self.post, stop_event=self.stop_event)
        assert self.server.start() is True

    def close(self):
        self.server.close()


@pytest.fixture()
def agent(tmp_path):
    fixture = AgentFixture(tmp_path)
    try:
        yield fixture
    finally:
        fixture.close()


def request(agent, path, *, host=None, origin=SITE, referer=None, method="GET"):
    conn = http.client.HTTPConnection("127.0.0.1", agent.server.port, timeout=3)
    try:
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{agent.server.port}")
        if origin is not None:
            conn.putheader("Origin", origin)
        if referer is not None:
            conn.putheader("Referer", referer)
        conn.endheaders()
        response = conn.getresponse()
        body = response.read()
        return response.status, dict(response.getheaders()), body
    finally:
        conn.close()


def nonce(agent):
    return agent.nonces.issue()


def pair(agent, value, **kwargs):
    return request(agent, f"/pair?nonce={value}", **kwargs)


def bootstrap(agent, reclaim="0"):
    status, headers, _ = request(agent, f"/pair?reclaim={reclaim}")
    assert status == 307
    location = headers["Location"]
    assert headers["Cache-Control"] == "no-store"
    return request(agent, location)


def offer(body):
    match = re.search(rb"const OFFER = (.*?); const SITE_ORIGIN", body, re.S)
    assert match is not None
    return json.loads(match.group(1))


def test_pair_bootstrap_exact_page_csp_and_payload(agent):
    status, headers, body = bootstrap(agent)
    assert status == 200
    payload = offer(body)
    assert payload == {
        "type": "hcrm.agent.offer", "nonce": payload["nonce"],
        "terminal_id": "counter-1", "pairing_code": CODE,
        "status": {"device": "present", "personality": "hid-a001",
                   "node": "/dev/input/event3"},
        "reason": None,
    }
    assert len(payload["nonce"]) == 22
    assert headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Content-Security-Policy"] == (
        f"default-src 'none'; script-src 'nonce-{payload['nonce']}'; "
        "connect-src 'self'; base-uri 'none'")
    assert "Access-Control-Allow-Origin" not in headers
    text = body.decode()
    assert f'<script nonce="{payload["nonce"]}">' in text
    assert "window.opener.postMessage(OFFER, SITE_ORIGIN)" in text
    assert "setTimeout(() => window.close(), 250)" in text
    assert 'postMessage(OFFER, "*")' not in text


def test_host_guard_rejects_rebinding_before_any_offer(agent):
    status, _, body = request(agent, "/status", host="evil.example")
    assert status == 403 and json.loads(body) == {"error": "bad_host"}
    status, _, body = pair(agent, nonce(agent), host="evil.example")
    assert status == 403 and CODE.encode() not in body
    assert agent.post.calls == []


def test_pair_origin_and_referer_rules(agent):
    value = nonce(agent)
    status, _, body = pair(agent, value, origin=None)
    assert status == 403 and CODE.encode() not in body

    value = nonce(agent)
    status, _, body = pair(agent, value, origin="http://attacker.example",
                           referer=SITE + "/")
    assert status == 403 and CODE.encode() not in body

    status, _, body = pair(agent, nonce(agent), origin=SITE)
    assert status == 200 and offer(body)["pairing_code"] == CODE

    status, _, body = pair(agent, nonce(agent), origin=None,
                           referer=SITE + "/")
    assert status == 200 and offer(body)["pairing_code"] == CODE

    status, _, body = pair(agent, nonce(agent), origin=None,
                           referer="http://attacker.example/")
    assert status == 403 and CODE.encode() not in body


def test_nonce_single_use_expiry_and_cap(agent):
    value = nonce(agent)
    assert pair(agent, value)[0] == 200
    assert pair(agent, value)[0] == 403
    expired = nonce(agent)
    agent.clock[0] += 121
    assert pair(agent, expired)[0] == 403
    assert agent.post.calls == []

    capped = daemon.NonceStore(cap=2, clock=lambda: 1000.0)
    oldest, second, third = (capped.issue() for _ in range(3))
    assert capped.consume(oldest) is False
    assert capped.consume(second) is True
    assert capped.consume(third) is True


def test_status_is_host_guarded_and_never_leaks_credentials(agent):
    status, headers, body = request(agent, "/status", origin=None)
    assert status == 200
    assert "Access-Control-Allow-Origin" not in headers
    payload = json.loads(body)
    assert payload == {
        "ok": True, "terminal_id": "counter-1", "server_url": SITE,
        "device": {"status": "present", "personality": "hid-a001",
                   "node": "/dev/input/event3"},
        "pairing_code_available": True, "agent_version": "1",
    }
    assert CODE not in body.decode() and SECRET not in body.decode()
    assert "pairing_code" not in payload and "secret" not in payload


def test_reclaim_precedes_offer_and_refreshes_memory_and_file(agent):
    fresh = "NEW23456"
    agent.post.outcomes = [{"pairing_code": fresh}]
    status, _, body = bootstrap(agent, reclaim="1")
    assert status == 200 and offer(body)["pairing_code"] == fresh
    assert len(agent.post.calls) == 1
    call = agent.post.calls[0]
    assert call["url"].endswith("/api/scanner/terminals/counter-1/reclaim")
    assert call["headers"]["X-Scanner-Secret"] == SECRET
    assert agent.offers.get() == fresh
    saved = json.loads((agent.offers.path).read_text())
    assert saved["pairing_code"] == fresh and "expires_at" in saved
    # The same nonce cannot trigger a second token rotation.
    location_nonce = offer(body)["nonce"]
    assert pair(agent, location_nonce)[0] == 403
    assert len(agent.post.calls) == 1


def test_device_snapshot_failure_never_emits_an_offer(agent):
    agent.server.device = daemon._device_not_configured
    status, _, body = pair(agent, nonce(agent))
    assert status == 503 and json.loads(body) == {"error": "device_unavailable"}
    assert CODE.encode() not in body


def test_reclaim_failure_and_no_offer_use_only_the_allowed_reasons(agent):
    agent.post.outcomes = [daemon.PostError(401, "unknown terminal")]
    status, _, body = bootstrap(agent, reclaim="1")
    assert status == 200
    payload = offer(body)
    assert payload["reason"] == "reclaim_failed"
    assert payload["pairing_code"] is None

    agent.offers = daemon.OfferStore(agent.offers.path.with_name("absent.json"),
                                     "counter-1")
    agent.server.offers = agent.offers
    status, _, body = pair(agent, nonce(agent))
    assert status == 200
    assert offer(body)["reason"] == "no_offer"


def test_offer_store_restart_ttl_and_legacy_file_fallback(tmp_path):
    mono, wall = [100.0], [1000.0]
    path = tmp_path / "offer.json"
    store = daemon.OfferStore(path, "counter-1", clock=lambda: mono[0],
                              wall_clock=lambda: wall[0])
    store.put(CODE)
    restarted = daemon.OfferStore(path, "counter-1", clock=lambda: mono[0],
                                  wall_clock=lambda: wall[0])
    assert restarted.get() == CODE
    mono[0] += 601
    wall[0] += 601
    assert restarted.get() is None

    legacy = tmp_path / "legacy.json"
    daemon.save_pairing_file(legacy, "counter-1", CODE)
    assert daemon.OfferStore(legacy, "counter-1").get() == CODE


def test_port_collision_is_loud_and_close_is_idempotent(agent):
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        lines = []
        other = daemon.LocalAgentServer(
            port, config=agent.config, offers=agent.offers, nonces=agent.nonces,
            device=lambda: {}, post=agent.post, log=lines.append)
        assert other.start() is False
        assert len(lines) == 1 and "already in use" in lines[0]
        other.close()  # failed bind also closes cleanly
    finally:
        sock.close()
    agent.server.close()
    agent.server.close()
    assert agent.server._http is None and agent.server._thread is None


def _run_main(tmp_path, monkeypatch, extra=()):
    cfg = tmp_path / "scanner.json"
    config = {"terminal_id": "counter-1", "secret": SECRET,
              "server_url": SITE}
    if "--site-origin" in extra:
        config["site_origin"] = SITE
    cfg.write_text(json.dumps(config))
    root = _input_class(tmp_path)
    _make_event_node(root, "event3", "065a", "a001")
    fake = FakeGrab()
    code = daemon.main(["--config", str(cfg), *extra],
                       post=PostScript([{}]), opener=fake.opener,
                       ioctl=fake.ioctl, closer=fake.closer,
                       scan_reader=reader_yielding(),
                       lock_path=str(tmp_path / "daemon.lock"),
                       sysfs_root=root, sleep=lambda s: None)
    return code, fake


def test_legacy_main_never_constructs_local_server(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("legacy main must not bind a port")
    monkeypatch.setattr(daemon, "LocalAgentServer", forbidden)
    assert _run_main(tmp_path, monkeypatch)[0] == 0


def test_main_local_server_starts_and_stops_with_grab(tmp_path, monkeypatch):
    started, closed = [], []

    class RecordingServer(daemon.LocalAgentServer):
        def start(self):
            result = super().start()
            started.append(result)
            return result

        def close(self):
            closed.append(True)
            return super().close()

    monkeypatch.setattr(daemon, "LocalAgentServer", RecordingServer)
    code, fake = _run_main(tmp_path, monkeypatch, extra=("--local-port", "0"))
    assert code == 0
    assert started == [True] and closed == [True]
    assert fake.releases() and fake.closed


def test_main_site_origin_alone_starts_local_server(tmp_path, monkeypatch):
    started = []

    class RecordingServer(daemon.LocalAgentServer):
        def start(self):
            result = super().start()
            started.append(result)
            return result

    monkeypatch.setattr(daemon, "LocalAgentServer", RecordingServer)
    code, _ = _run_main(tmp_path, monkeypatch,
                        extra=("--site-origin", SITE))
    assert code == 0 and started == [True]


def test_main_port_collision_keeps_grab_alive(tmp_path, capsys):
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        code, fake = _run_main(tmp_path, None, extra=("--local-port", str(port)))
        assert code == 0
        assert fake.releases() and fake.closed
        assert "already in use" in capsys.readouterr().err
    finally:
        sock.close()
