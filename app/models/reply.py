import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, str_enum


class ReplyKind(enum.StrEnum):
    REPLY = "REPLY"
    # They asked not to be emailed again; the address went on the do-not-contact list.
    OPT_OUT = "OPT_OUT"


class Reply(Base):
    """A message a person sent back to one of our mailboxes (found by app.services.replies)."""

    __tablename__ = "replies"
    __table_args__ = (
        # The same message is never stored twice, even if the inbox is read again from the start.
        UniqueConstraint("mailbox_id", "message_id", name="uq_replies_mailbox_message_id"),
        Index("ix_replies_received_at", "received_at"),
        Index("ix_replies_recipient_id", "recipient_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mailbox_id: Mapped[int] = mapped_column(ForeignKey("mailboxes.id"))
    # The campaign email it answers. None once the person was forgotten (their replies are deleted then).
    recipient_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaign_recipients.id", ondelete="SET NULL")
    )
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"))
    kind: Mapped[ReplyKind] = mapped_column(str_enum(ReplyKind, "reply_kind"))
    from_email: Mapped[str] = mapped_column(String(320))
    from_name: Mapped[str | None] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(998), default="", server_default="")
    # What they wrote, with the quoted earlier emails taken off; and the whole plain-text body.
    text: Mapped[str] = mapped_column(Text, default="", server_default="")
    full_text: Mapped[str] = mapped_column(Text, default="", server_default="")
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Their Message-ID and thread headers, so an answer from the app lands in the same conversation.
    message_id: Mapped[str] = mapped_column(String(300))
    references: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Inbox state in the app: unread until someone opens it; done once it needs nothing more.
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Who else they copied in (for the "brings a colleague" score signal).
    cc: Mapped[list | None] = mapped_column(JSONB)
    # The reply score (Deals integration step 3; None while scoring is off): 0-100, the rules that matched,
    # and the band: auto (to Deals at once), review (a person decides), low, or zero (hard stop / opt-out).
    score: Mapped[int | None] = mapped_column(Integer)
    score_rules: Mapped[list | None] = mapped_column(JSONB)
    score_band: Mapped[str | None] = mapped_column(String(10))
    # The Needs review list: pending, approved (sent to Deals) or rejected ("Not now").
    review_status: Mapped[str | None] = mapped_column(String(10), index=True)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # "Not now": not_interested, wrong_person, too_small or other, plus a note.
    review_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScoreRule(Base):
    """One word rule of the reply score. Admins edit them; plain rules only, never AI."""

    __tablename__ = "reply_score_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(10))  # words | regex | signal
    words: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    pattern: Mapped[str | None] = mapped_column(Text)
    signal: Mapped[str | None] = mapped_column(String(30))
    points: Mapped[int] = mapped_column(Integer)
    hard_stop: Mapped[str | None] = mapped_column(String(10))  # no | machine
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AnswerStatus(enum.StrEnum):
    SENT = "SENT"
    FAILED = "FAILED"


class ReplyAnswer(Base):
    """An answer written in the app and sent from the mailbox that got the reply."""

    __tablename__ = "reply_answers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reply_id: Mapped[int] = mapped_column(ForeignKey("replies.id", ondelete="CASCADE"), index=True)
    mailbox_id: Mapped[int] = mapped_column(ForeignKey("mailboxes.id"))
    sent_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[AnswerStatus] = mapped_column(str_enum(AnswerStatus, "answer_status"))
    to_email: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(998))
    body: Mapped[str] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(String(300))
    smtp_response: Mapped[str | None] = mapped_column(String(1000))
    error: Mapped[str | None] = mapped_column(String(1000))
    sent_copy_saved: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
