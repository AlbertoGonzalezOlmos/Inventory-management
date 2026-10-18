# Diagnosis — "the M-10 is plugged in but cannot be reached" (2026-10-10)

> ## ✅ RESOLVED 22:54 CEST — read §8 first
>
> The owner revived `c3:00.3` with unbind/bind + PCI remove/rescan (**no
> reboot**), and the scanner enumerated on the first attach as `065a:a001` on
> `/dev/hidraw1`. The diagnosis in §1–§2 was confirmed by the cure: the device
> landed on `usb1-port2`, an external USB-A jack on exactly the controller these
> sections name. **One blocker remains** — `/dev/hidraw1` is `root:root 0600`
> (§8) — and the prevention step in §4 has still not been applied, so it can die
> again.
>
> Cross-agent working note. Three pi sessions investigated this independently in
> the same repo on 2026-10-10; this file merges what each measured and says who
> found what, so the next reader does not repeat any of it.
>
> Live scratch channel: **`SCANNER-AGENT-SYNC.md`** (repo root, mirrored to
> `/tmp/scanner-agent-sync.md`) — that is the file the agents append to while
> working. This document is the durable, dated write-up: it carries the full
> command-for-command evidence and the corrections §7 lists.
>
> **Everything below is measured on the host, not inferred.** Host:
> `pentacosiarca-ThinkPad-T14s-Gen-4`, native Linux (Ubuntu, kernel
> `6.8.0-146-generic`), booted `2026-10-02 06:51:48`, uptime 8d15h at time of
> writing. Reproduce any line with the command next to it.

---

## 1. Answer

**The M-10 is not unreachable because of anything in this repository. The USB host
controller that the machine's two external USB-A ports hang off is dead, and has
been since 2026-10-03 10:58:34.**

A device plugged into those ports produces **no kernel event at all** — not an
error, not a descriptor failure, nothing. So every layer above (udev, `cdc_acm`,
`opticon`, `app/scanner/`, `scripts/opticon_detect.py`) is silently starved of
input and correctly reports "no scanner". The failure is below all of them.

```
$ journalctl -k | grep -iE "xhci|HC died|assume dead|couldn't allocate"
Oct 03 10:58:23 kernel: xhci_hcd 0000:c3:00.3: xHC is not running.
Oct 03 10:58:23 kernel: xhci_hcd 0000:c3:00.3: Port resume timed out, port 1-3: 0x400e03
Oct 03 10:58:34 kernel: xhci_hcd 0000:c3:00.3: xHCI host not responding to stop endpoint command
Oct 03 10:58:34 kernel: xhci_hcd 0000:c3:00.3: xHCI host controller not responding, assume dead
Oct 03 10:58:34 kernel: xhci_hcd 0000:c3:00.3: HC died; cleaning up
Oct 03 10:58:34 kernel: usb usb1-port3: couldn't allocate usb_device
```

That is the whole story. Two supporting facts make it airtight:

- **`065A` has never appeared in this host's kernel journal.**
  `journalctl -k | grep -icE "065a|opticon"` → `0`, across every boot recorded.
  The Opticon has therefore *never* enumerated here — so no personality/PID
  filter, driver choice, or app-level rule can be the cause.
- **Nothing external has enumerated since the controller died.** The last
  external device ever seen was a Razer Mamba on `usb 1-1` at `Oct 02 18:24:56`
  (removed `22:34:37`). Since `Oct 03 10:58:34`: zero USB events of any kind.

### The trap that hides it

`lsusb` still lists Bus 001 and Bus 002, and `lsusb -t` still prints
`Driver=xhci_hcd/5p` for them. **When an xHC dies the kernel keeps its root hubs
registered**, so the bus looks healthy and `scripts/opticon_detect.py` reports
"no Opticon device visible" rather than "this bus is dead". Likewise
`/sys/.../usb1-port*/state` reads `not attached` for every port — meaningless on
a controller that cannot answer. Only the journal shows the death.

---

## 2. Which physical ports are dead, and which are alive

