"""Saved answers for the Replies page."""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from app.api.deps import DB, CurrentActor, CurrentUser, EditorUser
from app.models import QuickReply
from app.services.audit import changes, log_activity, snapshot

router = APIRouter(prefix="/quick-replies", tags=["replies"])

MAX_QUICK_REPLIES = 50


class QuickReplyIn(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=20_000)

    @field_validator("title", "body")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("can't be empty")
        return v


class QuickReplyOut(BaseModel):
    id: int
    title: str
    body: str


def _out(q: QuickReply) -> QuickReplyOut:
    return QuickReplyOut(id=q.id, title=q.title, body=q.body)


def _get(db: DB, quick_reply_id: int) -> QuickReply:
    q = db.get(QuickReply, quick_reply_id)
    if q is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "quick_reply_not_found")
    return q


@router.get("", response_model=list[QuickReplyOut])
def list_quick_replies(db: DB, _: CurrentUser) -> list[QuickReplyOut]:
    return [_out(q) for q in db.scalars(select(QuickReply).order_by(QuickReply.position, QuickReply.id))]


@router.post("", response_model=QuickReplyOut, status_code=status.HTTP_201_CREATED)
def create_quick_reply(body: QuickReplyIn, db: DB, user: EditorUser, actor: CurrentActor) -> QuickReplyOut:
    if (db.scalar(select(func.count()).select_from(QuickReply)) or 0) >= MAX_QUICK_REPLIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "too_many_quick_replies")
    position = (db.scalar(select(func.max(QuickReply.position))) or 0) + 1
    q = QuickReply(title=body.title.strip(), body=body.body.strip(), position=position, created_by_id=user.id)
    db.add(q)
    db.flush()
    log_activity(
        db, actor, "quick_reply.created", entity_type="quick_reply", entity_id=q.id, after={"title": q.title}
    )
    db.commit()
    return _out(q)


@router.put("/{quick_reply_id}", response_model=QuickReplyOut)
def update_quick_reply(
    quick_reply_id: int, body: QuickReplyIn, db: DB, _: EditorUser, actor: CurrentActor
) -> QuickReplyOut:
    q = _get(db, quick_reply_id)
    before = snapshot(q, ["title", "body"])
    q.title, q.body = body.title.strip(), body.body.strip()
    after = snapshot(q, ["title", "body"])
    if before != after:
        old, new = changes(before, after)
        log_activity(
            db, actor, "quick_reply.updated", entity_type="quick_reply", entity_id=q.id, before=old, after=new
        )
        db.commit()
    return _out(q)


@router.delete("/{quick_reply_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_quick_reply(quick_reply_id: int, db: DB, _: EditorUser, actor: CurrentActor) -> None:
    q = _get(db, quick_reply_id)
    log_activity(
        db, actor, "quick_reply.deleted", entity_type="quick_reply", entity_id=q.id, before={"title": q.title}
    )
    db.delete(q)
    db.commit()
