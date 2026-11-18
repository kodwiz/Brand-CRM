from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.models import AuthSession, User
from backend.security import create_access_token, hash_password, new_jti, password_problems, verify_password
from backend.services import audit_service
from backend.services.errors import BusinessRuleError


class AuthenticationError(Exception):
    """Generic on purpose: never reveal whether the username or password was wrong."""


class AccountLockedError(Exception):
    def __init__(self, until: datetime):
        self.until = until


def _now() -> datetime:
    return datetime.now(timezone.utc)


def authenticate(db: Session, *, username: str, password: str, ip: str | None, user_agent: str | None) -> tuple[User, str, datetime]:
    s = get_settings()
    uname = username.strip().lower()
    user = db.scalar(select(User).where(User.username == uname))

    if user is not None and user.locked_until and user.locked_until > _now():
        audit_service.record(db, actor=None, actor_username=uname, action="auth.login_blocked_locked",
                             entity_type="user", entity_id=user.id, ip=ip)
        db.commit()
        raise AccountLockedError(user.locked_until)

    ok = verify_password(password, user.password_hash if user else None)
    if user is None or not ok or not user.is_active or not user.role.is_active:
        if user is not None:
            user.failed_login_count += 1
            if user.failed_login_count >= s.max_failed_logins:
                user.locked_until = _now() + timedelta(minutes=s.lockout_minutes)
                user.failed_login_count = 0
                audit_service.record(db, actor=None, actor_username=uname, action="auth.account_locked",
                                     entity_type="user", entity_id=user.id, ip=ip)
        audit_service.record(db, actor=None, actor_username=uname, action="auth.login_failed",
                             entity_type="user", entity_id=user.id if user else None, ip=ip)
        db.commit()
        raise AuthenticationError()

    jti = new_jti()
    token, exp = create_access_token(user.id, jti)
    db.add(AuthSession(jti=jti, user_id=user.id, expires_at=exp, ip_address=ip, user_agent=(user_agent or "")[:255]))
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = _now()
    audit_service.record(db, actor=user, action="auth.login", entity_type="user", entity_id=user.id, ip=ip)
    db.commit()
    return user, token, exp


def revoke_session(db: Session, *, jti: str) -> None:
    db.execute(update(AuthSession).where(AuthSession.jti == jti, AuthSession.revoked_at.is_(None)).values(revoked_at=_now()))


def revoke_all_sessions(db: Session, *, user_id: int) -> None:
    db.execute(update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)).values(revoked_at=_now()))


def change_password(db: Session, *, user: User, current_password: str, new_password: str, ip: str | None) -> None:
    if not verify_password(current_password, user.password_hash):
        raise BusinessRuleError("Current password is incorrect.")
    problems = password_problems(new_password)
    if problems:
        raise BusinessRuleError("New password " + "; ".join(problems) + ".")
    if verify_password(new_password, user.password_hash):
        raise BusinessRuleError("New password must differ from the current password.")
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    revoke_all_sessions(db, user_id=user.id)
    audit_service.record(db, actor=user, action="auth.password_changed", entity_type="user", entity_id=user.id, ip=ip)
