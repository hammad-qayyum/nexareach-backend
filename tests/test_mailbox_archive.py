"""Mailboxes that have sent keep their history: they can be archived, not deleted."""

from datetime import UTC, datetime

from app.models import CampaignRecipient, RecipientStatus
from tests.test_campaigns import MAILBOX


def _campaign(client, mailbox_ids, **extra):
    group = client.post("/api/groups/import", json={"name": "G", "rows": [{"email": "a@x.io"}]}).json()[
        "group"
    ]
    return client.post(
        "/api/campaigns",
        json={
            "name": "C",
            "subject": "Hi",
            "body": "Hello",
            "group_ids": [group["id"]],
            "mailbox_ids": mailbox_ids,
            **extra,
        },
    ).json()


def test_unused_or_never_sent_mailbox_can_be_deleted(client, admin):
    client.put("/api/settings/footer", json={"business_name": "F", "postal_address": "A"})
    mb = client.post("/api/mailboxes", json=MAILBOX).json()
    assert mb["can_delete"] is True

    # Picked for a campaign that was cancelled before it sent anything: still deletable.
    campaign = _campaign(client, [mb["id"]])
    client.post(f"/api/campaigns/{campaign['id']}/start")
    assert client.get("/api/mailboxes").json()[0]["can_delete"] is False  # running campaign uses it
    assert client.delete(f"/api/mailboxes/{mb['id']}").json()["detail"] == "mailbox_in_active_campaign"
    client.post(f"/api/campaigns/{campaign['id']}/cancel")
    assert client.get("/api/mailboxes").json()[0]["can_delete"] is True
    assert client.delete(f"/api/mailboxes/{mb['id']}").status_code == 204
    assert client.get(f"/api/campaigns/{campaign['id']}").json()["mailboxes"] == []


def test_mailbox_that_sent_can_only_be_archived(client, admin, db):
    client.put("/api/settings/footer", json={"business_name": "F", "postal_address": "A"})
    mb = client.post("/api/mailboxes", json=MAILBOX).json()
    campaign = _campaign(client, [mb["id"]])
    client.post(f"/api/campaigns/{campaign['id']}/start")
    r = db.query(CampaignRecipient).first()
    r.mailbox_id, r.status, r.sent_at = mb["id"], RecipientStatus.SENT, datetime.now(UTC)
    db.commit()
    client.post(f"/api/campaigns/{campaign['id']}/cancel")

    assert client.delete(f"/api/mailboxes/{mb['id']}").json()["detail"] == "mailbox_has_sent"
    draft = _campaign(client, [mb["id"]])

    archived = client.post(f"/api/mailboxes/{mb['id']}/archive").json()
    assert archived["status"] == "PAUSED" and archived["archived_at"]
    assert client.get("/api/mailboxes").json() == []
    assert [m["email"] for m in client.get("/api/mailboxes?include_archived=true").json()] == [
        "ali@fastnexa.com"
    ]
    # Drafts stop using it; it can't be newly picked or activated.
    assert client.get(f"/api/campaigns/{draft['id']}").json()["mailboxes"] == []
    r = client.patch(f"/api/campaigns/{draft['id']}", json={"mailbox_ids": [mb["id"]]})
    assert r.json()["detail"] == "mailbox_archived"
    assert (
        client.patch(f"/api/mailboxes/{mb['id']}", json={"status": "ACTIVE"}).json()["detail"]
        == "mailbox_archived"
    )
    # Testing it doesn't bring it back to life.
    client.post(f"/api/mailboxes/{mb['id']}/check")
    assert client.get("/api/mailboxes?include_archived=true").json()[0]["status"] == "PAUSED"

    restored = client.post(f"/api/mailboxes/{mb['id']}/restore").json()
    assert restored["archived_at"] is None and restored["status"] == "PAUSED"
    assert client.patch(f"/api/mailboxes/{mb['id']}", json={"status": "ACTIVE"}).json()["status"] == "ACTIVE"
