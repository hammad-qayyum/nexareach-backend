"""Starting campaigns and the sending engine (plan section 16, cut down: no warm-up yet).

The worker calls plan_tick() every minute, then send_one() for each mailbox at its planned (random)
second. For a mailbox that is due and under its daily cap, send_next() sends one email from the running
campaign whose turn it is, then sets the mailbox's next free time to now + a random gap; the domain's other
mailboxes wait a random 1–3 minutes, and no company gets more than two emails a day (app.services.pacing).
Each mailbox row is locked while it sends, so it never sends twice at once, even with several workers.

Follow-ups: after the opening email, each follow-up step goes out `delay_days` after the previous
email to that person, from the same mailbox, as a reply in the same thread. Due follow-ups go before
new opening emails. They stop when the person replies, bounces or opts out (app.services.replies), is
put on the do-not-contact list, or the campaign is cancelled. Follow-ups count towards the daily cap and
the 30-day rule like any other email.
"""

import logging
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Select, exists, func, or_, select, update
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models import (
    EMAILED,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    DeliverySource,
    DeliveryStatus,
    DncKind,
    DoNotContact,
    FollowUpEmail,
    FollowUpStatus,
    Lead,
    Mailbox,
    MailboxStatus,
    RecipientStatus,
    campaign_exclusions,
    campaign_mailboxes,
    lead_group_members,
)
from app.services import deals, delivery, mailer, pacing, placement, privacy, warmup
from app.services.app_settings import business_fields, email_footer
from app.services.audit import SYSTEM, Actor, log_activity
from app.services.templates import add_footer, compose, compose_versioned, lead_fields

log = logging.getLogger(__name__)

# Nobody is emailed twice within this many days, by any campaign.
OVERLAP_DAYS = 30
RETRY_DELAY = timedelta(minutes=15)
MAX_RETRIES = 3
# A send takes seconds (30 s timeouts); anything SENDING for longer was cut off by a crash or restart.
INTERRUPTED_AFTER = timedelta(minutes=10)
# The worker ticks once a minute, and a mailbox's next free time lands a moment after a tick (the gap is
# counted from the send, which happens just after the tick starts). Without this slack a 60-second gap would
# wait for the following tick and become two minutes.
TICK_SLACK = timedelta(seconds=5)
MAX_FOLLOW_UPS = 5
# People on Deals' do-not-email list are passed over within one send turn; after this many the turn ends.
MAX_BLOCKED_PER_TURN = 25
# The worker plans once a minute; each mailbox then sends at a random second of that minute (never all at
# :00). A mailbox that's simply free gets a second in the first RANDOM_START_SECONDS.
TICK = timedelta(seconds=60)
RANDOM_START_SECONDS = 55

Deliver = Callable[[Mailbox, EmailMessage], mailer.Sent]


def now() -> datetime:
    return datetime.now(UTC)


def in_window(campaign: Campaign, at: datetime) -> bool:
    return in_window_at(campaign, campaign.timezone, at)


def sent_today(db: Session, mailbox_id: int, at: datetime) -> int:
    """Opening emails, follow-ups, warm-up and placement-test emails sent by this mailbox today (UTC days)."""
    day_start = at.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    first = db.scalar(
        select(func.count())
        .select_from(CampaignRecipient)
        .where(
            CampaignRecipient.mailbox_id == mailbox_id,
            CampaignRecipient.status.in_(EMAILED),
            CampaignRecipient.sent_at >= day_start,
        )
    )
    follow_ups = db.scalar(
        select(func.count())
        .select_from(FollowUpEmail)
        .where(
            FollowUpEmail.mailbox_id == mailbox_id,
            FollowUpEmail.status == FollowUpStatus.SENT,
            FollowUpEmail.sent_at >= day_start,
        )
    )
    return (
        first
        + follow_ups
        + warmup.sent_today(db, mailbox_id, day_start)
        + placement.sent_today(db, mailbox_id, day_start)
    )


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[-1]


def on_do_not_contact(db: Session, email: str) -> bool:
    return (
        db.scalar(
            select(DoNotContact.id).where(
                or_(
                    (DoNotContact.kind == DncKind.EMAIL)
                    & (DoNotContact.value.in_([email, privacy.email_hash(email)])),
                    (DoNotContact.kind == DncKind.DOMAIN) & (DoNotContact.value == _domain(email)),
                )
            )
        )
        is not None
    )


