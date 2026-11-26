"""Deals' do-not-email list (integration step 1).

Deals publishes the people NexaReach must never cold-email, as hashes (app.services.deals_client.email_hash):
its leads at any stage, clients and their contacts, Lost leads for 6 months, Meta ad leads, and everyone who
opted out, bounced, complained or was forgotten. NexaReach keeps a copy (deals_blocklist): the worker fetches
changes every 10 minutes and the whole list once a day. It is checked three times:

1. at import: blocked leads are shown as "Blocked by Deals" (worked out live, so the badge stays right);
2. at campaign Start: Deals is asked directly about every recipient, plus the local copy;
3. before every send (send_mailbox): the local copy, for opening emails and follow-ups alike.

Fail closed: while the switch is on and the copy is older than `stale_minutes` (or was never fetched), no
campaign or follow-up email goes out; a critical alert opens once and the admins are emailed. Warm-up
carries on. Sending restarts by itself after the next good sync.

Everything is off until an admin switches it on (Settings → Deals); then NexaReach behaves as before.
"""

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.crypto import decrypt
from app.core.db import SessionLocal, engine
from app.models import Alert, AlertStatus, DealsBlock, DealsSyncState
from app.services import alerts
from app.services.app_settings import get_json
from app.services.audit import SYSTEM, log_activity
from app.services.deals_client import Client, DealsError, email_hash

log = logging.getLogger(__name__)

SETTINGS_KEY = "deals.settings"
# {"url", "secret_encrypted", "secret_set_at", "blocklist_enabled", "stale_minutes", "optouts_enabled",
#  "handover_enabled", "events_enabled"}
DEFAULT_URL = "https://deals.fastnexa.com"
DEFAULT_STALE_MINUTES = 30
BLOCKLIST = "blocklist"
PAGE_SIZE = 5000
CHECK_BATCH = 1000
# Arbitrary fixed number: only one blocklist sync at a time, whoever starts it (worker or "Sync now").
_LOCK_KEY = 0x0DEA15

REASONS = ("lead", "client", "lost", "meta_lead", "opted_out", "bounced", "complained", "forgotten")


@dataclass(frozen=True)
class DealsSettings:
    url: str
    secret: str | None
    secret_set_at: str | None
    blocklist_enabled: bool
    stale_minutes: int
    optouts_enabled: bool
    handover_enabled: bool
    events_enabled: bool


def settings(db: Session) -> DealsSettings:
    raw = get_json(db, SETTINGS_KEY)
    secret = None
    if raw.get("secret_encrypted"):
        try:
            secret = decrypt(raw["secret_encrypted"])
        except ValueError:
            log.warning("the Deals secret was saved under another encryption key; enter it again")
    return DealsSettings(
        url=raw.get("url") or DEFAULT_URL,
        secret=secret,
        secret_set_at=raw.get("secret_set_at"),
        blocklist_enabled=bool(raw.get("blocklist_enabled")),
        stale_minutes=int(raw.get("stale_minutes") or DEFAULT_STALE_MINUTES),
        optouts_enabled=bool(raw.get("optouts_enabled")),
        handover_enabled=bool(raw.get("handover_enabled")),
        events_enabled=bool(raw.get("events_enabled")),
    )


def client(db: Session, *, test: bool = False) -> Client:
    s = settings(db)
    if not s.secret:
        raise DealsError("not_configured", "The Deals secret isn't set yet (Settings → Deals).")
    return Client(s.url, s.secret, test=test)


def blocklist_enabled(db: Session) -> bool:
    return settings(db).blocklist_enabled


def sync_state(db: Session, name: str) -> DealsSyncState:
    state = db.get(DealsSyncState, name)
    if state is None:
        state = DealsSyncState(name=name)
        db.add(state)
        db.flush()
    return state


# --- The local copy -----------------------------------------------------------------------------------------


def _active(at: datetime) -> Any:
    return or_(DealsBlock.until.is_(None), DealsBlock.until > at)


def active_blocks(db: Session, hashes: Iterable[str], at: datetime) -> dict[str, tuple[str, datetime | None]]:
    """{hash: (reason, until)} for the hashes on the list right now."""
    wanted = list(dict.fromkeys(hashes))
    found: dict[str, tuple[str, datetime | None]] = {}
    for i in range(0, len(wanted), CHECK_BATCH):
        for row in db.scalars(
            select(DealsBlock).where(DealsBlock.hash.in_(wanted[i : i + CHECK_BATCH]), _active(at))
        ):
            found[row.hash] = (row.reason, row.until)
    return found


