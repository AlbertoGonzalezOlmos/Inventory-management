"""Catalogue endpoints: browse (members), semantic search, and item input (staff)."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, or_, select

from app.database import VECTOR_OPTIONS, engine, get_session
from app.deps import get_current_user, require_staff
from app.embeddings import embed_text, embed_text_blob, item_embedding_text, vec_to_blob
from app.barcodes import normalize_gtin
from app.models import Item, User, utcnow
from app.schemas import (ItemIn, ItemOut, ItemPatch, StockAdjustIn,
                         StockAdjustOut, VectorSearchOut)

router = APIRouter(prefix="/api/items", tags=["items"])


@router.get("", response_model=list[ItemOut])
def list_items(
    q: str = Query(default="", description="Search in name/description/SKU"),
    category: str = "",
    barcode: str = Query(
        default="",
        description="Exact GTIN lookup (any EAN/UPC form; normalised first)",
    ),
    limit: int = Query(default=24, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Paginated catalogue listing used by the scrollable frontend.

    `barcode=` is the scanner/POS path: an exact match on the canonical
    GTIN-14 after normalisation, so scanning a UPC-E finds the item filed
    under its EAN-13. An invalid GTIN is a client error (422), not an empty
    list — a misread must not masquerade as "product not in catalogue".
    """
    statement = select(Item)
    if barcode:
        gtin = normalize_gtin(barcode)
        if gtin is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"not a valid GTIN (bad check digit or length): {barcode!r}",
            )
        statement = statement.where(Item.barcode == gtin)
    if q:
        # Escape LIKE wildcards so q="%" or q="___" search literally instead
        # of matching every row.
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        needle = f"%{escaped}%"
        statement = statement.where(
            or_(
                Item.name.ilike(needle, escape="\\"),
                Item.description.ilike(needle, escape="\\"),
                Item.sku.ilike(needle, escape="\\"),
                # Stored barcodes are canonical GTIN-14 (zero-padded), so a
                # typed/scanned 13- or 12-digit form still substring-matches;
                # a keyboard-mode scanner typing into the search box finds
                # the item without any dedicated integration.
                Item.barcode.ilike(needle, escape="\\"),
            )
        )
    if category:
        statement = statement.where(Item.category == category)
    statement = statement.order_by(Item.id.desc()).offset(offset).limit(limit)
    items = session.exec(statement).all()
    return [ItemOut.model_validate(i) for i in items]


