from sqlalchemy import select

from app.models import ActivityLog, CampaignRecipient, Lead, Role

FOOTER = {"business_name": "FastNexa", "postal_address": "1 Main St, Lahore"}
MAILBOX = {
    "email": "ali@fastnexa.com",
    "display_name": "Ali Khan",
    "smtp_host": "mail.fastnexa.com",
    "smtp_port": 465,
    "smtp_security": "SSL",
    "smtp_username": "ali@fastnexa.com",
    "smtp_password": "secret-password",
}


def _import(client, name, rows):
    r = client.post("/api/groups/import", json={"name": name, "rows": rows})
    assert r.status_code == 201, r.text
    return r.json()


def _pending(client, **params):
    return client.get("/api/duplicates", params=params).json()


def test_existing_and_repeated_rows_wait_as_pending(client, admin):
    _import(client, "October", [{"email": "sara@acme.com", "first_name": "Sara", "company": "Acme"}])
    november = _import(
        client,
        "November",
        [
            {"email": " SARA@acme.com", "first_name": "Sara", "company": "Acme Ltd"},
            {"email": "omar@globex.com", "first_name": "Omar"},
            {"email": "omar@globex.com", "first_name": "Omar again"},
        ],
    )
    assert (november["added"], november["pending_duplicates"]) == (1, 2)
    assert november["group"]["lead_count"] == 1

    groups = {g["name"]: g for g in client.get("/api/groups").json()}
    assert groups["November"]["pending_count"] == 2
    assert client.get("/api/duplicates/count").json() == {"pending": 2}

    page = _pending(client)
    assert page["total"] == 2
    sara = next(d for d in page["items"] if d["new"]["email"] == "sara@acme.com")
    assert sara["existing"]["company"] == "Acme"
    assert sara["new"]["company"] == "Acme Ltd"
    assert sara["differences"] == ["company"]
    assert sara["group"]["name"] == "November"


def test_keep_old_adds_the_existing_lead_unchanged(client, admin, db):
    _import(client, "October", [{"email": "sara@acme.com", "first_name": "Sara", "company": "Acme"}])
    nov = _import(
        client, "November", [{"email": "sara@acme.com", "first_name": "Sarah", "company": "Acme Ltd"}]
    )
    dup = _pending(client)["items"][0]

    r = client.post("/api/duplicates/resolve", json={"decision": "KEEP_OLD", "ids": [dup["id"]]})
    assert r.json() == {"resolved": 1}

    leads = client.get(f"/api/groups/{nov['group']['id']}/leads").json()["items"]
    assert [(lead["id"], lead["first_name"], lead["company"]) for lead in leads] == [
        (dup["existing"]["id"], "Sara", "Acme")
    ]
    assert _pending(client)["total"] == 0
    decided = _pending(client, status="decided")["items"][0]
    assert decided["status"] == "KEPT_OLD"
    assert db.scalar(select(ActivityLog.id).where(ActivityLog.action == "duplicate.resolved")) is not None


def test_keep_new_updates_the_existing_lead(client, admin, db):
    _import(
        client,
        "October",
        [
            {
                "email": "sara@acme.com",
                "first_name": "Sara",
                "company": "Acme",
                "custom": {"industry": "Retail"},
            }
        ],
    )
    nov = _import(
        client,
        "November",
        [{"email": "sara@acme.com", "company": "Acme Ltd", "country": "UAE", "custom": {"city": "Dubai"}}],
    )
    dup = _pending(client)["items"][0]
    client.post("/api/duplicates/resolve", json={"decision": "KEEP_NEW", "ids": [dup["id"]]})

    lead = db.get(Lead, dup["existing"]["id"])
    db.refresh(lead)
    # New values win; a blank in the new row (first name) keeps the old value. Extra fields merge.
    assert (lead.first_name, lead.company, lead.country) == ("Sara", "Acme Ltd", "UAE")
    assert lead.custom == {"industry": "Retail", "city": "Dubai"}
    assert client.get(f"/api/groups/{nov['group']['id']}").json()["lead_count"] == 1
    update = db.scalar(select(ActivityLog).where(ActivityLog.action == "lead.updated"))
    # The new row's country also gives the lead a timezone.
    assert update.before == {
        "company": "Acme",
        "country": None,
        "timezone": None,
        "custom": {"industry": "Retail"},
    }
    assert update.after["timezone"] == "Asia/Dubai"


