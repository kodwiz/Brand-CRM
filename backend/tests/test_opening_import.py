"""Phase 9: opening-balance import from the paper registers. Synthetic data only."""
import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from openpyxl import Workbook, load_workbook
from sqlalchemy import select, func

from backend.models import OpeningBatch, StockTransaction, SupplierLedgerEntry, DistributorInvoice, AuditLog
from backend.services import opening_import_service as oi
from backend.tests.conftest import login, make_user
from backend.tests.test_accounting import run_posting, tb

D = Decimal
TODAY = date.today()
AS_OF = (TODAY - timedelta(days=30)).isoformat()


@pytest.fixture
def oc(client, auth, catalog, locations, users, db):
    make_user(db, "ceo2", "ceo")
    auth["ceo2"] = login(client, "ceo2")
    dist = client.post("/distributors", headers=auth["ceo"], json={"name": "Open Dist", "phone": "0300 1230001", "credit_limit": "100000", "payment_terms_days": 30}).json()
    sup = client.post("/suppliers", headers=auth["ceo"], json={"name": "Open Supplier", "payment_terms_days": 30}).json()
    cust = client.post("/customers", headers=auth["ceo"], json={"name": "Open Customer", "phone": "0300 1230002"}).json()
    return {"dist": dist["code"], "sup": sup["code"], "dist_id": dist["id"], "sup_id": sup["id"], "cust": cust["code"], "cust_id": cust["id"], "B1": "B1", **catalog}


def workbook(rows: dict) -> bytes:
    """rows: sheet name -> list of dicts keyed by the template's header names."""
    wb = Workbook()
    wb.remove(wb.active)
    for name, cols in oi.SHEETS.items():
        ws = wb.create_sheet(name)
        heads = [c[1] for c in cols]
        ws.append(heads)
        for r in rows.get(name, []):
            ws.append([r.get(h) for h in heads])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


V = {"evidence": "Register p.12", "verified": "yes"}


def good_rows(capital="5200.00", **over):
    r = {"Opening stock": [{"location_code": "B1", "sku": "TST-LIP-R", "qty": 10, "unit_cost": "40", **V}],
         "Distributors": [{"distributor_code": None, "amount": "5000", **V}],
         "Suppliers": [{"supplier_code": None, "amount": "2000", **V}],
         "Customers": [{"customer_code": None, "amount": "300", **V}],
         "Cash and bank": [{"cash_account": "Cash on hand", "amount": "1500", **V}],
         "Owner's capital": [{"owner_capital": capital, **V}]}
    r.update(over)
    return r


def fill(rows, oc):
    rows["Distributors"][0]["distributor_code"] = oc["dist"]
    rows["Suppliers"][0]["supplier_code"] = oc["sup"]
    rows["Customers"][0]["customer_code"] = oc["cust"]
    return rows


def post(client, h, content, dry_run=True, as_of=AS_OF):
    return client.post("/accounting/opening-import", headers=h, params={"as_of": as_of, "dry_run": dry_run}, content=content)


def counts(db):
    return (db.scalar(select(func.count()).select_from(OpeningBatch)), db.scalar(select(func.count()).select_from(DistributorInvoice)),
            db.scalar(select(func.count()).select_from(SupplierLedgerEntry)),
            db.scalar(select(func.count()).select_from(StockTransaction).where(StockTransaction.txn_type == "opening_stock")))


# ---- access ------------------------------------------------------------------------------------------------------
def test_template_downloads_and_is_for_the_owner_level_only(client, auth, oc):
    r = client.get("/accounting/opening-import/template", headers=auth["ceo"])
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    wb = load_workbook(io.BytesIO(r.content))
    assert set(oi.SHEETS) <= set(wb.sheetnames) and "Instructions" in wb.sheetnames
    for who in ("accounts", "manager", "stock", "booker"):
        assert client.get("/accounting/opening-import/template", headers=auth[who]).status_code == 403, who
        assert post(client, auth[who], b"x").status_code == 403, who
    assert client.post("/accounting/opening-import", params={"as_of": AS_OF}, content=b"x").status_code == 401


# ---- check, then import --------------------------------------------------------------------------------------------
def test_check_saves_nothing_and_shows_the_proof(client, auth, oc, db):
    j = post(client, auth["ceo"], workbook(fill(good_rows(capital="5200"), oc))).json()
    assert j["ok"] and j["dry_run"] and not j["errors"], j
    t = j["totals"]
    assert (t["assets"], t["liabilities"], t["expected_capital"], t["declared_capital"], t["difference"]) == ("7200.00", "2000.00", "5200.00", "5200.00", "0.00")
    db.expire_all()
    assert counts(db) == (0, 0, 0, 0), "a check must leave no trace"


