import base64
import datetime
import os

# Configure before any app module reads settings.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://fastnexa:fastnexa@localhost:5433/fastnexa_test"
)
os.environ["ENCRYPTION_KEY"] = base64.b64encode(b"k" * 32).decode()
os.environ["APP_BASE_URL"] = "http://testserver"

from collections.abc import Iterator  # noqa: E402

import pyotp  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.crypto import encrypt  # noqa: E402
from app.core.db import get_db  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Role, User, UserStatus  # noqa: E402

PASSWORD = "correct-horse-battery"


@pytest.fixture(scope="session")
def engine():
    engine = create_engine(get_settings().database_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "migrations"))
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine) -> Iterator[Session]:
    """Each test runs inside a transaction that is rolled back afterwards.
    App code can still call commit(): it only releases a savepoint."""
    conn = engine.connect()
    outer = conn.begin()
    session = Session(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
    yield session
    session.close()
    outer.rollback()
    conn.close()


@pytest.fixture
def client(db) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app, headers={"X-Requested-With": "fastnexa"}) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def make_user(db):
    def make(
        email: str,
        role: Role = Role.MEMBER,
        status: UserStatus = UserStatus.ACTIVE,
        totp_secret: str | None = None,
    ) -> User:
        user = User(
            name=email.split("@")[0],
            email=email,
            password_hash=hash_password(PASSWORD),
            role=role,
            status=status,
            totp_enabled=totp_secret is not None,
            totp_secret_encrypted=encrypt(totp_secret) if totp_secret else None,
        )
        db.add(user)
        db.flush()
        return user

    return make


def totp_code(secret: str, offset: int = 0) -> str:
    """Code for the current 30-second step plus `offset` steps (the server accepts ±1)."""
    totp = pyotp.TOTP(secret)
    return totp.generate_otp(totp.timecode(datetime.datetime.now()) + offset)


@pytest.fixture
def login(client):
    def do_login(email: str, totp_secret: str | None = None) -> str:
        r = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        mfa = r.json()["mfa"]
        if mfa == "verify":
            r = client.post("/api/auth/2fa/verify", json={"code": totp_code(totp_secret)})
            assert r.status_code == 200, r.text
            mfa = r.json()["mfa"]
        return mfa

    return do_login


@pytest.fixture
def admin(make_user, login):
    """A signed-in admin with 2FA, as the current client session."""
    secret = pyotp.random_base32()
    user = make_user("admin@fastnexa.com", role=Role.ADMIN, totp_secret=secret)
    assert login(user.email, secret) == "passed"
    return user


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """No real DNS in tests: domains with "google"/"microsoft" in them are hosted there, others elsewhere."""
    from app.services import providers

    def mx_hosts(domain: str) -> list[str]:
        if "nomail" in domain:
            return []
        if "google" in domain:
            return ["aspmx.l.google.com"]
        if "microsoft" in domain:
            return [f"{domain.replace('.', '-')}.mail.protection.outlook.com"]
        return [f"mail.{domain}"]

    monkeypatch.setattr(providers, "mx_hosts", mx_hosts)


@pytest.fixture(autouse=True)
def fake_mail_login(monkeypatch):
    """No real mail servers in tests: every mailbox login works unless a test says otherwise."""
    from app.services import mailer

    # Keep the real IMAP check reachable for the test that exercises it against a fake IMAP server.
    monkeypatch.setattr(mailer, "real_check_imap", mailer.check_imap, raising=False)
    monkeypatch.setattr(mailer, "check_login", lambda mailbox: None)
    monkeypatch.setattr(mailer, "check_imap", lambda mailbox: "INBOX.Sent")
