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
   controller. A controller that is ALIVE but still suspends at idle
   (``power/control=auto`` — the mode that caused the death) gets an INFO
   nag with the same prevention commands: the host works, but the next
   suspend can repeat the incident.
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
6. **Scanner-stack pairing identity** (read-only): the daemon config's
   ``terminal_id`` vs the server's ``terminals`` table vs the one-click
   pairing offer file. A stale three-way mismatch — a config that forwards
   as ANOTHER host's terminal, or an offer naming a terminal the server
   does not know — lands scans in a queue no browser on this host can
   read (the kiosk pill answers 401 ``bad_pairing``). The finding prints
   the exact recovery commands; every destructive step (stop the daemon,
   ``DELETE /api/scanner/terminalss/<id>``, move the config aside) is an
   OPERATOR command — this tool never writes anything, and secrets or
   pairing codes are never read out or printed.
7. **USB link flakiness**: repeated ``device descriptor read/64, error
   -71`` and enumerate-then-dropout in the kernel log — physical-layer
   evidence (cable/connector/port/device), not proof of which component
   failed or that power/port faults are excluded. It can explain both
   "the scanner looks absent" (retry-ladder exhaustion) and "attached
   but silent" (dropout mid-session). Also reported: a running
   ``scanner_daemon`` (it holds the single-instance lock and the
   exclusive evdev grab — INFO, not a fault) and a configured stack with
   no Opticon device currently on the bus.

Usage::

    uv run python scripts/usb_host_check.py            # human-readable
    uv run python scripts/usb_host_check.py --json     # machine-readable

Exit codes: 0 = no problems found, 1 = at least one problem found
(findings of severity error OR warning — the pairing-identity and
link-flakiness findings are warnings), 2 = cannot inspect (not Linux, or
dmesg unreadable).

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
import shlex
import socket
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import quote as _urlquote

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


# --- USB link flakiness (the -71 / enumerate-then-dropout signature) ---------

#: Full-speed devices whose first descriptor reads keep failing with EPROTO
#: (-71) never finish enumeration; the kernel retries address rounds and
#: eventually gives up ("attempt power cycle") — the device then looks
#: absent even though it is plugged in. Every line names its own port, so
#: attribution stays exact even when two ports interleave.
_ATTACH_RE = re.compile(
    r"\busb (\S+): new (?:full|high|low|super)-speed USB device number \d+")

#: The descriptor-read failure line carries the failing port in its prefix.
_DESC_ERR_RE = re.compile(
    r"\busb (\S+): device descriptor read/\d+, error (-\d+)")

#: The Opticon attach (idVendor 065a) — the one device this repo cares about.
_ENUM_OPTICON_RE = re.compile(r"usb (\S+): New USB device found, idVendor=065a\b",
                              re.I)

_USB_DISCONNECT_RE = re.compile(r"usb (\S+): USB disconnect")

#: "usb usb1-port2: attempt power cycle" -> port "1-2": the kernel gave up on
#: that port after the retry ladder; it then stays silent until the next plug.
_POWER_CYCLE_RE = re.compile(r"\busb usb(\d+)-port(\d+): attempt power cycle\b")


def _port_key(port: str) -> tuple[int, int]:
    """Natural (bus, port) sort key for a "N-M" port string."""
    bus, _, num = port.partition("-")
    return (int(bus), int(num))


def _port_tally(port_stats: dict, *, first: str | None = None) -> str:
    """Render ``port 1-2: 12 attach attempts / 0 successes, 4 power cycles;
    port 1-1: 6 attach attempts / 1 success``.

    Latest-episode port first, then natural order. Only ports with flakiness
    evidence (descriptor errors, power cycles, or an Opticon success) appear —
    a clean camera/BT attach stays in ``port_stats`` but never pollutes the
    sentence. ``attaches`` counts kernel address rounds, so the wording says
    "attach attempts", never "plugs" (R8).
    """
    order: list[str] = []
    if first is not None and first in port_stats:
        order.append(first)
    order += sorted((p for p in port_stats if p not in order), key=_port_key)
    parts = []
    for port in order:
        st = port_stats[port]
        if not (st["descriptor_errors"] or st["power_cycles"]
                or st["successes"]):
            continue
        part = (f"port {port}: {st['attaches']} attach attempts / "
                f"{st['successes']} "
                + ("success" if st["successes"] == 1 else "successes"))
        if st["power_cycles"]:
            part += f", {st['power_cycles']} power cycles"
        parts.append(part)
    return "; ".join(parts)


