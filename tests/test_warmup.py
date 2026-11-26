"""Warm-up: ramp-up limits, the daily plan, sending to helpers, helper actions, filing, the low-rate pause."""

import email
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.crypto import encrypt
from app.models import (
    ActivityLog,
    HelperProvider,
    LandedIn,
    Mailbox,
    MailboxStatus,
    Role,
    SmtpSecurity,
    WarmupHelper,
    WarmupMessage,
    WarmupStatus,
)
from app.services import alerts, mailer, replies, sending, warmup
from app.services.app_settings import set_json
from tests.test_campaigns import MAILBOX, TUESDAY_11AM_PKT, ready, smtp  # noqa: F401 (shared fixtures)

# Tuesday 10 Nov 2026, 11:00 in Karachi = 06:00 UTC. A working day inside the default 8-19 window.
TUESDAY = TUESDAY_11AM_PKT
SATURDAY = TUESDAY + timedelta(days=4)


class Always:
    """A stand-in random generator: every chance happens (random() = 0), lowest choices."""

    def __init__(self, value: float = 0.0):
        self.value = value

    def random(self) -> float:
        return self.value

    def randint(self, a: int, b: int) -> int:
        return a

    def choice(self, seq):
        return seq[0]


# --- Ramp-up ---------------------------------------------------------------------------------------------


def _mailbox(**kw) -> Mailbox:
    return Mailbox(daily_cap=40, ramp_up_enabled=True, ramp_start_limit=5, **kw)


def test_ramp_up_follows_the_plan():
    start = date(2026, 11, 2)  # a Monday
    m = _mailbox(ramp_start_date=start)
    assert warmup.daily_limit(m, start) == 5  # day 1
    assert warmup.daily_limit(m, date(2026, 11, 4)) == 8  # day 3 (2 working days later)
    assert warmup.daily_limit(m, date(2026, 11, 6)) == 11  # day 5
    # The weekend doesn't count: Monday the 9th is only 5 working days in.
    assert warmup.daily_limit(m, date(2026, 11, 8)) == 11
    assert warmup.daily_limit(m, date(2026, 11, 9)) == 11
    assert warmup.daily_limit(m, date(2026, 12, 15)) == 40  # capped
    assert warmup.ramp_day(m, date(2026, 11, 4)) == 3
    assert warmup.ramp_day(m, date(2026, 12, 15)) is None
    m.ramp_up_enabled = False
    assert warmup.daily_limit(m, start) == 40


def test_new_mailboxes_ramp_up_and_existing_ones_do_not(client, admin, db):
    new = client.post("/api/mailboxes", json=MAILBOX).json()
    assert new["ramp_up_enabled"] is True and new["daily_limit_today"] == 5 and new["ramp_day"] == 1
    old = client.post(
        "/api/mailboxes", json={**MAILBOX, "email": "old@fastnexa.com", "ramp_up_enabled": False}
    )
    assert old.json()["daily_limit_today"] == 40
    # Mailboxes from before ramp-up existed have it off (the column's default).
    assert Mailbox.__table__.c.ramp_up_enabled.server_default.arg == "false"
    r = client.patch(
        f"/api/mailboxes/{old.json()['id']}", json={"ramp_up_enabled": True, "ramp_start_limit": 20}
    )
    assert r.json()["daily_limit_today"] == 20 and r.json()["ramp_start_date"] == str(sending.now().date())


def test_campaign_sending_stops_at_the_ramp_up_limit(client, ready, db, smtp):
    mailbox_id = ready["mailbox"]["id"]
    m = db.get(Mailbox, mailbox_id)
    m.ramp_start_limit = 2
    m.ramp_start_date = TUESDAY.date()
    client.post(f"/api/campaigns/{ready['campaign']['id']}/start")
    assert sending.send_next(db, mailbox_id, TUESDAY, smtp) == "sent"
    assert sending.send_next(db, mailbox_id, TUESDAY + timedelta(minutes=11), smtp) == "sent"
    assert sending.send_next(db, mailbox_id, TUESDAY + timedelta(minutes=22), smtp) == "cap_reached"


