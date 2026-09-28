# Connecting and working with the Opticon OPN-2001 scanner

This document is the engineering guide for the **Opticon OPN-2001 "Pocket
Memory Scanner"**: what the device is, how to connect it to a computer, the
serial protocol it speaks, and how to use it from this repository
(`scripts/opn2001.py`), including the intended HCRM stock-counting workflow.

Everything protocol-related here was cross-verified against **three
independent open-source implementations** plus the **in-tree Linux kernel
driver** (see [Provenance](#provenance-and-references) at the end); the raw
frames quoted below are pinned as known-answer vectors in
`tests/test_opn2001.py`, so a regression in our own implementation fails the
test suite even without hardware attached.

---

## 1. What the OPN-2001 is

A tiny (key-fob sized) **batch barcode scanner**: it has no screen and no
keyboard — you press one button to scan, and every scan is **stored in the
device's memory** (with an optional timestamp). Later you plug it into a
computer over USB and *download* the collected scans. That makes it ideal for
stock counting: walk the shop, scan item after item, then reconcile the whole
batch against the catalogue in one go.

| Property | Value |
|---|---|
| Type | 1D laser pocket memory scanner (Class 1 laser — safe, but don't stare into the beam) |
| Scan engine | Opticon MDL-2100 |
| Buttons | **Scan** (large) and **Clear** (small) |
| Connectivity | USB (mini-B socket) — data **and** charging |
| USB identity | VID `065A` (Opticon), PID `0009` |
| Interface class | USB **V**irtual **C**OM **P**ort (vendor-specific protocol, not keyboard-wedge) |
| Firmware stream | Opticon "RBBV" |
| Battery | Internal Li-ion, charged over USB (~2.5 h for a full charge) |

The sibling PID `065A:0001` belongs to the **OPR-2001 / NLV-1001 in keyboard
mode** — a different beast (types scans as keystrokes). The OPN-2001 never
emulates a keyboard; it only talks the serial protocol described in §4.

---

## 2. Operating the hardware

*From the official Opticon user's manual (v01-2006).*

### 2.1 Scanning (adding to memory)

1. Point the scanner at the barcode and **press and hold the Scan button**.
   The laser is visible and the green LED flashes.
2. Make sure the beam covers the whole barcode plus some white margin; hold
   the scanner at a slight angle. If the code doesn't decode immediately,
   move the scanner closer.
3. A successful read is signalled by a **solid green LED** (and a beep, if
   the buzzer is on) — the code is now in memory.

If the laser switches off and the LED flashes **orange**, the read timed out:
release the button and try again. A **solid red LED** means the barcode was
read but **memory is full** — nothing was stored; download and clear the
device (§5).

### 2.2 Removing a scan (Clear button)

Point at the barcode and **press and hold the small Clear button**: the
device searches its memory and deletes the **first** matching entry (duplicates
are kept). If the barcode is not in memory you get a low error beep.

### 2.3 Special functions (long presses)

| Action | Effect |
|---|---|
| Hold **Clear** ≥ 6 s | **Erase all scans** (aim away from any barcode!) |
| Hold **Scan** ≥ 10 s | **Toggle the buzzer** on/off |
| Hold **Scan + Clear** ≥ 10 s | **Factory reset**: all configuration back to defaults **and all data erased** |

### 2.4 LED and buzzer reference

| State | Meaning |
|---|---|
| Green, flashing | Laser on (Scan held) — reading |
| Green, solid | Barcode read and stored |
| Red, solid | Read OK but **memory full** — not stored |
| Orange, flashing | Read timeout — release and retry |
| Low error beep (Clear) | Barcode not found in memory |
| Red, continuous (USB) | Battery charging |
| Green, continuous (USB) | Battery fully charged |
| Red/Green flashing (USB) | Battery may be defective — contact Opticon service |

### 2.5 Care and feeding

Operating range: avoid freezing areas, > 40 °C, prolonged direct sunlight and
water. No user-serviceable parts inside. Charge by plugging into a powered
USB port; charging is controlled by the scanner itself and stops automatically
when full. If the battery is completely empty it can take a while before the
computer even notices the device is attached.

---

## 3. Connecting to a computer

The OPN-2001 presents itself as a **virtual COM port** over USB. The serial
line settings (needed by whichever program opens the port) are:

> **9600 baud · 8 data bits · odd parity · 1 stop bit · no flow control**

### 3.1 Linux — works out of the box

The kernel has shipped a dedicated driver since 2008:
`drivers/usb/serial/opticon.c` (maintained upstream with contributions from
Opticon themselves). Its device table contains exactly one entry — the
OPN-2001:

```c
static const struct usb_device_id id_table[] = {
        { USB_DEVICE(0x065a, 0x0009) },
        { },
};
```

Plug the scanner in and it binds automatically, creating `/dev/ttyUSB0`:

```console
$ lsusb | grep -i 065a
Bus 001 Device 004: ID 065a:0009 Optoelectronics Co., Ltd. OPN-2001
$ dmesg | tail -2
usb 1-3: new full-speed USB device number 4 using xhci_hcd
opticon 1-3:1.0: device disconnected  # (unplug; on plug you'll see ttyUSB0 attach)
```

**Permissions**: the port is owned by root:`dialout`. Add yourself once:

```bash
sudo usermod -aG dialout "$USER"   # then log out and back in
```

or install a udev rule and stay out of groups entirely:

```bash
# /etc/udev/rules.d/99-opticon-opn2001.rules
SUBSYSTEM=="tty", ATTRS{idVendor}=="065a", ATTRS{idProduct}=="0009", \
    MODE="0660", GROUP="dialout", TAG+="uaccess"
```

```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Kernel-driver details worth knowing (they explain occasional quirks):

* The device has **no bulk-out endpoint** — host→scanner writes are sent as
  *vendor control requests*; the driver hides this behind a normal tty.
* Device→host packets arrive on a bulk-in endpoint with a **2-byte header**:
  `00 00` = data, `00 01` = a CTS line-state change. The driver strips it.
* The driver manages an RTS/CTS handshake over those same control messages
  and requests the current CTS state when the port opens. Userspace never
  needs to care.

### 3.2 Windows

Install the **Opticon USB driver** first (download from the Opticon support
section; the user's manual §4 walks through it), then plug in the scanner.
It appears as a **COM port** (`Device Manager → Ports (COM & LPT)`). Find the
`COMx` number and pass it to the tooling:

```powershell
uv run --with pyserial python scripts/opn2001.py --port COM4 info
```

(If the battery was completely empty, give it a few minutes on the cable
before expecting Windows to detect it.)

### 3.3 macOS

There is no vendor driver and no kernel extension for the OPN-2001's
vendor-specific interface on macOS — the device will show up in *System
Information* but not as a serial port. Practical options:

* run the download step on a Linux host or a Linux VM with USB passthrough, or
* use the Windows driver in a Windows VM.

(On other Opticon models that enumerate as standard CDC-ACM, macOS works out
of the box — the OPN-2001's 2006-era vendor interface is the problem, not the
protocol.)

---

## 4. The serial protocol ("RBBV" stream)

Once the port is open, the scanner exchanges small **binary frames**. The
host always speaks first (the first command also wakes the communication
link — expect the first response to take a moment).

### 4.1 Frame format

```
byte      opcode
byte      0x02                (constant in every frame seen in the wild)
byte      (flags << 5) | len  (len = payload length, max 31; flags observed 0)
byte[len] payload             (omitted when len == 0)
byte      0x00                (pad, only present when len > 0)
uint16be  CRC-16              (over everything above)
```

### 4.2 CRC-16

Reflected polynomial **0xA001**, init **0xFFFF**, final XOR **0xFFFF**, result
sent **big-endian** (high byte first). Reference implementation (the one in
`scripts/opn2001.py`, verified against all known-good frames):

```python
def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc = _TABLE[byte ^ (crc & 0xFF)] ^ (crc >> 8)   # LSB-first table walk
    return (~crc) & 0xFFFF
```

### 4.3 Command reference

All example bytes below are exact wire frames (CRC included), pinned in
`tests/test_opn2001.py`.

| Purpose | Opcode | Example frame (hex) | Reply |
|---|---|---|---|
| Interrogate / **wake** | `01` | `01 02 00 9F DE` | 23 B: header, pad, **device id** (8 B), **firmware** (8 B ASCII), pad, CRC |
| Clear all scans | `02` | `02 02 00 9F 2E` | 5 B ack |
| Set parameter | `03` | `03 02 02 <param> <value> 00 <crc>` | echoes param + value |
| Power down | `05` | `05 02 00 5E 9F` | 5 B ack (or it just switches off) |
| **Get data** (download) | `07` | `07 02 00 9E 3E` | see §4.4 |
| Get parameter | `08` | `08 02 01 <param> 00 <crc>` — e.g. param `0x02`: `08 02 01 02 00 26 6B` | echoes param + value |
| **Set time** | `09` | `09 02 06 <sec> <min> <hour> <day> <month> <year−2000> 00 <crc>` | 12 B, echoes the new time |
| Get time | `0A` | `0A 02 00 5D AF` | 12 B: header, len `06`, sec/min/hour/day/month/year, pad, CRC |

Worked examples:

* Set the clock to **2000-03-02 07:01:00**:
  `09 02 06 00 01 07 02 03 00 00 34 83` (payload `00 01 07 02 03 00`, pad,
  CRC `34 83`).
* Set the clock to **2025-06-15 14:30:05**:
  `09 02 06 05 1E 0E 0F 06 19 00 67 11`.
* Read back the time (`0A 02 00 5D AF`) → e.g.
  `06 02 06 05 1E 0E 0F 06 19 00 57 21` = 2025-06-15 14:30:05.

Reply frames use the same shape as requests (opcode, `02`, len, payload, pad,
CRC). The reply *opcode* observed in captures is `06`; robust parsers match on
structure and CRC rather than assuming it.

### 4.4 The "get data" reply (downloading scans)

The reply to opcode `07` is the one frame that **breaks the length-byte
convention** (a batch can be far larger than 31 bytes). It is a stream:

```
opcode          1 byte
0x02            1 byte
device id       8 bytes   (big-endian uint64, same id as interrogate)
record...       repeated (see below), until a record whose length byte is 0x00
CRC-16          2 bytes   (the 0x00 terminator doubles as the pad)
```

Each record:

```
length          1 byte    N (0 = end of batch; otherwise N ≥ 5)
symbology id    1 byte    (table in §4.5)
barcode         N−5 bytes (ASCII)
timestamp       4 bytes   big-endian packed bitfield (§4.6)
```

Notes:

* Reads can arrive split across USB packets — reassemble by exact byte counts,
  never by "whatever `read()` returned".
* Scans come back in the order they were scanned (oldest first).
* **Getting the data does not delete it.** Clearing is a separate command
  (opcode `02`) or the 6-second Clear button.
* The reference implementations disagree on whether the trailing CRC covers
  the whole stream including the device id; `scripts/opn2001.py` verifies it
  when present and **warns instead of failing** on a mismatch (records are
  kept).

### 4.5 Symbology IDs

| ID | Symbology | ID | Symbology | ID | Symbology |
|---|---|---|---|---|---|
| `01` | Code 39 | `0F` | EAN-128 (GS1-128) | `26` | Postbar (Canada) |
| `02` | Codabar | `10` | UPC-E1 | `27` | Postal (UK) |
| `03` | Code 128 | `11` | PDF-417 | `28` | Macro PDF |
| `04` | D25 | `13` | Code 39 Full ASCII | `30` | RSS-14 |
| `05` | IATA | `15` | Trioptic Code 39 | `31` | RSS Limited |
| `06` | ITF | `16` | Bookland | `32` | RSS Expanded |
| `07` | Code 93 | `17` | Coupon | `48`–`4B` | UPC/EAN + 2-digit add-on |
| `08` | UPC-A | `19` | ISBT-128 | `50` | UPC-E1+2 |
| `09` | UPC-E | `1B` | Data Matrix | `88`–`8B` | UPC/EAN + 5-digit add-on |
| `0A` | EAN-8 | `1C` | QR code | `90` | UPC-E1+5 |
| `0B` | EAN-13 | `1D` | Composite | | |
| `0C` | Code 11 | `1E` | Postnet (US) | | |
| `0E` | MSI | `20` | Code 32 | | |

For HCRM stock counting, print item barcodes as **Code 128** (`03`) encoding
the item **SKU** — the API's keyword search already matches SKUs exactly.

### 4.6 Packed timestamps

A 4-byte big-endian integer, second in the least-significant bits:

```
bits 0–5    year − 2000      (0–63  → 2000–2063)
bits 6–9    month            (1–12)
bits 10–14 day               (1–31)
bits 15–19 hour              (0–23)
bits 20–25 minute            (0–59)
bits 26–31 second            (0–59)
```

Example: 2025-06-15 14:30:05 → `25 | 6<<6 | 15<<10 | 14<<15 | 30<<20 | 5<<26`
= `367476121` = `0x15E73D99` → on the wire `15 E7 3D 99`.

Timestamps are only stored while the `store_rtc` parameter (§4.7) is enabled
(it is by default) — and they are only as good as the scanner's clock, so set
it before each counting session (§5, `set-time`).

### 4.7 Device parameters

Read with opcode `08` (payload: param id), write with opcode `03` (payload:
param id, value). Values are single bytes. Table from the official Opticon
wiki (archived; see Provenance):

**Supported parameters**

| ID | Name | Values / notes |
|---|---|---|
| `02` | Buzzer volume | 0 = off … 5 = softest |
| `04` | Reject redundant barcode | 0 = all allowed, 1 = not two consecutive, 2 = all unique |
| `05` | Scan angle | |
| `07` | Low battery indication | |
| `08` | Code 128 enable | 0/1 — **leave on for HCRM** |
| `09` | UPC enable | |
| `0A` | Host connect beep | beep when USB link comes up |
| `0B` | Host complete beep | beep when a download completes |
| `0F` | Auto clear | clear memory automatically after an upload |
| `11` | Scanner ON time | 1–10 s in 100 ms units (default 30 = 3 s) |
| `1E` | Good decode LED duration | |
| `1F` | Code 39 enable | |
| `21` | Delete enable | bit 0 = clear-all, bit 1 = delete-one, bit 2 = factory-reset via buttons |
| `22` | Max barcode length | 1–30 |
| `23` | Store RTC | timestamp each scan (default on) |
| `24`/`25` | UPC-A / UPC-E preamble | |
| `26` | Scratch pad | |
| `29`–`41` | Symbology conversions, check-digit and length options | see wiki / `scripts/opn2001.py` |
| `55` | Toggle buzzer | whether a 10 s Scan press toggles the buzzer |
| `57` | Buzzer enable | |

**Read-only (compatibility)**: `0D` baud rate (9600), `1C` reset baud rates,
`1D` baud switch delay, `20` comm awake time, `2F` UPC/EAN security level,
`31` data protection, `32`/`33` memory full/low indication, `38` coupon code,
`4F` ASCII mode, `56` beeper auto-on.

---

## 5. Working with the scanner from this repository

`scripts/opn2001.py` is the connection tool: a stdlib-only implementation of
§4 plus a CLI. `pyserial` is only needed at runtime, when a port is actually
opened — the recommended invocation keeps it out of the project's
dependencies:

```bash
# Linux (default port /dev/ttyUSB0, or $OPN2001_PORT, or --port)
uv run --with pyserial python scripts/opn2001.py info

# Windows
uv run --with pyserial python scripts/opn2001.py --port COM4 info
```

Subcommands:

| Command | What it does |
|---|---|
| `info` | Wakes the link; prints device id, firmware, scanner clock, scan count |
| `time` | Reads the scanner clock |
| `set-time [--datetime 2025-06-15T14:30:05]` | Sets the clock (default: now) |
| `read [--json] [--clear-after]` | Downloads all scans as CSV (`timestamp,symbology,barcode`) or JSON; `--clear-after` wipes the scanner afterwards |
| `clear [--yes]` | Deletes all scans (asks first) |
| `param NAME [VALUE]` | Reads/writes a parameter by name (`volume`, `auto_clear`, …) or numeric id (`0x02`) |

Example session — a full stock-count round-trip:

```console
$ uv run --with pyserial python scripts/opn2001.py set-time        # timestamps are trustworthy
scanner time set to: 2025-06-15 14:30:05

$ uv run --with pyserial python scripts/opn2001.py read --clear-after
2025-06-15 14:12:41,Code 128,EX-001
2025-06-15 14:12:44,Code 128,EX-001
2025-06-15 14:12:52,Code 128,EX-003
# 3 scan(s) from device 4711081519000001
# scanner memory cleared (--clear-after)

$ uv run --with pyserial python scripts/opn2001.py read --json
{"device_id": 4711081519000001, "count": 0, "scans": []}
```

The JSON output is deliberately machine-friendly so a reconcile step can
consume it directly (§6).

Design rules the tool follows (matching this repo's script conventions):

* **Destructive commands are explicit**: `read` never clears by default;
  `clear` prompts unless `--yes`.
* The protocol layer is importable without pyserial and fully unit-tested
  offline (`tests/test_opn2001.py` — CRC known-answer vectors, framing,
  timestamp packing, record parsing).
* Errors are reported, never raised raw: timeouts and CRC mismatches become
  clear messages on stderr with a non-zero exit code.

---

## 6. Using the scanner with HCRM

The OPN-2001 is a natural fit for the shop's stock counting, and needs **no
server changes** to start being useful:

1. **Label the stock** with Code 128 barcodes encoding the item **SKU**
   (print from the table view's CSV/PDF export). The catalogue API's keyword
   search (`GET /api/items?q=EX-001`) matches SKUs exactly.
2. **Count**: walk the shop scanning items. Duplicates are meaningful (one
   scan per physical unit) — leave "reject redundant" (`param reject_redundant
   0`) off.
3. **Download & reconcile** (staff or admin session):
   ```bash
   uv run --with pyserial python scripts/opn2001.py read --json > /tmp/count.json
   ```
   then aggregate `/tmp/count.json` per SKU and compare against the catalogue
   (`GET /api/items?limit=100&offset=…`), applying corrections with
   `PATCH /api/items/{id} {"stock": <counted>}`. The reconcile step is
   intentionally not automated yet — see the note below.
4. **Clear the scanner** only after the reconcile succeeded
   (`--clear-after`, or `clear --yes`).

Future work (tracked, not implemented here): an `POST /api/stock-count`
endpoint that accepts the JSON above and applies/audits the deltas
server-side; an EAN/GTIN field on items so retail barcodes can be scanned
instead of printed SKU labels; per-location counting (scan a location code
first, then items — the OPN-2001 keeps scan order).

---

## 7. Troubleshooting

| Symptom | Cause → fix |
|---|---|
| No `/dev/ttyUSB*` appears (Linux) | Check `lsusb` for `065a:0009`. If present but no tty: `sudo modprobe opticon` (driver may be a module); check `dmesg`. If the battery was deeply discharged, let it charge a few minutes first. |
| `Permission denied: '/dev/ttyUSB0'` | Your user is not in `dialout` — see §3.1 (group membership or udev rule), then re-login. |
| Windows: no COM port | Install the Opticon USB driver (manual §4) *before* connecting; check Device Manager. |
| First command times out | The first frame also **wakes** the link — retry once; make sure the battery isn't empty (red LED while on cable). |
| `read` returns 0 scans but you definitely scanned | Wrong port (another `ttyUSB`/`COMx`); memory was already cleared/downloaded with `auto_clear` enabled; or the scans happened before `store_rtc`/config changes — check with `info`. |
| CRC-mismatch warning on `read` | Records are kept; the reference implementations disagree on the data-response CRC scope (§4.4). If it persists on a real device, capture the hex dump and compare against §4. |
| Solid red LED while scanning | **Memory full** — download and clear. |
| No beep on good scans | Buzzer was toggled off by a 10 s Scan press; toggle it back the same way, or `param volume 1` (and `param toggle_buzzer 0` to stop it happening). |
| Red/Green flashing LED on USB | Battery may be defective — Opticon service. |
| Wrong timestamps on scans | Scanner clock drifted — `set-time` before each session; note the RTC range is 2000–2063. |

---

## 8. Provenance and references

* **Opticon OPN-2001 User's Manual**, v01-2006 (Opticon Sensors Europe B.V.)
  — hardware operation, LED/buzzer semantics, USB driver installation,
  charging. (The copy studied for this document: the PDF supplied with the
  feature request.)
* **Opticon wiki, "OPN 2001" page** (archived, last good capture 2009-11-29)
  — USB-VCP interface, RBBV stream name, MDL-2100 engine, the supported /
  read-only parameter tables, firmware upgrade procedure via Appload.
* **Linux kernel `drivers/usb/serial/opticon.c`** (Greg Kroah-Hartman 2008,
  Johan Hovold 2011, Martin Jansen / Opticon 2011) — authoritative source for
  VID/PID `065a:0009`, the 2-byte bulk-in packet header, control-endpoint
  writes, and the RTS/CTS handshake.
* **PyOPN** (Simon Jouet) — Python implementation of the framing, CRC-16,
  timestamp bitfield, parameter enums and the get-data record layout; the
  source of the set-time known-answer vector (`… 00 34 83`).
* **open-opn** (ktemkin) — Ruby implementation; the `KnownProtocols.xlsx`
  reverse-engineering sheet confirming the CRC parameters ("initial FFFF,
  final xor FFFF, reverse data") and the set/get-date frames.
* **OPN-Device-application** (C-Rodg) — Electron/Node implementation; source
  of the raw wake/clear/get-data/get-time/power-down frames, the 23-byte
  interrogate reply layout and the symbology-ID table.
* **`usb.ids`** — the `065a:0009` / `065a:0001` distinction (§1).
* **unix.stackexchange.com #235070** — the `opticon` kernel driver binding on
  other Opticon models (the `new_id` trick is *not* needed for the OPN-2001:
  its IDs are in the driver's table).

Every raw frame, the CRC-16 parameters and the timestamp layout stated in
this document were additionally **re-derived and verified computationally**
during the writing of `scripts/opn2001.py`; the results are pinned as
known-answer vectors in `tests/test_opn2001.py`, which runs in the normal
`uv run pytest` suite with no hardware attached.
