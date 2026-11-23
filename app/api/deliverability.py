"""The Deliverability page: mailbox cards, alerts, health checks, the mail-server log, placement tests, DMARC,
the provider mix, and their settings (plan section 19)."""

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import DB, AdminUser, CurrentActor, CurrentUser, EditorUser
from app.api.schemas import DeliveryEventOut, Page
from app.core.crypto import encrypt
from app.models import (
    Alert,
    AlertSeverity,
    AlertStatus,
    Campaign,
    DeliveryEvent,
    DeliverySource,
    DeliveryStatus,
    DmarcRecord,
    DmarcReport,
    EmailProvider,
    FollowUpEmail,
    HealthKind,
    HealthStatus,
    HelperProvider,
    Lead,
    Mailbox,
    MailboxStatus,
    PlacementFolder,
    PlacementResult,
    PlacementState,
    PlacementTest,
    PlacementTestStatus,
    User,
)
from app.services import alerts, cpanel, deliverability, dmarc, domain_health, mailer, placement, warmup
from app.services.app_settings import get_json, set_json
from app.services.audit import log_activity
from app.services.auth import normalize_email
from app.services.csv_export import csv_response
from app.services.sending import now

router = APIRouter(prefix="/deliverability", tags=["deliverability"])

PERIOD_DAYS = 30
EXPORT_LIMIT = 50_000

# --- Alerts -----------------------------------------------------------------------------------------------


class PausedMailbox(BaseModel):
    id: int
    email: str
    # Still paused now (someone may have resumed it since).
    paused: bool


class AlertOut(BaseModel):
    id: int
    kind: str
    title: str
    severity: AlertSeverity
    status: AlertStatus
    mailbox_id: int | None
    mailbox_email: str | None
    target: str
    message: str
    paused_mailboxes: list[PausedMailbox]
    opened_at: datetime
    last_seen_at: datetime
    acknowledged_at: datetime | None
    acknowledged_by: str | None
    resolved_at: datetime | None
    emailed_at: datetime | None
    email_error: str | None


def _alerts_out(db: Session, rows: list[Alert]) -> list[AlertOut]:
    mailbox_ids = {a.mailbox_id for a in rows if a.mailbox_id} | {
        m for a in rows for m in a.paused_mailbox_ids or []
    }
    mailboxes = {m.id: m for m in db.scalars(select(Mailbox).where(Mailbox.id.in_(mailbox_ids)))}
    users = dict(
        db.execute(
            select(User.id, User.name).where(
                User.id.in_({a.acknowledged_by_id for a in rows if a.acknowledged_by_id})
            )
        ).all()
    )
    out = []
    for a in rows:
        info = alerts.KINDS.get(a.kind)
        paused = [
            PausedMailbox(
                id=mid, email=mailboxes[mid].email, paused=mailboxes[mid].campaigns_paused_at is not None
            )
            for mid in a.paused_mailbox_ids or []
            if mid in mailboxes
        ]
        out.append(
            AlertOut(
                id=a.id,
                kind=a.kind,
                title=info.title if info else a.kind,
                severity=a.severity,
                status=a.status,
                mailbox_id=a.mailbox_id,
                mailbox_email=mailboxes[a.mailbox_id].email if a.mailbox_id in mailboxes else None,
                target=a.target,
                message=a.message,
                paused_mailboxes=paused,
                opened_at=a.opened_at,
                last_seen_at=a.last_seen_at,
                acknowledged_at=a.acknowledged_at,
                acknowledged_by=users.get(a.acknowledged_by_id),
                resolved_at=a.resolved_at,
                emailed_at=a.emailed_at,
                email_error=a.email_error,
            )
        )
    return out


def _active_alerts(db: Session) -> list[Alert]:
    return list(
        db.scalars(
            select(Alert)
            .where(Alert.status != AlertStatus.RESOLVED)
            .order_by((Alert.severity == AlertSeverity.CRITICAL).desc(), Alert.opened_at.desc())
        )
    )


class AlertCount(BaseModel):
    # Not yet acknowledged.
    open: int
    critical: int
    # Open or acknowledged (the problem is still there).
    active: int
    # Mailboxes whose campaign emails are paused (by an alert), whatever the alert's state.
    paused_mailboxes: int


@router.get("/alerts/count", response_model=AlertCount)
def alert_count(db: DB, _: CurrentUser) -> AlertCount:
    return AlertCount(**alerts.active_count(db))


