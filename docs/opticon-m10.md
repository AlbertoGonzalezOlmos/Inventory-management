# Opticon M-10 — connection & interaction guide

How to connect the **Opticon M-10** 2D presentation scanner to a computer and
integrate it with HCRM (this repository ships a driver in `app/scanner/`, a
ready-made bridge in `scripts/scanner_bridge.py`, and QR-badge login — §9).

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
| **USB-HID** (keyboard) | A keyboard; scans are "typed" wherever the cursor is | ❌ host → scanner not possible | Zero-integration data entry (click a field, scan) — incl. QR-badge login (§9) |
| **USB-COM** (VCP, CDC-ACM) | A virtual serial port (`COMx` / `/dev/ttyACM0`) | ✅ full command channel | **HCRM integration (recommended)** |
| **RS-232C** (DB9, external PSU) | A real serial port | ✅ full command channel | Legacy/POS hardware, long cable runs |

USB VID/PID identifies the personality, so you can check the mode from the OS
without scanning anything. **USB-COM = `065A:A002`** is documented by Opticon
(Specifications Manual §18.5). The HID personality reports **`065A:A001`** on
this project's machine, but `A001` appears in *no* public Opticon document we
could find (§18.4 lists no PID), so treat "A001 = the M-10 in HID mode" as an
observation, not a specification — `docs/opticon-hardware.md` §1 records the
claim and its provenance, `scripts/opticon_detect.py` carries it as
`confirmed: False`, and §3 below gives the one hardware step that settles it.
Scan the personality you actually have, not the one you assume.

Factory defaults (Specifications Manual §18): RS-232C line = **9600 bps, 8 data
bits, no parity, 1 stop bit, no handshaking**; data suffix = **CR**; read mode
= auto trigger; data buffering = buffered mode. USB-COM is CDC-ACM with
**Vendor ID 065A, Product ID A002**.

> To switch the USB personality (or restore defaults) you scan the appropriate
> configuration sheet: the wiki's *M-10* page links "USB COM Port" / "USB
> Keyboard (HID)" / "RS232" sheets, and §4.2 below gives the factory-default
> label. For anything more advanced use the **Universal Menu Book** or
> **OptiConfigure** (§7).

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
   - **WSL (Windows Subsystem for Linux)**: USB devices are *not* visible
     inside WSL by default — they belong to the Windows host until a
     `usbipd-win` passthrough (`bind` once, `attach --wsl` after every
     replug/reboot). The full three-layer story, the device-node permissions
     and this machine's dated state log are in **`docs/opticon-hardware.md`
     §2**; run `uv run python scripts/opticon_detect.py` to see which layer is
     failing. Once attached, `cdc_acm` gives you `/dev/ttyACM0` and the bridge
     runs inside WSL as on native Linux. Alternatively run it on the Windows
     side (`py -m pip install pyserial`, then
     `python scripts\scanner_bridge.py` against the `COMx` port).

