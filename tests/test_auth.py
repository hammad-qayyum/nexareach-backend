import pyotp
from sqlalchemy import select

from app.models import ActivityLog, Role, UserStatus
from tests.conftest import PASSWORD, totp_code


def actions(db) -> list[str]:
    return list(db.scalars(select(ActivityLog.action).order_by(ActivityLog.id)))


def test_member_login_without_2fa(client, make_user, login, db):
    make_user("member@fastnexa.com")
    assert login("member@fastnexa.com") == "passed"

    me = client.get("/api/auth/me").json()
    assert me["user"]["email"] == "member@fastnexa.com"
    assert me["mfa"] == "passed"
    assert "auth.login" in actions(db)


def test_email_is_case_and_space_insensitive(client, make_user):
    make_user("member@fastnexa.com")
    r = client.post("/api/auth/login", json={"email": "  Member@FastNexa.com ", "password": PASSWORD})
    assert r.status_code == 200


def test_wrong_password_is_rejected_and_logged(client, make_user, db):
    make_user("member@fastnexa.com")
    r = client.post("/api/auth/login", json={"email": "member@fastnexa.com", "password": "wrong-password"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_credentials"

    entry = db.scalar(select(ActivityLog).where(ActivityLog.action == "auth.login_failed"))
    assert entry.actor_label == "member@fastnexa.com"
    assert entry.after == {"reason": "bad_password"}


def test_unknown_email_gives_same_error(client):
    r = client.post("/api/auth/login", json={"email": "nobody@fastnexa.com", "password": "whatever-123"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_credentials"


def test_paused_user_cannot_log_in(client, make_user):
    make_user("member@fastnexa.com", status=UserStatus.PAUSED)
    r = client.post("/api/auth/login", json={"email": "member@fastnexa.com", "password": PASSWORD})
    assert r.status_code == 401


def test_lockout_after_repeated_failures(client, make_user):
    make_user("member@fastnexa.com")
    for _ in range(5):
        client.post("/api/auth/login", json={"email": "member@fastnexa.com", "password": "wrong-password"})
    # Even the right password is refused while locked out.
    r = client.post("/api/auth/login", json={"email": "member@fastnexa.com", "password": PASSWORD})
    assert r.status_code == 429


def test_admin_must_set_up_2fa_before_using_the_app(client, make_user, db):
    make_user("admin@fastnexa.com", role=Role.ADMIN)
    r = client.post("/api/auth/login", json={"email": "admin@fastnexa.com", "password": PASSWORD})
    assert r.json() == {"mfa": "setup"}

    # Password alone isn't enough.
    assert client.get("/api/users").json()["detail"] == "mfa_required"

    secret = client.post("/api/auth/2fa/setup").json()["secret"]
    assert client.post("/api/auth/2fa/enable", json={"code": "000000"}).status_code == 400
    r = client.post("/api/auth/2fa/enable", json={"code": totp_code(secret)})
    assert r.json() == {"mfa": "passed"}

    assert client.get("/api/users").status_code == 200
    assert "auth.2fa_enabled" in actions(db)


def test_admin_with_2fa_needs_a_valid_code(client, make_user):
    secret = pyotp.random_base32()
    make_user("admin@fastnexa.com", role=Role.ADMIN, totp_secret=secret)
    r = client.post("/api/auth/login", json={"email": "admin@fastnexa.com", "password": PASSWORD})
    assert r.json() == {"mfa": "verify"}

    assert client.post("/api/auth/2fa/verify", json={"code": "123456"}).status_code == 400
    assert client.post("/api/auth/2fa/verify", json={"code": totp_code(secret)}).json() == {"mfa": "passed"}


def test_2fa_code_cannot_be_reused(client, make_user):
    secret = pyotp.random_base32()
    make_user("admin@fastnexa.com", role=Role.ADMIN, totp_secret=secret)
    code = totp_code(secret)

    client.post("/api/auth/login", json={"email": "admin@fastnexa.com", "password": PASSWORD})
    assert client.post("/api/auth/2fa/verify", json={"code": code}).status_code == 200
    client.post("/api/auth/logout")

    client.post("/api/auth/login", json={"email": "admin@fastnexa.com", "password": PASSWORD})
    assert client.post("/api/auth/2fa/verify", json={"code": code}).status_code == 400


def test_logout_ends_the_session(client, make_user, login):
    make_user("member@fastnexa.com")
    login("member@fastnexa.com")
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_session_cookie_is_http_only(client, make_user):
    make_user("member@fastnexa.com")
    r = client.post("/api/auth/login", json={"email": "member@fastnexa.com", "password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_state_changing_request_without_csrf_header_is_blocked(client):
    r = client.post(
        "/api/auth/login",
        json={"email": "a@fastnexa.com", "password": "x"},
        headers={"X-Requested-With": ""},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "csrf_header_missing"
