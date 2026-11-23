"""Placement tests (plan section 19): where does our email land at Gmail, Outlook, Yahoo...?

A test sends a campaign's real subject and message from one mailbox to every active helper account (the
warm-up helpers), then signs in to each helper a few minutes later and looks for it: Inbox, Gmail's
Promotions tab, or Spam. Not found after an hour = missing. The helpers do nothing else with it (no
opening or rescuing), so the result is what a stranger's inbox would do.

Weekly for each mailbox that sent campaign emails lately, and on demand ("Run test"). Each test email
counts towards the mailbox's daily limit. A test that ends mostly in spam raises a warning alert.
"""

import logging
import random
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    Mailbox,
    MailboxStatus,
    PlacementFolder,
    PlacementResult,
    PlacementState,
    PlacementTest,
    PlacementTestStatus,
    RecipientStatus,
    WarmupHelper,
    campaign_mailboxes,
)
from app.services import alerts, mailer, warmup, warmup_content
from app.services.app_settings import business_fields
from app.services.audit import SYSTEM, Actor, log_activity
from app.services.templates import compose

log = logging.getLogger(__name__)

# Look for it this long after sending (providers file mail within seconds; some filters take a minute).
CHECK_AFTER = timedelta(minutes=3)
MISSING_AFTER = timedelta(hours=1)
WEEKLY_EVERY = timedelta(days=6)
ACTIVE_WITHIN = timedelta(days=14)


class PlacementError(Exception):
    pass


def default_campaign(db: Session, mailbox_id: int) -> Campaign | None:
    """The mailbox's running campaign, else the one it sent for most recently."""
    return db.scalar(
        select(Campaign)
        .join(campaign_mailboxes, campaign_mailboxes.c.campaign_id == Campaign.id)
        .where(campaign_mailboxes.c.mailbox_id == mailbox_id, Campaign.subject != "")
        .order_by(
            (Campaign.status == CampaignStatus.RUNNING).desc(), Campaign.last_sent_at.desc().nulls_last()
        )
        .limit(1)
    )


def start(
    db: Session, mailbox: Mailbox, campaign: Campaign | None, actor: Actor, at: datetime
) -> PlacementTest:
    """Plan a test (the worker sends it within a minute). The caller commits."""
    if mailbox.archived_at is not None:
        raise PlacementError("mailbox_archived")
    if mailbox.status == MailboxStatus.ERROR:
        raise PlacementError("mailbox_not_connected")
    helpers = warmup.active_helpers(db, exclude_email=mailbox.email)
    if not helpers:
        raise PlacementError("no_helpers")
    running = db.scalar(
        select(PlacementTest.id).where(
            PlacementTest.mailbox_id == mailbox.id, PlacementTest.status == PlacementTestStatus.RUNNING
        )
    )
    if running:
        raise PlacementError("placement_running")
    test = PlacementTest(
        mailbox_id=mailbox.id,
        campaign_id=campaign.id if campaign else None,
        subject=campaign.subject if campaign else "(sample message)",
        status=PlacementTestStatus.RUNNING,
        requested_by_id=actor.user_id,
        created_at=at,
    )
    db.add(test)
    db.flush()
    for h in helpers:
        db.add(
            PlacementResult(
                test_id=test.id,
                helper_id=h.id,
                helper_email=h.email,
                provider=h.provider,
                state=PlacementState.WAITING,
            )
        )
    log_activity(
        db,
        actor,
        "placement.started",
        entity_type="mailbox",
        entity_id=mailbox.id,
        after={"test_id": test.id, "campaign_id": test.campaign_id, "helpers": len(helpers)},
    )
    return test


def sent_today(db: Session, mailbox_id: int, day_start: datetime) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(PlacementResult)
            .join(PlacementTest, PlacementTest.id == PlacementResult.test_id)
            .where(
                PlacementTest.mailbox_id == mailbox_id,
                PlacementResult.sent_at >= day_start,
                PlacementResult.state.in_([PlacementState.SENT, PlacementState.CHECKED]),
            )
        )
        or 0
    )


def _send(
    db: Session, result: PlacementResult, at: datetime, deliver: warmup.Deliver, rng: random.Random
) -> None:
    test = db.get(PlacementTest, result.test_id)
    mailbox = db.get(Mailbox, test.mailbox_id)
    helper = db.get(WarmupHelper, result.helper_id) if result.helper_id else None
    if helper is None or mailbox is None or mailbox.status == MailboxStatus.ERROR:
        result.state = PlacementState.FAILED
        result.error = (
            "The helper was removed." if helper is None else "The mailbox can't send (login failing)."
        )
        return
    campaign = db.get(Campaign, test.campaign_id) if test.campaign_id else None
    if campaign is not None:
        fields = {
            **business_fields(db),
            "first_name": warmup_content.first_name(helper.display_name),
            "email": helper.email,
        }
        subject, body, html = compose(
            campaign.subject, campaign.body, fields, mailbox.display_name, rng, campaign.body_format
        )
    else:
        subject, body = warmup_content.message(rng, mailbox.display_name, helper.display_name)
        html = None
    mid = warmup.message_id(mailbox.email)
    msg = mailer.build_message(mailbox, helper.email, subject, body, html, message_id=mid)
    result.message_id = mid
    try:
        deliver(mailbox, msg)
    except (mailer.AuthFailed, mailer.SendFailed) as e:
        result.state = PlacementState.FAILED
        result.error = str(e)[:1000]
        return
    result.state = PlacementState.SENT
    result.sent_at = at


def _folder(inbox: mailer.Inbox, spam: str | None, code: str) -> PlacementFolder | None:
    found = inbox.search("INBOX", code)
    if found:
        if inbox.is_gmail and inbox.gmail_search("INBOX", code, "category:promotions"):
            return PlacementFolder.PROMOTIONS
        return PlacementFolder.INBOX
    if spam and inbox.search(spam, code):
        return PlacementFolder.SPAM
    return None


