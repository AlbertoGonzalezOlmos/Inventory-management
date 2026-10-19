# Opticon scanners — personalities, connection, permissions

The repository talks to Opticon barcode scanners on two dev machines (a
native Ubuntu ThinkPad — the primary host — and a WSL2 laptop; §2 keeps
their facts separate). One vendor ID, several
**personalities**: the same physical family can present itself as a vendor
serial device, as a CDC-ACM virtual COM port, or as a USB-HID keyboard, and
each presentation needs a different transport, a different reception tool and
different host plumbing. Those facts are not specific to any one model, so
they live here — **once** — and the per-model guides link to this page:

| Guide | Scope |
|---|---|
| this document | which device is attached, how to get it to the OS, how to receive scans from the HID personality, device-node permissions |
| `docs/scanner-opn2001.md` | the OPN-2001 (`065A:0009`): hardware operation, the "RBBV" serial protocol, the raw-libusb backend for kernels without the `opticon` module, `scripts/opn2001.py` |
| `docs/opticon-m10.md` | the M-10 (`065A:A002` in USB-COM mode): the bidirectional command channel, `app/scanner/`, `scripts/scanner_bridge.py`, QR-badge login |

> **Why this page exists.** This material was originally written inside the
> OPN-2001 guide, because that is where the hardware survey happened to be
> done — which put the *other* scanner's connection story under a model name
> it does not belong to, and got it documented a second time in the M-10
> guide. Both copies drifted. A hardware fact gets one canonical home.

---

## 1. Which device is plugged in?

```bash
uv run python scripts/opticon_detect.py            # human-readable survey
uv run python scripts/opticon_detect.py --json     # machine-readable
```

`scripts/opticon_detect.py` is stdlib-only and importable
(`from scripts.opticon_detect import describe, find_linux_usb_devices`), so
every tool in this repo answers "what is attached?" from the **same** map
instead of keeping a private one.

### Personality table

| VID:PID | Personality | Transport | Receive with | Identity status |
|---|---|---|---|---|
| `065A:0009` | **OPN-2001** pocket memory scanner | vendor serial, Opticon "RBBV" stream, **9600 8O1** | `scripts/opn2001.py read` | **documented** (OPN-2001 manual; Linux `drivers/usb/serial/opticon.c` binds this VID:PID) |
| `065A:A001` | USB-HID **keyboard-mode** scanner, bus-reported name *"Opticon USB Barcode Reader"* | USB HID boot keyboard — scans are typed, CR-suffixed | `scripts/scanner_hid.py` | **observed live, unconfirmed** — see the note below |
| `065A:A002` | **M-10 family** in USB-COM mode | CDC-ACM virtual serial port, bidirectional | `scripts/scanner_bridge.py` (M-10 command channel) | **documented** (M-10 Specifications Manual SS13063 §18.5: "USB-COM = CDC-ACM, VID 065A PID A002") |
| `065A:0001` | OPR-2001 / NLV-1001 in keyboard mode | HID | *no tool in this repo* | documented by Opticon for other models |

> **About `065A:A001` — read this before repeating the claim.**
> `A001` appears in **no public Opticon document** we could find: SS13063
> §18.5 lists only `A002` (for USB-COM), and §18.4 (USB-HID) lists no PID at
> all. What we know is that a device on this project's dev machine enumerates
> as `HID\VID_065A&PID_A001` with the bus-reported name *"Opticon USB
> Barcode Reader"* and works as a keyboard-mode scanner. That it is the
> **M-10's** HID personality is the leading hypothesis — the M-10 ships from
> the factory in USB-HID mode and is the presentation scanner this shop owns
> — but it is a hypothesis. `scripts/opticon_detect.py` carries it as
> `confirmed: False` and `tests/test_opticon_detect.py`
> (`test_unconfirmed_personality_keeps_its_hedge`) fails if the label is
> upgraded without the evidence.
>
> **Proving it is one hardware step:** scan the device's *USB COM Port*
> configuration sheet. If it re-enumerates as `065A:A002`, it is an M-10 (and
> `docs/opticon-m10.md` then applies to it directly). A second, cheaper
> discriminator: the OPN-2001 is a **1D laser** scanner (MDL-2100) and cannot
> read a QR code at all, so a device that reads the QR login badge has a 2D
> imager and is not an OPN-2001. Record the dated result here and flip
> `confirmed` in the same commit.

