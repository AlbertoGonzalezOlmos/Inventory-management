"""Checkout + purchase history (Task C).

A purchase is an EXPLICIT checkout action: ``POST /api/orders`` snapshots
the cart into an immutable receipt, decrements stock with clamp-and-report,
and clears the cart — all in ONE transaction, so stock, receipt and cart
move together or not at all. History reads serve the snapshots: immune to
later price edits and item deletion (weak-ref ``item_id``).

The transaction uses the ``adjust_stock`` BEGIN IMMEDIATE precedent on a
dedicated connection: a guarded read-modify-write needs the write lock
BEFORE the reads, and a single ``UPDATE … RETURNING`` cannot report
``applied_qty`` because SQLite's RETURNING only sees post-update values
(the old stock is unrecoverable — verified empirically). The
``max(0, old - qty)`` clamp (computed under the lock, written as an
absolute value) is defence-in-depth even though the lock already guarantees
the arithmetic.

Clamp-not-veto (coordinator decision 2): a sale beyond stock sells what is
available and the receipt records the ordered amount (``qty``) alongside
``applied_qty``/``clamped`` — reported, never silently dropped.
"""

import sqlite3
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from app.database import engine, get_session
from app.deps import get_current_user
from app.models import CartItem, Item, Purchase, PurchaseLine, User, utcnow
from app.schemas import (
    CalendarDayOut,
    CartOut,
    CheckoutOut,
    OrderCalendarOut,
    PurchaseLineOut,
    PurchaseOut,
)

router = APIRouter(prefix="/api/orders", tags=["orders"])


def _line_out(line: PurchaseLine) -> PurchaseLineOut:
    return PurchaseLineOut(
        item_id=line.item_id,
        sku=line.sku,
        name=line.name,
        price_cents=line.price_cents,
        qty=line.qty,
        applied_qty=line.applied_qty,
        clamped=line.clamped,
        line_total_cents=line.price_cents * line.applied_qty,
    )


def _purchase_out(session: Session, purchase: Purchase) -> PurchaseOut:
    lines = session.exec(
        select(PurchaseLine).where(PurchaseLine.purchase_id == purchase.id)
        .order_by(PurchaseLine.id)
    ).all()
    return PurchaseOut(
        id=purchase.id,
        created_at=purchase.created_at,
        total_cents=purchase.total_cents,
        lines=[_line_out(line) for line in lines],
    )


def _history(session: Session, user_id: int, limit: int, offset: int) -> list[PurchaseOut]:
    purchases = session.exec(
        select(Purchase).where(Purchase.user_id == user_id)
        .order_by(Purchase.id.desc())
        .offset(offset).limit(limit)
    ).all()
    return [_purchase_out(session, p) for p in purchases]


