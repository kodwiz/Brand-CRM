import threading
from decimal import Decimal

from backend.tests.conftest import line, receive
from backend.tests.test_inventory import integrity, stock


def make_transfer(client, h, locations, items):
    return client.post("/transfers", headers=h, json={
        "from_location_id": locations["WH"].id, "to_location_id": locations["B1"].id,
        "lines": [{"variant_id": v, "qty": q} for v, q in items]})


def run_to_dispatch(client, auth, locations, catalog, qty=10):
    t = make_transfer(client, auth["stock"], locations, [(catalog["rose"], qty)])
    assert t.status_code == 201, t.text
    tid = t.json()["id"]
    assert client.post(f"/transfers/{tid}/approve", headers=auth["manager"], json={}).status_code == 200
    assert client.post(f"/transfers/{tid}/dispatch", headers=auth["stock"]).status_code == 200
    return tid


def test_full_transfer_lifecycle_and_cost(client, auth, catalog, locations):
    wh, b1 = locations["WH"].id, locations["B1"].id
    ceo = auth["ceo"]
    receive(client, ceo, wh, [line(catalog["rose"], 20, "10.00")])
    receive(client, ceo, b1, [line(catalog["rose"], 10, "22.00")])      # destination already has stock at another cost
    t = make_transfer(client, auth["stock"], locations, [(catalog["rose"], 10)]).json()
    assert t["status"] == "requested"
    # approval reserves at source: available drops, on-hand does not
    client.post(f"/transfers/{t['id']}/approve", headers=auth["manager"], json={})
    s = stock(client, ceo, wh, catalog["rose"])
    assert (s["on_hand_good"], s["reserved"], s["available"]) == (20, 10, 10)
    client.post(f"/transfers/{t['id']}/dispatch", headers=auth["stock"])
    s, d = stock(client, ceo, wh, catalog["rose"]), stock(client, ceo, b1, catalog["rose"])
    assert (s["on_hand_good"], s["reserved"]) == (10, 0)
    assert (d["on_hand_good"], d["in_transit"]) == (10, 10)
    assert Decimal(d["avg_cost"]) == Decimal("22")                       # in-transit stock is not in the average yet
    val = client.get("/inventory/valuation", headers=ceo).json()
    assert Decimal(val["total_in_transit_value"]) == Decimal("100")
    rec = client.post(f"/transfers/{t['id']}/receive", headers=auth["stock"], json={"lines": [
        {"line_id": client.get(f"/transfers/{t['id']}", headers=ceo).json()["lines"][0]["id"], "qty_good": 10}]})
    assert rec.status_code == 200 and rec.json()["status"] == "received"
    d = stock(client, ceo, b1, catalog["rose"])
    assert (d["on_hand_good"], d["in_transit"]) == (20, 0)
    assert Decimal(d["avg_cost"]) == Decimal("16")                       # (10*22 + 10*10)/20
    assert Decimal(client.get("/inventory/valuation", headers=ceo).json()["total_value"]) == Decimal("420")  # nothing lost
    events = [e["event"] for e in client.get(f"/transfers/{t['id']}", headers=ceo).json()["events"]]
    assert events == ["requested", "approved", "dispatched", "received"]
    integrity(client, ceo)


def test_partial_receipt_then_complete_and_no_overreceive(client, auth, catalog, locations):
    wh, b1 = locations["WH"].id, locations["B1"].id
    receive(client, auth["ceo"], wh, [line(catalog["rose"], 10, "10")])
    tid = run_to_dispatch(client, auth, locations, catalog)
    lid = client.get(f"/transfers/{tid}", headers=auth["ceo"]).json()["lines"][0]["id"]
    r = client.post(f"/transfers/{tid}/receive", headers=auth["stock"], json={"lines": [{"line_id": lid, "qty_good": 4}]})
    assert r.json()["status"] == "partially_received"
    assert client.post(f"/transfers/{tid}/receive", headers=auth["stock"], json={"lines": [{"line_id": lid, "qty_good": 7}]}).status_code == 422
    r = client.post(f"/transfers/{tid}/receive", headers=auth["stock"],
                    json={"lines": [{"line_id": lid, "qty_good": 5, "qty_damaged": 1}]})
    assert r.json()["status"] == "received"
    d = stock(client, auth["ceo"], b1, catalog["rose"])
    assert (d["on_hand_good"], d["damaged"], d["in_transit"]) == (9, 1, 0)
    # no duplicate receipt or dispatch
    assert client.post(f"/transfers/{tid}/receive", headers=auth["stock"], json={"lines": [{"line_id": lid, "qty_good": 1}]}).status_code == 409
    assert client.post(f"/transfers/{tid}/dispatch", headers=auth["stock"]).status_code == 409
    integrity(client, auth["ceo"])


