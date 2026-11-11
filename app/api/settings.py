from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentActor, CurrentUser, EditorUser
from app.services.app_settings import FOOTER_BUSINESS_NAME, FOOTER_POSTAL_ADDRESS, get_footer, set_value
from app.services.audit import changes, log_activity

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