# --- The plan and the code ---------------------------------------------------------------------------------


def test_daily_plan_is_5_to_10_moments_in_working_hours():
    schedule = warmup.Schedule(**warmup.DEFAULT_SCHEDULE)
    tuesday = TUESDAY.date()
    plan = warmup.plan_for_day(7, tuesday, schedule)
    assert 5 <= len(plan) <= 10
    assert all(8 <= slot.hour < 19 and slot.date() == tuesday for slot in plan)
    assert plan == warmup.plan_for_day(7, tuesday, schedule)  # the same every time
    assert warmup.plan_for_day(7, SATURDAY.date(), schedule) == []


def test_the_hidden_code_is_recognised_and_cannot_be_forged():
    mid = warmup.message_id("ali@fastnexa.com")
    assert warmup.is_warmup(mid) and mid.endswith("@fastnexa.com>")
    assert warmup.is_warmup(f"<a@b.com> {mid}")
    assert not warmup.is_warmup("<nxw0123456789ab01234567@example.com>")
    assert not warmup.is_warmup(None)


# --- Sending ---------------------------------------------------------------------------------------------


def _helper(db, email_address="helper1@gmail.com", provider=HelperProvider.GMAIL, **kw) -> WarmupHelper:
    helper = WarmupHelper(
        email=email_address,
        display_name="Hamza Ali",
        provider=provider,
        smtp_host="smtp.gmail.com",
        smtp_port=465,
        smtp_security=SmtpSecurity.SSL,
        smtp_username=email_address,
        smtp_password_encrypted=encrypt("app-password"),
        imap_host="imap.gmail.com",
        imap_port=993,
        status=kw.pop("status", MailboxStatus.ACTIVE),
        **kw,
    )
    db.add(helper)
    db.flush()
    return helper


@pytest.fixture
def warm(client, admin, db):
    """A mailbox with warm-up on (no ramp-up, no campaign) and one Gmail helper."""
    created = client.post(
        "/api/mailboxes", json={**MAILBOX, "ramp_up_enabled": False, "warmup_enabled": True}
    )
    mailbox = db.get(Mailbox, created.json()["id"])
    return {"mailbox": mailbox, "helper": _helper(db)}


def _first_slot(mailbox_id: int, day: datetime) -> datetime:
    return warmup.plan_for_day(mailbox_id, day.date(), warmup.Schedule(**warmup.DEFAULT_SCHEDULE))[0]


def test_warmup_emails_go_to_a_helper_with_the_hidden_code(db, smtp, warm):
    mailbox, helper = warm["mailbox"], warm["helper"]
    slot = _first_slot(mailbox.id, TUESDAY)
    assert sending.send_next(db, mailbox.id, slot - timedelta(minutes=1), smtp) == "idle"
    assert sending.send_next(db, mailbox.id, slot, smtp) == "warmup_sent"
    msg = smtp.sent[0]
    assert msg["To"] == helper.email and warmup.is_warmup(msg["Message-ID"])
    assert "nxw" not in msg.get_content() and "Hamza" in msg.get_content()
    record = db.scalar(select(WarmupMessage))
    assert record.status == WarmupStatus.SENT and record.provider == HelperProvider.GMAIL
    assert timedelta(minutes=10) <= record.act_after - slot <= timedelta(minutes=40)
    # One per slot: the next goes at the next slot, after the mailbox's gap.
    assert sending.send_next(db, mailbox.id, slot + timedelta(minutes=11), smtp) == "idle"
    # Warm-up counts towards the daily limit.
    assert sending.sent_today(db, mailbox.id, slot) == 1


def test_no_warmup_outside_hours_without_helpers_or_when_off(db, smtp, warm):
    mailbox = warm["mailbox"]
    assert sending.send_next(db, mailbox.id, SATURDAY, smtp) == "idle"
    evening = TUESDAY + timedelta(hours=9)  # 20:00 in Karachi: past the window, even with slots unsent
    assert sending.send_next(db, mailbox.id, evening, smtp) == "idle"
    warm["helper"].status = MailboxStatus.PAUSED
    assert sending.send_next(db, mailbox.id, _first_slot(mailbox.id, TUESDAY), smtp) == "idle"
    warm["helper"].status = MailboxStatus.ACTIVE
    mailbox.warmup_enabled = False
    assert sending.send_next(db, mailbox.id, _first_slot(mailbox.id, TUESDAY), smtp) == "idle"
    assert smtp.sent == []


