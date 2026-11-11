import re
from datetime import datetime
from typing import Annotated, Any

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.api.deps import DB, CurrentActor, CurrentUser, EditorUser
from app.api.schemas import Page
from app.models import Lead, LeadGroup, campaign_groups, lead_group_members
from app.services.audit import log_activity
from app.services.auth import normalize_email
from app.services.templates import FIXED_FIELDS

router = APIRouter(prefix="/groups", tags=["groups"])

MAX_ROWS = 20_000
CUSTOM_KEY = re.compile(r"^[a-z][a-z0-9_]{0,49}$")
# Invalid rows reported back in detail; the rest are only counted.
MAX_INVALID_LISTED = 200


class GroupOut(BaseModel):
    id: int
    name: str
    source: str | None
    lead_count: int
    created_at: datetime


class LeadOut(BaseModel):
    id: int
    email: str
    first_name: str | None
    last_name: str | None
    company: str | None
    country: str | None
    custom: dict[str, Any]
    created_at: datetime


class LeadRowIn(BaseModel):
    email: str = Field(max_length=320)
    first_name: str | None = Field(default=None, max_length=200)
    last_name: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=300)
    country: str | None = Field(default=None, max_length=100)
    custom: dict[str, str] = Field(default_factory=dict)


class ImportIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source: str | None = Field(default=None, max_length=200)
    rows: list[LeadRowIn] = Field(min_length=1, max_length=MAX_ROWS)


class InvalidRow(BaseModel):
    row: int
    email: str
    reason: str


class ImportOut(BaseModel):
    group: GroupOut
    added: int
    existing: int
    duplicates_in_file: int
    invalid_count: int
    invalid: list[InvalidRow]


def _clean(value: str | None) -> str | None:
    value = " ".join(value.split()) if value else ""
    return value or None


def _counts(db: DB, group_ids: list[int]) -> dict[int, int]:
    rows = db.execute(
        select(lead_group_members.c.group_id, func.count())
        .where(lead_group_members.c.group_id.in_(group_ids))
        .group_by(lead_group_members.c.group_id)
    )
    return dict(rows.tuples().all())


def _group_out(group: LeadGroup, count: int) -> GroupOut:
    return GroupOut(
        id=group.id, name=group.name, source=group.source, lead_count=count, created_at=group.created_at
    )


def _get(db: DB, group_id: int) -> LeadGroup:
    group = db.get(LeadGroup, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "group_not_found")
    return group


@router.get("", response_model=list[GroupOut])
def list_groups(db: DB, _: CurrentUser) -> list[GroupOut]:
    groups = db.scalars(select(LeadGroup).order_by(LeadGroup.created_at.desc())).all()
    counts = _counts(db, [g.id for g in groups])
    return [_group_out(g, counts.get(g.id, 0)) for g in groups]


@router.get("/{group_id}", response_model=GroupOut)
def get_group(group_id: int, db: DB, _: CurrentUser) -> GroupOut:
    group = _get(db, group_id)
    return _group_out(group, _counts(db, [group.id]).get(group.id, 0))


@router.get("/{group_id}/leads", response_model=Page[LeadOut])
def list_group_leads(
    group_id: int,
    db: DB,
    _: CurrentUser,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[LeadOut]:
    _get(db, group_id)
    query = (
        select(Lead)
        .join(lead_group_members, lead_group_members.c.lead_id == Lead.id)
        .where(lead_group_members.c.group_id == group_id)
    )
    if q:
        like = f"%{q.strip()}%"
        query = query.where(
            or_(
                Lead.email.ilike(like),
                Lead.first_name.ilike(like),
                Lead.last_name.ilike(like),
                Lead.company.ilike(like),
            )
        )
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    leads = db.scalars(query.order_by(Lead.id).limit(limit).offset(offset))
    return Page(
        items=[
            LeadOut(
                id=lead.id,
                email=lead.email,
                first_name=lead.first_name,
                last_name=lead.last_name,
                company=lead.company,
                country=lead.country,
                custom=lead.custom or {},
                created_at=lead.created_at,
            )
            for lead in leads
        ],
        total=total,
    )


@router.post("/import", response_model=ImportOut, status_code=status.HTTP_201_CREATED)
def import_group(body: ImportIn, db: DB, user: EditorUser, actor: CurrentActor) -> ImportOut:
    """Create a group from rows the browser has already parsed and mapped from a CSV file.

    An address already in the system keeps its existing lead (and its data) and joins this group too.
    """
    invalid: list[InvalidRow] = []
    invalid_count = 0
    duplicates = 0
    valid: dict[str, LeadRowIn] = {}
    for i, row in enumerate(body.rows, start=1):
        raw = row.email.strip()
        try:
            validate_email(raw, check_deliverability=False)
        except EmailNotValidError:
            invalid_count += 1
            if len(invalid) < MAX_INVALID_LISTED:
                invalid.append(InvalidRow(row=i, email=raw[:320], reason="bad_syntax" if raw else "missing"))
            continue
        email = normalize_email(raw)
        if email in valid:
            duplicates += 1
            continue
        valid[email] = row

    if not valid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no_valid_rows")

    group = LeadGroup(name=body.name.strip(), source=_clean(body.source), created_by_id=user.id)
    db.add(group)
    db.flush()

    existing: dict[str, int] = {}
    emails = list(valid)
    for start in range(0, len(emails), 1000):
        for lead_id, email in db.execute(
            select(Lead.id, Lead.email).where(Lead.email.in_(emails[start : start + 1000])).order_by(Lead.id)
        ):
            existing.setdefault(email, lead_id)

    new_leads = []
    for email, row in valid.items():
        if email in existing:
            continue
        custom = {
            key: value.strip()
            for key, value in row.custom.items()
            if CUSTOM_KEY.match(key) and key not in FIXED_FIELDS and value and value.strip()
        }
        new_leads.append(
            Lead(
                email=email,
                first_name=_clean(row.first_name),
                last_name=_clean(row.last_name),
                company=_clean(row.company),
                country=_clean(row.country),
                custom={k: v[:1000] for k, v in custom.items()},
            )
        )
    db.add_all(new_leads)
    db.flush()

    lead_ids = list(existing.values()) + [lead.id for lead in new_leads]
    for start in range(0, len(lead_ids), 1000):
        db.execute(
            insert(lead_group_members)
            .values([{"group_id": group.id, "lead_id": lid} for lid in lead_ids[start : start + 1000]])
            .on_conflict_do_nothing()
        )

    result = {
        "added": len(new_leads),
        "existing": len(existing),
        "duplicates_in_file": duplicates,
        "invalid_count": invalid_count,
    }
    log_activity(
        db,
        actor,
        "group.imported",
        entity_type="group",
        entity_id=group.id,
        after={"name": group.name, "source": group.source, **result},
    )
    db.commit()
    return ImportOut(group=_group_out(group, len(lead_ids)), invalid=invalid, **result)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group(group_id: int, db: DB, _: EditorUser, actor: CurrentActor) -> None:
    """Remove a group (its leads stay in the system). Not allowed once a campaign uses it."""
    group = _get(db, group_id)
    if db.scalar(select(exists().where(campaign_groups.c.group_id == group.id))):
        raise HTTPException(status.HTTP_409_CONFLICT, "group_in_use")
    log_activity(
        db,
        actor,
        "group.deleted",
        entity_type="group",
        entity_id=group.id,
        before={"name": group.name, "source": group.source},
    )
    db.delete(group)
    db.commit()
