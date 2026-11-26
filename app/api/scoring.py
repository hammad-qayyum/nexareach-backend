"""Settings → Reply score: the word rules, the two cut-offs and the Try it box (Deals integration step 3).
Plain word rules only, never AI. Admins change them; members may use Try it."""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import DB, AdminUser, CurrentActor, EditorUser
from app.models import ScoreRule
from app.services import reply_scores, scoring
from app.services.app_settings import set_json
from app.services.audit import changes, log_activity, snapshot

router = APIRouter(prefix="/scoring", tags=["scoring"])

RULE_FIELDS = ["name", "kind", "words", "pattern", "signal", "points", "hard_stop", "enabled", "position"]


class ScoreSettingsIO(BaseModel):
    # Off: replies aren't scored and nothing joins the Needs review list.
    enabled: bool
    auto_threshold: int = Field(ge=1, le=100)
    review_threshold: int = Field(ge=0, le=99)
    free_mail_domains: list[str] = Field(max_length=2000)


class ThresholdsIn(BaseModel):
    auto_threshold: int = Field(ge=1, le=100)
    review_threshold: int = Field(ge=0, le=99)


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["words", "regex", "signal"]
    words: list[str] = Field(default_factory=list, max_length=200)
    pattern: str | None = Field(default=None, max_length=500)
    signal: str | None = None
    points: int = Field(ge=-100, le=100)
    hard_stop: Literal["no", "machine"] | None = None
    enabled: bool = True
    position: int | None = None


class RuleOut(BaseModel):
    id: int
    name: str
    kind: str
    words: list[str]
    pattern: str | None
    signal: str | None
    points: int
    hard_stop: str | None
    enabled: bool
    position: int


class TryIn(BaseModel):
    text: str = Field(min_length=1, max_length=50_000)
    sender: str = Field(default="", max_length=320)
    cc: list[str] = Field(default_factory=list, max_length=50)


class TryOut(BaseModel):
    score: int
    band: str
    matched: list[str]
    hard_stop: str | None
    # What the rules read: the reply without quoted history or signature.
    new_text: str


def _settings_out(db: DB) -> ScoreSettingsIO:
    s = reply_scores.settings(db)
    return ScoreSettingsIO(
        enabled=s.enabled,
        auto_threshold=s.auto_threshold,
        review_threshold=s.review_threshold,
        free_mail_domains=list(s.free_mail_domains),
    )


def _save(db: DB, value: ScoreSettingsIO, user_id: int, actor: CurrentActor, action: str) -> bool:
    if value.auto_threshold <= value.review_threshold:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "score_thresholds_order")
    domains = sorted({d.strip().lower().lstrip("@") for d in value.free_mail_domains if d.strip()})
    before = _settings_out(db).model_dump()
    after = {**value.model_dump(), "free_mail_domains": domains}
    if before == after:
        return False
    set_json(db, reply_scores.SETTINGS_KEY, after, user_id)
    old, new = changes(
        {**before, "free_mail_domains": len(before["free_mail_domains"])},
        {**after, "free_mail_domains": len(domains)},
    )
    log_activity(db, actor, action, entity_type="settings", before=old, after=new)
    db.commit()
    return not before["enabled"] and after["enabled"]


@router.get("/settings", response_model=ScoreSettingsIO)
def read_score_settings(db: DB, _: AdminUser) -> ScoreSettingsIO:
    return _settings_out(db)


@router.put("/settings", response_model=ScoreSettingsIO)
def update_score_settings(
    body: ScoreSettingsIO, db: DB, admin: AdminUser, actor: CurrentActor
) -> ScoreSettingsIO:
    if _save(db, body, admin.id, actor, "settings.score_updated"):
        # Switched on: earlier replies get a score to look at (never the review list or a handover).
        reply_scores.score_existing(db, datetime.now(UTC))
    return _settings_out(db)


