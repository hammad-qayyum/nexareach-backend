from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from app.api.deps import DB, CurrentActor, CurrentUser, EditorUser
from app.api.schemas import Page
from app.models import DuplicateStatus, Lead, LeadDuplicate, LeadGroup
from app.services.duplicates import LEAD_FIELDS, Decision, resolve

router = APIRouter(prefix="/duplicates", tags=["duplicates"])

MAX_IDS = 1000


class LeadData(BaseModel):
    email: str
    first_name: str | None
    last_name: str | None
    company: str | None
    country: str | None
    timezone: str | None
    custom: dict[str, Any]


class ExistingLead(LeadData):
    id: int
    created_at: datetime


class GroupRef(BaseModel):
    id: int
    name: str


class DuplicateOut(BaseModel):
    id: int
    status: DuplicateStatus
    group: GroupRef
    row_number: int
    new: LeadData
    existing: ExistingLead
    # Fields whose values differ between the existing lead and the new row ("custom.industry" for extras).
    differences: list[str]
    created_at: datetime
    decided_at: datetime | None


class CountOut(BaseModel):
    pending: int


class ResolveIn(BaseModel):
    decision: Decision
    ids: list[int] = Field(default_factory=list, max_length=MAX_IDS)
    # Decide every pending duplicate (optionally only one group's) instead of listing ids.
    all_pending: bool = False
    group_id: int | None = None


class ResolveOut(BaseModel):
    resolved: int


def _data(x: Lead | LeadDuplicate) -> dict[str, Any]:
    return {"email": x.email, **{f: getattr(x, f) for f in LEAD_FIELDS}, "custom": dict(x.custom or {})}


def _differences(old: Lead, new: LeadDuplicate) -> list[str]:
    diff = [f for f in LEAD_FIELDS if (getattr(old, f) or None) != (getattr(new, f) or None)]
    old_custom, new_custom = old.custom or {}, new.custom or {}
    diff += [
        f"custom.{k}"
        for k in sorted(set(old_custom) | set(new_custom))
        if old_custom.get(k) != new_custom.get(k)
    ]
    return diff


def _out(d: LeadDuplicate) -> DuplicateOut:
    old = d.existing_lead
    return DuplicateOut(
        id=d.id,
        status=d.status,
        group=GroupRef(id=d.group.id, name=d.group.name),
        row_number=d.row_number,
        new=LeadData(**_data(d)),
        existing=ExistingLead(id=old.id, created_at=old.created_at, **_data(old)),
        differences=_differences(old, d),
        created_at=d.created_at,
        decided_at=d.decided_at,
    )


@router.get("", response_model=Page[DuplicateOut])
def list_duplicates(
    db: DB,
    _: CurrentUser,
    state: Annotated[Literal["pending", "decided"], Query(alias="status")] = "pending",
    group_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[DuplicateOut]:
    query = select(LeadDuplicate).where(
        LeadDuplicate.status == DuplicateStatus.PENDING
        if state == "pending"
        else LeadDuplicate.status != DuplicateStatus.PENDING
    )
    if group_id is not None:
        query = query.where(LeadDuplicate.group_id == group_id)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    order = (
        (LeadDuplicate.id,)
        if state == "pending"
        else (LeadDuplicate.decided_at.desc(), LeadDuplicate.id.desc())
    )
    rows = db.scalars(
        query.options(joinedload(LeadDuplicate.group), joinedload(LeadDuplicate.existing_lead))
        .order_by(*order)
        .limit(limit)
        .offset(offset)
    )
    return Page(items=[_out(d) for d in rows], total=total)


@router.get("/count", response_model=CountOut)
def count_pending(db: DB, _: CurrentUser) -> CountOut:
    pending = db.scalar(
        select(func.count()).select_from(LeadDuplicate).where(LeadDuplicate.status == DuplicateStatus.PENDING)
    )
    return CountOut(pending=pending)


@router.post("/resolve", response_model=ResolveOut)
def resolve_duplicates(body: ResolveIn, db: DB, user: EditorUser, actor: CurrentActor) -> ResolveOut:
    if not body.all_pending and not body.ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "nothing_selected")
    if body.group_id is not None and db.get(LeadGroup, body.group_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "group_not_found")

    query = select(LeadDuplicate).where(LeadDuplicate.status == DuplicateStatus.PENDING)
    if not body.all_pending:
        query = query.where(LeadDuplicate.id.in_(body.ids))
    if body.group_id is not None:
        query = query.where(LeadDuplicate.group_id == body.group_id)
    # Locked so two people deciding at once can't both apply a decision to the same row.
    duplicates = db.scalars(
        query.options(joinedload(LeadDuplicate.existing_lead))
        .order_by(LeadDuplicate.id)
        .with_for_update(of=LeadDuplicate)
    ).all()
    for dup in duplicates:
        resolve(db, dup, body.decision, actor, user.id)
    db.commit()
    return ResolveOut(resolved=len(duplicates))
