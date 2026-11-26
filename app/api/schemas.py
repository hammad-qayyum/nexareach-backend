from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models import DeliverySource, DeliveryStatus, Role, UserStatus


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    role: Role
    status: UserStatus
    totp_enabled: bool
    last_login_at: datetime | None
    created_at: datetime


class ActivityLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    actor_type: str
    actor_user_id: int | None
    actor_label: str
    action: str
    entity_type: str | None
    entity_id: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    ip: str | None
    user_agent: str | None
    created_at: datetime


class Page[T](BaseModel):
    items: list[T]
    total: int


class DeliveryEventOut(BaseModel):
    """One line of the mail-server log (send, cPanel delivery log, or a bounce email)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    occurred_at: datetime
    source: DeliverySource
    status: DeliveryStatus
    email: str
    remote_host: str | None
    response: str | None
    queue_id: str | None
    recipient_id: int | None
    follow_up_id: int | None
