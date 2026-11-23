"""Numbers for the Reports page: emails per day, per mailbox and per campaign."""

from datetime import date, datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo, available_timezones

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import String, cast, func, literal, or_, select, union_all

from app.api.deps import DB, CurrentUser
from app.models import (
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    FollowUpEmail,
    FollowUpStatus,
    Mailbox,
    RecipientStatus,
)
from app.services.sending import now

router = APIRouter(prefix="/reports", tags=["reports"])


class DayCount(BaseModel):
    date: date
    sent: int
    failed: int


class MailboxCount(BaseModel):
    id: int
    email: str
    sent: int
    failed: int


class CampaignCount(BaseModel):
    id: int
    name: str
    status: CampaignStatus
    # In the chosen period.
    sent: int
    failed: int
    # Whole campaign, so far.
    total: int
    done: int


class Totals(BaseModel):
    # Emails, opening emails and follow-ups together.
    sent: int
    failed: int
    skipped: int
    people: int
    follow_ups_sent: int
    replied: int


class SummaryOut(BaseModel):
    days: list[DayCount]
    mailboxes: list[MailboxCount]
    campaigns: list[CampaignCount]
    totals: Totals


@router.get("/summary", response_model=SummaryOut)
def summary(
    db: DB,
    _: CurrentUser,
    days: Annotated[int, Query(ge=7, le=180)] = 30,
    # Days are cut in this timezone (the browser's).
    tz: str = "UTC",
) -> SummaryOut:
    if tz not in available_timezones():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_timezone")
    zone = ZoneInfo(tz)
    today = now().astimezone(zone).date()
    first = today - timedelta(days=days - 1)
    start = datetime.combine(first, time(0), zone)

    # Every email, opening email or follow-up. A sent one counts on the day it was sent; a failed one on
    # the day it was given up on.
    first_emails = select(
        CampaignRecipient.campaign_id.label("campaign_id"),
        CampaignRecipient.mailbox_id.label("mailbox_id"),
        CampaignRecipient.email.label("email"),
        cast(CampaignRecipient.status, String).label("outcome"),
        func.coalesce(CampaignRecipient.sent_at, CampaignRecipient.updated_at).label("at"),
        literal(False).label("follow_up"),
    ).where(CampaignRecipient.status.in_([RecipientStatus.SENT, RecipientStatus.FAILED]))
    follow_ups = (
        select(
            FollowUpEmail.campaign_id,
            FollowUpEmail.mailbox_id,
            CampaignRecipient.email,
            cast(FollowUpEmail.status, String),
            func.coalesce(FollowUpEmail.sent_at, FollowUpEmail.updated_at),
            literal(True),
        )
        .join(CampaignRecipient, CampaignRecipient.id == FollowUpEmail.recipient_id)
        .where(FollowUpEmail.status.in_([FollowUpStatus.SENT, FollowUpStatus.FAILED]))
    )
    emails = union_all(first_emails, follow_ups).subquery()
    outcome = [emails.c.at >= start]
    sent = func.count().filter(emails.c.outcome == "SENT")
    failed = func.count().filter(emails.c.outcome == "FAILED")

    day = func.date(func.timezone(tz, emails.c.at))
    per_day = {d: (s, f) for d, s, f in db.execute(select(day, sent, failed).where(*outcome).group_by(day))}
    day_list = [
        DayCount(date=d, sent=per_day.get(d, (0, 0))[0], failed=per_day.get(d, (0, 0))[1])
        for d in (first + timedelta(days=i) for i in range(days))
    ]

    mailboxes = [
        MailboxCount(id=mid, email=email, sent=s, failed=f)
        for mid, email, s, f in db.execute(
            select(Mailbox.id, Mailbox.email, sent, failed)
            .join(emails, emails.c.mailbox_id == Mailbox.id)
            .where(*outcome)
            .group_by(Mailbox.id)
            .order_by(sent.desc())
        )
    ]

    in_period = dict(
        (
            (cid, (s, f))
            for cid, s, f in db.execute(
                select(emails.c.campaign_id, sent, failed).where(*outcome).group_by(emails.c.campaign_id)
            )
        )
    )
    progress = {
        cid: (total, done)
        for cid, total, done in db.execute(
            select(
                CampaignRecipient.campaign_id,
                func.count(),
                func.count().filter(
                    CampaignRecipient.status.not_in([RecipientStatus.WAITING, RecipientStatus.SENDING])
                ),
            ).group_by(CampaignRecipient.campaign_id)
        )
    }
    campaigns = db.scalars(
        select(Campaign)
        .where(
            or_(
                Campaign.id.in_(list(in_period)),
                Campaign.status.in_([CampaignStatus.RUNNING, CampaignStatus.PAUSED]),
            )
        )
        .order_by(Campaign.started_at.desc().nulls_last(), Campaign.id.desc())
    ).all()

    totals = db.execute(
        select(
            sent,
            failed,
            func.count(func.distinct(emails.c.email)).filter(emails.c.outcome == "SENT"),
            func.count().filter(emails.c.outcome == "SENT", emails.c.follow_up),
        ).where(*outcome)
    ).one()
    replied = db.scalar(select(func.count()).where(CampaignRecipient.replied_at >= start))
    skipped = db.scalar(
        select(func.count()).where(
            CampaignRecipient.status == RecipientStatus.SKIPPED, CampaignRecipient.created_at >= start
        )
    )

    return SummaryOut(
        days=day_list,
        mailboxes=mailboxes,
        campaigns=[
            CampaignCount(
                id=c.id,
                name=c.name,
                status=c.status,
                sent=in_period.get(c.id, (0, 0))[0],
                failed=in_period.get(c.id, (0, 0))[1],
                total=progress.get(c.id, (0, 0))[0],
                done=progress.get(c.id, (0, 0))[1],
            )
            for c in campaigns
        ],
        totals=Totals(
            sent=totals[0],
            failed=totals[1],
            people=totals[2],
            skipped=skipped,
            follow_ups_sent=totals[3],
            replied=replied,
        ),
    )
