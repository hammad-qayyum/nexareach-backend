"""Deciding duplicate imports (plan section 13): keep new, keep old or keep both."""

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import DuplicateStatus, Lead, LeadDuplicate, lead_group_members
from app.services.audit import Actor, changes, log_activity

LEAD_FIELDS = ["first_name", "last_name", "company", "country", "timezone"]


class Decision(enum.StrEnum):
    KEEP_NEW = "KEEP_NEW"
    KEEP_OLD = "KEEP_OLD"
    KEEP_BOTH = "KEEP_BOTH"


RESULT = {
    Decision.KEEP_NEW: DuplicateStatus.KEPT_NEW,
    Decision.KEEP_OLD: DuplicateStatus.KEPT_OLD,
    Decision.KEEP_BOTH: DuplicateStatus.KEPT_BOTH,
}


def lead_snapshot(lead: Lead) -> dict[str, Any]:
    return {**{f: getattr(lead, f) for f in LEAD_FIELDS}, "custom": dict(lead.custom or {})}


def resolve(db: Session, dup: LeadDuplicate, decision: Decision, actor: Actor, user_id: int | None) -> Lead:
    """Apply one decision and add the resulting lead to the duplicate's group. The caller commits."""
    existing = dup.existing_lead
    if decision == Decision.KEEP_BOTH:
        lead = Lead(
            email=dup.email,
            first_name=dup.first_name,
            last_name=dup.last_name,
            company=dup.company,
            country=dup.country,
            timezone=dup.timezone,
            # Same address, so the same mail host.
            email_provider=existing.email_provider,
            custom=dict(dup.custom or {}),
        )
        db.add(lead)
        db.flush()
    else:
        lead = existing
        if decision == Decision.KEEP_NEW:
            before = lead_snapshot(lead)
            # Values in the new row win; a blank in the new row keeps what was there.
            for field in LEAD_FIELDS:
                value = getattr(dup, field)
                if value:
                    setattr(lead, field, value)
            lead.custom = {**(lead.custom or {}), **(dup.custom or {})}
            after = lead_snapshot(lead)
            if before != after:
                old, new = changes(before, after)
                log_activity(
                    db, actor, "lead.updated", entity_type="lead", entity_id=lead.id, before=old, after=new
                )

    db.execute(
        insert(lead_group_members).values(group_id=dup.group_id, lead_id=lead.id).on_conflict_do_nothing()
    )
    dup.status = RESULT[decision]
    dup.result_lead_id = lead.id
    dup.decided_by_id = user_id
    dup.decided_at = datetime.now(UTC)
    log_activity(
        db,
        actor,
        "duplicate.resolved",
        entity_type="lead",
        entity_id=existing.id,
        before={"status": DuplicateStatus.PENDING},
        after={"decision": decision, "group_id": dup.group_id, "email": dup.email, "result_lead_id": lead.id},
    )
    return lead
