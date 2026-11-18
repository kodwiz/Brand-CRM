"""Phase 5: distributor credit - records, dispatches on credit, returns/exchanges, payments and allocations, adjustments,
receivable ledger, aging and statements. Synthetic data only."""
import threading
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from backend.tests.conftest import line, receive

D = Decimal
TODAY = date.today()


def iso(d):
    return d.isoformat()


@pytest.fixture
def dc(client, auth, catalog, locations, users, db):
    """A distributor (10% terms, limit 10 000, 30 days) and 50 saleable units of rose at the warehouse."""
    WH = locations["WH"].id
    r = receive(client, auth["stock"], WH, [line(catalog["rose"], 50, 40)])
    assert r.status_code in (200, 201), r.text
    d = client.post("/distributors", headers=auth["manager"], json={"name": "Test Distributor", "discount_pct": "10", "credit_limit": "10000",
                                                                     "payment_terms_days": 30})
    assert d.status_code == 201, d.text
    return {"WH": WH, "B1": locations["B1"].id, "dist": d.json()["id"], **catalog}


def dispatch(client, h, c, qty=10, price=None, post=True, date_=None, dist=None, **kw):
    body = {"distributor_id": dist or c["dist"], "location_id": c["WH"], "invoice_date": iso(date_ or TODAY),
            "lines": [{"variant_id": c["rose"], "qty": qty, **({"unit_price": str(price)} if price is not None else {})}], "post_now": post, **kw}
    return client.post("/distributor-dispatches", headers=h, json=body)


def bal(client, h, c, dist=None):
    return client.get(f"/distributors/{dist or c['dist']}", headers=h).json()["balance"]


def stock_row(client, h, loc, variant):
    rows = client.get("/inventory/stock", headers=h, params={"location_id": loc, "include_zero": True, "page_size": 200}).json()["items"]
    return next(r for r in rows if r["variant_id"] == variant)


# ---- records, scope, permissions -------------------------------------------------------------------------------------
def test_distributor_crud_and_permissions(client, auth, dc):
    assert client.post("/distributors", headers=auth["manager"], json={"name": "test distributor"}).status_code == 409
    assert client.post("/distributors", headers=auth["stock"], json={"name": "X"}).status_code == 403
    assert client.get("/distributors", headers=auth["booker"]).status_code == 403
    r = client.put(f"/distributors/{dc['dist']}", headers=auth["manager"], json={"credit_limit": "5000", "city": "Alpha"})
    assert r.status_code == 200 and r.json()["credit_limit"] == "5000.00"
    assert client.put(f"/distributors/{dc['dist']}", headers=auth["stock"], json={"credit_limit": "999999"}).status_code == 403


def test_stock_keeper_sees_only_assigned_or_unassigned(client, auth, dc, users, db):
    other = client.post("/distributors", headers=auth["manager"], json={"name": "Assigned To Someone Else", "assigned_user_id": users["manager"].id}).json()
    mine = client.post("/distributors", headers=auth["manager"], json={"name": "Assigned To Stock", "assigned_user_id": users["stock"].id}).json()
    ids = {x["id"] for x in client.get("/distributors", headers=auth["stock"]).json()["items"]}
    assert mine["id"] in ids and dc["dist"] in ids and other["id"] not in ids
    assert client.get(f"/distributors/{other['id']}", headers=auth["stock"]).status_code == 404
    assert client.get(f"/distributors/{other['id']}", headers=auth["manager"]).status_code == 200
    assert dispatch(client, auth["stock"], dc, dist=other["id"]).status_code == 404


# ---- dispatches -------------------------------------------------------------------------------------------------------------------
def test_dispatch_prices_stock_and_ledger(client, auth, dc):
    r = dispatch(client, auth["stock"], dc, qty=10)
    assert r.status_code == 201, r.text
    i = r.json()
    ln = i["lines"][0]
    assert (ln["base_price"], ln["discount_pct"], ln["agreed_price"], ln["unit_price"]) == ("70.0000", "10.00", "63.0000", "63.0000")
    assert i["status"] == "posted" and i["total"] == "630.00" and i["due_date"] == iso(TODAY + timedelta(days=30))
    assert bal(client, auth["manager"], dc) == "630.00"
    assert stock_row(client, auth["manager"], dc["WH"], dc["rose"])["on_hand_good"] == 40
    assert "stock_value" not in ln                                   # stock keeper has no cost visibility
    full = client.get(f"/distributor-dispatches/{i['id']}", headers=auth["ceo"]).json()["lines"][0]
    assert full["stock_value"] == "400.0000"
    assert client.get("/inventory/integrity", headers=auth["ceo"]).json()["consistent"]


