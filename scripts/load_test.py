"""Load sanity check: concurrent logins must not lock the database.

Reproduces the incident scenario from the remediation review: 60 concurrent
bogus logins (full-cost PBKDF2, 600k iterations by default) plus 20
concurrent authenticated catalogue reads. With the old global BEGIN
IMMEDIATE hook this produced 11x HTTP 500 ("database is locked") and read
p50 of ~5 s. Expected now: zero 5xx and read p50 well under a second.

Usage:
    HCRM_DATA_DIR=$(mktemp -d) ./scripts/run.sh &   # separate terminal
    uv run python scripts/load_test.py              # or HCRM_BASE=... to target
"""
import json
import os
import secrets
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = os.environ.get("HCRM_BASE", "http://localhost:8000")
N_LOGINS = 60
N_READS = 20
ADMIN_EMAIL = "admin@shop.local"
ADMIN_DEFAULT_PASSWORD = "changeme"


def call(path, method="GET", token=None, body=None, timeout=30):
    req = urllib.request.Request(
        BASE + path, method=method,
        headers={"Content-Type": "application/json"},
        data=None if body is None else json.dumps(body).encode(),
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            payload = json.loads(res.read() or b"null")
            return res.status, payload, time.monotonic() - start
    except urllib.error.HTTPError as e:
        payload = e.read().decode(errors="replace")
        return e.code, payload, time.monotonic() - start


def main():
    ok = True

    # Bootstrap an admin session (the seeded admin must change its password;
    # the load test changes it to a random value and restores it at the end).
    status, data, _ = call("/api/auth/login", "POST",
                           body={"email": ADMIN_EMAIL, "password": ADMIN_DEFAULT_PASSWORD})
    if status == 401:
        print("Admin password is not the default — nothing to restore, but this "
              "script needs the default password to bootstrap; set it or edit "
              "ADMIN_DEFAULT_PASSWORD.")
        sys.exit(2)
    assert status == 200, data
    admin_token = data["token"]
    temp_password = "loadtest-" + secrets.token_hex(8)
    if data["user"].get("must_change_password"):
        status, err, _ = call("/api/auth/change-password", "POST", token=admin_token,
                              body={"current_password": ADMIN_DEFAULT_PASSWORD,
                                    "new_password": temp_password})
        assert status == 204, err
        print(f"  [..] Temporarily changed admin password for the test "
              f"(will be restored; if this script dies mid-run, it is: {temp_password})")
    try:
        _run_load(admin_token)
    finally:
        status, err, _ = call("/api/auth/change-password", "POST", token=admin_token,
                              body={"current_password": temp_password,
                                    "new_password": ADMIN_DEFAULT_PASSWORD})
        print("  [OK] Admin password restored" if status == 204
              else f"  [FAIL] Could not restore admin password: {err} (temp: {temp_password})")
        ok = ok and status == 204
    sys.exit(0 if ok else 1)


def _run_load(admin_token):
    ok = True
    barrier = threading.Barrier(N_LOGINS + N_READS)
    results = {"logins": [], "reads": []}
    lock = threading.Lock()

    def bogus_login(i):
        barrier.wait()
        status, body, elapsed = call(
            "/api/auth/login", "POST",
            body={"email": f"nobody-{i}@load.test", "password": "wrong-password"},
        )
        with lock:
            results["logins"].append((status, elapsed, str(body)))

    def catalogue_read(i):
        barrier.wait()
        status, body, elapsed = call(f"/api/items?limit=24&offset={i}", token=admin_token)
        with lock:
            results["reads"].append((status, elapsed, str(body)))

    threads = ([threading.Thread(target=bogus_login, args=(i,)) for i in range(N_LOGINS)]
               + [threading.Thread(target=catalogue_read, args=(i,)) for i in range(N_READS)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for name, rows in (("bogus logins", results["logins"]), ("catalogue reads", results["reads"])):
        statuses = {}
        for s, _, _ in rows:
            statuses[s] = statuses.get(s, 0) + 1
        latencies = sorted(e for _, e, _ in rows)
        p50 = statistics.median(latencies)
        locked = [b for _, _, b in rows if "locked" in b.lower()]
        errors_5xx = [s for s in statuses if s >= 500]
        print(f"  {name}: statuses={statuses} p50={p50:.2f}s max={latencies[-1]:.2f}s")
        if errors_5xx or locked:
            print(f"    FAIL: 5xx={errors_5xx} locked={len(locked)}")
            ok = False
        if name == "catalogue reads" and p50 > 1.0:
            print("    FAIL: read p50 above 1s budget")
            ok = False
        if name == "bogus logins" and statuses.get(401) != N_LOGINS:
            print("    FAIL: expected every bogus login to be a clean 401")
            ok = False
        if name == "catalogue reads" and statuses.get(200) != N_READS:
            print("    FAIL: expected every read to be a clean 200")
            ok = False

    print("\nLOAD TEST PASSED" if ok else "\nLOAD TEST FAILED")
    return ok


if __name__ == "__main__":
    main()
