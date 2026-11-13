"""Reports, the per-lead CSV, and GDPR export / forget."""

import csv
import io
from datetime import UTC, datetime

from app.models import CampaignRecipient, DoNotContact, Lead, RecipientStatus, Role
from tests.test_campaigns import MAILBOX


def _running_campaign(client, emails):
    client.put("/api/settings/footer", json={"business_name": "F", "postal_address": "A"})
    mb = client.post("/api/mailboxes", json=MAILBOX).json()
    group = client.post(
        "/api/groups/import",
        json={"name": "G", "rows": [{"email": e, "first_name": "=cmd", "company": "Acme"} for e in emails]},
    ).json()["group"]
    campaign = client.post(
        "/api/campaigns",
        json={
            "name": "Autumn",
            "subject": "Hi",
            "body": "Hello",
            "group_ids": [group["id"]],
            "mailbox_ids": [mb["id"]],
        },
    ).json()
    client.post(f"/api/campaigns/{campaign['id']}/start")
    return campaign, mb


def _mark(db, email, status, mailbox_id, **extra):
    r = db.query(CampaignRecipient).filter_by(email=email).one()
    r.status, r.mailbox_id = status, mailbox_id
    for k, v in extra.items():
        setattr(r, k, v)
    db.commit()


def test_summary_counts_per_day_mailbox_and_campaign(client, admin, db):
    campaign, mb = _running_campaign(client, ["a@x.io", "b@x.io", "c@x.io"])
    _mark(db, "a@x.io", RecipientStatus.SENT, mb["id"], sent_at=datetime.now(UTC))
    _mark(db, "b@x.io", RecipientStatus.FAILED, mb["id"], error="550 no such user")

    s = client.get("/api/reports/summary?days=7&tz=Asia/Karachi").json()
    assert len(s["days"]) == 7
    assert s["days"][-1]["sent"] == 1 and s["days"][-1]["failed"] == 1
    assert sum(d["sent"] for d in s["days"]) == 1
    assert s["mailboxes"] == [{"id": mb["id"], "email": "ali@fastnexa.com", "sent": 1, "failed": 1}]
    [c] = s["campaigns"]
    assert (c["name"], c["sent"], c["failed"], c["total"], c["done"]) == ("Autumn", 1, 1, 3, 2)
    assert s["totals"] == {"sent": 1, "failed": 1, "skipped": 0, "people": 1}
    assert client.get("/api/reports/summary?tz=Mars/Base").status_code == 400


def test_recipients_csv_export_is_safe_and_logged(client, admin, db):
    campaign, mb = _running_campaign(client, ["a@x.io", "b@x.io"])
    _mark(db, "a@x.io", RecipientStatus.SENT, mb["id"], sent_at=datetime.now(UTC), smtp_response="250 OK")

    r = client.get(f"/api/campaigns/{campaign['id']}/recipients/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "Autumn-recipients-" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [(x["email"], x["status"]) for x in rows] == [("a@x.io", "SENT"), ("b@x.io", "WAITING")]
    assert rows[0]["name"] == "'=cmd"  # formula neutralised
    assert rows[0]["server_reply"] == "250 OK" and rows[0]["from_mailbox"] == "ali@fastnexa.com"

    only_sent = client.get(f"/api/campaigns/{campaign['id']}/recipients/export?status=SENT").text
    assert "b@x.io" not in only_sent
    actions = [x["action"] for x in client.get("/api/logs").json()["items"]]
    assert "campaign.recipients_exported" in actions


def test_export_then_forget_a_person(client, admin, db):
    campaign, mb = _running_campaign(client, ["a@x.io", "b@x.io"])
    _mark(db, "a@x.io", RecipientStatus.SENT, mb["id"], sent_at=datetime.now(UTC), body_sent="Hello A")

    exported = client.post("/api/people/export", json={"email": "A@x.io"})
    assert exported.status_code == 200
    data = exported.json()
    assert data["email"] == "a@x.io" and data["leads"][0]["groups"][0]["group"] == "G"
    assert data["emails"][0]["body"] == "Hello A" and data["emails"][0]["campaign"] == "Autumn"

    r = client.post("/api/people/forget", json={"email": "a@x.io"})
    assert r.json() == {"leads": 1, "campaigns": 1, "campaign_emails": 1, "duplicate_rows": 0}

    db.expire_all()
    assert db.query(Lead).filter_by(email="a@x.io").count() == 0
    sent = db.query(CampaignRecipient).filter_by(status=RecipientStatus.SENT).one()
    assert sent.lead_id is None and sent.body_sent is None and sent.email.startswith("forgotten:")
    assert "a@x.io" not in [d.value for d in db.query(DoNotContact)]

    # The history still counts, without the address; the other lead is untouched.
    recipients = client.get(f"/api/campaigns/{campaign['id']}/recipients").json()["items"]
    assert [(x["email"], x["status"]) for x in recipients] == [
        ("Forgotten person", "SENT"),
        ("b@x.io", "WAITING"),
    ]
    assert client.get("/api/reports/summary").json()["totals"]["sent"] == 1

    # They can't come back in, and an exact search of the do-not-contact list finds them.
    imp = client.post(
        "/api/groups/import", json={"name": "Again", "rows": [{"email": "a@x.io"}, {"email": "c@x.io"}]}
    )
    assert imp.json()["added"] == 1 and imp.json()["invalid"] == [
        {"row": 1, "email": "a@x.io", "reason": "forgotten"}
    ]
    group_id = imp.json()["group"]["id"]
    assert (
        client.post(f"/api/groups/{group_id}/leads", json={"email": "a@x.io"}).json()["detail"]
        == "person_forgotten"
    )
    assert client.get("/api/dnc?q=a@x.io").json()["total"] == 1
    assert (
        client.post("/api/people/export", json={"email": "a@x.io"})
        .json()["do_not_contact"]["reason"]
        .startswith("Forgotten")
    )


def test_forgetting_a_waiting_lead_skips_it_and_can_finish_the_campaign(client, admin, db):
    campaign, _ = _running_campaign(client, ["a@x.io"])
    client.post("/api/people/forget", json={"email": "a@x.io"})
    c = client.get(f"/api/campaigns/{campaign['id']}").json()
    assert c["status"] == "COMPLETED" and c["counts"]["skipped"] == 1


def test_only_admins_can_forget(client, make_user, login, db):
    make_user("member@fastnexa.com", role=Role.MEMBER)
    db.commit()
    login("member@fastnexa.com")
    assert client.post("/api/people/forget", json={"email": "a@x.io"}).status_code == 403
    assert (
        client.post("/api/people/export", json={"email": "nobody@x.io"}).json()["detail"]
        == "person_not_found"
    )
