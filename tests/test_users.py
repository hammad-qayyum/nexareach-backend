from sqlalchemy import select

from app.models import ActivityLog, Role, User, UserStatus
from tests.conftest import PASSWORD


def test_invite_and_accept(client, admin, db):
    r = client.post(
        "/api/users/invite", json={"name": "Sara", "email": "Sara@FastNexa.com", "role": "MEMBER"}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["user"]["email"] == "sara@fastnexa.com"
    assert body["user"]["status"] == "INVITED"
    token = body["invite_url"].rsplit("/", 1)[1]

    client.post("/api/auth/logout")
    assert client.get(f"/api/auth/invites/{token}").json() == {"name": "Sara", "email": "sara@fastnexa.com"}

    assert client.post(f"/api/auth/invites/{token}/accept", json={"password": "short"}).status_code == 422
    assert client.post(f"/api/auth/invites/{token}/accept", json={"password": PASSWORD}).status_code == 204

    # The link only works once.
    assert client.get(f"/api/auth/invites/{token}").status_code == 404

    r = client.post("/api/auth/login", json={"email": "sara@fastnexa.com", "password": PASSWORD})
    assert r.json() == {"mfa": "passed"}

    logged = set(db.scalars(select(ActivityLog.action)))
    assert {"user.invited", "user.invite_accepted"} <= logged


def test_duplicate_invite_is_rejected(client, admin):
    payload = {"name": "Sara", "email": "sara@fastnexa.com", "role": "MEMBER"}
    assert client.post("/api/users/invite", json=payload).status_code == 201
    assert client.post("/api/users/invite", json=payload).status_code == 409


def test_resending_invite_disables_the_old_link(client, admin):
    r = client.post(
        "/api/users/invite", json={"name": "Sara", "email": "sara@fastnexa.com", "role": "MEMBER"}
    )
    user_id = r.json()["user"]["id"]
    old_token = r.json()["invite_url"].rsplit("/", 1)[1]

    new_url = client.post(f"/api/users/{user_id}/resend-invite").json()["invite_url"]
    assert client.get(f"/api/auth/invites/{old_token}").status_code == 404
    assert client.get(f"/api/auth/invites/{new_url.rsplit('/', 1)[1]}").status_code == 200


def test_only_admins_manage_users(client, make_user, login):
    make_user("member@fastnexa.com", role=Role.MEMBER)
    login("member@fastnexa.com")
    assert client.get("/api/users").status_code == 403
    r = client.post("/api/users/invite", json={"name": "X", "email": "x@fastnexa.com", "role": "ADMIN"})
    assert r.status_code == 403


def test_viewer_cannot_manage_users(client, make_user, login):
    make_user("viewer@fastnexa.com", role=Role.VIEWER)
    login("viewer@fastnexa.com")
    assert client.get("/api/users").status_code == 403


def test_change_role_is_logged_with_before_and_after(client, admin, make_user, db):
    member = make_user("member@fastnexa.com")
    r = client.patch(f"/api/users/{member.id}", json={"role": "VIEWER"})
    assert r.json()["role"] == "VIEWER"

    entry = db.scalar(select(ActivityLog).where(ActivityLog.action == "user.updated"))
    assert entry.actor_user_id == admin.id
    assert entry.before == {"role": "MEMBER"}
    assert entry.after == {"role": "VIEWER"}


def test_admin_cannot_change_own_role_or_status(client, admin):
    assert client.patch(f"/api/users/{admin.id}", json={"role": "MEMBER"}).status_code == 400
    assert client.patch(f"/api/users/{admin.id}", json={"status": "PAUSED"}).status_code == 400


def test_pausing_a_user_signs_them_out(client, make_user, login, admin, db):
    member = make_user("member@fastnexa.com")

    # Sign the member in on a second client.
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, headers={"X-Requested-With": "fastnexa"}) as member_client:
        member_client.post("/api/auth/login", json={"email": member.email, "password": PASSWORD})
        assert member_client.get("/api/auth/me").status_code == 200

        assert client.patch(f"/api/users/{member.id}", json={"status": "PAUSED"}).status_code == 200
        assert member_client.get("/api/auth/me").status_code == 401

    assert db.get(User, member.id).status == UserStatus.PAUSED


def test_reset_2fa(client, admin, make_user):
    other = make_user("other-admin@fastnexa.com", role=Role.ADMIN, totp_secret="JBSWY3DPEHPK3PXP")
    r = client.post(f"/api/users/{other.id}/reset-2fa")
    assert r.json()["totp_enabled"] is False
    assert client.post(f"/api/users/{admin.id}/reset-2fa").status_code == 400
