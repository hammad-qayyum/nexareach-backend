import re
from datetime import datetime
from typing import Annotated, Any

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.api.deps import DB, CurrentActor, CurrentUser, EditorUser
from app.api.schemas import Page
from app.models import (
    DuplicateStatus,
    EmailProvider,
    Lead,
    LeadDuplicate,
    LeadGroup,
    campaign_groups,
    lead_group_members,
)
from app.services import sheets
from app.services.audit import changes, log_activity
from app.services.auth import normalize_email
from app.services.geo import timezone_for
from app.services.providers import detect, detect_many, domain_of
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
    # Duplicate rows from this group's import still waiting for a decision.
    pending_count: int
    created_at: datetime


class LeadOut(BaseModel):
    id: int
    email: str
    first_name: str | None
    last_name: str | None
    company: str | None
    country: str | None
    timezone: str | None
    email_provider: EmailProvider | None
    custom: dict[str, Any]
    created_at: datetime


class LeadRowIn(BaseModel):
    email: str = Field(max_length=320)
    first_name: str | None = Field(default=None, max_length=200)
    last_name: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=300)
    country: str | None = Field(default=None, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)
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
    pending_duplicates: int
    invalid_count: int
    invalid: list[InvalidRow]
    # How many of the added leads use each email provider ("GOOGLE": 12, …).
    provider_mix: dict[str, int]


class GroupUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    source: str | None = Field(default=None, max_length=200)


class FacetCount(BaseModel):
    value: str
    count: int


class FacetsOut(BaseModel):
    providers: list[FacetCount]
    countries: list[FacetCount]


class AddLeadOut(BaseModel):
    added: bool
    # The email already belongs to a lead: the row waits on the Duplicates page instead.
    pending_duplicate: bool
    lead: LeadOut | None


class SheetIn(BaseModel):
    url: str = Field(min_length=10, max_length=2000)


class SheetOut(BaseModel):
    csv: str


def lead_out(lead: Lead) -> LeadOut:
    return LeadOut(
        id=lead.id,
        email=lead.email,
        first_name=lead.first_name,
        last_name=lead.last_name,
        company=lead.company,
        country=lead.country,
        timezone=lead.timezone,
        email_provider=lead.email_provider,
        custom=lead.custom or {},
        created_at=lead.created_at,
    )


def _clean(value: str | None) -> str | None:
    value = " ".join(value.split()) if value else ""
    return value or None


def _counts(db: DB, group_ids: list[int]) -> dict[int, int]:
    rows = db.execute(
        select(lead_group_members.c.group_id, func.count())
        .where(lead_group_members.c.group_id.in_(group_ids))
        .group_by(lead_group_members.c.group_id)
    )
    return {group_id: n for group_id, n in rows}


def _pending(db: DB, group_ids: list[int]) -> dict[int, int]:
    rows = db.execute(
        select(LeadDuplicate.group_id, func.count())
        .where(LeadDuplicate.group_id.in_(group_ids), LeadDuplicate.status == DuplicateStatus.PENDING)
        .group_by(LeadDuplicate.group_id)
    )
    return {group_id: n for group_id, n in rows}


def _group_out(group: LeadGroup, count: int, pending: int) -> GroupOut:
    return GroupOut(
        id=group.id,
        name=group.name,
        source=group.source,
        lead_count=count,
        pending_count=pending,
        created_at=group.created_at,
    )


def _get(db: DB, group_id: int) -> LeadGroup:
    group = db.get(LeadGroup, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "group_not_found")
    return group


@router.get("", response_model=list[GroupOut])
def list_groups(db: DB, _: CurrentUser) -> list[GroupOut]:
    groups = db.scalars(select(LeadGroup).order_by(LeadGroup.created_at.desc())).all()
    ids = [g.id for g in groups]
    counts, pending = _counts(db, ids), _pending(db, ids)
    return [_group_out(g, counts.get(g.id, 0), pending.get(g.id, 0)) for g in groups]


@router.get("/{group_id}", response_model=GroupOut)
def get_group(group_id: int, db: DB, _: CurrentUser) -> GroupOut:
    group = _get(db, group_id)
    return _group_out(
        group, _counts(db, [group.id]).get(group.id, 0), _pending(db, [group.id]).get(group.id, 0)
    )


