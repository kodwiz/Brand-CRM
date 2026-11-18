from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Location, User
from backend.models.location import LOCATION_TYPES
from backend.services import audit_service
from backend.services.errors import BusinessRuleError, ConflictError, NotFoundError
from backend.services.scope import visible_location_ids


def _snap(loc: Location) -> dict:
    return {"code": loc.code, "name": loc.name, "city": loc.city, "address": loc.address,
            "location_type": loc.location_type, "is_active": loc.is_active}


def list_locations(db: Session, user: User, include_inactive: bool = False) -> list[Location]:
    q = select(Location).order_by(Location.city, Location.name)
    ids = visible_location_ids(user)
    if ids is not None:
        q = q.where(Location.id.in_(ids))
    if not include_inactive:
        q = q.where(Location.is_active.is_(True))
    return list(db.scalars(q).all())


def get_location(db: Session, location_id: int) -> Location:
    loc = db.get(Location, location_id)
    if loc is None:
        raise NotFoundError("Location not found")
    return loc


def create_location(db: Session, *, actor: User, code: str, name: str, city: str, address: str | None,
                    location_type: str, ip: str | None) -> Location:
    code = code.strip().upper()
    if location_type not in LOCATION_TYPES:
        raise BusinessRuleError(f"location_type must be one of {LOCATION_TYPES}")
    if db.scalar(select(Location.id).where(Location.code == code)):
        raise ConflictError("Location code already exists")
    loc = Location(code=code, name=name.strip(), city=city.strip(), address=address, location_type=location_type)
    db.add(loc)
    db.flush()
    audit_service.record(db, actor=actor, action="location.created", entity_type="location", entity_id=loc.id,
                         location_id=loc.id, after=_snap(loc), ip=ip)
    return loc


def update_location(db: Session, *, actor: User, loc: Location, name: str | None, city: str | None,
                    address: str | None, location_type: str | None, is_active: bool | None,
                    reason: str | None, ip: str | None) -> Location:
    before = _snap(loc)
    if location_type is not None:
        if location_type not in LOCATION_TYPES:
            raise BusinessRuleError(f"location_type must be one of {LOCATION_TYPES}")
        loc.location_type = location_type
    if name is not None:
        loc.name = name.strip()
    if city is not None:
        loc.city = city.strip()
    if address is not None:
        loc.address = address
    if is_active is not None:
        loc.is_active = is_active
    db.flush()
    audit_service.record(db, actor=actor, action="location.updated", entity_type="location", entity_id=loc.id,
                         location_id=loc.id, reason=reason, before=before, after=_snap(loc), ip=ip)
    return loc