def block_for(db: Session, email: str, at: datetime) -> tuple[str, datetime | None] | None:
    """Check 3 (before a send): this person's active block, from the local copy, or None."""
    return active_blocks(db, [email_hash(email)], at).get(email_hash(email))


def active_count(db: Session, at: datetime) -> int:
    return db.scalar(select(func.count()).select_from(DealsBlock).where(_active(at))) or 0


def check_for_start(db: Session, hashes: list[str], at: datetime) -> dict[str, tuple[str, datetime | None]]:
    """Check 2 (campaign Start): ask Deals about every recipient, in batches, plus the local copy.
    Raises DealsError when Deals can't be asked: then the campaign doesn't start."""
    c = client(db)
    blocked = active_blocks(db, hashes, at)
    unique = list(dict.fromkeys(hashes))
    for i in range(0, len(unique), CHECK_BATCH):
        answer = c.post("/check", {"hashes": unique[i : i + CHECK_BATCH]}, f"check:{uuid.uuid4()}")
        for item in answer.get("blocked") or []:
            if isinstance(item, dict) and item.get("hash"):
                blocked[item["hash"]] = (str(item.get("reason") or "lead"), _when(item.get("until")))
    return blocked


def _when(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _valid_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


# --- Sync ---------------------------------------------------------------------------------------------------


def _full(db: Session, c: Client, state: DealsSyncState, at: datetime) -> int:
    """Fetch every page of a full snapshot, then replace the whole local table in one transaction."""
    items: dict[str, tuple[str, datetime | None]] = {}
    cursor: str | None = None
    token: str | None = None
    while True:
        params = [("mode", "full"), ("limit", str(PAGE_SIZE))]
        if token:
            params.insert(1, ("page_token", token))
        page = c.get("/blocklist", params)
        for item in page.get("items") or []:
            if _valid_hash(item.get("hash")):
                items[item["hash"]] = (str(item.get("reason") or "lead")[:20], _when(item.get("until")))
        cursor = page.get("cursor") or cursor
        token = page.get("next_page_token")
        if not token:
            break
    if not cursor:
        raise DealsError("unavailable", "Deals' full list came without a cursor.")
    db.execute(delete(DealsBlock))
    rows = [{"hash": h, "reason": r, "until": u} for h, (r, u) in items.items()]
    for i in range(0, len(rows), PAGE_SIZE):
        db.execute(insert(DealsBlock), rows[i : i + PAGE_SIZE])
    state.cursor = cursor
    state.last_full_at = at
    db.commit()
    return len(rows)


def _delta(db: Session, c: Client, state: DealsSyncState) -> int:
    changed = 0
    while True:
        page = c.get("/blocklist", [("since", state.cursor or ""), ("limit", str(PAGE_SIZE))])
        for item in page.get("items") or []:
            h = item.get("hash")
            if not _valid_hash(h):
                continue
            if item.get("removed"):
                db.execute(delete(DealsBlock).where(DealsBlock.hash == h))
            else:
                values = {"reason": str(item.get("reason") or "lead")[:20], "until": _when(item.get("until"))}
                db.execute(
                    insert(DealsBlock)
                    .values(hash=h, **values)
                    .on_conflict_do_update(
                        index_elements=[DealsBlock.hash], set_={**values, "updated_at": func.now()}
                    )
                )
            changed += 1
        if page.get("cursor"):
            state.cursor = page["cursor"]
        db.commit()  # each page, with its cursor
        if not page.get("has_more"):
            return changed


def sync_blocklist(db: Session, at: datetime, *, full: bool = False) -> dict[str, Any]:
    """Bring the local copy up to date: a delta, or a full snapshot (asked for, never done before, or the
    cursor is too old). Records the outcome and opens or closes alerts. Never raises."""
    # The lock lives on a connection of its own: the sync commits as it goes, and each commit can hand the
    # session's connection back to the pool, so a lock taken there would be unlocked on the wrong one.
    lock = engine.connect()
    if not lock.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}):
        lock.close()
        return {"ok": False, "detail": "Another sync is running."}
    try:
        state = sync_state(db, BLOCKLIST)
        try:
            c = client(db)
            if full or not state.cursor or state.last_full_at is None:
                count, mode = _full(db, c, state, at), "full"
            else:
                try:
                    count, mode = _delta(db, c, state), "delta"
                except DealsError as e:
                    if e.kind != "gone":
                        raise
                    count, mode = _full(db, c, state, at), "full"
        except DealsError as e:
            db.rollback()
            state = sync_state(db, BLOCKLIST)
            state.last_error = str(e)[:1000]
            state.last_error_at = at
            problem_alerts(db, e, at, target="blocklist")
            db.commit()
            log.warning("Deals blocklist sync failed: %s", e)
            return {"ok": False, "detail": str(e)}
        state.last_success_at = at
        state.last_error = None
        state.last_error_at = None
        problem_alerts(db, None, at, target="blocklist")
        db.commit()
        check_stale(db, at)
        return {"ok": True, "mode": mode, "changed": count}
    finally:
        lock.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
        lock.close()