@router.get("/categories", response_model=list[str])
def list_categories(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    rows = session.exec(select(Item.category).distinct()).all()
    return sorted(rows)


@router.get("/vector-search", response_model=list[VectorSearchOut])
def vector_search(
    q: str = Query(min_length=2, description="Natural-language query, matched "
                 "semantically against item descriptions"),
    category: str = Query(default="", description="Restrict results to this category"),
    limit: int = Query(default=12, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Semantic search: embeds the query and ranks items by cosine similarity.

    Uses the sqlite-vector extension: an exact SIMD scan over the float32
    BLOB embeddings stored in the items table.
    """
    query_vec = embed_text(q)
    if query_vec is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vector search unavailable (embedding model not loaded)",
        )
    # Embed once and reuse the vector for the scan (embedding is expensive).
    query_blob = vec_to_blob(query_vec)

    # vector_init registers the column for this connection (idempotent).
    try:
        session.execute(
            text("SELECT vector_init('items', 'embedding', :opts)"),
            {"opts": VECTOR_OPTIONS},
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vector search unavailable (sqlite-vector extension not loaded)",
        ) from exc

    # The category filter is applied in SQL (before LIMIT/OFFSET) so that
    # pagination stays correct; filtering after the scan would break it.
    scan_sql = (
        "SELECT v.rowid AS row_id, v.distance AS distance "
        "FROM vector_full_scan('items', 'embedding', :qvec) AS v "
        "JOIN items i ON i.id = v.rowid "
    )
    if category:
        scan_sql += "WHERE i.category = :cat "
    scan_sql += "ORDER BY v.distance LIMIT :limit OFFSET :offset"

    params = {"qvec": query_blob, "limit": limit, "offset": offset}
    if category:
        params["cat"] = category
    rows = session.execute(text(scan_sql), params).all()
    if not rows:
        return []

    items_by_id = {
        item.id: item
        for item in session.exec(
            select(Item).where(Item.id.in_([r.row_id for r in rows]))
        ).all()
    }

    results = []
    for row in rows:
        item = items_by_id.get(row.row_id)
        if item is not None:
            results.append(
                VectorSearchOut(
                    **ItemOut.model_validate(item).model_dump(),
                    # Clamp: cosine distance is in [0, 2], so raw similarity
                    # could otherwise go negative for unrelated items.
                    similarity=round(max(0.0, min(1.0, 1.0 - row.distance)), 4),
                )
            )
    return results


@router.get("/{item_id}", response_model=ItemOut)
def get_item(
    item_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    item = session.get(Item, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    return ItemOut.model_validate(item)


@router.post("", response_model=ItemOut, status_code=status.HTTP_201_CREATED)
def create_item(
    payload: ItemIn,
    staff: User = Depends(require_staff),
    session: Session = Depends(get_session),
):
    """Input a new item into the catalogue (staff/admin only)."""
    duplicate = session.exec(select(Item).where(Item.sku == payload.sku)).first()
    if duplicate:
        raise HTTPException(status.HTTP_409_CONFLICT, "SKU already exists")
    if payload.barcode:
        # Same-product guard: the GTIN is the global product identity, so a
        # second entry under the same barcode is always a mistake (the scan
        # that found it should have matched the existing item).
        duplicate = session.exec(
            select(Item).where(Item.barcode == payload.barcode)
        ).first()
        if duplicate:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Barcode already assigned to item {duplicate.sku}",
            )

    item = Item(**payload.model_dump())
    blob = embed_text_blob(item_embedding_text(item.name, item.description))
    if blob is None:
        # Same policy as PATCH: an item that cannot be embedded would be
        # invisible to semantic search until the next restart's backfill —
        # reject instead of silently creating an un-searchable item.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Embedding model unavailable; cannot create items right now "
            "(they would be invisible to semantic search). Try again shortly.",
        )
    item.embedding = blob
    session.add(item)
    try:
        session.commit()
    except IntegrityError:
        # Concurrent insert raced past the checks above (SKU or barcode).
        session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "SKU or barcode already exists (concurrent create)",
        )
    session.refresh(item)
    return ItemOut.model_validate(item)


@router.patch("/{item_id}", response_model=ItemOut)
def update_item(
    item_id: int,
    payload: ItemPatch,
    staff: User = Depends(require_staff),
    session: Session = Depends(get_session),
):
    item = session.get(Item, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")

    changes = payload.model_dump(exclude_unset=True)
    if changes.get("barcode"):
        duplicate = session.exec(
            select(Item).where(
                Item.barcode == changes["barcode"], Item.id != item_id
            )
        ).first()
        if duplicate:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Barcode already assigned to item {duplicate.sku}",
            )
    for field, value in changes.items():
        setattr(item, field, value)

    # Re-embed when the text that defines the embedding changed.
    if "name" in changes or "description" in changes:
        blob = embed_text_blob(
            item_embedding_text(item.name, item.description)
        )
        if blob is None:
            # Never null out the existing embedding on a transient model
            # outage (data loss). Reject the change instead; since we raise
            # before commit, no part of this PATCH is persisted.
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Embedding model unavailable; cannot change item name or "
                "description right now. No changes were saved.",
            )
        item.embedding = blob

    item.updated_at = utcnow()
    session.add(item)
    session.commit()
    session.refresh(item)
    return ItemOut.model_validate(item)


@router.post("/{item_id}/stock-adjust", response_model=StockAdjustOut)
def adjust_stock(
    item_id: int,
    payload: StockAdjustIn,
    staff: User = Depends(require_staff),
):
    """Atomically add/remove stock — the scanner bridge's POS/receiving path.

    The bridge used to do this as GET + PATCH: read-modify-write across two
    requests, so two bridges scanning the same item concurrently could lose
    an increment (REVIEW-m10.md P6). Here the read and the write run inside
    ONE targeted BEGIN IMMEDIATE on its own connection: the write lock is
    taken *before* the read, so concurrent adjusters serialise (SQLite's
    single-writer guarantee; busy_timeout makes the waiter wait, not fail)
    and the second one always reads the first one's committed value. This is
    a per-operation lock around a guarded operation — NOT the global begin
    hook app/database.py documents as an incident: that held a write lock on
    every request, reads included; WAL readers are unaffected by this one.

    Deliberately separate from PATCH /api/items/{id}: PATCH sets an absolute
    value (staff correcting the record), this applies a relative delta
    (stock events), and clamping is reported, not computed client-side.
    """
    # The guarded read-modify-write runs on a pooled DBAPI connection driven
    # directly, because SQLAlchemy 2.x's SQLite dialect has no supported
    # per-connection BEGIN IMMEDIATE (isolation_level accepts only
    # READ UNCOMMITTED/SERIALIZABLE/AUTOCOMMIT, and a dialect-level begin
    # *event hook* is exactly the banned pattern from app/database.py).
    # Switching this one connection to driver-autocommit lets this operation
    # issue its own BEGIN IMMEDIATE; the pool's connect event already set
    # busy_timeout=5000, so a racing writer waits rather than fails. The
    # previous isolation level is restored before the connection goes back
    # to the pool, so the ORM is never affected.
    raw = engine.raw_connection()
    previous_isolation = raw.isolation_level
    try:
        raw.isolation_level = None  # driver autocommit: we issue BEGIN ourselves
        cur = raw.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            row = cur.execute(
                "SELECT sku, stock FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
            sku, old_stock = row
            new_stock = max(0, old_stock + payload.delta)
            cur.execute(
                "UPDATE items SET stock = ?, updated_at = ? WHERE id = ?",
                (new_stock, utcnow().strftime("%Y-%m-%d %H:%M:%S.%f"), item_id),
            )
            cur.execute("COMMIT")
        except BaseException:
            if raw.in_transaction:
                cur.execute("ROLLBACK")
            raise
    finally:
        raw.isolation_level = previous_isolation
        raw.close()
    applied = new_stock - old_stock
    return StockAdjustOut(
        id=item_id, sku=sku, stock=new_stock,
        requested_delta=payload.delta, applied_delta=applied,
        clamped=applied != payload.delta,
    )


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(
    item_id: int,
    staff: User = Depends(require_staff),
    session: Session = Depends(get_session),
):
    item = session.get(Item, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    session.delete(item)
    session.commit()
