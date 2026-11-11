import enum
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, str_enum


class SmtpSecurity(enum.StrEnum):
    SSL = "SSL"  # port 465
    STARTTLS = "STARTTLS"  # port 587
    # Unencrypted, for local testing against Mailpit only. Refused unless SMTP_ALLOW_PLAIN is set.
    NONE = "NONE"


class MailboxStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    # Set by the sending engine when the server rejects the login; an editor fixes it and reactivates.
    ERROR = "ERROR"


class Mailbox(TimestampMixin, Base):
    __tablename__ = "mailboxes"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stored trimmed and lowercased.
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    smtp_host: Mapped[str] = mapped_column(String(255))
    smtp_port: Mapped[int] = mapped_column(Integer)
    smtp_security: Mapped[SmtpSecurity] = mapped_column(str_enum(SmtpSecurity, "smtp_security"))
    smtp_username: Mapped[str] = mapped_column(String(320))
    smtp_password_encrypted: Mapped[str] = mapped_column(String(1000))
    daily_cap: Mapped[int] = mapped_column(Integer, default=40, server_default="40")
    status: Mapped[MailboxStatus] = mapped_column(str_enum(MailboxStatus, "mailbox_status"))
    last_error: Mapped[str | None] = mapped_column(String(1000))
    # The mailbox is the unit of pacing: it sends nothing before this time, whichever campaign is next.
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
