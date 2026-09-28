"""Authentication endpoints: register, login, logout, whoami, password change."""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.database import get_session
from app.deps import bearer_scheme, get_current_user
from app.models import AuthToken, User, utcnow
from app.schemas import AuthOut, ChangePasswordIn, LoginIn, RegisterIn, UserOut
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
