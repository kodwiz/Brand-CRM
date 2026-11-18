"""Test fixtures. Tests run against a real PostgreSQL database (TEST_DATABASE_URL) whose schema is
built by running the Alembic migrations, so the migrations themselves are exercised."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values  # provided by pydantic-settings' python-dotenv dependency

ROOT = Path(__file__).resolve().parents[2]
_env = dotenv_values(ROOT / ".env")
TEST_URL = os.environ.get("TEST_DATABASE_URL") or _env.get("TEST_DATABASE_URL")
if not TEST_URL:
    pytest.exit("TEST_DATABASE_URL is not set (see .env.example)", returncode=2)

# Point the application at the test database BEFORE any backend module reads settings.
os.environ["DATABASE_URL"] = TEST_URL
os.environ.setdefault("JWT_SECRET", _env.get("JWT_SECRET") or "t" * 40)
os.environ["ENVIRONMENT"] = "development"
os.environ["ALEMBIC_DATABASE_URL"] = TEST_URL

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from backend.db import get_engine, get_session_factory  # noqa: E402
from backend.main import app  # noqa: E402
from backend.models import Location, Role, User, UserLocation  # noqa: E402
from backend.security import hash_password, login_limiter  # noqa: E402
from backend.services.role_service import seed_roles  # noqa: E402

PASSWORD = "Str0ngPassw0rd!"
TABLES = ("audit_log, auth_sessions, user_locations, users, role_permissions, roles, locations, "
          "categories, products, product_variants, batches, import_jobs, system_settings, suppliers, customers, promotions, distributors, "
          "accounts, cash_accounts, accounting_periods, period_requests, journal_entries, journal_lines, cash_reconciliations, expenses, opening_batches, opening_lines, "
          "employees, employee_discount_rules, employee_purchases, employee_purchase_lines, employee_purchase_payments, attendance_devices, "
          "attendance_imports, attendance_punches, attendance_policies, attendance_days, attendance_corrections")


def _alembic(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, check=True, capture_output=True)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    engine = get_engine()
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    _alembic("upgrade", "head")
    yield


@pytest.fixture(autouse=True)
def _clean():
    login_limiter.reset()
    yield
    with get_engine().begin() as c:
        c.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))


@pytest.fixture
def db():
    with get_session_factory()() as s:
        seed_roles(s)
        s.commit()
        yield s


@pytest.fixture
def client(db):
    return TestClient(app)


@pytest.fixture
def locations(db):
    out = {}
    for code, city in [("WH", "Alpha"), ("B1", "Alpha"), ("B2", "Beta")]:
        loc = Location(code=code, name=f"Test {code}", city=city, location_type="both" if code == "WH" else "branch")
        db.add(loc)
        db.flush()
        out[code] = loc
    db.commit()
    return out


def make_user(db, username: str, role_code: str, loc_ids=(), password: str = PASSWORD, **kw) -> User:
    role = db.query(Role).filter_by(code=role_code).one()
    u = User(username=username, full_name=username.title(), password_hash=hash_password(password), role_id=role.id, **kw)
    u.location_links = [UserLocation(location_id=i) for i in loc_ids]
    db.add(u)
    db.commit()
    return u


def login(client: TestClient, username: str, password: str = PASSWORD) -> dict:
    r = client.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def users(db, locations):
    return {
        "ceo": make_user(db, "ceo", "ceo"),
        "manager": make_user(db, "manager", "manager"),
        "stock": make_user(db, "stock", "stock_keeper", [locations["WH"].id, locations["B1"].id]),
        "booker": make_user(db, "booker", "order_booker", [locations["B1"].id]),
        "accounts": make_user(db, "accounts", "accounts"),
    }


# ---- Phase 2 helpers -----------------------------------------------------------------------------------
@pytest.fixture
def auth(client, users):
    """Headers per user name."""
    return {name: login(client, name) for name in users}


@pytest.fixture
def catalog(client, auth):
    """Two products: a plain lipstick with two shades, and a batch+expiry tracked serum. Synthetic test data only."""
    h = auth["ceo"]
    cat = client.post("/categories", json={"name": "Test Category"}, headers=h).json()
    plain = client.post("/products", headers=h, json={
        "code": "TST-LIP", "name": "Test Lipstick", "brand": "TestBrand", "category_id": cat["id"],
        "variants": [
            {"sku": "TST-LIP-R", "name": "Rose", "shade_hex": "#B76E79", "retail_price": "100.00", "employee_price": "80.00",
             "distributor_price": "70.00", "purchase_cost": "40", "reorder_threshold": 5, "barcode": "9000000000011"},
            {"sku": "TST-LIP-N", "name": "Nude", "shade_hex": "#D2A98B", "retail_price": "100.00"},
        ]}).json()
    batched = client.post("/products", headers=h, json={
        "code": "TST-SER", "name": "Test Serum", "category_id": cat["id"], "tracks_batches": True, "tracks_expiry": True,
        "variants": [{"sku": "TST-SER-30", "name": "30ml", "retail_price": "500.00"}]}).json()
    return {"cat": cat, "plain": plain, "batched": batched,
            "rose": plain["variants"][0]["id"], "nude": plain["variants"][1]["id"], "serum": batched["variants"][0]["id"]}


def receive(client, headers, loc, lines, post=True, date_="2026-01-05"):
    body = {"location_id": loc, "received_date": date_, "post_now": post, "lines": lines}
    r = client.post("/inventory/receipts", json=body, headers=headers)
    return r


def line(variant_id, accepted, cost, **kw):
    d = {"variant_id": variant_id, "qty_received": accepted + kw.pop("damaged", 0) + kw.pop("rejected_", 0) if False else 0,
         "qty_accepted": accepted, "unit_cost": str(cost)}
    d.update(kw)
    d["qty_received"] = d["qty_accepted"] + d.get("qty_damaged", 0) + d.get("qty_rejected", 0)
    return d
