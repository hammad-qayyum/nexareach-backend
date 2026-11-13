"""App settings stored in the settings table (key/value)."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Setting

FOOTER_BUSINESS_NAME = "footer.business_name"
FOOTER_POSTAL_ADDRESS = "footer.postal_address"


def get_value(db: Session, key: str, default: str = "") -> str:
    value = db.scalar(select(Setting.value).where(Setting.key == key))
    return value if isinstance(value, str) else default


def set_value(db: Session, key: str, value: str, user_id: int | None) -> None:
    db.execute(
        insert(Setting)
        .values(key=key, value=value, updated_by_id=user_id)
        .on_conflict_do_update(index_elements=[Setting.key], set_={"value": value, "updated_by_id": user_id})
    )


def get_footer(db: Session) -> tuple[str, str]:
    return get_value(db, FOOTER_BUSINESS_NAME), get_value(db, FOOTER_POSTAL_ADDRESS)


BUSINESS_FIELDS = ["business_name", "postal_address"]


def business_fields(db: Session) -> dict[str, str]:
    """{{business_name}} and {{postal_address}} for email templates (from Settings; blank ones left out)."""
    business_name, postal_address = get_footer(db)
    return {k: v for k, v in (("business_name", business_name), ("postal_address", postal_address)) if v}
