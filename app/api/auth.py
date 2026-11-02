from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DB, SESSION_COOKIE, CurrentSession, anonymous_actor, client_ip, user_agent
from app.api.schemas import UserOut
from app.core.config import get_settings
from app.core.crypto import decrypt, encrypt
from app.core.security import (
    MIN_PASSWORD_LENGTH,
    hash_password,
    new_totp_secret,
    totp_uri,
    verify_password,
    verify_totp,
)
from app.models import User, UserSession, UserStatus
from app.services import auth as auth_service
from app.services.audit import Actor, log_activity

router = APIRouter(prefix="/auth", tags=["auth"])

MfaState = Literal["passed", "verify", "setup"]


class LoginIn(BaseModel):
    email: str
    password: str


class LoginOut(BaseModel):
    mfa: MfaState


class MeOut(BaseModel):
    user: UserOut
    mfa: MfaState


class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class TotpSetupOut(BaseModel):
    secret: str
    otpauth_uri: str


class InviteInfoOut(BaseModel):
    name: str
    email: str


class AcceptInviteIn(BaseModel):
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)


def _mfa_state(session: UserSession) -> MfaState:
    if session.mfa_passed:
        return "passed"
    return "verify" if session.user.totp_enabled else "setup"


def _user_actor(request: Request, user: User) -> Actor:
    return Actor(user_id=user.id, label=user.email, ip=client_ip(request), user_agent=user_agent(request))


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn, request: Request, response: Response, db: DB) -> LoginOut:
    email = auth_service.normalize_email(body.email)
    ip = client_ip(request)

    if auth_service.is_rate_limited(db, email, ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too_many_attempts")

    user = db.scalar(select(User).where(User.email == email))
    password_ok = verify_password(user.password_hash if user else None, body.password)
    if user is None or not password_ok or user.status != UserStatus.ACTIVE:
        auth_service.record_attempt(db, email, ip, "PASSWORD", success=False)
        reason = "bad_password" if user and not password_ok else ("unknown_user" if not user else "inactive")
        log_activity(
            db,
            anonymous_actor(request, email),
            "auth.login_failed",
            entity_type="user" if user else None,
            entity_id=user.id if user else None,
            after={"reason": reason},
        )
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_credentials")

    auth_service.record_attempt(db, email, ip, "PASSWORD", success=True)
    mfa_needed = auth_service.needs_mfa(user)
    token = auth_service.create_session(
        db, user, mfa_passed=not mfa_needed, ip=ip, user_agent=user_agent(request)
    )
    if not mfa_needed:
        user.last_login_at = auth_service.now()
        log_activity(db, _user_actor(request, user), "auth.login", entity_type="user", entity_id=user.id)
    db.commit()

    _set_session_cookie(response, token)
    if not mfa_needed:
        return LoginOut(mfa="passed")
    return LoginOut(mfa="verify" if user.totp_enabled else "setup")


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: DB) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    session = auth_service.get_valid_session(db, token) if token else None
    if session is not None:
        auth_service.revoke_session(db, session)
        log_activity(
            db,
            _user_actor(request, session.user),
            "auth.logout",
            entity_type="user",
            entity_id=session.user_id,
        )
        db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", response_model=MeOut)
def me(session: CurrentSession) -> MeOut:
    return MeOut(user=UserOut.model_validate(session.user), mfa=_mfa_state(session))


@router.post("/2fa/setup", response_model=TotpSetupOut)
def totp_setup(session: CurrentSession, db: DB) -> TotpSetupOut:
    """Start (or restart) 2-step login setup. Allowed only while the user has no 2FA yet."""
    user = session.user
    if user.totp_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "already_enabled")
    secret = new_totp_secret()
    user.totp_secret_encrypted = encrypt(secret)
    user.totp_last_used_step = None
    db.commit()
    return TotpSetupOut(secret=secret, otpauth_uri=totp_uri(secret, user.email))


@router.post("/2fa/enable", response_model=LoginOut)
def totp_enable(body: CodeIn, session: CurrentSession, request: Request, db: DB) -> LoginOut:
    user = session.user
    if user.totp_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "already_enabled")
    if not user.totp_secret_encrypted:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "setup_not_started")
    _check_totp(db, request, user, body.code)

    user.totp_enabled = True
    actor = _user_actor(request, user)
    log_activity(db, actor, "auth.2fa_enabled", entity_type="user", entity_id=user.id)
    if not session.mfa_passed:
        session.mfa_passed = True
        user.last_login_at = auth_service.now()
        log_activity(db, actor, "auth.login", entity_type="user", entity_id=user.id)
    db.commit()
    return LoginOut(mfa="passed")


@router.post("/2fa/verify", response_model=LoginOut)
def totp_verify(body: CodeIn, session: CurrentSession, request: Request, db: DB) -> LoginOut:
    user = session.user
    if not user.totp_enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "not_enabled")
    if session.mfa_passed:
        return LoginOut(mfa="passed")
    _check_totp(db, request, user, body.code)

    session.mfa_passed = True
    user.last_login_at = auth_service.now()
    log_activity(db, _user_actor(request, user), "auth.login", entity_type="user", entity_id=user.id)
    db.commit()
    return LoginOut(mfa="passed")


@router.get("/invites/{token}", response_model=InviteInfoOut)
def invite_info(token: str, db: DB) -> InviteInfoOut:
    invite = auth_service.find_invite(db, token)
    if invite is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite_invalid")
    return InviteInfoOut(name=invite.user.name, email=invite.user.email)


@router.post("/invites/{token}/accept", status_code=status.HTTP_204_NO_CONTENT)
def accept_invite(token: str, body: AcceptInviteIn, request: Request, db: DB) -> None:
    invite = auth_service.find_invite(db, token)
    if invite is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite_invalid")
    user = invite.user
    user.password_hash = hash_password(body.password)
    user.status = UserStatus.ACTIVE
    invite.used_at = auth_service.now()
    log_activity(
        db,
        _user_actor(request, user),
        "user.invite_accepted",
        entity_type="user",
        entity_id=user.id,
        before={"status": UserStatus.INVITED},
        after={"status": UserStatus.ACTIVE},
    )
    db.commit()


def _check_totp(db: DB, request: Request, user: User, code: str) -> None:
    ip = client_ip(request)
    if auth_service.is_rate_limited(db, user.email, ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too_many_attempts")
    step = verify_totp(decrypt(user.totp_secret_encrypted), code, user.totp_last_used_step)
    if step is None:
        auth_service.record_attempt(db, user.email, ip, "TOTP", success=False)
        log_activity(db, _user_actor(request, user), "auth.2fa_failed", entity_type="user", entity_id=user.id)
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_code")
    auth_service.record_attempt(db, user.email, ip, "TOTP", success=True)
    user.totp_last_used_step = step


def _set_session_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=s.session_max_days * 86400,
        httponly=True,
        secure=s.cookie_secure,
        samesite="lax",
        path="/",
    )
