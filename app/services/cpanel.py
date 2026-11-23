"""cPanel's delivery log ("Track Delivery"): what the receiving server did with each email (plan section 19).

Every 10 minutes the worker asks cPanel (UAPI EmailTrack/search, with a user-level API token from Settings)
for the last few days of outgoing mail, and ties each line to the email we sent: by the mail server's queue
ID (in our send log, "250 OK id=..."), else by the address and time. Delivered, delayed and failed lines
become events in the log viewer; a failure is a bounce (a hard one puts the address on do-not-contact).

Note: if the host relays outgoing mail through another service (InterServer uses MailBaby), "delivered"
can mean "handed to the relay"; bounces from further on still come back as bounce emails.
"""

import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import certifi
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import decrypt
from app.models import (
    CampaignRecipient,
    DeliverySource,
    DeliveryStatus,
    FollowUpEmail,
    FollowUpStatus,
    Mailbox,
    RecipientStatus,
)
from app.services import alerts, delivery
from app.services.app_settings import get_json, set_json

log = logging.getLogger(__name__)

SETTINGS_KEY = "deliverability.cpanel"  # {"host", "port", "username", "token_encrypted"}
STATE_KEY = "deliverability.cpanel_state"  # {"synced_at", "error", "lines"}
LOOK_BACK = timedelta(days=3)
# Without a queue ID, a log line matches an email to the same address sent this close in time.
MATCH_WINDOW = timedelta(minutes=15)
TIMEOUT_SECONDS = 30


class CpanelFailed(Exception):
    pass


@dataclass
class Config:
    host: str
    port: int
    username: str
    token: str


Fetch = Callable[[Config, int], list[dict[str, Any]]]


def config(db: Session) -> Config | None:
    cfg = get_json(db, SETTINGS_KEY)
    if not (cfg.get("host") and cfg.get("username") and cfg.get("token_encrypted")):
        return None
    try:
        token = decrypt(cfg["token_encrypted"])
    except ValueError:
        return None
    return Config(cfg["host"], int(cfg.get("port") or 2083), cfg["username"], token)


def fetch(cfg: Config, since: int) -> list[dict[str, Any]]:
    """Outgoing-mail log lines sent after `since` (unix time). Raises CpanelFailed."""
    params = {
        "success": 1,
        "defer": 1,
        "failure": 1,
        "inprogress": 0,
        "deliverytype": "remote",
        "max_results_by_type": 1000,
        "api.sort": 1,
        "api.sort_column": "sendunixtime",
        "api.sort_method": "numeric",
        "api.filter": 1,
        "api.filter_column_0": "sendunixtime",
        "api.filter_type_0": "gt",
        "api.filter_term_0": since,
    }
    host = cfg.host.strip().removeprefix("https://").split("/")[0].split(":")[0]
    url = f"https://{host}:{cfg.port}/execute/EmailTrack/search?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"Authorization": f"cpanel {cfg.username}:{cfg.token}"})  # noqa: S310
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS, context=context) as response:  # noqa: S310
            body = json.load(response)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise CpanelFailed("cPanel refused the username or API token.") from e
        raise CpanelFailed(f"cPanel answered {e.code} {e.reason}.") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise CpanelFailed(f"Couldn't reach cPanel at {host}:{cfg.port} ({e}).") from e
    if not isinstance(body, dict) or not body.get("status"):
        errors = body.get("errors") if isinstance(body, dict) else None
        raise CpanelFailed(
            "cPanel refused the request: " + ("; ".join(map(str, errors or [])) or "no reason given")
        )
    return [r for r in body.get("data") or [] if isinstance(r, dict)]


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


@dataclass
class _Sent:
    recipient: CampaignRecipient
    follow_up: FollowUpEmail | None
    mailbox_id: int
    email: str
    sent_at: datetime


def _sent_emails(db: Session, since: datetime) -> list[_Sent]:
    out = [
        _Sent(r, None, r.mailbox_id, r.email, r.sent_at)
        for r in db.scalars(
            select(CampaignRecipient).where(
                CampaignRecipient.status == RecipientStatus.SENT, CampaignRecipient.sent_at >= since
            )
        )
    ]
    for f, r in db.execute(
        select(FollowUpEmail, CampaignRecipient)
        .join(CampaignRecipient, CampaignRecipient.id == FollowUpEmail.recipient_id)
        .where(FollowUpEmail.status == FollowUpStatus.SENT, FollowUpEmail.sent_at >= since)
    ):
        out.append(_Sent(r, f, f.mailbox_id, r.email, f.sent_at))
    return out