@router.post("", response_model=CheckoutOut, status_code=status.HTTP_201_CREATED)
def checkout(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Check out: snapshot the cart into a purchase, decrement stock with
    clamp-and-report, clear the cart — atomically."""
    user_id = user.id  # captured before the session release below
    # Empty-cart pre-check on the plain read (no transaction needed).
    cart_rows = session.exec(
        select(CartItem).where(CartItem.user_id == user_id)
    ).all()
    if not cart_rows:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cart is empty")

    # Release the connection the auth dependency used BEFORE checking out
    # the write connection — the adjust_stock doctrine (two pooled
    # connections at once halve the pool's effective capacity).
    session.close()

    raw = engine.raw_connection()
    previous_isolation = raw.isolation_level
    purchase_id = 0
    total = 0
    try:
        raw.isolation_level = None  # driver autocommit: we issue BEGIN ourselves
        cur = raw.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "database is locked by another writer; retry the checkout",
                    headers={"Retry-After": "1"},
                ) from exc
            raise
        try:
            created = utcnow()   # ONE clock read: the stored row and the
            now = created.strftime("%Y-%m-%d %H:%M:%S.%f")  # response agree
            # Everything is read under the write lock: current by construction.
            cart_lines = cur.execute(
                "SELECT id, item_id, qty FROM cart_items WHERE user_id = ? "
                "ORDER BY id", (user_id,)).fetchall()

            purchased: list[tuple] = []
            skipped: list[int] = []
            line_ids: list[int] = []
            for line_id, item_id, qty in cart_lines:
                row = cur.execute(
                    "SELECT sku, name, price_cents, stock FROM items WHERE id = ?",
                    (item_id,)).fetchone()
                line_ids.append(line_id)
                if row is None:
                    # Orphan: the item no longer exists. Skip (noted in the
                    # receipt) and clean the line — the checkout proceeds.
                    skipped.append(item_id)
                    continue
                sku, name, price_cents, old_stock = row
                applied = min(qty, old_stock)
                clamped = qty > old_stock
                # The clamp is defence-in-depth; the lock already guarantees
                # old_stock is current.
                new_stock = max(0, old_stock - qty)
                cur.execute(
                    "UPDATE items SET stock = ?, updated_at = ? WHERE id = ?",
                    (new_stock, now, item_id))
                purchased.append((item_id, sku, name, price_cents,
                                  qty, applied, clamped))

            def clear_cart() -> None:
                for line_id in line_ids:
                    cur.execute("DELETE FROM cart_items WHERE id = ?", (line_id,))

            if not purchased:
                # Only orphans in the cart: commit the housekeeping, then the
                # cart really is empty.
                clear_cart()
                cur.execute("COMMIT")
                empty = True
            else:
                total = sum(price * applied
                            for _iid, _sku, _n, price, _q, applied, _c in purchased)
                cur.execute(
                    "INSERT INTO purchases (user_id, total_cents, created_at) "
                    "VALUES (?, ?, ?)", (user_id, total, now))
                purchase_id = cur.lastrowid
                for item_id, sku, name, price_cents, qty, applied, clamped in purchased:
                    cur.execute(
                        "INSERT INTO purchase_lines (purchase_id, item_id, sku, "
                        "name, price_cents, qty, applied_qty, clamped, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (purchase_id, item_id, sku, name, price_cents,
                         qty, applied, clamped, now))
                clear_cart()
                cur.execute("COMMIT")
                empty = False
        except BaseException:
            if raw.in_transaction:
                cur.execute("ROLLBACK")
            raise
    finally:
        raw.isolation_level = previous_isolation
        raw.close()

    if empty:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cart is empty")

    purchase = PurchaseOut(
        id=purchase_id,
        created_at=created,   # the SAME instant stored in the row — the
        total_cents=total,    # response and a follow-up GET agree exactly
        lines=[
            PurchaseLineOut(
                item_id=item_id, sku=sku, name=name, price_cents=price_cents,
                qty=qty, applied_qty=applied, clamped=clamped,
                line_total_cents=price_cents * applied)
            for item_id, sku, name, price_cents, qty, applied, clamped in purchased
        ],
    )
    return CheckoutOut(
        purchase=purchase,
        # The post-checkout cart: EMPTY by construction — every line was
        # deleted inside the same transaction (one payload, no
        # read-after-write).
        cart=CartOut(lines=[], total_qty=0, total_cents=0),
        skipped=skipped,
    )


@router.get("/mine", response_model=list[PurchaseOut])
def my_orders(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The caller's purchase history, newest first, with lines embedded."""
    return _history(session, user.id, limit, offset)


@router.get("/calendar", response_model=OrderCalendarOut)
def order_calendar(
    year: int | None = Query(default=None, ge=1, le=9999),
    month: int | None = Query(default=None, ge=1, le=12),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The caller's purchases for one month, aggregated per UTC day (the
    purchase-history calendar).

    `days` maps "YYYY-MM-DD" → {cents, count}: the PAID amount that day
    (Σ price_cents × applied_qty over the day's receipt lines — the
    clamp-not-veto rule: a shortfall sells what is available, and the
    calendar shows what was paid, never what was requested) and the number
    of orders that landed on it. Only days with at least one purchase
    appear; an empty month is `days == {}` + `total_cents == 0`, not a 404.

    Both parameters are optional and default to the current UTC month;
    nonsense (month=0/13, a non-int) is a 422 before any query runs.
    Member-self only — the same `get_current_user` gate as /mine, so the
    requesting user sees their history and nobody else's.
    """
    now = utcnow()
    y = now.year if year is None else year
    m = now.month if month is None else month
    # Half-open [first of month, first of next month) over the naive-UTC
    # stored created_at — the same clock the receipts were written with.
    start = datetime(y, m, 1)
    # …except December of year 9999 (the validated max): "first of next
    # month" would be year 10000, which datetime cannot represent — a
    # validated input must never 500, so cap the half-open range at
    # datetime.max there.
    end = (datetime.max if (y, m) == (9999, 12)
           else datetime(y + (m == 12), m % 12 + 1, 1))

    rows = session.exec(
        select(
            Purchase.created_at,
            Purchase.id,
            PurchaseLine.price_cents,
            PurchaseLine.applied_qty,
        )
        .join(PurchaseLine, PurchaseLine.purchase_id == Purchase.id)
        .where(
            Purchase.user_id == user.id,
            Purchase.created_at >= start,
            Purchase.created_at < end,
        )
    ).all()

    days: dict[str, dict] = {}
    total = 0
    for created_at, purchase_id, price_cents, applied_qty in rows:
        key = created_at.strftime("%Y-%m-%d")
        bucket = days.setdefault(key, {"cents": 0, "ids": set()})
        bucket["cents"] += price_cents * applied_qty
        bucket["ids"].add(purchase_id)
        total += price_cents * applied_qty
    return OrderCalendarOut(
        year=y,
        month=m,
        days={
            key: CalendarDayOut(cents=bucket["cents"], count=len(bucket["ids"]))
            for key, bucket in days.items()
        },
        total_cents=total,
    )
