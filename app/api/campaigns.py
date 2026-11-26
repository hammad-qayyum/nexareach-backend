import logging
import random
import re
from datetime import UTC, datetime
from typing import Annotated, Any
from zoneinfo import available_timezones

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, Field, ValidationError, field_validator
from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import selectinload

from app.api.deps import DB, CurrentActor, CurrentUser, EditorUser
from app.api.schemas import DeliveryEventOut, Page
from app.core.config import get_settings
from app.models import (
    EMAILED,
    BodyFormat,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    DealsHandover,
    DealsLeadResult,
    DeliveryEvent,
    DeliveryStatus,
    EmailProvider,
    FollowUpEmail,
    FollowUpStatus,
    Lead,
    LeadGroup,
    Mailbox,
    MailboxStatus,
    RecipientStatus,
    Reply,
    campaign_exclusions,
    lead_group_members,
)
from app.services import deals, mailer, pacing, replies, sending
from app.services.app_settings import BUSINESS_FIELDS, business_fields, email_footer
from app.services.audit import changes, log_activity, snapshot
from app.services.csv_export import csv_response
from app.services.deals_client import DealsError
from app.services.email_html import sanitize, to_text
from app.services.templates import FIXED_FIELDS, add_footer, compose, lead_fields, variables_used

log = logging.getLogger(__name__)
router = APIRouter(prefix="/campaigns", tags=["campaigns"])

