"""Handing interested replies to Deals as leads (integration step 4).

A handover is queued when a reply scores auto (80+) with handover on, when someone presses Send to Deals
(approved from the review list, or manual under the cut-off), or when someone already handed over writes again
(repeat). One handover per reply (unique reply_id, Idempotency-Key "handover:<reply id>"), so a reply is
never sent twice. The worker sends queued ones every minute; Send to Deals also tries at once.

The payload (contract 4.5) holds the person, the lead list and its source, the campaign, the mailbox, the
score and the whole thread as plain text. It is saved with the first try so a retry repeats it exactly.

Deals answers:
- created / existing: the person is "Handed to Deals": every follow-up to them stops in every campaign, they
  never join a campaign again, and their lead link is saved ("Open in Deals").
- blocked: they opted out in Deals. They go on do-not-contact here (not sent back to Deals) and nothing else.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models import (
    Campaign,
    CampaignRecipient,
    DealsHandover,
    DncKind,
    DoNotContact,
    FollowUpEmail,
    FollowUpStatus,
    Lead,
    LeadGroup,
    Mailbox,
    RecipientStatus,
    Reply,
    ReplyAnswer,
    User,
    campaign_groups,
    lead_group_members,
)
from app.services import deals, geo
from app.services.audit import SYSTEM, Actor, log_activity
from app.services.deals_client import DealsError
from app.services.deals_optouts import FROM_DEALS_REASON, GIVE_UP_AFTER, RETRY_MINUTES

log = logging.getLogger(__name__)

HANDOVER = "handover"
MAX_THREAD = 20
MAX_TEXT = 20_000
# Extra fields that hold what the contract has its own field for.
TITLE_KEYS = ("title", "job_title", "jobtitle", "position", "role", "designation")
PHONE_KEYS = ("phone", "phone_number", "mobile", "telephone", "tel", "cell")
WEBSITE_KEYS = ("website", "company_website", "url", "web", "domain", "site")


def enabled(db: Session) -> bool:
    return deals.settings(db).handover_enabled


def queue(
    db: Session,
    reply: Reply,
    band: str,
    at: datetime,
    approved_by: User | None = None,
    actor: Actor = SYSTEM,
) -> DealsHandover | None:
    """Queue a reply for Deals (once). Returns the handover, or None when the reply can't go (no campaign
    email behind it). The caller commits."""
    if reply.recipient_id is None:
        return None
    existing = db.scalar(select(DealsHandover).where(DealsHandover.reply_id == reply.id))
    if existing is not None:
        return existing
    recipient = db.get(CampaignRecipient, reply.recipient_id)
    handover = DealsHandover(
        reply_id=reply.id,
        recipient_id=reply.recipient_id,
        lead_id=recipient.lead_id if recipient else None,
        band=band,
        approved_by_id=approved_by.id if approved_by else None,
        approved_by_name=approved_by.name if approved_by else None,
        status="queued",
        attempts=0,
        created_at=at,
    )
    db.add(handover)
    db.flush()
    log_activity(
        db,
        actor,
        "deals.handover_queued",
        entity_type="reply",
        entity_id=reply.id,
        after={"band": band, "score": reply.score},
    )
    return handover


# --- The payload --------------------------------------------------------------------------------------------


def _iso(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if value else ""


def _first(custom: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = custom.get(key)
        if value:
            return str(value)
    return ""


def _step(db: Session, reply: Reply, recipient: CampaignRecipient) -> str:
    """Which email they answered: "opening" or "follow-up N" (by thread headers, else the latest sent)."""
    refs = f"{reply.references or ''}"
    sent = db.scalars(
        select(FollowUpEmail)
        .where(FollowUpEmail.recipient_id == recipient.id, FollowUpEmail.status == FollowUpStatus.SENT)
        .order_by(FollowUpEmail.step)
    ).all()
    for follow_up in reversed(sent):
        if follow_up.message_id and follow_up.message_id in refs:
            return f"follow-up {follow_up.step}"
    if recipient.message_id and recipient.message_id in refs:
        return "opening"
    earlier = [f for f in sent if f.sent_at and f.sent_at <= reply.received_at]
    return f"follow-up {earlier[-1].step}" if earlier else "opening"


def _thread(db: Session, recipient: CampaignRecipient, mailbox: Mailbox | None) -> list[dict[str, str]]:
    """The campaign emails actually sent to this person, their replies and our answers: plain text, oldest
    first, the newest 20."""
    ours = mailbox.email if mailbox else ""
    items: list[tuple[datetime, dict[str, str]]] = []

    def add(
        direction: str, at: datetime | None, subject: str | None, text: str | None, frm: str, to: str
    ) -> None:
        if at is None:
            return
        items.append(
            (
                at,
                {
                    "direction": direction,
                    "from": frm,
                    "to": to,
                    "subject": subject or "",
                    "sent_at": _iso(at),
                    "text": (text or "")[:MAX_TEXT],
                },
            )
        )

    add("out", recipient.sent_at, recipient.subject_sent, recipient.body_sent, ours, recipient.email)
    for f in db.scalars(
        select(FollowUpEmail).where(
            FollowUpEmail.recipient_id == recipient.id, FollowUpEmail.status == FollowUpStatus.SENT
        )
    ):
        add("out", f.sent_at, f.subject_sent, f.body_sent, ours, recipient.email)
    for r in db.scalars(select(Reply).where(Reply.recipient_id == recipient.id)):
        add("in", r.received_at, r.subject, r.full_text, r.from_email, ours)
        answers = select(ReplyAnswer).where(ReplyAnswer.reply_id == r.id, ReplyAnswer.status == "SENT")
        for a in db.scalars(answers):
            add("out", a.created_at, a.subject, a.body, ours, a.to_email)
    items.sort(key=lambda pair: pair[0])
    return [item for _, item in items[-MAX_THREAD:]]


def _lead_list(db: Session, lead: Lead | None, campaign: Campaign | None) -> dict[str, str]:
    """The group this person came from: one of the campaign's groups, else any of theirs."""
    if lead is None:
        return {"name": "", "source": "unknown"}
    query = (
        select(LeadGroup)
        .join(lead_group_members, lead_group_members.c.group_id == LeadGroup.id)
        .where(lead_group_members.c.lead_id == lead.id)
    )
    group = None
    if campaign is not None:
        group = db.scalar(
            query.join(campaign_groups, campaign_groups.c.group_id == LeadGroup.id)
            .where(campaign_groups.c.campaign_id == campaign.id)
            .limit(1)
        )
    group = group or db.scalar(query.order_by(LeadGroup.id).limit(1))
    if group is None:
        return {"name": "", "source": "unknown"}
    return {"name": group.name, "source": group.source or "unknown"}


