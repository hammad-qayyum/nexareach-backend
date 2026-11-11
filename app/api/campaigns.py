import random
from datetime import datetime
from typing import Annotated, Any
from zoneinfo import available_timezones

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, or_, select

from app.api.deps import DB, CurrentActor, CurrentUser, EditorUser
from app.api.schemas import Page
from app.models import (
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    Lead,
    LeadGroup,
    Mailbox,
    MailboxStatus,
    RecipientStatus,
    lead_group_members,
)
from app.services import mailer, sending
from app.services.app_settings import get_footer
from app.services.audit import changes, log_activity, snapshot
from app.services.templates import FIXED_FIELDS, compose, footer, lead_fields, variables_used

router = APIRouter(prefix="/campaigns", tags=["campaigns"])

AUDIT_FIELDS = [
    "name",
    "status",
    "subject",
    "body",
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
    body: str = Field(default="", max_length=50_000)
    group_ids: list[int] = Field(default_factory=list, max_length=100)
    mailbox_ids: list[int] = Field(default_factory=list, max_length=100)


class CampaignUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    subject: str | None = Field(default=None, max_length=998)
    body: str | None = Field(default=None, max_length=50_000)
    group_ids: list[int] | None = Field(default=None, max_length=100)
    mailbox_ids: list[int] | None = Field(default=None, max_length=100)
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


class Counts(BaseModel):
    total: int = 0
    waiting: int = 0
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
    groups: list[GroupRef]
    mailboxes: list[MailboxRef]
    gap_min_seconds: int
    gap_max_seconds: int
    timezone: str
    send_days: list[int]
    start_hour: int
    end_hour: int
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


class PreviewIn(BaseModel):
    subject: str = Field(default="", max_length=998)
    body: str = Field(default="", max_length=50_000)
    group_ids: list[int] = Field(default_factory=list, max_length=100)
    mailbox_id: int | None = None


class PreviewVersion(BaseModel):
    subject: str
    body: str


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


def _out(c: Campaign, counts: Counts) -> CampaignOut:
    return CampaignOut(
        id=c.id,
        name=c.name,
        status=c.status,
        subject=c.subject,
        body=c.body,
        groups=[GroupRef(id=g.id, name=g.name) for g in c.groups],
        mailboxes=[
            MailboxRef(
                id=m.id,
                email=m.email,
                display_name=m.display_name,
                status=m.status,
                next_send_at=m.next_send_at,
            )
            for m in c.mailboxes
        ],
        gap_min_seconds=c.gap_min_seconds,
        gap_max_seconds=c.gap_max_seconds,
        timezone=c.timezone,
        send_days=c.send_days,
        start_hour=c.start_hour,
        end_hour=c.end_hour,
        started_at=c.started_at,
        finished_at=c.finished_at,
        created_at=c.created_at,
        counts=counts,
    )


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
    return [*FIXED_FIELDS, "sender_name", *sorted(keys)]


def _footer_text(db: DB, sender_name: str) -> str:
    business_name, postal_address = get_footer(db)
    return footer(
        sender_name, business_name or "(business name not set)", postal_address or "(address not set)"
    )


@router.get("", response_model=list[CampaignOut])
def list_campaigns(db: DB, _: CurrentUser) -> list[CampaignOut]:
    campaigns = db.scalars(select(Campaign).order_by(Campaign.created_at.desc())).all()
    counts = _counts(db, [c.id for c in campaigns])
    return [_out(c, counts[c.id]) for c in campaigns]


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
        body=body.body,
        gap_min_seconds=body.gap_min_seconds,
        gap_max_seconds=body.gap_max_seconds,
        timezone=body.timezone,
        send_days=body.send_days,
        start_hour=body.start_hour,
        end_hour=body.end_hour,
        created_by_id=user.id,
        groups=_load(db, LeadGroup, body.group_ids, "group_not_found"),
        mailboxes=_load(db, Mailbox, body.mailbox_ids, "mailbox_not_found"),
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
    fields = lead_fields(lead) if lead else {}
    footer_text = _footer_text(db, sender_name)
    rng = random.Random()  # noqa: S311
    versions = [
        PreviewVersion(
            **dict(
                zip(
                    ("subject", "body"),
                    compose(body.subject, body.body, fields, sender_name, footer_text, rng),
                    strict=True,
                )
            )
        )
        for _ in range(3)
    ]
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
    return _out(campaign, _counts(db, [campaign.id])[campaign.id])


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
    if body.body is not None:
        campaign.body = body.body
    if body.group_ids is not None:
        campaign.groups = _load(db, LeadGroup, body.group_ids, "group_not_found")
    if body.mailbox_ids is not None:
        campaign.mailboxes = _load(db, Mailbox, body.mailbox_ids, "mailbox_not_found")
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
    return _out(campaign, _counts(db, [campaign.id])[campaign.id])


@router.post("/{campaign_id}/start", response_model=CampaignOut)
def start_campaign(campaign_id: int, db: DB, _: EditorUser, actor: CurrentActor) -> CampaignOut:
    campaign = _get(db, campaign_id)
    if campaign.status != CampaignStatus.DRAFT:
        raise HTTPException(status.HTTP_409_CONFLICT, "campaign_not_draft")
    if not campaign.subject.strip() or not campaign.body.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "content_missing")
    if not campaign.groups:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no_groups")
    if not any(m.status == MailboxStatus.ACTIVE for m in campaign.mailboxes):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no_active_mailbox")
    business_name, postal_address = get_footer(db)
    if not business_name or not postal_address:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "footer_missing")
    sending.start(db, campaign, actor, sending.now())
    db.commit()
    return _out(campaign, _counts(db, [campaign.id])[campaign.id])


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
    return _out(campaign, _counts(db, [campaign.id])[campaign.id])


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
    if not campaign.subject.strip() or not campaign.body.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "content_missing")
    lead = _sample_lead(db, [g.id for g in campaign.groups])
    subject, text = compose(
        campaign.subject,
        campaign.body,
        lead_fields(lead) if lead else {},
        mailbox.display_name,
        _footer_text(db, mailbox.display_name),
        random.Random(),  # noqa: S311
    )
    msg = mailer.build_message(mailbox, str(body.to), f"[Test] {subject}", text)
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


@router.get("/{campaign_id}/recipients", response_model=Page[RecipientOut])
def list_recipients(
    campaign_id: int,
    db: DB,
    _: CurrentUser,
    status_filter: Annotated[RecipientStatus | None, Query(alias="status")] = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[RecipientOut]:
    _get(db, campaign_id)
    query = (
        select(CampaignRecipient, Lead, Mailbox.email)
        .join(Lead, Lead.id == CampaignRecipient.lead_id)
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
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(query.order_by(CampaignRecipient.id).limit(limit).offset(offset))
    items = [
        RecipientOut(
            id=r.id,
            email=r.email,
            name=" ".join(p for p in (lead.first_name, lead.last_name) if p) or None,
            company=lead.company,
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
        )
        for r, lead, mailbox_email in rows
    ]
    return Page(items=items, total=total)
