"""Per-user shopping cart (the server-side purchase list).

The cart is server-side and per-user: every mutation is its own committed
request, so the list is durable at every instant. That is what the badge
account-switch flow ("first save whatever had been done in the current user's
account") relies on, and what the old browser-localStorage list could never
guarantee — it was user-agnostic and not even cleared on logout.

Design notes (docs/plans/task1-FINAL.md):

- **Live prices.** Lines carry the current ``ItemOut``; ``line_total_cents``
  and ``total_cents`` are computed at read time. No snapshot column exists:
  checkout (the only consumer of "price at time of order") does not exist
  yet, and a snapshot would be write-only data with a staleness problem.
- **Stock is never mutated here** (coordinator decision): adding an item
  never decrements or reserves stock; ``qty > stock`` is a successful line —
  the UI warns from the live ``line.item.stock``.
- **Add is ONE statement** (:func:`_upsert_line`): the item-existence check
  is folded into the write itself (``INSERT ... SELECT FROM items``), so
  there is no check-then-write orphan window, and the accumulation happens
  inside the statement (``DO UPDATE SET qty = qty + excluded.qty``), so
  concurrent adds cannot lose one — SQLite serialises writers and
  ``busy_timeout`` (set on every pooled connection, app/database.py) makes
  the loser wait, not fail. This is the same bug class stock-adjust was
  rebuilt to close (REVIEW-m10.md P6); it is solved here with an upsert
  rather than ``BEGIN IMMEDIATE`` because an increment has no guard to race:
  there is no read-modify-write, the write statement *is* the accumulation.
- **FKs are declarative only** (``PRAGMA foreign_keys`` OFF,
  app/database.py D4): the API delete paths (items, members routers) clean
  up cart rows explicitly in the same transaction, and every read here
  lazily reaps rows orphaned by any other path (hand-written SQL, races) —
  the read is self-healing for exactly the limitation D4 documents.
- Every mutator — including the DELETEs — returns the full resulting
  ``CartOut`` rather than 204, so the SPA re-renders from one payload with
  no read-after-write.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, text
from sqlmodel import Session, select

from app.database import get_session
from app.deps import get_current_user
from app.models import CartItem, Item, User, utcnow
from app.schemas import (
    CART_QTY_MAX,
    CartAddIn,
    CartLineOut,
    CartOut,
    CartReplaceIn,
    CartSetIn,
    ItemOut,
)

router = APIRouter(prefix="/api/cart", tags=["cart"])

# Where accumulation happens, clamping happens: concurrent adds can push a
# stored quantity past any per-request bound, so the upsert caps it in SQL.
# An abuse bound, not policy — and not a silent drop of information: the
# response still shows the resulting (capped) line. Single source of truth:
# the same constant the input schemas bound per-request qtys with.
QTY_CAP = CART_QTY_MAX


def _upsert_line(session: Session, user_id: int, item_id: int, qty: int) -> bool:
    """Add-or-increment one cart line in a single SQL statement.

    ``INSERT ... SELECT FROM items`` folds the existence check into the
    write: a missing item selects zero rows, nothing is inserted, and the
    rowcount is 0 (the caller answers 404). On a ``(user_id, item_id)``
    conflict the ``DO UPDATE`` accumulates inside the statement, so
    concurrent increments are loss-free by construction.

    Shared deliberately (later tasks — the scan-to-cart endpoint — must call
    this, not reinvent a racy read-modify-write version of it).

    Commits: the write is a complete unit, and the session dependency does
    not autocommit (app/database.py get_session).
    """
    # strftime, not a datetime parameter: matches how SQLAlchemy's DateTime
    # column serialises (same convention as the stock-adjust raw SQL), and
    # avoids the deprecated default sqlite3 datetime adapter.
    now = utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
    result = session.execute(
        text(
            "INSERT INTO cart_items (user_id, item_id, qty, created_at, updated_at) "
            "SELECT :uid, id, :qty, :now, :now FROM items WHERE id = :iid "
            "ON CONFLICT(user_id, item_id) "
            "DO UPDATE SET qty = min(qty + excluded.qty, :cap), updated_at = :now"
        ),
        {"uid": user_id, "iid": item_id, "qty": qty, "cap": QTY_CAP, "now": now},
    )
    written = result.rowcount == 1
    if written:
        session.commit()
    return written


def _cart_out(session: Session, user: User) -> CartOut:
    """Assemble the caller's cart; lazily reap orphaned lines.

    A line whose item no longer exists (deleted by a non-API path — FKs are
    declarative only) is skipped in the response and deleted, so orphans
    cannot accumulate invisibly behind the join.
    """
    lines = session.exec(
        select(CartItem).where(CartItem.user_id == user.id).order_by(CartItem.id)
    ).all()
    if not lines:
        return CartOut(lines=[], total_qty=0, total_cents=0)

    items = {
        item.id: item
        for item in session.exec(
            select(Item).where(Item.id.in_([line.item_id for line in lines]))
        ).all()
    }

    out: list[CartLineOut] = []
    reaped = False
    for line in lines:
        item = items.get(line.item_id)
        if item is None:
            session.delete(line)
            reaped = True
            continue
        out.append(
            CartLineOut(
                item=ItemOut.model_validate(item),
                qty=line.qty,
                line_total_cents=item.price_cents * line.qty,
                created_at=line.created_at,
                updated_at=line.updated_at,
            )
        )
    if reaped:
        session.commit()
    return CartOut(
        lines=out,
        total_qty=sum(line.qty for line in out),
        total_cents=sum(line.line_total_cents for line in out),
    )


def _get_line(session: Session, user_id: int, item_id: int) -> CartItem | None:
    """The caller's line for an item, or None. Scoped to the caller by
    construction: another user's line is indistinguishable from a missing
    one (404, never 403 — no existence leak about other carts)."""
    return session.exec(
        select(CartItem).where(
            CartItem.user_id == user_id, CartItem.item_id == item_id
        )
    ).first()


@router.get("", response_model=CartOut)
def get_cart(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return _cart_out(session, user)


@router.post("/items", response_model=CartOut)
def add_item(
    payload: CartAddIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Add-or-increment: re-adding an item already in the cart raises its
    quantity — a re-scan is a quantity, not an error (never 409)."""
    if not _upsert_line(session, user.id, payload.item_id, payload.qty):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    return _cart_out(session, user)