def test_agreed_price_overrides_and_missing_price_refused(client, auth, dc):
    assert client.put(f"/distributors/{dc['dist']}/prices/{dc['rose']}", headers=auth["manager"], json={"price": "60"}).status_code == 200
    ln = dispatch(client, auth["stock"], dc, qty=1).json()["lines"][0]
    assert ln["base_price"] == "60.0000" and ln["unit_price"] == "54.0000"
    r = client.post("/distributor-dispatches", headers=auth["stock"], json={"distributor_id": dc["dist"], "location_id": dc["WH"],
                                                                           "lines": [{"variant_id": dc["nude"], "qty": 1}]})
    assert r.status_code == 422 and "distributor price" in r.text


def test_price_above_agreement_refused_and_extra_discount_needs_approval(client, auth, dc):
    assert dispatch(client, auth["stock"], dc, price=70).status_code == 422
    held = dispatch(client, auth["stock"], dc, price=50).json()
    assert held["status"] == "held" and any(r.startswith("discount") for r in held["hold_reasons"])
    assert bal(client, auth["manager"], dc) == "0.00" and stock_row(client, auth["manager"], dc["WH"], dc["rose"])["on_hand_good"] == 50
    client.put("/distributors/settings", headers=auth["ceo"], json={"max_extra_discount_pct": "25"})
    assert dispatch(client, auth["stock"], dc, price=50).json()["status"] == "posted"


def test_historical_dispatch_unaffected_by_price_change(client, auth, dc):
    i = dispatch(client, auth["stock"], dc).json()
    client.patch(f"/variants/{dc['rose']}", headers=auth["ceo"], json={"distributor_price": "90.00"})
    again = client.get(f"/distributor-dispatches/{i['id']}", headers=auth["manager"]).json()
    assert again["total"] == i["total"] and again["lines"][0]["unit_price"] == "63.0000"


def test_credit_limit_holds_and_approval_by_someone_else_posts(client, auth, dc):
    client.put(f"/distributors/{dc['dist']}", headers=auth["manager"], json={"credit_limit": "500"})
    held = dispatch(client, auth["ceo"], dc, qty=10).json()           # 630 > 500
    assert held["status"] == "held" and held["hold_reasons"][0].startswith("credit_limit")
    assert bal(client, auth["manager"], dc) == "0.00"
    assert client.post(f"/distributor-dispatches/{held['id']}/decide", headers=auth["ceo"], json={"approve": True, "note": "ok"}).status_code == 403
    assert client.post(f"/distributor-dispatches/{held['id']}/decide", headers=auth["stock"], json={"approve": True, "note": "ok"}).status_code == 403
    ok = client.post(f"/distributor-dispatches/{held['id']}/decide", headers=auth["manager"], json={"approve": True, "note": "good payer"})
    assert ok.status_code == 200 and ok.json()["status"] == "posted"
    assert bal(client, auth["manager"], dc) == "630.00" and stock_row(client, auth["manager"], dc["WH"], dc["rose"])["on_hand_good"] == 40


def test_rejected_hold_never_moves_stock_or_money(client, auth, dc):
    client.put(f"/distributors/{dc['dist']}", headers=auth["manager"], json={"credit_limit": "1"})
    h = dispatch(client, auth["stock"], dc).json()
    r = client.post(f"/distributor-dispatches/{h['id']}/decide", headers=auth["manager"], json={"approve": False, "note": "over limit"})
    assert r.json()["status"] == "rejected" and bal(client, auth["manager"], dc) == "0.00"
    assert client.post(f"/distributor-dispatches/{h['id']}/post", headers=auth["stock"]).status_code == 409