def _check_helper(
    db: Session, helper_id: int, results: list[PlacementResult], at: datetime, open_inbox: warmup.OpenInbox
) -> None:
    helper = db.get(WarmupHelper, helper_id)
    if helper is None or mailer.development_block(helper):
        for r in results:
            r.state, r.error, r.checked_at = PlacementState.FAILED, "The helper can't be checked here.", at
        return
    try:
        with open_inbox(helper) as inbox:
            spam = helper.spam_folder or inbox.spam_folder()
            helper.spam_folder = spam
            for r in results:
                folder = _folder(inbox, spam, warmup.code_of(r.message_id or ""))
                if folder is None and at - r.sent_at < MISSING_AFTER:
                    continue
                r.folder = folder or PlacementFolder.MISSING
                r.state = PlacementState.CHECKED
                r.checked_at = at
    except mailer.InboxFailed as e:
        warmup.helper_failed(db, helper, str(e), at)
        for r in results:
            r.state, r.error, r.checked_at = PlacementState.FAILED, f"Couldn't sign in to the helper: {e}", at


def _finish(db: Session, test: PlacementTest, at: datetime) -> list:
    test.status = PlacementTestStatus.DONE
    test.finished_at = at
    results = db.scalars(select(PlacementResult).where(PlacementResult.test_id == test.id)).all()
    checked = [r for r in results if r.state == PlacementState.CHECKED]
    bad = [r for r in checked if r.folder in (PlacementFolder.SPAM, PlacementFolder.MISSING)]
    log_activity(
        db,
        SYSTEM,
        "placement.finished",
        entity_type="mailbox",
        entity_id=test.mailbox_id,
        after={
            "test_id": test.id,
            **{f.value.lower(): sum(1 for r in checked if r.folder == f) for f in PlacementFolder},
            "failed": len(results) - len(checked),
        },
    )
    limit = alerts.thresholds(db).placement_spam_pct
    mailbox = db.get(Mailbox, test.mailbox_id)
    conditions = []
    if checked and len(bad) / len(checked) * 100 > limit:
        conditions.append(
            alerts.Condition(
                f"{mailbox.email}: {len(bad)} of {len(checked)} test emails landed in spam or went missing.",
                mailbox_id=test.mailbox_id,
                target=str(test.mailbox_id),
            )
        )
    if not checked:
        return []
    return alerts.sync(db, "placement_spam", conditions, at, scope={str(test.mailbox_id)})


def run(
    db: Session,
    at: datetime,
    deliver: warmup.Deliver = mailer.send,
    open_inbox: warmup.OpenInbox = mailer.Inbox,
    rng: random.Random | None = None,
) -> dict[str, int]:
    """Send waiting test emails, look for sent ones, finish complete tests. Commits; emails new alerts."""
    rng = rng or random.Random()  # noqa: S311 - variety, not security
    counts = {"sent": 0, "checked": 0, "finished": 0}
    waiting = db.scalars(
        select(PlacementResult)
        .where(PlacementResult.state == PlacementState.WAITING)
        .with_for_update(skip_locked=True)
    ).all()
    for r in waiting:
        _send(db, r, at, deliver, rng)
        counts["sent"] += r.state == PlacementState.SENT
        db.commit()

    due = db.scalars(
        select(PlacementResult).where(
            PlacementResult.state == PlacementState.SENT, PlacementResult.sent_at <= at - CHECK_AFTER
        )
    ).all()
    by_helper: dict[int, list[PlacementResult]] = {}
    for r in due:
        if r.helper_id is None:
            r.state, r.error = PlacementState.FAILED, "The helper was removed."
            continue
        by_helper.setdefault(r.helper_id, []).append(r)
    for helper_id, results in by_helper.items():
        _check_helper(db, helper_id, results, at, open_inbox)
        counts["checked"] += sum(1 for r in results if r.state == PlacementState.CHECKED)
        db.commit()

    new_alerts = []
    for test in db.scalars(select(PlacementTest).where(PlacementTest.status == PlacementTestStatus.RUNNING)):
        pending = db.scalar(
            select(func.count())
            .select_from(PlacementResult)
            .where(
                PlacementResult.test_id == test.id,
                PlacementResult.state.in_([PlacementState.WAITING, PlacementState.SENT]),
            )
        )
        if not pending:
            new_alerts += _finish(db, test, at)
            counts["finished"] += 1
    db.commit()
    alerts.email_admins(db, new_alerts, at)
    return counts


def weekly(db: Session, at: datetime) -> int:
    """Start this week's test for each mailbox that sent campaign emails lately. Returns how many. Commits."""
    started = 0
    if not warmup.active_helpers(db):
        return 0
    for mailbox in db.scalars(
        select(Mailbox).where(Mailbox.status == MailboxStatus.ACTIVE, Mailbox.archived_at.is_(None))
    ):
        recent_send = db.scalar(
            select(func.max(CampaignRecipient.sent_at)).where(
                CampaignRecipient.mailbox_id == mailbox.id, CampaignRecipient.status == RecipientStatus.SENT
            )
        )
        last_test = db.scalar(
            select(func.max(PlacementTest.created_at)).where(PlacementTest.mailbox_id == mailbox.id)
        )
        if (
            recent_send is None
            or at - recent_send > ACTIVE_WITHIN
            or (last_test and at - last_test < WEEKLY_EVERY)
        ):
            continue
        try:
            start(db, mailbox, default_campaign(db, mailbox.id), SYSTEM, at)
        except PlacementError:
            continue
        started += 1
    db.commit()
    return started
