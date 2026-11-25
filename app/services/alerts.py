"""Deliverability alerts (plan section 19): a banner in the app, an email to the admins, and for the serious
ones (only when `Thresholds.auto_pause` is switched on) a pause of the mailbox's campaign emails.

Each check reports what is wrong right now with sync(kind, conditions): a new problem opens an alert (and may
pause mailboxes); a problem that is still there only refreshes its alert; an alert whose problem went away is
resolved. Resolving never resumes sending: a person does that (Mailboxes, Dashboard or Deliverability page),
which acknowledges the alerts that paused the mailbox. An acknowledged alert doesn't pause again while its
problem lasts; once the problem goes away and comes back, a new alert does.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Alert, AlertSeverity, AlertStatus, Mailbox, MailboxStatus, Role, User, UserStatus
from app.services import mailer
from app.services.app_settings import get_json
from app.services.audit import SYSTEM, Actor, log_activity
from app.services.invites import invite_mailbox

log = logging.getLogger(__name__)

THRESHOLDS_KEY = "deliverability.thresholds"


class Thresholds(BaseModel):
    """Editable in Settings (admins). Defaults from the plan."""

    # Off (the default): serious problems only warn (banner and email). On: they also pause the mailbox's
    # campaign emails until a person resumes them.
    auto_pause: bool = False

    # More than this share of hard bounces in a mailbox's last `bounce_window` emails pauses it.
    hard_bounce_pct: float = Field(default=3.0, ge=0.5, le=50)
    bounce_window: int = Field(default=100, ge=20, le=1000)
    # More than this share of the last 24 hours' emails still not delivered.
    deferred_pct: float = Field(default=10.0, ge=1, le=100)
    # DMARC pass rate (last 7 days) below this.
    dmarc_pass_pct: float = Field(default=95.0, ge=50, le=100)
    # The mail server's certificate expires within this many days.
    cert_days: int = Field(default=14, ge=1, le=90)
    # More than this share of a placement test's emails in Spam (or missing).
    placement_spam_pct: float = Field(default=50.0, ge=10, le=100)


def thresholds(db: Session) -> Thresholds:
    try:
        return Thresholds(**get_json(db, THRESHOLDS_KEY))
    except ValueError:
        return Thresholds()


@dataclass(frozen=True)
class KindInfo:
    title: str
    severity: AlertSeverity
    pauses: bool


KINDS: dict[str, KindInfo] = {
    "hard_bounces": KindInfo("Too many hard bounces", AlertSeverity.CRITICAL, True),
    "blacklisted": KindInfo("On a blacklist", AlertSeverity.CRITICAL, True),
    "dns_broken": KindInfo("Email DNS records broken", AlertSeverity.CRITICAL, True),
    "warmup_low": KindInfo("Low warm-up inbox rate", AlertSeverity.CRITICAL, True),
    # The mailbox already sends nothing while its login fails (status ERROR).
    "login_failing": KindInfo("Mailbox login failing", AlertSeverity.CRITICAL, False),
    "deferred": KindInfo("Many emails delayed", AlertSeverity.WARNING, False),
    "dmarc_low": KindInfo("Low DMARC pass rate", AlertSeverity.WARNING, False),
    "placement_spam": KindInfo("Placement test: mostly spam", AlertSeverity.WARNING, False),
    "cert_expiring": KindInfo("Certificate expiring", AlertSeverity.WARNING, False),
    "helper_broken": KindInfo("Warm-up helper not working", AlertSeverity.WARNING, False),
    "cpanel_failing": KindInfo("cPanel delivery log not readable", AlertSeverity.WARNING, False),
    "dmarc_mailbox_failing": KindInfo("DMARC reports mailbox not readable", AlertSeverity.WARNING, False),
    # Deals' do-not-email list (app.services.deals). A stale copy stops all campaign emails by itself (not
    # per mailbox), and they start again by themselves after the next good sync.
    "deals_stale": KindInfo("Deals do-not-email list out of date", AlertSeverity.CRITICAL, False),
    "deals_signature": KindInfo("Deals rejected our signature", AlertSeverity.CRITICAL, False),
    "deals_error": KindInfo("Deals sync problem", AlertSeverity.WARNING, False),
}


@dataclass
class Condition:
    """Something wrong right now. `pause`: mailboxes whose campaign emails it should pause."""

    message: str
    mailbox_id: int | None = None
    target: str = ""
    pause: Iterable[int] = field(default_factory=tuple)


def _active(db: Session, kind: str) -> list[Alert]:
    return list(
        db.scalars(
            select(Alert)
            .where(Alert.kind == kind, Alert.status.in_([AlertStatus.OPEN, AlertStatus.ACKNOWLEDGED]))
            .with_for_update()
        )
    )


def _pause(db: Session, mailbox_id: int, kind: str, at: datetime) -> bool:
    mailbox = db.get(Mailbox, mailbox_id)
    if mailbox is None or mailbox.archived_at is not None or mailbox.campaigns_paused_at is not None:
        return False
    mailbox.campaigns_paused_at = at
    mailbox.campaigns_paused_reason = kind
    log_activity(
        db,
        SYSTEM,
        "mailbox.campaigns_paused",
        entity_type="mailbox",
        entity_id=mailbox.id,
        after={"reason": kind},
    )
    return True


def sync(
    db: Session, kind: str, conditions: list[Condition], at: datetime, *, scope: set[str] | None = None
) -> list[Alert]:
    """Make the active alerts of this kind match `conditions`. Returns the new ones (to email).

    `scope`: only resolve alerts for these targets (a check that looked at some targets only). The caller
    commits.
    """
    info = KINDS[kind]
    pausing = info.pauses and thresholds(db).auto_pause
    active = {(a.mailbox_id, a.target): a for a in _active(db, kind)}
    new: list[Alert] = []
    seen: set[tuple[int | None, str]] = set()
    for c in conditions:
        key = (c.mailbox_id, c.target)
        seen.add(key)
        alert = active.get(key)
        if alert is not None:
            alert.message = c.message[:1000]
            alert.last_seen_at = at
            # Pausing was switched on while this problem was open: pause now (not if a person resumed).
            if pausing and alert.status == AlertStatus.OPEN and not alert.paused_mailbox_ids:
                paused = [mid for mid in dict.fromkeys(c.pause) if _pause(db, mid, kind, at)]
                if paused:
                    alert.paused_mailbox_ids = paused
                    log_activity(
                        db,
                        SYSTEM,
                        "alert.paused",
                        entity_type="alert",
                        entity_id=alert.id,
                        after={"kind": kind, "paused_mailbox_ids": paused},
                    )
            continue
        alert = Alert(
            kind=kind,
            severity=info.severity,
            status=AlertStatus.OPEN,
            mailbox_id=c.mailbox_id,
            target=c.target[:255],
            message=c.message[:1000],
            opened_at=at,
            last_seen_at=at,
            paused_mailbox_ids=[],
        )
        db.add(alert)
        db.flush()
        if pausing:
            alert.paused_mailbox_ids = [mid for mid in dict.fromkeys(c.pause) if _pause(db, mid, kind, at)]
        log_activity(
            db,
            SYSTEM,
            "alert.opened",
            entity_type="alert",
            entity_id=alert.id,
            after={
                "kind": kind,
                "message": alert.message,
                "mailbox_id": c.mailbox_id,
                "target": c.target or None,
                "paused_mailbox_ids": alert.paused_mailbox_ids,
            },
        )
        active[key] = alert
        new.append(alert)
    for key, alert in active.items():
        if key in seen or (scope is not None and alert.target not in scope):
            continue
        alert.status = AlertStatus.RESOLVED
        alert.resolved_at = at
        log_activity(
            db, SYSTEM, "alert.resolved", entity_type="alert", entity_id=alert.id, after={"kind": kind}
        )
    return new


def acknowledge(db: Session, alert: Alert, actor: Actor, at: datetime) -> bool:
    """The caller commits. Returns whether anything changed."""
    if alert.status != AlertStatus.OPEN:
        return False
    alert.status = AlertStatus.ACKNOWLEDGED
    alert.acknowledged_at = at
    alert.acknowledged_by_id = actor.user_id
    log_activity(
        db, actor, "alert.acknowledged", entity_type="alert", entity_id=alert.id, after={"kind": alert.kind}
    )
    return True


def acknowledge_for_mailbox(db: Session, mailbox_id: int, actor: Actor, at: datetime) -> int:
    """Resuming a mailbox acknowledges the open alerts about it or that paused it. The caller commits."""
    alerts = db.scalars(
        select(Alert).where(
            Alert.status == AlertStatus.OPEN,
            or_(Alert.mailbox_id == mailbox_id, Alert.paused_mailbox_ids.contains([mailbox_id])),
        )
    ).all()
    return sum(acknowledge(db, a, actor, at) for a in alerts)


def resume_mailbox(db: Session, mailbox: Mailbox, actor: Actor, at: datetime) -> bool:
    """Let campaign emails go out again from a paused mailbox; acknowledges the alerts that paused it.
    The caller commits. Returns whether it was paused."""
    if mailbox.campaigns_paused_at is None:
        return False
    acknowledge_for_mailbox(db, mailbox.id, actor, at)
    log_activity(
        db,
        actor,
        "mailbox.campaigns_resumed",
        entity_type="mailbox",
        entity_id=mailbox.id,
        before={"reason": mailbox.campaigns_paused_reason},
    )
    mailbox.campaigns_paused_at = None
    mailbox.campaigns_paused_reason = None
    # Bounce and warm-up checks only count what happens after this, so they don't pause again at once.
    mailbox.campaigns_resumed_at = at
    return True


def active_count(db: Session) -> dict[str, int]:
    rows = db.execute(select(Alert.severity, Alert.status).where(Alert.status != AlertStatus.RESOLVED)).all()
    paused = db.scalar(
        select(func.count())
        .select_from(Mailbox)
        .where(Mailbox.archived_at.is_(None), Mailbox.campaigns_paused_at.is_not(None))
    )
    return {
        "open": sum(1 for _, s in rows if s == AlertStatus.OPEN),
        "critical": sum(1 for sev, s in rows if s == AlertStatus.OPEN and sev == AlertSeverity.CRITICAL),
        "active": len(rows),
        "paused_mailboxes": paused or 0,
    }


# --- Emails to the admins ----------------------------------------------------------------------------------


def _email_text(db: Session, alert: Alert) -> tuple[str, str]:
    info = KINDS.get(alert.kind)
    title = info.title if info else alert.kind
    paused = [
        m.email for m in db.scalars(select(Mailbox).where(Mailbox.id.in_(alert.paused_mailbox_ids or [])))
    ]
    lines = [f"{title}.", "", alert.message, ""]
    if paused:
        lines += [
            "Campaign emails are paused from: " + ", ".join(paused) + ".",
            "Fix the problem, then press Resume on the Deliverability page.",
            "",
        ]
    page = "settings/deals" if alert.kind.startswith("deals_") else "deliverability"
    lines += [f"Details: {get_settings().app_base_url.rstrip('/')}/{page}", ""]
    return f"NexaReach alert: {title}", "\n".join(lines)


def email_admins(db: Session, alerts: list[Alert], at: datetime, deliver: Any = None) -> None:
    """Email each new alert to every active admin, from the invite-emails mailbox. Records the outcome on the
    alert, never raises. Commits."""
    if not alerts:
        return
    deliver = deliver or mailer.send
    mailbox = invite_mailbox(db)
    admins = db.scalars(
        select(User.email).where(User.role == Role.ADMIN, User.status == UserStatus.ACTIVE)
    ).all()
    for alert in alerts:
        if mailbox is None:
            alert.email_error = "No mailbox is chosen for app emails (Settings → Invite emails)."
            continue
        if mailbox.status == MailboxStatus.ERROR:
            alert.email_error = f"{mailbox.email} can't send (its login failed)."
            continue
        subject, body = _email_text(db, alert)
        errors = []
        for to in admins:
            try:
                deliver(mailbox, mailer.build_message(mailbox, to, subject, body))
            except (mailer.AuthFailed, mailer.SendFailed) as e:
                errors.append(f"{to}: {e}")
        if errors:
            alert.email_error = "; ".join(errors)[:1000]
        if len(errors) < len(admins):
            alert.emailed_at = at
    db.commit()
