"""Deliverability (plan section 19): what happened to each email after it left, the domain's health, DMARC
reports, placement tests and alerts."""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, str_enum
from app.models.warmup import HelperProvider


class DeliveryStatus(enum.StrEnum):
    # Our mail server accepted it (the send log).
    SENT = "SENT"
    # The receiving server accepted it (cPanel's delivery log).
    DELIVERED = "DELIVERED"
    # Not delivered yet; the mail server keeps trying.
    DEFERRED = "DEFERRED"
    # Gave up for a reason that may pass (mailbox full, a temporary block). The address may be fine.
    SOFT_BOUNCE = "SOFT_BOUNCE"
    # The address doesn't exist: it goes on do-not-contact.
    HARD_BOUNCE = "HARD_BOUNCE"
    # Our own mail server refused it while sending.
    REJECTED = "REJECTED"


# When several events arrive for one email, the one that says most wins (a bounce after "delivered" is
# possible: the relay accepted it, the final server then refused it).
STATUS_WEIGHT = {
    DeliveryStatus.SENT: 0,
    DeliveryStatus.DEFERRED: 1,
    DeliveryStatus.DELIVERED: 2,
    DeliveryStatus.REJECTED: 3,
    DeliveryStatus.SOFT_BOUNCE: 4,
    DeliveryStatus.HARD_BOUNCE: 5,
}


class DeliverySource(enum.StrEnum):
    SMTP = "SMTP"  # our mail server's answer while sending
    CPANEL = "CPANEL"  # cPanel's "Track Delivery" log
    BOUNCE = "BOUNCE"  # a bounce email that came back to the mailbox