AUDIT_FIELDS = [
    "name",
    "deals_service_key",
    "status",
    "subject",
    "body",
    "body_format",
    "use_lead_timezone",
    "follow_ups",
    "follow_ups_enabled",
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
    "follow_ups",
    "follow_ups_enabled",
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
# Best practice (Smartlead and others): never less than 3 minutes between two emails from one mailbox.
MIN_GAP_SECONDS = 180


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


class FollowUpStep(BaseModel):
    """One follow-up: plain text, sent delay_days after the previous email, as a reply in the same thread."""

    delay_days: int = Field(ge=0, le=60)
    body: str = Field(max_length=20_000)

    @field_validator("delay_days")
    @classmethod
    def at_least_a_day(cls, v: int) -> int:
        # 0 ("right after the previous email") only exists to try follow-ups out on a test copy.
        if v < 1 and not get_settings().is_test_copy:
            raise ValueError("wait at least 1 day")
        return v


FollowUps = Annotated[list[FollowUpStep], Field(max_length=sending.MAX_FOLLOW_UPS)]


class FollowUpStepOut(BaseModel):
    delay_days: int
    body: str


class CampaignIn(Schedule):
    name: str = Field(min_length=1, max_length=200)
    subject: str = Field(default="", max_length=998)
    body: str = Field(default="", max_length=100_000)
    body_format: BodyFormat = BodyFormat.TEXT
    follow_ups: FollowUps = Field(default_factory=list)
    # Off: the steps are kept but nothing is sent after the first email.
    follow_ups_enabled: bool = True
    group_ids: list[int] = Field(default_factory=list, max_length=100)
    mailbox_ids: list[int] = Field(default_factory=list, max_length=100)
    # Deals' service for leads from this campaign (a key from Deals' service list), sent at handover.
    deals_service_key: str | None = Field(default=None, max_length=50)


class CampaignUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    subject: str | None = Field(default=None, max_length=998)
    body: str | None = Field(default=None, max_length=100_000)
    body_format: BodyFormat | None = None
    follow_ups: FollowUps | None = None
    follow_ups_enabled: bool | None = None
    group_ids: list[int] | None = Field(default=None, max_length=100)
    mailbox_ids: list[int] | None = Field(default=None, max_length=100)
    use_lead_timezone: bool | None = None
    gap_min_seconds: int | None = None
    gap_max_seconds: int | None = None
    timezone: str | None = None
    send_days: list[int] | None = None
    start_hour: int | None = None
    end_hour: int | None = None
    # Any time, like the name: it only matters when a reply is handed to Deals. "" clears it.
    deals_service_key: str | None = Field(default=None, max_length=50)


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
    # Replies are only noticed automatically for mailboxes with an incoming mail (IMAP) server.
    checks_replies: bool


class Counts(BaseModel):
    total: int = 0
    waiting: int = 0
    # Being handed to the mail server right now (seconds), or cut off by a crash until recovered.
    sending: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    cancelled: int = 0
    # On Deals' do-not-email list: never emailed. Left out of the answer while 0 (it only happens with the
    # Deals check switched on), so nothing changes for anyone who doesn't use it.
    blocked_by_deals: int = Field(default=0, exclude_if=lambda n: n == 0)
    # Replied with interest and handed to Deals (their opening email was sent). Left out while 0.
    handed_to_deals: int = Field(default=0, exclude_if=lambda n: n == 0)
    # Follow-ups and answers.
    follow_ups_sent: int = 0
    # People with a follow-up still to come.
    follow_ups_planned: int = 0
    replied: int = 0
    opted_out: int = 0
    bounced: int = 0
    # From cPanel's delivery log and bounce emails (app.services.delivery).
    delivered: int = 0
    hard_bounced: int = 0


class CompanyHold(BaseModel):
    """Waiting people at a company that already got its emails for the day (pacing.COMPANY_DAILY_LIMIT)."""

    domain: str
    waiting: int
    limit: int
    free_at: datetime


class CampaignOut(BaseModel):
    id: int
    name: str
    status: CampaignStatus
    subject: str
    body: str
    body_format: BodyFormat
    follow_ups: list[FollowUpStepOut]
    follow_ups_enabled: bool
    groups: list[GroupRef]
    mailboxes: list[MailboxRef]
    gap_min_seconds: int
    gap_max_seconds: int
    timezone: str
    send_days: list[int]
    start_hour: int
    end_hour: int
    use_lead_timezone: bool
    deals_service_key: str | None = None
    # Leads in the chosen groups that were unticked before starting.
    excluded_count: int
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    counts: Counts
    # Only on a running or paused campaign's own page, and left out while empty.
    company_holds: list[CompanyHold] = Field(default_factory=list, exclude_if=lambda v: not v)


class FollowUpOut(BaseModel):
    step: int
    status: FollowUpStatus
    sent_at: datetime | None
    subject_sent: str | None
    body_sent: str | None
    smtp_response: str | None
    error: str | None
    delivery_status: str | None = None


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
    follow_ups_sent: int
    next_follow_up_at: datetime | None
    replied_at: datetime | None
    # Why follow-ups stopped early: replied, opted_out, bounced, do_not_contact, failed, cancelled, forgotten.
    follow_up_stop: str | None
    follow_ups: list[FollowUpOut]
    # Their latest reply on the Replies page, if one was found in the inbox.
    last_reply_id: int | None = None
    # What happened after it left: DELIVERED, DEFERRED, SOFT_BOUNCE, HARD_BOUNCE, REJECTED (None: only sent).
    delivery_status: str | None = None
    # The mail-server log for this person (sends, cPanel lines, bounces), oldest first.
    delivery: list[DeliveryEventOut] = []
    # Handed to Deals (integration steps 4-5): the lead's link, its stage there and the Won value.
    deals: "RecipientDealsOut | None" = None


class RecipientDealsOut(BaseModel):
    status: str
    lead_url: str | None
    stage: str | None
    value_usd: int | None


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
    # Distinct leads in the chosen groups, and how many have no known timezone (they'd use the campaign's).
    lead_count: int = 0
    leads_without_timezone: int = 0


class SendTestIn(BaseModel):
    to: EmailStr
    mailbox_id: int | None = None
    # Also send each follow-up straight away, as replies in the same thread.
    include_follow_ups: bool = False


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
    for cid, replied, opted_out, bounced, planned, delivered, hard in db.execute(
        select(
            CampaignRecipient.campaign_id,
            func.count().filter(CampaignRecipient.replied_at.is_not(None)),
            func.count().filter(CampaignRecipient.follow_up_stop == "opted_out"),
            func.count().filter(CampaignRecipient.follow_up_stop == "bounced"),
            func.count().filter(CampaignRecipient.next_follow_up_at.is_not(None)),
            func.count().filter(CampaignRecipient.delivery_status == DeliveryStatus.DELIVERED.value),
            func.count().filter(CampaignRecipient.delivery_status == DeliveryStatus.HARD_BOUNCE.value),
        )
        .where(CampaignRecipient.campaign_id.in_(campaign_ids))
        .group_by(CampaignRecipient.campaign_id)
    ):
        counts = result[cid]
        counts.replied, counts.opted_out, counts.bounced, counts.follow_ups_planned = (
            replied,
            opted_out,
            bounced,
            planned,
        )
        counts.delivered, counts.hard_bounced = delivered, hard
    for cid, n in db.execute(
        select(FollowUpEmail.campaign_id, func.count())
        .where(FollowUpEmail.campaign_id.in_(campaign_ids), FollowUpEmail.status == FollowUpStatus.SENT)
        .group_by(FollowUpEmail.campaign_id)
    ):
        result[cid].follow_ups_sent = n
    return result


def _excluded(db: DB, campaign_ids: list[int]) -> dict[int, int]:
    rows = db.execute(
        select(campaign_exclusions.c.campaign_id, func.count())
        .where(campaign_exclusions.c.campaign_id.in_(campaign_ids))
        .group_by(campaign_exclusions.c.campaign_id)
    )
    return {cid: n for cid, n in rows}


def _full(db: DB, c: Campaign) -> "CampaignOut":
    out = _out(c, _counts(db, [c.id])[c.id], _excluded(db, [c.id]).get(c.id, 0))
    if c.status in (CampaignStatus.RUNNING, CampaignStatus.PAUSED) and out.counts.waiting:
        out.company_holds = [
            CompanyHold(domain=d, waiting=n, limit=pacing.COMPANY_DAILY_LIMIT, free_at=free)
            for d, n, free in pacing.company_holds(db, c.id, datetime.now(UTC))
        ]
    return out


def _out(c: Campaign, counts: Counts, excluded: int = 0) -> CampaignOut:
    return CampaignOut(
        id=c.id,
        name=c.name,
        status=c.status,
        subject=c.subject,
        body=c.body,
        body_format=c.body_format,
        follow_ups=[FollowUpStepOut(**f) for f in c.follow_ups or []],
        follow_ups_enabled=c.follow_ups_enabled,
        deals_service_key=c.deals_service_key,
        groups=[GroupRef(id=g.id, name=g.name) for g in c.groups],
        mailboxes=[
            MailboxRef(
                id=m.id,
                email=m.email,
                display_name=m.display_name,
                status=m.status,
                next_send_at=m.next_send_at,
                daily_cap=m.daily_cap,
                checks_replies=bool(m.imap_host),
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


def _follow_ups(steps: list[FollowUpStep]) -> list[dict[str, Any]]:
    return [{"delay_days": f.delay_days, "body": f.body} for f in steps]


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


def _timezone_counts(db: DB, group_ids: list[int]) -> tuple[int, int]:
    if not group_ids:
        return 0, 0
    members = select(lead_group_members.c.lead_id).where(lead_group_members.c.group_id.in_(group_ids))
    total, unknown = db.execute(
        select(func.count(), func.count().filter(Lead.timezone.is_(None))).where(Lead.id.in_(members))
    ).one()
    return total, unknown


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
    # Groups and mailboxes for all campaigns in two queries, not two per campaign.
    campaigns = db.scalars(
        select(Campaign)
        .options(selectinload(Campaign.groups), selectinload(Campaign.mailboxes))
        .order_by(Campaign.created_at.desc())
    ).all()
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
        follow_ups=_follow_ups(body.follow_ups),
        follow_ups_enabled=body.follow_ups_enabled,
        use_lead_timezone=body.use_lead_timezone,
        gap_min_seconds=body.gap_min_seconds,
        gap_max_seconds=body.gap_max_seconds,
        timezone=body.timezone,
        send_days=body.send_days,
        start_hour=body.start_hour,
        end_hour=body.end_hour,
        created_by_id=user.id,
        deals_service_key=(body.deals_service_key or "").strip() or None,
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
    footer = email_footer(db)
    for _ in range(3):
        subject, text, html = compose(body.subject, body.body, fields, sender_name, rng, body.body_format)
        text, html = add_footer(text, html, footer)
        versions.append(PreviewVersion(subject=subject, body=text, html=html))
    available = _available_fields(db, body.group_ids)
    unknown = sorted(variables_used(body.subject, body.body) - set(available))
    lead_count, without_timezone = _timezone_counts(db, body.group_ids)
    return PreviewOut(
        lead_email=lead.email if lead else None,
        versions=versions,
        available_fields=available,
        unknown_variables=unknown,
        lead_count=lead_count,
        leads_without_timezone=without_timezone,
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
    changed = set(sent) - {"name", "deals_service_key"}
    if campaign.status == CampaignStatus.PAUSED and not changed <= PAUSED_EDITABLE:
        raise HTTPException(status.HTTP_409_CONFLICT, "campaign_not_editable")
    if campaign.status not in (CampaignStatus.DRAFT, CampaignStatus.PAUSED) and changed:
        raise HTTPException(status.HTTP_409_CONFLICT, "campaign_not_editable")

    try:
        schedule = Schedule.model_validate(
            {f: sent.get(f, getattr(campaign, f)) for f in Schedule.model_fields}
        )
    except ValidationError as e:
        # A bad value (a gap under 3 minutes, an unknown timezone…) is the caller's mistake: 422, not 500.
        raise RequestValidationError(e.errors(include_url=False, include_context=False)) from e
    if schedule.gap_max_seconds < schedule.gap_min_seconds:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_gap")
    if schedule.end_hour <= schedule.start_hour:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_hours")

    before = _snapshot(campaign)
    for field, value in schedule.model_dump().items():
        setattr(campaign, field, value)
    if body.name is not None:
        campaign.name = body.name.strip()
    if "deals_service_key" in sent:
        campaign.deals_service_key = (body.deals_service_key or "").strip() or None
    if body.subject is not None:
        campaign.subject = body.subject
    if body.body_format is not None:
        campaign.body_format = body.body_format
    if body.body is not None:
        campaign.body = sanitize(body.body) if campaign.body_format == BodyFormat.HTML else body.body
    follow_ups_changed = False
    if body.follow_ups is not None:
        new_steps = _follow_ups(body.follow_ups)
        follow_ups_changed = new_steps != (campaign.follow_ups or [])
        campaign.follow_ups = new_steps
    if body.follow_ups_enabled is not None and body.follow_ups_enabled != campaign.follow_ups_enabled:
        follow_ups_changed = True
        campaign.follow_ups_enabled = body.follow_ups_enabled
    if body.group_ids is not None:
        campaign.groups = _load(db, LeadGroup, body.group_ids, "group_not_found")
    if body.mailbox_ids is not None:
        campaign.mailboxes = _usable_mailboxes(db, body.mailbox_ids, already=campaign.mailboxes)
    if follow_ups_changed and campaign.status == CampaignStatus.PAUSED:
        # People already emailed get the new steps, timed from the last email they got.
        sending.reschedule_follow_ups(db, campaign)
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
    if campaign.follow_ups_enabled and any(
        not str(f.get("body", "")).strip() for f in campaign.follow_ups or []
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "follow_up_empty")
    if not campaign.groups:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no_groups")
    if not any(m.status == MailboxStatus.ACTIVE for m in campaign.mailboxes):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no_active_mailbox")
    if any(mailer.development_block(m) for m in campaign.mailboxes):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "dev_real_mailbox")
    footer = email_footer(db)
    if footer is not None and not footer.postal_address:
        # Every email carries the postal address once the opt-out footer is on (CAN-SPAM).
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "postal_address_required")
    at = sending.now()
    check = None
    if deals.blocklist_enabled(db):
        # Check 2: Deals itself is asked about every recipient. If it can't be asked, nobody is emailed.
        def check(hashes: list[str]) -> dict[str, tuple[str, datetime | None]]:
            return deals.check_for_start(db, hashes, at)

    try:
        sending.start(db, campaign, actor, at, deals_check=check)
    except DealsError as e:
        db.rollback()
        log.warning("campaign %s: Deals check failed: %s", campaign_id, e)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "deals_check_failed") from e
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
        sending.stop_campaign_follow_ups(db, campaign.id, "cancelled")
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
    if to == CampaignStatus.RUNNING:
        # Nothing may be left (e.g. follow-ups were switched off while paused): then it's finished.
        sending.finish_if_done(db, campaign.id, sending.now())
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
    fields = {**business_fields(db), **(lead_fields(lead) if lead else {})}
    rng = random.Random()  # noqa: S311
    subject, text, html = compose(
        campaign.subject, campaign.body, fields, mailbox.display_name, rng, campaign.body_format
    )
    subject = f"[Test] {subject}"
    footer = email_footer(db)
    text, html = add_footer(text, html, footer)
    unsubscribe = footer is not None
    messages = [
        mailer.build_message(mailbox, str(body.to), subject, text, html, list_unsubscribe=unsubscribe)
    ]
    if body.include_follow_ups and campaign.follow_ups_enabled:
        for step in campaign.follow_ups or []:
            _, follow_text, _ = compose("", step["body"], fields, mailbox.display_name, rng)
            follow_text, _ = add_footer(follow_text, None, footer)
            thread = [m["Message-ID"] for m in messages]
            messages.append(
                mailer.build_message(
                    mailbox,
                    str(body.to),
                    sending.reply_subject(subject),
                    follow_text,
                    thread=thread,
                    list_unsubscribe=unsubscribe,
                )
            )
    try:
        for msg in messages:
            sent = mailer.send(mailbox, msg)
        what = "the email" if len(messages) == 1 else f"the email and {len(messages) - 1} follow-up(s)"
        detail = f"Sent {what} from {mailbox.email}. Server reply: {sent.response}"
        result = SendTestOut(ok=True, detail=detail)
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
        after={
            "to": str(body.to),
            "mailbox": mailbox.email,
            "follow_ups": body.include_follow_ups,
            "ok": result.ok,
            "detail": result.detail,
        },
    )
    db.commit()
    return result


FORGOTTEN_LABEL = "Forgotten person"


def _recipients_query(
    campaign_id: int, status_filter: RecipientStatus | None, q: str | None, replied: bool | None = None
) -> Select:
    query = (
        select(CampaignRecipient, Lead, Mailbox.email)
        .outerjoin(Lead, Lead.id == CampaignRecipient.lead_id)
        .outerjoin(Mailbox, Mailbox.id == CampaignRecipient.mailbox_id)
        .where(CampaignRecipient.campaign_id == campaign_id)
    )
    if status_filter:
        query = query.where(CampaignRecipient.status == status_filter)
    if replied is not None:
        query = query.where(
            CampaignRecipient.replied_at.is_not(None) if replied else CampaignRecipient.replied_at.is_(None)
        )
    if q:
        like = f"%{q.strip()}%"
        query = query.where(
            or_(CampaignRecipient.email.ilike(like), Lead.first_name.ilike(like), Lead.company.ilike(like))
        )
    return query


def _follow_ups_of(db: DB, recipient_ids: list[int]) -> dict[int, list[FollowUpOut]]:
    result: dict[int, list[FollowUpOut]] = {rid: [] for rid in recipient_ids}
    for f in db.scalars(
        select(FollowUpEmail)
        .where(FollowUpEmail.recipient_id.in_(recipient_ids))
        .order_by(FollowUpEmail.recipient_id, FollowUpEmail.step)
    ):
        result[f.recipient_id].append(
            FollowUpOut(
                step=f.step,
                status=f.status,
                sent_at=f.sent_at,
                subject_sent=f.subject_sent,
                body_sent=f.body_sent,
                smtp_response=f.smtp_response,
                error=f.error,
                delivery_status=f.delivery_status,
            )
        )
    return result


def _delivery_of(db: DB, recipient_ids: list[int]) -> dict[int, list[DeliveryEventOut]]:
    result: dict[int, list[DeliveryEventOut]] = {rid: [] for rid in recipient_ids}
    for e in db.scalars(
        select(DeliveryEvent)
        .where(DeliveryEvent.recipient_id.in_(recipient_ids))
        .order_by(DeliveryEvent.occurred_at, DeliveryEvent.id)
    ):
        result[e.recipient_id].append(DeliveryEventOut.model_validate(e))
    return result


def _handovers_of(db: DB, recipient_ids: list[int]) -> dict[int, "RecipientDealsOut"]:
    """The newest handover per person (a repeat reply adds one), with Deals' result for the lead."""
    found: dict[int, RecipientDealsOut] = {}
    rows = db.execute(
        select(DealsHandover, DealsLeadResult)
        .outerjoin(DealsLeadResult, DealsLeadResult.deals_lead_id == DealsHandover.deals_lead_id)
        .where(DealsHandover.recipient_id.in_(recipient_ids), DealsHandover.band != "repeat")
        .order_by(DealsHandover.id)
    )
    for h, result in rows:
        found[h.recipient_id] = RecipientDealsOut(
            status=h.status,
            lead_url=h.deals_lead_url,
            stage=result.stage if result else None,
            value_usd=result.value_usd if result else None,
        )
    return found


def _last_replies(db: DB, recipient_ids: list[int]) -> dict[int, int]:
    return dict(
        db.execute(
            select(Reply.recipient_id, func.max(Reply.id))
            .where(Reply.recipient_id.in_(recipient_ids))
            .group_by(Reply.recipient_id)
        ).all()
    )


def _recipient_out(
    r: CampaignRecipient,
    lead: Lead | None,
    mailbox_email: str | None,
    follow_ups: list[FollowUpOut] | None = None,
    last_reply_id: int | None = None,
    delivery: list[DeliveryEventOut] | None = None,
    deals_info: "RecipientDealsOut | None" = None,
) -> RecipientOut:
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
        follow_ups_sent=r.follow_ups_sent,
        next_follow_up_at=r.next_follow_up_at,
        replied_at=r.replied_at,
        follow_up_stop=r.follow_up_stop,
        follow_ups=follow_ups or [],
        last_reply_id=last_reply_id,
        delivery_status=r.delivery_status,
        delivery=delivery or [],
        deals=deals_info,
    )