@router.get("/alerts", response_model=list[AlertOut])
def list_alerts(
    db: DB,
    _: CurrentUser,
    state: Annotated[str, Query(pattern="^(active|resolved|all)$")] = "active",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AlertOut]:
    query = select(Alert)
    if state == "active":
        query = query.where(Alert.status != AlertStatus.RESOLVED)
    elif state == "resolved":
        query = query.where(Alert.status == AlertStatus.RESOLVED)
    rows = db.scalars(query.order_by(Alert.opened_at.desc()).limit(limit)).all()
    return _alerts_out(db, list(rows))


def _alert(db: Session, alert_id: int) -> Alert:
    alert = db.get(Alert, alert_id, with_for_update=True)
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alert_not_found")
    return alert


@router.post("/alerts/{alert_id}/acknowledge", response_model=AlertOut)
def acknowledge_alert(alert_id: int, db: DB, _: EditorUser, actor: CurrentActor) -> AlertOut:
    """Mark as seen. The banner goes away; the alert stays until the problem is fixed."""
    alert = _alert(db, alert_id)
    if alerts.acknowledge(db, alert, actor, now()):
        db.commit()
    return _alerts_out(db, [alert])[0]


@router.post("/alerts/{alert_id}/resume", response_model=AlertOut)
def resume_from_alert(alert_id: int, db: DB, _: EditorUser, actor: CurrentActor) -> AlertOut:
    """Acknowledge the alert and let the mailboxes it paused send campaign emails again."""
    alert = _alert(db, alert_id)
    at = now()
    alerts.acknowledge(db, alert, actor, at)
    for mailbox_id in alert.paused_mailbox_ids or []:
        mailbox = db.get(Mailbox, mailbox_id, with_for_update=True)
        if mailbox is not None:
            alerts.resume_mailbox(db, mailbox, actor, at)
    db.commit()
    return _alerts_out(db, [alert])[0]


# --- Overview (mailbox cards) -----------------------------------------------------------------------------


class BounceRateOut(BaseModel):
    hard: int
    emails: int
    window: int
    pct: float


class PlacementSummary(BaseModel):
    test_id: int
    at: datetime
    running: bool
    inbox: int
    promotions: int
    spam: int
    missing: int
    failed: int


class MailboxCard(BaseModel):
    id: int
    email: str
    display_name: str
    status: MailboxStatus
    last_error: str | None
    campaigns_paused_at: datetime | None
    campaigns_paused_reason: str | None
    # Last PERIOD_DAYS days, opening emails and follow-ups.
    sent: int
    delivered: int
    deferred: int
    soft_bounced: int
    hard_bounced: int
    rejected: int
    bounce_rate: BounceRateOut
    # The domain's DMARC pass rate (last 7 days), None without reports.
    dmarc_passed: int | None
    dmarc_total: int | None
    # Worst blacklist result among its server's addresses and its domain, None before the first check.
    blacklist: HealthStatus | None
    blacklist_detail: str | None
    # The domain's DNS checks (MX/SPF/DKIM/DMARC): the worst one.
    dns: HealthStatus | None
    placement: PlacementSummary | None


class ProviderShare(BaseModel):
    provider: EmailProvider | None
    leads: int


class SetupState(BaseModel):
    cpanel_configured: bool
    cpanel_synced_at: str | None
    cpanel_error: str | None
    dmarc_configured: bool
    dmarc_email: str | None
    dmarc_checked_at: str | None
    dmarc_error: str | None
    spamhaus_configured: bool
    dkim_selector: str
    helpers: int
    # Mailbox used for alert emails (Settings → Invite emails), None = alerts only show in the app.
    alert_email_mailbox: str | None


class DeliverabilityOverview(BaseModel):
    period_days: int
    mailboxes: list[MailboxCard]
    alerts: list[AlertOut]
    health_checked_at: datetime | None
    note: str | None
    setup: SetupState
    provider_mix: list[ProviderShare]
    thresholds: alerts.Thresholds


_WORST = [HealthStatus.FAIL, HealthStatus.WARN, HealthStatus.UNKNOWN, HealthStatus.OK]


def _worst(statuses: list[HealthStatus]) -> HealthStatus | None:
    return next((s for s in _WORST if s in statuses), None)


