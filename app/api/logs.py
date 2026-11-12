import csv
import io
import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import Select, func, select

from app.api.deps import DB, CurrentActor, CurrentUser
from app.api.schemas import ActivityLogOut, Page
from app.models import ActivityLog
from app.services.audit import log_activity

router = APIRouter(prefix="/logs", tags=["logs"])

EXPORT_LIMIT = 50_000


class LogFilters(BaseModel):
    actor_user_id: int | None = None
    # Exact action ("campaign.started") or a prefix ending in "." ("campaign.")
    action: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None


def _apply(query: Select, f: LogFilters) -> Select:
    if f.actor_user_id is not None:
        query = query.where(ActivityLog.actor_user_id == f.actor_user_id)
    if f.action:
        if f.action.endswith("."):
            query = query.where(ActivityLog.action.startswith(f.action, autoescape=True))
        else:
            query = query.where(ActivityLog.action == f.action)
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
    total = db.scalar(_apply(select(func.count()).select_from(ActivityLog), filters))
    rows = db.scalars(
        _apply(select(ActivityLog), filters).order_by(ActivityLog.id.desc()).limit(limit).offset(offset)
    )
    return Page(items=[ActivityLogOut.model_validate(r) for r in rows], total=total)


@router.get("/actions", response_model=list[str])
def list_actions(db: DB, _: CurrentUser) -> list[str]:
    """Distinct action names, for the filter dropdown."""
    return list(db.scalars(select(ActivityLog.action).distinct().order_by(ActivityLog.action)))


def _cell(value: str) -> str:
    """Stop spreadsheet apps from running a value as a formula (CSV injection): "=…" becomes "'=…"."""
    return f"'{value}" if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


@router.get("/export")
def export_logs(db: DB, actor: CurrentActor, filters: Filters) -> StreamingResponse:
    rows = list(
        db.scalars(_apply(select(ActivityLog), filters).order_by(ActivityLog.id.desc()).limit(EXPORT_LIMIT))
    )
    log_activity(
        db,
        actor,
        "logs.exported",
        after={"filters": filters.model_dump(exclude_none=True), "rows": len(rows)},
    )
    db.commit()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "time", "actor", "action", "entity_type", "entity_id", "before", "after", "ip"])
    for r in rows:
        writer.writerow(
            [
                r.id,
                r.created_at.isoformat(),
                _cell(r.actor_label),
                _cell(r.action),
                _cell(r.entity_type or ""),
                _cell(r.entity_id or ""),
                _cell(json.dumps(r.before)) if r.before is not None else "",
                _cell(json.dumps(r.after)) if r.after is not None else "",
                _cell(r.ip or ""),
            ]
        )
    filename = f"activity-log-{datetime.now():%Y%m%d-%H%M}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
