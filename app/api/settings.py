from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import DB, AdminUser, CurrentActor, CurrentUser, EditorUser
from app.models import Mailbox
from app.services.app_settings import (
    FOOTER_BUSINESS_NAME,
    FOOTER_POSTAL_ADDRESS,
    get_footer,
    get_value,
    set_value,
)
from app.services.audit import changes, log_activity
from app.services.invites import INVITE_MAILBOX, invite_mailbox

router = APIRouter(prefix="/settings", tags=["settings"])


class FooterSettings(BaseModel):
    business_name: str = Field(max_length=200)
    postal_address: str = Field(max_length=500)


@router.get("/footer", response_model=FooterSettings)
def read_footer(db: DB, _: CurrentUser) -> FooterSettings:
    business_name, postal_address = get_footer(db)
    return FooterSettings(business_name=business_name, postal_address=postal_address)


@router.put("/footer", response_model=FooterSettings)
def update_footer(body: FooterSettings, db: DB, user: EditorUser, actor: CurrentActor) -> FooterSettings:
    business_name, postal_address = get_footer(db)
    before = {"business_name": business_name, "postal_address": postal_address}
    after = {"business_name": body.business_name.strip(), "postal_address": body.postal_address.strip()}
    if before != after:
        set_value(db, FOOTER_BUSINESS_NAME, after["business_name"], user.id)
        set_value(db, FOOTER_POSTAL_ADDRESS, after["postal_address"], user.id)
        old, new = changes(before, after)
        log_activity(db, actor, "settings.footer_updated", entity_type="settings", before=old, after=new)
        db.commit()
    return FooterSettings(**after)


class InviteSettings(BaseModel):
    # The mailbox that emails invite links. None: the admin sends the link themselves.
    mailbox_id: int | None = None


@router.get("/invites", response_model=InviteSettings)
def read_invite_settings(db: DB, _: CurrentUser) -> InviteSettings:
    mailbox = invite_mailbox(db)
    return InviteSettings(mailbox_id=mailbox.id if mailbox else None)


@router.put("/invites", response_model=InviteSettings)
def update_invite_settings(
    body: InviteSettings, db: DB, admin: AdminUser, actor: CurrentActor
) -> InviteSettings:
    if body.mailbox_id is not None:
        mailbox = db.get(Mailbox, body.mailbox_id)
        if mailbox is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "mailbox_not_found")
        if mailbox.archived_at is not None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "mailbox_archived")
    before = get_value(db, INVITE_MAILBOX) or None
    after = str(body.mailbox_id) if body.mailbox_id is not None else None
    if before != after:
        set_value(db, INVITE_MAILBOX, after or "", admin.id)
        log_activity(
            db,
            actor,
            "settings.invites_updated",
            entity_type="settings",
            before={"mailbox_id": before},
            after={"mailbox_id": after},
        )
        db.commit()
    return InviteSettings(mailbox_id=body.mailbox_id)
