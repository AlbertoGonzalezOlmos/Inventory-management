# Round 4 T3 FINAL spec — grab + lifecycle + udev + systemd

## Grab (`scripts/scanner_daemon.py`)

- `EVIOCGRAB = 0x40044590`.
- `GrabbedDevice` context manager: open the discovered event device
  `O_RDONLY`, **verify it is 065a:a001** (from the discovery path — never grab a
  device whose VID/PID didn't match; this is the human-keyboard guard), then
  `fcntl.ioctl(fd, EVIOCGRAB, 1)`.
  - `EBUSY` → loud exit "another process holds the scanner".
  - `EACCES` → "no permission to open/gr ab — install the udev rule / run in the
    input group".
  - `ENODEV`/`EIO` (unplug) → close + signal re-discovery.
  - Exit path (normal, exception, crash) → fd close **auto-releases** the grab
    (kernel guarantees it), so the scanner reverts to keyboard-wedge with zero
    operator action.
- Re-discovery loop: on ENODEV/EIO, close, sleep, `discover_event_device()`
  again (by VID/PID), re-open, re-grab. Never exit on a transient unplug.
- A lockfile prevents two daemon instances grabbing the same device.

## Udev rule (`scripts/udev/99-opticon-scanner.rules`)

Append (the event-device node is `root:input` today; the daemon needs it):

```
SUBSYSTEM=="input", ATTRS{idVendor}=="065a", MODE="0660", GROUP="plugdev"
```

Update the file's "Covers:" comment block to mention the input subsystem.

## systemd unit (`scripts/systemd/scanner-daemon.service`)

`Restart=always`, `After=systemd-udevd.service`, `SupplementaryGroups=input`
(or `plugdev` per distro), `ExecStart` the daemon's T5 entrypoint
(`--forward --config …`), `StandardOutput=journal`. A short `[Install]`
`WantedBy=multi-user.target`. (The daemon binary args finalise in T5; keep the
unit pointing at the future CLI.)

## Tests (`tests/test_scanner_daemon_grab.py`)

Injected `opener` + `ioctl` callables (no real device):
- grab calls `EVIOCGRAB` with value 1; release (context exit) closes the fd.
- grab-on-exception still closes (auto-release).
- `EBUSY` / `EACCES` / `ENODEV` → the right message/behaviour, no throw past the
  contract.
- wrong-device refusal: an opener that would return a non-065a device is never
  grabbed (ioctl never called).
- lockfile: second instance refused.

## Gates

`uv run pytest` green. Live grab on `/dev/input/event8` + systemd install is a
manual step (documented in the unit comments), not a CI gate.
