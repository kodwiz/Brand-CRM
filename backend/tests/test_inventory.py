from decimal import Decimal

from backend.tests.conftest import line, login, make_user, receive


def stock(client, h, loc, variant=None):
    items = client.get(f"/inventory/stock?location_id={loc}&include_zero=true", headers=h).json()["items"]
    if variant is None:
        return items
    return next((i for i in items if i["variant_id"] == variant), None)


def integrity(client, h):
    r = client.get("/inventory/integrity", headers=h).json()
    assert r["consistent"], r["problems"]


# ---- weighted average cost -------------------------------------------------------------------------
def test_weighted_average_on_receipts(client, auth, catalog, locations):
    wh = locations["WH"].id
    h = auth["ceo"]
    assert receive(client, h, wh, [line(catalog["rose"], 10, "10.00")]).status_code == 201
    assert receive(client, h, wh, [line(catalog["rose"], 30, "20.00")]).status_code == 201
    s = stock(client, h, wh, catalog["rose"])
    assert s["on_hand_good"] == 40 and Decimal(s["avg_cost"]) == Decimal("17.5") and Decimal(s["value"]) == Decimal("700")
    # an adjustment loss is valued at the current average, leaving the average unchanged
    r = client.post("/inventory/requests", headers=h, json={"kind": "adjustment", "location_id": wh, "reason": "test",
                                                            "lines": [{"variant_id": catalog["rose"], "qty_delta": -4}]})
    assert r.status_code == 201
    m = auth["manager"]
    assert client.post(f"/inventory/requests/{r.json()['id']}/approve", headers=m, json={}).status_code == 200
    s = stock(client, h, wh, catalog["rose"])
    assert s["on_hand_good"] == 36 and Decimal(s["avg_cost"]) == Decimal("17.5") and Decimal(s["value"]) == Decimal("630")
    integrity(client, h)


def test_historical_costs_preserved_on_every_movement(client, auth, catalog, locations):
    wh = locations["WH"].id
    h = auth["ceo"]
    receive(client, h, wh, [line(catalog["rose"], 10, "10.00")])
    receive(client, h, wh, [line(catalog["rose"], 10, "30.00")])
    mv = client.get(f"/inventory/movements?location_id={wh}&variant_id={catalog['rose']}", headers=h).json()["items"]
    assert sorted(Decimal(m["unit_cost"]) for m in mv) == [Decimal("10"), Decimal("30")]
    assert all(m["value_delta"] is not None for m in mv)


def test_no_negative_stock(client, auth, catalog, locations):
    wh = locations["WH"].id
    h, m = auth["ceo"], auth["manager"]
    receive(client, h, wh, [line(catalog["rose"], 5, "10")])
    r = client.post("/inventory/requests", headers=h, json={"kind": "adjustment", "location_id": wh, "reason": "x",
                                                            "lines": [{"variant_id": catalog["rose"], "qty_delta": -6}]}).json()
    resp = client.post(f"/inventory/requests/{r['id']}/approve", headers=m, json={})
    assert resp.status_code == 422
    assert stock(client, h, wh, catalog["rose"])["on_hand_good"] == 5
    integrity(client, h)


def test_gain_needs_known_cost_and_uses_average(client, auth, catalog, locations):
    wh = locations["WH"].id
    h, m = auth["ceo"], auth["manager"]
    r = client.post("/inventory/requests", headers=h, json={"kind": "adjustment", "location_id": wh, "reason": "found",
                                                            "lines": [{"variant_id": catalog["nude"], "qty_delta": 3}]}).json()
    assert client.post(f"/inventory/requests/{r['id']}/approve", headers=m, json={}).status_code == 422  # no cost known
    receive(client, h, wh, [line(catalog["nude"], 10, "8.00")])
    assert client.post(f"/inventory/requests/{r['id']}/approve", headers=m, json={}).status_code == 200
    s = stock(client, h, wh, catalog["nude"])
    assert s["on_hand_good"] == 13 and Decimal(s["avg_cost"]) == Decimal("8")


# ---- receiving --------------------------------------------------------------------------------------
def test_receiving_only_accepted_enters_usable_stock(client, auth, catalog, locations):
    wh = locations["WH"].id
    h = auth["stock"]
    r = receive(client, h, wh, [line(catalog["rose"], 8, "10", qty_damaged=2, qty_rejected=1, qty_expected=12)])
    assert r.status_code == 201, r.text
    assert r.json()["totals"] == {"received": 11, "accepted": 8, "damaged": 2, "rejected": 1}
    s = stock(client, auth["ceo"], wh, catalog["rose"])
    assert (s["on_hand_good"], s["available"], s["damaged"]) == (8, 8, 2)
    # rejected units never reach the books; damaged ones are owned (valued) but not saleable
    assert Decimal(s["value"]) == Decimal("100")
    integrity(client, auth["ceo"])


