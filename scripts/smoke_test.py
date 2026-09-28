#!/usr/bin/env python3
"""End-to-end smoke test against a live HCRM server.

Covers what the pytest suite cannot: real HTTP, real embeddings (optional),
real semantic ranking, and the permission surface as a client sees it.

Usage:
    # default: boot a throwaway server on a scratch data dir and test it
    uv run python scripts/smoke_test.py                        # no model: degraded
    uv run python scripts/smoke_test.py --model BAAI/bge-small-en-v1.5   # full

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
import sys

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
        report.check("semantic coverage was exercised", allow_degraded,
                     "(the model is unavailable and --allow-degraded was NOT passed: "
                     "losing semantic coverage must be a decision)")
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
        report.check("item-write coverage was exercised", allow_degraded,
                     "(--allow-degraded was NOT passed)")
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

    report.section("Cleanup (only what this run created)")
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
    if args.base and (args.model or args.keep):
        die("--model/--keep apply to the self-hosted server only.", 3)

    report = Reporter("SMOKE TEST")
    summary: dict = {"target": args.base or "self-hosted", "forced": bool(args.force)}

    if args.base and args.force:
        exit_code = _run(args.base, report, summary, args, external=True)
    else:
        with self_host(model=args.model, keep=args.keep,
                       pbkdf2_iterations=1000) as server:
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
