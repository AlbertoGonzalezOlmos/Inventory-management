#!/usr/bin/env python3
"""Bridge between an Opticon M-10 scanner and a running HCRM server.

Every scan is looked up by **exact identity** — the canonical GTIN in
`items.barcode` for a retail barcode, the exact `sku` for an opaque
store-internal label — and can optionally adjust stock (goods receiving /
point of sale style). A scan that matches nothing is *reported*, never
guessed at: `--fuzzy` opts into keyword-search matching, and is refused
outright when combined with stock adjustment.

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
field on the login page instead (docs/opticon-m10.md §9.3).

This is an always-on back-office process, so it is built to survive its own
environment: an API error is reported per scan and the listener keeps going
(a 401 after the 7-day token TTL, a transient 500, a 409 — for a counter
bridge those are *when*, not *if*); `--relogin` re-authenticates once on a
401 and retries. Nothing here exits silently.

Environment overrides: HCRM_BASE, HCRM_SCANNER_PORT, HCRM_HTTP_TIMEOUT.
Run from the repo root, like the other scripts. Stdlib only, except the
optional pyserial (see app/scanner/transport.py).
"""

import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Allow `python scripts/scanner_bridge.py` from the repo root: put the
# repo root on sys.path so `app.*` and `scripts.*` imports resolve.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.barcodes import analyze_scan  # noqa: E402
from app.scanner import OpticonM10, find_scanner_ports  # noqa: E402
from app.scanner.transport import SerialOpenError  # noqa: E402

BASE = os.environ.get("HCRM_BASE", "http://localhost:8000")
BADGE_PREFIX = "HCRM1:"  # keep in sync with app.qrbadge.BADGE_PREFIX

# urllib's default is no timeout at all: a server that accepts the connection
# and then stalls would block the scanner's reader thread forever (scans stop
# being read, so the OS buffer overflows and they are lost). Bounded so a
# stalled server becomes an ordinary per-scan ApiError instead of a hang.
HTTP_TIMEOUT = float(os.environ.get("HCRM_HTTP_TIMEOUT", "10"))


class ApiError(Exception):
    def __init__(self, status, path, detail):
        super().__init__(f"API error {status} on {path}: {detail}")
        self.status = status


