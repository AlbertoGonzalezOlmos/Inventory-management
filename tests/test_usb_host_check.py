"""Tests for scripts/usb_host_check.py — host-layer USB detection preflight.

The controller-death fixture is the **real** kernel log from this project's
dev machine (2026-10-03): an xHCI controller died during a suspend/resume,
took its root hubs' ports with it, and every subsequent plug-in produced no
kernel event. That is the exact failure this script exists to name.
"""

import os
import re
import shutil
import tempfile

from scripts.usb_host_check import (
    MODULES,
    _opticon_mm_ignore,
    analyze,
    dead_controllers,
    gather_bus_bdf,
    parse_host_events,
    recovery_commands,
)

# --- fixtures ---------------------------------------------------------------

# The real sequence: precursor -> death -> devices detaching -> failed alloc.
DEAD_CONTROLLER_LOG = [
    "[101200.952549] xhci_hcd 0000:c3:00.3: xHC is not running.",
    "[101201.093869] xhci_hcd 0000:c3:00.3: Port resume timed out, port 1-3: 0x400e03",
    "[101211.703654] xhci_hcd 0000:c3:00.3: xHCI host not responding to stop endpoint command",
    "[101211.716893] xhci_hcd 0000:c3:00.3: xHCI host controller not responding, assume dead",
    "[101211.716905] xhci_hcd 0000:c3:00.3: HC died; cleaning up",
    "[101211.716996] usb 1-3: USB disconnect, device number 2",
    "[101211.716999] usb 1-3.1: USB disconnect, device number 3",
    "[101211.752667] usb usb1-port3: couldn't allocate usb_device",
]

NORMAL_BOOT_LOG = [
    "[    3.330000] usb usb1: New USB device found, idVendor=1d6b, idProduct=0002",
    "[    3.340000] usb 3-1: new high-speed USB device number 2 using xhci_hcd",
    "[    3.350000] usb 3-1: New USB device found, idVendor=04f2, idProduct=b7c0",
]

BUS_BDF = {1: "0000:c3:00.3", 2: "0000:c3:00.3",
           3: "0000:c3:00.4", 4: "0000:c3:00.4"}


def write_modules(root):
    # Kernel filenames hyphenate where modprobe names underscore.
    for name in MODULES:
        path = os.path.join(root, "nested", name.replace("_", "-") + ".ko.zst")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
    return root


def healthy_analyze(tmp_path, **overrides):
    rules = tmp_path / "99-opticon-scanner.rules"
    rules.write_text("# present\n")
    kwargs = dict(
        udev_rules=str(rules),
        modemmanager_active=False,
        opticon_ignore_rule=False,
        brltty_active=False,
        modules_root=write_modules(str(tmp_path / "modules")),
        modules=MODULES,
    )
    kwargs.update(overrides)
    return analyze(NORMAL_BOOT_LOG, BUS_BDF, **kwargs)


# --- controller parsing ------------------------------------------------------

def test_parse_flags_dead_controller():
    deaths, last_enum, precursors, last_alive = parse_host_events(DEAD_CONTROLLER_LOG)
    assert deaths == {"0000:c3:00.3": 4}          # the "HC died" line
    assert precursors and precursors[0][0] == "0000:c3:00.3"
    # The disconnects after the death must NOT count as evidence of life.
    assert last_enum.get(1, -1) < deaths["0000:c3:00.3"]
    assert last_alive.get("0000:c3:00.3", -1) < deaths["0000:c3:00.3"]


def test_dead_controllers_flags_died_and_no_enumeration_since():
    deaths, last_enum, _, last_alive = parse_host_events(DEAD_CONTROLLER_LOG)
    result = dead_controllers(deaths, last_enum, BUS_BDF, last_alive)
    ctrl = next(r for r in result if r["bdf"] == "0000:c3:00.3")
    assert ctrl["dead"] is True
    assert ctrl["buses"] == [1, 2]


