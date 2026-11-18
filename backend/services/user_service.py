from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Location, Role, User, UserLocation
from backend.security import hash_password, password_problems
from backend.services import audit_service, auth_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError


def _snapshot(u: User) -> dict:
    return {
        "username": u.username, "full_name": u.full_name, "email": u.email, "phone": u.phone,
        "role": u.role.code, "is_active": u.is_active, "locations": u.assigned_location_ids,
    }


def get_user(db: Session, user_id: int) -> User:
    u = db.get(User, user_id)
    if u is None:
        raise NotFoundError("User not found")
    return u


def create_user(db: Session, *, actor: User, username: str, full_name: str, password: str, role_id: int,
                email: str | None, phone: str | None, location_ids: list[int], ip: str | None) -> User:
    uname = username.strip().lower()
    if db.scalar(select(User.id).where(User.username == uname)):
        raise ConflictError("Username already exists")
    problems = password_problems(password)
    if problems:
        raise BusinessRuleError("Password " + "; ".join(problems) + ".")
    role = db.get(Role, role_id)
    if role is None or not role.is_active:
        raise BusinessRuleError("Role does not exist or is inactive")
    _validate_locations(db, role, location_ids)
    user = User(username=uname, full_name=full_name.strip(), email=email, phone=phone,
                password_hash=hash_password(password), role_id=role.id, must_change_password=True)
    user.location_links = [UserLocation(location_id=i) for i in sorted(set(location_ids))]
    db.add(user)
    db.flush()
    db.refresh(user)
    audit_service.record(db, actor=actor, action="user.created", entity_type="user", entity_id=user.id,
                         after=_snapshot(user), ip=ip)
    return user


def _validate_locations(db: Session, role: Role, location_ids: list[int]) -> None:
    ids = set(location_ids)
    if ids:
        found = set(db.scalars(select(Location.id).where(Location.id.in_(ids))).all())
        if found != ids:
            raise BusinessRuleError(f"Unknown location ids: {sorted(ids - found)}")
    if not role.scope_all_locations and not ids:
        raise BusinessRuleError(f"A {role.name} must be assigned to at least one location.")


def update_user(db: Session, *, actor: User, user: User, full_name: str | None, email: str | None, phone: str | None,
                role_id: int | None, is_active: bool | None, reason: str | None, ip: str | None) -> User:
    before = _snapshot(user)
    if is_active is False and user.id == actor.id:
        raise BusinessRuleError("You cannot deactivate your own account.")
    if full_name is not None:
        user.full_name = full_name.strip()
    if email is not None:
        user.email = email
    if phone is not None:
        user.phone = phone
    if role_id is not None and role_id != user.role_id:
        if user.id == actor.id:
            raise BusinessRuleError("You cannot change your own role.")
        role = db.get(Role, role_id)
        if role is None or not role.is_active:
            raise BusinessRuleError("Role does not exist or is inactive")
        _validate_locations(db, role, user.assigned_location_ids)
        user.role_id = role.id
        user.role = role
        auth_service.revoke_all_sessions(db, user_id=user.id)
    if is_active is not None and is_active != user.is_active:
        user.is_active = is_active
        if not is_active:
            auth_service.revoke_all_sessions(db, user_id=user.id)
    db.flush()
    audit_service.record(db, actor=actor, action="user.updated", entity_type="user", entity_id=user.id,
                         reason=reason, before=before, after=_snapshot(user), ip=ip)
    return user


def set_user_locations(db: Session, *, actor: User, user: User, location_ids: list[int], reason: str | None, ip: str | None) -> User:
    _validate_locations(db, user.role, location_ids)
    before = _snapshot(user)
    user.location_links = [UserLocation(user_id=user.id, location_id=i) for i in sorted(set(location_ids))]
    db.flush()
    db.refresh(user)
    audit_service.record(db, actor=actor, action="user.locations_changed", entity_type="user", entity_id=user.id,
                         reason=reason, before=before, after=_snapshot(user), ip=ip)
    return user


def admin_reset_password(db: Session, *, actor: User, user: User, new_password: str, ip: str | None) -> None:
    problems = password_problems(new_password)
    if problems:
        raise BusinessRuleError("Password " + "; ".join(problems) + ".")
    user.password_hash = hash_password(new_password)
    user.must_change_password = True
    user.failed_login_count = 0
    user.locked_until = None
    auth_service.revoke_all_sessions(db, user_id=user.id)
    audit_service.record(db, actor=actor, action="user.password_reset", entity_type="user", entity_id=user.id, ip=ip)
