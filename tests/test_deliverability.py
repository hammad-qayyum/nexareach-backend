"""Deliverability (milestone 10): the send log, bounces, cPanel's delivery log, alerts and pauses, health
checks, DMARC reports, placement tests, and the page's API. No real mail server, DNS or cPanel is used."""

import email
import gzip
import io
import ssl
import zipfile
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest
from sqlalchemy import func, select

from app.core.crypto import encrypt
from app.models import (
    ActivityLog,
    Alert,
    AlertStatus,
    CampaignRecipient,
    DeliveryEvent,
    DeliveryStatus,
    DoNotContact,
    HelperProvider,
    Mailbox,
    PlacementFolder,
    PlacementResult,
    PlacementState,
    PlacementTest,
    RecipientStatus,
    Role,
    Setting,
    SmtpSecurity,
)
from app.services import (
    alerts,
    cpanel,
    deliverability,
    delivery,
    dmarc,
    domain_health,
    mailer,
    placement,
    replies,
)
from app.services import sending as sending_service
from app.services.app_settings import set_json
from tests.test_campaigns import TUESDAY_11AM_PKT, ready, smtp  # noqa: F401 (shared fixtures)
from tests.test_follow_ups import FakeInbox, _start_and_send_first, campaign  # noqa: F401
from tests.test_warmup import FakeInbox as FakeImap
from tests.test_warmup import _helper

DAY2 = TUESDAY_11AM_PKT + timedelta(days=2)


class QueueSmtp:
    """mailer.send stand-in whose replies carry an Exim queue ID, like cPanel servers."""

    def __init__(self):
        self.sent: list[EmailMessage] = []
        self.n = 0

    def __call__(self, mailbox, msg):
        self.n += 1
        self.sent.append(msg)
        return mailer.Sent(message_id=msg["Message-ID"], response=f"250 OK id=1tAbC{self.n}-000123-Xy")


class MoveRecorder:
    """mailer.Inbox stand-in for filing bounce emails."""

    def __init__(self):
        self.moved: list[tuple[str, str, str]] = []

    def __call__(self, mailbox):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def ensure_folder(self, name):
        return f"INBOX.{name}"

    def move(self, folder, uid, destination):
        self.moved.append((folder, uid, destination))


def _dsn(
    first: EmailMessage, status: str = "5.1.1", diagnostic: str = "550 5.1.1 <sara@acme.com>: user unknown"
) -> bytes:
    return (
        "From: Mail Delivery System <mailer-daemon@mail.fastnexa.com>\r\n"
        "To: ali@fastnexa.com\r\nSubject: Mail delivery failed\r\n"
        "Date: Thu, 12 Nov 2026 10:00:00 +0000\r\nMessage-ID: <bounce1@mail.fastnexa.com>\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/report; report-type=delivery-status; boundary="B"\r\n\r\n'
        "--B\r\nContent-Type: text/plain\r\n\r\nThis message couldn't be delivered.\r\n"
        "--B\r\nContent-Type: message/delivery-status\r\n\r\n"
        "Reporting-MTA: dns; mail.fastnexa.com\r\n\r\n"
        f"Final-Recipient: rfc822; sara@acme.com\r\nAction: failed\r\nStatus: {status}\r\n"
        f"Remote-MTA: dns; mx.acme.com\r\nDiagnostic-Code: smtp; {diagnostic}\r\n"
        f"--B\r\nContent-Type: text/rfc822-headers\r\n\r\nMessage-ID: {first['Message-ID']}\r\n--B--\r\n"
    ).encode()


# --- Bounce types -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("550 5.1.1 <x@y.com>: Recipient address rejected: User unknown", DeliveryStatus.HARD_BOUNCE),
        ("550 No such user here", DeliveryStatus.HARD_BOUNCE),
        ("552 5.2.2 Mailbox full", DeliveryStatus.SOFT_BOUNCE),
        # Refused because of us (spam filter), not because the address is dead: never do-not-contact.
        ("550 5.7.1 Message rejected as spam", DeliveryStatus.SOFT_BOUNCE),
        ("421 4.7.0 Try again later", DeliveryStatus.SOFT_BOUNCE),
    ],
)
def test_bounce_kinds(text, kind):
    assert delivery.bounce_kind(text) == kind


def test_queue_id_is_read_from_the_server_reply():
    assert delivery.queue_id("250 OK id=1tAbCd-000123-Xy") == "1tAbCd-000123-Xy"
    assert delivery.queue_id("250 2.0.0 Ok: queued as 4F3A") is None


# --- The send log and bounce emails -----------------------------------------------------------------------


def test_each_send_is_logged_with_its_queue_id(client, db, campaign):
    smtp = QueueSmtp()
    recipient = _start_and_send_first(client, db, smtp, campaign)
    event = db.scalar(select(DeliveryEvent))
    assert (event.source, event.status, event.queue_id) == ("SMTP", DeliveryStatus.SENT, "1tAbC1-000123-Xy")
    assert event.recipient_id == recipient.id and event.email == "sara@acme.com"
    # "Sent" says nothing beyond the send itself.
    assert recipient.delivery_status is None


