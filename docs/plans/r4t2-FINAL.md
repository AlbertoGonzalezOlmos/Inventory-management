# Round 4 T2 FINAL spec — daemon evdev decode core (`scripts/scanner_daemon.py`)

Decode-only slice: no grab (that's T3), no HTTP (T5). Fully offline-testable.

## Discovery

`discover_event_device()` — walk `/sys/class/input/*/device/` and return the
`/dev/input/eventN` whose `id/vendor` == `065a` and `id/product` == `a001`
(hex, case-insensitive). Return `None` when absent (caller decides). Do not rely
on `/dev/input/by-id` (absent on this host).

## Evdev struct parsing

`parse_input_event(buf: bytes) -> (type, code, value)` — unpack the Linux
`input_event` struct: `timeval` (16 bytes on 64-bit, 8 on 32-bit) + `type`
(u16) + `code` (u16) + `value` (i32). **Parameterise the timeval size** (32/64-bit)
with a per-arch default; keep it pure so synthetic 24-byte and 16-byte streams
both work in tests. We only use `type == EV_KEY (0x01)`, `value == 1` (press).

## Key → char translation

Map Linux `KEY_*` codes to characters. Reuse the existing HID usage table from
`scripts/scanner_hid.py` (`_USAGE_MAP`) by translating `KEY_*` → HID usage:
letters `KEY_A..Z` (30..55) → HID usage 4..29; digits `KEY_1..0` (2..11) → HID
usage 30..39; `KEY_ENTER` (28) → HID 40; `KEY_SPACE` (57), `KEY_MINUS` (12),
`KEY_EQUAL` (13), and the punctuation/shift handling already in `_USAGE_MAP`.
Feed the translated report into the existing, tested `ScanAssembler` (reuse it —
do not write a second assembler). Keep the same CR/LF/CRLF terminator tolerance.

## Loop skeleton

`read_scans(fd) -> Iterator[str]`: read 24-byte (or arch-sized) records, decode
presses, translate, feed the assembler, yield one completed scan string per
terminator. (The actual `open`/grab lives in T3 — `read_scans` takes an already
open fd.)

## Tests (`tests/test_scanner_daemon.py`)

Synthetic `struct.pack` streams (digits, letters, punctuation, CR/CRLF/LF
terminators, shift, rollover, unmapped-key reset), a fake sysfs tree in
`tmp_path` for `discover_event_device` (present/absent/multiple), and the
32-bit-timeval variant. No real device required.

## Gates

`uv run pytest` green; the daemon module must import without a scanner attached.