def blocked_reason(db: Session, email: str, campaign_id: int, at: datetime) -> str | None:
    """Why this address must not be emailed now, or None."""
    if on_do_not_contact(db, email):
        return "do_not_contact"
    since = at - timedelta(days=OVERLAP_DAYS)
    recent = db.scalar(
        select(
            exists().where(
                CampaignRecipient.email == email,
                CampaignRecipient.status.in_(EMAILED),
                CampaignRecipient.sent_at >= since,
                CampaignRecipient.campaign_id != campaign_id,
            )
        )
    ) or db.scalar(
        select(
            exists().where(
                FollowUpEmail.recipient_id == CampaignRecipient.id,
                CampaignRecipient.email == email,
                FollowUpEmail.status == FollowUpStatus.SENT,
                FollowUpEmail.sent_at >= since,
                FollowUpEmail.campaign_id != campaign_id,
            )
        )
    )
    return "contacted_recently" if recent else None


DealsCheck = Callable[[list[str]], dict[str, tuple[str, datetime | None]]]


def start(
    db: Session, campaign: Campaign, actor: Actor, at: datetime, deals_check: DealsCheck | None = None
) -> dict[str, int]:
    """Fix the recipient list and set the campaign running. The caller has checked it can start.

    `deals_check` (when Deals' do-not-email list is switched on) gets every lead's hash and returns the
    blocked ones; it may raise DealsError, before anything is saved."""
    group_ids = [g.id for g in campaign.groups]
    excluded = select(campaign_exclusions.c.lead_id).where(campaign_exclusions.c.campaign_id == campaign.id)
    leads = db.scalars(
        select(Lead)
        .join(lead_group_members, lead_group_members.c.lead_id == Lead.id)
        .where(lead_group_members.c.group_id.in_(group_ids), Lead.id.not_in(excluded))
        .order_by(Lead.id)
        .distinct()
    ).all()
    blocked = deals_check([lead.email_hash for lead in leads]) if deals_check is not None else {}

    dnc_emails = set(db.scalars(select(DoNotContact.value).where(DoNotContact.kind == DncKind.EMAIL)))
    dnc_domains = set(db.scalars(select(DoNotContact.value).where(DoNotContact.kind == DncKind.DOMAIN)))
    since = at - timedelta(days=OVERLAP_DAYS)
    recent = set(
        db.scalars(
            select(CampaignRecipient.email).where(
                CampaignRecipient.status.in_(EMAILED), CampaignRecipient.sent_at >= since
            )
        )
    ) | set(
        db.scalars(
            select(CampaignRecipient.email)
            .join(FollowUpEmail, FollowUpEmail.recipient_id == CampaignRecipient.id)
            .where(FollowUpEmail.status == FollowUpStatus.SENT, FollowUpEmail.sent_at >= since)
        )
    )

    # One recipient per lead. Two leads may share an email when someone chose "keep both" for a
    # duplicate; each copy gets its own email, as the plan specifies.
    counts = {"waiting": 0, "skipped": 0}
    if deals_check is not None:
        counts["blocked_by_deals"] = 0
    for lead in leads:
        block = blocked.get(lead.email_hash)
        if block is None and lead.handed_to_deals_at is not None:
            block = ("lead", None)  # handed to Deals by NexaReach: Deals' sales team has them now
        if block is not None:
            db.add(
                CampaignRecipient(
                    campaign_id=campaign.id,
                    lead_id=lead.id,
                    email=lead.email,
                    status=RecipientStatus.BLOCKED_BY_DEALS,
                    skip_reason=block[0][:50],
                )
            )
            counts["blocked_by_deals"] = counts.get("blocked_by_deals", 0) + 1
            continue
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
    """Complete the campaign once nothing is left to send: no opening email and no follow-up."""
    db.flush()
    pending = db.scalar(
        select(
            exists().where(
                CampaignRecipient.campaign_id == campaign.id,
                or_(
                    CampaignRecipient.status.in_([RecipientStatus.WAITING, RecipientStatus.SENDING]),
                    CampaignRecipient.next_follow_up_at.is_not(None),
                ),
            )
        )
    ) or db.scalar(
        select(
            exists().where(
                FollowUpEmail.campaign_id == campaign.id, FollowUpEmail.status == FollowUpStatus.SENDING
            )
        )
    )
    if not pending and campaign.status == CampaignStatus.RUNNING:
        _complete(db, campaign, at)


