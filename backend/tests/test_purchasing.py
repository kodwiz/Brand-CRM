"""Phase 3: suppliers, purchase orders, receipts against orders, supplier invoices and matching, payments, adjustments, returns.
Synthetic data only."""
import threading
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from backend.tests.conftest import line, receive

TODAY = date.today()
D = Decimal


def iso(d):
    return d.isoformat()


# ---- helpers ---------------------------------------------------------------------------------------------
def mk_supplier(client, h, name="Test Supplier", terms=30, **kw):
    r = client.post("/suppliers", headers=h, json={"name": name, "payment_terms_days": terms, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def mk_po(client, auth, supplier, loc, lines, approve=True, by="accounts"):
    """lines: [(variant_id, qty, unit_cost)]"""
    r = client.post("/purchase-orders", headers=auth[by], json={
        "supplier_id": supplier["id"], "location_id": loc, "order_date": iso(TODAY),
        "lines": [{"variant_id": v, "qty_ordered": q, "unit_cost": str(c)} for v, q, c in lines]})
    assert r.status_code == 201, r.text
    po = r.json()
    if approve:
        a = client.post(f"/purchase-orders/{po['id']}/approve", headers=auth["manager"])
        assert a.status_code == 200, a.text
        po = a.json()
    return po


def po_receive(client, h, po, lines, post=True, **extra):
    """lines: [(variant_id, accepted, damaged, rejected)]"""
    body = {"location_id": po["location_id"], "received_date": iso(TODAY), "purchase_order_id": po["id"], "post_now": post,
            "lines": [{"variant_id": v, "qty_received": a + d + rj, "qty_accepted": a, "qty_damaged": d, "qty_rejected": rj}
                      for v, a, d, rj in lines], **extra}
    return client.post("/inventory/receipts", headers=h, json=body)


def mk_invoice(client, h, supplier, po, lines, no="INV-1", post=True, **kw):
    """lines: [(variant_id, qty, unit_price)] matched to the PO line of that variant."""
    pl = {l["variant_id"]: l["id"] for l in po["lines"]} if po else {}
    body = {"supplier_id": supplier["id"], "invoice_no": no, "invoice_date": iso(TODAY), "post_now": post,
            "purchase_order_id": po["id"] if po else None,
            "lines": [{"po_line_id": pl.get(v), "variant_id": None if pl.get(v) else v, "qty": q, "unit_price": str(p)}
                      for v, q, p in lines], **kw}
    return client.post("/supplier-invoices", headers=h, json=body)


def balance(client, h, supplier):
    return D(client.get(f"/suppliers/{supplier['id']}", headers=h).json()["balance"])


def pay(client, h, supplier, amount, allocations=(), **kw):
    body = {"supplier_id": supplier["id"], "amount": str(amount), "method": "cash", "paid_date": iso(TODAY),
            "allocations": [{"invoice_id": i, "amount": str(a)} for i, a in allocations], **kw}
    return client.post("/supplier-payments", headers=h, json=body)


def consistent(client, h):
    r = client.get("/purchasing/integrity", headers=h).json()
    assert r["consistent"], r["problems"]
    r = client.get("/inventory/integrity", headers=h).json()
    assert r["consistent"], r["problems"]


@pytest.fixture
def sup(client, auth):
    return mk_supplier(client, auth["accounts"])


# ---- suppliers -----------------------------------------------------------------------------------------------
def test_supplier_crud_and_duplicates(client, auth):
    h = auth["accounts"]
    s = mk_supplier(client, h, "Alpha Traders", terms=45, phone="0300-0000000")
    assert s["code"].startswith("SUP-") and s["payment_terms_days"] == 45 and s["is_active"]
    assert client.post("/suppliers", headers=h, json={"name": "alpha TRADERS"}).status_code == 409   # case-insensitive
    assert client.post("/suppliers", headers=h, json={"name": "X", "code": s["code"]}).status_code == 409
    assert client.post("/suppliers", headers=h, json={"name": "Y", "payment_terms_days": 999}).status_code == 422
    u = client.put(f"/suppliers/{s['id']}", headers=h, json={"city": "Lahore", "payment_terms_days": 10})
    assert u.status_code == 200 and u.json()["city"] == "Lahore" and u.json()["payment_terms_days"] == 10
    lst = client.get("/suppliers?q=alpha", headers=h).json()
    assert lst["total"] == 1
    assert client.get("/suppliers/9999", headers=h).status_code == 404


def test_cannot_deactivate_supplier_with_balance_or_open_orders(client, auth, catalog, locations, sup):
    h = auth["accounts"]
    po = mk_po(client, auth, sup, locations["WH"].id, [(catalog["rose"], 10, 40)])
    r = client.put(f"/suppliers/{sup['id']}", headers=h, json={"is_active": False})
    assert r.status_code == 422 and "open purchase orders" in r.json()["detail"]
    client.post(f"/purchase-orders/{po['id']}/cancel", headers=h, json={"reason": "not needed"})
    assert client.put(f"/suppliers/{sup['id']}", headers=h, json={"is_active": False}).status_code == 200
    # an inactive supplier cannot receive new orders
    r = client.post("/purchase-orders", headers=h, json={"supplier_id": sup["id"], "location_id": locations["WH"].id,
        "order_date": iso(TODAY), "lines": [{"variant_id": catalog["rose"], "qty_ordered": 1, "unit_cost": "1"}]})
    assert r.status_code == 422


# ---- purchase orders -----------------------------------------------------------------------------------------
def test_po_lifecycle_and_self_approval(client, auth, catalog, locations, sup):
    wh = locations["WH"].id
    po = mk_po(client, auth, sup, wh, [(catalog["rose"], 10, 40), (catalog["nude"], 5, 30)], approve=False, by="ceo")
    assert po["status"] == "draft" and po["total"] == "550.00" and po["payment_terms_days"] == 30
    # the creator cannot approve their own order (default limit is zero)
    r = client.post(f"/purchase-orders/{po['id']}/approve", headers=auth["ceo"])
    assert r.status_code == 403
    r = client.post(f"/purchase-orders/{po['id']}/approve", headers=auth["manager"])
    assert r.status_code == 200 and r.json()["status"] == "approved" and r.json()["approved_by"] == "manager"
    assert client.post(f"/purchase-orders/{po['id']}/approve", headers=auth["manager"]).status_code == 409
    # approved orders are not edited
    r = client.put(f"/purchase-orders/{po['id']}", headers=auth["accounts"], json={"notes": "x"})
    assert r.status_code == 409
    # a draft can be edited and replaced
    d = mk_po(client, auth, sup, wh, [(catalog["rose"], 1, 10)], approve=False)
    r = client.put(f"/purchase-orders/{d['id']}", headers=auth["accounts"],
                   json={"lines": [{"variant_id": catalog["nude"], "qty_ordered": 3, "unit_cost": "20"}]})
    assert r.status_code == 200 and r.json()["total"] == "60.00" and len(r.json()["lines"]) == 1


def test_po_validation(client, auth, catalog, locations, sup):
    h, wh = auth["accounts"], locations["WH"].id
    base = {"supplier_id": sup["id"], "location_id": wh, "order_date": iso(TODAY)}
    bad = [
        {"lines": [{"variant_id": catalog["rose"], "qty_ordered": 1, "unit_cost": "5"},
                   {"variant_id": catalog["rose"], "qty_ordered": 2, "unit_cost": "5"}]},   # duplicate item
        {"lines": [{"variant_id": 99999, "qty_ordered": 1, "unit_cost": "5"}]},
        {"lines": [{"variant_id": catalog["rose"], "qty_ordered": 1, "unit_cost": "5"}], "expected_date": iso(TODAY - timedelta(days=5))},
        {"lines": []},
    ]
    for extra in bad:
        r = client.post("/purchase-orders", headers=h, json={**base, **extra})
        assert r.status_code == 422, extra
    r = client.post("/purchase-orders", headers=h, json={**base, "lines": [{"variant_id": catalog["rose"], "qty_ordered": 0, "unit_cost": "5"}]})
    assert r.status_code == 422
    assert client.post("/purchase-orders", headers=h, json={**base, "supplier_id": 9999,
        "lines": [{"variant_id": catalog["rose"], "qty_ordered": 1, "unit_cost": "5"}]}).status_code == 404


def test_configurable_po_self_approval_limit(client, auth, catalog, locations, sup):
    wh = locations["WH"].id
    assert client.put("/purchasing/settings", headers=auth["ceo"], json={"po_self_approve_limit": "1000"}).status_code == 200
    small = mk_po(client, auth, sup, wh, [(catalog["rose"], 10, 40)], approve=False, by="ceo")      # 400
    assert client.post(f"/purchase-orders/{small['id']}/approve", headers=auth["ceo"]).status_code == 200
    big = mk_po(client, auth, sup, wh, [(catalog["rose"], 100, 40)], approve=False, by="ceo")        # 4000
    assert client.post(f"/purchase-orders/{big['id']}/approve", headers=auth["ceo"]).status_code == 403
    assert client.put("/purchasing/settings", headers=auth["manager"], json={"po_self_approve_limit": "0"}).status_code == 403


def test_cancel_and_close(client, auth, catalog, locations, sup):
    h, wh = auth["accounts"], locations["WH"].id
    po = mk_po(client, auth, sup, wh, [(catalog["rose"], 10, 40)])
    assert client.post(f"/purchase-orders/{po['id']}/cancel", headers=h, json={"reason": ""}).status_code == 422
    po2 = mk_po(client, auth, sup, wh, [(catalog["nude"], 10, 40)])
    po_receive(client, auth["stock"], po2, [(catalog["nude"], 4, 0, 0)])
    # received against: cancel is refused, close (remaining will not arrive) is allowed and keeps what was received
    assert client.post(f"/purchase-orders/{po2['id']}/cancel", headers=h, json={"reason": "x"}).status_code == 409
    r = client.post(f"/purchase-orders/{po2['id']}/close", headers=h, json={"reason": "supplier short-shipped"})
    assert r.status_code == 200 and r.json()["status"] == "closed" and r.json()["units_owned"] == 4
    assert po_receive(client, auth["stock"], po2, [(catalog["nude"], 1, 0, 0)]).status_code == 422   # closed
    assert client.post(f"/purchase-orders/{po['id']}/cancel", headers=h, json={"reason": "changed mind"}).json()["status"] == "cancelled"


# ---- receiving against orders ----------------------------------------------------------------------------------
def test_receipt_against_po_status_cost_and_stock(client, auth, catalog, locations, sup):
    wh = locations["WH"].id
    po = mk_po(client, auth, sup, wh, [(catalog["rose"], 10, 40), (catalog["nude"], 5, 30)])
    r = po_receive(client, auth["stock"], po, [(catalog["rose"], 4, 0, 0)], draft_unit_cost=None) if False else \
        client.post("/inventory/receipts", headers=auth["stock"], json={
            "location_id": wh, "received_date": iso(TODAY), "purchase_order_id": po["id"], "post_now": True,
            "lines": [{"variant_id": catalog["rose"], "qty_received": 4, "qty_accepted": 4, "unit_cost": "999"}]})
    assert r.status_code == 201, r.text
    rc = r.json()
    assert rc["supplier_id"] == sup["id"] and rc["purchase_order_id"] == po["id"] and rc["purchase_reference"] == po["ref"]
    assert "unit_cost" not in rc["lines"][0]                                  # the stock keeper never sees costs
    got = client.get(f"/purchase-orders/{po['id']}", headers=auth["accounts"]).json()
    assert got["status"] == "partially_received" and got["lines"][0]["qty_owned"] == 4 and got["lines"][0]["qty_remaining"] == 6
    s = next(i for i in client.get(f"/inventory/stock?location_id={wh}", headers=auth["ceo"]).json()["items"]
             if i["variant_id"] == catalog["rose"])
    assert s["on_hand_good"] == 4 and D(s["avg_cost"]) == D("40")             # the PO price, not the 999 typed by the client
    assert po_receive(client, auth["stock"], po, [(catalog["rose"], 6, 0, 0), (catalog["nude"], 5, 0, 0)]).status_code == 201
    assert client.get(f"/purchase-orders/{po['id']}", headers=auth["accounts"]).json()["status"] == "received"
    consistent(client, auth["ceo"])


def test_receipt_rules_against_po(client, auth, catalog, locations, sup):
    wh, b1 = locations["WH"].id, locations["B1"].id
    po = mk_po(client, auth, sup, wh, [(catalog["rose"], 10, 40)])
    st = auth["stock"]
    r = po_receive(client, st, po, [(catalog["rose"], 11, 0, 0)])
    assert r.status_code == 422 and "only 10 were ordered" in r.json()["detail"]                 # over-receipt
    assert po_receive(client, st, po, [(catalog["nude"], 1, 0, 0)]).status_code == 422             # not on the order
    other = client.post("/inventory/receipts", headers=st, json={"location_id": b1, "received_date": iso(TODAY),
        "purchase_order_id": po["id"], "lines": [{"variant_id": catalog["rose"], "qty_received": 1, "qty_accepted": 1}]})
    assert other.status_code == 422                                                              # wrong location
    draft = mk_po(client, auth, sup, wh, [(catalog["rose"], 1, 40)], approve=False)
    assert po_receive(client, st, draft, [(catalog["rose"], 1, 0, 0)]).status_code == 422          # not approved
    # rejected units do not count as received against the order
    assert po_receive(client, st, po, [(catalog["rose"], 6, 2, 2)]).status_code == 201
    m = client.get(f"/purchase-orders/{po['id']}/matching", headers=auth["accounts"]).json()["lines"][0]
    assert m["accepted"] == 6 and m["damaged"] == 2 and m["rejected"] == 2 and m["owned"] == 8 and m["remaining_to_receive"] == 2
    assert "rejected_on_receipt" in m["flags"] and "short_received" in m["flags"]


def test_over_receipt_tolerance_setting(client, auth, catalog, locations, sup):
    po = mk_po(client, auth, sup, locations["WH"].id, [(catalog["rose"], 100, 40)])
    client.put("/purchasing/settings", headers=auth["ceo"], json={"receipt_over_tolerance_pct": "5"})
    assert po_receive(client, auth["stock"], po, [(catalog["rose"], 105, 0, 0)]).status_code == 201
    po2 = mk_po(client, auth, sup, locations["WH"].id, [(catalog["nude"], 100, 40)])
    assert po_receive(client, auth["stock"], po2, [(catalog["nude"], 106, 0, 0)]).status_code == 422


def test_additional_costs_are_capitalised_exactly(client, auth, catalog, locations, sup):
    wh = locations["WH"].id
    po = mk_po(client, auth, sup, wh, [(catalog["rose"], 10, 40)])
    r = po_receive(client, auth["stock"], po, [(catalog["rose"], 8, 2, 0)],
                   additional_costs=[{"description": "Freight", "amount": "50"}, {"description": "Handling", "amount": "30"}])
    assert r.status_code == 201, r.text
    s = next(i for i in client.get(f"/inventory/stock?location_id={wh}", headers=auth["ceo"]).json()["items"]
             if i["variant_id"] == catalog["rose"])
    assert D(s["value"]) == D("480") and D(s["avg_cost"]) == D("48")               # 400 + 80, nothing lost to rounding
    rc = client.get(f"/inventory/receipts/{r.json()['id']}", headers=auth["ceo"]).json()
    assert sum(D(c["amount"]) for c in rc["additional_costs"]) == D("80")
    # extra costs never create a supplier liability
    assert balance(client, auth["accounts"], sup) == 0
    # and a receipt where nothing enters stock cannot carry them
    po2 = mk_po(client, auth, sup, wh, [(catalog["nude"], 3, 10)])
    bad = po_receive(client, auth["stock"], po2, [(catalog["nude"], 0, 0, 3)], additional_costs=[{"description": "Freight", "amount": "5"}])
    assert bad.status_code == 422
    consistent(client, auth["ceo"])


def test_receipt_reversal_reopens_the_order(client, auth, catalog, locations, sup):
    po = mk_po(client, auth, sup, locations["WH"].id, [(catalog["rose"], 10, 40)])
    r = po_receive(client, auth["stock"], po, [(catalog["rose"], 10, 0, 0)]).json()
    assert client.get(f"/purchase-orders/{po['id']}", headers=auth["accounts"]).json()["status"] == "received"
    txn = next(m for m in client.get("/inventory/movements?page_size=50", headers=auth["ceo"]).json()["items"] if m["txn_type"] == "receipt")
    rv = client.post(f"/inventory/transactions/{txn['txn_id']}/reverse", headers=auth["manager"], json={"reason": "wrong delivery"})
    assert rv.status_code == 200, rv.text
    assert client.get(f"/purchase-orders/{po['id']}", headers=auth["accounts"]).json()["status"] == "approved"


def test_receipt_without_order_still_works(client, auth, catalog, locations, sup):
    r = receive(client, auth["ceo"], locations["WH"].id, [line(catalog["rose"], 5, "12")])
    assert r.status_code == 201 and r.json()["purchase_order_id"] is None
    body = {"location_id": locations["WH"].id, "received_date": iso(TODAY), "supplier_id": sup["id"],
            "lines": [{"variant_id": catalog["rose"], "qty_received": 2, "qty_accepted": 2}]}
    assert client.post("/inventory/receipts", headers=auth["ceo"], json=body).status_code == 422   # a cost is required without an order
    body["lines"][0]["unit_cost"] = "12"
    ok = client.post("/inventory/receipts", headers=auth["ceo"], json=body)
    assert ok.status_code == 201 and ok.json()["supplier_id"] == sup["id"]


# ---- invoices and matching -------------------------------------------------------------------------------------
def test_matched_invoice_creates_liability_exactly_once(client, auth, catalog, locations, sup):
    a, wh = auth["accounts"], locations["WH"].id
    po = mk_po(client, auth, sup, wh, [(catalog["rose"], 10, 40)])
    po_receive(client, auth["stock"], po, [(catalog["rose"], 10, 0, 0)])
    assert balance(client, a, sup) == 0                                            # receiving alone creates no liability
    g = client.get("/purchasing/grni", headers=a).json()
    assert D(g["total_value"]) == D("400") and g["rows"][0]["po_ref"] == po["ref"]
    r = mk_invoice(client, a, sup, po, [(catalog["rose"], 10, 40)], no="INV-100")
    assert r.status_code == 201 and r.json()["status"] == "posted" and r.json()["match_status"] == "matched"
    inv = r.json()
    assert inv["total"] == "400.00" and inv["due_date"] == iso(TODAY + timedelta(days=30))
    assert balance(client, a, sup) == D("400")
    assert D(client.get("/purchasing/grni", headers=a).json()["total_value"]) == 0   # now covered by the invoice
    # the same invoice cannot be posted or entered twice
    assert client.post(f"/supplier-invoices/{inv['id']}/post", headers=a).status_code == 409
    dup = mk_invoice(client, a, sup, po, [(catalog["rose"], 10, 40)], no="inv-100")
    assert dup.status_code == 409 and "already recorded" in dup.json()["detail"]
    st = client.get(f"/suppliers/{sup['id']}/statement", headers=a).json()
    assert len(st["entries"]) == 1 and st["closing_balance"] == "400.00"
    consistent(client, auth["ceo"])


def test_price_variance_is_held_until_independent_approval(client, auth, catalog, locations, sup):
    a, wh = auth["accounts"], locations["WH"].id
    po = mk_po(client, auth, sup, wh, [(catalog["rose"], 10, 40)])
    po_receive(client, auth["stock"], po, [(catalog["rose"], 10, 0, 0)])
    r = mk_invoice(client, auth["ceo"], sup, po, [(catalog["rose"], 10, 45)], no="INV-V1")
    inv = r.json()
    assert inv["status"] == "held" and inv["match_status"] == "variance" and inv["variances"][0]["type"] == "price_variance"
    assert balance(client, a, sup) == 0                                            # held invoices owe nothing yet
    p = pay(client, a, sup, 450, [(inv["id"], 450)])
    assert p.status_code == 422 and "posted" in p.json()["detail"]                 # and cannot be paid
    # the person who entered it cannot decide it; a manager must, with a reason
    assert client.post(f"/supplier-invoices/{inv['id']}/decide", headers=auth["ceo"], json={"approve": True, "note": "ok"}).status_code == 403
    assert client.post(f"/supplier-invoices/{inv['id']}/decide", headers=auth["manager"], json={"approve": True}).status_code == 422
    assert client.post(f"/supplier-invoices/{inv['id']}/decide", headers=auth["stock"], json={"approve": True, "note": "x"}).status_code == 403
    ok = client.post(f"/supplier-invoices/{inv['id']}/decide", headers=auth["manager"],
                     json={"approve": True, "note": "supplier price rise agreed by phone"})
    assert ok.status_code == 200 and ok.json()["status"] == "posted" and ok.json()["decided_by"] == "manager"
    assert balance(client, a, sup) == D("450")
    assert client.post(f"/supplier-invoices/{inv['id']}/decide", headers=auth["manager"], json={"approve": True, "note": "again"}).status_code == 409
    consistent(client, auth["ceo"])


def test_rejected_invoice_frees_the_number(client, auth, catalog, locations, sup):
    a, wh = auth["accounts"], locations["WH"].id
    po = mk_po(client, auth, sup, wh, [(catalog["rose"], 10, 40)])
    inv = mk_invoice(client, auth["ceo"], sup, po, [(catalog["rose"], 10, 55)], no="INV-R").json()
    assert inv["status"] == "held"
    assert client.post(f"/supplier-invoices/{inv['id']}/decide", headers=auth["manager"], json={"approve": False}).status_code == 422
    rej = client.post(f"/supplier-invoices/{inv['id']}/decide", headers=auth["manager"], json={"approve": False, "note": "wrong price"})
    assert rej.status_code == 200 and rej.json()["status"] == "rejected" and balance(client, a, sup) == 0
    again = mk_invoice(client, a, sup, po, [(catalog["rose"], 10, 40)], no="INV-R")
    assert again.status_code == 201 and again.json()["status"] == "posted"


def test_price_tolerance_setting(client, auth, catalog, locations, sup):
    po = mk_po(client, auth, sup, locations["WH"].id, [(catalog["rose"], 100, 40)])
    client.put("/purchasing/settings", headers=auth["ceo"], json={"invoice_price_tolerance_pct": "5"})
    assert mk_invoice(client, auth["accounts"], sup, po, [(catalog["rose"], 10, 41.5)], no="T1").json()["status"] == "posted"   # 3.75%
    assert mk_invoice(client, auth["accounts"], sup, po, [(catalog["rose"], 10, 43)], no="T2").json()["status"] == "held"        # 7.5%


def test_invoice_before_receipt_is_allowed_and_visible(client, auth, catalog, locations, sup):
    a, wh = auth["accounts"], locations["WH"].id
    po = mk_po(client, auth, sup, wh, [(catalog["rose"], 10, 40)])
    r = mk_invoice(client, a, sup, po, [(catalog["rose"], 10, 40)], no="EARLY")
    assert r.status_code == 201 and r.json()["status"] == "posted" and r.json()["match_status"] == "awaiting_receipt"
    assert balance(client, a, sup) == D("400")
    m = client.get(f"/purchase-orders/{po['id']}/matching", headers=a).json()["lines"][0]
    assert "invoiced_ahead_of_receipt" in m["flags"] and m["invoiced"] == 10 and m["owned"] == 0
    po_receive(client, auth["stock"], po, [(catalog["rose"], 10, 0, 0)])
    m = client.get(f"/purchase-orders/{po['id']}/matching", headers=a).json()["lines"][0]
    assert m["flags"] == [] and balance(client, a, sup) == D("400")              # receiving later does not add a second liability
    consistent(client, auth["ceo"])


def test_over_invoicing_and_off_order_lines_are_held(client, auth, catalog, locations, sup):
    a, wh = auth["accounts"], locations["WH"].id
    po = mk_po(client, auth, sup, wh, [(catalog["rose"], 10, 40)])
    one = mk_invoice(client, a, sup, po, [(catalog["rose"], 8, 40)], no="A1").json()
    two = mk_invoice(client, a, sup, po, [(catalog["rose"], 5, 40)], no="A2").json()      # 13 invoiced of 10 ordered
    assert one["status"] == "posted" and two["status"] == "held"
    assert any(v["type"] == "over_invoiced" for v in two["variances"])
    # a line that is not on the order at all
    body = {"supplier_id": sup["id"], "invoice_no": "A3", "invoice_date": iso(TODAY), "purchase_order_id": po["id"],
            "lines": [{"description": "Courier service", "qty": 1, "unit_price": "100"}]}
    three = client.post("/supplier-invoices", headers=a, json=body).json()
    assert three["status"] == "held" and three["variances"][0]["type"] == "not_on_po"
    # invoices without any order are allowed (e.g. services) and are labelled as such
    four = client.post("/supplier-invoices", headers=a, json={**body, "invoice_no": "A4", "purchase_order_id": None}).json()
    assert four["status"] == "posted" and four["match_status"] == "no_po"


def test_invoice_validation(client, auth, catalog, locations, sup):
    a, wh = auth["accounts"], locations["WH"].id
    other = mk_supplier(client, a, "Other Supplier")
    po = mk_po(client, auth, sup, wh, [(catalog["rose"], 10, 40)])
    ok_line = [{"po_line_id": po["lines"][0]["id"], "qty": 1, "unit_price": "40"}]
    base = {"supplier_id": sup["id"], "invoice_no": "V1", "invoice_date": iso(TODAY), "purchase_order_id": po["id"], "lines": ok_line}
    assert client.post("/supplier-invoices", headers=a, json={**base, "supplier_id": other["id"]}).status_code == 422   # wrong supplier
    assert client.post("/supplier-invoices", headers=a, json={**base, "invoice_date": iso(TODAY + timedelta(days=2))}).status_code == 422
    assert client.post("/supplier-invoices", headers=a, json={**base, "due_date": iso(TODAY - timedelta(days=3))}).status_code == 422
    assert client.post("/supplier-invoices", headers=a, json={**base, "discount": "500"}).status_code == 422
    assert client.post("/supplier-invoices", headers=a, json={**base, "lines": [{"qty": 1, "unit_price": "5"}]}).status_code == 422
    draft_po = mk_po(client, auth, sup, wh, [(catalog["nude"], 1, 1)], approve=False)
    assert client.post("/supplier-invoices", headers=a, json={**base, "purchase_order_id": draft_po["id"],
                                                               "lines": [{"po_line_id": draft_po["lines"][0]["id"], "qty": 1, "unit_price": "1"}]}).status_code == 422
    # totals: lines + charges + tax - discount
    r = client.post("/supplier-invoices", headers=a, json={**base, "charges": [{"description": "Freight", "amount": "20"}],
                                                          "tax": "7", "discount": "5", "post_now": False})
    assert r.status_code == 201 and r.json()["total"] == "62.00" and r.json()["status"] == "draft"
    assert balance(client, a, sup) == 0                                            # drafts owe nothing
    assert client.post(f"/supplier-invoices/{r.json()['id']}/post", headers=a).json()["status"] == "posted"
    assert balance(client, a, sup) == D("62")


def test_void_invoice_rules(client, auth, catalog, locations, sup):
    a, wh = auth["accounts"], locations["WH"].id
    po = mk_po(client, auth, sup, wh, [(catalog["rose"], 10, 40)])
    inv = mk_invoice(client, a, sup, po, [(catalog["rose"], 10, 40)], no="VOID1").json()
    assert client.post(f"/supplier-invoices/{inv['id']}/void", headers=a, json={"reason": "x"}).status_code == 403   # own invoice
    assert client.post(f"/supplier-invoices/{inv['id']}/void", headers=auth["manager"], json={"reason": "x"}).status_code == 403  # no permission
    p = pay(client, a, sup, 100, [(inv["id"], 100)]).json()
    r = client.post(f"/supplier-invoices/{inv['id']}/void", headers=auth["ceo"], json={"reason": "duplicate"})
    assert r.status_code == 422 and "payments" in r.json()["detail"]
    client.post(f"/supplier-payments/{p['id']}/reverse", headers=auth["ceo"], json={"reason": "wrong supplier"})
    assert client.post(f"/supplier-invoices/{inv['id']}/void", headers=auth["ceo"], json={"reason": ""}).status_code == 422
    v = client.post(f"/supplier-invoices/{inv['id']}/void", headers=auth["ceo"], json={"reason": "entered in error"})
    assert v.status_code == 200 and v.json()["status"] == "void" and balance(client, a, sup) == 0
    assert client.post(f"/supplier-invoices/{inv['id']}/void", headers=auth["ceo"], json={"reason": "again"}).status_code == 409
    again = mk_invoice(client, a, sup, po, [(catalog["rose"], 10, 40)], no="VOID1")
    assert again.status_code == 201                                                  # the number is free once voided
    consistent(client, auth["ceo"])


# ---- payments ----------------------------------------------------------------------------------------------------
def _posted_invoice(client, auth, catalog, locations, sup, qty=10, price=40, no="P-INV"):
    po = mk_po(client, auth, sup, locations["WH"].id, [(catalog["rose"], qty, price)])
    po_receive(client, auth["stock"], po, [(catalog["rose"], qty, 0, 0)])
    return mk_invoice(client, auth["accounts"], sup, po, [(catalog["rose"], qty, price)], no=no).json()


def test_partial_payments_and_outstanding(client, auth, catalog, locations, sup):
    a = auth["accounts"]
    inv = _posted_invoice(client, auth, catalog, locations, sup)
    p1 = pay(client, a, sup, 150, [(inv["id"], 150)])
    assert p1.status_code == 201 and p1.json()["unallocated"] == "0.00"
    got = client.get(f"/supplier-invoices/{inv['id']}", headers=a).json()
    assert got["outstanding"] == "250.00" and got["payment_status"] == "partially_paid" and got["paid"] == "150.00"
    assert balance(client, a, sup) == D("250")
    over = pay(client, a, sup, 300, [(inv["id"], 300)])
    assert over.status_code == 422 and "still outstanding" in over.json()["detail"]
    assert pay(client, a, sup, 100).status_code == 422                                # unallocated needs an explicit choice
    assert pay(client, a, sup, 100, [(inv["id"], 150)]).status_code == 422             # allocations above the amount
    assert pay(client, a, sup, 10, [(inv["id"], 5), (inv["id"], 5)]).status_code == 422  # same invoice twice
    assert pay(client, a, sup, 100, method="cheque").status_code == 422                # cheque needs a number
    onacc = pay(client, a, sup, 100, on_account=True)
    assert onacc.status_code == 201 and onacc.json()["unallocated"] == "100.00"
    assert balance(client, a, sup) == D("150")                                        # the prepayment already reduced the balance
    al = client.post(f"/supplier-payments/{onacc.json()['id']}/allocate", headers=a, json={"allocations": [{"invoice_id": inv["id"], "amount": "100"}]})
    assert al.status_code == 200 and al.json()["unallocated"] == "0.00"
    assert balance(client, a, sup) == D("150")                                        # allocating moves nothing in the ledger
    assert client.post(f"/supplier-payments/{onacc.json()['id']}/allocate", headers=a,
                       json={"allocations": [{"invoice_id": inv["id"], "amount": "1"}]}).status_code == 422
    last = pay(client, a, sup, 150, [(inv["id"], 150)])
    assert last.status_code == 201
    done = client.get(f"/supplier-invoices/{inv['id']}", headers=a).json()
    assert done["payment_status"] == "paid" and done["outstanding"] == "0.00" and balance(client, a, sup) == 0
    consistent(client, auth["ceo"])


def test_payment_cannot_target_other_suppliers_or_unposted_invoices(client, auth, catalog, locations, sup):
    a = auth["accounts"]
    inv = _posted_invoice(client, auth, catalog, locations, sup)
    other = mk_supplier(client, a, "Second Supplier")
    r = pay(client, a, other, 50, [(inv["id"], 50)])
    assert r.status_code == 422 and "different supplier" in r.json()["detail"]
    assert pay(client, a, sup, 50, [(99999, 50)]).status_code == 422
    assert pay(client, a, sup, 50, paid_date=iso(TODAY + timedelta(days=1))) .status_code in (422,)


def test_payment_reversal(client, auth, catalog, locations, sup):
    a = auth["accounts"]
    inv = _posted_invoice(client, auth, catalog, locations, sup)
    p = pay(client, a, sup, 400, [(inv["id"], 400)]).json()
    assert balance(client, a, sup) == 0
    assert client.post(f"/supplier-payments/{p['id']}/reverse", headers=a, json={"reason": "bounced"}).status_code == 403      # own payment
    assert client.post(f"/supplier-payments/{p['id']}/reverse", headers=auth["manager"], json={"reason": "bounced"}).status_code == 403  # no permission
    assert client.post(f"/supplier-payments/{p['id']}/reverse", headers=auth["ceo"], json={"reason": " "}).status_code in (422,)
    r = client.post(f"/supplier-payments/{p['id']}/reverse", headers=auth["ceo"], json={"reason": "cheque bounced"})
    assert r.status_code == 200 and r.json()["status"] == "reversed"
    got = client.get(f"/supplier-invoices/{inv['id']}", headers=a).json()
    assert got["outstanding"] == "400.00" and got["payment_status"] == "unpaid" and balance(client, a, sup) == D("400")
    assert client.post(f"/supplier-payments/{p['id']}/reverse", headers=auth["ceo"], json={"reason": "again"}).status_code == 409
    assert pay(client, a, sup, 400, [(inv["id"], 400)]).status_code == 201           # can be paid again
    consistent(client, auth["ceo"])


def test_payment_idempotency(client, auth, catalog, locations, sup):
    a = auth["accounts"]
    inv = _posted_invoice(client, auth, catalog, locations, sup)
    k = {"idempotency_key": "pay-key-1"}
    r1 = pay(client, a, sup, 100, [(inv["id"], 100)], **k)
    r2 = pay(client, a, sup, 100, [(inv["id"], 100)], **k)
    assert r1.status_code == r2.status_code == 201 and r1.json()["id"] == r2.json()["id"]
    assert balance(client, a, sup) == D("300")


def test_aging_buckets_and_payables(client, auth, catalog, locations, sup):
    a, wh = auth["accounts"], locations["WH"].id
    po = mk_po(client, auth, sup, wh, [(catalog["rose"], 100, 10)])
    for no, age, qty in (("AGE-0", 0, 10), ("AGE-1", 40, 20), ("AGE-2", 75, 30), ("AGE-3", 150, 40)):
        r = client.post("/supplier-invoices", headers=a, json={
            "supplier_id": sup["id"], "invoice_no": no, "invoice_date": iso(TODAY - timedelta(days=age)), "due_date": iso(TODAY - timedelta(days=age)),
            "purchase_order_id": po["id"], "lines": [{"po_line_id": po["lines"][0]["id"], "qty": qty, "unit_price": "10"}]})
        assert r.status_code == 201, r.text
    pb = client.get("/purchasing/payables", headers=a).json()
    t = pb["aging_totals"]
    assert t == {"not_due": "100.00", "1_30": "0.00", "31_60": "200.00", "61_90": "300.00", "over_90": "400.00"} or \
        (t["not_due"] == "100.00" and t["31_60"] == "200.00" and t["over_90"] == "400.00")
    assert pb["total_balance"] == "1000.00" and pb["rows"][0]["overdue"] == "900.00"
    overdue = client.get("/supplier-invoices?overdue_only=true", headers=a).json()
    assert overdue["total"] == 3 and all(i["overdue"] for i in overdue["items"])


# ---- adjustments -------------------------------------------------------------------------------------------------
def test_credit_adjustment_against_invoice_needs_independent_approval(client, auth, catalog, locations, sup):
    a = auth["accounts"]
    inv = _posted_invoice(client, auth, catalog, locations, sup)
    body = {"supplier_id": sup["id"], "kind": "reduce_payable", "amount": "50", "adjustment_date": iso(TODAY),
            "document_no": "CN-77", "reason": "Price correction", "invoice_id": inv["id"]}
    r = client.post("/supplier-adjustments", headers=a, json=body)
    assert r.status_code == 201 and r.json()["status"] == "pending"
    adj = r.json()
    assert balance(client, a, sup) == D("400")                                        # nothing changes until approved
    assert client.post(f"/supplier-adjustments/{adj['id']}/approve", headers=a, json={}).status_code == 403   # no permission
    assert client.post(f"/supplier-adjustments/{adj['id']}/approve", headers=auth["manager"], json={"note": "ok"}).status_code == 200
    assert balance(client, a, sup) == D("350")
    got = client.get(f"/supplier-invoices/{inv['id']}", headers=a).json()
    assert got["outstanding"] == "350.00" and got["credited"] == "50.00"
    assert client.post(f"/supplier-adjustments/{adj['id']}/approve", headers=auth["manager"], json={}).status_code == 409
    # the requester cannot approve their own request even with permission
    own = client.post("/supplier-adjustments", headers=auth["ceo"], json={**body, "amount": "10", "document_no": "CN-78"}).json()
    assert client.post(f"/supplier-adjustments/{own['id']}/approve", headers=auth["ceo"], json={}).status_code == 403
    # a credit larger than what is outstanding no longer fits
    big = client.post("/supplier-adjustments", headers=a, json={**body, "amount": "400"}).json()
    r = client.post(f"/supplier-adjustments/{big['id']}/approve", headers=auth["manager"], json={})
    assert r.status_code == 422 and "no longer fits" in r.json()["detail"]
    rj = client.post(f"/supplier-adjustments/{big['id']}/reject", headers=auth["manager"], json={"reason": "wrong amount"})
    assert rj.json()["status"] == "rejected"
    # an invoice carrying an applied credit cannot be voided before the credit is dealt with
    assert client.post(f"/supplier-invoices/{inv['id']}/void", headers=auth["ceo"], json={"reason": "x"}).status_code == 422
    consistent(client, auth["ceo"])


def test_increase_adjustment_and_cancel(client, auth, catalog, locations, sup):
    a = auth["accounts"]
    _posted_invoice(client, auth, catalog, locations, sup)
    r = client.post("/supplier-adjustments", headers=a, json={"supplier_id": sup["id"], "kind": "increase_payable", "amount": "25",
                    "adjustment_date": iso(TODAY), "reason": "Late delivery fee billed"}).json()
    assert client.post(f"/supplier-adjustments/{r['id']}/approve", headers=auth["manager"], json={}).status_code == 200
    assert balance(client, a, sup) == D("425")
    c = client.post("/supplier-adjustments", headers=a, json={"supplier_id": sup["id"], "kind": "reduce_payable", "amount": "5",
                    "adjustment_date": iso(TODAY), "reason": "oops"}).json()
    assert client.post(f"/supplier-adjustments/{c['id']}/cancel", headers=auth["ceo"]).status_code == 422     # only the requester
    assert client.post(f"/supplier-adjustments/{c['id']}/cancel", headers=a).json()["status"] == "cancelled"
    bad = client.post("/supplier-adjustments", headers=a, json={"supplier_id": sup["id"], "kind": "increase_payable", "amount": "5",
                      "adjustment_date": iso(TODAY), "reason": "x", "invoice_id": 1})
    assert bad.status_code == 422
    consistent(client, auth["ceo"])


# ---- supplier returns --------------------------------------------------------------------------------------------
def test_supplier_return_moves_stock_and_links_credit(client, auth, catalog, locations, sup):
    a, wh = auth["accounts"], locations["WH"].id
    po = mk_po(client, auth, sup, wh, [(catalog["rose"], 10, 40)])
    po_receive(client, auth["stock"], po, [(catalog["rose"], 8, 2, 0)])
    body = {"supplier_id": sup["id"], "location_id": wh, "reason": "Damaged on arrival", "purchase_order_id": po["id"],
            "lines": [{"variant_id": catalog["rose"], "bucket": "damaged", "qty": 2}]}
    assert client.post("/supplier-returns", headers=auth["stock"], json=body).status_code == 403            # stock keepers cannot create
    r = client.post("/supplier-returns", headers=a, json=body)
    assert r.status_code == 201 and r.json()["status"] == "draft", r.text
    ret = r.json()
    s = next(i for i in client.get(f"/inventory/stock?location_id={wh}", headers=auth["ceo"]).json()["items"] if i["variant_id"] == catalog["rose"])
    assert s["damaged"] == 2                                                         # nothing moves until dispatch
    d = client.post(f"/supplier-returns/{ret['id']}/dispatch", headers=a)
    assert d.status_code == 200 and d.json()["status"] == "dispatched"
    s = next(i for i in client.get(f"/inventory/stock?location_id={wh}", headers=auth["ceo"]).json()["items"] if i["variant_id"] == catalog["rose"])
    assert s["damaged"] == 0 and s["on_hand_good"] == 8 and D(s["value"]) == D("320")
    assert client.get(f"/supplier-returns/{ret['id']}", headers=auth["ceo"]).json()["lines"][0]["stock_value"] == "80.0000"
    assert client.post(f"/supplier-returns/{ret['id']}/dispatch", headers=a).status_code == 409            # no double dispatch
    assert client.post(f"/supplier-returns/{ret['id']}/cancel", headers=a, json={"reason": "x"}).status_code == 409
    # not enough stock: refused, nothing created in stock
    big = client.post("/supplier-returns", headers=a, json={**body, "lines": [{"variant_id": catalog["rose"], "bucket": "good", "qty": 9}]}).json()
    assert client.post(f"/supplier-returns/{big['id']}/dispatch", headers=a).status_code == 422
    # the supplier's credit arrives as an approved adjustment linked to the return
    adj = client.post("/supplier-adjustments", headers=a, json={"supplier_id": sup["id"], "kind": "reduce_payable", "amount": "80",
                      "adjustment_date": iso(TODAY), "reason": "Credit for returned goods", "supplier_return_id": ret["id"],
                      "document_no": "CN-1"}).json()
    assert client.post(f"/supplier-adjustments/{adj['id']}/approve", headers=auth["manager"], json={}).status_code == 200
    assert client.get(f"/supplier-returns/{ret['id']}", headers=a).json()["status"] == "credited"
    assert balance(client, a, sup) == D("-80")                                       # nothing was invoiced, so the supplier owes us
    consistent(client, auth["ceo"])


def test_draft_return_can_be_cancelled_and_validated(client, auth, catalog, locations, sup):
    a, wh = auth["accounts"], locations["WH"].id
    body = {"supplier_id": sup["id"], "location_id": wh, "reason": "Wrong shade",
            "lines": [{"variant_id": catalog["rose"], "bucket": "good", "qty": 1}]}
    r = client.post("/supplier-returns", headers=a, json=body).json()
    assert client.post(f"/supplier-returns/{r['id']}/cancel", headers=a, json={"reason": "changed mind"}).json()["status"] == "cancelled"
    assert client.post("/supplier-returns", headers=a, json={**body, "lines": [{"variant_id": catalog["rose"], "bucket": "nope", "qty": 1}]}).status_code == 422
    assert client.post("/supplier-returns", headers=a, json={**body, "reason": ""}).status_code == 422


# ---- permissions and scope -----------------------------------------------------------------------------------------
def test_permission_matrix_for_purchasing(client, auth, catalog, locations, sup):
    wh, b2 = locations["WH"].id, locations["B2"].id
    po = mk_po(client, auth, sup, wh, [(catalog["rose"], 10, 40)])
    # stock keepers see orders (quantities only), never costs, invoices, payments or supplier balances
    got = client.get(f"/purchase-orders/{po['id']}", headers=auth["stock"]).json()
    assert "total" not in got and "unit_cost" not in got["lines"][0] and got["lines"][0]["qty_ordered"] == 10
    assert "unit_cost" not in client.get(f"/purchase-orders/{po['id']}/matching", headers=auth["stock"]).json()["lines"][0]
    for path in ("/supplier-invoices", "/supplier-payments", "/supplier-adjustments", "/purchasing/payables",
                 f"/suppliers/{sup['id']}", f"/suppliers/{sup['id']}/statement"):
        assert client.get(path, headers=auth["stock"]).status_code == 403, path
    assert "balance" not in client.get("/suppliers", headers=auth["stock"]).json()["items"][0]
    # order bookers have no purchasing access at all
    for path in ("/purchase-orders", "/suppliers", "/supplier-returns", "/supplier-invoices"):
        assert client.get(path, headers=auth["booker"]).status_code == 403, path
    # managers view and approve but cannot pay, void, reverse or create invoices
    m = auth["manager"]
    assert client.get("/supplier-invoices", headers=m).status_code == 200
    assert pay(client, m, sup, 10, on_account=True).status_code == 403
    assert client.post("/suppliers", headers=m, json={"name": "Mgr Supplier"}).status_code == 403
    assert mk_invoice(client, m, sup, po, [(catalog["rose"], 1, 40)], no="M1").status_code == 403
    # accounts cannot approve orders or variances
    draft = mk_po(client, auth, sup, wh, [(catalog["nude"], 1, 5)], approve=False)
    assert client.post(f"/purchase-orders/{draft['id']}/approve", headers=auth["accounts"]).status_code == 403
    # location scope: the stock keeper (WH, B1) cannot see an order delivered to B2
    far = mk_po(client, auth, sup, b2, [(catalog["rose"], 1, 5)])
    assert client.get(f"/purchase-orders/{far['id']}", headers=auth["stock"]).status_code == 404
    ids = {p["id"] for p in client.get("/purchase-orders", headers=auth["stock"]).json()["items"]}
    assert far["id"] not in ids and po["id"] in ids
    assert client.get("/purchase-orders?location_id=%d" % b2, headers=auth["stock"]).status_code == 404
    # unauthenticated
    assert client.get("/purchase-orders").status_code == 401


def test_default_role_grants(db):
    from backend.permissions import DEFAULT_ROLES
    mgr, acc, stk = (DEFAULT_ROLES[r]["permissions"] for r in ("manager", "accounts", "stock_keeper"))
    assert not {"purchasing.pay", "purchasing.reverse", "purchasing.manage", "suppliers.manage",
                "accounting.post", "accounting.close_period", "accounting.reopen_period"} & mgr
    assert {"purchasing.approve", "purchasing.approve_variance"} <= mgr
    assert not {"purchasing.approve", "purchasing.approve_variance"} & acc          # whoever pays does not approve
    assert not {"suppliers.view", "purchasing.manage", "purchasing.pay", "products.view_cost"} & stk


def test_audit_trail_for_purchasing(client, auth, catalog, locations, sup):
    a = auth["accounts"]
    inv = _posted_invoice(client, auth, catalog, locations, sup)
    p = pay(client, a, sup, 100, [(inv["id"], 100)]).json()
    client.post(f"/supplier-payments/{p['id']}/reverse", headers=auth["ceo"], json={"reason": "test"})
    actions = {x["action"] for x in client.get("/audit?page_size=200", headers=auth["ceo"]).json()["items"]}
    assert {"supplier.created", "po.created", "po.approved", "supplier_invoice.created", "supplier_invoice.posted",
            "supplier_payment.created", "supplier_payment.reversed", "receipt.posted"} <= actions


# ---- ledger protection and concurrency --------------------------------------------------------------------------
def test_supplier_ledger_is_append_only(db, client, auth, catalog, locations, sup):
    _posted_invoice(client, auth, catalog, locations, sup)
    for stmt in ("UPDATE supplier_ledger SET amount = 1", "DELETE FROM supplier_ledger"):
        with pytest.raises(DBAPIError):
            db.execute(text(stmt))
        db.rollback()


def test_ledger_unique_source_blocks_a_second_liability(db, client, auth, catalog, locations, sup):
    from backend.models import SupplierLedgerEntry
    from sqlalchemy.exc import IntegrityError
    inv = _posted_invoice(client, auth, catalog, locations, sup)
    db.add(SupplierLedgerEntry(supplier_id=sup["id"], entry_type="invoice", amount=D("400"), entry_date=TODAY,
                               source_type="supplier_invoice", source_id=inv["id"], ref=inv["ref"]))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_concurrent_payments_cannot_overpay_an_invoice(client, auth, catalog, locations, sup, users):
    from backend.db import get_session_factory
    from backend.models import User
    from backend.services import payment_service
    from backend.services.errors import BusinessRuleError
    inv = _posted_invoice(client, auth, catalog, locations, sup)
    results, barrier = [], threading.Barrier(6)

    def worker():
        with get_session_factory()() as s:
            actor = s.get(User, users["accounts"].id)
            barrier.wait()
            try:
                payment_service.create_payment(s, actor=actor, supplier_id=sup["id"], amount=400, method="cash", paid_date=TODAY,
                                               reference=None, notes=None, allocations=[{"invoice_id": inv["id"], "amount": 400}],
                                               on_account=False, idempotency_key=None, ip=None)
                s.commit()
                results.append("ok")
            except BusinessRuleError:
                s.rollback()
                results.append("refused")

    ts = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert results.count("ok") == 1 and results.count("refused") == 5
    assert client.get(f"/supplier-invoices/{inv['id']}", headers=auth["accounts"]).json()["outstanding"] == "0.00"
    assert balance(client, auth["accounts"], sup) == 0
    consistent(client, auth["ceo"])


def test_concurrent_invoices_cannot_both_pass_the_order_quantity(client, auth, catalog, locations, sup, users):
    from backend.db import get_session_factory
    from backend.models import User
    from backend.services import invoice_service
    po = mk_po(client, auth, sup, locations["WH"].id, [(catalog["rose"], 10, 40)])
    results, barrier = [], threading.Barrier(2)

    def worker(n):
        with get_session_factory()() as s:
            actor = s.get(User, users["accounts"].id)
            barrier.wait()
            inv = invoice_service.create_invoice(
                s, actor=actor, supplier_id=sup["id"], invoice_no=f"CC-{n}", invoice_date=TODAY, due_date=None,
                purchase_order_id=po["id"], lines=[{"po_line_id": po["lines"][0]["id"], "qty": 6, "unit_price": "40"}],
                charges=None, discount=0, tax=0, notes=None, post_now=True, ip=None)
            s.commit()
            results.append(inv.status)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(results) == ["held", "posted"]                                      # 12 invoiced against 10 ordered => one is held
    assert balance(client, auth["accounts"], sup) == D("240")


def test_concurrent_duplicate_invoice_numbers_book_once(client, auth, catalog, locations, sup, users):
    from backend.db import get_session_factory
    from backend.models import User
    from backend.services import invoice_service
    from backend.services.errors import ConflictError
    from sqlalchemy.exc import IntegrityError
    results, barrier = [], threading.Barrier(4)

    def worker():
        with get_session_factory()() as s:
            actor = s.get(User, users["accounts"].id)
            barrier.wait()
            try:
                invoice_service.create_invoice(
                    s, actor=actor, supplier_id=sup["id"], invoice_no="SAME-1", invoice_date=TODAY, due_date=None,
                    purchase_order_id=None, lines=[{"description": "Service", "qty": 1, "unit_price": "100"}],
                    charges=None, discount=0, tax=0, notes=None, post_now=True, ip=None)
                s.commit()
                results.append("ok")
            except (ConflictError, IntegrityError):
                s.rollback()
                results.append("dup")

    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert results.count("ok") == 1
    assert balance(client, auth["accounts"], sup) == D("100")
    consistent(client, auth["ceo"])


def test_full_cycle_ties_out(client, auth, catalog, locations, sup):
    """Order -> partial receipts -> invoice -> partial payments -> return and credit; every view agrees at the end."""
    a, wh = auth["accounts"], locations["WH"].id
    po = mk_po(client, auth, sup, wh, [(catalog["rose"], 20, 40), (catalog["nude"], 10, 30)])
    po_receive(client, auth["stock"], po, [(catalog["rose"], 12, 0, 0), (catalog["nude"], 10, 0, 0)])
    po_receive(client, auth["stock"], po, [(catalog["rose"], 6, 2, 0)], additional_costs=[{"description": "Freight", "amount": "40"}])
    assert client.get(f"/purchase-orders/{po['id']}", headers=a).json()["status"] == "received"
    inv = mk_invoice(client, a, sup, po, [(catalog["rose"], 20, 40), (catalog["nude"], 10, 30)], no="FULL-1").json()
    assert inv["total"] == "1100.00" and inv["match_status"] == "matched"
    assert pay(client, a, sup, 600, [(inv["id"], 600)]).status_code == 201
    ret = client.post("/supplier-returns", headers=a, json={"supplier_id": sup["id"], "location_id": wh, "reason": "damaged",
        "lines": [{"variant_id": catalog["rose"], "bucket": "damaged", "qty": 2}], "dispatch_now": True}).json()
    adj = client.post("/supplier-adjustments", headers=a, json={"supplier_id": sup["id"], "kind": "reduce_payable", "amount": "80",
        "adjustment_date": iso(TODAY), "reason": "returned", "invoice_id": inv["id"], "supplier_return_id": ret["id"]}).json()
    client.post(f"/supplier-adjustments/{adj['id']}/approve", headers=auth["manager"], json={})
    assert balance(client, a, sup) == D("420")                                        # 1100 - 600 - 80
    summary = client.get(f"/suppliers/{sup['id']}", headers=a).json()["summary"]
    assert summary["balance"] == "420.00" and summary["open_invoices_total"] == "420.00"
    st = client.get(f"/suppliers/{sup['id']}/statement", headers=a).json()
    assert [e["type"] for e in st["entries"]] == ["invoice", "payment", "adjustment"] and st["closing_balance"] == "420.00"
    # stock: 18 + 10 owned, 2 damaged returned => rose 18 good, nude 10; total value = 18*40+... + freight
    assert D(client.get("/purchasing/grni", headers=a).json()["total_value"]) == 0
    consistent(client, auth["ceo"])