def parse_usb_flakiness(lines) -> dict:
    """Scan kernel lines for the scanner's link signature. Pure; no I/O.

    Per-port state machine (R3): every line names its own port, so interleaved
    ports never steal each other's errors. An *episode* is one attach sequence
    on one port — the ``new … USB device number N`` rounds — until an Opticon
    success, a power cycle, a disconnect, or the next attach on that port. A
    journal window that starts mid-episode (descriptor errors or a power cycle
    with no attach line in view) yields a truncated episode with ``attaches
    == 0``; it is first-class, never degraded to silence (R4).

    The five legacy keys keep their exact meaning. The new keys are ALWAYS
    present (schema stability, R1): ``port_stats`` tallies every port with any
    USB activity (R7), ``power_cycle_ports`` is unique in first-seen order
    (R8), ``latest_attempt`` is the newest attach line, ``latest_success`` the
    last Opticon enumeration, and ``latest_episode`` the newest *interesting*
    episode (failed, or an Opticon success) — a clean camera/BT attach can
    never mask it (R7).
    """
    descriptor_errors = 0
    proto_errors = 0
    opticon_port = None
    enumerated_at = None
    disconnected_at = None
    port_stats: dict[str, dict] = {}
    power_cycle_ports: list[str] = []
    latest_attempt: dict | None = None
    latest_success: dict | None = None
    open_episodes: dict[str, dict] = {}
    latest_episode: dict | None = None

    def stats(port):
        return port_stats.setdefault(port, {
            "attaches": 0, "successes": 0, "descriptor_errors": 0,
            "power_cycles": 0, "last_index": -1, "last_ok": False})

    def consider(ep):
        nonlocal latest_episode
        if (ep["success"] or ep["descriptor_errors"] or ep["power_cycled"]) \
                and (latest_episode is None
                     or ep["index"] > latest_episode["index"]):
            latest_episode = ep

    def new_episode(port, i):
        return {"port": port, "index": i, "attaches": 1,
                "descriptor_errors": 0, "power_cycled": False,
                "success": False, "truncated": False}

    def truncated_episode(port, i):
        return {"port": port, "index": i, "attaches": 0,
                "descriptor_errors": 0, "power_cycled": False,
                "success": False, "truncated": True}

    for i, line in enumerate(lines):
        m = _ATTACH_RE.search(line)
        if m:
            port = m.group(1)
            st = stats(port)
            st["attaches"] += 1
            st["last_index"] = i
            st["last_ok"] = False
            if port in open_episodes:
                # Same ladder, next address round: one physical plug burns
                # two "new device number" lines (device 4 then 5). Count
                # the round, keep accumulating into the SAME episode (R8).
                open_episodes[port]["attaches"] += 1
            else:
                open_episodes[port] = new_episode(port, i)
            latest_attempt = {"port": port, "index": i}
            continue
        m = _DESC_ERR_RE.search(line)
        if m:
            port, code = m.group(1), m.group(2)
            descriptor_errors += 1
            if code == "-71":
                proto_errors += 1
            st = stats(port)
            st["descriptor_errors"] += 1
            st["last_index"] = i
            st["last_ok"] = False
            ep = open_episodes.get(port)
            if ep is None:  # the window starts mid-episode (R4)
                ep = truncated_episode(port, i)
                open_episodes[port] = ep
            ep["descriptor_errors"] += 1
            continue
        m = _POWER_CYCLE_RE.search(line)
        if m:
            port = f"{m.group(1)}-{m.group(2)}"
            st = stats(port)
            st["power_cycles"] += 1
            st["last_index"] = i
            st["last_ok"] = False
            if port not in power_cycle_ports:
                power_cycle_ports.append(port)
            ep = open_episodes.get(port)
            if ep is None:
                ep = truncated_episode(port, i)
            ep["power_cycled"] = True
            consider(ep)
            open_episodes.pop(port, None)
            continue
        m = _ENUM_OPTICON_RE.search(line)
        if m:
            port = m.group(1)
            opticon_port, enumerated_at = port, i
            disconnected_at = None  # replug: only a LATER dropout counts
            latest_success = {"port": port, "index": i}
            st = stats(port)
            st["successes"] += 1
            st["last_index"] = i
            st["last_ok"] = True
            ep = open_episodes.pop(port, None)
            if ep is not None:
                ep["success"] = True
                consider(ep)
            continue
        m = _USB_DISCONNECT_RE.search(line)
        if m:
            port = m.group(1)
            if (opticon_port is not None and disconnected_at is None
                    and port == opticon_port):
                disconnected_at = i
            if port in port_stats:
                port_stats[port]["last_index"] = i
            old = open_episodes.pop(port, None)
            if old is not None:
                consider(old)
            continue
    for ep in open_episodes.values():
        consider(ep)
    return {"descriptor_errors": descriptor_errors,
            "proto_errors": proto_errors,
            "opticon_port": opticon_port,
            "opticon_enumerated_at": enumerated_at,
            "opticon_disconnected_at": disconnected_at,
            "port_stats": port_stats,
            "power_cycle_ports": power_cycle_ports,
            "latest_attempt": latest_attempt,
            "latest_success": latest_success,
            "latest_episode": latest_episode}


