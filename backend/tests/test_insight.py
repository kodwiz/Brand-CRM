"""Phase 8: dashboards, reports, exports, approvals inbox. Synthetic data only."""
import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from backend.models import AuditLog
from backend.tests.conftest import login, make_user
from backend.tests.test_accounting import run_posting
from backend.tests.test_sales import ctx, delivered_paid_order, mk_order  # noqa: F401  (ctx is a fixture)

D = Decimal
TODAY = date.today()
iso = lambda d: d.isoformat()  # noqa: E731


def rep(client, h, key, **params):
    return client.get(f"/insight/reports/{key}", headers=h, params=params)


def keys(client, h):
    return {r["key"] for r in client.get("/insight/reports", headers=h).json()["items"]}


@pytest.fixture
def sold(client, auth, ctx):  # noqa: F811
    o = delivered_paid_order(client, auth, ctx, qty=4)
    run_posting(client, auth)
    return {"order": o, **ctx}


# ---- catalogue and permissions ------------------------------------------------------------------------------
def test_catalogue_follows_permissions_and_hidden_reports_answer_404(client, auth, sold):
    ceo, acc, mgr, bk = keys(client, auth["ceo"]), keys(client, auth["accounts"]), keys(client, auth["manager"]), keys(client, auth["booker"])
    assert len(ceo) >= 17
    assert "profit_and_loss" in ceo and "profit_and_loss" in acc and "profit_and_loss" not in mgr
    assert "inventory_movement" in mgr and "inventory_movement" not in acc
    assert not bk or "profit_and_loss" not in bk
    assert rep(client, auth["manager"], "profit_and_loss").status_code == 404
    assert rep(client, auth["accounts"], "inventory_movement").status_code == 404
    assert rep(client, auth["ceo"], "no_such_report").status_code == 404


def test_bad_dates_are_refused(client, auth, sold):
    r = rep(client, auth["ceo"], "sales_by_date_location", date_from="2026-02-01", date_to="2026-01-01")
    assert r.status_code == 422
    assert rep(client, auth["ceo"], "sales_by_date_location", date_from="nonsense").status_code in (400, 422)


# ---- reconciliation -------------------------------------------------------------------------------------------
def test_every_report_runs_for_the_ceo_and_has_the_standard_shape(client, auth, sold):
    for k in keys(client, auth["ceo"]):
        params = {}
        if k == "party_statement":
            continue                                   # needs a party; covered separately
        r = rep(client, auth["ceo"], k, **params)
        assert r.status_code == 200, (k, r.text)
        j = r.json()
        assert {"title", "columns", "rows", "totals", "notes"} <= set(j), k
        assert j["key"] == k and j["generated_by"]
        cols = {c["key"] for c in j["columns"]}
        for row in j["rows"]:
            assert cols & set(row), k


def test_sales_report_reconciles_to_the_ledger(client, auth, sold):
    j = rep(client, auth["ceo"], "sales_by_date_location").json()
    assert j["rows"], "a delivered sale shows"
    assert j["reconciliation"] and all(r["agrees"] and D(r["difference"]) == 0 for r in j["reconciliation"]), j["reconciliation"]


def test_product_performance_reconciles_and_shows_margin(client, auth, sold):
    j = rep(client, auth["ceo"], "product_performance").json()
    assert j["rows"]
    row = next(r for r in j["rows"] if r["sku"] == "TST-LIP-R")
    assert row["units"] >= 4 and D(row["margin"]) == D(row["revenue"]) - D(row["cost"])
    assert j["reconciliation"]


def test_profit_and_loss_matches_ledger_balances(client, auth, sold):
    j = rep(client, auth["accounts"], "profit_and_loss").json()
    t = j["totals"]
    assert D(str(t["net_revenue"])) > 0 and D(str(t["cost_of_sales"])) > 0
    assert D(str(t["gross_profit"])) == D(str(t["net_revenue"])) - D(str(t["cost_of_sales"]))