def finish_if_done(db: Session, campaign_id: int, at: datetime) -> None:
    campaign = db.get(Campaign, campaign_id)
    if campaign is not None:
        _finish_if_done(db, campaign, at)


def in_window_at(campaign: Campaign, zone: str, at: datetime) -> bool:
    local = at.astimezone(ZoneInfo(zone))
    return local.weekday() in campaign.send_days and campaign.start_hour <= local.hour < campaign.end_hour


# --- Follow-up helpers --------------------------------------------------------------------------------


def follow_up_delay(campaign: Campaign, index: int) -> timedelta | None:
    """Wait before follow-up number index + 1 (counted from the previous email), or None if there is none."""
    steps: list[dict[str, Any]] = campaign.follow_ups or []
    if not campaign.follow_ups_enabled or index >= len(steps):
        return None
    return timedelta(days=int(steps[index]["delay_days"]))


def reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def stop_follow_ups(db: Session, recipient: CampaignRecipient, reason: str) -> bool:
    """No more follow-ups for this person. Returns whether any were still planned."""
    planned = recipient.next_follow_up_at is not None
    recipient.next_follow_up_at = None
    if planned and recipient.follow_up_stop is None:
        recipient.follow_up_stop = reason
    db.execute(
        update(FollowUpEmail)
        .where(FollowUpEmail.recipient_id == recipient.id, FollowUpEmail.status == FollowUpStatus.WAITING)
        .values(status=FollowUpStatus.FAILED, error=f"Not sent: follow-ups stopped ({reason}).")
        .execution_options(synchronize_session=False)
    )
    return planned


def stop_campaign_follow_ups(db: Session, campaign_id: int, reason: str) -> None:
    for recipient in db.scalars(
        select(CampaignRecipient).where(
            CampaignRecipient.campaign_id == campaign_id, CampaignRecipient.next_follow_up_at.is_not(None)
        )
    ):
        stop_follow_ups(db, recipient, reason)


def reschedule_follow_ups(db: Session, campaign: Campaign) -> int:
    """After the follow-up steps changed (campaign paused): plan each person's next one again.

    The wait counts from the last email the person got. Returns how many people have one planned.
    """
    last_follow_up = (
        select(FollowUpEmail.recipient_id, func.max(FollowUpEmail.sent_at).label("at"))
        .where(FollowUpEmail.campaign_id == campaign.id, FollowUpEmail.status == FollowUpStatus.SENT)
        .group_by(FollowUpEmail.recipient_id)
        .subquery()
    )
    rows = db.execute(
        select(CampaignRecipient, last_follow_up.c.at)
        .outerjoin(last_follow_up, last_follow_up.c.recipient_id == CampaignRecipient.id)
        .where(
            CampaignRecipient.campaign_id == campaign.id,
            CampaignRecipient.status == RecipientStatus.SENT,
            CampaignRecipient.lead_id.is_not(None),
            CampaignRecipient.replied_at.is_(None),
            CampaignRecipient.follow_up_stop.is_(None),
        )
    ).all()
    planned = 0
    for recipient, last_at in rows:
        delay = follow_up_delay(campaign, recipient.follow_ups_sent)
        if delay is None:
            # They already had every follow-up that is left: nothing more to send, nothing "stopped".
            recipient.next_follow_up_at = None
            db.execute(
                update(FollowUpEmail)
                .where(
                    FollowUpEmail.recipient_id == recipient.id, FollowUpEmail.status == FollowUpStatus.WAITING
                )
                .values(status=FollowUpStatus.FAILED, error="Not sent: this follow-up was removed.")
                .execution_options(synchronize_session=False)
            )
            continue
        recipient.next_follow_up_at = max(filter(None, [recipient.sent_at, last_at])) + delay
        planned += 1
    return planned


# --- Picking the next email ------------------------------------------------------------------------------


def _open_query(db: Session, campaign: Campaign, at: datetime, due: list[Any]) -> Select | None:
    """Rows of this campaign whose sending window is open now, or None when it is closed for all of them."""
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
            return None
        return query.join(Lead, Lead.id == CampaignRecipient.lead_id).where(zone.in_(open_zones))
    if not in_window(campaign, at):
        return None
    return query


