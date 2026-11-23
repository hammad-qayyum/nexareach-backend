"""The 10-minute deliverability check and the per-mailbox numbers for the Deliverability page.

The worker runs run() every 10 minutes: cPanel's delivery log is read (app.services.cpanel), then each
mailbox's recent emails are looked at for too many hard bounces (pauses its campaign emails) or delays, and
failing mailbox or helper logins raise alerts. Health checks, DMARC and placement tests have their own
schedules and alerts.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, union_all
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models import (
    CampaignRecipient,
    DeliveryStatus,
    FollowUpEmail,
    FollowUpStatus,
    Mailbox,
    MailboxStatus,
    RecipientStatus,
    WarmupHelper,
)
from app.services import alerts, cpanel

NOT_TESTED = "Connection not tested yet."
# Fewer emails than this in 24 hours: a few delays are normal, don't alert.
MIN_FOR_DEFERRED = 10


@dataclass
class BounceRate:
    hard: int
    emails: int
    window: int

    @property
    def rate(self) -> float:
        """Hard bounces over the window (or over the emails sent, once there are more): 4 bounces in the first
        20 emails already counts as 4%, not 20%, so one unlucky address doesn't pause a new mailbox."""
        return self.hard / max(self.emails, self.window) if self.emails else 0.0


def bounce_rate(db: Session, mailbox: Mailbox, window: int) -> BounceRate:
    """Hard bounces among the mailbox's last `window` opening emails (since sending was last resumed)."""
    query = select(CampaignRecipient.delivery_status).where(
        CampaignRecipient.mailbox_id == mailbox.id, CampaignRecipient.status == RecipientStatus.SENT
    )
    if mailbox.campaigns_resumed_at is not None:
        query = query.where(CampaignRecipient.sent_at > mailbox.campaigns_resumed_at)
    statuses = db.scalars(query.order_by(CampaignRecipient.sent_at.desc()).limit(window)).all()
    hard = sum(1 for s in statuses if s == DeliveryStatus.HARD_BOUNCE)
    return BounceRate(hard=hard, emails=len(statuses), window=window)


def _messages(since: datetime, mailbox_ids: list[int] | None = None):
    """(mailbox_id, delivery_status) of every opening email and follow-up sent since then."""
    first = select(CampaignRecipient.mailbox_id, CampaignRecipient.delivery_status).where(
        CampaignRecipient.status == RecipientStatus.SENT, CampaignRecipient.sent_at >= since
    )
    follow = select(FollowUpEmail.mailbox_id, FollowUpEmail.delivery_status).where(
        FollowUpEmail.status == FollowUpStatus.SENT, FollowUpEmail.sent_at >= since
    )
    if mailbox_ids is not None:
        first = first.where(CampaignRecipient.mailbox_id.in_(mailbox_ids))
        follow = follow.where(FollowUpEmail.mailbox_id.in_(mailbox_ids))
    return union_all(first, follow).subquery()


def status_counts(db: Session, since: datetime) -> dict[int, dict[str, int]]:
    """{mailbox_id: {"sent": n, "DELIVERED": n, "DEFERRED": n, ...}} for emails sent since then."""
    messages = _messages(since)
    out: dict[int, dict[str, int]] = {}
    for mailbox_id, status, n in db.execute(
        select(messages.c.mailbox_id, messages.c.delivery_status, func.count()).group_by(
            messages.c.mailbox_id, messages.c.delivery_status
        )
    ):
        counts = out.setdefault(mailbox_id, {"sent": 0})
        counts["sent"] += n
        if status:
            counts[status] = counts.get(status, 0) + n
    return out


def evaluate(db: Session, at: datetime) -> list:
    """Update the bounce, delay and login alerts. The caller commits and emails the new ones."""
    limits = alerts.thresholds(db)
    mailboxes = db.scalars(select(Mailbox).where(Mailbox.archived_at.is_(None)).order_by(Mailbox.id)).all()

    bounces, delayed, logins = [], [], []
    day = status_counts(db, at - timedelta(hours=24))
    for m in mailboxes:
        rate = bounce_rate(db, m, limits.bounce_window)
        if rate.hard and rate.rate * 100 > limits.hard_bounce_pct:
            bounces.append(
                alerts.Condition(
                    f"{m.email}: {rate.hard} hard bounces (addresses that don't exist) in its last "
                    f"{rate.emails} emails, over the {limits.hard_bounce_pct:g}% limit. Clean the lead list "
                    "before resuming.",
                    mailbox_id=m.id,
                    pause=(m.id,),
                )
            )
        counts = day.get(m.id, {})
        sent, deferred = counts.get("sent", 0), counts.get(DeliveryStatus.DEFERRED.value, 0)
        if sent >= MIN_FOR_DEFERRED and deferred / sent * 100 > limits.deferred_pct:
            delayed.append(
                alerts.Condition(
                    f"{m.email}: {deferred} of {sent} emails in the last 24 hours are still delayed (the "
                    "receiving servers asked to try later). Look at their answers in the server log.",
                    mailbox_id=m.id,
                )
            )
        if m.status == MailboxStatus.ERROR and (m.last_error or "") != NOT_TESTED:
            logins.append(
                alerts.Condition(
                    f"{m.email} can't sign in to its mail server, so it sends nothing: "
                    f"{m.last_error or 'unknown error'}",
                    mailbox_id=m.id,
                )
            )
    helpers = [
        alerts.Condition(
            f"Warm-up helper {h.email} can't be used: {h.last_error or 'its login failed'}. "
            "Fix it on the Warm-up page.",
            target=h.email,
        )
        for h in db.scalars(select(WarmupHelper).where(WarmupHelper.status == MailboxStatus.ERROR))
    ]
    new = alerts.sync(db, "hard_bounces", bounces, at)
    new += alerts.sync(db, "deferred", delayed, at)
    new += alerts.sync(db, "login_failing", logins, at)
    new += alerts.sync(db, "helper_broken", helpers, at)
    return new


def run() -> None:
    """Worker entry point (every 10 minutes)."""
    at = datetime.now(UTC)
    with SessionLocal() as db:
        cpanel.sync(db, at)
    with SessionLocal() as db:
        new = evaluate(db, at)
        db.commit()
        alerts.email_admins(db, new, at)
