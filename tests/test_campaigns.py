from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import ActivityLog, CampaignRecipient, Mailbox, MailboxStatus, RecipientStatus, Role
from app.services import mailer, sending

# A Tuesday, 11:00 in Asia/Karachi.
TUESDAY_11AM_PKT = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)

MAILBOX = {
    "email": "Ali@FastNexa.com",
    "display_name": "Ali Khan",
    "smtp_host": "mail.fastnexa.com",
    "smtp_port": 465,
    "smtp_security": "SSL",
    "smtp_username": "ali@fastnexa.com",
    "smtp_password": "secret-password",
}

ROWS = [
    {"email": "sara@acme.com", "first_name": "Sara", "company": "Acme", "custom": {"industry": "Logistics"}},
    {"email": "SARA@acme.com ", "first_name": "Sara again"},
    {"email": "not-an-email"},
    {"email": "omar@globex.com", "first_name": "Omar", "company": "Globex"},
    {"email": "lee@blocked.com", "first_name": "Lee"},
]


class FakeSmtp:
    """Stands in for mailer.send. Raise `error` instead of sending when it is set."""

    def __init__(self):
        self.sent: list = []
        self.error: Exception | None = None

    def __call__(self, mailbox, msg):
        if self.error:
            raise self.error
        self.sent.append(msg)
        return mailer.Sent(message_id=msg["Message-ID"], response="250 OK queued as ABC123")


@pytest.fixture
def smtp():
    return FakeSmtp()


@pytest.fixture
def ready(client, admin):
    """Footer, one mailbox, one imported group and a draft campaign that may send at any time."""
    client.put(
        "/api/settings/footer", json={"business_name": "FastNexa", "postal_address": "1 Main St, Lahore"}
    )
    mailbox = client.post("/api/mailboxes", json=MAILBOX).json()
    imported = client.post(
        "/api/groups/import", json={"name": "October", "source": "LinkedIn", "rows": ROWS}
    ).json()
    campaign = client.post(
        "/api/campaigns",
        json={
            "name": "Intro",
            "subject": "{Quick|Short} question for {{company|your team}}",
            "body": "Hi {{first_name|there}},\n\n"
            "We help {{industry|logistics}} companies.\n\n{{sender_name}}",
            "group_ids": [imported["group"]["id"]],
            "mailbox_ids": [mailbox["id"]],
            "send_days": [0, 1, 2, 3, 4, 5, 6],
            "start_hour": 0,
            "end_hour": 24,
        },
    ).json()
    return {"mailbox": mailbox, "import": imported, "campaign": campaign}


def test_import_cleans_and_counts_rows(client, ready):
    result = ready["import"]
    # SARA@acme.com repeats sara@acme.com: it waits as a pending duplicate instead of joining the group.
    assert (result["added"], result["pending_duplicates"], result["invalid_count"]) == (3, 1, 1)
    assert result["invalid"] == [{"row": 3, "email": "not-an-email", "reason": "bad_syntax"}]
    assert result["group"]["pending_count"] == 1

    leads = client.get(f"/api/groups/{result['group']['id']}/leads").json()
    assert leads["total"] == 3
    sara = next(lead for lead in leads["items"] if lead["email"] == "sara@acme.com")
    assert sara["custom"] == {"industry": "Logistics"}


def test_mailbox_password_is_never_returned(client, ready):
    body = client.get("/api/mailboxes").json()[0]
    assert body["email"] == "ali@fastnexa.com"
    assert "smtp_password" not in body and "smtp_password_encrypted" not in body


def test_plain_smtp_is_refused_unless_allowed(client, admin):
    r = client.post("/api/mailboxes", json={**MAILBOX, "smtp_security": "NONE"})
    assert r.status_code == 400
    assert r.json()["detail"] == "plain_smtp_not_allowed"


def test_viewers_cannot_change_anything(client, make_user, login):
    make_user("viewer@fastnexa.com", role=Role.VIEWER)
    login("viewer@fastnexa.com")
    assert client.get("/api/campaigns").status_code == 200
    assert client.post("/api/mailboxes", json=MAILBOX).status_code == 403
    assert client.post("/api/groups/import", json={"name": "x", "rows": ROWS}).status_code == 403
    assert client.post("/api/campaigns", json={"name": "x"}).status_code == 403


