"""Scanner feed API — the daemon→SPA channel and the counter-session binding.

Round 4, T4 (docs/plans/r4t4-FINAL.md; the architecture record is
docs/plans/COORDINATOR4.md). The scanner daemon (scripts/scanner_daemon.py,
T2/T3: EVIOCGRAB + evdev decode) forwards each scan to
``POST /api/scanner/events``; the kiosk SPA polls ``GET /api/scanner/events``
— logged out included, badge login IS the logged-out flow — and feeds what
it receives into its existing ``enqueueScan`` bus. The daemon is a wire,
not a brain: nothing here calls qr-login, cart/scan or anything user-facing,
so the SPA stays the single owner of every flow.

Counter-session binding (the deferred "Phase B"): a TERMINAL is one kiosk
(scanner + browser). Credentials — all stored as SHA-256 via
``app.security.token_hash``, never raw (the session-token doctrine):

* the terminal **secret** — the daemon's WRITE credential, generated
  daemon-side, presented at registration and then on every ingest
  (``X-Scanner-Secret`` header, body fallback);
* the terminal **token** — the browser's READ credential, minted at
  pairing and presented on every poll (``X-Scanner-Token`` header).
  Terminal-auth, not user-auth, is what lets the logged-out login screen
  poll;
* the **pairing code** — 8 chars (Crockford base32, no lookalikes) minted
  at registration, verified constant-time at pairing, single-use by
  construction: once the terminal holds a token, pairing answers 409.

Registration is loopback-gated by default (``127.0.0.1``/``::1`` — the
default bind; ``HCRM_SCANNER_REGISTER_ANY=1`` opens it for a LAN
deployment, accepting that the app is plaintext HTTP there either way).
Recovery for a lost secret/token is DELETE (staff or loopback) then
re-register.

Delivery semantics, settled by the coordinator: **at-most-once**. A poll
returns only unconsumed events with ``seq > after`` for its terminal and
marks them ``consumed_at`` in the same commit — two browser tabs can never
double-add one scan to a cart. The cost (a tab dying mid-batch loses the
batch) is accepted: a lost scan is re-scannable and visible, a phantom
cart line is a data error.

TTL: badge payloads are bearer credentials, so ``scan_events`` rows rest
at most ``SCANNER_EVENT_TTL_SECONDS`` (default 300) — every ingest and
poll prunes the touched terminal's consumed and stale rows (the lazy
``_cart_out`` orphan-reaping precedent).

``terminal`` and ``scan_events`` are NEW tables: ``create_all()`` adds
them, so no ``COLUMN_MIGRATIONS``/``INDEX_MIGRATIONS`` entries exist —
noted here because the append-only doctrine makes reviewers look for one.
"""

import hmac
import os
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import delete, or_, update
from sqlmodel import Session, select

from app.database import get_session
from app.deps import bearer_scheme, get_current_user
from app.models import ScanEvent, Terminal, utcnow
from app.schemas import (
    ScanEventOut,
    ScanEventsOut,
    TerminalPairIn,
    TerminalPairOut,
    TerminalRegisterIn,
    TerminalRegisterOut,
    TerminalScanIn,
    TerminalScanOut,
)
from app.scanshapes import classify_scan
from app.security import token_hash

router = APIRouter(prefix="/api/scanner", tags=["scanner"])

#: How long an unconsumed scan may rest in the queue (badge payloads are
#: bearer credentials). Module attribute at call time — tests monkeypatch it.
SCANNER_EVENT_TTL_SECONDS = 300

#: Crockford base32 without lookalikes (no I L O U): 8 chars ≈ 40 bits —
#: strong enough without a brute-force counter on a LAN plaintext app.
_PAIRING_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
_PAIRING_CODE_LEN = 8

#: Hosts allowed to register terminals / delete without user auth. A module
#: attribute (read at call time) so tests can admit the TestClient.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_REGISTER_ANY_ENV = "HCRM_SCANNER_REGISTER_ANY"

_AUTH_FAILED = {"status": "unknown_terminal",
                "message": "Unknown terminal or bad credential"}


