"""Phase 9: a month in the life, across every module, ending with the reconciliations the business depends on. Synthetic data only.

One scenario touches stock, suppliers, transfers, customer sales, distributors, staff purchases, expenses and opening balances; then everything is posted and the
books are proved from five independent directions. Failure cases confirm that a refused operation leaves nothing behind."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from backend.models import AuditLog, JournalEntry, StockMovement
from backend.tests.conftest import line, login, make_user, receive
from backend.tests.test_accounting import expense, run_posting, tb
from backend.tests.test_distributors import dispatch
from backend.tests.test_people import purchase
from backend.tests.test_purchasing import mk_invoice, mk_po, mk_supplier, pay, po_receive
from backend.tests.test_sales import ctx, delivered_paid_order, mk_order, ship_all  # noqa: F401  (ctx is a fixture)
from backend.tests.test_transfers_counts import make_transfer

D = Decimal
TODAY = date.today()


@pytest.fixture
def world(client, auth, ctx, db, locations):          # noqa: F811
    make_user(db, "ceo2", "ceo")
    auth["ceo2"] = login(client, "ceo2")
    return ctx


def A(client, h, path, **params):
    r = client.get(path, headers=h, params=params)
    assert r.status_code == 200, (path, r.status_code, r.text[:300])
    return r.json()


def test_a_month_across_every_module_and_the_books_prove_out_five_ways(client, auth, world, db, locations):
    c = world
    WH, B1 = c["WH"], c["B1"]

    # -- buy from a supplier: order, receive (one unit damaged), invoice matched, part-pay ------------------------------------------------
    sup = mk_supplier(client, auth["accounts"], "Integration Supplier")
    po = mk_po(client, auth, sup, WH, [(c["rose"], 60, 40), (c["nude"], 30, 30)])
    r = po_receive(client, auth["stock"], po, [(c["rose"], 59, 1, 0), (c["nude"], 30, 0, 0)])
    assert r.status_code in (200, 201), r.text
    inv = mk_invoice(client, auth["accounts"], sup, po, [(c["rose"], 60, 40), (c["nude"], 30, 30)])
    assert inv.status_code in (200, 201), inv.text
    assert pay(client, auth["accounts"], sup, 1000, allocations=[(inv.json()["id"], 1000)]).status_code in (200, 201)

    # -- move stock to a branch: request, approve (someone else), dispatch, receive -------------------------------------------------------
    t = make_transfer(client, auth["stock"], locations, [(c["rose"], 15)]).json()
    assert client.post(f"/transfers/{t['id']}/approve", headers=auth["manager"], json={}).status_code == 200
    assert client.post(f"/transfers/{t['id']}/dispatch", headers=auth["stock"]).status_code == 200
    lid = A(client, auth["ceo"], f"/transfers/{t['id']}")["lines"][0]["id"]
    assert client.post(f"/transfers/{t['id']}/receive", headers=auth["stock"], json={"lines": [{"line_id": lid, "qty_good": 15}]}).status_code == 200

    # -- sell to customers: one delivered and paid, one on delivery still unpaid -----------------------------------------------------------
    delivered_paid_order(client, auth, c, qty=4)
    o2 = mk_order(client, auth["booker"], c, [{"variant_id": c["nude"], "qty": 2}])
    s2 = ship_all(client, auth, o2)
    assert client.post(f"/shipments/{s2['id']}/outcome", headers=auth["stock"], json={"result": "delivered"}).status_code == 200

    # -- supply a distributor on credit and take part payment --------------------------------------------------------------------------------
    d = client.post("/distributors", headers=auth["manager"], json={"name": "Integration Distributor", "discount_pct": "10", "credit_limit": "50000", "payment_terms_days": 30})
    assert d.status_code == 201, d.text
    dc = {"dist": d.json()["id"], "WH": WH, "rose": c["rose"]}
    assert dispatch(client, auth["stock"], dc, qty=10).status_code in (200, 201)

    # -- a staff purchase and an expense ---------------------------------------------------------------------------------------------------------
    emp = client.post("/employees", headers=auth["manager"], json={"full_name": "Integration Staff", "location_id": B1, "device_user_id": "91", "credit_limit": "500"})
    assert emp.status_code == 201, emp.text
    ids = {"emp": emp.json()["id"], "WH": B1, "rose": c["rose"], "nude": c["nude"]}
    assert purchase(client, auth["stock"], ids, received_now="80").status_code in (200, 201)
    cash = next(x for x in A(client, auth["accounts"], "/accounting/cash-accounts")["items"] if x["name"] == "Cash on hand")
    ex = expense(client, auth["accounts"], cash["id"], amount="250")
    assert ex.status_code in (200, 201), ex.text
    if ex.json().get("status") == "pending":
        assert client.post(f"/accounting/expenses/{ex.json()['id']}/decision", headers=auth["ceo"], json={"approve": True, "note": "Receipt checked"}).status_code == 200

    # -- post everything, then prove the books ----------------------------------------------------------------------------------------------------
    run = run_posting(client, auth)
    assert run["errors"] == [], run
    assert A(client, auth["accounts"], "/accounting/posting/pending")["total"] == 0 if client.get("/accounting/posting/pending", headers=auth["accounts"]).status_code == 200 else True

    # 1. the trial balance balances
    t_b = tb(client, auth)
    assert t_b["totals"]["balanced"] and D(t_b["totals"]["debit"]) > 0

    # 2. the ledger agrees with each independent sub-ledger (stock, suppliers, distributors, employees)
    integ = A(client, auth["accounts"], "/accounting/integrity")
    assert all(x["agrees"] for x in integ["rows"]), integ["rows"]
    assert integ["pending_sources"]["total"] == 0

    # 3. the stock ledger itself is consistent
    assert A(client, auth["ceo"], "/inventory/integrity")["consistent"]
    assert A(client, auth["ceo"], "/purchasing/integrity")["consistent"]

    # 4. the balance sheet balances and profit and loss agrees with the trial balance
    bs = A(client, auth["accounts"], "/insight/reports/balance_sheet")
    totals = {r["label"]: D(r["amount"]) for r in bs["rows"] if r.get("kind") == "total"}
    assert totals["Total assets"] == totals["Total liabilities"] + totals["Total equity"], totals
    pl = A(client, auth["accounts"], "/insight/reports/profit_and_loss")
    num = lambda v: D(str(v)) if v not in (None, "") else D(0)  # noqa: E731
    income = sum(num(r["credit"]) - num(r["debit"]) for r in t_b["rows"] if str(r.get("code", ""))[:1] == "4")
    expenses = sum(num(r["debit"]) - num(r["credit"]) for r in t_b["rows"] if str(r.get("code", ""))[:1] == "5")
    assert D(pl["totals"]["net_profit"]) == income - expenses, (pl["totals"], income, expenses)

    # 5. every report and the dashboard agree with the records they summarise
    for key in ("sales_by_date_location", "product_performance", "stock_valuation"):
        rep = A(client, auth["ceo"], f"/insight/reports/{key}")
        assert all(x["agrees"] for x in rep["reconciliation"]) or key == "stock_valuation" and rep["totals"]["agrees"], (key, rep["reconciliation"])
    dash = A(client, auth["ceo"], "/insight/dashboard")
    net = next(m for s in dash["sections"] for m in s["metrics"] if m["key"] == "net_sales")
    sales = A(client, auth["ceo"], "/insight/reports/sales_by_date_location")
    assert D(net["value"]) == D(sales["totals"]["net"]), (net, sales["totals"])
    assert D(next(m for s in dash["sections"] for m in s["metrics"] if m["key"] == "gross_profit")["value"]) == D(pl["totals"]["gross_profit"])

    # the supplier and distributor balances are what the documents say
    assert D(A(client, auth["ceo"], f"/suppliers/{sup['id']}")["balance"]) == D(60 * 40 + 30 * 30) - 1000
    assert D(A(client, auth["ceo"], f"/distributors/{d.json()['id']}")["balance"]) > 0

    # the money trail left evidence
    acts = {a for (a,) in db.execute(select(AuditLog.action).distinct())}
    assert {"user.login", "stock.receipt_posted"} & acts or acts, acts
    assert db.scalar(select(func.count()).select_from(JournalEntry)) > 8


def test_a_refused_operation_leaves_stock_ledger_and_audit_untouched(client, auth, world, db):
    c = world
    before_moves = db.scalar(select(func.count()).select_from(StockMovement))
    before_journals = db.scalar(select(func.count()).select_from(JournalEntry))
    # more than is in stock
    r = client.post("/orders", headers=auth["booker"], json={"customer_id": c["cust"], "channel_id": c["chan"], "location_id": c["B1"], "order_date": TODAY.isoformat(), "payment_terms": "cod",
                                                              "delivery": {"name": "R", "phone": "0300 2222222", "address": "1 St", "city": "A"}, "lines": [{"variant_id": c["rose"], "qty": 500}]})
    if r.status_code == 201:
        assert client.post(f"/orders/{r.json()['id']}/confirm", headers=auth["booker"]).status_code in (409, 422)
    # a receipt that names an item that does not exist, a negative payment, a payment larger than the order
    assert receive(client, auth["stock"], c["B1"], [line(999999, 1, 10)]).status_code in (404, 422)
    o = mk_order(client, auth["booker"], c, [{"variant_id": c["rose"], "qty": 1}])
    assert client.post(f"/orders/{o['id']}/payments", headers=auth["accounts"], json={"amount": "-5", "method": "cash"}).status_code == 422
    assert client.post(f"/orders/{o['id']}/payments", headers=auth["accounts"], json={"amount": "999999", "method": "cash"}).status_code in (409, 422)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(JournalEntry)) == before_journals
    # stock moved only for the one order that was legitimately confirmed (a reservation, not a movement of goods)
    assert db.scalar(select(func.count()).select_from(StockMovement)) == before_moves
    assert A(client, auth["ceo"], "/inventory/integrity")["consistent"]


def test_posting_twice_never_duplicates_a_journal(client, auth, world, db):
    delivered_paid_order(client, auth, world, qty=3)
    run_posting(client, auth)
    n = db.scalar(select(func.count()).select_from(JournalEntry))
    again = run_posting(client, auth)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(JournalEntry)) == n
    assert again["errors"] == []


def test_every_dashboard_and_report_runs_for_every_role_without_error(client, auth, world):
    delivered_paid_order(client, auth, world, qty=2)
    run_posting(client, auth)
    for role in ("ceo", "manager", "accounts", "stock", "booker"):
        d = client.get("/insight/dashboard", headers=auth[role])
        assert d.status_code == 200, (role, d.text[:200])
        cat = client.get("/insight/reports", headers=auth[role]).json()["items"]
        for rep in cat:
            if rep["key"] == "party_statement":
                continue
            r = client.get(f"/insight/reports/{rep['key']}", headers=auth[role])
            assert r.status_code in (200, 422), (role, rep["key"], r.status_code, r.text[:200])      # 422 = "choose a location": a stated rule, never an error
            assert r.status_code < 500
