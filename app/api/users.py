from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from app.api.deps import DB, AdminUser, CurrentActor
from app.api.schemas import UserOut
from app.models import Role, User, UserStatus
from app.services import auth as auth_service
from app.services.audit import changes, log_activity, snapshot

router = APIRouter(prefix="/users", tags=["users"])

AUDIT_FIELDS = ["name", "email", "role", "status", "totp_enabled"]


class InviteIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    role: Role


class InviteOut(BaseModel):
    user: UserOut
    invite_url: str


class UserUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    role: Role | None = None
    status: UserStatus | None = None


class InviteLinkOut(BaseModel):
    invite_url: str


@router.get("", response_model=list[UserOut])
def list_users(db: DB, _: AdminUser) -> list[User]:
    return list(db.scalars(select(User).order_by(User.created_at)))


@router.post("/invite", response_model=InviteOut, status_code=status.HTTP_201_CREATED)
def invite_user(body: InviteIn, db: DB, admin: AdminUser, actor: CurrentActor) -> InviteOut:
    email = auth_service.normalize_email(body.email)
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "email_exists")
    user = User(
        name=body.name.strip(), email=email, role=body.role, status=UserStatus.INVITED, invited_by_id=admin.id
    )
    db.add(user)
    db.flush()
    token = auth_service.create_invite_token(db, user, admin)
    log_activity(
        db, actor, "user.invited", entity_type="user", entity_id=user.id, after=snapshot(user, AUDIT_FIELDS)
    )
    db.commit()
    return InviteOut(user=UserOut.model_validate(user), invite_url=auth_service.invite_url(token))


@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: int, body: UserUpdateIn, db: DB, admin: AdminUser, actor: CurrentActor) -> User:
    user = _get_user(db, user_id)
    if user.id == admin.id and (body.role is not None or body.status is not None):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot_change_own_role_or_status")
    if body.status == UserStatus.INVITED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_status")
    if body.status in (UserStatus.ACTIVE, UserStatus.PAUSED) and user.password_hash is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invite_not_accepted")

    before = snapshot(user, AUDIT_FIELDS)
    if body.name is not None:
        user.name = body.name.strip()
    if body.role is not None:
        user.role = body.role
    if body.status is not None:
        user.status = body.status
    after = snapshot(user, AUDIT_FIELDS)
    if before == after:
        return user

    # Role or access changes take effect immediately: sign the user out everywhere.
    if before["role"] != after["role"] or before["status"] != after["status"]:
        auth_service.revoke_user_sessions(db, user.id)
    old, new = changes(before, after)
    log_activity(db, actor, "user.updated", entity_type="user", entity_id=user.id, before=old, after=new)
    db.commit()
    return user


@router.post("/{user_id}/resend-invite", response_model=InviteLinkOut)
def resend_invite(user_id: int, db: DB, admin: AdminUser, actor: CurrentActor) -> InviteLinkOut:
    user = _get_user(db, user_id)
    if user.status != UserStatus.INVITED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "not_invited")
    token = auth_service.create_invite_token(db, user, admin)
    log_activity(db, actor, "user.invite_resent", entity_type="user", entity_id=user.id)
    db.commit()
    return InviteLinkOut(invite_url=auth_service.invite_url(token))


@router.post("/{user_id}/reset-2fa", response_model=UserOut)
def reset_2fa(user_id: int, db: DB, admin: AdminUser, actor: CurrentActor) -> User:
    """For a user who lost their authenticator. They set it up again at next sign-in."""
    user = _get_user(db, user_id)
    if user.id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot_reset_own_2fa")
    if not user.totp_enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "not_enabled")
    user.totp_enabled = False
    user.totp_secret_encrypted = None
    user.totp_last_used_step = None
    auth_service.revoke_user_sessions(db, user.id)
    log_activity(
        db,
        actor,
        "user.2fa_reset",
        entity_type="user",
        entity_id=user.id,
        before={"totp_enabled": True},
        after={"totp_enabled": False},
    )
    db.commit()
    return user


def _get_user(db: DB, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user_not_found")
    return user
