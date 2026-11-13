import random
import re
from datetime import datetime
from typing import Annotated, Any
from zoneinfo import available_timezones

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.api.deps import DB, CurrentActor, CurrentUser, EditorUser
from app.api.schemas import Page
from app.models import (
    BodyFormat,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    EmailProvider,
    Lead,
    LeadGroup,
    Mailbox,
    MailboxStatus,
    RecipientStatus,
    campaign_exclusions,
    lead_group_members,
)
from app.services import mailer, sending
from app.services.app_settings import BUSINESS_FIELDS, business_fields
from app.services.audit import changes, log_activity, snapshot
from app.services.csv_export import csv_response
from app.services.email_html import sanitize, to_text
from app.services.templates import FIXED_FIELDS, compose, lead_fields, variables_used

router = APIRouter(prefix="/campaigns", tags=["campaigns"])

AUDIT_FIELDS = [
    "name",
    "status",
    "subject",
    "body",
    "body_format",
    "use_lead_timezone",
    "gap_min_seconds",
    "gap_max_seconds",
    "timezone",
    "send_days",
    "start_hour",
    "end_hour",
]
# Fields that can change while a campaign is paused (they only affect emails not yet sent).
PAUSED_EDITABLE = {
    "subject",
    "body",
    "body_format",
    "use_lead_timezone",
    "gap_min_seconds",
    "gap_max_seconds",
    "timezone",
    "send_days",
    "start_hour",
    "end_hour",
    "mailbox_ids",
}
MIN_GAP_SECONDS = 60