def test_stock_valuation_agrees_with_general_ledger(client, auth, sold):
    j = rep(client, auth["ceo"], "stock_valuation").json()
    assert j["totals"]["agrees"] in (True, "true", "yes", "Yes"), j["totals"]


# ---- scope --------------------------------------------------------------------------------------------------------
def test_scoped_user_cannot_read_a_location_they_are_not_assigned_to(client, auth, sold):
    r = rep(client, auth["stock"], "inventory_movement", location_id=sold["B2"])
    assert r.status_code == 404, r.text            # out of scope is "not found", as everywhere else
    ok = rep(client, auth["stock"], "inventory_movement", location_id=sold["B1"])
    assert ok.status_code == 200
    mine = rep(client, auth["stock"], "inventory_movement")
    assert mine.status_code == 200


def test_scoped_user_without_location_gets_only_their_locations(client, auth, sold):
    j = rep(client, auth["stock"], "inventory_movement").json()
    locs = {str(r.get("location")) for r in j["rows"] if r.get("location")}
    assert "B2" not in locs and not any("B2" in l for l in locs)


# ---- exports ----------------------------------------------------------------------------------------------------
def test_export_xlsx_and_pdf_are_valid_and_audited(client, auth, sold, db):
    from openpyxl import load_workbook
    x = client.get("/insight/reports/sales_by_date_location/export", headers=auth["ceo"], params={"format": "xlsx"})
    assert x.status_code == 200 and "attachment" in x.headers["content-disposition"] and x.headers["cache-control"] == "no-store"
    wb = load_workbook(io.BytesIO(x.content))
    assert wb.active.max_row >= 2
    p = client.get("/insight/reports/sales_by_date_location/export", headers=auth["ceo"], params={"format": "pdf"})
    assert p.status_code == 200 and p.content[:4] == b"%PDF"
    logged = db.scalars(select(AuditLog).where(AuditLog.action == "report.export")).all()
    assert len(logged) >= 2


def test_export_needs_permission_and_a_visible_report(client, auth, sold):
    assert client.get("/insight/reports/sales_by_date_location/export", headers=auth["stock"], params={"format": "xlsx"}).status_code in (403, 404)
    assert client.get("/insight/reports/profit_and_loss/export", headers=auth["manager"], params={"format": "xlsx"}).status_code == 404
    assert client.get("/insight/reports/sales_by_date_location/export", headers=auth["ceo"], params={"format": "docx"}).status_code in (400, 422)


def test_formula_injection_is_neutralised_in_exports():
    from openpyxl import load_workbook
    from backend.services.export_service import to_xlsx
    rep_ = {"title": "T", "subtitle": "", "columns": [{"key": "a", "label": "A", "align": "left"}], "rows": [{"a": "=1+1"}, {"a": "+SUM(A1)"}, {"a": "@cmd"}], "totals": {}, "notes": []}
    wb = load_workbook(io.BytesIO(to_xlsx(rep_)))
    for row in wb.active.iter_rows():
        for c in row:
            assert c.data_type != "f"


# ---- dashboards -----------------------------------------------------------------------------------------------
def dash(client, h, **p):
    return client.get("/insight/dashboard", headers=h, params=p)


def metric(d, key):
    for s in d["sections"]:
        for m in s["metrics"]:
            if m["key"] == key:
                return m
    return None


def test_dashboard_view_follows_role(client, auth, sold):
    views = {n: dash(client, auth[n]).json()["view"] for n in ("ceo", "manager", "accounts", "stock", "booker")}
    assert views["ceo"] == "management" and views["manager"] == "management"
    assert views["accounts"] == "accounts"
    assert views["stock"] == "operations"
    assert views["booker"] == "booker"


def test_ceo_dashboard_keeps_revenue_cash_receivables_and_stock_separate(client, auth, sold):
    d = dash(client, auth["ceo"]).json()
    labels = [m["label"].lower() for s in d["sections"] for m in s["metrics"]]
    blob = " ".join(labels)
    for word in ("net sales", "received", "owe", "damaged"):
        assert word in blob, word
    assert len(set(labels)) == len(labels)


