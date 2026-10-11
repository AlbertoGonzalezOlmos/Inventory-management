"""Task C — checkout + purchase history.

A purchase is an explicit checkout: POST /api/orders snapshots the cart,
decrements stock with clamp-and-report, and clears the cart — ONE
transaction (the adjust_stock BEGIN IMMEDIATE precedent, extended to the
whole cart). Pinned here:

- clamp-not-veto: a shortfall sells what is available and the receipt
  records BOTH the ordered amount and the applied amount;
- the exact-to-zero boundary (applied == qty) is NOT a clamp — the guard
  against a future RETURNING-only "optimisation";
- the lock: two concurrent checkouts serialise — exactly one receipt, the
  loser sees an empty cart; the last-unit race reports {1, 0};
- history is snapshots: immune to price edits and item deletion.
"""

import threading

from sqlmodel import Session, select

from app.database import engine
from app.models import CartItem, Item, Purchase, PurchaseLine
from tests.conftest import auth, create_user

JOIN_TIMEOUT = 120


def _create_item(client, token, sku, *, price_cents=100, stock=5):
    r = client.post("/api/items", json={"sku": sku, "name": f"Item {sku}",
                                        "price_cents": price_cents, "stock": stock},
                    headers=auth(token))
    assert r.status_code == 201, r.text
    return r.json()


def _add(client, token, item_id, qty=1):
    r = client.post("/api/cart/items", json={"item_id": item_id, "qty": qty},
                    headers=auth(token))
    assert r.status_code == 200, r.text


def _checkout(client, token):
    return client.post("/api/orders", headers=auth(token))


def _stock(client, admin_token, item_id):
    return client.get(f"/api/items/{item_id}", headers=auth(admin_token)).json()["stock"]


# --- the happy path --------------------------------------------------------------


def test_checkout_snapshots_decrements_and_clears(client, admin_token):
    user, token = create_user(client, admin_token)
    a = _create_item(client, admin_token, "ORD-A", price_cents=250, stock=10)
    b = _create_item(client, admin_token, "ORD-B", price_cents=99, stock=3)
    _add(client, token, a["id"], qty=2)
    _add(client, token, b["id"], qty=1)

    r = _checkout(client, token)
    assert r.status_code == 201, r.text
    body = r.json()
    purchase = body["purchase"]
    assert purchase["total_cents"] == 250 * 2 + 99 * 1
    assert [line["qty"] for line in purchase["lines"]] == [2, 1]
    assert all(line["applied_qty"] == line["qty"] and not line["clamped"]
               for line in purchase["lines"])
    assert [line["line_total_cents"] for line in purchase["lines"]] == [500, 99]
    # Stock decremented exactly.
    assert _stock(client, admin_token, a["id"]) == 8
    assert _stock(client, admin_token, b["id"]) == 2
    # Cart cleared — and the emptied cart rides in the response.
    assert body["cart"] == {"lines": [], "total_qty": 0, "total_cents": 0}
    assert client.get("/api/cart", headers=auth(token)).json()["lines"] == []
    assert body["skipped"] == []


def test_empty_cart_is_400(client, admin_token):
    user, token = create_user(client, admin_token)
    r = _checkout(client, token)
    assert r.status_code == 400 and "empty" in r.json()["detail"].lower()


# --- clamp-not-veto ----------------------------------------------------------------


def test_clamp_records_both_amounts(client, admin_token):
    user, token = create_user(client, admin_token)
    item = _create_item(client, admin_token, "ORD-CLAMP", stock=3)
    _add(client, token, item["id"], qty=5)

    r = _checkout(client, token)
    assert r.status_code == 201, r.text
    line = r.json()["purchase"]["lines"][0]
    assert line["qty"] == 5            # the ORDERED amount is recorded
    assert line["applied_qty"] == 3    # …so is what was deliverable
    assert line["clamped"] is True
    assert line["line_total_cents"] == 100 * 3   # you pay for the applied amount
    assert r.json()["purchase"]["total_cents"] == 300
    assert _stock(client, admin_token, item["id"]) == 0


def test_exact_to_zero_is_not_a_clamp(client, admin_token):
    """The boundary pin: consuming EXACTLY the stock is not a clamp. This is
    the guard against a future 'optimisation' to a RETURNING-only update,
    which cannot distinguish exhausted from clamped."""
    user, token = create_user(client, admin_token)
    item = _create_item(client, admin_token, "ORD-EXACT", stock=3)
    _add(client, token, item["id"], qty=3)
    r = _checkout(client, token)
    line = r.json()["purchase"]["lines"][0]
    assert line["applied_qty"] == 3 and line["clamped"] is False


