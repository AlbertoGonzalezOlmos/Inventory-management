"""Member scan requests: the "unknown item" queue (staff-only resolution).

Coordinator decision 1: catalogue creation stays staff-only. A member whose
scan could not be resolved (POST /api/cart/scan → 404) files a ScanRequest
with the name/price the intake modal collected; staff see the queue, create
the catalogue item through the normal staff-only path (POST /api/items,
prefilled from the request's gtin14) and link it back — or dismiss the
request as junk. The member never writes the catalogue.

Filing re-validates server-side (never trusting the client's classification):
a badge payload is a 400, a misread is a 422 (misreads are never filed —
the intake doctrine), a code that NOW resolves is a 409 carrying the item
(staff may have beaten the member to it — the UI offers "add that instead"),
and a duplicate open request by the same user for the same code is a 409
(kiosk double-submit is noise, not data).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, select

from app.database import get_session
from app.deps import get_current_user, require_staff
from app.models import Item, ScanRequest, User, utcnow
from app.qrbadge import parse_badge_payload
from app.routers.cart import _upsert_line
from app.routers.items import DbLookup
from app.schemas import (
    ItemOut,
    RequesterOut,
    ScanRequestIn,
    ScanRequestOut,
    ScanRequestResolveIn,
)
from app.scanlookup import resolve_scan

router = APIRouter(prefix="/api/scan-requests", tags=["scan-requests"])

VALID_STATUSES = ("open", "resolved", "dismissed")


def _request_out(session: Session, request: ScanRequest) -> ScanRequestOut:
    requester = session.get(User, request.requester_id)
    if requester is None:
        # Should be impossible (delete_member cleans requests in the same
        # transaction), but a dangling requester must not 500 the queue.
        requester = User(id=request.requester_id, name="?", email="?",
                         password_hash="", role="member")
    return ScanRequestOut(
        id=request.id,
        requester=RequesterOut(id=requester.id, name=requester.name,
                               email=requester.email),
        code=request.code,
        symbology=request.symbology,
        kind=request.kind,
        gtin14=request.gtin14,
        name=request.name,
        price_cents=request.price_cents,
        status=request.status,
        resolved_item_id=request.resolved_item_id,
        resolved_by=request.resolved_by,
        resolved_at=request.resolved_at,
        created_at=request.created_at,
        updated_at=request.updated_at,
    )


def _get_open_request(session: Session, request_id: int) -> ScanRequest:
    request = session.get(ScanRequest, request_id)
    if request is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scan request not found")
    if request.status != "open":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"status": "not_open", "message":
                    f"request is already {request.status}"},
        )
    return request


@router.post("", response_model=ScanRequestOut, status_code=status.HTTP_201_CREATED)
def file_request(
    payload: ScanRequestIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """File an 'unknown item' request (any authenticated user — the member
    path; staff can file too). The classification is re-derived server-side."""
    if parse_badge_payload(payload.code) is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"status": "badge",
                    "message": "QR badge payload — badges log in via "
                               "POST /api/auth/qr-login, they are not "
                               "catalogue items"},
        )
    verdict = resolve_scan(DbLookup(session), payload.code,
                           symbology=payload.symbology)
    if verdict.analysis.kind == "invalid-gtin":
        # Misreads are never filed — re-scan instead (intake doctrine).
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"status": "misread",
                    "message": "MISREAD — check digit fails; re-scan, "
                               "do not file"},
        )
    if verdict.item is not None:
        # The code resolves now (staff may have created it after the scan
        # 404'd): offer the existing item instead of queueing a duplicate.
        # (model_dump: HTTPException.detail is rendered by plain json.dumps.)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"status": "already",
                    "message": "This code is already in the catalogue",
                    "item": ItemOut.model_validate(verdict.item).model_dump(
                        mode="json")},
        )
    duplicate = session.exec(
        select(ScanRequest).where(
            ScanRequest.requester_id == user.id,
            ScanRequest.code == payload.code,
            ScanRequest.status == "open",
        )
    ).first()
    if duplicate is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"status": "duplicate",
                    "message": "You already have an open request for this code",
                    "request": _request_out(session, duplicate).model_dump(
                        mode="json")},
        )

    analysis = verdict.analysis
    request = ScanRequest(
        requester_id=user.id,
        code=payload.code,
        symbology=payload.symbology,
        kind=analysis.kind,
        gtin14=analysis.key if analysis.kind in ("gtin", "restricted") else None,
        name=payload.name,
        price_cents=payload.price_cents,
    )
    session.add(request)
    session.commit()
    session.refresh(request)
    return _request_out(session, request)


@router.get("/mine", response_model=list[ScanRequestOut])
def my_requests(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The caller's own requests, newest first — powers the Task-5
    'your report was resolved' view."""
    requests = session.exec(
        select(ScanRequest).where(ScanRequest.requester_id == user.id)
        .order_by(ScanRequest.id.desc())
    ).all()
    return [_request_out(session, r) for r in requests]


@router.get("", response_model=list[ScanRequestOut])
def list_requests(
    # alias: the public query parameter is ?status= (the Task-3 spec's
    # contract); the Python name stays status_filter so it cannot shadow
    # fastapi.status inside this module.
    status_filter: str = Query(default="open", alias="status"),
    staff: User = Depends(require_staff),
    session: Session = Depends(get_session),
):
    """The staff queue. Default: open requests, oldest first (queue order)."""
    if status_filter not in VALID_STATUSES + ("all",):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"status must be one of {VALID_STATUSES + ('all',)}",
        )
    statement = select(ScanRequest)
    if status_filter != "all":
        statement = statement.where(ScanRequest.status == status_filter)
    requests = session.exec(statement.order_by(ScanRequest.id)).all()
    return [_request_out(session, r) for r in requests]


@router.post("/{request_id}/resolve", response_model=ScanRequestOut)
def resolve_request(
    request_id: int,
    payload: ScanRequestResolveIn,
    staff: User = Depends(require_staff),
    session: Session = Depends(get_session),
):
    """Link the (already created) catalogue item and close the request.

    Two-step by design: item creation stays on the tested POST /api/items
    path (embeddings, duplicate-barcode guards) — no second creation code
    path. ``add_to_cart`` (default false) additionally puts the item in the
    REQUESTER's cart via the race-free upsert — the loop-closing option;
    stock is never touched (coordinator decision 6).
    """
    request = _get_open_request(session, request_id)
    item = session.get(Item, payload.item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")

    request.status = "resolved"
    request.resolved_item_id = item.id
    request.resolved_by = staff.id
    request.resolved_at = utcnow()
    request.updated_at = utcnow()
    session.add(request)
    if payload.add_to_cart:
        _upsert_line(session, request.requester_id, item.id, 1)
    session.commit()
    session.refresh(request)
    return _request_out(session, request)


@router.post("/{request_id}/dismiss", response_model=ScanRequestOut)
def dismiss_request(
    request_id: int,
    staff: User = Depends(require_staff),
    session: Session = Depends(get_session),
):
    """Dismiss a request (not a real product / duplicate / junk)."""
    request = _get_open_request(session, request_id)
    request.status = "dismissed"
    request.updated_at = utcnow()
    session.add(request)
    session.commit()
    session.refresh(request)
    return _request_out(session, request)
