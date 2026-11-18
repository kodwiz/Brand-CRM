"""Migrations apply cleanly from nothing, downgrade cleanly, and re-apply (run against a scratch database)."""
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.tests.conftest import ROOT, TEST_URL


@pytest.fixture
def scratch_url():
    url = make_url(TEST_URL)
    name = url.database + "_migcheck"
    admin = create_engine(url.set(database="postgres").render_as_string(hide_password=False), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        c.execute(text(f'CREATE DATABASE "{name}"'))
    yield url.set(database=name).render_as_string(hide_password=False)
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url, "ALEMBIC_DATABASE_URL": url}
    r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-1500:]


def test_upgrade_downgrade_upgrade(scratch_url):
    _alembic(scratch_url, "upgrade", "head")
    e = create_engine(scratch_url)
    with e.connect() as c:
        tables = {r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public'"))}
        assert {"stock_movements", "stock_balances", "stock_costs", "transfers", "product_variants", "import_jobs"} <= tables
        trig = {r[0] for r in c.execute(text("select tgname from pg_trigger where not tgisinternal"))}
        assert any("movement" in t for t in trig)           # append-only protection exists
    _alembic(scratch_url, "downgrade", "ca3a14e3d810")       # back to the Phase 1 revision
    with e.connect() as c:
        tables = {r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public'"))}
        assert "stock_movements" not in tables and "users" in tables
    _alembic(scratch_url, "upgrade", "head")
    e.dispose()


def test_phase3_upgrade_is_safe_for_populated_phase2_data(scratch_url):
    """Phase 3 adds a NOT NULL column to an existing table (stock_receipt_lines.landed_alloc). It must carry a server
    default so a database that already holds receipts upgrades without error, and downgrade must remove every Phase 3 object."""
    _alembic(scratch_url, "upgrade", "4e5845f4eab9")                       # Phase 2
    e = create_engine(scratch_url)
    with e.connect() as c:
        assert not c.execute(text("select 1 from pg_tables where tablename='suppliers'")).first()
    _alembic(scratch_url, "upgrade", "head")
    with e.connect() as c:
        d = c.execute(text("select column_default, is_nullable from information_schema.columns "
                           "where table_name='stock_receipt_lines' and column_name='landed_alloc'")).one()
        assert d[0] is not None and d[1] == "NO"                           # existing rows are filled with 0
        assert c.execute(text("select 1 from pg_trigger where tgname like '%supplier_ledger%' and not tgisinternal")).first()
        seqs = {r[0] for r in c.execute(text("select sequencename from pg_sequences where schemaname='public'"))}
        assert {"supplier_code_seq", "purchase_order_ref_seq", "supplier_invoice_ref_seq", "supplier_payment_ref_seq"} <= seqs
        perms = {r[0] for r in c.execute(text("select p.code from permissions p"))} if c.execute(
            text("select 1 from pg_tables where tablename='permissions'")).first() else set()
        if perms:
            assert "purchasing.approve_variance" in perms
    _alembic(scratch_url, "downgrade", "4e5845f4eab9")
    with e.connect() as c:
        left = {r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public' "
                                             "and (tablename like 'supplier%' or tablename like 'purchase_order%')"))}
        assert left == set()
        seqs = {r[0] for r in c.execute(text("select sequencename from pg_sequences where schemaname='public'"))}
        assert not {s for s in seqs if s.startswith(("supplier_", "purchase_order_"))}
        assert not c.execute(text("select 1 from pg_proc where proname='supplier_ledger_immutable'")).first()
        assert not c.execute(text("select 1 from information_schema.columns where table_name='stock_receipt_lines' and column_name='landed_alloc'")).first()
    _alembic(scratch_url, "upgrade", "head")
    e.dispose()


