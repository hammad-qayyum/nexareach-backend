"""Settings → Deals (admins only): the connection to the Deals CRM and its switches."""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import DB, AdminUser, CurrentActor, CurrentUser
from app.core.config import get_settings
from app.core.crypto import encrypt
from app.services import deals, deals_events, deals_handover, deals_optouts
from app.services.app_settings import get_json, set_json
from app.services.audit import log_activity
from app.services.deals_client import DealsError

settings_router = APIRouter(prefix="/settings/deals", tags=["deals"])
SERVICES_CACHE_KEY = "deals.services_cache"
router = APIRouter(prefix="/deals", tags=["deals"])


class DealsSettingsOut(BaseModel):
    url: str
    # The secret is never sent back; only whether and when it was set.
    secret_set: bool
    secret_set_at: str | None
    blocklist_enabled: bool
    stale_minutes: int
    optouts_enabled: bool
    handover_enabled: bool
    events_enabled: bool


class DealsSettingsIn(BaseModel):
    url: str = Field(default=deals.DEFAULT_URL, max_length=255)
    # None or blank keeps the saved secret.
    secret: str | None = Field(default=None, max_length=500)
    blocklist_enabled: bool = False
    stale_minutes: int = Field(default=deals.DEFAULT_STALE_MINUTES, ge=5, le=1440)
    optouts_enabled: bool = False
    handover_enabled: bool = False
    events_enabled: bool = False


class TestOut(BaseModel):
    ok: bool
    detail: str


class SyncStateOut(BaseModel):
    last_success_at: datetime | None
    last_full_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    active_blocks: int


class OptoutsStateOut(BaseModel):
    last_success_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    # Opt-outs not accepted by Deals yet.
    waiting: int
    # Batches given up on (Deals refused them, or 3 days of failures).
    failed_batches: int


class EventsStateOut(BaseModel):
    last_success_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None


class DealsStatusOut(BaseModel):
    blocklist: SyncStateOut
    optouts: OptoutsStateOut
    events: EventsStateOut
    # Handovers by status: queued, created, existing, blocked, failed.
    handovers: dict[str, int]
    # Campaign and follow-up emails are on hold because the list copy is too old.
    campaigns_held: bool
    # Outside production every call is a test call, whatever the settings say.
    test_mode: bool


def _out(db: DB) -> DealsSettingsOut:
    s = deals.settings(db)
    return DealsSettingsOut(
        url=s.url,
        secret_set=bool(get_json(db, deals.SETTINGS_KEY).get("secret_encrypted")),
        secret_set_at=s.secret_set_at,
        blocklist_enabled=s.blocklist_enabled,
        stale_minutes=s.stale_minutes,
        optouts_enabled=s.optouts_enabled,
        handover_enabled=s.handover_enabled,
        events_enabled=s.events_enabled,
    )


def _clean_url(url: str) -> str:
    url = url.strip().rstrip("/") or deals.DEFAULT_URL
    parsed = urlparse(url)
    # HTTPS only, except a local fake Deals server on a test copy.
    local = parsed.hostname in ("localhost", "127.0.0.1") and get_settings().is_test_copy
    if not parsed.hostname or (parsed.scheme != "https" and not (local and parsed.scheme == "http")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "deals_url_invalid")
    if parsed.path or parsed.query:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "deals_url_invalid")
    return url


@settings_router.get("", response_model=DealsSettingsOut)
def read_deals_settings(db: DB, _: AdminUser) -> DealsSettingsOut:
    return _out(db)


