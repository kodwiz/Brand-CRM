from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Role, RolePermission, User
from backend.permissions import CRITICAL_ADMIN_PERMISSIONS, DEFAULT_ROLES, validate_permissions
from backend.services import audit_service
from backend.services.errors import BusinessRuleError


def seed_roles(db: Session) -> None:
    """Idempotently create default roles. Existing roles keep any customised permissions;
    only permissions missing from a *new* role are added."""
    for code, spec in DEFAULT_ROLES.items():
        role = db.scalar(select(Role).where(Role.code == code))
        if role is None:
            role = Role(
                code=code,
                name=spec["name"],
                description=spec["description"],
                scope_all_locations=spec["scope_all_locations"],
                is_system=True,
            )
            role.permissions = [RolePermission(permission=p) for p in sorted(spec["permissions"])]
            db.add(role)
    db.flush()


def set_role_permissions(db: Session, *, actor: User, role: Role, permissions: set[str], reason: str | None, ip: str | None) -> Role:
    unknown = validate_permissions(permissions)
    if unknown:
        raise BusinessRuleError(f"Unknown permissions: {', '.join(unknown)}")
    # Guard against locking the company out of administration.
    if role.code == "ceo" and not CRITICAL_ADMIN_PERMISSIONS <= permissions:
        raise BusinessRuleError("The CEO role must keep users.manage and roles.manage.")
    before = sorted(p.permission for p in role.permissions)
    role.permissions = [RolePermission(role_id=role.id, permission=p) for p in sorted(permissions)]
    db.flush()
    audit_service.record(
        db, actor=actor, action="role.permissions_changed", entity_type="role", entity_id=role.id,
        reason=reason, before={"permissions": before}, after={"permissions": sorted(permissions)}, ip=ip,
    )
    return role