# --- Helper actions ---------------------------------------------------------------------------------------


class FakeInbox:
    """Stands in for mailer.Inbox: folders of {uid: raw message}."""

    def __init__(self, folders: dict[str, dict[str, bytes]], spam: str | None = "[Gmail]/Spam", fail=None):
        self.folders = folders
        self._spam = spam
        self.fail = fail
        self.flagged: list[tuple[str, str]] = []
        self.next_uid = 100

    def __call__(self, account):
        if self.fail:
            raise mailer.InboxFailed(self.fail)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    is_gmail = True

    def spam_folder(self):
        return self._spam

    def ensure_folder(self, name):
        self.folders.setdefault(name, {})
        return name

    def search(self, folder, text, headers=("Message-ID",)):
        found = []
        for uid, raw in self.folders.get(folder, {}).items():
            msg = email.message_from_bytes(raw)
            if any(text in (msg.get(h) or "") for h in headers):
                found.append(uid)
        return found

    def header_text(self, folder, uid):
        msg = email.message_from_bytes(self.folders[folder][uid])
        return " ".join(f"{h}: {msg.get(h, '')}" for h in ("Message-ID", "In-Reply-To", "References"))

    def move(self, folder, uid, destination):
        raw = self.folders[folder].pop(uid)
        self.next_uid += 1
        self.folders.setdefault(destination, {})[str(self.next_uid)] = raw

    def mark_engaged(self, folder, uid):
        self.flagged.append((folder, uid))


def _sent_warmup(db, smtp, warm) -> tuple[WarmupMessage, bytes]:
    slot = _first_slot(warm["mailbox"].id, TUESDAY)
    assert sending.send_next(db, warm["mailbox"].id, slot, smtp) == "warmup_sent"
    return db.scalar(select(WarmupMessage)), smtp.sent[-1].as_bytes()


def test_a_helper_rescues_from_spam_flags_replies_and_files(db, smtp, warm):
    record, raw = _sent_warmup(db, smtp, warm)
    inbox = FakeInbox({"INBOX": {}, "[Gmail]/Spam": {"7": raw}})
    replies_sent = []

    def deliver(account, msg):
        replies_sent.append((account, msg))
        return mailer.Sent(message_id=msg["Message-ID"], response="250 OK")

    later = record.act_after + timedelta(minutes=1)
    counts = warmup.act_for_helper(db, warm["helper"].id, later, inbox, deliver, Always(0.0))
    assert counts["spam"] == 1
    assert record.landed_in == LandedIn.SPAM and record.rescued and record.acted_at == later
    assert inbox.folders["[Gmail]/Spam"] == {}
    assert inbox.flagged and record.moved_to_folder and inbox.folders[warmup.HELPER_FOLDER]
    # The helper answered from its own account, in the same thread, with its own hidden code.
    account, reply = replies_sent[0]
    assert account.email == warm["helper"].email and reply["To"] == "ali@fastnexa.com"
    assert reply["In-Reply-To"] == record.message_id and warmup.is_warmup(reply["Message-ID"])
    assert reply["Subject"] == f"Re: {record.subject}" and record.replied_at == later


def test_inbox_found_waiting_and_missing(db, smtp, warm):
    record, raw = _sent_warmup(db, smtp, warm)
    # Too early: nothing happens yet.
    early = warmup.act_for_helper(
        db, warm["helper"].id, record.act_after - timedelta(minutes=1), FakeInbox({})
    )
    assert not any(early.values())
    # Not arrived yet: try again later.
    empty = FakeInbox({"INBOX": {}, "[Gmail]/Spam": {}})
    later = record.act_after + timedelta(minutes=1)
    assert warmup.act_for_helper(db, warm["helper"].id, later, empty, rng=Always(0.99))["waiting"] == 1
    assert record.acted_at is None
    # Hours later and still nowhere: missing.
    assert warmup.act_for_helper(db, warm["helper"].id, later + timedelta(hours=4), empty)["missing"] == 1
    assert record.landed_in == LandedIn.MISSING


