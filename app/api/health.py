from datetime import datetime

from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import select, text

from app.api.deps import DB
from app.models import Setting
from app.services.auth import now

router = APIRouter(tags=["health"])

WORKER_HEARTBEAT_KEY = "worker.heartbeat"
# The worker writes a heartbeat every minute; older than this counts as down.
WORKER_STALE_SECONDS = 180


class HealthOut(BaseModel):
    database: bool
    worker: bool
    worker_last_seen: datetime | None


@router.get("/health", response_model=HealthOut)
def health(db: DB, response: Response) -> HealthOut:
    """For Uptime Kuma. Returns 503 when the database or worker is down."""
    try:
        db.execute(text("SELECT 1"))
        database_ok = True
    except Exception:
        database_ok = False

    last_seen = None
    if database_ok:
        value = db.scalar(select(Setting.value).where(Setting.key == WORKER_HEARTBEAT_KEY))
        last_seen = datetime.fromisoformat(value) if value else None
    worker_ok = last_seen is not None and (now() - last_seen).total_seconds() < WORKER_STALE_SECONDS

    if not (database_ok and worker_ok):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthOut(database=database_ok, worker=worker_ok, worker_last_seen=last_seen)
