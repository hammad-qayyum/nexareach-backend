from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import exists, select

from app.api.deps import DB, CurrentActor, CurrentUser, EditorUser
from app.core.config import get_settings
from app.core.crypto import encrypt
from app.models import CampaignRecipient, Mailbox, MailboxStatus, SmtpSecurity, campaign_mailboxes
from app.services import mailer
from app.services.audit import changes, log_activity, snapshot
from app.services.auth import normalize_email
from app.services.sending import now, sent_today

router = APIRouter(prefix="/mailboxes", tags=["mailboxes"])

# Never include the password.
AUDIT_FIELDS = [
    "email",
    "display_name",
    "smtp_host",
    "smtp_port",
    "smtp_security",
    "smtp_username",
    "daily_cap",
    "status",
]


class MailboxOut(BaseModel):
    id: int
    email: str
    display_name: str
    smtp_host: str
    smtp_port: int
    smtp_security: SmtpSecurity
    smtp_username: str
    daily_cap: int
    status: MailboxStatus
    last_error: str | None
    next_send_at: datetime | None
    sent_today: int
    created_at: datetime


class MailboxIn(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=200)
    smtp_host: str = Field(min_length=1, max_length=255)
    smtp_port: int = Field(ge=1, le=65535)
    smtp_security: SmtpSecurity
    smtp_username: str = Field(min_length=1, max_length=320)
    smtp_password: str = Field(min_length=1, max_length=500)
    daily_cap: int = Field(default=40, ge=1, le=500)


class MailboxUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    smtp_host: str | None = Field(default=None, min_length=1, max_length=255)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_security: SmtpSecurity | None = None
    smtp_username: str | None = Field(default=None, min_length=1, max_length=320)
    # Leave out to keep the current password.
    smtp_password: str | None = Field(default=None, min_length=1, max_length=500)
    daily_cap: int | None = Field(default=None, ge=1, le=500)
    status: MailboxStatus | None = None


class CheckOut(BaseModel):
    ok: bool
    detail: str


def _out(db: DB, m: Mailbox) -> MailboxOut:
    return MailboxOut(
        id=m.id,
        email=m.email,
        display_name=m.display_name,
        smtp_host=m.smtp_host,
        smtp_port=m.smtp_port,
        smtp_security=m.smtp_security,
        smtp_username=m.smtp_username,
        daily_cap=m.daily_cap,
        status=m.status,
        last_error=m.last_error,
        next_send_at=m.next_send_at,
        sent_today=sent_today(db, m.id, now()),
        created_at=m.created_at,
    )


def _get(db: DB, mailbox_id: int) -> Mailbox:
    mailbox = db.get(Mailbox, mailbox_id)
    if mailbox is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "mailbox_not_found")
    return mailbox


def _check_security(security: SmtpSecurity | None) -> None:
    if security == SmtpSecurity.NONE and not get_settings().smtp_allow_plain:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "plain_smtp_not_allowed")


@router.get("", response_model=list[MailboxOut])
def list_mailboxes(db: DB, _: CurrentUser) -> list[MailboxOut]:
    return [_out(db, m) for m in db.scalars(select(Mailbox).order_by(Mailbox.email))]


@router.post("", response_model=MailboxOut, status_code=status.HTTP_201_CREATED)
def create_mailbox(body: MailboxIn, db: DB, user: EditorUser, actor: CurrentActor) -> MailboxOut:
    _check_security(body.smtp_security)
    email = normalize_email(body.email)
    if db.scalar(select(Mailbox.id).where(Mailbox.email == email)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "mailbox_exists")
    mailbox = Mailbox(
        email=email,
        display_name=body.display_name.strip(),
        smtp_host=body.smtp_host.strip(),
        smtp_port=body.smtp_port,
        smtp_security=body.smtp_security,
        smtp_username=body.smtp_username.strip(),
        smtp_password_encrypted=encrypt(body.smtp_password),
        daily_cap=body.daily_cap,
        status=MailboxStatus.ACTIVE,
        created_by_id=user.id,
    )
    db.add(mailbox)
    db.flush()
    log_activity(
        db,
        actor,
        "mailbox.created",
        entity_type="mailbox",
        entity_id=mailbox.id,
        after=snapshot(mailbox, AUDIT_FIELDS),
    )
    db.commit()
    return _out(db, mailbox)


@router.patch("/{mailbox_id}", response_model=MailboxOut)
def update_mailbox(
    mailbox_id: int, body: MailboxUpdateIn, db: DB, _: EditorUser, actor: CurrentActor
) -> MailboxOut:
    mailbox = _get(db, mailbox_id)
    _check_security(body.smtp_security)
    before = snapshot(mailbox, AUDIT_FIELDS)
    for field in ["display_name", "smtp_host", "smtp_username"]:
        value = getattr(body, field)
        if value is not None:
            setattr(mailbox, field, value.strip())
    for field in ["smtp_port", "smtp_security", "daily_cap", "status"]:
        value = getattr(body, field)
        if value is not None:
            setattr(mailbox, field, value)
    password_changed = body.smtp_password is not None
    if password_changed:
        mailbox.smtp_password_encrypted = encrypt(body.smtp_password)
    if body.status == MailboxStatus.ACTIVE:
        mailbox.last_error = None
    after = snapshot(mailbox, AUDIT_FIELDS)
    if before != after or password_changed:
        old, new = changes(before, after)
        if password_changed:
            new["password_changed"] = True
        log_activity(
            db, actor, "mailbox.updated", entity_type="mailbox", entity_id=mailbox.id, before=old, after=new
        )
        db.commit()
    return _out(db, mailbox)


@router.delete("/{mailbox_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mailbox(mailbox_id: int, db: DB, _: EditorUser, actor: CurrentActor) -> None:
    mailbox = _get(db, mailbox_id)
    in_use = db.scalar(
        select(
            exists().where(campaign_mailboxes.c.mailbox_id == mailbox.id)
            | exists().where(CampaignRecipient.mailbox_id == mailbox.id)
        )
    )
    if in_use:
        # Its sending history must stay; pause it instead.
        raise HTTPException(status.HTTP_409_CONFLICT, "mailbox_in_use")
    log_activity(
        db,
        actor,
        "mailbox.deleted",
        entity_type="mailbox",
        entity_id=mailbox.id,
        before=snapshot(mailbox, AUDIT_FIELDS),
    )
    db.delete(mailbox)
    db.commit()


@router.post("/{mailbox_id}/check", response_model=CheckOut)
def check_mailbox(mailbox_id: int, db: DB, _: EditorUser, actor: CurrentActor) -> CheckOut:
    """Log in to the SMTP server without sending anything."""
    mailbox = _get(db, mailbox_id)
    try:
        mailer.check_login(mailbox)
        result = CheckOut(ok=True, detail="Signed in to the SMTP server.")
    except mailer.AuthFailed as e:
        result = CheckOut(ok=False, detail=f"The server rejected the username or password ({e}).")
    except mailer.SendFailed as e:
        result = CheckOut(ok=False, detail=str(e))
    log_activity(
        db,
        actor,
        "mailbox.checked",
        entity_type="mailbox",
        entity_id=mailbox.id,
        after={"ok": result.ok, "detail": result.detail},
    )
    db.commit()
    return result