@router.put("/thresholds", response_model=ScoreSettingsIO)
def update_thresholds(body: ThresholdsIn, db: DB, admin: AdminUser, actor: CurrentActor) -> ScoreSettingsIO:
    current = _settings_out(db)
    _save(
        db,
        current.model_copy(update=body.model_dump()),
        admin.id,
        actor,
        "settings.score_thresholds_updated",
    )
    return _settings_out(db)


def _rule_out(r: ScoreRule) -> RuleOut:
    return RuleOut(
        id=r.id,
        name=r.name,
        kind=r.kind,
        words=list(r.words or []),
        pattern=r.pattern,
        signal=r.signal,
        points=r.points,
        hard_stop=r.hard_stop,
        enabled=r.enabled,
        position=r.position,
    )


def _check(body: RuleIn) -> dict:
    words = [w.strip() for w in body.words if w.strip()]
    if body.kind == "words" and not words:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "rule_words_required")
    if body.kind == "regex":
        problem = scoring.check_pattern(body.pattern or "") if body.pattern else "A pattern is required."
        if problem:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"rule_pattern_invalid: {problem}")
    if body.kind == "signal" and body.signal not in scoring.SIGNALS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "rule_signal_unknown")
    return {
        "name": body.name.strip(),
        "kind": body.kind,
        "words": words if body.kind == "words" else [],
        "pattern": body.pattern if body.kind == "regex" else None,
        "signal": body.signal if body.kind == "signal" else None,
        "points": body.points,
        "hard_stop": body.hard_stop,
        "enabled": body.enabled,
    }


@router.get("/rules", response_model=list[RuleOut])
def list_rules(db: DB, _: AdminUser) -> list[RuleOut]:
    return [_rule_out(r) for r in db.scalars(select(ScoreRule).order_by(ScoreRule.position, ScoreRule.id))]


@router.post("/rules", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
def create_rule(body: RuleIn, db: DB, _: AdminUser, actor: CurrentActor) -> RuleOut:
    values = _check(body)
    position = body.position
    if position is None:
        position = (db.scalar(select(func.max(ScoreRule.position))) or 0) + 1
    rule = ScoreRule(**values, position=position)
    db.add(rule)
    db.flush()
    log_activity(
        db,
        actor,
        "score_rule.created",
        entity_type="score_rule",
        entity_id=rule.id,
        after=snapshot(rule, RULE_FIELDS),
    )
    db.commit()
    return _rule_out(rule)


def _get_rule(db: DB, rule_id: int) -> ScoreRule:
    rule = db.get(ScoreRule, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule_not_found")
    return rule


@router.put("/rules/{rule_id}", response_model=RuleOut)
def update_rule(rule_id: int, body: RuleIn, db: DB, _: AdminUser, actor: CurrentActor) -> RuleOut:
    rule = _get_rule(db, rule_id)
    before = snapshot(rule, RULE_FIELDS)
    for field, value in _check(body).items():
        setattr(rule, field, value)
    if body.position is not None:
        rule.position = body.position
    old, new = changes(before, snapshot(rule, RULE_FIELDS))
    if new:
        log_activity(
            db,
            actor,
            "score_rule.updated",
            entity_type="score_rule",
            entity_id=rule.id,
            before=old,
            after=new,
        )
        db.commit()
    return _rule_out(rule)


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(rule_id: int, db: DB, _: AdminUser, actor: CurrentActor) -> None:
    rule = _get_rule(db, rule_id)
    log_activity(
        db,
        actor,
        "score_rule.deleted",
        entity_type="score_rule",
        entity_id=rule.id,
        before=snapshot(rule, RULE_FIELDS),
    )
    db.delete(rule)
    db.commit()


@router.post("/try", response_model=TryOut)
def try_score(body: TryIn, db: DB, _: EditorUser) -> TryOut:
    """Score a pasted reply with the current rules. Nothing is saved."""
    return TryOut(**reply_scores.try_text(db, body.text, body.sender.strip(), [c.strip() for c in body.cc]))
