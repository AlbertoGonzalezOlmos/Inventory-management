# Coordinated debug: Opticon M-10 USB connection (2026-11-20)

Five independent agents (`deepseek`, `mimo`, `glm`, `kimi`, `qwen`, each at maximum
thinking) planned this debugging task in parallel, cross-reviewed every other plan,
and rewrote their own. This document is the coordinator's synthesis: what they agree
on, what was settled, and the prioritized backlog we implement on branches.

Source artifacts: `/tmp/m10-debug/plans/*.md`, `/tmp/m10-debug/plans/*-v2.md`,
`/tmp/m10-debug/reviews/*.md`, `brief.md`. Durable record of the earlier incident:
`docs/m10-unreachable-2026-10-10.md` and `NOTES.md` §9.

Host: `hertha-shop`, kernel `7.0.0-43-generic`, booted 2026-11-16 21:43.

---

## 1. The symptom, measured

With the owner reporting the scanner plugged in:

```
Nov 17 19:50:29  usb 1-1: New USB device found, idVendor=065a, idProduct=a001   ← the one success
Nov 17 19:50:29  hid-generic 0003:065A:A001.0004: input,hidraw3: USB HID v1.11 Keyboard
Nov 17 19:55:06  usb 1-1: USB disconnect, device number 15                     ← dropped after 4m37s
Nov 18 05:53:51  usb 1-2: new full-speed USB device number 16 using xhci_hcd
Nov 18 05:53:51  usb 1-2: device descriptor read/64, error -71                 ← EPROTO
Nov 20 19:28:38  usb usb1-port2: attempt power cycle                           ← kernel gave up
```

* `-EPROTO` at the **first control transfer**: the port sees VBUS + D+ pull-up
  (attach events fire), the device never returns a descriptor, so no VID/PID is
  ever logged and no device node is ever created.
* 25 descriptor errors this boot. Zero `065a` in `lsusb`. Three hotplug ports
  (`usb1-port1/2/3`, under `/sys/bus/usb/devices/1-0:1.0/`) all read
  `state=not attached`, `over_current_count=0`, `disable=0`.
* Per-port tally: `1-2` ≈10 attempts / **0** successes (Nov 16 → Nov 20);
  `1-1` 3 attempts / **1** success — and even that needed three `-71` retries
  first (Nov 17 19:45 → 19:50), then dropped of its own accord 4m37s later.

**So the link was marginal on its best day, on both external jacks.** The failure
is below udev, below ModemManager, below every userspace layer: nothing exists yet
to be mis-permissioned.

## 2. What is settled — do not re-litigate

| Ruled out | Evidence |
|---|---|
| Dead xHCI controller (the 2026-10-10 failure mode) | No `HC died`/`assume dead`; camera `1-4` and BT `1-5` enumerate; `-71` lines *are* events; different BDF from the old doc |
| Runtime PM as trigger | PCI `0000:00:14.0/power/control` is already `on` (old prevention landed); `usb1` root-hub `auto` is a different, normal knob |
| udev rule missing/inert | `/etc/udev/rules.d/99-opticon-scanner.rules` is byte-identical to `scripts/udev/…`, installed Nov 17 19:45, carries both `ID_MM_DEVICE_IGNORE` forms — nothing to match, because no node exists |
| Permissions / groups / ModemManager | Same reason: pre-enumeration failure |
| Charge-only cable (literally no data wires) | Attach events fire, which a data-less cable cannot produce; a *marginal* data cable is H1, a data-less one is not |
| Over-current or admin port disable | `over_current_count=0`, `disable=0` on ports 1–3 |
| Suspend/resume | Zero suspend events this boot |
| Oct-10 §7 open items (MM-ignore, recovery order, prevention) | Already landed and regression-tested in `tests/test_usb_host_check.py` |
| Nov 18 06:14 xHCI remove as a crash | It was the owner running `sudo bash /tmp/usb-reset.sh`; recovery was already attempted and did not hold |

Two corrections to the phase-1 brief, both found by the agents:
* the sysfs hotplug port dirs **do** exist, one level down at
  `/sys/bus/usb/devices/1-0:1.0/usb1-portN`;
* `reset_method` is **absent** on `0000:00:14.0`, so the old doc's `pm` caveat and
  any `/reset` rung do not apply here.

## 3. Convergent hypotheses (all five agents, same ranking order)

| Rank | Hypothesis | Decisive test |
|---|---|---|
| H1 (~50–55%) | Marginal cable / connector / contact fault | Same jack, known-good **data** cable, bounded watch → clean `065a` enumeration = PASS |
| H2 (~20–25%) | Scanner-side fault (socket, board, PHY) or firmware wedge | Same unit + known-good cable on **another machine**; plus unplug ≥30 s to drain and reset |
| H3 (~15–20%) | Jack `1-2` faulty or wedged by the kernel's failed power cycle | Known-good device in the same jack → it enumerates = jack fine |
| H4 (~5–10%) | Host port/PHY or full-speed link timing | Only if H1–H3 exonerate both jacks: usbmon during a replug, then `old_scheme_first`, then reboot |

