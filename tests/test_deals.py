"""Deals integration, step 1: the do-not-email list (signed client, sync, three checks, fail closed).

A fake Deals server implements the contract (section 4 of the integration prompt), signature checks included;
nothing here touches the network."""

import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.crypto import encrypt
from app.models import (
    ActivityLog,
    Alert,
    AlertStatus,
    CampaignRecipient,
    DealsBlock,
    DealsSyncState,
    Lead,
    Mailbox,
    RecipientStatus,
    Role,
)
from app.services import deals, deals_client, sending, warmup
from app.services.app_settings import set_json
from app.services.deals_client import PREFIX, Client, DealsError, email_hash, sign
from tests.test_campaigns import MAILBOX, TUESDAY_11AM_PKT, ready, smtp  # noqa: F401 (shared fixtures)
from tests.test_warmup import _first_slot, _helper

SECRET = "deals-test-secret"
URL = "https://deals.example"
T = TUESDAY_11AM_PKT


class FakeDeals:
    """Deals' side of the contract, in memory."""

    def __init__(self):
        self.blocks: dict[str, dict] = {}
        self.changes: list[dict] = []  # what a delta returns, in order; a cursor is a position in it
        self.expired: set[str] = set()  # cursors too old: 410
        self.down = False
        self.page_size: int | None = None
        self.requests: list[httpx.Request] = []
        self.status: int | None = None  # answer every call with this status instead
        self.optouts: list[dict] = []  # what /optouts accepted
        self.keys: dict[str, bytes] = {}  # Idempotency-Key → body
        self.answers: dict[str, dict] = {}  # Idempotency-Key → the first answer
        self.handover_status = "created"
        self.handovers: list[dict] = []  # what /handover received
        self.events: list[dict] = []  # what /events hands out; a cursor is a position in it

    def block(self, email: str, reason: str = "lead", until: str | None = None) -> str:
        h = email_hash(email)
        self.blocks[h] = {"hash": h, "reason": reason, "until": until}
        self.changes.append({**self.blocks[h], "removed": False})
        return h

    def unblock(self, email: str) -> None:
        h = email_hash(email)
        self.blocks.pop(h, None)
        self.changes.append({"hash": h, "removed": True})

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == PREFIX + path]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        try:
            parts = dict(p.split("=", 1) for p in request.headers["X-Fastnexa-Signature"].split(","))
            t = int(parts["t"])
        except (KeyError, ValueError):
            return httpx.Response(401)
        expected = sign(SECRET, t, request.method, request.url.raw_path.decode(), request.content)
        if not hmac.compare_digest(expected, parts.get("v1", "")) or abs(time.time() - t) > 300:
            return httpx.Response(401)
        if self.down:
            return httpx.Response(503)
        if self.status:
            return httpx.Response(self.status, json={"error": "invalid", "fields": {"items.0.hash": "bad"}})
        assert request.headers["User-Agent"].startswith("NexaReach/")
        path = request.url.path.removeprefix(PREFIX)
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        if request.method == "POST":
            assert request.headers["Idempotency-Key"]
            body = json.loads(request.content)
            key = request.headers["Idempotency-Key"]
            if key in self.keys and self.keys[key] != request.content:
                return httpx.Response(409)
            first_time = key not in self.keys
            self.keys[key] = request.content
            if not first_time and key in self.answers:
                return httpx.Response(200, json=self.answers[key])
            if path == "/handover":
                self.handovers.append(body)
                n = len(self.handovers)
                answer = {"status": self.handover_status, "lead_id": None, "lead_url": None}
                if self.handover_status != "blocked":
                    answer = {
                        **answer,
                        "lead_id": f"lead_{n}",
                        "lead_url": f"https://deals.example/leads/lead_{n}",
                    }
                self.answers[key] = answer
                return httpx.Response(200, json=answer)
            if path == "/optouts":
                assert 1 <= len(body["items"]) <= 500
                if first_time:
                    self.optouts.extend(body["items"])
                return httpx.Response(200, json={"accepted": len(body["items"])})
            if path == "/check":
                today = datetime.now(UTC).isoformat()
                found = [self.blocks[h] for h in body["hashes"] if h in self.blocks]
                active = [b for b in found if b["until"] is None or b["until"] > today]
                return httpx.Response(200, json={"blocked": active})
        elif path == "/events":
            start = int(query.get("after", 0))
            size = int(query["limit"])
            items = self.events[start : start + size]
            end = start + len(items)
            return httpx.Response(
                200, json={"events": items, "cursor": str(end), "has_more": end < len(self.events)}
            )
        elif path == "/services":
            return httpx.Response(200, json={"services": [{"key": "vapt", "name": "VAPT"}]})
        elif path == "/blocklist" and query.get("mode") == "full":
            size = self.page_size or int(query["limit"])
            start = int(query.get("page_token", 0))
            items = sorted(self.blocks.values(), key=lambda i: i["hash"])
            more = start + size < len(items)
            return httpx.Response(
                200,
                json={
                    "full": True,
                    "items": items[start : start + size],
                    "next_page_token": str(start + size) if more else None,
                    "cursor": str(len(self.changes)),
                },
            )
        elif path == "/blocklist":
            since = query["since"]
            if since in self.expired:
                return httpx.Response(410)
            size = self.page_size or int(query["limit"])
            items = self.changes[int(since) : int(since) + size]
            end = int(since) + len(items)
            return httpx.Response(
                200,
                json={"full": False, "items": items, "cursor": str(end), "has_more": end < len(self.changes)},
            )
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch):
    server = FakeDeals()
    monkeypatch.setattr(deals_client, "transport", httpx.MockTransport(server.handle))
    return server