def _pick(db: Session, mailbox: Mailbox, at: datetime) -> tuple[CampaignRecipient, Campaign, bool] | None:
    """The next email for this mailbox: (recipient, campaign, is_follow_up)."""
    if mailbox.campaigns_paused_at is not None:
        # Campaign sending is paused for this mailbox (e.g. a poor warm-up inbox rate); warm-up carries on.
        return None

    # People at a company that already got its emails for today wait (opening emails and follow-ups).
    company_ok = pacing.not_full(pacing.full_companies(db, at))

    # Follow-ups first: each is due on a given day, while opening emails can always wait a little.
    # They go from the mailbox that sent the opening email, even if it was since taken off the campaign.
    def follow_up_due(campaign_id: Any) -> list[Any]:
        return [
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.mailbox_id == mailbox.id,
            CampaignRecipient.status == RecipientStatus.SENT,
            CampaignRecipient.next_follow_up_at <= at,
            *company_ok,
        ]

    with_follow_ups = db.scalars(
        select(Campaign)
        .where(Campaign.status == CampaignStatus.RUNNING, exists().where(*follow_up_due(Campaign.id)))
        .order_by(Campaign.last_sent_at.asc().nulls_first(), Campaign.id)
    ).all()
    for campaign in with_follow_ups:
        due = follow_up_due(campaign.id)
        query = _open_query(db, campaign, at, due)
        if query is None:
            continue
        recipient = db.scalar(
            query.where(*due)
            .order_by(CampaignRecipient.next_follow_up_at, CampaignRecipient.id)
            .limit(1)
            .with_for_update(of=CampaignRecipient, skip_locked=True)
        )
        if recipient is not None:
            return recipient, campaign, True

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
            *company_ok,
        ]
        query = _open_query(db, campaign, at, due)
        if query is None:
            continue
        recipient = db.scalar(
            query.where(*due)
            .order_by(CampaignRecipient.id)
            .limit(1)
            .with_for_update(of=CampaignRecipient, skip_locked=True)
        )
        if recipient is not None:
            return recipient, campaign, False
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
            or_(Mailbox.next_send_at.is_(None), Mailbox.next_send_at <= at + TICK_SLACK),
        )
        .with_for_update(skip_locked=True)
    )
    if mailbox is None:
        return "not_due"
    if sent_today(db, mailbox.id, at) >= warmup.daily_limit(mailbox, at.astimezone(UTC).date()):
        return "cap_reached"
    # Warm-up emails have set times in the day; campaign emails can always wait a little.
    sent_warmup = warmup.send(db, mailbox, at, deliver, rng)
    if sent_warmup is not None:
        return sent_warmup
    check_deals = deals.blocklist_enabled(db)
    if check_deals and deals.campaigns_held(db, at):
        # Fail closed: the copy of Deals' list is too old to trust. Warm-up (above) carries on.
        return "deals_stale"
    passed_over = 0
    for _ in range(MAX_BLOCKED_PER_TURN):
        picked = _pick(db, mailbox, at)
        if picked is None:
            if passed_over:
                db.commit()
            return "idle"
        recipient, campaign, is_follow_up = picked
        block = deals.block_for(db, recipient.email, at) if check_deals else None
        if block is None and _handed_to_deals(db, recipient):
            block = ("lead", None)
        if block is None:
            break
        # Not committed yet: the mailbox stays locked for the rest of its turn.
        _blocked_by_deals(db, recipient, campaign, is_follow_up, block, at)
        passed_over += 1
    else:
        db.commit()
        return "blocked_by_deals"
    if is_follow_up:
        return _send_follow_up(db, mailbox, recipient, campaign, at, deliver, rng)

    reason = blocked_reason(db, recipient.email, campaign.id, at)
    if reason:
        recipient.status = RecipientStatus.SKIPPED
        recipient.skip_reason = reason
        _finish_if_done(db, campaign, at)
        db.commit()
        return "skipped"

    lead = db.get(Lead, recipient.lead_id)
    subject, body, html, version = compose_versioned(
        campaign.subject,
        campaign.body,
        {**business_fields(db), **lead_fields(lead)},
        mailbox.display_name,
        rng,
        campaign.body_format,
    )
    recipient.subject_version = version[:998]
    footer = email_footer(db)
    body, html = add_footer(body, html, footer)
    msg = mailer.build_message(
        mailbox, recipient.email, subject, body, html, list_unsubscribe=footer is not None
    )

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
    pacing.space_domain(db, mailbox, at, rng)
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
        _mailbox_login_failed(db, mailbox, e)
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
        _record_failure(db, e, at, recipient=recipient)
        _log_failure(db, outcome, recipient.lead_id, campaign.id, mailbox, e)
    else:
        recipient.status = RecipientStatus.SENT
        recipient.sent_at = at
        recipient.message_id = sent.message_id
        recipient.smtp_response = sent.response
        recipient.error = None
        recipient.next_attempt_at = None
        delay = follow_up_delay(campaign, 0)
        recipient.next_follow_up_at = at + delay if delay is not None else None
        outcome = "sent"
        delivery.record(
            db,
            source=DeliverySource.SMTP,
            status=DeliveryStatus.SENT,
            at=at,
            recipient=recipient,
            response=sent.response,
        )
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