def _is_loopback(request: Request) -> bool:
    client = request.client
    return client is not None and client.host in _LOOPBACK_HOSTS


def _register_open() -> bool:
    """HCRM_SCANNER_REGISTER_ANY=1 admits LAN registrations (opt-in)."""
    return os.environ.get(_REGISTER_ANY_ENV, "").strip().lower() in (
        "1", "true", "yes", "on")


def _mint_pairing_code() -> str:
    return "".join(secrets.choice(_PAIRING_ALPHABET)
                   for _ in range(_PAIRING_CODE_LEN))


def _presented_credential(request: Request, header_name: str,
                           fallback: str | None = None) -> str | None:
    """The credential a request presents, in either accepted form.

    Primary: the dedicated ``X-Scanner-Secret`` / ``X-Scanner-Token``
    headers. Alternative: ``Authorization: Bearer <credential>`` — the
    SPA's shared ``api()`` helper can only attach foreign credentials via
    the Authorization header (its ``{token, raw}`` form, round 4, T6), and
    that form deliberately bypasses the global 401-logout cleanup, so the
    browser MUST use it for the feed (a feed 401 is "terminal deleted",
    never "user session expired"). Both presentations are the same
    credential; a USER bearer token simply matches no secret/token hash
    and 401s like any other unknown credential.
    """
    header = request.headers.get(header_name)
    if header:
        return header
    authorization = request.headers.get("Authorization", "")
    if authorization[:7].lower() == "bearer ":
        # An EMPTY bearer ("Authorization: Bearer ") is no credential at
        # all — fall through to the body fallback rather than 401ing a
        # request that presented a valid one elsewhere.
        return authorization[7:].strip() or fallback
    return fallback


def _terminal_by_credential(session: Session, column, presented: str | None) -> Terminal:
    """The terminal a presented secret/token belongs to, or 401.

    Lookup is BY HASH (the stored side is a SHA-256), and "unknown
    terminal" and "bad credential" are deliberately indistinguishable —
    the login-endpoint doctrine: no existence leak. The secret and the
    token check DIFFERENT columns, so each credential class is refused on
    the other's endpoints (a stolen read token cannot ingest; a stolen
    write secret cannot read the badge-bearing queue).
    """
    if not presented:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=_AUTH_FAILED)
    terminal = session.exec(
        select(Terminal).where(column == token_hash(presented))
    ).first()
    if terminal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=_AUTH_FAILED)
    return terminal


def _prune(session: Session, terminal_id: str) -> None:
    """Drop the touched terminal's consumed and stale events.

    Lazy, on every ingest and poll (the cart's orphan-reaping precedent):
    consumed rows serve nothing once delivered, and rows older than the TTL
    must never reach a later poll — a scan nobody consumed in time must not
    replay into a different member's session.
    """
    cutoff = utcnow() - timedelta(seconds=SCANNER_EVENT_TTL_SECONDS)
    session.exec(
        delete(ScanEvent).where(
            ScanEvent.terminal_id == terminal_id,
            or_(ScanEvent.consumed_at.is_not(None),  # type: ignore[union-attr]
                ScanEvent.created_at < cutoff),
        )
    )


@router.post("/terminals", response_model=TerminalRegisterOut,
             status_code=status.HTTP_201_CREATED)