def test_shortage_needs_independent_approval(client, auth, catalog, locations, db):
    wh, b1 = locations["WH"].id, locations["B1"].id
    receive(client, auth["ceo"], wh, [line(catalog["rose"], 10, "10")])
    tid = run_to_dispatch(client, auth, locations, catalog)
    lid = client.get(f"/transfers/{tid}", headers=auth["ceo"]).json()["lines"][0]["id"]
    client.post(f"/transfers/{tid}/receive", headers=auth["stock"], json={"lines": [{"line_id": lid, "qty_good": 7}]})
    assert client.post(f"/transfers/{tid}/flag-discrepancy", headers=auth["stock"], json={"reason": "3 missing"}).status_code == 200
    # the dispatcher / requester cannot approve their own shortage; stock keepers lack the permission anyway
    assert client.post(f"/transfers/{tid}/resolve-discrepancy", headers=auth["stock"], json={"note": "ok"}).status_code == 403
    r = client.post(f"/transfers/{tid}/resolve-discrepancy", headers=auth["manager"], json={"note": "carrier lost 3"})
    assert r.status_code == 200 and r.json()["status"] == "received"
    assert r.json()["lines"][0]["qty_short"] == 3
    assert stock(client, auth["ceo"], b1, catalog["rose"])["in_transit"] == 0
    integrity(client, auth["ceo"])
    # total value fell by the lost goods (3 * 10)
    assert Decimal(client.get("/inventory/valuation", headers=auth["ceo"]).json()["total_value"]) == Decimal("70")


def test_requester_cannot_approve_own_transfer(client, auth, catalog, locations):
    receive(client, auth["ceo"], locations["WH"].id, [line(catalog["rose"], 10, "10")])
    t = make_transfer(client, auth["ceo"], locations, [(catalog["rose"], 2)]).json()
    assert client.post(f"/transfers/{t['id']}/approve", headers=auth["ceo"], json={}).status_code == 403
    assert client.post(f"/transfers/{t['id']}/approve", headers=auth["manager"], json={}).status_code == 200


def test_transfer_cannot_be_approved_without_stock_and_cancel_releases(client, auth, catalog, locations):
    wh = locations["WH"].id
    receive(client, auth["ceo"], wh, [line(catalog["rose"], 5, "10")])
    t = make_transfer(client, auth["stock"], locations, [(catalog["rose"], 6)]).json()
    assert client.post(f"/transfers/{t['id']}/approve", headers=auth["manager"], json={}).status_code == 422
    assert client.get(f"/transfers/{t['id']}", headers=auth["ceo"]).json()["status"] == "requested"
    t2 = make_transfer(client, auth["stock"], locations, [(catalog["rose"], 5)]).json()
    client.post(f"/transfers/{t2['id']}/approve", headers=auth["manager"], json={})
    assert stock(client, auth["ceo"], wh, catalog["rose"])["available"] == 0
    assert client.post(f"/transfers/{t2['id']}/cancel", headers=auth["stock"], json={"reason": "changed mind"}).status_code == 200
    assert stock(client, auth["ceo"], wh, catalog["rose"])["available"] == 5
    # reserved stock cannot be taken by another transfer
    t3 = make_transfer(client, auth["stock"], locations, [(catalog["rose"], 5)]).json()
    t4 = make_transfer(client, auth["stock"], locations, [(catalog["rose"], 1)]).json()
    assert client.post(f"/transfers/{t3['id']}/approve", headers=auth["manager"], json={}).status_code == 200
    assert client.post(f"/transfers/{t4['id']}/approve", headers=auth["manager"], json={}).status_code == 422