def test_keep_both_creates_a_second_lead_that_gets_its_own_email(client, admin, db):
    _import(client, "October", [{"email": "sara@acme.com", "first_name": "Sara"}])
    nov = _import(client, "November", [{"email": "sara@acme.com", "first_name": "Sara (finance)"}])
    dup = _pending(client)["items"][0]
    client.post("/api/duplicates/resolve", json={"decision": "KEEP_BOTH", "ids": [dup["id"]]})

    assert len(db.scalars(select(Lead).where(Lead.email == "sara@acme.com")).all()) == 2
    nov_leads = client.get(f"/api/groups/{nov['group']['id']}/leads").json()["items"]
    assert [lead["first_name"] for lead in nov_leads] == ["Sara (finance)"]

    # A campaign to both groups sends to each copy.
    client.put("/api/settings/footer", json=FOOTER)
    mailbox = client.post("/api/mailboxes", json=MAILBOX).json()
    groups = [g["id"] for g in client.get("/api/groups").json()]
    campaign = client.post(
        "/api/campaigns",
        json={
            "name": "Both",
            "subject": "Hi",
            "body": "Hello",
            "group_ids": groups,
            "mailbox_ids": [mailbox["id"]],
        },
    ).json()
    counts = client.post(f"/api/campaigns/{campaign['id']}/start").json()["counts"]
    assert counts["waiting"] == 2
    emails = db.scalars(
        select(CampaignRecipient.email).where(CampaignRecipient.campaign_id == campaign["id"])
    ).all()
    assert emails == ["sara@acme.com", "sara@acme.com"]


def test_pending_duplicates_are_not_emailed(client, admin):
    _import(client, "October", [{"email": "sara@acme.com"}])
    nov = _import(client, "November", [{"email": "sara@acme.com"}, {"email": "omar@globex.com"}])
    client.put("/api/settings/footer", json=FOOTER)
    mailbox = client.post("/api/mailboxes", json=MAILBOX).json()
    campaign = client.post(
        "/api/campaigns",
        json={
            "name": "Nov",
            "subject": "Hi",
            "body": "Hello",
            "group_ids": [nov["group"]["id"]],
            "mailbox_ids": [mailbox["id"]],
        },
    ).json()
    counts = client.post(f"/api/campaigns/{campaign['id']}/start").json()["counts"]
    assert counts["total"] == 1  # only omar; sara is still pending


def test_apply_to_all_pending_and_never_twice(client, admin):
    _import(client, "October", [{"email": "a@x.io"}, {"email": "b@x.io"}])
    first = _import(client, "November", [{"email": "a@x.io"}, {"email": "b@x.io"}])
    _import(client, "December", [{"email": "a@x.io"}])
    assert client.get("/api/duplicates/count").json() == {"pending": 3}

    r = client.post(
        "/api/duplicates/resolve",
        json={"decision": "KEEP_OLD", "all_pending": True, "group_id": first["group"]["id"]},
    )
    assert r.json() == {"resolved": 2}
    assert client.get("/api/duplicates/count").json() == {"pending": 1}

    decided = _pending(client, status="decided")["items"][0]["id"]
    again = client.post("/api/duplicates/resolve", json={"decision": "KEEP_BOTH", "ids": [decided]})
    assert again.json() == {"resolved": 0}

    assert client.post("/api/duplicates/resolve", json={"decision": "KEEP_OLD"}).status_code == 400


def test_viewers_cannot_decide(client, make_user, login):
    make_user("viewer@fastnexa.com", role=Role.VIEWER)
    login("viewer@fastnexa.com")
    assert client.get("/api/duplicates").status_code == 200
    r = client.post("/api/duplicates/resolve", json={"decision": "KEEP_OLD", "all_pending": True})
    assert r.status_code == 403
