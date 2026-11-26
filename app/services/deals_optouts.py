"""Opt-outs shared with Deals (integration step 2): one opt-out list for NexaReach, Deals and CyberBrief.

Whenever NexaReach adds someone to do-not-contact (an opt-out reply, a hard bounce, a spam complaint, an entry
added by hand, a GDPR forget), a row goes into deals_optout_outbox in the same transaction. Every 2 minutes
the worker sends up to 500 of them to Deals (POST /optouts). Each batch keeps its exact items, so a retry
sends the same Idempotency-Key with the same body. A GDPR forget sends the hash only, never the address.

When the switch is first turned on (and on every run after), entries made earlier are caught up: each
do-not-contact address is shared once. Whole domains can't be shared (Deals takes addresses), and entries that
came from Deals itself are never sent back. Forgets are queued even while the switch is off (hash only), since
their address is gone afterwards.

Opt-outs from Deals and CyberBrief need nothing here: they arrive through the do-not-email list.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import cast, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy.types import String

from app.core.db import SessionLocal
from app.models import DealsOptout, DealsOptoutBatch, DealsSyncState, DncKind, DoNotContact
from app.services import deals
from app.services.audit import SYSTEM, log_activity
from app.services.deals_client import DealsError, email_hash

log = logging.getLogger(__name__)

OPTOUTS = "optouts"
BATCH_SIZE = 500
# Retry after 1, 5, 15 and 60 minutes, then hourly; give up after 3 days (contract section 4.2).
RETRY_MINUTES = (1, 5, 15, 60)
GIVE_UP_AFTER = timedelta(days=3)
# Entries that came from Deals itself (a handover answered "blocked"): never sent back.
FROM_DEALS_REASON = "Opted out in Deals"

# Do-not-contact reasons written by NexaReach → the reason Deals expects.
_REASONS = {
    "Asked not to be emailed (found in their reply)": "unsubscribed",
    "Email address doesn't exist (hard bounce)": "bounced",
    "Marked our email as spam (complaint)": "complained",
}
COMPLAINT_REASON = "Marked our email as spam (complaint)"


def deals_reason(dnc_reason: str | None) -> str:
    return _REASONS.get(dnc_reason or "", "manual")


def enabled(db: Session) -> bool:
    return deals.settings(db).optouts_enabled


def queue(db: Session, entry: DoNotContact, reason: str | None = None) -> None:
    """Share this new do-not-contact entry (an address) with Deals. Call in the same transaction, after a
    flush. Does nothing while the switch is off (the catch-up picks it up when it's turned on)."""
    if entry.kind != DncKind.EMAIL or entry.value.startswith("forgotten:"):
        return
    if entry.reason == FROM_DEALS_REASON:
        return
    if not enabled(db):
        return
    db.execute(
        insert(DealsOptout)
        .values(
            hash=email_hash(entry.value),
            email=entry.value,
            reason=reason or deals_reason(entry.reason),
            at=entry.created_at or datetime.now(UTC),
            ref=str(entry.id),
        )
        .on_conflict_do_nothing(index_elements=[DealsOptout.ref])
    )


def queue_value(db: Session, value: str) -> None:
    """For code that inserts do-not-contact rows with INSERT … RETURNING value."""
    entry = db.scalar(select(DoNotContact).where(DoNotContact.value == value))
    if entry is not None:
        queue(db, entry)


def forgotten(db: Session, email: str, entry_id: int, at: datetime) -> None:
    """A GDPR forget: share the hash only, and take the address off anything not sent yet. Unsent batches
    that held it are replaced (their body had the address), so a retry never sends it. Queued even while
    the switch is off: afterwards nobody can work out the hash again."""
    h = email_hash(email)
    stale = db.scalars(
        select(DealsOptout.batch_id).where(
            DealsOptout.hash == h, DealsOptout.sent_at.is_(None), DealsOptout.batch_id.is_not(None)
        )
    ).all()
    for batch_id in set(stale):
        db.execute(update(DealsOptoutBatch).where(DealsOptoutBatch.id == batch_id).values(superseded=True))
        db.execute(
            update(DealsOptout)
            .where(DealsOptout.batch_id == batch_id, DealsOptout.sent_at.is_(None))
            .values(batch_id=None)
        )
    db.execute(
        update(DealsOptout).where(DealsOptout.hash == h, DealsOptout.sent_at.is_(None)).values(email=None)
    )
    db.execute(
        insert(DealsOptout)
        .values(hash=h, email=None, reason="forgotten", at=at, ref=str(entry_id))
        .on_conflict_do_nothing(index_elements=[DealsOptout.ref])
    )


def catch_up(db: Session) -> int:
    """Queue every do-not-contact address not shared yet (made before the switch was on, or while it was off).
    Returns how many."""
    missing = db.scalars(
        select(DoNotContact).where(
            DoNotContact.kind == DncKind.EMAIL,
            ~DoNotContact.value.startswith("forgotten:"),
            DoNotContact.reason.is_distinct_from(FROM_DEALS_REASON),
            ~exists().where(DealsOptout.ref == cast(DoNotContact.id, String)),
        )
    ).all()
    for entry in missing:
        db.execute(
            insert(DealsOptout)
            .values(
                hash=email_hash(entry.value),
                email=entry.value,
                reason=deals_reason(entry.reason),
                at=entry.created_at,
                ref=str(entry.id),
            )
            .on_conflict_do_nothing(index_elements=[DealsOptout.ref])
        )
    return len(missing)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _item(row: DealsOptout) -> dict[str, Any]:
    item: dict[str, Any] = {"hash": row.hash}
    if row.email and row.reason != "forgotten":  # never an address for a forget
        item["email"] = row.email
    return {**item, "reason": row.reason, "at": _iso(row.at), "ref": row.ref}


def _next_batch(db: Session, at: datetime) -> DealsOptoutBatch | None:
    """A batch waiting for a retry that is due, else a new one from unsent rows."""
    waiting = db.scalar(
        select(DealsOptoutBatch)
        .where(
            DealsOptoutBatch.sent_at.is_(None),
            DealsOptoutBatch.gave_up_at.is_(None),
            DealsOptoutBatch.superseded.is_(False),
        )
        .order_by(DealsOptoutBatch.created_at)
        .limit(1)
    )
    if waiting is not None:
        return waiting if waiting.next_attempt_at is None or waiting.next_attempt_at <= at else None
    rows = db.scalars(
        select(DealsOptout)
        .where(DealsOptout.batch_id.is_(None), DealsOptout.sent_at.is_(None))
        .order_by(DealsOptout.id)
        .limit(BATCH_SIZE)
    ).all()
    if not rows:
        return None
    batch = DealsOptoutBatch(id=str(uuid.uuid4()), items=[_item(r) for r in rows], created_at=at, attempts=0)
    db.add(batch)
    db.flush()
    for row in rows:
        row.batch_id = batch.id
    db.commit()  # the body is fixed before the first try
    return batch


def push(db: Session, at: datetime) -> dict[str, Any]:
    """Catch up, then send what's waiting, one batch after another. Never raises. Commits."""
    if not enabled(db) or not deals.settings(db).secret:
        return {"ok": True, "sent": 0}
    if catch_up(db):
        db.commit()
    sent = 0
    while (batch := _next_batch(db, at)) is not None:
        try:
            answer = deals.client(db).post("/optouts", {"items": batch.items}, f"optouts:{batch.id}")
        except DealsError as e:
            _failed(db, batch, e, at)
            return {"ok": False, "sent": sent, "detail": str(e)}
        batch.sent_at = at
        batch.last_error = None
        db.execute(update(DealsOptout).where(DealsOptout.batch_id == batch.id).values(sent_at=at))
        state = deals.sync_state(db, OPTOUTS)
        state.last_success_at, state.last_error, state.last_error_at = at, None, None
        log_activity(
            db,
            SYSTEM,
            "deals.optouts_pushed",
            entity_type="settings",
            after={"batch": batch.id, "items": len(batch.items), "accepted": answer.get("accepted")},
        )
        deals.problem_alerts(db, None, at, target=OPTOUTS)
        db.commit()
        sent += len(batch.items)
    return {"ok": True, "sent": sent}


def _failed(db: Session, batch: DealsOptoutBatch, e: DealsError, at: datetime) -> None:
    batch.attempts += 1
    batch.last_error = str(e)[:1000]
    state = deals.sync_state(db, OPTOUTS)
    state.last_error, state.last_error_at = batch.last_error, at
    failing = None
    if not e.retryable or at - batch.created_at >= GIVE_UP_AFTER:
        # 400 / 409 / 401 / no secret: retrying the same body can't help. Or 3 days of trying.
        batch.gave_up_at = at
        failing = f"{len(batch.items)} opt-outs couldn't be shared with Deals: {e}"
        log_activity(
            db,
            SYSTEM,
            "deals.optouts_failed",
            entity_type="settings",
            after={"batch": batch.id, "items": len(batch.items), "error": str(e), "kind": e.kind},
        )
    else:
        if e.kind == "rate_limited" and e.retry_after:
            wait = timedelta(seconds=e.retry_after)
        else:
            minutes = RETRY_MINUTES[batch.attempts - 1] if batch.attempts <= len(RETRY_MINUTES) else 60
            wait = timedelta(minutes=minutes)
        batch.next_attempt_at = at + wait
        if batch.attempts >= len(RETRY_MINUTES):
            failing = f"Opt-outs can't reach Deals (tried {batch.attempts} times): {e} Still retrying hourly."
    deals.problem_alerts(db, e, at, target=OPTOUTS, failing=failing)
    db.commit()


def status(db: Session) -> dict[str, Any]:
    state = db.get(DealsSyncState, OPTOUTS)
    waiting = db.scalar(select(func.count()).select_from(DealsOptout).where(DealsOptout.sent_at.is_(None)))
    failed = db.scalar(
        select(func.count()).select_from(DealsOptoutBatch).where(DealsOptoutBatch.gave_up_at.is_not(None))
    )
    return {
        "last_success_at": state.last_success_at if state else None,
        "last_error": state.last_error if state else None,
        "last_error_at": state.last_error_at if state else None,
        "waiting": waiting or 0,
        "failed_batches": failed or 0,
    }


def run_push() -> None:
    """Worker entry point (every 2 minutes)."""
    with SessionLocal() as db:
        push(db, datetime.now(UTC))
