from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from backend.models import AuditLog, AuthSession, User
from backend.security import create_access_token
from backend.tests.conftest import PASSWORD, login


def test_health_is_public(client):
    assert client.get("/health").json()["status"] == "ok"


def test_login_success_returns_token_and_audits(client, users, db):
    r = client.post("/auth/login", json={"username": "ceo", "password": PASSWORD})
    assert r.status_code == 200
    assert r.json()["token_type"] == "bearer"
    assert db.scalar(select(AuditLog).where(AuditLog.action == "auth.login")) is not None


def test_login_is_case_insensitive_on_username(client, users):
    assert client.post("/auth/login", json={"username": "CEO", "password": PASSWORD}).status_code == 200


def test_wrong_password_and_unknown_user_look_identical(client, users):
    a = client.post("/auth/login", json={"username": "ceo", "password": "nope"})
    b = client.post("/auth/login", json={"username": "ghost", "password": "nope"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_failed_logins_are_audited(client, users, db):
    client.post("/auth/login", json={"username": "ceo", "password": "wrong"})
    assert db.scalar(select(AuditLog).where(AuditLog.action == "auth.login_failed")) is not None


def test_account_locks_after_repeated_failures(client, users, db):
    for _ in range(5):
        assert client.post("/auth/login", json={"username": "booker", "password": "bad"}).status_code == 401
    # even the correct password is refused while locked
    r = client.post("/auth/login", json={"username": "booker", "password": PASSWORD})
    assert r.status_code == 423
    assert db.scalar(select(AuditLog).where(AuditLog.action == "auth.account_locked")) is not None


def test_lock_expires(client, users, db):
    for _ in range(5):
        client.post("/auth/login", json={"username": "booker", "password": "bad"})
    u = db.scalar(select(User).where(User.username == "booker"))
    u.locked_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    assert client.post("/auth/login", json={"username": "booker", "password": PASSWORD}).status_code == 200


def test_inactive_user_cannot_login(client, users, db):
    u = db.scalar(select(User).where(User.username == "booker"))
    u.is_active = False
    db.commit()
    assert client.post("/auth/login", json={"username": "booker", "password": PASSWORD}).status_code == 401


def test_login_rate_limit(client, users):
    from backend.config import get_settings

    limit = get_settings().login_rate_limit_per_minute
    codes = [client.post("/auth/login", json={"username": "x", "password": "y"}).status_code for _ in range(limit + 3)]
    assert 429 in codes


def test_protected_endpoint_requires_token(client, users):
    assert client.get("/auth/me").status_code == 401
    assert client.get("/locations").status_code == 401
    assert client.get("/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_logout_revokes_token(client, users):
    h = login(client, "ceo")
    assert client.get("/auth/me", headers=h).status_code == 200
    assert client.post("/auth/logout", headers=h).status_code == 204
    assert client.get("/auth/me", headers=h).status_code == 401


def test_expired_session_rejected(client, users, db):
    h = login(client, "ceo")
    s = db.scalar(select(AuthSession))
    s.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    assert client.get("/auth/me", headers=h).status_code == 401


def test_token_without_server_session_rejected(client, users):
    token, _ = create_access_token(users["ceo"].id, "jti-that-was-never-issued")
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_deactivating_user_kills_existing_sessions(client, users):
    ceo_h = login(client, "ceo")
    booker_h = login(client, "booker")
    assert client.get("/auth/me", headers=booker_h).status_code == 200
    r = client.patch(f"/users/{users['booker'].id}", headers=ceo_h, json={"is_active": False, "reason": "left company"})
    assert r.status_code == 200
    assert client.get("/auth/me", headers=booker_h).status_code == 401


def test_me_returns_permissions_and_scope(client, users, locations):
    me = client.get("/auth/me", headers=login(client, "stock")).json()
    assert me["all_locations"] is False
    assert sorted(l["code"] for l in me["locations"]) == ["B1", "WH"]
    assert "inventory.receive" in me["permissions"]
    assert "accounting.post" not in me["permissions"]
    assert me["currency"] == "PKR"


def test_change_password_flow(client, users):
    h = login(client, "booker")
    bad = client.post("/auth/change-password", headers=h, json={"current_password": "wrong", "new_password": "NewPassw0rd!!"})
    assert bad.status_code == 422
    weak = client.post("/auth/change-password", headers=h, json={"current_password": PASSWORD, "new_password": "short"})
    assert weak.status_code == 422
    ok = client.post("/auth/change-password", headers=h, json={"current_password": PASSWORD, "new_password": "NewPassw0rd!!"})
    assert ok.status_code == 204
    assert client.get("/auth/me", headers=h).status_code == 401  # old sessions revoked
    assert client.post("/auth/login", json={"username": "booker", "password": PASSWORD}).status_code == 401
    assert client.post("/auth/login", json={"username": "booker", "password": "NewPassw0rd!!"}).status_code == 200


def test_passwords_are_hashed_not_stored(users, db):
    u = db.scalar(select(User).where(User.username == "ceo"))
    assert PASSWORD not in u.password_hash
    assert u.password_hash.startswith("$argon2")