def _switch(db, on: bool = True, secret: str = SECRET) -> None:
    set_json(
        db,
        deals.SETTINGS_KEY,
        {"url": URL, "secret_encrypted": encrypt(secret), "blocklist_enabled": on, "stale_minutes": 30},
        None,
        secret=True,
    )
    db.flush()


@pytest.fixture
def deals_on(db):
    _switch(db)


def _logged(db, action: str) -> list[ActivityLog]:
    return list(db.scalars(select(ActivityLog).where(ActivityLog.action == action)))


def _table(db) -> dict[str, tuple[str, datetime | None]]:
    return {b.hash: (b.reason, b.until) for b in db.scalars(select(DealsBlock))}


# --- Signing, hashing, test mode ---------------------------------------------------------------------------


def test_signatures_match_the_contract_examples():
    body = b'{"hashes":["5d41402abc4b2a76b9719d911017c592"]}'
    assert (
        sign("test-secret-123", 1767225600, "POST", "/api/integrations/nexareach/check", body)
        == "e521acf314fd4ac582bac147af75324c9a26f44174ab366b1ce4735dbba6ea79"
    )
    assert (
        sign("test-secret-123", 1767225600, "GET", "/api/integrations/nexareach/blocklist?since=abc123", b"")
        == "8b7ccc6b0e3c99215e102a60b8018ee829de394091ab3cc0f6560ab9d94ebbd2"
    )


def test_the_hash_matches_the_contract_in_python_and_in_the_database(db):
    expected = "c7c83aad71bdef49f5c119bcddb898159d9169836a39e294177f5088c469933f"
    assert email_hash("  Jane.Doe@ACME.co.uk ") == expected
    lead = Lead(email="jane.doe@acme.co.uk", custom={})
    db.add(lead)
    db.flush()
    db.refresh(lead)
    assert lead.email_hash == expected


def test_deals_refuses_a_wrong_secret_or_an_old_signature(fake):
    assert Client(URL, SECRET).get("/services")["services"][0]["key"] == "vapt"
    for client in (Client(URL, "wrong-secret"), Client(URL, SECRET, clock=lambda: time.time() - 301)):
        with pytest.raises(DealsError) as e:
            client.get("/services")
        assert e.value.kind == "signature" and not e.value.retryable


