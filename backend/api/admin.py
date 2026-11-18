"""Locations, users, roles and audit endpoints."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.api.serializers import role_out, user_out
from backend.db import get_db
from backend.deps import Principal, client_ip, ensure_location_access, require
from backend.models import Role, User
from backend.permissions import PERMISSIONS
from backend.schemas.core import (
    AuditOut, LocationCreate, LocationOut, LocationUpdate, Page, PasswordReset, RoleOut,
    RolePermissionsUpdate, UserCreate, UserLocationsUpdate, UserOut, UserUpdate,
)
from backend.services import audit_service, location_service, role_service, user_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError

router = APIRouter()


def _commit_or_http(db: Session, fn):
    """Run a service call, mapping domain errors to HTTP and committing on success."""
    try:
        result = fn()
        db.commit()
        return result
    except NotFoundError as e:
        db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    except ConflictError as e:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    except BusinessRuleError as e:
        db.rollback()
        raise HTTPException(422, str(e))


# ---- locations -----------------------------------------------------------------
@router.get("/locations", response_model=list[LocationOut], tags=["locations"])
def list_locations(include_inactive: bool = False, p: Principal = Depends(require("locations.view")),
                   db: Session = Depends(get_db)):
    return location_service.list_locations(db, p.user, include_inactive=include_inactive and p.has("locations.manage"))


@router.post("/locations", response_model=LocationOut, status_code=201, tags=["locations"])
def create_location(body: LocationCreate, request: Request, p: Principal = Depends(require("locations.manage")),
                    db: Session = Depends(get_db)):
    return _commit_or_http(db, lambda: location_service.create_location(
        db, actor=p.user, ip=client_ip(request), **body.model_dump()))


@router.patch("/locations/{location_id}", response_model=LocationOut, tags=["locations"])
def update_location(location_id: int, body: LocationUpdate, request: Request,
                    p: Principal = Depends(require("locations.manage")), db: Session = Depends(get_db)):
    ensure_location_access(p, location_id)

    def run():
        loc = location_service.get_location(db, location_id)
        return location_service.update_location(db, actor=p.user, loc=loc, ip=client_ip(request), **body.model_dump())

    return _commit_or_http(db, run)


# ---- users ---------------------------------------------------------------------
@router.get("/users", response_model=Page[UserOut], tags=["users"])
def list_users(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), q: str | None = None,
               p: Principal = Depends(require("users.view")), db: Session = Depends(get_db)):
    stmt = select(User)
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(User.username).like(like) | func.lower(User.full_name).like(like))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(User.full_name).offset((page - 1) * page_size).limit(page_size)).all()
    return Page[UserOut](items=[user_out(u) for u in rows], total=total, page=page, page_size=page_size)


@router.post("/users", response_model=UserOut, status_code=201, tags=["users"])
def create_user(body: UserCreate, request: Request, p: Principal = Depends(require("users.manage")),
                db: Session = Depends(get_db)):
    u = _commit_or_http(db, lambda: user_service.create_user(db, actor=p.user, ip=client_ip(request), **body.model_dump()))
    return user_out(u)


@router.patch("/users/{user_id}", response_model=UserOut, tags=["users"])
def update_user(user_id: int, body: UserUpdate, request: Request, p: Principal = Depends(require("users.manage")),
                db: Session = Depends(get_db)):
    def run():
        user = user_service.get_user(db, user_id)
        return user_service.update_user(db, actor=p.user, user=user, ip=client_ip(request), **body.model_dump())

    return user_out(_commit_or_http(db, run))


@router.put("/users/{user_id}/locations", response_model=UserOut, tags=["users"])
def set_user_locations(user_id: int, body: UserLocationsUpdate, request: Request,
                       p: Principal = Depends(require("users.manage")), db: Session = Depends(get_db)):
    def run():
        user = user_service.get_user(db, user_id)
        return user_service.set_user_locations(db, actor=p.user, user=user, location_ids=body.location_ids,
                                               reason=body.reason, ip=client_ip(request))

    return user_out(_commit_or_http(db, run))


@router.post("/users/{user_id}/reset-password", status_code=204, tags=["users"])
def reset_password(user_id: int, body: PasswordReset, request: Request,
                   p: Principal = Depends(require("users.manage")), db: Session = Depends(get_db)):
    def run():
        user = user_service.get_user(db, user_id)
        user_service.admin_reset_password(db, actor=p.user, user=user, new_password=body.new_password, ip=client_ip(request))

    _commit_or_http(db, run)


# ---- roles ---------------------------------------------------------------------
@router.get("/roles", response_model=list[RoleOut], tags=["roles"])
def list_roles(p: Principal = Depends(require("users.view")), db: Session = Depends(get_db)):
    return [role_out(r) for r in db.scalars(select(Role).order_by(Role.id)).all()]


@router.get("/roles/permission-catalogue", response_model=dict[str, str], tags=["roles"])
def permission_catalogue(p: Principal = Depends(require("roles.manage"))):
    return PERMISSIONS


@router.put("/roles/{role_id}/permissions", response_model=RoleOut, tags=["roles"])
def set_role_permissions(role_id: int, body: RolePermissionsUpdate, request: Request,
                         p: Principal = Depends(require("roles.manage")), db: Session = Depends(get_db)):
    def run():
        role = db.get(Role, role_id)
        if role is None:
            raise NotFoundError("Role not found")
        role_service.set_role_permissions(db, actor=p.user, role=role, permissions=set(body.permissions),
                                          reason=body.reason, ip=client_ip(request))
        db.flush()
        db.refresh(role)
        return role

    return role_out(_commit_or_http(db, run))


# ---- audit ---------------------------------------------------------------------
@router.get("/audit", response_model=Page[AuditOut], tags=["audit"])
def list_audit(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
               entity_type: str | None = None, entity_id: str | None = None, action: str | None = None,
               actor_user_id: int | None = None, date_from: datetime | None = None, date_to: datetime | None = None,
               p: Principal = Depends(require("audit.view")), db: Session = Depends(get_db)):
    rows, total = audit_service.search(db, page=page, page_size=page_size, entity_type=entity_type,
                                       entity_id=entity_id, action=action, actor_user_id=actor_user_id,
                                       date_from=date_from, date_to=date_to)
    return Page[AuditOut](items=[AuditOut.model_validate(r) for r in rows], total=total, page=page, page_size=page_size)
