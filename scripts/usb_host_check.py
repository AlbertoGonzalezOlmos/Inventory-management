#!/usr/bin/env python3
"""usb_host_check — host-layer preflight for USB scanner detection.

``scripts/opticon_detect.py`` answers *"is an Opticon device attached?"*. It
cannot see a device that never reached the kernel. This script checks the
layer **below** it: conditions on a Linux host that silently stop *any* USB
device — scanner included — from being detected or used.

Checks, in order of how completely they explain "it is plugged in and
nothing happens":

1. **Dead xHCI host controllers.** When an xHCI controller dies
   (``HC died; cleaning up``, ``xHCI host controller not responding,
   assume dead``, ``xHC is not running`` — typically after a suspend/resume
   ``Port resume timed out``), the kernel keeps the controller's *root hubs*
   registered. So ``lsusb`` and ``opticon_detect.py`` still list the bus, but
   every physical port on it is dead: re-plugging a device produces **no
   kernel event at all** — which looks exactly like a charge-only cable or a
   dead scanner. The recovery ladder (unbind/bind → PCI remove + rescan →
   caveated reset → reboot, then the runtime-PM prevention) is emitted by
   :func:`recovery_commands`; or move the device to a port on a *different*
   controller.
2. **Serial-port grabbers.** ModemManager probes/claims ``/dev/ttyACM*``
   (the M-10 USB-COM personality is CDC-ACM) and brltty claims many
   USB-serial adapters. Neither stops *detection*, but both can make the port
   unusable or intermittently held open.
3. **Missing Opticon udev rules.** Without
   ``/etc/udev/rules.d/99-opticon-scanner.rules`` the HID personality's
   ``/dev/hidraw*`` is ``root:root 0600`` (permission denied for normal
   users); ``/dev/ttyACM*`` is usually ``root:dialout`` already.
4. **Kernel modules** for each Opticon personality present on disk.
5. **WSL USB passthrough**, delegated to ``scripts/opticon_detect.py``.

Usage::

    uv run python scripts/usb_host_check.py            # human-readable
    uv run python scripts/usb_host_check.py --json     # machine-readable

Exit codes: 0 = no problems found, 1 = at least one problem found,
2 = cannot inspect (not Linux, or dmesg unreadable).

Everything decision-making is a pure function over injectable inputs
(``tests/test_usb_host_check.py`` runs the real failure log from this
project's dev machine as a fixture); only ``gather()`` touches the host.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --------------------------------------------------------------------------
# xHCI controller liveness
# --------------------------------------------------------------------------

#: A controller is gone for good (until reset) when one of these appears.
DEATH_MARKERS = (
    "HC died; cleaning up",
    "xHCI host controller not responding",
    "xHC is not running",
    "assume dead",
)

#: Precursors that usually precede a death on a flaky controller.
DEATH_PRECURSORS = (
    "Port resume timed out",
    "xHCI host not responding to stop endpoint command",
)

#: Log lines proving a controller (re)registered and is running. The capital-H
#: "xHCI Host Controller" registration line must not be confused with the
#: lowercase "xHCI host controller not responding" death line.
ALIVE_MARKERS = (
    "new USB bus registered",
    "xHCI Host Controller",
)

_HC_RE = re.compile(r"xhci_hcd (\S+):\s*(.*)")
_USB_DEV_RE = re.compile(r"\busb (\d+)-")
_USB_ROOT_RE = re.compile(r"\busb usb(\d+):")
_BDF_RE = re.compile(r"(0000:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9])")


def parse_host_events(lines) -> tuple[dict, dict, list, dict]:
    """Scan dmesg lines for controller deaths and USB enumerations.

    Returns ``(deaths, last_enum, precursors, last_alive)`` where *deaths*
    maps a controller PCI address to the last line index that declared it
    dead, *last_enum* maps a bus number to the last line index showing a
    device (or the root hub itself) on that bus, *precursors* is a list of
    ``(bdf, line)`` for resume timeouts, and *last_alive* maps a PCI address
    to the last line index proving that controller registered or was rebound
    (so a recovered controller is not mistaken for a dead one).
    """
    deaths: dict[str, int] = {}
    last_enum: dict[int, int] = {}
    last_alive: dict[str, int] = {}
    precursors: list[tuple[str, str]] = []
    for i, line in enumerate(lines):
        m = _HC_RE.search(line)
        if m:
            bdf, msg = m.group(1), m.group(2)
            if any(marker in msg for marker in DEATH_MARKERS):
                deaths[bdf] = i
            elif any(marker in msg for marker in ALIVE_MARKERS):
                last_alive[bdf] = i
            elif any(marker in msg for marker in DEATH_PRECURSORS):
                precursors.append((bdf, msg.strip()))
            continue
        m = _USB_DEV_RE.search(line) or _USB_ROOT_RE.search(line)
        if m and "new" in line.lower():
            # Only attach events count as evidence of life. A "USB disconnect"
            # line also matches the bus pattern, and counting it would mask a
            # controller death that is followed by its devices detaching.
            last_enum[int(m.group(1))] = i
    return deaths, last_enum, precursors, last_alive


def dead_controllers(deaths: dict, last_enum: dict, bus_bdf: dict,
                     last_alive: dict | None = None) -> list[dict]:
    """Controllers that died and have seen no sign of life since.

    ``bus_bdf`` maps a USB bus number to its controller's PCI address. A
    controller is reported only when the death is *later* than every event on
    every bus it owns **and** later than any (re)registration of the
    controller itself — so a controller that was rebound after dying is not
    flagged just because nothing has been plugged into it yet. A bus that is
    merely idle (no death marker) is never reported.
    """
    last_alive = last_alive or {}
    out = []
    for bdf, died_at in deaths.items():
        buses = sorted(b for b, addr in bus_bdf.items() if addr == bdf)
        last = max((last_enum.get(b, -1) for b in buses), default=-1)
        last = max(last, last_alive.get(bdf, -1))
        out.append({
            "bdf": bdf,
            "buses": buses,
            "dead": died_at > last,
            "died_at": died_at,
            "last_enum_at": last,
        })
    return sorted(out, key=lambda d: d["bdf"])


def recovery_commands(bdf: str) -> list[str]:
    """Shell commands to revive one xHCI controller, in order of force.

    The order is the honest one measured on the 2026-10-10 host
    (docs/m10-unreachable-2026-10-10.md §3): unbind/bind first, then the
    PCI remove + rescan that actually worked there, then the function
    reset — demoted with its caveat: ``reset_method`` may be ``pm`` (no
    FLR), and a PM reset rarely revives a controller the kernel already
    declared dead. Reboot is the reliable last resort. The prevention
    step follows (the death was a runtime-PM resume failure), with the
    permanent udev rule left as a <VENDOR> placeholder + the command that
    fills it — this host's vendor id is deliberately NOT baked into a
    generic script.
    """
    return [
        "# 1. rebind the driver (gentlest)",
        f"echo {bdf} | sudo tee /sys/bus/pci/drivers/xhci_hcd/unbind && "
        "sleep 1 && "
        f"echo {bdf} | sudo tee /sys/bus/pci/drivers/xhci_hcd/bind",
        "# 2. PCI-level remove + rescan (rebuilds the device from scratch — "
        "this is what worked on the 2026-10-10 host)",
        f"echo 1 | sudo tee /sys/bus/pci/devices/{bdf}/remove && "
        "sleep 1 && "
        "echo 1 | sudo tee /sys/bus/pci/rescan",
        "# 3. function reset — CAVEAT: check "
        f"cat /sys/bus/pci/devices/{bdf}/reset_method first; 'pm' means no "
        "FLR, and a PM reset rarely revives a controller already declared "
        "dead",
        f"echo 1 | sudo tee /sys/bus/pci/devices/{bdf}/reset",
        "# 4. last resort: reboot — the only guaranteed cure",
        "# 5. prevention, once revived (the death was a runtime-PM resume "
        "failure — pin the controller awake or it recurs):",
        f"echo on | sudo tee /sys/bus/pci/devices/{bdf}/power/control",
        "# make it permanent (fill <VENDOR> from the command below — do NOT "
        "guess; this script is host-generic):",
        f"cat /sys/bus/pci/devices/{bdf}/vendor",
        "echo 'ACTION==\"add|change\", SUBSYSTEM==\"pci\", "
        "ATTR{vendor}=\"<VENDOR>\", ATTR{power/control}=\"on\"' | "
        "sudo tee /etc/udev/rules.d/98-usb-controller-nosuspend.rules",
        "sudo udevadm control --reload-rules",
    ]


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------

@dataclass
class Finding:
    severity: str          # "error" | "warning" | "info"
    title: str
    detail: str
    fix: list[str] = field(default_factory=list)


def _module_present(modules_root: str, name: str) -> bool:
    """Is the module file on disk? Kernel files hyphenate where modprobe
    names underscore (``modprobe cdc_acm`` -> ``cdc-acm.ko``), so accept both."""
    for candidate in {name, name.replace("_", "-")}:
        if glob.glob(os.path.join(modules_root, "**", f"{candidate}.ko*"),
                     recursive=True):
            return True
    return False


def analyze(dmesg_lines, bus_bdf: dict, *, udev_rules: str,
            modemmanager_active: bool, opticon_ignore_rule: bool,
            brltty_active: bool, modules_root: str | None,
            modules: dict[str, str] | None = None) -> list[Finding]:
    """Turn host facts into a ranked list of findings. Pure; no I/O."""
    findings: list[Finding] = []

    deaths, last_enum, precursors, last_alive = parse_host_events(dmesg_lines)
    dead = [d for d in dead_controllers(deaths, last_enum, bus_bdf,
                                        last_alive) if d["dead"]]
    for d in dead:
        buses = ", ".join(f"usb{b}" for b in d["buses"]) or "?"
        findings.append(Finding(
            severity="error",
            title=f"xHCI controller {d['bdf']} is dead (buses {buses})",
            detail="It died and no device has enumerated on its buses since. "
                   "The root hubs still appear in lsusb, so a device on this "
                   "controller produces NO kernel event when plugged in — "
                   "indistinguishable from a charge-only cable.",
            fix=recovery_commands(d["bdf"]),
        ))
    if precursors and not dead:
        for bdf, msg in precursors[-3:]:
            findings.append(Finding(
                severity="info",
                title=f"xHCI controller {bdf} reported a resume problem",
                detail=msg + " — a flaky controller may die on the next "
                             "suspend/resume; keep an eye on it.",
            ))

    if modemmanager_active and not opticon_ignore_rule:
        findings.append(Finding(
            severity="warning",
            title="ModemManager is active and will probe /dev/ttyACM*",
            detail="The M-10's USB-COM personality is CDC-ACM. ModemManager "
                   "sends AT commands to new ttyACM ports and can hold the "
                   "port open, making scans fail or the device disappear.",
            fix=[
                # The repo's own vendor-wide file — NOT a second rules file:
                # its header is explicit that "a second file would mean a
                # second install step and two rules that can drift".
                "sudo cp scripts/udev/99-opticon-scanner.rules "
                "/etc/udev/rules.d/ (it carries the MM-ignore lines)",
                "sudo udevadm control --reload-rules && "
                "sudo udevadm trigger  # then re-plug",
            ],
        ))

    if brltty_active:
        findings.append(Finding(
            severity="warning",
            title="brltty is active and may claim USB-serial scanners",
            detail="brltty treats many USB-serial devices as braille "
                   "displays and can detach them. Stop/disable it while "
                   "scanning if the port keeps vanishing.",
            fix=["sudo systemctl stop brltty brltty-udev",
                 "sudo systemctl mask brltty-udev"],
        ))

    if not os.path.exists(udev_rules):
        findings.append(Finding(
            severity="warning",
            title="Opticon udev rules are not installed",
            detail="The HID personality's /dev/hidraw* is root:root 0600 "
                   "without them, so a keyboard-mode scanner is detected but "
                   "unreadable by your user. (ttyACM* is usually "
                   "root:dialout, which works if you are in dialout.)",
            fix=[
                "sudo cp scripts/udev/99-opticon-scanner.rules "
                "/etc/udev/rules.d/",
                "sudo udevadm control --reload-rules && "
                "sudo udevadm trigger  # then re-plug",
            ],
        ))

    if modules is not None and modules_root:
        missing = [name for name in modules
                   if not _module_present(modules_root, name)]
        if missing:
            findings.append(Finding(
                severity="warning",
                title=f"Kernel modules missing: {', '.join(missing)}",
                detail="A personality whose driver is absent will enumerate "
                       "but produce no device node (no /dev/ttyACM*, "
                       "no /dev/hidraw*).",
                fix=["install the matching linux-modules package, or use "
                     "the raw-USB backend (scripts/opn2001.py --backend usb)"],
            ))
    return findings


# --------------------------------------------------------------------------
# Host collection
# --------------------------------------------------------------------------

def gather_dmesg() -> list[str] | None:
    # Plain `dmesg` (raw monotonic "[  123.456]"), deliberately NOT `dmesg -T`.
    # `-T` reconstructs wall-clock time from CLOCK_BOOTTIME and drifts ~+7 days
    # on this host after many suspend/resume cycles (qwen / verify-sw,
    # 2026-10-10). The parser reasons about line ORDER, never timestamps, so
    # plain kmsg order is the correct and skew-immune input. Do NOT "fix" this
    # to -T; test_gather_dmesg_must_use_plain_dmesg guards it.
    try:
        out = subprocess.run(["dmesg"], capture_output=True, text=True,
                             timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.splitlines()


def gather_bus_bdf(root: str = "/sys/bus/usb/devices") -> dict[int, str]:
    bus_bdf: dict[int, str] = {}
    try:
        names = os.listdir(root)
    except OSError:
        return bus_bdf
    for name in names:
        if re.fullmatch(r"usb\d+", name):
            real = os.path.realpath(os.path.join(root, name))
            # The controller is the LAST PCI address in the path: the leading
            # ones are the PCI bridges it hangs off (e.g. pci0000:00/
            # 0000:00:08.1/0000:c3:00.3/usb1).
            matches = _BDF_RE.findall(real)
            if matches:
                bus_bdf[int(name[3:])] = matches[-1]
    return bus_bdf


def _service_active(name: str) -> bool:
    try:
        out = subprocess.run(["systemctl", "is-active", name],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.stdout.strip() == "active"


def _opticon_mm_ignore(udev_dirs=("/etc/udev/rules.d", "/usr/lib/udev/rules.d",
                                  "/lib/udev/rules.d")) -> bool:
    for d in udev_dirs:
        for path in glob.glob(os.path.join(d, "*.rules")):
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            if re.search(r'idVendor}=="065a"', text, re.I) and \
               "ID_MM_DEVICE_IGNORE" in text:
                return True
    return False


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UDEV_RULES = "/etc/udev/rules.d/99-opticon-scanner.rules"

#: Modules each Opticon personality needs on disk (see docs/opticon-hardware.md).
MODULES = {
    "cdc_acm": "usb/class",       # 065A:A002 M-10 USB-COM
    "opticon": "usb/serial",      # 065A:0009 OPN-2001
    "usbserial": "usb/serial",    # generic USB-serial bridge
    "usbhid": "usb/core",         # HID personalities
}


def gather() -> dict:
    uname = os.uname().release
    modules_root = f"/lib/modules/{uname}"
    return {
        "dmesg": gather_dmesg(),
        "bus_bdf": gather_bus_bdf(),
        "udev_rules": UDEV_RULES,
        "modemmanager_active": _service_active("ModemManager"),
        "opticon_ignore_rule": _opticon_mm_ignore(),
        "brltty_active": _service_active("brltty"),
        "modules_root": modules_root,
        "modules": MODULES,
        "uname": uname,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Preflight the Linux host for USB conditions that prevent "
                    "scanner detection or use")
    ap.add_argument("--json", action="store_true",
                    help="emit findings as JSON")
    args = ap.parse_args(argv)

    if not sys.platform.startswith("linux"):
        print(f"usb_host_check: not Linux (platform {sys.platform}) — the "
              "xHCI/udev checks do not apply. See docs/opticon-hardware.md.",
              file=sys.stderr)
        return 2

    facts = gather()
    if facts["dmesg"] is None:
        print("usb_host_check: cannot read dmesg (try `sudo dmesg`, or check "
              "kernel.dmesg_restrict) — controller checks will be incomplete.",
              file=sys.stderr)
        lines: list[str] = []
    else:
        lines = facts["dmesg"]

    findings = analyze(
        lines, facts["bus_bdf"], udev_rules=facts["udev_rules"],
        modemmanager_active=facts["modemmanager_active"],
        opticon_ignore_rule=facts["opticon_ignore_rule"],
        brltty_active=facts["brltty_active"],
        modules_root=facts["modules_root"], modules=facts["modules"])

    if args.json:
        print(json.dumps({"kernel": facts["uname"],
                          "findings": [asdict(f) for f in findings]}, indent=2))
    else:
        print(f"host preflight — kernel {facts['uname']}")
        if not findings:
            print("\nNo host-level problems found that would prevent USB "
                  "scanner detection.")
        for f in findings:
            tag = {"error": "ERROR", "warning": "WARN", "info": "INFO"}[f.severity]
            print(f"\n[{tag}] {f.title}\n  {f.detail}")
            if f.fix:
                for line in f.fix:
                    print(f"    $ {line}")
        errors = sum(1 for f in findings if f.severity == "error")
        warns = sum(1 for f in findings if f.severity == "warning")
        print(f"\n{errors} error(s), {warns} warning(s), "
              f"{len(findings) - errors - warns} info")
    return 1 if any(f.severity in ("error", "warning") for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())