def _placements(db: Session, mailbox_ids: list[int]) -> dict[int, PlacementSummary]:
    newest = (
        select(func.max(PlacementTest.id))
        .where(PlacementTest.mailbox_id.in_(mailbox_ids))
        .group_by(PlacementTest.mailbox_id)
    )
    latest = db.scalars(select(PlacementTest).where(PlacementTest.id.in_(newest))).all()
    out = {}
    for t in latest:
        results = db.scalars(select(PlacementResult).where(PlacementResult.test_id == t.id)).all()
        folders = [r.folder for r in results]
        out[t.mailbox_id] = PlacementSummary(
            test_id=t.id,
            at=t.created_at,
            running=t.status == PlacementTestStatus.RUNNING,
            inbox=folders.count(PlacementFolder.INBOX),
            promotions=folders.count(PlacementFolder.PROMOTIONS),
            spam=folders.count(PlacementFolder.SPAM),
            missing=folders.count(PlacementFolder.MISSING),
            failed=sum(1 for r in results if r.state == PlacementState.FAILED),
        )
    return out


def _setup(db: Session) -> SetupState:
    c = get_json(db, cpanel.STATE_KEY)
    d = get_json(db, dmarc.STATE_KEY)
    acct = dmarc.account(db)
    selector, key = domain_health.config(db)
    from app.services.invites import invite_mailbox

    mailbox = invite_mailbox(db)
    return SetupState(
        cpanel_configured=cpanel.config(db) is not None,
        cpanel_synced_at=c.get("synced_at"),
        cpanel_error=c.get("error"),
        dmarc_configured=acct is not None,
        dmarc_email=acct.email if acct else None,
        dmarc_checked_at=d.get("checked_at"),
        dmarc_error=d.get("error"),
        spamhaus_configured=bool(key),
        dkim_selector=selector,
        helpers=len(warmup.active_helpers(db)),
        alert_email_mailbox=mailbox.email if mailbox else None,
    )


@router.get("", response_model=DeliverabilityOverview)
def overview(db: DB, _: CurrentUser) -> DeliverabilityOverview:
    at = now()
    limits = alerts.thresholds(db)
    mailboxes = db.scalars(select(Mailbox).where(Mailbox.archived_at.is_(None)).order_by(Mailbox.email)).all()
    counts = deliverability.status_counts(db, at - timedelta(days=PERIOD_DAYS))
    rates = dmarc.pass_rates(db, at - timedelta(days=dmarc.PASS_RATE_DAYS))
    health = domain_health.latest(db)
    placements = _placements(db, [m.id for m in mailboxes])
    cards = []
    for m in mailboxes:
        c = counts.get(m.id, {})
        rate = deliverability.bounce_rate(db, m, limits.bounce_window)
        domain = m.email.rsplit("@", 1)[-1]
        mine = [h for h in health if m.id in (h.data or {}).get("mailbox_ids", [])]
        lists = [h for h in mine if h.kind == HealthKind.BLACKLIST]
        listed = [h for h in lists if h.status == HealthStatus.FAIL]
        passed, total = rates.get(domain, (None, None))
        cards.append(
            MailboxCard(
                id=m.id,
                email=m.email,
                display_name=m.display_name,
                status=m.status,
                last_error=m.last_error,
                campaigns_paused_at=m.campaigns_paused_at,
                campaigns_paused_reason=m.campaigns_paused_reason,
                sent=c.get("sent", 0),
                delivered=c.get(DeliveryStatus.DELIVERED.value, 0),
                deferred=c.get(DeliveryStatus.DEFERRED.value, 0),
                soft_bounced=c.get(DeliveryStatus.SOFT_BOUNCE.value, 0),
                hard_bounced=c.get(DeliveryStatus.HARD_BOUNCE.value, 0),
                rejected=c.get(DeliveryStatus.REJECTED.value, 0),
                bounce_rate=BounceRateOut(
                    hard=rate.hard, emails=rate.emails, window=rate.window, pct=round(rate.rate * 100, 2)
                ),
                dmarc_passed=passed,
                dmarc_total=total,
                blacklist=_worst([h.status for h in lists]),
                blacklist_detail="; ".join(f"{h.target}: {h.detail}" for h in listed) or None,
                dns=_worst(
                    [
                        h.status
                        for h in mine
                        if h.kind in (HealthKind.MX, HealthKind.SPF, HealthKind.DKIM, HealthKind.DMARC)
                    ]
                ),
                placement=placements.get(m.id),
            )
        )
    mix = db.execute(select(Lead.email_provider, func.count()).group_by(Lead.email_provider)).all()
    return DeliverabilityOverview(
        period_days=PERIOD_DAYS,
        mailboxes=cards,
        alerts=_alerts_out(db, _active_alerts(db)),
        health_checked_at=health[0].checked_at if health else None,
        note=domain_health.development_note(),
        setup=_setup(db),
        provider_mix=[ProviderShare(provider=p, leads=n) for p, n in sorted(mix, key=lambda r: -r[1])],
        thresholds=limits,
    )