def test_receipt_validation(client, auth, catalog, locations):
    wh = locations["WH"].id
    h = auth["stock"]
    bad = line(catalog["rose"], 5, "10"); bad["qty_received"] = 9
    assert receive(client, h, wh, [bad]).status_code == 422                         # totals must reconcile
    assert receive(client, h, wh, [line(catalog["serum"], 5, "10")]).status_code == 422  # batch required
    assert receive(client, h, wh, [line(catalog["serum"], 5, "10", batch_no="B1")]).status_code == 422  # expiry required
    assert receive(client, h, wh, [line(catalog["serum"], 5, "10", batch_no="B1", expiry_date="2020-01-01")]).status_code == 422
    assert receive(client, h, wh, [line(catalog["rose"], 5, "10", batch_no="B1")]).status_code == 422  # not batch tracked
    assert receive(client, h, wh, [line(catalog["rose"], 5, "10")], date_="2999-01-01").status_code == 422
    ok = receive(client, h, wh, [line(catalog["serum"], 5, "10", batch_no="B1", expiry_date="2999-01-01")])
    assert ok.status_code == 201
    # same batch with a different expiry is refused
    assert receive(client, h, wh, [line(catalog["serum"], 5, "10", batch_no="B1", expiry_date="2998-01-01")]).status_code == 422


def test_receipt_cannot_be_posted_twice(client, auth, catalog, locations):
    wh = locations["WH"].id
    h = auth["stock"]
    r = receive(client, h, wh, [line(catalog["rose"], 5, "10")], post=False).json()
    assert client.post(f"/inventory/receipts/{r['id']}/post", headers=h).status_code == 200
    assert client.post(f"/inventory/receipts/{r['id']}/post", headers=h).status_code == 409
    assert stock(client, h, wh, catalog["rose"])["on_hand_good"] == 5


def test_draft_receipt_adds_no_stock_and_can_be_cancelled(client, auth, catalog, locations):
    wh = locations["WH"].id
    h = auth["stock"]
    r = receive(client, h, wh, [line(catalog["rose"], 5, "10")], post=False).json()
    assert stock(client, h, wh, catalog["rose"]) is None
    assert client.post(f"/inventory/receipts/{r['id']}/cancel", headers=h, json={"reason": "wrong"}).status_code == 200
    assert client.post(f"/inventory/receipts/{r['id']}/post", headers=h).status_code == 409


# ---- location permissions ---------------------------------------------------------------------------
def test_location_permissions(client, auth, catalog, locations):
    b2 = locations["B2"].id
    h = auth["stock"]   # assigned to WH and B1 only
    assert receive(client, h, b2, [line(catalog["rose"], 5, "10")]).status_code == 404
    assert client.get(f"/inventory/stock?location_id={b2}", headers=h).status_code == 404
    receive(client, auth["ceo"], b2, [line(catalog["rose"], 5, "10")])
    rows = client.get("/inventory/stock", headers=h).json()["items"]
    assert all(r["location_id"] != b2 for r in rows)
    assert client.get("/inventory/valuation", headers=h).status_code == 403  # no valuation permission
    # costs are redacted for those without the valuation permission
    receive(client, h, locations["WH"].id, [line(catalog["rose"], 5, "10")])
    row = client.get("/inventory/stock", headers=h).json()["items"][0]
    assert "avg_cost" not in row and "value" not in row
    mv = client.get("/inventory/movements", headers=h).json()["items"][0]
    assert "unit_cost" not in mv


# ---- opening stock ------------------------------------------------------------------------------------
def test_opening_stock_dry_run_duplicates_and_cost_required(client, auth, catalog, locations):
    wh = locations["WH"].id
    h = auth["ceo"]
    body = {"lines": [{"location_id": wh, "variant_id": catalog["rose"], "qty": 12, "unit_cost": "9.50"}], "dry_run": True}
    r = client.post("/inventory/opening-stock", headers=h, json=body).json()
    assert r["ok"] and stock(client, h, wh, catalog["rose"]) is None       # dry run writes nothing
    body["dry_run"] = False
    assert client.post("/inventory/opening-stock", headers=h, json=body).json()["ok"]
    assert stock(client, h, wh, catalog["rose"])["on_hand_good"] == 12
    again = client.post("/inventory/opening-stock", headers=h, json=body).json()
    assert not again["ok"] and "already" in again["errors"][0]["message"]
    assert stock(client, h, wh, catalog["rose"])["on_hand_good"] == 12
    # one bad line blocks the whole batch
    mix = {"dry_run": False, "lines": [{"location_id": wh, "variant_id": catalog["nude"], "qty": 5, "unit_cost": "1"},
                                       {"location_id": wh, "variant_id": catalog["serum"], "qty": 5, "unit_cost": "1"}]}
    res = client.post("/inventory/opening-stock", headers=h, json=mix).json()
    assert not res["ok"] and stock(client, h, wh, catalog["nude"]) is None
    assert client.post("/inventory/opening-stock", headers=auth["stock"], json=body).status_code == 403


