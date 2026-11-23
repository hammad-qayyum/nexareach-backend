import json
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import Select, Text, cast, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.api.deps import DB, CurrentActor, CurrentUser
from app.api.schemas import ActivityLogOut, Page
from app.models import ActivityLog, Mailbox
from app.services.audit import log_activity
from app.services.csv_export import csv_response

router = APIRouter(prefix="/logs", tags=["logs"])

EXPORT_LIMIT = 50_000


# Things that went wrong, for the "Problems only" filter.
PROBLEM_ACTIONS = (
    "email.failed",
    "email.bounced",
    "email.deferred",
    "email.uncertain",
    "mailbox.login_failed",
    "mailbox.campaigns_paused",
    "auth.login_failed",
    "warmup.failed",
    "warmup.helper_failed",
    "reply.answer_failed",
    "user.invite_email_failed",
    "alert.opened",
)


class LogFilters(BaseModel):
    # Words anywhere in the entry: who, the action, the item, the details (an email address, a subject…), IP.
    q: str | None = Field(default=None, max_length=200)
    # Each is "any of these"; filters of different kinds combine with AND.
    actor_user_id: list[int] = Field(default_factory=list)
    # USER (people) or SYSTEM (the automatic sender, checks…).
    actor_type: Literal["USER", "SYSTEM"] | None = None
    # Exact actions ("campaign.started") or prefixes ending in "." ("campaign.").
    action: list[str] = Field(default_factory=list)
    campaign_id: list[int] = Field(default_factory=list)
    mailbox_id: list[int] = Field(default_factory=list)
    problems: bool = False
    entity_type: str | None = None
    entity_id: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None


def _detail(key: str) -> ColumnElement:
    return ActivityLog.after[key].astext


def _apply(db: Session, query: Select, f: LogFilters) -> Select:
    if f.actor_user_id:
        query = query.where(ActivityLog.actor_user_id.in_(f.actor_user_id))
    if f.actor_type:
        query = query.where(ActivityLog.actor_type == f.actor_type)
    if f.action:
        query = query.where(
            or_(
                *(
                    ActivityLog.action.startswith(a, autoescape=True)
                    if a.endswith(".")
                    else ActivityLog.action == a
                    for a in f.action
                )
            )
        )
    if f.problems:
        query = query.where(ActivityLog.action.in_(PROBLEM_ACTIONS))
    if f.campaign_id:
        ids = [str(i) for i in f.campaign_id]
        query = query.where(
            or_(
                (ActivityLog.entity_type == "campaign") & ActivityLog.entity_id.in_(ids),
                _detail("campaign_id").in_(ids),
            )
        )
    if f.mailbox_id:
        ids = [str(i) for i in f.mailbox_id]
        emails = list(db.scalars(select(Mailbox.email).where(Mailbox.id.in_(f.mailbox_id))))
        query = query.where(
            or_(
                (ActivityLog.entity_type == "mailbox") & ActivityLog.entity_id.in_(ids),
                _detail("mailbox_id").in_(ids),
                _detail("mailbox").in_(emails),
            )
        )
    if f.q and f.q.strip():
        words = f.q.strip()
        query = query.where(
            or_(
                ActivityLog.actor_label.icontains(words, autoescape=True),
                ActivityLog.action.icontains(words, autoescape=True),
                ActivityLog.entity_type.icontains(words, autoescape=True),
                ActivityLog.entity_id.icontains(words, autoescape=True),
                ActivityLog.ip.icontains(words, autoescape=True),
                cast(ActivityLog.after, Text).icontains(words, autoescape=True),
                cast(ActivityLog.before, Text).icontains(words, autoescape=True),
            )
        )
    if f.entity_type:
        query = query.where(ActivityLog.entity_type == f.entity_type)
    if f.entity_id:
        query = query.where(ActivityLog.entity_id == f.entity_id)
    if f.date_from:
        query = query.where(ActivityLog.created_at >= f.date_from)
    if f.date_to:
        query = query.where(ActivityLog.created_at < f.date_to)
    return query


Filters = Annotated[LogFilters, Query()]


@router.get("", response_model=Page[ActivityLogOut])
def list_logs(
    db: DB,
    _: CurrentUser,
    filters: Filters,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ActivityLogOut]:
    total = db.scalar(_apply(db, select(func.count()).select_from(ActivityLog), filters))
    rows = db.scalars(
        _apply(db, select(ActivityLog), filters).order_by(ActivityLog.id.desc()).limit(limit).offset(offset)
    )
    return Page(items=[ActivityLogOut.model_validate(r) for r in rows], total=total)


@router.get("/actions", response_model=list[str])
def list_actions(db: DB, _: CurrentUser) -> list[str]:
    """Distinct action names, for the filter dropdown."""
    return list(db.scalars(select(ActivityLog.action).distinct().order_by(ActivityLog.action)))


@router.get("/export")
def export_logs(db: DB, actor: CurrentActor, filters: Filters) -> StreamingResponse:
    rows = list(
        db.scalars(
            _apply(db, select(ActivityLog), filters).order_by(ActivityLog.id.desc()).limit(EXPORT_LIMIT)
        )
    )
    log_activity(
        db,
        actor,
        "logs.exported",
        after={"filters": filters.model_dump(exclude_none=True), "rows": len(rows)},
    )
    db.commit()

    return csv_response(
        f"activity-log-{datetime.now():%Y%m%d-%H%M}.csv",
        ["id", "time", "actor", "action", "entity_type", "entity_id", "before", "after", "ip"],
        (
            [
                r.id,
                r.created_at.isoformat(),
                r.actor_label,
                r.action,
                r.entity_type,
                r.entity_id,
                json.dumps(r.before) if r.before is not None else None,
                json.dumps(r.after) if r.after is not None else None,
                r.ip,
            ]
            for r in rows
        ),
    )
