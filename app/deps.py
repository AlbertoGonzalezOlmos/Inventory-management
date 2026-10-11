"""Shared FastAPI dependencies: current user and role guards."""

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session

from app.database import get_session
from app.models import AuthToken, User, utcnow
from app.security import token_hash

bearer_scheme = HTTPBearer(auto_error=False)

# Endpoints usable while a password change is still pending (must_change_password).
MUST_CHANGE_EXEMPT_PATHS = {
    "/api/auth/me",
    "/api/auth/logout",
    "/api/auth/change-password",
}


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: Session = Depends(get_session),
) -> User:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    # Tokens are stored as SHA-256 hashes; look up by hash of the presented one.
    auth_token = session.get(AuthToken, token_hash(credentials.credentials))
    now = utcnow()
    if auth_token is None or auth_token.expires_at < now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalid or expired",
        )

    # Lazy logoff expiry (Task 4): a pending countdown whose deadline has
    # passed kills the token even if the browser that armed it never comes
    # back — that is the whole point of server-side enforcement. Inclusive
    # comparison: the countdown reaching zero IS the logoff moment (unlike
    # expires_at, which keeps its strict <). Reject-AND-delete so the row
    # cannot linger or be resurrected (logout's end state); the delete fires
    # at most once per killed token — never on the hot path, which is why a
    # write here does not repeat the database.py begin-hook incident.
    if auth_token.logoff_deadline is not None and now >= auth_token.logoff_deadline:
        try:
            session.delete(auth_token)
            session.commit()
        except Exception:
            # The 401 must not depend on the cleanup write succeeding: the
            # deadline comparison is authoritative even if the delete fails.
            session.rollback()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalid or expired",  # uniform: no mechanism leak
        )

    user = session.get(User, auth_token.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer exists"
        )
    # Force the password change before anything else: applies to the seeded
    # default admin and to admin-issued password resets.
    if user.must_change_password and request.url.path not in MUST_CHANGE_EXEMPT_PATHS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password change required: change your password "
            "(Account → Change password) before using the app",
        )
    return user


def require_staff(user: User = Depends(get_current_user)) -> User:
    if user.role not in ("staff", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Staff privileges required",
        )
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return user