def register_terminal(
    payload: TerminalRegisterIn,
    request: Request,
    session: Session = Depends(get_session),
):
    """Register a terminal (the daemon, at first start).

    Loopback-gated by default: the kiosk and the server are the same host
    in the default deployment, and an open register would let any LAN peer
    mint terminals. The response carries the single-use pairing code —
    shown exactly once (the daemon prints it); only its hash is stored.
    Re-registering an existing terminal_id is a 409, not a rotate: the
    recovery path for a lost secret is DELETE (staff or loopback) + register.
    """
    if not (_is_loopback(request) or _register_open()):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={"status": "loopback_only",
                    "message": "Terminal registration is loopback-only by "
                               "default (HCRM_SCANNER_REGISTER_ANY=1 to open "
                               "for a LAN deployment)"},
        )
    existing = session.exec(
        select(Terminal).where(Terminal.terminal_id == payload.terminal_id)
    ).first()
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"status": "already_registered",
                    "message": f"Terminal {payload.terminal_id!r} is already "
                               "registered — recovery is DELETE "
                               "/api/scanner/terminals/"
                               f"{payload.terminal_id} (staff or loopback), "
                               "then re-register"},
        )
    secret_owner = session.exec(
        select(Terminal).where(Terminal.secret_hash == token_hash(payload.secret))
    ).first()
    if secret_owner is not None:
        # The secret identifies the terminal at ingest (lookup BY hash) —
        # two terminals sharing one (a copied daemon config) would route
        # every scan of both kiosks into the FIRST terminal's queue,
        # silently. Fail loudly instead; register is loopback-gated, so
        # naming the owner is no leak.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"status": "secret_in_use",
                    "message": "That secret is already registered to "
                               f"terminal {secret_owner.terminal_id!r} — "
                               "generate a fresh secret per terminal"},
        )
    code = _mint_pairing_code()
    now = utcnow()
    session.add(Terminal(
        terminal_id=payload.terminal_id,
        secret_hash=token_hash(payload.secret),
        pairing_code_hash=token_hash(code),
        created_at=now,
        last_seen_at=now,
    ))
    session.commit()
    return TerminalRegisterOut(pairing_code=code)


def _pair_terminal(session: Session, terminal_id: str, code: str) -> str:
    """The shared verify-and-mint core of ``/pair`` and ``/pair/auto``.

    One code path so the two endpoints can never drift: constant-time
    code check against the hash, unknown-terminal and wrong-code answered
    by the SAME 401 (no enumeration), the 409 single-use rule, and an
    ATOMIC mint. The mint is a conditional UPDATE
    (``WHERE token_hash IS NULL``) rather than the obvious
    read-then-write: two concurrent pair calls would both pass a NULL
    check and both commit, and the loser would hold a dead token whose
    polls 401 — the feed silently going unpaired. ``rowcount != 1`` is
    the loser's 409, decided by the database, not by a race.
    """
    terminal = session.exec(
        select(Terminal).where(Terminal.terminal_id == terminal_id)
    ).first()
    code_hash = token_hash(code)
    if (terminal is None or terminal.pairing_code_hash is None
            or not hmac.compare_digest(code_hash, terminal.pairing_code_hash)):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={"status": "bad_pairing",
                    "message": "Unknown terminal or wrong pairing code"},
        )
    raw = secrets.token_urlsafe(32)
    result = session.exec(
        update(Terminal)
        .where(Terminal.terminal_id == terminal_id,
               Terminal.token_hash.is_(None))  # type: ignore[union-attr]
        .values(token_hash=token_hash(raw))
    )
    if result.rowcount != 1:
        # Someone else paired this terminal between our read and the
        # UPDATE — the single-use rule, decided atomically.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"status": "already_paired",
                    "message": "This terminal is already paired — DELETE it "
                               "(staff or loopback) and re-register to "
                               "re-pair"},
        )
    session.commit()
    return raw


@router.post("/pair", response_model=TerminalPairOut)
def pair_terminal(payload: TerminalPairIn, session: Session = Depends(get_session)):
    """Bind this browser to a terminal (the manual path: an operator who
    read the code from ``--print-code`` types it once).

    The code is verified constant-time against its hash; a wrong code and
    an unknown terminal are indistinguishable (401, no enumeration). An
    already-paired terminal answers 409 even with the correct code — that
    IS the single-use rule (and why the code hash stays: its usability
    ended with the pairing, and re-pairing after a cleared localStorage is
    the DELETE + re-register recovery, not a second use of the same code).
    Verification and minting live in :func:`_pair_terminal`, shared with
    the one-click ``/pair/auto`` path.
    """
    return TerminalPairOut(token=_pair_terminal(
        session, payload.terminal_id, payload.code))


