#!/usr/bin/env python3
"""End-to-end smoke test against a live HCRM server.

Covers what the pytest suite cannot: real HTTP, real embeddings (optional),
real semantic ranking, and the permission surface as a client sees it.

Usage:
    # default: boot a throwaway server on a scratch data dir and test it
    uv run python scripts/smoke_test.py                        # no model: degraded
    uv run python scripts/smoke_test.py --model BAAI/bge-small-en-v1.5   # full
    uv run python scripts/smoke_test.py --fast-hashing         # cheap PBKDF2 (flagged)

    # against an existing server (explicit opt-in AND explicit credentials)
    HCRM_ADMIN_USERNAME=... HCRM_ADMIN_PASSWORD=... \\
        uv run python scripts/smoke_test.py --base http://host:8000 --force

Contract (each clause is a regression that actually shipped):
- **It reports, it never raises.** A previous version indexed a response that
  was an error dict and died with `TypeError: string indices must be integers`
  — no `[FAIL]` line, and because the password restore was not in a `finally`,
  the run left the only admin account on an unknown random password
  (PLAN-v2 §8.2 N4, B13). Every payload is shape-checked here.
- **No password restore, ever** (PLAN.md P-1/P-2): if the bootstrap had to
  change a password, the final state is reported and left in place.
- **Self-host by default**, so the default path cannot touch a real deployment.
- **Degraded is loud, not silent.** Without an embedding model the semantic
  checks are `[SKIP]`ped with the reason and the run exits **1** unless
  `--allow-degraded` is passed: losing semantic coverage must be a decision,
  not an accident.

Exit codes: 0 pass · 1 checks failed · 2 target unreachable · 3 usage.
"""

import argparse
import json
import os
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _hcrm import (  # noqa: E402
    NO_MODEL,
    Reporter,
    bootstrap_admin,
    call,
    credentials_for_external,
    credentials_for_self_hosted,
    detail_of,
    die,
    healthz,
    self_host,
)

TEST_SKU = "EX-TST-1"
CLEANUP_PREFIX = "EX-TST-"

# (query, an item that must appear in the top 3) — real semantic ranking.
SEMANTIC_CASES = [
    ("something to keep my drinks warm on a hike", "Insulated Water Bottle"),
    ("tool for chopping vegetables", "Chef Knife 20 cm"),
    ("quiet typing accessory for the office", "Wireless Mouse"),
    ("book about improving daily routines", "Atomic Habits"),
]


def as_list(payload) -> list | None:
    """A list payload, or None — never a dict we would iterate as keys (N4)."""
    return payload if isinstance(payload, list) else None


def as_dict(payload) -> dict | None:
    return payload if isinstance(payload, dict) else None


def names_of(payload) -> list[str]:
    rows = as_list(payload)
    if rows is None:
        return []
    return [str(r.get("name")) for r in rows if isinstance(r, dict)]