def build_payload(db: Session, handover: DealsHandover) -> dict[str, Any]:
    reply = db.get(Reply, handover.reply_id)
    recipient = db.get(CampaignRecipient, handover.recipient_id) if handover.recipient_id else None
    if reply is None or recipient is None:
        raise DealsError("invalid", "The reply or the campaign email behind it is gone.")
    lead = db.get(Lead, recipient.lead_id) if recipient.lead_id else None
    campaign = db.get(Campaign, recipient.campaign_id)
    mailbox = db.get(Mailbox, reply.mailbox_id)
    custom = dict(lead.custom or {}) if lead else {}
    used = set(TITLE_KEYS + PHONE_KEYS + WEBSITE_KEYS)
    prospect: dict[str, Any] = {
        "email": recipient.email,
        "first_name": (lead.first_name if lead else None) or "",
        "last_name": (lead.last_name if lead else None) or "",
        "company": (lead.company if lead else None) or "",
        "title": _first(custom, TITLE_KEYS),
        "phone": _first(custom, PHONE_KEYS),
        "country": geo.country_code(lead.country) if lead and lead.country else "",
        "timezone": (lead.timezone if lead else None) or "",
        "website": _first(custom, WEBSITE_KEYS),
        "extra": {k: str(v) for k, v in custom.items() if k not in used and v not in (None, "")},
    }
    base = get_settings().app_base_url.rstrip("/")
    return {
        "reply_id": f"rpl_{reply.id}",
        "prospect": {k: v for k, v in prospect.items() if v not in ("", None)},
        "lead_list": _lead_list(db, lead, campaign),
        "campaign": {
            "id": f"cmp_{campaign.id}" if campaign else "",
            "name": campaign.name if campaign else "",
            "deals_service": campaign.deals_service_key if campaign else None,
            "step": _step(db, reply, recipient),
        },
        "mailbox": mailbox.email if mailbox else "",
        "score": {
            "value": reply.score or 0,
            "band": handover.band,
            "rules": list(reply.score_rules or []),
            "approved_by": handover.approved_by_name if handover.band in ("approved", "manual") else None,
        },
        "thread": _thread(db, recipient, mailbox),
        "nexareach_url": f"{base}/replies?open={reply.id}",
    }


