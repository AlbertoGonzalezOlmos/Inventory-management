# Round 4 T7 FINAL spec — docs + smoke + checklist

## Docs

- `README.md`: a "scanner daemon (option 2)" install section — install the udev
  rule (`sudo cp scripts/udev/99-opticon-scanner.rules …`), enable the systemd
  unit, run `--register` once to print the pairing code, then pair from the
  login screen. One paragraph + env/config note.
- `docs/opticon-m10.md`: a § on the daemon (grab, lifecycle, fallback to
  keyboard-wedge, pairing, the status pill).
- `NOTES.md`: a round-4 paragraph.

## Hardware-free smoke round trip (`scripts/smoke_test.py`)

Add a scanner-channel check (no hardware — pure HTTP): register a terminal
(loopback), ingest an event with the secret, poll it back with the token
(cursor + at-most-once: second poll returns nothing), unpair. Runs in the
existing CI `scripts` job.

## Hardware checklist (`docs/plans/r4-checklist.md`)

The live 12-step Track-A run (scanner attached): grab engaged (scan does NOTHING
in a focused terminal), pair from login, scan a badge → logs in via the feed
(no focus needed), scan a barcode → cart via the feed, same-user badge → 60 s
overlay, different-user badge → switch, `kill -9` the daemon → typing resumes
(wedge fallback), no double-delivery (wedge + feed never both), TTL expiry,
status pill states, unplug/replug re-discovery.

## Gates

`uv run pytest`; `scripts/smoke_test.py --allow-degraded`; `node --check
static/app.js static/scanfeed.js`.