# --- Health checks ----------------------------------------------------------------------------------------


class HealthCheckOut(BaseModel):
    model_config = {"from_attributes": True}

    kind: HealthKind
    target: str
    status: HealthStatus
    detail: str
    data: dict[str, Any]
    checked_at: datetime


class HealthCheckResults(BaseModel):
    checked_at: datetime | None
    results: list[HealthCheckOut]
    note: str | None


def _health(db: Session) -> HealthCheckResults:
    rows = domain_health.latest(db)
    return HealthCheckResults(
        checked_at=rows[0].checked_at if rows else None,
        results=[HealthCheckOut.model_validate(r) for r in rows],
        note=domain_health.development_note(),
    )


@router.get("/health", response_model=HealthCheckResults)
def health_results(db: DB, _: CurrentUser) -> HealthCheckResults:
    return _health(db)


@router.post("/health/run", response_model=HealthCheckResults)
def run_health_checks(db: DB, _: EditorUser, actor: CurrentActor) -> HealthCheckResults:
    """Run every check now (a few seconds) instead of waiting for the daily run."""
    results = domain_health.run(db, now())
    log_activity(db, actor, "deliverability.checks_run", after={"results": len(results)})
    db.commit()
    return _health(db)


@router.get("/health/history", response_model=list[HealthCheckOut])
def health_history(db: DB, _: CurrentUser, kind: HealthKind, target: str) -> list[HealthCheckOut]:
    return [HealthCheckOut.model_validate(r) for r in domain_health.history(db, kind, target)]


# --- Mail-server log ----------------------------------------------------------------------------------------


class LogLine(DeliveryEventOut):
    mailbox_email: str | None
    campaign_id: int | None
    campaign_name: str | None
    follow_up_step: int | None


def _log_query(
    mailbox_id: int | None,
    status_filter: DeliveryStatus | None,
    source: DeliverySource | None,
    campaign_id: int | None,
    q: str | None,
    since: datetime | None,
) -> Select:
    query = (
        select(DeliveryEvent, Mailbox.email, Campaign.name, FollowUpEmail.step)
        .outerjoin(Mailbox, Mailbox.id == DeliveryEvent.mailbox_id)
        .outerjoin(Campaign, Campaign.id == DeliveryEvent.campaign_id)
        .outerjoin(FollowUpEmail, FollowUpEmail.id == DeliveryEvent.follow_up_id)
    )
    if mailbox_id is not None:
        query = query.where(DeliveryEvent.mailbox_id == mailbox_id)
    if status_filter is not None:
        query = query.where(DeliveryEvent.status == status_filter)
    if source is not None:
        query = query.where(DeliveryEvent.source == source)
    if campaign_id is not None:
        query = query.where(DeliveryEvent.campaign_id == campaign_id)
    if since is not None:
        query = query.where(DeliveryEvent.occurred_at >= since)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.where(
            or_(
                DeliveryEvent.email.ilike(like),
                DeliveryEvent.response.ilike(like),
                DeliveryEvent.queue_id == q.strip(),
            )
        )
    return query


def _line(
    e: DeliveryEvent, mailbox_email: str | None, campaign_name: str | None, step: int | None
) -> LogLine:
    return LogLine(
        **DeliveryEventOut.model_validate(e).model_dump(),
        mailbox_email=mailbox_email,
        campaign_id=e.campaign_id,
        campaign_name=campaign_name,
        follow_up_step=step,
    )


class LogFilters(BaseModel):
    mailbox_id: int | None = None
    status: DeliveryStatus | None = None
    source: DeliverySource | None = None
    campaign_id: int | None = None
    q: str | None = Field(default=None, max_length=320)
    days: int | None = Field(default=None, ge=1, le=365)


