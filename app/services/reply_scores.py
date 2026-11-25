"""Scoring replies as they are found, and the Needs review list (Deals integration step 3).

The scoring itself is app.services.scoring (pure word rules, never AI). This module loads the admins' rules
and cut-offs, saves the score on the reply, and decides what happens next:

- auto (score ≥ auto_threshold, default 80): handed to Deals at once if handover is on, else it waits on the
  Needs review list;
- review (≥ review_threshold, default 40): waits on the Needs review list;
- low: stays in NexaReach (anyone can still send it to Deals by hand);
- zero: an opt-out, or a hard-stop rule matched.

Off until an admin switches scoring on. Replies found earlier are then scored for display only: they never
join the review list or go to Deals by themselves.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Reply, ReplyKind, ScoreRule
from app.services import scoring
from app.services.app_settings import get_json
from app.services.audit import SYSTEM, log_activity

log = logging.getLogger(__name__)

SETTINGS_KEY = "score.settings"  # {"enabled", "auto_threshold", "review_threshold", "free_mail_domains"}
DEFAULT_AUTO = 80
DEFAULT_REVIEW = 40


@dataclass(frozen=True)
class ScoreSettings:
    enabled: bool = False
    auto_threshold: int = DEFAULT_AUTO
    review_threshold: int = DEFAULT_REVIEW
    free_mail_domains: tuple[str, ...] = field(default=tuple(scoring.DEFAULT_FREE_MAIL))


def settings(db: Session) -> ScoreSettings:
    raw = get_json(db, SETTINGS_KEY)
    free = raw.get("free_mail_domains")
    return ScoreSettings(
        enabled=bool(raw.get("enabled")),
        auto_threshold=int(raw.get("auto_threshold") or DEFAULT_AUTO),
        review_threshold=int(raw.get("review_threshold") if raw.get("review_threshold") is not None else 40),
        free_mail_domains=tuple(free)
        if isinstance(free, list) and free
        else tuple(scoring.DEFAULT_FREE_MAIL),
    )


def to_rule(row: ScoreRule) -> scoring.Rule:
    return scoring.Rule(
        name=row.name,
        kind=row.kind,
        points=row.points,
        words=tuple(row.words or ()),
        pattern=row.pattern,
        signal=row.signal,
        hard_stop=row.hard_stop,
    )


def rules(db: Session) -> list[scoring.Rule]:
    rows = db.scalars(
        select(ScoreRule).where(ScoreRule.enabled.is_(True)).order_by(ScoreRule.position, ScoreRule.id)
    )
    return [to_rule(r) for r in rows]


def band_for(result: scoring.ScoreResult, s: ScoreSettings) -> str:
    if result.hard_stop:
        return "zero"
    if result.value >= s.auto_threshold:
        return "auto"
    if result.value >= s.review_threshold:
        return "review"
    return "low"


def _opt_out_result(db: Session) -> scoring.ScoreResult:
    """An opt-out found by the existing check: score 0, shown as the "no" hard stop."""
    name = db.scalar(select(ScoreRule.name).where(ScoreRule.hard_stop == "no").order_by(ScoreRule.position))
    return scoring.ScoreResult(value=0, matched=[name or "Asked not to be emailed"], hard_stop="no")


def score(db: Session, reply: Reply, at: datetime, *, review: bool = True) -> str | None:
    """Score a reply and save it; returns its band (None while scoring is off). `review`: new replies join
    the review list or are handed over; replies scored afterwards for display don't. The caller commits."""
    s = settings(db)
    if not s.enabled:
        return None
    if reply.kind == ReplyKind.OPT_OUT:
        result = _opt_out_result(db)
    else:
        result = scoring.score_reply(
            reply.full_text or reply.text,
            reply.from_email,
            list(reply.cc or []),
            rules(db),
            set(s.free_mail_domains),
        )
    band = band_for(result, s)
    reply.score, reply.score_rules, reply.score_band = result.value, result.matched, band
    if review:
        if band == "auto":
            from app.services import deals, deals_handover  # deals_handover uses this module too

            if deals.settings(db).handover_enabled:
                deals_handover.queue(db, reply, "auto", at)
            else:
                reply.review_status = "pending"  # handover is off: a person decides
        elif band == "review":
            reply.review_status = "pending"
    log_activity(
        db,
        SYSTEM,
        "reply.scored",
        entity_type="reply",
        entity_id=reply.id,
        after={"score": result.value, "band": band, "rules": result.matched, "review": reply.review_status},
    )
    return band


def try_text(db: Session, text: str, sender: str, cc: list[str]) -> dict[str, Any]:
    """The Try it box: score a pasted reply with the current rules. Nothing is saved."""
    s = settings(db)
    result = scoring.score_reply(text, sender, cc, rules(db), set(s.free_mail_domains))
    return {
        "score": result.value,
        "matched": result.matched,
        "hard_stop": result.hard_stop,
        "band": band_for(result, s),
        "new_text": scoring.extract_new_text(text),
    }


def score_existing(db: Session, at: datetime) -> int:
    """After scoring is switched on: give earlier replies a score for display only. Commits."""
    count = 0
    for reply in db.scalars(select(Reply).where(Reply.score.is_(None)).order_by(Reply.id)).all():
        score(db, reply, at, review=False)
        count += 1
        if count % 200 == 0:
            db.commit()
    db.commit()
    return count