def run_checks(report: Reporter, base: str, token: str, degraded: bool,
               allow_degraded: bool, summary: dict) -> None:
    auth = {"token": token}

    def get(path):
        return call(base, path, token=token)

    def req(path, method, body=None):
        return call(base, path, method, token=token, body=body)

    report.section("Liveness")
    status, payload, _ = get("/api/healthz")
    info = as_dict(payload) or {}
    report.check("GET /api/healthz -> 200 ok", status == 200 and info.get("ok") is True,
                 f"(HTTP {status} {detail_of(payload)})")
    summary["healthz"] = info

    report.section("Listing / data explorer")
    status, payload, _ = get("/api/items?limit=100")
    rows = as_list(payload)
    if report.check("GET /api/items -> 200 with a list", status == 200 and rows is not None,
                    f"(HTTP {status} {detail_of(payload)})"):
        report.check("the catalogue has at least the 12 seeded items",
                     len(rows) >= 12, f"({len(rows)} items)")
        report.check("every item exposes has_embedding",
                     all(isinstance(r, dict) and "has_embedding" in r for r in rows))
    status, payload, _ = get("/api/items/categories")
    cats = as_list(payload)
    report.check("GET /api/items/categories -> 200 with >= 5 categories",
                 status == 200 and cats is not None and len(cats) >= 5,
                 f"(HTTP {status} {detail_of(payload) if cats is None else cats})")

    report.section("Keyword search semantics")
    status, payload, _ = get("/api/items?q=mouse")
    report.check("q=mouse finds the mouse", status == 200 and
                 any("Mouse" in n for n in names_of(payload)),
                 f"(HTTP {status} -> {names_of(payload)[:3]})")
    # LIKE wildcards must be matched literally, not as wildcards.
    status, payload, _ = get("/api/items?q=%25")
    wild = as_list(payload)
    if report.check("q=% is escaped (does not match the whole catalogue)",
                    status == 200 and wild is not None and 0 < len(wild) < 12,
                    f"(HTTP {status}, {len(wild) if wild is not None else '?'} rows)"):
        report.check("every q=% hit really contains a percent sign",
                     all("%" in (str(r.get("name", "")) + str(r.get("description", ""))
                                 + str(r.get("sku", ""))) for r in wild))

    report.section("Semantic search (needs the embedding model)")
    if degraded:
        for query, expected in SEMANTIC_CASES:
            report.skip(f"'{query}' -> {expected}",
                        "embedding model not loaded; pass --model BAAI/bge-small-en-v1.5")
        report.check("the degraded semantic run was explicitly allowed "
                     "(--allow-degraded)", allow_degraded,
                     "" if allow_degraded else
                     "(the model is unavailable and --allow-degraded was NOT passed: "
                     "losing semantic coverage must be a decision, not an accident)")
    else:
        for query, expected in SEMANTIC_CASES:
            status, payload, _ = get(
                "/api/items/vector-search?q=" + _quote(query) + "&limit=5")
            top = names_of(payload)
            report.check(f"'{query}' ranks {expected} in the top 3",
                         status == 200 and expected in top[:3],
                         f"(HTTP {status} -> top: {top[:3]})")

    report.section("Item lifecycle (staff write path)")
    created_id = None
    if degraded:
        # By design an item cannot be created while the model is down: it would
        # be invisible to semantic search, so the API returns 503.
        status, payload, _ = req("/api/items", "POST", {
            "sku": TEST_SKU, "name": "Espresso Grinder",
            "description": "Burr coffee grinder with 40 settings.",
            "category": "Kitchen", "price_cents": 8900, "stock": 10})
        report.check("POST /api/items is rejected with 503 while the model is down",
                     status == 503, f"(HTTP {status} {detail_of(payload)})")
        skus = _skus(base, token)
        report.check("nothing was persisted by the rejected POST",
                     TEST_SKU not in skus,
                     "" if TEST_SKU not in skus else f"({TEST_SKU} IS present)")
        report.skip("create/update/re-embed + vector-search visibility",
                    "requires the embedding model")
        report.check("the degraded item-write run was explicitly allowed "
                     "(--allow-degraded)", allow_degraded,
                     "" if allow_degraded else "(--allow-degraded was NOT passed)")
    else:
        status, payload, _ = req("/api/items", "POST", {
            "sku": TEST_SKU, "name": "Espresso Grinder",
            "description": "Burr coffee grinder with 40 settings for espresso "
                           "and filter brewing.",
            "category": "Kitchen", "price_cents": 8900, "stock": 10})
        item = as_dict(payload) or {}
        if report.check("POST /api/items -> 201 with an embedding",
                        status == 201 and item.get("has_embedding") is True,
                        f"(HTTP {status} {detail_of(payload)})"):
            created_id = item.get("id")
            summary["created_item_id"] = created_id

            status, payload, _ = req(f"/api/items/{created_id}", "PATCH", {
                "description": "Manual hand-crank coffee grinder for camping."})
            patched = as_dict(payload) or {}
            report.check("PATCH description re-embeds (has_embedding stays true)",
                         status == 200 and patched.get("has_embedding") is True,
                         f"(HTTP {status} {detail_of(payload)})")

            status, payload, _ = get(
                "/api/items/vector-search?q="
                + _quote("portable coffee grinder for camping") + "&limit=5")
            report.check("vector search finds the new item",
                         status == 200 and "Espresso Grinder" in names_of(payload)[:3],
                         f"(HTTP {status} -> {names_of(payload)[:3]})")

    report.section("Permissions")
    status, payload, _ = call(base, "/api/items/vector-search?q=testing")
    report.check("anonymous vector search -> 401", status == 401, f"(HTTP {status})")
    status, payload, _ = call(base, "/api/items")
    report.check("anonymous listing -> 401", status == 401, f"(HTTP {status})")
    status, payload, _ = call(base, "/api/members")
    report.check("anonymous member list -> 401", status == 401, f"(HTTP {status})")
    status, payload, _ = call(base, "/api/items", "POST",
                              body={"sku": "EX-TST-PERM", "name": "x", "price_cents": 1})
    report.check("anonymous item creation -> 401", status == 401, f"(HTTP {status})")

    report.section("Kiosk loop (badge / cart / scan / intake)")
    # A scratch member per run (unique email so re-runs against an external
    # target do not collide); deleted in Cleanup.
    kiosk_email = f"kiosk-smoke-{os.getpid()}-{int(time.time())}@example.com"
    kiosk_member_id = None
    scan_item_id = None
    status, payload, _ = call(base, "/api/auth/register", "POST",
                              body={"name": "Kiosk Smoke", "email": kiosk_email,
                                    "password": "kiosk-pass-123"})
    reg = as_dict(payload) or {}
    badge = as_dict(reg.get("qr_badge")) or {}
    member_token = reg.get("token")
    badge_payload = badge.get("payload") or ""
    if report.check("register -> 201 with a one-shot qr_badge",
                    status == 201 and bool(member_token)
                    and badge_payload.startswith("HCRM1:"),
                    f"(HTTP {status} {detail_of(payload)})"):
        status, payload, _ = call(base, "/api/auth/qr-login", "POST",
                                  body={"token": badge_payload})
        report.check("the creation badge qr-logins -> 200 + token",
                     status == 200 and bool((as_dict(payload) or {}).get("token")),
                     f"(HTTP {status} {detail_of(payload)})")
        status, payload, _ = call(base, "/api/auth/qr-identify", "POST",
                                  token=member_token, body={"token": badge_payload})
        report.check("qr-identify own badge -> {same_user: true}",
                     status == 200
                     and (as_dict(payload) or {}).get("same_user") is True,
                     f"(HTTP {status} {detail_of(payload)})")

    def mreq(path, method="GET", body=None):
        return call(base, path, method, token=member_token, body=body)

    # Scan-to-cart of a KNOWN GTIN needs an item filed under that barcode;
    # item creation requires the embedding model (503 while degraded).
    SCAN_GTIN = "08412345678905"        # canonical of EAN-13 8412345678905
    if degraded:
        report.skip("scan-to-cart of a known GTIN (needs a barcoded item)",
                    "embedding model not loaded; item creation is 503 by design")
    else:
        status, payload, _ = req("/api/items", "POST", {
            "sku": "EX-TST-SCAN", "name": "Kiosk Smoke Scannable",
            "category": "general", "price_cents": 123, "stock": 9,
            "barcode": SCAN_GTIN})
        scan_item = as_dict(payload) or {}
        if report.check("create a barcoded item for the scan path -> 201",
                        status == 201 and scan_item.get("barcode") == SCAN_GTIN,
                        f"(HTTP {status} {detail_of(payload)})"):
            scan_item_id = scan_item.get("id")
            status, payload, _ = mreq("/api/cart/scan", "POST",
                                      {"code": "8412345678905"})  # EAN-13 form
            scan_out = as_dict(payload) or {}
            report.check("POST /api/cart/scan (EAN-13 form) -> 200 added",
                         status == 200 and scan_out.get("status") == "added"
                         and scan_out.get("match") == "barcode",
                         f"(HTTP {status} {detail_of(payload)})")
            status, payload, _ = mreq("/api/cart/scan", "POST",
                                      {"code": SCAN_GTIN})  # canonical form
            cart = as_dict(as_dict(payload) or {}).get("cart") or {}
            report.check("re-scan increments (qty 2, one line, total follows)",
                         status == 200 and len(cart.get("lines", [])) == 1
                         and cart.get("total_qty") == 2
                         and cart.get("total_cents") == 246,
                         f"(HTTP {status} {detail_of(payload)})")

    # Misread: right shape, bad check digit — 422, nothing added.
    status, payload, _ = mreq("/api/cart/scan", "POST", {"code": "8412345678906"})
    misread = as_dict(as_dict(payload) or {}).get("detail") or {}
    report.check("a misread GTIN -> 422 {status: misread} (never filed)",
                 status == 422 and misread.get("status") == "misread",
                 f"(HTTP {status} {detail_of(payload)})")

    # Unknown valid GTIN -> 404 structured detail + suggestions; the intake
    # flow then files a ScanRequest (the member path, no embeddings needed).
    # 8412345000126 shares a >=6-significant-digit GS1 prefix with SCAN_GTIN,
    # so the prefix-sibling suggestion must surface the scannable item when
    # it exists.
    UNKNOWN_GTIN = "8412345000126"
    status, payload, _ = mreq("/api/cart/scan", "POST", {"code": UNKNOWN_GTIN})
    unknown = as_dict(as_dict(payload) or {}).get("detail") or {}
    unknown_suggestions = as_dict(unknown.get("suggestions")) or {}
    report.check("unknown valid GTIN -> 404 {status: unknown, catalogable}",
                 status == 404 and unknown.get("status") == "unknown"
                 and unknown.get("catalogable") is True,
                 f"(HTTP {status} {detail_of(payload)})")
    if scan_item_id is not None:
        sibling_names = [str(i.get("name"))
                         for i in (unknown_suggestions.get("by_prefix") or [])
                         if isinstance(i, dict)]
        report.check("prefix sibling suggests the same-company item",
                     "Kiosk Smoke Scannable" in sibling_names,
                     f"(by_prefix: {sibling_names})")
    status, payload, _ = mreq("/api/scan-requests", "POST", {
        "code": UNKNOWN_GTIN, "name": "Kiosk Smoke Unknown", "price_cents": 99})
    scan_req = as_dict(payload) or {}
    report.check("member files a ScanRequest -> 201 (open, gtin14 captured)",
                 status == 201 and scan_req.get("status") == "open"
                 and scan_req.get("gtin14") == "08412345000126",
                 f"(HTTP {status} {detail_of(payload)})")
    status, payload, _ = mreq("/api/scan-requests/mine")
    mine = as_list(payload) or []
    report.check("GET /api/scan-requests/mine lists the request",
                 status == 200 and any(r.get("code") == UNKNOWN_GTIN for r in mine),
                 f"(HTTP {status} {detail_of(payload)})")
    status, payload, _ = mreq("/api/auth/logoff-schedule", "POST")
    sched = as_dict(payload) or {}
    report.check("logoff-schedule -> 200 {logoff_deadline, grace_seconds}",
                 status == 200 and bool(sched.get("logoff_deadline"))
                 and isinstance(sched.get("grace_seconds"), int),
                 f"(HTTP {status} {detail_of(payload)})")
    status, payload, _ = mreq("/api/auth/logoff-cancel", "POST")
    report.check("logoff-cancel -> 204 (idempotent disarm)", status == 204,
                 f"(HTTP {status})")

    # --- Task F additions: re-view / change-phone / change-email /
    # checkout / history (the profile feature's public flows).
    status, payload, _ = mreq("/api/auth/qr-badge")   # GET (re-view)
    view = as_dict(payload) or {}
    report.check("GET /api/auth/qr-badge re-views the identical payload",
                 status == 200 and view.get("payload") == badge_payload
                 and view.get("generation") == 1
                 and view.get("status") == "ok",
                 f"(HTTP {status} {detail_of(payload)})")

    status, payload, _ = mreq("/api/auth/change-phone", "POST",
                              {"phone": "+34 600 123 456"})
    report.check("change-phone -> 200 with the normalised phone",
                 status == 200 and (as_dict(payload) or {}).get("phone") == "+34600123456",
                 f"(HTTP {status} {detail_of(payload)})")

    new_email = "kiosk-smoke-renamed-" + kiosk_email
    status, payload, _ = mreq("/api/auth/change-email", "POST",
                              {"new_email": new_email,
                               "current_password": "kiosk-pass-123"})
    email_ok = status == 200 and (as_dict(payload) or {}).get("email") == new_email
    report.check("change-email -> 200 (password-gated, revokes others)",
                 email_ok, f"(HTTP {status} {detail_of(payload)})")
    if email_ok:
        kiosk_email = new_email   # cleanup finds the member under the new email
        # The email change must NOT touch the badge (derivation keys on user_id).
        status, payload, _ = mreq("/api/auth/qr-badge")
        report.check("badge unchanged after the email change",
                     status == 200 and (as_dict(payload) or {}).get("payload") == badge_payload,
                     f"(HTTP {status} {detail_of(payload)})")

    # Checkout: the cart currently holds the scannable item x2 (skipped in
    # degraded mode — no barcoded item could be created), qty 2 vs stock 9.
    # The POST runs ONLY in the non-degraded branch: smoke can target an
    # external deployment (--base), where firing a checkout just to skip it
    # would be a real (if empty-cart) side effect.
    if degraded:
        report.skip("checkout (needs a barcoded item in the cart)",
                    "embedding model not loaded; nothing was scannable")
    else:
        status, payload, _ = mreq("/api/orders", "POST")
        order = as_dict(payload) or {}
        purchase = as_dict(order.get("purchase")) or {}
        lines = [as_dict(l) for l in (purchase.get("lines") or []) if isinstance(l, dict)]
        report.check("checkout -> 201 (snapshot, no clamp, cart emptied)",
                     status == 201 and purchase.get("total_cents") == 246
                     and len(lines) == 1 and lines[0].get("qty") == 2
                     and lines[0].get("applied_qty") == 2
                     and lines[0].get("clamped") is False
                     and (as_dict(order.get("cart")) or {}).get("lines") == [],
                     f"(HTTP {status} {detail_of(payload)})")
        status, payload, _ = mreq("/api/orders/mine")
        history = as_list(payload) or []
        report.check("GET /api/orders/mine lists the receipt",
                     status == 200 and any(
                         (as_dict(p) or {}).get("id") == purchase.get("id")
                         for p in history),
                     f"(HTTP {status} {detail_of(payload)})")

    status, payload, _ = get("/api/members")
    for m in (as_list(payload) or []):
        if isinstance(m, dict) and m.get("email") == kiosk_email:
            kiosk_member_id = m.get("id")

    # --- Round 4, T7: the scanner feed channel, hardware-free. The same
    # round trip the daemon (secret) and the kiosk SPA (token) perform,
    # against the live HTTP surface — including the Authorization-Bearer
    # credential form the SPA's api() helper uses. Registration is
    # loopback-gated, so a --base run from a remote host SKIPs cleanly.
    report.section("Scanner feed channel (round 4, hardware-free)")
    terminal_id = "smoke-counter-1"
    terminal_secret = secrets.token_urlsafe(32)
    status, payload, _ = req("/api/scanner/terminals", "POST",
                             {"terminal_id": terminal_id,
                              "secret": terminal_secret})
    gate = as_dict((as_dict(payload) or {}).get("detail")) or {}
    if status == 403 and gate.get("status") == "loopback_only":
        report.skip("the scanner feed channel",
                    "terminal registration is loopback-gated and this "
                    "target is remote (--base without HCRM_SCANNER_REGISTER_ANY)")
    elif report.check("POST /api/scanner/terminals -> 201 with a pairing code",
                      status == 201 and bool((as_dict(payload) or {}).get("pairing_code")),
                      f"(HTTP {status} {detail_of(payload)})"):
        pairing_code = (as_dict(payload) or {}).get("pairing_code")
        status, payload, _ = call(base, "/api/scanner/pair", "POST",
                                  body={"terminal_id": terminal_id,
                                        "code": pairing_code})
        paired = as_dict(payload) or {}
        if report.check("pair with the single-use code -> 200 read token",
                        status == 200 and bool(paired.get("token")),
                        f"(HTTP {status} {detail_of(payload)})"):
            feed_token = paired["token"]
            badge = "HCRM1:" + "S" * 43
            status, payload, _ = call(base, "/api/scanner/events", "POST",
                                      token=terminal_secret, body={"code": badge})
            report.check("ingest a badge scan with the SECRET -> 201 {seq}",
                         status == 201
                         and isinstance((as_dict(payload) or {}).get("seq"), int),
                         f"(HTTP {status} {detail_of(payload)})")
            status, payload, _ = call(base, "/api/scanner/events", "POST",
                                      token=terminal_secret,
                                      body={"code": "8412345678905"})
            report.check("ingest a barcode scan -> 201 {seq}",
                         status == 201
                         and isinstance((as_dict(payload) or {}).get("seq"), int),
                         f"(HTTP {status} {detail_of(payload)})")
            status, payload, _ = call(base, "/api/scanner/events",
                                      token=feed_token)
            polled = as_dict(payload) or {}
            events = [as_dict(e) or {} for e in as_list(polled.get("events")) or []]
            report.check("poll with the TOKEN -> both scans, kinds SERVER-derived",
                         status == 200
                         and [(e.get("code"), e.get("kind")) for e in events]
                             == [(badge, "badge"), ("8412345678905", "barcode")]
                         and isinstance(polled.get("latest_seq"), int),
                         f"(HTTP {status} {detail_of(payload)})")
            status, payload, _ = call(base, "/api/scanner/events",
                                      token=feed_token)
            report.check("second poll -> empty (at-most-once delivery)",
                         status == 200 and (as_dict(payload) or {}).get("events") == [],
                         f"(HTTP {status} {detail_of(payload)})")
            status, payload, _ = call(base, "/api/scanner/events", "POST",
                                      token=feed_token, body={"code": "EX-007"})
            report.check("the read TOKEN cannot ingest -> 401 (capability split)",
                         status == 401, f"(HTTP {status} {detail_of(payload)})")
            status, payload, _ = req(f"/api/scanner/terminals/{terminal_id}",
                                     "DELETE")
            report.check("DELETE the terminal (staff) -> 204", status == 204,
                         f"(HTTP {status} {detail_of(payload)})")
            status, payload, _ = call(base, "/api/scanner/events",
                                      token=feed_token)
            report.check("the token died with the terminal -> 401",
                         status == 401, f"(HTTP {status} {detail_of(payload)})")

    report.section("Cleanup (only what this run created)")
    if scan_item_id is not None:
        status, payload, _ = req(f"/api/items/{scan_item_id}", "DELETE")
        report.check("DELETE the barcoded scan item -> 204", status == 204,
                     f"(HTTP {status} {detail_of(payload)})")
    if kiosk_member_id is not None:
        status, payload, _ = req(f"/api/members/{kiosk_member_id}", "DELETE")
        report.check("DELETE the scratch kiosk member -> 204 "
                     "(cart rows and scan requests go with it)", status == 204,
                     f"(HTTP {status} {detail_of(payload)})")
    if created_id is not None:
        status, payload, _ = req(f"/api/items/{created_id}", "DELETE")
        report.check("DELETE the item this run created -> 204", status == 204,
                     f"(HTTP {status} {detail_of(payload)})")
    else:
        report.skip("delete the created item", "nothing was created on this run")
    leftovers = [sku for sku in _skus(base, token) if sku.startswith(CLEANUP_PREFIX)]
    report.check(f"no {CLEANUP_PREFIX}* leftovers remain", not leftovers,
                 f"({leftovers})" if leftovers else "")
    # NOTE: the previous version also deleted any item whose SKU was exactly
    # "X" — a real catalogue may legitimately have that SKU. Gone.


