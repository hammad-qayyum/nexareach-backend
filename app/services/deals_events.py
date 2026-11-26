"""Results from Deals (integration step 5): what happened to every lead NexaReach handed over.

Every 5 minutes the worker asks Deals for events after its cursor (GET /events): stage changes (with the Won
value in whole US dollars and the Lost reason), merges and deletions. Each event is stored once, in order,
and each lead's current state is kept in deals_lead_results, which the reports read. Deals keeps 30 days of
events, so a few days of downtime loses nothing.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models import DealsHandover, DealsLeadEvent, DealsLeadResult, DealsSyncState, Lead
from app.services import deals
from app.services.audit import SYSTEM, log_activity
from app.services.deals_client import DealsError
from app.services.deals_handover import reply_id_from_deals

log = logging.getLogger(__name__)

EVENTS = "events"
PAGE = 200
STAGES = ("new", "contacted", "discovery", "proposal", "negotiation", "won", "lost", "disqualified")
# Proposal or further, for "reached proposal" in the reports.
PROPOSAL_OR_LATER = ("proposal", "negotiation", "won")


def enabled(db: Session) -> bool:
    return deals.settings(db).events_enabled


def _when(value: Any) -> datetime:
    parsed = deals._when(value)  # same parsing as the blocklist's dates
    return parsed or datetime.now(UTC)


def _handover_for(db: Session, lead_id: str, reply_id: str | None) -> DealsHandover | None:
    handover = db.scalar(select(DealsHandover).where(DealsHandover.deals_lead_id == lead_id).limit(1))
    rid = reply_id_from_deals(reply_id)
    if handover is None and rid is not None:
        handover = db.scalar(select(DealsHandover).where(DealsHandover.reply_id == rid))
    return handover


def _result(db: Session, lead_id: str, reply_id: str | None, at: datetime) -> DealsLeadResult:
    result = db.get(DealsLeadResult, lead_id)
    if result is None:
        handover = _handover_for(db, lead_id, reply_id)
        result = DealsLeadResult(
            deals_lead_id=lead_id, handover_id=handover.id if handover else None, stage="new", updated_at=at
        )
        db.add(result)
        db.flush()
    return result


def apply(db: Session, event: dict[str, Any]) -> bool:
    """Store one event and update the lead's result. Returns False for an event seen before."""
    event_id = str(event.get("id") or "")
    lead_id = str(event.get("lead_id") or "")
    if not event_id or not lead_id or db.get(DealsLeadEvent, event_id) is not None:
        return False
    at = _when(event.get("at"))
    kind = str(event.get("type") or "")
    value = event.get("value_usd")
    db.add(
        DealsLeadEvent(
            id=event_id,
            type=kind,
            deals_lead_id=lead_id,
            into_lead_id=event.get("into_lead_id"),
            reply_id=reply_id_from_deals(event.get("reply_id")),
            from_stage=event.get("from"),
            to_stage=event.get("to"),
            value_usd=int(value) if isinstance(value, int | float) else None,
            lost_reason=event.get("lost_reason"),
            at=at,
        )
    )
    result = _result(db, lead_id, event.get("reply_id"), at)
    if kind == "stage_changed":
        result.stage = str(event.get("to") or result.stage)
        if result.stage == "won":
            result.value_usd = int(value) if isinstance(value, int | float) else result.value_usd
        if result.stage == "lost":
            result.lost_reason = event.get("lost_reason") or result.lost_reason
        result.updated_at = at
    elif kind == "lead_merged" and event.get("into_lead_id"):
        _merge(db, result, str(event["into_lead_id"]), at)
    elif kind == "lead_deleted":
        result.stage = "deleted"
        result.updated_at = at
    return True


def _merge(db: Session, result: DealsLeadResult, into: str, at: datetime) -> None:
    """The lead became part of another one in Deals: point everything at the surviving lead."""
    old = result.deals_lead_id
    target = db.get(DealsLeadResult, into)
    if target is None:
        db.add(
            DealsLeadResult(
                deals_lead_id=into,
                handover_id=result.handover_id,
                stage=result.stage,
                value_usd=result.value_usd,
                lost_reason=result.lost_reason,
                updated_at=at,
            )
        )
    db.delete(result)
    db.execute(update(DealsHandover).where(DealsHandover.deals_lead_id == old).values(deals_lead_id=into))
    db.execute(update(Lead).where(Lead.deals_lead_id == old).values(deals_lead_id=into))
    db.flush()


def sync(db: Session, at: datetime) -> dict[str, Any]:
    """Fetch and apply new events, page by page (the cursor is saved after each page). Never raises."""
    state = deals.sync_state(db, EVENTS)
    applied = 0
    try:
        client = deals.client(db)
        while True:
            params = [("limit", str(PAGE))]
            if state.cursor:
                params.insert(0, ("after", state.cursor))
            page = client.get("/events", params)
            for event in page.get("events") or []:
                if isinstance(event, dict) and apply(db, event):
                    applied += 1
            if page.get("cursor"):
                state.cursor = str(page["cursor"])
            db.commit()
            if not page.get("has_more"):
                break
    except DealsError as e:
        db.rollback()
        state = deals.sync_state(db, EVENTS)
        state.last_error, state.last_error_at = str(e)[:1000], at
        deals.problem_alerts(db, e, at, target=EVENTS)
        db.commit()
        return {"ok": False, "detail": str(e)}
    state.last_success_at, state.last_error, state.last_error_at = at, None, None
    if applied:
        log_activity(db, SYSTEM, "deals.events_received", entity_type="settings", after={"events": applied})
    deals.problem_alerts(db, None, at, target=EVENTS)
    db.commit()
    return {"ok": True, "applied": applied}


def run() -> None:
    """Worker entry point (every 5 minutes)."""
    with SessionLocal() as db:
        if enabled(db) and deals.settings(db).secret:
            sync(db, datetime.now(UTC))


def status(db: Session) -> dict[str, Any]:
    state = db.get(DealsSyncState, EVENTS)
    return {
        "last_success_at": state.last_success_at if state else None,
        "last_error": state.last_error if state else None,
        "last_error_at": state.last_error_at if state else None,
    }
