import re
from datetime import datetime
from typing import Annotated

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.api.deps import DB, CurrentActor, CurrentUser, EditorUser
from app.api.schemas import Page
from app.models import DncKind, DoNotContact
from app.services import privacy
from app.services.audit import log_activity
from app.services.auth import normalize_email

router = APIRouter(prefix="/dnc", tags=["do-not-contact"])

DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")


class DncOut(BaseModel):
    id: int
    value: str
    kind: DncKind
    reason: str | None
    created_at: datetime


class DncAddIn(BaseModel):
    # Emails or domains, one per line (commas and spaces also separate). "@acme.com" or "acme.com" = a domain.
    entries: str = Field(min_length=1, max_length=200_000)
    reason: str | None = Field(default=None, max_length=500)


class DncAddOut(BaseModel):
    added: int
    already_listed: int
    invalid: list[str]


def _parse(entry: str) -> tuple[str, DncKind] | None:
    entry = entry.strip().strip(";").lower()
    if entry.startswith("@"):
        entry = entry[1:]
    if "@" in entry:
        try:
            validate_email(entry, check_deliverability=False)
        except EmailNotValidError:
            return None
        return normalize_email(entry), DncKind.EMAIL
    return (entry, DncKind.DOMAIN) if DOMAIN.match(entry) else None


@router.get("", response_model=Page[DncOut])
def list_dnc(
    db: DB,
    _: CurrentUser,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[DncOut]:
    query = select(DoNotContact)
    if q:
        q = q.strip().lower()
        match = DoNotContact.value.ilike(f"%{q}%")
        # Forgotten people are listed by a hash of their address: an exact address search finds them.
        if "@" in q:
            match = or_(match, DoNotContact.value == privacy.email_hash(q))
        query = query.where(match)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(
        query.order_by(DoNotContact.created_at.desc(), DoNotContact.id.desc()).limit(limit).offset(offset)
    )
    return Page(
        items=[
            DncOut(id=r.id, value=r.value, kind=r.kind, reason=r.reason, created_at=r.created_at)
            for r in rows
        ],
        total=total,
    )


@router.post("", response_model=DncAddOut)
def add_dnc(body: DncAddIn, db: DB, user: EditorUser, actor: CurrentActor) -> DncAddOut:
    parsed: dict[str, DncKind] = {}
    invalid: list[str] = []
    for raw in re.split(r"[\s,]+", body.entries):
        if not raw.strip():
            continue
        result = _parse(raw)
        if result is None:
            invalid.append(raw[:320])
        else:
            parsed[result[0]] = result[1]
    if not parsed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no_valid_entries")

    reason = body.reason.strip() if body.reason and body.reason.strip() else None
    added = db.scalars(
        insert(DoNotContact)
        .values(
            [{"value": v, "kind": k, "reason": reason, "created_by_id": user.id} for v, k in parsed.items()]
        )
        .on_conflict_do_nothing(index_elements=[DoNotContact.value])
        .returning(DoNotContact.value)
    ).all()
    for value in added:
        log_activity(db, actor, "dnc.added", entity_type="dnc", entity_id=value, after={"reason": reason})
    db.commit()
    return DncAddOut(added=len(added), already_listed=len(parsed) - len(added), invalid=invalid[:200])


@router.delete("/{dnc_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_dnc(dnc_id: int, db: DB, _: EditorUser, actor: CurrentActor) -> None:
    entry = db.get(DoNotContact, dnc_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dnc_not_found")
    log_activity(
        db,
        actor,
        "dnc.removed",
        entity_type="dnc",
        entity_id=entry.value,
        before={"kind": entry.kind, "reason": entry.reason},
    )
    db.delete(entry)
    db.commit()