def _quote(text: str) -> str:
    import urllib.parse
    return urllib.parse.quote(text)


def _skus(base: str, token: str) -> list[str]:
    status, payload, _ = call(base, "/api/items?limit=100", token=token)
    rows = as_list(payload)
    if rows is None:
        return []
    return [str(r.get("sku")) for r in rows if isinstance(r, dict)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=None,
                        help="target an existing server (requires --force)")
    parser.add_argument("--force", action="store_true",
                        help="allow targeting an existing server")
    parser.add_argument("--model", default=None,
                        help=f"self-hosted only: HCRM_EMBED_MODEL (default {NO_MODEL}, "
                             f"i.e. no model — offline boot, semantic checks skipped)")
    parser.add_argument("--fast-hashing", action="store_true",
                        help="self-hosted only: cheap PBKDF2 (1000 iterations) for a "
                             "quick run — the report then says so, because the "
                             "numbers are not comparable to production")
    parser.add_argument("--allow-degraded", action="store_true",
                        help="exit 0 even though the embedding model is unavailable "
                             "and the semantic checks were skipped")
    parser.add_argument("--keep", action="store_true",
                        help="self-hosted only: keep the scratch data dir")
    parser.add_argument("--json", action="store_true", help="machine-readable summary")
    args = parser.parse_args(argv)

    if args.base and not args.force:
        die("--base targets an existing server; pass --force to confirm, and set "
            "HCRM_ADMIN_USERNAME/HCRM_ADMIN_PASSWORD. Without --force this script "
            "self-hosts a throwaway server so it cannot touch a real deployment.", 3)
    if args.force and not args.base:
        die("--force is only meaningful together with --base.", 3)
    if args.base and (args.model or args.keep or args.fast_hashing):
        die("--model/--keep/--fast-hashing apply to the self-hosted server only.", 3)

    report = Reporter("SMOKE TEST")
    summary: dict = {"target": args.base or "self-hosted", "forced": bool(args.force)}

    if args.base and args.force:
        exit_code = _run(args.base, report, summary, args, external=True)
    else:
        # pbkdf2_iterations=None keeps the app's real (expensive) default;
        # 1000 is an explicit, reported trade-off (mirrors load_test.py).
        with self_host(model=args.model, keep=args.keep,
                       pbkdf2_iterations=1000 if args.fast_hashing else None) as server:
            summary["data_dir"] = server.data_dir
            summary["model"] = args.model or NO_MODEL
            exit_code = _run(server.base, report, summary, args, external=False)

    summary["exit_code"] = exit_code
    summary["passed"] = report.passed
    summary["failed"] = report.failed
    summary["skipped"] = report.skipped
    if args.json:
        print(json.dumps(summary, indent=2))
    return exit_code


