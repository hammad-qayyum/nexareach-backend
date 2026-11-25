"""Warm-up (plan section 18): ramp-up limits, and emails with helper accounts that build reputation.

Ramp-up: a new mailbox may send ramp_start_limit emails on its first day, RAMP_STEP more every RAMP_EVERY
working days, up to its daily cap. Warm-up emails count inside that limit.

Helpers: each warm-up mailbox sends 5-10 natural emails a day (Mon-Fri, working hours, at random times) to
random active helper accounts. 10-40 minutes later the worker signs in to the helper and acts like an
interested person: if the email went to spam it moves it to the inbox (and records that), marks it read and
starred (and important on Gmail), files about 20% into a folder and answers about 30%. How often warm-up
emails land in the inbox, per provider, is the mailbox's inbox rate.

Warm-up emails carry a hidden code in their Message-ID (and the helpers' answers in theirs and in
In-Reply-To), never in what people read. In our own mailboxes the helpers' answers are moved to a Warmup
folder, and reply checking ignores them.

If a mailbox's inbox rate stays under 50% for 3 days, its campaign emails pause (warm-up carries on) until
someone resumes them.
"""

import hashlib
import hmac
import logging
import random
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.crypto import _key
from app.core.db import SessionLocal
from app.models import (
    LandedIn,
    Mailbox,
    MailboxStatus,
    Setting,
    WarmupHelper,
    WarmupMessage,
    WarmupStatus,
)
from app.services import mailer, pacing, warmup_content
from app.services.audit import SYSTEM, log_activity

log = logging.getLogger(__name__)

RAMP_STEP = 3
RAMP_EVERY = 2  # working days
WARMUP_PER_DAY = (5, 10)
ACT_AFTER_MINUTES = (10, 40)
FOLDER_SHARE = 0.2
REPLY_SHARE = 0.3
# Not in the helper's inbox or spam this long after sending: counted as missing.
MISSING_AFTER = timedelta(hours=3)
# Pacing after a warm-up email (there's no campaign gap to borrow).
GAP_SECONDS = (300, 600)
HELPER_FOLDER = "Work"
WARMUP_FOLDER = "Warmup"
# Keep filing helper answers this long after a mailbox's last warm-up email.
FILING_AFTER_OFF = timedelta(days=7)
LOW_RATE = 0.5
LOW_RATE_DAYS = 3
MIN_RESULTS_PER_DAY = 2
SCHEDULE_KEY = "warmup.schedule"
DEFAULT_SCHEDULE = {"timezone": "Asia/Karachi", "days": [0, 1, 2, 3, 4], "start_hour": 8, "end_hour": 19}

Deliver = Callable[[Any, Any], mailer.Sent]
OpenInbox = Callable[[Any], mailer.Inbox]


# --- Ramp-up -------------------------------------------------------------------------------------------


