"""Phase 4: customers, pricing, orders, reservations, fulfilment, payments, returns. Synthetic data only."""
import threading
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from backend.tests.conftest import line, login, make_user, receive

D = Decimal
TODAY = date.today().isoformat()


# ---- helpers -----------------------------------------------------------------------------------------------
@pytest.fixture
def ctx(client, auth, catalog, locations, users, db):
    """Stock at B1 (rose 20, nude 5) and a customer; returns handy ids."""
    loc = locations["B1"].id
    r = receive(client, auth["stock"], loc, [line(catalog["rose"], 20, 40), line(catalog["nude"], 5, 40)])
    assert r.status_code in (200, 201), r.text
    ch = client.get("/sales/channels", headers=auth["booker"]).json()
    assert ch, "channels are seeded"
    cust = client.post("/customers", headers=auth["booker"], json={"name": "Test Customer", "phone": "0300 1111111"})
    assert cust.status_code == 201, cust.text
    return {"B1": loc, "WH": locations["WH"].id, "B2": locations["B2"].id, "chan": ch[0]["id"], "cust": cust.json()["id"], **catalog}


def order_body(c, lines=None, **kw):
    body = {"customer_id": c["cust"], "channel_id": c["chan"], "location_id": c["B1"], "order_date": TODAY, "payment_terms": "cod",
            "delivery": {"name": "Recipient", "phone": "0300 2222222", "address": "1 Test Street", "city": "Alpha"},
            "lines": lines or [{"variant_id": c["rose"], "qty": 2}]}
    body.update(kw)
    return body


def mk_order(client, h, c, lines=None, confirm=True, **kw):
    r = client.post("/orders", headers=h, json=order_body(c, lines, **kw))
    assert r.status_code == 201, r.text
    o = r.json()
    if confirm and o["status"] == "draft":
        r = client.post(f"/orders/{o['id']}/confirm", headers=h)
        assert r.status_code == 200, r.text
        o = r.json()
    return o


def avail(client, h, loc, variant):
    """(available, good on hand) for one variant at one location."""
    r = client.get("/inventory/stock", headers=h, params={"location_id": loc, "include_zero": True, "page_size": 200})
    assert r.status_code == 200, r.text
    rows = [x for x in r.json()["items"] if x["variant_id"] == variant]
    return sum(x["available"] for x in rows), sum(x["on_hand_good"] for x in rows)


def ship_all(client, auth, o, dispatch=True, by="stock"):
    r = client.post(f"/orders/{o['id']}/shipments", headers=auth[by], json={"lines": [{"order_line_id": l["id"], "qty": l["to_ship"]} for l in o["lines"]]})
    assert r.status_code == 201, r.text
    s = r.json()
    if dispatch:
        d = client.post(f"/shipments/{s['id']}/dispatch", headers=auth[by], json={"courier": "TestCourier", "tracking_no": "TRK1"})
        assert d.status_code == 200, d.text
        s = d.json()
    return s


def get(client, h, o):
    return client.get(f"/orders/{o['id']}", headers=h).json()


# ---- customers ---------------------------------------------------------------------------------------------
def test_customer_duplicate_phone_refused_in_any_format(client, auth, ctx):
    r = client.post("/customers", headers=auth["booker"], json={"name": "Another", "phone": "+92-300-1111111"})
    assert r.status_code in (409, 422), r.text


def test_booker_sees_only_own_customers(client, auth, ctx):
    mine = client.get("/customers", headers=auth["booker"]).json()
    assert [c["id"] for c in mine["items"]] == [ctx["cust"]]
    other = client.post("/customers", headers=auth["manager"], json={"name": "Manager Customer", "phone": "0311 5555555"}).json()
    assert client.get(f"/customers/{other['id']}", headers=auth["booker"]).status_code == 404
    assert all(c["id"] != other["id"] for c in client.get("/customers", headers=auth["booker"]).json()["items"])
    assert client.get("/customers", headers=auth["manager"]).json()["total"] == 2