def test_start_needs_a_footer(client, ready):
    client.put("/api/settings/footer", json={"business_name": "", "postal_address": ""})
    r = client.post(f"/api/campaigns/{ready['campaign']['id']}/start")
    assert r.json()["detail"] == "footer_missing"


def test_start_skips_do_not_contact(client, ready):
    client.post("/api/dnc", json={"entries": "blocked.com\nnobody@acme.com", "reason": "Asked to stop"})
    r = client.post(f"/api/campaigns/{ready['campaign']['id']}/start")
    assert r.status_code == 200
    assert r.json()["status"] == "RUNNING"
    assert r.json()["counts"] == {
        "total": 3,
        "waiting": 2,
        "sending": 0,
        "sent": 0,
        "failed": 0,
        "skipped": 1,
        "cancelled": 0,
    }


def test_sending_paces_each_mailbox(client, ready, db, smtp):
    campaign_id = ready["campaign"]["id"]
    mailbox_id = ready["mailbox"]["id"]
    client.post(f"/api/campaigns/{campaign_id}/start")

    assert sending.send_next(db, mailbox_id, TUESDAY_11AM_PKT, smtp) == "sent"
    msg = smtp.sent[0]
    assert msg["From"] == "Ali Khan <ali@fastnexa.com>"
    assert msg["To"] == "sara@acme.com"
    assert msg["Subject"] in {"Quick question for Acme", "Short question for Acme"}
    text = msg.get_content()
    assert text.startswith("Hi Sara,\n\nWe help Logistics companies.\n\nAli Khan\n\n--\nAli Khan · FastNexa")

    # The next email waits for the gap (5-10 minutes by default).
    assert sending.send_next(db, mailbox_id, TUESDAY_11AM_PKT + timedelta(minutes=4), smtp) == "not_due"
    later = TUESDAY_11AM_PKT + timedelta(minutes=11)
    assert sending.send_next(db, mailbox_id, later, smtp) == "sent"
    assert sending.send_next(db, mailbox_id, later + timedelta(minutes=11), smtp) == "sent"

    detail = client.get(f"/api/campaigns/{campaign_id}").json()
    assert detail["status"] == "COMPLETED"
    assert detail["counts"]["sent"] == 3

    recipients = client.get(f"/api/campaigns/{campaign_id}/recipients?status=SENT").json()["items"]
    assert recipients[0]["smtp_response"] == "250 OK queued as ABC123"
    assert recipients[0]["body_sent"] == text
    assert db.scalar(select(ActivityLog.id).where(ActivityLog.action == "email.sent")) is not None


def test_daily_cap_and_sending_hours(client, ready, db, smtp):
    campaign_id = ready["campaign"]["id"]
    mailbox_id = ready["mailbox"]["id"]
    client.patch(f"/api/mailboxes/{mailbox_id}", json={"daily_cap": 1})
    client.patch(
        f"/api/campaigns/{campaign_id}", json={"send_days": [0, 1, 2, 3, 4], "start_hour": 9, "end_hour": 17}
    )
    client.post(f"/api/campaigns/{campaign_id}/start")

    saturday = TUESDAY_11AM_PKT + timedelta(days=4)
    assert sending.send_next(db, mailbox_id, saturday, smtp) == "idle"
    evening = TUESDAY_11AM_PKT + timedelta(hours=8)  # 19:00 in Karachi
    assert sending.send_next(db, mailbox_id, evening, smtp) == "idle"

    assert sending.send_next(db, mailbox_id, TUESDAY_11AM_PKT, smtp) == "sent"
    assert sending.send_next(db, mailbox_id, TUESDAY_11AM_PKT + timedelta(hours=1), smtp) == "cap_reached"
    assert sending.send_next(db, mailbox_id, TUESDAY_11AM_PKT + timedelta(days=1), smtp) == "sent"