def reply_id_from_deals(value: str | None) -> int | None:
    if value and value.startswith("rpl_") and value[4:].isdigit():
        return int(value[4:])
    return None


# --- Sending ------------------------------------------------------------------------------------------------


def send(db: Session, handover: DealsHandover, at: datetime) -> str:
    """One try. Returns the handover's status. Commits."""
    try:
        if handover.payload is None:
            handover.payload = build_payload(db, handover)
            db.commit()  # fixed before the first try: a retry repeats it exactly
        answer = deals.client(db).post("/handover", handover.payload, f"handover:{handover.reply_id}")
    except DealsError as e:
        _failed(db, handover, e, at)
        return handover.status
    status = str(answer.get("status") or "")
    if status not in ("created", "existing", "blocked"):
        _failed(
            db, handover, DealsError("unavailable", f"Deals answered an unknown status ({status!r})."), at
        )
        return handover.status
    handover.status = status
    handover.completed_at = at
    handover.last_error = None
    handover.next_attempt_at = None
    if status == "blocked":
        _blocked(db, handover)
    else:
        handover.deals_lead_id = str(answer.get("lead_id") or "") or None
        handover.deals_lead_url = str(answer.get("lead_url") or "") or None
        handed_over(db, handover, at)
        if status == "existing":
            log.warning(
                "handover of reply %s: already a lead in Deals (%s)",
                handover.reply_id,
                handover.deals_lead_id,
            )
    log_activity(
        db,
        SYSTEM,
        f"deals.handover_{status}",
        entity_type="reply",
        entity_id=handover.reply_id,
        after={"band": handover.band, "lead_id": handover.deals_lead_id, "lead_url": handover.deals_lead_url},
    )
    deals.problem_alerts(db, None, at, target=HANDOVER)
    db.commit()
    return status


def handed_over(db: Session, handover: DealsHandover, at: datetime) -> None:
    """The person is a Deals lead now: stop everything NexaReach would still send them, in every campaign."""
    recipient = db.get(CampaignRecipient, handover.recipient_id) if handover.recipient_id else None
    if recipient is None:
        return
    from app.services import sending  # sending imports deals; keep this one-way at import time

    if recipient.status == RecipientStatus.SENT:
        recipient.status = RecipientStatus.HANDED_TO_DEALS
    lead = db.get(Lead, recipient.lead_id) if recipient.lead_id else None
    if lead is not None:
        lead.handed_to_deals_at = lead.handed_to_deals_at or at
        lead.deals_lead_id = handover.deals_lead_id or lead.deals_lead_id
        lead.deals_lead_url = handover.deals_lead_url or lead.deals_lead_url
    same_person = select(CampaignRecipient).where(
        CampaignRecipient.email == recipient.email,
        or_(
            CampaignRecipient.next_follow_up_at.is_not(None),
            CampaignRecipient.status == RecipientStatus.WAITING,
        ),
    )
    for other in db.scalars(same_person).all():
        if other.status == RecipientStatus.WAITING:
            other.status = RecipientStatus.BLOCKED_BY_DEALS
            other.skip_reason = "lead"
        sending.stop_follow_ups(db, other, "handed_to_deals")
    reply = db.get(Reply, handover.reply_id)
    if reply is not None and reply.review_status == "pending":
        reply.review_status = "approved"


