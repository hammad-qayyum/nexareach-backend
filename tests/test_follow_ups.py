"""Follow-up emails, reply checking (opt-outs, auto-replies, bounces) and invite emails."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import (
    ActivityLog,
    CampaignRecipient,
    DoNotContact,
    FollowUpEmail,
    FollowUpStatus,
    Mailbox,
)
from app.services import mailer, replies, sending
from tests.test_campaigns import MAILBOX, TUESDAY_11AM_PKT, ready, smtp  # noqa: F401 (shared fixtures)

FOLLOW_UPS = [
    {"delay_days": 3, "body": "Hi {{first_name|there}}, just checking you saw this."},
    {"delay_days": 4, "body": "Last note from me, {{first_name|there}}."},
]


@pytest.fixture
def campaign(client, ready):
    """The ready campaign, with two follow-ups, one lead (Sara) and one mailbox with IMAP."""
    mailbox_id = ready["mailbox"]["id"]
    client.patch(f"/api/mailboxes/{mailbox_id}", json={"imap_host": "mail.fastnexa.com"})
    group_id = ready["import"]["group"]["id"]
    leads = client.get(f"/api/groups/{group_id}/leads").json()["items"]
    campaign_id = ready["campaign"]["id"]
    client.put(
        f"/api/campaigns/{campaign_id}/exclusions",
        json={"lead_ids": [x["id"] for x in leads if x["email"] != "sara@acme.com"], "excluded": True},
    )
    r = client.patch(f"/api/campaigns/{campaign_id}", json={"follow_ups": FOLLOW_UPS})
    assert r.status_code == 200, r.text
    return {"id": campaign_id, "mailbox_id": mailbox_id}


def _start_and_send_first(client, db, smtp, campaign):
    client.post(f"/api/campaigns/{campaign['id']}/start")
    assert sending.send_next(db, campaign["mailbox_id"], TUESDAY_11AM_PKT, smtp) == "sent"
    return db.scalar(select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign["id"]))


def test_follow_ups_go_out_in_the_same_thread_then_the_campaign_finishes(client, db, smtp, campaign):
    recipient = _start_and_send_first(client, db, smtp, campaign)
    first = smtp.sent[0]
    assert recipient.next_follow_up_at == TUESDAY_11AM_PKT + timedelta(days=3)
    # Still running: follow-ups are on their way.
    assert client.get(f"/api/campaigns/{campaign['id']}").json()["status"] == "RUNNING"

    mailbox_id = campaign["mailbox_id"]
    assert sending.send_next(db, mailbox_id, TUESDAY_11AM_PKT + timedelta(days=2), smtp) == "idle"
    day3 = TUESDAY_11AM_PKT + timedelta(days=3)
    assert sending.send_next(db, mailbox_id, day3, smtp) == "follow_up_sent"
    follow = smtp.sent[1]
    assert follow["To"] == "sara@acme.com"
    assert follow["Subject"] == f"Re: {first['Subject']}"
    assert follow["In-Reply-To"] == first["Message-ID"]
    assert follow["References"] == first["Message-ID"]
    assert follow.get_content().strip() == "Hi Sara, just checking you saw this."
    assert recipient.next_follow_up_at == day3 + timedelta(days=4)

    day7 = day3 + timedelta(days=4)
    assert sending.send_next(db, mailbox_id, day7, smtp) == "follow_up_sent"
    last = smtp.sent[2]
    assert last["In-Reply-To"] == follow["Message-ID"]
    assert last["References"] == f"{first['Message-ID']} {follow['Message-ID']}"
    assert recipient.next_follow_up_at is None

    detail = client.get(f"/api/campaigns/{campaign['id']}").json()
    assert detail["status"] == "COMPLETED"
    assert detail["counts"]["follow_ups_sent"] == 2
    item = client.get(f"/api/campaigns/{campaign['id']}/recipients").json()["items"][0]
    assert [f["step"] for f in item["follow_ups"]] == [1, 2]
    assert item["follow_ups"][0]["body_sent"].strip() == "Hi Sara, just checking you saw this."
    assert db.scalar(select(ActivityLog.id).where(ActivityLog.action == "email.follow_up_sent")) is not None


def test_follow_ups_count_towards_the_daily_cap(client, db, smtp, campaign):
    _start_and_send_first(client, db, smtp, campaign)
    client.patch(f"/api/mailboxes/{campaign['mailbox_id']}", json={"daily_cap": 1})
    day3 = TUESDAY_11AM_PKT + timedelta(days=3)
    assert sending.send_next(db, campaign["mailbox_id"], day3, smtp) == "follow_up_sent"
    assert sending.send_next(db, campaign["mailbox_id"], day3 + timedelta(hours=1), smtp) == "cap_reached"


def test_follow_ups_respect_sending_hours(client, db, smtp, campaign):
    client.patch(f"/api/campaigns/{campaign['id']}", json={"start_hour": 9, "end_hour": 17})
    _start_and_send_first(client, db, smtp, campaign)
    evening = TUESDAY_11AM_PKT + timedelta(days=3, hours=8)  # 19:00 in Karachi
    assert sending.send_next(db, campaign["mailbox_id"], evening, smtp) == "idle"
    next_morning = evening + timedelta(hours=14)
    assert sending.send_next(db, campaign["mailbox_id"], next_morning, smtp) == "follow_up_sent"


def test_temporary_error_retries_the_follow_up(client, db, smtp, campaign):
    recipient = _start_and_send_first(client, db, smtp, campaign)
    smtp.error = mailer.SendFailed("451 try later", temporary=True, code=451)
    at = TUESDAY_11AM_PKT + timedelta(days=3)
    assert sending.send_next(db, campaign["mailbox_id"], at, smtp) == "follow_up_retry"
    assert recipient.next_follow_up_at == at + sending.RETRY_DELAY
    smtp.error = None
    assert sending.send_next(db, campaign["mailbox_id"], at + timedelta(minutes=16), smtp) == "follow_up_sent"
    rows = db.scalars(select(FollowUpEmail).where(FollowUpEmail.recipient_id == recipient.id)).all()
    assert [(f.step, f.status, f.attempts) for f in rows] == [(1, FollowUpStatus.SENT, 2)]


def test_permanent_error_stops_follow_ups(client, db, smtp, campaign):
    recipient = _start_and_send_first(client, db, smtp, campaign)
    smtp.error = mailer.SendFailed("550 no such user", temporary=False, code=550)
    assert sending.send_next(db, campaign["mailbox_id"], TUESDAY_11AM_PKT + timedelta(days=3), smtp) == (
        "follow_up_failed"
    )
    assert recipient.follow_up_stop == "failed" and recipient.next_follow_up_at is None
    assert client.get(f"/api/campaigns/{campaign['id']}").json()["status"] == "COMPLETED"


def test_do_not_contact_stops_follow_ups(client, db, smtp, campaign):
    recipient = _start_and_send_first(client, db, smtp, campaign)
    client.post("/api/dnc", json={"entries": "acme.com"})
    assert (
        sending.send_next(db, campaign["mailbox_id"], TUESDAY_11AM_PKT + timedelta(days=3), smtp) == "skipped"
    )
    assert recipient.follow_up_stop == "do_not_contact"
    assert len(smtp.sent) == 1


def test_cancel_stops_follow_ups(client, db, smtp, campaign):
    recipient = _start_and_send_first(client, db, smtp, campaign)
    r = client.post(f"/api/campaigns/{campaign['id']}/cancel").json()
    assert r["status"] == "CANCELLED"
    assert recipient.next_follow_up_at is None and recipient.follow_up_stop == "cancelled"


def test_changing_follow_ups_while_paused_replans_them(client, db, smtp, campaign):
    recipient = _start_and_send_first(client, db, smtp, campaign)
    client.post(f"/api/campaigns/{campaign['id']}/pause")
    r = client.patch(
        f"/api/campaigns/{campaign['id']}",
        json={"follow_ups": [{"delay_days": 5, "body": "Any thoughts?"}]},
    )
    assert r.status_code == 200, r.text
    assert recipient.next_follow_up_at == TUESDAY_11AM_PKT + timedelta(days=5)
    client.patch(f"/api/campaigns/{campaign['id']}", json={"follow_ups": []})
    assert recipient.next_follow_up_at is None and recipient.follow_up_stop is None


def test_follow_up_validation(client, ready):
    campaign_id = ready["campaign"]["id"]
    zero = client.patch(
        f"/api/campaigns/{campaign_id}", json={"follow_ups": [{"delay_days": 0, "body": "x"}]}
    )
    assert zero.status_code == 422
    six = [{"delay_days": 2, "body": "x"}] * 6
    assert client.patch(f"/api/campaigns/{campaign_id}", json={"follow_ups": six}).status_code == 422
    client.patch(f"/api/campaigns/{campaign_id}", json={"follow_ups": [{"delay_days": 2, "body": "  "}]})
    r = client.post(f"/api/campaigns/{campaign_id}/start")
    assert r.status_code == 400 and r.json()["detail"] == "follow_up_empty"


def test_test_send_can_include_the_follow_ups(client, campaign, monkeypatch):
    sent = []

    def fake_send(mailbox, msg):
        sent.append(msg)
        return mailer.Sent(message_id=msg["Message-ID"], response="250 OK")

    monkeypatch.setattr(mailer, "send", fake_send)
    r = client.post(
        f"/api/campaigns/{campaign['id']}/test-send",
        json={"to": "me@example.com", "include_follow_ups": True},
    ).json()
    assert r["ok"] is True and "2 follow-up(s)" in r["detail"]
    assert [m["Subject"].startswith("Re: [Test] ") for m in sent] == [False, True, True]
    assert sent[2]["References"] == f"{sent[0]['Message-ID']} {sent[1]['Message-ID']}"


# --- Reply checking -------------------------------------------------------------------------------------


class FakeInbox:
    """Stands in for mailer.read_inbox."""

    def __init__(self, *raws: bytes):
        self.messages = [mailer.InboxMessage(uid=i + 1, raw=raw) for i, raw in enumerate(raws)]
        self.calls: list[tuple] = []

    def __call__(self, mailbox, after_uid, uidvalidity, since):
        self.calls.append((after_uid, uidvalidity, since))
        new = [m for m in self.messages if after_uid is None or m.uid > after_uid]
        return mailer.InboxBatch(uidvalidity=7, messages=new)


def _message(*, sender: str, subject: str, body: str, headers: str = "") -> bytes:
    return (
        f"From: {sender}\r\nTo: ali@fastnexa.com\r\nSubject: {subject}\r\n"
        f"Date: Thu, 12 Nov 2026 10:00:00 +0000\r\n{headers}\r\n{body}\r\n"
    ).encode()


def test_a_reply_stops_follow_ups(client, db, smtp, campaign):
    recipient = _start_and_send_first(client, db, smtp, campaign)
    first = smtp.sent[0]
    inbox = FakeInbox(
        _message(
            sender="Sara <sara@acme.com>",
            subject=f"Re: {first['Subject']}",
            body="Sounds interesting, can we talk on Friday?\r\n\r\n> Hi Sara,",
            headers=f"In-Reply-To: {first['Message-ID']}\r\n",
        )
    )
    counts = replies.check_mailbox(db, campaign["mailbox_id"], TUESDAY_11AM_PKT + timedelta(days=2), inbox)
    assert counts["reply"] == 1
    assert recipient.replied_at is not None
    assert recipient.next_follow_up_at is None and recipient.follow_up_stop == "replied"
    mailbox = db.get(Mailbox, campaign["mailbox_id"])
    assert (mailbox.inbox_uidvalidity, mailbox.inbox_last_uid) == (7, 1)
    # Nothing left to send: done.
    detail = client.get(f"/api/campaigns/{campaign['id']}").json()
    assert detail["status"] == "COMPLETED" and detail["counts"]["replied"] == 1
    assert sending.send_next(db, campaign["mailbox_id"], TUESDAY_11AM_PKT + timedelta(days=3), smtp) == "idle"

    # The conversation is still watched (for the Replies page), from where the last check stopped;
    # the same message isn't recorded twice.
    replies.check_mailbox(db, campaign["mailbox_id"], TUESDAY_11AM_PKT + timedelta(days=2), inbox)
    assert inbox.calls[-1][:2] == (1, 7)
    assert client.get("/api/replies?state=all").json()["total"] == 1
    replied = client.get(f"/api/campaigns/{campaign['id']}/recipients?replied=true").json()
    assert replied["total"] == 1


def test_a_reply_is_matched_by_address_too(client, db, smtp, campaign):
    recipient = _start_and_send_first(client, db, smtp, campaign)
    inbox = FakeInbox(_message(sender="someone@else.com", subject="Hello", body="Unrelated."))
    replies.check_mailbox(db, campaign["mailbox_id"], TUESDAY_11AM_PKT + timedelta(days=2), inbox)
    assert recipient.replied_at is None
    # The next check only reads newer messages.
    inbox.messages.append(
        mailer.InboxMessage(
            uid=2, raw=_message(sender="sara@acme.com", subject="Your email", body="Call me.")
        )
    )
    replies.check_mailbox(db, campaign["mailbox_id"], TUESDAY_11AM_PKT + timedelta(days=2), inbox)
    assert inbox.calls[-1][:2] == (1, 7)
    assert recipient.replied_at is not None


def test_an_out_of_office_reply_is_ignored(client, db, smtp, campaign):
    recipient = _start_and_send_first(client, db, smtp, campaign)
    inbox = FakeInbox(
        _message(
            sender="sara@acme.com",
            subject="Automatic reply: Quick question",
            body="I'm out of the office until Monday.",
            headers="Auto-Submitted: auto-replied\r\n",
        )
    )
    replies.check_mailbox(db, campaign["mailbox_id"], TUESDAY_11AM_PKT + timedelta(days=2), inbox)
    assert recipient.replied_at is None and recipient.next_follow_up_at is not None


def test_an_opt_out_reply_adds_them_to_do_not_contact(client, db, smtp, campaign):
    recipient = _start_and_send_first(client, db, smtp, campaign)
    inbox = FakeInbox(
        _message(
            sender="sara@acme.com",
            subject="Re: question",
            body="Please remove me from your list.\r\n\r\nOn Tue, Ali wrote:\r\n> Hi Sara, not interested?",
        )
    )
    counts = replies.check_mailbox(db, campaign["mailbox_id"], TUESDAY_11AM_PKT + timedelta(days=2), inbox)
    assert counts["opt_out"] == 1
    assert recipient.follow_up_stop == "opted_out"
    assert db.scalar(select(DoNotContact.reason).where(DoNotContact.value == "sara@acme.com")) is not None


def test_quoted_text_alone_is_not_an_opt_out(client, db, smtp, campaign):
    recipient = _start_and_send_first(client, db, smtp, campaign)
    inbox = FakeInbox(
        _message(
            sender="sara@acme.com",
            subject="Re: question",
            body="Yes, tell me more.\r\n\r\nOn Tue, Ali wrote:\r\n> If you're not interested, just say so.",
        )
    )
    replies.check_mailbox(db, campaign["mailbox_id"], TUESDAY_11AM_PKT + timedelta(days=2), inbox)
    assert recipient.follow_up_stop == "replied"
    assert db.scalar(select(DoNotContact.id).where(DoNotContact.value == "sara@acme.com")) is None


def test_a_bounce_stops_follow_ups(client, db, smtp, campaign):
    recipient = _start_and_send_first(client, db, smtp, campaign)
    first = smtp.sent[0]
    bounce = _message(
        sender="Mail Delivery System <MAILER-DAEMON@mail.fastnexa.com>",
        subject="Undelivered Mail Returned to Sender",
        body="Action: failed\r\nFinal-Recipient: rfc822; sara@acme.com\r\n\r\n"
        f"Message-ID: {first['Message-ID']}",
    )
    counts = replies.check_mailbox(
        db, campaign["mailbox_id"], TUESDAY_11AM_PKT + timedelta(hours=1), FakeInbox(bounce)
    )
    assert counts["bounce"] == 1
    assert recipient.follow_up_stop == "bounced" and recipient.replied_at is None
    assert client.get(f"/api/campaigns/{campaign['id']}").json()["counts"]["bounced"] == 1


def test_mailboxes_without_imap_are_not_checked(client, db, smtp, campaign):
    _start_and_send_first(client, db, smtp, campaign)
    client.patch(f"/api/mailboxes/{campaign['mailbox_id']}", json={"imap_host": ""})
    inbox = FakeInbox()
    replies.check_mailbox(db, campaign["mailbox_id"], TUESDAY_11AM_PKT, inbox)
    assert inbox.calls == []


def test_mark_replied_by_hand(client, db, smtp, campaign):
    recipient = _start_and_send_first(client, db, smtp, campaign)
    r = client.post(f"/api/campaigns/{campaign['id']}/recipients/{recipient.id}/replied")
    assert r.status_code == 200, r.text
    assert r.json()["replied_at"] is not None and r.json()["follow_up_stop"] == "replied"
    assert db.scalar(select(ActivityLog.id).where(ActivityLog.action == "email.marked_replied")) is not None


def test_forget_erases_follow_ups(client, db, smtp, campaign):
    recipient = _start_and_send_first(client, db, smtp, campaign)
    sending.send_next(db, campaign["mailbox_id"], TUESDAY_11AM_PKT + timedelta(days=3), smtp)
    exported = client.post("/api/people/export", json={"email": "sara@acme.com"}).json()
    assert exported["emails"][0]["follow_ups"][0]["follow_up"] == 1
    assert client.post("/api/people/forget", json={"email": "sara@acme.com"}).status_code == 200
    db.expire_all()
    row = db.scalar(select(FollowUpEmail).where(FollowUpEmail.recipient_id == recipient.id))
    assert row.body_sent is None and row.subject_sent is None and row.message_id is None
    assert db.get(CampaignRecipient, recipient.id).next_follow_up_at is None


def test_reports_count_follow_ups(client, db, smtp, campaign):
    _start_and_send_first(client, db, smtp, campaign)
    sending.send_next(db, campaign["mailbox_id"], TUESDAY_11AM_PKT + timedelta(days=3), smtp)
    # Report periods end today, so move the test sends to today.
    now = sending.now()
    for r in db.scalars(select(CampaignRecipient)):
        r.sent_at = now
    for f in db.scalars(select(FollowUpEmail)):
        f.sent_at = now
    db.flush()
    totals = client.get("/api/reports/summary").json()["totals"]
    assert (totals["sent"], totals["follow_ups_sent"], totals["people"]) == (2, 1, 1)


# --- Reading an inbox over IMAP -----------------------------------------------------------------------


class FakeImap:
    def __init__(self, uids: list[int], uidvalidity: int = 7):
        self.uids = uids
        self.uidvalidity = uidvalidity
        self.searches: list[tuple] = []

    def select(self, name, readonly=False):
        assert readonly is True
        return "OK", [str(len(self.uids)).encode()]

    def response(self, code):
        return code, [str(self.uidvalidity).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            self.searches.append(args)
            criteria = args[1:]
            if criteria[0].startswith("UID"):
                low = int(criteria[0].split()[1].split(":")[0])
                # Like real servers, "n:*" includes the newest message even when it is below n.
                found = [u for u in self.uids if u >= low] or self.uids[-1:]
            else:
                found = self.uids
            return "OK", [" ".join(map(str, found)).encode()]
        uid = int(args[0])
        assert args[1] == f"(BODY.PEEK[]<0.{mailer.INBOX_TEXT_BYTES}>)"
        raw = f"From: x{uid}@acme.com\r\nSubject: m{uid}\r\n\r\ntext".encode()
        return "OK", [(f"{uid} (UID {uid} BODY[]<0> {{{len(raw)}}}".encode(), raw), b")"]

    def logout(self):
        pass


def test_read_inbox_continues_after_the_last_message(monkeypatch):
    fake = FakeImap([3, 4, 5])
    monkeypatch.setattr(mailer, "_imap", lambda mailbox: fake)
    mailbox = Mailbox(email="ali@fastnexa.com", imap_host="mail.fastnexa.com")
    since = TUESDAY_11AM_PKT.date()

    first = mailer.read_inbox(mailbox, None, None, since)
    assert [m.uid for m in first.messages] == [3, 4, 5] and first.uidvalidity == 7
    assert first.messages[0].raw.startswith(b"From: x3@acme.com") and first.messages[0].raw.endswith(b"text")
    assert fake.searches[-1][1:] == ("SINCE", "10-Nov-2026")

    assert mailer.read_inbox(mailbox, 5, 7, since).messages == []
    fake.uids.append(6)
    assert [m.uid for m in mailer.read_inbox(mailbox, 5, 7, since).messages] == [6]
    # The server renumbered its inbox: start again from the date.
    assert [m.uid for m in mailer.read_inbox(mailbox, 5, 6, since).messages] == [3, 4, 5, 6]


# --- Invite emails ----------------------------------------------------------------------------------------


def test_invites_are_emailed_from_the_chosen_mailbox(client, admin, monkeypatch):
    sent = []

    def fake_send(mailbox, msg):
        sent.append(msg)
        return mailer.Sent(message_id=msg["Message-ID"], response="250 OK")

    monkeypatch.setattr(mailer, "send", fake_send)
    r = client.post(
        "/api/users/invite", json={"name": "Zara", "email": "zara@fastnexa.com", "role": "MEMBER"}
    )
    assert r.json()["email_sent"] is None and sent == []

    mailbox = client.post("/api/mailboxes", json=MAILBOX).json()
    assert client.put("/api/settings/invites", json={"mailbox_id": mailbox["id"]}).status_code == 200
    omar = {"name": "Omar", "email": "omar@fastnexa.com", "role": "VIEWER"}
    r = client.post("/api/users/invite", json=omar)
    body = r.json()
    assert body["email_sent"] is True
    assert sent[0]["To"] == "omar@fastnexa.com"
    assert body["invite_url"] in sent[0].get_content()

    resent = client.post(f"/api/users/{body['user']['id']}/resend-invite").json()
    assert resent["email_sent"] is True and resent["invite_url"] in sent[1].get_content()


def test_a_failed_invite_email_still_creates_the_invite(client, admin, monkeypatch):
    def broken(mailbox, msg):
        raise mailer.SendFailed("The server name wasn't found.", temporary=True)

    monkeypatch.setattr(mailer, "send", broken)
    mailbox = client.post("/api/mailboxes", json=MAILBOX).json()
    client.put("/api/settings/invites", json={"mailbox_id": mailbox["id"]})
    omar = {"name": "Omar", "email": "omar@fastnexa.com", "role": "VIEWER"}
    r = client.post("/api/users/invite", json=omar)
    assert r.status_code == 201
    assert r.json()["email_sent"] is False and "server name" in r.json()["email_detail"]


def test_only_admins_choose_the_invite_mailbox(client, make_user, login):
    make_user("member@fastnexa.com")
    login("member@fastnexa.com")
    assert client.put("/api/settings/invites", json={"mailbox_id": None}).status_code == 403


def test_follow_ups_can_be_switched_off_and_on(client, db, smtp, campaign):
    recipient = _start_and_send_first(client, db, smtp, campaign)
    client.post(f"/api/campaigns/{campaign['id']}/pause")
    r = client.patch(f"/api/campaigns/{campaign['id']}", json={"follow_ups_enabled": False}).json()
    assert r["follow_ups_enabled"] is False and len(r["follow_ups"]) == 2  # the text is kept
    assert recipient.next_follow_up_at is None and recipient.follow_up_stop is None
    client.post(f"/api/campaigns/{campaign['id']}/resume")
    assert sending.send_next(db, campaign["mailbox_id"], TUESDAY_11AM_PKT + timedelta(days=3), smtp) == "idle"
    assert client.get(f"/api/campaigns/{campaign['id']}").json()["status"] == "COMPLETED"


def test_a_campaign_with_follow_ups_off_sends_only_the_first_email(client, db, smtp, campaign):
    client.patch(f"/api/campaigns/{campaign['id']}", json={"follow_ups_enabled": False})
    recipient = _start_and_send_first(client, db, smtp, campaign)
    assert recipient.next_follow_up_at is None
    assert client.get(f"/api/campaigns/{campaign['id']}").json()["status"] == "COMPLETED"
