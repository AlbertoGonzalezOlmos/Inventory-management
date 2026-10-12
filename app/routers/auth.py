"""Authentication endpoints: register, login (password & QR badge), logout,
whoami, password change, kiosk logoff scheduling, badge identification."""

import hmac
import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import security
from app.database import get_session
from app.deps import bearer_scheme, get_current_user
from app.models import AuthToken, User, utcnow
from app.qrbadge import (
    BADGE_PREFIX,
    badge_svg,
    new_badge_for,
    parse_badge_payload,
    view_badge,
)
from app.schemas import (
    AuthOut,
    ChangeEmailIn,
    ChangePasswordIn,
    ChangePhoneIn,
    LogoffScheduleOut,
    LoginIn,
    QrBadgeOut,
    QrBadgeViewOut,
    QrIdentifyOut,
    QrLoginIn,
    RegisterIn,
    RegisterOut,
    UserOut,
)
from app.security import (
    DUMMY_HASH,
    hash_password,
    is_well_known_password,
    new_token,
    token_hash,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

WELL_KNOWN_DETAIL = (
    "That password is published in this project's documentation/seed code; "
    "choose another one"
)


def _issue_token(session: Session, user: User) -> AuthOut:
    # Housekeeping: drop expired tokens so the table does not grow forever.
    for expired in session.exec(
        select(AuthToken).where(AuthToken.expires_at < utcnow())
    ).all():
        session.delete(expired)

    token, raw = new_token(user.id)
    session.add(token)
    session.commit()
    return AuthOut(token=raw, user=UserOut.model_validate(user))


@router.post("/register", response_model=RegisterOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterIn, session: Session = Depends(get_session)):
    if is_well_known_password(payload.password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, WELL_KNOWN_DETAIL)
    email = payload.email.strip().lower()
    existing = session.exec(select(User).where(User.email == email)).first()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    user = User(
        email=email,
        name=payload.name.strip(),
        password_hash=hash_password(payload.password),
        role="member",
    )
    # Auto-badge: every new account is issued its first QR badge. Task A:
    # derivation needs user.id, so the flow is flush-then-derive in ONE
    # transaction — the flush executes the INSERT (assigning the id) WITHOUT
    # committing, so a raced duplicate-email insert still rolls back row,
    # generation and never-shown payload together (no badgeless-account
    # window; the IntegrityError simply fires at the flush instead of the
    # commit). The raw payload is returned exactly once, in this response
    # — and is re-viewable forever after via GET /api/auth/qr-badge.
    session.add(user)
    try:
        session.flush()
        badge_payload = new_badge_for(user)
        session.commit()
    except IntegrityError:
        # Concurrent registration with the same email raced past the check.
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    session.refresh(user)
    auth_out = _issue_token(session, user)
    # badge_svg is rendered after the commit (pure CPU over the payload;
    # the rotate endpoints do the same), so the response carries the badge
    # only on the success path.
    return RegisterOut(
        token=auth_out.token,
        user=auth_out.user,
        qr_badge=QrBadgeOut(payload=badge_payload, svg=badge_svg(badge_payload)),
    )


@router.post("/login", response_model=AuthOut)
def login(payload: LoginIn, session: Session = Depends(get_session)):
    email = payload.email.strip().lower()
    user = session.exec(select(User).where(User.email == email)).first()
    # Always run the PBKDF2 verification, even for unknown emails, so the
    # response time does not reveal whether the account exists.
    stored = user.password_hash if user is not None else DUMMY_HASH
    if not verify_password(payload.password, stored) or user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    return _issue_token(session, user)


# A raw badge token (without the HCRM1: prefix) is also accepted, so a badge
# works even if a scanner/bridge strips the prefix.
_RAW_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")


def _badge_from_scanned(scanned: str) -> str | None:
    """The ONE badge-acceptance rule (Task 4): a full ``HCRM1:`` payload, or
    the raw 43-char token — a scanner/bridge may strip the prefix. Shared by
    qr-login and qr-identify so the two endpoints cannot drift on what
    counts as a badge presentation (the same anti-drift doctrine as the
    Task-3 scan-resolution core)."""
    badge = parse_badge_payload(scanned)
    if badge is None and _RAW_TOKEN_RE.fullmatch(scanned.strip()):
        badge = BADGE_PREFIX + scanned.strip()
    return badge


@router.post("/qr-login", response_model=AuthOut)
def qr_login(payload: QrLoginIn, session: Session = Depends(get_session)):
    """Log in with a QR badge instead of email+password.

    The badge is a bearer credential (256-bit random) — whoever holds the
    printed code holds the account, so treat badges like passwords: only the
    SHA-256 hash is stored, and a compromised badge must be revoked/rotated
    (Account → QR badge, or staff in Members).
    """
    badge = _badge_from_scanned(payload.token)
    user = (
        session.exec(select(User).where(User.qr_badge_hash == token_hash(badge))).first()
        if badge is not None
        else None
    )
    if user is None:
        # Same status as password login: do not distinguish "no such badge"
        # from other failures (a badge either matches or it doesn't).
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid QR badge")
    return _issue_token(session, user)


@router.post("/qr-badge", response_model=QrBadgeOut, status_code=status.HTTP_201_CREATED)
def create_my_badge(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Generate (or replace) the caller's own QR badge.

    Regenerating is also how you rotate a lost/copied badge: the old payload
    stops working the moment the new hash is stored. This is also the
    recovery path for an auto-issued creation badge whose response was
    never seen (client disconnect): rotate and a fresh badge is shown.
    """
    payload = new_badge_for(user)
    session.add(user)
    session.commit()
    return QrBadgeOut(payload=payload, svg=badge_svg(payload))


def badge_view_response(session, user) -> QrBadgeViewOut:
    """Map the qrbadge domain result to the HTTP envelope (shared with the
    staff endpoint in app/routers/members.py — the scan_requests→cart import
    precedent). Status-convention envelopes, house style: a ``status``
    discriminator, never a bare detail string."""
    result = view_badge(session, user)
    if result.status == "none":
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"status": "none",
                    "message": "No QR badge for this account — generate one first"})
    if result.status == "no_key":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"status": "no_key",
                    "message": "Badge re-viewing is unavailable (no derivation "
                               "key) — logins are unaffected; regenerate the "
                               "badge once a key exists"})
    if result.status == "key_mismatch":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"status": "key_mismatch",
                    "message": "The badge key changed since this badge was "
                               "issued — regenerate the badge to re-enable "
                               "viewing (logins are unaffected: they verify "
                               "by hash)"})
    return QrBadgeViewOut(
        status=result.status,          # "ok" | "migrated"
        payload=result.payload,
        svg=badge_svg(result.payload),
        generation=result.generation,
        migrated=result.migrated,
    )


@router.get("/qr-badge", response_model=QrBadgeViewOut)
def view_my_badge(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Re-view the caller's QR badge — repeatable, forever (Task A).

    The payload is derived from the server's master key on demand (only its
    hash is stored), so unlike the issuance/rotation responses this can be
    fetched again and again. A LEGACY (pre-derivation) badge auto-migrates
    on first view: a fresh derived badge is issued and the previously
    printed one stops working — ``migrated: true`` says so (the UI warns).
    """
    return badge_view_response(session, user)


@router.delete("/qr-badge", status_code=status.HTTP_204_NO_CONTENT)
def revoke_my_badge(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    user.qr_badge_hash = None
    session.add(user)
    session.commit()


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    # Revoke only the presented token — other sessions of the user stay alive.
    token = session.get(AuthToken, token_hash(credentials.credentials))
    if token is not None:
        session.delete(token)
        session.commit()


@router.post("/logoff-schedule", response_model=LogoffScheduleOut)
def logoff_schedule(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Arm the kiosk logoff countdown on the PRESENTED token (Task 4).

    Per-presented-token, exactly like logout: the user's other sessions are
    unaffected. The SPA calls this when the user scans their own badge
    ("log off in 60 seconds?"); calling it again re-arms from now — that
    overwrite IS the same-badge-rescan-restarts-the-countdown semantics, no
    special case. The deadline is clamped to the token's natural expiry so
    an absurd grace cannot arm a countdown past the token's life. The
    deadline is computed server-side and returned — the SPA counts down
    from THIS timestamp, never from a client-side now+60 (clock skew and
    request latency stay away from the truth).

    Even if the browser dies mid-countdown, get_current_user enforces the
    deadline lazily on the token's next use anywhere — the security property
    never depends on the client.
    """
    token = session.get(AuthToken, token_hash(credentials.credentials))
    # Module attribute at call time (not a from-import binding): tests
    # monkeypatch app.security.LOGOFF_GRACE_SECONDS to 0 — the env clamp
    # stays out of the way.
    grace = security.LOGOFF_GRACE_SECONDS
    deadline = min(utcnow() + timedelta(seconds=grace), token.expires_at)
    token.logoff_deadline = deadline
    session.add(token)
    session.commit()
    return LogoffScheduleOut(logoff_deadline=deadline, grace_seconds=grace)


@router.post("/logoff-cancel", status_code=status.HTTP_204_NO_CONTENT)
def logoff_cancel(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Disarm the kiosk logoff countdown on the presented token.

    Idempotent ("the desired end state, not a client error" — logout's
    doctrine): the SPA fires it on [Stay logged in], on a different-badge
    scan before the account switch, and defensively; none of those should
    be able to error. Note: after the deadline has passed the token is
    already dead — cancel then 401s (a dead session cannot be resurrected).
    """
    token = session.get(AuthToken, token_hash(credentials.credentials))
    token.logoff_deadline = None
    session.add(token)
    session.commit()


@router.post("/qr-identify", response_model=QrIdentifyOut)
def qr_identify(
    payload: QrLoginIn,
    user: User = Depends(get_current_user),
):
    """Is this scanned badge the CALLER's own? (Task 4)

    The same-user question the kiosk flow needs before choosing
    "logoff screen" vs "save-then-switch". A pure predicate: no session
    minted, nothing armed, no writes of any kind — unlike qr-login (which
    mints a full bearer session to answer a question that is usually
    same-user, leaving a live credential to revoke). The switch flow still
    uses qr-login exactly when it wants the new session.

    Enumeration-safe: every non-matching input — garbage, a product
    barcode, somebody else's badge — answers the same {same_user: false}; a
    badgeless account answers false to everything. The one bit disclosed is
    about the caller's OWN qr_badge_hash, nothing else.
    """
    badge = _badge_from_scanned(payload.token)
    same_user = (
        badge is not None
        and user.qr_badge_hash is not None
        and hmac.compare_digest(token_hash(badge), user.qr_badge_hash)
    )
    return QrIdentifyOut(same_user=same_user)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: ChangePasswordIn,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Current password is incorrect"
        )
    # A no-op "change" must not count as a change: accepting new == current
    # returned 204 AND cleared must_change_password, so a staff-issued
    # temporary password could be kept indefinitely — B9 through the back door
    # (the account stays on a password somebody else chose and knows).
    if payload.new_password == payload.current_password:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "New password must differ from the current password",
        )
    # Rejecting the published defaults here is what closes the loop: without it,
    # `change-password changeme -> changeme` returned 204 AND cleared
    # must_change_password, leaving a well-known credential with full API access
    # until the next boot (PLAN-v2 §0 B10).
    if is_well_known_password(payload.new_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, WELL_KNOWN_DETAIL)
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False  # self-service change satisfies the flag
    session.add(user)
    # Revoke every other session of this account; the current one stays valid.
    current_hash = token_hash(credentials.credentials)
    for token in session.exec(
        select(AuthToken).where(AuthToken.user_id == user.id)
    ).all():
        if token.token != current_hash:
            session.delete(token)
    session.commit()


@router.post("/change-phone", response_model=UserOut)
def change_phone(
    payload: ChangePhoneIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Set or clear the caller's contact phone.

    Session-only — NO password re-entry (coordinator decision 3): a phone
    is contact info, not a credential; a stolen session can at worst change
    a contact field, the same exposure class as saying it at the counter.
    The schema normalises (optional '+', 6-15 digits) or clears (""/null).
    Idempotent: setting the current value is a plain 200 (no credential
    semantics, no revocation — the deliberate contrast with change-email's
    no-op 400). The full UserOut returns so the SPA refreshes in one
    assignment (the Task-1 mutator contract).
    """
    if payload.phone != user.phone:  # normalise-then-compare: exact no-op
        user.phone = payload.phone
        session.add(user)
        session.commit()
    return UserOut.model_validate(user)


@router.post("/change-email", response_model=UserOut)
def change_email(
    payload: ChangeEmailIn,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Change the caller's EMAIL — the login identifier, therefore the
    account-takeover surface (coordinator decision 4):

    1. the CURRENT PASSWORD is verified first (a stolen session must not be
       able to move the login identity); the taken-email 409 is only served
       to callers who proved credential possession;
    2. a NO-OP (new == current after normalisation) is a 400 — it must not
       look like a change, and a "successful" one would revoke every other
       session for nothing (the change-password no-op doctrine);
    3. every OTHER session is revoked, the presented one survives
       (change-password parity — the identity moved; old-identity sessions
       should not silently persist);
    4. the BADGE is never touched: derivation keys on user_id (Task A),
       pinned in tests/test_profile.py.
    """
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Current password is incorrect"
        )
    email = payload.new_email.strip().lower()
    if email == user.email:  # normalise-then-compare, BEFORE any revocation
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "New email must differ from the current email",
        )
    existing = session.exec(select(User).where(User.email == email)).first()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    user.email = email
    session.add(user)
    # Revoke every other session; the presented one stays valid (the
    # change_password loop, verbatim — keyed on the presented token's hash).
    current_hash = token_hash(credentials.credentials)
    for token in session.exec(
        select(AuthToken).where(AuthToken.user_id == user.id)
    ).all():
        if token.token != current_hash:
            session.delete(token)
    try:
        session.commit()
    except IntegrityError:
        # Concurrent change raced past the pre-check (register's pattern:
        # the pre-check is courtesy, the unique index is the law).
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    return UserOut.model_validate(user)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return UserOut.model_validate(user)