def test_a_refused_send_is_logged(client, db, smtp, campaign):
    client.post(f"/api/campaigns/{campaign['id']}/start")
    smtp.error = mailer.SendFailed("550 5.1.1 user unknown", temporary=False, code=550)
    assert sending_service.send_next(db, campaign["mailbox_id"], TUESDAY_11AM_PKT, smtp) == "failed"
    event = db.scalar(select(DeliveryEvent))
    assert (event.status, event.response) == (DeliveryStatus.REJECTED, "550 5.1.1 user unknown")


def test_a_hard_bounce_goes_on_do_not_contact_and_is_filed(client, db, smtp, campaign):
    recipient = _start_and_send_first(client, db, smtp, campaign)
    inbox, mover = FakeInbox(_dsn(smtp.sent[0])), MoveRecorder()
    counts = replies.check_mailbox(db, campaign["mailbox_id"], DAY2, inbox, mover)
    assert counts["bounce"] == 1
    assert recipient.delivery_status == DeliveryStatus.HARD_BOUNCE
    assert recipient.follow_up_stop == "bounced" and recipient.next_follow_up_at is None
    event = db.scalar(select(DeliveryEvent).where(DeliveryEvent.source == "BOUNCE"))
    assert event.remote_host == "mx.acme.com" and "user unknown" in event.response
    assert db.scalar(select(DoNotContact.reason).where(DoNotContact.value == "sara@acme.com")) == (
        delivery.HARD_BOUNCE_REASON
    )
    assert mover.moved == [("INBOX", "1", "INBOX.Bounces")]
    # Reading the inbox again (say after a restart) records nothing twice.
    mailbox = db.get(Mailbox, campaign["mailbox_id"])
    mailbox.inbox_last_uid = None
    assert replies.check_mailbox(db, campaign["mailbox_id"], DAY2, inbox, mover)["bounce"] == 0
    # The person's timeline in the campaign report.
    timeline = client.get(f"/api/campaigns/{campaign['id']}/recipients").json()["items"][0]
    assert timeline["delivery_status"] == "HARD_BOUNCE"
    assert [e["status"] for e in timeline["delivery"]] == ["SENT", "HARD_BOUNCE"]


def test_a_soft_bounce_stops_follow_ups_but_keeps_the_address(client, db, smtp, campaign):
    recipient = _start_and_send_first(client, db, smtp, campaign)
    bounce = _dsn(smtp.sent[0], status="5.2.2", diagnostic="552 5.2.2 Mailbox full")
    replies.check_mailbox(db, campaign["mailbox_id"], DAY2, FakeInbox(bounce), MoveRecorder())
    assert recipient.delivery_status == DeliveryStatus.SOFT_BOUNCE
    assert recipient.follow_up_stop == "bounced"
    assert db.scalar(select(DoNotContact.id)) is None


# --- cPanel's delivery log --------------------------------------------------------------------------------


def _line(**kw):
    base = {
        "email": "ali@fastnexa.com",
        "recipient": "sara@acme.com",
        "type": "success",
        "message": "250 2.0.0 OK",
        "host": "mx.acme.com",
        "sendunixtime": int(TUESDAY_11AM_PKT.timestamp()),
        "actionunixtime": int(TUESDAY_11AM_PKT.timestamp()) + 5,
    }
    return {**base, **kw}


def _set_cpanel(db):
    db.add(
        Setting(
            key=cpanel.SETTINGS_KEY,
            value={
                "host": "fastnexa.com",
                "port": 2083,
                "username": "fastnexa",
                "token_encrypted": encrypt("tok"),
            },
        )
    )
    db.flush()


def test_cpanel_lines_are_matched_by_queue_id_or_address(client, db, campaign):
    smtp = QueueSmtp()
    recipient = _start_and_send_first(client, db, smtp, campaign)
    _set_cpanel(db)
    at = TUESDAY_11AM_PKT + timedelta(minutes=30)
    asked = []

    def fetch(cfg, since):
        asked.append((cfg.host, cfg.token, since))
        return [
            _line(type="defer", message="451 4.7.1 Greylisted, try again", msgid="1tAbC1-000123-Xy"),
            _line(type="defer", message="451 4.7.1 Greylisted, try again", msgid="1tAbC1-000123-Xy"),
            # No queue ID: matched by address and time.
            _line(type="success"),
            # Not one of our mailboxes.
            _line(email="someone@else.com", type="failure", message="550 5.1.1 unknown"),
        ]

    result = cpanel.sync(db, at, fetch)
    assert result == {"configured": True, "lines": 4, "added": 2}
    assert asked[0][:2] == ("fastnexa.com", "tok")
    assert recipient.delivery_status == DeliveryStatus.DELIVERED
    statuses = db.scalars(select(DeliveryEvent.status).order_by(DeliveryEvent.id)).all()
    assert statuses == [DeliveryStatus.SENT, DeliveryStatus.DEFERRED, DeliveryStatus.DELIVERED]
    # The next sync sees the same lines and adds nothing.
    assert cpanel.sync(db, at + timedelta(minutes=10), fetch)["added"] == 0