class DeliveryEvent(Base):
    """One line of the mail-server log viewer."""

    __tablename__ = "delivery_events"
    __table_args__ = (
        Index("ix_delivery_events_mailbox_occurred", "mailbox_id", "occurred_at"),
        Index("ix_delivery_events_occurred", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mailbox_id: Mapped[int | None] = mapped_column(ForeignKey("mailboxes.id", ondelete="SET NULL"))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    recipient_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaign_recipients.id", ondelete="CASCADE"), index=True
    )
    follow_up_id: Mapped[int | None] = mapped_column(ForeignKey("follow_up_emails.id", ondelete="CASCADE"))
    # The person's address ("" once they were forgotten).
    email: Mapped[str] = mapped_column(String(320))
    source: Mapped[DeliverySource] = mapped_column(str_enum(DeliverySource, "delivery_source"))
    status: Mapped[DeliveryStatus] = mapped_column(str_enum(DeliveryStatus, "delivery_status"))
    # The receiving server, and what it answered (the raw text, for the log viewer).
    remote_host: Mapped[str | None] = mapped_column(String(255))
    response: Mapped[str | None] = mapped_column(String(2000))
    # The mail server's queue ID ("id=1tAbCd-000123-Xy"), which ties cPanel's log to our send.
    queue_id: Mapped[str | None] = mapped_column(String(100))
    # Stops the same cPanel line or bounce email being recorded twice.
    external_id: Mapped[str | None] = mapped_column(String(300), unique=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DmarcReport(Base):
    """A daily DMARC aggregate report from a receiver (Google, Microsoft, Yahoo...) about our domain."""

    __tablename__ = "dmarc_reports"
    __table_args__ = (UniqueConstraint("reporter", "report_id", name="uq_dmarc_reports_reporter_report_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    reporter: Mapped[str] = mapped_column(String(200))
    report_id: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str] = mapped_column(String(255), index=True)
    # The domain's published policy at the time (none / quarantine / reject).
    policy: Mapped[str | None] = mapped_column(String(20))
    begin_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DmarcRecord(Base):
    """One row of a report: how many emails came from one server and whether they passed."""

    __tablename__ = "dmarc_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("dmarc_reports.id", ondelete="CASCADE"), index=True)
    source_ip: Mapped[str] = mapped_column(String(64))
    count: Mapped[int] = mapped_column(Integer)
    disposition: Mapped[str | None] = mapped_column(String(20))
    # The DMARC verdicts (aligned with the From domain): "pass" or "fail".
    dkim: Mapped[str] = mapped_column(String(10))
    spf: Mapped[str] = mapped_column(String(10))
    header_from: Mapped[str | None] = mapped_column(String(255))


class HealthStatus(enum.StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"
    # The check itself couldn't run (a DNS timeout, a list that didn't answer). Never raises an alert.
    UNKNOWN = "UNKNOWN"


class HealthKind(enum.StrEnum):
    MX = "MX"
    SPF = "SPF"
    DKIM = "DKIM"
    DMARC = "DMARC"
    CERTIFICATE = "CERTIFICATE"
    BLACKLIST = "BLACKLIST"


class HealthCheck(Base):
    """One result of one check. Kept as history; the newest per (kind, target) is the current state."""

    __tablename__ = "health_checks"
    __table_args__ = (Index("ix_health_checks_kind_target_checked", "kind", "target", "checked_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[HealthKind] = mapped_column(str_enum(HealthKind, "health_kind"))
    # A domain (fastnexa.com), a mail server (mail.fastnexa.com) or an IP address.
    target: Mapped[str] = mapped_column(String(255))
    status: Mapped[HealthStatus] = mapped_column(str_enum(HealthStatus, "health_status"))
    detail: Mapped[str] = mapped_column(String(2000))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PlacementTestStatus(enum.StrEnum):
    RUNNING = "RUNNING"
    DONE = "DONE"


class PlacementTest(Base):
    """A campaign's real message sent from one mailbox to every helper account, to see where it lands."""

    __tablename__ = "placement_tests"

    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_id: Mapped[int] = mapped_column(ForeignKey("mailboxes.id", ondelete="CASCADE"), index=True)
    # None: a neutral sample message (no campaign to take the text from).
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"))
    subject: Mapped[str] = mapped_column(String(998))
    status: Mapped[PlacementTestStatus] = mapped_column(
        str_enum(PlacementTestStatus, "placement_test_status")
    )
    # None: started by the weekly schedule.
    requested_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlacementState(enum.StrEnum):
    WAITING = "WAITING"  # not sent yet
    SENT = "SENT"  # sent, not looked for yet
    CHECKED = "CHECKED"  # found (or given up on): see folder
    FAILED = "FAILED"  # couldn't be sent, or the helper couldn't be checked


class PlacementFolder(enum.StrEnum):
    INBOX = "INBOX"
    PROMOTIONS = "PROMOTIONS"  # Gmail's Promotions tab
    SPAM = "SPAM"
    MISSING = "MISSING"  # not found in the inbox or spam a while after sending


class PlacementResult(Base):
    __tablename__ = "placement_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("placement_tests.id", ondelete="CASCADE"), index=True)
    helper_id: Mapped[int | None] = mapped_column(ForeignKey("warmup_helpers.id", ondelete="SET NULL"))
    helper_email: Mapped[str] = mapped_column(String(320))
    provider: Mapped[HelperProvider] = mapped_column(str_enum(HelperProvider, "helper_provider"))
    state: Mapped[PlacementState] = mapped_column(str_enum(PlacementState, "placement_state"))
    folder: Mapped[PlacementFolder | None] = mapped_column(str_enum(PlacementFolder, "placement_folder"))
    message_id: Mapped[str | None] = mapped_column(String(300))
    error: Mapped[str | None] = mapped_column(String(1000))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AlertSeverity(enum.StrEnum):
    CRITICAL = "CRITICAL"  # pauses campaign emails
    WARNING = "WARNING"


class AlertStatus(enum.StrEnum):
    OPEN = "OPEN"
    # Someone has seen it (e.g. resumed sending anyway). Stays until the problem goes away.
    ACKNOWLEDGED = "ACKNOWLEDGED"
    # The problem went away (found by the next check).
    RESOLVED = "RESOLVED"


class Alert(Base):
    """Something wrong with deliverability. One active alert per (kind, mailbox, target)."""

    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alerts_kind_status", "kind", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # app.services.alerts.Kind: hard_bounces, blacklisted, dns_broken, login_failing, deferred, ...
    kind: Mapped[str] = mapped_column(String(40))
    severity: Mapped[AlertSeverity] = mapped_column(str_enum(AlertSeverity, "alert_severity"))
    status: Mapped[AlertStatus] = mapped_column(str_enum(AlertStatus, "alert_status"))
    mailbox_id: Mapped[int | None] = mapped_column(ForeignKey("mailboxes.id", ondelete="CASCADE"))
    # What it's about besides a mailbox: a domain, an IP address, a helper account ("" when none).
    target: Mapped[str] = mapped_column(String(255), default="", server_default="")
    message: Mapped[str] = mapped_column(String(1000))
    # Mailboxes whose campaign emails this alert paused (resuming one acknowledges the alert).
    paused_mailbox_ids: Mapped[list[int]] = mapped_column(JSONB, default=list, server_default="[]")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The email to admins: when it went, or why it couldn't.
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_error: Mapped[str | None] = mapped_column(String(1000))
