"""P2 slice 3 (Heartbeater, reassigned from mimo): honest device-state reporting.

All injectable: post, clock, sleep, probe, the grab triple. No hardware, no
network, no real sleeping in the interval test (the fake clock advances).
"""

import errno
import threading
import time

import pytest

from scripts.scanner_daemon import (
    DeviceLostError, GrabPermissionError, Heartbeater, LocalAgentServer,
    NonceStore, OfferStore, PostError, _device_probe, run_loop,
)
from tests.test_scanner_daemon import _input_class, _make_event_node
from tests.test_scanner_daemon_grab import FakeGrab

SECRET = "S" * 43
PRESENT = {"status": "present", "node": "/dev/input/event3",
           "personality": "hid-a001"}
ABSENT = {"status": "absent", "node": None, "personality": None}
NOPERM = {"status": "no-permission", "node": "/dev/input/event3",
          "personality": "hid-a001"}


def _hb(post, *, probe=None, **kw):
    return Heartbeater(post, "http://shop:8000", "laptop-1", SECRET,
                       probe=probe or (lambda: dict(PRESENT)), **kw)


@pytest.fixture()
def scanner_sysfs(tmp_path):
    root = _input_class(tmp_path)
    _make_event_node(root, "event3", "065a", "a001")
    return root


def test_heartbeat_maps_grabbed_device_to_present(tmp_path):
    root = _input_class(tmp_path)
    _make_event_node(root, "event3", "065a", "a001")
    fake = FakeGrab()                       # open + grab succeed → "free"
    probed = _device_probe(sysfs_root=root, opener=fake.opener,
                           ioctl=fake.ioctl, closer=fake.closer)
    assert probed["status"] == "present"
    assert probed["personality"] == "hid-a001"
    posts = []
    hb = _hb(lambda u, h, b: posts.append((u, h, b)) or {},
             probe=lambda: dict(probed))
    hb.beat_once()
    url, headers, body = posts[0]
    assert url == "http://shop:8000/api/scanner/terminals/laptop-1/heartbeat"
    assert headers["X-Scanner-Secret"] == SECRET
    assert body == {"device_status": "present", "personality": "hid-a001"}
    assert hb.snapshot()["status"] == "present"


def test_heartbeat_maps_missing_node_to_absent(tmp_path):
    root = _input_class(tmp_path)           # empty: no scanner node
    probed = _device_probe(sysfs_root=root)
    assert probed == ABSENT
    posts = []
    hb = _hb(lambda u, h, b: posts.append(b) or {}, probe=lambda: dict(probed))
    hb.beat_once()
    assert posts[0] == {"device_status": "absent", "personality": None}
    assert hb.snapshot()["status"] == "absent"


def test_heartbeat_maps_permission_error_to_no_permission_never_absent(tmp_path):
    root = _input_class(tmp_path)
    _make_event_node(root, "event3", "065a", "a001")
    fake = FakeGrab(open_error=PermissionError(errno.EACCES, "denied"))
    probed = _device_probe(sysfs_root=root, opener=fake.opener,
                           ioctl=fake.ioctl, closer=fake.closer)
    assert probed["status"] == "no-permission"   # NEVER absent
    assert probed["node"] == "/dev/input/event3"
    posts = []
    hb = _hb(lambda u, h, b: posts.append(b) or {}, probe=lambda: dict(probed))
    hb.beat_once()
    assert posts[0]["device_status"] == "no-permission"
    assert hb.snapshot()["status"] == "no-permission"


def test_heartbeat_posts_on_state_change_and_interval():
    t = [0.0]
    posts = []
    stop = threading.Event()
    state = dict(ABSENT)

    def post(u, h, b):
        posts.append((dict(b), t[0]))
        if len(posts) >= 3:
            stop.set()
        return {}

    hb = _hb(post, interval=5, stop_event=stop, clock=lambda: t[0],
             sleep=lambda s: t.__setitem__(0, t[0] + s),
             probe=lambda: dict(state))
    # a grab-thread state change wakes an immediate beat
    hb.update("present", "/dev/input/event3", "hid-a001")
    assert hb._wake.is_set()
    assert hb.snapshot()["status"] == "present"
    state.update(PRESENT)
    hb.start()
    hb.stop()
    assert len(posts) >= 3                       # start + interval beats
    assert posts[0][0]["device_status"] == "present"
    assert all(p[0]["device_status"] == "present" for p in posts)
    assert posts[-1][1] > posts[0][1]            # the clock advanced (interval)


def test_heartbeat_401_logs_once_stops_heartbeating_and_never_exits():
    logs, posts = [], []

    def post(u, h, b):
        posts.append(b)
        raise PostError(401, "unknown_terminal")

    hb = _hb(post, log=logs.append)
    hb.beat_once()
    hb.beat_once()
    hb.beat_once()
    assert len(posts) == 1                       # stopped after the first 401
    assert len(logs) == 1                        # ONE actionable warning
    assert "401" in logs[0] and "re-register" in logs[0]
    assert hb._revoked is True
    assert hb.snapshot()["status"] == "present"  # process alive, state intact
    # starting the loop after revocation must not raise or exit either
    hb.start()
    hb.stop()
    assert len(posts) == 1


