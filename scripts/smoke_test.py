"""Smoke test for the HCRM API (run against a live server).

Environment overrides:
  HCRM_BASE            target server (default http://localhost:8000)
  HCRM_ADMIN_EMAIL     admin login (default admin@shop.local)
  HCRM_ADMIN_PASSWORD  admin password (default changeme) — override this once
                       you have changed the default password, as instructed.

If the admin account still carries the must_change_password flag (default
or admin-issued password), the test temporarily sets a random password and
restores it at the end (printing the temporary one in case it dies mid-run).
"""
import json
import os
import secrets
import sys
import urllib.request

BASE = os.environ.get("HCRM_BASE", "http://localhost:8000")
ADMIN_EMAIL = os.environ.get("HCRM_ADMIN_EMAIL", "admin@shop.local")
ADMIN_PASSWORD = os.environ.get("HCRM_ADMIN_PASSWORD", "changeme")


def call(path, method="GET", token=None, body=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        headers={"Content-Type": "application/json"},
        data=None if body is None else json.dumps(body).encode(),
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def main():
    ok = True

    # login as admin
    status, data = call("/api/auth/login", "POST",
                        body={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert status == 200, data
    token = data["token"]
    temp_password = None
    if data["user"].get("must_change_password"):
        temp_password = "smoke-" + secrets.token_hex(8)
        status, err = call("/api/auth/change-password", "POST", token=token,
                           body={"current_password": ADMIN_PASSWORD,
                                 "new_password": temp_password})
        assert status == 204, err
        print(f"  [..] Admin password temporarily set to {temp_password} "
              "(restored at the end; note it down if this script dies)")

    def check(label, cond, extra=""):
        nonlocal ok
        print(f"  [{'OK' if cond else 'FAIL'}] {label} {extra}")
        ok = ok and cond

    print("== Cleanup leftovers from previous runs ==")
    status, existing = call("/api/items?limit=100", token=token)
    for i in existing:
        if i["sku"].startswith("EX-TST-") or i["sku"] == "X":
            call(f"/api/items/{i['id']}", "DELETE", token=token)
            print(f"  removed leftover {i['sku']}")

    print("== Semantic search ==")
    for query, expected_top in [
        ("something to keep my drinks warm on a hike", "Insulated Water Bottle"),
        ("tool for chopping vegetables", "Chef Knife 20 cm"),
        ("quiet typing accessory for the office", "Wireless Mouse"),
        ("book about improving daily routines", "Atomic Habits"),
    ]:
        status, results = call(
            "/api/items/vector-search?q=" + urllib.request.quote(query) + "&limit=5",
            token=token,
        )
        names = [r["name"] for r in results]
        check(f"'{query}'", status == 200 and expected_top in names[:3],
              f"-> top: {names[:3]}")

    print("== Listing / data explorer ==")
    status, items = call("/api/items?limit=100", token=token)
    check("list items", status == 200 and len(items) >= 12, f"({len(items)} items)")
    check("has_embedding flag", all("has_embedding" in i for i in items))
    status, cats = call("/api/items/categories", token=token)
    check("categories", status == 200 and len(cats) >= 5, f"-> {cats}")

    print("== Create item (auto-embedding) ==")
    test_sku = "EX-TST-1"
    status, item = call("/api/items", "POST", token=token, body={
        "sku": test_sku, "name": "Espresso Grinder",
        "description": "Burr coffee grinder with 40 settings for espresso and filter brewing.",
        "category": "Kitchen", "price_cents": 8900, "stock": 10,
    })
    check("create item", status == 201 and item["has_embedding"])

    print("== Update description (re-embedding) ==")
    status, item2 = call(f"/api/items/{item['id']}", "PATCH", token=token,
                         body={"description": "Manual hand-crank coffee grinder for camping."})
    check("update item", status == 200 and item2["has_embedding"])

    print("== Vector search finds the new item ==")
    status, results = call(
        "/api/items/vector-search?q=" + urllib.request.quote("portable coffee grinder for camping") + "&limit=5",
        token=token)
    names = [r["name"] for r in results]
    check("finds Espresso Grinder", "Espresso Grinder" in names[:3], f"-> {names[:3]}")

    print("== Permissions ==")
    status, _ = call("/api/items/vector-search?q=test")
    check("anonymous vector search blocked", status == 401)
    status, _ = call("/api/items", "POST", token=token,
                     body={"sku": "EX-TST-2", "name": "Temp check",
                           "description": "temporary", "price_cents": 1, "stock": 1})
    check("staff can still create", status == 201)
    if status == 201:
        # find and remove the temp item via search
        status2, found = call("/api/items?q=Temp%20check", token=token)
        if found:
            call(f"/api/items/{found[0]['id']}", "DELETE", token=token)

    print("== Cleanup ==")
    status, _ = call(f"/api/items/{item['id']}", "DELETE", token=token)
    check("delete test item", status == 204)

    if temp_password is not None:
        status, err = call("/api/auth/change-password", "POST", token=token,
                           body={"current_password": temp_password,
                                 "new_password": ADMIN_PASSWORD})
        check("restore admin password", status == 204,
              "" if status == 204 else f"(temp password was: {temp_password})")

    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