def _status(line: dict[str, Any]) -> DeliveryStatus | None:
    kind = str(line.get("type") or "").lower()
    if kind == "success":
        return DeliveryStatus.DELIVERED
    if kind == "defer":
        return DeliveryStatus.DEFERRED
    if kind in ("failure", "rejected", "reject"):
        return delivery.bounce_kind(str(line.get("message") or ""))
    return None


def apply(db: Session, lines: list[dict[str, Any]], at: datetime) -> int:
    """Record the log lines that belong to our campaign emails. Returns how many were new. The caller
    commits."""
    mailboxes = {m.email: m.id for m in db.scalars(select(Mailbox))}
    sent = _sent_emails(db, at - LOOK_BACK - timedelta(days=1))
    by_queue: dict[str, _Sent] = {}
    for s in sent:
        row = s.follow_up or s.recipient
        if qid := delivery.queue_id(row.smtp_response):
            by_queue[qid] = s
    added = 0
    for line in lines:
        sender = str(line.get("email") or line.get("sender") or "").strip().lower()
        to = str(line.get("recipient") or "").strip().lower()
        status = _status(line)
        if sender not in mailboxes or not to or status is None:
            continue
        queue = str(line.get("msgid") or "").strip()
        match = by_queue.get(queue) if queue else None
        sent_unix = _int(line.get("sendunixtime"))
        if match is None and sent_unix is not None:
            sent_time = datetime.fromtimestamp(sent_unix, UTC)
            candidates = [
                s
                for s in sent
                if s.email == to
                and s.mailbox_id == mailboxes[sender]
                and abs(s.sent_at - sent_time) <= MATCH_WINDOW
            ]
            match = min(candidates, key=lambda s: abs(s.sent_at - sent_time), default=None)
        if match is None:
            continue
        action_unix = _int(line.get("actionunixtime")) or sent_unix
        occurred = datetime.fromtimestamp(action_unix, UTC) if action_unix else at
        # A delayed email shows up again on every retry: record the first delay only.
        stamp = "" if status == DeliveryStatus.DEFERRED else f":{action_unix}"
        if delivery.record(
            db,
            source=DeliverySource.CPANEL,
            status=status,
            at=occurred,
            recipient=match.recipient,
            follow_up=match.follow_up,
            mailbox_id=match.mailbox_id,
            response=str(line.get("message") or "") or None,
            remote_host=str(line.get("host") or line.get("deliveredto") or "") or None,
            queue=queue or None,
            external_id=f"cpanel:{queue or sent_unix}:{to}:{status.value}{stamp}",
        ):
            added += 1
            if status in (DeliveryStatus.HARD_BOUNCE, DeliveryStatus.SOFT_BOUNCE):
                delivery.bounced(db, match.recipient, status, "cpanel")
    return added


def sync(db: Session, at: datetime, fetch_lines: Fetch = fetch) -> dict[str, Any]:
    """One sync, when cPanel is set up. Commits; emails a new "can't read cPanel" alert."""
    cfg = config(db)
    if cfg is None:
        return {"configured": False}
    state = get_json(db, STATE_KEY)
    try:
        lines = fetch_lines(cfg, int((at - LOOK_BACK).timestamp()))
    except CpanelFailed as e:
        state.update(error=str(e)[:1000], synced_at=at.isoformat())
        set_json(db, STATE_KEY, state, None)
        new = alerts.sync(
            db,
            "cpanel_failing",
            [alerts.Condition(f"Reading cPanel's delivery log failed: {e}", target="cpanel")],
            at,
        )
        db.commit()
        alerts.email_admins(db, new, at)
        log.warning("cPanel sync failed: %s", e)
        return {"configured": True, "error": str(e)}
    added = apply(db, lines, at)
    state.update(error=None, synced_at=at.isoformat(), lines=len(lines), added=added)
    set_json(db, STATE_KEY, state, None)
    alerts.sync(db, "cpanel_failing", [], at)
    db.commit()
    return {"configured": True, "lines": len(lines), "added": added}