class Schedule(BaseModel):
    gap_min_seconds: int = Field(default=300, ge=MIN_GAP_SECONDS, le=86_400)
    gap_max_seconds: int = Field(default=600, ge=MIN_GAP_SECONDS, le=86_400)
    timezone: str = "Asia/Karachi"
    send_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4], min_length=1, max_length=7)
    start_hour: int = Field(default=9, ge=0, le=23)
    end_hour: int = Field(default=17, ge=1, le=24)
    use_lead_timezone: bool = False

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, v: str) -> str:
        if v not in available_timezones():
            raise ValueError("unknown timezone")
        return v

    @field_validator("send_days")
    @classmethod
    def valid_days(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("days are 0 (Monday) to 6 (Sunday)")
        return sorted(set(v))


class CampaignIn(Schedule):
    name: str = Field(min_length=1, max_length=200)
    subject: str = Field(default="", max_length=998)
    body: str = Field(default="", max_length=100_000)
    body_format: BodyFormat = BodyFormat.TEXT
    group_ids: list[int] = Field(default_factory=list, max_length=100)
    mailbox_ids: list[int] = Field(default_factory=list, max_length=100)


class CampaignUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    subject: str | None = Field(default=None, max_length=998)
    body: str | None = Field(default=None, max_length=100_000)
    body_format: BodyFormat | None = None
    group_ids: list[int] | None = Field(default=None, max_length=100)
    mailbox_ids: list[int] | None = Field(default=None, max_length=100)
    use_lead_timezone: bool | None = None
    gap_min_seconds: int | None = None
    gap_max_seconds: int | None = None
    timezone: str | None = None
    send_days: list[int] | None = None
    start_hour: int | None = None
    end_hour: int | None = None


class GroupRef(BaseModel):
    id: int
    name: str


class MailboxRef(BaseModel):
    id: int
    email: str
    display_name: str
    status: MailboxStatus
    next_send_at: datetime | None
    daily_cap: int


class Counts(BaseModel):
    total: int = 0
    waiting: int = 0
    # Being handed to the mail server right now (seconds), or cut off by a crash until recovered.
    sending: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    cancelled: int = 0


class CampaignOut(BaseModel):
    id: int
    name: str
    status: CampaignStatus
    subject: str
    body: str
    body_format: BodyFormat
    groups: list[GroupRef]
    mailboxes: list[MailboxRef]
    gap_min_seconds: int
    gap_max_seconds: int
    timezone: str
    send_days: list[int]
    start_hour: int
    end_hour: int
    use_lead_timezone: bool
    # Leads in the chosen groups that were unticked before starting.
    excluded_count: int
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    counts: Counts


class RecipientOut(BaseModel):
    id: int
    email: str
    name: str | None
    company: str | None
    status: RecipientStatus
    skip_reason: str | None
    mailbox_email: str | None
    attempts: int
    next_attempt_at: datetime | None
    sent_at: datetime | None
    smtp_response: str | None
    error: str | None
    subject_sent: str | None
    body_sent: str | None
    html_sent: str | None
    sent_copy_saved: bool | None


class PreviewIn(BaseModel):
    subject: str = Field(default="", max_length=998)
    body: str = Field(default="", max_length=100_000)
    body_format: BodyFormat = BodyFormat.TEXT
    group_ids: list[int] = Field(default_factory=list, max_length=100)
    mailbox_id: int | None = None


class PreviewVersion(BaseModel):
    subject: str
    body: str
    html: str | None


class AudienceLead(BaseModel):
    id: int
    email: str
    name: str | None
    company: str | None
    country: str | None
    timezone: str | None
    email_provider: EmailProvider | None
    excluded: bool


class ExclusionsIn(BaseModel):
    lead_ids: list[int] = Field(min_length=1, max_length=5000)
    excluded: bool


class ExclusionsOut(BaseModel):
    excluded_count: int


class PreviewOut(BaseModel):
    lead_email: str | None
    versions: list[PreviewVersion]
    available_fields: list[str]
    unknown_variables: list[str]


class SendTestIn(BaseModel):
    to: EmailStr
    mailbox_id: int | None = None


class SendTestOut(BaseModel):
    ok: bool
    detail: str


def _counts(db: DB, campaign_ids: list[int]) -> dict[int, Counts]:
    result: dict[int, Counts] = {cid: Counts() for cid in campaign_ids}
    rows = db.execute(
        select(CampaignRecipient.campaign_id, CampaignRecipient.status, func.count())
        .where(CampaignRecipient.campaign_id.in_(campaign_ids))
        .group_by(CampaignRecipient.campaign_id, CampaignRecipient.status)
    )
    for cid, st, n in rows:
        counts = result[cid]
        setattr(counts, st.value.lower(), n)
        counts.total += n
    return result


def _excluded(db: DB, campaign_ids: list[int]) -> dict[int, int]:
    rows = db.execute(
        select(campaign_exclusions.c.campaign_id, func.count())
        .where(campaign_exclusions.c.campaign_id.in_(campaign_ids))
        .group_by(campaign_exclusions.c.campaign_id)
    )
    return {cid: n for cid, n in rows}


def _full(db: DB, c: Campaign) -> "CampaignOut":
    return _out(c, _counts(db, [c.id])[c.id], _excluded(db, [c.id]).get(c.id, 0))


def _out(c: Campaign, counts: Counts, excluded: int = 0) -> CampaignOut:
    return CampaignOut(
        id=c.id,
        name=c.name,
        status=c.status,
        subject=c.subject,
        body=c.body,
        body_format=c.body_format,
        groups=[GroupRef(id=g.id, name=g.name) for g in c.groups],
        mailboxes=[
            MailboxRef(
                id=m.id,
                email=m.email,
                display_name=m.display_name,
                status=m.status,
                next_send_at=m.next_send_at,
                daily_cap=m.daily_cap,
            )
            for m in c.mailboxes
        ],
        gap_min_seconds=c.gap_min_seconds,
        gap_max_seconds=c.gap_max_seconds,
        timezone=c.timezone,
        send_days=c.send_days,
        start_hour=c.start_hour,
        end_hour=c.end_hour,
        use_lead_timezone=c.use_lead_timezone,
        excluded_count=excluded,
        started_at=c.started_at,
        finished_at=c.finished_at,
        created_at=c.created_at,
        counts=counts,
    )


def _has_content(c: Campaign) -> bool:
    body = to_text(c.body) if c.body_format == BodyFormat.HTML else c.body
    return bool(c.subject.strip() and body.strip())


def _get(db: DB, campaign_id: int) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign_not_found")
    return campaign


def _load(db: DB, model: type[LeadGroup] | type[Mailbox], ids: list[int], missing: str) -> list[Any]:
    ids = list(dict.fromkeys(ids))
    found = db.scalars(select(model).where(model.id.in_(ids))).all() if ids else []
    if len(found) != len(ids):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, missing)
    order = {i: n for n, i in enumerate(ids)}
    return sorted(found, key=lambda x: order[x.id])


def _usable_mailboxes(db: DB, ids: list[int], already: list[Mailbox]) -> list[Mailbox]:
    """Load mailboxes for a campaign. Archived ones can't be newly picked (ones already on it may stay)."""
    mailboxes = _load(db, Mailbox, ids, "mailbox_not_found")
    kept = {m.id for m in already}
    if any(m.archived_at is not None and m.id not in kept for m in mailboxes):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "mailbox_archived")
    return mailboxes


