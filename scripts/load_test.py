#!/usr/bin/env python3
"""Load sanity check: concurrent logins must not lock the database.

Reproduces the incident scenario from the round-4 review: 60 concurrent bogus
logins (full-cost PBKDF2 — the effective iteration count is read from
/api/healthz and printed, because a latency budget is meaningless without it)
plus 20 concurrent authenticated catalogue reads. With the old global BEGIN
IMMEDIATE hook this produced 11x HTTP 500 ("database is locked") and a read p50
of ~5 s. Expected: zero 5xx, no "database is locked", read p50 under budget.

Usage:
    # default: boot a throwaway server on a scratch data dir, test it, tear it
    # down. Cannot touch a real deployment (PLAN.md P-3).
    uv run python scripts/load_test.py

    # against an existing server — explicit opt-in AND explicit credentials:
    HCRM_ADMIN_USERNAME=... HCRM_ADMIN_PASSWORD=... \\
        uv run python scripts/load_test.py --base http://host:8000 --force

Contract (each clause is a regression that actually shipped):
- the load phase feeds the exit code. A previous version discarded _run_load()'s
  return value, so a failing load still exited 0 (finding C).
- NO password restore, ever. Restoring re-armed the well-known `changeme`
  default with must_change_password cleared, i.e. the script left the server
  *less* safe than it found it (finding E), and an unconditional restore made a
  passing run exit 1 whenever the account was not flagged (finding D). If the
  bootstrap had to change a password, the final state is reported — loudly, and
  in --json — and left in place (PLAN.md P-1/P-2).

Exit codes: 0 pass · 1 checks failed · 2 target unreachable · 3 usage.
"""

import argparse
import json
import os
import statistics
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _hcrm import (  # noqa: E402
    EXIT_CHECKS_FAILED,
    EXIT_OK,
    EXIT_USAGE,
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

DEFAULT_LOGINS = 60
DEFAULT_READS = 20
DEFAULT_READ_P50_BUDGET = 1.0


def run_load(report: Reporter, base: str, token: str, n_logins: int, n_reads: int,
             budget: float) -> bool:
    """The load phase. Returns False if any assertion fails."""
    barrier = threading.Barrier(n_logins + n_reads)
    results: dict[str, list] = {"logins": [], "reads": []}
    lock = threading.Lock()

    def bogus_login(i: int) -> None:
        barrier.wait()
        status, body, elapsed = call(
            base, "/api/auth/login", "POST",
            body={"email": f"nobody-{i}@load.test", "password": "wrong-password"},
        )
        with lock:
            results["logins"].append((status, elapsed, detail_of(body)))

    def catalogue_read(i: int) -> None:
        barrier.wait()
        status, body, elapsed = call(
            base, f"/api/items?limit=24&offset={i}", token=token)
        with lock:
            results["reads"].append((status, elapsed, detail_of(body)))

    threads = ([threading.Thread(target=bogus_login, args=(i,)) for i in range(n_logins)]
               + [threading.Thread(target=catalogue_read, args=(i,)) for i in range(n_reads)])
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    ok = True
    for label, key, expected_status, expected_count in (
        ("bogus logins", "logins", 401, n_logins),
        ("catalogue reads", "reads", 200, n_reads),
    ):
        rows = results[key]
        statuses: dict[int, int] = {}
        for status, _, _ in rows:
            statuses[status] = statuses.get(status, 0) + 1
        latencies = sorted(elapsed for _, elapsed, _ in rows)
        p50 = statistics.median(latencies) if latencies else float("nan")
        worst = latencies[-1] if latencies else float("nan")
        locked = [body for _, _, body in rows if "locked" in body.lower()]
        fivexx = sorted(s for s in statuses if s >= 500)
        print(f"  {label}: statuses={statuses} p50={p50:.2f}s max={worst:.2f}s")

        ok &= report.check(f"{label}: every response is a clean {expected_status}",
                           statuses.get(expected_status) == expected_count,
                           f"(got {statuses})")
        ok &= report.check(f"{label}: no 5xx", not fivexx, f"(got {fivexx})")
        ok &= report.check(f"{label}: no 'database is locked'", not locked,
                           f"({len(locked)} occurrence(s))")
        if label == "catalogue reads":
            ok &= report.check(f"{label}: p50 under the {budget:.2f}s budget",
                               p50 <= budget, f"(p50={p50:.2f}s)")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=None,
                        help="target an existing server (requires --force)")
    parser.add_argument("--force", action="store_true",
                        help="allow targeting an existing server instead of "
                             "self-hosting a throwaway one")
    parser.add_argument("--logins", type=int, default=DEFAULT_LOGINS)
    parser.add_argument("--reads", type=int, default=DEFAULT_READS)
    parser.add_argument("--read-p50-budget", type=float, default=DEFAULT_READ_P50_BUDGET)
    parser.add_argument("--fast-hashing", action="store_true",
                        help="self-hosted only: cheap PBKDF2 (1000 iterations) for a "
                             "quick run — the report then says so, because the "
                             "latency numbers are not comparable to production")
    parser.add_argument("--model", default=None,
                        help="self-hosted only: HCRM_EMBED_MODEL (default: none, so "
                             "the boot is offline and fast)")
    parser.add_argument("--keep", action="store_true",
                        help="self-hosted only: keep the scratch data dir")
    parser.add_argument("--json", action="store_true", help="machine-readable summary")
    args = parser.parse_args(argv)

    if args.base and not args.force:
        die("--base targets an existing server; pass --force to confirm you mean "
            "it, and set HCRM_ADMIN_USERNAME/HCRM_ADMIN_PASSWORD. Without --force "
            "this script self-hosts a throwaway server so it cannot touch a real "
            "deployment.", EXIT_USAGE)
    if args.force and not args.base:
        die("--force is only meaningful together with --base.", EXIT_USAGE)
    if args.base and args.force and (args.fast_hashing or args.model or args.keep):
        die("--fast-hashing/--model/--keep apply to the self-hosted server only.",
            EXIT_USAGE)

    report = Reporter("LOAD TEST")
    summary: dict = {"target": args.base or "self-hosted", "forced": bool(args.force)}

    if args.base and args.force:
        exit_code = _run(args.base, report, summary, args, external=True)
    else:
        with self_host(model=args.model, keep=args.keep,
                       pbkdf2_iterations=1000 if args.fast_hashing else None) as server:
            summary["data_dir"] = server.data_dir
            exit_code = _run(server.base, report, summary, args, external=False)

    summary["exit_code"] = exit_code
    summary["passed"] = report.passed
    summary["failed"] = report.failed
    if args.json:
        print(json.dumps(summary, indent=2))
    return exit_code