This is the part that turns the diagnosis into an action. Mapped with
`readlink -f /sys/bus/usb/devices/usbN` → PCI BDF, cross-checked against
`connect_type` and the enumeration history above.

| PCI controller | USB buses | Ports | Physical jack | State |
|---|---|---|---|---|
| `0000:c3:00.3` | 1 (USB2, 5p), 2 (USB3, 2p) | `usb1-port1`, `usb1-port2`, `usb2-port1`, `usb2-port2` = `hotplug`; `usb1-port3` = onboard hub | **the two USB-A jacks** | ☠ **DEAD since Oct 03** |
| `0000:c3:00.4` | 3, 4 | `usb3-port1` = `not used` (internal) | integrated camera only | ✅ alive (suspended) |
| `0000:c5:00.4` | 5, 6 | `usb5-port1` = `hotplug` | **a USB-C / USB4 port** | ✅ alive |
| `0000:c5:00.3` | 7, 8 | `usb7-port1` = `hotplug` | **the other USB-C / Thunderbolt 4 port** | ✅ alive |

Evidence the USB-A jacks are on the dead controller: the Razer Mamba enumerated
as `usb 1-1`, i.e. bus 1 port 1 → `0000:c3:00.3`; and `usb2` (the SuperSpeed
half of the same two jacks) has exactly two `hotplug` ports. `c3:00.4`'s only
port is the internal camera. The two USB-C ports ride the USB4/TB controllers
(`c5:00.3`, `c5:00.4`, alongside the TB NHIs at `c5:00.5`/`c5:00.6`), which are
alive and idle.

**Note the trigger:** the death began with `Port resume timed out, port 1-3` —
the onboard Genesys hub (`05e3:0610`, with `10ab:9309` behind it), which was
present from boot. A failing runtime-PM resume on that hub took the whole
controller down with it. `power/control` on `0000:c3:00.3` is `auto`, and its
`power/runtime_status` is pinned at `active` (a wedged controller never
suspends). Expect a repeat unless runtime PM is pinned off — see §4.

### ⇒ The immediate, no-root test and workaround

**Plug the M-10 into a USB-C port** (directly if the cable is USB-C, or via a
USB-C→USB-A adapter). Those ports are on live controllers. Watch it land:

```bash
sudo journalctl -kf | grep --line-buffered -iE "usb|xhci|065a"
# then plug in. A healthy attach looks like:
#   usb 7-1: new full-speed USB device number 2 using xhci_hcd
#   usb 7-1: New USB device found, idVendor=065a, idProduct=a001   (or a002)
```

This one experiment settles three questions at once:

| Result | Conclusion |
|---|---|
| Enumerates on bus 5–8 | Scanner and cable are fine; the USB-A ports/controller are the fault. Keep using USB-C, then revive `c3:00.3` at leisure. |
| Still nothing on bus 5–8 | Suspect the cable (charge-only / broken data pair) or the unit. Try another cable before blaming hardware. |
| `idProduct=a001` | HID keyboard personality → `scripts/scanner_hid.py`. The M-10 **driver** only accepts `a002` (see §5). |
| `idProduct=a002` | USB-COM → `scripts/scanner_bridge.py` / `scripts/m10_scan_test.py` will find it. |

A `journalctl -kf` watcher is already running in the background from an earlier
agent, appending to `/tmp/usb-watch.log` (0 bytes = no events, which is itself
the finding).

---

## 3. Reviving the dead controller (needs root — no passwordless sudo here)

`sudo -n true` → *"a password is required"*, so a human must run these. In order
of increasing force:

```bash
# 1. Rebind the driver
echo 0000:c3:00.3 | sudo tee /sys/bus/pci/drivers/xhci_hcd/unbind
sleep 1
echo 0000:c3:00.3 | sudo tee /sys/bus/pci/drivers/xhci_hcd/bind

# 2. PCI-level remove + rescan (stronger: rebuilds the device from scratch)
echo 1 | sudo tee /sys/bus/pci/devices/0000:c3:00.3/remove
sleep 1
echo 1 | sudo tee /sys/bus/pci/rescan

# 3. Function reset
echo 1 | sudo tee /sys/bus/pci/devices/0000:c3:00.3/reset

# 4. Reboot — the only guaranteed cure for an "assume dead" xHC
```

