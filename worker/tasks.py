from sqlalchemy.dialects.postgresql import insert

from app.api.health import WORKER_HEARTBEAT_KEY
from app.core.db import SessionLocal
from app.models import Setting
from app.services import sending
from app.services.auth import now
from worker.app import app


@app.periodic(cron="* * * * *")
@app.task(queueing_lock="heartbeat")
def heartbeat(timestamp: int) -> None:
    """Proves the worker is alive. /api/health reports the worker down if this goes stale."""
    value = now().isoformat()
    with SessionLocal() as db:
        db.execute(
            insert(Setting)
            .values(key=WORKER_HEARTBEAT_KEY, value=value)
            .on_conflict_do_update(index_elements=[Setting.key], set_={"value": value, "updated_at": now()})
        )
        db.commit()


@app.periodic(cron="* * * * *")
@app.task(queueing_lock="send-tick")
def send_tick(timestamp: int) -> None:
    """Sends at most one email per due mailbox. Pacing (gaps, caps, hours) lives in app.services.sending."""
    sending.run_tick()