def _legacy_dropout_finding(flaky: dict) -> Finding:
    """The enumerate-then-dropout finding, byte-identical to pre-P1 (R6):
    ``test_analyze_warns_on_enumerate_then_dropout`` pins title, detail and
    fix verbatim."""
    errors_seen = flaky["proto_errors"] or flaky["descriptor_errors"]
    return Finding(
        severity="warning",
        title=f"the scanner enumerated, then dropped off the USB bus "
              f"(port {flaky['opticon_port']})",
        detail=f"{errors_seen} descriptor-read error(s) this boot, one "
               "successful Opticon attach, then a USB disconnect — "
               "physical-layer evidence (cable/connector/port/device); it "
               "does not identify the failing component or rule out power/port "
               "faults. A flaky link explains both 'the scanner looks absent' "
               "(retry-ladder exhaustion on plug) and 'attached but silent' "
               "(dropout mid-session).",
        fix=[
            "journalctl -k | grep -E 'error -71|USB disconnect'  # the "
            "signature",
            "re-plug until the node appears: ls /sys/class/hidraw/",
            "try one known-good data cable, then another direct USB port "
            "(one change at a time)",
            "re-run: uv run python scripts/usb_host_check.py",
        ],
    )


def _legacy_retry_fragile_finding(flaky: dict) -> Finding:
    """The no-history retry-fragile finding, byte-identical to pre-P1 (R6):
    ``test_analyze_warns_when_the_retry_ladder_exhausts`` pins title, detail
    and fix[2] verbatim."""
    errors_seen = flaky["proto_errors"] or flaky["descriptor_errors"]
    return Finding(
        severity="warning",
        title=f"USB enumeration is retry-fragile: "
              f"{errors_seen} descriptor-read errors, no successful "
              "scanner enumeration this boot",
        detail="Each plug burns kernel address rounds; when the retry "
               "ladder exhausts (journal: 'attempt power cycle'), the "
               "device stays ABSENT — it looks like a dead scanner or a "
               "charge-only cable. Repeated descriptor-read failures are "
               "physical-layer evidence (cable/connector/port/device); they "
               "do not identify the failing component or rule out power/port "
               "faults.",
        fix=[
            "journalctl -k | grep -E 'error -71|new full-speed'  # the "
            "retry ladder at work",
            "re-plug until /dev/hidraw* appears "
            "(scripts/opticon_detect.py says which node)",
            "try one known-good data cable, then another direct USB port "
            "(one change at a time)",
        ],
    )