**Caveat measured on this host:** `reset_method` for `0000:c3:00.3` is **`pm`**,
i.e. the only supported reset is a D3hot→D0 power-management reset — there is no
FLR. A PM reset frequently does *not* revive a controller the kernel has already
declared dead, so step 3 is weaker than it looks and **step 4 (reboot) is the
reliable fix** after 8d15h of uptime. Prefer step 1→2, and if the ports are
still silent, reboot.

After recovery, re-plug and confirm with the §2 watcher, then run
`uv run python scripts/opticon_detect.py` and
`uv run python scripts/m10_scan_test.py --wait 60`.

---

## 4. Stopping it happening again

The death was a runtime-PM resume failure, and `power/control` is still `auto`:

```bash
# pin the controller awake (survives until reboot)
echo on | sudo tee /sys/bus/pci/devices/0000:c3:00.3/power/control

# make it permanent
echo 'ACTION=="add|change", SUBSYSTEM=="pci", ATTR{vendor}=="0x1022", ATTR{power/control}="on"' \
  | sudo tee /etc/udev/rules.d/98-usb-controller-nosuspend.rules
sudo udevadm control --reload-rules
```

Also worth noting: the port that failed to resume, `usb1-port3`, is the
**onboard** Genesys hub (`05e3:0610`), and `1-3.1` behind it is the **internal
USI Bluetooth module** — `readlink -f /sys/class/bluetooth/hci0/device` resolves
to `.../0000:c3:00.3/usb1/1-3/1-3.1/1-3.1:1.0`, `10ab` is *USI Co., Ltd*, and
`hci0` messages follow its enumeration immediately.

> **Correction.** An earlier revision of this section advised checking for a
> dock or hub plugged into a USB-A port and trying without it. There is no dock:
> the hub is soldered to the board and the device behind it is the laptop's own
> Bluetooth radio. The trigger is therefore an internal runtime-PM/firmware
> fault on `c3:00.3`, not a removable accessory — pinning `power/control=on`
> above is the whole of the available mitigation.

---

## 5. What will bite *after* the controller is back (repo-level, ranked)

None of these can be the current cause (the device never reaches the kernel),
but each is a real "cannot be reached" one layer up. Verified state on this host:

| # | Finding | Measured state here | Action |
|---|---|---|---|
| 1 | **Personality filter.** `find_scanner_ports()` accepts only `065A:A002`; a unit in HID mode (`A001`) is *declined by design* (REVIEW-m10.md P4) | n/a until the device appears | If it enumerates as `A001`: scan the *USB COM Port* sheet to switch personality, or pass `--any-port`, or use `scripts/scanner_hid.py` |
| 2 | **ModemManager is `active`** and probes every new `/dev/ttyACM*` — the M-10's USB-COM personality *is* CDC-ACM | `systemctl is-active ModemManager` → `active` | `scripts/udev/99-opticon-scanner.rules` has **no** `ENV{ID_MM_DEVICE_IGNORE}="1"` line. Add one for `065a` tty nodes |
| 3 | **Repo udev rule not installed — and it genuinely matters here** | `/etc/udev/rules.d/` holds only `70-persistent-cd`, `70-snap.foliate`, `70-snap.snapd` | `sudo cp scripts/udev/99-opticon-scanner.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules`. Needed for the **HID** personality and the **pyusb** backend; *not* needed for `ttyACM`. See #4 for the rule-by-rule evidence |
| 4 | `/dev/hidraw0` is a **red herring** — it is the i²c touchpad, not a scanner | `HID_NAME=SYNA8018:00 06CB:CE67`, `HID_PHYS=i2c-SYNA8018:00` | Ignore that node. But do **not** conclude hidraw is fine: no scanner hidraw node gets `uaccess` either — see the table below |

