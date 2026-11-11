import enum
from datetime import datetime
from typing import Any

from sqlalchemy import Column, DateTime, ForeignKey, String, Table, func
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
    first_name: Mapped[str | None] = mapped_column(String(200))
    last_name: Mapped[str | None] = mapped_column(String(200))
    company: Mapped[str | None] = mapped_column(String(300))
    country: Mapped[str | None] = mapped_column(String(100))
    # Any other sheet columns, keyed by snake_case column name, usable as {{variables}}.
    custom: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")

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
