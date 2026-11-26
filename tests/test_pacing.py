"""Pacing beyond one mailbox's gap: same-domain spacing, the per-company daily limit, sends at random
seconds, and lead timezones from country, state and city."""

import random
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import CampaignRecipient, Mailbox
from app.services import geo, pacing, sending
from tests.test_campaigns import MAILBOX, TUESDAY_11AM_PKT, smtp  # noqa: F401 (shared fixture)

T = TUESDAY_11AM_PKT
NEXT = timedelta(minutes=11)  # longer than the default 5–10 minute gap


def _setup(client, emails, mailboxes=("ali@fastnexa.com",), follow_ups=None):
    client.put("/api/settings/footer", json={"business_name": "FastNexa", "postal_address": "1 Main St"})
    ids = [
        client.post(
            "/api/mailboxes",
            json={**MAILBOX, "email": m, "smtp_username": m, "daily_cap": 50, "ramp_up_enabled": False},
        ).json()["id"]
        for m in mailboxes
    ]
    group = client.post(
        "/api/groups/import", json={"name": "G", "rows": [{"email": e} for e in emails]}
    ).json()["group"]
    body = {
        "name": "C",
        "subject": "Hi",
        "body": "Hello",
        "group_ids": [group["id"]],
        "mailbox_ids": ids,
        "send_days": [0, 1, 2, 3, 4, 5, 6],
        "start_hour": 0,
        "end_hour": 24,
    }
    if follow_ups:
        body["follow_ups"] = follow_ups
    campaign = client.post("/api/campaigns", json=body).json()
    assert client.post(f"/api/campaigns/{campaign['id']}/start").status_code == 200
    return campaign["id"], ids


def _sent_to(db, campaign_id):
    return {
        r.email
        for r in db.scalars(select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign_id))
        if r.sent_at is not None
    }


def test_mailboxes_on_one_domain_stay_one_to_three_minutes_apart(client, admin, db, smtp):
    _, (ali, sana, omar) = _setup(
        client,
        ["a@one.com", "b@two.com", "c@three.com"],
        mailboxes=("ali@fastnexa.com", "sana@fastnexa.com", "omar@otherbrand.com"),
    )
    assert sending.send_next(db, ali, T, smtp) == "sent"
    db.expire_all()
    held = db.get(Mailbox, sana).next_send_at
    assert T + timedelta(seconds=60) <= held <= T + timedelta(seconds=180)
    # A mailbox on another domain isn't held back.
    assert db.get(Mailbox, omar).next_send_at is None
    assert sending.send_next(db, omar, T, smtp) == "sent"
    # Sana waits for her slot, then sends.
    assert sending.send_next(db, sana, T + timedelta(seconds=30), smtp) == "not_due"
    assert sending.send_next(db, sana, held, smtp) == "sent"


def test_a_company_gets_at_most_two_emails_a_day(client, admin, db, smtp):
    campaign_id, (mailbox,) = _setup(
        client,
        ["a@bluehub.com", "b@bluehub.com", "c@bluehub.com", "d@gmail.com", "e@gmail.com", "f@gmail.com"],
    )
    at = T
    for _ in range(5):
        assert sending.send_next(db, mailbox, at, smtp) == "sent"
        at += NEXT
    # Two at bluehub.com, and gmail.com (different people) isn't limited.
    assert _sent_to(db, campaign_id) == {
        "a@bluehub.com",
        "b@bluehub.com",
        "d@gmail.com",
        "e@gmail.com",
        "f@gmail.com",
    }
    assert sending.send_next(db, mailbox, at, smtp) == "idle"
    # 24 hours after the first bluehub email, the third person gets theirs.
    assert sending.send_next(db, mailbox, T + timedelta(days=1, minutes=1), smtp) == "sent"
    assert "c@bluehub.com" in _sent_to(db, campaign_id)


