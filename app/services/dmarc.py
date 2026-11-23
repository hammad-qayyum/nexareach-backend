"""DMARC aggregate reports (plan section 19).

The domain's DMARC record asks receivers (Google, Microsoft, Yahoo...) to send a daily report to an address
(rua=mailto:dmarc@fastnexa.com). The worker signs in to that mailbox (set up in Settings) every 6 hours,
reads new messages read-only, unpacks the attached report (.zip, .gz or .xml) and stores how many emails each
server sent as our domain and whether they passed SPF / DKIM. The Deliverability page shows the pass rate
and anyone sending as us who fails (a spoofer, or a service we forgot to authorise).
"""

import email
import gzip
import io
import logging
import xml.etree.ElementTree as ET  # noqa: S405 - documents with a DOCTYPE/ENTITY are refused before parsing
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import Message
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import DmarcRecord, DmarcReport
from app.services import alerts, mailer
from app.services.app_settings import get_json, set_json

log = logging.getLogger(__name__)

SETTINGS_KEY = "deliverability.dmarc"  # {"email", "imap_host", "imap_port", "password_encrypted"}
STATE_KEY = "deliverability.dmarc_state"  # {"uidvalidity", "last_uid", "checked_at", "error", "reports"}
MAX_XML_BYTES = 20_000_000
FIRST_READ_DAYS = 30
PASS_RATE_DAYS = 7
# Below this many emails in a week the pass rate says too little to alert on.
MIN_EMAILS_FOR_ALERT = 20


@dataclass
class Account:
    """Duck-types the Mailbox fields mailer's IMAP code uses."""

    email: str
    imap_host: str
    imap_port: int
    smtp_username: str
    smtp_password_encrypted: str


def account(db: Session) -> Account | None:
    cfg = get_json(db, SETTINGS_KEY)
    if not (cfg.get("email") and cfg.get("imap_host") and cfg.get("password_encrypted")):
        return None
    return Account(
        email=cfg["email"],
        imap_host=cfg["imap_host"],
        imap_port=int(cfg.get("imap_port") or 993),
        smtp_username=cfg.get("username") or cfg["email"],
        smtp_password_encrypted=cfg["password_encrypted"],
    )


def development_block(acct: Account) -> str | None:
    if get_settings().is_development and acct.imap_host.strip().lower() not in mailer.LOCAL_SMTP_HOSTS:
        return "This development copy only reads the local test mail server."
    return None


# --- Parsing ---------------------------------------------------------------------------------------------


def report_files(msg: Message) -> Iterator[bytes]:
    """The XML documents attached to a report email."""
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = (part.get_filename() or "").lower()
        ctype = part.get_content_type()
        try:
            data = part.get_payload(decode=True)
            if not isinstance(data, bytes) or not data:
                continue
            if name.endswith(".zip") or ctype in ("application/zip", "application/x-zip-compressed"):
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for info in z.infolist():
                        if info.filename.lower().endswith(".xml") and info.file_size <= MAX_XML_BYTES:
                            yield z.read(info)
            elif name.endswith((".gz", ".gzip")) or ctype in ("application/gzip", "application/x-gzip"):
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as g:
                    xml = g.read(MAX_XML_BYTES + 1)
                if len(xml) <= MAX_XML_BYTES:
                    yield xml
            elif name.endswith(".xml") or ctype in ("text/xml", "application/xml"):
                yield data
        except (zipfile.BadZipFile, OSError, EOFError, ValueError) as e:
            log.warning("unreadable DMARC attachment %s: %s", name or ctype, e)


def _text(node: ET.Element | None, path: str) -> str:
    found = node.find(path) if node is not None else None
    return (found.text or "").strip() if found is not None and found.text else ""