def test_transfer_location_rules(client, auth, catalog, locations):
    b2 = locations["B2"].id
    r = client.post("/transfers", headers=auth["stock"], json={"from_location_id": b2, "to_location_id": b2 + 0,
                                                              "lines": [{"variant_id": catalog["rose"], "qty": 1}]})
    assert r.status_code == 404                                   # stock keeper is not assigned to B2
    same = client.post("/transfers", headers=auth["stock"], json={"from_location_id": locations["WH"].id,
          "to_location_id": locations["WH"].id, "lines": [{"variant_id": catalog["rose"], "qty": 1}]})
    assert same.status_code == 422
    receive(client, auth["ceo"], locations["WH"].id, [line(catalog["rose"], 5, "10")])
    t = make_transfer(client, auth["stock"], locations, [(catalog["rose"], 1)]).json()
    # booker has transfers.view? no -- and cannot act
    assert client.post(f"/transfers/{t['id']}/approve", headers=auth["booker"], json={}).status_code == 403


def test_batch_transfer_fefo_split(client, auth, catalog, locations):
    wh, b1 = locations["WH"].id, locations["B1"].id
    ceo = auth["ceo"]
    receive(client, ceo, wh, [line(catalog["serum"], 3, "10", batch_no="LATE", expiry_date="2999-06-01")])
    receive(client, ceo, wh, [line(catalog["serum"], 3, "10", batch_no="EARLY", expiry_date="2999-01-01")])
    t = client.post("/transfers", headers=auth["stock"], json={"from_location_id": wh, "to_location_id": b1,
        "lines": [{"variant_id": catalog["serum"], "qty": 4}]}).json()
    client.post(f"/transfers/{t['id']}/approve", headers=auth["manager"], json={})
    d = client.post(f"/transfers/{t['id']}/dispatch", headers=auth["stock"]).json()
    got = {(l["batch_no"], l["qty_dispatched"]) for l in d["lines"]}
    assert got == {("EARLY", 3), ("LATE", 1)}                     # earliest expiry first
    rec = client.post(f"/transfers/{t['id']}/receive", headers=auth["stock"],
                      json={"lines": [{"line_id": l["id"], "qty_good": l["qty_dispatched"]} for l in d["lines"]]})
    assert rec.json()["status"] == "received"
    integrity(client, ceo)


# ---- counts ----------------------------------------------------------------------------------------------
def test_count_variance_flow_with_approval(client, auth, catalog, locations):
    wh = locations["WH"].id
    receive(client, auth["ceo"], wh, [line(catalog["rose"], 10, "10"), line(catalog["nude"], 6, "5")])
    c = client.post("/inventory/counts", headers=auth["stock"], json={"location_id": wh}).json()
    assert len(c["lines"]) == 2
    assert client.post("/inventory/counts", headers=auth["stock"], json={"location_id": wh}).status_code == 409
    sub = client.post(f"/inventory/counts/{c['id']}/submit", headers=auth["stock"])
    assert sub.status_code == 422                                 # not everything counted
    entries = [{"line_id": l["id"], "counted_qty": l["expected_qty"]} for l in c["lines"]]
    entries[0]["counted_qty"] -= 2                                 # two missing, no reason yet
    client.put(f"/inventory/counts/{c['id']}/entries", headers=auth["stock"], json={"entries": entries})
    assert client.post(f"/inventory/counts/{c['id']}/submit", headers=auth["stock"]).status_code == 422
    entries[0]["reason"] = "shrinkage"
    client.put(f"/inventory/counts/{c['id']}/entries", headers=auth["stock"], json={"entries": entries})
    s = client.post(f"/inventory/counts/{c['id']}/submit", headers=auth["stock"])
    assert s.status_code == 200 and s.json()["request_ref"]
    assert stock(client, auth["ceo"], wh, catalog["rose"])["on_hand_good"] + stock(client, auth["ceo"], wh, catalog["nude"])["on_hand_good"] == 16
    reqs = client.get("/inventory/requests?status=pending", headers=auth["manager"]).json()["items"]
    rq = next(r for r in reqs if r["kind"] == "count_variance")
    assert client.post(f"/inventory/requests/{rq['id']}/approve", headers=auth["stock"], json={}).status_code == 403
    assert client.post(f"/inventory/requests/{rq['id']}/approve", headers=auth["manager"], json={}).status_code == 200
    assert sum(stock(client, auth["ceo"], wh, v)["on_hand_good"] for v in (catalog["rose"], catalog["nude"])) == 14
    integrity(client, auth["ceo"])