# ---- damaged / expired ----------------------------------------------------------------------------------
def test_damage_and_expiry_separate_from_saleable(client, auth, catalog, locations):
    wh = locations["WH"].id
    h = auth["stock"]
    receive(client, h, wh, [line(catalog["rose"], 10, "10")])
    r = client.post("/inventory/moves", headers=h, json={"action": "damage", "location_id": wh, "variant_id": catalog["rose"],
                                                         "qty": 3, "reason": "dropped"})
    assert r.status_code == 201
    s = stock(client, h, wh, catalog["rose"])
    assert (s["on_hand_good"], s["available"], s["damaged"]) == (7, 7, 3)
    assert Decimal(stock(client, auth["ceo"], wh, catalog["rose"])["value"]) == Decimal("100")  # owned until written off
    assert client.post("/inventory/moves", headers=h, json={"action": "damage", "location_id": wh,
                       "variant_id": catalog["rose"], "qty": 99, "reason": "x"}).status_code == 422
    assert client.post("/inventory/moves", headers=h, json={"action": "damage", "location_id": wh,
                       "variant_id": catalog["rose"], "qty": 1, "reason": " "}).status_code in (422,)
    # write-off removes it from the books only with approval from a different user
    w = client.post("/inventory/requests", headers=h, json={"kind": "writeoff", "location_id": wh, "reason": "destroy",
                    "lines": [{"variant_id": catalog["rose"], "bucket": "damaged", "qty_delta": -3}]}).json()
    assert client.post(f"/inventory/requests/{w['id']}/approve", headers=h, json={}).status_code == 403  # no permission
    assert client.post(f"/inventory/requests/{w['id']}/approve", headers=auth["manager"], json={}).status_code == 200
    s = stock(client, auth["ceo"], wh, catalog["rose"])
    assert s["damaged"] == 0 and Decimal(s["value"]) == Decimal("70")
    integrity(client, auth['ceo'])


def test_expired_batch_is_not_available(client, auth, catalog, locations, db):
    from backend.models import Batch
    wh = locations["WH"].id
    h = auth["stock"]
    receive(client, h, wh, [line(catalog["serum"], 10, "50", batch_no="E1", expiry_date="2999-01-01")])
    b = db.query(Batch).filter_by(batch_no="E1").one()
    b.expiry_date = __import__("datetime").date(2020, 1, 1); db.commit()   # simulate the date passing
    s = stock(client, h, wh, catalog["serum"])
    assert s["available"] == 0 and s["expired"] == 10
    alerts = client.get("/inventory/alerts", headers=h).json()
    assert alerts["expiring"] and alerts["expiring"][0]["already_expired"]
    # staff can move it to the expired bucket explicitly
    r = client.post("/inventory/moves", headers=h, json={"action": "expiry", "location_id": wh, "variant_id": catalog["serum"],
                                                         "batch_id": b.id, "qty": 10, "reason": "expired"})
    assert r.status_code == 201


def test_low_stock_alert(client, auth, catalog, locations):
    wh = locations["WH"].id
    receive(client, auth["stock"], wh, [line(catalog["rose"], 4, "10")])   # threshold is 5
    a = client.get("/inventory/alerts", headers=auth["stock"]).json()
    assert [r["sku"] for r in a["low_stock"]] == ["TST-LIP-R"]
    receive(client, auth["stock"], wh, [line(catalog["rose"], 20, "10")])
    assert client.get("/inventory/alerts", headers=auth["stock"]).json()["low_stock"] == []