def test_zero_stock_sale_is_recorded_with_applied_zero(client, admin_token):
    user, token = create_user(client, admin_token)
    item = _create_item(client, admin_token, "ORD-ZERO", stock=0)
    _add(client, token, item["id"], qty=1)  # allowed: cart adds are stock-neutral
    r = _checkout(client, token)
    assert r.status_code == 201, r.text
    line = r.json()["purchase"]["lines"][0]
    assert line["applied_qty"] == 0 and line["clamped"] is True
    assert r.json()["purchase"]["total_cents"] == 0
    assert _stock(client, admin_token, item["id"]) == 0


# --- orphans -------------------------------------------------------------------------


def test_orphan_lines_are_skipped_and_cleaned(client, admin_token):
    user, token = create_user(client, admin_token)
    item = _create_item(client, admin_token, "ORD-KEEP", stock=5)
    _add(client, token, item["id"], qty=1)
    # An orphan line, as a non-API deletion path would leave behind.
    with Session(engine) as session:
        session.add(CartItem(user_id=user["id"], item_id=999_999, qty=2))
        session.commit()

    r = _checkout(client, token)
    assert r.status_code == 201, r.text
    assert r.json()["skipped"] == [999_999]
    assert [line["item_id"] for line in r.json()["purchase"]["lines"]] == [item["id"]]
    with Session(engine) as session:  # the orphan line is gone, not just hidden
        remaining = session.exec(select(CartItem).where(
            CartItem.user_id == user["id"])).all()
        assert remaining == []


def test_cart_of_only_orphans_is_400_and_cleaned(client, admin_token):
    user, token = create_user(client, admin_token)
    with Session(engine) as session:
        session.add(CartItem(user_id=user["id"], item_id=888_888, qty=1))
        session.commit()
    r = _checkout(client, token)
    assert r.status_code == 400
    with Session(engine) as session:
        assert session.exec(select(CartItem).where(
            CartItem.user_id == user["id"])).all() == []


# --- concurrency (the BEGIN IMMEDIATE pins) ----------------------------------------------


def _run_threads(workers):
    threads = [threading.Thread(target=worker) for worker in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOIN_TIMEOUT)
        assert not t.is_alive(), "a worker thread hung"


def test_concurrent_double_checkout_yields_exactly_one_purchase(client, admin_token):
    user, token = create_user(client, admin_token)
    item = _create_item(client, admin_token, "ORD-RACE1", stock=10)
    _add(client, token, item["id"], qty=2)
    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait()
        r = _checkout(client, token)
        results.append(r.status_code)

    _run_threads([worker, worker])
    assert sorted(results) == [201, 400]   # one receipt; the loser's cart is empty
    assert _stock(client, admin_token, item["id"]) == 8  # decremented ONCE
    with Session(engine) as session:
        purchases = session.exec(select(Purchase).where(
            Purchase.user_id == user["id"])).all()
        assert len(purchases) == 1


def test_concurrent_last_unit_race_reports_both_amounts(client, admin_token):
    alice, alice_token = create_user(client, admin_token)
    bob, bob_token = create_user(client, admin_token)
    item = _create_item(client, admin_token, "ORD-RACE2", stock=1)
    _add(client, alice_token, item["id"], qty=1)
    _add(client, bob_token, item["id"], qty=1)
    barrier = threading.Barrier(2)
    applied = []

    def worker(token):
        barrier.wait()
        r = _checkout(client, token)
        assert r.status_code == 201, r.text
        applied.append(r.json()["purchase"]["lines"][0]["applied_qty"])

    _run_threads([lambda: worker(alice_token), lambda: worker(bob_token)])
    assert sorted(applied) == [0, 1]        # one full sale, one honest shortfall
    assert _stock(client, admin_token, item["id"]) == 0


# --- history: snapshots survive catalogue changes ------------------------------------------


def test_price_snapshot_ignores_later_edits(client, admin_token):
    user, token = create_user(client, admin_token)
    item = _create_item(client, admin_token, "ORD-PRICE", price_cents=100)
    _add(client, token, item["id"], qty=1)
    _checkout(client, token)
    r = client.patch(f"/api/items/{item['id']}", json={"price_cents": 999},
                     headers=auth(admin_token))
    assert r.status_code == 200
    history = client.get("/api/orders/mine", headers=auth(token)).json()
    assert history[0]["lines"][0]["price_cents"] == 100
    assert history[0]["total_cents"] == 100


def test_receipt_uses_the_checkout_time_price(client, admin_token):
    """The snapshot is LIVE AT CHECKOUT, not at add-to-cart: a price edit
    between the two moves the receipt (the cart's add-time line_total is
    deliberately ignored)."""
    user, token = create_user(client, admin_token)
    item = _create_item(client, admin_token, "ORD-LIVE", price_cents=100)
    _add(client, token, item["id"], qty=2)
    r = client.patch(f"/api/items/{item['id']}", json={"price_cents": 150},
                     headers=auth(admin_token))
    assert r.status_code == 200
    r = _checkout(client, token)
    assert r.status_code == 201, r.text
    line = r.json()["purchase"]["lines"][0]
    assert line["price_cents"] == 150          # checkout-time price…
    assert line["line_total_cents"] == 300     # …not the add-time 100
    assert r.json()["purchase"]["total_cents"] == 300