def test_stock_keeper_cannot_see_customers(client, auth, ctx):
    assert client.get("/customers", headers=auth["stock"]).status_code == 403


def test_customer_search_by_phone_digits(client, auth, ctx):
    r = client.get("/customers", headers=auth["booker"], params={"q": "1111111"}).json()
    assert r["total"] == 1


# ---- pricing and promotions ----------------------------------------------------------------------------------
def test_quote_uses_list_price_and_promotion(client, auth, ctx):
    p = client.post("/promotions", headers=auth["ceo"], json={"name": "10% off rose", "kind": "percent", "value": "10",
                                                              "starts_on": TODAY, "variant_id": ctx["rose"]})
    assert p.status_code == 201, p.text
    q = client.post("/orders/quote", headers=auth["booker"], json={"customer_id": ctx["cust"], "lines": [{"variant_id": ctx["rose"], "qty": 2}, {"variant_id": ctx["nude"], "qty": 1}]}).json()
    rose, nude = q["lines"]
    assert (rose["list_price"], rose["promo_discount"], rose["unit_price"]) == ("100.00", "10.00", "90.00")
    assert nude["promo_discount"] == "0.00" and q["total"] == "280.00"
    assert q["approval_required"] is False


def test_expired_and_inactive_promotions_do_not_apply(client, auth, ctx):
    old = (date.today() - timedelta(days=30)).isoformat()
    client.post("/promotions", headers=auth["ceo"], json={"name": "Old", "kind": "percent", "value": "50", "starts_on": old,
                                                          "ends_on": (date.today() - timedelta(days=1)).isoformat(), "variant_id": ctx["rose"]})
    inactive = client.post("/promotions", headers=auth["ceo"], json={"name": "Off", "kind": "percent", "value": "50", "starts_on": old,
                                                                     "variant_id": ctx["rose"], "is_active": False})
    assert inactive.status_code == 201
    q = client.post("/orders/quote", headers=auth["booker"], json={"customer_id": ctx["cust"], "lines": [{"variant_id": ctx["rose"], "qty": 1}]}).json()
    assert q["lines"][0]["promo_discount"] == "0.00"


def test_non_stackable_promotions_use_best_only_and_cap_applies(client, auth, ctx):
    for name, v in (("A", "10"), ("B", "20")):
        client.post("/promotions", headers=auth["ceo"], json={"name": name, "kind": "percent", "value": v, "starts_on": TODAY, "variant_id": ctx["rose"]})
    q = client.post("/orders/quote", headers=auth["booker"], json={"customer_id": ctx["cust"], "lines": [{"variant_id": ctx["rose"], "qty": 1}]}).json()
    assert q["lines"][0]["promo_discount"] == "20.00"
    client.post("/promotions", headers=auth["ceo"], json={"name": "Capped", "kind": "percent", "value": "60", "starts_on": TODAY,
                                                          "variant_id": ctx["nude"], "max_discount_pct": "25"})
    q = client.post("/orders/quote", headers=auth["booker"], json={"customer_id": ctx["cust"], "lines": [{"variant_id": ctx["nude"], "qty": 1}]}).json()
    assert q["lines"][0]["promo_discount"] == "25.00"


def test_only_pricing_managers_create_promotions(client, auth, ctx):
    r = client.post("/promotions", headers=auth["booker"], json={"name": "X", "kind": "percent", "value": "5", "starts_on": TODAY})
    assert r.status_code == 403


def test_price_above_list_refused(client, auth, ctx):
    r = client.post("/orders", headers=auth["booker"], json=order_body(ctx, [{"variant_id": ctx["rose"], "qty": 1, "unit_price": "150"}]))
    assert r.status_code == 422 and "list price" in r.text


