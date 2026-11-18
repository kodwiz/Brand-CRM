"""Migration and database-level guarantees."""
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from backend.db import get_engine
from backend.models import AuditLog, Location
from backend.services import audit_service
from backend.tests.conftest import ROOT


def test_audit_log_rejects_update_and_delete(db):
    audit_service.record(db, actor=None, actor_username="t", action="x.y", entity_type="thing", entity_id=1)
    db.commit()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("UPDATE audit_log SET action='tampered'"))
    db.rollback()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("DELETE FROM audit_log"))
    db.rollback()
    assert db.query(AuditLog).count() == 1


def test_location_type_check_constraint(db):
    db.add(Location(code="BAD", name="n", city="c", location_type="kiosk"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_location_code_unique(db):
    db.add(Location(code="U1", name="a", city="c"))
    db.commit()
    db.add(Location(code="U1", name="b", city="c"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_migrations_upgrade_downgrade_upgrade_roundtrip():
    """Downgrade to base and back to head on the test database."""
    run = lambda *a: subprocess.run([sys.executable, "-m", "alembic", *a], cwd=ROOT, check=True, capture_output=True)
    run("downgrade", "base")
    with get_engine().connect() as c:
        tables = {r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public'"))}
    assert tables <= {"alembic_version"}
    run("upgrade", "head")
    with get_engine().connect() as c:
        tables = {r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public'"))}
    assert {"users", "roles", "role_permissions", "locations", "user_locations", "audit_log", "auth_sessions"} <= tables


def test_seed_roles_is_idempotent_and_preserves_customisation(db):
    from sqlalchemy import select

    from backend.models import Role, RolePermission
    from backend.services.role_service import seed_roles

    role = db.scalar(select(Role).where(Role.code == "order_booker"))
    n = len(role.permissions)
    role.permissions.append(RolePermission(role_id=role.id, permission="reports.operational"))
    db.commit()
    seed_roles(db)
    db.commit()
    db.expire_all()
    assert len(db.scalar(select(Role).where(Role.code == "order_booker")).permissions) == n + 1
    assert db.query(Role).count() == 5
