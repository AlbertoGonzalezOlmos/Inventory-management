#!/usr/bin/env python3
"""opticon_detect — the repository's single map of Opticon USB personalities.

Every Opticon scanner we have met on one vendor ID (``065A``) but several
personalities, and each personality needs a different transport, a different
reception tool and different host plumbing. Knowing *which* personality is
plugged in is a prerequisite for everything else, so it lives here — one
device-agnostic module, stdlib-only, importable from any tool and testable
without hardware (``find_linux_usb_devices`` takes an injectable sysfs root).

    uv run python scripts/opticon_detect.py            # human-readable survey
    uv run python scripts/opticon_detect.py --json     # machine-readable

Consumers:

* ``scripts/scanner_hid.py`` — receives from the USB-HID personality.
* ``scripts/opn2001.py detect`` — the OPN-2001-flavoured report (adds the
  RBBV/``--backend usb`` guidance on top of this module's inventory).
* ``scripts/scanner_bridge.py`` — the M-10 driver's port filter (it must open
  only ``065A:A002``, never whatever serial port happens to be first).

Personality table and its epistemic status: see ``docs/opticon-hardware.md``.
Only ``065A:0009`` (OPN-2001) and ``065A:A002`` (M-10 USB-COM, Specifications
Manual SS13063 §18.5) are documented by Opticon. ``065A:A001`` is *observed*
on this project's dev machine and is **not** in any public Opticon document we
could find — the table below records it as observed-but-unconfirmed on
purpose, and ``tests/test_opticon_detect.py`` fails if that label is quietly
upgraded without the hardware step that would prove it (scan the *USB COM
Port* configuration sheet and watch the device re-enumerate as ``A002``).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys

# --------------------------------------------------------------------------
# The hardware map
# --------------------------------------------------------------------------

#: Every Opticon device we know of shares this vendor ID.
OPTICON_VID = 0x065A

#: PID → what the device is, how to reach it, and how we know.
#:
#: ``evidence`` is load-bearing: it separates what Opticon publishes from what
#: we merely observed on one machine, so nobody "fixes" a hedge into a fact.
PERSONALITIES: dict[int, dict] = {
    0x0009: {
        "name": "OPN-2001 pocket memory scanner",
        "transport": "vendor serial (Opticon 'RBBV' stream, 9600 8O1)",
        "backend": "serial",
        "nodes": "ttys",
        "receive_with": "scripts/opn2001.py read",
        "evidence": "documented: Opticon OPN-2001 manual; Linux "
                    "drivers/usb/serial/opticon.c binds this VID:PID",
        "confirmed": True,
    },
    0xA001: {
        "name": "USB-HID keyboard-mode scanner "
                '(bus-reported name "Opticon USB Barcode Reader")',
        "transport": "USB HID boot keyboard (scans are typed, CR-suffixed)",
        "backend": "hidraw",
        "nodes": "hidraws",
        "receive_with": "scripts/scanner_hid.py",
        "evidence": "OBSERVED LIVE on this project's dev machine (2026-09-29, "
                    "WSL2 + usbipd-win → /dev/hidraw0). A001 appears in no "
                    "public Opticon document we could find: SS13063 §18.5 "
                    "lists only A002 for USB-COM and §18.4 (USB-HID) lists no "
                    "PID. Identity as the M-10's HID personality is the "
                    "leading hypothesis, NOT a confirmed fact — proving it is "
                    "one hardware step: scan the 'USB COM Port' configuration "
                    "sheet and check the device re-enumerates as 065A:A002.",
        "confirmed": False,
    },
    0xA002: {
        "name": "M-10 family in USB-COM (CDC-ACM) mode",
        "transport": "virtual serial port (CDC-ACM), bidirectional",
        "backend": "serial",
        "nodes": "ttys",
        "receive_with": "scripts/scanner_bridge.py (M-10 command channel)",
        "evidence": "documented: Opticon M-10 Specifications Manual SS13063 "
                    "§18.5 (USB-COM = CDC-ACM, VID 065A PID A002)",
        "confirmed": True,
    },
}

#: Sibling PID seen in Opticon's own documentation for other models
#: (OPR-2001 / NLV-1001 in keyboard mode). Not handled by this repo's tools;
#: listed so ``describe()`` can say "known, but not ours" instead of "unknown".
KNOWN_OTHER_PIDS = {
    0x0001: "OPR-2001 / NLV-1001 in keyboard mode (documented by Opticon; "
            "no tool in this repository)",
}


def describe(pid: int) -> dict:
    """Personality record for a PID, or an explicit 'unknown' record.

    Never raises and never invents an identity: an unrecognised PID is
    reported as unknown, which is the honest answer and the safe one (the
    alternative — guessing — is how a tool ends up opening the wrong device
    at the wrong line settings and interpreting binary protocol as barcodes).
    """
    if pid in PERSONALITIES:
        return {"pid": pid, "known": True, **PERSONALITIES[pid]}
    if pid in KNOWN_OTHER_PIDS:
        return {"pid": pid, "known": True, "name": KNOWN_OTHER_PIDS[pid],
                "transport": "unknown to this repo", "backend": None,
                "nodes": None, "receive_with": None,
                "evidence": "documented by Opticon for other models",
                "confirmed": True}
    return {"pid": pid, "known": False,
            "name": f"unrecognised Opticon device (065A:{pid:04X})",
            "transport": "unknown", "backend": None, "nodes": None,
            "receive_with": None,
            "evidence": "not in this repo's hardware map — do not guess; "
                        "check the bus-reported name and add it here with "
                        "its provenance",
            "confirmed": False}


# --------------------------------------------------------------------------
# Host discovery (Linux sysfs / WSL2 / Windows)
# --------------------------------------------------------------------------


def is_wsl() -> bool:
    try:
        with open("/proc/version", "rb") as fh:
            return b"microsoft" in fh.read().lower()
    except OSError:
        return False


def find_linux_usb_devices(root: str = "/sys/bus/usb/devices",
                           vendor: int = OPTICON_VID) -> list[dict]:
    """Scan sysfs for devices of ``vendor`` (any PID).

    Each entry is ``{sysfs, vid, pid, busnum, devnum, ttys, hidraws}``. Which
    nodes appear depends on the personality and on the guest kernel: a serial
    personality needs its driver bound (``ttyUSB*`` for the in-kernel
    ``opticon`` driver, ``ttyACM*`` for CDC-ACM), the HID personality needs
    ``CONFIG_HIDRAW``. ``root`` is injectable so this is unit-testable with a
    fake sysfs tree and no hardware.
    """
    found = []
    for path in glob.glob(os.path.join(root, "*")):
        try:
            with open(os.path.join(path, "idVendor")) as fh:
                vid = int(fh.read().strip(), 16)
            with open(os.path.join(path, "idProduct")) as fh:
                pid = int(fh.read().strip(), 16)
        except (OSError, ValueError):
            continue
        if vid != vendor:
            continue
        ttys = []
        for iface in glob.glob(os.path.join(path, "*:1.*")):
            tty_dir = os.path.join(iface, "tty")
            if os.path.isdir(tty_dir):
                ttys += [f"/dev/{t}" for t in sorted(os.listdir(tty_dir))]
        hidraws = []
        for iface in glob.glob(os.path.join(path, "*:1.*", "0*", "hidraw")):
            hidraws += [f"/dev/{t}" for t in sorted(os.listdir(iface))]

        def _int(name):
            try:
                with open(os.path.join(path, name)) as fh:
                    return int(fh.read().strip())
            except (OSError, ValueError):
                return None

        found.append({"sysfs": path, "vid": vid, "pid": pid,
                      "busnum": _int("busnum"), "devnum": _int("devnum"),
                      "ttys": ttys, "hidraws": hidraws})
    return found


def wsl_kernel_has_opticon() -> bool | None:
    """True/False if determinable, None if the kernel exposes no config."""
    try:
        import gzip
        with gzip.open("/proc/config.gz", "rt") as fh:
            for line in fh:
                if line.startswith("CONFIG_USB_SERIAL_OPTICON"):
                    return not line.split("=", 1)[1].strip().startswith("n") \
                        if "=" in line else False
    except OSError:
        pass
    for path in (f"/lib/modules/{os.uname().release}/modules.builtin",
                 f"/lib/modules/{os.uname().release}/modules.order"):
        try:
            with open(path) as fh:
                if "opticon" in fh.read():
                    return True
        except OSError:
            continue
    if glob.glob(f"/lib/modules/{os.uname().release}/kernel/drivers/usb/serial/opticon.ko*"):
        return True
    return False if os.path.exists("/proc/config.gz") else None


def usbipd_attached() -> bool:
    """True when the WSL2 guest can see any USB device (i.e. something is attached).

    usbipd-win passes a device through by exporting it and attaching it to the
    WSL ``/dev/bus/usb`` bus; without that attach the guest kernel never sees
    the hardware at all, whatever the host reports.
    """
    if not os.path.isdir("/dev/bus/usb"):
        return False
    return any(glob.glob(f"/dev/bus/usb/{b}/*") for b in os.listdir("/dev/bus/usb"))


def probe_windows_pnp() -> list[dict] | None:
    """Ask the Windows host (via WSL interop or natively) about the scanner.

    Returns a list of {Status, FriendlyName, InstanceId} for Opticon devices
    *and* for failed USB devices (problem 43 — 'Device Descriptor Request
    Failed'), which is how a device that is not talking to the host at all
    shows up. None when powershell is unavailable.
    """
    exe = shutil.which("powershell.exe") or shutil.which("powershell")
    if not exe:
        return None
    script = (
        "Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
        "Where-Object { $_.InstanceId -like '*VID_065A*' -or "
        "($_.InstanceId -like 'USB*' -and $_.Status -ne 'OK' "
        " -and $_.Class -in @('USB','Ports','Unknown',$null)) } | "
        "Select-Object Status,Class,FriendlyName,InstanceId | ConvertTo-Json"
    )
    try:
        out = subprocess.run(
            [exe, "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    return [data] if isinstance(data, dict) else data


def find_windows_ports(vendor: int = OPTICON_VID, pid: int | None = None) -> list[dict]:
    """COM ports belonging to ``vendor`` (optionally one ``pid``). Needs pyserial."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [
        {"port": p.device, "description": p.description, "vid": p.vid, "pid": p.pid}
        for p in list_ports.comports()
        if p.vid == vendor and (pid is None or p.pid == pid)
    ]