def test_a_cpanel_failure_line_is_a_bounce(client, db, campaign):
    smtp = QueueSmtp()
    recipient = _start_and_send_first(client, db, smtp, campaign)
    _set_cpanel(db)
    lines = [
        _line(type="failure", message="550 5.1.1 <sara@acme.com>: User unknown", msgid="1tAbC1-000123-Xy")
    ]
    cpanel.sync(db, TUESDAY_11AM_PKT + timedelta(minutes=5), lambda cfg, since: lines)
    assert recipient.delivery_status == DeliveryStatus.HARD_BOUNCE
    assert db.scalar(select(DoNotContact.id).where(DoNotContact.value == "sara@acme.com")) is not None


def test_cpanel_errors_raise_an_alert(db, admin):
    _set_cpanel(db)

    def broken(cfg, since):
        raise cpanel.CpanelFailed("cPanel refused the username or API token.")

    assert cpanel.sync(db, TUESDAY_11AM_PKT, broken)["error"].startswith("cPanel refused")
    alert = db.scalar(select(Alert).where(Alert.kind == "cpanel_failing"))
    assert alert.status == AlertStatus.OPEN
    cpanel.sync(db, TUESDAY_11AM_PKT + timedelta(minutes=10), lambda cfg, since: [])
    assert alert.status == AlertStatus.RESOLVED


# --- Hard-bounce alert, pause and resume ------------------------------------------------------------------


def _sent_rows(db, mailbox_id, campaign_id, n, hard, start):
    for i in range(n):
        db.add(
            CampaignRecipient(
                campaign_id=campaign_id,
                email=f"p{i}@example.com",
                status=RecipientStatus.SENT,
                mailbox_id=mailbox_id,
                sent_at=start + timedelta(minutes=i),
                delivery_status=DeliveryStatus.HARD_BOUNCE.value if i >= n - hard else None,
            )
        )
    db.flush()


def pause_on(db) -> None:
    """Switch on "Pause sending on serious problems" (off by default: alerts only warn)."""
    set_json(db, alerts.THRESHOLDS_KEY, {"auto_pause": True}, None)


def test_serious_problems_only_warn_until_pausing_is_switched_on(client, db, ready):
    mailbox_id, campaign_id = ready["mailbox"]["id"], ready["campaign"]["id"]
    _sent_rows(db, mailbox_id, campaign_id, 100, 5, TUESDAY_11AM_PKT)
    new = deliverability.evaluate(db, DAY2)
    assert [a.kind for a in new] == ["hard_bounces"] and new[0].paused_mailbox_ids == []
    mailbox = db.get(Mailbox, mailbox_id)
    assert mailbox.campaigns_paused_at is None
    # Switched on while the problem is still there: the next check pauses, without a second alert.
    r = client.put("/api/deliverability/settings/thresholds", json={"auto_pause": True})
    assert r.status_code == 200 and r.json()["thresholds"]["auto_pause"] is True
    assert deliverability.evaluate(db, DAY2 + timedelta(minutes=10)) == []
    assert mailbox.campaigns_paused_reason == "hard_bounces"
    assert db.get(Alert, new[0].id).paused_mailbox_ids == [mailbox_id]
    assert db.scalar(select(func.count()).select_from(Alert)) == 1


def test_too_many_hard_bounces_pause_the_mailbox_until_resumed(client, db, ready):
    pause_on(db)
    mailbox_id, campaign_id = ready["mailbox"]["id"], ready["campaign"]["id"]
    # 3 of 100 is the limit, not over it.
    _sent_rows(db, mailbox_id, campaign_id, 100, 3, TUESDAY_11AM_PKT)
    assert deliverability.evaluate(db, DAY2) == []
    # A fourth: over 3%. Fewer than 100 emails still count as out of 100.
    _sent_rows(db, mailbox_id, campaign_id, 1, 1, TUESDAY_11AM_PKT + timedelta(days=1))
    new = deliverability.evaluate(db, DAY2)
    assert [a.kind for a in new] == ["hard_bounces"]
    mailbox = db.get(Mailbox, mailbox_id)
    assert mailbox.campaigns_paused_reason == "hard_bounces"
    assert new[0].paused_mailbox_ids == [mailbox_id]
    # Still true later: the same alert, no second one.
    assert deliverability.evaluate(db, DAY2 + timedelta(minutes=10)) == []

    count = client.get("/api/deliverability/alerts/count").json()
    assert count == {"open": 1, "critical": 1, "active": 1, "paused_mailboxes": 1}
    r = client.post(f"/api/deliverability/alerts/{new[0].id}/resume")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ACKNOWLEDGED" and body["paused_mailboxes"][0]["paused"] is False
    assert mailbox.campaigns_paused_at is None and mailbox.campaigns_resumed_at is not None
    # After resuming only new emails count, so the old bounces don't pause it again: the alert resolves.
    deliverability.evaluate(db, DAY2 + timedelta(minutes=20))
    assert db.get(Alert, new[0].id).status == AlertStatus.RESOLVED
    assert mailbox.campaigns_paused_at is None