def test_temporary_errors_retry_then_fail(client, ready, db, smtp):
    campaign_id = ready["campaign"]["id"]
    mailbox_id = ready["mailbox"]["id"]
    client.post(f"/api/campaigns/{campaign_id}/start")
    smtp.error = mailer.SendFailed("451 try later", temporary=True, code=451)

    at = TUESDAY_11AM_PKT
    for _ in range(sending.MAX_RETRIES):
        assert sending.send_next(db, mailbox_id, at, smtp) == "retry"
        at += timedelta(minutes=16)
    assert sending.send_next(db, mailbox_id, at, smtp) == "failed"

    failed = client.get(f"/api/campaigns/{campaign_id}/recipients?status=FAILED").json()["items"]
    assert failed[0]["email"] == "sara@acme.com"
    assert failed[0]["attempts"] == sending.MAX_RETRIES + 1

    # A permanent error fails at once.
    smtp.error = mailer.SendFailed("550 no such user", temporary=False, code=550)
    assert sending.send_next(db, mailbox_id, at + timedelta(minutes=11), smtp) == "failed"


def test_rejected_login_stops_the_mailbox(client, ready, db, smtp):
    campaign_id = ready["campaign"]["id"]
    mailbox_id = ready["mailbox"]["id"]
    client.post(f"/api/campaigns/{campaign_id}/start")
    smtp.error = mailer.AuthFailed("535 bad credentials")

    assert sending.send_next(db, mailbox_id, TUESDAY_11AM_PKT, smtp) == "auth_failed"
    assert db.get(Mailbox, mailbox_id).status == MailboxStatus.ERROR
    waiting = db.scalars(
        select(CampaignRecipient).where(CampaignRecipient.status == RecipientStatus.WAITING)
    ).all()
    assert len(waiting) == 3 and all(r.attempts == 0 for r in waiting)


def test_pause_resume_and_cancel(client, ready, db, smtp):
    campaign_id = ready["campaign"]["id"]
    mailbox_id = ready["mailbox"]["id"]
    client.post(f"/api/campaigns/{campaign_id}/start")

    assert client.post(f"/api/campaigns/{campaign_id}/pause").json()["status"] == "PAUSED"
    assert sending.send_next(db, mailbox_id, TUESDAY_11AM_PKT, smtp) == "idle"
    # Content can change while paused; the recipient list can't.
    assert client.patch(f"/api/campaigns/{campaign_id}", json={"subject": "New subject"}).status_code == 200
    assert client.patch(f"/api/campaigns/{campaign_id}", json={"group_ids": []}).status_code == 409

    assert client.post(f"/api/campaigns/{campaign_id}/resume").json()["status"] == "RUNNING"
    assert sending.send_next(db, mailbox_id, TUESDAY_11AM_PKT, smtp) == "sent"
    assert smtp.sent[0]["Subject"] == "New subject"

    r = client.post(f"/api/campaigns/{campaign_id}/cancel").json()
    assert r["status"] == "CANCELLED"
    assert r["counts"]["cancelled"] == 2


def test_nobody_is_emailed_twice_within_30_days(client, ready, db, smtp):
    campaign_id = ready["campaign"]["id"]
    mailbox_id = ready["mailbox"]["id"]
    client.post(f"/api/campaigns/{campaign_id}/start")
    sending.send_next(db, mailbox_id, sending.now(), smtp)

    second = client.post(
        "/api/campaigns",
        json={
            "name": "Second",
            "subject": "Hi",
            "body": "Hello",
            "group_ids": [ready["import"]["group"]["id"]],
            "mailbox_ids": [mailbox_id],
        },
    ).json()
    counts = client.post(f"/api/campaigns/{second['id']}/start").json()["counts"]
    assert counts["skipped"] == 1
    assert counts["waiting"] == 2


def test_preview_reports_unknown_variables(client, ready):
    r = client.post(
        "/api/campaigns/preview",
        json={
            "subject": "Hi {{first_name}}",
            "body": "{{industry}} {{favourite_colour}}",
            "group_ids": [ready["import"]["group"]["id"]],
            "mailbox_id": ready["mailbox"]["id"],
        },
    ).json()
    assert len(r["versions"]) == 3
    assert "industry" in r["available_fields"]
    assert r["unknown_variables"] == ["favourite_colour"]
    assert "Ali Khan · FastNexa" in r["versions"][0]["body"]
