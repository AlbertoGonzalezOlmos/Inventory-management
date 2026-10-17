"""Catalogue endpoints: browse (members), semantic search, and item input (staff)."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, or_, select

from app.database import VECTOR_OPTIONS, engine, get_session
from app.deps import get_current_user, require_staff
from app.embeddings import embed_text, embed_text_blob, item_embedding_text, vec_to_blob
from app.barcodes import normalize_input_gtin, rank_prefix_siblings
from app.models import CartItem, Item, User, utcnow
from app.schemas import (ItemIn, ItemOut, ItemPatch, StockAdjustIn,
                         StockAdjustOut, SuggestOut, VectorSearchOut)

router = APIRouter(prefix="/api/items", tags=["items"])


def keyword_filter(statement, q: str):
    """The ONE keyword-search condition (extracted from list_items so every
    consumer — ?q=, the suggest endpoint, DbLookup.search — shares one
    dialect, wildcards escaped identically).

    Substring match, case-insensitive, over name/description/SKU/barcode.
    Stored barcodes are canonical GTIN-14 (zero-padded), so a typed/scanned
    13- or 12-digit form still substring-matches; a keyboard-mode scanner
    typing into the search box finds the item without any dedicated
    integration.
    """
    # Escape LIKE wildcards so q="%" or q="___" search literally instead
    # of matching every row.
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    needle = f"%{escaped}%"
    return statement.where(
        or_(
            Item.name.ilike(needle, escape="\\"),
            Item.description.ilike(needle, escape="\\"),
            Item.sku.ilike(needle, escape="\\"),
            Item.barcode.ilike(needle, escape="\\"),
        )
    )


class DbLookup:
    """The server-side ``ItemLookup`` of the shared decision table
    (``app/scanlookup.py``), over an ORM session.

    Deliberately lives HERE, not in ``app/scanlookup.py``: the bridge imports
    that module and its documented contract is "Stdlib only, except the
    optional pyserial" — this class needs sqlmodel/app.models, so hosting it
    in the core would silently widen the bridge's dependency footprint
    (measured incident: the bridge pulled in sqlmodel/sqlalchemy/pydantic).

    ``by_sku`` compares ``lower(sku) == lower(code)`` directly: the same
    case-insensitive exact equality the bridge obtains via ``?q=`` plus a
    client-side filter, without depending on the search endpoint's LIKE
    escaping. Small catalogues: no functional index needed (documented).
    ``search`` mirrors the ``?q=`` ordering (id DESC) so parity holds for
    the bridge's fuzzy first-hit; the server itself never calls it (fuzzy
    is the bridge's CLI opt-in — the API never guesses).
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def by_barcode(self, gtin14: str) -> list[Item]:
        return list(self.session.exec(
            select(Item).where(Item.barcode == gtin14)
        ).all())

    def by_sku(self, sku: str) -> list[Item]:
        return list(self.session.exec(
            select(Item).where(func.lower(Item.sku) == sku.strip().lower())
        ).all())

    def search(self, q: str) -> list[Item]:
        return list(self.session.exec(
            keyword_filter(select(Item), q).order_by(Item.id.desc()).limit(100)
        ).all())


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
        gtin = normalize_input_gtin(barcode)
        if gtin is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"not a valid GTIN (bad check digit or length): {barcode!r}",
            )
        statement = statement.where(Item.barcode == gtin)
    if q:
        statement = keyword_filter(statement, q)
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
    limit: int | None = Query(default=None, ge=1,
                              description="Max results; omit for every item "
                                          "(ranked by similarity, no cutoff)"),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Semantic search: embeds the query and ranks items by cosine similarity.

    Uses the sqlite-vector extension: an exact SIMD scan over the float32
    BLOB embeddings stored in the items table.
    """
    if limit is None and offset > 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="offset requires a limit (omit limit to rank every item)",
        )
    query_vec = embed_text(q)
    if query_vec is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vector search unavailable (embedding model not loaded)",
        )
    # Embed once and reuse the vector for the scan (embedding is expensive).
    query_blob = vec_to_blob(query_vec)

    # vector_init registers the column for this connection (idempotent).
    rows = _vector_search_rows(session, query_blob, category=category,
                               limit=limit, offset=offset)
    if rows is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vector search unavailable (sqlite-vector extension not loaded)",
        )
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


def _vector_search_rows(session: Session, query_blob: bytes, *,
                        category: str = "", limit: int | None = None,
                        offset: int = 0):
    """The sqlite-vector exact cosine scan (extracted from vector_search so
    the suggest endpoint can reuse it AND degrade to [] instead of 503).

    Returns the ``(row_id, distance)`` rows, or ``None`` when the extension
    is unavailable — the caller decides whether that is a 503 (the search
    endpoint, unchanged behaviour) or an empty suggestion group.
    ``limit`` defaults to None: rank EVERY matching item (no threshold, no
    cutoff); callers that want a slice (suggestions, "similar items") pass
    an explicit limit.
    The category filter is applied in SQL (before LIMIT/OFFSET) so that
    pagination stays correct; filtering after the scan would break it.
    """
    # vector_init registers the column for this connection (idempotent).
    try:
        session.execute(
            text("SELECT vector_init('items', 'embedding', :opts)"),
            {"opts": VECTOR_OPTIONS},
        )
    except Exception:
        return None

    scan_sql = (
        "SELECT v.rowid AS row_id, v.distance AS distance "
        "FROM vector_full_scan('items', 'embedding', :qvec) AS v "
        "JOIN items i ON i.id = v.rowid "
    )
    if category:
        scan_sql += "WHERE i.category = :cat "
    scan_sql += "ORDER BY v.distance"
    if limit is not None:
        scan_sql += " LIMIT :limit OFFSET :offset"

    params = {"qvec": query_blob}
    if category:
        params["cat"] = category
    if limit is not None:
        params["limit"] = limit
        params["offset"] = offset
    return session.execute(text(scan_sql), params).all()


def suggest_items(session: Session, *, gtin14: str | None = None,
                  q: str = "", limit: int = 5, semantic: bool = True) -> dict:
    """The ONE suggestion assembler — consumed by GET /api/items/suggest and
    embedded in POST /api/cart/scan's 404 detail, so there is a single
    suggestion brain.

    - ``by_prefix``: GS1 company-prefix siblings of ``gtin14`` (ranked by
      longest shared significant prefix — see app.barcodes.
      rank_prefix_siblings); only when a canonical GTIN is given.
    - ``by_keyword``: the shared keyword_filter on ``q`` (useful for opaque
      labels whose code resembles a SKU fragment).
    - ``by_semantic``: vector search on ``q`` — DEGRADES TO [] on any
      embedding/extension unavailability: a suggestion miss must never
      fail a scan flow (coordinator decision 7). Only computed for a text
      query of >= 2 chars (the vector-search endpoint's own minimum), and
      only when ``semantic`` is true: the cart-scan 404 surfaces just
      by_prefix/by_keyword, and embedding a raw barcode/label there would
      run a real model forward pass per unknown scan whose result is
      discarded — ``semantic=False`` skips it entirely.
    """
    by_prefix: list[ItemOut] = []
    if gtin14:
        with_barcodes = session.exec(
            select(Item).where(Item.barcode.is_not(None))  # type: ignore[attr-defined]
        ).all()
        pairs = [(item.barcode, item.name) for item in with_barcodes]
        by_id = {item.barcode: item for item in with_barcodes}
        for stored, _name in rank_prefix_siblings(gtin14, pairs, limit=limit):
            by_prefix.append(ItemOut.model_validate(by_id[stored]))

    by_keyword: list[ItemOut] = []
    if q:
        hits = session.exec(keyword_filter(select(Item), q).limit(limit)).all()
        by_keyword = [ItemOut.model_validate(i) for i in hits]

    by_semantic: list[ItemOut] = []
    if semantic and q and len(q) >= 2:
        query_vec = embed_text(q)
        if query_vec is not None:
            rows = _vector_search_rows(session, vec_to_blob(query_vec),
                                       limit=limit)
            if rows:
                items_by_id = {
                    item.id: item
                    for item in session.exec(
                        select(Item).where(Item.id.in_([r.row_id for r in rows]))
                    ).all()
                }
                by_semantic = [
                    ItemOut.model_validate(items_by_id[r.row_id])
                    for r in rows if r.row_id in items_by_id
                ]

    return {"by_prefix": by_prefix, "by_keyword": by_keyword,
            "by_semantic": by_semantic}


@router.get("/suggest", response_model=SuggestOut)
def suggest(
    barcode: str = Query(default="", description="Canonical or any EAN/UPC "
                 "form; drives the by_prefix group"),
    q: str = Query(default="", description="Keyword/semantic text "
                 "(e.g. the name being typed in the intake form)"),
    limit: int = Query(default=5, ge=1, le=25),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Suggestions for the unknown-item intake flow.

    ``by_semantic`` degrades to ``[]`` when the embedding model or the
    sqlite-vector extension is unavailable: this endpoint NEVER answers
    503 — a suggestion miss must not fail the flow that called it.
    """
    gtin14 = None
    if barcode:
        gtin14 = normalize_input_gtin(barcode)
        if gtin14 is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"not a valid GTIN (bad check digit or length): {barcode!r}",
            )
    return suggest_items(session, gtin14=gtin14, q=q, limit=limit)


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
    try:
        session.commit()
    except IntegrityError:
        # F1: the barcode pre-check above is not race-free (under WAL the
        # earlier auth read can hold a snapshot where the other item's
        # barcode is not yet visible). The unique index is the only
        # race-free authority; mirror create_item and answer 409, never 500.
        session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Barcode already assigned to another item (concurrent update)",
        )
    session.refresh(item)
    return ItemOut.model_validate(item)


