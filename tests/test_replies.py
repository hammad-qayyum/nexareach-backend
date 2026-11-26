"""The Replies page: stored replies, the conversation, answering, inbox states, privacy, simulate-reply."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import ActivityLog, CampaignRecipient, Reply, Role
from app.services import mailer, replies, sending
from tests.test_campaigns import TUESDAY_11AM_PKT, ready, smtp  # noqa: F401 (shared fixtures)
from tests.test_follow_ups import FakeInbox, _message, _start_and_send_first, campaign  # noqa: F401

DAY2 = TUESDAY_11AM_PKT + timedelta(days=2)


def _counts(client):
    c = client.get("/api/replies/count").json()
    return c["unread"], c["open"]


def _reply_from_sara(first, body="Sounds good. Can you send prices?", message_id="<r1@acme.com>"):
    return _message(
        sender="Sara Ahmed <sara@acme.com>",
        subject=f"Re: {first['Subject']}",
        body=f"{body}\r\n\r\nOn Tue, Ali wrote:\r\n> Hi Sara,",
        headers=f"Message-ID: {message_id}\r\nIn-Reply-To: {first['Message-ID']}\r\n"
        f"References: {first['Message-ID']}\r\n",
    )


@pytest.fixture
def replied(client, db, smtp, campaign):
    """Sara got the first email, then replied."""
    _start_and_send_first(client, db, smtp, campaign)
    first = smtp.sent[0]
    inbox = FakeInbox(_reply_from_sara(first))
    replies.check_mailbox(db, campaign["mailbox_id"], DAY2, inbox)
    reply = db.scalar(select(Reply))
    return {"first": first, "inbox": inbox, "reply": reply, **campaign}


def test_a_reply_is_stored_with_the_conversation(client, replied):
    listed = client.get("/api/replies").json()
    assert listed["total"] == 1
    item = listed["items"][0]
    assert (item["from_email"], item["from_name"], item["kind"]) == ("sara@acme.com", "Sara Ahmed", "REPLY")
    assert item["snippet"] == "Sounds good. Can you send prices?"
    assert item["campaign"]["name"] == "Intro" and item["read_at"] is None and item["answered"] is False
    assert _counts(client) == (1, 1)

    detail = client.get(f"/api/replies/{item['id']}").json()
    assert detail["text"] == "Sounds good. Can you send prices?"
    assert "> Hi Sara," in detail["full_text"]
    assert [t["kind"] for t in detail["thread"]] == ["email", "reply"]
    assert detail["thread"][0]["subject"] == replied["first"]["Subject"]
    recipient = client.get(f"/api/campaigns/{replied['id']}/recipients").json()["items"][0]
    assert recipient["last_reply_id"] == item["id"]


def test_read_done_and_reopen(client, replied):
    rid = replied["reply"].id
    assert client.post(f"/api/replies/{rid}/read", json={"read": True}).json()["read_at"] is not None
    assert _counts(client) == (0, 1)
    assert client.post(f"/api/replies/{rid}/read", json={"read": False}).json()["read_at"] is None
    done = client.post(f"/api/replies/{rid}/done", json={"done": True}).json()
    assert done["done_at"] is not None and done["read_at"] is not None
    assert client.get("/api/replies").json()["total"] == 0
    assert client.get("/api/replies?state=done").json()["total"] == 1
    assert client.post(f"/api/replies/{rid}/done", json={"done": False}).json()["done_at"] is None
    assert client.get("/api/replies?q=prices").json()["total"] == 1
    assert client.get("/api/replies?q=nothing-like-this").json()["total"] == 0


def test_answering_sends_in_the_same_thread(client, db, replied, monkeypatch):
    sent = []

    def fake_send(mailbox, msg):
        sent.append(msg)
        return mailer.Sent(message_id=msg["Message-ID"], response="250 OK")

    monkeypatch.setattr(mailer, "send", fake_send)
    rid = replied["reply"].id
    r = client.post(f"/api/replies/{rid}/answer", json={"body": "Hi Sara, prices attached below.\n\nAli"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["reply"]["answered"] is True and body["reply"]["done_at"] is not None
    msg = sent[0]
    assert (msg["To"], msg["From"]) == ("sara@acme.com", "Ali Khan <ali@fastnexa.com>")
    assert msg["Subject"] == f"Re: {replied['first']['Subject']}"
    assert msg["In-Reply-To"] == "<r1@acme.com>"
    assert msg["References"] == f"{replied['first']['Message-ID']} <r1@acme.com>"
    assert [t["kind"] for t in body["reply"]["thread"]] == ["email", "reply", "answer"]
    assert db.scalar(select(ActivityLog.id).where(ActivityLog.action == "reply.answered")) is not None

    # Their answer to our answer is part of the same conversation, and shows as a new unread reply.
    again = _message(
        sender="sara@acme.com",
        subject="Re: prices",
        body="Thanks, booked a call.",
        headers=f"Message-ID: <r2@acme.com>\r\nIn-Reply-To: {msg['Message-ID']}\r\n",
    )
    replied["inbox"].messages.append(mailer.InboxMessage(uid=2, raw=again))
    replies.check_mailbox(db, replied["mailbox_id"], DAY2 + timedelta(hours=2), replied["inbox"])
    assert _counts(client) == (1, 1)
    newest = client.get("/api/replies").json()["items"][0]
    thread = client.get(f"/api/replies/{newest['id']}").json()["thread"]
    assert sorted(t["kind"] for t in thread) == ["answer", "email", "reply", "reply"]


def test_a_failed_answer_is_recorded(client, replied, monkeypatch):
    def broken(mailbox, msg):
        raise mailer.SendFailed("The server name wasn't found.", temporary=True)

    monkeypatch.setattr(mailer, "send", broken)
    body = client.post(f"/api/replies/{replied['reply'].id}/answer", json={"body": "Hi"}).json()
    assert body["ok"] is False and "server name" in body["detail"]
    assert body["reply"]["done_at"] is None
    assert body["reply"]["thread"][-1]["status"] == "FAILED"


def test_the_same_message_is_stored_once(client, db, replied):
    # The server renumbered its inbox, so everything is read again.
    db.get(sending.Mailbox, replied["mailbox_id"]).inbox_uidvalidity = 1
    replies.check_mailbox(db, replied["mailbox_id"], DAY2, replied["inbox"])
    assert client.get("/api/replies?state=all").json()["total"] == 1


def test_an_opt_out_reply_is_marked(client, db, smtp, campaign):
    _start_and_send_first(client, db, smtp, campaign)
    inbox = FakeInbox(_reply_from_sara(smtp.sent[0], body="Please remove me from your list."))
    replies.check_mailbox(db, campaign["mailbox_id"], DAY2, inbox)
    assert client.get("/api/replies").json()["items"][0]["kind"] == "OPT_OUT"


def _bottom_posted(first, sent_text, answer, *, marked):
    """A reply written under the quoted email (Roundcube, some phones)."""
    lines = sent_text.splitlines()
    quoted = "\r\n".join(("> " if marked else "  ") + line for line in lines)
    return _message(
        sender="Sara Ahmed <sara@acme.com>",
        subject=f"Re: {first['Subject']}",
        body=f"On 2026-11-26 03:49, Ali wrote:\r\n{quoted}\r\n\r\n{answer}",
        headers=f"Message-ID: <bottom@acme.com>\r\nIn-Reply-To: {first['Message-ID']}\r\n",
    )


@pytest.mark.parametrize("marked", [True, False])
def test_an_answer_below_the_quote_is_found(client, db, smtp, campaign, marked):
    _start_and_send_first(client, db, smtp, campaign)
    sent_text = db.scalar(select(CampaignRecipient.body_sent))
    answer = "Yes, I am interested. Can we talk on Thursday?"
    inbox = FakeInbox(_bottom_posted(smtp.sent[0], sent_text, answer, marked=marked))
    replies.check_mailbox(db, campaign["mailbox_id"], DAY2, inbox)
    item = client.get("/api/replies").json()["items"][0]
    assert item["kind"] == "REPLY" and item["snippet"] == answer


def test_unsubscribe_below_the_quote_is_an_opt_out(client, db, smtp, campaign):
    _start_and_send_first(client, db, smtp, campaign)
    sent_text = db.scalar(select(CampaignRecipient.body_sent))
    answer = "Please remove me from your list."
    inbox = FakeInbox(_bottom_posted(smtp.sent[0], sent_text, answer, marked=True))
    replies.check_mailbox(db, campaign["mailbox_id"], DAY2, inbox)
    assert client.get("/api/replies").json()["items"][0]["kind"] == "OPT_OUT"


def test_viewers_read_but_cannot_answer(client, replied, make_user, login):
    make_user("viewer@fastnexa.com", role=Role.VIEWER)
    login("viewer@fastnexa.com")
    rid = replied["reply"].id
    assert client.get(f"/api/replies/{rid}").status_code == 200
    assert client.post(f"/api/replies/{rid}/answer", json={"body": "Hi"}).status_code == 403
    assert client.post(f"/api/replies/{rid}/done", json={"done": True}).status_code == 403


def test_forget_deletes_replies_and_export_includes_them(client, db, replied):
    exported = client.post("/api/people/export", json={"email": "sara@acme.com"}).json()
    assert exported["replies"][0]["subject"].startswith("Re: ")
    forgot = client.post("/api/people/forget", json={"email": "sara@acme.com"}).json()
    assert forgot["replies"] == 1
    assert db.scalar(select(Reply.id)) is None


def test_simulate_reply_in_development(client, db, smtp, campaign, monkeypatch):
    from app.cli import DevOnlyError, apply_simulated_reply
    from app.core.config import get_settings

    _start_and_send_first(client, db, smtp, campaign)
    with pytest.raises(DevOnlyError):
        apply_simulated_reply(db, "sara@acme.com", "Hello")
    monkeypatch.setattr(get_settings(), "app_env", "development")
    assert "Recorded a reply" in apply_simulated_reply(db, "Sara@Acme.com", "Interested, call me.")
    item = client.get("/api/replies").json()["items"][0]
    assert item["snippet"] == "Interested, call me."
    with pytest.raises(DevOnlyError):
        apply_simulated_reply(db, "nobody@acme.com", "Hi")


def test_counts_for_every_tab_and_inbox_status(client, replied):
    c = client.get("/api/replies/count").json()
    assert (c["unread"], c["open"], c["done"], c["all"]) == (1, 1, 0, 1)
    assert c["checked_mailboxes"] == 1 and c["checked_at"] is not None


def test_check_now_reads_the_inboxes(client, replied, monkeypatch):
    calls = []
    monkeypatch.setattr(replies, "check_all", lambda: calls.append(1))
    r = client.post("/api/replies/check").json()
    assert r == {"started": True, "checked_mailboxes": 1} and calls == [1]


def test_reply_details_for_the_side_panel(client, replied):
    d = client.get(f"/api/replies/{replied['reply'].id}").json()["details"]
    assert d["fields"]["first_name"] == "Sara" and d["fields"]["industry"] == "Logistics"
    assert d["sender_name"] == d["fields"]["sender_name"] == "Ali Khan"
    assert d["campaign_status"] == "COMPLETED" and d["follow_up_stop"] == "replied"
    assert d["first_email_at"] is not None


def test_quick_replies(client, admin, make_user, login):
    # The migration starts everyone with a few.
    starters = client.get("/api/quick-replies").json()
    assert [q["title"] for q in starters][:2] == ["Happy to call", "Send details"]
    made = client.post(
        "/api/quick-replies", json={"title": "Price list", "body": "Hi {{first_name}}, prices attached."}
    )
    assert made.status_code == 201
    qid = made.json()["id"]
    assert client.get("/api/quick-replies").json()[-1]["title"] == "Price list"
    edited = client.put(
        f"/api/quick-replies/{qid}", json={"title": "Prices", "body": "Hi, prices below."}
    ).json()
    assert edited["title"] == "Prices"
    assert client.post("/api/quick-replies", json={"title": " ", "body": "x"}).status_code == 422
    assert client.delete(f"/api/quick-replies/{qid}").status_code == 204
    assert client.put(f"/api/quick-replies/{qid}", json={"title": "x", "body": "y"}).status_code == 404

    make_user("viewer@fastnexa.com", role=Role.VIEWER)
    login("viewer@fastnexa.com")
    assert client.get("/api/quick-replies").status_code == 200
    assert client.post("/api/quick-replies", json={"title": "x", "body": "y"}).status_code == 403


def test_gmail_quote_line_wrapped_over_two_lines_is_removed():
    # Exactly what Gmail sent in the staging test (with the narrow no-break space before PM).
    text = (
        "please tell me when we can have a meeting\n\n"
        "On Wed, Nov 18, 2026 at 3:15\u202fPM zamna razzaq <zamnarazzaq@fastnexa.com>\n"
        "wrote:\n\n> Hi Layla,\n> We have buyers.\n"
    )
    assert replies._strip_quote(text) == "please tell me when we can have a meeting"
    one_line = "Sounds good.\n\nOn Tue, Ali <ali@fastnexa.com> wrote:\n> Hi"
    assert replies._strip_quote(one_line) == "Sounds good."