**Why #3 is required, per node** (stock Ubuntu 24.04 udev, verified by reading
the rules rather than assuming `logind` handles it):

| Node | Stock rule that applies | Result for our user | Repo rule needed? |
|---|---|---|---|
| `/dev/hidrawN` (HID `A001`) | **none.** `70-uaccess.rules:89` is the only `SUBSYSTEM=="hidraw"` uaccess line and it is gated on `ENV{ID_AV_PRODUCTION_CONTROLLER}=="1"`; every other hidraw uaccess tag is vendor-pinned gamepad/VR/FIDO (`60-steam-input.rules`, `60-steam-vr.rules`) | kernel default `root:root 0600` → **EACCES** | **YES** |
| `/dev/ttyACM0` (USB-COM `A002`) | `50-udev-default.rules:45` `KERNEL=="tty[A-Z]*[0-9]\|…", GROUP="dialout"` — `ttyACM0` matches that regex | `root:dialout 0660`, and the user *is* in `dialout` | no |
| `/dev/bus/usb/…` (pyusb backend) | `50-udev-default.rules:72` `SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", MODE="0664"` | `root:root 0664` → readable but **cannot claim the interface** | **YES** |

`grep -rni 065a /usr/lib/udev/` returns only *name* entries in
`20-usb-vendor-model.hwdb` (`v065A*`, `v065Ap0001*`, `v065Ap0009*` — note that
neither `A001` nor `A002` is in the USB ID database, so `lsusb` will print the
vendor but not a product name). There is no permission tag for Opticon anywhere
in the stock rules.
| 5 | **`brltty` is not a factor here** | `brltty`, `brltty-udev` → `inactive` | Nothing to do |
| 6 | Kernel modules for every personality **are present** | `CONFIG_HIDRAW=y`, `CONFIG_USB_ACM=m`, `CONFIG_USB_SERIAL_OPTICON=m`; `opticon.ko.zst` and `cdc-acm.ko.zst` on disk | Nothing to do |
| 7 | User is already in `dialout` and `plugdev` | `id -nG` | Nothing to do |

---

## 6. ⚠ `docs/opticon-hardware.md` describes a *different machine*

This will mislead the next person debugging exactly this symptom, so it is worth
fixing in the canonical home rather than only noting it here.

The doc states: *"This repository's dev machine is WSL2 (Ubuntu on Windows 11),
so all …"*, then builds a three-layer usbipd model on that, says the WSL2 kernel
has *"CONFIG_USB_SERIAL_OPTICON is not set"*, says *"WSL sessions have no seat,
so [uaccess] never fires"*, and documents a Windows *"problem 43"* failure on
port HS05. **None of that holds on this host:**