@router.post("/events", response_model=TerminalScanOut,
             status_code=status.HTTP_201_CREATED)
def ingest_scan(
    payload: TerminalScanIn,
    request: Request,
    session: Session = Depends(get_session),
):
    """Ingest one scan (the daemon). Auth: the terminal SECRET.

    The kind is NEVER taken from the wire — there is no kind field to
    trust: ``classify_scan`` runs here, the scan_requests doctrine. The
    response is the new event's seq (the polling cursor's next value).
    The secret may ride the ``X-Scanner-Secret`` header, the body, or
    ``Authorization: Bearer`` (the last so the SPA can reuse its ``api()``
    helper, T6 — see :func:`_presented_credential`).
    """
    secret = _presented_credential(request, "X-Scanner-Secret", payload.secret)
    terminal = _terminal_by_credential(session, Terminal.secret_hash, secret)
    _prune(session, terminal.terminal_id)
    event = ScanEvent(terminal_id=terminal.terminal_id,
                      code=payload.code, kind=classify_scan(payload.code))
    session.add(event)
    terminal.last_seen_at = utcnow()
    session.add(terminal)
    session.commit()
    session.refresh(event)
    return TerminalScanOut(seq=event.seq)


@router.get("/events", response_model=ScanEventsOut)
def poll_events(
    request: Request,
    after: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
):
    """Poll the terminal's queue (the kiosk SPA). Auth: the terminal TOKEN.

    Returns the terminal's unconsumed events with ``seq > after`` (cursor
    + consume-marking together give at-most-once delivery), marks them
    consumed in the same commit, and reports ``latest_seq`` (computed
    before pruning, so it reflects everything that existed when the poll
    started). An empty poll is ``{events: [], latest_seq}``, never an
    error. Terminal-auth, not user-auth: the logged-out login screen polls
    (the credential may also ride ``Authorization: Bearer`` — T6's adapter
    form; see :func:`_presented_credential`).
    """
    token = _presented_credential(request, "X-Scanner-Token")
    terminal = _terminal_by_credential(session, Terminal.token_hash, token)

    latest_row = session.exec(
        select(ScanEvent.seq)
        .where(ScanEvent.terminal_id == terminal.terminal_id)
        .order_by(ScanEvent.seq.desc())  # type: ignore[attr-defined]
        .limit(1)
    ).first()
    latest_seq = latest_row or 0

    _prune(session, terminal.terminal_id)
    events = session.exec(
        select(ScanEvent).where(
            ScanEvent.terminal_id == terminal.terminal_id,
            ScanEvent.seq > after,
            ScanEvent.consumed_at.is_(None),  # type: ignore[union-attr]
        ).order_by(ScanEvent.seq)
    ).all()
    now = utcnow()
    for event in events:
        event.consumed_at = now
        session.add(event)
    session.commit()
    return ScanEventsOut(
        events=[ScanEventOut(seq=e.seq, code=e.code, kind=e.kind) for e in events],
        latest_seq=latest_seq,
    )


@router.delete("/terminals/{terminal_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_terminal(
    terminal_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: Session = Depends(get_session),
):
    """Un-register a terminal: staff (any logged-in staff/admin) or loopback.

    The recovery path for a lost secret or token (re-register after this),
    and the kill switch for a compromised kiosk. Deletes the terminal and
    its queued events together (FKs are declarative only — D4 — so the
    queue is cleaned explicitly, the members-router precedent).
    """
    if not _is_loopback(request):
        user = get_current_user(request, credentials, session)  # 401s if anonymous
        if user.role not in ("staff", "admin"):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail={"status": "staff_required",
                        "message": "Deleting a terminal requires staff "
                                   "privileges (or a loopback caller)"},
            )
    terminal = session.exec(
        select(Terminal).where(Terminal.terminal_id == terminal_id)
    ).first()
    if terminal is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"status": "not_found",
                    "message": f"No terminal {terminal_id!r}"},
        )
    session.exec(delete(ScanEvent).where(ScanEvent.terminal_id == terminal_id))
    session.delete(terminal)
    session.commit()
