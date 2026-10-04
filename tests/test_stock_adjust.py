"""Tests for POST /api/items/{id}/stock-adjust — the atomic stock path.

This endpoint replaces the bridge's GET + PATCH read-modify-write, which
left a window where two concurrent adjustments could lose one
(REVIEW-m10.md P6). The guarantees pinned here:

- the delta is applied atomically — the read and the write run inside one
  targeted BEGIN IMMEDIATE, so concurrent adjusters serialise and the total
  is exact (the concurrency test would lose updates under the old scheme);
- clamping at 0 is reported precisely: `applied_delta` and `clamped` are
  computed from the actual old stock, including the boundary cases
  (exact-to-zero is NOT a clamp; decrementing empty stock IS);
- validation and permissions match the rest of the items API.
"""

import threading

from tests.conftest import auth, create_user

JOIN_TIMEOUT = 120


def _create_item(client, token, sku, stock):
    r = client.post(
        "/api/items",
        json={"sku": sku, "name": f"Item {sku}", "stock": stock,
              "price_cents": 100},
        headers=auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _adjust(client, token, item_id, delta):
    return client.post(
        f"/api/items/{item_id}/stock-adjust",
        json={"delta": delta},
        headers=auth(token),
    )


def test_adjust_increments_and_decrements(client, admin_token):
    item_id = _create_item(client, admin_token, "ADJ-UP", stock=10)

    r = _adjust(client, admin_token, item_id, 3)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stock"] == 13
    assert body["requested_delta"] == 3
    assert body["applied_delta"] == 3
    assert body["clamped"] is False

    r = _adjust(client, admin_token, item_id, -4)
    assert r.status_code == 200, r.text
    assert r.json()["stock"] == 9


def test_response_identifies_the_item(client, admin_token):
    item_id = _create_item(client, admin_token, "ADJ-ID", stock=1)
    body = _adjust(client, admin_token, item_id, 1).json()
    assert body["id"] == item_id
    assert body["sku"] == "ADJ-ID"


def test_clamps_at_zero_and_reports_the_applied_delta(client, admin_token):
    """stock 2, delta -5: applied is -2, not -5 — and it says so."""
    item_id = _create_item(client, admin_token, "ADJ-CLAMP", stock=2)
    r = _adjust(client, admin_token, item_id, -5)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stock"] == 0
    assert body["requested_delta"] == -5
    assert body["applied_delta"] == -2
    assert body["clamped"] is True


def test_exact_to_zero_is_not_a_clamp(client, admin_token):
    """stock 2, delta -2 → 0: fully applied, so clamped stays False."""
    item_id = _create_item(client, admin_token, "ADJ-EXACT", stock=2)
    body = _adjust(client, admin_token, item_id, -2).json()
    assert body["stock"] == 0
    assert body["applied_delta"] == -2
    assert body["clamped"] is False


def test_decrementing_empty_stock_is_a_reported_no_op(client, admin_token):
    """stock 0, delta -1: nothing is applied and that is reported — a sale
    beyond available stock must not look like a normal transaction (P6)."""
    item_id = _create_item(client, admin_token, "ADJ-EMPTY", stock=0)
    body = _adjust(client, admin_token, item_id, -1).json()
    assert body["stock"] == 0
    assert body["applied_delta"] == 0
    assert body["clamped"] is True


def test_zero_delta_is_rejected(client, admin_token):
    item_id = _create_item(client, admin_token, "ADJ-ZERO", stock=1)
    assert _adjust(client, admin_token, item_id, 0).status_code == 422


def test_unknown_fields_are_rejected(client, admin_token):
    item_id = _create_item(client, admin_token, "ADJ-EXTRA", stock=1)
    r = client.post(
        f"/api/items/{item_id}/stock-adjust",
        json={"delta": 1, "note": "typo field"},
        headers=auth(admin_token),
    )
    assert r.status_code == 422


def test_unknown_item_is_404(client, admin_token):
    assert _adjust(client, admin_token, 999_999, 1).status_code == 404


def test_members_cannot_adjust_stock(client, admin_token):
    _, member = create_user(client, admin_token, role="member")
    item_id = _create_item(client, admin_token, "ADJ-PERM", stock=1)
    assert _adjust(client, member, item_id, 1).status_code == 403


def test_unauthenticated_is_401(client, admin_token):
    item_id = _create_item(client, admin_token, "ADJ-ANON", stock=1)
    r = client.post(f"/api/items/{item_id}/stock-adjust", json={"delta": 1})
    assert r.status_code == 401


def test_adjust_never_touches_the_embedding(client, admin_token):
    """A stock event is not a text edit: the embedding survives (the PATCH
    path is the one that re-embeds, guarded against model outages)."""
    item_id = _create_item(client, admin_token, "ADJ-EMB", stock=1)
    assert client.get(
        f"/api/items/{item_id}", headers=auth(admin_token)
    ).json()["has_embedding"] is True
    _adjust(client, admin_token, item_id, 1)
    assert client.get(
        f"/api/items/{item_id}", headers=auth(admin_token)
    ).json()["has_embedding"] is True


def test_concurrent_adjustments_lose_nothing(client, admin_token):
    """The P6 regression: under GET + PATCH, two adjusters racing the same
    item could both read the same stock and one increment vanished. Inside
    one BEGIN IMMEDIATE per request the writers serialise, so the total must
    be exact — THREADS × PER_THREAD, every time."""
    THREADS, PER_THREAD = 4, 25
    item_id = _create_item(client, admin_token, "ADJ-RACE", stock=100)
    failures = []

    def worker():
        for _ in range(PER_THREAD):
            r = _adjust(client, admin_token, item_id, 1)
            if r.status_code != 200:
                failures.append((r.status_code, r.text))
                return

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=JOIN_TIMEOUT)
        assert not t.is_alive(), f"worker still running after {JOIN_TIMEOUT}s"
    assert failures == []

    final = client.get(f"/api/items/{item_id}", headers=auth(admin_token))
    assert final.json()["stock"] == 100 + THREADS * PER_THREAD