| Doc claim (WSL2) | Measured here (native ThinkPad) |
|---|---|
| usbipd passthrough layer must attach the device | No usbipd, no Windows host. `grep -qi microsoft /proc/version` → not WSL. `opticon_detect.py`'s "usbipd: nothing attached" path is unreachable |
| `CONFIG_USB_SERIAL_OPTICON is not set` | `=m`, and `opticon.ko.zst` is installed |
| No seat → `uaccess` never fires → hidraw/usbfs stay root-only | The **seat** part is wrong but the **conclusion** is right. `seat0` does exist here with an active session (`loginctl`) — and it still would not help, because stock Ubuntu has no generic hidraw or usbfs `uaccess` rule (§5 #4). So the repo's rule is required on native Linux *exactly as much as* on WSL, just for a different reason than the doc gives |
| Windows problem 43 on port HS05 | Not applicable; the native equivalent fault is the dead xHC in §1 |

Consequence for the diagnosis above: **permissions are not the current cause**,
because nothing enumerates at all — but they *will* be a cause the moment the
controller is back and the unit turns out to be in HID personality. Advice
derived from the WSL2 framing that *is* a dead end here: "re-attach with
usbipd", "the `opticon` module is missing", "add yourself to `dialout` to fix
hidraw" (dialout covers `ttyACM`, not `hidraw`).

> **Correction recorded so it is not repeated.** An earlier revision of this
> note claimed the missing-udev-rule warning was a *false positive* on this
> host, reasoning that `seat0` + `uaccess` would chown the scanner's nodes. That
> was wrong: `uaccess` is not tagged for generic hidraw or usbfs at all. The
> claim was retracted in `SCANNER-AGENT-SYNC.md` (PEER UPDATES, item E) once the
> rules were actually read. The lesson is the one REVIEW-m10.md §5.4 already
> makes — a *fact* about the host has to be measured, not inferred from which
> machine the docs happen to describe.

Suggested fix: keep the WSL2 material (it is a real, documented second machine)
but scope it explicitly, and add a native-Linux section carrying §1–§4 — a dead
xHC is a *native-host* failure mode that the WSL2 framing cannot even express.

---

## 7. Who found what (so the work is not redone)

| Session | Contribution |
|---|---|
| `01a1275c` (19:48) | OPN-2001 sibling investigation; wrote `scripts/scanner_test.py` |
| `01a12763` (19:56) | **Found the dead xHCI controller.** Wrote `scripts/m10_scan_test.py` (barcode + QR read test, on-screen QR target via the vendored Nayuki encoder) and `scripts/usb_host_check.py` + `tests/test_usb_host_check.py`, with the real Oct-03 failure log as a fixture |
| `01a1276f` (20:09) | Independently confirmed zero enumeration events; started the live `/tmp/usb-watch.log` watcher; narrowed to cable-vs-port-vs-controller |
| `01a12771` (20:11, this note) | Independently confirmed the death from the journal; **mapped physical ports → controllers (§2)**, which identifies the two USB-A jacks as dead and both USB-C ports as alive; established that `065A` has *never* enumerated on this host (all boots, not just this one); measured `reset_method=pm` (§3 caveat); identified the runtime-PM trigger and the onboard hub on `usb1-port3` (§4); found the doc/machine mismatch (§6) and read the stock udev rules to establish which device nodes actually need the repo's rule (§5 #3–#4) |

### Corrections handed back to `01a12763` via `SCANNER-AGENT-SYNC.md`

Its diagnosis is right and its detector is the correct tool. Four things in its
sync file needed amending, all recorded there under **PEER UPDATES**:

| Its claim | Correction |
|---|---|
| FIX step 3: `echo 1 > …/reset` | `reset_method` is **`pm`** — no FLR. A PM reset rarely revives a declared-dead xHC. Order: unbind/bind → PCI remove+rescan → reset → reboot |
| Workaround: USB-C ports are on `c3:00.4` / `c5:00.3` / `c5:00.4` | `c3:00.4` is the **internal camera** controller (`usb3-port1` = `not used`). Only `c5:00.3` and `c5:00.4` are external USB-C |
| (no prevention step) | `power/control=auto` is still set and the trigger was a resume timeout on the onboard hub at `usb1-port3`. Pin `power/control=on` or it recurs |
| SECONDARY 2: hidraw is `root:root` without the repo rule | **Correct — I withdrew my objection to it.** No generic hidraw `uaccess` rule exists in stock Ubuntu (§5 #4), so the rule is required on a native seat too. What *is* wrong is the doc's *reasoning* for it ("WSL has no seat") |

**Open items for whoever picks this up next** (deliberately not done here, to
avoid two agents writing the same files):

1. Add `ID_MM_DEVICE_IGNORE` to `scripts/udev/99-opticon-scanner.rules` (§5.2).
2. Scope `docs/opticon-hardware.md`'s WSL2 section and add the native-Linux /
   dead-controller material (§6). Consider teaching `scripts/usb_host_check.py`
   to print the §2 port map — it currently names the dead BDF but not which
   *physical jacks* that means, which is the only actionable form.
3. `usb_host_check.py`'s `recovery_commands()` lists `reset` before the reboot
   note; given `reset_method=pm` it should be ordered unbind/bind → PCI
   remove+rescan → reset → reboot, and should carry the `power/control=on`
   prevention step (§4), which it currently omits.

---

## 8. Resolution (22:54 CEST) — and the one thing still blocking

### What happened

The owner ran §3's recovery: `xhci_hcd` unbind/bind plus PCI remove + rescan.
`uptime -s` is still `2026-10-02 06:51:48`, so **no reboot was needed**. The
controller came back and re-enumerated everything on bus 1:

```
22:54:03  usb 1-3:   New USB device found, idVendor=05e3, idProduct=0610   (onboard hub)
22:54:04  usb 1-3.1: New USB device found, idVendor=10ab, idProduct=9309   (internal Bluetooth)
22:54:41  usb 1-2:   New USB device found, idVendor=065a, idProduct=a001   ← the scanner
22:54:41  hid-generic 0003:065A:A001.0007: input,hidraw1: USB HID v1.11 Keyboard
              [Opticon Opticon USB Barcode Reader] on usb-0000:c3:00.3-2/input0
```

`readlink -f /sys/bus/usb/devices/1-2` →
`…/0000:c3:00.3/usb1/1-2`, i.e. **`usb1-port2` — an external USB-A jack on the
controller §1 named as dead.** The §2 port map predicted that location; the cure
confirmed it. `scripts/usb_host_check.py` now reports **0 errors**.

### What the device is

| Field | Value |
|---|---|
| VID:PID | `065a:a001` |
| `iProduct` | *Opticon USB Barcode Reader* |
| `bInterfaceClass` | `3` = Human Interface Device, 1 interface |
| Node | `/dev/hidraw1` |
| `/dev/ttyACM*` | **absent** — HID personality, not CDC-ACM |

So it is in **USB-HID keyboard mode**, not USB-COM. Consequences:

* `scripts/scanner_hid.py` is the right tool, and its defaults (`--vid 065a
  --pid a001`) already match. `--list` works **unprivileged** and prints
  `/dev/hidraw1: 065a:a001 Opticon Opticon USB Barcode Reader <-- target`.
* `find_scanner_ports()` returns `[]` **even with `include_unknown=True`** — it
  enumerates *serial* ports and this personality creates none. So
  `scanner_bridge.py` and `m10_scan_test.py`'s `Z1`/`Z2` command channel are
  structurally unavailable until the device is switched to `a002`.
* `opticon_detect.py --json` still reports `confirmed: False`. **Seeing `a001`
  does not prove this unit is an M-10** — it proves it is *an* Opticon HID
  keyboard-mode reader. `tests/test_opticon_detect.py::test_unconfirmed_personality_keeps_its_hedge`
  guards that hedge deliberately; do not upgrade it on this evidence. The step
  that settles it is unchanged: scan the *USB COM Port* sheet and watch for
  re-enumeration as `065a:a002`.

### The remaining blocker

```console
$ ls -la /dev/hidraw1
crw------- 1 root root 241, 1 Oct 10 22:54 /dev/hidraw1
$ python3 -c "open('/dev/hidraw1','rb')"
PermissionError: [Errno 13] Permission denied: '/dev/hidraw1'
```

Exactly the §5 #3/#4 prediction: stock Ubuntu tags no generic hidraw node with
`uaccess`, so the node stays `root:root 0600`. Fix (needs sudo):

```bash
sudo cp scripts/udev/99-opticon-scanner.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
# re-plug the scanner, then:
uv run python scripts/scanner_hid.py --count 3
```

If the unit is switched to USB-COM (`a002`) instead, also add ModemManager's
ignore rule in the same pass — MM is `active` and will probe the new `ttyACM0`
(§5 #2).

### Still not done

* **Prevention (§4).** `power/control` on `c3:00.3` is still **`auto`**, and
  `usb_host_check.py` now emits two INFO findings for the *same* controller —
  `Port resume timed out, port 1-3` and `xHCI host not responding to stop
  endpoint command`. The precursors that killed it on Oct 03 are still in the
  log. Expect a repeat on a future suspend.
* The three open items in §7 all still stand.
