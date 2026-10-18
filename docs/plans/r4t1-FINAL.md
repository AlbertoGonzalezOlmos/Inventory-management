# Round 4 T1 FINAL spec — shared scan classifier (`app/scanshapes.py`)

Single source of truth for "is this scanned string a badge, a barcode, or
unshaped", used by auth, the bridge, and (later) the daemon ingest. The server
re-derives kind at ingest — never trusts the client/daemon.

## `app/scanshapes.py`

```python
BADGE_PREFIX = "HCRM1:"          # already defined in app/qrbadge.py
BADGE_FULL_RE = r"^HCRM1:[A-Za-z0-9_-]{43}$"
BADGE_RAW_RE  = r"^[A-Za-z0-9_-]{43}$"

def classify_scan(code: str) -> str:
    # -> "badge" | "barcode" | "unshaped"
```

- `badge` if `code` matches `BADGE_FULL_RE` or (no prefix and) `BADGE_RAW_RE`.
- `barcode` if it is "barcode-ish" (any non-empty printable string that is not a
  badge — the routing-only gate the SPA/daemon uses; the barcode *validation*
  stays in `app/barcodes.analyze_scan`).
- `unshaped` for empty/whitespace.

Import `BADGE_PREFIX` from here (or re-export from `app/qrbadge.py` — do NOT
introduce a second copy). Prefer `classify_scan` as the public entry.

## Delegate the existing sites

- `app/routers/auth.py`: `_badge_from_scanned` uses `classify_scan`/the shared
  regexes (prefixed + raw 43-char forms) — no behaviour change.
- `scripts/scanner_bridge.py`: `redact_code` / `_RAW_BADGE_RE` use the shared
  shapes (import `app.scanshapes`; the bridge already imports from `app.`).
- Keep `app/qrbadge.parse_badge_payload` (it also does the DB-facing strict
  parse) but route its prefix/len checks through the same constants.

## Parity test

A **regex-source-equality** test (stronger than behaviour vectors): assert the
auth and bridge badge detectors reference the *same* compiled regex objects (or
the same `classify_scan`) as `app/scanshapes.py`, so the three sites can never
drift. Plus a small classification table (badge full / badge raw / barcode /
unshaped / empty / 43-char-but-wrong-charset → barcode).

## Gates

`uv run pytest` green (no behaviour change — all existing qr-login / bridge tests
must pass unchanged).