def test_dead_controllers_ignores_recovered_controller():
    """A death followed by a later attach on the same bus is not fatal."""
    lines = DEAD_CONTROLLER_LOG + [
        "[102000.000000] usb 1-2: new full-speed USB device number 4 using xhci_hcd",
        "[102000.100000] usb 1-2: New USB device found, idVendor=065a, idProduct=a002",
    ]
    deaths, last_enum, _, last_alive = parse_host_events(lines)
    ctrl = next(r for r in dead_controllers(deaths, last_enum, BUS_BDF, last_alive)
                if r["bdf"] == "0000:c3:00.3")
    assert ctrl["dead"] is False


def test_dead_controllers_ignores_rebound_controller_with_nothing_plugged():
    """Peer-review case: a controller rebound after death must not be flagged
    just because nothing has been plugged in since."""
    lines = DEAD_CONTROLLER_LOG + [
        "[102500.000000] xhci_hcd 0000:c3:00.3: xHCI Host Controller",
        "[102500.010000] xhci_hcd 0000:c3:00.3: new USB bus registered, assigned bus number 1",
    ]
    deaths, last_enum, _, last_alive = parse_host_events(lines)
    ctrl = next(r for r in dead_controllers(deaths, last_enum, BUS_BDF, last_alive)
                if r["bdf"] == "0000:c3:00.3")
    assert ctrl["dead"] is False


def test_recovery_commands_name_the_controller():
    cmds = recovery_commands("0000:c3:00.3")
    assert any("unbind" in c and "bind" in c for c in cmds)
    assert any("0000:c3:00.3" in c for c in cmds)
    assert any("reboot" in c for c in cmds)


def test_gather_dmesg_must_use_plain_dmesg(monkeypatch):
    """Regression guard (qwen, 2026-10-10): `dmesg -T` is suspend-skewed
    (~+7 days) on this host. The detector relies on kmsg line ORDER, so it
    must call plain `dmesg`. Changing this to `-T` would introduce skewed
    wall-clock assumptions into an order-based parser."""
    import scripts.usb_host_check as host_check

    captured = {}

    class _Result:
        returncode = 0
        stdout = "[    0.000000] xhci_hcd 0000:c3:00.3: HC died; cleaning up\n"

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _Result()

    monkeypatch.setattr(host_check.subprocess, "run", fake_run)
    assert host_check.gather_dmesg() == _Result.stdout.splitlines()
    assert captured["args"] == ["dmesg"]
    assert "-T" not in captured["args"]


# --- findings ----------------------------------------------------------------

def test_analyze_reports_dead_controller_as_error_with_fix(tmp_path):
    findings = analyze(
        DEAD_CONTROLLER_LOG, BUS_BDF,
        udev_rules=str(tmp_path / "present.rules"),
        modemmanager_active=False, opticon_ignore_rule=False,
        brltty_active=False,
        modules_root=write_modules(str(tmp_path / "modules")), modules=MODULES)
    dead = [f for f in findings if "is dead" in f.title]
    assert len(dead) == 1 and dead[0].severity == "error"
    assert "0000:c3:00.3" in dead[0].title
    assert any("unbind" in line for line in dead[0].fix)


def test_analyze_warns_about_modemmanager(tmp_path):
    findings = healthy_analyze(tmp_path, modemmanager_active=True)
    assert any("ModemManager" in f.title for f in findings)
    # The fix must point at the REPO's vendor-wide file, not advise a second
    # rules file — that file's own header: "a second file would mean a
    # second install step and two rules that can drift".
    mm = next(f for f in findings if "ModemManager" in f.title)
    assert any("scripts/udev/99-opticon-scanner.rules" in line for line in mm.fix), (
        "the MM finding must point at the repo rules file (no second file)")
    assert not any("no-modemmanager" in line for line in mm.fix)


def test_analyze_modemmanager_ok_with_ignore_rule(tmp_path):
    findings = healthy_analyze(tmp_path, modemmanager_active=True,
                               opticon_ignore_rule=True)
    assert not any("ModemManager" in f.title for f in findings)


# --- the repo's own udev rule (item 1: the MM ignore) -------------------------

REPO_RULES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "udev", "99-opticon-scanner.rules")