def test_capital_that_does_not_match_is_refused_with_the_difference_and_nothing_is_saved(client, auth, oc, db):
    j = post(client, auth["ceo"], workbook(fill(good_rows(capital="9999"), oc)), dry_run=False).json()
    assert not j["ok"]
    msg = " ".join(e["message"] for e in j["errors"])
    assert "does not equal" in msg and "difference" in msg
    db.expire_all()
    assert counts(db) == (0, 0, 0, 0)


def test_full_import_then_approval_clears_opening_equity_and_the_ledger_can_go_live(client, auth, oc, db):
    # assets: stock 400 + distributor 5000 + customer 300 + cash 1500 = 7200; liabilities: supplier 2000 => capital 5200
    content = workbook(fill(good_rows(capital="5200"), oc))
    j = post(client, auth["ceo"], content, dry_run=False).json()
    assert j["ok"], j
    assert j["totals"]["expected_capital"] == "5200.00" and j["batch_ref"]
    db.expire_all()
    assert counts(db) == (1, 1, 1, 1)
    batch = db.scalars(select(OpeningBatch)).one()
    assert batch.status == "draft", "the batch waits for a second person"
    assert client.post(f"/accounting/opening-batches/{batch.id}/approve", headers=auth["ceo"]).status_code == 403, "the importer cannot approve their own batch"
    run_posting(client, auth)
    assert client.post(f"/accounting/opening-batches/{batch.id}/approve", headers=auth["ceo2"]).status_code == 200
    run_posting(client, auth)
    t = tb(client, auth)
    assert t["totals"]["balanced"] and t["totals"]["debit"] == t["totals"]["credit"]
    obe = next((r for r in t["rows"] if r.get("code") == "3200"), None)
    assert obe is None or D(str(obe.get("debit") or 0)) - D(str(obe.get("credit") or 0)) == 0, obe
    cap = next(r for r in t["rows"] if r.get("code") == "3000")
    assert D(str(cap["credit"])) == D("5200"), cap
    chk = client.get("/accounting/go-live", headers=auth["ceo2"]).json()
    failing = [c["name"] for c in chk["checks"] if not c["ok"]]
    assert not failing, failing
    done = client.post("/accounting/go-live", headers=auth["ceo2"], json={"note": "Checked against register pages 1 to 20 and the counted cash."})
    assert done.status_code == 200, done.text
    assert client.post("/accounting/go-live", headers=auth["ceo"], json={"note": "x" * 30}).status_code in (409, 422, 403)


def test_the_same_file_cannot_be_imported_twice(client, auth, oc, db):
    content = workbook(fill(good_rows(capital="5200"), oc))
    assert post(client, auth["ceo"], content, dry_run=False).json()["ok"]
    r = post(client, auth["ceo"], content, dry_run=False)
    assert r.status_code == 409 and "already" in r.json()["detail"].lower()
    db.expire_all()
    assert counts(db) == (1, 1, 1, 1)


# ---- verified and documented only --------------------------------------------------------------------------------
def test_unverified_or_undocumented_rows_are_refused_row_by_row(client, auth, oc, db):
    rows = fill(good_rows(capital="5200"), oc)
    rows["Opening stock"][0]["verified"] = "no"
    rows["Suppliers"][0]["evidence"] = None
    j = post(client, auth["ceo"], workbook(rows), dry_run=False).json()
    assert not j["ok"]
    by = {(e["sheet"], e["row"]): e["message"] for e in j["errors"]}
    assert "Not verified" in by[("Opening stock", 2)] and "evidence" in by[("Suppliers", 2)].lower()
    db.expire_all()
    assert counts(db) == (0, 0, 0, 0)


def test_missing_owner_capital_is_refused(client, auth, oc):
    rows = fill(good_rows(), oc)
    rows["Owner's capital"] = []
    j = post(client, auth["ceo"], workbook(rows)).json()
    assert not j["ok"] and any("capital" in e["message"].lower() for e in j["errors"])