@router.get("/{group_id}/leads", response_model=Page[LeadOut])
def list_group_leads(
    group_id: int,
    db: DB,
    _: CurrentUser,
    q: str | None = None,
    provider: EmailProvider | None = None,
    country: str | None = None,
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
    if provider:
        query = query.where(Lead.email_provider == provider)
    if country:
        query = query.where(func.lower(Lead.country) == country.strip().lower())
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    leads = db.scalars(query.order_by(Lead.id).limit(limit).offset(offset))
    return Page(
        items=[lead_out(lead) for lead in leads],
        total=total,
    )


@router.post("/import", response_model=ImportOut, status_code=status.HTTP_201_CREATED)
def import_group(body: ImportIn, db: DB, user: EditorUser, actor: CurrentActor) -> ImportOut:
    """Create a group from rows the browser has already parsed and mapped from a CSV file.

    A row whose email already belongs to a lead (in the system, or earlier in this file) doesn't join
    the group yet: it waits as a pending duplicate until someone decides keep new, old or both.
    """
    invalid: list[InvalidRow] = []
    invalid_count = 0
    valid: list[tuple[int, str, LeadRowIn]] = []
    for i, row in enumerate(body.rows, start=1):
        raw = row.email.strip()
        try:
            validate_email(raw, check_deliverability=False)
        except EmailNotValidError:
            invalid_count += 1
            if len(invalid) < MAX_INVALID_LISTED:
                invalid.append(InvalidRow(row=i, email=raw[:320], reason="bad_syntax" if raw else "missing"))
            continue
        valid.append((i, normalize_email(raw), row))

    if not valid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no_valid_rows")

    group = LeadGroup(name=body.name.strip(), source=_clean(body.source), created_by_id=user.id)
    db.add(group)
    db.flush()

    # The oldest lead per email already in the system (there can be several after "keep both").
    existing: dict[str, int] = {}
    emails = list(dict.fromkeys(email for _, email, _ in valid))
    for start in range(0, len(emails), 1000):
        for lead_id, email in db.execute(
            select(Lead.id, Lead.email).where(Lead.email.in_(emails[start : start + 1000])).order_by(Lead.id)
        ):
            existing.setdefault(email, lead_id)

    # First pass: the first row for each new email becomes a lead in this group.
    created: dict[str, Lead] = {}
    first_rows: set[int] = set()
    for i, email, row in valid:
        if email in existing or email in created:
            continue
        created[email] = Lead(email=email, **_fields(row))
        first_rows.add(i)
    providers = detect_many(domain_of(email) for email in created)
    for email, lead in created.items():
        lead.email_provider = providers[domain_of(email)]
    db.add_all(created.values())
    db.flush()

    lead_ids = [lead.id for lead in created.values()]
    for start in range(0, len(lead_ids), 1000):
        db.execute(
            insert(lead_group_members)
            .values([{"group_id": group.id, "lead_id": lid} for lid in lead_ids[start : start + 1000]])
            .on_conflict_do_nothing()
        )

    # Second pass: every other row is a duplicate of a lead that already exists.
    duplicates = [
        LeadDuplicate(
            group_id=group.id,
            existing_lead_id=existing[email] if email in existing else created[email].id,
            email=email,
            row_number=i,
            status=DuplicateStatus.PENDING,
            **_fields(row),
        )
        for i, email, row in valid
        if i not in first_rows
    ]
    db.add_all(duplicates)

    mix: dict[str, int] = {}
    for lead in created.values():
        mix[lead.email_provider.value] = mix.get(lead.email_provider.value, 0) + 1
    result = {"added": len(created), "pending_duplicates": len(duplicates), "invalid_count": invalid_count}
    log_activity(
        db,
        actor,
        "group.imported",
        entity_type="group",
        entity_id=group.id,
        after={"name": group.name, "source": group.source, **result},
    )
    db.commit()
    return ImportOut(
        group=_group_out(group, len(created), len(duplicates)), invalid=invalid, provider_mix=mix, **result
    )


def _fields(row: LeadRowIn) -> dict[str, Any]:
    custom = {
        key: value.strip()[:1000]
        for key, value in row.custom.items()
        if CUSTOM_KEY.match(key) and key not in FIXED_FIELDS and value and value.strip()
    }
    country = _clean(row.country)
    return {
        "first_name": _clean(row.first_name),
        "last_name": _clean(row.last_name),
        "company": _clean(row.company),
        "country": country,
        "timezone": timezone_for(row.timezone, country),
        "custom": custom,
    }


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


@router.patch("/{group_id}", response_model=GroupOut)
def update_group(group_id: int, body: GroupUpdateIn, db: DB, _: EditorUser, actor: CurrentActor) -> GroupOut:
    group = _get(db, group_id)
    before = {"name": group.name, "source": group.source}
    if body.name is not None:
        group.name = body.name.strip()
    if "source" in body.model_fields_set:
        group.source = _clean(body.source)
    after = {"name": group.name, "source": group.source}
    if before != after:
        old, new = changes(before, after)
        log_activity(
            db, actor, "group.updated", entity_type="group", entity_id=group.id, before=old, after=new
        )
        db.commit()
    return get_group(group.id, db, _)


@router.get("/{group_id}/facets", response_model=FacetsOut)
def group_facets(group_id: int, db: DB, _: CurrentUser) -> FacetsOut:
    """Counts per email provider and per country, for the lead filters."""
    _get(db, group_id)
    members = select(lead_group_members.c.lead_id).where(lead_group_members.c.group_id == group_id)
    providers = db.execute(
        select(Lead.email_provider, func.count()).where(Lead.id.in_(members)).group_by(Lead.email_provider)
    )
    countries = db.execute(
        select(Lead.country, func.count())
        .where(Lead.id.in_(members), Lead.country.is_not(None))
        .group_by(Lead.country)
        .order_by(func.count().desc(), Lead.country)
    )
    return FacetsOut(
        providers=[FacetCount(value=(p.value if p else "PENDING"), count=n) for p, n in providers],
        countries=[FacetCount(value=c, count=n) for c, n in countries],
    )


@router.post("/{group_id}/leads", response_model=AddLeadOut, status_code=status.HTTP_201_CREATED)
def add_lead(group_id: int, body: LeadRowIn, db: DB, _: EditorUser, actor: CurrentActor) -> AddLeadOut:
    """Add one lead by hand. An email already in the system waits on the Duplicates page, like an import."""
    group = _get(db, group_id)
    try:
        validate_email(body.email.strip(), check_deliverability=False)
    except EmailNotValidError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_email") from e
    email = normalize_email(body.email)
    existing = db.scalar(select(Lead.id).where(Lead.email == email).order_by(Lead.id).limit(1))
    if existing is not None:
        db.add(
            LeadDuplicate(
                group_id=group.id,
                existing_lead_id=existing,
                email=email,
                row_number=0,
                status=DuplicateStatus.PENDING,
                **_fields(body),
            )
        )
        log_activity(
            db, actor, "group.lead_pending", entity_type="group", entity_id=group.id, after={"email": email}
        )
        db.commit()
        return AddLeadOut(added=False, pending_duplicate=True, lead=None)
    lead = Lead(email=email, email_provider=detect(domain_of(email)), **_fields(body))
    db.add(lead)
    db.flush()
    db.execute(insert(lead_group_members).values(group_id=group.id, lead_id=lead.id))
    log_activity(
        db,
        actor,
        "lead.created",
        entity_type="lead",
        entity_id=lead.id,
        after={"email": email, "group_id": group.id},
    )
    db.commit()
    return AddLeadOut(added=True, pending_duplicate=False, lead=lead_out(lead))


@router.delete("/{group_id}/leads/{lead_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_lead(group_id: int, lead_id: int, db: DB, _: EditorUser, actor: CurrentActor) -> None:
    """Take a lead out of this group. The lead stays in the system and in its other groups."""
    _get(db, group_id)
    result = db.execute(
        delete(lead_group_members).where(
            lead_group_members.c.group_id == group_id, lead_group_members.c.lead_id == lead_id
        )
    )
    if result.rowcount == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lead_not_in_group")
    log_activity(
        db, actor, "group.lead_removed", entity_type="lead", entity_id=lead_id, after={"group_id": group_id}
    )
    db.commit()


@router.post("/google-sheet", response_model=SheetOut)
def fetch_google_sheet(body: SheetIn, _: EditorUser) -> SheetOut:
    """Download a Google Sheet shared as "anyone with the link" as CSV, for the import screen."""
    try:
        return SheetOut(csv=sheets.fetch_csv(body.url))
    except sheets.SheetError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, e.code) from e