**None of these can be concluded from software.** Every agent independently reached
the same conclusion: the discriminating evidence requires one variable changed at a
time with a human hand on the plug, because the unit never gets far enough to appear
in any userspace list.

## 4. Separately proven: even a perfect USB fix does not deliver a scan

All five agents found the same second, independent blocker, which is *not* a USB
fault:

* `data/hcrm.db` has exactly one `terminals` row: `pentacosiarca-ThinkPad-T14s-Gen-4`
  (the old laptop, paired 2026-11-03).
* `data/scanner.json` forwards under that foreign identity.
* the daemon (PID 234104, up since Nov 17 19:52:36) **predates** the Nov 18
  identity-gate fixes, so it is running code from before the safeguards.
* `data/scanner-pairing.json` offers `hertha-shop`, which the server does not know →
  the kiosk pill's `/pair/auto` answers 401 `bad_pairing`, `--register` answers 409.
* `scan_events = 0`.

This is `NOTES.md` §9 verbatim ("a stale three-way identity strands scans"). Its
runbook is accurate; the destructive steps need an owner decision. Restarting with
`--register` alone is not enough — the process must start from current `main`.

## 5. The repo defect the agents actually found

`scripts/usb_host_check.py` — the one tool built for exactly this symptom — is
**misreporting the live failure**. In `parse_usb_flakiness()` /
the retry-fragile branch, an older Opticon attach + dropout (`1-1`, Nov 17) shadows
the newer exhausted attach ladder (`1-2`, Nov 18–20), because the retry-fragile
branch is gated on "Opticon never enumerated". Today it prints 0 errors / 4 warnings
and never names port `1-2` — the actionable port.

Second gap: it does not recognize the kernel's `attempt power cycle` marker, does not
keep a per-port success/failure tally, and does not print the hotplug port state
table — the three facts that distinguish "marginal link" from "dead port" from
"device absent".

## 6. Prioritized backlog

Ordered by (a) does it unblock the next measurement, (b) can agents do it without the
owner, (c) does it prevent a wrong conclusion.

| # | Item | Kind | Owner of the human step |
|---|---|---|---|
| **P1** | **Fix `usb_host_check.py` episode reporting**: latest-episode selection, per-port success/failure tally, `attempt power cycle` marker, hotplug port state table; regression fixtures from this boot's journal | code + tests | none — fully autonomous |
| **P2** | **One-variable physical triage**, run as a documented matrix with bounded, self-terminating watchers and a machine-readable PASS/FAIL per rung, so the owner's 5 plug experiments settle H1–H4 instead of generating ambiguous logs | code + docs | owner: replug / swap cable / other machine |
| **P3** | **Scope `docs/opticon-hardware.md` for this host**: `hertha-shop` subsection (controller `00:14.0`, ports 1/2/3 hotplug, camera `1-4`, BT `1-5`, PCI PM already pinned, no `reset_method`), the `-71` "marginal link" discriminator, and `attempt power cycle` semantics. Refresh the Oct-10 doc's open-item status | docs | none |
| **P4** | **Post-enumeration delivery re-home**: run the `NOTES.md` §9 runbook against current `main`, restart the daemon on current code, re-register `hertha-shop`, pair the kiosk on this host, prove a scan lands in `scan_events` **and** in the UI | runbook + verification | owner: approves the destructive DELETE / moves config aside |
| **P5** | **Replace the hardware** if P2 concludes H2 (another machine fails identically) | hardware | owner |

P1 comes first because the tool we will use to judge P2 currently lies about which
port is failing. P4 is deliberately after P2: fixing identity first would hide
whether the USB link ever recovered.

## 7. Definition of done for the whole task

Not "the device enumerates". All five agents independently rejected that bar.

1. First-try clean attach on a plain replug: `New USB device found, idVendor=065a,
   idProduct=a001` with no preceding `-71`, and stable across 3 replugs plus one
   `attempt power cycle` window.
2. `/dev/hidrawN` is `root:plugdev 0660` and `scripts/scanner_hid.py --list` marks it
   `<-- target`; `scanner_hid.py --count 3` decodes three real scans, content-exact.
3. Stability: ≥10–30 min, ≥10–20 scans, zero new `-71`/`USB disconnect` lines.
4. Daemon restarting on current `main`, identity `hertha-shop`, `--find-scanner`
   exits 0 (device + server + identity + pairing OK).
5. **Delivery proof**: a scan appears in this host's kiosk UI *and* `scan_events`
   increments exactly once — scanner → kernel → daemon → HTTP → browser.
6. `usb_host_check.py` names the right port and reports no fresh-episode finding for
   the verification window (whole-boot history is not the meter).
