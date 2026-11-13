"""Starting campaigns and the sending engine (plan section 16, cut down: no warm-up, IMAP or bounces yet).

The worker calls run_tick() every minute. For each active mailbox that is due and under its daily cap,
send_next() sends one email from the running campaign whose turn it is, then sets the mailbox's next
free time to now + a random gap. Each mailbox row is locked while it sends, so it never sends twice
at once, even with several workers.
"""

import logging
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models import (
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    DncKind,
    DoNotContact,
    Lead,
    Mailbox,
    MailboxStatus,
    RecipientStatus,
    campaign_exclusions,
    campaign_mailboxes,
    lead_group_members,
)
from app.services import mailer, privacy
from app.services.app_settings import business_fields
from app.services.audit import SYSTEM, Actor, log_activity
from app.services.templates import compose, lead_fields

log = logging.getLogger(__name__)

# Nobody is emailed twice within this many days, by any campaign.
OVERLAP_DAYS = 30
RETRY_DELAY = timedelta(minutes=15)
MAX_RETRIES = 3
# A send takes seconds (30 s timeouts); anything SENDING for longer was cut off by a crash or restart.
INTERRUPTED_AFTER = timedelta(minutes=10)

Deliver = Callable[[Mailbox, EmailMessage], mailer.Sent]


def now() -> datetime:
    return datetime.now(UTC)


def in_window(campaign: Campaign, at: datetime) -> bool:
    return in_window_at(campaign, campaign.timezone, at)


def sent_today(db: Session, mailbox_id: int, at: datetime) -> int:
    # Days are counted in UTC.
    day_start = at.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return db.scalar(
        select(func.count())
        .select_from(CampaignRecipient)
        .where(
            CampaignRecipient.mailbox_id == mailbox_id,
            CampaignRecipient.status == RecipientStatus.SENT,
            CampaignRecipient.sent_at >= day_start,
        )
    )


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[-1]


def blocked_reason(db: Session, email: str, campaign_id: int, at: datetime) -> str | None:
    """Why this address must not be emailed now, or None."""
    dnc = db.scalar(
        select(DoNotContact.id).where(
            or_(
                (DoNotContact.kind == DncKind.EMAIL)
                & (DoNotContact.value.in_([email, privacy.email_hash(email)])),
                (DoNotContact.kind == DncKind.DOMAIN) & (DoNotContact.value == _domain(email)),
            )
        )
    )
    if dnc is not None:
        return "do_not_contact"
    recent = db.scalar(
        select(
            exists().where(
                CampaignRecipient.email == email,
                CampaignRecipient.status == RecipientStatus.SENT,
                CampaignRecipient.sent_at >= at - timedelta(days=OVERLAP_DAYS),
                CampaignRecipient.campaign_id != campaign_id,
            )
        )
    )
    return "contacted_recently" if recent else None


def start(db: Session, campaign: Campaign, actor: Actor, at: datetime) -> dict[str, int]:
    """Fix the recipient list and set the campaign running. The caller has checked it can start."""
    group_ids = [g.id for g in campaign.groups]
    excluded = select(campaign_exclusions.c.lead_id).where(campaign_exclusions.c.campaign_id == campaign.id)
    leads = db.scalars(
        select(Lead)
        .join(lead_group_members, lead_group_members.c.lead_id == Lead.id)
        .where(lead_group_members.c.group_id.in_(group_ids), Lead.id.not_in(excluded))
        .order_by(Lead.id)
        .distinct()
    ).all()

    dnc_emails = set(db.scalars(select(DoNotContact.value).where(DoNotContact.kind == DncKind.EMAIL)))
    dnc_domains = set(db.scalars(select(DoNotContact.value).where(DoNotContact.kind == DncKind.DOMAIN)))
    recent = set(
        db.scalars(
            select(CampaignRecipient.email).where(
                CampaignRecipient.status == RecipientStatus.SENT,
                CampaignRecipient.sent_at >= at - timedelta(days=OVERLAP_DAYS),
            )
        )
    )

    # One recipient per lead. Two leads may share an email when someone chose "keep both" for a
    # duplicate; each copy gets its own email, as the plan specifies.
    counts = {"waiting": 0, "skipped": 0}
    for lead in leads:
        reason = None
        if (
            lead.email in dnc_emails
            or _domain(lead.email) in dnc_domains
            or privacy.email_hash(lead.email) in dnc_emails
        ):
            reason = "do_not_contact"
        elif lead.email in recent:
            reason = "contacted_recently"
        db.add(
            CampaignRecipient(
                campaign_id=campaign.id,
                lead_id=lead.id,
                email=lead.email,
                status=RecipientStatus.SKIPPED if reason else RecipientStatus.WAITING,
                skip_reason=reason,
            )
        )
        counts["skipped" if reason else "waiting"] += 1

    campaign.status = CampaignStatus.RUNNING
    campaign.started_at = at
    log_activity(
        db,
        actor,
        "campaign.started",
        entity_type="campaign",
        entity_id=campaign.id,
        before={"status": CampaignStatus.DRAFT},
        after={"status": CampaignStatus.RUNNING, **counts},
    )
    if counts["waiting"] == 0:
        _complete(db, campaign, at)
    return counts


