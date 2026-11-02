"""Sessions, login rate limiting and invite tokens."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_token, new_token
from app.models import LoginAttempt, Role, TokenPurpose, User, UserSession, UserStatus, UserToken

# Only refresh last_seen_at / expiry this often, to avoid a write on every request.
_TOUCH_INTERVAL = timedelta(minutes=5)


def now() -> datetime:
    return datetime.now(UTC)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def needs_mfa(user: User) -> bool:
    """Admins must use 2-step login. Other roles only if they turned it on."""
    return user.role == Role.ADMIN or user.totp_enabled


# --- Rate limiting -----------------------------------------------------------------------------


def record_attempt(db: Session, email: str, ip: str | None, kind: str, success: bool) -> None:
    db.add(LoginAttempt(email=normalize_email(email), ip=ip, kind=kind, success=success))


def is_rate_limited(db: Session, email: str, ip: str | None) -> bool:
    """True when this email or IP has too many recent failures (password or 2FA)."""
    s = get_settings()
    since = now() - timedelta(minutes=s.login_window_minutes)
    failures = (
        select(func.count())
        .select_from(LoginAttempt)
        .where(LoginAttempt.success.is_(False), LoginAttempt.created_at >= since)
    )
    by_email = db.scalar(failures.where(LoginAttempt.email == normalize_email(email)))
    if by_email >= s.login_max_failures_per_email:
        return True
    if ip:
        by_ip = db.scalar(failures.where(LoginAttempt.ip == ip))
        if by_ip >= s.login_max_failures_per_ip:
            return True
    return False


# --- Sessions ----------------------------------------------------------------------------------


def create_session(
    db: Session, user: User, *, mfa_passed: bool, ip: str | None, user_agent: str | None
) -> str:
    token = new_token()
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_token(token),
            mfa_passed=mfa_passed,
            ip=ip,
            user_agent=(user_agent or "")[:500] or None,
            expires_at=now() + timedelta(hours=get_settings().session_idle_hours),
        )
    )
    return token


def get_valid_session(db: Session, token: str) -> UserSession | None:
    s = get_settings()
    session = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
    if session is None or session.revoked_at is not None:
        return None
    current = now()
    if session.expires_at <= current or session.created_at + timedelta(days=s.session_max_days) <= current:
        return None
    if session.user.status != UserStatus.ACTIVE:
        return None
    if current - session.last_seen_at >= _TOUCH_INTERVAL:
        session.last_seen_at = current
        session.expires_at = current + timedelta(hours=s.session_idle_hours)
        db.commit()
    return session


def revoke_session(db: Session, session: UserSession) -> None:
    session.revoked_at = now()


def revoke_user_sessions(db: Session, user_id: int) -> None:
    db.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now())
    )


# --- Invites -----------------------------------------------------------------------------------


def create_invite_token(db: Session, user: User, created_by: User) -> str:
    """Issue a fresh invite link. Any earlier unused link for this user stops working."""
    db.execute(
        update(UserToken)
        .where(
            UserToken.user_id == user.id,
            UserToken.purpose == TokenPurpose.INVITE,
            UserToken.used_at.is_(None),
        )
        .values(used_at=now())
    )
    token = new_token()
    db.add(
        UserToken(
            user_id=user.id,
            purpose=TokenPurpose.INVITE,
            token_hash=hash_token(token),
            created_by_id=created_by.id,
            expires_at=now() + timedelta(days=get_settings().invite_valid_days),
        )
    )
    return token


def find_invite(db: Session, token: str) -> UserToken | None:
    invite = db.scalar(
        select(UserToken).where(
            UserToken.token_hash == hash_token(token), UserToken.purpose == TokenPurpose.INVITE
        )
    )
    if invite is None or invite.used_at is not None or invite.expires_at <= now():
        return None
    if invite.user.status != UserStatus.INVITED:
        return None
    return invite


def invite_url(token: str) -> str:
    return f"{get_settings().app_base_url}/invite/{token}"