### What the survey tells you

`opticon_detect.py` reports, per attached device: the PID, the personality
name, the kernel node bound to it (`/dev/ttyUSB*`, `/dev/ttyACM*`,
`/dev/hidraw*`, `COMx`) or the fact that none is, whether the identity is
documented or merely observed, and which tool receives from it. An
**unrecognised** Opticon PID is reported as unknown rather than guessed at —
guessing is how a tool ends up opening the wrong device at the wrong line
settings and interpreting binary protocol bytes as "barcodes".

---

## 2. Getting the device to the OS

Two dev machines, two different stories — a claim below is only true for
the machine it names:

* the **primary host** is a native Ubuntu ThinkPad (2026-10-10 survey:
  `grep -qi microsoft /proc/version` fails — not WSL; no usbipd layer, the
  kernel builds every Opticon module, and `opticon_detect.py`'s "usbipd:
  nothing attached" path is unreachable);
* the **second machine** is a WSL2 laptop (Windows 11 + usbipd), surveyed
  2026-09-29 — its three-layer material is scoped below, unchanged.

### Linux (native) — the primary host

* Serial personalities bind a kernel driver: `065A:0009` → the in-kernel
  `opticon` usb-serial driver → `/dev/ttyUSB*`; `065A:A002` → `cdc_acm` →
  `/dev/ttyACM*` (all present on disk here: `CONFIG_HIDRAW=y`,
  `CONFIG_USB_ACM=m`, `CONFIG_USB_SERIAL_OPTICON=m`). If the device
  enumerates but no node appears, the module is missing or unloaded
  (`sudo modprobe opticon`; check `dmesg`).
* The HID personality needs `CONFIG_HIDRAW` (set in stock distro kernels) →
  `/dev/hidraw*`.
* Nodes are root-owned by default → §4 — and on this host that is NOT
  rescued by the seat: see §4 for the corrected mechanism.

**The native-host failure mode the WSL2 framing cannot even express: a
dead USB host controller.** When an xHCI controller dies (journal markers:
`xHC is not running`, `Port resume timed out`, `xHCI host not responding,
assume dead`, `HC died; cleaning up` — typically after a suspend/resume),
the kernel keeps the controller's *root hubs* registered, so `lsusb` and
`opticon_detect.py` still list the bus and report "nothing attached" —
while every physical port on that controller is dead: **re-plugging
produces no kernel event at all**, which looks exactly like a charge-only
cable or a dead scanner. Measured on this host, 2026-10-10: controller
`0000:c3:00.3` (the two external USB-A jacks) died on a runtime-PM resume;
the USB-C jacks (`c5:00.3` / `c5:00.4`) stayed alive — the port→controller
map (`readlink /sys/bus/usb/devices/usbN`) is how you tell which physical
jacks a dead BDF means.

```bash
uv run python scripts/usb_host_check.py    # the detector: dead xHC, MM/brltty, udev, modules
```

Its recovery ladder (unbind/bind → PCI remove + rescan → the *caveated*
reset — `reset_method` here is `pm`, no FLR, and a PM reset rarely revives
a declared-dead controller → reboot) and the runtime-PM prevention
(`power/control=on` — still `auto` on this host, so expect a repeat on a
future suspend; `usb_host_check.py` now nags about it: an INFO finding
when a healthy controller still reads `auto`) are emitted by the tool itself; the command-for-command
evidence and the resolution are
`docs/m10-unreachable-2026-10-10.md` §1–§4 (the controller was revived
**without a reboot** by PCI remove + rescan).

> **`dmesg -T` is skewed ~+7 days on this host** (suspend clock drift) —
> diagnose from `dmesg` (monotonic timestamps) or the journal, never
> `dmesg -T`. `usb_host_check.py` uses plain `dmesg` for exactly this
> reason (regression-pinned).

### Windows

Install the vendor's USB driver **before** plugging in, then use the `COMx`
port. A device that shows up in Device Manager as *"Unknown USB Device
(Device Descriptor Request Failed)"* (problem 43) never enumerated at all —
see §5.

### macOS

No vendor driver exists for the OPN-2001's vendor-specific interface. The HID
personality does work through `hidapi`:
`uv run --with hid python scripts/scanner_hid.py`. For the serial
personalities, use a Linux host/VM with USB passthrough.

### WSL2 (the second dev machine) — three layers, and who handles each

**The project's other dev machine is WSL2 (Ubuntu on Windows 11)**, so all
three layers apply *there*. Verified facts, 2026-09-29:

1. **USB belongs to the Windows host.** WSL2 only sees a device after a
   `usbipd-win` passthrough. Installed here with
   `winget install --id dorssel.usbipd-win` (one UAC approval), then:
   ```powershell
   usbipd list                          # find the BUSID of the 065a:xxxx device
   usbipd bind --busid <id>             # admin PowerShell, once per machine
   usbipd attach --wsl --busid <id>     # after EVERY replug / reboot
   ```
   `bind` survives a reboot; **`attach` does not** — re-run it.
   `opticon_detect.py` reports `usbipd: nothing attached` when this layer is
   the problem.
2. **The guest kernel must have a driver for that personality.** The stock
   WSL2 kernel has `CONFIG_USB_SERIAL_OPTICON is not set` (its module
   directory ships ch341/cp210x/ftdi/pl2303/… only), so an attached
   `065A:0009` enumerates but gets **no `/dev/ttyUSB*`**; the generic
   usb-serial driver cannot substitute, because that device has no bulk-out
   endpoint — the real driver sends host→device data as *vendor control
   requests*. `CONFIG_HIDRAW=y` **is** set, so the HID personality works
   (`/dev/hidraw0` appears as soon as the device is attached). Per-model
   workarounds live in the model's guide: `docs/scanner-opn2001.md` documents
   a raw-libusb backend (`--backend usb`, pyusb) that re-implements
   `opticon.c` in userspace, and a custom-kernel option.
3. **Or skip WSL entirely**: install the driver on Windows (§2 Windows) and
   run the same tools there against `COMx`.

**Device state log (the WSL2 dev machine, 2026-09-29).** A USB device on root-hub
port **HS05** fails enumeration — Windows problem 43, *"Unknown USB Device
(Device Descriptor Request Failed)"*, reported as `VID_0000&PID_0002` because
its descriptor was never read. For an OPN-2001 that is the documented
deeply-discharged-battery / charge-only-cable signature: leave it on a
known-good **data** cable ≥ 30 min (steady **red** LED = charging) and
re-plug. A second Opticon device on **HS09** enumerates fine as
`065A:A001` (USB-HID keyboard mode), was bound and attached with
`usbipd-win`, and appears inside WSL as `/dev/hidraw0` — which is what
proves the cable, the ports and the tooling path, and isolates HS05's fault
to the device/power side.

---

## 3. Receiving scans from the USB-HID personality

Presentation/corded Opticon scanners ship from the factory in **USB-HID**
mode: the OS sees a keyboard and each scan is "typed" into the focused
window, terminated by Enter. That already works against the SPA with zero
integration — click the catalogue search box and scan.

To capture scans **without window focus** (a headless counter, a kiosk, or
feeding a pipeline), use `scripts/scanner_hid.py`, which reads the raw HID
boot-keyboard reports:

```bash
# Linux/WSL2, stdlib only (attach with usbipd first — §2):
uv run python scripts/scanner_hid.py --count 10

# Windows/macOS, via hidapi (claims the device from the OS keyboard driver
# while it runs — that is the point: the tool owns the stream):
uv run --with hid python scripts/scanner_hid.py
```

* Output is one CSV line per scan, `timestamp,symbology,barcode`, no header —
  the shape this repo's scan pipelines consume; `--json` emits
  `{device_id, count, scans[]}`.
* `--vid`/`--pid` override the default `065a:a001`; `--node` pins a specific
  `/dev/hidraw*`.
* Report parsing (8-byte boot-keyboard reports, HID usage page 0x07, shift
  handling, rollover tolerance) is stdlib and offline-tested in
  `tests/test_scanner_hid.py`.

The **bidirectional** channel (host → scanner: suffix/menu configuration,
command/ACK framing) does not exist in HID mode. To get it, scan the device's
*USB COM Port* configuration sheet: it re-enumerates as `065A:A002` (CDC-ACM)
— and, per §1, that scan is also the experiment that confirms what the `A001`
device actually is.

---

## 4. Device-node permissions (one-time, needs sudo)

`/dev/ttyUSB*`, `/dev/ttyACM*`, `/dev/hidraw*` and `/dev/bus/usb/*` are
root-owned. The conclusion is the same on both machines — install the
vendor-wide rule — but the *mechanism* differs (corrected 2026-10-10, the
m10 diagnosis §6; an earlier revision wrongly claimed the missing-udev
warning was a false positive on the native host):

* **WSL2**: sessions have **no seat**, so `logind`'s `uaccess` ACL never
  fires, and `usermod -aG dialout` does not cover `hidraw` or `usbfs`.
* **Native Ubuntu**: `seat0` **does** exist with an active session — but
  it still does not help, because stock Ubuntu tags **no generic `hidraw`
  or `usbfs` node with `uaccess`** (the only stock hidraw uaccess line,
  `70-uaccess.rules:89`, is gated on `ID_AV_PRODUCTION_CONTROLLER="1"`;
  every other is vendor-pinned gamepad/VR/FIDO). `/dev/ttyACM0` *is*
  covered separately (`root:dialout 0660`, and the user is in `dialout`);
  `/dev/hidraw*` and `/dev/bus/usb/*` are not. Note `dialout` covers
  `ttyACM`, never `hidraw` — "add yourself to dialout" cannot fix a
  hidraw EACCES.

Hence one vendor-wide rule file:

```bash
sudo cp scripts/udev/99-opticon-scanner.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
# then re-plug, or on WSL2: usbipd detach --busid <id> && usbipd attach --wsl --busid <id>
```

It grants `plugdev` access to the tty/hidraw/usb/input nodes for VID `065a`
and carries the two ModemManager-ignore lines (§2's grabber check) — one
file, one install step, every personality. `GROUP=plugdev` matches stock
Ubuntu/Debian WSL images (the default user is already a member; verify with
`id`); use `dialout` or `users` elsewhere.

---

## 5. Troubleshooting

| Symptom | Cause → fix |
|---|---|
| **Linux: plugged in, nothing at all — no kernel event, no node, the survey says "nothing attached"** | A **dead xHCI host controller** looks exactly like a charge-only cable (§2 native): the root hubs stay registered so the bus still lists. Run `scripts/usb_host_check.py` — it names the dead BDF, the buses, and the recovery ladder. Full evidence: `docs/m10-unreachable-2026-10-10.md`. |
| Linux: ModemManager holds/AT-probes the new `/dev/ttyACM*` | Install the repo udev rule (§4 — it carries both MM-ignore forms) or `systemctl stop ModemManager` while scanning. |
| Linux: timestamps look days off in `dmesg -T` | `dmesg -T` is suspend-skewed on this host (~+7 days, 2026-10-10) — diagnose from plain `dmesg` or the journal. |
| Windows: **"Unknown USB Device (Device Descriptor Request Failed)"** (problem 43) | The device never enumerated — it is *not talking to the host at all*. Almost always power/cable: deeply-discharged battery (leave on the cable ≥ 30 min; steady red LED = charging) or a **charge-only cable** (swap for a known-good data cable). Re-plug; Windows re-enumerates automatically. |
| WSL2: `usbipd attach` says no device / lost after reboot | `attach` is not persistent — re-run it. `usbipd list` shows STATE=Shared/Attached. |
| WSL2: device attached but no `/dev/ttyUSB*` | The guest kernel lacks the driver for that personality (§2 layer 2). The HID personality is unaffected. |
| Device enumerates but the survey reports no node | Missing kernel module, or a permission problem on the node — §4. |
| `/dev/hidraw0` permission denied | Root-only node: on WSL no seat fires, on native no generic hidraw `uaccess` exists (§4) — either way, install the udev rule (§4) or run with sudo once. |
| `Permission denied: '/dev/ttyUSB0'` | Your user is not in `dialout` (group membership or the udev rule, §4), then re-login. |
| HID scans go to the wrong window | That is keyboard-wedge behaviour by design; use `scripts/scanner_hid.py`, which owns the stream (§3). |
| Wrong device opened / garbage "barcodes" | Two Opticon personalities can be attached at once and their line settings differ (9600 **8O1** for `0009`, 9600 **8N1** for `A002`). Always select by VID:PID via `scripts/opticon_detect.py`, never by "first serial port". |
| Windows: no COM port | Install the vendor USB driver *before* connecting; check Device Manager. |

---

## 6. Provenance

| Claim | Source |
|---|---|
| OPN-2001 = `065A:0009`, vendor serial, 9600 8O1, RBBV stream | Opticon OPN-2001 manual; Linux `drivers/usb/serial/opticon.c` (kernel.org) |
| M-10 USB-COM = CDC-ACM, VID `065A` PID `A002` | Opticon M-10 Specifications Manual SS13063 §18.5 (downloaded PDF) |
| M-10 factory default = USB-HID mode; personality switched by configuration sheet | Opticon wiki, *M-10* page ("USB COM Port" / "USB Keyboard (HID)" / "RS232" sheets) |
| `065A:0001` = OPR-2001 / NLV-1001 keyboard mode | Opticon documentation for those models |
| `065A:A001` = the **WSL2 dev machine's** HID-mode device | **Observed**, 2026-09-29, WSL2 + `usbipd-win` → `/dev/hidraw0`. Not in any public Opticon document we could find — see §1 |
| `065A:A001` also observed on the native ThinkPad | **Observed**, 2026-10-10, `/dev/hidraw1`, `iProduct="Opticon USB Barcode Reader"`, HID personality — **still unconfirmed as an M-10** (the §1 hedge is unchanged; `opticon_detect.py` reports `confirmed: False`) |
| Native ThinkPad: xHCI `0000:c3:00.3` died on a runtime-PM resume, revived by unbind/bind + PCI remove + rescan **without reboot**; `reset_method=pm` (no FLR); `power/control` still `auto` | `docs/m10-unreachable-2026-10-10.md` §1–§4/§8 (the dated diagnosis; the journal markers are quoted there) |
| Stock Ubuntu (24.04) tags no generic hidraw/usbfs node with `uaccess`; `70-uaccess.rules:89` is gated on `ID_AV_PRODUCTION_CONTROLLER` | Read from `/usr/lib/udev/rules.d/` on the native host, 2026-10-10 (m10 diagnosis §5 #3–#4) |
| `dmesg -T` suspend-skewed ~+7 days on the native host | Measured 2026-10-10; `usb_host_check.py`'s plain-`dmesg` requirement is regression-pinned (`test_gather_dmesg_must_use_plain_dmesg`) |
| WSL2 kernel lacks `CONFIG_USB_SERIAL_OPTICON`; `CONFIG_HIDRAW=y` | `/proc/config.gz` and the module directory **on the WSL2 dev machine**, 2026-09-29 (the native host builds every Opticon module — `=m`, on disk) |
| usbipd `bind` persists, `attach` does not | usbipd-win documentation (learn.microsoft.com) + observed here |
| `files.opticonusa.com` / `wiki.opticonusa.com` serve a broken TLS chain | observed; use `curl -k` — the content itself is genuine |