def test_checkout_response_timestamp_matches_the_stored_row(client, admin_token):
    """One clock read for the row and the response: the checkout payload and
    an immediate GET /mine agree exactly (no fabricated created_at)."""
    user, token = create_user(client, admin_token)
    item = _create_item(client, admin_token, "ORD-TS")
    _add(client, token, item["id"])
    purchase = _checkout(client, token).json()["purchase"]
    history = client.get("/api/orders/mine", headers=auth(token)).json()
    assert history[0]["id"] == purchase["id"]
    assert history[0]["created_at"] == purchase["created_at"]


def test_history_survives_item_deletion(client, admin_token):
    user, token = create_user(client, admin_token)
    item = _create_item(client, admin_token, "ORD-GONE")
    _add(client, token, item["id"], qty=1)
    _checkout(client, token)
    assert client.delete(f"/api/items/{item['id']}",
                         headers=auth(admin_token)).status_code == 204
    history = client.get("/api/orders/mine", headers=auth(token)).json()
    assert history[0]["lines"][0]["name"] == "Item ORD-GONE"  # the snapshot
    assert history[0]["lines"][0]["item_id"] == item["id"]     # the weak ref


def test_mine_isolation_ordering_and_pagination(client, admin_token):
    alice, alice_token = create_user(client, admin_token)
    bob, bob_token = create_user(client, admin_token)
    a = _create_item(client, admin_token, "ORD-H1")
    b = _create_item(client, admin_token, "ORD-H2")
    _add(client, alice_token, a["id"]); _checkout(client, alice_token)
    _add(client, bob_token, b["id"]); _checkout(client, bob_token)
    _add(client, alice_token, b["id"]); _checkout(client, alice_token)

    history = client.get("/api/orders/mine", headers=auth(alice_token)).json()
    assert len(history) == 2
    assert all(line["item_id"] != b["id"] or p["id"] == history[0]["id"]
               for p in history for line in p["lines"])  # bob's order is absent
    assert history[0]["id"] > history[1]["id"]           # newest first
    page = client.get("/api/orders/mine?limit=1&offset=1",
                      headers=auth(alice_token)).json()
    assert [p["id"] for p in page] == [history[1]["id"]]


# --- staff view + cleanup + gates ------------------------------------------------------------


def test_staff_history_privilege_matrix(client, admin_token):
    member, _ = create_user(client, admin_token)
    staff, staff_token = create_user(client, admin_token, role="staff")
    item = _create_item(client, admin_token, "ORD-STAFF")
    # The member has one purchase.
    r = client.post("/api/auth/login", json={"email": member["email"],
                                             "password": "user-password-1"})
    member_token = r.json()["token"]
    _add(client, member_token, item["id"]); _checkout(client, member_token)

    r = client.get(f"/api/members/{member['id']}/orders", headers=auth(staff_token))
    assert r.status_code == 200 and len(r.json()) == 1
    # Staff never read an admin's history (the manage rule)…
    admins = client.get("/api/members", headers=auth(admin_token)).json()
    admin_id = [u for u in admins if u["role"] == "admin"][0]["id"]
    assert client.get(f"/api/members/{admin_id}/orders",
                      headers=auth(staff_token)).status_code == 403
    # …members cannot use the staff endpoint…
    assert client.get(f"/api/members/{member['id']}/orders",
                      headers=auth(member_token)).status_code == 403
    # …and unknown ids 404.
    assert client.get("/api/members/9999/orders",
                      headers=auth(staff_token)).status_code == 404


def test_delete_member_removes_purchases_and_lines(client, admin_token):
    member, member_token = create_user(client, admin_token)
    other, other_token = create_user(client, admin_token)
    item = _create_item(client, admin_token, "ORD-DEL")
    _add(client, member_token, item["id"], qty=2); _checkout(client, member_token)
    _add(client, other_token, item["id"], qty=1); _checkout(client, other_token)

    assert client.delete(f"/api/members/{member['id']}",
                         headers=auth(admin_token)).status_code == 204
    with Session(engine) as session:  # the twin cleanup pins
        assert session.exec(select(Purchase).where(
            Purchase.user_id == member["id"])).all() == []
        assert session.exec(select(PurchaseLine).join(Purchase).where(
            Purchase.user_id == member["id"])).all() == []
        # …and the OTHER user's history is untouched.
        assert len(session.exec(select(Purchase).where(
            Purchase.user_id == other["id"])).all()) == 1


def test_gates(client, admin_token):
    user, token = create_user(client, admin_token)
    from tests.test_cart import _flagged_token
    assert _checkout(client, None or "").status_code == 401
    flagged = _flagged_token(client, admin_token)
    assert _checkout(client, flagged).status_code == 403
    assert client.get("/api/orders/mine").status_code == 401