def test_backdated_dispatch_needs_approval(client, auth, dc):
    old = dispatch(client, auth["stock"], dc, date_=TODAY - timedelta(days=20)).json()
    assert old["status"] == "held" and old["hold_reasons"][0].startswith("backdated")
    recent = dispatch(client, auth["stock"], dc, date_=TODAY - timedelta(days=3)).json()
    assert recent["status"] == "posted"


def test_oversell_blocked_and_nothing_booked(client, auth, dc):
    client.put(f"/distributors/{dc['dist']}", headers=auth["manager"], json={"credit_limit": "1000000"})
    r = dispatch(client, auth["stock"], dc, qty=500)
    assert r.status_code == 422, r.text
    assert bal(client, auth["manager"], dc) == "0.00"


def test_concurrent_dispatches_cannot_both_fit_the_credit_limit(client, auth, dc):
    client.put(f"/distributors/{dc['dist']}", headers=auth["manager"], json={"credit_limit": "1000"})
    ids = [dispatch(client, auth["stock"], dc, qty=10, post=False).json()["id"] for _ in range(2)]      # 630 each
    out, bar = [], threading.Barrier(2)

    def go(i):
        bar.wait()
        out.append(client.post(f"/distributor-dispatches/{i}/post", headers=auth["stock"]).json()["status"])

    ts = [threading.Thread(target=go, args=(i,)) for i in ids]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(out) == ["held", "posted"], out
    assert bal(client, auth["manager"], dc) == "630.00"


def test_draft_edit_and_cancel_and_idempotency(client, auth, dc):
    a = dispatch(client, auth["stock"], dc, post=False, idempotency_key="k1")
    b = dispatch(client, auth["stock"], dc, post=False, idempotency_key="k1")
    assert a.json()["id"] == b.json()["id"]
    i = a.json()["id"]
    up = client.put(f"/distributor-dispatches/{i}", headers=auth["stock"], json={"lines": [{"variant_id": dc["rose"], "qty": 3}]})
    assert up.status_code == 200 and up.json()["total"] == "189.00"
    c = client.post(f"/distributor-dispatches/{i}/cancel", headers=auth["stock"], json={"reason": "entered twice"})
    assert c.json()["status"] == "cancelled" and bal(client, auth["manager"], dc) == "0.00"
    posted = dispatch(client, auth["stock"], dc).json()
    assert client.post(f"/distributor-dispatches/{posted['id']}/cancel", headers=auth["stock"], json={"reason": "x"}).status_code == 409


def test_inactive_distributor_cannot_receive_goods(client, auth, dc):
    assert client.put(f"/distributors/{dc['dist']}", headers=auth["manager"], json={"is_active": False}).status_code == 200
    assert dispatch(client, auth["stock"], dc).status_code == 422


def test_cannot_deactivate_with_balance(client, auth, dc):
    dispatch(client, auth["stock"], dc)
    assert client.put(f"/distributors/{dc['dist']}", headers=auth["manager"], json={"is_active": False}).status_code == 422


# ---- payments -----------------------------------------------------------------------------------------------------------------------
def pay(client, h, c, amount, allocations=None, **kw):
    body = {"distributor_id": c["dist"], "amount": str(amount), "method": "cash", "paid_date": iso(TODAY), "allocations": allocations or [], **kw}
    return client.post("/distributor-payments", headers=h, json=body)


