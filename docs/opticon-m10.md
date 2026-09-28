# Opticon M-10 — connection & interaction guide

How to connect the **Opticon M-10** 2D presentation scanner to a computer and
integrate it with HCRM (this repository ships a driver in `app/scanner/` and a
ready-made bridge in `scripts/scanner_bridge.py`).

**Sources** (all retrieved from Opticon; see "References" at the bottom):

- Opticon USA wiki: *M-10* product page
- *M-10 Datasheet* (`m10datasheet.pdf`)
- *M-10 Specifications Manual* (`m10_specifications_manual.pdf`, SS13063)
- Opticon USA wiki: *MDI3100* page (the M-10's scan engine)
- *Universal Menu Book* (UMB) / *OptiConfigure* (configuration tools)

Where a claim could only come from the family-level "MDI3100 Serial Interface"
document (which Opticon distributes via tech support), it is flagged as
*family-documented* rather than verified.

---

## 1. What the M-10 is

A fixed/hands-free **2D CMOS imager presentation scanner** (Opticon MDI3100
engine, up to 60 fps, omnidirectional). It reads all common 1D barcodes
(UPC/EAN/JAN, Code 39, Code 128, GS1-128, …), 2D codes (QR, Data Matrix,
PDF417, Aztec, …) and postal codes, including codes on phone screens. It has
manual (top trigger switch) and auto-trigger presentation modes, a buzzer and
LEDs, and survives 1.5 m drops (IP52).

**It has no memory/batch mode and no Bluetooth** — scanned data goes to the
host immediately over the cable. (For wireless/batch use, that is a different
Opticon family — OPN/MTL — not covered here.)

## 2. Interfaces — choose how to connect

The M-10 is sold in **USB** and **RS-232C** hardware versions. The USB version
itself can be configured into two personalities:

| Interface | What it looks like to the PC | Bidirectional? | Use it for |
|---|---|---|---|
| **USB-HID** (keyboard) | A keyboard; scans are "typed" wherever the cursor is | ❌ host → scanner not possible | Zero-integration data entry (click a field, scan) |
| **USB-COM** (VCP, CDC-ACM) | A virtual serial port (`COMx` / `/dev/ttyACM0`) | ✅ full command channel | **HCRM integration (recommended)** |
| **RS-232C** (DB9, external PSU) | A real serial port | ✅ full command channel | Legacy/POS hardware, long cable runs |

Factory defaults (Specifications Manual §18): RS-232C line = **9600 bps, 8 data
bits, no parity, 1 stop bit, no handshaking**; data suffix = **CR**; read mode
= auto trigger; data buffering = buffered mode. USB-COM is CDC-ACM with
**Vendor ID 065A, Product ID A002**.

> To switch the USB personality (or restore defaults) you scan the appropriate
> configuration sheet: the wiki's *M-10* page links "USB COM Port" / "USB
> Keyboard (HID)" / "RS232" sheets, and §6.1 below gives the factory-default
> label. For anything more advanced use the **Universal Menu Book** or
> **OptiConfigure** (§6.2).

## 3. Physical setup

### USB-COM (recommended)

1. Configure the scanner for USB-COM if it isn't already (scan the *USB COM
   Port* sheet from the wiki, or ship a new unit straight to step 2 — many
   USB units default to HID, so **check this first** if nothing shows up).