def test_outside_production_every_call_is_a_test_call(fake, monkeypatch):
    Client(URL, SECRET).get("/services")
    Client(URL, SECRET).post("/check", {"hashes": [email_hash("a@b.com")]}, "check:1")
    get, post = fake.requests
    assert get.url.params["test"] == "1"
    assert json.loads(post.content)["test"] is True
    # In production only an explicit test call (the Test connection button) is one.
    monkeypatch.setattr(get_settings(), "app_env", "production")
    Client(URL, SECRET).get("/services")
    Client(URL, SECRET).post("/check", {"hashes": [email_hash("a@b.com")]}, "check:2")
    Client(URL, SECRET, test=True).get("/services")
    get, post, explicit = fake.requests[2:]
    assert "test" not in get.url.params and json.loads(post.content)["test"] is False
    assert explicit.url.params["test"] == "1"


def test_deals_errors_say_what_to_do(fake):
    fake.down = True
    with pytest.raises(DealsError) as e:
        Client(URL, SECRET).get("/services")
    assert e.value.kind == "unavailable" and e.value.retryable


# --- The local copy -----------------------------------------------------------------------------------------


def test_a_full_snapshot_pages_through_and_replaces_the_table(db, deals_on, fake):
    db.add(DealsBlock(hash="0" * 64, reason="lead"))  # gone from Deals since: must disappear
    for i in range(5):
        fake.block(f"person{i}@acme.com")
    fake.page_size = 2
    assert deals.sync_blocklist(db, T)["mode"] == "full"
    assert set(_table(db)) == {email_hash(f"person{i}@acme.com") for i in range(5)}
    assert len(fake.calls("/blocklist")) == 3
    state = db.get(DealsSyncState, "blocklist")
    assert state.cursor == "5" and state.last_full_at == T and state.last_success_at == T


def test_only_one_sync_runs_at_a_time(db, deals_on, fake):
    from sqlalchemy import text

    from app.core.db import engine

    with engine.connect() as other:
        other.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": deals._LOCK_KEY})
        assert deals.sync_blocklist(db, T) == {"ok": False, "detail": "Another sync is running."}
        other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": deals._LOCK_KEY})
    assert deals.sync_blocklist(db, T)["ok"] is True


def test_a_delta_adds_changes_and_removes(db, deals_on, fake):
    fake.block("stays@acme.com")
    fake.block("leaves@acme.com")
    deals.sync_blocklist(db, T)
    fake.unblock("leaves@acme.com")
    fake.block("new@acme.com", "lost", "2027-05-24T10:00:00Z")
    fake.block("stays@acme.com", "client")
    fake.page_size = 1  # has_more: fetched again straight away
    assert deals.sync_blocklist(db, T + timedelta(minutes=10))["mode"] == "delta"
    assert _table(db) == {
        email_hash("stays@acme.com"): ("client", None),
        email_hash("new@acme.com"): ("lost", datetime(2027, 5, 24, 10, tzinfo=UTC)),
    }
    assert db.get(DealsSyncState, "blocklist").cursor == str(len(fake.changes))


def test_a_cursor_that_is_too_old_means_a_full_snapshot(db, deals_on, fake):
    fake.block("a@acme.com")
    deals.sync_blocklist(db, T)
    fake.expired.add(db.get(DealsSyncState, "blocklist").cursor)
    fake.block("b@acme.com")
    assert deals.sync_blocklist(db, T + timedelta(minutes=10))["mode"] == "full"
    assert len(_table(db)) == 2