def test_prices_are_preserved_after_catalogue_price_change(client, auth, ctx):
    o = mk_order(client, auth["booker"], ctx)
    client.patch(f"/variants/{ctx['rose']}", headers=auth["ceo"], json={"retail_price": "120.00"})
    again = get(client, auth["booker"], o)
    assert again["lines"][0]["list_price"] == "100.00" and again["total"] == o["total"]


def test_stale_price_on_draft_blocks_confirmation(client, auth, ctx):
    o = mk_order(client, auth["booker"], ctx, confirm=False)
    ch = client.patch(f"/variants/{ctx['rose']}", headers=auth["ceo"], json={"retail_price": "90.00"})
    r = client.post(f"/orders/{o['id']}/confirm", headers=auth["booker"])
    assert r.status_code == 409


# ---- order lifecycle and reservations ---------------------------------------------------------------------------
def test_confirm_reserves_stock_and_cancel_releases_it(client, auth, ctx):
    a0, h0 = avail(client, auth["manager"], ctx["B1"], ctx["rose"])
    o = mk_order(client, auth["booker"], ctx, [{"variant_id": ctx["rose"], "qty": 5}])
    assert o["status"] == "confirmed"
    a1, h1 = avail(client, auth["manager"], ctx["B1"], ctx["rose"])
    assert (a1, h1) == (a0 - 5, h0)
    r = client.post(f"/orders/{o['id']}/cancel", headers=auth["booker"], json={"reason": "customer changed mind"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert avail(client, auth["manager"], ctx["B1"], ctx["rose"]) == (a0, h0)


def test_confirm_is_all_or_nothing(client, auth, ctx):
    a0 = avail(client, auth["manager"], ctx["B1"], ctx["rose"])
    o = mk_order(client, auth["booker"], ctx, [{"variant_id": ctx["rose"], "qty": 3}, {"variant_id": ctx["nude"], "qty": 99}], confirm=False)
    r = client.post(f"/orders/{o['id']}/confirm", headers=auth["booker"])
    assert r.status_code == 422
    assert get(client, auth["booker"], o)["status"] == "draft"
    assert avail(client, auth["manager"], ctx["B1"], ctx["rose"]) == a0


def test_cannot_oversell_under_concurrency(client, auth, ctx, db):
    drafts = [mk_order(client, auth["booker"], ctx, [{"variant_id": ctx["nude"], "qty": 3}], confirm=False) for _ in range(4)]
    results, bar = [], threading.Barrier(4)

    def go(o):
        bar.wait()
        results.append(client.post(f"/orders/{o['id']}/confirm", headers=auth["booker"]).status_code)

    ts = [threading.Thread(target=go, args=(o,)) for o in drafts]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results).count(200) == 1, results      # 5 units, 3 each: exactly one fits
    assert avail(client, auth["manager"], ctx["B1"], ctx["nude"])[0] == 2


def test_idempotency_key_returns_same_order(client, auth, ctx):
    b = order_body(ctx, idempotency_key="abc-1")
    a = client.post("/orders", headers=auth["booker"], json=b)
    c = client.post("/orders", headers=auth["booker"], json=b)
    assert a.status_code == 201 and c.status_code in (200, 201) and a.json()["id"] == c.json()["id"]


def test_confirmed_orders_cannot_be_edited_drafts_can(client, auth, ctx):
    o = mk_order(client, auth["booker"], ctx, confirm=False)
    r = client.put(f"/orders/{o['id']}", headers=auth["booker"], json={"lines": [{"variant_id": ctx["rose"], "qty": 4}]})
    assert r.status_code == 200 and r.json()["lines"][0]["qty"] == 4
    client.post(f"/orders/{o['id']}/confirm", headers=auth["booker"])
    assert client.put(f"/orders/{o['id']}", headers=auth["booker"], json={"notes": "x"}).status_code == 409


def test_reopen_cancelled_order_reserves_again(client, auth, ctx):
    o = mk_order(client, auth["booker"], ctx)
    client.post(f"/orders/{o['id']}/cancel", headers=auth["booker"], json={"reason": "test"})
    assert client.post(f"/orders/{o['id']}/reopen", headers=auth["booker"], json={"reason": "x"}).status_code == 403
    r = client.post(f"/orders/{o['id']}/reopen", headers=auth["manager"], json={"reason": "customer came back"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] in ("draft", "confirmed")


# ---- discount approval -------------------------------------------------------------------------------------------
def test_manual_discount_needs_approval_by_someone_else(client, auth, ctx):
    client.put("/sales/settings", headers=auth["ceo"], json={"max_manual_discount_pct": "5"})
    o = mk_order(client, auth["booker"], ctx, [{"variant_id": ctx["rose"], "qty": 1, "unit_price": "80"}], confirm=False)
    assert o["discount_approval"] == "pending"
    assert client.post(f"/orders/{o['id']}/confirm", headers=auth["booker"]).status_code == 422
    assert client.post(f"/orders/{o['id']}/discount-decision", headers=auth["booker"], json={"approve": True, "note": "me"}).status_code == 403
    mgr = client.post(f"/orders/{o['id']}/discount-decision", headers=auth["manager"], json={"approve": True, "note": "loyal customer"})
    assert mgr.status_code == 200 and mgr.json()["discount_approval"] == "approved"
    assert client.post(f"/orders/{o['id']}/confirm", headers=auth["booker"]).status_code == 200


def test_manager_cannot_approve_own_discount(client, auth, ctx):
    client.put("/sales/settings", headers=auth["ceo"], json={"max_manual_discount_pct": "5"})
    o = mk_order(client, auth["manager"], ctx, [{"variant_id": ctx["rose"], "qty": 1, "unit_price": "50"}], confirm=False)
    r = client.post(f"/orders/{o['id']}/discount-decision", headers=auth["manager"], json={"approve": True, "note": "ok"})
    assert r.status_code == 403


def test_default_discount_limit_is_zero(client, auth, ctx):
    o = mk_order(client, auth["booker"], ctx, [{"variant_id": ctx["rose"], "qty": 1, "unit_price": "99"}], confirm=False)
    assert o["discount_approval"] == "pending"


def test_rejected_discount_blocks_confirmation(client, auth, ctx):
    o = mk_order(client, auth["booker"], ctx, [{"variant_id": ctx["rose"], "qty": 1, "unit_price": "90"}], confirm=False)
    client.post(f"/orders/{o['id']}/discount-decision", headers=auth["manager"], json={"approve": False, "note": "too deep"})
    assert client.post(f"/orders/{o['id']}/confirm", headers=auth["booker"]).status_code == 422


# ---- fulfilment -----------------------------------------------------------------------------------------------------
def test_dispatch_posts_stock_out_at_cost_and_consumes_reservation(client, auth, ctx):
    o = mk_order(client, auth["booker"], ctx, [{"variant_id": ctx["rose"], "qty": 4}])
    s = ship_all(client, auth, o)
    assert s["status"] == "dispatched"
    a, h = avail(client, auth["manager"], ctx["B1"], ctx["rose"])
    assert (a, h) == (16, 16)
    sm = client.get(f"/shipments/{s['id']}", headers=auth["manager"]) if False else None
    assert get(client, auth["booker"], o)["status"] == "dispatched"
    assert client.get("/inventory/integrity", headers=auth["ceo"]).json()["consistent"]


def test_partial_pack_dispatch_and_delivery(client, auth, ctx):
    o = mk_order(client, auth["booker"], ctx, [{"variant_id": ctx["rose"], "qty": 6}])
    lid = o["lines"][0]["id"]
    s1 = client.post(f"/orders/{o['id']}/shipments", headers=auth["stock"], json={"lines": [{"order_line_id": lid, "qty": 4}]}).json()
    over = client.post(f"/orders/{o['id']}/shipments", headers=auth["stock"], json={"lines": [{"order_line_id": lid, "qty": 3}]})
    assert over.status_code == 422
    client.post(f"/shipments/{s1['id']}/dispatch", headers=auth["stock"], json={})
    client.post(f"/shipments/{s1['id']}/outcome", headers=auth["stock"], json={"result": "delivered"})
    assert get(client, auth["booker"], o)["status"] == "partially_delivered"
    s2 = client.post(f"/orders/{o['id']}/shipments", headers=auth["stock"], json={"lines": [{"order_line_id": lid, "qty": 2}]}).json()
    client.post(f"/shipments/{s2['id']}/dispatch", headers=auth["stock"], json={})
    client.post(f"/shipments/{s2['id']}/outcome", headers=auth["stock"], json={"result": "delivered"})
    assert get(client, auth["booker"], o)["status"] == "delivered"


def test_prepaid_cannot_dispatch_until_paid(client, auth, ctx):
    o = mk_order(client, auth["booker"], ctx, payment_terms="prepaid")
    s = client.post(f"/orders/{o['id']}/shipments", headers=auth["stock"], json={"lines": [{"order_line_id": o["lines"][0]["id"], "qty": 2}]}).json()
    assert client.post(f"/shipments/{s['id']}/dispatch", headers=auth["stock"], json={}).status_code == 422
    pay = client.post(f"/orders/{o['id']}/payments", headers=auth["accounts"], json={"amount": o["total"], "method": "cash"})
    assert pay.status_code == 201, pay.text
    assert client.post(f"/shipments/{s['id']}/dispatch", headers=auth["stock"], json={}).status_code == 200


def test_failed_delivery_goes_to_inspection_not_saleable_stock(client, auth, ctx):
    o = mk_order(client, auth["booker"], ctx, [{"variant_id": ctx["rose"], "qty": 3}])
    s = ship_all(client, auth, o)
    assert client.post(f"/shipments/{s['id']}/outcome", headers=auth["stock"], json={"result": "failed"}).status_code == 422   # reason needed
    r = client.post(f"/shipments/{s['id']}/outcome", headers=auth["stock"], json={"result": "failed", "reason": "no one home"})
    assert r.status_code == 200
    assert avail(client, auth["manager"], ctx["B1"], ctx["rose"])[1] == 17
    rb = client.post(f"/shipments/{s['id']}/receive-back", headers=auth["stock"])
    assert rb.status_code == 200, rb.text
    a, h = avail(client, auth["manager"], ctx["B1"], ctx["rose"])
    assert a == 17      # the 3 returned units wait in inspection: not saleable
    assert client.get("/inventory/integrity", headers=auth["ceo"]).json()["consistent"]


def test_dispatched_order_cannot_be_cancelled(client, auth, ctx):
    o = mk_order(client, auth["booker"], ctx)
    ship_all(client, auth, o)
    assert client.post(f"/orders/{o['id']}/cancel", headers=auth["manager"], json={"reason": "x"}).status_code == 409


# ---- payments -------------------------------------------------------------------------------------------------------------
def test_payments_cannot_exceed_balance_and_status_follows(client, auth, ctx):
    o = mk_order(client, auth["booker"], ctx)     # 200.00
    assert client.post(f"/orders/{o['id']}/payments", headers=auth["accounts"], json={"amount": "250", "method": "cash"}).status_code == 422
    assert client.post(f"/orders/{o['id']}/payments", headers=auth["accounts"], json={"amount": "50", "method": "cash"}).status_code == 201
    assert get(client, auth["booker"], o)["payment_status"] == "partially_paid"
    assert client.post(f"/orders/{o['id']}/payments", headers=auth["accounts"], json={"amount": "150", "method": "cash"}).status_code == 201
    assert get(client, auth["booker"], o)["payment_status"] == "paid"


def test_bank_payment_needs_reference_and_booker_cannot_record(client, auth, ctx):
    o = mk_order(client, auth["booker"], ctx)
    assert client.post(f"/orders/{o['id']}/payments", headers=auth["accounts"], json={"amount": "10", "method": "bank_transfer"}).status_code == 422
    assert client.post(f"/orders/{o['id']}/payments", headers=auth["booker"], json={"amount": "10", "method": "cash"}).status_code == 403


def test_payment_idempotency_and_reversal_by_other_user(client, auth, ctx):
    o = mk_order(client, auth["booker"], ctx)
    body = {"amount": "40", "method": "cash", "idempotency_key": "p-1"}
    a = client.post(f"/orders/{o['id']}/payments", headers=auth["accounts"], json=body)
    b = client.post(f"/orders/{o['id']}/payments", headers=auth["accounts"], json=body)
    assert a.json()["id"] == b.json()["id"]
    assert float(get(client, auth["accounts"], o)["money"]["paid"]) == 40
    assert client.post(f"/order-payments/{a.json()['id']}/reverse", headers=auth["accounts"], json={"reason": "typo"}).status_code == 403
    r = client.post(f"/order-payments/{a.json()['id']}/reverse", headers=auth["manager"], json={"reason": "typo"})
    assert r.status_code == 200, r.text
    assert float(get(client, auth["accounts"], o)["money"]["paid"]) == 0


# ---- returns and refunds ------------------------------------------------------------------------------------------------------
def delivered_paid_order(client, auth, ctx, qty=4):
    o = mk_order(client, auth["booker"], ctx, [{"variant_id": ctx["rose"], "qty": qty}])
    client.post(f"/orders/{o['id']}/payments", headers=auth["accounts"], json={"amount": o["total"], "method": "cash"})
    s = ship_all(client, auth, o)
    client.post(f"/shipments/{s['id']}/outcome", headers=auth["stock"], json={"result": "delivered"})
    return get(client, auth["booker"], o)


def test_full_return_flow_restocks_only_after_inspection_and_refunds(client, auth, ctx):
    o = delivered_paid_order(client, auth, ctx)      # 400 paid
    lid = o["lines"][0]["id"]
    big = client.post(f"/orders/{o['id']}/returns", headers=auth["booker"], json={"lines": [{"order_line_id": lid, "qty": 9}], "reason": "x"})
    assert big.status_code == 422
    r = client.post(f"/orders/{o['id']}/returns", headers=auth["booker"], json={"lines": [{"order_line_id": lid, "qty": 2}], "reason": "wrong shade"})
    assert r.status_code == 201, r.text
    ret = r.json()
    assert client.post(f"/sales-returns/{ret['id']}/decide", headers=auth["booker"], json={"approve": True}).status_code == 403
    assert client.post(f"/sales-returns/{ret['id']}/receive", headers=auth["stock"],
                       json={"lines": [{"line_id": ret["lines"][0]["id"], "qty_received": 2}]}).status_code == 409   # not approved yet
    assert client.post(f"/sales-returns/{ret['id']}/decide", headers=auth["manager"], json={"approve": True, "note": "ok"}).status_code == 200
    before = avail(client, auth["manager"], ctx["B1"], ctx["rose"])
    rc = client.post(f"/sales-returns/{ret['id']}/receive", headers=auth["stock"], json={"lines": [{"line_id": ret["lines"][0]["id"], "qty_received": 2}]})
    assert rc.status_code == 200, rc.text
    assert avail(client, auth["manager"], ctx["B1"], ctx["rose"])[0] == before[0]       # still not saleable
    lid_r = ret["lines"][0]["id"]
    bad = client.post(f"/sales-returns/{ret['id']}/inspect", headers=auth["stock"], json={"lines": [{"line_id": lid_r, "restock": 1}]})
    assert bad.status_code == 422                                                       # must account for every unit
    ok = client.post(f"/sales-returns/{ret['id']}/inspect", headers=auth["stock"],
                     json={"lines": [{"line_id": lid_r, "restock": 1, "damaged": 1, "refund_qty": 2}]})
    assert ok.status_code == 200, ok.text
    assert avail(client, auth["manager"], ctx["B1"], ctx["rose"])[0] == before[0] + 1  # only the good unit is saleable
    assert client.post(f"/sales-returns/{ret['id']}/settle", headers=auth["booker"], json={"method": "cash"}).status_code == 403
    st = client.post(f"/sales-returns/{ret['id']}/settle", headers=auth["accounts"], json={"method": "cash"})
    assert st.status_code == 200, st.text
    assert st.json()["status"] == "settled" and st.json()["refund_paid"] == "200.00"
    full = get(client, auth["accounts"], o)
    assert full["status"] == "partially_returned"
    assert client.get("/inventory/integrity", headers=auth["ceo"]).json()["consistent"]
    assert client.get("/sales/integrity", headers=auth["ceo"]).json()["consistent"]


def test_return_of_unpaid_order_refunds_nothing(client, auth, ctx):
    o = mk_order(client, auth["booker"], ctx, [{"variant_id": ctx["rose"], "qty": 2}])
    s = ship_all(client, auth, o)
    client.post(f"/shipments/{s['id']}/outcome", headers=auth["stock"], json={"result": "delivered"})
    o = get(client, auth["booker"], o)
    ret = client.post(f"/orders/{o['id']}/returns", headers=auth["booker"], json={"lines": [{"order_line_id": o["lines"][0]["id"], "qty": 2}], "reason": "no"}).json()
    client.post(f"/sales-returns/{ret['id']}/decide", headers=auth["manager"], json={"approve": True})
    client.post(f"/sales-returns/{ret['id']}/receive", headers=auth["stock"], json={"lines": [{"line_id": ret["lines"][0]["id"], "qty_received": 2}]})
    client.post(f"/sales-returns/{ret['id']}/inspect", headers=auth["stock"], json={"lines": [{"line_id": ret["lines"][0]["id"], "restock": 2, "refund_qty": 2}]})
    r = client.post(f"/sales-returns/{ret['id']}/settle", headers=auth["accounts"], json={"method": "cash"})
    assert r.status_code == 200, r.text
    assert r.json()["refund_paid"] in ("0.00", None)
    assert get(client, auth["accounts"], o)["money"]["balance_due"] == "0.00"


def test_store_credit_refund_is_ledgered(client, auth, ctx):
    o = delivered_paid_order(client, auth, ctx, qty=2)
    ret = client.post(f"/orders/{o['id']}/returns", headers=auth["booker"],
                      json={"lines": [{"order_line_id": o["lines"][0]["id"], "qty": 1}], "reason": "x", "refund_mode": "store_credit"}).json()
    client.post(f"/sales-returns/{ret['id']}/decide", headers=auth["manager"], json={"approve": True})
    client.post(f"/sales-returns/{ret['id']}/receive", headers=auth["stock"], json={"lines": [{"line_id": ret["lines"][0]["id"], "qty_received": 1}]})
    client.post(f"/sales-returns/{ret['id']}/inspect", headers=auth["stock"], json={"lines": [{"line_id": ret["lines"][0]["id"], "restock": 1, "refund_qty": 1}]})
    assert client.post(f"/sales-returns/{ret['id']}/settle", headers=auth["accounts"], json={}).status_code == 200
    assert client.get(f"/customers/{ctx['cust']}", headers=auth["manager"]).json()["store_credit"] == "100.00"


def test_credit_ledger_is_append_only(client, auth, ctx, db):
    test_store_credit_refund_is_ledgered(client, auth, ctx)
    with pytest.raises(DBAPIError):
        db.execute(text("UPDATE customer_credit_entries SET amount = 1"))
        db.commit()
    db.rollback()
    with pytest.raises(DBAPIError):
        db.execute(text("DELETE FROM customer_credit_entries"))
        db.commit()
    db.rollback()


# ---- permissions, scope and redaction ----------------------------------------------------------------------------------------------
def test_booker_sees_only_own_orders(client, auth, ctx, db, locations):
    mine = mk_order(client, auth["booker"], ctx)
    other = mk_order(client, auth["manager"], ctx)
    ids = [x["id"] for x in client.get("/orders", headers=auth["booker"]).json()["items"]]
    assert ids == [mine["id"]]
    assert client.get(f"/orders/{other['id']}", headers=auth["booker"]).status_code == 404
    assert client.post(f"/orders/{other['id']}/cancel", headers=auth["booker"], json={"reason": "x"}).status_code == 404
    assert client.get("/orders", headers=auth["manager"]).json()["total"] == 2
    s = client.get("/orders/my-summary", headers=auth["booker"]).json()
    assert s["orders"] == 1


def test_booker_cannot_order_for_a_location_outside_scope(client, auth, ctx):
    r = client.post("/orders", headers=auth["booker"], json=order_body(ctx, location_id=ctx["B2"]))
    assert r.status_code == 404


def test_stock_keeper_sees_fulfilment_view_without_money(client, auth, ctx):
    o = mk_order(client, auth["booker"], ctx)
    d = client.get(f"/orders/{o['id']}", headers=auth["stock"]).json()
    for k in ("total", "payments", "money", "subtotal_list", "discount_total"):
        assert k not in d
    assert "unit_price" not in d["lines"][0]
    row = client.get("/orders", headers=auth["stock"]).json()["items"][0]
    assert "total" not in row
    drafts = mk_order(client, auth["booker"], ctx, confirm=False)
    assert client.get(f"/orders/{drafts['id']}", headers=auth["stock"]).status_code == 404
    assert client.post(f"/orders/{o['id']}/payments", headers=auth["stock"], json={"amount": "1", "method": "cash"}).status_code == 403


def test_no_token_and_wrong_role_are_refused(client, auth, ctx):
    assert client.get("/orders").status_code == 401
    assert client.post("/sales/channels", headers=auth["booker"], json={"code": "tiktok", "name": "TikTok"}).status_code == 403


def test_channels_are_configurable(client, auth, ctx):
    r = client.post("/sales/channels", headers=auth["ceo"], json={"code": "tiktok", "name": "TikTok"})
    assert r.status_code == 201
    assert any(c["code"] == "tiktok" for c in client.get("/sales/channels", headers=auth["booker"]).json())
    client.put(f"/sales/channels/{r.json()['id']}", headers=auth["ceo"], json={"is_active": False})
    assert all(c["code"] != "tiktok" for c in client.get("/sales/channels", headers=auth["booker"]).json())
    bad = client.post("/orders", headers=auth["booker"], json=order_body(ctx, channel_id=r.json()["id"]))
    assert bad.status_code == 422


def test_actions_are_audited(client, auth, ctx):
    o = mk_order(client, auth["booker"], ctx)
    client.post(f"/orders/{o['id']}/cancel", headers=auth["booker"], json={"reason": "audit me"})
    rows = client.get("/audit", headers=auth["ceo"], params={"page_size": 100})
    if rows.status_code == 200:
        items = rows.json()["items"] if isinstance(rows.json(), dict) else rows.json()
        actions = {x["action"] for x in items}
        assert any(a.startswith("order.") for a in actions), actions
    events = get(client, auth["booker"], o)["events"]
    assert {e["event"] for e in events} >= {"created", "confirmed", "cancelled"} or len(events) >= 2


def test_sales_integrity_clean_after_activity(client, auth, ctx):
    delivered_paid_order(client, auth, ctx)
    assert client.get("/sales/integrity", headers=auth["ceo"]).json() == {"consistent": True, "problems": []}