def _send_follow_up(
    db: Session,
    mailbox: Mailbox,
    recipient: CampaignRecipient,
    campaign: Campaign,
    at: datetime,
    deliver: Deliver,
    rng: random.Random,
) -> str:
    step = recipient.follow_ups_sent + 1
    steps: list[dict[str, Any]] = campaign.follow_ups or []
    # Nothing to follow up on: the steps were removed, the person was forgotten, or the opening
    # email's record is gone.
    gone = recipient.lead_id is None or not recipient.subject_sent or not recipient.message_id
    if step > len(steps) or gone or not campaign.follow_ups_enabled:
        recipient.next_follow_up_at = None
        _finish_if_done(db, campaign, at)
        db.commit()
        return "skipped"
    if on_do_not_contact(db, recipient.email):
        stop_follow_ups(db, recipient, "do_not_contact")
        _finish_if_done(db, campaign, at)
        db.commit()
        return "skipped"

    lead = db.get(Lead, recipient.lead_id)
    _, body, _ = compose(
        "", steps[step - 1]["body"], {**business_fields(db), **lead_fields(lead)}, mailbox.display_name, rng
    )
    footer = email_footer(db)
    body, _ = add_footer(body, None, footer)
    subject = reply_subject(recipient.subject_sent)
    earlier = [
        recipient.message_id,
        *db.scalars(
            select(FollowUpEmail.message_id)
            .where(
                FollowUpEmail.recipient_id == recipient.id,
                FollowUpEmail.status == FollowUpStatus.SENT,
                FollowUpEmail.message_id.is_not(None),
            )
            .order_by(FollowUpEmail.step)
        ),
    ]
    msg = mailer.build_message(
        mailbox, recipient.email, subject, body, thread=earlier, list_unsubscribe=footer is not None
    )

    row = db.scalar(
        select(FollowUpEmail).where(FollowUpEmail.recipient_id == recipient.id, FollowUpEmail.step == step)
    )
    if row is None:
        row = FollowUpEmail(recipient_id=recipient.id, campaign_id=campaign.id, step=step, attempts=0)
        db.add(row)
    # Two-phase, like opening emails: committed as SENDING before the mail server is contacted.
    row.status = FollowUpStatus.SENDING
    row.mailbox_id = mailbox.id
    row.attempts += 1
    row.send_started_at = at
    row.subject_sent = subject
    row.body_sent = body
    recipient.next_follow_up_at = None
    campaign.last_sent_at = at
    mailbox.next_send_at = at + timedelta(
        seconds=rng.randint(campaign.gap_min_seconds, campaign.gap_max_seconds)
    )
    pacing.space_domain(db, mailbox, at, rng)
    db.commit()

    try:
        sent = deliver(mailbox, msg)
    except mailer.AuthFailed as e:
        row.status = FollowUpStatus.WAITING
        row.attempts -= 1
        row.send_started_at = None
        recipient.next_follow_up_at = at
        _mailbox_login_failed(db, mailbox, e)
        db.commit()
        return "auth_failed"
    except mailer.SendFailed as e:
        row.error = str(e)
        if e.temporary and not e.uncertain and row.attempts <= MAX_RETRIES:
            row.status = FollowUpStatus.WAITING
            recipient.next_follow_up_at = at + RETRY_DELAY
            outcome = "retry"
        else:
            row.status = FollowUpStatus.FAILED
            stop_follow_ups(db, recipient, "failed")
            # stop_follow_ups only records a reason when one was planned; this one was being sent.
            recipient.follow_up_stop = recipient.follow_up_stop or "failed"
            outcome = "uncertain" if e.uncertain else "failed"
        _record_failure(db, e, at, recipient=recipient, follow_up=row)
        _log_failure(db, outcome, recipient.lead_id, campaign.id, mailbox, e, step=step)
    else:
        row.status = FollowUpStatus.SENT
        row.sent_at = at
        row.message_id = sent.message_id
        row.smtp_response = sent.response
        row.error = None
        recipient.follow_ups_sent = step
        delivery.record(
            db,
            source=DeliverySource.SMTP,
            status=DeliveryStatus.SENT,
            at=at,
            recipient=recipient,
            follow_up=row,
            response=sent.response,
        )
        # A reply may have been found while this was being sent: then nothing more is planned.
        if recipient.replied_at is None and recipient.follow_up_stop is None:
            delay = follow_up_delay(campaign, step)
            recipient.next_follow_up_at = at + delay if delay is not None else None
        outcome = "sent"
        log_activity(
            db,
            SYSTEM,
            "email.follow_up_sent",
            entity_type="lead",
            entity_id=recipient.lead_id,
            after={
                "campaign_id": campaign.id,
                "mailbox": mailbox.email,
                "step": step,
                "message_id": sent.message_id,
            },
        )

    _finish_if_done(db, campaign, at)
    db.commit()
    if outcome == "sent" and mailbox.imap_host:
        _save_sent_copy(db, mailbox, row, msg)
    return f"follow_up_{outcome}"