def _new_episode_finding(flaky: dict, ep: dict) -> Finding:
    """The new wording (R9) — used ONLY for the mixed case (older Opticon
    success, newer failing episode) and truncated windows. Numbers are
    computed (R8); the tripwire phrases stay; the failure is never claimed
    to be the scanner (R7)."""
    port = ep["port"]
    n = ep["descriptor_errors"]
    tally = _port_tally(flaky["port_stats"], first=port)
    title = (f"USB enumeration is retry-fragile on port {port}: "
             f"{n} descriptor-read errors"
             + (", the kernel power-cycled the port" if ep["power_cycled"]
                else ""))
    if ep["power_cycled"]:
        head = ("The newest attach episode on port " + port + " exhausted "
                "the retry ladder ('attempt power cycle') — the kernel gave "
                "up on that port")
    else:
        head = (f"The newest attach episode on port {port} burned {n} "
                "descriptor-read error(s) without completing a single "
                "descriptor read")
    if ep["truncated"]:
        head += " (the journal window starts mid-episode: the attach line " \
                "itself is not in the log)"
    head += ", so the device stays ABSENT even though it is plugged in"
    detail = f"{tally}. {head}"
    if (flaky["opticon_enumerated_at"] is not None
            and flaky["opticon_enumerated_at"] < ep["index"]):
        detail += (f"; an earlier Opticon attach on {flaky['opticon_port']} "
                   "succeeded, then dropped off"
                   if flaky["opticon_disconnected_at"] is not None else
                   f"; an earlier Opticon attach on {flaky['opticon_port']} "
                   "succeeded")
    detail += (". Repeated descriptor-read failures are physical-layer "
               "evidence (cable/connector/port/device); they do not identify "
               "the failing component or rule out power/port faults.")
    return Finding(
        severity="warning",
        title=title,
        detail=detail,
        fix=[
            "journalctl -k | grep -E 'error -71|attempt power cycle'  # the "
            "signature",
            "re-plug until the node appears: ls /sys/class/hidraw/",
            "try one known-good data cable, then another direct USB port "
            "(one change at a time)",
            "re-run: uv run python scripts/usb_host_check.py",
        ],
    )


def _flakiness_finding(flaky: dict, device_present: bool | None) -> Finding | None:
    """The ONE flakiness finding, selected by the newest attach episode (R5).

    Historical events may appear only as context, never as the headline. A
    clean camera/BT episode never masks a failing port (R7), because
    ``latest_episode`` only ever holds failed or Opticon-successful
    episodes. The legacy texts stay byte-identical where tests pin them
    (R6), and the legacy if/elif stays as the no-episode fallback."""
    ep = flaky.get("latest_episode")
    if ep is None:
        # Legacy fallback, unchanged (R6): only reachable when the window
        # holds no attach/error/power-cycle lines at all.
        if flaky["opticon_disconnected_at"] is not None:
            return _legacy_dropout_finding(flaky)
        if (flaky["descriptor_errors"] >= 3
                and flaky["opticon_enumerated_at"] is None
                and device_present is not True):
            return _legacy_retry_fragile_finding(flaky)
        return None
    if (ep["success"] and flaky["opticon_disconnected_at"] is not None
            and ep["port"] == flaky["opticon_port"]):
        return _legacy_dropout_finding(flaky)
    if (not ep["success"]
            and (ep["descriptor_errors"] >= 3 or ep["power_cycled"])
            and device_present is not True):
        if flaky["opticon_enumerated_at"] is None and not ep["truncated"]:
            # No Opticon ever enumerated and the window saw the attach: the
            # pinned no-history wording (R6), not the new one.
            return _legacy_retry_fragile_finding(flaky)
        return _new_episode_finding(flaky, ep)
    return None


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


def prevention_runtime_command(bdf: str) -> str:
    """Pin ONE controller awake until reboot. BDF-parameterised: never a
    vendor id, never a host-specific constant."""
    return f"echo on | sudo tee /sys/bus/pci/devices/{bdf}/power/control"