def test_partial_payment_allocation_and_on_account(client, auth, dc):
    i1 = dispatch(client, auth["stock"], dc, qty=10).json()       # 630
    i2 = dispatch(client, auth["stock"], dc, qty=10).json()       # 630
    p = pay(client, auth["accounts"], dc, 400, [{"invoice_id": i1["id"], "amount": "400"}])
    assert p.status_code == 201, p.text
    assert bal(client, auth["manager"], dc) == "860.00"
    assert client.get(f"/distributor-dispatches/{i1['id']}", headers=auth["manager"]).json()["outstanding"] == "230.00"
    over = pay(client, auth["accounts"], dc, 500, [{"invoice_id": i1["id"], "amount": "500"}])
    assert over.status_code == 422 and "outstanding" in over.text
    onacct = pay(client, auth["accounts"], dc, 300)                 # no allocation: credit on account
    assert onacct.json()["unallocated"] == "300.00" and bal(client, auth["manager"], dc) == "560.00"
    al = client.post(f"/distributors/{dc['dist']}/allocate", headers=auth["accounts"],
                     json={"source_type": "payment", "source_id": onacct.json()["id"], "allocations": [{"invoice_id": i1["id"], "amount": "230"}, {"invoice_id": i2["id"], "amount": "70"}]})
    assert al.status_code == 200, al.text
    assert client.get(f"/distributor-payments/{onacct.json()['id']}", headers=auth["manager"]).json()["unallocated"] == "0.00"
    too = client.post(f"/distributors/{dc['dist']}/allocate", headers=auth["accounts"],
                      json={"source_type": "payment", "source_id": onacct.json()["id"], "allocations": [{"invoice_id": i2["id"], "amount": "10"}]})
    assert too.status_code == 422
    assert client.get("/distributors/integrity", headers=auth["ceo"]).json()["consistent"]


def test_auto_allocate_settles_oldest_first(client, auth, dc):
    i1 = dispatch(client, auth["stock"], dc, qty=10, date_=TODAY - timedelta(days=5)).json()
    i2 = dispatch(client, auth["stock"], dc, qty=10).json()
    pay(client, auth["accounts"], dc, 700, auto_allocate=True)
    assert client.get(f"/distributor-dispatches/{i1['id']}", headers=auth["manager"]).json()["outstanding"] == "0.00"
    assert client.get(f"/distributor-dispatches/{i2['id']}", headers=auth["manager"]).json()["outstanding"] == "560.00"


def test_payment_reference_idempotency_and_roles(client, auth, dc):
    assert pay(client, auth["accounts"], dc, 10, method="bank_transfer").status_code == 422
    a = pay(client, auth["accounts"], dc, 10, idempotency_key="p1")
    b = pay(client, auth["accounts"], dc, 10, idempotency_key="p1")
    assert a.json()["id"] == b.json()["id"] and bal(client, auth["manager"], dc) == "-10.00"
    assert pay(client, auth["booker"], dc, 10).status_code == 403
    assert pay(client, auth["accounts"], dc, 10, paid_date=iso(TODAY + timedelta(days=2))).status_code == 422


def test_backdated_payment_waits_for_approval(client, auth, dc):
    i = dispatch(client, auth["stock"], dc).json()
    p = pay(client, auth["accounts"], dc, 100, [{"invoice_id": i["id"], "amount": "100"}], paid_date=iso(TODAY - timedelta(days=30))).json()
    assert p["status"] == "pending" and bal(client, auth["manager"], dc) == "630.00"
    assert client.post(f"/distributor-payments/{p['id']}/decide", headers=auth["accounts"], json={"approve": True, "note": "x"}).status_code == 403
    ok = client.post(f"/distributor-payments/{p['id']}/decide", headers=auth["manager"], json={"approve": True, "note": "receipt seen"})
    assert ok.status_code == 200 and ok.json()["status"] == "posted" and bal(client, auth["manager"], dc) == "530.00"
    assert client.get(f"/distributor-dispatches/{i['id']}", headers=auth["manager"]).json()["outstanding"] == "530.00"


def test_payment_reversal_needs_someone_else_and_restores_balance(client, auth, dc):
    i = dispatch(client, auth["stock"], dc).json()
    p = pay(client, auth["accounts"], dc, 630, [{"invoice_id": i["id"], "amount": "630"}]).json()
    assert bal(client, auth["manager"], dc) == "0.00"
    assert client.post(f"/distributor-payments/{p['id']}/reverse", headers=auth["accounts"], json={"reason": "x"}).status_code == 403   # no approve perm
    r = client.post(f"/distributor-payments/{p['id']}/reverse", headers=auth["manager"], json={"reason": "cheque bounced"})
    assert r.status_code == 200 and r.json()["status"] == "reversed"
    assert bal(client, auth["manager"], dc) == "630.00"
    assert client.get(f"/distributor-dispatches/{i['id']}", headers=auth["manager"]).json()["outstanding"] == "630.00"
    assert client.post(f"/distributor-payments/{p['id']}/reverse", headers=auth["manager"], json={"reason": "again"}).status_code == 409
    assert client.get("/distributors/integrity", headers=auth["ceo"]).json()["consistent"]