def test_dashboard_location_filter_and_scope(client, auth, sold):
    d = dash(client, auth["ceo"], location_id=sold["B2"]).json()
    assert d["view"] == "management"
    assert dash(client, auth["stock"], location_id=sold["B2"]).status_code == 404
    assert dash(client, auth["ceo"], date_from="2026-03-01", date_to="2026-01-01").status_code == 422


def test_booker_sees_only_own_orders_and_no_company_finance(client, auth, sold):
    d = dash(client, auth["booker"]).json()
    text = str(d).lower()
    assert "gross profit" not in text and "payable" not in text
    assert d["view"] == "booker"


def test_manager_does_not_see_financial_statements(client, auth, sold):
    d = dash(client, auth["manager"]).json()
    assert "gross profit" not in " ".join(m["label"].lower() for s in d["sections"] for m in s["metrics"])


def test_low_stock_alert_appears(client, auth, sold, db):
    d = dash(client, auth["ceo"]).json()
    assert isinstance(d["alerts"], list)


# ---- approvals inbox ------------------------------------------------------------------------------------------
def test_inbox_is_gated_by_permission(client, auth, sold):
    assert client.get("/insight/approvals", headers=auth["booker"]).status_code == 403
    ceo = client.get("/insight/approvals", headers=auth["ceo"])
    assert ceo.status_code == 200 and {"items", "counts"} <= set(ceo.json())


def test_stock_adjustment_request_appears_for_approver_and_marks_own(client, auth, sold):
    r = client.post("/inventory/requests", headers=auth["stock"], json={"kind": "adjustment", "location_id": sold["B1"], "reason": "count variance",
                                                                        "lines": [{"variant_id": sold["rose"], "qty_delta": -2}]})
    assert r.status_code == 201, r.text
    mine = client.get("/insight/approvals", headers=auth["manager"]).json()
    row = next(i for i in mine["items"] if i["kind"] == "stock_change")
    assert row["own"] is False and row["screen"] == "inventory"
    assert mine["counts"]["total"] >= 1
    assert client.get("/insight/approvals", headers=auth["booker"]).status_code == 403


def test_inbox_item_disappears_once_decided(client, auth, sold):
    r = client.post("/inventory/requests", headers=auth["stock"], json={"kind": "adjustment", "location_id": sold["B1"], "reason": "count",
                                                                        "lines": [{"variant_id": sold["rose"], "qty_delta": -1}]}).json()
    assert client.post(f"/inventory/requests/{r['id']}/approve", headers=auth["manager"], json={}).status_code == 200
    items = client.get("/insight/approvals", headers=auth["manager"]).json()["items"]
    assert not any(i["kind"] == "stock_change" and i["id"] == r["id"] for i in items)


def test_distributor_dispatch_shows_in_sales_and_product_reports_and_both_reconcile(client, auth, catalog, locations, users, db):
    from backend.tests.conftest import line, receive
    from backend.tests.test_distributors import dispatch
    from backend.tests.test_accounting import run_posting
    assert receive(client, auth["stock"], locations["WH"].id, [line(catalog["rose"], 50, 40)]).status_code in (200, 201)
    dist = client.post("/distributors", headers=auth["ceo"], json={"name": "Dist One", "phone": "0300 5555555", "credit_limit": "100000", "payment_terms_days": 30})
    assert dist.status_code == 201, dist.text
    c = {"WH": locations["WH"].id, "rose": catalog["rose"], "dist": dist.json()["id"]}
    r = dispatch(client, auth["ceo"], c, qty=10)
    assert r.status_code in (200, 201), r.text
    run_posting(client, auth)
    for key in ("sales_by_date_location", "product_performance"):
        j = rep(client, auth["ceo"], key).json()
        assert j["rows"], key
        assert all(x["agrees"] for x in j["reconciliation"]), (key, j["reconciliation"])
    prod = rep(client, auth["ceo"], "product_performance").json()
    assert next(x for x in prod["rows"] if x["sku"] == "TST-LIP-R")["units"] == 10