def _snapshot(c: Campaign) -> dict[str, Any]:
    return {
        **snapshot(c, AUDIT_FIELDS),
        "group_ids": sorted(g.id for g in c.groups),
        "mailbox_ids": sorted(m.id for m in c.mailboxes),
    }


def _sample_lead(db: DB, group_ids: list[int]) -> Lead | None:
    if not group_ids:
        return None
    query = (
        select(Lead)
        .join(lead_group_members, lead_group_members.c.lead_id == Lead.id)
        .where(lead_group_members.c.group_id.in_(group_ids))
    )
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    if not total:
        return None
    return db.scalars(query.order_by(Lead.id).offset(random.randrange(total)).limit(1)).first()  # noqa: S311


def _available_fields(db: DB, group_ids: list[int]) -> list[str]:
    keys: set[str] = set()
    if group_ids:
        customs = db.scalars(
            select(Lead.custom)
            .join(lead_group_members, lead_group_members.c.lead_id == Lead.id)
            .where(lead_group_members.c.group_id.in_(group_ids))
            .limit(1000)
        )
        for custom in customs:
            keys.update((custom or {}).keys())
    return [*FIXED_FIELDS, "sender_name", *BUSINESS_FIELDS, *sorted(keys - set(BUSINESS_FIELDS))]


@router.get("", response_model=list[CampaignOut])
def list_campaigns(db: DB, _: CurrentUser) -> list[CampaignOut]:
    campaigns = db.scalars(select(Campaign).order_by(Campaign.created_at.desc())).all()
    ids = [c.id for c in campaigns]
    counts, excluded = _counts(db, ids), _excluded(db, ids)
    return [_out(c, counts[c.id], excluded.get(c.id, 0)) for c in campaigns]


@router.post("", response_model=CampaignOut, status_code=status.HTTP_201_CREATED)
def create_campaign(body: CampaignIn, db: DB, user: EditorUser, actor: CurrentActor) -> CampaignOut:
    if body.gap_max_seconds < body.gap_min_seconds:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_gap")
    if body.end_hour <= body.start_hour:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_hours")
    campaign = Campaign(
        name=body.name.strip(),
        status=CampaignStatus.DRAFT,
        subject=body.subject,
        body=sanitize(body.body) if body.body_format == BodyFormat.HTML else body.body,
        body_format=body.body_format,
        use_lead_timezone=body.use_lead_timezone,
        gap_min_seconds=body.gap_min_seconds,
        gap_max_seconds=body.gap_max_seconds,
        timezone=body.timezone,
        send_days=body.send_days,
        start_hour=body.start_hour,
        end_hour=body.end_hour,
        created_by_id=user.id,
        groups=_load(db, LeadGroup, body.group_ids, "group_not_found"),
        mailboxes=_usable_mailboxes(db, body.mailbox_ids, already=[]),
    )
    db.add(campaign)
    db.flush()
    log_activity(
        db,
        actor,
        "campaign.created",
        entity_type="campaign",
        entity_id=campaign.id,
        after=_snapshot(campaign),
    )
    db.commit()
    return _out(campaign, Counts())


@router.post("/preview", response_model=PreviewOut)
def preview(body: PreviewIn, db: DB, _: CurrentUser) -> PreviewOut:
    """Three random versions of the email for one random lead, plus any variables no lead has."""
    lead = _sample_lead(db, body.group_ids)
    mailbox = db.get(Mailbox, body.mailbox_id) if body.mailbox_id else None
    sender_name = mailbox.display_name if mailbox else "Your name"
    fields = {**business_fields(db), **(lead_fields(lead) if lead else {})}
    rng = random.Random()  # noqa: S311
    versions = []
    for _ in range(3):
        subject, text, html = compose(body.subject, body.body, fields, sender_name, rng, body.body_format)
        versions.append(PreviewVersion(subject=subject, body=text, html=html))
    available = _available_fields(db, body.group_ids)
    unknown = sorted(variables_used(body.subject, body.body) - set(available))
    return PreviewOut(
        lead_email=lead.email if lead else None,
        versions=versions,
        available_fields=available,
        unknown_variables=unknown,
    )


@router.get("/{campaign_id}", response_model=CampaignOut)
def get_campaign(campaign_id: int, db: DB, _: CurrentUser) -> CampaignOut:
    campaign = _get(db, campaign_id)
    return _full(db, campaign)