def test_adjust_holds_exactly_one_pooled_connection(client, admin_token):
    """Regression tripwire: the endpoint used to check out a *second*
    connection for the guarded write while still holding the auth
    dependency's, halving the pool's capacity for this route — measured on
    the default QueuePool (5 + 10 overflow): 16 concurrent adjustments
    saturated all 15 connections, and a 16th would block on the 30 s pool
    timeout and 500. One request must hold one connection at a time.
    """
    from sqlalchemy import event

    from app.database import engine

    live = 0
    peak = 0
    lock = threading.Lock()

    def on_checkout(dbapi_conn, rec, proxy):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)

    def on_checkin(dbapi_conn, rec):
        nonlocal live
        with lock:
            live -= 1

    item_id = _create_item(client, admin_token, "ADJ-POOL", stock=1)
    event.listen(engine, "checkout", on_checkout)
    event.listen(engine, "checkin", on_checkin)
    try:
        peak = 0
        r = _adjust(client, admin_token, item_id, 1)
        assert r.status_code == 200, r.text
    finally:
        event.remove(engine, "checkout", on_checkout)
        event.remove(engine, "checkin", on_checkin)
    assert peak == 1, f"stock-adjust held {peak} pooled connections; expected 1"


def test_lock_timeout_maps_to_503_not_500(client, admin_token, monkeypatch):
    """F7: if `BEGIN IMMEDIATE` cannot take the write lock within
    `busy_timeout`, the route answers 503 (retry-later with `Retry-After`),
    not a 500 "database is locked" traceback.

    A separate engine onto the same file with a 1 ms busy timeout keeps the
    test fast (production is 5 s); a second connection holds an uncommitted
    `BEGIN IMMEDIATE` so the endpoint's own begin genuinely cannot proceed.
    """
    import sqlite3

    import sqlalchemy

    import app.routers.items as items_router
    from tests.conftest import get_db_path

    item_id = _create_item(client, admin_token, "ADJ-LOCK", stock=1)
    db = get_db_path()

    lock_engine = sqlalchemy.create_engine(f"sqlite:///{db}")
    sqlalchemy.event.listen(
        lock_engine, "connect",
        lambda dbapi_conn, _rec: dbapi_conn.execute("PRAGMA busy_timeout=1"),
    )
    monkeypatch.setattr(items_router, "engine", lock_engine)

    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("PRAGMA busy_timeout=1")
    holder.execute("BEGIN IMMEDIATE")  # hold the write lock, uncommitted
    try:
        r = _adjust(client, admin_token, item_id, 1)
        assert r.status_code == 503, r.text
        assert "locked" in r.json()["detail"].lower()
        assert r.headers.get("retry-after") == "1"
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        lock_engine.dispose()