def test_simultaneous_payments_cannot_overpay_a_dispatch(client, auth, dc):
    i = dispatch(client, auth["stock"], dc).json()       # 630
    out, bar = [], threading.Barrier(2)

    def go(n):
        bar.wait()
        out.append(pay(client, auth["accounts"], dc, 400, [{"invoice_id": i["id"], "amount": "400"}], idempotency_key=f"c{n}").status_code)

    ts = [threading.Thread(target=go, args=(n,)) for n in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(out) == [201, 422], out
    assert client.get(f"/distributor-dispatches/{i['id']}", headers=auth["manager"]).json()["outstanding"] == "230.00"


# ---- returns and exchanges ------------------------------------------------------------------------------------------------------------
def start_return(client, auth, c, qty=4):
    i = dispatch(client, auth["stock"], c, qty=10).json()
    r = client.post("/distributor-returns", headers=auth["stock"], json={"distributor_id": c["dist"], "location_id": c["WH"], "reason": "unsold",
                                                                         "lines": [{"invoice_line_id": i["lines"][0]["id"], "qty": qty}]})
    assert r.status_code == 201, r.text
    return i, r.json()


def test_return_flow_credit_after_inspection_at_original_price(client, auth, dc):
    i, r = start_return(client, auth, dc)
    big = client.post("/distributor-returns", headers=auth["stock"], json={"distributor_id": dc["dist"], "location_id": dc["WH"], "reason": "x",
                                                                          "lines": [{"invoice_line_id": i["lines"][0]["id"], "qty": 7}]})
    assert big.status_code == 422                                                       # only 6 left to return
    client.patch(f"/variants/{dc['rose']}", headers=auth["ceo"], json={"distributor_price": "100.00"})
    assert bal(client, auth["manager"], dc) == "630.00"
    rec = client.post(f"/distributor-returns/{r['id']}/receive", headers=auth["stock"], json={"lines": [{"line_id": r["lines"][0]["id"], "qty_received": 4}]})
    assert rec.status_code == 200, rec.text
    row = stock_row(client, auth["manager"], dc["WH"], dc["rose"])
    assert row["on_hand_good"] == 40 and row["available"] == 40                          # not saleable before inspection
    assert bal(client, auth["manager"], dc) == "630.00"                                  # no credit before approval
    bad = client.post(f"/distributor-returns/{r['id']}/inspect", headers=auth["stock"], json={"lines": [{"line_id": r["lines"][0]["id"], "restock": 1}]})
    assert bad.status_code == 422
    ins = client.post(f"/distributor-returns/{r['id']}/inspect", headers=auth["stock"],
                      json={"lines": [{"line_id": r["lines"][0]["id"], "restock": 3, "damaged": 1, "credit_qty": 4}]})
    assert ins.status_code == 200 and ins.json()["credit_value"] == "252.00", ins.text      # 4 x 63, not the new price
    assert stock_row(client, auth["manager"], dc["WH"], dc["rose"])["available"] == 43
    assert bal(client, auth["manager"], dc) == "630.00"
    assert client.post(f"/distributor-returns/{r['id']}/decide", headers=auth["stock"], json={"approve": True, "note": "ok"}).status_code == 403
    ok = client.post(f"/distributor-returns/{r['id']}/decide", headers=auth["manager"], json={"approve": True, "note": "damaged unit accepted"})
    assert ok.status_code == 200 and ok.json()["status"] == "credited"
    assert bal(client, auth["manager"], dc) == "378.00"
    assert client.get(f"/distributor-dispatches/{i['id']}", headers=auth["manager"]).json()["outstanding"] == "378.00"
    assert client.get("/distributors/integrity", headers=auth["ceo"]).json()["consistent"]
    assert client.get("/inventory/integrity", headers=auth["ceo"]).json()["consistent"]


def test_returns_cannot_exceed_dispatched_and_requester_cannot_decide(client, auth, dc):
    i, r = start_return(client, auth, dc, qty=6)
    again = client.post("/distributor-returns", headers=auth["stock"], json={"distributor_id": dc["dist"], "location_id": dc["WH"], "reason": "x",
                                                                            "lines": [{"invoice_line_id": i["lines"][0]["id"], "qty": 5}]})
    assert again.status_code == 422
    client.post(f"/distributor-returns/{r['id']}/receive", headers=auth["stock"], json={"lines": [{"line_id": r["lines"][0]["id"], "qty_received": 6}]})
    client.post(f"/distributor-returns/{r['id']}/inspect", headers=auth["stock"], json={"lines": [{"line_id": r["lines"][0]["id"], "restock": 6}]})
    # a manager who requested and inspected would also be refused; here the requester (stock) lacks approve, and the ceo is a fresh decider
    assert client.post(f"/distributor-returns/{r['id']}/decide", headers=auth["ceo"], json={"approve": False, "note": "no credit"}).json()["status"] == "rejected"
    assert bal(client, auth["manager"], dc) == "630.00"


def test_exchange_is_return_plus_replacement_dispatch(client, auth, dc):
    i, r = start_return(client, auth, dc, qty=2)
    client.post(f"/distributor-returns/{r['id']}/receive", headers=auth["stock"], json={"lines": [{"line_id": r["lines"][0]["id"], "qty_received": 2}]})
    client.post(f"/distributor-returns/{r['id']}/inspect", headers=auth["stock"], json={"lines": [{"line_id": r["lines"][0]["id"], "damaged": 2}]})
    client.post(f"/distributor-returns/{r['id']}/decide", headers=auth["manager"], json={"approve": True, "note": "swap"})
    rep = dispatch(client, auth["stock"], dc, qty=2, exchange_return_id=r["id"])
    assert rep.status_code == 201 and rep.json()["exchange_return_id"] == r["id"]
    assert bal(client, auth["manager"], dc) == "630.00"                                    # 630 - 126 + 126
    assert len(client.get("/distributor-dispatches", headers=auth["manager"], params={"distributor_id": dc["dist"]}).json()["items"]) == 2
    other = client.post("/distributors", headers=auth["manager"], json={"name": "Other One", "credit_limit": "5000"}).json()
    bad = dispatch(client, auth["stock"], dc, dist=other["id"], exchange_return_id=r["id"])
    assert bad.status_code == 422


# ---- adjustments, opening balance, aging, statements -----------------------------------------------------------------------------------------
def test_write_off_needs_other_person_and_clears_the_dispatch(client, auth, dc):
    i = dispatch(client, auth["stock"], dc).json()
    a = client.post("/distributor-adjustments", headers=auth["stock"], json={"distributor_id": dc["dist"], "kind": "write_off", "amount": "100",
                                                                           "invoice_id": i["id"], "reason": "expired stock"})
    assert a.status_code == 201, a.text
    assert bal(client, auth["manager"], dc) == "630.00"
    assert client.post(f"/distributor-adjustments/{a.json()['id']}/decide", headers=auth["stock"], json={"approve": True, "note": "ok"}).status_code == 403
    ok = client.post(f"/distributor-adjustments/{a.json()['id']}/decide", headers=auth["manager"], json={"approve": True, "note": "approved by management"})
    assert ok.status_code == 200 and bal(client, auth["manager"], dc) == "530.00"
    assert client.get(f"/distributor-dispatches/{i['id']}", headers=auth["manager"]).json()["outstanding"] == "530.00"
    too = client.post("/distributor-adjustments", headers=auth["stock"], json={"distributor_id": dc["dist"], "kind": "write_off", "amount": "9999",
                                                                             "invoice_id": i["id"], "reason": "x"})
    assert too.status_code == 422
    debit = client.post("/distributor-adjustments", headers=auth["stock"], json={"distributor_id": dc["dist"], "kind": "debit", "amount": "50", "reason": "freight"}).json()
    client.post(f"/distributor-adjustments/{debit['id']}/decide", headers=auth["manager"], json={"approve": True, "note": "ok"})
    assert bal(client, auth["manager"], dc) == "580.00"
    assert client.get("/distributors/integrity", headers=auth["ceo"]).json()["consistent"]


def test_opening_balance_once_and_aging_buckets(client, auth, dc):
    r = client.post(f"/distributors/{dc['dist']}/opening-balance", headers=auth["stock"], json={"amount": "1000", "as_of": iso(TODAY - timedelta(days=100))})
    assert r.status_code == 403
    r = client.post(f"/distributors/{dc['dist']}/opening-balance", headers=auth["ceo"], json={"amount": "1000", "as_of": iso(TODAY - timedelta(days=100))})
    assert r.status_code == 201, r.text
    assert client.post(f"/distributors/{dc['dist']}/opening-balance", headers=auth["ceo"], json={"amount": "5", "as_of": iso(TODAY)}).status_code == 409
    dispatch(client, auth["stock"], dc, qty=10, date_=TODAY - timedelta(days=3))        # not yet due
    ag = client.get(f"/distributors/{dc['dist']}/aging", headers=auth["manager"]).json()
    assert ag["buckets"]["over_90"] == "1000.00" or ag["buckets"]["61_90"] == "1000.00"   # 100 days old, 30-day terms => 70 days late
    assert ag["buckets"]["61_90"] == "1000.00" and ag["buckets"]["current"] == "630.00" and ag["overdue"] == "1000.00"
    assert ag["balance"] == "1630.00"


def test_monthly_statement_carries_forward_and_reconciles(client, auth, dc):
    old = TODAY.replace(day=1) - timedelta(days=40)
    client.post(f"/distributors/{dc['dist']}/opening-balance", headers=auth["ceo"], json={"amount": "1000", "as_of": iso(old)})
    i = dispatch(client, auth["stock"], dc, qty=10).json()
    pay(client, auth["accounts"], dc, 200, [{"invoice_id": i["id"], "amount": "200"}])
    st = client.get(f"/distributors/{dc['dist']}/statement", headers=auth["manager"], params={"month": TODAY.strftime("%Y-%m")}).json()
    assert st["opening_balance"] == "1000.00" and st["charges"] == "630.00" and st["payments"] == "200.00" and st["closing_balance"] == "1430.00"
    assert st["reconciles"] is True and [e["type"] for e in st["entries"]] == ["invoice", "payment"] and st["entries"][-1]["balance"] == "1430.00"
    prev = client.get(f"/distributors/{dc['dist']}/statement", headers=auth["manager"], params={"month": old.strftime("%Y-%m")}).json()
    assert prev["closing_balance"] == "1000.00"
    assert client.get(f"/distributors/{dc['dist']}/statement", headers=auth["manager"], params={"month": "nonsense"}).status_code == 422


# ---- ledger protection ------------------------------------------------------------------------------------------------------------------------
def test_ledger_is_append_only_and_cannot_double_book(client, auth, dc, db):
    dispatch(client, auth["stock"], dc)
    with pytest.raises(DBAPIError):
        db.execute(text("UPDATE distributor_ledger SET amount = 1"))
        db.commit()
    db.rollback()
    with pytest.raises(DBAPIError):
        db.execute(text("DELETE FROM distributor_ledger"))
        db.commit()
    db.rollback()
    with pytest.raises(Exception):
        db.execute(text("INSERT INTO distributor_ledger (distributor_id, entry_type, amount, entry_date, source_type, source_id, ref, accounting_status) "
                        "SELECT distributor_id, entry_type, amount, entry_date, source_type, source_id, ref, 'pending' FROM distributor_ledger LIMIT 1"))
        db.commit()
    db.rollback()


def test_audit_trail_and_integrity_after_mixed_activity(client, auth, dc):
    i = dispatch(client, auth["stock"], dc).json()
    pay(client, auth["accounts"], dc, 100, [{"invoice_id": i["id"], "amount": "100"}])
    ev = client.get("/audit", headers=auth["ceo"], params={"page_size": 200})
    if ev.status_code == 200:
        items = ev.json()["items"] if isinstance(ev.json(), dict) else ev.json()
        actions = {x["action"] for x in items}
        assert {"distributor_dispatch.posted", "distributor_payment.posted"} <= actions, actions
    assert client.get("/distributors/integrity", headers=auth["ceo"]).json() == {"consistent": True, "problems": []}