@router.patch("/{campaign_id}", response_model=CampaignOut)
def update_campaign(
    campaign_id: int, body: CampaignUpdateIn, db: DB, _: EditorUser, actor: CurrentActor
) -> CampaignOut:
    campaign = _get(db, campaign_id)
    sent = body.model_dump(exclude_unset=True)
    changed = set(sent) - {"name"}
    if campaign.status == CampaignStatus.PAUSED and not changed <= PAUSED_EDITABLE:
        raise HTTPException(status.HTTP_409_CONFLICT, "campaign_not_editable")
    if campaign.status not in (CampaignStatus.DRAFT, CampaignStatus.PAUSED) and changed:
        raise HTTPException(status.HTTP_409_CONFLICT, "campaign_not_editable")

    schedule = Schedule.model_validate({f: sent.get(f, getattr(campaign, f)) for f in Schedule.model_fields})
    if schedule.gap_max_seconds < schedule.gap_min_seconds:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_gap")
    if schedule.end_hour <= schedule.start_hour:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_hours")

    before = _snapshot(campaign)
    for field, value in schedule.model_dump().items():
        setattr(campaign, field, value)
    if body.name is not None:
        campaign.name = body.name.strip()
    if body.subject is not None:
        campaign.subject = body.subject
    if body.body_format is not None:
        campaign.body_format = body.body_format
    if body.body is not None:
        campaign.body = sanitize(body.body) if campaign.body_format == BodyFormat.HTML else body.body
    if body.group_ids is not None:
        campaign.groups = _load(db, LeadGroup, body.group_ids, "group_not_found")
    if body.mailbox_ids is not None:
        campaign.mailboxes = _usable_mailboxes(db, body.mailbox_ids, already=campaign.mailboxes)
    after = _snapshot(campaign)
    if before != after:
        old, new = changes(before, after)
        log_activity(
            db,
            actor,
            "campaign.updated",
            entity_type="campaign",
            entity_id=campaign.id,
            before=old,
            after=new,
        )
        db.commit()
    return _full(db, campaign)


@router.post("/{campaign_id}/start", response_model=CampaignOut)
def start_campaign(campaign_id: int, db: DB, _: EditorUser, actor: CurrentActor) -> CampaignOut:
    campaign = _get(db, campaign_id)
    if campaign.status != CampaignStatus.DRAFT:
        raise HTTPException(status.HTTP_409_CONFLICT, "campaign_not_draft")
    if not _has_content(campaign):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "content_missing")
    if not campaign.groups:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no_groups")
    if not any(m.status == MailboxStatus.ACTIVE for m in campaign.mailboxes):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no_active_mailbox")
    sending.start(db, campaign, actor, sending.now())
    db.commit()
    return _full(db, campaign)


def _transition(
    db: DB,
    actor: CurrentActor,
    campaign: Campaign,
    allowed: set[CampaignStatus],
    to: CampaignStatus,
    action: str,
) -> CampaignOut:
    if campaign.status not in allowed:
        raise HTTPException(status.HTTP_409_CONFLICT, "invalid_transition")
    before = campaign.status
    campaign.status = to
    if to == CampaignStatus.CANCELLED:
        campaign.finished_at = sending.now()
        for r in db.scalars(
            select(CampaignRecipient).where(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.status == RecipientStatus.WAITING,
            )
        ):
            r.status = RecipientStatus.CANCELLED
    log_activity(
        db,
        actor,
        action,
        entity_type="campaign",
        entity_id=campaign.id,
        before={"status": before},
        after={"status": to},
    )
    db.commit()
    return _full(db, campaign)


@router.post("/{campaign_id}/pause", response_model=CampaignOut)
def pause_campaign(campaign_id: int, db: DB, _: EditorUser, actor: CurrentActor) -> CampaignOut:
    campaign = _get(db, campaign_id)
    return _transition(
        db, actor, campaign, {CampaignStatus.RUNNING}, CampaignStatus.PAUSED, "campaign.paused"
    )


@router.post("/{campaign_id}/resume", response_model=CampaignOut)
def resume_campaign(campaign_id: int, db: DB, _: EditorUser, actor: CurrentActor) -> CampaignOut:
    campaign = _get(db, campaign_id)
    return _transition(
        db, actor, campaign, {CampaignStatus.PAUSED}, CampaignStatus.RUNNING, "campaign.resumed"
    )


