"""The Warm-up page: each mailbox's ramp-up and warm-up, helper accounts, and the warm-up hours."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, select

from app.api.deps import DB, CurrentActor, CurrentUser, EditorUser
from app.core.config import get_settings
from app.core.crypto import encrypt
from app.models import (
    EmailProvider,
    HelperProvider,
    Mailbox,
    MailboxStatus,
    SmtpSecurity,
    WarmupHelper,
    WarmupMessage,
)
from app.services import mailer, providers, warmup
from app.services.audit import changes, log_activity, snapshot
from app.services.auth import normalize_email

router = APIRouter(prefix="/warmup", tags=["warmup"])

# Never include the password.
HELPER_AUDIT_FIELDS = [
    "email",
    "display_name",
    "provider",
    "smtp_host",
    "smtp_port",
    "smtp_security",
    "smtp_username",
    "imap_host",
    "imap_port",
    "status",
]
CONNECTION_FIELDS = ("smtp_host", "smtp_port", "smtp_security", "smtp_username", "imap_host", "imap_port")
TREND_DAYS = 14
RATE_DAYS = 7


# --- Schedule -----------------------------------------------------------------------------------------------


class ScheduleIO(BaseModel):
    timezone: str
    days: list[int] = Field(min_length=1, max_length=7)
    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=1, le=24)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, v: str) -> str:
        if v not in available_timezones():
            raise ValueError("unknown timezone")
        return v

    @field_validator("days")
    @classmethod
    def valid_days(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("days are 0 (Monday) to 6 (Sunday)")
        return sorted(set(v))


def _schedule_out(s: warmup.Schedule) -> ScheduleIO:
    return ScheduleIO(timezone=s.timezone, days=s.days, start_hour=s.start_hour, end_hour=s.end_hour)


@router.get("/schedule", response_model=ScheduleIO)
def read_schedule(db: DB, _: CurrentUser) -> ScheduleIO:
    return _schedule_out(warmup.get_schedule(db))


@router.put("/schedule", response_model=ScheduleIO)
def update_schedule(body: ScheduleIO, db: DB, user: EditorUser, actor: CurrentActor) -> ScheduleIO:
    if body.end_hour <= body.start_hour:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_hours")
    before = _schedule_out(warmup.get_schedule(db)).model_dump()
    after = body.model_dump()
    if before != after:
        warmup.set_schedule(db, warmup.Schedule(**after), user.id)
        old, new = changes(before, after)
        log_activity(
            db, actor, "settings.warmup_schedule_updated", entity_type="settings", before=old, after=new
        )
        db.commit()
    return body


# --- Helpers ---------------------------------------------------------------------------------------


class CheckOut(BaseModel):
    ok: bool
    detail: str


class HelperOut(BaseModel):
    id: int
    email: str
    display_name: str
    provider: HelperProvider
    smtp_host: str
    smtp_port: int
    smtp_security: SmtpSecurity
    smtp_username: str
    imap_host: str
    imap_port: int
    status: MailboxStatus
    last_error: str | None
    last_checked_at: datetime | None
    spam_folder: str | None
    # Warm-up emails it received in the last 7 days.
    received_7d: int
    created_at: datetime
    # The connection test run by this request (adding, changing settings, reactivating).
    connection: CheckOut | None = None


class HelperIn(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=200)
    # Guessed from the address (and its mail servers) when left out.
    provider: HelperProvider | None = None
    smtp_host: str = Field(min_length=1, max_length=255)
    smtp_port: int = Field(ge=1, le=65535)
    smtp_security: SmtpSecurity
    # Usually the email address itself.
    smtp_username: str | None = Field(default=None, max_length=320)
    # The account's password or app password (Gmail and Yahoo need an app password).
    password: str = Field(min_length=1, max_length=500)
    imap_host: str = Field(min_length=1, max_length=255)
    imap_port: int = Field(default=993, ge=1, le=65535)


class HelperUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    provider: HelperProvider | None = None
    smtp_host: str | None = Field(default=None, min_length=1, max_length=255)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_security: SmtpSecurity | None = None
    smtp_username: str | None = Field(default=None, min_length=1, max_length=320)
    password: str | None = Field(default=None, min_length=1, max_length=500)
    imap_host: str | None = Field(default=None, min_length=1, max_length=255)
    imap_port: int | None = Field(default=None, ge=1, le=65535)
    # ACTIVE (tested first) or PAUSED.
    status: MailboxStatus | None = None


def guess_provider(email: str) -> HelperProvider:
    domain = providers.domain_of(email)
    if domain.startswith("yahoo.") or domain in ("ymail.com", "rocketmail.com"):
        return HelperProvider.YAHOO
    found = providers.detect(domain)
    if found == EmailProvider.YAHOO:
        return HelperProvider.YAHOO
    if found == EmailProvider.GOOGLE:
        return HelperProvider.GMAIL
    if found == EmailProvider.MICROSOFT:
        return HelperProvider.OUTLOOK
    return HelperProvider.OTHER


def check_helper(helper: WarmupHelper) -> CheckOut:
    """Sign in to SMTP (without sending) and IMAP, and find the spam folder."""
    try:
        mailer.check_login(helper)
    except mailer.AuthFailed as e:
        return CheckOut(ok=False, detail=f"Sending (SMTP) refused the username or password: {e}")
    except mailer.SendFailed as e:
        return CheckOut(ok=False, detail=f"Sending (SMTP): {e}")
    try:
        with mailer.Inbox(helper) as inbox:
            spam = inbox.spam_folder()
    except mailer.InboxFailed as e:
        return CheckOut(ok=False, detail=f"Incoming mail (IMAP): {e}")
    helper.spam_folder = spam
    found = f'spam folder "{spam}"' if spam else "no spam folder found (only the inbox will be checked)"
    return CheckOut(ok=True, detail=f"Connected: sending and inbox both work, {found}.")


def _test_and_set_status(db: DB, helper: WarmupHelper, actor: CurrentActor) -> CheckOut:
    before = helper.status
    result = check_helper(helper)
    helper.last_checked_at = warmup.now()
    if result.ok:
        if helper.status == MailboxStatus.ERROR:
            helper.status = MailboxStatus.ACTIVE
        helper.last_error = None
    else:
        helper.status = MailboxStatus.ERROR
        helper.last_error = result.detail[:1000]
    log_activity(
        db,
        actor,
        "warmup.helper_tested",
        entity_type="warmup_helper",
        entity_id=helper.id,
        before={"status": before},
        after={"status": helper.status, "ok": result.ok, "detail": result.detail},
    )
    db.commit()
    return result


def _received(db: DB, helper_ids: list[int]) -> dict[int, int]:
    since = warmup.now() - timedelta(days=RATE_DAYS)
    return dict(
        db.execute(
            select(WarmupMessage.helper_id, func.count())
            .where(WarmupMessage.helper_id.in_(helper_ids), WarmupMessage.sent_at >= since)
            .group_by(WarmupMessage.helper_id)
        ).all()
    )


def _helper_out(h: WarmupHelper, received: int, connection: CheckOut | None = None) -> HelperOut:
    return HelperOut(
        id=h.id,
        email=h.email,
        display_name=h.display_name,
        provider=h.provider,
        smtp_host=h.smtp_host,
        smtp_port=h.smtp_port,
        smtp_security=h.smtp_security,
        smtp_username=h.smtp_username,
        imap_host=h.imap_host,
        imap_port=h.imap_port,
        status=h.status,
        last_error=h.last_error,
        last_checked_at=h.last_checked_at,
        spam_folder=h.spam_folder,
        received_7d=received,
        created_at=h.created_at,
        connection=connection,
    )


def _get_helper(db: DB, helper_id: int) -> WarmupHelper:
    helper = db.get(WarmupHelper, helper_id)
    if helper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "helper_not_found")
    return helper


def _check_allowed(smtp_host: str, imap_host: str, security: SmtpSecurity) -> None:
    if security == SmtpSecurity.NONE and not get_settings().smtp_allow_plain:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "plain_smtp_not_allowed")
    if (
        get_settings().is_development
        and not {smtp_host.lower(), imap_host.lower()} <= mailer.LOCAL_SMTP_HOSTS
    ):
        # Like mailboxes: a development copy never touches a real mail server.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "dev_real_helper")


@router.get("/helpers", response_model=list[HelperOut])
def list_helpers(db: DB, _: CurrentUser) -> list[HelperOut]:
    helpers = db.scalars(select(WarmupHelper).order_by(WarmupHelper.email)).all()
    received = _received(db, [h.id for h in helpers])
    return [_helper_out(h, received.get(h.id, 0)) for h in helpers]


@router.post("/helpers", response_model=HelperOut, status_code=status.HTTP_201_CREATED)
def create_helper(body: HelperIn, db: DB, user: EditorUser, actor: CurrentActor) -> HelperOut:
    email = normalize_email(body.email)
    _check_allowed(body.smtp_host.strip(), body.imap_host.strip(), body.smtp_security)
    if db.scalar(select(WarmupHelper.id).where(WarmupHelper.email == email)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "helper_exists")
    if db.scalar(select(Mailbox.id).where(Mailbox.email == email)) is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "helper_is_mailbox")
    helper = WarmupHelper(
        email=email,
        display_name=body.display_name.strip(),
        provider=body.provider or guess_provider(email),
        smtp_host=body.smtp_host.strip(),
        smtp_port=body.smtp_port,
        smtp_security=body.smtp_security,
        smtp_username=(body.smtp_username or email).strip(),
        smtp_password_encrypted=encrypt(body.password),
        imap_host=body.imap_host.strip(),
        imap_port=body.imap_port,
        status=MailboxStatus.ERROR,
        last_error="Connection not tested yet.",
        created_by_id=user.id,
    )
    db.add(helper)
    db.flush()
    log_activity(
        db,
        actor,
        "warmup.helper_added",
        entity_type="warmup_helper",
        entity_id=helper.id,
        after=snapshot(helper, HELPER_AUDIT_FIELDS),
    )
    db.commit()
    result = _test_and_set_status(db, helper, actor)
    return _helper_out(helper, 0, result)


@router.patch("/helpers/{helper_id}", response_model=HelperOut)
def update_helper(
    helper_id: int, body: HelperUpdateIn, db: DB, _: EditorUser, actor: CurrentActor
) -> HelperOut:
    helper = _get_helper(db, helper_id)
    _check_allowed(
        (body.smtp_host or helper.smtp_host).strip(),
        (body.imap_host or helper.imap_host).strip(),
        body.smtp_security or helper.smtp_security,
    )
    before = snapshot(helper, HELPER_AUDIT_FIELDS)
    for field in ["display_name", "smtp_host", "smtp_username", "imap_host"]:
        value = getattr(body, field)
        if value is not None:
            setattr(helper, field, value.strip())
    for field in ["provider", "smtp_port", "smtp_security", "imap_port"]:
        value = getattr(body, field)
        if value is not None:
            setattr(helper, field, value)
    if body.imap_host is not None and before["imap_host"] != helper.imap_host:
        helper.spam_folder = None
    password_changed = body.password is not None
    if password_changed:
        helper.smtp_password_encrypted = encrypt(body.password)
    if body.status == MailboxStatus.PAUSED:
        helper.status = MailboxStatus.PAUSED
    elif body.status == MailboxStatus.ACTIVE and helper.status == MailboxStatus.PAUSED:
        # Reactivating only sticks if the connection test below passes.
        helper.status = MailboxStatus.ERROR
    after = snapshot(helper, HELPER_AUDIT_FIELDS)
    if before != after or password_changed:
        old, new = changes(before, after)
        if password_changed:
            new["password_changed"] = True
        log_activity(
            db,
            actor,
            "warmup.helper_updated",
            entity_type="warmup_helper",
            entity_id=helper.id,
            before=old,
            after=new,
        )
        db.commit()
    connection_changed = password_changed or any(before[f] != after[f] for f in CONNECTION_FIELDS)
    result = None
    if connection_changed or body.status == MailboxStatus.ACTIVE:
        if helper.status == MailboxStatus.PAUSED and body.status != MailboxStatus.ACTIVE:
            result = check_helper(helper)  # report only: a paused helper stays paused
            db.commit()
        else:
            result = _test_and_set_status(db, helper, actor)
    return _helper_out(helper, _received(db, [helper.id]).get(helper.id, 0), result)


@router.post("/helpers/{helper_id}/test", response_model=HelperOut)
def test_helper(helper_id: int, db: DB, _: EditorUser, actor: CurrentActor) -> HelperOut:
    helper = _get_helper(db, helper_id)
    if helper.status == MailboxStatus.PAUSED:
        result = check_helper(helper)
        db.commit()
    else:
        result = _test_and_set_status(db, helper, actor)
    return _helper_out(helper, _received(db, [helper.id]).get(helper.id, 0), result)


@router.delete("/helpers/{helper_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_helper(helper_id: int, db: DB, _: EditorUser, actor: CurrentActor) -> None:
    helper = _get_helper(db, helper_id)
    log_activity(
        db,
        actor,
        "warmup.helper_removed",
        entity_type="warmup_helper",
        entity_id=helper.id,
        before=snapshot(helper, HELPER_AUDIT_FIELDS),
    )
    # Its past warm-up emails stay (inbox rates keep their provider); they just lose the link.
    db.delete(helper)
    db.commit()


# --- Overview --------------------------------------------------------------------------------------


class ProviderRate(BaseModel):
    provider: HelperProvider
    inbox: int
    spam: int
    missing: int
    # Share that landed in the inbox, 0-100; None before there are results.
    rate: int | None


class DayRate(BaseModel):
    date: date
    inbox: int
    total: int


class MailboxWarmup(BaseModel):
    id: int
    email: str
    display_name: str
    status: MailboxStatus
    warmup_enabled: bool
    # Helpers' answers are filed and reply checking works only with an incoming mail (IMAP) server.
    has_imap: bool
    daily_cap: int
    daily_limit_today: int
    ramp_up_enabled: bool
    ramp_day: int | None
    sent_today: int
    planned_today: int
    rates: list[ProviderRate]
    rate: int | None
    trend: list[DayRate]
    low_rate: bool
    campaigns_paused_at: datetime | None
    campaigns_paused_reason: str | None


class OverviewOut(BaseModel):
    schedule: ScheduleIO
    mailboxes: list[MailboxWarmup]
    active_helpers: int
    helpers_with_errors: int


def _percent(inbox: int, total: int) -> int | None:
    return round(inbox * 100 / total) if total else None


@router.get("", response_model=OverviewOut)
def overview(db: DB, _: CurrentUser) -> OverviewOut:
    at = warmup.now()
    schedule = warmup.get_schedule(db)
    today = at.astimezone(ZoneInfo(schedule.timezone)).date()
    mailboxes = db.scalars(select(Mailbox).where(Mailbox.archived_at.is_(None)).order_by(Mailbox.email)).all()
    rates = warmup.inbox_rates(db, [m.id for m in mailboxes], at - timedelta(days=RATE_DAYS))
    out = []
    for m in mailboxes:
        by_provider = [
            ProviderRate(
                provider=HelperProvider(p),
                **counts,
                rate=_percent(counts["inbox"], sum(counts.values())),
            )
            for p, counts in sorted(rates[m.id].items())
        ]
        inbox = sum(r.inbox for r in by_provider)
        total = sum(r.inbox + r.spam + r.missing for r in by_provider)
        out.append(
            MailboxWarmup(
                id=m.id,
                email=m.email,
                display_name=m.display_name,
                status=m.status,
                warmup_enabled=m.warmup_enabled,
                has_imap=bool(m.imap_host),
                daily_cap=m.daily_cap,
                daily_limit_today=warmup.daily_limit(m, at.date()),
                ramp_up_enabled=m.ramp_up_enabled,
                ramp_day=warmup.ramp_day(m, at.date()),
                sent_today=warmup.attempted_today(db, m.id, today, schedule),
                planned_today=len(warmup.plan_for_day(m.id, today, schedule)) if m.warmup_enabled else 0,
                rates=by_provider,
                rate=_percent(inbox, total),
                trend=[
                    DayRate(date=d, inbox=i, total=t)
                    for d, i, t in warmup.daily_rates(db, m.id, TREND_DAYS, at, schedule.timezone)
                ],
                low_rate=m.warmup_enabled and warmup.low_rate(db, m, at, schedule.timezone),
                campaigns_paused_at=m.campaigns_paused_at,
                campaigns_paused_reason=m.campaigns_paused_reason,
            )
        )
    helpers = db.scalars(select(WarmupHelper.status)).all()
    return OverviewOut(
        schedule=_schedule_out(schedule),
        mailboxes=out,
        active_helpers=sum(1 for s in helpers if s == MailboxStatus.ACTIVE),
        helpers_with_errors=sum(1 for s in helpers if s == MailboxStatus.ERROR),
    )