def test_follow_ups_count_towards_the_company_limit(client, admin, db, smtp):
    campaign_id, (mailbox,) = _setup(
        client,
        ["a@bluehub.com", "b@bluehub.com", "c@bluehub.com"],
        follow_ups=[{"delay_days": 1, "body": "Just checking."}],
    )
    assert sending.send_next(db, mailbox, T, smtp) == "sent"  # a
    assert sending.send_next(db, mailbox, T + timedelta(hours=5), smtp) == "sent"  # b
    assert sending.send_next(db, mailbox, T + timedelta(hours=6), smtp) == "idle"  # c waits
    # a's follow-up: only b's email is in the last 24 hours, so it goes…
    assert sending.send_next(db, mailbox, T + timedelta(days=1, minutes=1), smtp) == "follow_up_sent"
    # …and now c still waits: b's opening email + a's follow-up make two.
    assert sending.send_next(db, mailbox, T + timedelta(days=1, minutes=12), smtp) == "idle"
    assert "c@bluehub.com" not in _sent_to(db, campaign_id)


def test_free_mail_is_never_a_company():
    assert "gmail.com" in pacing.FREE_MAIL and "yahoo.co.uk" in pacing.FREE_MAIL
    assert "bluehub.com" not in pacing.FREE_MAIL


def test_each_mailbox_sends_at_a_random_second():
    rng = random.Random(1)
    planned = T + timedelta(seconds=37)
    plan = sending.tick_plan(
        [(1, None), (2, planned), (3, T - timedelta(hours=2)), (4, T + timedelta(minutes=5))], T, rng
    )
    when = dict((m, w) for w, m in plan)
    assert when[2] == planned  # its gap ends at a random second; it goes then
    assert T <= when[1] < T + timedelta(seconds=60) and when[1] != T  # free: a random second
    assert T <= when[3] < T + timedelta(seconds=60)
    assert 4 not in when  # not due this minute
    assert [w for w, _ in plan] == sorted(w for w, _ in plan)
    seconds = {sending.tick_plan([(1, None)], T, rng)[0][0].second for _ in range(30)}
    assert len(seconds) > 10  # not always :00


@pytest.mark.parametrize(
    ("country", "region", "city", "expected"),
    [
        ("USA", None, None, "America/New_York"),
        ("United States", "Texas", None, "America/Chicago"),
        ("US", "CA", None, "America/Los_Angeles"),
        ("usa", None, "Seattle", "America/Los_Angeles"),
        ("U.S.A.", "Arizona", None, "America/Phoenix"),
        ("Canada", "BC", None, "America/Vancouver"),
        ("Canada", None, "Calgary", "America/Edmonton"),
        ("Canada", None, None, "America/Toronto"),
        ("Australia", "WA", None, "Australia/Perth"),
        ("Australia", None, "Brisbane", "Australia/Brisbane"),
        ("Australia", None, None, "Australia/Sydney"),
        ("Brazil", None, None, "America/Sao_Paulo"),
        ("Mexico", None, "Tijuana", "America/Tijuana"),
        ("Russia", None, "Novosibirsk", "Asia/Novosibirsk"),
        ("Indonesia", "Bali", None, "Asia/Makassar"),
        # "Georgia" as a country is Tbilisi; as a US state it's New York time.
        ("Georgia", None, None, "Asia/Tbilisi"),
        ("USA", "Georgia", None, "America/New_York"),
        ("Vietnam", None, None, "Asia/Ho_Chi_Minh"),
        ("Atlantis", None, None, None),
        (None, "Texas", None, None),
    ],
)
def test_timezone_from_country_state_and_city(country, region, city, expected):
    assert geo.timezone_for(None, country, region, city) == expected


def test_import_uses_a_state_column(client, admin):
    rows = [
        {"email": "a@x.com", "country": "USA", "custom": {"state": "California"}},
        {"email": "b@x.com", "country": "USA", "custom": {"city": "Chicago"}},
        {"email": "c@x.com", "country": "USA", "timezone": "Europe/Paris"},  # a timezone column wins
        {"email": "d@x.com"},
    ]
    gid = client.post("/api/groups/import", json={"name": "US", "rows": rows}).json()["group"]["id"]
    leads = {x["email"]: x["timezone"] for x in client.get(f"/api/groups/{gid}/leads").json()["items"]}
    assert leads == {
        "a@x.com": "America/Los_Angeles",
        "b@x.com": "America/Chicago",
        "c@x.com": "Europe/Paris",
        "d@x.com": None,
    }
    preview = client.post(
        "/api/campaigns/preview", json={"subject": "", "body": "", "body_format": "TEXT", "group_ids": [gid]}
    ).json()
    assert (preview["lead_count"], preview["leads_without_timezone"]) == (4, 1)
