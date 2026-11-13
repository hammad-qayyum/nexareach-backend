"""Milestones 4, 6 and 7: timezones, providers, Google Sheets, group editing, rich text, lead-local
sending windows, leaving leads out, and the Sent-folder copy."""

import imaplib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import CampaignRecipient, Lead, Mailbox, RecipientStatus
from app.services import mailer, sending, sheets
from tests.test_campaigns import MAILBOX, FakeSmtp

FOOTER = {"business_name": "FastNexa", "postal_address": "1 Main St, Lahore"}
ROWS = [
    {"email": "sara@acme-google.com", "first_name": "Sara", "country": "UAE"},
    {"email": "omar@globex-microsoft.com", "first_name": "Omar", "country": "Pakistan"},
    {"email": "lee@initech.com", "first_name": "Lee", "country": "USA"},
    {"email": "nina@hooli.com", "first_name": "Nina", "country": "USA", "timezone": "america/new_york"},
    {"email": "zoe@gmail.com", "first_name": "Zoe", "country": "UK"},
]


def _import(client, rows=ROWS, name="Nov"):
    r = client.post("/api/groups/import", json={"name": name, "rows": rows})
    assert r.status_code == 201, r.text
    return r.json()


def _leads(client, group_id, **params):
    return {
        lead["email"]: lead
        for lead in client.get(f"/api/groups/{group_id}/leads", params=params).json()["items"]
    }


# --- Milestone 4 -------------------------------------------------------------------------------------


def test_import_sets_timezone_and_provider(client, admin):
    result = _import(client)
    assert result["provider_mix"] == {"GOOGLE": 2, "MICROSOFT": 1, "OTHER": 2}
    leads = _leads(client, result["group"]["id"])
    assert leads["sara@acme-google.com"]["timezone"] == "Asia/Dubai"
    assert leads["omar@globex-microsoft.com"]["timezone"] == "Asia/Karachi"
    assert leads["lee@initech.com"]["timezone"] is None  # USA has several zones
    assert leads["nina@hooli.com"]["timezone"] == "America/New_York"  # timezone column wins
    assert leads["zoe@gmail.com"]["email_provider"] == "GOOGLE"
    assert leads["omar@globex-microsoft.com"]["email_provider"] == "MICROSOFT"


def test_filter_facets_rename_add_edit_remove(client, admin):
    group = _import(client)["group"]
    gid = group["id"]
    assert set(_leads(client, gid, provider="GOOGLE")) == {"sara@acme-google.com", "zoe@gmail.com"}
    assert set(_leads(client, gid, country="usa")) == {"lee@initech.com", "nina@hooli.com"}
    facets = client.get(f"/api/groups/{gid}/facets").json()
    assert {f["value"]: f["count"] for f in facets["countries"]}["USA"] == 2

    renamed = client.patch(f"/api/groups/{gid}", json={"name": "November UAE", "source": "LinkedIn"}).json()
    assert (renamed["name"], renamed["source"]) == ("November UAE", "LinkedIn")

    added = client.post(
        f"/api/groups/{gid}/leads", json={"email": "New@Pied.com", "first_name": "Priya"}
    ).json()
    assert added["added"] and added["lead"]["email"] == "new@pied.com"
    again = client.post(f"/api/groups/{gid}/leads", json={"email": "sara@acme-google.com"}).json()
    assert again == {"added": False, "pending_duplicate": True, "lead": None}
    assert client.post(f"/api/groups/{gid}/leads", json={"email": "nope"}).json()["detail"] == "invalid_email"

    lee = _leads(client, gid)["lee@initech.com"]
    edited = client.patch(f"/api/leads/{lee['id']}", json={"company": "Initech", "country": "UK"}).json()
    assert (edited["company"], edited["timezone"]) == ("Initech", "Europe/London")
    assert client.patch(f"/api/leads/{lee['id']}", json={"timezone": "Mars/Base"}).status_code == 400

    assert client.delete(f"/api/groups/{gid}/leads/{lee['id']}").status_code == 204
    assert "lee@initech.com" not in _leads(client, gid)
    assert client.delete(f"/api/groups/{gid}/leads/{lee['id']}").status_code == 404


