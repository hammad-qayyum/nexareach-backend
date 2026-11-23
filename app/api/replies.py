"""The Replies page: answers people sent to campaign emails, the whole conversation, and answering them."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, or_, select, true

from app.api.deps import DB, CurrentActor, CurrentUser, EditorUser
from app.api.schemas import Page
from app.models import (
    AnswerStatus,
    Campaign,
    CampaignRecipient,
    FollowUpEmail,
    FollowUpStatus,
    Lead,
    Mailbox,
    Reply,
    ReplyAnswer,
    ReplyKind,
    User,
)
from app.services import replies, sending
from app.services.app_settings import business_fields
from app.services.audit import log_activity
from app.services.templates import lead_fields

router = APIRouter(prefix="/replies", tags=["replies"])


class ReplyState(StrEnum):
    UNREAD = "unread"
    # Not marked done yet (read or not).
    OPEN = "open"
    DONE = "done"
    ALL = "all"


class CampaignRef(BaseModel):
    id: int
    name: str


class ReplyOut(BaseModel):
    id: int
    kind: ReplyKind
    from_email: str
    from_name: str | None
    # From the lead, when we still know them.
    lead_name: str | None
    company: str | None
    subject: str
    snippet: str
    received_at: datetime
    read_at: datetime | None
    done_at: datetime | None
    answered: bool
    has_attachments: bool
    mailbox_email: str
    campaign: CampaignRef | None


class ThreadItem(BaseModel):
    """One email in the conversation, ours or theirs, oldest first."""

    kind: str  # "email" (the campaign email), "follow_up", "reply" (theirs) or "answer" (ours, from the app)
    id: int | None  # the Reply id for "reply" items
    at: datetime | None
    from_email: str
    to_email: str
    subject: str | None
    text: str | None
    full_text: str | None = None
    html: str | None = None
    status: str | None = None
    error: str | None = None
    sent_by: str | None = None


class LeadDetails(BaseModel):
    """For the details panel and for filling {{variables}} in quick replies."""

    # The lead's fields (first_name, company, city, budget…), the business fields and sender_name.
    fields: dict[str, str]
    sender_name: str
    campaign_status: str | None
    follow_ups_sent: int
    # Why their follow-ups stopped early (replied, opted_out…), if they did.
    follow_up_stop: str | None
    first_email_at: datetime | None


class ReplyDetail(ReplyOut):
    text: str
    full_text: str
    thread: list[ThreadItem]
    details: LeadDetails


class CountOut(BaseModel):
    unread: int
    open: int
    done: int
    all: int
    # Reply checking: mailboxes whose inbox is read (they have IMAP), and when one was last read.
    checked_mailboxes: int
    checked_at: datetime | None


class CheckOut(BaseModel):
    started: bool
    checked_mailboxes: int


class ReadIn(BaseModel):
    read: bool = True


class DoneIn(BaseModel):
    done: bool = True


class AnswerIn(BaseModel):
    body: str = Field(min_length=1, max_length=20_000)


class AnswerOut(BaseModel):
    ok: bool
    detail: str
    reply: ReplyDetail


def _state_filter(state: ReplyState):  # noqa: ANN202 - a SQLAlchemy condition
    return {
        ReplyState.UNREAD: Reply.read_at.is_(None) & Reply.done_at.is_(None),
        ReplyState.OPEN: Reply.done_at.is_(None),
        ReplyState.DONE: Reply.done_at.is_not(None),
        ReplyState.ALL: true(),
    }[state]


def _lead_name(lead: Lead | None) -> str | None:
    if lead is None:
        return None
    return " ".join(p for p in (lead.first_name, lead.last_name) if p) or None


def _out(
    r: Reply, lead: Lead | None, mailbox_email: str, campaign: Campaign | None, answered: bool
) -> ReplyOut:
    return ReplyOut(
        id=r.id,
        kind=r.kind,
        from_email=r.from_email,
        from_name=r.from_name,
        lead_name=_lead_name(lead),
        company=lead.company if lead else None,
        subject=r.subject,
        snippet=" ".join((r.text or r.full_text).split())[:200],
        received_at=r.received_at,
        read_at=r.read_at,
        done_at=r.done_at,
        answered=answered,
        has_attachments=r.has_attachments,
        mailbox_email=mailbox_email,
        campaign=CampaignRef(id=campaign.id, name=campaign.name) if campaign else None,
    )


def _query():  # noqa: ANN202
    answered = exists().where(ReplyAnswer.reply_id == Reply.id, ReplyAnswer.status == AnswerStatus.SENT)
    return (
        select(Reply, Lead, Mailbox.email, Campaign, answered)
        .join(Mailbox, Mailbox.id == Reply.mailbox_id)
        .outerjoin(CampaignRecipient, CampaignRecipient.id == Reply.recipient_id)
        .outerjoin(Lead, Lead.id == CampaignRecipient.lead_id)
        .outerjoin(Campaign, Campaign.id == Reply.campaign_id)
    )


@router.get("", response_model=Page[ReplyOut])
def list_replies(
    db: DB,
    _: CurrentUser,
    state: ReplyState = ReplyState.OPEN,
    q: str | None = None,
    campaign_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ReplyOut]:
    query = _query().where(_state_filter(state))
    if campaign_id is not None:
        query = query.where(Reply.campaign_id == campaign_id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.where(
            or_(
                Reply.from_email.ilike(like),
                Reply.from_name.ilike(like),
                Reply.subject.ilike(like),
                Reply.text.ilike(like),
                Lead.company.ilike(like),
            )
        )
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(query.order_by(Reply.received_at.desc(), Reply.id.desc()).limit(limit).offset(offset))
    return Page(items=[_out(*row) for row in rows], total=total)


def _checked_mailboxes(db: DB) -> tuple[int, datetime | None]:
    count, last = db.execute(
        select(func.count(), func.max(Mailbox.inbox_checked_at)).where(
            Mailbox.imap_host.is_not(None), Mailbox.archived_at.is_(None)
        )
    ).one()
    return count, last


@router.get("/count", response_model=CountOut)
def count_replies(db: DB, _: CurrentUser) -> CountOut:
    unread, open_, done, all_ = db.execute(
        select(
            func.count().filter(_state_filter(ReplyState.UNREAD)),
            func.count().filter(_state_filter(ReplyState.OPEN)),
            func.count().filter(_state_filter(ReplyState.DONE)),
            func.count(),
        ).select_from(Reply)
    ).one()
    checked, checked_at = _checked_mailboxes(db)
    return CountOut(
        unread=unread, open=open_, done=done, all=all_, checked_mailboxes=checked, checked_at=checked_at
    )


@router.post("/check", response_model=CheckOut)
def check_now(db: DB, _: EditorUser, background: BackgroundTasks) -> CheckOut:
    """Read the inboxes now instead of waiting for the next 5-minute check. Runs after the response."""
    checked, _ = _checked_mailboxes(db)
    if checked:
        background.add_task(replies.check_all)
    return CheckOut(started=bool(checked), checked_mailboxes=checked)


def _get(db: DB, reply_id: int) -> Reply:
    reply = db.get(Reply, reply_id)
    if reply is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "reply_not_found")
    return reply


def _thread(db: DB, reply: Reply) -> list[ThreadItem]:
    """Every email between us and this person in this campaign conversation, oldest first."""
    mailbox_email = db.scalar(select(Mailbox.email).where(Mailbox.id == reply.mailbox_id)) or ""
    items: list[ThreadItem] = []
    recipient = db.get(CampaignRecipient, reply.recipient_id) if reply.recipient_id else None
    if recipient is not None and recipient.sent_at:
        items.append(
            ThreadItem(
                kind="email",
                id=None,
                at=recipient.sent_at,
                from_email=mailbox_email,
                to_email=recipient.email,
                subject=recipient.subject_sent,
                text=recipient.body_sent,
                html=recipient.html_sent,
            )
        )
        for f in db.scalars(
            select(FollowUpEmail)
            .where(FollowUpEmail.recipient_id == recipient.id, FollowUpEmail.status == FollowUpStatus.SENT)
            .order_by(FollowUpEmail.step)
        ):
            items.append(
                ThreadItem(
                    kind="follow_up",
                    id=None,
                    at=f.sent_at,
                    from_email=mailbox_email,
                    to_email=recipient.email,
                    subject=f.subject_sent,
                    text=f.body_sent,
                )
            )
    theirs = (
        db.scalars(select(Reply).where(Reply.recipient_id == recipient.id)).all()
        if recipient is not None
        else [reply]
    )
    for r in theirs:
        items.append(
            ThreadItem(
                kind="reply",
                id=r.id,
                at=r.received_at,
                from_email=r.from_email,
                to_email=mailbox_email,
                subject=r.subject,
                text=r.text,
                full_text=r.full_text,
            )
        )
    for a, sender in db.execute(
        select(ReplyAnswer, User.name)
        .outerjoin(User, User.id == ReplyAnswer.sent_by_id)
        .where(ReplyAnswer.reply_id.in_([r.id for r in theirs]))
    ):
        items.append(
            ThreadItem(
                kind="answer",
                id=None,
                at=a.created_at,
                from_email=mailbox_email,
                to_email=a.to_email,
                subject=a.subject,
                text=a.body,
                status=a.status.value,
                error=a.error,
                sent_by=sender,
            )
        )

    def order(item: ThreadItem) -> tuple[datetime, int, datetime]:
        at = item.at or datetime.min.replace(tzinfo=UTC)
        # Their Date header has whole seconds only, so within the same second our email goes first:
        # a reply that quick is an answer to it.
        return at.replace(microsecond=0), 1 if item.kind == "reply" else 0, at

    return sorted(items, key=order)


def _details(db: DB, reply: Reply, lead: Lead | None, campaign: Campaign | None) -> LeadDetails:
    mailbox = db.get(Mailbox, reply.mailbox_id)
    recipient = db.get(CampaignRecipient, reply.recipient_id) if reply.recipient_id else None
    sender_name = mailbox.display_name if mailbox else ""
    fields = {**business_fields(db), **(lead_fields(lead) if lead else {"email": reply.from_email})}
    return LeadDetails(
        fields={"sender_name": sender_name, **fields},
        sender_name=sender_name,
        campaign_status=campaign.status.value if campaign else None,
        follow_ups_sent=recipient.follow_ups_sent if recipient else 0,
        follow_up_stop=recipient.follow_up_stop if recipient else None,
        first_email_at=recipient.sent_at if recipient else None,
    )


def _detail(db: DB, reply: Reply) -> ReplyDetail:
    row = db.execute(_query().where(Reply.id == reply.id)).one()
    _, lead, _, campaign, _ = row
    return ReplyDetail(
        **_out(*row).model_dump(),
        text=reply.text,
        full_text=reply.full_text,
        thread=_thread(db, reply),
        details=_details(db, reply, lead, campaign),
    )


@router.get("/{reply_id}", response_model=ReplyDetail)
def get_reply(reply_id: int, db: DB, _: CurrentUser) -> ReplyDetail:
    return _detail(db, _get(db, reply_id))


@router.post("/{reply_id}/read", response_model=ReplyDetail)
def mark_read(reply_id: int, body: ReadIn, db: DB, _: EditorUser) -> ReplyDetail:
    """Opened (or put back to unread). Not logged: it only changes what the inbox highlights."""
    reply = _get(db, reply_id)
    if body.read != (reply.read_at is not None):
        reply.read_at = sending.now() if body.read else None
        db.commit()
    return _detail(db, reply)


@router.post("/{reply_id}/done", response_model=ReplyDetail)
def mark_done(reply_id: int, body: DoneIn, db: DB, _: EditorUser, actor: CurrentActor) -> ReplyDetail:
    reply = _get(db, reply_id)
    if body.done != (reply.done_at is not None):
        now = sending.now()
        reply.done_at = now if body.done else None
        if body.done:
            reply.read_at = reply.read_at or now
        action = "reply.done" if body.done else "reply.reopened"
        log_activity(db, actor, action, entity_type="reply", entity_id=reply.id)
        db.commit()
    return _detail(db, reply)


@router.post("/{reply_id}/answer", response_model=AnswerOut)
def answer_reply(reply_id: int, body: AnswerIn, db: DB, user: EditorUser, actor: CurrentActor) -> AnswerOut:
    """Send an answer from the mailbox that got the reply, in the same thread."""
    reply = _get(db, reply_id)
    if not body.body.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "answer_empty")
    mailbox = db.get(Mailbox, reply.mailbox_id)
    if mailbox is None or mailbox.archived_at is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "mailbox_archived")
    result = replies.answer(db, reply, body.body, user.id, actor, sending.now())
    ok = result.answer.status == AnswerStatus.SENT
    return AnswerOut(ok=ok, detail=result.detail, reply=_detail(db, reply))
