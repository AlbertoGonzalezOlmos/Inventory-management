"""Tests for scripts/usb_host_check.py — host-layer USB detection preflight.

The controller-death fixture is the **real** kernel log from this project's
dev machine (2026-10-03): an xHCI controller died during a suspend/resume,
took its root hubs' ports with it, and every subsequent plug-in produced no
kernel event. That is the exact failure this script exists to name.
"""

import json
import os
import re
import shutil
import sqlite3
import tempfile

import pytest

import scripts.usb_host_check as host_check
from scripts.usb_host_check import (
    MODULES,
    _default_lockfile,
    _opticon_mm_ignore,
    analyze,
    dead_controllers,
    gather_bus_bdf,
    gather_daemon_pid,
    gather_device_present,
    gather_power_control,
    gather_scanner_identity,
    pairing_recovery_commands,
    parse_host_events,
    parse_usb_flakiness,
    prevention_commands,
    prevention_permanent_rule,
    prevention_runtime_command,
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


def healthy_analyze(tmp_path, log=None, **overrides):
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
    return analyze(log or NORMAL_BOOT_LOG, BUS_BDF, **kwargs)


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


def test_recovery_commands_honest_order_and_prevention():
    """The ladder is the one measured on the 2026-10-10 host: PCI
    remove+rescan BEFORE the function reset (reset_method may be 'pm' —
    no FLR — and a PM reset rarely revives a declared-dead controller),
    reboot last, and the runtime-PM prevention the old list omitted.
    The permanent rule must carry a <VENDOR> placeholder — baking this
    host's vendor id into a host-generic script would misfire on every
    other machine.
    """
    cmds = recovery_commands("0000:c3:00.3")
    text = "\n".join(cmds)

    def step(pred, what):
        """The ELEMENT index of one ladder step — not a substring index.

        ``text.index(key)`` is fooled by the ladder's own comments, which
        carry the same words: "rescan" first appears in the ``# 2. PCI-level
        remove + rescan`` COMMENT, ``/reset`` first appears in the caveat's
        ``cat …/reset_method``, and ``power/control`` also appears in the
        permanent rule's ``ATTR{power/control}``. Mutation-probed: inserting
        the reset COMMAND ahead of the remove+rescan command left every
        substring index green, because the comments did not move.
        """
        hits = [i for i, c in enumerate(cmds) if pred(c)]
        assert hits, f"the recovery ladder lost its {what} step"
        return hits[0]

    i_unbind = step(lambda c: "xhci_hcd/unbind" in c, "unbind/bind")
    i_remove = step(lambda c: c.startswith("echo 1 |") and "/remove" in c,
                    "PCI remove + rescan")
    i_reset = step(lambda c: c.endswith("/reset"), "function reset")
    i_reboot = step(lambda c: "reboot" in c, "reboot")
    # The RUNTIME prevention command, matched by its own form: the
    # permanent rule's ATTR{power/control}="on" also contains the substring
    # "power/control", so deleting `echo on | …/power/control` alone once
    # left every assert green (mutation-probed).
    i_prevent = step(lambda c: c.startswith("echo on |"),
                     "runtime power/control=on prevention")
    i_permanent = step(lambda c: "<VENDOR>" in c, "permanent udev rule")
    assert i_unbind < i_remove < i_reset < i_reboot < i_prevent < i_permanent, (
        "the recovery ladder is out of order")
    assert "remove" in text and "<VENDOR>" in text
    # every emitted command references the bdf or is global/prevention
    assert not any("0x1022" in c for c in cmds), (
        "host-specific PCI vendor id baked into a generic script")
    # the reset carries its caveat inline (reset_method may be 'pm')
    assert any("reset_method" in c and "pm" in c for c in cmds)


def test_recovery_ladder_ends_with_the_shared_prevention():
    """The no-drift pin: recovery_commands' tail IS prevention_commands —
    the recovery ladder and the healthy-but-auto INFO finding share one
    source, so the two paths cannot drift apart."""
    tail = prevention_commands("0000:c3:00.3")
    assert recovery_commands("0000:c3:00.3")[-len(tail):] == tail


def test_prevention_commands_caveat_and_no_baked_vendor():
    """The permanent rule needs systemd >= 255 for its add|change
    match-key alternation (on older systemd it silently never matches) —
    the caveat must ride along wherever the rule is emitted. And the
    vendor id stays a <VENDOR> placeholder + fill-in, never 0x1022."""
    cmds = prevention_commands("0000:c3:00.3")
    assert prevention_runtime_command("0000:c3:00.3") == (
        "echo on | sudo tee /sys/bus/pci/devices/0000:c3:00.3/power/control")
    # the exact caveat phrase, not just the word 'systemd' (which appears
    # twice in the same string — a substring assert on the word alone was
    # mutation-proven vacuous)
    assert any("systemd >= 255" in c and "alternation" in c for c in cmds), (
        "the systemd >= 255 alternation caveat is missing")
    assert any("one per ACTION" in c for c in cmds)
    assert any("<VENDOR>" in c for c in cmds)
    assert "cat /sys/bus/pci/devices/0000:c3:00.3/vendor" in cmds
    assert not any("0x1022" in c for c in cmds)


# --- the healthy-but-auto runtime-PM INFO nag (item 4) ------------------------


def test_healthy_controller_on_auto_pm_nags_with_info(tmp_path):
    findings = healthy_analyze(
        tmp_path, power_control={"0000:c3:00.3": "auto"})
    infos = [f for f in findings if f.severity == "info"]
    assert len(infos) == 1
    assert "0000:c3:00.3" in infos[0].title and "auto" in infos[0].title
    assert prevention_runtime_command("0000:c3:00.3") in infos[0].fix
    assert any("<VENDOR>" in line for line in infos[0].fix)
    assert not any("0x1022" in line for line in infos[0].fix)


def test_applied_prevention_is_silent(tmp_path):
    """'on' means the operator applied the pin — no nag."""
    assert healthy_analyze(
        tmp_path, power_control={"0000:c3:00.3": "on"}) == []


def test_missing_or_unknown_pm_fact_is_silent(tmp_path):
    """Fail-silent: an unreadable sysfs (container, permissions, non-Linux)
    must produce no finding, never a false one."""
    assert healthy_analyze(tmp_path) == []                     # None default
    assert healthy_analyze(tmp_path, power_control={}) == []
    assert healthy_analyze(
        tmp_path, power_control={"0000:ff:00.0": "auto"}) == []  # unknown BDF


def test_pm_nag_names_only_the_auto_controllers(tmp_path):
    mixed = {"0000:c3:00.3": "auto", "0000:c3:00.4": "on"}
    findings = healthy_analyze(tmp_path, power_control=mixed)
    infos = [f for f in findings if f.severity == "info"]
    assert len(infos) == 1
    assert "0000:c3:00.3" in infos[0].title
    assert "0000:c3:00.4" not in infos[0].title


def test_pm_nag_is_one_aggregated_finding_with_one_permanent_rule(tmp_path):
    """ONE finding for every affected controller, and the vendor-wide
    permanent rule emitted exactly ONCE.

    The 2026-10-10 host has FOUR ``auto`` controllers, so a per-BDF finding
    would print four copies of a rule that is not per-BDF (``ATTR{vendor}``
    matches every controller of that vendor). The runtime pin IS per-BDF,
    because it writes a per-BDF sysfs node. This also pins the dedupe:
    ``BUS_BDF`` maps two buses to each controller, and a controller must be
    named once, not once per bus. Mutation-probed: emitting one finding per
    BDF left every other test green, because each of them feeds a single
    ``auto`` BDF — aggregation was the one unpinned decision here.
    """
    findings = healthy_analyze(
        tmp_path, power_control={"0000:c3:00.3": "auto",
                                 "0000:c3:00.4": "auto"})
    infos = [f for f in findings if f.severity == "info"]
    assert len(infos) == 1, "the nag must aggregate, not repeat per controller"
    title = infos[0].title
    assert "2 xHCI controller(s)" in title
    assert title.count("0000:c3:00.3") == 1, "two buses, one mention"
    assert title.count("0000:c3:00.4") == 1, "two buses, one mention"
    for bdf in ("0000:c3:00.3", "0000:c3:00.4"):
        assert prevention_runtime_command(bdf) in infos[0].fix
    rule = [line for line in infos[0].fix
            if "98-usb-controller-nosuspend" in line]
    assert len(rule) == 1, f"the vendor-wide rule must appear once: {rule}"


def test_dead_controller_owns_prevention_no_info(tmp_path):
    """A dead controller already carries the full ladder (prevention
    included) in its error's fix — the INFO must not duplicate it."""
    findings = analyze(
        DEAD_CONTROLLER_LOG, BUS_BDF,
        udev_rules=str(tmp_path / "present.rules"),
        modemmanager_active=False, opticon_ignore_rule=False,
        brltty_active=False,
        modules_root=write_modules(str(tmp_path / "modules")), modules=MODULES,
        power_control={"0000:c3:00.3": "auto"})
    assert not [f for f in findings if f.severity == "info"]
    dead = next(f for f in findings if "is dead" in f.title)
    assert any("power/control" in line for line in dead.fix)


def test_gather_power_control_reads_an_injected_sysfs(tmp_path):
    """The gather_bus_bdf convention: a default-argument root, so the
    sysfs read is testable offline against a tmp_path fake tree."""
    for bdf, mode in (("0000:c3:00.3", "auto\n"), ("0000:c5:00.4", "on\n")):
        node = tmp_path / bdf / "power"
        node.mkdir(parents=True)
        (node / "control").write_text(mode)
    out = gather_power_control(["0000:c3:00.3", "0000:c5:00.4"],
                               root=str(tmp_path))
    assert out == {"0000:c3:00.3": "auto", "0000:c5:00.4": "on"}
    # fail-silent per bdf: a missing node is omitted, never guessed
    assert gather_power_control(["0000:ff:00.0"], root=str(tmp_path)) == {}


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
    # the dead-controller finding's fix now carries the prevention step too
    assert any("power/control" in line for line in dead[0].fix)


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


# --- USB link flakiness (-71 / enumerate-then-dropout) + scanner-stack identity
# (glm, 2026-11-17: the real journal of the "enumerated but no scans" incident)

SCANNER_DROPOUT_LOG = [
    "[79000.000000] usb 1-1: new full-speed USB device number 13 using xhci_hcd",
    "[79000.100000] usb 1-1: device descriptor read/64, error -71",
    "[79000.200000] usb 1-1: device descriptor read/64, error -71",
    "[79500.000000] usb 1-1: new full-speed USB device number 14 using xhci_hcd",
    "[79500.100000] usb 1-1: device descriptor read/64, error -71",
    "[79600.000000] usb 1-1: new full-speed USB device number 15 using xhci_hcd",
    "[79600.100000] usb 1-1: device descriptor read/64, error -71",
    "[79613.674279] usb 1-1: New USB device found, idVendor=065a, idProduct=a001, bcdDevice= 1.00",
    "[79613.680000] usb 1-1: New USB device strings: Mfr=1, Product=2, SerialNumber=0",
    "[79613.744546] hid-generic 0003:065A:A001.0004: input,hidraw3: USB HID v1.11 Keyboard",
    "[79890.502382] usb 1-1: USB disconnect, device number 15",
]

LADDER_EXHAUSTED_LOG = [
    "[80000.000000] usb 1-1: new full-speed USB device number 16 using xhci_hcd",
    "[80000.100000] usb 1-1: device descriptor read/64, error -71",
    "[80000.200000] usb 1-1: device descriptor read/64, error -71",
    "[80000.300000] usb 1-1: device descriptor read/64, error -71",
    "[80000.400000] usb usb1-port1: attempt power cycle",
]

FOREIGN_TERMINAL = "pentacosiarca-ThinkPad-T14s-Gen-4"
SECRET = "S" * 43
PAIRING_CODE = "ABCD2345"


def build_identity_stack(tmp_path, *, config_terminal=FOREIGN_TERMINAL,
                         offer_terminal="hertha-shop",
                         registered=(FOREIGN_TERMINAL,),
                         last_seen="2026-11-03 05:56:54.846862"):
    """A scratch data dir shaped like the live one (bug A): config pairing
    as one terminal, an offer naming another, an hcrm.db holding `registered`.
    All files carry real secret/pairing-code values — the tripwire is that
    NO finding ever echoes them."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "scanner.json").write_text(json.dumps(
        {"terminal_id": config_terminal, "secret": SECRET}))
    (data / "scanner-pairing.json").write_text(json.dumps(
        {"terminal_id": offer_terminal, "pairing_code": PAIRING_CODE}))
    db = data / "hcrm.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE terminals (terminal_id TEXT, secret_hash TEXT,"
                 " pairing_code_hash TEXT, token_hash TEXT, last_seen_at TEXT)")
    for tid in registered:
        conn.execute("INSERT INTO terminals VALUES (?, 'h', 'h', 'tok', ?)",
                     (tid, last_seen))
    conn.commit()
    conn.close()
    return gather_scanner_identity(str(data))


# --- parse_usb_flakiness -------------------------------------------------------

def test_parse_usb_flakiness_flags_the_real_dropout():
    out = parse_usb_flakiness(SCANNER_DROPOUT_LOG)
    assert out["descriptor_errors"] == 4
    assert out["proto_errors"] == 4
    assert out["opticon_port"] == "1-1"
    assert out["opticon_enumerated_at"] == 7
    assert out["opticon_disconnected_at"] == 10
    assert out["opticon_enumerated_at"] < out["opticon_disconnected_at"]


def test_parse_usb_flakiness_a_replug_resets_the_dropout_counter():
    """A disconnect followed by a NEW enumeration is a replug, not a
    dropout — only a disconnect AFTER the last attach counts."""
    lines = SCANNER_DROPOUT_LOG + [
        "[90000.000000] usb 1-1: New USB device found, idVendor=065a, idProduct=a001",
        "[90000.010000] input: Opticon Opticon USB Barcode Reader",
    ]
    out = parse_usb_flakiness(lines)
    assert out["opticon_disconnected_at"] is None
    assert out["opticon_enumerated_at"] > SCANNER_DROPOUT_LOG.index(
        SCANNER_DROPOUT_LOG[-1])


def test_parse_usb_flakiness_ignores_other_devices_disconnects():
    lines = SCANNER_DROPOUT_LOG + [
        "[90001.000000] usb 1-4: USB disconnect, device number 3",
    ]
    assert parse_usb_flakiness(lines)["opticon_disconnected_at"] == 10


def test_parse_usb_flakiness_silent_on_healthy_log():
    out = parse_usb_flakiness(NORMAL_BOOT_LOG)
    assert out == {"descriptor_errors": 0, "proto_errors": 0,
                   "opticon_port": None, "opticon_enumerated_at": None,
                   "opticon_disconnected_at": None}


# --- the flakiness findings ----------------------------------------------------

def test_analyze_warns_on_enumerate_then_dropout(tmp_path):
    findings = healthy_analyze(tmp_path, log=SCANNER_DROPOUT_LOG)
    warn = [f for f in findings if "dropped off the USB bus" in f.title]
    assert len(warn) == 1
    assert "port 1-1" in warn[0].title
    assert warn[0].detail == (
        "4 descriptor-read error(s) this boot, one successful Opticon attach, "
        "then a USB disconnect — physical-layer evidence "
        "(cable/connector/port/device); it does not identify the failing "
        "component or rule out power/port faults. A flaky link explains both "
        "'the scanner looks absent' (retry-ladder exhaustion on plug) and "
        "'attached but silent' (dropout mid-session).")
    assert any("journalctl" in c for c in warn[0].fix)
    assert warn[0].fix[2] == (
        "try one known-good data cable, then another direct USB port "
        "(one change at a time)")


def test_analyze_warns_when_the_retry_ladder_exhausts(tmp_path):
    findings = healthy_analyze(tmp_path, log=LADDER_EXHAUSTED_LOG,
                               device_present=False)
    warn = [f for f in findings if "retry-fragile" in f.title]
    assert len(warn) == 1
    assert "no successful scanner enumeration" in warn[0].title
    assert warn[0].detail == (
        "Each plug burns kernel address rounds; when the retry ladder exhausts "
        "(journal: 'attempt power cycle'), the device stays ABSENT — it looks "
        "like a dead scanner or a charge-only cable. Repeated descriptor-read "
        "failures are physical-layer evidence (cable/connector/port/device); "
        "they do not identify the failing component or rule out power/port "
        "faults.")
    assert warn[0].fix[2] == (
        "try one known-good data cable, then another direct USB port "
        "(one change at a time)")
    # device_present True (attached) must NOT fire the "looks absent" finding
    assert healthy_analyze(tmp_path, log=LADDER_EXHAUSTED_LOG,
                           device_present=True) == []


@pytest.mark.parametrize(("log", "title_fragment"), [
    (SCANNER_DROPOUT_LOG, "dropped off the USB bus"),
    (LADDER_EXHAUSTED_LOG, "retry-fragile"),
])
def test_flakiness_findings_do_not_claim_uncollected_measurements(
        tmp_path, log, title_fragment):
    findings = healthy_analyze(tmp_path, log=log, device_present=False)
    finding = next(f for f in findings if title_fragment in f.title)
    rendered = finding.detail + "\n" + "\n".join(finding.fix)
    for needle in ("NOT power", "NOT a dead port", "over_current_count",
                   "every port tried", "works on another host"):
        assert needle not in rendered
    assert "physical-layer evidence" in finding.detail
    assert "rule out power/port faults" in finding.detail


def test_analyze_ladder_exhausted_needs_three_errors(tmp_path):
    two = LADDER_EXHAUSTED_LOG[:3]  # only 2 descriptor errors
    assert healthy_analyze(tmp_path, log=two, device_present=False) == []


# --- the identity findings -----------------------------------------------------

def test_analyze_flags_the_foreign_terminal_with_the_delete_ladder(tmp_path):
    identity = build_identity_stack(tmp_path)
    findings = healthy_analyze(tmp_path, identity=identity,
                               device_present=False, daemon_pid=234104,
                               hostname="hertha-shop")
    foreign = [f for f in findings if "ANOTHER host's terminal" in f.title]
    assert len(foreign) == 1
    assert FOREIGN_TERMINAL in foreign[0].title
    assert "hertha-shop" in foreign[0].detail
    assert "no browser on this host can poll" in foreign[0].detail
    fix = foreign[0].fix
    delete = [c for c in fix if c.startswith("curl -X DELETE")]
    assert len(delete) == 1 and FOREIGN_TERMINAL in delete[0]
    assert "DESTRUCTIVE" in delete[0]
    # the ladder's order: stop the daemon, DELETE, move the config, register,
    # then the pill — element indices, not substring indices (comments carry
    # the same words; the existing recovery test's mutation-probed pattern)
    def idx(pred):
        hits = [i for i, c in enumerate(fix) if pred(c)]
        assert hits
        return hits[0]
    assert idx(lambda c: c.startswith("kill ")) < \
        idx(lambda c: c.startswith("curl -X DELETE")) < \
        idx(lambda c: c.startswith("mv ")) < \
        idx(lambda c: c.startswith("uv run python scripts/scanner_daemon")) < \
        idx(lambda c: c.startswith("# then click")) < len(fix)


def test_analyze_flags_the_stale_offer_file(tmp_path):
    identity = build_identity_stack(tmp_path)
    findings = healthy_analyze(tmp_path, identity=identity,
                               device_present=False,
                               hostname="hertha-shop")
    offer = [f for f in findings if "pairing offer is stale" in f.title]
    assert len(offer) == 1
    assert "'hertha-shop'" in offer[0].title
    assert "401 bad_pairing" in offer[0].detail
    assert "409 already_registered" in offer[0].detail
    assert "three-way" in offer[0].detail


def test_analyze_flags_config_unknown_to_the_server(tmp_path):
    identity = build_identity_stack(
        tmp_path, config_terminal="ghost-terminal", registered=())
    findings = healthy_analyze(tmp_path, identity=identity,
                               device_present=True, hostname="hertha-shop")
    unknown = [f for f in findings
               if "terminal the server does not know" in f.title]
    assert len(unknown) == 1
    assert "ghost-terminal" in unknown[0].title
    assert "401" in unknown[0].detail
    # the config is NOT registered — the ladder must not print a DELETE
    assert not any("DELETE" in c for c in unknown[0].fix)
    # the offer names an (unregistered) terminal too: still one stale-offer
    # finding, and its ladder equally has no DELETE (nothing to free)
    offer = [f for f in findings if "pairing offer is stale" in f.title]
    assert len(offer) == 1
    assert not any("DELETE" in c for c in offer[0].fix)


def test_analyze_flags_device_absent_when_the_stack_exists(tmp_path):
    identity = build_identity_stack(
        tmp_path, config_terminal="hertha-shop", offer_terminal="hertha-shop",
        registered=("hertha-shop",))
    findings = healthy_analyze(tmp_path, identity=identity,
                               device_present=False, hostname="hertha-shop")
    absent = [f for f in findings if "no Opticon device is on the USB bus"
              in f.title]
    assert len(absent) == 1
    # consistent identity otherwise: exactly this one warning, nothing else
    assert [f.severity for f in findings] == ["warning"]


def test_analyze_daemon_running_is_info_and_names_the_pid(tmp_path):
    identity = build_identity_stack(
        tmp_path, config_terminal="hertha-shop", offer_terminal="hertha-shop",
        registered=("hertha-shop",))
    findings = healthy_analyze(tmp_path, identity=identity,
                               device_present=True, daemon_pid=234104,
                               hostname="hertha-shop")
    info = [f for f in findings if f.severity == "info"]
    assert len(info) == 1
    assert "234104" in info[0].title and "scanner_daemon" in info[0].title
    assert "EVIOCGRAB" in info[0].detail and "lock" in info[0].detail
    assert any(c == "kill 234104  # only to capture or re-register — "
                "the lock and the grab release instantly" for c in info[0].fix)
    # a running daemon is normal operation: it must not affect the exit-code
    # contract (info never counts as a problem)
    assert not any(f.severity in ("error", "warning") for f in findings)


def test_analyze_stays_silent_without_the_scanner_stack(tmp_path):
    """Host-genericity: identity facts all absent (a host without the app
    stack, or an unreadable one) produce NO findings — fail-silent, never a
    false one."""
    empty_identity = {"config_terminal_id": None, "offer_terminal_id": None,
                      "terminals": None, "config_path": "/x", "offer_path": "/y"}
    assert healthy_analyze(tmp_path, identity=empty_identity,
                           device_present=False) == []
    assert healthy_analyze(tmp_path, identity=None, device_present=None,
                           daemon_pid=None, hostname=None) == []


def test_identity_findings_never_leak_secrets(tmp_path):
    """The doctrine tripwire: scanner.json holds the write secret, the offer
    file the single-use pairing code — neither may appear in any finding."""
    identity = build_identity_stack(tmp_path)
    findings = healthy_analyze(tmp_path, log=SCANNER_DROPOUT_LOG,
                               identity=identity, device_present=False,
                               daemon_pid=234104, hostname="hertha-shop")
    rendered = json.dumps([f.__dict__ for f in findings])
    assert SECRET not in rendered
    assert PAIRING_CODE not in rendered
    assert "pairing_code" not in rendered and "secret" not in rendered


# --- the read-only gathers -----------------------------------------------------

def test_gather_scanner_identity_reads_a_scratch_data_dir(tmp_path):
    identity = build_identity_stack(tmp_path)
    assert identity["config_terminal_id"] == FOREIGN_TERMINAL
    assert identity["offer_terminal_id"] == "hertha-shop"
    assert identity["config_path"] == str(tmp_path / "data" / "scanner.json")
    assert identity["terminals"] == [
        {"terminal_id": FOREIGN_TERMINAL, "paired": True,
         "last_seen": "2026-11-03 05:56:54.846862"}]


def test_gather_scanner_identity_is_read_only(tmp_path):
    """The ./data doctrine as a tripwire: the DB bytes are unchanged and no
    journal/WAL sidecar is created by a read-only open (the sidecars that
    exist on the live host belong to the running server, not to us)."""
    identity = build_identity_stack(tmp_path)
    db = tmp_path / "data" / "hcrm.db"
    before = db.read_bytes()
    assert identity["terminals"]  # the read really happened
    assert db.read_bytes() == before
    assert not list((tmp_path / "data").glob("hcrm.db-*"))


def test_gather_scanner_identity_fail_silent(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    # empty dir: no config, no offer, no db
    empty = gather_scanner_identity(str(data))
    assert empty["config_terminal_id"] is None
    assert empty["offer_terminal_id"] is None
    assert empty["terminals"] is None
    # garbage files: unreadable JSON and a non-sqlite hcrm.db
    (data / "scanner.json").write_text("not json{")
    (data / "scanner-pairing.json").write_text("")
    (data / "hcrm.db").write_bytes(b"garbage not a database")
    broken = gather_scanner_identity(str(data))
    assert broken["config_terminal_id"] is None
    assert broken["offer_terminal_id"] is None
    assert broken["terminals"] is None


def test_gather_daemon_pid_detects_live_stale_and_foreign_locks(
        tmp_path, monkeypatch):
    live = tmp_path / "live.lock"
    live.write_text(f"{os.getpid()}\n")
    assert gather_daemon_pid(str(live)) == os.getpid()
    # stale residue: flock was released when the holder died
    stale = tmp_path / "stale.lock"
    stale.write_text("999999\n")
    assert gather_daemon_pid(str(stale)) is None
    # another user's daemon: PermissionError means alive, keep the pid
    def refuse(pid, sig):
        raise PermissionError(13, "permission denied")
    monkeypatch.setattr("scripts.usb_host_check.os.kill", refuse)
    assert gather_daemon_pid(str(live)) == os.getpid()
    # absent / garbage lockfiles
    assert gather_daemon_pid(str(tmp_path / "absent.lock")) is None
    junk = tmp_path / "junk.lock"
    junk.write_text("not a pid\n")
    assert gather_daemon_pid(str(junk)) is None


def test_default_lockfile_matches_the_daemon():
    """The two-implementations-one-test pattern (same as the pairing file):
    this tool's lock path must resolve exactly like the daemon's, or the
    PID detection would read the wrong file."""
    from scripts.scanner_daemon import _default_lockfile_path
    assert _default_lockfile() == str(_default_lockfile_path())


def test_gather_device_present_scans_an_injected_sysfs(tmp_path):
    sysfs = tmp_path / "devices"
    (sysfs / "1-1").mkdir(parents=True)
    (sysfs / "1-1" / "idVendor").write_text("065a\n")
    assert gather_device_present(str(sysfs)) is True
    # case-insensitive hex, other devices ignored
    (sysfs / "1-1" / "idVendor").write_text("065A\n")
    assert gather_device_present(str(sysfs)) is True
    (sysfs / "1-1" / "idVendor").write_text("8087\n")
    assert gather_device_present(str(sysfs)) is False
    # unreadable sysfs: None (fail-silent), never a false False
    assert gather_device_present(str(tmp_path / "nope")) is None


def test_recovery_ladder_omits_delete_without_a_registered_terminal():
    cmds = pairing_recovery_commands(
        None, config_path="/data/scanner.json",
        daemon_pid=None, delete_terminal=True)
    assert not any("DELETE" in c for c in cmds)
    assert any(c.startswith("# stop the running daemon") for c in cmds)
    assert any(c.startswith("mv ") for c in cmds)
    assert any("--register --register-only" in c for c in cmds)
    # without a config path the register step still names the conventional one
    cmds = pairing_recovery_commands(
        "some-terminal", config_path=None,
        daemon_pid=None, delete_terminal=False)
    assert any("<data>/scanner.json" in c for c in cmds)


# --- CLI verdicts -------------------------------------------------------------


def _stub_main_environment(monkeypatch, dmesg, findings):
    facts = {
        "dmesg": dmesg,
        "bus_bdf": {},
        "power_control": {},
        "udev_rules": host_check.UDEV_RULES,
        "modemmanager_active": False,
        "opticon_ignore_rule": True,
        "brltty_active": False,
        "modules_root": None,
        "modules": host_check.MODULES,
        "uname": "test-kernel",
        "identity": None,
        "device_present": None,
        "daemon_pid": None,
        "hostname": "test-host",
    }
    monkeypatch.setattr(host_check.sys, "platform", "linux")
    monkeypatch.setattr(host_check, "gather", lambda: facts)
    monkeypatch.setattr(host_check, "analyze", lambda *a, **kw: findings)


@pytest.mark.parametrize("json_output", [False, True], ids=["text", "json"])
def test_main_dmesg_unavailable_returns_2_in_both_formats(
        monkeypatch, capsys, json_output):
    findings = [host_check.Finding(
        severity="warning", title="synthetic warning", detail="detail")]
    _stub_main_environment(monkeypatch, None, findings)

    assert host_check.main(["--json"] if json_output else []) == 2
    captured = capsys.readouterr()
    assert "cannot read dmesg" in captured.err
    if json_output:
        payload = json.loads(captured.out)
        assert set(payload) == {"kernel", "findings"}
        assert payload["findings"][0]["severity"] == "warning"
    else:
        assert "[WARN] synthetic warning" in captured.out


@pytest.mark.parametrize("json_output", [False, True], ids=["text", "json"])
@pytest.mark.parametrize(("severity", "expected"), [
    ("info", 0), ("warning", 1), ("error", 1),
])
def test_main_dmesg_available_preserves_verdict_in_both_formats(
        monkeypatch, capsys, severity, expected, json_output):
    findings = [host_check.Finding(
        severity=severity, title="synthetic finding", detail="detail")]
    _stub_main_environment(monkeypatch, [], findings)

    assert host_check.main(["--json"] if json_output else []) == expected
    captured = capsys.readouterr()
    assert captured.err == ""
    if json_output:
        payload = json.loads(captured.out)
        assert payload["findings"][0]["severity"] == severity
    else:
        label = {"error": "ERROR", "warning": "WARN", "info": "INFO"}[severity]
        assert f"[{label}] synthetic finding" in captured.out
