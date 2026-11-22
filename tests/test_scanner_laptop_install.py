"""P4: the portable laptop's systemd USER unit + one-time installer.

Hardware-free by construction: the unit is parsed as text (configparser —
it is an INI file), and the installer is exercised through --dry-run into a
throwaway HOME. The dry run must print every privileged/systemd step (the
udev cp, the 0600 env write, the systemctl lines) and create NOTHING; the
invite must never appear in the output. The real install's behaviour is the
daemon's own test surface (tests/test_scanner_daemon_portable.py) — this
suite pins the packaging, not the grab.
"""

import configparser
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIT = REPO_ROOT / "scripts" / "systemd" / "user" / "hcrm-scanner-agent.service"
INSTALLER = REPO_ROOT / "scripts" / "laptop-install.sh"


def _unit() -> configparser.ConfigParser:
    # interpolation=None: the unit is full of %h specifiers; optionxform=str:
    # systemd keys are case-SENSITIVE (ExecStart is not execstart).
    cp = configparser.ConfigParser(interpolation=None, allow_no_value=True)
    cp.optionxform = str
    cp.read(UNIT, encoding="utf-8")
    return cp


def test_user_unit_execstart_flags_paths_and_install_target():
    cp = _unit()
    exec_start = cp["Service"]["ExecStart"]
    # An absolute interpreter, the daemon through the per-user repo symlink,
    # and exactly the P4 flags: the XDG config + the portable popup port.
    assert exec_start.startswith("/usr/bin/env python3 "), exec_start
    assert "%h/.config/hcrm/repo/scripts/scanner_daemon.py" in exec_start
    assert "--config %h/.config/hcrm/scanner.json" in exec_start
    assert "--local-port 47631" in exec_start
    assert cp["Service"]["EnvironmentFile"] == "-%h/.config/hcrm/agent.env"
    assert cp["Service"]["Restart"] == "on-failure"
    assert cp["Service"]["RestartSec"] == "3"
    # A USER unit: login-started, never root, and network-online is a Wants
    # only — ordering against the system network stack would delay login.
    assert cp["Install"]["WantedBy"] == "default.target"
    assert "User" not in cp["Service"]
    assert "root" not in exec_start
    assert cp["Unit"]["Wants"] == "network-online.target"
    assert "network-online" not in cp["Unit"].get("After", "")


def test_installer_bash_syntax():
    proc = subprocess.run(["bash", "-n", str(INSTALLER)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def _run_installer(tmp_path, *flags, home=None):
    env = dict(os.environ, HOME=str(home or tmp_path))
    env.pop("XDG_CONFIG_HOME", None)   # the installer owns ~/.config paths
    return subprocess.run(["bash", str(INSTALLER), *flags],
                          capture_output=True, text=True, env=env,
                          cwd=REPO_ROOT, timeout=120)


def test_installer_dry_run_prints_every_step_and_creates_nothing(tmp_path):
    proc = _run_installer(tmp_path, "--server", "http://127.0.0.1:8000",
                          "--invite", "TESTINVITE123", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    # the udev step (the only sudo)
    assert "sudo cp" in out
    assert "scripts/udev/99-opticon-scanner.rules" in out
    assert "/etc/udev/rules.d/" in out
    assert "sudo udevadm control --reload-rules" in out
    # the per-user layout: repo symlink + verbatim unit copy
    assert "ln -sfn" in out and ".config/hcrm/repo" in out
    assert ".config/systemd/user/hcrm-scanner-agent.service" in out
    # the 0600 env write, with the site-origin defaulting to --server
    assert "install -m 600" in out
    assert str(tmp_path / ".config" / "hcrm" / "agent.env") in out
    assert "HCRM_SCANNER_SERVER=http://127.0.0.1:8000" in out
    assert "HCRM_SCANNER_SITE_ORIGIN=http://127.0.0.1:8000" in out
    assert "HCRM_SCANNER_HEARTBEAT_SECONDS=" in out
    # registration (once) and the systemd user lines
    assert "--register --register-only" in out
    assert "systemctl --user daemon-reload" in out
    assert "systemctl --user enable --now hcrm-scanner-agent.service" in out
    # the invite is a secret: never printed, even in the dry-run command
    assert "TESTINVITE123" not in out
    # and NOTHING was created under the throwaway HOME
    assert list(tmp_path.rglob("*")) == []


def test_installer_dry_run_without_invite_skips_registration(tmp_path):
    proc = _run_installer(tmp_path, "--server", "http://127.0.0.1:8000",
                          "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "--register" not in proc.stdout
    assert "systemctl --user enable --now" in proc.stdout
    assert list(tmp_path.rglob("*")) == []


def test_installer_dry_run_is_repeatable(tmp_path):
    first = _run_installer(tmp_path, "--server", "http://127.0.0.1:8000",
                           "--dry-run")
    second = _run_installer(tmp_path, "--server", "http://127.0.0.1:8000",
                            "--dry-run")
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout   # idempotent: no state, no drift
    assert list(tmp_path.rglob("*")) == []


def test_installer_requires_server_and_rejects_a_pathed_origin(tmp_path):
    proc = _run_installer(tmp_path, "--dry-run")
    assert proc.returncode != 0
    assert "--server" in proc.stderr
    proc = _run_installer(tmp_path, "--server", "http://shop:8000",
                          "--site-origin", "http://shop:8000/path", "--dry-run")
    assert proc.returncode != 0
    assert "scheme://host" in proc.stderr
    assert list(tmp_path.rglob("*")) == []


def test_installer_help_needs_no_flags(tmp_path):
    proc = _run_installer(tmp_path, "--help")
    assert proc.returncode == 0
    for flag in ("--server", "--site-origin", "--invite", "--dry-run"):
        assert flag in proc.stdout
    assert list(tmp_path.rglob("*")) == []


def test_installer_refuses_a_lone_copy_outside_the_repo(tmp_path):
    fake = tmp_path / "scripts" / "laptop-install.sh"
    fake.parent.mkdir(parents=True)
    fake.write_text(INSTALLER.read_text(encoding="utf-8"), encoding="utf-8")
    env = dict(os.environ, HOME=str(tmp_path / "home"))
    proc = subprocess.run(
        ["bash", str(fake), "--server", "http://127.0.0.1:8000", "--dry-run"],
        capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode != 0
    assert "repo root" in proc.stderr