def test_inbox_landing_without_reply_or_folder(db, smtp, warm):
    record, raw = _sent_warmup(db, smtp, warm)
    inbox = FakeInbox({"INBOX": {"3": raw}})
    later = record.act_after + timedelta(minutes=1)
    assert warmup.act_for_helper(db, warm["helper"].id, later, inbox, rng=Always(0.99))["inbox"] == 1
    assert record.landed_in == LandedIn.INBOX and not record.rescued and record.replied_at is None
    assert inbox.flagged == [("INBOX", "3")] and not record.moved_to_folder


def test_a_helper_that_cannot_sign_in_is_marked_and_logged(db, smtp, warm):
    record, _ = _sent_warmup(db, smtp, warm)
    broken = FakeInbox({}, fail="The IMAP server rejected the username or password.")
    warmup.act_for_helper(db, warm["helper"].id, record.act_after + timedelta(minutes=1), broken)
    assert warm["helper"].status == MailboxStatus.ERROR and "rejected" in warm["helper"].last_error
    assert db.scalar(select(ActivityLog.id).where(ActivityLog.action == "warmup.helper_failed")) is not None


# --- Our side: filing and reply checking ---------------------------------------------------------------


def _helper_reply(record: WarmupMessage) -> bytes:
    return (
        f"From: hamza@gmail.com\r\nTo: ali@fastnexa.com\r\nSubject: Re: {record.subject}\r\n"
        f"Message-ID: {warmup.message_id('hamza@gmail.com')}\r\n"
        f"In-Reply-To: {record.message_id}\r\n\r\nThanks!\r\n"
    ).encode()


def test_helper_answers_are_filed_into_the_warmup_folder(db, smtp, warm):
    record, _ = _sent_warmup(db, smtp, warm)
    warm["mailbox"].imap_host = "mail.fastnexa.com"
    forged = b"From: x@y.com\r\nMessage-ID: <nxw0123456789ab01234567@y.com>\r\n\r\nhi\r\n"
    real = b"From: lead@acme.com\r\nMessage-ID: <real@acme.com>\r\n\r\nhello\r\n"
    inbox = FakeInbox({"INBOX": {"1": _helper_reply(record), "2": forged, "3": real}})
    assert warmup.file_mailbox(db, warm["mailbox"].id, inbox) == 1
    assert list(inbox.folders["INBOX"]) == ["2", "3"] and len(inbox.folders["Warmup"]) == 1
    assert warm["mailbox"].warmup_folder == "Warmup"


def test_filing_only_opens_inboxes_that_warm_up_and_can_log_in(client, ready, db):
    m = db.get(Mailbox, ready["mailbox"]["id"])
    m.imap_host, m.warmup_enabled, m.status = "mail.fastnexa.com", False, MailboxStatus.ACTIVE
    now = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
    # Warm-up never used: no IMAP logins every 5 minutes.
    assert warmup.filing_mailbox_ids(db, now) == []
    m.warmup_enabled = True
    assert warmup.filing_mailbox_ids(db, now) == [m.id]
    # Switched off, but it sent warm-up mail 3 days ago: answers may still come, keep filing for a week.
    m.warmup_enabled = False
    helper = _helper(db)
    _results(db, m.id, helper.id, now - timedelta(days=3), inbox=1, spam=0)
    assert warmup.filing_mailbox_ids(db, now) == [m.id]
    assert warmup.filing_mailbox_ids(db, now + timedelta(days=5)) == []
    # A failing login is never retried by filing (cPanel would block the server's IP).
    m.warmup_enabled, m.status = True, MailboxStatus.ERROR
    assert warmup.filing_mailbox_ids(db, now) == []


