"""P2 qwen slice: portable config/XDG/machine_key + invite registration.

Hardware-free: inject post (PostScript), the grab triple (FakeGrab), the scan
reader, machine_id_path and hostname. The legacy same-host path is pinned
unchanged; the portable path defaults to XDG and writes the offer beside it.
"""

import hashlib
import json
import os
import stat
import tempfile

import pytest

from scripts.scanner_daemon import (
    PostError, derive_machine_key, load_config, main, portable_config_file,
    save_config,
)
from tests.test_scanner_daemon import _input_class, _make_event_node
from tests.test_scanner_daemon_forward import (
    PAIRING_CODE, PostScript, reader_yielding,
)
from tests.test_scanner_daemon_grab import FakeGrab

INVITE = "7Q2MB4XW7Q2MB4XW"          # 16 Crockford chars
MACHINE_ID = "test-machine-id-123\n"
EXPECTED_KEY = hashlib.sha256(MACHINE_ID.strip().encode()).hexdigest()[:16]


@pytest.fixture()
def scanner_sysfs(tmp_path):
    root = _input_class(tmp_path)
    _make_event_node(root, "event3", "065a", "a001")
    return root


@pytest.fixture()
def machine_id(tmp_path):
    p = tmp_path / "machine-id"
    p.write_text(MACHINE_ID)
    return str(p)


def run(args, post, scanner_sysfs, *, machine_id_path="/nonexistent-machine-id",
        hostname="laptop-1", reader=None, clock=None):
    fake = FakeGrab()
    lock = os.path.join(tempfile.mkdtemp(prefix="hcrm-p2-"), "daemon.lock")
    return main(args, post=post, opener=fake.opener, ioctl=fake.ioctl,
                closer=fake.closer, scan_reader=reader or reader_yielding(),
                lock_path=lock, sysfs_root=scanner_sysfs,
                sleep=lambda s: None, clock=clock, hostname=hostname,
                machine_id_path=machine_id_path)


# --- machine_key -------------------------------------------------------------

def test_machine_key_from_machine_id(machine_id):
    assert derive_machine_key(machine_id_path=machine_id) == EXPECTED_KEY


def test_machine_key_fallback_is_16hex_and_persisted(tmp_path):
    mk = derive_machine_key(machine_id_path="/nonexistent-machine-id")
    assert len(mk) == 16 and all(c in "0123456789abcdef" for c in mk)
    cfg = tmp_path / "scanner.json"
    save_config(cfg, "t", "S" * 43, machine_key=mk)
    # absent machine-id + a persisted key → reused, never a fresh random
    assert derive_machine_key(load_config(cfg),
                              machine_id_path="/nonexistent-machine-id") == mk


def test_machine_key_fallback_reuses_config_value():
    assert derive_machine_key({"machine_key": "0123456789abcdef"},
                              machine_id_path="/nonexistent-machine-id") \
        == "0123456789abcdef"


def test_machine_id_wins_over_config(tmp_path, machine_id):
    # a present /etc/machine-id always beats a stored fallback
    assert derive_machine_key({"machine_key": "0123456789abcdef"},
                              machine_id_path=machine_id) == EXPECTED_KEY


# --- XDG config path ---------------------------------------------------------

def test_portable_config_path_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv("HCRM_SCANNER_CONFIG", str(tmp_path / "explicit.json"))
    assert portable_config_file() == tmp_path / "explicit.json"
    monkeypatch.delenv("HCRM_SCANNER_CONFIG")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert portable_config_file() == tmp_path / "xdg" / "hcrm" / "scanner.json"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert portable_config_file() == tmp_path / "home" / ".config" / "hcrm" / "scanner.json"


# --- portable registration ---------------------------------------------------

def _portable_args(cfg=None, *extra):
    args = ["--register", "--invite", INVITE, "--server", "http://shop:8000",
            "--site-origin", "http://shop:8000", "--register-only"]
    if cfg:
        args += ["--config", str(cfg)]
    return args + list(extra)


def test_portable_register_writes_xdg_config_0600(tmp_path, scanner_sysfs,
                                                  machine_id, monkeypatch, capsys):
    cfg = tmp_path / "xdg" / "hcrm" / "scanner.json"
    monkeypatch.setenv("HCRM_SCANNER_CONFIG", str(cfg))
    post = PostScript([{"pairing_code": PAIRING_CODE}])
    code = run(_portable_args(), post, scanner_sysfs, machine_id_path=machine_id)
    assert code == 0
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    saved = json.loads(cfg.read_text())
    assert saved["terminal_id"] == "laptop-1"
    assert saved["machine_key"] == EXPECTED_KEY
    assert saved["server_url"] == "http://shop:8000"
    assert saved["site_origin"] == "http://shop:8000"
    assert len(saved["secret"]) >= 32
    outerr = capsys.readouterr()
    assert INVITE not in cfg.read_text()        # invite never in config
    assert INVITE not in outerr.out + outerr.err  # never logged


def test_portable_register_posts_invite_and_machine_key(tmp_path, scanner_sysfs,
                                                        machine_id, monkeypatch):
    monkeypatch.setenv("HCRM_SCANNER_CONFIG", str(tmp_path / "c.json"))
    post = PostScript([{"pairing_code": PAIRING_CODE}])
    run(_portable_args(), post, scanner_sysfs, machine_id_path=machine_id)
    body = post.calls[0]["body"]
    assert post.calls[0]["url"] == "http://shop:8000/api/scanner/terminals"
    assert body["invite"] == INVITE
    assert body["machine_key"] == EXPECTED_KEY
    assert body["terminal_id"] == "laptop-1"