def test_alerts_are_emailed_to_admins_from_the_invite_mailbox(client, db, ready, make_user):
    mailbox_id = ready["mailbox"]["id"]
    db.get(Mailbox, mailbox_id).status = "ACTIVE"
    client.put("/api/settings/invites", json={"mailbox_id": mailbox_id})
    make_user("second-admin@fastnexa.com", role=Role.ADMIN)
    make_user("member@fastnexa.com", role=Role.MEMBER)
    [alert] = alerts.sync(
        db, "cert_expiring", [alerts.Condition("mail.fastnexa.com: expires in 5 days", target="x")], DAY2
    )
    smtp = QueueSmtp()
    alerts.email_admins(db, [alert], DAY2, smtp)
    assert sorted(m["To"] for m in smtp.sent) == ["admin@fastnexa.com", "second-admin@fastnexa.com"]
    assert smtp.sent[0]["Subject"] == "NexaReach alert: Certificate expiring"
    assert "/deliverability" in smtp.sent[0].get_content()
    assert alert.emailed_at == DAY2 and alert.email_error is None


def test_without_an_invite_mailbox_alerts_only_show_in_the_app(db, admin):
    [alert] = alerts.sync(
        db, "deferred", [alerts.Condition("many delays", mailbox_id=None, target="t")], DAY2
    )
    alerts.email_admins(db, [alert], DAY2, QueueSmtp())
    assert alert.emailed_at is None and "Invite emails" in alert.email_error


def test_failing_logins_and_helpers_raise_alerts(client, db, ready):
    mailbox = db.get(Mailbox, ready["mailbox"]["id"])
    # A mailbox that was never tested isn't a failing login.
    mailbox.status, mailbox.last_error = "ERROR", deliverability.NOT_TESTED
    assert deliverability.evaluate(db, DAY2) == []
    mailbox.status, mailbox.last_error = "ERROR", "535 Incorrect authentication data"
    _helper(db, status="ERROR", last_error="Login rejected")
    kinds = sorted(a.kind for a in deliverability.evaluate(db, DAY2))
    assert kinds == ["helper_broken", "login_failing"]


# --- Health checks ----------------------------------------------------------------------------------------


GOOD_DNS = {
    ("fastnexa.com", "MX"): ["0 mail.fastnexa.com"],
    ("fastnexa.com", "TXT"): ["v=spf1 +a +mx +ip4:64.20.37.26 ~all", "google-site-verification=x"],
    ("default._domainkey.fastnexa.com", "TXT"): ["v=DKIM1; k=rsa; p=MIIBIjANBgkq"],
    ("_dmarc.fastnexa.com", "TXT"): ["v=DMARC1; p=none; rua=mailto:dmarc@fastnexa.com; fo=1;"],
    ("mail.fastnexa.com", "A"): ["64.20.37.26"],
}


def _lookup(records, listed=(), broken=()):
    def lookup(name, rdtype):
        if name in broken:
            raise domain_health.LookupFailed("timed out")
        if any(name.startswith("26.37.20.64.") and name.endswith(zone) for zone in listed):
            return ["127.0.0.2"]
        return records.get((name, rdtype), [])

    return lookup


def _cert(days):
    def expiry(host, port, security):
        assert (host, port, security) == ("mail.fastnexa.com", 465, SmtpSecurity.SSL)
        return DAY2 + timedelta(days=days)

    return expiry


def test_a_healthy_domain(db, ready):
    results = domain_health.run(db, DAY2, _lookup(GOOD_DNS), _cert(60))
    by_kind = {(r.kind.value, r.target): r for r in results}
    assert {k: r.status.value for k, r in by_kind.items()} == {
        ("MX", "fastnexa.com"): "OK",
        ("SPF", "fastnexa.com"): "OK",
        ("DKIM", "fastnexa.com"): "OK",
        ("DMARC", "fastnexa.com"): "OK",
        ("CERTIFICATE", "mail.fastnexa.com"): "OK",
        ("BLACKLIST", "64.20.37.26"): "OK",
    }
    assert "monitoring only" in by_kind[("DMARC", "fastnexa.com")].detail
    assert db.scalar(select(Alert.id)) is None
    assert by_kind[("BLACKLIST", "64.20.37.26")].data["mailbox_ids"] == [ready["mailbox"]["id"]]


