"""Member management endpoints (staff/admin).

Privilege rules:
- staff may manage member and staff accounts, but can never touch an admin
  account, never grant the admin role, and never reset passwords;
- admins may do everything, except demote/delete the *last* remaining admin
  (so the system can never end up with zero admins).
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.database import get_session
from app.deps import require_admin, require_staff
from app.models import AuthToken, User
from app.qrbadge import badge_svg, new_badge_payload
from app.schemas import MemberIn, MemberPatch, QrBadgeOut, UserOut
from app.security import hash_password, is_well_known_password, token_hash

router = APIRouter(prefix="/api/members", tags=["members"])

VALID_ROLES = ("member", "staff", "admin")

WELL_KNOWN_DETAIL = (
    "That password is published in this project's documentation/seed code; "
    "choose another one"
)


def _admin_count(session: Session) -> int:
    return len(session.exec(select(User).where(User.role == "admin")).all())


@router.get("", response_model=list[UserOut])
def list_members(
    staff: User = Depends(require_staff),
    session: Session = Depends(get_session),
):
    users = session.exec(select(User).order_by(User.id)).all()
    return [UserOut.model_validate(u) for u in users]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_member(
    payload: MemberIn,
    staff: User = Depends(require_staff),
    session: Session = Depends(get_session),
):
    """Staff creates accounts for shop members (optionally other staff)."""
    if payload.role not in VALID_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid role")
    if is_well_known_password(payload.password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, WELL_KNOWN_DETAIL)
    if payload.role == "admin" and staff.role != "admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only admins can create admin accounts"
        )

    email = payload.email.strip().lower()
    if session.exec(select(User).where(User.email == email)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    user = User(
        email=email,
        name=payload.name.strip(),
        password_hash=hash_password(payload.password),
        role=payload.role,
        # A staff- or admin-issued password is a *temporary* password — the UI
        # has always labelled the field that way, but the flag was never set, so
        # the account worked indefinitely on a password somebody else chose and
        # knows (PLAN-v2 §0 B9 / finding G). Blocked from the API until the
        # holder changes it, exactly like an admin-issued reset.
        must_change_password=True,
    )
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    session.refresh(user)
    return UserOut.model_validate(user)


@router.patch("/{user_id}", response_model=UserOut)
def update_member(
    user_id: int,
    payload: MemberPatch,
    staff: User = Depends(require_staff),
    session: Session = Depends(get_session),
):
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")

    changes = payload.model_dump(exclude_unset=True)
    actor_is_admin = staff.role == "admin"

    # Staff can never modify an admin account.
    if user.role == "admin" and not actor_is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Admin accounts can only be modified by admins"
        )

    new_role = changes.get("role")
    if new_role is not None:
        if new_role not in VALID_ROLES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid role")
        if new_role == "admin" and not actor_is_admin:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Only admins can grant the admin role"
            )
        if (
            user.role == "admin"
            and new_role != "admin"
            and _admin_count(session) <= 1
        ):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Cannot demote the last admin"
            )

    password = changes.pop("password", None)
    if password is not None:
        if not actor_is_admin:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Only admins can reset passwords"
            )
        if is_well_known_password(password):
            # An admin reset used to accept `changeme` (200), re-arming the
            # well-known default on any account (PLAN-v2 §0 B10).
            raise HTTPException(status.HTTP_400_BAD_REQUEST, WELL_KNOWN_DETAIL)
        if user.id == staff.id:
            # Reject self-service reset here: this path does not verify the
            # current password, so allowing it (an earlier revision spared the
            # caller's session) would let a stolen session change the password
            # and persist while locking out the real owner. Own-password
            # changes must go through POST /api/auth/change-password, which
            # requires the current password.
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Use POST /api/auth/change-password to change your own password "
                "(it verifies your current password)",
            )

    for field, value in changes.items():
        setattr(user, field, value)

    if password is not None:
        user.password_hash = hash_password(password)
        # The new password is admin-issued ("temporary"): force a change at
        # next use, and revoke every session of the account.
        user.must_change_password = True
        for token in session.exec(
            select(AuthToken).where(AuthToken.user_id == user.id)
        ).all():
            session.delete(token)

    session.add(user)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        # Backstop fired: the DB trigger rejected a concurrent last-admin
        # demotion that raced past the check above.
        if "last admin" in str(exc):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Cannot demote the last admin"
            )
        raise
    session.refresh(user)
    return UserOut.model_validate(user)


def _get_manageable_member(session: Session, user_id: int, actor: User) -> User:
    """Fetch a member for badge management, applying the same privilege rule
    as account edits: staff can never touch an admin account."""
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
    if user.role == "admin" and actor.role != "admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Admin accounts can only be managed by admins"
        )
    return user


@router.post(
    "/{user_id}/qr-badge",
    response_model=QrBadgeOut,
    status_code=status.HTTP_201_CREATED,
)
def create_member_badge(
    user_id: int,
    staff: User = Depends(require_staff),
    session: Session = Depends(get_session),
):
    """Generate (or replace) a member's QR login badge — e.g. to print a
    membership card at the counter. Replacing revokes the previous badge."""
    user = _get_manageable_member(session, user_id, staff)
    payload = new_badge_payload()
    user.qr_badge_hash = token_hash(payload)
    session.add(user)
    session.commit()
    return QrBadgeOut(payload=payload, svg=badge_svg(payload))


@router.delete("/{user_id}/qr-badge", status_code=status.HTTP_204_NO_CONTENT)
def revoke_member_badge(
    user_id: int,
    staff: User = Depends(require_staff),
    session: Session = Depends(get_session),
):
    """Revoke a member's QR badge (lost/stolen card)."""
    user = _get_manageable_member(session, user_id, staff)
    user.qr_badge_hash = None
    session.add(user)
    session.commit()


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_member(
    user_id: int,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
):
    if user_id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot delete yourself")

    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")

    if user.role == "admin" and _admin_count(session) <= 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Cannot delete the last admin"
        )

    for token in session.exec(
        select(AuthToken).where(AuthToken.user_id == user_id)
    ).all():
        session.delete(token)
    session.delete(user)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if "last admin" in str(exc):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Cannot delete the last admin"
            )
        raise