def test_count_accounts_for_movements_after_snapshot(client, auth, catalog, locations):
    wh = locations["WH"].id
    receive(client, auth["ceo"], wh, [line(catalog["rose"], 10, "10")])
    c = client.post("/inventory/counts", headers=auth["stock"], json={"location_id": wh}).json()
    receive(client, auth["ceo"], wh, [line(catalog["rose"], 5, "10")])       # arrives while counting
    lid = c["lines"][0]["id"]
    client.put(f"/inventory/counts/{c['id']}/entries", headers=auth["stock"], json={"entries": [{"line_id": lid, "counted_qty": 10}]})
    got = client.get(f"/inventory/counts/{c['id']}", headers=auth["stock"]).json()["lines"][0]
    assert got["moved_since_snapshot"] == 5 and got["variance"] == -5       # counted 10 of expected 15 -> that is a real shortage
    client.put(f"/inventory/counts/{c['id']}/entries", headers=auth["stock"], json={"entries": [{"line_id": lid, "counted_qty": 15}]})
    sub = client.post(f"/inventory/counts/{c['id']}/submit", headers=auth["stock"]).json()
    assert sub["request_ref"] is None                                        # no variance, so nothing to approve


# ---- reservations / concurrency -------------------------------------------------------------------------------
def test_reservation_service_basics(db, client, auth, catalog, locations):
    from backend.services import reservation_service as rs
    from backend.services.errors import InsufficientStockError
    receive(client, auth["ceo"], locations["WH"].id, [line(catalog["rose"], 5, "10")])
    rs.reserve(db, location_id=locations["WH"].id, variant_id=catalog["rose"], qty=3, ref_type="order", ref_id="O1")
    db.commit()
    import pytest
    with pytest.raises(InsufficientStockError):
        rs.reserve(db, location_id=locations["WH"].id, variant_id=catalog["rose"], qty=3, ref_type="order", ref_id="O2")
    db.rollback()
    assert rs.release(db, "order", "O1") == 1
    db.commit()
    assert rs.release(db, "order", "O1") == 0                                # idempotent
    s = stock(client, auth["ceo"], locations["WH"].id, catalog["rose"])
    assert s["available"] == 5 and s["reserved"] == 0


def test_concurrent_reservations_never_oversell(client, auth, catalog, locations):
    from backend.db import get_session_factory
    from backend.services import reservation_service as rs
    from backend.services.errors import InsufficientStockError
    wh = locations["WH"].id
    receive(client, auth["ceo"], wh, [line(catalog["rose"], 10, "10")])
    results = []
    barrier = threading.Barrier(8)

    def worker(i):
        with get_session_factory()() as s:
            barrier.wait()
            try:
                rs.reserve(s, location_id=wh, variant_id=catalog["rose"], qty=3, ref_type="order", ref_id=f"C{i}")
                s.commit()
                results.append("ok")
            except InsufficientStockError:
                s.rollback()
                results.append("short")

    ts_ = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts_]; [t.join() for t in ts_]
    assert results.count("ok") == 3 and results.count("short") == 5           # 10 units / 3 each
    s = stock(client, auth["ceo"], wh, catalog["rose"])
    assert s["reserved"] == 9 and s["available"] == 1


def test_concurrent_adjustments_cannot_make_stock_negative(client, auth, catalog, locations):
    from backend.db import get_session_factory
    from backend.services import inventory_service as inv
    from backend.services.errors import BusinessRuleError
    from backend.models import User
    wh = locations["WH"].id
    receive(client, auth["ceo"], wh, [line(catalog["rose"], 10, "10")])
    out = []
    barrier = threading.Barrier(6)

    def worker(i):
        with get_session_factory()() as s:
            actor = s.query(User).filter_by(username="stock").one()
            barrier.wait()
            try:
                inv.move_bucket(s, actor=actor, action="damage", location_id=wh, variant_id=catalog["rose"], batch_id=None,
                                qty=4, reason="race", notes=None, idempotency_key=None, ip=None)
                s.commit(); out.append("ok")
            except BusinessRuleError:
                s.rollback(); out.append("refused")

    ts_ = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in ts_]; [t.join() for t in ts_]
    assert out.count("ok") == 2                                                # only 8 of 10 can be moved in 4s
    s = stock(client, auth["ceo"], wh, catalog["rose"])
    assert (s["on_hand_good"], s["damaged"]) == (2, 8)
    integrity(client, auth["ceo"])


