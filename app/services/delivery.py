"""What happened to each campaign email after it left (plan section 19).

Three sources write events: the send itself (our mail server's answer), cPanel's delivery log
(app.services.cpanel) and bounce emails found in the inbox (app.services.replies). Each event is a line in
the mail-server log viewer, and the strongest status so far is kept on the email itself
(`delivery_status` on campaign_recipients / follow_up_emails), for the per-mailbox numbers.

A hard bounce (the address doesn't exist) puts the address on do-not-contact.
"""

import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import (
    CampaignRecipient,
    DeliveryEvent,
    DeliverySource,
    DeliveryStatus,
    DncKind,
    DoNotContact,
    FollowUpEmail,
)
from app.models.deliverability import STATUS_WEIGHT
from app.services.audit import SYSTEM, log_activity

HARD_BOUNCE_REASON = "Email address doesn't exist (hard bounce)"

# Exim's "250 OK id=1tAbCd-000123-Xy" (cPanel servers): the queue ID that cPanel's log uses too.
_QUEUE_ID = re.compile(r"\bid=([A-Za-z0-9]{4,12}-[A-Za-z0-9]{4,12}-[A-Za-z0-9]{2,8})\b")
_STATUS_CODE = re.compile(r"\b([245])\.(\d{1,3})\.(\d{1,3})\b")
# Wording that means the address itself is dead, whatever the code.
_DEAD_ADDRESS = re.compile(
    r"user unknown|unknown user|no such user|no such (mailbox|recipient)|does not exist|doesn't exist|"
    r"invalid (recipient|mailbox|address)|recipient (address )?rejected|recipient not found|"
    r"address not found|mailbox not found|account (has been )?disabled|account is disabled|"
    r"mailbox (is )?unavailable|unrouteable address|domain not found|host or domain name not found",
    re.IGNORECASE,
)
# Wording that means it may work later, or that the problem is us (spam filters, limits), not the address.
_MAY_PASS = re.compile(
    r"mailbox (is )?full|over quota|quota exceeded|insufficient storage|try again|temporar|"
    r"spam|blocked|blacklist|block ?list|policy|reputation|rate limit|too many|unsolicited|dmarc|spf|dkim",
    re.IGNORECASE,
)


def queue_id(response: str | None) -> str | None:
    match = _QUEUE_ID.search(response or "")
    return match.group(1) if match else None


def status_code(text: str | None) -> str | None:
    """The enhanced status code in a server reply or bounce ("5.1.1"), if any."""
    match = _STATUS_CODE.search(text or "")
    return ".".join(match.groups()) if match else None


def bounce_kind(text: str, code: str | None = None) -> DeliveryStatus:
    """HARD_BOUNCE when the address doesn't exist, else SOFT_BOUNCE (it may work later, or the refusal was
    about us, not them: a spam block must never put a real person on do-not-contact)."""
    code = code or status_code(text)
    if _MAY_PASS.search(text or "") or (code or "").startswith("4."):
        return DeliveryStatus.SOFT_BOUNCE
    if (code or "").startswith(("5.1.", "5.2.1")) or _DEAD_ADDRESS.search(text or ""):
        return DeliveryStatus.HARD_BOUNCE
    return DeliveryStatus.SOFT_BOUNCE


def _apply(row: CampaignRecipient | FollowUpEmail | None, status: DeliveryStatus) -> None:
    if row is None or status == DeliveryStatus.SENT:
        return
    current = DeliveryStatus(row.delivery_status) if row.delivery_status else None
    if current is None or STATUS_WEIGHT[status] >= STATUS_WEIGHT[current]:
        row.delivery_status = status.value


def record(
    db: Session,
    *,
    source: DeliverySource,
    status: DeliveryStatus,
    at: datetime,
    recipient: CampaignRecipient | None = None,
    follow_up: FollowUpEmail | None = None,
    email: str | None = None,
    mailbox_id: int | None = None,
    response: str | None = None,
    remote_host: str | None = None,
    queue: str | None = None,
    external_id: str | None = None,
) -> bool:
    """Add one event and update the email's status. Returns False when external_id was already recorded."""
    values = {
        "source": source,
        "status": status,
        "occurred_at": at,
        "recipient_id": recipient.id if recipient else None,
        "follow_up_id": follow_up.id if follow_up else None,
        "campaign_id": recipient.campaign_id if recipient else (follow_up.campaign_id if follow_up else None),
        "email": email if email is not None else (recipient.email if recipient else ""),
        "mailbox_id": mailbox_id
        if mailbox_id is not None
        else ((follow_up.mailbox_id if follow_up else None) or (recipient.mailbox_id if recipient else None)),
        "response": (response or "")[:2000] or None,
        "remote_host": (remote_host or "")[:255] or None,
        "queue_id": (queue or queue_id(response) or "")[:100] or None,
        "external_id": external_id[:300] if external_id else None,
    }
    stmt = insert(DeliveryEvent).values(**values)
    if external_id:
        stmt = stmt.on_conflict_do_nothing(index_elements=[DeliveryEvent.external_id])
    added = db.scalar(stmt.returning(DeliveryEvent.id))
    if added is None:
        return False
    _apply(follow_up if follow_up is not None else recipient, status)
    return True


def bounced(db: Session, recipient: CampaignRecipient, status: DeliveryStatus, source: str) -> None:
    """After a bounce event: stop follow-ups, and put a dead address on do-not-contact. The caller commits."""
    from app.services import sending  # sending records events through this module

    if recipient.follow_up_stop != "bounced":
        sending.stop_follow_ups(db, recipient, "bounced")
        recipient.follow_up_stop = "bounced"
        log_activity(
            db,
            SYSTEM,
            "email.bounced",
            entity_type="lead",
            entity_id=recipient.lead_id,
            after={
                "campaign_id": recipient.campaign_id,
                "hard": status == DeliveryStatus.HARD_BOUNCE,
                "source": source,
            },
        )
    if status != DeliveryStatus.HARD_BOUNCE or not recipient.email or recipient.lead_id is None:
        return
    added = db.scalar(
        insert(DoNotContact)
        .values(value=recipient.email, kind=DncKind.EMAIL, reason=HARD_BOUNCE_REASON)
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
            after={"reason": "hard_bounce", "campaign_id": recipient.campaign_id},
        )


def timeline(db: Session, recipient_id: int) -> list[DeliveryEvent]:
    return list(
        db.scalars(
            select(DeliveryEvent)
            .where(DeliveryEvent.recipient_id == recipient_id)
            .order_by(DeliveryEvent.occurred_at, DeliveryEvent.id)
        )
    )
