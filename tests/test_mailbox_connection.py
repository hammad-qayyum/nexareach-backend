"""A mailbox is only active once its connection has been tested and works."""

from sqlalchemy import select

from app.models import ActivityLog
from app.services import mailer
from tests.test_campaigns import MAILBOX


def _reject(mailbox):
    raise mailer.AuthFailed("535 Incorrect authentication data")


def test_new_mailbox_is_active_only_after_a_passing_test(client, admin, db):
    r = client.post("/api/mailboxes", json={**MAILBOX, "imap_host": "mail.fastnexa.com"}).json()
    assert r["status"] == "ACTIVE"
    assert r["connection"]["ok"] is True
    assert r["sent_folder"] == "INBOX.Sent"
    assert r["last_error"] is None
    log = db.scalar(select(ActivityLog).where(ActivityLog.action == "mailbox.checked"))
    assert log.before == {"status": "ERROR"} and log.after["status"] == "ACTIVE"


def test_new_mailbox_with_a_bad_password_is_not_active(client, admin, monkeypatch):
    monkeypatch.setattr(mailer, "check_login", _reject)
    r = client.post("/api/mailboxes", json=MAILBOX).json()
    assert r["status"] == "ERROR"
    assert r["connection"]["ok"] is False
    assert "rejected the username or password" in r["last_error"]


def test_fixing_the_password_retests_and_activates(client, admin, monkeypatch):
    monkeypatch.setattr(mailer, "check_login", _reject)
    mailbox = client.post("/api/mailboxes", json=MAILBOX).json()

    still_bad = client.patch(f"/api/mailboxes/{mailbox['id']}", json={"smtp_password": "wrong-again"}).json()
    assert still_bad["status"] == "ERROR" and still_bad["connection"]["ok"] is False

    monkeypatch.setattr(mailer, "check_login", lambda m: None)
    fixed = client.patch(f"/api/mailboxes/{mailbox['id']}", json={"smtp_password": "right"}).json()
    assert fixed["status"] == "ACTIVE" and fixed["last_error"] is None


def test_reactivating_requires_a_passing_test(client, admin, monkeypatch):
    mailbox = client.post("/api/mailboxes", json=MAILBOX).json()
    client.patch(f"/api/mailboxes/{mailbox['id']}", json={"status": "PAUSED"})

    monkeypatch.setattr(mailer, "check_login", _reject)
    r = client.patch(f"/api/mailboxes/{mailbox['id']}", json={"status": "ACTIVE"}).json()
    assert r["status"] == "ERROR" and r["connection"]["ok"] is False

    monkeypatch.setattr(mailer, "check_login", lambda m: None)
    r = client.patch(f"/api/mailboxes/{mailbox['id']}", json={"status": "ACTIVE"}).json()
    assert r["status"] == "ACTIVE"


def test_paused_mailbox_stays_paused_when_tested(client, admin, monkeypatch):
    mailbox = client.post("/api/mailboxes", json=MAILBOX).json()
    client.patch(f"/api/mailboxes/{mailbox['id']}", json={"status": "PAUSED"})
    assert client.post(f"/api/mailboxes/{mailbox['id']}/check").json()["ok"] is True
    assert client.get("/api/mailboxes").json()[0]["status"] == "PAUSED"

    # Editing a paused mailbox's name doesn't need a test and doesn't change its status.
    r = client.patch(f"/api/mailboxes/{mailbox['id']}", json={"display_name": "Ali K"}).json()
    assert r["status"] == "PAUSED" and r["connection"] is None


def test_a_failing_test_stops_an_active_mailbox(client, admin, monkeypatch):
    mailbox = client.post("/api/mailboxes", json=MAILBOX).json()
    monkeypatch.setattr(mailer, "check_login", _reject)
    assert client.post(f"/api/mailboxes/{mailbox['id']}/check").json()["ok"] is False
    assert client.get("/api/mailboxes").json()[0]["status"] == "ERROR"


def test_sent_copy_problem_does_not_block_sending(client, admin, monkeypatch):
    def no_imap(mailbox):
        raise mailer.SentCopyFailed("No Sent folder found in this mailbox.")

    monkeypatch.setattr(mailer, "check_imap", no_imap)
    r = client.post("/api/mailboxes", json={**MAILBOX, "imap_host": "mail.fastnexa.com"}).json()
    assert r["status"] == "ACTIVE"
    assert "Sent copy won't work" in r["connection"]["detail"]