Filters = Annotated[LogFilters, Query()]


@router.get("/log", response_model=Page[LogLine])
def server_log(
    db: DB,
    _: CurrentUser,
    filters: Filters,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[LogLine]:
    since = now() - timedelta(days=filters.days) if filters.days else None
    query = _log_query(
        filters.mailbox_id, filters.status, filters.source, filters.campaign_id, filters.q, since
    )
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.execute(
        query.order_by(DeliveryEvent.occurred_at.desc(), DeliveryEvent.id.desc()).limit(limit).offset(offset)
    ).all()
    return Page(items=[_line(*row) for row in rows], total=total)


@router.get("/log/export")
def export_server_log(db: DB, _: CurrentUser, actor: CurrentActor, filters: Filters) -> StreamingResponse:
    since = now() - timedelta(days=filters.days) if filters.days else None
    query = _log_query(
        filters.mailbox_id, filters.status, filters.source, filters.campaign_id, filters.q, since
    )
    rows = [
        _line(*row)
        for row in db.execute(query.order_by(DeliveryEvent.occurred_at.desc()).limit(EXPORT_LIMIT))
    ]
    log_activity(
        db,
        actor,
        "deliverability.log_exported",
        after={"filters": filters.model_dump(mode="json", exclude_none=True), "rows": len(rows)},
    )
    db.commit()
    return csv_response(
        f"mail-server-log-{datetime.now():%Y%m%d-%H%M}.csv",
        [
            "time",
            "mailbox",
            "to",
            "campaign",
            "follow_up",
            "source",
            "status",
            "server",
            "queue_id",
            "server_reply",
        ],
        (
            [
                r.occurred_at.isoformat(),
                r.mailbox_email,
                r.email,
                r.campaign_name,
                r.follow_up_step,
                r.source.value,
                r.status.value,
                r.remote_host,
                r.queue_id,
                r.response,
            ]
            for r in rows
        ),
    )


@router.post("/log/sync")
def sync_cpanel_now(db: DB, _: EditorUser) -> dict[str, Any]:
    """Read cPanel's delivery log now instead of waiting up to 10 minutes."""
    return cpanel.sync(db, now())


# --- Placement tests --------------------------------------------------------------------------------------


class PlacementResultOut(BaseModel):
    helper_email: str
    provider: HelperProvider
    state: PlacementState
    folder: PlacementFolder | None
    error: str | None
    sent_at: datetime | None
    checked_at: datetime | None


class PlacementTestOut(BaseModel):
    id: int
    mailbox_id: int
    mailbox_email: str
    campaign_id: int | None
    campaign_name: str | None
    subject: str
    status: PlacementTestStatus
    created_at: datetime
    finished_at: datetime | None
    requested_by: str | None
    results: list[PlacementResultOut]


def _tests_out(db: Session, tests: list[PlacementTest]) -> list[PlacementTestOut]:
    ids = [t.id for t in tests]
    results: dict[int, list[PlacementResultOut]] = {i: [] for i in ids}
    for r in db.scalars(
        select(PlacementResult).where(PlacementResult.test_id.in_(ids)).order_by(PlacementResult.id)
    ):
        results[r.test_id].append(
            PlacementResultOut(
                helper_email=r.helper_email,
                provider=r.provider,
                state=r.state,
                folder=r.folder,
                error=r.error,
                sent_at=r.sent_at,
                checked_at=r.checked_at,
            )
        )
    mailboxes = dict(
        db.execute(
            select(Mailbox.id, Mailbox.email).where(Mailbox.id.in_({t.mailbox_id for t in tests}))
        ).all()
    )
    campaigns = dict(
        db.execute(
            select(Campaign.id, Campaign.name).where(
                Campaign.id.in_({t.campaign_id for t in tests if t.campaign_id})
            )
        ).all()
    )
    users = dict(
        db.execute(
            select(User.id, User.name).where(
                User.id.in_({t.requested_by_id for t in tests if t.requested_by_id})
            )
        ).all()
    )
    return [
        PlacementTestOut(
            id=t.id,
            mailbox_id=t.mailbox_id,
            mailbox_email=mailboxes.get(t.mailbox_id, ""),
            campaign_id=t.campaign_id,
            campaign_name=campaigns.get(t.campaign_id) if t.campaign_id else None,
            subject=t.subject,
            status=t.status,
            created_at=t.created_at,
            finished_at=t.finished_at,
            requested_by=users.get(t.requested_by_id) if t.requested_by_id else None,
            results=results[t.id],
        )
        for t in tests
    ]


@router.get("/placement", response_model=list[PlacementTestOut])
def list_placement_tests(
    db: DB, _: CurrentUser, mailbox_id: int | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[PlacementTestOut]:
    query = select(PlacementTest)
    if mailbox_id is not None:
        query = query.where(PlacementTest.mailbox_id == mailbox_id)
    return _tests_out(db, list(db.scalars(query.order_by(PlacementTest.created_at.desc()).limit(limit))))


class PlacementIn(BaseModel):
    mailbox_id: int
    # None: the mailbox's running (or latest) campaign, unless use_sample.
    campaign_id: int | None = None
    use_sample: bool = False


@router.post("/placement", response_model=PlacementTestOut, status_code=status.HTTP_201_CREATED)
def start_placement_test(body: PlacementIn, db: DB, _: EditorUser, actor: CurrentActor) -> PlacementTestOut:
    """Send the message to every helper account; results come in over the next few minutes."""
    mailbox = db.get(Mailbox, body.mailbox_id)
    if mailbox is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "mailbox_not_found")
    campaign = None
    if not body.use_sample:
        campaign = (
            db.get(Campaign, body.campaign_id)
            if body.campaign_id
            else placement.default_campaign(db, mailbox.id)
        )
        if body.campaign_id and campaign is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign_not_found")
        if campaign is not None and not campaign.subject.strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "campaign_empty")
    try:
        test = placement.start(db, mailbox, campaign, actor, now())
    except placement.PlacementError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    db.commit()
    return _tests_out(db, [test])[0]