def test_unknown_items_duplicates_and_bad_numbers_are_all_reported_together(client, auth, oc, db):
    rows = fill(good_rows(capital="5200"), oc)
    rows["Opening stock"] = [{"location_code": "NOPE", "sku": "TST-LIP-R", "qty": 1, "unit_cost": "1", **V}, {"location_code": "B1", "sku": "NO-SKU", "qty": 1, "unit_cost": "1", **V},
                             {"location_code": "B1", "sku": "TST-LIP-R", "qty": 0, "unit_cost": "1", **V}, {"location_code": "B1", "sku": "TST-LIP-R", "qty": 2, "unit_cost": None, **V}]
    rows["Customers"] = [{"customer_code": oc["cust"], "amount": "abc", **V}, {"customer_code": oc["cust"], "amount": "10", **V}, {"customer_code": "CUST-NONE", "amount": "5", **V}]
    j = post(client, auth["ceo"], workbook(rows)).json()
    assert not j["ok"]
    msgs = " | ".join(f"{e['sheet']}:{e['row']}:{e['message']}" for e in j["errors"])
    for needle in ("Location 'NOPE'", "SKU 'NO-SKU'", "whole number greater than zero", "unit cost is required", "more than once", "does not exist", "not a number"):
        assert needle.lower() in msgs.lower(), needle
    db.expire_all()
    assert counts(db) == (0, 0, 0, 0)


def test_balance_owed_to_a_party_that_already_has_an_opening_is_refused(client, auth, oc, db):
    assert client.post(f"/distributors/{oc['dist_id']}/opening-balance", headers=auth["ceo"], json={"amount": "10", "as_of": AS_OF}).status_code == 201
    j = post(client, auth["ceo"], workbook(fill(good_rows(capital="5200"), oc))).json()
    assert not j["ok"] and any("already has an opening balance" in e["message"] for e in j["errors"])


def test_other_balances_reject_stock_cash_equity_and_income_accounts(client, auth, oc):
    rows = fill(good_rows(capital="5200"), oc)
    rows["Other balances"] = [{"account_code": c, "debit": "10", **V} for c in ("1200", "1000", "3000", "4000", "1100", "9999")]
    j = post(client, auth["ceo"], workbook(rows)).json()
    assert not j["ok"] and len([e for e in j["errors"] if e["sheet"] == "Other balances"]) == 6, j["errors"]


def test_other_assets_and_liabilities_count_toward_the_proof(client, auth, oc):
    rows = fill(good_rows(capital="5250"), oc)       # 5200 + other asset 100 - other liability 50
    rows["Other balances"] = [{"account_code": "1500", "debit": "100", **V}, {"account_code": "2300", "credit": "50", **V}]
    j = post(client, auth["ceo"], workbook(rows)).json()
    assert j["ok"], j["errors"]
    assert j["totals"]["expected_capital"] == "5250.00"


def test_dates_and_files_are_validated(client, auth, oc):
    good = workbook(fill(good_rows(capital="5200"), oc))
    assert post(client, auth["ceo"], good, as_of=(TODAY + timedelta(days=1)).isoformat()).status_code == 422
    assert post(client, auth["ceo"], b"not an excel file").status_code == 422
    assert post(client, auth["ceo"], b"").status_code == 422
    empty = workbook({})
    assert post(client, auth["ceo"], empty).status_code == 422


def test_a_failing_row_in_the_real_run_leaves_nothing_behind(client, auth, oc, db):
    rows = fill(good_rows(capital="5200"), oc)
    rows["Suppliers"][0]["supplier_code"] = "SUP-NOPE"
    assert not post(client, auth["ceo"], workbook(rows), dry_run=False).json()["ok"]
    db.expire_all()
    assert counts(db) == (0, 0, 0, 0)
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "opening.import")) == 0


# ---- the supplier opening balance on its own --------------------------------------------------------------------------------
def test_supplier_opening_balance_posts_once_ages_and_reconciles(client, auth, oc, db):
    r = client.post(f"/suppliers/{oc['sup_id']}/opening-balance", headers=auth["accounts"], json={"amount": "800", "as_of": (TODAY - timedelta(days=90)).isoformat(), "note": "Register p.3"})
    assert r.status_code == 201 and r.json()["balance"] == "800.00"
    assert client.post(f"/suppliers/{oc['sup_id']}/opening-balance", headers=auth["accounts"], json={"amount": "1", "as_of": AS_OF}).status_code == 409
    assert client.post(f"/suppliers/{oc['sup_id']}/opening-balance", headers=auth["stock"], json={"amount": "1", "as_of": AS_OF}).status_code == 403
    run_posting(client, auth)
    integ = client.get("/accounting/integrity", headers=auth["accounts"]).json()
    assert all(x["agrees"] for x in integ["rows"]), integ["rows"]
    summ = client.get(f"/suppliers/{oc['sup_id']}", headers=auth["ceo"]).json()
    pay = client.get("/payables", headers=auth["ceo"]).json() if client.get("/payables", headers=auth["ceo"]).status_code == 200 else None
    if pay:
        row = next(x for x in pay["rows"] if x["supplier_id"] == oc["sup_id"])
        assert D(row["balance"]) == D("800") and D(row["overdue"]) == D("800"), row
    assert summ is not None
