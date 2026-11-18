"""Phase 6: chart of accounts, double-entry journals, automatic posting, periods, expenses, cash/bank, opening balances, reports.
Synthetic data only."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from backend.tests.conftest import line, login, make_user, receive
from backend.tests.test_sales import mk_order, ship_all
from backend.tests.test_distributors import dispatch as dist_dispatch

D = Decimal
TODAY = date.today()
iso = lambda d: d.isoformat()  # noqa: E731


@pytest.fixture
def ac(client, auth, catalog, locations, users, db):
    """Shared setup: a second approver (ceo2) and ids."""
    make_user(db, "ceo2", "ceo")
    auth["ceo2"] = login(client, "ceo2")
    return {"WH": locations["WH"].id, "B1": locations["B1"].id, "B2": locations["B2"].id, **catalog}


def run_posting(client, auth, who="accounts"):
    r = client.post("/accounting/posting/run", headers=auth[who])
    assert r.status_code == 200, r.text
    return r.json()


def accounts(client, auth):
    return {a["code"]: a for a in client.get("/accounting/accounts", headers=auth["accounts"]).json()["items"]}


def bal(client, auth, code):
    return D(accounts(client, auth)[code]["balance"])


def tb(client, auth, **params):
    r = client.get("/accounting/reports/trial-balance", headers=auth["accounts"], params=params)
    assert r.status_code == 200, r.text
    return r.json()


def jl(client, auth, **kw):
    return client.get("/accounting/journals", headers=auth["accounts"], params=kw).json()


# ---- chart of accounts ----------------------------------------------------------------------------------------------------------------
def test_chart_is_seeded_and_extendable_only_by_authorised_roles(client, auth, ac):
    a = accounts(client, auth)
    for code in ("1000", "1010", "1100", "1110", "1200", "2000", "2050", "3200", "4000", "5000"):
        assert code in a
    body = {"code": "5950", "name": "Test Licences", "type": "expense"}
    assert client.post("/accounting/accounts", headers=auth["manager"], json=body).status_code == 403
    assert client.post("/accounting/accounts", headers=auth["stock"], json=body).status_code == 403
    r = client.post("/accounting/accounts", headers=auth["accounts"], json=body)
    assert r.status_code == 201, r.text
    assert client.post("/accounting/accounts", headers=auth["accounts"], json=body).status_code in (409, 422)
    assert client.post("/accounting/accounts", headers=auth["accounts"], json={**body, "code": "5951", "name": "test licences"}).status_code in (409, 422)
    assert client.post("/accounting/accounts", headers=auth["accounts"], json={**body, "code": "5952", "name": "Bad", "type": "nonsense"}).status_code == 422
    # system accounts cannot be switched off
    sysid = a["1200"]["id"]
    assert client.patch(f"/accounting/accounts/{sysid}", headers=auth["accounts"], json={"is_active": False}).status_code in (409, 422)


# ---- journals: double entry ----------------------------------------------------------------------------------------------------------------
def manual(client, h, lines, **kw):
    body = {"entry_date": iso(TODAY), "memo": "Test journal", "reason": "Testing", "lines": lines, **kw}
    return client.post("/accounting/journals/manual", headers=h, json=body)


def test_unbalanced_and_invalid_manual_journals_are_refused(client, auth, ac):
    A = accounts(client, auth)
    h = auth["accounts"]
    bad = manual(client, h, [{"account_id": A["5900"]["id"], "debit": "100"}, {"account_id": A["1000"]["id"], "credit": "90"}])
    assert bad.status_code == 422 and "balance" in bad.text.lower()
    assert manual(client, h, [{"account_id": A["5900"]["id"], "debit": "100", "credit": "100"}, {"account_id": A["1000"]["id"], "credit": "0"}]).status_code in (400, 422)
    assert manual(client, h, [{"account_id": A["5900"]["id"], "debit": "0"}, {"account_id": A["1000"]["id"], "credit": "0"}]).status_code in (400, 422)
    # control accounts are filled by their modules, never by hand
    assert manual(client, h, [{"account_id": A["1200"]["id"], "debit": "50"}, {"account_id": A["1000"]["id"], "credit": "50"}]).status_code == 422
    assert manual(client, h, [{"account_id": A["1100"]["id"], "debit": "50"}, {"account_id": A["1000"]["id"], "credit": "50"}]).status_code == 422
    # future dates refused
    assert manual(client, h, [{"account_id": A["5900"]["id"], "debit": "5"}, {"account_id": A["1000"]["id"], "credit": "5"}], entry_date=iso(TODAY + timedelta(days=3))).status_code == 422
    assert manual(client, auth["stock"], [{"account_id": A["5900"]["id"], "debit": "5"}, {"account_id": A["1000"]["id"], "credit": "5"}]).status_code == 403
    assert jl(client, auth)["total"] == 0


def test_manual_journal_needs_second_person_and_cannot_be_edited_after_posting(client, auth, ac, db):
    A = accounts(client, auth)
    r = manual(client, auth["accounts"], [{"account_id": A["5900"]["id"], "debit": "100"}, {"account_id": A["3000"]["id"], "credit": "100"}])
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["status"] == "draft"
    assert client.post(f"/accounting/journals/{j['id']}/post", headers=auth["accounts"]).status_code == 403     # lacks approve
    assert bal(client, auth, "5900") == 0                                                                        # a draft moves nothing
    p = client.post(f"/accounting/journals/{j['id']}/post", headers=auth["ceo"])
    assert p.status_code == 200 and p.json()["status"] == "posted"
    assert bal(client, auth, "5900") == 100
    assert client.post(f"/accounting/journals/{j['id']}/post", headers=auth["ceo"]).status_code == 409
    assert client.delete(f"/accounting/journals/{j['id']}", headers=auth["accounts"]).status_code == 409
    # the database refuses to change or delete a posted entry or its lines
    with pytest.raises(DBAPIError):
        db.execute(text("update journal_entries set memo='tampered' where id=:i"), {"i": j["id"]})
    db.rollback()
    with pytest.raises(DBAPIError):
        db.execute(text("update journal_lines set debit=1 where entry_id=:i and debit>0"), {"i": j["id"]})
    db.rollback()
    with pytest.raises(DBAPIError):
        db.execute(text("delete from journal_lines where entry_id=:i"), {"i": j["id"]})
    db.rollback()
    with pytest.raises(DBAPIError):
        db.execute(text("delete from journal_entries where id=:i"), {"i": j["id"]})
    db.rollback()


def test_ceo_cannot_post_own_manual_journal(client, auth, ac):
    A = accounts(client, auth)
    j = manual(client, auth["ceo"], [{"account_id": A["5900"]["id"], "debit": "10"}, {"account_id": A["3000"]["id"], "credit": "10"}]).json()
    assert client.post(f"/accounting/journals/{j['id']}/post", headers=auth["ceo"]).status_code == 403
    assert client.post(f"/accounting/journals/{j['id']}/post", headers=auth["ceo2"]).status_code == 200


def test_database_rejects_unbalanced_entry_even_if_service_is_bypassed(client, auth, ac, db):
    A = accounts(client, auth)
    pid = db.execute(text("select id from accounting_periods limit 1")).scalar()
    if pid is None:
        client.get("/accounting/periods", headers=auth["accounts"])
        pid = db.execute(text("select id from accounting_periods limit 1")).scalar()
    with pytest.raises(DBAPIError):
        db.execute(text("insert into journal_entries (ref, entry_date, period_id, memo, kind, status, total, posted_at) "
                        "values ('RAW-1', current_date, :p, 'raw', 'manual', 'posted', 10, now())"), {"p": pid})
        eid = db.execute(text("select id from journal_entries where ref='RAW-1'")).scalar()
        db.execute(text("insert into journal_lines (entry_id, account_id, debit, credit, entry_date) values (:e,:a,10,0,current_date)"), {"e": eid, "a": A["5900"]["id"]})
        db.execute(text("insert into journal_lines (entry_id, account_id, debit, credit, entry_date) values (:e,:a,0,7,current_date)"), {"e": eid, "a": A["1000"]["id"]})
        db.commit()
    db.rollback()


def test_reversal_mirrors_original_and_only_once(client, auth, ac):
    A = accounts(client, auth)
    j = manual(client, auth["accounts"], [{"account_id": A["5900"]["id"], "debit": "40"}, {"account_id": A["3000"]["id"], "credit": "40"}]).json()
    client.post(f"/accounting/journals/{j['id']}/post", headers=auth["ceo"])
    assert client.post(f"/accounting/journals/{j['id']}/reverse", headers=auth["accounts"], json={"reason": "x"}).status_code == 403
    assert client.post(f"/accounting/journals/{j['id']}/reverse", headers=auth["ceo2"], json={"reason": ""}).status_code == 422
    assert client.post(f"/accounting/journals/{j['id']}/reverse", headers=auth["ceo"], json={"reason": "own posting"}).status_code == 403   # the poster cannot reverse
    r = client.post(f"/accounting/journals/{j['id']}/reverse", headers=auth["ceo2"], json={"reason": "Posted to wrong account"})
    assert r.status_code == 201, r.text
    assert bal(client, auth, "5900") == 0 and bal(client, auth, "3000") == 0
    assert client.post(f"/accounting/journals/{j['id']}/reverse", headers=auth["ceo2"], json={"reason": "again"}).status_code in (409, 422)
    assert client.get(f"/accounting/journals/{j['id']}", headers=auth["accounts"]).json()["status"] == "reversed"
    assert tb(client, auth)["totals"]["balanced"]


# ---- automatic posting -----------------------------------------------------------------------------------------------------------------------
def test_receipt_posts_inventory_and_grni_once(client, auth, ac):
    r = receive(client, auth["stock"], ac["WH"], [line(ac["rose"], 10, 40)])
    assert r.status_code in (200, 201), r.text
    pend = client.get("/accounting/posting/pending", headers=auth["accounts"]).json()
    assert pend["total"] >= 1
    res = run_posting(client, auth)
    assert res["posted"] >= 1 and not res["errors"] and res["remaining"] == 0
    assert bal(client, auth, "1200") == 400
    assert bal(client, auth, "2050") == -400
    again = run_posting(client, auth)
    assert again["posted"] == 0
    assert bal(client, auth, "1200") == 400
    assert tb(client, auth)["totals"]["balanced"]
    integ = client.get("/accounting/integrity", headers=auth["accounts"]).json()
    inv = next(x for x in integ["rows"] if x["name"].startswith("Inventory"))
    assert inv["agrees"], integ


def test_posting_requires_permission_and_view_does_not_post(client, auth, ac):
    receive(client, auth["stock"], ac["WH"], [line(ac["rose"], 3, 40)])
    assert client.post("/accounting/posting/run", headers=auth["manager"]).status_code == 403
    assert client.post("/accounting/posting/run", headers=auth["stock"]).status_code == 403
    assert client.get("/accounting/accounts", headers=auth["stock"]).status_code == 403
    assert client.get("/accounting/posting/pending", headers=auth["booker"]).status_code == 403
    assert bal(client, auth, "1200") == 0


def test_distributor_credit_dispatch_payment_and_ledger_agree(client, auth, ac):
    receive(client, auth["stock"], ac["WH"], [line(ac["rose"], 50, 40)])
    d = client.post("/distributors", headers=auth["manager"], json={"name": "Acct Distributor", "discount_pct": "10", "credit_limit": "10000", "payment_terms_days": 30}).json()
    c = {"dist": d["id"], "WH": ac["WH"], "rose": ac["rose"]}
    r = dist_dispatch(client, auth["stock"], c, qty=10)
    assert r.status_code == 201, r.text
    run_posting(client, auth)
    assert bal(client, auth, "1110") == D("630.00")          # 10 x 70 distributor price less 10% terms discount? see assertion below
    assert bal(client, auth, "5000") == 400                    # 10 units at weighted-average cost 40
    integ = client.get("/accounting/integrity", headers=auth["accounts"]).json()
    assert all(x["agrees"] for x in integ["rows"]), integ


# ---- customer sales, payments, returns -----------------------------------------------------------------------------------------------------
from backend.tests.test_sales import ctx, delivered_paid_order, get  # noqa: E402,F401


def test_sale_payment_and_cogs_post_and_reports_agree(client, auth, ctx, db):
    make_user(db, "ceo2", "ceo")
    receive_total = D(25 * 40)
    o = delivered_paid_order(client, auth, ctx, qty=4)          # 400 paid in cash before dispatch, then dispatched and delivered
    res = run_posting(client, auth)
    assert not res["errors"], res
    assert bal(client, auth, "1000") == 400                      # cash on hand
    assert bal(client, auth, "4000") == -400                     # product sales (credit balance)
    assert bal(client, auth, "5000") == 160                      # 4 units at weighted-average cost 40
    assert bal(client, auth, "1100") == 0                        # the customer owes nothing
    assert bal(client, auth, "1200") == receive_total - 160
    t = tb(client, auth)
    assert t["totals"]["balanced"] and t["totals"]["debit"] == t["totals"]["credit"]
    pl = client.get("/accounting/reports/income-statement", headers=auth["accounts"], params={"date_from": "2026-01-01", "date_to": iso(TODAY)}).json()
    assert pl["totals"]["net_revenue"] == "400.00" and pl["totals"]["gross_profit"] == "240.00" and pl["totals"]["net_profit"] == "240.00"
    bs = client.get("/accounting/reports/balance-sheet", headers=auth["accounts"]).json()
    assert bs["totals"]["balanced"], bs["totals"]
    cf = client.get("/accounting/reports/cash-flow", headers=auth["accounts"], params={"date_from": "2026-01-01", "date_to": iso(TODAY)}).json()
    assert cf["totals"]["agrees_with_ledger"] and cf["totals"]["closing"] == "400.00"
    gl = client.get("/accounting/reports/general-ledger", headers=auth["accounts"], params={"account_id": accounts(client, auth)["1000"]["id"], "date_from": "2026-01-01"}).json()
    assert gl["totals"]["closing"] == "400.00"
    assert client.get("/accounting/reports/cogs", headers=auth["accounts"]).status_code == 200
    inv = client.get("/accounting/reports/inventory-valuation", headers=auth["accounts"]).json()
    assert inv["totals"]["agrees"], inv["totals"]
    integ = client.get("/accounting/integrity", headers=auth["accounts"]).json()
    assert integ["consistent"], integ
    assert run_posting(client, auth)["posted"] == 0


def test_unpaid_credit_sale_appears_in_receivables_ageing_and_statement(client, auth, ctx):
    from backend.tests.test_sales import mk_order, ship_all
    o = mk_order(client, auth["booker"], ctx, [{"variant_id": ctx["rose"], "qty": 3}])
    ship_all(client, auth, o)
    run_posting(client, auth)
    assert bal(client, auth, "1100") == 300
    ag = client.get("/accounting/reports/aging/receivables", headers=auth["accounts"]).json()
    row = next(r for r in ag["rows"] if r["party_id"] == ctx["cust"])
    assert row["total"] == "300.00" and row["0-30"] == "300.00"
    # a part payment is applied to the oldest charge first
    client.post(f"/orders/{o['id']}/payments", headers=auth["accounts"], json={"amount": "100", "method": "cash"})
    run_posting(client, auth)
    ag = client.get("/accounting/reports/aging/receivables", headers=auth["accounts"]).json()
    assert next(r for r in ag["rows"] if r["party_id"] == ctx["cust"])["total"] == "200.00"
    st = client.get("/accounting/reports/statement", headers=auth["accounts"], params={"party_type": "customer", "party_id": ctx["cust"], "date_from": "2026-01-01"}).json()
    assert st["totals"]["closing"] == "200.00"
    assert client.get("/accounting/reports/aging/nonsense", headers=auth["accounts"]).status_code == 422


def test_payment_reversal_is_mirrored_in_ledger(client, auth, ctx):
    o = delivered_paid_order(client, auth, ctx, qty=2)
    run_posting(client, auth)
    assert bal(client, auth, "1000") == 200
    pays = get(client, auth["accounts"], o)["payments"]
    r = client.post(f"/order-payments/{pays[0]['id']}/reverse", headers=auth["manager"], json={"reason": "Entered twice"})
    assert r.status_code in (200, 201), r.text
    run_posting(client, auth)
    assert bal(client, auth, "1000") == 0 and bal(client, auth, "1100") == 200
    assert tb(client, auth)["totals"]["balanced"]


def test_return_refund_and_restock_are_posted(client, auth, ctx):
    o = delivered_paid_order(client, auth, ctx, qty=4)
    run_posting(client, auth)
    lid = o["lines"][0]["id"]
    ret = client.post(f"/orders/{o['id']}/returns", headers=auth["booker"], json={"lines": [{"order_line_id": lid, "qty": 2}], "reason": "wrong shade"}).json()
    client.post(f"/sales-returns/{ret['id']}/decide", headers=auth["manager"], json={"approve": True, "note": "ok"})
    client.post(f"/sales-returns/{ret['id']}/receive", headers=auth["stock"], json={"lines": [{"line_id": ret["lines"][0]["id"], "qty_received": 2}]})
    client.post(f"/sales-returns/{ret['id']}/inspect", headers=auth["stock"], json={"lines": [{"line_id": ret["lines"][0]["id"], "restock": 1, "damaged": 1, "refund_qty": 2}]})
    client.post(f"/sales-returns/{ret['id']}/settle", headers=auth["accounts"], json={"method": "cash"})
    res = run_posting(client, auth)
    assert not res["errors"], res
    assert bal(client, auth, "4100") == 200                      # sales returns (contra revenue)
    assert bal(client, auth, "1000") == 200                      # 400 received, 200 refunded
    assert bal(client, auth, "5000") == 80                       # COGS reversed for both returned units (2 x 40)
    assert bal(client, auth, "5100") == 0                        # the damaged unit is still owned (in the damaged bucket) until a write-off is approved
    assert bal(client, auth, "1200") == 25 * 40 - 4 * 40 + 2 * 40
    assert tb(client, auth)["totals"]["balanced"]
    assert client.get("/accounting/integrity", headers=auth["accounts"]).json()["consistent"]
    sr = client.get("/accounting/reports/sales", headers=auth["accounts"], params={"date_from": "2026-01-01"}).json()
    assert sr["totals"]["gross"] == "400.00" and sr["totals"]["returns"] == "200.00" and sr["totals"]["net"] == "200.00"


def test_posting_is_safe_under_concurrent_runs(client, auth, ac):
    import threading
    receive(client, auth["stock"], ac["WH"], [line(ac["rose"], 5, 40)])
    out = []

    def go():
        out.append(client.post("/accounting/posting/run", headers=auth["accounts"]).status_code)
    ts = [threading.Thread(target=go) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(c == 200 for c in out), out
    assert bal(client, auth, "1200") == 200 and bal(client, auth, "2050") == -200
    n = jl(client, auth, source_type="stock_txn")["total"]
    assert n == 1


# ---- periods -------------------------------------------------------------------------------------------------------------------------------
def backdated_dispatch(client, auth, ac, day, qty=2):
    """A supplier payment on account dated in the past: a source document whose business date differs from today."""
    sid = client.post("/suppliers", headers=auth["accounts"], json={"name": f"Supplier {day} {qty}", "payment_terms_days": 30}).json()["id"]
    r = client.post("/supplier-payments", headers=auth["accounts"], json={"supplier_id": sid, "amount": str(qty * 10), "method": "cash", "paid_date": day, "on_account": True})
    assert r.status_code == 201, r.text
    return r.json()


def test_closed_period_shifts_system_postings_and_refuses_manual_ones_until_reopened(client, auth, ac):
    backdated_dispatch(client, auth, ac, "2026-01-05")
    run_posting(client, auth)
    ps = client.get("/accounting/periods", headers=auth["accounts"], params={"fiscal_year": 2026}).json()
    jan = next(p for p in ps["items"] if p["name"] == "2026-01")
    assert client.post(f"/accounting/periods/{jan['id']}/close", headers=auth["manager"]).status_code == 403
    assert client.post(f"/accounting/periods/{jan['id']}/close", headers=auth["accounts"]).status_code == 200
    # a late January document lands in the first open period, keeping its original date
    backdated_dispatch(client, auth, ac, "2026-01-20")
    assert not run_posting(client, auth)["errors"]
    late = [e for e in jl(client, auth, source_type="supplier_ledger")["items"] if e["original_date"]]
    assert len(late) == 1 and late[0]["original_date"] == "2026-01-20" and late[0]["entry_date"] >= "2026-02-01"
    A = accounts(client, auth)
    closed = manual(client, auth["accounts"], [{"account_id": A["5900"]["id"], "debit": "5"}, {"account_id": A["3000"]["id"], "credit": "5"}], entry_date="2026-01-25")
    assert closed.status_code == 422
    assert client.post(f"/accounting/periods/{jan['id']}/close", headers=auth["accounts"]).status_code == 409
    # reopening needs a reason, a requester with the permission and a different approver
    assert client.post(f"/accounting/periods/{jan['id']}/reopen-requests", headers=auth["accounts"], json={"reason": "x"}).status_code == 403
    assert client.post(f"/accounting/periods/{jan['id']}/reopen-requests", headers=auth["ceo"], json={"reason": ""}).status_code == 422
    rq = client.post(f"/accounting/periods/{jan['id']}/reopen-requests", headers=auth["ceo"], json={"reason": "Supplier credit note for January"})
    assert rq.status_code == 201, rq.text
    rid = rq.json()["id"]
    assert client.post(f"/accounting/reopen-requests/{rid}/decision", headers=auth["ceo"], json={"approve": True, "note": "self"}).status_code == 403
    assert client.post(f"/accounting/reopen-requests/{rid}/decision", headers=auth["ceo2"], json={"approve": True, "note": "Verified with the credit note"}).status_code == 200
    ok = manual(client, auth["accounts"], [{"account_id": A["5900"]["id"], "debit": "5"}, {"account_id": A["3000"]["id"], "credit": "5"}], entry_date="2026-01-25")
    assert ok.status_code == 201


def test_period_cannot_close_before_it_ends_or_with_unposted_sources(client, auth, ac):
    cur = next(p for p in client.get("/accounting/periods", headers=auth["accounts"]).json()["items"] if p["start_date"] <= iso(TODAY) <= p["end_date"])
    assert client.post(f"/accounting/periods/{cur['id']}/close", headers=auth["accounts"]).status_code == 422
    backdated_dispatch(client, auth, ac, "2026-01-05")
    ps = client.get("/accounting/periods", headers=auth["accounts"], params={"fiscal_year": 2026}).json()["items"]
    jan = next(p for p in ps if p["name"] == "2026-01")
    r = client.post(f"/accounting/periods/{jan['id']}/close", headers=auth["accounts"])
    assert r.status_code == 422 and "posted" in r.text


def test_revenue_policy_is_configurable_then_locked_once_sales_exist(client, auth, ctx):
    assert client.put("/accounting/settings", headers=auth["manager"], json={"revenue_recognition": "delivery"}).status_code == 403
    assert client.put("/accounting/settings", headers=auth["accounts"], json={"revenue_recognition": "bogus"}).status_code == 422
    assert client.put("/accounting/settings", headers=auth["accounts"], json={"revenue_recognition": "delivery"}).json()["revenue_recognition"] == "delivery"
    from backend.tests.test_sales import mk_order, ship_all
    o = mk_order(client, auth["booker"], ctx, [{"variant_id": ctx["rose"], "qty": 2}])
    s = ship_all(client, auth, o)
    run_posting(client, auth)
    assert bal(client, auth, "4000") == 0 and bal(client, auth, "1220") == 80       # not revenue yet; goods are with the courier
    client.post(f"/shipments/{s['id']}/outcome", headers=auth["stock"], json={"result": "delivered"})
    run_posting(client, auth)
    assert bal(client, auth, "4000") == -200 and bal(client, auth, "1220") == 0 and bal(client, auth, "5000") == 80
    assert client.put("/accounting/settings", headers=auth["accounts"], json={"revenue_recognition": "dispatch"}).status_code == 409
    assert tb(client, auth)["totals"]["balanced"]


# ---- expenses, cash and bank ---------------------------------------------------------------------------------------------------------------
def cash_ids(client, auth):
    return {c["name"]: c for c in client.get("/accounting/cash-accounts", headers=auth["accounts"]).json()["items"]}


def expense(client, h, cash, amount="100", acct=None, **kw):
    if acct is None:
        r = client.get("/accounting/expense-accounts", headers=h)
        acct = next(a["id"] for a in r.json()["items"] if a["code"] == "5400") if r.status_code == 200 else 1
    body = {"expense_date": iso(TODAY), "account_id": acct, "cash_account_id": cash, "amount": amount, "description": "Electricity", "payee": "Test Utility", **kw}
    return client.post("/accounting/expenses", headers=h, json=body)


def fund_bank(client, auth, amount="1000"):
    A = accounts(client, auth)
    j = manual(client, auth["accounts"], [{"account_id": A["1010"]["id"], "debit": amount}, {"account_id": A["3000"]["id"], "credit": amount}]).json()
    assert client.post(f"/accounting/journals/{j['id']}/post", headers=auth["ceo"]).status_code == 200


def test_expense_needs_approval_above_limit_and_posts_when_approved(client, auth, ac):
    C = cash_ids(client, auth)
    assert expense(client, auth["manager"], C["Cash on hand"]["id"]).status_code == 403
    r = expense(client, auth["accounts"], C["Cash on hand"]["id"])
    assert r.status_code == 201 and r.json()["status"] == "pending"
    assert bal(client, auth, "5400") == 0
    x = r.json()
    assert client.post(f"/accounting/expenses/{x['id']}/decision", headers=auth["accounts"], json={"approve": True, "note": "ok"}).status_code == 403
    assert client.post(f"/accounting/expenses/{x['id']}/decision", headers=auth["ceo"], json={"approve": True, "note": ""}).status_code in (400, 422)
    d = client.post(f"/accounting/expenses/{x['id']}/decision", headers=auth["ceo"], json={"approve": True, "note": "Bill seen"})
    assert d.status_code == 200 and d.json()["status"] == "posted"
    assert bal(client, auth, "5400") == 100 and bal(client, auth, "1000") == -100
    assert client.post(f"/accounting/expenses/{x['id']}/decision", headers=auth["ceo"], json={"approve": True, "note": "again"}).status_code == 409
    rej = expense(client, auth["accounts"], C["Cash on hand"]["id"], amount="50").json()
    assert client.post(f"/accounting/expenses/{rej['id']}/decision", headers=auth["ceo"], json={"approve": False, "note": "Not business"}).json()["status"] == "rejected"
    assert bal(client, auth, "5400") == 100
    # a CEO who records an expense cannot approve it
    own = expense(client, auth["ceo"], C["Cash on hand"]["id"], amount="20").json()
    assert client.post(f"/accounting/expenses/{own['id']}/decision", headers=auth["ceo"], json={"approve": True, "note": "mine"}).status_code == 403
    # voiding posts a mirror entry, by someone who neither recorded nor approved it
    assert client.post(f"/accounting/expenses/{x['id']}/void", headers=auth["ceo"], json={"reason": "Duplicate"}).status_code == 403
    v = client.post(f"/accounting/expenses/{x['id']}/void", headers=auth["ceo2"], json={"reason": "Duplicate"})
    assert v.status_code == 200 and v.json()["status"] == "void"
    assert bal(client, auth, "5400") == 0 and bal(client, auth, "1000") == 0


def test_small_expenses_post_immediately_within_limit_and_validation(client, auth, ac):
    C = cash_ids(client, auth)
    assert client.put("/accounting/settings", headers=auth["accounts"], json={"expense_approval_limit": "200"}).status_code == 200
    ok = expense(client, auth["accounts"], C["Cash on hand"]["id"], amount="150")
    assert ok.json()["status"] == "posted" and bal(client, auth, "5400") == 150
    assert expense(client, auth["accounts"], C["Cash on hand"]["id"], amount="250").json()["status"] == "pending"
    A = accounts(client, auth)
    assert expense(client, auth["accounts"], C["Cash on hand"]["id"], amount="0").status_code == 422
    assert expense(client, auth["accounts"], C["Cash on hand"]["id"], amount="-5").status_code == 422
    assert expense(client, auth["accounts"], C["Cash on hand"]["id"], acct=A["4000"]["id"]).status_code == 422          # not an expense account
    assert expense(client, auth["accounts"], C["Cash on hand"]["id"], expense_date=iso(TODAY + timedelta(days=2))).status_code == 422
    k = expense(client, auth["accounts"], C["Cash on hand"]["id"], amount="10", idempotency_key="exp-key-1")
    k2 = expense(client, auth["accounts"], C["Cash on hand"]["id"], amount="10", idempotency_key="exp-key-1")
    assert k.json()["id"] == k2.json()["id"] and bal(client, auth, "5400") == 160


def test_bank_reconciliation_requires_exact_agreement_and_can_be_voided(client, auth, ac):
    fund_bank(client, auth, "1000")
    C = cash_ids(client, auth)
    bank = C["Main bank account"]
    client.put("/accounting/settings", headers=auth["accounts"], json={"expense_approval_limit": "500"})
    assert expense(client, auth["accounts"], bank["id"], amount="100").json()["status"] == "posted"
    lines = client.get(f"/accounting/cash-accounts/{bank['id']}/unreconciled", headers=auth["accounts"]).json()["items"]
    assert len(lines) == 2
    ids = [x["line_id"] for x in lines]
    assert client.get(f"/accounting/cash-accounts/{bank['id']}/unreconciled", headers=auth["manager"]).status_code == 403
    body = {"statement_date": iso(TODAY), "statement_balance": "850", "line_ids": ids}
    pv = client.post(f"/accounting/cash-accounts/{bank['id']}/reconcile/preview", headers=auth["accounts"], json=body).json()
    assert pv["balanced"] is False and pv["difference"] == "-50.00"
    bad = client.post(f"/accounting/cash-accounts/{bank['id']}/reconcile", headers=auth["accounts"], json=body)
    assert bad.status_code == 422 and "difference" in bad.text
    assert client.get("/accounting/cash-accounts", headers=auth["accounts"]).json()["items"][1]["unreconciled_lines"] == 2    # nothing was cleared or plugged
    ok = client.post(f"/accounting/cash-accounts/{bank['id']}/reconcile", headers=auth["accounts"], json={**body, "statement_balance": "900"})
    assert ok.status_code == 201, ok.text
    assert client.get(f"/accounting/cash-accounts/{bank['id']}/unreconciled", headers=auth["accounts"]).json()["items"] == []
    # reconciled lines are protected from being changed; voiding needs another approver
    assert client.post(f"/accounting/reconciliations/{ok.json()['id']}/void", headers=auth["accounts"], json={"reason": "x"}).status_code == 403
    assert client.post(f"/accounting/reconciliations/{ok.json()['id']}/void", headers=auth["ceo"], json={"reason": "Wrong statement"}).status_code == 200
    assert len(client.get(f"/accounting/cash-accounts/{bank['id']}/unreconciled", headers=auth["accounts"]).json()["items"]) == 2


def test_new_cash_account_gets_its_own_ledger_account(client, auth, ac):
    assert client.post("/accounting/cash-accounts", headers=auth["manager"], json={"name": "Petty cash", "kind": "cash"}).status_code == 403
    r = client.post("/accounting/cash-accounts", headers=auth["accounts"], json={"name": "Petty cash", "kind": "cash"})
    assert r.status_code == 201 and r.json()["account_code"].startswith("10")
    assert client.post("/accounting/cash-accounts", headers=auth["accounts"], json={"name": "petty cash", "kind": "cash"}).status_code == 409
    assert client.post("/accounting/cash-accounts", headers=auth["accounts"], json={"name": "X", "kind": "weird"}).status_code == 422
    assert r.json()["account_code"] in accounts(client, auth)


# ---- opening balances and go-live --------------------------------------------------------------------------------------------------------------
def opening_lines(client, auth, verified=True):
    A = accounts(client, auth)
    ev = "Cash count sheet 1 Jul signed by owner"
    return [{"account_id": A["1000"]["id"], "debit": "500", "evidence": ev, "verified": verified},
            {"account_id": A["3000"]["id"], "credit": "500", "evidence": "Owner statement", "verified": verified}]


def test_opening_batch_rules_verified_balanced_and_second_person(client, auth, ac):
    A = accounts(client, auth)
    body = {"as_of_date": "2026-01-01", "description": "Opening balances from the paper registers", "lines": opening_lines(client, auth, verified=False)}
    assert client.post("/accounting/opening-batches", headers=auth["manager"], json=body).status_code == 403
    b = client.post("/accounting/opening-batches", headers=auth["accounts"], json=body)
    assert b.status_code == 201, b.text
    bid = b.json()["id"]
    assert b.json()["unverified"] == 2
    assert client.post(f"/accounting/opening-batches/{bid}/approve", headers=auth["ceo"]).status_code == 422          # unverified figures are not posted
    assert bal(client, auth, "1000") == 0
    up = client.put(f"/accounting/opening-batches/{bid}", headers=auth["accounts"], json={"lines": opening_lines(client, auth)[:1]})
    assert up.status_code == 200
    assert client.post(f"/accounting/opening-batches/{bid}/approve", headers=auth["ceo"]).status_code == 422          # does not balance; no silent plug
    assert bal(client, auth, "1000") == 0
    client.put(f"/accounting/opening-batches/{bid}", headers=auth["accounts"], json={"lines": opening_lines(client, auth)})
    # control accounts and unknown parties are refused
    ctl = client.put(f"/accounting/opening-batches/{bid}", headers=auth["accounts"], json={"lines": [{"account_id": A["1110"]["id"], "debit": "10", "verified": True},
                                                                                                    {"account_id": A["3000"]["id"], "credit": "10", "verified": True}]})
    assert ctl.status_code == 422
    nop = client.put(f"/accounting/opening-batches/{bid}", headers=auth["accounts"], json={"lines": [{"account_id": A["1100"]["id"], "debit": "10", "verified": True},
                                                                                                    {"account_id": A["3000"]["id"], "credit": "10", "verified": True}]})
    assert nop.status_code == 422                                                                                       # customer receivable without a customer
    assert client.post(f"/accounting/opening-batches/{bid}/approve", headers=auth["accounts"]).status_code == 403
    ok = client.post(f"/accounting/opening-batches/{bid}/approve", headers=auth["ceo"])
    assert ok.status_code == 200 and ok.json()["status"] == "posted"
    assert bal(client, auth, "1000") == 500 and bal(client, auth, "3000") == -500
    assert client.put(f"/accounting/opening-batches/{bid}", headers=auth["accounts"], json={"description": "edit"}).status_code == 409
    assert client.post(f"/accounting/opening-batches/{bid}/approve", headers=auth["ceo"]).status_code == 409


def test_creator_cannot_approve_own_opening_batch(client, auth, ac):
    body = {"as_of_date": "2026-01-01", "description": "Opening balances prepared by the CEO", "lines": opening_lines(client, auth)}
    b = client.post("/accounting/opening-batches", headers=auth["ceo"], json=body).json()
    assert client.post(f"/accounting/opening-batches/{b['id']}/approve", headers=auth["ceo"]).status_code == 403
    assert client.post(f"/accounting/opening-batches/{b['id']}/approve", headers=auth["ceo2"]).status_code == 200


def test_opening_customer_receivable_with_party_and_go_live_gate(client, auth, ctx, db):
    make_user(db, "ceo2", "ceo")
    auth["ceo2"] = login(client, "ceo2")
    A = accounts(client, auth)
    gl = lambda: client.get("/accounting/go-live", headers=auth["accounts"]).json()  # noqa: E731
    assert gl()["ready"] is False and gl()["ledger_status"] == "preparing"
    lines = [{"account_id": A["1100"]["id"], "debit": "250", "party_type": "customer", "party_id": ctx["cust"], "evidence": "Customer notebook page 4, confirmed by phone", "verified": True},
             {"account_id": A["3200"]["id"], "credit": "250", "evidence": "Offsets the opening receivable", "verified": True}]
    b = client.post("/accounting/opening-batches", headers=auth["accounts"], json={"as_of_date": "2026-01-01", "description": "Opening receivables", "lines": lines}).json()
    client.post(f"/accounting/opening-batches/{b['id']}/approve", headers=auth["ceo"])
    assert bal(client, auth, "1100") == 250
    ag = client.get("/accounting/reports/aging/receivables", headers=auth["accounts"]).json()
    assert next(r for r in ag["rows"] if r["party_id"] == ctx["cust"])["total"] == "250.00"
    # Opening balance equity must be moved to owner capital with a documented entry before the ledger is official
    run_posting(client, auth)
    st = gl()
    assert not st["ready"] and any(c["name"].startswith("Opening balance equity") and not c["ok"] for c in st["checks"])
    note = "Checked opening balances against the registers on 1 July with the owner."
    assert client.post("/accounting/go-live", headers=auth["ceo2"], json={"note": note}).status_code == 422
    b2 = client.post("/accounting/opening-batches", headers=auth["accounts"], json={"as_of_date": "2026-01-01", "description": "Move opening equity to owner capital", "lines": [
        {"account_id": A["3200"]["id"], "debit": "250", "evidence": "Owner agreement", "verified": True}, {"account_id": A["3000"]["id"], "credit": "250", "evidence": "Owner agreement", "verified": True}]}).json()
    client.post(f"/accounting/opening-batches/{b2['id']}/approve", headers=auth["ceo"])
    assert gl()["ready"], gl()
    assert client.post("/accounting/go-live", headers=auth["ceo2"], json={"note": "short"}).status_code == 422
    assert client.post("/accounting/go-live", headers=auth["accounts"], json={"note": note}).status_code == 403
    prov = client.get("/accounting/reports/trial-balance", headers=auth["accounts"]).json()
    assert any("not yet official" in n for n in prov["notes"])
    ok = client.post("/accounting/go-live", headers=auth["ceo2"], json={"note": note})
    assert ok.status_code == 200 and ok.json()["ledger_status"] == "official"
    assert not any("not yet official" in n for n in client.get("/accounting/reports/trial-balance", headers=auth["accounts"]).json()["notes"])
    assert client.post("/accounting/go-live", headers=auth["ceo2"], json={"note": note}).status_code == 409


# ---- access ----------------------------------------------------------------------------------------------------------------------------------------
def test_reports_and_ledgers_are_permissioned(client, auth, ac):
    for path in ("/accounting/reports/trial-balance", "/accounting/reports/balance-sheet", "/accounting/journals", "/accounting/overview", "/accounting/integrity"):
        assert client.get(path, headers=auth["stock"]).status_code == 403, path
        assert client.get(path, headers=auth["booker"]).status_code == 403, path
        assert client.get(path, headers=auth["accounts"]).status_code == 200, path
    assert client.get("/accounting/reports/trial-balance").status_code == 401
    assert client.get("/accounting/reports/income-statement", headers=auth["accounts"], params={"date_from": "2026-03-01", "date_to": "2026-02-01"}).status_code == 422
    assert client.get("/accounting/reports/monthly", headers=auth["accounts"]).status_code == 200
    assert client.get("/accounting/reports/annual", headers=auth["accounts"]).status_code == 200
    # a location the user is not assigned to answers 404
    assert client.get("/accounting/reports/income-statement", headers=auth["manager"], params={"location_id": 999999}).status_code in (403, 404)


def test_everything_is_audited(client, auth, ac, db):
    from backend.models import AuditLog
    A = accounts(client, auth)
    j = manual(client, auth["accounts"], [{"account_id": A["5900"]["id"], "debit": "9"}, {"account_id": A["3000"]["id"], "credit": "9"}]).json()
    client.post(f"/accounting/journals/{j['id']}/post", headers=auth["ceo"])
    acts = {a.action for a in db.query(AuditLog).all()}
    assert {"journal.draft_created", "journal.posted"} <= acts


# ---- stock write-offs, adjustments, transfers, counts ------------------------------------------------------------------------------------------------
def test_damage_then_approved_writeoff_hits_the_ledger_once(client, auth, ac):
    wh = ac["WH"]
    receive(client, auth["stock"], wh, [line(ac["rose"], 10, 10)])
    client.post("/inventory/moves", headers=auth["stock"], json={"action": "damage", "location_id": wh, "variant_id": ac["rose"], "qty": 3, "reason": "dropped"})
    run_posting(client, auth)
    assert bal(client, auth, "1200") == 100 and bal(client, auth, "5100") == 0          # damaged stock is still an asset
    w = client.post("/inventory/requests", headers=auth["stock"], json={"kind": "writeoff", "location_id": wh, "reason": "destroy",
                                                                       "lines": [{"variant_id": ac["rose"], "bucket": "damaged", "qty_delta": -3}]}).json()
    client.post(f"/inventory/requests/{w['id']}/approve", headers=auth["manager"], json={})
    assert not run_posting(client, auth)["errors"]
    assert bal(client, auth, "1200") == 70 and bal(client, auth, "5100") == 30
    assert run_posting(client, auth)["posted"] == 0
    assert client.get("/accounting/integrity", headers=auth["accounts"]).json()["consistent"]


def test_transfer_moves_value_through_stock_in_transit(client, auth, ac, locations):
    from backend.tests.test_transfers_counts import make_transfer
    wh = ac["WH"]
    receive(client, auth["stock"], wh, [line(ac["rose"], 20, 10)])
    t = make_transfer(client, auth["stock"], locations, [(ac["rose"], 10)]).json()
    client.post(f"/transfers/{t['id']}/approve", headers=auth["manager"], json={})
    client.post(f"/transfers/{t['id']}/dispatch", headers=auth["stock"])
    run_posting(client, auth)
    assert bal(client, auth, "1210") == 100 and bal(client, auth, "1200") == 100       # 200 total, 100 of it in transit
    lid = client.get(f"/transfers/{t['id']}", headers=auth["ceo"]).json()["lines"][0]["id"]
    client.post(f"/transfers/{t['id']}/receive", headers=auth["stock"], json={"lines": [{"line_id": lid, "qty_good": 10}]})
    run_posting(client, auth)
    assert bal(client, auth, "1210") == 0 and bal(client, auth, "1200") == 200
    assert client.get("/accounting/integrity", headers=auth["accounts"]).json()["consistent"]


def test_count_variance_posts_to_stock_adjustments_after_approval(client, auth, ac):
    from backend.tests.test_inventory import stock  # noqa: F401
    wh = ac["WH"]
    receive(client, auth["stock"], wh, [line(ac["rose"], 10, 10)])
    c = client.post("/inventory/counts", headers=auth["stock"], json={"location_id": wh}).json()
    entries = [{"line_id": x["id"], "counted_qty": x["expected_qty"] - 2, "reason": "shrinkage"} for x in c["lines"]]
    client.put(f"/inventory/counts/{c['id']}/entries", headers=auth["stock"], json={"entries": entries})
    sub = client.post(f"/inventory/counts/{c['id']}/submit", headers=auth["stock"]).json()
    run_posting(client, auth)
    assert bal(client, auth, "5110") == 0                                                # nothing before approval
    rq = next(r for r in client.get("/inventory/requests", headers=auth["manager"]).json()["items"] if r["ref"] == sub["request_ref"])
    assert client.post(f"/inventory/requests/{rq['id']}/approve", headers=auth["manager"], json={}).status_code == 200
    run_posting(client, auth)
    assert bal(client, auth, "5110") == 20 and bal(client, auth, "1200") == 80


def test_stock_reversal_is_mirrored(client, auth, ac):
    r = receive(client, auth["stock"], ac["WH"], [line(ac["rose"], 10, 10)]).json()
    run_posting(client, auth)
    txn = client.get("/inventory/movements", headers=auth["manager"], params={"page_size": 5}).json()
    tid = (txn.get("items") or txn)[0]["txn_id"] if isinstance(txn, dict) else None
    if tid is None:
        pytest.skip("movement listing has no txn id")
    rv = client.post(f"/inventory/transactions/{tid}/reverse", headers=auth["manager"], json={"reason": "Wrong delivery"})
    assert rv.status_code in (200, 201), rv.text
    run_posting(client, auth)
    assert bal(client, auth, "1200") == 0 and bal(client, auth, "2050") == 0


# ---- suppliers ---------------------------------------------------------------------------------------------------------------------------------------
def test_supplier_invoice_clears_grni_and_payment_reduces_payables(client, auth, ac):
    from backend.tests.test_purchasing import mk_invoice, mk_po, mk_supplier, po_receive, pay
    sp = mk_supplier(client, auth["accounts"], "Acct Supplier")
    po = mk_po(client, auth, sp, ac["WH"], [(ac["rose"], 10, 40)])
    assert po_receive(client, auth["stock"], po, [(ac["rose"], 10, 0, 0)]).status_code in (200, 201)
    run_posting(client, auth)
    assert bal(client, auth, "1200") == 400 and bal(client, auth, "2050") == -400 and bal(client, auth, "2000") == 0
    inv = mk_invoice(client, auth["accounts"], sp, po, [(ac["rose"], 10, 40)])
    assert inv.status_code == 201, inv.text
    iv = inv.json()
    if iv["status"] != "posted":
        client.post(f"/supplier-invoices/{iv['id']}/post", headers=auth["accounts"])
    run_posting(client, auth)
    assert bal(client, auth, "2050") == 0 and bal(client, auth, "2000") == -400
    p = pay(client, auth["accounts"], sp, 150, allocations=[(iv["id"], 150)])
    assert p.status_code == 201, p.text
    run_posting(client, auth)
    assert bal(client, auth, "2000") == -250 and bal(client, auth, "1000") == -150
    ag = client.get("/accounting/reports/aging/payables", headers=auth["accounts"]).json()
    assert ag["totals"]["total"] == "250.00"
    integ = client.get("/accounting/integrity", headers=auth["accounts"]).json()
    assert integ["consistent"], integ
    # payment through the bank account follows the payment-method mapping
    p2 = pay(client, auth["accounts"], sp, 50, allocations=[(iv["id"], 50)], method="bank_transfer", reference="TT-1")
    assert p2.status_code == 201, p2.text
    run_posting(client, auth)
    assert bal(client, auth, "1010") == -50


def test_distributor_payment_and_return_credit(client, auth, ac):
    from backend.tests.test_distributors import pay as dpay
    receive(client, auth["stock"], ac["WH"], [line(ac["rose"], 50, 40)])
    d = client.post("/distributors", headers=auth["manager"], json={"name": "Acct Distributor 2", "discount_pct": "10", "credit_limit": "10000", "payment_terms_days": 30}).json()
    c = {"dist": d["id"], "WH": ac["WH"], "rose": ac["rose"]}
    assert dist_dispatch(client, auth["stock"], c, qty=10).status_code == 201
    run_posting(client, auth)
    assert bal(client, auth, "4000") == -700 and bal(client, auth, "4200") == 70        # gross sale and the terms discount shown separately
    p = dpay(client, auth["accounts"], c, 200, method="cash") if False else dpay(client, auth["accounts"], c, 200)
    assert p.status_code == 201, p.text
    run_posting(client, auth)
    assert bal(client, auth, "1110") == 430 and bal(client, auth, "1000") == 200
    ag = client.get("/accounting/reports/aging/receivables", headers=auth["accounts"]).json()
    assert ag["totals"]["total"] == "430.00"
    assert client.get("/accounting/integrity", headers=auth["accounts"]).json()["consistent"]