# --- DMARC ------------------------------------------------------------------------------------------------


class DmarcRate(BaseModel):
    domain: str
    passed: int
    total: int


class DmarcSource(BaseModel):
    domain: str
    source_ip: str
    emails: int
    passed: int
    dkim_pass: int
    spf_pass: int
    reporters: list[str]
    last_report_at: datetime


class DmarcReportOut(BaseModel):
    id: int
    reporter: str
    domain: str
    policy: str | None
    begin_at: datetime
    end_at: datetime
    emails: int
    passed: int


class DmarcOut(BaseModel):
    days: int
    rates: list[DmarcRate]
    sources: list[DmarcSource]
    reports: list[DmarcReportOut]


@router.get("/dmarc", response_model=DmarcOut)
def dmarc_overview(db: DB, _: CurrentUser, days: Annotated[int, Query(ge=1, le=365)] = 30) -> DmarcOut:
    since = now() - timedelta(days=days)
    passed = func.sum(DmarcRecord.count).filter((DmarcRecord.dkim == "pass") | (DmarcRecord.spf == "pass"))
    reports = db.execute(
        select(DmarcReport, func.coalesce(func.sum(DmarcRecord.count), 0), func.coalesce(passed, 0))
        .outerjoin(DmarcRecord, DmarcRecord.report_id == DmarcReport.id)
        .where(DmarcReport.end_at >= since)
        .group_by(DmarcReport.id)
        .order_by(DmarcReport.end_at.desc())
        .limit(50)
    ).all()
    return DmarcOut(
        days=days,
        rates=[
            DmarcRate(domain=d, passed=p, total=t)
            for d, (p, t) in sorted(dmarc.pass_rates(db, since).items())
        ],
        sources=[DmarcSource(**s) for s in dmarc.sources(db, since)],
        reports=[
            DmarcReportOut(
                id=r.id,
                reporter=r.reporter,
                domain=r.domain,
                policy=r.policy,
                begin_at=r.begin_at,
                end_at=r.end_at,
                emails=int(total),
                passed=int(ok),
            )
            for r, total, ok in reports
        ],
    )


@router.post("/dmarc/check")
def check_dmarc_now(db: DB, _: EditorUser) -> dict[str, Any]:
    """Read new DMARC reports now instead of waiting up to 6 hours."""
    return dmarc.check(db, now())


# --- Settings ---------------------------------------------------------------------------------------------


class CpanelSettingsOut(BaseModel):
    host: str
    port: int
    username: str
    token_set: bool


class DmarcMailboxOut(BaseModel):
    email: str
    imap_host: str
    imap_port: int
    password_set: bool


