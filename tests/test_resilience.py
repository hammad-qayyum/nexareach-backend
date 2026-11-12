"""A crash, restart or slow mail server must never lose a lead's status or email anyone twice."""

import smtplib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import (
    ActivityLog,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    Mailbox,
    RecipientStatus,
)
from app.services import mailer, sending
from tests.test_campaigns import MAILBOX, ROWS, TUESDAY_11AM_PKT, FakeSmtp


class Crash(BaseException):
    """Stands in for the process being killed (not an ordinary error the code would catch)."""


@pytest.fixture
def running(client, admin, db):
    client.put("/api/settings/footer", json={"business_name": "FastNexa", "postal_address": "1 Main St"})
    mailbox = client.post("/api/mailboxes", json=MAILBOX).json()
    group = client.post("/api/groups/import", json={"name": "Oct", "rows": ROWS}).json()["group"]
    campaign = client.post(
        "/api/campaigns",
        json={
            "name": "Intro",
            "subject": "Hi {{first_name|there}}",
            "body": "Hello",
            "group_ids": [group["id"]],
            "mailbox_ids": [mailbox["id"]],
            "send_days": [0, 1, 2, 3, 4, 5, 6],
            "start_hour": 0,
            "end_hour": 24,
        },
    ).json()
    client.post(f"/api/campaigns/{campaign['id']}/start")
    return {"campaign_id": campaign["id"], "mailbox_id": mailbox["id"]}


def statuses(db, campaign_id):
    db.expire_all()
    rows = db.scalars(
        select(CampaignRecipient)
        .where(CampaignRecipient.campaign_id == campaign_id)
        .order_by(CampaignRecipient.id)
    )
    return [(r.email, r.status.value) for r in rows]


def test_campaign_continues_where_it_stopped_after_a_restart(running, db):
    smtp = FakeSmtp()
    at = TUESDAY_11AM_PKT
    assert sending.send_next(db, running["mailbox_id"], at, smtp) == "sent"

    # "Restart": everything in memory is gone; only the database remains.
    db.expire_all()
    db.close()
    at += timedelta(hours=3)
    assert sending.send_next(db, running["mailbox_id"], at, smtp) == "sent"
    at += timedelta(minutes=11)
    assert sending.send_next(db, running["mailbox_id"], at, smtp) == "sent"

    assert [m["To"] for m in smtp.sent] == ["sara@acme.com", "omar@globex.com", "lee@blocked.com"]
    assert db.get(Campaign, running["campaign_id"]).status == CampaignStatus.COMPLETED


def test_paused_campaign_stays_paused_after_a_restart(running, client, db):
    client.post(f"/api/campaigns/{running['campaign_id']}/pause")
    db.expire_all()
    assert db.get(Campaign, running["campaign_id"]).status == CampaignStatus.PAUSED
    assert sending.send_next(db, running["mailbox_id"], TUESDAY_11AM_PKT, FakeSmtp()) == "idle"


def test_crash_mid_send_never_emails_anyone_twice(running, db):
    def dies_after_handover(mailbox, msg):
        raise Crash  # the server may have taken the email, then the process was killed

    with pytest.raises(Crash):
        sending.send_next(db, running["mailbox_id"], TUESDAY_11AM_PKT, dies_after_handover)
    db.rollback()  # what a dead process's open transaction amounts to

    # The attempt was saved before the mail server was contacted.
    assert statuses(db, running["campaign_id"])[0] == ("sara@acme.com", "SENDING")
    recipient = db.scalar(select(CampaignRecipient).where(CampaignRecipient.email == "sara@acme.com"))
    assert recipient.body_sent.startswith("Hello\n\n--\nAli Khan · FastNexa\n1 Main St\n")

    # After the restart the mailbox keeps its pacing and Sara is not picked again.
    smtp = FakeSmtp()
    later = TUESDAY_11AM_PKT + timedelta(minutes=11)
    assert sending.send_next(db, running["mailbox_id"], later, smtp) == "sent"
    assert [m["To"] for m in smtp.sent] == ["omar@globex.com"]

    # The recovery pass settles Sara as failed, with a reason, without resending.
    assert sending.recover_interrupted(db, later) == 1
    db.expire_all()
    assert recipient.status == RecipientStatus.FAILED
    assert "may have been delivered" in recipient.error
    assert db.scalar(select(ActivityLog.id).where(ActivityLog.action == "email.uncertain")) is not None


def test_recovery_leaves_a_send_in_progress_alone(running, db):
    r = db.scalar(select(CampaignRecipient).where(CampaignRecipient.email == "sara@acme.com"))
    r.status = RecipientStatus.SENDING
    r.send_started_at = datetime.now(UTC) - timedelta(seconds=20)
    db.commit()
    assert sending.recover_interrupted(db, datetime.now(UTC)) == 0


def test_uncertain_failure_is_not_retried(running, db):
    smtp = FakeSmtp()
    smtp.error = mailer.SendFailed("connection broke", temporary=False, uncertain=True)
    assert sending.send_next(db, running["mailbox_id"], TUESDAY_11AM_PKT, smtp) == "uncertain"
    assert statuses(db, running["campaign_id"])[0] == ("sara@acme.com", "FAILED")


def test_campaign_does_not_finish_while_an_email_is_mid_send(running, db):
    rows = db.scalars(
        select(CampaignRecipient).where(CampaignRecipient.campaign_id == running["campaign_id"])
    )
    for r in rows:
        r.status = RecipientStatus.SENT if r.email != "lee@blocked.com" else RecipientStatus.SENDING
        r.send_started_at = TUESDAY_11AM_PKT
    db.commit()
    sending._finish_if_done(db, db.get(Campaign, running["campaign_id"]), TUESDAY_11AM_PKT)
    assert db.get(Campaign, running["campaign_id"]).status == CampaignStatus.RUNNING


class _FakeConnection:
    """smtplib.SMTP_SSL stand-in whose DATA step breaks the way a dying network does."""

    def __init__(self, *a, **k):
        pass

    def ehlo_or_helo_if_needed(self):
        pass

    def has_extn(self, name):
        return True

    def login(self, user, password):
        pass

    def mail(self, sender):
        return 250, b"OK"

    def rcpt(self, to):
        return 250, b"OK"

    def data(self, msg):
        raise TimeoutError("timed out")

    def quit(self):
        raise smtplib.SMTPServerDisconnected("gone")

    def close(self):
        pass


def test_mailer_marks_a_broken_handover_as_uncertain(monkeypatch, db, running):
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeConnection)
    mailbox = db.get(Mailbox, running["mailbox_id"])
    msg = mailer.build_message(mailbox, "x@example.com", "Hi", "Hello")
    with pytest.raises(mailer.SendFailed) as caught:
        mailer.send(mailbox, msg)
    assert caught.value.uncertain and not caught.value.temporary


def test_mailer_ignores_a_failed_goodbye_after_success(monkeypatch, db, running):
    class SentThenHungUp(_FakeConnection):
        def data(self, msg):
            return 250, b"OK queued as ABC"

    monkeypatch.setattr(smtplib, "SMTP_SSL", SentThenHungUp)
    mailbox = db.get(Mailbox, running["mailbox_id"])
    sent = mailer.send(mailbox, mailer.build_message(mailbox, "x@example.com", "Hi", "Hello"))
    assert sent.response == "250 OK queued as ABC"