# --------------------------------------------------------------------------
# Survey
# --------------------------------------------------------------------------


def survey() -> dict:
    """Everything this host can tell us about attached Opticon devices.

    Device-agnostic on purpose: per-model advice (RBBV framing, ``--backend
    usb``, the M-10 command channel) belongs to the tool that speaks that
    model's protocol. This report answers "what is plugged in, through which
    node, and with what confidence do we know what it is".
    """
    report: dict = {
        "platform": sys.platform,
        "wsl": False,
        "usbipd_attached": None,
        "devices": [],
        "windows_pnp": None,
        "guidance": [],
    }
    if sys.platform.startswith("linux"):
        report["wsl"] = is_wsl()
        if report["wsl"]:
            report["usbipd_attached"] = usbipd_attached()
            report["wsl_kernel_opticon"] = wsl_kernel_has_opticon()
            if not report["usbipd_attached"]:
                report["guidance"].append(
                    "WSL2 sees no USB devices: they are attached to the Windows "
                    "host. Pass one through with usbipd-win (admin PowerShell): "
                    "usbipd bind --busid <id> ; then usbipd attach --wsl "
                    "--busid <id> — see docs/opticon-hardware.md"
                )
            elif report["wsl_kernel_opticon"] is False:
                report["guidance"].append(
                    "this WSL2 kernel has no CONFIG_USB_SERIAL_OPTICON, so a "
                    "serial-personality Opticon will enumerate but get no "
                    "/dev/ttyUSB*; the HID personality is unaffected "
                    "(CONFIG_HIDRAW is set in the stock kernel)"
                )
        for dev in find_linux_usb_devices():
            info = describe(dev["pid"])
            nodes = dev.get(info["nodes"] or "ttys") if info["nodes"] else []
            entry = {**dev, "personality": info, "nodes": nodes,
                     "usable": bool(nodes)}
            report["devices"].append(entry)
            if nodes and info["receive_with"]:
                report["guidance"].append(
                    f"{info['name']} on {', '.join(nodes)} — receive with "
                    f"{info['receive_with']} (docs/opticon-hardware.md)"
                )
            elif not nodes:
                report["guidance"].append(
                    f"Opticon device 065A:{dev['pid']:04X} enumerated but no "
                    f"kernel node bound — missing driver/permission, or the "
                    f"device is failing to enumerate (see "
                    f"docs/opticon-hardware.md)"
                )
        if report["wsl"]:
            report["windows_pnp"] = probe_windows_pnp()
            for entry in report["windows_pnp"] or []:
                status = (entry.get("Status") or "").lower()
                name = entry.get("FriendlyName") or ""
                if status == "error" or "Descriptor Request Failed" in name:
                    report["guidance"].append(
                        "Windows reports a FAILED USB enumeration "
                        f"({name!r}): the device is not talking to the host at "
                        "all. Usual causes: deeply-discharged battery (leave "
                        "it plugged in 30+ min) or a charge-only cable (try a "
                        "known-good DATA cable)"
                    )
    elif sys.platform == "win32":
        report["windows_pnp"] = probe_windows_pnp()
        for port in find_windows_ports():
            info = describe(port["pid"])
            report["devices"].append({**port, "personality": info,
                                      "nodes": [port["port"]], "usable": True})
            if info["receive_with"]:
                report["guidance"].append(
                    f"{info['name']} on {port['port']} — receive with "
                    f"{info['receive_with']} (docs/opticon-hardware.md)"
                )
        if not report["devices"]:
            report["guidance"].append(
                "no Opticon COM port: install the vendor's USB driver BEFORE "
                "plugging in, or attach the device to WSL with usbipd"
            )
    else:  # macOS and friends
        report["guidance"].append(
            "no vendor driver exists for the OPN-2001's vendor interface on "
            "macOS; the HID personality works via hidapi "
            "(uv run --with hid python scripts/scanner_hid.py) — see "
            "docs/opticon-hardware.md"
        )
    if not report["devices"]:
        report["guidance"].append(
            "no Opticon device (VID 065A) visible to this OS"
        )
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true",
                        help="emit the report as JSON instead of text")
    args = parser.parse_args(argv)

    report = survey()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print(f"platform: {report['platform']}"
          + (" (WSL2)" if report["wsl"] else ""))
    if report["usbipd_attached"] is not None:
        print(f"usbipd: {'attached device(s) visible' if report['usbipd_attached'] else 'nothing attached'}")
    if not report["devices"]:
        print("no Opticon device found")
    for dev in report["devices"]:
        info = dev["personality"]
        where = ", ".join(dev["nodes"]) or "(no node bound)"
        print(f"065A:{dev['pid']:04X}  {info['name']}")
        print(f"    node:     {where}")
        print(f"    identity: {'documented' if info['confirmed'] else 'OBSERVED, unconfirmed'}")
        if not info["confirmed"]:
            print(f"    note:     {info['evidence']}")
    for line in report["guidance"]:
        print(f"- {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
