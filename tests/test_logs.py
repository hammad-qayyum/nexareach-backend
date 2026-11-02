import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.crypto import decrypt, encrypt
from app.models import ActivityLog, Role
from app.services.audit import SYSTEM, log_activity


def test_logs_are_visible_to_every_role(client, make_user, login):
    make_user("viewer@fastnexa.com", role=Role.VIEWER)
    login("viewer@fastnexa.com")
    r = client.get("/api/logs")
    assert r.status_code == 200
    assert r.json()["total"] >= 1  # at least the viewer's own login


def test_logs_require_sign_in(client):
    assert client.get("/api/logs").status_code == 401


def test_filter_by_action_prefix_and_entity(client, admin, db):
    log_activity(db, SYSTEM, "campaign.started", entity_type="campaign", entity_id=7)
    log_activity(db, SYSTEM, "campaign.paused", entity_type="campaign", entity_id=7)
    log_activity(db, SYSTEM, "mailbox.added", entity_type="mailbox", entity_id=1)
    db.commit()

    items = client.get("/api/logs", params={"action": "campaign."}).json()["items"]
    assert {i["action"] for i in items} == {"campaign.started", "campaign.paused"}

    items = client.get("/api/logs", params={"entity_type": "mailbox", "entity_id": "1"}).json()["items"]
    assert [i["action"] for i in items] == ["mailbox.added"]


def test_export_is_csv_and_is_itself_logged(client, admin, db):
    r = client.get("/api/logs/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0].startswith("id,time,actor,action")
    assert db.scalar(select(ActivityLog).where(ActivityLog.action == "logs.exported")) is not None


def test_activity_log_cannot_be_changed_in_the_database(db):
    log_activity(db, SYSTEM, "test.entry")
    db.flush()
    with pytest.raises(DBAPIError, match="append-only"):
        with db.begin_nested():
            db.execute(text("UPDATE activity_logs SET action = 'tampered'"))
    with pytest.raises(DBAPIError, match="append-only"):
        with db.begin_nested():
            db.execute(text("DELETE FROM activity_logs"))


def test_encryption_round_trip():
    token = encrypt("mailbox-password")
    assert token.startswith("v1:")
    assert "mailbox-password" not in token
    assert decrypt(token) == "mailbox-password"
    assert encrypt("same") != encrypt("same")  # random nonce each time