# ---- approvals, reversal, duplicates ----------------------------------------------------------------------
def test_self_approval_blocked(client, auth, catalog, locations):
    wh = locations["WH"].id
    h = auth["ceo"]
    receive(client, h, wh, [line(catalog["rose"], 10, "10")])
    r = client.post("/inventory/requests", headers=h, json={"kind": "adjustment", "location_id": wh, "reason": "x",
                    "lines": [{"variant_id": catalog["rose"], "qty_delta": -1}]}).json()
    resp = client.post(f"/inventory/requests/{r['id']}/approve", headers=h, json={})
    assert resp.status_code == 403 and "yourself" in resp.json()["detail"]
    assert stock(client, h, wh, catalog["rose"])["on_hand_good"] == 10
    assert client.post(f"/inventory/requests/{r['id']}/approve", headers=auth["manager"], json={}).status_code == 200
    assert client.post(f"/inventory/requests/{r['id']}/approve", headers=auth["manager"], json={}).status_code == 409


def test_reversal_not_edit_and_self_reversal_blocked(client, auth, catalog, locations):
    wh = locations["WH"].id
    r = receive(client, auth["stock"], wh, [line(catalog["rose"], 10, "10")]).json()
    t = client.get(f"/inventory/movements?location_id={wh}", headers=auth["ceo"]).json()["items"][0]["txn_id"]
    assert client.post(f"/inventory/transactions/{t}/reverse", headers=auth["stock"], json={"reason": "x"}).status_code == 403
    rev = client.post(f"/inventory/transactions/{t}/reverse", headers=auth["manager"], json={"reason": "entered twice"})
    assert rev.status_code == 200 and rev.json()["txn_type"] == "reversal"
    assert stock(client, auth["ceo"], wh, catalog["rose"])["on_hand_good"] == 0
    assert client.post(f"/inventory/transactions/{t}/reverse", headers=auth["manager"], json={"reason": "again"}).status_code == 409
    mv = client.get(f"/inventory/movements?location_id={wh}", headers=auth["ceo"]).json()["items"]
    assert len(mv) == 2    # original line still present + the reversal
    assert client.get(f"/inventory/receipts/{r['id']}", headers=auth["ceo"]).json()["status"] == "reversed"
    integrity(client, auth["ceo"])
    # a reversal that would make stock negative is refused
    receive(client, auth["stock"], wh, [line(catalog["nude"], 5, "10")])
    t2 = client.get(f"/inventory/movements?location_id={wh}&variant_id={catalog['nude']}", headers=auth["ceo"]).json()["items"][0]["txn_id"]
    q = client.post("/inventory/requests", headers=auth["stock"], json={"kind": "adjustment", "location_id": wh, "reason": "x",
        "lines": [{"variant_id": catalog["nude"], "qty_delta": -4}]}).json()
    client.post(f"/inventory/requests/{q['id']}/approve", headers=auth["manager"], json={})
    assert client.post(f"/inventory/transactions/{t2}/reverse", headers=auth["manager"], json={"reason": "x"}).status_code == 422
    integrity(client, auth["ceo"])


def test_idempotent_duplicate_submission(client, auth, catalog, locations):
    wh = locations["WH"].id
    h = auth["stock"]
    receive(client, h, wh, [line(catalog["rose"], 10, "10")])
    body = {"action": "damage", "location_id": wh, "variant_id": catalog["rose"], "qty": 2, "reason": "x",
            "idempotency_key": "abc-123"}
    a = client.post("/inventory/moves", headers=h, json=body)
    b = client.post("/inventory/moves", headers=h, json=body)
    assert a.status_code == b.status_code == 201 and a.json()["ref"] == b.json()["ref"]
    assert stock(client, h, wh, catalog["rose"])["damaged"] == 2


def test_audit_trail_for_stock_actions(client, auth, catalog, locations):
    wh = locations["WH"].id
    receive(client, auth["stock"], wh, [line(catalog["rose"], 10, "10")])
    r = client.post("/inventory/requests", headers=auth["stock"], json={"kind": "adjustment", "location_id": wh, "reason": "why",
        "lines": [{"variant_id": catalog["rose"], "qty_delta": -1}]}).json()
    client.post(f"/inventory/requests/{r['id']}/approve", headers=auth["manager"], json={"note": "ok"})
    actions = {a["action"] for a in client.get("/audit?page_size=200", headers=auth["ceo"]).json()["items"]}
    assert {"receipt.created", "receipt.posted", "stock_request.created", "stock_request.approved"} <= actions


def test_stock_ledger_is_append_only(db, client, auth, catalog, locations):
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    receive(client, auth["stock"], locations["WH"].id, [line(catalog["rose"], 3, "10")])
    for stmt in ("UPDATE stock_movements SET qty_delta = 99", "DELETE FROM stock_movements",
                 "UPDATE stock_transactions SET reason = 'tamper'", "DELETE FROM stock_transactions"):
        with pytest.raises(DBAPIError):
            db.execute(text(stmt))
        db.rollback()