> **Field report (this project's dev machine, 2026-09-29):** the attached
> Opticon unit enumerates as `HID\VID_065A&PID_A001` ("HID Keyboard Device")
> — i.e. it is in **USB-HID mode**, so no `COMx`/`ttyACM*` port appears and
> *this driver and bridge cannot talk to it as-is*. HID mode is fine for
> QR-badge login and catalogue-search scanning (§9). Whether that unit is an
> M-10 is the open question from §2: `A001` is not a documented Opticon PID.
> **The experiment that settles both at once** — scan the *USB COM Port*
> configuration sheet (wiki link in §10): if the device re-enumerates as
> `065A:A002` and a `COMx`/`ttyACM*` port appears, it is an M-10, the bridge
> works, and `docs/opticon-hardware.md` §1 can upgrade `A001` to
> `confirmed`. Until then, receive from it with
> `scripts/scanner_hid.py` (focus-free HID capture,
> `docs/opticon-hardware.md` §3).

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

Turns scans into catalogue actions on a running HCRM server.

**Identity is exact, never guessed.** A scan is matched on its canonical GTIN
(`items.barcode`, via `GET /api/items?barcode=`) or, for an opaque
store-internal label, on exact `sku` equality. There is no silent fallback to
"the first keyword-search hit": that used to make any partial word or mis-scan
resolve to an unrelated product — and with `--stock-out` it *demonstrably*
decremented the wrong item's stock (REVIEW-m10.md P1). Keyword matching is now
behind `--fuzzy`, which the bridge **refuses to combine** with a stock
adjustment.

Because identity comes from `app/barcodes`, one product cannot become two
catalogue entries: scan its UPC-E form and it finds the item filed under its
EAN-13 GTIN. Do **not** file retail barcodes in the `sku` field (the convention
this guide used to describe) — `sku` is for opaque internal labels only.

What a non-match reports:

| Verdict | Meaning |
|---|---|
| `valid GTIN, not in the catalogue — new-entry candidate (region, usage)` | An unknown product: create it (staff), don't force a match. |
| `MISREAD — check digit fails; re-scan, do not file` | A damaged/mis-scanned code. Not looked up at all, so it cannot become a phantom identity. |
| `no catalogue match (opaque payload) — if this is a UPC-E, …` | A short all-digit payload. `app/barcodes` only expands a UPC-E on the scanner's word, because the expansion computes its own check digit — configure the scanner to transmit its symbology ID and pass `--symbology UPC-E`. |

The bridge is built to survive an always-on back office: an API error is
reported per scan (stderr, and an `error` field in `--json`) and the listener
keeps running; `--relogin` re-authenticates once on a 401 and retries (a counter
bridge outlives the 7-day token TTL); a server that is down or restarting gives
one clear line rather than a traceback. And it refuses to open a port it has not
verified as `065A:A002` — see §3 and `docs/opticon-hardware.md` §1 for why
auto-picking "the first serial port" is how a shop ends up reading the OPN-2001's
binary protocol at the wrong parity and calling it barcodes (`--any-port` is the
explicit, warned opt-in).

```bash
./scripts/run.sh &                       # 1. start HCRM

uv run python scripts/scanner_bridge.py \  # 2. listen & look up
    --email staff@shop.local --password 'secret'

uv run python scripts/scanner_bridge.py --email staff@shop.local \
    --stock-in 1                          # goods receiving: +1 stock per scan

uv run python scripts/scanner_bridge.py --email staff@shop.local \
    --stock-out 1 --json                  # sales counter: -1, JSONL output
```

Flags: `--port` (default: auto-detect a **verified** `065A:A002`),
`--any-port PORT` (skip verification, warned), `--baudrate` (RS-232C only),
`--stock-in N` / `--stock-out N` (mutually exclusive; staff/admin only; a
decrement clamped at 0 is reported as clamped, not silently dropped),
`--symbology SYM`, `--fuzzy` (never with a stock adjustment), `--relogin`,
`--json`. Env: `HCRM_BASE`, `HCRM_SCANNER_PORT`.

Stock adjustment is **atomic**: the bridge calls
`POST /api/items/{id}/stock-adjust`, which applies the delta inside one
targeted `BEGIN IMMEDIATE` (a per-operation write lock around the
read-modify-write — *not* the global connection hook `app/database.py`
documents as an incident). Two bridges scanning the same item concurrently
serialise and lose nothing; the response reports the applied delta and
whether the adjustment clamped at 0, and the bridge surfaces both.

## 6a. Bridge behaviour with QR badges

Since the QR-login feature (§9), the bridge treats scans whose payload
starts with `HCRM1:` specially: instead of a catalogue lookup it exchanges
the badge for a session via `POST /api/auth/qr-login` and prints the
resulting session token (honouring `--json`). A rejected badge is reported
and the bridge keeps listening. This covers headless/kiosk setups where
the scanned credential must reach the API without a browser field to type
into; for an interactive PC, USB-HID + the login-page badge field (§9) is
the simpler path.

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
| No `/dev/ttyACM0` on Linux | Scanner is still in USB-HID mode → scan the *USB COM Port* sheet. Check `dmesg \| grep cdc_acm`. On WSL2, also check the usbipd attach (`docs/opticon-hardware.md` §2). |
| `Permission denied` opening the port | Add user to `dialout`/`uucp` (§3) or install `scripts/udev/99-opticon-scanner.rules` once (`docs/opticon-hardware.md` §4). Running the bridge with sudo is not recommended. |
| The bridge opened a port but reads garbage | Wrong device: another Opticon personality is attached and the line settings differ (the OPN-2001 is 9600 **8O1**, this one 9600 **8N1**). Select by VID:PID `065A:A002` — `uv run python scripts/opticon_detect.py` (`docs/opticon-hardware.md` §1). |
| Garbage characters / no frames | RS-232C line settings mismatch → 9600 8N1 no handshake, or `restore_factory_defaults()`. |
| Scans arrive but "no catalogue match" | Read the verdict the bridge prints (§6): a *new-entry candidate* means the GTIN is valid and simply not in the catalogue; a *MISREAD* means the check digit failed (re-scan); an *opaque payload* means it was matched against `sku` only — if it is a UPC-E, pass `--symbology UPC-E`. The item's retail code belongs in `items.barcode`, never in `sku`. |
| The bridge adjusts the stock of the wrong item | Cannot happen from a guess any more: matches are exact-identity only, and `--fuzzy` is refused together with `--stock-in/--stock-out` (§6, REVIEW-m10.md P1). If it happens, the catalogue has two entries for one product — check for a GTIN filed under `sku`. |
| Commands time out | Scanner is in USB-HID mode (no command channel), or the host opened the wrong port. |
| Driver won't install on Windows | FIPS mode enabled blocks Opticon's USB driver (Opticon's own note on the wiki). |
| `Z1`/`Z2` rejected (ESC) | Command not executable in current state — e.g. trigger disabled config; restore defaults and retry. |
| Badge scan logs nobody in | Badge was revoked/rotated, or never generated → issue a fresh one (§9.2). A scan of an *item* barcode in the login field also fails — only `HCRM1:` payloads are badges. |
| Badge scan opens a Google/search page | A phone camera or a scanner in HID mode typed the payload into the wrong window — the badge is not a URL by design; use the login-page badge field (§9.3). |

## 9. QR badge login (scan-to-login)

The M-10 reads QR codes natively (MDI3100 engine, 2D imager), so HCRM uses
it for **passwordless login**: every account can have a printable **QR
badge** that is scanned instead of typing email+password.

### 9.1 The credential

- Payload printed inside the QR: `HCRM1:<urlsafe-token>` — a versioned
  prefix (distinguishes badges from product barcodes; the bridge keys on
  it, §6a) plus 256 bits of randomness (`secrets.token_urlsafe(32)`).
- A badge is a **bearer credential, exactly like a password**: whoever
  holds the printed code logs in as that account. Only the SHA-256 hash is
  stored (`users.qr_badge_hash`, same treatment as session tokens), so the
  raw payload is shown **exactly once** — at generation time — and a
  database leak does not yield printable badges.
- The QR is generated server-side with a vendored pure-Python encoder
  (`app/vendor/qrcodegen.py`, Nayuki, MIT — the project installs offline,
  so no new dependency) and returned as **inline SVG** (ECC level M,
  scuff-tolerant; no PNG tooling needed, prints crisply at any size).

### 9.2 Issuing a badge

| Who | Endpoint / UI |
|---|---|
| Self-service | Account → *QR badge login*, or `POST /api/auth/qr-badge` (replace = rotate), `DELETE /api/auth/qr-badge` (revoke) |
| Staff at the counter | Members table → *QR badge* (prints a card), or `POST /api/members/{id}/qr-badge` / `DELETE …/qr-badge` |

Staff can manage badges for member/staff accounts but **never for admin
accounts** (same rule as account edits); admins can manage anyone's.
Regenerating a badge immediately invalidates the previous one — that is
the rotation path for a lost or copied card. Deleting an account destroys
its badge with it.

### 9.3 Logging in with a badge

**USB-HID mode (zero setup — how the dev machine's unit is configured):**
the login page has a *QR badge* field under the password form. Click it
(or tab to it), scan the badge: the scanner types `HCRM1:…` and its CR
suffix submits the form. The frontend posts it to `POST /api/auth/qr-login`
and stores the returned session exactly like a password login.

**HID mode without a browser field** (kiosk, or capturing scans without window
focus): `scripts/scanner_hid.py` reads the raw HID reports — see
`docs/opticon-hardware.md` §3.

**USB-COM / RS-232C mode:** the bridge handles it (§6a) —

```bash
uv run python scripts/scanner_bridge.py --email staff@shop.local
# scan a badge → "[badge] logged in: Alice <alice@shop.local> (member)"
#                 session token: <bearer token>
```

**API:** `POST /api/auth/qr-login {"token": "HCRM1:…"}` → the same
`{token, user}` shape as `POST /api/auth/login` (raw token without the
prefix is also accepted). 401 otherwise. A badge login for an account
flagged `must_change_password` behaves like a password login: the session
works, but only the password-change endpoints until the flag is cleared.

### 9.4 Security notes

- Badges are unguessable (256-bit), so the unauthenticated `qr-login`
  endpoint carries no rate limit; do not "simplify" badges to short
  numeric codes.
- No user-enumeration channel: the lookup is by exact hash match and
  failures are a uniform 401.
- Treat badges physically like keys: print on demand, hand over privately,
  revoke on loss. For shared/kiosk PCs, remember the session itself is the
  normal 7-day bearer token — log out after use.

## 10. References

- M-10 product page — <https://wiki.opticonusa.com/techsupport/en/M-10>
- M-10 datasheet — <https://files.opticonusa.com/Downloads/m10datasheet.pdf>
- M-10 Specifications Manual — <https://files.opticonusa.com/Downloads/m10_specifications_manual.pdf>
- Universal Menu Book — <https://files.opticonusa.com/UniversalMenuBook/UniversalMenuBook.pdf>
- MDI3100 engine page (Linux/ttyACM note, serial command doc pointer) — <https://wiki.opticonusa.com/techsupport/en/MDI3100>
- OptiConfigure — <https://configure.opticon.com/>
- All-In-One PC Drivers — <https://files.opticonusa.com/Downloads/USB%20Drivers%20Installer.zip>