def test_heartbeat_stop_joins_within_two_seconds_without_raising():
    hb = _hb(lambda u, h, b: {}, interval=5, probe=lambda: dict(ABSENT))
    hb.start()
    assert hb._thread.is_alive()
    began = time.monotonic()
    hb.stop()                                    # never raises, joins <= 2s
    assert time.monotonic() - began < 2.0
    assert not hb._thread.is_alive()


# --- F2: a non-numeric interval clamps to 30 with ONE warning, no traceback --

def test_heartbeat_nonnumeric_env_clamps_to_default_with_one_warning(monkeypatch):
    monkeypatch.setenv("HCRM_SCANNER_HEARTBEAT_SECONDS", "not-a-number")
    logs = []
    hb = _hb(lambda u, h, b: {}, log=logs.append)
    assert hb.interval == 30
    assert len(logs) == 1 and "30" in logs[0]


def test_heartbeat_nonnumeric_flag_clamps_to_default_with_one_warning():
    logs = []
    hb = _hb(lambda u, h, b: {}, interval="not-a-number", log=logs.append)
    assert hb.interval == 30
    assert len(logs) == 1 and "30" in logs[0]


def test_heartbeat_interval_still_clamps_to_minimum_five():
    assert _hb(lambda u, h, b: {}, interval="3").interval == 5
    assert _hb(lambda u, h, b: {}, interval="12").interval == 12


# --- F3: the four run_loop on_state hook points ------------------------------

def _recorder():
    states = []
    return states, lambda s, n, p: states.append((s, n, p))


def test_run_loop_reports_present_on_grab(scanner_sysfs):
    fake = FakeGrab()
    states, on_state = _recorder()

    def reader(fd, on_unmapped=None):
        yield "8412345678905"
        return                                   # EOF → device loss → stop

    run_loop(lambda c: None, sysfs_root=scanner_sysfs, opener=fake.opener,
             ioctl=fake.ioctl, closer=fake.closer, scan_reader=reader,
             sleep=lambda s: None, stop=lambda: True, on_state=on_state)
    assert states[0][0] == "present"
    assert states[0][2] == "hid-a001"
    assert states[0][1]                          # the grabbed node


def test_run_loop_reports_absent_on_device_loss(scanner_sysfs):
    fake = FakeGrab()
    states, on_state = _recorder()

    def reader(fd, on_unmapped=None):
        yield "8412345678905"
        return                                   # EOF → DeviceLostError

    run_loop(lambda c: None, sysfs_root=scanner_sysfs, opener=fake.opener,
             ioctl=fake.ioctl, closer=fake.closer, scan_reader=reader,
             sleep=lambda s: None, stop=lambda: True, on_state=on_state)
    assert [s[0] for s in states] == ["present", "absent"]
    assert states[1] == ("absent", None, None)


def test_run_loop_reports_no_permission_before_loud_raise(scanner_sysfs):
    fake = FakeGrab(open_error=PermissionError(errno.EACCES, "denied"))
    states, on_state = _recorder()
    with pytest.raises(GrabPermissionError):
        run_loop(lambda c: None, sysfs_root=scanner_sysfs, opener=fake.opener,
                 ioctl=fake.ioctl, closer=fake.closer,
                 scan_reader=lambda fd, on_unmapped=None: iter(()),
                 sleep=lambda s: None, stop=lambda: True, on_state=on_state)
    # the hook fired BEFORE the loud raise, and never as "absent"
    assert len(states) == 1
    assert states[0][0] == "no-permission"
    assert states[0][2] == "hid-a001"
    assert states[0][1]                          # the node that exists


def test_run_loop_reports_absent_when_no_device_found(tmp_path):
    root = _input_class(tmp_path)                # empty: nothing discovered
    states, on_state = _recorder()
    run_loop(lambda c: None, sysfs_root=root, scan_reader=lambda fd, on_unmapped=None: iter(()),
             sleep=lambda s: None, stop=lambda: True, on_state=on_state)
    assert states == [("absent", None, None)]


# --- F4: a set stop_event suppresses the reclaim POST ------------------------

def test_stop_event_suppresses_reclaim_post(tmp_path):
    calls = []

    def post(u, h, b):
        calls.append((u, h, b))
        return {"pairing_code": "7Q2MB4XW"}

    config = {"terminal_id": "t", "secret": SECRET,
              "server_url": "http://shop:8000", "site_origin": "http://shop:8000"}
    stop = threading.Event()
    server = LocalAgentServer(
        0, config=config, offers=OfferStore(tmp_path / "offer.json", "t"),
        nonces=NonceStore(), device=lambda: dict(ABSENT), post=post,
        stop_event=stop)
    stop.set()
    assert server._reclaim_code() is None
    assert calls == []                           # NO reclaim POST when stopping
    # control: without the stop signal the same call DOES post and rotate
    stop.clear()
    assert server._reclaim_code() == "7Q2MB4XW"
    assert len(calls) == 1 and calls[0][0].endswith("/terminals/t/reclaim")
