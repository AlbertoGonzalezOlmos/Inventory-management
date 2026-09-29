"""Authentication endpoints: register, login (password & QR badge), logout,
whoami, password change."""

import re

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.database import get_session
from app.deps import bearer_scheme, get_current_user
from app.models import AuthToken, User, utcnow
from app.qrbadge import BADGE_PREFIX, badge_svg, new_badge_payload, parse_badge_payload
from app.schemas import (
    AuthOut,
    ChangePasswordIn,
    LoginIn,
    QrBadgeOut,
    QrLoginIn,
    RegisterIn,
    UserOut,
)
from app.security import DUMMY_HASH, hash_password, new_token, token_hash, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


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


@router.post("/register", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterIn, session: Session = Depends(get_session)):
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
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        # Concurrent registration with the same email raced past the check.
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    session.refresh(user)
    return _issue_token(session, user)


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


@router.post("/qr-login", response_model=AuthOut)
def qr_login(payload: QrLoginIn, session: Session = Depends(get_session)):
    """Log in with a QR badge instead of email+password.

    The badge is a bearer credential (256-bit random) — whoever holds the
    printed code holds the account, so treat badges like passwords: only the
    SHA-256 hash is stored, and a compromised badge must be revoked/rotated
    (Account → QR badge, or staff in Members).
    """
    badge = parse_badge_payload(payload.token)
    if badge is None and _RAW_TOKEN_RE.fullmatch(payload.token.strip()):
        badge = BADGE_PREFIX + payload.token.strip()
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
    stops working the moment the new hash is stored.
    """
    payload = new_badge_payload()
    user.qr_badge_hash = token_hash(payload)
    session.add(user)
    session.commit()
    return QrBadgeOut(payload=payload, svg=badge_svg(payload))


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


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return UserOut.model_validate(user)