def test_broken_dns_and_a_blacklisting_pause_sending(db, ready):
    pause_on(db)
    records = {**GOOD_DNS, ("fastnexa.com", "TXT"): []}
    domain_health.run(db, DAY2, _lookup(records, listed=("bl.spamcop.net",)), _cert(5))
    kinds = {a.kind: a for a in db.scalars(select(Alert))}
    assert set(kinds) == {"dns_broken", "blacklisted", "cert_expiring"}
    assert "SPF: No SPF record" in kinds["dns_broken"].message
    assert "SpamCop" in kinds["blacklisted"].message
    assert kinds["dns_broken"].paused_mailbox_ids == [ready["mailbox"]["id"]]
    assert db.get(Mailbox, ready["mailbox"]["id"]).campaigns_paused_reason == "dns_broken"
    # Fixed the next day: every alert resolves (sending stays paused until someone resumes it).
    domain_health.run(db, DAY2 + timedelta(days=1), _lookup(GOOD_DNS), _cert(60))
    assert {a.status for a in db.scalars(select(Alert))} == {AlertStatus.RESOLVED}
    assert db.get(Mailbox, ready["mailbox"]["id"]).campaigns_paused_at is not None


def test_a_check_that_cannot_run_never_alerts_or_resolves(db, ready):
    domain_health.run(db, DAY2, _lookup({**GOOD_DNS, ("fastnexa.com", "TXT"): []}), _cert(60))
    assert db.scalar(select(Alert.kind)) == "dns_broken"
    # DNS times out: UNKNOWN, and the open alert stays as it was.
    results = domain_health.run(
        db, DAY2 + timedelta(days=1), _lookup(GOOD_DNS, broken={"fastnexa.com"}), _cert(60)
    )
    assert {
        r.status.value for r in results if r.target == "fastnexa.com" and r.kind.value in ("MX", "SPF")
    } == {"UNKNOWN"}
    assert db.scalar(select(Alert.status)) == AlertStatus.OPEN


def test_an_invalid_certificate_fails(db, ready):
    def invalid(host, port, security):
        raise ssl.SSLCertVerificationError("certificate has expired")

    results = domain_health.run(db, DAY2, _lookup(GOOD_DNS), invalid)
    cert = next(r for r in results if r.kind.value == "CERTIFICATE")
    assert cert.status.value == "FAIL" and "isn't valid" in cert.detail


def test_spamhaus_is_only_asked_with_a_key(db, ready):
    asked = []

    def lookup(name, rdtype):
        asked.append(name)
        return _lookup(GOOD_DNS)(name, rdtype)

    domain_health.run(db, DAY2, lookup, _cert(60))
    assert not any("spamhaus" in n for n in asked)
    db.add(Setting(key=domain_health.SETTINGS_KEY, value={"spamhaus_key_encrypted": encrypt("abc123")}))
    db.flush()
    domain_health.run(db, DAY2, lookup, _cert(60))
    assert "26.37.20.64.abc123.zen.dq.spamhaus.net" in asked
    assert "fastnexa.com.abc123.dbl.dq.spamhaus.net" in asked


def test_test_domains_are_not_checked(db, admin):
    db.add(
        Mailbox(
            email="sender1@example.com",
            display_name="Dev",
            smtp_host="localhost",
            smtp_port=1025,
            smtp_security=SmtpSecurity.NONE,
            smtp_username="sender1@example.com",
            smtp_password_encrypted=encrypt("x"),
            status="ACTIVE",
        )
    )
    db.flush()
    assert domain_health.run(db, DAY2, _lookup({}), _cert(60)) == []


def test_run_checks_from_the_page(client, db, ready, monkeypatch):
    monkeypatch.setattr(domain_health, "dns_lookup", _lookup(GOOD_DNS))
    monkeypatch.setattr(domain_health, "cert_expiry", _cert(60))
    r = client.post("/api/deliverability/health/run")
    assert r.status_code == 200, r.text
    assert len(r.json()["results"]) == 6
    history = client.get("/api/deliverability/health/history?kind=SPF&target=fastnexa.com").json()
    assert history[0]["status"] == "OK"


# --- DMARC ------------------------------------------------------------------------------------------------


def _report_xml(report_id="r1", passed=90, failed=10):
    begin, end = int((DAY2 - timedelta(days=1)).timestamp()), int(DAY2.timestamp())
    return f"""<?xml version="1.0"?>
<feedback>
  <report_metadata><org_name>google.com</org_name><report_id>{report_id}</report_id>
    <date_range><begin>{begin}</begin><end>{end}</end></date_range>
  </report_metadata>
  <policy_published><domain>fastnexa.com</domain><p>none</p></policy_published>
  <record><row><source_ip>64.20.37.26</source_ip><count>{passed}</count>
    <policy_evaluated><disposition>none</disposition><dkim>pass</dkim><spf>pass</spf></policy_evaluated></row>
    <identifiers><header_from>fastnexa.com</header_from></identifiers></record>
  <record><row><source_ip>203.0.113.9</source_ip><count>{failed}</count>
    <policy_evaluated><disposition>none</disposition><dkim>fail</dkim><spf>fail</spf></policy_evaluated></row>
    <identifiers><header_from>fastnexa.com</header_from></identifiers></record>
</feedback>""".encode()


