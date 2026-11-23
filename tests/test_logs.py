import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.crypto import decrypt, encrypt
from app.models import ActivityLog, Mailbox, MailboxStatus, Role, SmtpSecurity
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


def test_detailed_filters(client, admin, db):
    box = Mailbox(
        email="ali@fastnexa.com",
        display_name="Ali",
        smtp_host="mail.fastnexa.com",
        smtp_port=465,
        smtp_security=SmtpSecurity.SSL,
        smtp_username="ali@fastnexa.com",
        smtp_password_encrypted=encrypt("x"),
        status=MailboxStatus.ACTIVE,
    )
    db.add(box)
    db.flush()
    sent = {"campaign_id": 7, "mailbox": "ali@fastnexa.com", "message_id": "<a@b>"}
    log_activity(db, SYSTEM, "email.sent", entity_type="lead", entity_id=1, after=sent)
    log_activity(
        db,
        SYSTEM,
        "email.failed",
        entity_type="lead",
        entity_id=2,
        after={**sent, "error": "550 no such user"},
    )
    log_activity(
        db,
        SYSTEM,
        "email.sent",
        entity_type="lead",
        entity_id=3,
        after={"campaign_id": 8, "mailbox": "x@y.com"},
    )
    log_activity(db, SYSTEM, "campaign.started", entity_type="campaign", entity_id=7)
    log_activity(db, SYSTEM, "mailbox.login_failed", entity_type="mailbox", entity_id=box.id)
    db.commit()

    def actions(**params):
        params = {"date_from": "2000-01-01T00:00:00Z", **params}
        return sorted(i["action"] for i in client.get("/api/logs", params=params).json()["items"])

    assert actions(campaign_id=7) == ["campaign.started", "email.failed", "email.sent"]
    assert actions(campaign_id=[7, 8]) == ["campaign.started", "email.failed", "email.sent", "email.sent"]
    assert actions(mailbox_id=box.id) == ["email.failed", "email.sent", "mailbox.login_failed"]
    assert actions(problems="true", campaign_id=7) == ["email.failed"]
    assert actions(problems="true", mailbox_id=box.id) == ["email.failed", "mailbox.login_failed"]
    assert actions(action=["email.failed", "campaign."]) == ["campaign.started", "email.failed"]
    # Search looks inside the details too, case-insensitively, and treats % and _ literally.
    assert actions(q="550 NO SUCH") == ["email.failed"]
    assert actions(q="100%") == []
    assert actions(actor_type="SYSTEM", q="login_failed") == ["mailbox.login_failed"]
    assert "auth.login" in actions(actor_type="USER")
    assert "email.sent" not in actions(actor_type="USER")
    assert client.get("/api/logs", params={"actor_type": "ROBOT"}).status_code == 422


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