def _run(base: str, report: Reporter, summary: dict, args, external: bool) -> int:
    info = healthz(base)
    if info is None:
        die(f"{base} did not answer /api/healthz", 2)
    degraded = info.get("embeddings") != "ready"
    print(f"target:   {base}{' (EXTERNAL — --force)' if external else ' (self-hosted)'}")
    print(f"healthz:  vector={info.get('vector')} embeddings={info.get('embeddings')} "
          f"pbkdf2_iterations={info.get('pbkdf2_iterations')}")
    if degraded:
        print("  NOTE: the embedding model is not loaded — semantic checks will be "
              "SKIPPED and this run fails unless --allow-degraded is given.")
    if args.fast_hashing:
        print("  note: --fast-hashing — latency numbers are NOT production-comparable")

    creds = credentials_for_external() if external else credentials_for_self_hosted()
    token, final_password, changed = bootstrap_admin(base, creds)
    summary["password_changed"] = changed
    summary["final_password"] = final_password if changed else None
    summary["degraded"] = degraded

    run_checks(report, base, token, degraded, args.allow_degraded, summary)

    exit_code = report.summary("SMOKE TEST")
    if changed:
        if external:
            print(f"\nFINAL STATE: the admin password was changed to proceed and is "
                  f"NOT reverted (restoring it would re-arm a well-known default). "
                  f"It is now:\n    {final_password}\n"
                  f"Re-run with HCRM_ADMIN_PASSWORD set to that value.")
        else:
            print("\nFINAL STATE: the scratch admin password was changed; the whole "
                  "database is discarded with the server.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
