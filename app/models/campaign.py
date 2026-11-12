import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, str_enum
from app.models.lead import LeadGroup
from app.models.mailbox import Mailbox

campaign_groups = Table(
    "campaign_groups",
    Base.metadata,
    Column("campaign_id", ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", ForeignKey("lead_groups.id"), primary_key=True),
)

campaign_mailboxes = Table(
    "campaign_mailboxes",
    Base.metadata,
    Column("campaign_id", ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True),
    Column("mailbox_id", ForeignKey("mailboxes.id"), primary_key=True, index=True),
)


campaign_exclusions = Table(
    "campaign_exclusions",
    Base.metadata,
    Column("campaign_id", ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True),
    Column("lead_id", ForeignKey("leads.id", ondelete="CASCADE"), primary_key=True),
)


class BodyFormat(enum.StrEnum):
    TEXT = "TEXT"
    # Sanitised HTML from the rich text editor; a plain-text copy is generated for the email.
    HTML = "HTML"


class CampaignStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class Campaign(TimestampMixin, Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[CampaignStatus] = mapped_column(str_enum(CampaignStatus, "campaign_status"))
    # Plain text with {{variables}} and {spin|tax}.
    subject: Mapped[str] = mapped_column(String(998), default="", server_default="")
    body: Mapped[str] = mapped_column(Text, default="", server_default="")
    body_format: Mapped[BodyFormat] = mapped_column(
        str_enum(BodyFormat, "body_format"), default=BodyFormat.TEXT, server_default=BodyFormat.TEXT.value
    )

    # Pause between two emails from the same mailbox, picked at random in this range.
    gap_min_seconds: Mapped[int] = mapped_column(Integer, default=300, server_default="300")
    gap_max_seconds: Mapped[int] = mapped_column(Integer, default=600, server_default="600")
    # Sending window, in the campaign's timezone. Days are 0 = Monday … 6 = Sunday; end_hour is exclusive.
    timezone: Mapped[str] = mapped_column(String(64))
    send_days: Mapped[list[int]] = mapped_column(JSONB)
    start_hour: Mapped[int] = mapped_column(Integer)
    end_hour: Mapped[int] = mapped_column(Integer)
    # Apply the days/hours window in each lead's own timezone (falls back to `timezone` when unknown).
    use_lead_timezone: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Running campaigns that share a mailbox take turns: the one that sent least recently goes next.
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    groups: Mapped[list[LeadGroup]] = relationship(secondary=campaign_groups)
    mailboxes: Mapped[list[Mailbox]] = relationship(secondary=campaign_mailboxes)


class RecipientStatus(enum.StrEnum):
    WAITING = "WAITING"
    # Committed just before the email is handed to the mail server, so a crash mid-send can't lose
    # track of it (and it is never sent twice; see sending.recover_interrupted).
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class CampaignRecipient(Base):
    """One lead in a started campaign. The list is fixed when the campaign starts."""

    __tablename__ = "campaign_recipients"
    __table_args__ = (
        Index("ix_campaign_recipients_campaign_status", "campaign_id", "status"),
        Index("ix_campaign_recipients_email_sent_at", "email", "sent_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"))
    email: Mapped[str] = mapped_column(String(320))
    status: Mapped[RecipientStatus] = mapped_column(str_enum(RecipientStatus, "recipient_status"))
    skip_reason: Mapped[str | None] = mapped_column(String(50))
    mailbox_id: Mapped[int | None] = mapped_column(ForeignKey("mailboxes.id"))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Set after a temporary failure: not retried before this time.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    send_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_id: Mapped[str | None] = mapped_column(String(300))
    smtp_response: Mapped[str | None] = mapped_column(String(1000))
    error: Mapped[str | None] = mapped_column(String(1000))
    # The exact text that went out.
    subject_sent: Mapped[str | None] = mapped_column(String(998))
    body_sent: Mapped[str | None] = mapped_column(Text)
    html_sent: Mapped[str | None] = mapped_column(Text)
    # Copy in the mailbox's Sent folder: None = not attempted (no IMAP set up), True/False = result.
    sent_copy_saved: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
