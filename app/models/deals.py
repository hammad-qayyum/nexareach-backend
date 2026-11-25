"""The connection to Deals, the company's sales CRM (deals.fastnexa.com).

Step 1: Deals' do-not-email list. Deals shares SHA-256 hashes of addresses NexaReach must never cold-email
(leads, clients, Lost for 6 months, Meta ad leads, opt-outs); NexaReach keeps a copy here and checks it at
import, at campaign Start and before every send (app.services.deals).
"""

from datetime import datetime

from sqlalchemy import CHAR, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DealsBlock(Base):
    """One hash on Deals' do-not-email list. Active while `until` is empty or still ahead."""

    __tablename__ = "deals_blocklist"

    hash: Mapped[str] = mapped_column(CHAR(64), primary_key=True)
    # lead, client, lost, meta_lead, opted_out, bounced, complained or forgotten (Deals merged several
    # reasons into the strongest one).
    reason: Mapped[str] = mapped_column(String(20))
    # Set for "lost": the day the person may be emailed again. Deals sends no event when it passes.
    until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DealsSyncState(Base):
    """Where each sync with Deals stands: "blocklist" (later also "events" and "optouts")."""

    __tablename__ = "deals_sync_state"

    name: Mapped[str] = mapped_column(String(30), primary_key=True)
    # Where the next delta starts (Deals' opaque cursor).
    cursor: Mapped[str | None] = mapped_column(Text)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_full_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(1000))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DealsOptoutBatch(Base):
    """One POST /optouts to Deals: up to 500 opt-outs. The exact items are kept, so a retry sends the same
    Idempotency-Key with the same body (step 2)."""

    __tablename__ = "deals_optout_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # uuid4; key "optouts:<id>"
    items: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(1000))
    # Stopped for good: Deals refused the data (400/409), or 3 days of failures.
    gave_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Replaced before it got through (a person in it was forgotten): its rows went into a new batch.
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class DealsOptout(Base):
    """A do-not-contact entry to share with Deals (step 2): opt-out reply, hard bounce, complaint, added by
    hand, or GDPR forget. Written in the same transaction as the entry."""

    __tablename__ = "deals_optout_outbox"
    __table_args__ = (Index("ix_deals_optout_outbox_unsent", "batch_id", "sent_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hash: Mapped[str] = mapped_column(CHAR(64), index=True)
    # The address, so CyberBrief's list can use it. Never for a GDPR forget (hash only).
    email: Mapped[str | None] = mapped_column(String(320))
    # unsubscribed, bounced, complained, manual or forgotten.
    reason: Mapped[str] = mapped_column(String(20))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The do-not-contact entry's id (unique: each entry is shared once).
    ref: Mapped[str] = mapped_column(String(64), unique=True)
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("deals_optout_batches.id"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DealsHandover(Base):
    """A reply handed to Deals as a lead (step 4). One per reply: the same reply is never sent twice."""

    __tablename__ = "deals_handovers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reply_id: Mapped[int] = mapped_column(ForeignKey("replies.id", ondelete="CASCADE"), unique=True)
    recipient_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaign_recipients.id", ondelete="SET NULL"), index=True
    )
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    # auto (scored 80+), approved (from the review list), manual (sent by hand under 40), repeat (a later
    # reply from someone already handed over).
    band: Mapped[str] = mapped_column(String(10))
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_by_name: Mapped[str | None] = mapped_column(String(200))
    # queued, created, existing, blocked or failed.
    status: Mapped[str] = mapped_column(String(10), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The exact body of the first try: a retry repeats it (same Idempotency-Key, same body).
    payload: Mapped[dict | None] = mapped_column(JSONB)
    deals_lead_id: Mapped[str | None] = mapped_column(String(100), index=True)
    deals_lead_url: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DealsLeadEvent(Base):
    """One event from Deals about a lead NexaReach handed over (step 5): a stage change, a merge or a delete.
    Kept once per Deals event id."""

    __tablename__ = "deals_lead_events"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    type: Mapped[str] = mapped_column(String(30))  # stage_changed, lead_merged, lead_deleted
    deals_lead_id: Mapped[str] = mapped_column(String(100), index=True)
    into_lead_id: Mapped[str | None] = mapped_column(String(100))
    reply_id: Mapped[int | None] = mapped_column(BigInteger)
    from_stage: Mapped[str | None] = mapped_column(String(30))
    to_stage: Mapped[str | None] = mapped_column(String(30))
    value_usd: Mapped[int | None] = mapped_column(Integer)
    lost_reason: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DealsLeadResult(Base):
    """Where each handed-over lead stands in Deals now (step 5): its stage, Won value and Lost reason."""

    __tablename__ = "deals_lead_results"

    deals_lead_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    handover_id: Mapped[int | None] = mapped_column(ForeignKey("deals_handovers.id", ondelete="SET NULL"))
    # new, contacted, discovery, proposal, negotiation, won, lost, disqualified ("Not a fit") or deleted.
    stage: Mapped[str] = mapped_column(String(30))
    value_usd: Mapped[int | None] = mapped_column(Integer)
    lost_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