def working_days_between(start: date, end: date) -> int:
    """Mon-Fri days after `start`, up to and including `end`."""
    if end <= start:
        return 0
    days = (end - start).days
    full_weeks, rest = divmod(days, 7)
    count = full_weeks * 5
    for i in range(1, rest + 1):
        if (start + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


def daily_limit(mailbox: Mailbox, today: date) -> int:
    """Emails this mailbox may send today (campaign and warm-up together)."""
    if not mailbox.ramp_up_enabled or mailbox.ramp_start_date is None:
        return mailbox.daily_cap
    steps = working_days_between(mailbox.ramp_start_date, today) // RAMP_EVERY
    return max(1, min(mailbox.daily_cap, mailbox.ramp_start_limit + RAMP_STEP * steps))


def ramp_day(mailbox: Mailbox, today: date) -> int | None:
    """Which day of ramp-up this is (1 = the first), or None when it isn't ramping (any more)."""
    if not mailbox.ramp_up_enabled or mailbox.ramp_start_date is None:
        return None
    if daily_limit(mailbox, today) >= mailbox.daily_cap:
        return None
    return (today - mailbox.ramp_start_date).days + 1


# --- The hidden code -------------------------------------------------------------------------------------

_CODE = re.compile(r"nxw([0-9a-f]{12})([0-9a-f]{8})")


def _signature(token: str) -> str:
    return hmac.new(_key(), f"warmup:{token}".encode(), hashlib.sha256).hexdigest()[:8]


def new_code() -> str:
    token = secrets.token_hex(6)
    return f"nxw{token}{_signature(token)}"


def code_of(message_id: str) -> str:
    """The searchable code inside one of our Message-IDs ("" if it has none)."""
    match = _CODE.search(message_id or "")
    return match.group(0) if match else ""


def is_warmup(text: str | None) -> bool:
    """Whether a Message-ID / In-Reply-To / References value carries one of our warm-up codes."""
    for match in _CODE.finditer(text or ""):
        if hmac.compare_digest(match.group(2), _signature(match.group(1))):
            return True
    return False


def message_id(sender_email: str) -> str:
    return f"<{secrets.token_hex(8)}.{new_code()}@{sender_email.rsplit('@', 1)[1]}>"


# --- Schedule ----------------------------------------------------------------------------------------------


@dataclass
class Schedule:
    timezone: str
    days: list[int]
    start_hour: int
    end_hour: int


def get_schedule(db: Session) -> Schedule:
    value = db.scalar(select(Setting.value).where(Setting.key == SCHEDULE_KEY))
    data = {**DEFAULT_SCHEDULE, **(value if isinstance(value, dict) else {})}
    return Schedule(data["timezone"], list(data["days"]), int(data["start_hour"]), int(data["end_hour"]))


def set_schedule(db: Session, schedule: Schedule, user_id: int | None) -> None:
    value = {
        "timezone": schedule.timezone,
        "days": schedule.days,
        "start_hour": schedule.start_hour,
        "end_hour": schedule.end_hour,
    }
    db.execute(
        insert(Setting)
        .values(key=SCHEDULE_KEY, value=value, updated_by_id=user_id)
        .on_conflict_do_update(index_elements=[Setting.key], set_={"value": value, "updated_by_id": user_id})
    )


def plan_for_day(mailbox_id: int, day: date, schedule: Schedule) -> list[datetime]:
    """When this mailbox's warm-up emails go out on `day`: 5-10 random moments in the working hours.

    The same every time it's asked (seeded by mailbox and day), so the plan doesn't need storing.
    """
    if day.weekday() not in schedule.days or schedule.end_hour <= schedule.start_hour:
        return []
    rng = random.Random(f"warmup:{mailbox_id}:{day.isoformat()}")  # noqa: S311 - timing, not security
    count = rng.randint(*WARMUP_PER_DAY)
    zone = ZoneInfo(schedule.timezone)
    start = datetime.combine(day, time(schedule.start_hour), zone)
    span = (datetime.combine(day, time(0), zone) + timedelta(hours=schedule.end_hour) - start) / count
    return sorted(start + span * i + span * rng.random() for i in range(count))


def _local_day_bounds(day: date, schedule: Schedule) -> tuple[datetime, datetime]:
    zone = ZoneInfo(schedule.timezone)
    start = datetime.combine(day, time(0), zone)
    return start, start + timedelta(days=1)


def attempted_today(db: Session, mailbox_id: int, day: date, schedule: Schedule) -> int:
    start, end = _local_day_bounds(day, schedule)
    return db.scalar(
        select(func.count())
        .select_from(WarmupMessage)
        .where(
            WarmupMessage.mailbox_id == mailbox_id,
            WarmupMessage.created_at >= start,
            WarmupMessage.created_at < end,
        )
    )


def sent_today(db: Session, mailbox_id: int, day_start: datetime) -> int:
    """Warm-up emails sent since day_start (for the daily limit, which counts in UTC days)."""
    return db.scalar(
        select(func.count())
        .select_from(WarmupMessage)
        .where(
            WarmupMessage.mailbox_id == mailbox_id,
            WarmupMessage.status == WarmupStatus.SENT,
            WarmupMessage.sent_at >= day_start,
        )
    )


def is_due(db: Session, mailbox: Mailbox, at: datetime, schedule: Schedule | None = None) -> bool:
    if not mailbox.warmup_enabled:
        return False
    schedule = schedule or get_schedule(db)
    local = at.astimezone(ZoneInfo(schedule.timezone))
    # Only in working hours, even if it fell behind (the mailbox was busy or the worker was down).
    if local.weekday() not in schedule.days or not schedule.start_hour <= local.hour < schedule.end_hour:
        return False
    day = local.date()
    attempted = attempted_today(db, mailbox.id, day, schedule)
    # Warm-up takes at most half of today's limit, so a mailbox in ramp-up (5 a day at first) still sends
    # campaign emails. From a limit of 20 the full 5-10 fit.
    if attempted >= max(1, daily_limit(mailbox, at.astimezone(UTC).date()) // 2):
        return False
    due = sum(1 for slot in plan_for_day(mailbox.id, day, schedule) if slot <= at)
    return due > attempted


def active_helpers(db: Session, exclude_email: str | None = None) -> list[WarmupHelper]:
    query = select(WarmupHelper).where(WarmupHelper.status == MailboxStatus.ACTIVE)
    if exclude_email:
        query = query.where(WarmupHelper.email != exclude_email)
    return list(db.scalars(query.order_by(WarmupHelper.id)))


# --- Sending (called by sending.send_next, which holds the mailbox's lock and pacing) -------------------


def send(db: Session, mailbox: Mailbox, at: datetime, deliver: Deliver, rng: random.Random) -> str | None:
    """Send one warm-up email if one is due. Returns what happened, or None if nothing was due."""
    if not is_due(db, mailbox, at):
        return None
    helpers = active_helpers(db, exclude_email=mailbox.email)
    if not helpers:
        return None
    # Spread the mailbox's emails over the helpers: the one it wrote to least in the last week goes first.
    week = at - timedelta(days=7)
    used = dict(
        db.execute(
            select(WarmupMessage.helper_id, func.count())
            .where(WarmupMessage.mailbox_id == mailbox.id, WarmupMessage.created_at >= week)
            .group_by(WarmupMessage.helper_id)
        ).all()
    )
    fewest = min(used.get(h.id, 0) for h in helpers)
    helper = rng.choice([h for h in helpers if used.get(h.id, 0) == fewest])

    subject, body = warmup_content.message(rng, mailbox.display_name, helper.display_name)
    mid = message_id(mailbox.email)
    msg = mailer.build_message(mailbox, helper.email, subject, body, message_id=mid)
    record = WarmupMessage(
        mailbox_id=mailbox.id,
        helper_id=helper.id,
        provider=helper.provider,
        status=WarmupStatus.SENDING,
        message_id=mid,
        subject=subject,
        send_started_at=at,
        created_at=at,
    )
    db.add(record)
    mailbox.next_send_at = at + timedelta(seconds=rng.randint(*GAP_SECONDS))
    pacing.space_domain(db, mailbox, at, rng)
    db.commit()

    try:
        sent = deliver(mailbox, msg)
    except mailer.AuthFailed as e:
        record.status = WarmupStatus.FAILED
        record.error = str(e)[:1000]
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
        record.status = WarmupStatus.FAILED
        record.error = str(e)[:1000]
        log_activity(
            db,
            SYSTEM,
            "warmup.failed",
            entity_type="mailbox",
            entity_id=mailbox.id,
            after={"helper": helper.email, "error": str(e)},
        )
        db.commit()
        return "warmup_failed"
    record.status = WarmupStatus.SENT
    record.sent_at = at
    record.act_after = at + timedelta(minutes=rng.randint(*ACT_AFTER_MINUTES))
    log_activity(
        db,
        SYSTEM,
        "warmup.sent",
        entity_type="mailbox",
        entity_id=mailbox.id,
        after={"helper": helper.email, "message_id": sent.message_id},
    )
    db.commit()
    return "warmup_sent"


# --- Helper actions -----------------------------------------------------------------------------------------


def helper_failed(db: Session, helper: WarmupHelper, error: str, at: datetime) -> None:
    was = helper.status
    helper.status = MailboxStatus.ERROR
    helper.last_error = error[:1000]
    helper.last_checked_at = at
    if was != MailboxStatus.ERROR:
        log_activity(
            db,
            SYSTEM,
            "warmup.helper_failed",
            entity_type="warmup_helper",
            entity_id=helper.id,
            before={"status": was},
            after={"status": MailboxStatus.ERROR, "error": error[:300]},
        )


def act_for_helper(
    db: Session,
    helper_id: int,
    at: datetime,
    open_inbox: OpenInbox = mailer.Inbox,
    deliver: Deliver = mailer.send,
    rng: random.Random | None = None,
) -> dict[str, int]:
    """Sign in to one helper and handle the warm-up emails that are due. Commits."""
    rng = rng or random.Random()  # noqa: S311 - behaviour variety, not security
    counts = {"inbox": 0, "spam": 0, "missing": 0, "replied": 0, "waiting": 0}
    helper = db.get(WarmupHelper, helper_id)
    if helper is None or helper.status != MailboxStatus.ACTIVE or mailer.development_block(helper):
        return counts
    due = db.scalars(
        select(WarmupMessage)
        .where(
            WarmupMessage.helper_id == helper.id,
            WarmupMessage.status == WarmupStatus.SENT,
            WarmupMessage.acted_at.is_(None),
            WarmupMessage.act_after <= at,
        )
        .order_by(WarmupMessage.act_after)
        .limit(20)
    ).all()
    if not due:
        return counts
    try:
        inbox = open_inbox(helper)
    except mailer.InboxFailed as e:
        helper_failed(db, helper, str(e), at)
        db.commit()
        return counts

    with inbox:
        try:
            spam = helper.spam_folder or inbox.spam_folder()
            helper.spam_folder = spam
            for record in due:
                outcome = _act(db, helper, inbox, spam, record, at, deliver, rng)
                counts[outcome] += 1
                if record.replied_at == at:
                    counts["replied"] += 1
                db.commit()
        except mailer.InboxFailed as e:
            helper_failed(db, helper, str(e), at)
    helper.last_checked_at = at
    if helper.status == MailboxStatus.ACTIVE:
        helper.last_error = None
    db.commit()
    return counts


def _act(
    db: Session,
    helper: WarmupHelper,
    inbox: mailer.Inbox,
    spam: str | None,
    record: WarmupMessage,
    at: datetime,
    deliver: Deliver,
    rng: random.Random,
) -> str:
    code = _CODE.search(record.message_id).group(0)  # type: ignore[union-attr]
    found = inbox.search("INBOX", code)
    landed = LandedIn.INBOX
    if not found and spam:
        in_spam = inbox.search(spam, code)
        if in_spam:
            landed = LandedIn.SPAM
            # "Not spam": the strongest signal a person can give the provider.
            inbox.move(spam, in_spam[0], "INBOX")
            record.rescued = True
            found = inbox.search("INBOX", code)
    if not found:
        if record.sent_at and at - record.sent_at > MISSING_AFTER:
            record.landed_in = LandedIn.MISSING
            record.acted_at = at
            return "missing"
        return "waiting"

    uid = found[0]
    record.landed_in = landed
    inbox.mark_engaged("INBOX", uid)
    if rng.random() < REPLY_SHARE:
        mailbox = db.get(Mailbox, record.mailbox_id)
        if mailbox is not None:
            _reply(helper, mailbox, record, at, deliver, rng)
    if rng.random() < FOLDER_SHARE:
        inbox.move("INBOX", uid, inbox.ensure_folder(HELPER_FOLDER))
        record.moved_to_folder = True
    record.acted_at = at
    return "spam" if landed == LandedIn.SPAM else "inbox"


def _reply(
    helper: WarmupHelper,
    mailbox: Mailbox,
    record: WarmupMessage,
    at: datetime,
    deliver: Deliver,
    rng: random.Random,
) -> None:
    body = warmup_content.reply(rng, helper.display_name, mailbox.display_name)
    subject = record.subject if record.subject.lower().startswith("re:") else f"Re: {record.subject}"
    mid = message_id(helper.email)
    msg = mailer.build_message(
        helper, mailbox.email, subject, body, thread=[record.message_id], message_id=mid
    )
    try:
        deliver(helper, msg)
    except (mailer.SendFailed, mailer.AuthFailed) as e:
        # Not worth failing the rest for: the email was still found and opened.
        log.warning("helper %s couldn't send a warm-up reply: %s", helper.id, e)
        return
    record.replied_at = at
    record.reply_message_id = mid


# --- Filing in our own mailboxes --------------------------------------------------------------------------


def file_mailbox(db: Session, mailbox_id: int, open_inbox: OpenInbox = mailer.Inbox) -> int:
    """Move the helpers' answers out of the mailbox's inbox into its Warmup folder. Returns how many."""
    mailbox = db.get(Mailbox, mailbox_id)
    if mailbox is None or not mailbox.imap_host or mailer.development_block(mailbox):
        return 0
    try:
        with open_inbox(mailbox) as inbox:
            candidates = inbox.search("INBOX", "nxw", headers=("Message-ID", "In-Reply-To", "References"))
            ours = [uid for uid in candidates if is_warmup(inbox.header_text("INBOX", uid))]
            if not ours:
                return 0
            folder = mailbox.warmup_folder or inbox.ensure_folder(WARMUP_FOLDER)
            mailbox.warmup_folder = folder
            # Highest UIDs first, so moving one doesn't change the others.
            for uid in sorted(ours, key=int, reverse=True):
                inbox.move("INBOX", uid, folder)
            db.commit()
            return len(ours)
    except mailer.InboxFailed as e:
        log.warning("couldn't file warm-up mail in mailbox %s: %s", mailbox_id, e)
        return 0


# --- Inbox rates and the low-rate pause ------------------------------------------------------------------


def inbox_rates(db: Session, mailbox_ids: list[int], since: datetime) -> dict[int, dict[str, dict[str, int]]]:
    """{mailbox: {provider: {"inbox": n, "spam": n, "missing": n}}} for warm-up emails since `since`."""
    result: dict[int, dict[str, dict[str, int]]] = {mid: {} for mid in mailbox_ids}
    for mailbox_id, provider, landed, n in db.execute(
        select(WarmupMessage.mailbox_id, WarmupMessage.provider, WarmupMessage.landed_in, func.count())
        .where(
            WarmupMessage.mailbox_id.in_(mailbox_ids),
            WarmupMessage.landed_in.is_not(None),
            WarmupMessage.sent_at >= since,
        )
        .group_by(WarmupMessage.mailbox_id, WarmupMessage.provider, WarmupMessage.landed_in)
    ):
        bucket = result[mailbox_id].setdefault(provider.value, {"inbox": 0, "spam": 0, "missing": 0})
        bucket[landed.value.lower()] += n
    return result


def daily_rates(
    db: Session, mailbox_id: int, days: int, at: datetime, zone: str, after: datetime | None = None
) -> list[tuple[date, int, int]]:
    """(day, landed in inbox, results) for each of the last `days` days, oldest first (only emails sent
    after `after`, if given)."""
    tz = ZoneInfo(zone)
    today = at.astimezone(tz).date()
    first = today - timedelta(days=days - 1)
    day = func.date(func.timezone(zone, WarmupMessage.sent_at))
    rows = {
        d: (inbox, total)
        for d, inbox, total in db.execute(
            select(day, func.count().filter(WarmupMessage.landed_in == LandedIn.INBOX), func.count())
            .where(
                WarmupMessage.mailbox_id == mailbox_id,
                WarmupMessage.landed_in.is_not(None),
                WarmupMessage.sent_at >= max(filter(None, [datetime.combine(first, time(0), tz), after])),
            )
            .group_by(day)
        )
    }
    return [(d, *rows.get(d, (0, 0))) for d in (first + timedelta(days=i) for i in range(days))]


def low_rate(db: Session, mailbox: Mailbox, at: datetime, zone: str) -> bool:
    """Under 50% for each of the last 3 days that had enough results (looking back up to a week)."""
    days = daily_rates(db, mailbox.id, 7, at, zone, after=mailbox.campaigns_resumed_at)
    measured = [(inbox, total) for _, inbox, total in days if total >= MIN_RESULTS_PER_DAY]
    recent = measured[-LOW_RATE_DAYS:]
    return len(recent) == LOW_RATE_DAYS and all(inbox / total < LOW_RATE for inbox, total in recent)


def check_health(db: Session, at: datetime) -> list[int]:
    """Alert on (and pause campaign sending for) warm-up mailboxes whose inbox rate stayed low. Returns the
    mailboxes this paused."""
    from app.services import alerts  # alerts emails through the invite mailbox; keep imports one-way

    zone = get_schedule(db).timezone
    conditions = [
        alerts.Condition(
            f"{mailbox.email}: under {LOW_RATE:.0%} of warm-up emails reached the inbox on each of the last "
            f"{LOW_RATE_DAYS} days. Campaign emails from it are paused.",
            mailbox_id=mailbox.id,
            pause=(mailbox.id,),
        )
        for mailbox in db.scalars(
            select(Mailbox).where(Mailbox.warmup_enabled.is_(True), Mailbox.archived_at.is_(None))
        )
        if low_rate(db, mailbox, at, zone)
    ]
    new = alerts.sync(db, "warmup_low", conditions, at)
    db.commit()
    alerts.email_admins(db, new, at)
    return [mid for a in new for mid in a.paused_mailbox_ids]


# --- Worker entry points -----------------------------------------------------------------------------------


def now() -> datetime:
    return datetime.now(UTC)


def run_helpers() -> None:
    with SessionLocal() as db:
        helper_ids = db.scalars(
            select(WarmupHelper.id).where(WarmupHelper.status == MailboxStatus.ACTIVE)
        ).all()
    for helper_id in helper_ids:
        try:
            with SessionLocal() as db:
                counts = act_for_helper(db, helper_id, now())
            if any(counts.values()):
                log.info("warm-up helper %s: %s", helper_id, counts)
        except Exception:
            log.exception("warm-up failed for helper %s", helper_id)


def filing_mailbox_ids(db: Session, at: datetime) -> list[int]:
    """Mailboxes whose inbox may hold helper answers: warm-up is on, or it sent warm-up mail lately (answers
    still coming in after it was switched off). Never one whose login fails: trying every 5 minutes with a
    wrong password can get the server's IP blocked (cPanel's cPHulk)."""
    recent = select(WarmupMessage.mailbox_id).where(WarmupMessage.sent_at >= at - FILING_AFTER_OFF)
    return list(
        db.scalars(
            select(Mailbox.id)
            .where(
                Mailbox.imap_host.is_not(None),
                Mailbox.archived_at.is_(None),
                Mailbox.status != MailboxStatus.ERROR,
                (Mailbox.warmup_enabled.is_(True)) | Mailbox.id.in_(recent),
            )
            .order_by(Mailbox.id)
        )
    )


def run_filing() -> None:
    with SessionLocal() as db:
        mailbox_ids = filing_mailbox_ids(db, datetime.now(UTC))
    for mailbox_id in mailbox_ids:
        try:
            with SessionLocal() as db:
                file_mailbox(db, mailbox_id)
        except Exception:
            log.exception("warm-up filing failed for mailbox %s", mailbox_id)


def run_health() -> None:
    with SessionLocal() as db:
        paused = check_health(db, now())
    if paused:
        log.warning("paused campaign sending for mailbox(es) %s: low warm-up inbox rate", paused)
