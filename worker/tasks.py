from procrastinate.exceptions import AlreadyEnqueued
from sqlalchemy.dialects.postgresql import insert

from app.api.health import WORKER_HEARTBEAT_KEY
from app.core.db import SessionLocal
from app.models import Setting
from app.services import deliverability, dmarc, domain_health, placement, replies, sending, warmup
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
    """Plans the minute: each due mailbox gets a job at a random second. Pacing (gaps, caps, hours, domain
    spacing, company limit) lives in app.services.sending and app.services.pacing."""
    for when, mailbox_id in sending.plan_tick():
        try:
            # One waiting job per mailbox; the lock stops two sends from one mailbox overlapping.
            send_mailbox.configure(
                schedule_at=when, queueing_lock=f"send-{mailbox_id}", lock=f"send-{mailbox_id}"
            ).defer(mailbox_id=mailbox_id)
        except AlreadyEnqueued:
            pass  # still waiting from the last minute (the worker was busy)


@app.task(name="send_mailbox")
def send_mailbox(mailbox_id: int) -> None:
    """Sends at most one email from this mailbox (if it's still due when the job runs)."""
    sending.send_one(mailbox_id)


@app.periodic(cron="*/5 * * * *")
@app.task(queueing_lock="reply-check")
def reply_check(timestamp: int) -> None:
    """Reads each mailbox's inbox for replies, opt-outs and bounces (app.services.replies)."""
    replies.check_all()


@app.periodic(cron="*/5 * * * *")
@app.task(queueing_lock="warmup-tick")
def warmup_tick(timestamp: int) -> None:
    """Helpers act on due warm-up emails; helper answers are filed into each mailbox's Warmup folder."""
    warmup.run_helpers()
    warmup.run_filing()


@app.periodic(cron="7 * * * *")
@app.task(queueing_lock="warmup-health")
def warmup_health(timestamp: int) -> None:
    """Pauses campaign sending from mailboxes whose warm-up inbox rate stayed under 50% for 3 days."""
    warmup.run_health()


@app.periodic(cron="*/10 * * * *")
@app.task(queueing_lock="deliverability")
def deliverability_check(timestamp: int) -> None:
    """Reads cPanel's delivery log, then alerts on hard bounces, delays and failing logins."""
    deliverability.run()


@app.periodic(cron="20 1 * * *")
@app.task(queueing_lock="health-checks")
def health_checks(timestamp: int) -> None:
    """Daily (06:20 in Pakistan): SPF, DKIM, DMARC, MX, certificates and blacklists."""
    with SessionLocal() as db:
        domain_health.run(db, now())


@app.periodic(cron="40 */6 * * *")
@app.task(queueing_lock="dmarc-check")
def dmarc_check(timestamp: int) -> None:
    """Reads new DMARC reports from the reports mailbox (receivers send one a day)."""
    with SessionLocal() as db:
        dmarc.check(db, now())


@app.periodic(cron="* * * * *")
@app.task(queueing_lock="placement-tick")
def placement_tick(timestamp: int) -> None:
    """Sends planned placement-test emails and looks for the sent ones at the helpers."""
    with SessionLocal() as db:
        placement.run(db, now())


@app.periodic(cron="0 6 * * 1")
@app.task(queueing_lock="placement-weekly")
def placement_weekly(timestamp: int) -> None:
    """Mondays 11:00 in Pakistan: a placement test for each mailbox that sent campaign emails lately."""
    with SessionLocal() as db:
        placement.weekly(db, now())
