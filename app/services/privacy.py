"""GDPR requests for one person, by email: export everything we hold, or forget them.

Forgetting deletes the person's leads and pending duplicate rows, and strips their address and the email
text from campaign history (the rows stay so sending counts and reports still add up). A keyed hash of
the address goes on the do-not-contact list, so they are never emailed or imported again without us
keeping the address itself. The activity log can't be changed; its entries refer to leads by id.
"""

import hashlib
import hmac
from datetime import datetime
from typing import Any

from sqlalchemy import delete, distinct, exists, func, or_, select, update
from sqlalchemy.orm import Session

from app.core.crypto import _key
from app.models import (
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    DncKind,
    DoNotContact,
    DuplicateStatus,
    Lead,
    LeadDuplicate,
    LeadGroup,
    Mailbox,
    RecipientStatus,
    lead_group_members,
)
from app.services.audit import Actor, log_activity
from app.services.auth import normalize_email

HASH_PREFIX = "forgotten:"
FORGOTTEN_REASON = "Forgotten on request (GDPR). Only a hash of the address is kept."


class PersonBusy(Exception):
    """An email to this person is being handed to the mail server right now."""


def email_hash(email: str) -> str:
    """Keyed (HMAC-SHA256) so the stored value can't be reversed by guessing addresses offline."""
    digest = hmac.new(_key(), normalize_email(email).encode(), hashlib.sha256).hexdigest()
    return HASH_PREFIX + digest[:48]


def is_forgotten(db: Session, email: str) -> bool:
    return bool(
        db.scalar(
            select(
                exists().where(DoNotContact.kind == DncKind.EMAIL, DoNotContact.value == email_hash(email))
            )
        )
    )


def forgotten_among(db: Session, emails: list[str]) -> set[str]:
    """The emails in this list whose person asked to be forgotten."""
    hashes = {email_hash(e): e for e in emails}
    found: set[str] = set()
    keys = list(hashes)
    for start in range(0, len(keys), 1000):
        found.update(
            db.scalars(
                select(DoNotContact.value).where(
                    DoNotContact.kind == DncKind.EMAIL, DoNotContact.value.in_(keys[start : start + 1000])
                )
            )
        )
    return {hashes[h] for h in found}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def export_person(db: Session, email: str) -> dict[str, Any]:
    email = normalize_email(email)
    leads = db.scalars(select(Lead).where(Lead.email == email).order_by(Lead.id)).all()
    lead_ids = [lead.id for lead in leads]

    groups: dict[int, list[dict[str, Any]]] = {lid: [] for lid in lead_ids}
    for lead_id, group, added_at in db.execute(
        select(lead_group_members.c.lead_id, LeadGroup, lead_group_members.c.added_at)
        .join(LeadGroup, LeadGroup.id == lead_group_members.c.group_id)
        .where(lead_group_members.c.lead_id.in_(lead_ids))
        .order_by(lead_group_members.c.added_at)
    ):
        groups[lead_id].append({"group": group.name, "source": group.source, "added_at": _iso(added_at)})

    emails = db.execute(
        select(CampaignRecipient, Campaign.name, Mailbox.email)
        .join(Campaign, Campaign.id == CampaignRecipient.campaign_id)
        .outerjoin(Mailbox, Mailbox.id == CampaignRecipient.mailbox_id)
        .where(or_(CampaignRecipient.email == email, CampaignRecipient.lead_id.in_(lead_ids)))
        .order_by(CampaignRecipient.id)
    ).all()
    pending = db.execute(
        select(LeadDuplicate, LeadGroup.name)
        .join(LeadGroup, LeadGroup.id == LeadDuplicate.group_id)
        .where(LeadDuplicate.email == email, LeadDuplicate.status == DuplicateStatus.PENDING)
    ).all()
    # A forgotten person is listed by the hash of their address only.
    dnc = db.scalar(
        select(DoNotContact).where(
            DoNotContact.kind == DncKind.EMAIL, DoNotContact.value.in_([email, email_hash(email)])
        )
    )

    return {
        "email": email,
        "exported_at": datetime.now().astimezone().isoformat(),
        "leads": [
            {
                "id": lead.id,
                "first_name": lead.first_name,
                "last_name": lead.last_name,
                "company": lead.company,
                "country": lead.country,
                "timezone": lead.timezone,
                "email_provider": lead.email_provider,
                "extra_fields": lead.custom,
                "added_at": _iso(lead.created_at),
                "groups": groups[lead.id],
            }
            for lead in leads
        ],
        "emails": [
            {
                "campaign": campaign_name,
                "status": r.status,
                "from": mailbox_email,
                "sent_at": _iso(r.sent_at),
                "subject": r.subject_sent,
                "body": r.body_sent,
            }
            for r, campaign_name, mailbox_email in emails
        ],
        "waiting_duplicates": [
            {
                "group": group_name,
                "first_name": d.first_name,
                "last_name": d.last_name,
                "company": d.company,
                "country": d.country,
                "extra_fields": d.custom,
                "added_at": _iso(d.created_at),
            }
            for d, group_name in pending
        ],
        "do_not_contact": {"reason": dnc.reason, "since": _iso(dnc.created_at)} if dnc else None,
        "forgotten": bool(dnc and dnc.value.startswith(HASH_PREFIX)),
    }