def _report_email(xml: bytes, kind="zip") -> bytes:
    msg = EmailMessage()
    msg["From"] = "noreply-dmarc-support@google.com"
    msg["To"] = "dmarc@fastnexa.com"
    msg["Subject"] = "Report domain: fastnexa.com"
    msg.set_content("DMARC report attached.")
    if kind == "zip":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("google.com!fastnexa.com!1!2.xml", xml)
        msg.add_attachment(buf.getvalue(), maintype="application", subtype="zip", filename="report.zip")
    else:
        msg.add_attachment(
            gzip.compress(xml), maintype="application", subtype="gzip", filename="report.xml.gz"
        )
    return msg.as_bytes()


def _set_dmarc(db):
    db.add(
        Setting(
            key=dmarc.SETTINGS_KEY,
            value={
                "email": "dmarc@fastnexa.com",
                "imap_host": "mail.fastnexa.com",
                "imap_port": 993,
                "password_encrypted": encrypt("pw"),
            },
        )
    )
    db.flush()


def test_dmarc_reports_are_read_and_a_low_pass_rate_alerts(client, db, admin):
    _set_dmarc(db)
    inbox = FakeInbox(_report_email(_report_xml("r1")), _report_email(_report_xml("r2"), kind="gz"))
    result = dmarc.check(db, DAY2, inbox)
    assert result == {"configured": True, "messages": 2, "reports": 2}
    assert dmarc.pass_rates(db, DAY2 - timedelta(days=7)) == {"fastnexa.com": (180, 200)}
    # 90% is under the 95% target.
    alert = db.scalar(select(Alert).where(Alert.kind == "dmarc_low"))
    assert alert is not None and "90%" in alert.message
    # The same reports again (a new UIDVALIDITY makes it re-read): nothing doubles.
    db.get(Setting, dmarc.STATE_KEY).value = {}
    db.flush()
    assert dmarc.check(db, DAY2, inbox)["reports"] == 0
    page = client.get("/api/deliverability/dmarc").json()
    assert page["rates"] == [{"domain": "fastnexa.com", "passed": 180, "total": 200}]
    failing = next(s for s in page["sources"] if s["source_ip"] == "203.0.113.9")
    assert (failing["emails"], failing["passed"], failing["reporters"]) == (20, 0, ["google.com"])


def test_dmarc_files_with_entities_are_refused():
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><feedback>&a;</feedback>'
    assert dmarc.parse(evil) is None


def test_a_dmarc_mailbox_that_cannot_be_read_alerts(db, admin):
    _set_dmarc(db)

    def broken(*args):
        raise mailer.InboxFailed("The IMAP server rejected the username or password.")

    assert "rejected" in dmarc.check(db, DAY2, broken)["error"]
    assert db.scalar(select(Alert.kind)) == "dmarc_mailbox_failing"


# --- Placement tests --------------------------------------------------------------------------------------


@pytest.fixture
def helpers(db):
    return [
        _helper(db, "helper1@gmail.com", HelperProvider.GMAIL),
        _helper(db, "helper2@outlook.com", HelperProvider.OUTLOOK),
    ]


class PlacementImap(FakeImap):
    """Each helper's inbox; Gmail's Promotions tab is a set of UIDs."""

    def __init__(self, by_email, promotions=()):
        super().__init__({})
        self.by_email = by_email
        self.promotions = set(promotions)
        self.current = None

    def __call__(self, account):
        self.folders = self.by_email.setdefault(account.email, {"INBOX": {}, "[Gmail]/Spam": {}})
        self.is_gmail = account.email.endswith("gmail.com")
        return self

    def gmail_search(self, folder, text, query):
        return [u for u in self.search(folder, text) if u in self.promotions]


