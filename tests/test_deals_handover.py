"""Deals integration, steps 3–5: scores on replies, the Needs review list, handovers, results and reports."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.crypto import encrypt
from app.models import (
    ActivityLog,
    CampaignRecipient,
    DealsHandover,
    DealsLeadResult,
    DealsOptout,
    DealsSyncState,
    DoNotContact,
    Lead,
    RecipientStatus,
    Reply,
    Role,
)
from app.services import deals_events, deals_handover, deals_reports, replies, reply_scores, sending
from app.services.app_settings import set_json
from tests.test_campaigns import MAILBOX, TUESDAY_11AM_PKT, ready, smtp  # noqa: F401 (shared fixtures)
from tests.test_deals import SECRET, URL, fake  # noqa: F401 (shared fixture)
from tests.test_follow_ups import FakeInbox, _message, _start_and_send_first, campaign  # noqa: F401

T = TUESDAY_11AM_PKT
DAY2 = T + timedelta(days=2)


def _deals(db, **flags) -> None:
    set_json(
        db,
        "deals.settings",
        {"url": URL, "secret_encrypted": encrypt(SECRET), **flags},
        None,
        secret=True,
    )
    db.flush()


def _scoring(db, on: bool = True, auto: int = 80, review: int = 40) -> None:
    set_json(
        db,
        reply_scores.SETTINGS_KEY,
        {"enabled": on, "auto_threshold": auto, "review_threshold": review},
        None,
    )
    db.flush()


def _reply(first, body: str, message_id: str = "<r1@acme.com>", cc: str = "") -> bytes:
    headers = f"Message-ID: {message_id}\r\nIn-Reply-To: {first['Message-ID']}\r\n"
    if cc:
        headers += f"Cc: {cc}\r\n"
    return _message(
        sender="Sara <sara@acme.com>", subject=f"Re: {first['Subject']}", body=body, headers=headers
    )


def _got(client, db, smtp, campaign, body: str, **kw) -> Reply:  # noqa: F811
    _start_and_send_first(client, db, smtp, campaign)
    replies.check_mailbox(db, campaign["mailbox_id"], DAY2, FakeInbox(_reply(smtp.sent[0], body, **kw)))
    return db.scalar(select(Reply))


def _logged(db, action: str) -> list[ActivityLog]:
    return list(db.scalars(select(ActivityLog).where(ActivityLog.action == action)))


# --- Step 3: scores on replies ------------------------------------------------------------------------------


def test_scoring_off_changes_nothing(client, db, smtp, campaign):
    reply = _got(client, db, smtp, campaign, "Interested, can we talk Thursday?")
    assert reply.score is None and reply.review_status is None


def test_a_reply_is_scored_and_logged(client, db, smtp, campaign):
    _scoring(db)
    # acme.com is a company domain; the quoted line doesn't count.
    reply = _got(client, db, smtp, campaign, "Sounds good. What's the price?\r\n\r\n> Can we book a call?")
    assert reply.score == 90 and reply.score_band == "auto"
    assert reply.score_rules == ["Clear interest", "Asks about price", "Asks a question", "Company email"]
    # Handover is off: an 80+ reply waits for a person instead.
    assert reply.review_status == "pending"
    assert _logged(db, "reply.scored")[0].after["band"] == "auto"


@pytest.mark.parametrize(
    ("auto", "review", "band", "status"),
    [
        (51, 40, "review", "pending"),  # 50: just under the auto line
        (50, 40, "auto", "pending"),  # exactly on it (handover is off, so a person decides)
        (80, 50, "review", "pending"),  # exactly on the review line
        (90, 51, "low", None),  # just under it
    ],
)
def test_bands_at_the_edges(client, db, smtp, campaign, auto, review, band, status):
    # The reply scores exactly 50 (interested 40 + company email 10).
    _scoring(db, auto=auto, review=review)
    reply = _got(client, db, smtp, campaign, "I'm interested.")
    assert reply.score == 50
    assert (reply.score_band, reply.review_status) == (band, status)


def test_score_exactly_40_and_79(client, db, smtp, campaign):
    _scoring(db, auto=80, review=40)
    rules = reply_scores.rules(db)
    s = reply_scores.settings(db)
    from app.services.scoring import ScoreResult

    assert reply_scores.band_for(ScoreResult(40), s) == "review"
    assert reply_scores.band_for(ScoreResult(39), s) == "low"
    assert reply_scores.band_for(ScoreResult(79), s) == "review"
    assert reply_scores.band_for(ScoreResult(80), s) == "auto"
    assert reply_scores.band_for(ScoreResult(95, hard_stop="no"), s) == "zero"
    assert len(rules) == 12  # the seeded starting rules


def test_an_opt_out_scores_zero(client, db, smtp, campaign):
    _scoring(db)
    reply = _got(client, db, smtp, campaign, "Please remove me from your list.")
    assert (reply.score, reply.score_band, reply.review_status) == (0, "zero", None)
    assert reply.score_rules == ["Hard stop: no"]


def test_cc_is_saved_and_counts_as_a_colleague(client, db, smtp, campaign):
    _scoring(db)
    reply = _got(client, db, smtp, campaign, "Looping in my boss.", cc="Boss <boss@acme.com>")
    assert reply.cc == ["boss@acme.com"] and "Brings a colleague" in reply.score_rules


def test_switching_on_scores_earlier_replies_for_display_only(client, admin, db, smtp, campaign):
    reply = _got(client, db, smtp, campaign, "Interested! Price?")
    r = client.put(
        "/api/scoring/settings",
        json={
            "enabled": True,
            "auto_threshold": 80,
            "review_threshold": 40,
            "free_mail_domains": ["gmail.com"],
        },
    )
    assert r.status_code == 200
    db.refresh(reply)
    assert reply.score == 90 and reply.review_status is None
    assert db.scalar(select(DealsHandover)) is None


def test_try_it_and_rules_editor(client, admin, db, make_user, login):
    r = client.post(
        "/api/scoring/try", json={"text": "Interested!\n\n> call me", "sender": "x@acme.com"}
    ).json()
    assert r["score"] == 50 and r["band"] == "review" and r["new_text"] == "Interested!"
    rule = client.post(
        "/api/scoring/rules", json={"name": "Urdu yes", "kind": "words", "words": ["ji haan"], "points": 30}
    ).json()
    assert (
        client.post("/api/scoring/try", json={"text": "ji haan", "sender": "x@gmail.com"}).json()["score"]
        == 30
    )
    bad = client.post(
        "/api/scoring/rules", json={"name": "Slow", "kind": "regex", "pattern": "(a+)+!", "points": 5}
    )
    assert bad.status_code == 400 and "too long" in bad.json()["detail"]
    client.put(f"/api/scoring/rules/{rule['id']}", json={**rule, "points": 35})
    assert {log.action for log in db.scalars(select(ActivityLog))} >= {
        "score_rule.created",
        "score_rule.updated",
    }
    assert (
        client.put("/api/scoring/thresholds", json={"auto_threshold": 40, "review_threshold": 40}).status_code
        == 400
    )
    # Members may try it, but not change rules or cut-offs.
    make_user("member@fastnexa.com", role=Role.MEMBER)
    login("member@fastnexa.com")
    assert client.post("/api/scoring/try", json={"text": "hi", "sender": ""}).status_code == 200
    assert client.get("/api/scoring/rules").status_code == 403
    assert (
        client.put("/api/scoring/thresholds", json={"auto_threshold": 90, "review_threshold": 40}).status_code
        == 403
    )


# --- The Needs review list ----------------------------------------------------------------------------------


def test_needs_review_tab_and_not_now(client, admin, db, smtp, campaign):
    _scoring(db)
    reply = _got(client, db, smtp, campaign, "I'm interested.")
    assert client.get("/api/replies/count").json()["review"] == 1
    listed = client.get("/api/replies", params={"state": "review"}).json()["items"]
    assert [x["id"] for x in listed] == [reply.id] and listed[0]["score"] == 50
    r = client.post(
        f"/api/replies/{reply.id}/not-now", json={"reason": "too_small", "note": "2 people"}
    ).json()
    assert r["review_status"] == "rejected" and r["review_reason"] == "too_small: 2 people"
    assert r["reviewed_by"] == admin.name
    assert client.get("/api/replies/count").json()["review"] == 0


def test_viewers_can_look_but_not_decide(client, admin, db, smtp, campaign, make_user, login):
    _scoring(db)
    reply = _got(client, db, smtp, campaign, "I'm interested.")
    make_user("viewer@fastnexa.com", role=Role.VIEWER)
    login("viewer@fastnexa.com")
    assert client.get("/api/replies", params={"state": "review"}).json()["total"] == 1
    assert client.post(f"/api/replies/{reply.id}/send-to-deals").status_code == 403
    assert client.post(f"/api/replies/{reply.id}/not-now", json={"reason": "other"}).status_code == 403


# --- Step 4: handover ---------------------------------------------------------------------------------------


def test_an_auto_reply_is_handed_over_with_the_whole_thread(client, admin, db, smtp, campaign, fake):
    _deals(db, handover_enabled=True)
    _scoring(db)
    client.patch(f"/api/campaigns/{campaign['id']}", json={"deals_service_key": "vapt"})
    reply = _got(client, db, smtp, campaign, "Sounds good. What's the price?")
    handover = db.scalar(select(DealsHandover))
    assert (handover.band, handover.status, reply.review_status) == ("auto", "queued", None)

    assert deals_handover.run(db, DAY2) == 1
    (sent,) = fake.handovers
    assert sent["test"] is True and sent["reply_id"] == f"rpl_{reply.id}"
    assert sent["prospect"]["email"] == "sara@acme.com" and sent["prospect"]["first_name"] == "Sara"
    assert sent["prospect"]["extra"] == {"industry": "Logistics"}
    assert sent["lead_list"] == {"name": "October", "source": "LinkedIn"}
    assert sent["campaign"]["deals_service"] == "vapt" and sent["campaign"]["step"] == "opening"
    assert sent["score"] == {"value": 90, "band": "auto", "rules": reply.score_rules, "approved_by": None}
    assert [m["direction"] for m in sent["thread"]] == ["out", "in"]
    assert sent["thread"][0]["sent_at"].endswith("Z") and sent["nexareach_url"].endswith(
        f"/replies?open={reply.id}"
    )
    assert fake.calls("/handover")[0].headers["Idempotency-Key"] == f"handover:{reply.id}"

    recipient = db.get(CampaignRecipient, reply.recipient_id)
    lead = db.get(Lead, recipient.lead_id)
    assert recipient.status == RecipientStatus.HANDED_TO_DEALS and recipient.next_follow_up_at is None
    assert recipient.follow_up_stop in ("replied", "handed_to_deals")
    assert lead.handed_to_deals_at is not None and lead.deals_lead_url == "https://deals.example/leads/lead_1"
    detail = client.get(f"/api/replies/{reply.id}").json()
    assert detail["handover"]["status"] == "created"
    assert detail["handover"]["deals_lead_url"] == "https://deals.example/leads/lead_1"
    # Counted as emailed everywhere (reports, the 30-day rule), shown as handed over on the campaign.
    counts = client.get(f"/api/campaigns/{campaign['id']}").json()["counts"]
    assert counts["handed_to_deals"] == 1


def test_handing_over_stops_them_in_every_campaign(client, admin, db, smtp, campaign, fake, ready):  # noqa: F811
    _deals(db, handover_enabled=True)
    # A second campaign where Sara is still waiting for her first email.
    other = client.post(
        "/api/campaigns",
        json={
            "name": "Second",
            "subject": "Hi",
            "body": "Hello",
            "group_ids": [ready["import"]["group"]["id"]],
            "mailbox_ids": [ready["mailbox"]["id"]],
            "send_days": [0, 1, 2, 3, 4, 5, 6],
            "start_hour": 0,
            "end_hour": 24,
        },
    ).json()
    reply = _got(client, db, smtp, campaign, "Let's talk.")
    client.post(f"/api/campaigns/{other['id']}/start")
    waiting = db.scalar(
        select(CampaignRecipient).where(
            CampaignRecipient.campaign_id == other["id"], CampaignRecipient.email == "sara@acme.com"
        )
    )
    # The 30-day rule already skips her; force the case "still waiting" to see the handover catch it.
    waiting.status, waiting.skip_reason = RecipientStatus.WAITING, None
    db.flush()
    r = client.post(f"/api/replies/{reply.id}/send-to-deals")
    assert r.status_code == 200
    deals_handover.run(db, DAY2)
    db.refresh(waiting)
    assert waiting.status == RecipientStatus.BLOCKED_BY_DEALS and waiting.skip_reason == "lead"
    # And a new campaign never adds her again.
    third = client.post(
        "/api/campaigns",
        json={
            "name": "Third",
            "subject": "Hi",
            "body": "Hello",
            "group_ids": [ready["import"]["group"]["id"]],
            "mailbox_ids": [ready["mailbox"]["id"]],
            "send_days": [0, 1, 2, 3, 4, 5, 6],
            "start_hour": 0,
            "end_hour": 24,
        },
    ).json()
    client.post(f"/api/campaigns/{third['id']}/start")
    sara = db.scalar(
        select(CampaignRecipient).where(
            CampaignRecipient.campaign_id == third["id"], CampaignRecipient.email == "sara@acme.com"
        )
    )
    assert sara.status == RecipientStatus.BLOCKED_BY_DEALS


def test_send_to_deals_by_hand_and_no_duplicates(client, admin, db, smtp, campaign, fake):
    _scoring(db)
    reply = _got(client, db, smtp, campaign, "ok")  # 10: low
    assert client.post(f"/api/replies/{reply.id}/send-to-deals").json()["detail"] == "deals_handover_off"
    _deals(db, handover_enabled=True)
    r = client.post(f"/api/replies/{reply.id}/send-to-deals").json()
    assert r["handover"]["band"] == "manual" and r["handover"]["approved_by"] == admin.name
    assert client.post(f"/api/replies/{reply.id}/send-to-deals").json()["detail"] == "already_sent_to_deals"
    deals_handover.run(db, DAY2)
    deals_handover.run(db, DAY2 + timedelta(minutes=5))
    assert len(fake.handovers) == 1
    assert fake.handovers[0]["score"]["approved_by"] == admin.name


def test_an_existing_lead_and_a_blocked_one(client, admin, db, smtp, campaign, fake):
    _deals(db, handover_enabled=True, optouts_enabled=True)
    fake.handover_status = "blocked"
    reply = _got(client, db, smtp, campaign, "Let's talk.")
    client.post(f"/api/replies/{reply.id}/send-to-deals")
    deals_handover.run(db, DAY2)
    handover = db.scalar(select(DealsHandover))
    assert handover.status == "blocked" and handover.deals_lead_id is None
    entry = db.scalar(select(DoNotContact).where(DoNotContact.value == "sara@acme.com"))
    assert entry.reason == "Opted out in Deals"
    # Not shared back to Deals: Deals told us.
    assert db.scalar(select(DealsOptout)) is None


def test_retries_follow_the_schedule(client, admin, db, smtp, campaign, fake):
    _deals(db, handover_enabled=True)
    reply = _got(client, db, smtp, campaign, "Let's talk.")
    client.post(f"/api/replies/{reply.id}/send-to-deals")
    fake.down = True
    at = DAY2
    for minutes in (1, 5, 15, 60):
        deals_handover.run(db, at)
        handover = db.scalar(select(DealsHandover))
        assert handover.next_attempt_at == at + timedelta(minutes=minutes)
        assert deals_handover.run(db, at + timedelta(seconds=30)) == 0  # not due yet
        at = handover.next_attempt_at
    fake.down = False
    deals_handover.run(db, at)
    bodies = {c.content for c in fake.calls("/handover")}
    assert len(bodies) == 1  # every try sent exactly the same body
    assert db.scalar(select(DealsHandover)).status == "created"


def test_a_later_reply_from_someone_handed_over_goes_as_repeat(client, admin, db, smtp, campaign, fake):
    _deals(db, handover_enabled=True)
    _scoring(db)
    first = _got(client, db, smtp, campaign, "Let's talk, call me Thursday?")
    deals_handover.run(db, DAY2)
    earlier = _reply(smtp.sent[0], "Let's talk, call me Thursday?")
    later = _reply(smtp.sent[0], "ok", message_id="<r2@acme.com>")
    inbox = FakeInbox(earlier, later)  # the inbox keeps its numbering: the later message is new
    replies.check_mailbox(db, campaign["mailbox_id"], DAY2 + timedelta(hours=1), inbox, lambda m: None)
    second = db.scalar(select(Reply).where(Reply.id != first.id))
    assert second is not None, "later replies from a handed-over person are still read"
    handover = db.scalar(select(DealsHandover).where(DealsHandover.reply_id == second.id))
    assert handover.band == "repeat" and second.review_status is None
    deals_handover.run(db, DAY2 + timedelta(hours=1))
    assert fake.handovers[-1]["score"]["band"] == "repeat"


# --- Step 5: results and reports ----------------------------------------------------------------------------


def _handed(client, db, smtp, campaign, fake) -> DealsHandover:
    _deals(db, handover_enabled=True, events_enabled=True)
    reply = _got(client, db, smtp, campaign, "Let's talk.")
    client.post(f"/api/replies/{reply.id}/send-to-deals")
    deals_handover.run(db, DAY2)
    return db.scalar(select(DealsHandover))


def test_results_come_back_in_order_once(client, admin, db, smtp, campaign, fake):
    handover = _handed(client, db, smtp, campaign, fake)
    rid = f"rpl_{handover.reply_id}"
    fake.events = [
        {
            "id": "e1",
            "type": "stage_changed",
            "lead_id": "lead_1",
            "reply_id": rid,
            "from": "new",
            "to": "proposal",
            "value_usd": None,
            "lost_reason": None,
            "at": "2026-12-01T10:00:00Z",
        },
        {
            "id": "e2",
            "type": "stage_changed",
            "lead_id": "lead_1",
            "reply_id": rid,
            "from": "proposal",
            "to": "won",
            "value_usd": 7000,
            "lost_reason": None,
            "at": "2026-12-15T10:30:00Z",
        },
    ]
    assert deals_events.sync(db, DAY2)["applied"] == 2
    result = db.get(DealsLeadResult, "lead_1")
    assert (result.stage, result.value_usd, result.handover_id) == ("won", 7000, handover.id)
    assert db.get(DealsSyncState, "events").cursor == "2"
    # The same events again change nothing; the cursor moves on.
    assert deals_events.sync(db, DAY2)["applied"] == 0
    (call,) = [c for c in fake.calls("/events") if c.url.params.get("after") == "2"]
    assert call.url.params["test"] == "1"
    detail = client.get(f"/api/replies/{handover.reply_id}").json()
    assert detail["handover"]["stage"] == "won" and detail["handover"]["value_usd"] == 7000


def test_merged_and_deleted_leads(client, admin, db, smtp, campaign, fake):
    handover = _handed(client, db, smtp, campaign, fake)
    fake.events = [
        {
            "id": "e1",
            "type": "stage_changed",
            "lead_id": "lead_1",
            "to": "discovery",
            "at": "2026-12-01T10:00:00Z",
        },
        {
            "id": "e2",
            "type": "lead_merged",
            "lead_id": "lead_1",
            "into_lead_id": "lead_9",
            "at": "2026-12-02T10:00:00Z",
        },
        {"id": "e3", "type": "lead_deleted", "lead_id": "lead_9", "at": "2026-12-03T10:00:00Z"},
    ]
    deals_events.sync(db, DAY2)
    db.refresh(handover)
    assert db.get(DealsLeadResult, "lead_1") is None
    assert db.get(DealsLeadResult, "lead_9").stage == "deleted" and handover.deals_lead_id == "lead_9"


def test_reports_add_up(client, admin, db, smtp, campaign, fake):
    handover = _handed(client, db, smtp, campaign, fake)
    fake.events = [
        {
            "id": "e1",
            "type": "stage_changed",
            "lead_id": "lead_1",
            "to": "won",
            "value_usd": 5000,
            "at": "2026-12-01T10:00:00Z",
        }
    ]
    deals_events.sync(db, DAY2)
    # Emails count by when they were sent, MQLs by when they were handed over (today, in this test).
    end = sending.now() + timedelta(days=1)
    report = deals_reports.compute(db, T - timedelta(days=1), end, "campaign")
    total = report["total"]
    assert (total["emails_sent"], total["mqls"], total["manual"] + total["approved"]) == (1, 1, 1)
    assert (total["won"], total["won_usd"], total["revenue_per_1000"]) == (1, 5000, 5_000_000.0)
    by_version = deals_reports.compute(db, T - timedelta(days=1), end, "version")
    assert by_version["rows"][0]["label"].startswith(
        ("Quick question for {{company", "Short question for {{company")
    )
    api = client.get("/api/reports/deals", params={"dimension": "lead_list"}).json()
    assert api["dimension"] == "lead_list"
    csv = client.get("/api/reports/deals/export", params={"dimension": "mailbox"})
    assert csv.status_code == 200 and "revenue per 1000 emails" in csv.text
    assert handover.status == "created"


def test_services_are_cached_for_an_hour(client, admin, db, fake):
    _deals(db)
    assert client.get("/api/deals/services").json()["services"] == [{"key": "vapt", "name": "VAPT"}]
    client.get("/api/deals/services")
    assert len(fake.calls("/services")) == 1


def test_status_counts_handovers(client, admin, db, smtp, campaign, fake):
    _handed(client, db, smtp, campaign, fake)
    status = client.get("/api/deals/status").json()
    assert status["handovers"] == {"created": 1}
    assert json.dumps(status)  # serialisable