@router.post("/{campaign_id}/cancel", response_model=CampaignOut)
def cancel_campaign(campaign_id: int, db: DB, _: EditorUser, actor: CurrentActor) -> CampaignOut:
    campaign = _get(db, campaign_id)
    return _transition(
        db,
        actor,
        campaign,
        {CampaignStatus.RUNNING, CampaignStatus.PAUSED},
        CampaignStatus.CANCELLED,
        "campaign.cancelled",
    )


@router.post("/{campaign_id}/test-send", response_model=SendTestOut)
def send_test(campaign_id: int, body: SendTestIn, db: DB, _: EditorUser, actor: CurrentActor) -> SendTestOut:
    """Send the email, filled in for a random lead, to one address (usually your own)."""
    campaign = _get(db, campaign_id)
    mailbox = db.get(Mailbox, body.mailbox_id) if body.mailbox_id else next(iter(campaign.mailboxes), None)
    if mailbox is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no_mailbox")
    if not _has_content(campaign):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "content_missing")
    lead = _sample_lead(db, [g.id for g in campaign.groups])
    subject, text, html = compose(
        campaign.subject,
        campaign.body,
        {**business_fields(db), **(lead_fields(lead) if lead else {})},
        mailbox.display_name,
        random.Random(),  # noqa: S311
        campaign.body_format,
    )
    msg = mailer.build_message(mailbox, str(body.to), f"[Test] {subject}", text, html)
    try:
        sent = mailer.send(mailbox, msg)
        result = SendTestOut(ok=True, detail=f"Sent from {mailbox.email}. Server reply: {sent.response}")
    except mailer.AuthFailed as e:
        result = SendTestOut(
            ok=False, detail=f"The server rejected {mailbox.email}'s username or password ({e})."
        )
    except mailer.SendFailed as e:
        result = SendTestOut(ok=False, detail=str(e))
    log_activity(
        db,
        actor,
        "campaign.test_sent",
        entity_type="campaign",
        entity_id=campaign.id,
        after={"to": str(body.to), "mailbox": mailbox.email, "ok": result.ok, "detail": result.detail},
    )
    db.commit()
    return result


FORGOTTEN_LABEL = "Forgotten person"


def _recipients_query(campaign_id: int, status_filter: RecipientStatus | None, q: str | None) -> Select:
    query = (
        select(CampaignRecipient, Lead, Mailbox.email)
        .outerjoin(Lead, Lead.id == CampaignRecipient.lead_id)
        .outerjoin(Mailbox, Mailbox.id == CampaignRecipient.mailbox_id)
        .where(CampaignRecipient.campaign_id == campaign_id)
    )
    if status_filter:
        query = query.where(CampaignRecipient.status == status_filter)
    if q:
        like = f"%{q.strip()}%"
        query = query.where(
            or_(CampaignRecipient.email.ilike(like), Lead.first_name.ilike(like), Lead.company.ilike(like))
        )
    return query


def _recipient_out(r: CampaignRecipient, lead: Lead | None, mailbox_email: str | None) -> RecipientOut:
    return RecipientOut(
        id=r.id,
        email=r.email if r.lead_id is not None else FORGOTTEN_LABEL,
        name=(" ".join(p for p in (lead.first_name, lead.last_name) if p) or None) if lead else None,
        company=lead.company if lead else None,
        status=r.status,
        skip_reason=r.skip_reason,
        mailbox_email=mailbox_email,
        attempts=r.attempts,
        next_attempt_at=r.next_attempt_at,
        sent_at=r.sent_at,
        smtp_response=r.smtp_response,
        error=r.error,
        subject_sent=r.subject_sent,
        body_sent=r.body_sent,
        html_sent=r.html_sent,
        sent_copy_saved=r.sent_copy_saved,
    )


StatusFilter = Annotated[RecipientStatus | None, Query(alias="status")]


