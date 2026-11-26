"""App settings stored in the settings table (key/value)."""

from dataclasses import dataclass

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


def get_json(db: Session, key: str) -> dict:
    """A setting stored as a JSON object ({} when unset)."""
    value = db.scalar(select(Setting.value).where(Setting.key == key))
    return value if isinstance(value, dict) else {}


def set_json(db: Session, key: str, value: dict, user_id: int | None, *, secret: bool = False) -> None:
    """Secret parts must already be encrypted by the caller (field names ending in _encrypted)."""
    db.execute(
        insert(Setting)
        .values(key=key, value=value, is_secret=secret, updated_by_id=user_id)
        .on_conflict_do_update(
            index_elements=[Setting.key], set_={"value": value, "is_secret": secret, "updated_by_id": user_id}
        )
    )


# --- The opt-out line and postal address on campaign emails (Deals integration step 2) ----------------------

EMAIL_FOOTER_KEY = "email.footer"  # {"enabled", "optout_line"}
DEFAULT_OPTOUT_LINE = "Not relevant? Reply 'stop' and we won't email you again."


@dataclass(frozen=True)
class EmailFooter:
    optout_line: str
    postal_address: str


def email_footer_settings(db: Session) -> tuple[bool, str]:
    cfg = get_json(db, EMAIL_FOOTER_KEY)
    return bool(cfg.get("enabled")), (cfg.get("optout_line") or DEFAULT_OPTOUT_LINE)


def email_footer(db: Session) -> EmailFooter | None:
    """What goes under every campaign and follow-up email, or None while switched off (an email is then
    exactly what was written). Never on warm-up, test-free app emails or answers to replies."""
    enabled, line = email_footer_settings(db)
    if not enabled:
        return None
    return EmailFooter(optout_line=line, postal_address=get_value(db, FOOTER_POSTAL_ADDRESS).strip())