def forget_person(db: Session, email: str, actor: Actor, at: datetime) -> dict[str, int]:
    """Erase one person. The caller commits."""
    from app.services import sending  # sending imports this module

    email = normalize_email(email)
    hashed = email_hash(email)
    lead_ids = list(db.scalars(select(Lead.id).where(Lead.email == email)))
    theirs = or_(CampaignRecipient.email == email, CampaignRecipient.lead_id.in_(lead_ids))

    if db.scalar(select(exists().where(theirs, CampaignRecipient.status == RecipientStatus.SENDING))):
        raise PersonBusy

    campaigns = db.scalar(select(func.count(distinct(CampaignRecipient.campaign_id))).where(theirs)) or 0
    affected = list(
        db.scalars(
            select(CampaignRecipient.campaign_id).where(
                theirs, CampaignRecipient.status == RecipientStatus.WAITING
            )
        )
    )
    db.execute(
        update(CampaignRecipient)
        .where(theirs, CampaignRecipient.status == RecipientStatus.WAITING)
        .values(status=RecipientStatus.SKIPPED, skip_reason="forgotten", next_attempt_at=None)
        .execution_options(synchronize_session=False)
    )
    history = db.execute(
        update(CampaignRecipient)
        .where(theirs)
        .values(
            lead_id=None,
            email=hashed,
            subject_sent=None,
            body_sent=None,
            html_sent=None,
            smtp_response=None,
            error=None,
            message_id=None,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    duplicates = db.execute(
        delete(LeadDuplicate)
        .where(
            or_(
                LeadDuplicate.email == email,
                LeadDuplicate.existing_lead_id.in_(lead_ids),
                LeadDuplicate.result_lead_id.in_(lead_ids),
            )
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    # Group memberships and campaign exclusions go with the leads (ON DELETE CASCADE).
    leads = db.execute(
        delete(Lead).where(Lead.id.in_(lead_ids)).execution_options(synchronize_session=False)
    ).rowcount
    db.execute(delete(DoNotContact).where(DoNotContact.kind == DncKind.EMAIL, DoNotContact.value == email))
    if not db.scalar(select(exists().where(DoNotContact.value == hashed))):
        db.add(
            DoNotContact(
                value=hashed, kind=DncKind.EMAIL, reason=FORGOTTEN_REASON, created_by_id=actor.user_id
            )
        )
    db.expire_all()

    for campaign in db.scalars(
        select(Campaign).where(Campaign.id.in_(set(affected)), Campaign.status == CampaignStatus.RUNNING)
    ):
        sending._finish_if_done(db, campaign, at)

    result = {
        "leads": leads,
        "campaigns": campaigns,
        "campaign_emails": history,
        "duplicate_rows": duplicates,
    }
    log_activity(db, actor, "person.forgotten", entity_type="person", entity_id=hashed, after=result)
    return result
