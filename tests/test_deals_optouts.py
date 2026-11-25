"""Deals integration, step 2: opt-outs shared with Deals, spam complaints, and the opt-out line and footer."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.crypto import encrypt
from app.models import (
    ActivityLog,
    Alert,
    AlertStatus,
    CampaignRecipient,
    DealsOptout,
    DealsOptoutBatch,
    DoNotContact,
    Reply,
    ReplyKind,
)
from app.services import deals, deals_optouts, delivery, privacy, replies, sending
from app.services.app_settings import set_json
from app.services.audit import SYSTEM
from app.services.deals_client import email_hash
from tests.test_campaigns import TUESDAY_11AM_PKT, ready, smtp  # noqa: F401 (shared fixtures)
from tests.test_deals import SECRET, URL, fake  # noqa: F401 (shared fixture)
from tests.test_follow_ups import FakeInbox, _message, _start_and_send_first, campaign  # noqa: F401

T = TUESDAY_11AM_PKT
DAY2 = T + timedelta(days=2)


def _switch(db, *, optouts: bool = True) -> None:
    set_json(
        db,
        deals.SETTINGS_KEY,
        {"url": URL, "secret_encrypted": encrypt(SECRET), "optouts_enabled": optouts},
        None,
        secret=True,
    )
    db.flush()


@pytest.fixture
def optouts_on(db):
    _switch(db)


def _rows(db) -> list[DealsOptout]:
    return list(db.scalars(select(DealsOptout).order_by(DealsOptout.id)))


def _logged(db, action: str) -> list[ActivityLog]:
    return list(db.scalars(select(ActivityLog).where(ActivityLog.action == action)))


def _no_filing(mailbox):
    class Nothing:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __getattr__(self, name):
            return lambda *a, **k: None

    return Nothing()


def _reply(first, body: str, subject: str | None = None, message_id: str = "<r1@acme.com>") -> bytes:
    return _message(
        sender="Sara <sara@acme.com>",
        subject=subject or f"Re: {first['Subject']}",
        body=body,
        headers=f"Message-ID: {message_id}\r\nIn-Reply-To: {first['Message-ID']}\r\n",
    )


# --- Written with the entry ---------------------------------------------------------------------------------


def test_an_opt_out_reply_is_queued_for_deals(client, db, smtp, campaign, optouts_on):
    _start_and_send_first(client, db, smtp, campaign)
    first = smtp.sent[0]
    replies.check_mailbox(db, campaign["mailbox_id"], DAY2, FakeInbox(_reply(first, "Please remove me.")))
    (row,) = _rows(db)
    assert (row.email, row.reason, row.hash) == ("sara@acme.com", "unsubscribed", email_hash("sara@acme.com"))
    assert row.ref == str(db.scalar(select(DoNotContact.id).where(DoNotContact.value == "sara@acme.com")))


def test_a_hard_bounce_and_entries_added_by_hand_are_queued(client, ready, db, optouts_on):
    client.post("/api/campaigns/" + str(ready["campaign"]["id"]) + "/start")
    recipient = db.scalar(select(CampaignRecipient).where(CampaignRecipient.email == "sara@acme.com"))
    delivery.bounced(db, recipient, delivery.DeliveryStatus.HARD_BOUNCE, "bounce_email")
    client.post("/api/dnc", json={"entries": "manual@acme.com\nwhole-domain.com", "reason": "Asked by phone"})
    reasons = {r.email: r.reason for r in _rows(db)}
    # A whole domain can't be shared: Deals takes addresses.
    assert reasons == {"sara@acme.com": "bounced", "manual@acme.com": "manual"}


def test_nothing_is_queued_while_off_and_older_entries_catch_up_when_on(client, admin, db, fake):
    _switch(db, optouts=False)
    client.post("/api/dnc", json={"entries": "early@acme.com"})
    assert _rows(db) == []
    _switch(db)
    assert deals_optouts.push(db, T)["sent"] == 1
    assert [o["email"] for o in fake.optouts] == ["early@acme.com"]
    # Each entry goes once, however often the catch-up runs.
    assert deals_optouts.push(db, T + timedelta(minutes=2))["sent"] == 0


# --- Sending ------------------------------------------------------------------------------------------------


def test_a_batch_goes_out_signed_as_a_test_call_and_is_marked_sent(client, admin, db, optouts_on, fake):
    client.post("/api/dnc", json={"entries": "a@acme.com b@acme.com"})
    assert deals_optouts.push(db, T) == {"ok": True, "sent": 2}
    (call,) = fake.calls("/optouts")
    body = json.loads(call.content)
    assert body["test"] is True and call.headers["Idempotency-Key"].startswith("optouts:")
    assert {i["email"] for i in body["items"]} == {"a@acme.com", "b@acme.com"}
    assert {i["reason"] for i in body["items"]} == {"manual"} and body["items"][0]["at"].endswith("Z")
    assert all(r.sent_at == T for r in _rows(db))
    assert _logged(db, "deals.optouts_pushed")[0].after["items"] == 2


def test_a_retry_sends_the_same_key_and_body_on_schedule(client, admin, db, optouts_on, fake):
    client.post("/api/dnc", json={"entries": "a@acme.com"})
    fake.down = True
    assert deals_optouts.push(db, T)["ok"] is False
    batch = db.scalar(select(DealsOptoutBatch))
    assert batch.attempts == 1 and batch.next_attempt_at == T + timedelta(minutes=1)
    # Not due yet: nothing is sent, and a new entry waits behind the batch.
    client.post("/api/dnc", json={"entries": "later@acme.com"})
    deals_optouts.push(db, T + timedelta(seconds=30))
    assert len(fake.calls("/optouts")) == 1
    for i, minutes in enumerate((5, 15, 60), start=2):
        due = batch.next_attempt_at
        deals_optouts.push(db, due)
        assert batch.attempts == i and batch.next_attempt_at == due + timedelta(minutes=minutes)
    # After the 4th failure a warning alert opens; it closes once it gets through.
    assert db.scalar(select(Alert).where(Alert.kind == "deals_error")).status == AlertStatus.OPEN
    fake.down = False
    assert deals_optouts.push(db, batch.next_attempt_at)["sent"] == 2
    calls = fake.calls("/optouts")
    assert len(calls) == 6  # 4 failures, the same batch getting through, then the later entry's batch
    assert len({c.headers["Idempotency-Key"] for c in calls[:5]}) == 1
    assert len({c.content for c in calls[:5]}) == 1
    assert calls[5].headers["Idempotency-Key"] != calls[0].headers["Idempotency-Key"]
    assert db.scalar(select(Alert).where(Alert.kind == "deals_error")).status == AlertStatus.RESOLVED


def test_refused_data_is_not_retried(client, admin, db, optouts_on, fake):
    client.post("/api/dnc", json={"entries": "a@acme.com"})
    fake.status = 400
    deals_optouts.push(db, T)
    batch = db.scalar(select(DealsOptoutBatch))
    assert batch.gave_up_at == T and "items.0.hash" in batch.last_error
    assert db.scalar(select(Alert).where(Alert.kind == "deals_error")).status == AlertStatus.OPEN
    fake.status = None
    deals_optouts.push(db, T + timedelta(hours=1))
    assert len(fake.calls("/optouts")) == 1
    assert _logged(db, "deals.optouts_failed")[0].after["kind"] == "invalid"


# --- GDPR forget --------------------------------------------------------------------------------------------


def test_a_forget_sends_the_hash_only_and_scrubs_waiting_rows(client, admin, db, fake):
    _switch(db)
    client.post("/api/dnc", json={"entries": "jane@acme.com"})
    fake.down = True
    deals_optouts.push(db, T)  # a batch with the address is now waiting for a retry
    privacy.forget_person(db, "jane@acme.com", SYSTEM, T)
    db.flush()
    assert all(r.email is None for r in _rows(db))
    assert db.scalar(select(DealsOptoutBatch)).superseded is True
    fake.down = False
    deals_optouts.push(db, T + timedelta(minutes=5))
    sent = json.loads(fake.calls("/optouts")[-1].content)["items"]
    assert "jane@acme.com" not in fake.calls("/optouts")[-1].content.decode()
    assert {i["reason"] for i in sent} == {"manual", "forgotten"} and all("email" not in i for i in sent)


def test_a_forget_is_queued_even_while_off(client, admin, db):
    _switch(db, optouts=False)
    privacy.forget_person(db, "gone@acme.com", SYSTEM, T)
    (row,) = _rows(db)
    assert (row.hash, row.email, row.reason) == (email_hash("gone@acme.com"), None, "forgotten")


# --- Spam complaints ----------------------------------------------------------------------------------------


def _complaint(first) -> bytes:
    boundary = "ARF"
    return (
        "From: Yahoo Feedback <feedback@arf.mail.yahoo.com>\r\nTo: ali@fastnexa.com\r\n"
        "Subject: Email Feedback Report\r\nDate: Thu, 12 Nov 2026 10:00:00 +0000\r\n"
        "Message-ID: <arf1@yahoo.com>\r\nMIME-Version: 1.0\r\n"
        f'Content-Type: multipart/report; report-type=feedback-report; boundary="{boundary}"\r\n\r\n'
        f"--{boundary}\r\nContent-Type: text/plain\r\n\r\nThis is an abuse report.\r\n"
        f"--{boundary}\r\nContent-Type: message/feedback-report\r\n\r\nFeedback-Type: abuse\r\n"
        f"--{boundary}\r\nContent-Type: text/rfc822-headers\r\n\r\nMessage-ID: {first['Message-ID']}\r\n"
        f"To: sara@acme.com\r\n--{boundary}--\r\n"
    ).encode()


def test_a_spam_complaint_stops_everything_and_is_shared(client, db, smtp, campaign, optouts_on):
    recipient = _start_and_send_first(client, db, smtp, campaign)
    counts = replies.check_mailbox(
        db, campaign["mailbox_id"], DAY2, FakeInbox(_complaint(smtp.sent[0])), _no_filing
    )
    assert counts["complaint"] == 1 and counts["bounce"] == 0
    assert recipient.follow_up_stop == "complained" and recipient.next_follow_up_at is None
    entry = db.scalar(select(DoNotContact).where(DoNotContact.value == "sara@acme.com"))
    assert entry.reason == deals_optouts.COMPLAINT_REASON
    assert [(r.email, r.reason) for r in _rows(db)] == [("sara@acme.com", "complained")]
    assert len(_logged(db, "email.complained")) == 1
    assert db.scalar(select(Reply)) is None  # a report, not a person's reply


# --- The opt-out line, footer and List-Unsubscribe ----------------------------------------------------------


def test_the_footer_switch(client, ready, db, smtp):
    cid = ready["campaign"]["id"]
    client.put("/api/settings/footer", json={"business_name": "FastNexa", "postal_address": ""})
    r = client.put("/api/settings/email-footer", json={"enabled": True, "optout_line": "Reply stop to stop."})
    assert r.json()["detail"] == "postal_address_required"
    address = {"business_name": "FastNexa", "postal_address": "1 Main St, Lahore"}
    client.put("/api/settings/footer", json=address)
    assert client.put(
        "/api/settings/email-footer", json={"enabled": True, "optout_line": "Reply stop to stop."}
    ).json() == {"enabled": True, "optout_line": "Reply stop to stop."}

    preview = client.post(
        "/api/campaigns/preview",
        json={"subject": "Hi", "body": "Hello", "group_ids": [], "body_format": "TEXT"},
    ).json()
    assert preview["versions"][0]["body"] == "Hello\n\nReply stop to stop.\n\n1 Main St, Lahore\n"

    client.post(f"/api/campaigns/{cid}/start")
    assert sending.send_next(db, ready["mailbox"]["id"], T, smtp) == "sent"
    msg = smtp.sent[0]
    assert msg["List-Unsubscribe"] == "<mailto:ali@fastnexa.com?subject=unsubscribe>"
    assert msg.get_content().rstrip().endswith("Reply stop to stop.\n\n1 Main St, Lahore")
    recipient = db.scalar(select(CampaignRecipient).where(CampaignRecipient.status == "SENT"))
    assert recipient.body_sent.endswith("1 Main St, Lahore\n")  # the exact text, footer included


def test_starting_needs_the_address_once_the_footer_is_on(client, ready, db):
    set_json(db, "email.footer", {"enabled": True, "optout_line": "Reply stop."}, None)
    client.put("/api/settings/footer", json={"business_name": "FastNexa", "postal_address": ""})
    r = client.post(f"/api/campaigns/{ready['campaign']['id']}/start")
    assert r.status_code == 400 and r.json()["detail"] == "postal_address_required"


@pytest.mark.parametrize(("footer", "kind"), [(True, ReplyKind.OPT_OUT), (False, ReplyKind.REPLY)])
def test_an_unsubscribe_subject_is_an_opt_out_when_the_footer_is_on(client, db, smtp, campaign, footer, kind):
    set_json(db, "email.footer", {"enabled": footer, "optout_line": "Reply stop."}, None)
    client.put("/api/settings/footer", json={"business_name": "FastNexa", "postal_address": "1 Main St"})
    _start_and_send_first(client, db, smtp, campaign)
    replies.check_mailbox(
        db, campaign["mailbox_id"], DAY2, FakeInbox(_reply(smtp.sent[0], "", subject="unsubscribe"))
    )
    assert db.scalar(select(Reply)).kind == kind
