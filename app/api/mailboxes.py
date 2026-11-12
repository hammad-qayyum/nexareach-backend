from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, exists, select

from app.api.deps import DB, CurrentActor, CurrentUser, EditorUser
from app.core.config import get_settings
from app.core.crypto import encrypt
from app.models import (
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    Mailbox,
    MailboxStatus,
    SmtpSecurity,
    campaign_mailboxes,
)
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
    "imap_host",
    "imap_port",
    "daily_cap",
    "status",
]


class CheckOut(BaseModel):
    ok: bool
    detail: str


class MailboxOut(BaseModel):
    id: int
    email: str
    display_name: str
    smtp_host: str
    smtp_port: int
    smtp_security: SmtpSecurity
    smtp_username: str
    imap_host: str | None
    imap_port: int
    sent_folder: str | None
    daily_cap: int
    status: MailboxStatus
    last_error: str | None
    next_send_at: datetime | None
    sent_today: int
    created_at: datetime
    archived_at: datetime | None
    # False once it has sent (or tried to send) an email, or while a running/paused campaign uses it.
    # Such a mailbox can be archived instead, which keeps its history.
    can_delete: bool
    # Result of the connection test run by this request (adding, editing settings, reactivating).
    connection: CheckOut | None = None


class MailboxIn(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=200)
    smtp_host: str = Field(min_length=1, max_length=255)
    smtp_port: int = Field(ge=1, le=65535)
    smtp_security: SmtpSecurity
    smtp_username: str = Field(min_length=1, max_length=320)
    smtp_password: str = Field(min_length=1, max_length=500)
    # Incoming mail server for the Sent copy (cPanel: same host as SMTP, port 993). Blank = no copy.
    imap_host: str | None = Field(default=None, max_length=255)
    imap_port: int = Field(default=993, ge=1, le=65535)
    daily_cap: int = Field(default=40, ge=1, le=500)


class MailboxUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    smtp_host: str | None = Field(default=None, min_length=1, max_length=255)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_security: SmtpSecurity | None = None
    smtp_username: str | None = Field(default=None, min_length=1, max_length=320)
    # Leave out to keep the current password.
    smtp_password: str | None = Field(default=None, min_length=1, max_length=500)
    imap_host: str | None = Field(default=None, max_length=255)
    imap_port: int | None = Field(default=None, ge=1, le=65535)
    daily_cap: int | None = Field(default=None, ge=1, le=500)
    status: MailboxStatus | None = None


def _out(db: DB, m: Mailbox, connection: CheckOut | None = None) -> MailboxOut:
    return MailboxOut(
        id=m.id,
        email=m.email,
        display_name=m.display_name,
        smtp_host=m.smtp_host,
        smtp_port=m.smtp_port,
        smtp_security=m.smtp_security,
        smtp_username=m.smtp_username,
        imap_host=m.imap_host,
        imap_port=m.imap_port,
        sent_folder=m.sent_folder,
        daily_cap=m.daily_cap,
        status=m.status,
        last_error=m.last_error,
        next_send_at=m.next_send_at,
        sent_today=sent_today(db, m.id, now()),
        created_at=m.created_at,
        archived_at=m.archived_at,
        can_delete=_delete_blocker(db, m) is None,
        connection=connection,
    )


def _delete_blocker(db: DB, m: Mailbox) -> str | None:
    """Why this mailbox can't be deleted, or None. Its sending history must survive."""
    if db.scalar(select(exists().where(CampaignRecipient.mailbox_id == m.id))):
        return "mailbox_has_sent"
    in_live_campaign = db.scalar(
        select(
            exists()
            .where(campaign_mailboxes.c.mailbox_id == m.id)
            .where(campaign_mailboxes.c.campaign_id == Campaign.id)
            .where(Campaign.status.in_([CampaignStatus.RUNNING, CampaignStatus.PAUSED]))
        )
    )
    return "mailbox_in_active_campaign" if in_live_campaign else None


def _get(db: DB, mailbox_id: int) -> Mailbox:
    mailbox = db.get(Mailbox, mailbox_id)
    if mailbox is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "mailbox_not_found")
    return mailbox


def _check_security(security: SmtpSecurity | None) -> None:
    if security == SmtpSecurity.NONE and not get_settings().smtp_allow_plain:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "plain_smtp_not_allowed")


@router.get("", response_model=list[MailboxOut])
def list_mailboxes(db: DB, _: CurrentUser, include_archived: bool = False) -> list[MailboxOut]:
    query = select(Mailbox).order_by(Mailbox.email)
    if not include_archived:
        query = query.where(Mailbox.archived_at.is_(None))
    return [_out(db, m) for m in db.scalars(query)]


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
        imap_host=(body.imap_host or "").strip() or None,
        imap_port=body.imap_port,
        daily_cap=body.daily_cap,
        # Never active before its login has been tested (just below).
        status=MailboxStatus.ERROR,
        last_error="Connection not tested yet.",
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
    result = _test_and_set_status(db, mailbox, actor)
    return _out(db, mailbox, result)