def test_reply_checking_ignores_warmup_mail(db, smtp, warm):
    record, _ = _sent_warmup(db, smtp, warm)
    watched = replies.Watched(by_email={"hamza@gmail.com": [1]}, sent_at={1: TUESDAY})
    msg = email.message_from_bytes(_helper_reply(record))
    assert replies.classify(msg, watched, TUESDAY + timedelta(days=1)) is None


# --- Inbox rate, the low-rate pause, and resuming --------------------------------------------------------


def _results(db, mailbox_id, helper_id, day: datetime, inbox: int, spam: int):
    for i in range(inbox + spam):
        db.add(
            WarmupMessage(
                mailbox_id=mailbox_id,
                helper_id=helper_id,
                provider=HelperProvider.GMAIL,
                status=WarmupStatus.SENT,
                message_id=warmup.message_id("ali@fastnexa.com"),
                subject="Hi",
                sent_at=day + timedelta(minutes=i),
                created_at=day + timedelta(minutes=i),
                landed_in=LandedIn.INBOX if i < inbox else LandedIn.SPAM,
            )
        )
    db.flush()


def test_three_poor_days_pause_campaign_sending_but_not_warmup(client, ready, db, smtp):
    set_json(db, alerts.THRESHOLDS_KEY, {"auto_pause": True}, None)
    mailbox_id = ready["mailbox"]["id"]
    m = db.get(Mailbox, mailbox_id)
    m.ramp_up_enabled = False
    m.warmup_enabled = True
    helper = _helper(db)
    now = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)  # Friday
    for days_ago in (3, 2, 1):
        _results(db, mailbox_id, helper.id, now - timedelta(days=days_ago), inbox=1, spam=3)
    assert warmup.check_health(db, now) == [mailbox_id]
    assert m.campaigns_paused_reason == "warmup_low"
    assert (
        db.scalar(select(ActivityLog.id).where(ActivityLog.action == "mailbox.campaigns_paused")) is not None
    )

    client.post(f"/api/campaigns/{ready['campaign']['id']}/start")
    tuesday = TUESDAY + timedelta(days=7)
    assert (
        sending.send_next(db, mailbox_id, tuesday + timedelta(hours=8), smtp) == "idle"
    )  # 19:00, no warm-up
    assert sending.send_next(db, mailbox_id, _first_slot(mailbox_id, tuesday), smtp) == "warmup_sent"

    r = client.post(f"/api/mailboxes/{mailbox_id}/resume-campaigns").json()
    assert r["campaigns_paused_at"] is None
    assert sending.send_next(db, mailbox_id, tuesday + timedelta(hours=8), smtp) == "sent"
    # The same bad days don't pause it again right after someone resumed it.
    db.expire_all()
    assert warmup.check_health(db, now) == []


def test_two_poor_days_or_a_good_one_do_not_pause(db, ready):
    mailbox_id = ready["mailbox"]["id"]
    db.get(Mailbox, mailbox_id).warmup_enabled = True
    helper = _helper(db)
    now = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
    _results(db, mailbox_id, helper.id, now - timedelta(days=3), inbox=4, spam=0)
    _results(db, mailbox_id, helper.id, now - timedelta(days=2), inbox=0, spam=4)
    _results(db, mailbox_id, helper.id, now - timedelta(days=1), inbox=0, spam=4)
    assert warmup.check_health(db, now) == []


def test_overview_shows_rates_per_provider(client, db, warm):
    _results(db, warm["mailbox"].id, warm["helper"].id, sending.now() - timedelta(hours=2), inbox=3, spam=1)
    o = client.get("/api/warmup").json()
    mb = next(m for m in o["mailboxes"] if m["id"] == warm["mailbox"].id)
    assert mb["rate"] == 75 and mb["rates"][0]["provider"] == "GMAIL" and mb["rates"][0]["spam"] == 1
    assert len(mb["trend"]) == 14 and o["active_helpers"] == 1
    assert o["schedule"] == {
        "timezone": "Asia/Karachi",
        "days": [0, 1, 2, 3, 4],
        "start_hour": 8,
        "end_hour": 19,
    }