def test_a_lost_lead_is_blocked_only_until_its_date(db):
    db.add_all(
        [
            DealsBlock(hash=email_hash("past@acme.com"), reason="lost", until=T - timedelta(days=1)),
            DealsBlock(hash=email_hash("future@acme.com"), reason="lost", until=T + timedelta(days=1)),
            DealsBlock(hash=email_hash("client@acme.com"), reason="client"),
        ]
    )
    db.flush()
    assert deals.block_for(db, "past@acme.com", T) is None
    assert deals.block_for(db, "FUTURE@acme.com ", T)[0] == "lost"
    assert deals.block_for(db, "client@acme.com", T) == ("client", None)


def test_a_bad_signature_opens_a_critical_alert_and_a_good_sync_closes_it(db, fake):
    _switch(db, secret="wrong-secret")
    assert deals.sync_blocklist(db, T)["ok"] is False
    alert = db.scalar(select(Alert).where(Alert.kind == "deals_signature"))
    assert alert.status == AlertStatus.OPEN and "clock and the secret" in alert.message
    assert "signature" in db.get(DealsSyncState, "blocklist").last_error
    _switch(db)
    assert deals.sync_blocklist(db, T)["ok"] is True
    db.refresh(alert)
    assert alert.status == AlertStatus.RESOLVED
    assert db.get(DealsSyncState, "blocklist").last_error is None


# --- Check 1: import ----------------------------------------------------------------------------------------


def test_import_marks_people_on_the_list(client, admin, db, fake):
    rows = [{"email": "free@acme.com"}, {"email": "lee@blocked.com"}, {"email": "ex@lost.com"}]
    # Switched off: nothing about Deals is shown.
    off = client.post("/api/groups/import", json={"name": "Off", "rows": [{"email": "x@acme.com"}]}).json()
    assert off["blocked_by_deals"] is None

    _switch(db)
    fake.block("lee@blocked.com", "client")
    fake.block("ex@lost.com", "lost", "2099-01-01T00:00:00Z")
    deals.sync_blocklist(db, datetime.now(UTC))
    result = client.post("/api/groups/import", json={"name": "On", "rows": rows}).json()
    assert result["blocked_by_deals"] == 2
    gid = result["group"]["id"]
    leads = {x["email"]: x["deals_block"] for x in client.get(f"/api/groups/{gid}/leads").json()["items"]}
    assert leads["lee@blocked.com"] == {"reason": "client", "until": None}
    assert leads["ex@lost.com"]["reason"] == "lost" and leads["free@acme.com"] is None
    only_blocked = client.get(f"/api/groups/{gid}/leads", params={"deals_blocked": True}).json()
    assert only_blocked["total"] == 2
    assert client.get(f"/api/groups/{gid}/leads", params={"deals_blocked": False}).json()["total"] == 1
    assert client.get(f"/api/groups/{gid}/facets").json()["deals_blocked"] == 2


# --- Check 2: campaign Start --------------------------------------------------------------------------------


def test_start_asks_deals_and_never_emails_blocked_people(client, ready, db, deals_on, fake):
    # Only Deals knows yet (the local copy hasn't synced): Start still catches it.
    fake.block("omar@globex.com", "meta_lead")
    r = client.post(f"/api/campaigns/{ready['campaign']['id']}/start")
    assert r.status_code == 200
    assert r.json()["counts"]["blocked_by_deals"] == 1 and r.json()["counts"]["waiting"] == 2
    omar = db.scalar(select(CampaignRecipient).where(CampaignRecipient.email == "omar@globex.com"))
    assert omar.status == RecipientStatus.BLOCKED_BY_DEALS and omar.skip_reason == "meta_lead"
    (check,) = fake.calls("/check")
    assert email_hash("omar@globex.com") in json.loads(check.content)["hashes"]
    assert check.headers["Idempotency-Key"].startswith("check:")
    blocked = client.get(
        f"/api/campaigns/{ready['campaign']['id']}/recipients", params={"status": "BLOCKED_BY_DEALS"}
    ).json()
    assert blocked["total"] == 1


