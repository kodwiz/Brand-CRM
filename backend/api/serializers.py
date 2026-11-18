from backend.models import Role, User
from backend.schemas.core import RoleOut, UserOut


def user_out(u: User) -> UserOut:
    return UserOut(
        id=u.id, username=u.username, full_name=u.full_name, email=u.email, phone=u.phone,
        role_code=u.role.code, role_name=u.role.name, is_active=u.is_active,
        must_change_password=u.must_change_password, last_login_at=u.last_login_at,
        location_ids=u.assigned_location_ids,
    )


def role_out(r: Role) -> RoleOut:
    return RoleOut(
        id=r.id, code=r.code, name=r.name, description=r.description,
        scope_all_locations=r.scope_all_locations, is_system=r.is_system, is_active=r.is_active,
        permissions=sorted(p.permission for p in r.permissions),
    )
