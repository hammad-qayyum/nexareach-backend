from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models import Role, User, UserSession
from app.services.audit import Actor
from app.services.auth import get_valid_session

SESSION_COOKIE = "fn_session"

DB = Annotated[Session, Depends(get_db)]


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def user_agent(request: Request) -> str | None:
    ua = request.headers.get("user-agent")
    return ua[:500] if ua else None


def anonymous_actor(request: Request, label: str) -> Actor:
    """Actor for actions before sign-in (e.g. a failed login), labelled with the email tried."""
    return Actor(user_id=None, label=label, ip=client_ip(request), user_agent=user_agent(request))


def get_session(request: Request, db: DB) -> UserSession:
    """The signed-in session. 2-step login may still be pending."""
    token = request.cookies.get(SESSION_COOKIE)
    session = get_valid_session(db, token) if token else None
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not_authenticated")
    return session


CurrentSession = Annotated[UserSession, Depends(get_session)]


def get_current_user(session: CurrentSession) -> User:
    """The signed-in user, after any required 2-step login."""
    if not session.mfa_passed:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "mfa_required")
    return session.user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_actor(request: Request, user: CurrentUser) -> Actor:
    return Actor(user_id=user.id, label=user.email, ip=client_ip(request), user_agent=user_agent(request))


CurrentActor = Annotated[Actor, Depends(get_actor)]


def require_role(*roles: Role) -> Callable[[User], User]:
    def check(user: CurrentUser) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
        return user

    return check


AdminUser = Annotated[User, Depends(require_role(Role.ADMIN))]
EditorUser = Annotated[User, Depends(require_role(Role.ADMIN, Role.MEMBER))]