def _run(base: str, report: Reporter, summary: dict, args, external: bool) -> int:
    info = healthz(base)
    if info is None:
        die(f"{base} did not answer /api/healthz", 2)
    iterations = info.get("pbkdf2_iterations")
    print(f"target:   {base}{' (EXTERNAL — --force)' if external else ' (self-hosted)'}")
    print(f"healthz:  vector={info.get('vector')} embeddings={info.get('embeddings')} "
          f"pbkdf2_iterations={iterations} insecure_dev_admin={info.get('insecure_dev_admin')}")
    summary["healthz"] = info
    if args.fast_hashing:
        print("  note: --fast-hashing — latency numbers are NOT production-comparable")

    creds = credentials_for_external() if external else credentials_for_self_hosted()
    token, final_password, changed = bootstrap_admin(base, creds)
    summary["password_changed"] = changed
    summary["final_password"] = final_password if changed else None

    report.section(f"Concurrent load ({args.logins} bogus logins + {args.reads} reads)")
    load_ok = run_load(report, base, token, args.logins, args.reads,
                       args.read_p50_budget)

    # The load phase feeds the exit code (finding C).
    exit_code = report.summary("LOAD TEST") if load_ok else EXIT_CHECKS_FAILED
    if not load_ok:
        print()
        print("LOAD TEST FAILED (load phase)")

    # P-2: never print the password on a self-hosted success path (the database
    # is about to be deleted); always print it when it was intentionally left
    # changed on somebody else's server.
    if changed:
        if external:
            print(f"\nFINAL STATE: the admin password was changed to proceed and is "
                  f"NOT reverted (that is the contract — restoring it would re-arm a "
                  f"well-known default). It is now:\n    {final_password}\n"
                  f"Re-run with HCRM_ADMIN_PASSWORD set to that value.")
        else:
            print("\nFINAL STATE: the scratch admin password was changed; the whole "
                  "database is discarded with the server.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