def _complete(db: Session, campaign: Campaign, at: datetime) -> None:
    campaign.status = CampaignStatus.COMPLETED
    campaign.finished_at = at
    log_activity(
        db,
        SYSTEM,
        "campaign.completed",
        entity_type="campaign",
        entity_id=campaign.id,
        before={"status": CampaignStatus.RUNNING},
        after={"status": CampaignStatus.COMPLETED},
    )


def _finish_if_done(db: Session, campaign: Campaign, at: datetime) -> None:
    db.flush()
    waiting = db.scalar(
        select(
            exists().where(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.status.in_([RecipientStatus.WAITING, RecipientStatus.SENDING]),
            )
        )
    )
    if not waiting and campaign.status == CampaignStatus.RUNNING:
        _complete(db, campaign, at)


def in_window_at(campaign: Campaign, zone: str, at: datetime) -> bool:
    local = at.astimezone(ZoneInfo(zone))
    return local.weekday() in campaign.send_days and campaign.start_hour <= local.hour < campaign.end_hour


def _pick(db: Session, mailbox: Mailbox, at: datetime) -> tuple[CampaignRecipient, Campaign] | None:
    campaigns = db.scalars(
        select(Campaign)
        .join(campaign_mailboxes, campaign_mailboxes.c.campaign_id == Campaign.id)
        .where(campaign_mailboxes.c.mailbox_id == mailbox.id, Campaign.status == CampaignStatus.RUNNING)
        .order_by(Campaign.last_sent_at.asc().nulls_first(), Campaign.id)
    ).all()
    for campaign in campaigns:
        due = [
            CampaignRecipient.campaign_id == campaign.id,
            CampaignRecipient.status == RecipientStatus.WAITING,
            or_(CampaignRecipient.next_attempt_at.is_(None), CampaignRecipient.next_attempt_at <= at),
        ]
        query = select(CampaignRecipient)
        if campaign.use_lead_timezone:
            # Each lead's own local time decides; leads without a timezone use the campaign's.
            zone = func.coalesce(Lead.timezone, campaign.timezone)
            zones = db.scalars(
                select(zone)
                .distinct()
                .select_from(CampaignRecipient)
                .join(Lead, Lead.id == CampaignRecipient.lead_id)
                .where(*due)
            ).all()
            open_zones = [z for z in zones if in_window_at(campaign, z, at)]
            if not open_zones:
                continue
            query = query.join(Lead, Lead.id == CampaignRecipient.lead_id).where(zone.in_(open_zones))
        elif not in_window(campaign, at):
            continue
        recipient = db.scalar(
            query.where(*due)
            .order_by(CampaignRecipient.id)
            .limit(1)
            .with_for_update(of=CampaignRecipient, skip_locked=True)
        )
        if recipient is not None:
            return recipient, campaign
    return None