def _handed_to_deals(db: Session, recipient: CampaignRecipient) -> bool:
    lead = db.get(Lead, recipient.lead_id) if recipient.lead_id else None
    return lead is not None and lead.handed_to_deals_at is not None


def _blocked_by_deals(
    db: Session,
    recipient: CampaignRecipient,
    campaign: Campaign,
    is_follow_up: bool,
    block: tuple[str, datetime | None],
    at: datetime,
) -> None:
    """Check 3: this person is on Deals' do-not-email list. Never emailed; someone already emailed keeps
    SENT and only their follow-ups stop. The caller commits."""
    reason, until = block
    if is_follow_up:
        stop_follow_ups(db, recipient, "blocked_by_deals")
    else:
        recipient.status = RecipientStatus.BLOCKED_BY_DEALS
        recipient.skip_reason = reason[:50]
        recipient.next_attempt_at = None
    log_activity(
        db,
        SYSTEM,
        "email.blocked_by_deals",
        entity_type="lead",
        entity_id=recipient.lead_id,
        after={
            "campaign_id": campaign.id,
            "reason": reason,
            "until": until.isoformat() if until else None,
            "follow_up": is_follow_up,
        },
    )
    _finish_if_done(db, campaign, at)


def _mailbox_login_failed(db: Session, mailbox: Mailbox, e: Exception) -> None:
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


def _record_failure(
    db: Session,
    e: mailer.SendFailed,
    at: datetime,
    *,
    recipient: CampaignRecipient,
    follow_up: FollowUpEmail | None = None,
) -> None:
    """The mail server's refusal, for the log viewer. Not when it may have gone out (uncertain)."""
    if e.uncertain:
        return
    db.flush()  # a new follow-up row needs its id
    delivery.record(
        db,
        source=DeliverySource.SMTP,
        status=DeliveryStatus.DEFERRED if e.temporary else DeliveryStatus.REJECTED,
        at=at,
        recipient=recipient,
        follow_up=follow_up,
        response=str(e),
    )


def _log_failure(
    db: Session,
    outcome: str,
    lead_id: int | None,
    campaign_id: int,
    mailbox: Mailbox,
    e: Exception,
    step: int | None = None,
) -> None:
    after: dict[str, Any] = {"campaign_id": campaign_id, "mailbox": mailbox.email, "error": str(e)}
    if step is not None:
        after["follow_up_step"] = step
    log_activity(
        db,
        SYSTEM,
        {"retry": "email.deferred", "failed": "email.failed", "uncertain": "email.uncertain"}[outcome],
        entity_type="lead",
        entity_id=lead_id,
        after=after,
    )