@router.get("/{campaign_id}/recipients", response_model=Page[RecipientOut])
def list_recipients(
    campaign_id: int,
    db: DB,
    _: CurrentUser,
    status_filter: StatusFilter = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[RecipientOut]:
    _get(db, campaign_id)
    query = _recipients_query(campaign_id, status_filter, q)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(query.order_by(CampaignRecipient.id).limit(limit).offset(offset))
    return Page(items=[_recipient_out(*row) for row in rows], total=total)


@router.get("/{campaign_id}/recipients/export")
def export_recipients(
    campaign_id: int,
    db: DB,
    _: CurrentUser,
    actor: CurrentActor,
    status_filter: StatusFilter = None,
    q: str | None = None,
) -> StreamingResponse:
    """The per-lead report as CSV (the current filters apply)."""
    campaign = _get(db, campaign_id)
    rows = [
        _recipient_out(*row)
        for row in db.execute(_recipients_query(campaign_id, status_filter, q).order_by(CampaignRecipient.id))
    ]
    log_activity(
        db,
        actor,
        "campaign.recipients_exported",
        entity_type="campaign",
        entity_id=campaign.id,
        after={"status": status_filter, "q": q, "rows": len(rows)},
    )
    db.commit()
    safe_name = re.sub(r"[^A-Za-z0-9]+", "-", campaign.name).strip("-")[:60] or "campaign"
    return csv_response(
        f"{safe_name}-recipients-{datetime.now():%Y%m%d-%H%M}.csv",
        [
            "email",
            "name",
            "company",
            "status",
            "skip_reason",
            "from_mailbox",
            "attempts",
            "sent_at",
            "next_retry_at",
            "server_reply",
            "error",
            "subject",
            "sent_copy_saved",
        ],
        (
            [
                r.email,
                r.name,
                r.company,
                r.status.value,
                r.skip_reason,
                r.mailbox_email,
                r.attempts,
                r.sent_at.isoformat() if r.sent_at else None,
                r.next_attempt_at.isoformat() if r.next_attempt_at else None,
                r.smtp_response,
                r.error,
                r.subject_sent,
                None if r.sent_copy_saved is None else ("yes" if r.sent_copy_saved else "no"),
            ]
            for r in rows
        ),
    )


@router.get("/{campaign_id}/audience", response_model=Page[AudienceLead])
def list_audience(
    campaign_id: int,
    db: DB,
    _: CurrentUser,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AudienceLead]:
    """The leads the campaign's groups hold now, with whether each has been left out."""
    campaign = _get(db, campaign_id)
    group_ids = [g.id for g in campaign.groups]
    excluded = select(campaign_exclusions.c.lead_id).where(campaign_exclusions.c.campaign_id == campaign.id)
    query = (
        select(Lead, Lead.id.in_(excluded))
        .join(lead_group_members, lead_group_members.c.lead_id == Lead.id)
        .where(lead_group_members.c.group_id.in_(group_ids))
        .distinct()
    )
    if q:
        like = f"%{q.strip()}%"
        query = query.where(
            or_(Lead.email.ilike(like), Lead.first_name.ilike(like), Lead.company.ilike(like))
        )
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(query.order_by(Lead.id).limit(limit).offset(offset))
    return Page(
        items=[
            AudienceLead(
                id=lead.id,
                email=lead.email,
                name=" ".join(p for p in (lead.first_name, lead.last_name) if p) or None,
                company=lead.company,
                country=lead.country,
                timezone=lead.timezone,
                email_provider=lead.email_provider,
                excluded=is_excluded,
            )
            for lead, is_excluded in rows
        ],
        total=total,
    )


@router.put("/{campaign_id}/exclusions", response_model=ExclusionsOut)
def set_exclusions(
    campaign_id: int, body: ExclusionsIn, db: DB, _: EditorUser, actor: CurrentActor
) -> ExclusionsOut:
    """Leave leads out of (or put them back into) a draft campaign before it starts."""
    campaign = _get(db, campaign_id)
    if campaign.status != CampaignStatus.DRAFT:
        raise HTTPException(status.HTTP_409_CONFLICT, "campaign_not_draft")
    ids = list(dict.fromkeys(body.lead_ids))
    if body.excluded:
        db.execute(
            insert(campaign_exclusions)
            .values([{"campaign_id": campaign.id, "lead_id": lid} for lid in ids])
            .on_conflict_do_nothing()
        )
    else:
        db.execute(
            delete(campaign_exclusions).where(
                campaign_exclusions.c.campaign_id == campaign.id, campaign_exclusions.c.lead_id.in_(ids)
            )
        )
    log_activity(
        db,
        actor,
        "campaign.leads_excluded" if body.excluded else "campaign.leads_included",
        entity_type="campaign",
        entity_id=campaign.id,
        after={"lead_ids": ids[:200], "count": len(ids)},
    )
    db.commit()
    return ExclusionsOut(excluded_count=_excluded(db, [campaign.id]).get(campaign.id, 0))