StatusFilter = Annotated[RecipientStatus | None, Query(alias="status")]
RepliedFilter = Annotated[bool | None, Query(description="Only people who replied (true) or didn't (false)")]


@router.get("/{campaign_id}/recipients", response_model=Page[RecipientOut])
def list_recipients(
    campaign_id: int,
    db: DB,
    _: CurrentUser,
    status_filter: StatusFilter = None,
    replied: RepliedFilter = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[RecipientOut]:
    _get(db, campaign_id)
    query = _recipients_query(campaign_id, status_filter, q, replied)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(query.order_by(CampaignRecipient.id).limit(limit).offset(offset)).all()
    ids = [r.id for r, _, _ in rows]
    follow_ups, last_replies, events = _follow_ups_of(db, ids), _last_replies(db, ids), _delivery_of(db, ids)
    handovers = _handovers_of(db, ids)
    return Page(
        items=[
            _recipient_out(
                r, lead, m, follow_ups[r.id], last_replies.get(r.id), events[r.id], handovers.get(r.id)
            )
            for r, lead, m in rows
        ],
        total=total,
    )


@router.post("/{campaign_id}/recipients/{recipient_id}/replied", response_model=RecipientOut)
def mark_replied(
    campaign_id: int, recipient_id: int, db: DB, _: EditorUser, actor: CurrentActor
) -> RecipientOut:
    """Record a reply by hand (it came another way, or the mailbox has no IMAP). Stops their follow-ups."""
    recipient = db.get(CampaignRecipient, recipient_id, with_for_update=True)
    if recipient is None or recipient.campaign_id != campaign_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "recipient_not_found")
    if recipient.status not in EMAILED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "not_sent")
    if recipient.replied_at is None:
        replies.mark_replied(db, recipient, actor, sending.now())
        db.commit()
    query = _recipients_query(campaign_id, None, None).where(CampaignRecipient.id == recipient.id)
    row = db.execute(query).one()
    follow_ups, last_reply = _follow_ups_of(db, [recipient.id]), _last_replies(db, [recipient.id])
    events = _delivery_of(db, [recipient.id])
    return _recipient_out(*row, follow_ups[recipient.id], last_reply.get(recipient.id), events[recipient.id])


@router.get("/{campaign_id}/recipients/export")
def export_recipients(
    campaign_id: int,
    db: DB,
    _: CurrentUser,
    actor: CurrentActor,
    status_filter: StatusFilter = None,
    replied: RepliedFilter = None,
    q: str | None = None,
) -> StreamingResponse:
    """The per-lead report as CSV (the current filters apply)."""
    campaign = _get(db, campaign_id)
    rows = [
        _recipient_out(*row)
        for row in db.execute(
            _recipients_query(campaign_id, status_filter, q, replied).order_by(CampaignRecipient.id)
        )
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
            "follow_ups_sent",
            "next_follow_up_at",
            "replied_at",
            "follow_ups_stopped",
            "delivery",
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
                r.follow_ups_sent,
                r.next_follow_up_at.isoformat() if r.next_follow_up_at else None,
                r.replied_at.isoformat() if r.replied_at else None,
                r.follow_up_stop,
                r.delivery_status,
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