def parse(xml: bytes) -> dict[str, Any] | None:
    """One aggregate report, or None if it isn't one."""
    head = xml[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in xml.lower():
        return None  # never expand entities from a stranger's file
    try:
        root = ET.fromstring(xml)  # noqa: S314
    except ET.ParseError:
        return None
    meta = root.find("report_metadata")
    policy = root.find("policy_published")
    begin, end = _text(meta, "date_range/begin"), _text(meta, "date_range/end")
    if meta is None or policy is None or not begin.isdigit() or not end.isdigit():
        return None
    records = []
    for rec in root.findall("record"):
        count = _text(rec, "row/count")
        records.append(
            {
                "source_ip": _text(rec, "row/source_ip")[:64],
                "count": int(count) if count.isdigit() else 0,
                "disposition": _text(rec, "row/policy_evaluated/disposition")[:20] or None,
                "dkim": (_text(rec, "row/policy_evaluated/dkim").lower() or "fail")[:10],
                "spf": (_text(rec, "row/policy_evaluated/spf").lower() or "fail")[:10],
                "header_from": _text(rec, "identifiers/header_from").lower()[:255] or None,
            }
        )
    return {
        "reporter": (_text(meta, "org_name") or _text(meta, "email") or "unknown")[:200],
        "report_id": (_text(meta, "report_id") or f"{begin}-{end}")[:200],
        "domain": _text(policy, "domain").lower()[:255],
        "policy": _text(policy, "p").lower()[:20] or None,
        "begin_at": datetime.fromtimestamp(int(begin), UTC),
        "end_at": datetime.fromtimestamp(int(end), UTC),
        "records": records,
    }


def store(db: Session, report: dict[str, Any]) -> bool:
    """Save a report unless it's already saved. The caller commits."""
    report_id = db.scalar(
        insert(DmarcReport)
        .values({k: v for k, v in report.items() if k != "records"})
        .on_conflict_do_nothing(constraint="uq_dmarc_reports_reporter_report_id")
        .returning(DmarcReport.id)
    )
    if report_id is None:
        return False
    for rec in report["records"]:
        db.add(DmarcRecord(report_id=report_id, **rec))
    return True


# --- Reading the mailbox --------------------------------------------------------------------------------


def check(db: Session, at: datetime, read: Any = None) -> dict[str, Any]:
    """Read new report emails. Commits; emails new alerts."""
    read = read or mailer.read_inbox
    acct = account(db)
    if acct is None:
        return {"configured": False}
    state = get_json(db, STATE_KEY)
    new_alerts: list = []
    blocked = development_block(acct)
    try:
        if blocked:
            raise mailer.InboxFailed(blocked)
        since = (at - timedelta(days=FIRST_READ_DAYS)).date()
        batch = read(acct, state.get("last_uid"), state.get("uidvalidity"), since)
    except mailer.InboxFailed as e:
        state.update(error=str(e)[:1000], checked_at=at.isoformat())
        set_json(db, STATE_KEY, state, None)
        if not blocked:
            new_alerts = alerts.sync(
                db,
                "dmarc_mailbox_failing",
                [alerts.Condition(f"Reading {acct.email} failed: {e}", target=acct.email)],
                at,
            )
        db.commit()
        alerts.email_admins(db, new_alerts, at)
        return {"configured": True, "error": str(e)}

    saved = 0
    last_uid = state.get("last_uid") if batch.uidvalidity == state.get("uidvalidity") else None
    for item in batch.messages:
        last_uid = item.uid if last_uid is None else max(last_uid, item.uid)
        try:
            msg = email.message_from_bytes(item.raw)
            for xml in report_files(msg):
                report = parse(xml)
                if report is not None:
                    with db.begin_nested():
                        saved += store(db, report)
        except Exception:
            log.exception("couldn't read DMARC message %s", item.uid)
    state.update(
        uidvalidity=batch.uidvalidity,
        last_uid=last_uid,
        checked_at=at.isoformat(),
        error=None,
        reports=int(state.get("reports") or 0) + saved,
    )
    set_json(db, STATE_KEY, state, None)
    alerts.sync(db, "dmarc_mailbox_failing", [], at)
    new_alerts = evaluate(db, at)
    db.commit()
    alerts.email_admins(db, new_alerts, at)
    return {"configured": True, "messages": len(batch.messages), "reports": saved}


# --- Numbers --------------------------------------------------------------------------------------------


def pass_rates(db: Session, since: datetime) -> dict[str, tuple[int, int]]:
    """{domain: (emails that passed DMARC, all emails)} in reports ending after `since`."""
    passed = func.sum(DmarcRecord.count).filter((DmarcRecord.dkim == "pass") | (DmarcRecord.spf == "pass"))
    rows = db.execute(
        select(DmarcReport.domain, func.coalesce(passed, 0), func.coalesce(func.sum(DmarcRecord.count), 0))
        .join(DmarcRecord, DmarcRecord.report_id == DmarcReport.id)
        .where(DmarcReport.end_at >= since)
        .group_by(DmarcReport.domain)
    ).all()
    return {domain: (int(p), int(t)) for domain, p, t in rows}


def sources(db: Session, since: datetime) -> list[dict[str, Any]]:
    """Per domain and sending server: emails, how many passed, and who reported them."""
    passed = func.sum(DmarcRecord.count).filter((DmarcRecord.dkim == "pass") | (DmarcRecord.spf == "pass"))
    rows = db.execute(
        select(
            DmarcReport.domain,
            DmarcRecord.source_ip,
            func.coalesce(func.sum(DmarcRecord.count), 0),
            func.coalesce(passed, 0),
            func.coalesce(func.sum(DmarcRecord.count).filter(DmarcRecord.dkim == "pass"), 0),
            func.coalesce(func.sum(DmarcRecord.count).filter(DmarcRecord.spf == "pass"), 0),
            func.array_agg(func.distinct(DmarcReport.reporter)),
            func.max(DmarcReport.end_at),
        )
        .join(DmarcRecord, DmarcRecord.report_id == DmarcReport.id)
        .where(DmarcReport.end_at >= since)
        .group_by(DmarcReport.domain, DmarcRecord.source_ip)
        .order_by(func.sum(DmarcRecord.count).desc())
    ).all()
    return [
        {
            "domain": d,
            "source_ip": ip,
            "emails": int(total),
            "passed": int(ok),
            "dkim_pass": int(dkim),
            "spf_pass": int(spf),
            "reporters": sorted(r for r in reporters if r),
            "last_report_at": last,
        }
        for d, ip, total, ok, dkim, spf, reporters, last in rows
    ]


def evaluate(db: Session, at: datetime) -> list:
    """Alert on domains whose DMARC pass rate dropped below the threshold. The caller commits and emails."""
    limit = alerts.thresholds(db).dmarc_pass_pct
    conditions = [
        alerts.Condition(
            f"Only {passed / total:.0%} of {total} emails sent as {domain} in the last {PASS_RATE_DAYS} days "
            f"passed DMARC (target {limit:g}%). Check the failing servers on the Deliverability page.",
            target=domain,
        )
        for domain, (passed, total) in pass_rates(db, at - timedelta(days=PASS_RATE_DAYS)).items()
        if total >= MIN_EMAILS_FOR_ALERT and passed / total * 100 < limit
    ]
    return alerts.sync(db, "dmarc_low", conditions, at)