@router.post("/{item_id}/stock-adjust", response_model=StockAdjustOut)
def adjust_stock(
    item_id: int,
    payload: StockAdjustIn,
    staff: User = Depends(require_staff),
    session: Session = Depends(get_session),
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
    # Release the connection the auth dependency used for its read *before*
    # checking out the write connection. Without this the request holds two
    # pooled connections at once, halving the pool's effective capacity for
    # this endpoint (measured: QueuePool 5+10 → 16 concurrent adjustments
    # saturated all 15, and the next one would block on the 30 s pool timeout
    # and 500). The dependency teardown closes the session again afterwards;
    # Session.close() is idempotent, and `staff` is not used past this point.
    session.close()

    raw = engine.raw_connection()
    previous_isolation = raw.isolation_level
    try:
        raw.isolation_level = None  # driver autocommit: we issue BEGIN ourselves
        cur = raw.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            # F7: sustained write contention outlasted busy_timeout (5 s).
            # That is a retry-later condition, not a server fault, so answer
            # 503 rather than a 500 "database is locked" traceback.
            if "locked" in str(exc).lower():
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "database is locked by another writer; retry the adjustment",
                    headers={"Retry-After": "1"},
                ) from exc
            raise
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
    # Cart cleanup in the same transaction (FKs are declarative only,
    # app/database.py D4): deleting the item must not leave cart rows that
    # point at it — GET /api/cart would silently drop them via the join, but
    # they would live in the table forever. Mirrors the AuthToken cleanup
    # in members.delete_member.
    for line in session.exec(
        select(CartItem).where(CartItem.item_id == item_id)
    ).all():
        session.delete(line)
    session.delete(item)
    session.commit()