def prevention_permanent_rule(bdf: str) -> list[str]:
    """The vendor-wide permanent rule: the ``cat …/vendor`` fill-in, the
    ``<VENDOR>``-placeholder rule (this host's vendor id is deliberately
    NOT baked into a generic script), and the reload. Shared by the
    recovery ladder and the healthy-but-auto INFO finding so the two
    cannot drift."""
    return [
        "# make it permanent (fill <VENDOR> from the command below — do NOT "
        "guess; this script is host-generic). Needs systemd >= 255 for the "
        "add|change match-key alternation; on older systemd write two "
        "rules, one per ACTION:",
        f"cat /sys/bus/pci/devices/{bdf}/vendor",
        "echo 'ACTION==\"add|change\", SUBSYSTEM==\"pci\", "
        "ATTR{vendor}=\"<VENDOR>\", ATTR{power/control}=\"on\"' | "
        "sudo tee /etc/udev/rules.d/98-usb-controller-nosuspend.rules",
        "sudo udevadm control --reload-rules",
    ]


def prevention_commands(bdf: str) -> list[str]:
    """The shared prevention tail of :func:`recovery_commands` — also the
    fix list of the healthy-but-``auto`` INFO finding. One source."""
    return [
        "# prevention (the death was a runtime-PM resume failure — pin the "
        "controller awake or it recurs):",
        prevention_runtime_command(bdf),
        *prevention_permanent_rule(bdf),
    ]