def test_portable_offer_file_beside_config_with_expiry(tmp_path, scanner_sysfs,
                                                       machine_id, monkeypatch):
    cfg = tmp_path / "hcrm" / "scanner.json"
    monkeypatch.setenv("HCRM_SCANNER_CONFIG", str(cfg))
    post = PostScript([{"pairing_code": PAIRING_CODE}])
    run(_portable_args(), post, scanner_sysfs, machine_id_path=machine_id)
    offer = cfg.with_name("scanner-pairing.json")   # R3: beside the XDG config
    assert offer.exists()
    assert stat.S_IMODE(offer.stat().st_mode) == 0o600
    saved = json.loads(offer.read_text())
    assert saved["terminal_id"] == "laptop-1"
    assert saved["pairing_code"] == PAIRING_CODE
    assert isinstance(saved["expires_at"], (int, float))


def test_portable_register_only_exits_without_grab(tmp_path, scanner_sysfs,
                                                   machine_id, monkeypatch):
    monkeypatch.setenv("HCRM_SCANNER_CONFIG", str(tmp_path / "c.json"))
    post = PostScript([{"pairing_code": PAIRING_CODE}])
    # reader that would fail if the grab loop ran: register-only must exit first
    code = run(_portable_args(), post, scanner_sysfs, machine_id_path=machine_id,
               reader=reader_yielding(then=AssertionError))
    assert code == 0


# --- 409 recovery ------------------------------------------------------------

def test_register_409_machine_key_in_use_one_ladder(tmp_path, scanner_sysfs,
                                                    machine_id, monkeypatch, capsys):
    monkeypatch.setenv("HCRM_SCANNER_CONFIG", str(tmp_path / "c.json"))
    post = PostScript([PostError(409, "machine_key_in_use: terminal 'old'")])
    code = run(_portable_args(), post, scanner_sysfs, machine_id_path=machine_id)
    assert code == 1
    err = capsys.readouterr().err
    assert "machine key" in err and "DELETE" in err
    assert "reclaim" in err
    assert "Traceback" not in err
    assert INVITE not in err and "S" * 43 not in err


def test_register_409_already_registered_names_recovery(tmp_path, scanner_sysfs,
                                                        machine_id, monkeypatch, capsys):
    monkeypatch.setenv("HCRM_SCANNER_CONFIG", str(tmp_path / "c.json"))
    post = PostScript([PostError(409, "terminal_already_registered")])
    code = run(_portable_args(), post, scanner_sysfs, machine_id_path=machine_id)
    assert code == 1
    err = capsys.readouterr().err
    assert "DELETE" in err and "Traceback" not in err
    assert INVITE not in err


# --- flag validation ---------------------------------------------------------

def test_site_origin_required_with_invite(scanner_sysfs):
    post = PostScript([])
    with pytest.raises(SystemExit) as exc:
        run(["--register", "--invite", INVITE], post, scanner_sysfs)
    assert exc.value.code == 2
    assert post.calls == []


@pytest.mark.parametrize("origin", ["ftp://x", "http://x/path", "not-a-url",
                                    "http://x?q=1"])
def test_site_origin_invalid_rejected_before_post(scanner_sysfs, origin):
    post = PostScript([])
    with pytest.raises(SystemExit) as exc:
        run(["--register", "--invite", INVITE, "--site-origin", origin],
            post, scanner_sysfs)
    assert exc.value.code == 2
    assert post.calls == []


def test_legacy_config_still_required_without_invite(scanner_sysfs):
    with pytest.raises(SystemExit) as exc:
        run(["--register"], PostScript([]), scanner_sysfs)
    assert exc.value.code == 2


def test_invite_requires_server(scanner_sysfs, capsys):
    # F1: --invite without --server must NOT silently fall back to --forward
    # (http://127.0.0.1:8000) and burn the staff invite against the wrong server.
    post = PostScript([])
    with pytest.raises(SystemExit) as exc:
        run(["--register", "--invite", INVITE,
             "--site-origin", "http://shop:8000"], post, scanner_sysfs)
    assert exc.value.code == 2
    assert "--server is required with --invite" in capsys.readouterr().err
    assert post.calls == []


# --- legacy path unchanged ---------------------------------------------------

def test_legacy_register_body_and_config_unchanged(tmp_path, scanner_sysfs):
    cfg = tmp_path / "scanner.json"
    offer = tmp_path / "scanner-pairing.json"
    post = PostScript([{"pairing_code": PAIRING_CODE}])
    code = run(["--config", str(cfg), "--register", "--terminal-id", "counter-1",
                "--pairing-file", str(offer), "--register-only"],
               post, scanner_sysfs)
    assert code == 0
    assert post.calls[0]["body"] == {"terminal_id": "counter-1",
                                     "secret": post.calls[0]["body"]["secret"]}
    assert "invite" not in post.calls[0]["body"]
    assert "machine_key" not in post.calls[0]["body"]
    assert set(json.loads(cfg.read_text())) == {"terminal_id", "secret"}
    assert set(json.loads(offer.read_text())) == {"terminal_id", "pairing_code"}


def test_server_flag_overrides_forward_target(tmp_path, scanner_sysfs, machine_id):
    cfg = tmp_path / "scanner.json"
    post = PostScript([{"pairing_code": PAIRING_CODE}])
    code = run(["--config", str(cfg), "--register", "--terminal-id", "counter-1",
                "--server", "http://shop:8000", "--forward", "http://other:9000",
                "--local-port", "0"],
               post, scanner_sysfs, machine_id_path=machine_id,
               reader=reader_yielding("8412345678905"))
    assert code == 0
    urls = [c["url"] for c in post.calls]
    assert "http://shop:8000/api/scanner/terminals" in urls
    assert "http://shop:8000/api/scanner/events" in urls
    assert not any("other:9000" in u for u in urls)