@router.patch("/{mailbox_id}", response_model=MailboxOut)
def update_mailbox(
    mailbox_id: int, body: MailboxUpdateIn, db: DB, _: EditorUser, actor: CurrentActor
) -> MailboxOut:
    mailbox = _get(db, mailbox_id)
    _check_security(body.smtp_security)
    if body.status == MailboxStatus.ACTIVE and mailbox.archived_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "mailbox_archived")
    before = snapshot(mailbox, AUDIT_FIELDS)
    for field in ["display_name", "smtp_host", "smtp_username"]:
        value = getattr(body, field)
        if value is not None:
            setattr(mailbox, field, value.strip())
    if "imap_host" in body.model_fields_set:
        new_host = (body.imap_host or "").strip() or None
        if new_host != mailbox.imap_host:
            mailbox.sent_folder = None  # look it up again on the new server
        mailbox.imap_host = new_host
    for field in ["smtp_port", "smtp_security", "imap_port", "daily_cap", "status"]:
        value = getattr(body, field)
        if value is not None:
            setattr(mailbox, field, value)
    password_changed = body.smtp_password is not None
    if password_changed:
        mailbox.smtp_password_encrypted = encrypt(body.smtp_password)
    if body.status == MailboxStatus.ACTIVE:
        # Reactivating only sticks if the connection test below passes.
        mailbox.status = before["status"] if before["status"] != MailboxStatus.PAUSED else MailboxStatus.ERROR
    after = snapshot(mailbox, AUDIT_FIELDS)
    if before != after or password_changed:
        old, new = changes(before, after)
        if password_changed:
            new["password_changed"] = True
        log_activity(
            db, actor, "mailbox.updated", entity_type="mailbox", entity_id=mailbox.id, before=old, after=new
        )
        db.commit()
    connection_changed = password_changed or any(
        before[f] != after[f]
        for f in ("smtp_host", "smtp_port", "smtp_security", "smtp_username", "imap_host", "imap_port")
    )
    result = None
    if connection_changed or body.status == MailboxStatus.ACTIVE:
        result = _test_and_set_status(db, mailbox, actor)
    return _out(db, mailbox, result)


@router.delete("/{mailbox_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mailbox(mailbox_id: int, db: DB, _: EditorUser, actor: CurrentActor) -> None:
    """Only for a mailbox that never sent an email and no running or paused campaign uses."""
    mailbox = _get(db, mailbox_id)
    blocker = _delete_blocker(db, mailbox)
    if blocker:
        raise HTTPException(status.HTTP_409_CONFLICT, blocker)
    # Drafts and finished campaigns that listed it simply lose it.
    db.execute(delete(campaign_mailboxes).where(campaign_mailboxes.c.mailbox_id == mailbox.id))
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


@router.post("/{mailbox_id}/archive", response_model=MailboxOut)
def archive_mailbox(mailbox_id: int, db: DB, _: EditorUser, actor: CurrentActor) -> MailboxOut:
    """Hide a mailbox and stop it sending, keeping its history. Draft campaigns stop using it."""
    mailbox = _get(db, mailbox_id)
    if mailbox.archived_at is None:
        before = mailbox.status
        mailbox.archived_at = now()
        mailbox.status = MailboxStatus.PAUSED
        drafts = select(Campaign.id).where(Campaign.status == CampaignStatus.DRAFT)
        db.execute(
            delete(campaign_mailboxes).where(
                campaign_mailboxes.c.mailbox_id == mailbox.id, campaign_mailboxes.c.campaign_id.in_(drafts)
            )
        )
        log_activity(
            db,
            actor,
            "mailbox.archived",
            entity_type="mailbox",
            entity_id=mailbox.id,
            before={"status": before},
            after={"status": MailboxStatus.PAUSED, "archived": True},
        )
        db.commit()
    return _out(db, mailbox)


@router.post("/{mailbox_id}/restore", response_model=MailboxOut)
def restore_mailbox(mailbox_id: int, db: DB, _: EditorUser, actor: CurrentActor) -> MailboxOut:
    """Bring an archived mailbox back, paused. "Test and activate" makes it send again."""
    mailbox = _get(db, mailbox_id)
    if mailbox.archived_at is not None:
        mailbox.archived_at = None
        mailbox.status = MailboxStatus.PAUSED
        log_activity(
            db,
            actor,
            "mailbox.restored",
            entity_type="mailbox",
            entity_id=mailbox.id,
            after={"status": "PAUSED"},
        )
        db.commit()
    return _out(db, mailbox)


@router.post("/{mailbox_id}/check", response_model=CheckOut)
def check_mailbox(mailbox_id: int, db: DB, _: EditorUser, actor: CurrentActor) -> CheckOut:
    """Log in to the SMTP (and IMAP) server without sending anything. Passing makes the mailbox
    active again; failing stops it from sending. A paused mailbox stays paused."""
    return _test_and_set_status(db, _get(db, mailbox_id), actor)


def _test_and_set_status(db: DB, mailbox: Mailbox, actor: CurrentActor) -> CheckOut:
    """Test the connection and set the status from the result: a mailbox is only ACTIVE while its login
    works. Commits."""
    try:
        mailer.check_login(mailbox)
        result = CheckOut(ok=True, detail="Signed in to the SMTP server.")
    except mailer.AuthFailed as e:
        result = CheckOut(ok=False, detail=f"The server rejected the username or password ({e}).")
    except mailer.SendFailed as e:
        result = CheckOut(ok=False, detail=str(e))
    if result.ok and mailbox.imap_host:
        try:
            mailbox.sent_folder = mailer.check_imap(mailbox)
            result.detail += f' Sent copies go to the "{mailbox.sent_folder}" folder.'
        except mailer.SentCopyFailed as e:
            # Sending works, so the mailbox is usable; only the Sent copy is affected.
            result.detail += f" But the Sent copy won't work: {e}"

    before = mailbox.status
    # Paused and archived mailboxes keep their status; only active/not-connected ones follow the test.
    if before != MailboxStatus.PAUSED and mailbox.archived_at is None:
        mailbox.status = MailboxStatus.ACTIVE if result.ok else MailboxStatus.ERROR
    mailbox.last_error = None if result.ok else result.detail
    log_activity(
        db,
        actor,
        "mailbox.checked",
        entity_type="mailbox",
        entity_id=mailbox.id,
        before={"status": before},
        after={"status": mailbox.status, "ok": result.ok, "detail": result.detail},
    )
    db.commit()
    return result