def problem_alerts(
    db: Session, e: DealsError | None, at: datetime, *, target: str, failing: str | None = None
) -> None:
    """Alerts for one kind of call to Deals (`target`: blocklist, optouts…). A bad signature is critical;
    data Deals refused is a warning, and so is `failing` (a call that keeps failing). A success closes this
    target's alerts. For the blocklist, network trouble needs no alert of its own: the copy going stale
    raises the critical one."""
    signature = [alerts.Condition(str(e), target=target)] if e and e.kind == "signature" else []
    problem = str(e) if e and e.kind in ("invalid", "conflict") else failing
    error = [alerts.Condition(problem, target=target)] if problem else []
    new = alerts.sync(db, "deals_signature", signature, at, scope={target}) + alerts.sync(
        db, "deals_error", error, at, scope={target}
    )
    if new:
        db.commit()
        alerts.email_admins(db, new, at)


def run_blocklist_sync(*, full: bool = False) -> None:
    """Worker entry point. Does nothing while the switch is off or no secret is saved."""
    with SessionLocal() as db:
        s = settings(db)
        if not s.blocklist_enabled or not s.secret:
            return
        sync_blocklist(db, datetime.now(UTC), full=full)


# --- Fail closed --------------------------------------------------------------------------------------------


def stale_since(db: Session, at: datetime) -> tuple[bool, datetime | None]:
    """(is the copy too old to send by, when it was last fetched)."""
    s = settings(db)
    state = db.get(DealsSyncState, BLOCKLIST)
    last = state.last_success_at if state else None
    if not s.blocklist_enabled:
        return False, last
    return last is None or at - last > timedelta(minutes=s.stale_minutes), last


def campaigns_held(db: Session, at: datetime) -> bool:
    """True while campaign and follow-up emails must wait for a fresh copy of the list."""
    return stale_since(db, at)[0]


def check_stale(db: Session, at: datetime) -> None:
    """Open the critical alert once when the copy goes stale (and email the admins), close it after a good
    sync, and log both. Called every minute by the worker and after each sync. Commits."""
    stale, last = stale_since(db, at)
    was_open = any(
        a.status != AlertStatus.RESOLVED for a in db.scalars(select(Alert).where(Alert.kind == "deals_stale"))
    )
    conditions = []
    if stale:
        since = f"since {last:%d %b %Y, %H:%M} UTC" if last else "(never fetched)"
        conditions.append(
            alerts.Condition(
                f"Deals do-not-email list is out of date {since}. Campaign and follow-up emails are on hold "
                "until it syncs again; warm-up carries on.",
                target="deals",
            )
        )
    new = alerts.sync(db, "deals_stale", conditions, at)
    after = {"last_sync": _iso(last)}
    if new:
        log_activity(db, SYSTEM, "deals.sending_held", entity_type="settings", after=after)
    elif was_open and not stale:
        log_activity(db, SYSTEM, "deals.sending_resumed", entity_type="settings", after=after)
    db.commit()
    if new:
        alerts.email_admins(db, new, at)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def status(db: Session, at: datetime) -> dict[str, Any]:
    state = db.get(DealsSyncState, BLOCKLIST)
    stale, _ = stale_since(db, at)
    return {
        "blocklist": {
            "last_success_at": state.last_success_at if state else None,
            "last_full_at": state.last_full_at if state else None,
            "last_error": state.last_error if state else None,
            "last_error_at": state.last_error_at if state else None,
            "active_blocks": active_count(db, at),
        },
        "campaigns_held": stale,
    }


def is_production() -> bool:
    return get_settings().app_env == "production"
