"""Server-side per-user cart: /api/cart CRUD, isolation, cleanup, concurrency.

Pins the Task-1 guarantees (docs/plans/task1-FINAL.md):

- the cart is per-user (the old localStorage list leaked across users) and
  durable (every mutation is its own committed request — the foundation the
  badge account-switch "save then switch" flow is built on);
- add-or-increment is a single-statement upsert: the existence check is
  folded into the write and concurrent adds cannot lose one (the same bug
  class stock-adjust was rebuilt to close, REVIEW-m10.md P6);
- prices are LIVE (no snapshot column) and stock is never mutated;
- delete paths clean up cart rows explicitly (FKs are declarative only) and
  reads lazily reap anything orphaned by other paths;
- PUT is all-or-nothing: a failed replace never destroys the saved cart.
"""

import threading

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.database import engine
from app.models import CartItem
from tests.conftest import auth, create_user, unique_email

JOIN_TIMEOUT = 120


def _create_item(client, token, sku, *, price_cents=100, stock=5):
    r = client.post(
        "/api/items",
        json={
            "sku": sku,
            "name": f"Item {sku}",
            "price_cents": price_cents,
            "stock": stock,
        },
        headers=auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _add(client, token, item_id, qty=1):
    return client.post(
        "/api/cart/items",
        json={"item_id": item_id, "qty": qty},
        headers=auth(token),
    )


def _cart(client, token):
    r = client.get("/api/cart", headers=auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def _flagged_token(client, admin_token):
    """A token whose account still has must_change_password set (staff-issued,
    password change NOT performed) — the conftest create_user helper clears
    the flag, so build the state by hand."""
    email = unique_email()
    r = client.post(
        "/api/members",
        json={
            "name": "Flagged User",
            "email": email,
            "password": "password123",
            "role": "member",
        },
        headers=auth(admin_token),
    )
    assert r.status_code == 201, r.text
    login = client.post(
        "/api/auth/login", json={"email": email, "password": "password123"}
    )
    assert login.status_code == 200, login.text
    return login.json()["token"]


# --- auth gating -----------------------------------------------------------


def test_unauthenticated_is_401(client):
    cases = [
        ("get", "/api/cart"),
        ("post", "/api/cart/items", {"item_id": 1}),
        ("patch", "/api/cart/items/1", {"qty": 1}),
        ("delete", "/api/cart/items/1"),
        ("put", "/api/cart", {"lines": []}),
        ("delete", "/api/cart"),
    ]
    for case in cases:
        method, path = case[0], case[1]
        body = case[2] if len(case) > 2 else None
        # client.request, not the per-method shortcuts: httpx's get/delete
        # helpers do not accept a json body parameter.
        r = client.request(method, path, json=body)
        assert r.status_code == 401, (method, path, r.status_code)


def test_must_change_password_is_403(client, admin_token):
    token = _flagged_token(client, admin_token)
    cases = [
        ("get", "/api/cart"),
        ("post", "/api/cart/items", {"item_id": 1}),
        ("patch", "/api/cart/items/1", {"qty": 1}),
        ("delete", "/api/cart/items/1"),
        ("put", "/api/cart", {"lines": []}),
        ("delete", "/api/cart"),
    ]
    for case in cases:
        method, path = case[0], case[1]
        body = case[2] if len(case) > 2 else None
        r = client.request(method, path, json=body, headers=auth(token))
        assert r.status_code == 403, (method, path, r.status_code)


# --- add / increment -------------------------------------------------------


def test_add_and_list(client, admin_token):
    user, token = create_user(client, admin_token)
    item_id = _create_item(client, admin_token, "CART-ADD-1", price_cents=250)

    r = _add(client, token, item_id, qty=2)
    assert r.status_code == 200, r.text
    cart = r.json()
    assert len(cart["lines"]) == 1
    line = cart["lines"][0]
    assert line["item"]["id"] == item_id
    assert line["qty"] == 2
    assert line["line_total_cents"] == 500
    assert cart["total_qty"] == 2
    assert cart["total_cents"] == 500
    # The mutator's response is the cart (one contract, no read-after-write).
    assert cart == _cart(client, token)


def test_readd_increments_never_409(client, admin_token):
    user, token = create_user(client, admin_token)
    item_id = _create_item(client, admin_token, "CART-INC-1")

    assert _add(client, token, item_id).status_code == 200
    r = _add(client, token, item_id, qty=2)
    assert r.status_code == 200, r.text
    cart = r.json()
    assert len(cart["lines"]) == 1  # one line, not a duplicate
    assert cart["lines"][0]["qty"] == 3


def test_add_unknown_item_404(client, admin_token):
    user, token = create_user(client, admin_token)
    r = _add(client, token, 999_999)
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Item not found"
    assert _cart(client, token)["lines"] == []


def test_add_qty_bounds_422(client, admin_token):
    user, token = create_user(client, admin_token)
    item_id = _create_item(client, admin_token, "CART-BOUND-1")
    for bad in (0, -1, 10_001):
        r = _add(client, token, item_id, qty=bad)
        assert r.status_code == 422, (bad, r.text)
    # Unknown fields are rejected like everywhere else (extra="forbid").
    r = client.post(
        "/api/cart/items",
        json={"item_id": item_id, "qty": 1, "price_cent": 100},
        headers=auth(token),
    )
    assert r.status_code == 422, r.text


def test_accumulation_caps_at_the_bound(client, admin_token):
    """The upsert's min(qty + excluded.qty, cap) is the abuse bound.

    Per-request qtys are schema-bounded, but accumulation across requests
    happens inside the SQL statement — so the clamp must live there too.
    Clamping is reported the repo's way (stock-adjust idiom): no error, the
    response shows the resulting capped line.
    """
    user, token = create_user(client, admin_token)
    item_id = _create_item(client, admin_token, "CART-CAP-1")

    r = _add(client, token, item_id, qty=10_000)
    assert r.status_code == 200 and r.json()["lines"][0]["qty"] == 10_000

    r = _add(client, token, item_id, qty=10_000)  # would accumulate to 20k
    assert r.status_code == 200, r.text
    assert r.json()["lines"][0]["qty"] == 10_000  # clamped, still one line

    r = _add(client, token, item_id, qty=1)  # at the cap: a reported no-op
    assert r.status_code == 200, r.text
    assert r.json()["total_qty"] == 10_000


# --- set absolute ----------------------------------------------------------


def test_set_absolute_and_zero_removes(client, admin_token):
    user, token = create_user(client, admin_token)
    item_id = _create_item(client, admin_token, "CART-SET-1")
    _add(client, token, item_id, qty=2)

    r = client.patch(
        f"/api/cart/items/{item_id}", json={"qty": 5}, headers=auth(token)
    )
    assert r.status_code == 200, r.text
    assert r.json()["lines"][0]["qty"] == 5

    r = client.patch(
        f"/api/cart/items/{item_id}", json={"qty": 0}, headers=auth(token)
    )
    assert r.status_code == 200, r.text
    assert r.json()["lines"] == []


def test_patch_never_creates_and_404_when_absent(client, admin_token):
    user, token = create_user(client, admin_token)
    item_id = _create_item(client, admin_token, "CART-SET-2")
    r = client.patch(
        f"/api/cart/items/{item_id}", json={"qty": 3}, headers=auth(token)
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Item not in cart"
    assert _cart(client, token)["lines"] == []  # nothing was created


# --- delete line / clear ---------------------------------------------------


def test_delete_line_then_404(client, admin_token):
    user, token = create_user(client, admin_token)
    item_id = _create_item(client, admin_token, "CART-DEL-1")
    _add(client, token, item_id)

    r = client.delete(f"/api/cart/items/{item_id}", headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["lines"] == []

    r = client.delete(f"/api/cart/items/{item_id}", headers=auth(token))
    assert r.status_code == 404, r.text


def test_clear_is_idempotent(client, admin_token):
    user, token = create_user(client, admin_token)
    _add(client, token, _create_item(client, admin_token, "CART-CLR-1"))

    for _ in range(2):  # non-empty, then already-empty
        r = client.delete("/api/cart", headers=auth(token))
        assert r.status_code == 200, r.text
        assert r.json() == {"lines": [], "total_qty": 0, "total_cents": 0}


# --- replace (PUT) ---------------------------------------------------------


def test_replace_wholesale_and_empty_clears(client, admin_token):
    user, token = create_user(client, admin_token)
    old = _create_item(client, admin_token, "CART-REP-1")
    new = _create_item(client, admin_token, "CART-REP-2", price_cents=300)
    _add(client, token, old, qty=4)

    r = client.put(
        "/api/cart",
        json={"lines": [{"item_id": new, "qty": 2}]},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text
    cart = r.json()
    assert [line["item"]["id"] for line in cart["lines"]] == [new]
    assert cart["total_cents"] == 600

    r = client.put("/api/cart", json={"lines": []}, headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["lines"] == []


def test_replace_unknown_item_leaves_cart_byte_identical(client, admin_token):
    user, token = create_user(client, admin_token)
    item_id = _create_item(client, admin_token, "CART-REP-3", price_cents=150)
    _add(client, token, item_id, qty=2)
    before = _cart(client, token)

    r = client.put(
        "/api/cart",
        json={
            "lines": [
                {"item_id": item_id, "qty": 1},
                {"item_id": 999_999, "qty": 1},
            ]
        },
        headers=auth(token),
    )
    assert r.status_code == 404, r.text
    assert _cart(client, token) == before  # nothing was written


def test_replace_duplicate_ids_422(client, admin_token):
    user, token = create_user(client, admin_token)
    item_id = _create_item(client, admin_token, "CART-REP-4")
    r = client.put(
        "/api/cart",
        json={
            "lines": [
                {"item_id": item_id, "qty": 1},
                {"item_id": item_id, "qty": 2},
            ]
        },
        headers=auth(token),
    )
    assert r.status_code == 422, r.text


def test_replace_with_overlapping_lines(client, admin_token):
    """A PUT whose lines reuse existing (user_id, item_id) pairs must work.

    Regression: SQLAlchemy's flush emits INSERTs before DELETEs for the same
    table, so a delete-then-add replace of an overlapping pair collided with
    uq_cart_items_user_item (IntegrityError → 500). Overlapping replaces are
    the common flush shape — the account-switch flush and the localStorage
    import keep most of their lines — so this is the PUT case that must never
    break. The router now emits the delete as its own statement first.
    """
    user, token = create_user(client, admin_token)
    keep = _create_item(client, admin_token, "CART-REP-5", price_cents=100)
    drop = _create_item(client, admin_token, "CART-REP-6", price_cents=200)
    fresh = _create_item(client, admin_token, "CART-REP-7", price_cents=300)
    _add(client, token, keep, qty=2)
    _add(client, token, drop, qty=1)

    r = client.put(
        "/api/cart",
        json={"lines": [{"item_id": keep, "qty": 5},
                        {"item_id": fresh, "qty": 3}]},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text
    cart = r.json()
    assert {line["item"]["id"]: line["qty"] for line in cart["lines"]} == {
        keep: 5, fresh: 3,
    }
    assert cart["total_qty"] == 8
    assert cart["total_cents"] == 5 * 100 + 3 * 300

    # A full-overlap replace (the identical cart again) is fine too — the
    # idempotent flush a nervous SPA might send twice.
    r = client.put(
        "/api/cart",
        json={"lines": [{"item_id": keep, "qty": 5},
                        {"item_id": fresh, "qty": 3}]},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["total_qty"] == 8
    assert _cart(client, token) == r.json()


def test_replace_line_count_bounds(client, admin_token):
    """PUT is bounded at 200 lines (a cart, not an import job).

    Discriminator without creating 200 items: pydantic's max_length fires
    before any router logic, so 201 (even fictitious) lines are 422 while
    200 fictitious lines get through to the existence check and are 404.
    """
    user, token = create_user(client, admin_token)
    fictitious = lambda n: {"lines": [{"item_id": 900_000 + i, "qty": 1}
                                      for i in range(n)]}

    r = client.put("/api/cart", json=fictitious(201), headers=auth(token))
    assert r.status_code == 422, r.text

    r = client.put("/api/cart", json=fictitious(200), headers=auth(token))
    assert r.status_code == 404, r.text  # length accepted; items are missing

    assert _cart(client, token)["lines"] == []  # neither wrote anything


# --- live price / stock pins ------------------------------------------------


def test_live_price_pin(client, admin_token):
    user, token = create_user(client, admin_token)
    item_id = _create_item(client, admin_token, "CART-PRICE-1", price_cents=100)
    _add(client, token, item_id, qty=2)
    assert _cart(client, token)["total_cents"] == 200

    r = client.patch(
        f"/api/items/{item_id}",
        json={"price_cents": 250},
        headers=auth(admin_token),
    )
    assert r.status_code == 200, r.text

    cart = _cart(client, token)
    assert cart["lines"][0]["line_total_cents"] == 500
    assert cart["total_cents"] == 500  # follows the current price, no snapshot


def test_stock_untouched_pin(client, admin_token):
    user, token = create_user(client, admin_token)
    item_id = _create_item(client, admin_token, "CART-STOCK-1", stock=3)

    r = _add(client, token, item_id, qty=10)  # qty > stock is allowed
    assert r.status_code == 200, r.text

    detail = client.get(f"/api/items/{item_id}", headers=auth(token)).json()
    assert detail["stock"] == 3  # never decremented, never reserved
    assert r.json()["lines"][0]["item"]["stock"] == 3  # live stock for the UI


# --- per-user isolation ------------------------------------------------------


def test_per_user_isolation(client, admin_token):
    alice, alice_token = create_user(client, admin_token)
    bob, bob_token = create_user(client, admin_token)
    item_id = _create_item(client, admin_token, "CART-ISO-1")
    _add(client, alice_token, item_id, qty=3)

    # Bob sees nothing of Alice's cart…
    assert _cart(client, bob_token)["lines"] == []
    # …cannot modify or delete her line (404, not 403: no existence leak)…
    r = client.patch(
        f"/api/cart/items/{item_id}", json={"qty": 1}, headers=auth(bob_token)
    )
    assert r.status_code == 404, r.text
    r = client.delete(f"/api/cart/items/{item_id}", headers=auth(bob_token))
    assert r.status_code == 404, r.text
    # …and a clear by Bob leaves her cart intact.
    client.delete("/api/cart", headers=auth(bob_token))
    assert _cart(client, alice_token)["total_qty"] == 3


# --- explicit cleanup on delete ----------------------------------------------


def test_item_delete_cleans_both_carts(client, admin_token):
    alice, alice_token = create_user(client, admin_token)
    bob, bob_token = create_user(client, admin_token)
    item_id = _create_item(client, admin_token, "CART-ITEMDEL-1")
    survivor_id = _create_item(client, admin_token, "CART-ITEMDEL-2")
    _add(client, alice_token, item_id)
    _add(client, alice_token, survivor_id, qty=2)
    _add(client, bob_token, item_id, qty=5)

    r = client.delete(f"/api/items/{item_id}", headers=auth(admin_token))
    assert r.status_code == 204, r.text

    alice_cart = _cart(client, alice_token)
    assert [line["item"]["id"] for line in alice_cart["lines"]] == [survivor_id]
    assert _cart(client, bob_token)["lines"] == []


def test_member_delete_cleans_cart_rows(client, admin_token):
    member, member_token = create_user(client, admin_token)
    other, other_token = create_user(client, admin_token)
    item_id = _create_item(client, admin_token, "CART-USERDEL-1")
    _add(client, member_token, item_id, qty=2)
    _add(client, other_token, item_id, qty=1)

    r = client.delete(f"/api/members/{member['id']}", headers=auth(admin_token))
    assert r.status_code == 204, r.text

    with Session(engine) as session:  # direct DB check, not via the API
        gone = session.exec(
            select(CartItem).where(CartItem.user_id == member["id"])
        ).all()
        assert gone == []
        surviving = session.exec(
            select(CartItem).where(CartItem.user_id == other["id"])
        ).all()
        assert len(surviving) == 1  # a shared item's other lines survive


# --- orphan reap --------------------------------------------------------------


def test_get_reaps_orphan_lines(client, admin_token):
    user, token = create_user(client, admin_token)
    item_id = _create_item(client, admin_token, "CART-ORPHAN-1")
    _add(client, token, item_id, qty=2)

    # Simulate a non-API deletion path (hand-written SQL / a race): a cart
    # row pointing at an item that no longer exists.
    with Session(engine) as session:
        session.add(CartItem(user_id=user["id"], item_id=999_999, qty=7))
        session.commit()

    cart = _cart(client, token)
    assert [line["item"]["id"] for line in cart["lines"]] == [item_id]
    assert cart["total_qty"] == 2  # the orphan contributed nothing

    with Session(engine) as session:  # …and was deleted, not just hidden
        orphans = session.exec(
            select(CartItem).where(CartItem.item_id == 999_999)
        ).all()
        assert orphans == []


# --- unique constraint (behavioural) ------------------------------------------


def test_unique_user_item_constraint(client, admin_token):
    user, token = create_user(client, admin_token)
    item_id = _create_item(client, admin_token, "CART-UQ-1")
    with Session(engine) as session:
        session.add(CartItem(user_id=user["id"], item_id=item_id, qty=1))
        session.commit()
        session.add(CartItem(user_id=user["id"], item_id=item_id, qty=1))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


# --- concurrency ----------------------------------------------------------------


def _run_threads(workers):
    threads = [threading.Thread(target=worker) for worker in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOIN_TIMEOUT)
        assert not t.is_alive(), "a worker thread hung"


def test_concurrent_adds_of_same_item_lose_nothing(client, admin_token):
    """8 threads, one barrier, same (user, item): every increment must land.

    This is the regression test for the single-statement upsert — a
    check-then-write (SELECT, then INSERT or UPDATE) would lose updates
    here, and an uncaught IntegrityError would surface as a 500.
    """
    user, token = create_user(client, admin_token)
    item_id = _create_item(client, admin_token, "CART-RACE-1")
    threads_count = 8
    barrier = threading.Barrier(threads_count)

    def worker():
        barrier.wait()
        r = _add(client, token, item_id, qty=1)
        assert r.status_code == 200, r.text

    _run_threads([worker for _ in range(threads_count)])

    cart = _cart(client, token)
    assert len(cart["lines"]) == 1
    assert cart["lines"][0]["qty"] == threads_count


def test_concurrent_contention_no_lost_updates(client, admin_token):
    """3 threads contending on one line with multi-unit quantities: the final
    quantity is exact (3 × 3 = 9), no lost update, no flake."""
    user, token = create_user(client, admin_token)
    item_id = _create_item(client, admin_token, "CART-RACE-2")
    threads_count = 3
    per_thread = 3
    barrier = threading.Barrier(threads_count)

    def worker():
        barrier.wait()
        r = _add(client, token, item_id, qty=per_thread)
        assert r.status_code == 200, r.text

    _run_threads([worker for _ in range(threads_count)])

    cart = _cart(client, token)
    assert len(cart["lines"]) == 1
    assert cart["lines"][0]["qty"] == threads_count * per_thread
    assert cart["total_qty"] == threads_count * per_thread
