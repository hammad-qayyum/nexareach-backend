"""GDPR requests for one person, looked up by email (they may be several leads after "keep both")."""

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, EmailStr

from app.api.deps import DB, AdminUser, CurrentActor, EditorUser
from app.services import privacy
from app.services.audit import log_activity
from app.services.sending import now

router = APIRouter(prefix="/people", tags=["people"])


class PersonIn(BaseModel):
    email: EmailStr


class ForgetOut(BaseModel):
    leads: int
    campaigns: int
    campaign_emails: int
    duplicate_rows: int


@router.post("/export")
def export_person(body: PersonIn, db: DB, _: EditorUser, actor: CurrentActor) -> Response:
    """Everything held about this person, as a JSON file (right of access)."""
    data = privacy.export_person(db, str(body.email))
    if (
        not data["leads"]
        and not data["emails"]
        and not data["waiting_duplicates"]
        and not data["do_not_contact"]
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "person_not_found")
    log_activity(
        db,
        actor,
        "person.exported",
        entity_type="lead",
        entity_id=data["leads"][0]["id"] if data["leads"] else None,
        after={"leads": len(data["leads"]), "emails": len(data["emails"])},
    )
    db.commit()
    return Response(
        json.dumps(data, indent=2, ensure_ascii=False, default=str),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="person-data-{datetime.now():%Y%m%d-%H%M}.json"'
        },
    )


@router.post("/forget", response_model=ForgetOut)
def forget_person(body: PersonIn, db: DB, _: AdminUser, actor: CurrentActor) -> ForgetOut:
    """Erase this person (right to erasure). Admins only; can't be undone."""
    try:
        result = privacy.forget_person(db, str(body.email), actor, now())
    except privacy.PersonBusy as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "person_being_emailed") from e
    db.commit()
    return ForgetOut(**result)