def test_concurrent_duplicate_idempotency_key_posts_once(client, auth, catalog, locations):
    from backend.db import get_session_factory
    from backend.services import inventory_service as inv
    from backend.services.errors import ConflictError
    from backend.models import User
    wh = locations["WH"].id
    receive(client, auth["ceo"], wh, [line(catalog["rose"], 10, "10")])
    res = []
    barrier = threading.Barrier(5)

    def worker():
        with get_session_factory()() as s:
            actor = s.query(User).filter_by(username="stock").one()
            barrier.wait()
            try:
                inv.move_bucket(s, actor=actor, action="damage", location_id=wh, variant_id=catalog["rose"], batch_id=None,
                                qty=1, reason="dup", notes=None, idempotency_key="same-key", ip=None)
                s.commit(); res.append("ok")
            except ConflictError:
                s.rollback(); res.append("dup")

    ts_ = [threading.Thread(target=worker) for _ in range(5)]
    [t.start() for t in ts_]; [t.join() for t in ts_]
    assert stock(client, auth["ceo"], wh, catalog["rose"])["damaged"] == 1


# ---- WAC rounding / settings ------------------------------------------------------------------------------------
def test_average_rounding_never_loses_value(client, auth, catalog, locations):
    wh = locations["WH"].id
    ceo = auth["ceo"]
    receive(client, ceo, wh, [line(catalog["rose"], 3, "10.0000")])
    receive(client, ceo, wh, [line(catalog["rose"], 4, "10.3333")])       # value 31.3332 over 7 units
    for _ in range(7):
        r = client.post("/inventory/requests", headers=auth["stock"], json={"kind": "adjustment", "location_id": wh,
            "reason": "x", "lines": [{"variant_id": catalog["rose"], "qty_delta": -1}]}).json()
        assert client.post(f"/inventory/requests/{r['id']}/approve", headers=auth["manager"], json={}).status_code == 200
    s = stock(client, ceo, wh, catalog["rose"])
    assert s["on_hand_good"] == 0 and Decimal(s["value"]) == Decimal("0")   # last unit takes exact remainder
    integrity(client, ceo)


def test_costing_policy_locked_after_first_transaction(client, auth, catalog, locations):
    ceo = auth["ceo"]
    s = client.get("/inventory/settings", headers=ceo).json()
    assert s["costing_method"] == "weighted_average" and not s["costing_locked"]
    assert client.put("/inventory/settings/costing", headers=ceo, json={"method": "fifo"}).status_code == 422
    assert client.put("/inventory/settings/costing", headers=ceo, json={"method": "weighted_average"}).status_code == 200
    receive(client, ceo, locations["WH"].id, [line(catalog["rose"], 1, "10")])
    assert client.get("/inventory/settings", headers=ceo).json()["costing_locked"]
    assert client.put("/inventory/settings/costing", headers=ceo, json={"method": "weighted_average"}).status_code == 409
    assert client.put("/inventory/settings/costing", headers=auth["manager"], json={"method": "weighted_average"}).status_code == 403


def test_auto_approve_threshold(client, auth, catalog, locations):
    wh = locations["WH"].id
    receive(client, auth["ceo"], wh, [line(catalog["rose"], 10, "10")])
    req = {"kind": "adjustment", "location_id": wh, "reason": "tiny", "lines": [{"variant_id": catalog["rose"], "qty_delta": -1}]}
    assert client.post("/inventory/requests", headers=auth["stock"], json=req).json()["status"] == "pending"   # default: never
    client.put("/inventory/settings/thresholds", headers=auth["ceo"], json={"adjustment_auto_approve_value": "15"})
    assert client.post("/inventory/requests", headers=auth["stock"], json=req).json()["status"] == "approved"
    big = {**req, "lines": [{"variant_id": catalog["rose"], "qty_delta": -3}]}
    assert client.post("/inventory/requests", headers=auth["stock"], json=big).json()["status"] == "pending"
    wo = {"kind": "writeoff", "location_id": wh, "reason": "x", "lines": [{"variant_id": catalog["rose"], "qty_delta": -1}]}
    assert client.post("/inventory/requests", headers=auth["stock"], json=wo).json()["status"] == "pending"   # write-offs never auto
