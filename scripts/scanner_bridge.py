#!/usr/bin/env python3
"""Bridge between an Opticon M-10 scanner and a running HCRM server.

Every scanned barcode is looked up in the catalogue (exact SKU match
first — so put the product's barcode/EAN in the item's SKU field), and
can optionally adjust stock (goods receiving / point of sale style).

Usage (server must be running; scanner in USB-COM or RS-232C mode):

    uv run python scripts/scanner_bridge.py \
        --email staff@shop.local --password 'secret' [--port /dev/ttyACM0]

    # stock adjustments (staff account required):
    ... --stock-in 1        # each scan increments stock by 1
    ... --stock-out 1       # each scan decrements stock by 1

    # machine-readable output (one JSON object per scan on stdout):
    ... --json

Scans of an HCRM QR badge (payload "HCRM1:…") are NOT looked up in the
catalogue: they are exchanged for a login session via /api/auth/qr-login
and the resulting session token is printed (useful for headless/kiosk
setups and for testing badges from the command line). For interactive
browser login, put the scanner in USB-HID mode and scan into the badge
field on the login page instead (docs/opticon-m10.md §10).

Environment overrides: HCRM_BASE, HCRM_SCANNER_PORT.
Run from the repo root, like the other scripts. Stdlib only, except the
optional pyserial (see app/scanner/transport.py).
"""

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# Allow `python scripts/scanner_bridge.py` from the repo root: put the
# repo root on sys.path so `app.*` imports resolve.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.scanner import OpticonM10, find_scanner_ports  # noqa: E402
from app.scanner.transport import SerialOpenError  # noqa: E402

BASE = os.environ.get("HCRM_BASE", "http://localhost:8000")
BADGE_PREFIX = "HCRM1:"  # keep in sync with app.qrbadge.BADGE_PREFIX


class ApiError(Exception):
    def __init__(self, status, path, detail):
        super().__init__(f"API error {status} on {path}: {detail}")
        self.status = status


def api(path, method="GET", token=None, body=None):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        headers={"Content-Type": "application/json"},
        data=None if body is None else json.dumps(body).encode(),
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as res:
            raw = res.read()
            return res.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except ValueError:
            pass
        raise ApiError(exc.code, f"{method} {path}", detail) from exc


def find_item(token, code):
    """Exact SKU match first, then fall back to the first search hit."""
    status, items = api(f"/api/items?q={urllib.parse.quote(code)}&limit=100", token=token)
    for item in items:
        if item["sku"].lower() == code.lower():
            return item, "sku"
    return (items[0], "search") if items else (None, None)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", default=os.environ.get("HCRM_SCANNER_PORT"),
                        help="serial port (default: auto-detect)")
    parser.add_argument("--baudrate", type=int, default=9600,
                        help="RS-232C model only (default 9600, the factory setting)")
    parser.add_argument("--email", required=True, help="HCRM account email")
    parser.add_argument("--password", help="omit to be prompted")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--stock-in", type=int, metavar="N",
                       help="add N to stock on every scan")
    group.add_argument("--stock-out", type=int, metavar="N",
                       help="subtract N from stock on every scan")
    parser.add_argument("--json", action="store_true",
                        help="emit one JSON object per scan instead of text")
    args = parser.parse_args()

    password = args.password or getpass.getpass("HCRM password: ")
    try:
        status, auth = api("/api/auth/login", method="POST",
                           body={"email": args.email, "password": password})
    except ApiError as exc:
        raise SystemExit(str(exc))
    token = auth["token"]
    user = auth["user"]
    adjust = args.stock_in if args.stock_in else (-args.stock_out if args.stock_out else 0)
    if adjust and user["role"] not in ("staff", "admin"):
        raise SystemExit("stock adjustment requires a staff or admin account")

    port = args.port
    if not port:
        candidates = find_scanner_ports()
        if not candidates:
            raise SystemExit(
                "no scanner port found — plug in the M-10 (USB-COM mode) or "
                "pass --port (see docs/opticon-m10.md)"
            )
        port = candidates[0]
        print(f"Auto-detected scanner port: {port}", file=sys.stderr)

    def emit(record):
        if args.json:
            print(json.dumps(record), flush=True)
        elif record["found"]:
            item = record["item"]
            print(f"[{record['code']}] {item['name']} "
                  f"(SKU {item['sku']}, stock {item['stock']}, "
                  f"{item['price_cents'] / 100:.2f})"
                  + (f"  → stock {record['old_stock']} → {record['new_stock']}"
                     if record.get("new_stock") is not None else ""))
        else:
            print(f"[{record['code']}] no catalogue match", flush=True)

    def badge_login(code):
        """Exchange a scanned QR badge for a session; never kills the bridge."""
        try:
            _, authd = api("/api/auth/qr-login", method="POST", body={"token": code})
        except ApiError as exc:
            record = {"code": code, "badge_login": False, "error": str(exc)}
        else:
            record = {
                "code": code,
                "badge_login": True,
                "user": authd["user"],
                "token": authd["token"],
            }
        if args.json:
            print(json.dumps(record), flush=True)
        elif record["badge_login"]:
            u = record["user"]
            print(f"[badge] logged in: {u['name']} <{u['email']}> ({u['role']})\n"
                  f"        session token: {record['token']}", flush=True)
        else:
            print(f"[badge] login failed: {record['error']}", flush=True)

    def on_scan(code):
        if code.startswith(BADGE_PREFIX):
            badge_login(code)
            return
        try:
            item, how = find_item(token, code)
            record = {"code": code, "found": item is not None, "match": how,
                      "item": item}
            if item is not None and adjust:
                old = item["stock"]
                new = max(0, old + adjust)
                if new != old:
                    _, updated = api(f"/api/items/{item['id']}", method="PATCH",
                                     token=token, body={"stock": new})
                    record.update(old_stock=old, new_stock=updated["stock"],
                                  item=updated)
                else:
                    record.update(old_stock=old, new_stock=old)
        except ApiError as exc:
            raise SystemExit(str(exc))
        emit(record)

    try:
        with OpticonM10(port, baudrate=args.baudrate) as scanner:
            print(f"Listening on {port} — scan a barcode (Ctrl-C to quit).",
                  file=sys.stderr)
            scanner.start_reading(on_scan)
            try:
                while True:
                    import time
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
    except SerialOpenError as exc:
        raise SystemExit(f"Cannot open scanner: {exc}")


if __name__ == "__main__":
    main()