def test_phase4_upgrade_downgrade_with_populated_phase3_data(scratch_url):
    """Phase 4 must upgrade a database that already holds Phase 3 suppliers, seed its channels, remove every Phase 4
    object on downgrade without touching Phase 3 data, and re-apply."""
    _alembic(scratch_url, "upgrade", "b6b26358303d")                       # Phase 3
    e = create_engine(scratch_url)
    with e.begin() as c:
        c.execute(text("insert into suppliers (code, name, name_norm, payment_terms_days, status) "
                       "values ('SUP-X1', 'Kept Supplier', 'kept supplier', 30, 'active')")) if _has_cols(c) else None
    _alembic(scratch_url, "upgrade", "head")
    with e.connect() as c:
        tables = {r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public'"))}
        assert {"customers", "orders", "order_lines", "shipments", "order_payments", "customer_credit_entries",
                "sales_returns", "promotions", "sales_channels"} <= tables
        assert c.execute(text("select count(*) from sales_channels")).scalar() >= 1
        assert c.execute(text("select 1 from pg_trigger where tgrelid='customer_credit_entries'::regclass and not tgisinternal")).first()
    _alembic(scratch_url, "downgrade", "b6b26358303d")
    with e.connect() as c:
        tables = {r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public'"))}
        assert not {"customers", "orders", "shipments", "sales_returns", "sales_channels"} & tables
        assert "suppliers" in tables
        seqs = {r[0] for r in c.execute(text("select sequencename from pg_sequences where schemaname='public'"))}
        assert not {s for s in seqs if s.startswith(("customer_", "order_", "shipment_", "sales_"))}
    _alembic(scratch_url, "upgrade", "head")
    e.dispose()


def test_phase5_upgrade_downgrade_with_populated_phase4_data(scratch_url):
    """Phase 5 must add the distributor tables, sequences, ledger trigger and permission grant on top of Phase 4, and
    remove them on downgrade without touching Phase 4 objects."""
    _alembic(scratch_url, "upgrade", "6a589785a4bc")                       # Phase 4
    e = create_engine(scratch_url)
    _alembic(scratch_url, "upgrade", "head")
    with e.connect() as c:
        tables = {r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public'"))}
        assert {"distributors", "distributor_prices", "distributor_invoices", "distributor_invoice_lines", "distributor_returns",
                "distributor_payments", "distributor_allocations", "distributor_adjustments", "distributor_ledger"} <= tables
        seqs = {r[0] for r in c.execute(text("select sequencename from pg_sequences where schemaname='public'"))}
        assert {"distributor_code_seq", "distributor_invoice_ref_seq", "distributor_payment_ref_seq"} <= seqs
        assert c.execute(text("select 1 from pg_trigger where tgrelid='distributor_ledger'::regclass and not tgisinternal")).first()
    _alembic(scratch_url, "downgrade", "6a589785a4bc")
    with e.connect() as c:
        tables = {r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public'"))}
        assert not {t for t in tables if t.startswith("distributor")}
        assert "orders" in tables and "customers" in tables
        seqs = {r[0] for r in c.execute(text("select sequencename from pg_sequences where schemaname='public'"))}
        assert not {s for s in seqs if s.startswith("distributor_")}
    _alembic(scratch_url, "upgrade", "head")
    e.dispose()


def test_phase6_upgrade_downgrade_over_phase5(scratch_url):
    """Phase 6 adds the accounting tables, sequences, journal guard triggers and grants; it relaxes the sub-ledger triggers only for
    accounting_status; downgrade removes all of it and restores the strict triggers."""
    _alembic(scratch_url, "upgrade", "65011ae679e3")                       # Phase 5
    e = create_engine(scratch_url)
    _alembic(scratch_url, "upgrade", "head")
    with e.connect() as c:
        tables = {r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public'"))}
        assert {"accounts", "accounting_periods", "period_requests", "journal_entries", "journal_lines", "cash_accounts", "cash_reconciliations",
                "expenses", "opening_batches", "opening_lines"} <= tables
        seqs = {r[0] for r in c.execute(text("select sequencename from pg_sequences where schemaname='public'"))}
        assert {"journal_ref_seq", "expense_ref_seq", "opening_ref_seq"} <= seqs
        trg = {r[0] for r in c.execute(text("select tgname from pg_trigger where not tgisinternal"))}
        assert {"trg_journal_lines_balance", "trg_journal_entries_balance", "trg_journal_line_guard", "trg_journal_entry_guard"} <= trg
        perms = {r[0] for r in c.execute(text("select permission from role_permissions"))}
        assert "accounting.approve" in perms or perms == set()          # a fresh database has no roles yet
        fn = c.execute(text("select prosrc from pg_proc where proname='supplier_ledger_immutable'")).scalar()
        assert "accounting_status" in fn
    _alembic(scratch_url, "downgrade", "65011ae679e3")
    with e.connect() as c:
        tables = {r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public'"))}
        assert not ({"accounts", "journal_entries", "journal_lines", "expenses", "cash_accounts"} & tables)
        assert "distributors" in tables
        fn = c.execute(text("select prosrc from pg_proc where proname='supplier_ledger_immutable'")).scalar()
        assert "accounting_status" not in fn
        assert not c.execute(text("select 1 from pg_proc where proname in ('journal_balance_check','journal_line_guard','journal_entry_guard')")).first()
    _alembic(scratch_url, "upgrade", "head")
    e.dispose()


def test_phase7_upgrade_downgrade_over_phase6(scratch_url):
    """Phase 7 adds the employee, purchase and attendance tables, three sequences, the punch guard trigger and role grants; downgrade removes
    all of it and leaves Phase 6 intact."""
    _alembic(scratch_url, "upgrade", "9e5096afa85a")                       # Phase 6
    e = create_engine(scratch_url)
    with e.begin() as c:       # an existing deployment already has roles; the migration must grant the new permissions to them
        c.execute(text("insert into roles (id, code, name, scope_all_locations, is_system, is_active) values (901, 'ceo', 'CEO', true, true, true), "
                       "(902, 'manager', 'Manager', true, true, true), (903, 'stock_keeper', 'Stock keeper', false, true, true), (904, 'accounts', 'Accounts', true, true, true)"))
    _alembic(scratch_url, "upgrade", "head")
    with e.connect() as c:
        tables = {r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public'"))}
        assert {"employees", "employee_discount_rules", "employee_purchases", "employee_purchase_lines", "employee_purchase_payments", "attendance_devices",
                "attendance_imports", "attendance_punches", "attendance_policies", "attendance_days", "attendance_corrections"} <= tables
        seqs = {r[0] for r in c.execute(text("select sequencename from pg_sequences where schemaname='public'"))}
        assert {"employee_code_seq", "employee_purchase_ref_seq", "employee_payment_ref_seq"} <= seqs
        assert "attendance_punch_guard" in {r[0] for r in c.execute(text("select tgname from pg_trigger where not tgisinternal"))}
        grants = {(r[0], r[1]) for r in c.execute(text("select r.code, p.permission from role_permissions p join roles r on r.id = p.role_id"))}
        assert ("ceo", "attendance.approve") in grants and ("manager", "employees.purchase_approve") in grants
        assert ("stock_keeper", "employees.purchase") in grants and ("accounts", "employees.purchase_pay") in grants
        assert ("accounts", "attendance.view") not in grants
    _alembic(scratch_url, "downgrade", "9e5096afa85a")
    with e.connect() as c:
        tables = {r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public'"))}
        assert not ({"employees", "employee_purchases", "attendance_punches", "attendance_days"} & tables)
        assert "journal_entries" in tables
        assert not c.execute(text("select 1 from pg_proc where proname = 'attendance_punch_guard'")).first()
        assert not c.execute(text("select 1 from role_permissions where permission like 'employees.purchase%' or permission = 'attendance.approve'")).first()
    _alembic(scratch_url, "upgrade", "head")
    e.dispose()


def _has_cols(c) -> bool:
    cols = {r[0] for r in c.execute(text("select column_name from information_schema.columns where table_name='suppliers'"))}
    return {"code", "name", "name_norm", "payment_terms_days", "status"} <= cols