class DeliverabilitySettings(BaseModel):
    cpanel: CpanelSettingsOut
    dmarc: DmarcMailboxOut
    dkim_selector: str
    spamhaus_key_set: bool
    thresholds: alerts.Thresholds


def _settings(db: Session) -> DeliverabilitySettings:
    c = get_json(db, cpanel.SETTINGS_KEY)
    d = get_json(db, dmarc.SETTINGS_KEY)
    checks = get_json(db, domain_health.SETTINGS_KEY)
    return DeliverabilitySettings(
        cpanel=CpanelSettingsOut(
            host=c.get("host", ""),
            port=int(c.get("port") or 2083),
            username=c.get("username", ""),
            token_set=bool(c.get("token_encrypted")),
        ),
        dmarc=DmarcMailboxOut(
            email=d.get("email", ""),
            imap_host=d.get("imap_host", ""),
            imap_port=int(d.get("imap_port") or 993),
            password_set=bool(d.get("password_encrypted")),
        ),
        dkim_selector=checks.get("dkim_selector") or domain_health.DEFAULT_DKIM_SELECTOR,
        spamhaus_key_set=bool(checks.get("spamhaus_key_encrypted")),
        thresholds=alerts.thresholds(db),
    )


@router.get("/settings", response_model=DeliverabilitySettings)
def read_settings(db: DB, _: CurrentUser) -> DeliverabilitySettings:
    """No secret is ever returned, only whether it is set."""
    return _settings(db)


class CpanelIn(BaseModel):
    # The cPanel server name (e.g. fastnexa.com or the server's hostname). Blank clears the setup.
    host: str = Field(default="", max_length=255)
    port: int = Field(default=2083, ge=1, le=65535)
    username: str = Field(default="", max_length=100)
    # None keeps the saved token.
    token: str | None = Field(default=None, max_length=500)


class SettingTestOut(BaseModel):
    ok: bool
    detail: str


def _clean_host(host: str) -> str:
    return host.strip().removeprefix("https://").removeprefix("http://").split("/")[0].split(":")[0].lower()


@router.put("/settings/cpanel", response_model=DeliverabilitySettings)
def update_cpanel(body: CpanelIn, db: DB, admin: AdminUser, actor: CurrentActor) -> DeliverabilitySettings:
    before = get_json(db, cpanel.SETTINGS_KEY)
    host = _clean_host(body.host)
    if not host:
        value: dict[str, Any] = {}
    else:
        token = body.token.strip() if body.token is not None else None
        if not token and not before.get("token_encrypted"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "token_required")
        value = {
            "host": host,
            "port": body.port,
            "username": body.username.strip(),
            "token_encrypted": encrypt(token) if token else before["token_encrypted"],
        }
        if not value["username"]:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "username_required")
    set_json(db, cpanel.SETTINGS_KEY, value, admin.id, secret=True)
    log_activity(
        db,
        actor,
        "settings.cpanel_updated",
        entity_type="settings",
        before={k: before.get(k) for k in ("host", "port", "username")},
        after={
            "host": value.get("host"),
            "port": value.get("port"),
            "username": value.get("username"),
            "token_changed": bool(body.token),
        },
    )
    db.commit()
    return _settings(db)


@router.post("/settings/cpanel/test", response_model=SettingTestOut)
def test_cpanel(db: DB, _: AdminUser) -> SettingTestOut:
    cfg = cpanel.config(db)
    if cfg is None:
        return SettingTestOut(ok=False, detail="cPanel isn't set up yet.")
    try:
        lines = cpanel.fetch(cfg, int((now() - timedelta(days=1)).timestamp()))
    except cpanel.CpanelFailed as e:
        return SettingTestOut(ok=False, detail=str(e))
    return SettingTestOut(ok=True, detail=f"Connected. {len(lines)} outgoing emails in the last 24 hours.")


class DmarcMailboxIn(BaseModel):
    # Blank clears the setup.
    email: EmailStr | None = None
    imap_host: str = Field(default="", max_length=255)
    imap_port: int = Field(default=993, ge=1, le=65535)
    # None keeps the saved password.
    password: str | None = Field(default=None, max_length=500)