def _save_sent_copy(
    db: Session, mailbox: Mailbox, record: CampaignRecipient | FollowUpEmail, msg: EmailMessage
) -> None:
    """Best effort, after the send is safely recorded: a failure here never affects the send."""
    try:
        mailbox.sent_folder = mailer.save_sent_copy(mailbox, msg)
        record.sent_copy_saved = True
    except mailer.SentCopyFailed as e:
        record.sent_copy_saved = False
        log.warning("couldn't save a Sent copy: %s", e)
    db.commit()


def recover_interrupted(db: Session, at: datetime) -> int:
    """Settle emails left in SENDING by a crash or restart. Returns how many.

    We can't know whether the mail server took them, so they are marked failed with an explanation
    and never resent automatically: a missed email is better than the same cold email twice.
    """
    message = (
        "Sending was interrupted (the server stopped or restarted). It may have been delivered, "
        "so it wasn't sent again."
    )
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
        r.error = message
        log_activity(
            db,
            SYSTEM,
            "email.uncertain",
            entity_type="lead",
            entity_id=r.lead_id,
            after={"campaign_id": r.campaign_id, "reason": "interrupted"},
        )
    stuck_follow_ups = db.scalars(
        select(FollowUpEmail)
        .where(
            FollowUpEmail.status == FollowUpStatus.SENDING,
            FollowUpEmail.send_started_at < at - INTERRUPTED_AFTER,
        )
        .with_for_update(skip_locked=True)
    ).all()
    for f in stuck_follow_ups:
        f.status = FollowUpStatus.FAILED
        f.error = message
        recipient = db.get(CampaignRecipient, f.recipient_id)
        if recipient is not None:
            recipient.next_follow_up_at = None
            recipient.follow_up_stop = recipient.follow_up_stop or "failed"
        log_activity(
            db,
            SYSTEM,
            "email.uncertain",
            entity_type="lead",
            entity_id=recipient.lead_id if recipient else None,
            after={"campaign_id": f.campaign_id, "reason": "interrupted", "follow_up_step": f.step},
        )
    for campaign_id in {r.campaign_id for r in stuck} | {f.campaign_id for f in stuck_follow_ups}:
        finish_if_done(db, campaign_id, at)
    db.commit()
    return len(stuck) + len(stuck_follow_ups)


def tick_plan(
    mailboxes: list[tuple[int, datetime | None]], start: datetime, rng: random.Random
) -> list[tuple[datetime, int]]:
    """When in the minute after `start` to try each mailbox, earliest first.

    A mailbox's next free time is random to the second, so it goes at that second. One that's already free
    (idle, or a campaign just started) gets a random second, so emails never all leave at :00.
    """
    plan = []
    for mailbox_id, free_at in mailboxes:
        if free_at is not None and free_at > start:
            when = free_at
        else:
            when = start + timedelta(seconds=rng.uniform(0, RANDOM_START_SECONDS))
        if when < start + TICK:
            plan.append((when, mailbox_id))
    return sorted(plan)


def plan_tick(
    clock: Callable[[], datetime] = now, rng: random.Random | None = None
) -> list[tuple[datetime, int]]:
    """Run once a minute by the worker: tidy up cut-off sends, then say when each mailbox should send.
    The worker books one small job per mailbox at that second (see send_one)."""
    rng = rng or random.Random()  # noqa: S311 - variety, not security
    start = clock()
    with SessionLocal() as db:
        recovered = recover_interrupted(db, start)
        if recovered:
            log.warning("marked %s interrupted send(s) as failed without resending", recovered)
        # Opens (once) or closes the "Deals list out of date" alert; the sends themselves check too.
        deals.check_stale(db, start)
        mailboxes = db.execute(
            select(Mailbox.id, Mailbox.next_send_at).where(Mailbox.status == MailboxStatus.ACTIVE)
        ).all()
    return tick_plan([(m.id, m.next_send_at) for m in mailboxes], start, rng)


def send_one(mailbox_id: int, deliver: Deliver = mailer.send) -> str:
    """One mailbox's turn, at the second plan_tick picked for it."""
    with SessionLocal() as db:
        outcome = send_next(db, mailbox_id, now(), deliver)
    if outcome not in ("not_due", "idle"):
        log.info("mailbox %s: %s", mailbox_id, outcome)
    return outcome
