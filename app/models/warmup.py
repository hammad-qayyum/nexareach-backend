import enum
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, str_enum
from app.models.mailbox import MailboxStatus, SmtpSecurity


class HelperProvider(enum.StrEnum):
    GMAIL = "GMAIL"
    OUTLOOK = "OUTLOOK"
    YAHOO = "YAHOO"
    OTHER = "OTHER"


class WarmupHelper(TimestampMixin, Base):
    """An inbox we own that receives warm-up emails, rescues them from spam, opens and answers them.

    Field names match Mailbox's so the same mail code (mailer) logs in to both.
    """

    __tablename__ = "warmup_helpers"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    provider: Mapped[HelperProvider] = mapped_column(str_enum(HelperProvider, "helper_provider"))
    smtp_host: Mapped[str] = mapped_column(String(255))
    smtp_port: Mapped[int] = mapped_column(Integer)
    smtp_security: Mapped[SmtpSecurity] = mapped_column(str_enum(SmtpSecurity, "smtp_security"))
    smtp_username: Mapped[str] = mapped_column(String(320))
    smtp_password_encrypted: Mapped[str] = mapped_column(String(1000))
    imap_host: Mapped[str] = mapped_column(String(255))
    imap_port: Mapped[int] = mapped_column(Integer, default=993, server_default="993")
    # ACTIVE, PAUSED by a person, or ERROR when its login failed (warm-up carries on with the others).
    status: Mapped[MailboxStatus] = mapped_column(str_enum(MailboxStatus, "mailbox_status"))
    last_error: Mapped[str | None] = mapped_column(String(1000))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Found once with IMAP LIST and remembered.
    spam_folder: Mapped[str | None] = mapped_column(String(255))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class WarmupStatus(enum.StrEnum):
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class LandedIn(enum.StrEnum):
    INBOX = "INBOX"
    SPAM = "SPAM"
    # Not found in the helper's inbox or spam within a few hours.
    MISSING = "MISSING"


class WarmupMessage(Base):
    """One warm-up email from a mailbox to a helper, and what the helper did with it."""

    __tablename__ = "warmup_messages"
    __table_args__ = (
        Index("ix_warmup_messages_mailbox_sent_at", "mailbox_id", "sent_at"),
        Index("ix_warmup_messages_helper_due", "helper_id", "status", "acted_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mailbox_id: Mapped[int] = mapped_column(ForeignKey("mailboxes.id", ondelete="CASCADE"))
    helper_id: Mapped[int | None] = mapped_column(ForeignKey("warmup_helpers.id", ondelete="SET NULL"))
    # The helper's provider when it was sent (for inbox rates by provider, even after a helper is removed).
    provider: Mapped[HelperProvider] = mapped_column(str_enum(HelperProvider, "helper_provider"))
    status: Mapped[WarmupStatus] = mapped_column(str_enum(WarmupStatus, "warmup_status"))
    message_id: Mapped[str] = mapped_column(String(300), unique=True)
    subject: Mapped[str] = mapped_column(String(998))
    send_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(1000))
    # The helper looks for it after this time (10-40 minutes after sending, like a person would).
    act_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    landed_in: Mapped[LandedIn | None] = mapped_column(str_enum(LandedIn, "landed_in"))
    rescued: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    moved_to_folder: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reply_message_id: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
