import enum
from datetime import datetime
from typing import Any

from sqlalchemy import CHAR, Column, Computed, DateTime, ForeignKey, Index, Integer, String, Table, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, str_enum

lead_group_members = Table(
    "lead_group_members",
    Base.metadata,
    Column("group_id", ForeignKey("lead_groups.id", ondelete="CASCADE"), primary_key=True),
    Column("lead_id", ForeignKey("leads.id", ondelete="CASCADE"), primary_key=True, index=True),
    Column("added_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)


class EmailProvider(enum.StrEnum):
    GOOGLE = "GOOGLE"
    MICROSOFT = "MICROSOFT"
    ZOHO = "ZOHO"
    YAHOO = "YAHOO"  # Yahoo and AOL
    ICLOUD = "ICLOUD"
    PROTON = "PROTON"
    GODADDY = "GODADDY"
    TITAN = "TITAN"
    YANDEX = "YANDEX"
    AMAZON = "AMAZON"  # Amazon WorkMail / SES
    MIMECAST = "MIMECAST"  # a filter in front of the real mailbox
    PROOFPOINT = "PROOFPOINT"  # a filter in front of the real mailbox
    # The company runs its own mail server (cPanel and similar: the MX is on its own domain).
    OWN_SERVER = "OWN_SERVER"
    OTHER = "OTHER"
    # The domain has no mail server (MX) we could find.
    UNKNOWN = "UNKNOWN"


class LeadGroup(TimestampMixin, Base):
    __tablename__ = "lead_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # Where the leads came from (GDPR), e.g. "LinkedIn research Oct".
    source: Mapped[str | None] = mapped_column(String(200))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    leads: Mapped[list["Lead"]] = relationship(secondary=lead_group_members, back_populates="groups")


class Lead(TimestampMixin, Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stored trimmed and lowercased. Not unique: the duplicates milestone adds "keep both".
    email: Mapped[str] = mapped_column(String(320), index=True)
    # sha256(trimmed, lower-case email) as 64 hex characters: how Deals' do-not-email list names people.
    # Worked out by the database, so it is always right and needs no backfill.
    email_hash: Mapped[str] = mapped_column(
        CHAR(64),
        Computed("nexareach_email_hash(email)", persisted=True),
        index=True,
    )
    first_name: Mapped[str | None] = mapped_column(String(200))
    last_name: Mapped[str | None] = mapped_column(String(200))
    company: Mapped[str | None] = mapped_column(String(300))
    country: Mapped[str | None] = mapped_column(String(100))
    # IANA name (e.g. "Asia/Dubai"), from a timezone column or the country. None when the country spans
    # several zones (USA, Canada…); campaigns then fall back to their own timezone.
    timezone: Mapped[str | None] = mapped_column(String(64))
    # Who hosts the address, from the domain's MX records. None = not looked up yet.
    email_provider: Mapped[EmailProvider | None] = mapped_column(str_enum(EmailProvider, "email_provider"))
    # Any other sheet columns, keyed by snake_case column name, usable as {{variables}}.
    custom: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    # Handed to Deals as a lead (step 4): never added to a campaign again; links to the lead in Deals.
    handed_to_deals_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deals_lead_id: Mapped[str | None] = mapped_column(String(100))
    deals_lead_url: Mapped[str | None] = mapped_column(Text)

    groups: Mapped[list[LeadGroup]] = relationship(secondary=lead_group_members, back_populates="leads")


class DncKind(enum.StrEnum):
    EMAIL = "EMAIL"
    DOMAIN = "DOMAIN"


class DoNotContact(Base):
    __tablename__ = "do_not_contact"

    id: Mapped[int] = mapped_column(primary_key=True)
    # A lowercased email address, or a bare domain ("acme.com") that blocks every address at it.
    value: Mapped[str] = mapped_column(String(320), unique=True)
    kind: Mapped[DncKind] = mapped_column(str_enum(DncKind, "dnc_kind"))
    reason: Mapped[str | None] = mapped_column(String(500))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DuplicateStatus(enum.StrEnum):
    PENDING = "PENDING"
    KEPT_NEW = "KEPT_NEW"
    KEPT_OLD = "KEPT_OLD"
    KEPT_BOTH = "KEPT_BOTH"


class LeadDuplicate(Base):
    """An imported row whose email already belongs to a lead (in the system or earlier in the same file).

    It waits here, outside the group, until someone decides: keep new, keep old or keep both.
    Until then it can't be emailed.
    """

    __tablename__ = "lead_duplicates"
    __table_args__ = (Index("ix_lead_duplicates_status_group", "status", "group_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("lead_groups.id", ondelete="CASCADE"))
    existing_lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    # The new row's data, as imported.
    email: Mapped[str] = mapped_column(String(320))
    first_name: Mapped[str | None] = mapped_column(String(200))
    last_name: Mapped[str | None] = mapped_column(String(200))
    company: Mapped[str | None] = mapped_column(String(300))
    country: Mapped[str | None] = mapped_column(String(100))
    timezone: Mapped[str | None] = mapped_column(String(64))
    custom: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    row_number: Mapped[int] = mapped_column(Integer)

    status: Mapped[DuplicateStatus] = mapped_column(str_enum(DuplicateStatus, "duplicate_status"))
    # The lead that joined the group once decided (the existing one, or the new copy for "keep both").
    result_lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"))
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    group: Mapped[LeadGroup] = relationship()
    existing_lead: Mapped[Lead] = relationship(foreign_keys=[existing_lead_id])