def recovery_commands(bdf: str) -> list[str]:
    """Shell commands to revive one xHCI controller, in order of force.

    The order is the honest one measured on the 2026-10-10 host
    (docs/m10-unreachable-2026-10-10.md §3): unbind/bind first, then the
    PCI remove + rescan that actually worked there, then the function
    reset — demoted with its caveat: ``reset_method`` may be ``pm`` (no
    FLR), and a PM reset rarely revives a controller the kernel already
    declared dead. Reboot is the reliable last resort. The prevention
    tail (:func:`prevention_commands`) follows: the runtime
    ``power/control=on`` pin plus the permanent ``<VENDOR>``-placeholder
    udev rule — which needs systemd >= 255 for its ``add|change`` match-key
    alternation; on older systemd write two rules, one per ACTION.
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
        *prevention_commands(bdf),
    ]


# --------------------------------------------------------------------------
# Scanner-stack identity (READ-ONLY: this tool never writes ./data, never
# reads a secret out of the config, and never prints a pairing code)
# --------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_data_dir() -> str:
    """`$HCRM_DATA_DIR` or `<repo>/data` — the same resolution the daemon's
    default pairing file and app.database.DATA_DIR use."""
    return os.environ.get("HCRM_DATA_DIR", os.path.join(REPO_ROOT, "data"))


def _read_terminals_read_only(db_path: str) -> list[dict] | None:
    """The server's ``terminals`` table, opened strictly read-only.

    ``file:…?mode=ro`` guarantees no journal/WAL sidecar is ever created —
    the repo doctrine is that nothing runs against ``./data`` read-write.
    Returns None (fail-silent) when the DB is absent, unreadable, or has a
    different schema: a host without the app stack must produce no finding,
    never a false one.
    """
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{_urlquote(db_path)}?mode=ro",
                               uri=True, timeout=2.0)
        try:
            rows = conn.execute(
                "SELECT terminal_id, token_hash, last_seen_at FROM terminals "
                "ORDER BY terminal_id").fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None
    return [{"terminal_id": row[0], "paired": row[1] is not None,
             "last_seen": str(row[2]) if row[2] is not None else None}
            for row in rows]


def gather_scanner_identity(data_dir: str | None = None) -> dict:
    """Config/offer/server identity facts, all read-only, fail-silent.

    ``{config_path, config_terminal_id, offer_path, offer_terminal_id,
    terminals}`` where ``terminals`` is None when no readable DB exists.
    Only ``terminal_id`` is extracted from the JSON files — the secret and
    the pairing code are deliberately never read out (nothing downstream
    may print them)."""
    base = str(Path(data_dir) if data_dir is not None else Path(_default_data_dir()))
    out: dict = {
        "config_path": os.path.join(base, "scanner.json"),
        "config_terminal_id": None,
        "offer_path": os.path.join(base, "scanner-pairing.json"),
        "offer_terminal_id": None,
        "terminals": None,
    }
    for key, name in (("config_terminal_id", "scanner.json"),
                      ("offer_terminal_id", "scanner-pairing.json")):
        try:
            with open(os.path.join(base, name), encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        terminal_id = doc.get("terminal_id") if isinstance(doc, dict) else None
        if isinstance(terminal_id, str) and terminal_id:
            out[key] = terminal_id
    out["terminals"] = _read_terminals_read_only(
        os.path.join(base, "hcrm.db"))
    return out


def _default_lockfile() -> str:
    """The daemon's DaemonLock path (`scanner_daemon._default_lockfile_path`
    parity — read-only here: this tool NEVER flocks, so a running daemon is
    never disturbed)."""
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(base, "hcrm-scanner-daemon.lock")


def gather_daemon_pid(lock_path: str | None = None) -> int | None:
    """The running daemon's PID from its lockfile, or None.

    flock is released by the kernel when the holder dies, so a leftover
    file with a dead PID is stale residue, not a running daemon."""
    try:
        with open(lock_path or _default_lockfile(), encoding="ascii") as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid  # alive, just another user's daemon
    except OSError:
        return None
    return pid


def gather_device_present(sysfs_root: str = "/sys/bus/usb/devices") -> bool | None:
    """Any 065a device on the bus right now (None = sysfs unreadable)."""
    try:
        names = os.listdir(sysfs_root)
    except OSError:
        return None
    for name in names:
        try:
            with open(os.path.join(sysfs_root, name, "idVendor"),
                      encoding="ascii") as fh:
                if fh.read().strip().lower() == "065a":
                    return True
        except OSError:
            continue
    return False


def pairing_recovery_commands(config_terminal_id, *, config_path,
                              daemon_pid, delete_terminal) -> list[str]:
    """ONE recovery ladder for the stale-identity findings — the same shape
    README documents for the lost-browser-token recovery, so the daemon's
    advice, the docs and this tool cannot drift apart. Every destructive
    step is an OPERATOR command; the DELETE appears only when the config
    names a REGISTERED terminal (freeing it is the point), and it is flagged
    as the owner decision it is — the terminal may belong to a machine
    still in use."""
    cmds: list[str] = []
    if daemon_pid:
        cmds.append(f"kill {daemon_pid}  # stop the running daemon — "
                    "releases the single-instance lock AND the evdev grab")
    else:
        cmds.append("# stop the running daemon first (it holds the lock and "
                    "the evdev grab): kill <pid>")
    if delete_terminal and config_terminal_id:
        cmds.append(
            f"curl -X DELETE http://127.0.0.1:8000/api/scanner/terminals/"
            f"{config_terminal_id}  # DESTRUCTIVE, owner decision first (the "
            "terminal may be a machine in use); loopback, or a staff "
            "session's bearer token")
    cfg = shlex.quote(config_path) if config_path else "<data>/scanner.json"
    if config_path:
        cmds.append(f"mv {cfg} {cfg}.old-host  # set the stale identity aside "
                    "so --register mints THIS host's")
    cmds.append(f"uv run python scripts/scanner_daemon.py --config {cfg} "
                "--register --register-only  # re-register (writes the "
                "config + the one-click offer, 0600)")
    cmds.append("# then click the 'no scanner' pill in the kiosk browser ON "
                "THIS host — the read token is minted to that browser")
    return cmds


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
            modules: dict[str, str] | None = None,
            power_control: dict[str, str] | None = None,
            identity: dict | None = None,
            device_present: bool | None = None,
            daemon_pid: int | None = None,
            hostname: str | None = None) -> list[Finding]:
    """Turn host facts into a ranked list of findings. Pure; no I/O.

    The scanner-stack kwargs (``identity``/``device_present``/
    ``daemon_pid``/``hostname``, all default None) are optional on purpose:
    a host without the app stack must produce no identity findings — the
    tool stays host-generic. The -71 flakiness signature is parsed from
    ``dmesg_lines`` unconditionally."""
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

    # Healthy-but-auto controllers: the host works, so this is a NAG, not
    # an alarm — but runtime-PM resume failure is exactly what killed
    # 0000:c3:00.3 on 2026-10-10, and nothing else reminded the operator
    # that prevention was still unapplied. ONE aggregated finding for all
    # affected BDFs (the permanent rule is vendor-wide — per-BDF would
    # print N copies of it); the runtime pin is per-BDF because it is.
    # Dead controllers are skipped: their error finding already carries
    # the full ladder, prevention included.
    dead_bdfs = {d["bdf"] for d in dead}
    auto = sorted(b for b in dict.fromkeys(bus_bdf.values())
                  if b not in dead_bdfs
                  and (power_control or {}).get(b) == "auto")
    if auto:
        findings.append(Finding(
            severity="info",
            title=f"{len(auto)} xHCI controller(s) still suspend at idle "
                  f"(power/control=auto): {', '.join(auto)}",
            detail="Runtime-PM resume failure is what killed 0000:c3:00.3 "
                   "on 2026-10-10 (docs/m10-unreachable-2026-10-10.md "
                   "§1/§4): the controller dies on resume, the root hubs "
                   "stay registered, and every port on it goes silently "
                   "dead. 'auto' means it can happen again on the next "
                   "suspend.",
            fix=["# pin every affected controller awake (survives until "
                  "reboot):"]
                + [prevention_runtime_command(b) for b in auto]
                + prevention_permanent_rule(auto[0]),
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

    # --- USB link flakiness: the -71 / enumerate-then-dropout signature ---
    # The newest attach episode decides (R5); legacy texts stay byte-identical
    # where tests pin them (R6).
    flaky = parse_usb_flakiness(dmesg_lines)
    flaky_finding = _flakiness_finding(flaky, device_present)
    if flaky_finding is not None:
        findings.append(flaky_finding)

    # --- a running daemon: plain words, INFO (normal operation, but it
    # explains why capture tools on the input path see nothing and why a
    # second daemon refuses to start with a lock error)
    if daemon_pid:
        findings.append(Finding(
            severity="info",
            title=f"a scanner_daemon is running (PID {daemon_pid})",
            detail="It holds the single-instance lock AND the exclusive "
                   "EVIOCGRAB on the scanner's evdev node: capture tools "
                   "reading the input path see nothing BY DESIGN while it "
                   "runs (hidraw still receives — it taps below the input "
                   "layer), and a second daemon will refuse to start with "
                   "a lock error. The daemon's console owns the scan "
                   "output.",
            fix=[f"kill {daemon_pid}  # only to capture or re-register — "
                  "the lock and the grab release instantly"],
        ))

    # --- scanner-stack identity (only when the caller gathered facts) ---
    if identity is not None:
        config_terminal = identity.get("config_terminal_id")
        offer_terminal = identity.get("offer_terminal_id")
        terminals = identity.get("terminals")
        known = ({t["terminal_id"] for t in terminals}
                 if terminals is not None else None)
        stack_present = bool(config_terminal or offer_terminal)

        if device_present is False and stack_present:
            findings.append(Finding(
                severity="warning",
                title="no Opticon device is on the USB bus right now, but a "
                      "scanner stack is configured on this host",
                detail="The daemon/bridge has nothing to grab; if the device "
                       "was attached earlier this boot, the journal shows "
                       "where it went (see the flakiness finding above, if "
                       "present).",
                fix=[
                    "lsusb | grep -i 065a  # confirm absence",
                    "re-plug the scanner and re-run "
                    "uv run python scripts/usb_host_check.py",
                ],
            ))

        registered_row = next((t for t in terminals or []
                               if t["terminal_id"] == config_terminal), None)
        if (config_terminal and known is not None
                and registered_row is not None
                and hostname and config_terminal != hostname):
            last_seen = registered_row.get("last_seen") or "never"
            findings.append(Finding(
                severity="warning",
                title=f"the daemon config is ANOTHER host's terminal "
                      f"('{config_terminal}')",
                detail=f"'{config_terminal}' is registered on the server "
                       f"(last seen {last_seen}) but this host is "
                       f"'{hostname}'. The browser read token for that queue "
                       "was minted on the OLD machine — scans forwarded here "
                       "land in a queue no browser on this host can poll. "
                       "This alone makes the kiosk show 'no scanner' even "
                       "with a perfectly healthy device.",
                fix=pairing_recovery_commands(
                    config_terminal,
                    config_path=identity.get("config_path"),
                    daemon_pid=daemon_pid, delete_terminal=True),
            ))
        elif (config_terminal and known is not None
              and registered_row is None):
            findings.append(Finding(
                severity="warning",
                title=f"the daemon config names a terminal the server does "
                      f"not know ('{config_terminal}')",
                detail="Ingest will answer 401: the terminal was deleted "
                       "server-side, or the config was copied from a host "
                       "whose registration no longer exists.",
                fix=pairing_recovery_commands(
                    config_terminal,
                    config_path=identity.get("config_path"),
                    daemon_pid=daemon_pid, delete_terminal=False),
            ))

        if (offer_terminal and known is not None
                and offer_terminal not in known):
            divergence = ""
            if config_terminal and offer_terminal != config_terminal:
                divergence = (" It also disagrees with the config's terminal "
                              f"'{config_terminal}' — a three-way "
                              "config/offer/server mismatch.")
            findings.append(Finding(
                severity="warning",
                title=f"the one-click pairing offer is stale: terminal "
                      f"'{offer_terminal}' is not registered on the server",
                detail="Clicking the kiosk's 'no scanner' pill will answer "
                       "401 bad_pairing, and re-running --register on the "
                       "stale config would 409 already_registered. The "
                       "offer file is overwritten atomically by the next "
                       "successful --register." + divergence,
                fix=pairing_recovery_commands(
                    config_terminal,
                    config_path=identity.get("config_path"),
                    daemon_pid=daemon_pid,
                    delete_terminal=registered_row is not None),
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


UDEV_RULES = "/etc/udev/rules.d/99-opticon-scanner.rules"

#: Modules each Opticon personality needs on disk (see docs/opticon-hardware.md).
MODULES = {
    "cdc_acm": "usb/class",       # 065A:A002 M-10 USB-COM
    "opticon": "usb/serial",      # 065A:0009 OPN-2001
    "usbserial": "usb/serial",    # generic USB-serial bridge
    "usbhid": "usb/core",         # HID personalities
}


def gather_power_control(bdfs, root: str = "/sys/bus/pci/devices") -> dict[str, str]:
    """``{bdf: "auto"|"on"}`` read from ``<root>/<bdf>/power/control``.

    Default-argument root (the ``gather_bus_bdf`` convention) so tests
    point it at a ``tmp_path`` fake tree. FAIL-SILENT per bdf: an
    unreadable or absent node is OMITTED, never guessed — a container, a
    non-Linux host or a permissions problem must produce no finding
    rather than a false one.
    """
    out: dict[str, str] = {}
    for bdf in bdfs:
        try:
            with open(os.path.join(root, bdf, "power", "control"),
                      encoding="ascii") as fh:
                value = fh.read().strip()
        except OSError:
            continue
        if value:
            out[bdf] = value
    return out


def gather() -> dict:
    uname = os.uname().release
    modules_root = f"/lib/modules/{uname}"
    bus_bdf = gather_bus_bdf()
    return {
        "dmesg": gather_dmesg(),
        "bus_bdf": bus_bdf,
        "power_control": gather_power_control(set(bus_bdf.values())),
        "udev_rules": UDEV_RULES,
        "modemmanager_active": _service_active("ModemManager"),
        "opticon_ignore_rule": _opticon_mm_ignore(),
        "brltty_active": _service_active("brltty"),
        "modules_root": modules_root,
        "modules": MODULES,
        "uname": uname,
        # scanner-stack facts — all read-only, fail-silent (a host without
        # the app stack yields identity with terminal ids None + terminals
        # None, which produces no findings)
        "identity": gather_scanner_identity(),
        "device_present": gather_device_present(),
        "daemon_pid": gather_daemon_pid(),
        "hostname": socket.gethostname(),
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
        modules_root=facts["modules_root"], modules=facts["modules"],
        power_control=facts.get("power_control"),
        identity=facts.get("identity"),
        device_present=facts.get("device_present"),
        daemon_pid=facts.get("daemon_pid"),
        hostname=facts.get("hostname"))

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
    if facts["dmesg"] is None:
        return 2
    return 1 if any(f.severity in ("error", "warning") for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
