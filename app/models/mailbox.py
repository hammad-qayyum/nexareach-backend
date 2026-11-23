import enum
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String
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
    # Incoming mail (IMAP, always SSL), used to save a copy of each sent email in the Sent folder.
    # Same username and password as SMTP (true for cPanel). No host = no Sent copy.
    imap_host: Mapped[str | None] = mapped_column(String(255))
    imap_port: Mapped[int] = mapped_column(Integer, default=993, server_default="993")
    # Found once with IMAP LIST (cPanel calls it "INBOX.Sent") and remembered.
    sent_folder: Mapped[str | None] = mapped_column(String(255))
    # Reply checking (app.services.replies): where the last inbox check stopped, so each check only
    # reads new messages. A new UIDVALIDITY means the server renumbered the inbox: start again.
    inbox_uidvalidity: Mapped[int | None] = mapped_column(BigInteger)
    inbox_last_uid: Mapped[int | None] = mapped_column(BigInteger)
    inbox_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    inbox_error: Mapped[str | None] = mapped_column(String(1000))
    daily_cap: Mapped[int] = mapped_column(Integer, default=40, server_default="40")
    # Ramp-up (app.services.warmup.daily_limit): a new mailbox starts at ramp_start_limit emails a day and
    # grows by RAMP_STEP every RAMP_EVERY working days up to daily_cap. Off for mailboxes that were
    # already sending when ramp-up was added.
    ramp_up_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    ramp_start_date: Mapped[date | None] = mapped_column(Date)
    ramp_start_limit: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    # Warm-up with helper accounts: 5-10 emails a day to helpers, inside the daily limit.
    warmup_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Where warm-up emails and helper replies are filed in this mailbox (found or created once).
    warmup_folder: Mapped[str | None] = mapped_column(String(255))
    # Campaign emails (not warm-up) stop from this mailbox while set, e.g. after a poor warm-up inbox rate.
    campaigns_paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    campaigns_paused_reason: Mapped[str | None] = mapped_column(String(50))
    # When someone last resumed them: the low-rate check then only looks at warm-up results after this, so it
    # doesn't pause again straight away on the same bad days.
    campaigns_resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[MailboxStatus] = mapped_column(str_enum(MailboxStatus, "mailbox_status"))
    last_error: Mapped[str | None] = mapped_column(String(1000))
    # The mailbox is the unit of pacing: it sends nothing before this time, whichever campaign is next.
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Archived mailboxes are hidden and never send, but keep their history (which emails they sent).
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