def test_a_placement_test_reports_where_each_email_landed(client, db, ready, helpers):
    mailbox = db.get(Mailbox, ready["mailbox"]["id"])
    mailbox.status = "ACTIVE"
    r = client.post("/api/deliverability/placement", json={"mailbox_id": mailbox.id})
    assert r.status_code == 201, r.text
    test = r.json()
    assert test["campaign_id"] == ready["campaign"]["id"] and len(test["results"]) == 2
    assert client.post("/api/deliverability/placement", json={"mailbox_id": mailbox.id}).status_code == 409

    smtp = QueueSmtp()
    at = datetime.now(UTC)
    assert placement.run(db, at, smtp)["sent"] == 2
    gmail_msg = next(m for m in smtp.sent if m["To"] == "helper1@gmail.com")
    # The campaign's real subject, with the helper's first name filled in.
    assert "question for" in gmail_msg["Subject"] and "Hi Hamza" in gmail_msg.get_content()
    assert sending_service.sent_today(db, mailbox.id, at) == 2

    inbox = PlacementImap(
        {
            "helper1@gmail.com": {"INBOX": {"5": gmail_msg.as_bytes()}, "[Gmail]/Spam": {}},
            "helper2@outlook.com": {
                "INBOX": {},
                "[Gmail]/Spam": {
                    "9": next(m for m in smtp.sent if m["To"] == "helper2@outlook.com").as_bytes()
                },
            },
        },
        promotions={"5"},
    )
    # Too early to look.
    assert placement.run(db, at + timedelta(minutes=1), smtp, inbox)["checked"] == 0
    counts = placement.run(db, at + timedelta(minutes=5), smtp, inbox)
    assert counts == {"sent": 0, "checked": 2, "finished": 1}
    folders = {r.helper_email: r.folder for r in db.scalars(select(PlacementResult))}
    assert folders == {
        "helper1@gmail.com": PlacementFolder.PROMOTIONS,
        "helper2@outlook.com": PlacementFolder.SPAM,
    }
    # Half in spam is not more than the 50% limit.
    assert db.scalar(select(Alert.id)) is None
    card = next(c for c in client.get("/api/deliverability").json()["mailboxes"] if c["id"] == mailbox.id)
    assert card["placement"]["promotions"] == 1 and card["placement"]["spam"] == 1
    listed = client.get("/api/deliverability/placement").json()
    assert listed[0]["status"] == "DONE" and listed[0]["requested_by"] == "admin"


def test_missing_and_spam_raise_a_placement_alert(client, db, ready, helpers):
    mailbox = db.get(Mailbox, ready["mailbox"]["id"])
    mailbox.status = "ACTIVE"
    client.post("/api/deliverability/placement", json={"mailbox_id": mailbox.id, "use_sample": True})
    at = datetime.now(UTC)
    placement.run(db, at, QueueSmtp())
    inbox = PlacementImap({})
    assert placement.run(db, at + timedelta(minutes=30), QueueSmtp(), inbox)["checked"] == 0
    placement.run(db, at + timedelta(hours=2), QueueSmtp(), inbox)
    assert {r.folder for r in db.scalars(select(PlacementResult))} == {PlacementFolder.MISSING}
    assert db.scalar(select(Alert.kind)) == "placement_spam"
    assert db.scalar(select(PlacementTest.subject)) == "(sample message)"


def test_placement_needs_helpers(client, db, ready):
    db.get(Mailbox, ready["mailbox"]["id"]).status = "ACTIVE"
    r = client.post("/api/deliverability/placement", json={"mailbox_id": ready["mailbox"]["id"]})
    assert r.status_code == 409 and r.json()["detail"] == "no_helpers"


def test_weekly_tests_only_for_mailboxes_that_send(db, ready, helpers, campaign):
    mailbox = db.get(Mailbox, ready["mailbox"]["id"])
    mailbox.status = "ACTIVE"
    at = datetime.now(UTC)
    assert placement.weekly(db, at) == 0
    db.add(
        CampaignRecipient(
            campaign_id=campaign["id"],
            email="x@example.com",
            status=RecipientStatus.SENT,
            mailbox_id=mailbox.id,
            sent_at=at - timedelta(days=1),
        )
    )
    db.flush()
    assert placement.weekly(db, at) == 1
    db.scalar(select(PlacementTest)).status = "DONE"
    # Not again within the week.
    assert placement.weekly(db, at + timedelta(days=2)) == 0
    assert db.scalar(select(PlacementResult.state)) == PlacementState.WAITING


# --- The page's API ---------------------------------------------------------------------------------------


def test_overview_cards_and_provider_mix(client, db, ready):
    _sent_rows(
        db, ready["mailbox"]["id"], ready["campaign"]["id"], 10, 1, datetime.now(UTC) - timedelta(days=1)
    )
    body = client.get("/api/deliverability").json()
    card = body["mailboxes"][0]
    assert (card["sent"], card["hard_bounced"]) == (10, 1)
    assert card["bounce_rate"] == {"hard": 1, "emails": 10, "window": 100, "pct": 1.0}
    assert card["blacklist"] is None and card["placement"] is None
    assert sum(p["leads"] for p in body["provider_mix"]) == 3
    assert body["setup"]["cpanel_configured"] is False and body["thresholds"]["hard_bounce_pct"] == 3.0


def test_server_log_filters_and_export(client, db, campaign):
    smtp = QueueSmtp()
    _start_and_send_first(client, db, smtp, campaign)
    log = client.get("/api/deliverability/log").json()
    assert log["total"] == 1 and log["items"][0]["campaign_name"] == "Intro"
    assert log["items"][0]["mailbox_email"] == "ali@fastnexa.com"
    assert client.get("/api/deliverability/log?status=HARD_BOUNCE").json()["total"] == 0
    assert client.get("/api/deliverability/log?q=1tAbC1-000123-Xy").json()["total"] == 1
    csv_text = client.get("/api/deliverability/log/export").text
    assert csv_text.splitlines()[0].startswith("time,mailbox,to,campaign")
    assert "sara@acme.com" in csv_text
    assert db.scalar(select(ActivityLog.id).where(ActivityLog.action == "deliverability.log_exported"))


