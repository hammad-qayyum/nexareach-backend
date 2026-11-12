from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentActor, EditorUser
from app.api.groups import CUSTOM_KEY, LeadOut, lead_out
from app.models import Lead
from app.services.audit import changes, log_activity
from app.services.duplicates import lead_snapshot
from app.services.geo import timezone_for, valid_timezone
from app.services.templates import FIXED_FIELDS

router = APIRouter(prefix="/leads", tags=["leads"])


class LeadUpdateIn(BaseModel):
    first_name: str | None = Field(default=None, max_length=200)
    last_name: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=300)
    country: str | None = Field(default=None, max_length=100)
    # Empty string = work it out from the country again.
    timezone: str | None = Field(default=None, max_length=64)
    # Replaces all extra fields. Empty values are dropped.
    custom: dict[str, str] | None = None


def _clean(value: str | None) -> str | None:
    value = " ".join(value.split()) if value else ""
    return value or None


@router.patch("/{lead_id}", response_model=LeadOut)
def update_lead(lead_id: int, body: LeadUpdateIn, db: DB, _: EditorUser, actor: CurrentActor) -> LeadOut:
    """Correct a lead's details. Its email can't change: add the new address as a new lead instead."""
    lead = db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lead_not_found")
    sent = body.model_fields_set
    before = lead_snapshot(lead)
    for field in ("first_name", "last_name", "company", "country"):
        if field in sent:
            setattr(lead, field, _clean(getattr(body, field)))
    if "timezone" in sent:
        if body.timezone and not valid_timezone(body.timezone):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_timezone")
        lead.timezone = timezone_for(body.timezone, lead.country)
    elif "country" in sent and lead.timezone is None:
        lead.timezone = timezone_for(None, lead.country)
    if body.custom is not None:
        lead.custom = {
            k: v.strip()[:1000]
            for k, v in body.custom.items()
            if CUSTOM_KEY.match(k) and k not in FIXED_FIELDS and v and v.strip()
        }
    after = lead_snapshot(lead)
    if before != after:
        old, new = changes(before, after)
        log_activity(db, actor, "lead.updated", entity_type="lead", entity_id=lead.id, before=old, after=new)
        db.commit()
    return lead_out(lead)