@settings_router.put("", response_model=DealsSettingsOut)
def update_deals_settings(
    body: DealsSettingsIn, db: DB, admin: AdminUser, actor: CurrentActor, background: BackgroundTasks
) -> DealsSettingsOut:
    before = get_json(db, deals.SETTINGS_KEY)
    secret = (body.secret or "").strip()
    switched_on = (
        body.blocklist_enabled or body.optouts_enabled or body.handover_enabled or body.events_enabled
    )
    if switched_on and not secret and not before.get("secret_encrypted"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "deals_secret_required")
    now = datetime.now(UTC)
    value: dict[str, Any] = {
        **before,
        "url": _clean_url(body.url),
        "blocklist_enabled": body.blocklist_enabled,
        "stale_minutes": body.stale_minutes,
        "optouts_enabled": body.optouts_enabled,
        "handover_enabled": body.handover_enabled,
        "events_enabled": body.events_enabled,
    }
    if secret:
        value["secret_encrypted"] = encrypt(secret)
        value["secret_set_at"] = now.isoformat()
    set_json(db, deals.SETTINGS_KEY, value, admin.id, secret=True)
    shown = (
        "url",
        "blocklist_enabled",
        "stale_minutes",
        "optouts_enabled",
        "handover_enabled",
        "events_enabled",
    )
    log_activity(
        db,
        actor,
        "settings.deals_updated",
        entity_type="settings",
        before={k: before.get(k) for k in shown},
        after={**{k: value.get(k) for k in shown}, "secret_changed": bool(secret)},
    )
    db.commit()
    # Switching the list off lets held emails go at once; switching it on fetches the list straight away,
    # so sending is held for seconds rather than until the next 10-minute sync.
    deals.check_stale(db, now)
    if body.blocklist_enabled and not before.get("blocklist_enabled"):
        background.add_task(deals.run_blocklist_sync)
    if body.optouts_enabled and not before.get("optouts_enabled"):
        background.add_task(deals_optouts.run_push)  # existing opt-outs go at once
    return _out(db)


@settings_router.post("/test", response_model=TestOut)
def test_deals_connection(db: DB, _: AdminUser) -> TestOut:
    """Calls Deals' service list in test mode: proves the address, the secret and the clock are right."""
    try:
        answer = deals.client(db, test=True).get("/services")
    except DealsError as e:
        return TestOut(ok=False, detail=str(e))
    count = len(answer.get("services") or [])
    return TestOut(ok=True, detail=f"Connected. Deals lists {count} service{'' if count == 1 else 's'}.")


@router.get("/status", response_model=DealsStatusOut)
def deals_status(db: DB, _: AdminUser) -> DealsStatusOut:
    s = deals.status(db, datetime.now(UTC))
    return DealsStatusOut(
        blocklist=SyncStateOut(**s["blocklist"]),
        optouts=OptoutsStateOut(**deals_optouts.status(db)),
        events=EventsStateOut(**deals_events.status(db)),
        handovers=deals_handover.status_counts(db),
        campaigns_held=s["campaigns_held"],
        test_mode=not deals.is_production(),
    )


@router.post("/blocklist/sync", response_model=TestOut)
def sync_blocklist_now(db: DB, _: AdminUser, actor: CurrentActor) -> TestOut:
    """Fetch Deals' whole do-not-email list now instead of waiting for the worker."""
    if not deals.settings(db).secret:
        return TestOut(ok=False, detail="Save the Deals secret first.")
    log_activity(db, actor, "deals.blocklist_sync_requested", entity_type="settings")
    db.commit()
    result = deals.sync_blocklist(db, datetime.now(UTC), full=True)
    if not result["ok"]:
        return TestOut(ok=False, detail=result["detail"])
    n = result["changed"]
    return TestOut(ok=True, detail=f"Up to date: {n} {'person' if n == 1 else 'people'} on the list.")


class ServiceOut(BaseModel):
    key: str
    name: str


class ServicesOut(BaseModel):
    services: list[ServiceOut]
    # When the list couldn't be fetched (Deals not set up or down): campaigns show the saved key as text.
    error: str | None = None


@router.get("/services", response_model=ServicesOut)
def deals_services(db: DB, _: CurrentUser) -> ServicesOut:
    """Deals' services for the campaign's Deals service dropdown. Cached for an hour."""
    cache = get_json(db, SERVICES_CACHE_KEY)
    fresh = cache.get("at") and datetime.fromisoformat(cache["at"]) > datetime.now(UTC) - timedelta(hours=1)
    if fresh:
        return ServicesOut(services=[ServiceOut(**x) for x in cache.get("services") or []])
    try:
        answer = deals.client(db).get("/services")
    except DealsError as e:
        return ServicesOut(services=[ServiceOut(**x) for x in cache.get("services") or []], error=str(e))
    services = [
        {"key": str(x["key"]), "name": str(x.get("name") or x["key"])}
        for x in answer.get("services") or []
        if isinstance(x, dict) and x.get("key")
    ]
    set_json(db, SERVICES_CACHE_KEY, {"at": datetime.now(UTC).isoformat(), "services": services}, None)
    db.commit()
    return ServicesOut(services=[ServiceOut(**x) for x in services])