# --- Helper accounts API ---------------------------------------------------------------------------------

HELPER = {
    "email": "Hamza.Helper@gmail.com",
    "display_name": "Hamza Ali",
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 465,
    "smtp_security": "SSL",
    "password": "abcd efgh ijkl mnop",
    "imap_host": "imap.gmail.com",
    "imap_port": 993,
}


def test_adding_testing_pausing_and_removing_a_helper(client, admin, monkeypatch):
    from app.api import warmup as warmup_api

    monkeypatch.setattr(
        warmup_api, "check_helper", lambda h: warmup_api.CheckOut(ok=True, detail="Connected: both work.")
    )
    r = client.post("/api/warmup/helpers", json=HELPER)
    assert r.status_code == 201, r.text
    h = r.json()
    assert (h["email"], h["provider"], h["status"], h["smtp_username"]) == (
        "hamza.helper@gmail.com",
        "GMAIL",
        "ACTIVE",
        "hamza.helper@gmail.com",
    )
    assert "password" not in h
    assert client.post("/api/warmup/helpers", json=HELPER).status_code == 409
    paused = client.patch(f"/api/warmup/helpers/{h['id']}", json={"status": "PAUSED"}).json()
    assert paused["status"] == "PAUSED"

    monkeypatch.setattr(
        warmup_api, "check_helper", lambda h: warmup_api.CheckOut(ok=False, detail="IMAP: refused")
    )
    again = client.patch(f"/api/warmup/helpers/{h['id']}", json={"status": "ACTIVE"}).json()
    assert again["status"] == "ERROR" and again["connection"]["detail"] == "IMAP: refused"
    assert client.delete(f"/api/warmup/helpers/{h['id']}").status_code == 204
    assert client.get("/api/warmup/helpers").json() == []


def test_guessing_the_provider(monkeypatch):
    from app.api.warmup import guess_provider

    assert guess_provider("a@gmail.com") == HelperProvider.GMAIL
    assert guess_provider("a@hotmail.com") == HelperProvider.OUTLOOK
    assert guess_provider("a@yahoo.co.uk") == HelperProvider.YAHOO
    assert guess_provider("a@mycompany-google.com") == HelperProvider.GMAIL  # Google Workspace (fake DNS)
    assert guess_provider("a@acme.com") == HelperProvider.OTHER


def test_helper_rules(client, make_user, login, monkeypatch):
    from app.core.config import get_settings

    member = make_user("member@fastnexa.com", role=Role.MEMBER)
    login(member.email)
    monkeypatch.setattr(get_settings(), "app_env", "development")
    r = client.post("/api/warmup/helpers", json=HELPER)
    assert r.status_code == 400 and r.json()["detail"] == "dev_real_helper"
    make_user("viewer@fastnexa.com", role=Role.VIEWER)
    login("viewer@fastnexa.com")
    assert client.get("/api/warmup").status_code == 200
    assert client.post("/api/warmup/helpers", json=HELPER).status_code == 403


def test_warmup_hours_can_be_changed(client, admin):
    body = {"timezone": "Asia/Dubai", "days": [0, 1, 2, 3, 4, 5], "start_hour": 9, "end_hour": 18}
    assert client.put("/api/warmup/schedule", json=body).json() == body
    assert client.get("/api/warmup/schedule").json() == body
    bad = {**body, "end_hour": 9}
    assert client.put("/api/warmup/schedule", json=bad).status_code == 400


def test_warmup_leaves_room_for_campaigns_during_ramp_up(db, smtp, warm):
    """Day 1 of ramp-up allows 5 emails: warm-up takes at most 2, so campaigns still get 3."""
    mailbox = warm["mailbox"]
    mailbox.ramp_up_enabled = True
    mailbox.ramp_start_date = TUESDAY.date()
    late = TUESDAY + timedelta(hours=7)  # 18:00 in Karachi: every slot of the day is due
    sent = []
    for i in range(6):
        mailbox.next_send_at = None
        sent.append(sending.send_next(db, mailbox.id, late + timedelta(seconds=i), smtp))
    assert sent.count("warmup_sent") == 2