def test_start_fails_closed_when_deals_cannot_be_asked(client, ready, db, deals_on, fake):
    fake.down = True
    r = client.post(f"/api/campaigns/{ready['campaign']['id']}/start")
    assert r.status_code == 503 and r.json()["detail"] == "deals_check_failed"
    assert client.get(f"/api/campaigns/{ready['campaign']['id']}").json()["status"] == "DRAFT"
    assert db.scalar(select(CampaignRecipient)) is None


def test_start_is_unchanged_with_the_switch_off(client, ready, fake):
    r = client.post(f"/api/campaigns/{ready['campaign']['id']}/start")
    assert r.status_code == 200 and "blocked_by_deals" not in r.json()["counts"]
    assert fake.requests == []


# --- Check 3: every send ------------------------------------------------------------------------------------


def test_a_send_skips_someone_blocked_since_start_and_moves_on(client, ready, db, deals_on, fake, smtp):
    client.post(f"/api/campaigns/{ready['campaign']['id']}/start")
    fake.block("sara@acme.com", "opted_out")
    deals.sync_blocklist(db, T)
    mailbox_id = ready["mailbox"]["id"]
    assert sending.send_next(db, mailbox_id, T + timedelta(minutes=1), smtp) == "sent"
    assert [m["To"] for m in smtp.sent] == ["omar@globex.com"]  # the same turn went on to the next person
    sara = db.scalar(select(CampaignRecipient).where(CampaignRecipient.email == "sara@acme.com"))
    assert sara.status == RecipientStatus.BLOCKED_BY_DEALS and sara.skip_reason == "opted_out"
    (log,) = _logged(db, "email.blocked_by_deals")
    assert log.after["reason"] == "opted_out" and log.after["follow_up"] is False


def test_a_blocked_follow_up_stops_but_the_person_stays_sent(client, admin, db, deals_on, fake, smtp):
    mailbox = client.post("/api/mailboxes", json={**MAILBOX, "ramp_up_enabled": False}).json()
    group = client.post("/api/groups/import", json={"name": "G", "rows": [{"email": "sara@acme.com"}]}).json()
    campaign = client.post(
        "/api/campaigns",
        json={
            "name": "C",
            "subject": "Hi",
            "body": "Hello",
            "group_ids": [group["group"]["id"]],
            "mailbox_ids": [mailbox["id"]],
            "send_days": [0, 1, 2, 3, 4, 5, 6],
            "start_hour": 0,
            "end_hour": 24,
            "follow_ups_enabled": True,
            "follow_ups": [{"delay_days": 1, "body": "Just checking."}],
        },
    ).json()
    client.post(f"/api/campaigns/{campaign['id']}/start")
    deals.sync_blocklist(db, T)
    assert sending.send_next(db, mailbox["id"], T + timedelta(minutes=1), smtp) == "sent"
    fake.block("sara@acme.com", "lead")
    later = T + timedelta(days=1, minutes=5)
    deals.sync_blocklist(db, later)
    assert sending.send_next(db, mailbox["id"], later, smtp) == "idle"
    assert len(smtp.sent) == 1
    sara = db.scalar(select(CampaignRecipient))
    assert sara.status == RecipientStatus.SENT
    assert sara.follow_up_stop == "blocked_by_deals" and sara.next_follow_up_at is None


# --- Fail closed --------------------------------------------------------------------------------------------