@router.put("/settings/dmarc", response_model=DeliverabilitySettings)
def update_dmarc_mailbox(
    body: DmarcMailboxIn, db: DB, admin: AdminUser, actor: CurrentActor
) -> DeliverabilitySettings:
    before = get_json(db, dmarc.SETTINGS_KEY)
    if not body.email:
        value: dict[str, Any] = {}
    else:
        password = body.password if body.password else None
        if not password and not before.get("password_encrypted"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "password_required")
        host = _clean_host(body.imap_host)
        if not host:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "imap_host_required")
        email = normalize_email(str(body.email))
        value = {
            "email": email,
            "imap_host": host,
            "imap_port": body.imap_port,
            "password_encrypted": encrypt(password) if password else before["password_encrypted"],
        }
    set_json(db, dmarc.SETTINGS_KEY, value, admin.id, secret=True)
    if value.get("email") != before.get("email") or value.get("imap_host") != before.get("imap_host"):
        # Another mailbox: start reading it from the beginning.
        set_json(db, dmarc.STATE_KEY, {}, admin.id)
    log_activity(
        db,
        actor,
        "settings.dmarc_updated",
        entity_type="settings",
        before={k: before.get(k) for k in ("email", "imap_host", "imap_port")},
        after={
            "email": value.get("email"),
            "imap_host": value.get("imap_host"),
            "imap_port": value.get("imap_port"),
            "password_changed": bool(body.password),
        },
    )
    db.commit()
    return _settings(db)


@router.post("/settings/dmarc/test", response_model=SettingTestOut)
def test_dmarc_mailbox(db: DB, _: AdminUser) -> SettingTestOut:
    acct = dmarc.account(db)
    if acct is None:
        return SettingTestOut(ok=False, detail="The DMARC reports mailbox isn't set up yet.")
    if blocked := dmarc.development_block(acct):
        return SettingTestOut(ok=False, detail=blocked)
    try:
        batch = mailer.read_inbox(acct, None, None, (now() - timedelta(days=dmarc.FIRST_READ_DAYS)).date())  # type: ignore[arg-type]
    except mailer.InboxFailed as e:
        return SettingTestOut(ok=False, detail=str(e))
    return SettingTestOut(
        ok=True, detail=f"Signed in. {len(batch.messages)} emails in the last {dmarc.FIRST_READ_DAYS} days."
    )


class ChecksIn(BaseModel):
    dkim_selector: str = Field(
        default=domain_health.DEFAULT_DKIM_SELECTOR, min_length=1, max_length=63, pattern=r"^[A-Za-z0-9._-]+$"
    )
    # None keeps the saved key, "" removes it.
    spamhaus_key: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9]*$")


@router.put("/settings/checks", response_model=DeliverabilitySettings)
def update_checks(body: ChecksIn, db: DB, admin: AdminUser, actor: CurrentActor) -> DeliverabilitySettings:
    before = get_json(db, domain_health.SETTINGS_KEY)
    value = {"dkim_selector": body.dkim_selector.strip()}
    if body.spamhaus_key is None:
        if before.get("spamhaus_key_encrypted"):
            value["spamhaus_key_encrypted"] = before["spamhaus_key_encrypted"]
    elif body.spamhaus_key:
        value["spamhaus_key_encrypted"] = encrypt(body.spamhaus_key.strip())
    set_json(db, domain_health.SETTINGS_KEY, value, admin.id, secret=True)
    log_activity(
        db,
        actor,
        "settings.checks_updated",
        entity_type="settings",
        before={
            "dkim_selector": before.get("dkim_selector"),
            "spamhaus": bool(before.get("spamhaus_key_encrypted")),
        },
        after={"dkim_selector": value["dkim_selector"], "spamhaus": "spamhaus_key_encrypted" in value},
    )
    db.commit()
    return _settings(db)


@router.put("/settings/thresholds", response_model=DeliverabilitySettings)
def update_thresholds(
    body: alerts.Thresholds, db: DB, admin: AdminUser, actor: CurrentActor
) -> DeliverabilitySettings:
    before = alerts.thresholds(db).model_dump()
    after = body.model_dump()
    if before != after:
        set_json(db, alerts.THRESHOLDS_KEY, after, admin.id)
        changed = [k for k in after if after[k] != before[k]]
        log_activity(
            db,
            actor,
            "settings.thresholds_updated",
            entity_type="settings",
            before={k: before[k] for k in changed},
            after={k: after[k] for k in changed},
        )
        db.commit()
    return _settings(db)
