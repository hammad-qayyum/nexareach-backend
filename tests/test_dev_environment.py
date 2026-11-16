from email.message import EmailMessage

import pytest
from sqlalchemy import func, select

from app.cli import DEV_LEADS, DevOnlyError, apply_dev_seed
from app.core.config import get_settings
from app.models import ActivityLog, DoNotContact, Lead, Mailbox, SmtpSecurity, User
from app.services import mailer


@pytest.fixture
def development(monkeypatch):
    monkeypatch.setattr(get_settings(), "app_env", "development")


def _mailbox(host: str) -> Mailbox:
    return Mailbox(
        email="a@fastnexa.com",
        display_name="A",
        smtp_host=host,
        smtp_port=465,
        smtp_security=SmtpSecurity.SSL,
        smtp_username="a",
        smtp_password_encrypted="x",
    )


def test_development_block_only_allows_local_mail_servers(development):
    assert mailer.development_block(_mailbox("localhost")) is None
    assert mailer.development_block(_mailbox("Mailpit")) is None
    assert "only sends through Mailpit" in mailer.development_block(_mailbox("mail.fastnexa.com"))
    msg = EmailMessage()
    msg["To"] = "someone@example.com"
    with pytest.raises(mailer.SendFailed) as e:  # refused before any connection is made
        mailer.send(_mailbox("mail.fastnexa.com"), msg)
    assert e.value.temporary is False


def test_production_sends_normally():
    assert get_settings().app_env == "test"
    assert mailer.development_block(_mailbox("mail.fastnexa.com")) is None


def test_me_reports_the_environment(client, admin):
    assert client.get("/api/auth/me").json()["environment"] == "test"


def test_seed_dev_fills_an_empty_database(db, development):
    summary = apply_dev_seed(db)
    assert summary == {"users": 3, "mailboxes": 2, "leads": len(DEV_LEADS), "do_not_contact": 2}
    assert db.scalar(select(func.count()).select_from(User)) == 3
    assert {m.smtp_host for m in db.scalars(select(Mailbox))} == {"localhost"}
    ahmed = db.scalar(select(Lead).where(Lead.email == "ahmed@emaar-brokers.example.com"))
    assert ahmed.timezone == "Asia/Dubai" and ahmed.custom["property_type"] == "luxury villas"
    assert all(e.endswith(".example.com") for e in db.scalars(select(Lead.email)))
    assert db.scalar(select(func.count()).select_from(DoNotContact)) == 2
    assert db.scalar(select(ActivityLog).where(ActivityLog.action == "dev.seeded")) is not None

    with pytest.raises(DevOnlyError):  # only into an empty database
        apply_dev_seed(db)


def test_seed_dev_refuses_outside_development(db):
    with pytest.raises(DevOnlyError):
        apply_dev_seed(db)