def test_a_stale_list_holds_campaigns_but_not_warmup_and_alerts_once(client, ready, db, deals_on, fake, smtp):
    client.post(f"/api/campaigns/{ready['campaign']['id']}/start")
    mailbox = db.get(Mailbox, ready["mailbox"]["id"])
    mailbox.warmup_enabled = True
    _helper(db)
    db.flush()
    slot = _first_slot(mailbox.id, T)

    # Never synced counts as stale.
    assert sending.send_next(db, mailbox.id, slot - timedelta(minutes=1), smtp) == "deals_stale"
    assert sending.send_next(db, mailbox.id, slot, smtp) == "warmup_sent"
    assert all(warmup.is_warmup(m["Message-ID"]) for m in smtp.sent)

    deals.check_stale(db, slot)
    deals.check_stale(db, slot + timedelta(minutes=1))
    (alert,) = db.scalars(select(Alert).where(Alert.kind == "deals_stale"))
    assert alert.status == AlertStatus.OPEN and "never fetched" in alert.message
    assert len(_logged(db, "deals.sending_held")) == 1

    # A good sync closes it and sending carries on by itself.
    mailbox.warmup_enabled = False  # so the next warm-up slot doesn't go first
    later = slot + timedelta(minutes=30)
    deals.sync_blocklist(db, later)
    db.refresh(alert)
    assert alert.status == AlertStatus.RESOLVED
    assert len(_logged(db, "deals.sending_resumed")) == 1
    assert sending.send_next(db, mailbox.id, later, smtp) == "sent"

    # Older than 30 minutes again: held.
    assert sending.send_next(db, mailbox.id, later + timedelta(minutes=31), smtp) == "deals_stale"


def test_switching_the_list_off_releases_held_emails(client, ready, db, deals_on, fake, smtp):
    client.post(f"/api/campaigns/{ready['campaign']['id']}/start")
    deals.check_stale(db, datetime.now(UTC))
    assert db.scalar(select(Alert).where(Alert.kind == "deals_stale")).status == AlertStatus.OPEN
    _switch(db, on=False)
    deals.check_stale(db, datetime.now(UTC))
    assert db.scalar(select(Alert).where(Alert.kind == "deals_stale")).status == AlertStatus.RESOLVED
    assert sending.send_next(db, ready["mailbox"]["id"], T, smtp) == "sent"


# --- Settings page ------------------------------------------------------------------------------------------


def test_settings_are_for_admins_and_the_secret_is_write_only(client, admin, db, fake, make_user, login):
    assert client.put("/api/settings/deals", json={"blocklist_enabled": True}).json()["detail"] == (
        "deals_secret_required"
    )
    r = client.put("/api/settings/deals", json={"url": URL, "secret": SECRET, "stale_minutes": 20})
    body = r.json()
    assert r.status_code == 200 and body["secret_set"] is True and body["secret_set_at"]
    assert SECRET not in r.text and SECRET not in client.get("/api/settings/deals").text
    (log,) = _logged(db, "settings.deals_updated")
    assert log.after["secret_changed"] is True and SECRET not in json.dumps(log.after)
    # Saving again without a secret keeps it.
    assert client.put("/api/settings/deals", json={"url": URL}).json()["secret_set"] is True
    assert client.put("/api/settings/deals", json={"url": "http://deals.example"}).status_code == 400

    test = client.post("/api/settings/deals/test").json()
    assert test["ok"] is True and "1 service" in test["detail"]
    assert fake.calls("/services")[-1].url.params["test"] == "1"
    assert client.get("/api/deals/status").json()["test_mode"] is True

    make_user("member@fastnexa.com", role=Role.MEMBER)
    login("member@fastnexa.com")
    assert client.get("/api/settings/deals").status_code == 403
    assert client.put("/api/settings/deals", json={"url": URL}).status_code == 403
    assert client.get("/api/deals/status").status_code == 403


def test_sync_now_and_status(client, admin, db, deals_on, fake):
    fake.block("a@acme.com")
    r = client.post("/api/deals/blocklist/sync").json()
    assert r["ok"] is True and "1 person" in r["detail"]
    # The lock is released after each sync, so the next one runs too.
    assert client.post("/api/deals/blocklist/sync").json()["ok"] is True
    status = client.get("/api/deals/status").json()
    assert status["blocklist"]["active_blocks"] == 1 and status["campaigns_held"] is False
    assert status["blocklist"]["last_full_at"]