@router.patch("/items/{item_id}", response_model=CartOut)
def set_line_qty(
    item_id: int,
    payload: CartSetIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Absolute set (UI steppers, corrections); ``qty == 0`` removes the
    line. Never creates: a line that is not in the caller's cart is a 404,
    creation is the add endpoint's job."""
    line = _get_line(session, user.id, item_id)
    if line is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not in cart")
    if payload.qty == 0:
        session.delete(line)
    else:
        line.qty = payload.qty
        line.updated_at = utcnow()
        session.add(line)
    session.commit()
    return _cart_out(session, user)


@router.delete("/items/{item_id}", response_model=CartOut)
def remove_line(
    item_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    line = _get_line(session, user.id, item_id)
    if line is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not in cart")
    session.delete(line)
    session.commit()
    return _cart_out(session, user)


@router.put("", response_model=CartOut)
def replace_cart(
    payload: CartReplaceIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """All-or-nothing whole-cart replace — the flush/replay primitive for
    the account-switch flow and the one-time localStorage import.

    Validation completes before any write, so an unknown item (404) or a
    duplicate line (422) leaves the previous cart byte-identical: a failed
    flush must never destroy the outgoing user's saved cart.
    """
    ids = [line.item_id for line in payload.lines]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Duplicate item_id in lines: {duplicates}",
        )
    if ids:
        existing = set(
            session.exec(select(Item.id).where(Item.id.in_(ids))).all()
        )
        missing = sorted(set(ids) - existing)
        if missing:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"Item(s) not found: {missing}"
            )

    now = utcnow()
    # The DELETE must be emitted as its own statement, BEFORE the ORM adds:
    # a single flush processes INSERTs before DELETEs on the same table, so a
    # delete-then-add replace whose lines reuse an existing (user_id, item_id)
    # — the common flush shape (same items, new quantities) — would collide
    # with uq_cart_items_user_item and 500 (measured; regression test:
    # test_replace_with_overlapping_lines). The Core delete executes
    # immediately and starts the write transaction, so delete+inserts still
    # commit as ONE all-or-nothing unit.
    session.execute(delete(CartItem).where(CartItem.user_id == user.id))
    for line in payload.lines:
        session.add(
            CartItem(
                user_id=user.id,
                item_id=line.item_id,
                qty=line.qty,
                created_at=now,
                updated_at=now,
            )
        )
    session.commit()
    return _cart_out(session, user)


@router.delete("", response_model=CartOut)
def clear_cart(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Idempotent clear (the collection always \"exists\"): clearing an
    already-empty cart is the desired end state, not a client error."""
    for line in session.exec(
        select(CartItem).where(CartItem.user_id == user.id)
    ).all():
        session.delete(line)
    session.commit()
    return _cart_out(session, user)
