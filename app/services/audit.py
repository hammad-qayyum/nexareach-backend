"""Activity logging. Call log_activity() in the same transaction as the change it records."""

import enum
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import ActivityLog


@dataclass(frozen=True)
class Actor:
    """Who is doing something: a signed-in user, or the system (worker, scheduled jobs)."""

    user_id: int | None
    label: str
    ip: str | None = None
    user_agent: str | None = None

    @property
    def type(self) -> str:
        return "USER" if self.user_id is not None else "SYSTEM"


SYSTEM = Actor(user_id=None, label="system")


def log_activity(
    db: Session,
    actor: Actor,
    action: str,
    *,
    entity_type: str | None = None,
    entity_id: Any = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> ActivityLog:
    entry = ActivityLog(
        actor_type=actor.type,
        actor_user_id=actor.user_id,
        actor_label=actor.label,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        before=_json_safe(before),
        after=_json_safe(after),
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    db.add(entry)
    return entry


def snapshot(obj: Any, fields: list[str]) -> dict[str, Any]:
    """Pick fields from a model for a before/after record. Never include secrets."""
    return {f: getattr(obj, f) for f in fields}


def changes(before: dict[str, Any], after: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reduce two snapshots to only the fields that changed."""
    keys = [k for k in after if before.get(k) != after.get(k)]
    return {k: before.get(k) for k in keys}, {k: after[k] for k in keys}


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value
