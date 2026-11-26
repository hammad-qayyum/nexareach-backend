"""A mailbox is only active once its connection has been tested and works."""

import pytest
from sqlalchemy import select

from app.api import mailboxes as mailboxes_api
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


class _Flaky:
    """A connection check that fails the first `fails` times with `error`, then works."""

    def __init__(self, error, fails=1, result=None):
        self.error, self.fails, self.result, self.calls = error, fails, result, 0

    def __call__(self, mailbox):
        self.calls += 1
        if self.calls <= self.fails:
            raise self.error
        return self.result


@pytest.fixture
def no_pause(monkeypatch):
    monkeypatch.setattr(mailboxes_api, "RETRY_PAUSE_SECONDS", 0)


def test_a_slow_server_gets_one_more_try(client, admin, monkeypatch, no_pause):
    timeout = mailer.SendFailed("No answer from the server (timed out).", temporary=True)
    login = _Flaky(timeout)
    monkeypatch.setattr(mailer, "check_login", login)
    r = client.post("/api/mailboxes", json=MAILBOX).json()
    assert login.calls == 2
    assert r["status"] == "ACTIVE"
    assert r["connection"]["detail"] == "Signed in to the SMTP server on the second try."


def test_a_server_down_twice_is_not_active(client, admin, monkeypatch, no_pause):
    login = _Flaky(mailer.SendFailed("Connection problem: reset", temporary=True), fails=5)
    monkeypatch.setattr(mailer, "check_login", login)
    r = client.post("/api/mailboxes", json=MAILBOX).json()
    assert login.calls == 2  # one more try, not more
    assert r["status"] == "ERROR" and "Connection problem" in r["last_error"]


@pytest.mark.parametrize(
    "error",
    [
        mailer.AuthFailed("535 Incorrect authentication data"),
        mailer.SendFailed("Secure connection failed. Check that Security matches the port.", temporary=False),
    ],
)
def test_a_wrong_password_or_setting_is_not_retried(client, admin, monkeypatch, no_pause, error):
    """Each extra failed login brings cPanel's brute-force block closer; a wrong port won't fix itself."""
    login = _Flaky(error)
    monkeypatch.setattr(mailer, "check_login", login)
    r = client.post("/api/mailboxes", json=MAILBOX).json()
    assert login.calls == 1 and r["status"] == "ERROR"


def test_a_slow_imap_server_gets_one_more_try(client, admin, monkeypatch, no_pause):
    imap = _Flaky(mailer.SentCopyFailed("Couldn't connect (timed out).", temporary=True), result="INBOX.Sent")
    monkeypatch.setattr(mailer, "check_imap", imap)
    r = client.post("/api/mailboxes", json={**MAILBOX, "imap_host": "mail.fastnexa.com"}).json()
    assert imap.calls == 2 and r["sent_folder"] == "INBOX.Sent" and r["status"] == "ACTIVE"
    # A rejected IMAP password isn't retried (and sending still works).
    imap = _Flaky(mailer.SentCopyFailed("The IMAP server rejected the username or password."))
    monkeypatch.setattr(mailer, "check_imap", imap)
    r = client.post(f"/api/mailboxes/{r['id']}/check").json()
    assert imap.calls == 1 and "Sent copy won't work" in r["detail"]