def test_google_sheet_links(client, admin, monkeypatch):
    assert (
        sheets.export_url(
            "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/edit#gid=777"
        )
        == "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/export?format=csv&gid=777"
    )
    for bad in [
        "https://evil.example/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345",
        "http://docs.google.com/x",
    ]:
        with pytest.raises(sheets.SheetError):
            sheets.export_url(bad)
    r = client.post("/api/groups/google-sheet", json={"url": "https://example.com/not-a-sheet"})
    assert r.json()["detail"] == "sheet_link_invalid"

    monkeypatch.setattr(sheets, "fetch_csv", lambda url: "Email,First Name\nsara@x.com,Sara\n")
    r = client.post(
        "/api/groups/google-sheet",
        json={"url": "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/edit"},
    )
    assert r.json() == {"csv": "Email,First Name\nsara@x.com,Sara\n"}


# --- Milestones 6 and 7 ------------------------------------------------------------------------------


@pytest.fixture
def setup(client, admin):
    client.put("/api/settings/footer", json=FOOTER)
    mailbox = client.post("/api/mailboxes", json=MAILBOX).json()
    group = _import(client)["group"]
    return {"mailbox_id": mailbox["id"], "group_id": group["id"]}


def _campaign(client, setup, **extra):
    body = {
        "name": "Rich",
        "subject": "Hi {{first_name}}",
        "body": "Hello",
        "group_ids": [setup["group_id"]],
        "mailbox_ids": [setup["mailbox_id"]],
        "send_days": [0, 1, 2, 3, 4, 5, 6],
        "start_hour": 0,
        "end_hour": 24,
        **extra,
    }
    r = client.post("/api/campaigns", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_rich_text_is_cleaned_and_sent_as_html_plus_text(client, setup, db):
    campaign = _campaign(
        client,
        setup,
        body_format="HTML",
        body='<p>Hi <strong>{{first_name}}</strong>, <a href="https://fastnexa.com">see</a></p>'
        '<script>alert(1)</script><p onclick="x()">Thanks</p>',
    )
    assert "<script>" not in campaign["body"] and "onclick" not in campaign["body"]
    client.post(f"/api/campaigns/{campaign['id']}/start")

    smtp = FakeSmtp()
    sending.send_next(db, setup["mailbox_id"], datetime.now(UTC), smtp)
    msg = smtp.sent[0]
    assert msg.get_content_type() == "multipart/alternative"
    text = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert text.rstrip("\n") == "Hi Sara, see (https://fastnexa.com)\n\nThanks"
    assert "<strong>Sara</strong>" in html and "1 Main St, Lahore" not in html
    recipient = db.scalar(select(CampaignRecipient).where(CampaignRecipient.status == RecipientStatus.SENT))
    assert recipient.html_sent and recipient.body_sent == text

    preview = client.post(
        "/api/campaigns/preview",
        json={
            "subject": "Hi",
            "body": "<p>{Hi|Hello} {{first_name}}</p>",
            "body_format": "HTML",
            "group_ids": [setup["group_id"]],
        },
    ).json()
    assert preview["versions"][0]["html"].startswith("<p>H")


def test_sending_window_uses_each_leads_timezone(client, setup, db):
    campaign = _campaign(client, setup, use_lead_timezone=True, timezone="UTC", start_hour=9, end_hour=17)
    client.post(f"/api/campaigns/{campaign['id']}/start")
    # 06:00 UTC: 10:00 in Dubai (open), 11:00 in Karachi (open), 06:00 in London and UTC (closed),
    # 01:00 in New York (closed).
    at = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
    smtp = FakeSmtp()
    for i in range(3):
        sending.send_next(db, setup["mailbox_id"], at + timedelta(minutes=11 * i), smtp)
    assert [m["To"] for m in smtp.sent] == ["sara@acme-google.com", "omar@globex-microsoft.com"]


def test_leads_left_out_before_start_are_not_emailed(client, setup, db):
    campaign = _campaign(client, setup)
    audience = client.get(f"/api/campaigns/{campaign['id']}/audience").json()
    assert audience["total"] == 5
    ids = [a["id"] for a in audience["items"] if a["email"] in ("lee@initech.com", "zoe@gmail.com")]
    r = client.put(f"/api/campaigns/{campaign['id']}/exclusions", json={"lead_ids": ids, "excluded": True})
    assert r.json() == {"excluded_count": 2}
    flags = {
        a["email"]: a["excluded"]
        for a in client.get(f"/api/campaigns/{campaign['id']}/audience").json()["items"]
    }
    assert flags["lee@initech.com"] and not flags["sara@acme-google.com"]

    started = client.post(f"/api/campaigns/{campaign['id']}/start").json()
    assert started["counts"]["total"] == 3 and started["excluded_count"] == 2
    r = client.put(f"/api/campaigns/{campaign['id']}/exclusions", json={"lead_ids": ids, "excluded": False})
    assert r.status_code == 409


def test_sent_copy_is_saved_without_affecting_the_send(client, setup, db, monkeypatch):
    client.patch(f"/api/mailboxes/{setup['mailbox_id']}", json={"imap_host": "mail.fastnexa.com"})
    campaign = _campaign(client, setup)
    client.post(f"/api/campaigns/{campaign['id']}/start")

    monkeypatch.setattr(mailer, "save_sent_copy", lambda mailbox, msg: "INBOX.Sent")
    smtp, at = FakeSmtp(), datetime.now(UTC)
    assert sending.send_next(db, setup["mailbox_id"], at, smtp) == "sent"

    def broken(mailbox, msg):
        raise mailer.SentCopyFailed("IMAP down")

    monkeypatch.setattr(mailer, "save_sent_copy", broken)
    assert sending.send_next(db, setup["mailbox_id"], at + timedelta(minutes=11), smtp) == "sent"

    rows = db.scalars(
        select(CampaignRecipient)
        .where(CampaignRecipient.status == RecipientStatus.SENT)
        .order_by(CampaignRecipient.id)
    ).all()
    assert [r.sent_copy_saved for r in rows] == [True, False]
    assert db.get(Mailbox, setup["mailbox_id"]).sent_folder == "INBOX.Sent"


class FakeImap:
    appended: list = []

    def __init__(self, host, port, ssl_context=None, timeout=None):
        self.host = host

    def login(self, user, password):
        pass

    def list(self):
        return "OK", [b'(\\HasNoChildren) "." "INBOX.Drafts"', b'(\\HasNoChildren \\Sent) "." "INBOX.Sent"']

    def append(self, folder, flags, date, message):
        FakeImap.appended.append((folder, flags, message))
        return "OK", [b"APPEND completed"]

    def logout(self):
        pass


def test_mailer_appends_to_the_sent_folder_marked_read(setup, db, monkeypatch):
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImap)
    mailbox = db.get(Mailbox, setup["mailbox_id"])
    mailbox.imap_host = "mail.fastnexa.com"
    msg = mailer.build_message(mailbox, "x@example.com", "Hi", "Hello", "<p>Hello</p>")
    assert mailer.save_sent_copy(mailbox, msg) == "INBOX.Sent"
    folder, flags, raw = FakeImap.appended[-1]
    assert folder == '"INBOX.Sent"' and flags == "(\\Seen)" and b"Subject: Hi" in raw
    assert mailer.real_check_imap(mailbox) == "INBOX.Sent"