def _blocked(db: Session, handover: DealsHandover) -> None:
    """They opted out in Deals: do-not-contact here too, and not shared back (Deals told us)."""
    recipient = db.get(CampaignRecipient, handover.recipient_id) if handover.recipient_id else None
    if recipient is None or not recipient.email or recipient.lead_id is None:
        return
    from app.services import sending

    sending.stop_follow_ups(db, recipient, "do_not_contact")
    added = db.scalar(
        insert(DoNotContact)
        .values(value=recipient.email, kind=DncKind.EMAIL, reason=FROM_DEALS_REASON)
        .on_conflict_do_nothing(index_elements=[DoNotContact.value])
        .returning(DoNotContact.value)
    )
    if added:
        log_activity(
            db,
            SYSTEM,
            "dnc.added",
            entity_type="dnc",
            entity_id=added,
            after={"reason": "opted out in Deals"},
        )


def _failed(db: Session, handover: DealsHandover, e: DealsError, at: datetime) -> None:
    handover.attempts += 1
    handover.last_error = str(e)[:1000]
    failing = None
    if not e.retryable or at - handover.created_at >= GIVE_UP_AFTER:
        handover.status = "failed"
        handover.next_attempt_at = None
        failing = f"A reply couldn't be handed to Deals: {e}"
        log_activity(
            db,
            SYSTEM,
            "deals.handover_failed",
            entity_type="reply",
            entity_id=handover.reply_id,
            after={"error": str(e), "kind": e.kind, "attempts": handover.attempts},
        )
    else:
        if e.kind == "rate_limited" and e.retry_after:
            wait = timedelta(seconds=e.retry_after)
        else:
            minutes = RETRY_MINUTES[handover.attempts - 1] if handover.attempts <= len(RETRY_MINUTES) else 60
            wait = timedelta(minutes=minutes)
        handover.next_attempt_at = at + wait
        if handover.attempts >= len(RETRY_MINUTES):
            failing = (
                f"Handovers can't reach Deals (tried {handover.attempts} times): {e} Still retrying hourly."
            )
    deals.problem_alerts(db, e, at, target=HANDOVER, failing=failing)
    db.commit()


def run(db: Session, at: datetime) -> int:
    """Send every queued handover that is due. Returns how many were tried. Commits."""
    if not enabled(db) or not deals.settings(db).secret:
        return 0
    tried = 0
    while True:
        handover = db.scalar(
            select(DealsHandover)
            .where(
                DealsHandover.status == "queued",
                or_(DealsHandover.next_attempt_at.is_(None), DealsHandover.next_attempt_at <= at),
            )
            .order_by(DealsHandover.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if handover is None:
            return tried
        send(db, handover, at)
        tried += 1


def run_due() -> None:
    """Worker entry point (every minute)."""
    with SessionLocal() as db:
        run(db, datetime.now(UTC))


def status_counts(db: Session) -> dict[str, int]:
    rows = db.execute(select(DealsHandover.status, func.count()).group_by(DealsHandover.status)).all()
    return {status: n for status, n in rows}


def is_handed(db: Session, email: str) -> bool:
    return (
        db.scalar(select(Lead.id).where(Lead.email == email, Lead.handed_to_deals_at.is_not(None)).limit(1))
        is not None
    )