def test_repo_udev_rule_carries_both_mm_ignore_forms():
    """The MM ignore must exist in BOTH subsystem forms, and the four
    permission lines must survive the addition (an append cannot silently
    replace them). udev ENV does NOT propagate parent→child: the tty form
    is the one that stops MM probing the ttyACM port; the usb form covers
    MM versions that consider the device itself."""
    text = open(REPO_RULES, encoding="utf-8").read()
    assert 'SUBSYSTEM=="tty",    ATTRS{idVendor}=="065a", ' \
           'ENV{ID_MM_DEVICE_IGNORE}="1"' in text, (
        "the tty-form MM ignore is missing — the one that stops the probe")
    assert 'SUBSYSTEM=="usb",    ATTRS{idVendor}=="065a", ' \
           'ENV{ID_MM_DEVICE_IGNORE}="1"' in text, (
        "the usb-form MM ignore is missing")
    for perm in ('SUBSYSTEM=="tty"', 'SUBSYSTEM=="hidraw"',
                 'SUBSYSTEM=="usb"', 'SUBSYSTEM=="input"'):
        assert re.search(
            rf'{re.escape(perm)},\s+ATTRS{{idVendor}}=="065a", MODE=', text), (
            f"the {perm} permission line was lost")


def test_repo_udev_rule_is_recognised_by_the_detector():
    """Closes the loop: the rule the detector looks for is the rule the
    repo ships — drop the repo file into a udev dir and _opticon_mm_ignore
    must see it."""
    d = tempfile.mkdtemp(prefix="hcrm-udevpin-")
    try:
        shutil.copy(REPO_RULES, os.path.join(d, "99-opticon-scanner.rules"))
        assert _opticon_mm_ignore(udev_dirs=(d,)) is True
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_analyze_warns_about_brltty(tmp_path):
    findings = healthy_analyze(tmp_path, brltty_active=True)
    assert any("brltty" in f.title for f in findings)


def test_analyze_warns_when_udev_rules_missing(tmp_path):
    findings = healthy_analyze(tmp_path,
                               udev_rules=str(tmp_path / "nope.rules"))
    assert any("udev rules are not installed" in f.title for f in findings)


def test_analyze_no_udev_warning_when_present(tmp_path):
    findings = healthy_analyze(tmp_path)
    assert not any("udev rules" in f.title for f in findings)


def test_analyze_warns_when_module_missing(tmp_path):
    # A directory distinct from healthy_analyze's default, so its helper does
    # not recreate the file this test removes.
    root = write_modules(str(tmp_path / "partial"))
    os.remove(os.path.join(root, "nested", "cdc-acm.ko.zst"))
    findings = healthy_analyze(tmp_path, modules_root=root)
    missing = [f for f in findings if "Kernel modules missing" in f.title]
    assert missing and "cdc_acm" in missing[0].title


def test_module_present_accepts_underscore_filename(tmp_path):
    from scripts.usb_host_check import _module_present
    open(os.path.join(str(tmp_path), "cdc_acm.ko.zst"), "w").close()
    assert _module_present(str(tmp_path), "cdc_acm")


def test_analyze_healthy_host_has_no_findings(tmp_path):
    assert healthy_analyze(tmp_path) == []


# --- sysfs bus -> controller mapping ----------------------------------------

def test_gather_bus_bdf_maps_buses_to_pci_address(tmp_path):
    target = tmp_path / "devices" / "pci0000:00" / "0000:00:08.1" / \
        "0000:c3:00.3" / "usb1"
    target.mkdir(parents=True)
    root = tmp_path / "usb_devices"
    root.mkdir()
    os.symlink(str(target), str(root / "usb1"))
    t2 = tmp_path / "devices" / "pci0000:00" / "0000:00:08.1" / \
        "0000:c3:00.4" / "usb3"
    t2.mkdir(parents=True)
    os.symlink(str(t2), str(root / "usb3"))
    (root / "1-1").mkdir()  # non-usbN entries must be ignored

    assert gather_bus_bdf(str(root)) == {1: "0000:c3:00.3", 3: "0000:c3:00.4"}