def test_keep_both_copy_keeps_provider_and_timezone(client, admin, db):
    _import(client, [{"email": "sara@acme-google.com", "country": "UAE"}], name="A")
    _import(client, [{"email": "sara@acme-google.com", "country": "Qatar"}], name="B")
    dup = client.get("/api/duplicates").json()["items"][0]
    client.post(
        "/api/duplicates/resolve", json={"decision": "KEEP_BOTH", "ids": [dup["id"]], "all_pending": False}
    )
    copies = db.scalars(select(Lead).where(Lead.email == "sara@acme-google.com").order_by(Lead.id)).all()
    assert [(c.timezone, c.email_provider.value) for c in copies] == [
        ("Asia/Dubai", "GOOGLE"),
        ("Asia/Qatar", "GOOGLE"),
    ]


def test_editor_html_is_tidied_for_email():
    from app.services.email_html import sanitize, to_text

    html = "<p>Hi</p><ul><li><p>One</p></li><li><p>Two</p><p>more</p></li></ul><p></p><p>Bye</p>"
    clean = sanitize(html)
    assert clean == "<p>Hi</p><ul><li>One</li><li><p>Two</p><p>more</p></li></ul><p>Bye</p>"
    assert to_text(clean) == "Hi\n\n- One\n- Two more\n\nBye"