def api(path, method="GET", token=None, body=None):
    """One HTTP call. Raises ApiError; never SystemExit.

    The distinction is load-bearing: this runs inside the scanner's reader
    thread, and a SystemExit there is a BaseException — it kills the callback
    and the thread while the main loop keeps sleeping, i.e. a bridge that still
    prints "Listening" but no longer listens (REVIEW-m10.md P2).
    """
    req = urllib.request.Request(
        BASE + path,
        method=method,
        headers={"Content-Type": "application/json"},
        data=None if body is None else json.dumps(body).encode(),
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as res:
            raw = res.read()
            return res.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except ValueError:
            pass
        raise ApiError(exc.code, f"{method} {path}", detail) from exc
    except urllib.error.URLError as exc:
        # Server down / restarting / wrong HCRM_BASE. Still an ApiError, not a
        # raw traceback and not a SystemExit: at the login prompt it becomes one
        # clear message, and mid-session it is a per-scan report that leaves the
        # listener alive so the bridge survives a server restart.
        raise ApiError(0, f"{method} {path}",
                       f"cannot reach {BASE}: {exc.reason}") from exc


def find_item(get, code, fuzzy=False, symbology=""):
    """Exact-identity catalogue lookup. Returns (item, how, analysis).

    ``get(path)`` performs the authenticated call and returns
    ``(status, payload)``; injecting it keeps this decision table testable
    without a server.

    ``how`` is one of ``"barcode"`` (canonical GTIN match), ``"sku"`` (exact
    SKU equality for an opaque label), ``"search"`` (keyword hit — only with
    ``fuzzy=True``) or ``None``.

    There is deliberately **no** silent fallback to "the first search hit"
    (REVIEW-m10.md P1): that made any partial word, mis-scan or label fragment
    resolve to an unrelated item, and with `--stock-out` it *demonstrably*
    decremented the wrong product's stock. A code that identifies nothing is
    reported as identifying nothing.

    Identity comes from `app.barcodes.analyze_scan` (P5), so a product scanned
    as UPC-E finds the item filed under its EAN-13 GTIN — which the old
    "put the barcode in the SKU field" convention could never do, since the two
    forms are different strings.
    """
    analysis = analyze_scan(code, symbology)

    if analysis.kind in ("gtin", "restricted"):
        _, items = get(f"/api/items?barcode={urllib.parse.quote(analysis.key)}")
        if items:
            return items[0], "barcode", analysis
        return None, None, analysis

    if analysis.kind == "invalid-gtin":
        # Right shape, bad check digit: a misread. Looking it up would turn a
        # scanning error into either a phantom identity or a wrong match.
        return None, None, analysis

    # Opaque payload (Code 128 SKU label, internal code): exact SKU equality
    # only. A short all-digit payload *might* be a zero-suppressed UPC-E, but
    # app.barcodes deliberately refuses to expand one without the scanner's
    # word — the expansion computes its own check digit, so any 6 digits would
    # "validate" and internal numeric codes would turn into fake GTINs. The
    # bridge does not undermine that by trying both number systems; the verdict
    # it emits says what is missing instead (--symbology UPC-E).
    _, items = get(f"/api/items?q={urllib.parse.quote(code)}&limit=100")
    for item in items:
        if item["sku"].lower() == code.lower():
            return item, "sku", analysis
    if fuzzy and items:
        return items[0], "search", analysis
    return None, None, analysis


def call_with_relogin(api_call, login_call, path, token, method="GET",
                      body=None, relogin=False):
    """One API call, with the P2 contract: re-authenticate once on a 401.

    Module-level and injected for the same reason ``find_item`` takes a
    ``get``: the 401-retry path is exactly the one a token that outlives its
    7-day TTL exercises every week, and it was previously untestable because
    it lived as a closure inside ``main()``.

    ``api_call`` is :func:`api`; ``login_call()`` performs a fresh login and
    returns the new token. Returns ``(result, token)`` — ``result`` is
    ``api_call``'s ``(status, payload)`` and ``token`` is the possibly
    refreshed token, which the caller **must keep using**. A second 401 (the
    fresh credentials were rejected too) propagates: one retry, never a loop.
    """
    try:
        return api_call(path, method=method, token=token, body=body), token
    except ApiError as exc:
        if exc.status != 401 or not relogin:
            raise
        print("session rejected (401) — re-authenticating", file=sys.stderr)
        new_token = login_call()
        return (api_call(path, method=method, token=new_token, body=body),
                new_token)


def badge_login(code):
    """Exchange a scanned QR badge for a session; return the record to emit.

    A rejected badge (or an unreachable server) is a *record*, never an
    exception: no scan of any kind may be able to kill an always-on bridge
    (REVIEW-m10.md P2).
    """
    try:
        _, authd = api("/api/auth/qr-login", method="POST", body={"token": code})
    except ApiError as exc:
        return {"code": code, "badge_login": False, "error": str(exc)}
    return {"code": code, "badge_login": True,
            "user": authd["user"], "token": authd["token"]}


def describe_attached_opticon():
    """What the shared hardware map sees — used to explain a refusal to guess.

    One hardware map for the repo (`scripts/opticon_detect.py`) instead of a
    private one per tool: this is the same knowledge `opn2001.py detect` and
    `scanner_hid.py` use.
    """
    try:
        from scripts.opticon_detect import (describe, find_linux_usb_devices,
                                            find_windows_ports)
    except Exception:  # pragma: no cover - the map is optional context
        return []
    lines = []
    try:
        if sys.platform.startswith("linux"):
            for d in find_linux_usb_devices():
                info = describe(d["pid"])
                nodes = ", ".join(d["ttys"] + d["hidraws"]) or "no node bound"
                lines.append(f"  065A:{d['pid']:04X}  {info['name']} [{nodes}]")
        elif sys.platform == "win32":
            for p in find_windows_ports():
                info = describe(p["pid"])
                lines.append(f"  065A:{p['pid']:04X}  {info['name']} [{p['port']}]")
    except Exception:  # pragma: no cover
        return []
    return lines


def resolve_port(explicit, any_port):
    """Pick the port to open, or fail loudly explaining what is attached."""
    if explicit:
        return explicit
    verified = find_scanner_ports()
    if verified:
        print(f"M-10 (065A:A002) verified on {verified[0]}", file=sys.stderr)
        return verified[0]
    if any_port:
        print(f"WARNING: opening {any_port} WITHOUT verifying it is an M-10 "
              f"(--any-port). A different Opticon personality uses different "
              f"line settings and a binary protocol; the result is garbage "
              f"that looks like barcodes, not an error.", file=sys.stderr)
        return any_port
    attached = describe_attached_opticon()
    raise SystemExit(
        "no M-10 in USB-COM mode (065A:A002) found.\n"
        + ("Opticon devices this host can see:\n" + "\n".join(attached) + "\n"
           if attached else "")
        + "Refusing to auto-pick a serial port: the OPN-2001 is 9600 8O1 and "
          "speaks a binary protocol, the M-10 is 9600 8N1 ASCII, and opening "
          "the wrong one yields garbage rather than an error.\n"
          "  · survey the host:  uv run python scripts/opticon_detect.py\n"
          "  · in USB-HID mode?  scan the *USB COM Port* configuration sheet "
          "so it re-enumerates as 065A:A002, or receive with "
          "scripts/scanner_hid.py\n"
          "  · sure about a port? pass --port PORT, or --any-port PORT to skip "
          "verification\n"
          "  · see docs/opticon-m10.md §3 and docs/opticon-hardware.md §1"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", default=os.environ.get("HCRM_SCANNER_PORT"),
                        help="serial port of a VERIFIED M-10 (default: auto-detect 065A:A002)")
    parser.add_argument("--any-port", metavar="PORT",
                        help="open PORT without verifying it is an M-10 (explicit opt-in)")
    parser.add_argument("--baudrate", type=int, default=9600,
                        help="RS-232C model only (default 9600, the factory setting)")
    parser.add_argument("--email", required=True, help="HCRM account email")
    parser.add_argument("--password",
                        help="omit to be prompted (recommended: the flag is visible "
                             "in the process list and in shell history)")
    parser.add_argument("--relogin", action="store_true",
                        help="re-authenticate once and retry on a 401 (an always-on "
                             "bridge outlives the 7-day token TTL)")
    parser.add_argument("--symbology", default="", metavar="SYM",
                        help="symbology the scanner reports for every scan (e.g. UPC-E). "
                             "Only needed if the device is configured to transmit a "
                             "symbology ID; without it a zero-suppressed UPC-E payload "
                             "is treated as an opaque code (app.barcodes refuses to "
                             "expand one on a guess)")
    parser.add_argument("--fuzzy", action="store_true",
                        help="fall back to the first keyword-search hit when no exact "
                             "identity matches (NOT allowed with --stock-in/--stock-out)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--stock-in", type=int, metavar="N",
                       help="add N to stock on every scan")
    group.add_argument("--stock-out", type=int, metavar="N",
                       help="subtract N from stock on every scan")
    parser.add_argument("--json", action="store_true",
                        help="emit one JSON object per scan instead of text")
    args = parser.parse_args()

    adjust = args.stock_in if args.stock_in else (-args.stock_out if args.stock_out else 0)
    if args.fuzzy and adjust:
        raise SystemExit(
            "--fuzzy cannot be combined with --stock-in/--stock-out: a keyword "
            "match is not an identity, and adjusting stock on a guess corrupts "
            "an unrelated product (REVIEW-m10.md P1). Drop --fuzzy, or drop the "
            "stock adjustment.")

    if args.password:
        print("warning: --password was given on the command line; it is visible "
              "in the process list and in shell history. Omit it to be prompted.",
              file=sys.stderr)

    credentials = {"email": args.email,
                   "password": args.password or getpass.getpass("HCRM password: ")}

    def login():
        # Raises ApiError, never SystemExit: login() is also the --relogin
        # callback, and that runs inside on_scan's `except ApiError`. Wrapping
        # it in SystemExit skipped the per-scan error record (only the driver's
        # BaseException net kept the thread alive) — a failed re-authentication
        # must be a reported scan failure like any other.
        _, auth = api("/api/auth/login", method="POST", body=credentials)
        return auth["token"], auth["user"]

    try:
        token, user = login()
    except ApiError as exc:
        # Startup: one clean line, as before (the ApiError carries the detail).
        raise SystemExit(str(exc))
    if adjust and user["role"] not in ("staff", "admin"):
        raise SystemExit("stock adjustment requires a staff or admin account")

    port = resolve_port(args.port, args.any_port)

    def emit(record):
        if args.json:
            print(json.dumps(record), flush=True)
            return
        if record.get("error"):
            print(f"[{record['code']}] ERROR {record['error']}", file=sys.stderr,
                  flush=True)
        elif record.get("badge_login") is not None:
            if record["badge_login"]:
                u = record["user"]
                print(f"[badge] logged in: {u['name']} <{u['email']}> ({u['role']})\n"
                      f"        session token: {record['token']}", flush=True)
            else:
                print(f"[badge] login failed: {record['error']}", flush=True)
        elif record["found"]:
            item = record["item"]
            line = (f"[{record['code']}] {item['name']} "
                    f"(SKU {item['sku']}, stock {item['stock']}, "
                    f"{item['price_cents'] / 100:.2f})"
                    f"  [match: {record['match']}]")
            if record.get("new_stock") is not None:
                line += f"  → stock {record['old_stock']} → {record['new_stock']}"
            if record.get("clamped"):
                line += "  (clamped at 0 — a sale beyond available stock)"
            print(line, flush=True)
        else:
            verdict = record.get("verdict") or "no catalogue match"
            print(f"[{record['code']}] {verdict}", flush=True)

    def call(path, method="GET", body=None):
        """api() with the P2 contract: one loud retry on 401 when --relogin."""
        nonlocal token
        result, token = call_with_relogin(
            api, lambda: login()[0], path, token, method=method, body=body,
            relogin=args.relogin)
        return result

    def on_scan(code):
        if code.startswith(BADGE_PREFIX):
            emit(badge_login(code))
            return
        record = {"code": code, "found": False, "match": None, "item": None}
        try:
            item, how, analysis = find_item(
                lambda path: call(path), code, fuzzy=args.fuzzy,
                symbology=args.symbology)
        except ApiError as exc:
            # Report and keep listening: a transient 500 or an expired token
            # must not turn an always-on counter bridge into a zombie.
            record["error"] = str(exc)
            emit(record)
            return
        record["found"] = item is not None
        record["match"] = how
        record["item"] = item
        record["analysis"] = {"kind": analysis.kind, "key": analysis.key,
                              "addon": analysis.addon}
        if item is None:
            record["verdict"] = {
                "invalid-gtin": "MISREAD — check digit fails; re-scan, do not file",
                "opaque": ("no catalogue match (opaque payload)"
                           + (" — if this is a UPC-E, the scanner must transmit "
                              "its symbology ID: re-run with --symbology UPC-E"
                              if code.isdigit() and len(code) in (6, 7, 8) else "")),
                "gtin": "valid GTIN, not in the catalogue — new-entry candidate",
                "restricted": "valid in-store GTIN, not in the catalogue — "
                              "new-entry candidate (restricted circulation)",
            }.get(analysis.kind, "no catalogue match")
            if analysis.kind in ("gtin", "restricted") and analysis.info:
                where = analysis.info.region or "unknown GS1 region"
                record["verdict"] += f" ({where}, {analysis.info.usage})"
            emit(record)
            return
        if adjust:
            try:
                # Atomic, SQL-side: no read-modify-write window for a second
                # bridge to lose an increment in, and the server reports the
                # applied delta and any clamp precisely (REVIEW-m10.md P6).
                _, result = call(f"/api/items/{item['id']}/stock-adjust",
                                 method="POST", body={"delta": adjust})
                record.update(
                    old_stock=result["stock"] - result["applied_delta"],
                    new_stock=result["stock"],
                    item={**item, "stock": result["stock"]},
                    clamped=result["clamped"])
            except ApiError as exc:
                record["error"] = f"matched, but the stock update failed: {exc}"
        emit(record)

    def on_error(exc):
        print(f"scanner error: {exc}", file=sys.stderr, flush=True)

    try:
        with OpticonM10(port, baudrate=args.baudrate) as scanner:
            print(f"Listening on {port} — scan a barcode (Ctrl-C to quit).",
                  file=sys.stderr)
            scanner.start_reading(on_scan, on_error=on_error)
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
    except SerialOpenError as exc:
        raise SystemExit(f"Cannot open scanner: {exc}")


if __name__ == "__main__":
    main()