def send_next(
    db: Session,
    mailbox_id: int,
    at: datetime,
    deliver: Deliver = mailer.send,
    rng: random.Random | None = None,
) -> str:
    """Send at most one email from this mailbox. Returns what happened, for logs and tests."""
    rng = rng or random.Random()  # noqa: S311 - variety, not security
    mailbox = db.scalar(
        select(Mailbox)
        .where(
            Mailbox.id == mailbox_id,
            Mailbox.status == MailboxStatus.ACTIVE,
            or_(Mailbox.next_send_at.is_(None), Mailbox.next_send_at <= at),
        )
        .with_for_update(skip_locked=True)
    )
    if mailbox is None:
        return "not_due"
    if sent_today(db, mailbox.id, at) >= mailbox.daily_cap:
        return "cap_reached"
    picked = _pick(db, mailbox, at)
    if picked is None:
        return "idle"
    recipient, campaign = picked

    reason = blocked_reason(db, recipient.email, campaign.id, at)
    if reason:
        recipient.status = RecipientStatus.SKIPPED
        recipient.skip_reason = reason
        _finish_if_done(db, campaign, at)
        db.commit()
        return "skipped"

    lead = db.get(Lead, recipient.lead_id)
    subject, body, html = compose(
        campaign.subject,
        campaign.body,
        {**business_fields(db), **lead_fields(lead)},
        mailbox.display_name,
        rng,
        campaign.body_format,
    )
    msg = mailer.build_message(mailbox, recipient.email, subject, body, html)

    # Record the attempt and move the mailbox's next free time *before* talking to the mail server,
    # and commit. If the process dies mid-send, the row stays SENDING (never silently WAITING again,
    # which would email the person twice) and the mailbox keeps its pacing.
    recipient.status = RecipientStatus.SENDING
    recipient.send_started_at = at
    recipient.mailbox_id = mailbox.id
    recipient.attempts += 1
    recipient.subject_sent = subject
    recipient.body_sent = body
    recipient.html_sent = html
    campaign.last_sent_at = at
    mailbox.next_send_at = at + timedelta(
        seconds=rng.randint(campaign.gap_min_seconds, campaign.gap_max_seconds)
    )
    db.commit()

    try:
        sent = deliver(mailbox, msg)
    except mailer.AuthFailed as e:
        # The mailbox, not the recipient, is the problem: put the email back in the queue and stop
        # using the mailbox until someone fixes the login.
        recipient.status = RecipientStatus.WAITING
        recipient.send_started_at = None
        recipient.mailbox_id = None
        recipient.attempts -= 1
        mailbox.status = MailboxStatus.ERROR
        mailbox.last_error = str(e)
        log_activity(
            db,
            SYSTEM,
            "mailbox.login_failed",
            entity_type="mailbox",
            entity_id=mailbox.id,
            before={"status": MailboxStatus.ACTIVE},
            after={"status": MailboxStatus.ERROR, "error": str(e)},
        )
        db.commit()
        return "auth_failed"
    except mailer.SendFailed as e:
        recipient.error = str(e)
        if e.temporary and not e.uncertain and recipient.attempts <= MAX_RETRIES:
            recipient.status = RecipientStatus.WAITING
            recipient.next_attempt_at = at + RETRY_DELAY
            outcome = "retry"
        else:
            recipient.status = RecipientStatus.FAILED
            outcome = "uncertain" if e.uncertain else "failed"
        log_activity(
            db,
            SYSTEM,
            {"retry": "email.deferred", "failed": "email.failed", "uncertain": "email.uncertain"}[outcome],
            entity_type="lead",
            entity_id=recipient.lead_id,
            after={"campaign_id": campaign.id, "mailbox": mailbox.email, "error": str(e)},
        )
    else:
        recipient.status = RecipientStatus.SENT
        recipient.sent_at = at
        recipient.message_id = sent.message_id
        recipient.smtp_response = sent.response
        recipient.error = None
        recipient.next_attempt_at = None
        outcome = "sent"
        log_activity(
            db,
            SYSTEM,
            "email.sent",
            entity_type="lead",
            entity_id=recipient.lead_id,
            after={"campaign_id": campaign.id, "mailbox": mailbox.email, "message_id": sent.message_id},
        )

    _finish_if_done(db, campaign, at)
    db.commit()
    if outcome == "sent" and mailbox.imap_host:
        _save_sent_copy(db, mailbox, recipient, msg)
    return outcome


def _save_sent_copy(db: Session, mailbox: Mailbox, recipient: CampaignRecipient, msg: EmailMessage) -> None:
    """Best effort, after the send is safely recorded: a failure here never affects the send."""
    try:
        mailbox.sent_folder = mailer.save_sent_copy(mailbox, msg)
        recipient.sent_copy_saved = True
    except mailer.SentCopyFailed as e:
        recipient.sent_copy_saved = False
        log.warning("couldn't save a Sent copy for %s: %s", recipient.email, e)
    db.commit()


def recover_interrupted(db: Session, at: datetime) -> int:
    """Settle emails left in SENDING by a crash or restart. Returns how many.

    We can't know whether the mail server took them, so they are marked failed with an explanation
    and never resent automatically: a missed email is better than the same cold email twice.
    """
    stuck = db.scalars(
        select(CampaignRecipient)
        .where(
            CampaignRecipient.status == RecipientStatus.SENDING,
            CampaignRecipient.send_started_at < at - INTERRUPTED_AFTER,
        )
        .with_for_update(skip_locked=True)
    ).all()
    for r in stuck:
        r.status = RecipientStatus.FAILED
        r.error = (
            "Sending was interrupted (the server stopped or restarted). It may have been delivered, "
            "so it wasn't sent again."
        )
        log_activity(
            db,
            SYSTEM,
            "email.uncertain",
            entity_type="lead",
            entity_id=r.lead_id,
            after={"campaign_id": r.campaign_id, "reason": "interrupted"},
        )
    for campaign_id in {r.campaign_id for r in stuck}:
        _finish_if_done(db, db.get(Campaign, campaign_id), at)
    db.commit()
    return len(stuck)


def run_tick(deliver: Deliver = mailer.send) -> None:
    with SessionLocal() as db:
        recovered = recover_interrupted(db, now())
        if recovered:
            log.warning("marked %s interrupted send(s) as failed without resending", recovered)
        mailbox_ids = db.scalars(select(Mailbox.id).where(Mailbox.status == MailboxStatus.ACTIVE)).all()
    for mailbox_id in mailbox_ids:
        try:
            with SessionLocal() as db:
                outcome = send_next(db, mailbox_id, now(), deliver)
            if outcome not in ("not_due", "idle"):
                log.info("mailbox %s: %s", mailbox_id, outcome)
        except Exception:
            # One broken mailbox must not stop the others.
            log.exception("sending failed for mailbox %s", mailbox_id)