2. Plug into a USB port that can supply 500 mA (bus powered, no adapter).
3. Driver:
   - **Windows**: install Opticon's *All-In-One PC Drivers* package (link on
     the wiki). The scanner appears as a `COMx` port in Device Manager. Note
     from Opticon: the driver may not install with **FIPS mode** enabled.
   - **Linux**: no driver needed — the kernel's `cdc_acm` module picks it up
     as `/dev/ttyACM0` (confirmed on Opticon's MDI3100 wiki page). Add your
     user to the `dialout` group (Debian/Ubuntu) or `uucp` (Arch) so you can
     open the port without root:
     `sudo usermod -aG dialout "$USER"` (log out/in afterwards).
   - **macOS**: appears as `/dev/tty.usbmodem*`.

### RS-232C

1. Connect the DB9 cable to a serial port (or a USB→RS-232 adapter) **and**
   the dedicated 6 V AC adapter — this model is *not* bus powered.
2. Match the line settings on the host: 9600 8N1, no flow control (defaults;
   300–115200 bps configurable via menu commands).

### USB-HID (no integration)

Just plug it in. Scans are typed into the focused window followed by CR —
which conveniently "presses Enter" in most search boxes, including the HCRM
catalogue search field. No driver, no code — but also no command channel.

## 4. Talking to the scanner: the wire protocol

Only USB-COM and RS-232C have a command channel. Everything in this section
is implemented in `app/scanner/protocol.py`.

### 4.1 Scanner → host: barcode frames

Each successful read is sent as the barcode's ASCII/UTF-8 data followed by
the configured **suffix** (factory default: **CR**, `\r`). Parse a stream by
splitting on CR/LF; `ScanParser` does this incrementally and tolerates CR,
LF and CRLF suffixes:

```python
from app.scanner import ScanParser
parser = ScanParser()
parser.feed(b"5901234123457\r")   # -> ["5901234123457"]
```

### 4.2 Host → scanner: commands

Two command families exist (*family-documented* via the MDI3x00 serial
interface; the menu-command *format* is verified against the published
factory-default label):

1. **Menu commands** — the Universal Menu Book's configuration codes,
   wrapped in an envelope: `@MENU_OPTO@<CMD>@<CMD>…@OTPO_UNEM@`. The same
   string can be printed as a Code 128 label *or* sent over the serial
   link. Example, the published factory-default label:

   ```
   @MENU_OPTO@ZZ@BAP@ZZ@OTPO_UNEM@
   ```

   Build these with `build_menu_command("ZZ", "BAP", "ZZ")` — the test
   suite asserts this reproduces the published string exactly.

2. **Dedicated commands** — short operational commands, notably:

   | Command | Meaning |
   |---|---|
   | `Z1` | **Read enable** — software trigger, start one read cycle |
   | `Z2` | **Read disable** — abort reading / trigger off |

3. **Responses** — the scanner answers commands with a single byte:

   | Byte | Name | Meaning |
   |---|---|---|
   | `0x06` | ACK | command accepted |
   | `0x15` | NAK | command rejected (unknown/invalid) |
   | `0x1B` | ESC | recognised but not executable in the current state |

   In buffered mode scanned data can interleave with responses; the driver
   skips non-response bytes while awaiting ACK.

## 5. Using the HCRM driver (`app/scanner/`)

Stdlib-only on Linux/macOS (termios); on Windows install pyserial
(`uv pip install pyserial`). pyserial is intentionally **not** a project
dependency — the app installs offline from a frozen `uv.lock`.

```python
from app.scanner import OpticonM10

# 1) open a known port — or auto-discover the USB-COM model (VID 065A/PID A002)
scanner = OpticonM10("/dev/ttyACM0").open()      # or OpticonM10.discover()

# 2) stream scans to a callback
scanner.start_reading(lambda code: print("scanned:", code))

# 3) command channel
scanner.trigger_on()                 # Z1 — software trigger
scanner.restore_factory_defaults()   # published default menu label
scanner.send_menu_commands("ZZ", "BAP", "ZZ")
scanner.close()
```

Or as a context manager:

```python
with OpticonM10.discover() as scanner:
    scanner.start_reading(print)
    ...
```

API summary:

| Member | Purpose |
|---|---|
| `OpticonM10(port, baudrate=9600, …)` | RS-232C line settings; ignored by USB-COM |
| `OpticonM10.discover()` | first port from `find_scanner_ports()` |
| `find_scanner_ports()` | candidate ports, Opticon VID/PID matches first |
| `start_reading(on_scan, on_error=None)` | daemon thread, callback per barcode |
| `stop_reading()` / `close()` | lifecycle |
| `send_command(cmd, timeout=2.0)` | → `True` on ACK; raises `CommandRejected` (NAK/ESC) or `TimeoutError` |
| `send_menu_commands(*codes)` | send UMB codes in the `@MENU_OPTO@…` envelope |
| `restore_factory_defaults()` | apply the published default label |
| `trigger_on()` / `trigger_off()` | `Z1` / `Z2` |

## 6. Using the bridge (`scripts/scanner_bridge.py`)

Turns scans into catalogue actions on a running HCRM server. **Convention:
put the product's barcode (EAN/UPC) in the item's `sku` field** — the bridge
tries an exact SKU match first, then falls back to the first search hit.

```bash
./scripts/run.sh &                       # 1. start HCRM

uv run python scripts/scanner_bridge.py \  # 2. listen & look up
    --email staff@shop.local --password 'secret'

uv run python scripts/scanner_bridge.py --email staff@shop.local \
    --stock-in 1                          # goods receiving: +1 stock per scan

uv run python scripts/scanner_bridge.py --email staff@shop.local \
    --stock-out 1 --json                  # sales counter: -1, JSONL output
```

Flags: `--port` (default: auto-detect), `--baudrate` (RS-232C only),
`--stock-in N` / `--stock-out N` (mutually exclusive; staff/admin only),
`--json`. Env: `HCRM_BASE`, `HCRM_SCANNER_PORT`.

Stock adjustment is read-modify-write (`GET` then `PATCH /api/items/{id}`),
so it is fine at counter speed but not atomic — two bridges scanning the
same item concurrently can lose an increment. If that becomes a real
deployment mode, add a dedicated `POST /api/items/{id}/stock-adjust`
endpoint with SQL-side arithmetic.

## 7. Configuring the scanner itself

1. **Restore defaults** first when in doubt: scan the factory-default label
   (§4.2, also printed in the Specifications Manual §18.1) or call
   `restore_factory_defaults()`.
2. **Universal Menu Book (UMB)** — PDF of Code 128 menu labels: interface
   selection, symbology enable/disable, prefixes/suffixes (e.g. change CR
   to CRLF), read options (single/multiple read, trigger disable = always
   on), buzzer/LED settings. Print the page, scan `-` (menu on/off) plus
   the option labels.
3. **OptiConfigure** (online) — searchable UMB; can generate custom
   configuration sheets (PDF) or export `.ocg` files. Updated more often
   than the UMB PDF.
4. **Universal Config Tool 2D** — Windows application for 2D imager
   products; configure over USB-COM.

Recommended for HCRM: USB-COM interface, suffix **CR** (default), auto
trigger (default), all common 1D symbologies enabled (default covers
UPC/EAN/Code 39/Code 128).

## 8. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Nothing appears anywhere when scanning | Scanner is in USB-COM mode and nothing is reading the port (or auto-trigger sleep — move it/present a code). In HID mode scans go to the *focused window*. |
| No `/dev/ttyACM0` on Linux | Scanner is still in USB-HID mode → scan the *USB COM Port* sheet. Check `dmesg \| grep cdc_acm`. |
| `Permission denied` opening the port | Add user to `dialout`/`uucp` (§3), or run the bridge with sudo (not recommended). |
| Garbage characters / no frames | RS-232C line settings mismatch → 9600 8N1 no handshake, or `restore_factory_defaults()`. |
| Scans arrive but "no catalogue match" | Item's `sku` doesn't hold the scanned barcode → set SKU to the EAN/UPC (§6). |
| Commands time out | Scanner is in USB-HID mode (no command channel), or the host opened the wrong port. |
| Driver won't install on Windows | FIPS mode enabled blocks Opticon's USB driver (Opticon's own note on the wiki). |
| `Z1`/`Z2` rejected (ESC) | Command not executable in current state — e.g. trigger disabled config; restore defaults and retry. |

## 9. References

- M-10 product page — <https://wiki.opticonusa.com/techsupport/en/M-10>
- M-10 datasheet — <https://files.opticonusa.com/Downloads/m10datasheet.pdf>
- M-10 Specifications Manual — <https://files.opticonusa.com/Downloads/m10_specifications_manual.pdf>
- Universal Menu Book — <https://files.opticonusa.com/UniversalMenuBook/UniversalMenuBook.pdf>
- MDI3100 engine page (Linux/ttyACM note, serial command doc pointer) — <https://wiki.opticonusa.com/techsupport/en/MDI3100>
- OptiConfigure — <https://configure.opticon.com/>
- All-In-One PC Drivers — <https://files.opticonusa.com/Downloads/USB%20Drivers%20Installer.zip>