def test_settings_never_return_secrets_and_only_admins_change_them(client, db, admin, make_user, login):
    r = client.put(
        "/api/deliverability/settings/cpanel",
        json={
            "host": "https://FastNexa.com:2083/",
            "port": 2083,
            "username": "fastnexa",
            "token": "SECRET-TOKEN",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["cpanel"] == {
        "host": "fastnexa.com",
        "port": 2083,
        "username": "fastnexa",
        "token_set": True,
    }
    # Saving again without a token keeps it.
    client.put("/api/deliverability/settings/cpanel", json={"host": "fastnexa.com", "username": "other"})
    assert cpanel.config(db).token == "SECRET-TOKEN" and cpanel.config(db).username == "other"
    r = client.put(
        "/api/deliverability/settings/dmarc",
        json={"email": "dmarc@fastnexa.com", "imap_host": "mail.fastnexa.com", "password": "PW-SECRET"},
    )
    assert r.json()["dmarc"]["password_set"] is True
    r = client.put(
        "/api/deliverability/settings/checks", json={"dkim_selector": "default", "spamhaus_key": "abc123"}
    )
    assert r.json()["spamhaus_key_set"] is True
    r = client.put(
        "/api/deliverability/settings/thresholds", json={"hard_bounce_pct": 2, "bounce_window": 200}
    )
    assert r.json()["thresholds"]["hard_bounce_pct"] == 2
    assert alerts.thresholds(db).bounce_window == 200
    everything = client.get("/api/deliverability/settings").text
    for secret in ("SECRET-TOKEN", "PW-SECRET", "abc123"):
        assert secret not in everything
    logs = " ".join(str(a.after) for a in db.scalars(select(ActivityLog)))
    assert "SECRET-TOKEN" not in logs and "PW-SECRET" not in logs

    make_user("member@fastnexa.com", role=Role.MEMBER)
    client.post("/api/auth/logout")
    login("member@fastnexa.com")
    assert client.get("/api/deliverability/settings").status_code == 200
    assert client.put("/api/deliverability/settings/thresholds", json={}).status_code == 403
    assert client.put("/api/deliverability/settings/cpanel", json={"host": ""}).status_code == 403


def test_forgetting_a_person_removes_their_delivery_log(client, db, smtp, campaign):
    _start_and_send_first(client, db, smtp, campaign)
    replies.check_mailbox(db, campaign["mailbox_id"], DAY2, FakeInbox(_dsn(smtp.sent[0])), MoveRecorder())
    assert db.scalar(select(DeliveryEvent.id)) is not None
    exported = client.post("/api/people/export", json={"email": "sara@acme.com"}).json()
    assert [e["status"] for e in exported["emails"][0]["delivery"]] == ["SENT", "HARD_BOUNCE"]
    r = client.post("/api/people/forget", json={"email": "sara@acme.com"})
    assert r.status_code == 200, r.text
    assert db.scalar(select(DeliveryEvent.id)) is None


def test_bounce_details_from_a_plain_text_bounce():
    raw = (
        b"From: MAILER-DAEMON@mail.fastnexa.com\r\nTo: ali@fastnexa.com\r\nSubject: Undelivered Mail\r\n\r\n"
        b"This message was created automatically by mail delivery software.\r\n\r\n"
        b"  sara@acme.com\r\n    host mx.acme.com said: 550 5.1.1 The email account does not exist\r\n"
    )
    text, code, remote = replies.bounce_details(email.message_from_bytes(raw))
    assert "550 5.1.1" in text and code is None and remote is None
    assert delivery.bounce_kind(text, code) == DeliveryStatus.HARD_BOUNCE


def test_simulate_bounce_in_development(client, db, smtp, campaign, monkeypatch):
    from app.cli import DevOnlyError, apply_simulated_bounce
    from app.core.config import get_settings

    recipient = _start_and_send_first(client, db, smtp, campaign)
    with pytest.raises(DevOnlyError):
        apply_simulated_bounce(db, "sara@acme.com")
    monkeypatch.setattr(get_settings(), "app_env", "development")
    assert "hard bounce" in apply_simulated_bounce(db, "Sara@Acme.com")
    assert recipient.delivery_status == DeliveryStatus.HARD_BOUNCE
    assert db.scalar(select(DoNotContact.id).where(DoNotContact.value == "sara@acme.com")) is not None
    # A later, weaker event never hides a hard bounce.
    assert "soft bounce" in apply_simulated_bounce(db, "sara@acme.com", soft=True)
    assert recipient.delivery_status == DeliveryStatus.HARD_BOUNCE
