"""Phase 7: employee purchases (stock, discounts, credit, payments, posting) and biometric attendance (adapter, import, de-duplication,
daily summaries, exceptions, corrections). Synthetic data only."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from backend.tests.conftest import line, login, make_user, receive
from backend.tests.test_accounting import bal, run_posting
from backend.tests.test_sales import avail

D = Decimal
TODAY = date.today()


@pytest.fixture
def pe(client, auth, catalog, locations, users, db):
    """Stock at the warehouse and B1, a second CEO, a staff member and a stock keeper without any employee link."""
    make_user(db, "ceo2", "ceo")
    auth["ceo2"] = login(client, "ceo2")
    ids = {"WH": locations["WH"].id, "B1": locations["B1"].id, "B2": locations["B2"].id, **catalog}
    assert receive(client, auth["stock"], ids["WH"], [line(ids["rose"], 20, "40"), line(ids["nude"], 10, "30")]).status_code == 201
    r = client.post("/employees", headers=auth["manager"], json={"full_name": "Sara Test", "location_id": ids["B1"], "device_user_id": "17", "credit_limit": "500"})
    assert r.status_code == 201, r.text
    ids["emp"] = r.json()["id"]
    assert run_posting(client, auth)["errors"] == []              # start with an empty posting queue
    return ids


def purchase(client, h, ids, lines=None, **kw):
    body = {"employee_id": ids["emp"], "location_id": ids["WH"], "purchase_date": TODAY.isoformat(), "received_now": "80", "method": "cash",
            "lines": lines or [{"variant_id": ids["rose"], "qty": 1}], "receipt_ref": "R-1"}
    body.update(kw)
    return client.post("/employees/purchases", headers=h, json=body)


def on_hand(client, h, loc, variant):
    return avail(client, h, loc, variant)[1]


# ---- access --------------------------------------------------------------------------------------------------------------------------
def test_personnel_data_is_restricted_by_role(client, auth, pe):
    assert client.get("/employees", headers=auth["manager"]).status_code == 200
    for who in ("stock", "booker", "accounts"):
        assert client.get("/employees", headers=auth[who]).status_code == 403, who
        assert client.get(f"/employees/{pe['emp']}", headers=auth[who]).status_code == 403
        assert client.post("/employees", headers=auth[who], json={"full_name": "X", "location_id": pe["B1"]}).status_code == 403
    # the counter gets a plain name list, nothing else
    names = client.get("/employees/lookup", headers=auth["stock"]).json()
    assert names and set(names[0]) == {"id", "code", "full_name"}
    assert client.get("/employees/lookup", headers=auth["booker"]).status_code == 403
    # accounts sees what is owed but not the personnel record
    assert client.get("/employees/purchases", headers=auth["accounts"]).status_code == 200
    assert client.get("/employees/discount-rules", headers=auth["booker"]).status_code == 403


def test_stock_keeper_sees_only_staff_and_purchases_at_own_locations(client, auth, pe):
    other = client.post("/employees", headers=auth["ceo"], json={"full_name": "Far Away", "location_id": pe["B2"]}).json()
    ids = {e["id"] for e in client.get("/employees/lookup", headers=auth["stock"]).json()}
    assert pe["emp"] in ids and other["id"] not in ids
    assert purchase(client, auth["stock"], pe, employee_id=other["id"]).status_code == 404
    assert purchase(client, auth["stock"], pe, location_id=pe["B2"]).status_code == 404
    mine = purchase(client, auth["stock"], pe).json()
    assert client.get(f"/employees/purchases/{mine['id']}", headers=auth["stock"]).status_code == 200
    theirs = purchase(client, auth["ceo"], pe, lines=[{"variant_id": pe["nude"], "qty": 1}], received_now="100").json()
    assert client.get(f"/employees/purchases/{theirs['id']}", headers=auth["stock"]).status_code == 404       # not recorded by this person
    assert [x["id"] for x in client.get("/employees/purchases", headers=auth["stock"]).json()["items"]] == [mine["id"]]


def test_employee_device_id_and_user_link_must_be_unique(client, auth, pe, users):
    dup = client.post("/employees", headers=auth["manager"], json={"full_name": "Twin", "location_id": pe["B1"], "device_user_id": "17"})
    assert dup.status_code == 409
    a = client.post("/employees", headers=auth["manager"], json={"full_name": "Linked", "location_id": pe["B1"], "user_id": users["stock"].id})
    assert a.status_code == 201
    b = client.post("/employees", headers=auth["manager"], json={"full_name": "Linked again", "location_id": pe["B1"], "user_id": users["stock"].id})
    assert b.status_code == 409


# ---- pricing and discounts -------------------------------------------------------------------------------------------------------------
def test_discount_comes_from_price_list_then_central_rule_then_none(client, auth, pe):
    q = lambda v, n=1: client.post("/employees/purchases/quote", headers=auth["stock"], json={"lines": [{"variant_id": v, "qty": n}]}).json()  # noqa: E731
    rose = q(pe["rose"], 2)
    assert rose["lines"][0]["discount_source"] == "price_list" and D(rose["lines"][0]["unit_price"]) == 80 and D(rose["discount_total"]) == 40 and D(rose["total"]) == 160
    nude = q(pe["nude"])
    assert nude["lines"][0]["discount_source"] == "none" and D(nude["total"]) == 100
    assert client.post("/employees/discount-rules", headers=auth["stock"], json={"name": "Staff", "pct": "10"}).status_code == 403
    r = client.post("/employees/discount-rules", headers=auth["manager"], json={"name": "Staff 10%", "pct": "10"})
    assert r.status_code == 201, r.text
    nude = q(pe["nude"])
    assert nude["lines"][0]["discount_source"].startswith("rule:") and D(nude["total"]) == 90
    assert q(pe["rose"])["lines"][0]["discount_source"] == "price_list"          # the price list still wins
    assert client.post("/employees/discount-rules", headers=auth["manager"], json={"name": "Again", "pct": "5"}).status_code == 409
    cat = client.post("/employees/discount-rules", headers=auth["manager"], json={"name": "Category", "pct": "25", "category_id": pe["cat"]["id"]})
    assert cat.status_code == 201
    assert D(q(pe["nude"])["total"]) == 75                                         # the category rule is more specific
    assert client.patch(f"/employees/discount-rules/{cat.json()['id']}", headers=auth["manager"], json={"is_active": False, "reason": "ended"}).status_code == 200
    assert D(q(pe["nude"])["total"]) == 90
    assert client.post("/employees/discount-rules", headers=auth["manager"], json={"name": "Bad", "pct": "0", "category_id": pe["cat"]["id"]}).status_code == 422


# ---- a paid purchase: stock, sale and ledger ----------------------------------------------------------------------------------------------
def test_paid_purchase_moves_stock_and_posts_sale_discount_and_cost(client, auth, pe):
    before = on_hand(client, auth["ceo"], pe["WH"], pe["rose"])
    r = purchase(client, auth["stock"], pe)
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["status"] == "completed" and D(p["total"]) == 80 and D(p["discount_total"]) == 20 and D(p["balance"]) == 0
    assert p["receipt_ref"] == "R-1" and p["payments"][0]["method"] == "cash"
    assert on_hand(client, auth["ceo"], pe["WH"], pe["rose"]) == before - 1
    run = run_posting(client, auth)
    assert run["errors"] == [] and run["remaining"] == 0
    assert bal(client, auth, "4000") == -100 and bal(client, auth, "4200") == 20 and bal(client, auth, "5000") == 40
    assert bal(client, auth, "1400") == 0                                           # owed 80, paid 80
    integ = client.get("/accounting/integrity", headers=auth["accounts"]).json()
    emp_row = next(r for r in integ["rows"] if r["name"].startswith("Employee receivables"))
    assert emp_row["agrees"] and integ["consistent"]
    # posting twice changes nothing
    assert run_posting(client, auth)["posted"] == 0


def test_purchase_validation_and_duplicate_submission(client, auth, pe, users):
    assert purchase(client, auth["booker"], pe).status_code == 403
    assert purchase(client, auth["stock"], pe, purchase_date=(TODAY + timedelta(days=1)).isoformat()).status_code == 422
    assert purchase(client, auth["stock"], pe, received_now="500").status_code == 422             # more than the total
    assert purchase(client, auth["stock"], pe, method="bank_transfer").status_code == 422        # reference needed
    assert purchase(client, auth["stock"], pe, method="barter").status_code == 422
    assert purchase(client, auth["stock"], pe, lines=[{"variant_id": pe["rose"], "qty": 1}, {"variant_id": pe["rose"], "qty": 1}]).status_code == 422
    assert purchase(client, auth["stock"], pe, lines=[{"variant_id": pe["rose"], "qty": 999}], received_now="0").status_code == 422   # not enough stock
    a = purchase(client, auth["stock"], pe, idempotency_key="abc-1")
    b = purchase(client, auth["stock"], pe, idempotency_key="abc-1")
    assert a.status_code == 201 and b.status_code == 201 and a.json()["id"] == b.json()["id"]
    assert on_hand(client, auth["ceo"], pe["WH"], pe["rose"]) == 19


def test_nobody_can_record_a_purchase_for_their_own_employee_record(client, auth, pe, users):
    me = client.post("/employees", headers=auth["manager"], json={"full_name": "Stock Person", "location_id": pe["B1"], "user_id": users["stock"].id}).json()
    r = purchase(client, auth["stock"], pe, employee_id=me["id"])
    assert r.status_code == 403
    assert purchase(client, auth["ceo"], pe, employee_id=me["id"]).status_code == 201             # someone else can


def test_inactive_employee_cannot_buy_and_monthly_cap_is_enforced(client, auth, pe):
    assert client.put("/employees/settings", headers=auth["stock"], json={"monthly_purchase_cap": "100"}).status_code == 403
    assert client.put("/employees/settings", headers=auth["manager"], json={"monthly_purchase_cap": "150", "reason": "policy"}).status_code == 200
    assert purchase(client, auth["stock"], pe).status_code == 201                                     # 80
    over = purchase(client, auth["stock"], pe)                                                        # 160 > 150
    assert over.status_code == 422 and "cap" in over.json()["detail"]
    assert client.patch(f"/employees/{pe['emp']}", headers=auth["manager"], json={"status": "inactive", "reason": "left"}).status_code == 200
    assert purchase(client, auth["stock"], pe, lines=[{"variant_id": pe["nude"], "qty": 1}], received_now="100").status_code == 422


# ---- credit, approval, payments, void ------------------------------------------------------------------------------------------------
def test_credit_is_refused_until_allowed_then_needs_a_second_person(client, auth, pe):
    before = on_hand(client, auth["ceo"], pe["WH"], pe["rose"])
    r = purchase(client, auth["stock"], pe, received_now="30")
    assert r.status_code == 422 and "credit" in r.json()["detail"].lower()
    assert client.put("/employees/settings", headers=auth["manager"], json={"credit_allowed": True}).status_code == 200
    r = purchase(client, auth["stock"], pe, received_now="30")
    assert r.status_code == 201 and r.json()["status"] == "pending_approval"
    pid = r.json()["id"]
    assert on_hand(client, auth["ceo"], pe["WH"], pe["rose"]) == before                               # goods are not released yet
    assert run_posting(client, auth)["posted"] == 0 and bal(client, auth, "1400") == 0
    assert client.post(f"/employees/purchases/{pid}/decision", headers=auth["stock"], json={"approve": True}).status_code == 403
    assert client.post(f"/employees/purchases/{pid}/decision", headers=auth["manager"], json={"approve": False, "note": ""}).status_code == 422
    ok = client.post(f"/employees/purchases/{pid}/decision", headers=auth["manager"], json={"approve": True, "note": "ok"})
    assert ok.status_code == 200 and ok.json()["status"] == "completed" and D(ok.json()["balance"]) == 50
    assert on_hand(client, auth["ceo"], pe["WH"], pe["rose"]) == before - 1
    assert client.post(f"/employees/purchases/{pid}/decision", headers=auth["manager"], json={"approve": True}).status_code == 409
    run_posting(client, auth)
    assert bal(client, auth, "1400") == 50
    emp = client.get(f"/employees/{pe['emp']}", headers=auth["manager"]).json()
    assert D(emp["outstanding"]) == 50


def test_creator_cannot_approve_and_rejection_releases_nothing(client, auth, pe):
    client.put("/employees/settings", headers=auth["manager"], json={"credit_allowed": True})
    # the manager lacks employees.purchase, so the CEO (who has everything) records it
    pid = purchase(client, auth["ceo"], pe, received_now="0").json()["id"]
    assert client.post(f"/employees/purchases/{pid}/decision", headers=auth["ceo"], json={"approve": True}).status_code == 403
    rej = client.post(f"/employees/purchases/{pid}/decision", headers=auth["manager"], json={"approve": False, "note": "Over policy"})
    assert rej.status_code == 200 and rej.json()["status"] == "rejected"
    assert on_hand(client, auth["ceo"], pe["WH"], pe["rose"]) == 20
    assert run_posting(client, auth)["posted"] == 0


def test_credit_limit_blocks_and_payments_settle_the_balance(client, auth, pe):
    client.put("/employees/settings", headers=auth["manager"], json={"credit_allowed": True})
    big = purchase(client, auth["stock"], pe, lines=[{"variant_id": pe["nude"], "qty": 6}], received_now="0")       # 600 > limit 500
    assert big.status_code == 422 and "limit" in big.json()["detail"]
    pid = purchase(client, auth["stock"], pe, lines=[{"variant_id": pe["nude"], "qty": 4}], received_now="0").json()["id"]
    assert client.post(f"/employees/purchases/{pid}/decision", headers=auth["manager"], json={"approve": True, "note": "ok"}).status_code == 200
    run_posting(client, auth)
    assert bal(client, auth, "1400") == 400
    pay = lambda amt, h="accounts", **kw: client.post(f"/employees/purchases/{pid}/payments", headers=auth[h], json={  # noqa: E731
        "amount": amt, "method": "cash", "paid_date": TODAY.isoformat(), **kw})
    assert pay("100", "stock").status_code == 403
    assert pay("500").status_code == 422                                                              # more than owed
    assert pay("0").status_code == 422
    first = pay("150")
    assert first.status_code == 201 and D(first.json()["balance"]) == 250
    assert pay("250").json()["balance"] in ("0.00", "0")
    run_posting(client, auth)
    assert bal(client, auth, "1400") == 0 and bal(client, auth, "1000") == 400
    # a wrongly recorded payment is reversed by someone else, and the debt returns
    pid_pay = first.json()["payments"][0]["id"]
    assert client.post(f"/employees/purchases/payments/{pid_pay}/reverse", headers=auth["accounts"], json={"reason": "wrong employee"}).status_code == 403
    rv = client.post(f"/employees/purchases/payments/{pid_pay}/reverse", headers=auth["ceo"], json={"reason": "wrong employee"})
    assert rv.status_code == 200 and D(rv.json()["balance"]) == 150
    assert client.post(f"/employees/purchases/payments/{pid_pay}/reverse", headers=auth["ceo"], json={"reason": "again"}).status_code == 409
    run_posting(client, auth)
    assert bal(client, auth, "1400") == 150 and bal(client, auth, "1000") == 250


def test_void_returns_stock_reverses_payments_and_the_sale(client, auth, pe):
    before = on_hand(client, auth["ceo"], pe["WH"], pe["rose"])
    pid = purchase(client, auth["stock"], pe).json()["id"]
    run_posting(client, auth)
    assert client.post(f"/employees/purchases/{pid}/void", headers=auth["stock"], json={"reason": "mistake"}).status_code == 403     # no permission
    assert client.post(f"/employees/purchases/{pid}/void", headers=auth["manager"], json={"reason": ""}).status_code == 422
    v = client.post(f"/employees/purchases/{pid}/void", headers=auth["manager"], json={"reason": "Wrong employee"})
    assert v.status_code == 200, v.text
    assert v.json()["status"] == "voided" and v.json()["payments"][0]["status"] == "reversed"
    assert on_hand(client, auth["ceo"], pe["WH"], pe["rose"]) == before
    run = run_posting(client, auth)
    assert run["errors"] == [] and run["remaining"] == 0
    for code in ("4000", "4200", "5000", "1400", "1000"):
        assert bal(client, auth, code) == 0, code
    assert client.post(f"/employees/purchases/{pid}/void", headers=auth["manager"], json={"reason": "twice"}).status_code == 409


def test_recorder_cannot_void_their_own_purchase(client, auth, pe):
    pid = purchase(client, auth["ceo"], pe).json()["id"]
    assert client.post(f"/employees/purchases/{pid}/void", headers=auth["ceo"], json={"reason": "oops"}).status_code == 403
    assert client.post(f"/employees/purchases/{pid}/void", headers=auth["ceo2"], json={"reason": "oops"}).status_code == 200


def test_batch_stock_is_issued_first_expiring_first_and_expired_is_skipped(client, auth, pe):
    h, wh, serum = auth["stock"], pe["WH"], pe["serum"]
    soon, later = (TODAY + timedelta(days=40)).isoformat(), (TODAY + timedelta(days=400)).isoformat()
    assert receive(client, h, wh, [line(serum, 3, "10", batch_no="LATE", expiry_date=later), line(serum, 2, "10", batch_no="SOON", expiry_date=soon)]).status_code == 201
    r = purchase(client, h, pe, lines=[{"variant_id": serum, "qty": 4}], received_now="2000")
    assert r.status_code == 201, r.text
    lines = r.json()["lines"]
    assert sorted(l["qty"] for l in lines) == [2, 2]                                                   # 2 from SOON then 2 from LATE
    assert on_hand(client, auth["ceo"], wh, serum) == 1


def test_stock_shortage_leaves_nothing_behind(client, auth, pe):
    n = len(client.get("/employees/purchases", headers=auth["ceo"]).json()["items"])
    bad = purchase(client, auth["ceo"], pe, lines=[{"variant_id": pe["rose"], "qty": 5}, {"variant_id": pe["nude"], "qty": 50}], received_now="0")
    assert bad.status_code == 422
    assert len(client.get("/employees/purchases", headers=auth["ceo"]).json()["items"]) == n
    assert on_hand(client, auth["ceo"], pe["WH"], pe["rose"]) == 20


def test_employee_actions_are_audited(client, auth, pe):
    pid = purchase(client, auth["stock"], pe).json()["id"]
    client.post(f"/employees/purchases/{pid}/void", headers=auth["manager"], json={"reason": "audit me"})
    acts = {a["action"] for a in client.get("/audit", headers=auth["ceo"], params={"page_size": 200}).json()["items"]}
    assert {"employee.created", "employee_purchase.recorded", "employee_purchase.voided"} <= acts


# =============================================================================================================================
# Attendance
# =============================================================================================================================
MON = date(2026, 10, 5)          # a Monday in the past
CFG = {"columns": {"user_id": "ID", "timestamp": "Time", "direction": "State"}, "timestamp_format": "%Y-%m-%d %H:%M:%S", "timezone": "Asia/Karachi",
       "direction_map": {"in": ["IN"], "out": ["OUT"]}}
POLICY = {"timezone": "Asia/Karachi", "shift_start": "09:00", "shift_end": "17:00", "grace_minutes": 10, "min_present_minutes": 240, "weekly_off": [6],
          "direction_mode": "device", "overtime_enabled": True, "overtime_after_minutes": 0}


def csv(rows, header="ID,Time,State"):
    return (header + "\n" + "\n".join(rows) + "\n").encode()


def upload(client, h, device_id, content, name="punches.csv", **params):
    return client.post("/attendance/imports", headers=h, params={"device_id": device_id, "filename": name, **params}, content=content)


@pytest.fixture
def at(client, auth, pe):
    r = client.post("/attendance/devices", headers=auth["manager"], json={
        "name": "Door B1", "location_id": pe["B1"], "adapter": "generic_file", "brand": "ExampleBrand", "model": "X-1", "protocol": "file export",
        "export_format": "csv", "config": CFG})
    assert r.status_code == 201, r.text
    pe["dev"] = r.json()["id"]
    return pe


def day(client, h, emp, d, **kw):
    rows = client.get("/attendance/days", headers=h, params={"date_from": d.isoformat(), "date_to": d.isoformat(), "employee_id": emp, **kw}).json()
    return rows[0] if rows else None


def set_policy(client, h, **over):
    r = client.put("/attendance/policies", headers=h, json={**POLICY, **over})
    assert r.status_code == 200, r.text
    return r.json()


def test_attendance_is_visible_only_to_authorised_roles(client, auth, at):
    for who in ("stock", "booker", "accounts"):
        for path in ("/attendance/devices", "/attendance/policies", "/attendance/corrections", "/attendance/adapters"):
            assert client.get(path, headers=auth[who]).status_code == 403, (who, path)
        assert client.get("/attendance/days", headers=auth[who], params={"date_from": "2026-10-05", "date_to": "2026-10-05"}).status_code == 403
        assert upload(client, auth[who], at["dev"], csv(["17,2026-10-05 09:00:00,IN"])).status_code == 403
    assert client.get("/attendance/devices", headers=auth["manager"]).status_code == 200


def test_device_settings_are_validated_and_hold_no_secrets(client, auth, at):
    body = {"name": "Bad", "location_id": at["B1"], "adapter": "generic_file"}
    assert client.post("/attendance/devices", headers=auth["manager"], json={**body, "config": {**CFG, "timezone": ""}}).status_code == 422       # tz never assumed
    assert client.post("/attendance/devices", headers=auth["manager"], json={**body, "config": {**CFG, "api_password": "x"}}).status_code in (409, 422)
    assert client.post("/attendance/devices", headers=auth["manager"], json={**body, "config": {**CFG, "columns": {"user_id": "ID"}}}).status_code == 422
    assert client.post("/attendance/devices", headers=auth["manager"], json={**body, "adapter": "magic", "config": CFG}).status_code == 422
    assert client.post("/attendance/devices", headers=auth["manager"], json={**body, "name": "Door B1", "config": CFG}).status_code == 409
    nested = {**CFG, "extra": {"token": "abc"}}
    assert client.post("/attendance/devices", headers=auth["manager"], json={**body, "name": "N", "config": nested}).status_code == 422
    assert client.get("/attendance/adapters", headers=auth["manager"]).json()[0]["code"] == "generic_file"


def test_import_reads_rows_rejects_bad_ones_and_reports_unmatched_ids(client, auth, at):
    f = csv(["17,2026-10-05 09:03:00,IN", "17,2026-10-05 17:05:00,OUT", "99,2026-10-05 09:00:00,IN", ",2026-10-05 09:00:00,IN",
             "17,not a time,IN", "17,2026-10-06 09:00:00,SIDEWAYS"])
    dry = upload(client, auth["manager"], at["dev"], f, dry_run="true").json()
    assert dry["dry_run"] and dry["rows_new"] == 4 and dry["rows_rejected"] == 2 and dry["rows_unmatched"] == 1 and dry["unmatched_ids"] == ["99"]
    assert client.get("/attendance/imports", headers=auth["manager"]).json() == []                       # a dry run saves nothing
    r = upload(client, auth["manager"], at["dev"], f)
    assert r.status_code == 200, r.text
    s = r.json()
    assert (s["rows_total"], s["rows_new"], s["rows_duplicate"], s["rows_rejected"], s["rows_unmatched"]) == (6, 4, 0, 2, 1)
    assert {x["reason"] for x in s["rejects"]} >= {"no user id"} and any("does not match" in x["reason"] for x in s["rejects"])
    ex = client.get("/attendance/exceptions", headers=auth["manager"], params={"date_from": "2026-10-01", "date_to": "2026-10-31"}).json()
    assert [u["device_user_id"] for u in ex["unmatched_device_ids"]] == ["99"]


def test_importing_the_same_punches_twice_never_duplicates_them(client, auth, at):
    first = csv(["17,2026-10-05 09:00:00,IN", "17,2026-10-05 17:00:00,OUT"])
    assert upload(client, auth["manager"], at["dev"], first).status_code == 200
    again = upload(client, auth["manager"], at["dev"], first)
    assert again.status_code == 409 and "already imported" in again.json()["detail"]                      # the identical file is refused
    overlap = csv(["17,2026-10-05 17:00:00,OUT", "17,2026-10-06 09:00:00,IN", "17,2026-10-06 09:00:00,IN"])    # overlaps the first and repeats a line inside itself
    s = upload(client, auth["manager"], at["dev"], overlap, name="overlap.csv").json()
    assert s["rows_new"] == 1 and s["rows_duplicate"] == 2
    set_policy(client, auth["manager"])
    assert len(client.get("/attendance/punches", headers=auth["manager"], params={"employee_id": at["emp"], "work_date": "2026-10-05"}).json()) == 2
    # the same instant from the same person is kept once even if the second file words it differently
    again2 = upload(client, auth["manager"], at["dev"], csv(["17,2026-10-05 09:00:00,OUT"]), name="other.csv").json()
    assert again2["rows_new"] == 0 and again2["rows_duplicate"] == 1


def test_a_file_with_no_readable_rows_is_refused_with_the_first_problem(client, auth, at):
    r = upload(client, auth["manager"], at["dev"], csv(["17,garbage,IN"]))
    assert r.status_code == 422 and "line 2" in r.json()["detail"]
    assert upload(client, auth["manager"], at["dev"], b"").status_code == 422
    wrong = upload(client, auth["manager"], at["dev"], csv(["1,2"], header="Name,Other"))
    assert wrong.status_code == 422 and "no column called" in wrong.json()["detail"]


def test_excel_exports_are_read_too(client, auth, at, tmp_path):
    from openpyxl import Workbook
    from datetime import datetime
    wb = Workbook()
    ws = wb.active
    ws.append(["ID", "Time", "State"])
    ws.append([17, datetime(2026, 10, 5, 9, 0, 0), "IN"])
    ws.append([17, datetime(2026, 10, 5, 17, 0, 0), "OUT"])
    p = tmp_path / "x.xlsx"
    wb.save(p)
    r = upload(client, auth["manager"], at["dev"], p.read_bytes(), name="x.xlsx")
    assert r.status_code == 200 and r.json()["rows_new"] == 2, r.text


def test_no_summary_without_a_policy_then_days_are_classified(client, auth, at):
    rows = ["17,2026-10-05 08:58:00,IN", "17,2026-10-05 17:30:00,OUT",           # Mon: on time, 30 min extra
            "17,2026-10-06 09:30:00,IN", "17,2026-10-06 17:00:00,OUT",            # Tue: late
            "17,2026-10-07 09:00:00,IN",                                          # Wed: no check-out
            "17,2026-10-08 09:00:00,IN", "17,2026-10-08 10:00:00,OUT"]            # Thu: short
    assert upload(client, auth["manager"], at["dev"], csv(rows)).status_code == 200
    assert day(client, auth["manager"], at["emp"], MON) is None                                              # no policy yet
    set_policy(client, auth["manager"])
    d = lambda n: day(client, auth["manager"], at["emp"], MON + timedelta(days=n))  # noqa: E731
    assert d(0)["status"] == "present" and d(0)["worked_minutes"] == 512 and d(0)["overtime_minutes"] == 32 and d(0)["late_minutes"] == 0
    assert d(1)["status"] == "late" and d(1)["late_minutes"] == 30 and d(1)["worked_minutes"] == 450
    assert d(2)["status"] == "incomplete" and d(2)["exception"] == "missing_out" and d(2)["worked_minutes"] == 0
    assert d(3)["status"] == "short" and d(3)["worked_minutes"] == 60
    assert d(4)["status"] == "no_punch"                                                                       # Friday, nothing recorded
    sun = day(client, auth["manager"], at["emp"], MON + timedelta(days=6))
    assert sun["status"] == "weekly_off"
    ex = client.get("/attendance/exceptions", headers=auth["manager"], params={"date_from": "2026-10-05", "date_to": "2026-10-11"}).json()
    assert [x["exception"] for x in ex["days"]] == ["missing_out"]
    s = client.get("/attendance/summary", headers=auth["manager"], params={"date_from": "2026-10-05", "date_to": "2026-10-11"}).json()[0]
    assert (s["days_present"], s["days_late"], s["days_short"], s["days_incomplete"]) == (3, 1, 1, 1) and s["overtime_minutes"] == 32


def test_punch_near_midnight_belongs_to_the_local_day(client, auth, at):
    set_policy(client, auth["manager"])
    assert upload(client, auth["manager"], at["dev"], csv(["17,2026-10-05 00:20:00,IN", "17,2026-10-05 23:40:00,OUT"])).status_code == 200
    d = day(client, auth["manager"], at["emp"], MON)
    assert d["punch_count"] == 2 and d["worked_minutes"] == 23 * 60 + 20
    sunday = day(client, auth["manager"], at["emp"], MON - timedelta(days=1))                      # 00:20 on Monday is 19:20 UTC on Sunday
    assert sunday["status"] == "weekly_off" and sunday["punch_count"] == 0


def test_first_last_mode_for_devices_that_do_not_say_in_or_out(client, auth, at):
    dev = client.post("/attendance/devices", headers=auth["manager"], json={
        "name": "No direction", "location_id": at["B1"], "adapter": "generic_file",
        "config": {"columns": {"user_id": "ID", "timestamp": "Time"}, "timestamp_format": "%Y-%m-%d %H:%M:%S", "timezone": "Asia/Karachi"}}).json()
    rows = csv(["17,2026-10-05 09:00:00", "17,2026-10-05 13:00:00", "17,2026-10-05 17:00:00", "17,2026-10-06 09:00:00"], header="ID,Time")
    assert upload(client, auth["manager"], dev["id"], rows, name="a.csv").status_code == 200
    set_policy(client, auth["manager"])                                                                  # device mode: nothing says in or out
    assert day(client, auth["manager"], at["emp"], MON)["exception"] == "unclassified_punch"
    set_policy(client, auth["manager"], direction_mode="first_last")
    assert day(client, auth["manager"], at["emp"], MON)["worked_minutes"] == 480 and day(client, auth["manager"], at["emp"], MON)["exception"] is None
    assert day(client, auth["manager"], at["emp"], MON + timedelta(days=1))["exception"] == "single_punch"


def test_policy_is_validated_and_scoped(client, auth, at):
    bad = lambda **o: client.put("/attendance/policies", headers=auth["manager"], json={**POLICY, **o}).status_code  # noqa: E731
    assert bad(timezone="Mars/Base") == 422 and bad(shift_end="08:00") == 422 and bad(weekly_off=[7]) == 422 and bad(direction_mode="guess") == 422
    assert client.put("/attendance/policies", headers=auth["stock"], json=POLICY).status_code == 403
    p = set_policy(client, auth["manager"], location_id=at["B1"], shift_start="10:00")
    assert p["location_id"] == at["B1"] and p["shift_start"] == "10:00"
    assert len(client.get("/attendance/policies", headers=auth["manager"]).json()) == 1


def test_employee_given_a_device_id_later_picks_up_earlier_punches(client, auth, at):
    set_policy(client, auth["manager"])
    new = client.post("/employees", headers=auth["manager"], json={"full_name": "Late Id", "location_id": at["B1"]}).json()
    assert upload(client, auth["manager"], at["dev"], csv(["55,2026-10-05 09:00:00,IN", "55,2026-10-05 17:00:00,OUT"])).json()["rows_unmatched"] == 2
    assert day(client, auth["manager"], new["id"], MON) is None                                        # nothing to summarise yet
    assert client.patch(f"/employees/{new['id']}", headers=auth["manager"], json={"device_user_id": "55"}).status_code == 200
    assert day(client, auth["manager"], new["id"], MON)["status"] == "present"
    assert client.get("/attendance/exceptions", headers=auth["manager"], params={"date_from": "2026-10-01", "date_to": "2026-10-31"}).json()["unmatched_device_ids"] == []


def test_employee_created_with_a_device_id_picks_up_punches_already_stored(client, auth, at):
    set_policy(client, auth["manager"])
    assert upload(client, auth["manager"], at["dev"], csv(["56,2026-10-05 09:00:00,IN", "56,2026-10-05 17:00:00,OUT"])).json()["rows_unmatched"] == 2
    new = client.post("/employees", headers=auth["manager"], json={"full_name": "Created With Id", "location_id": at["B1"], "device_user_id": "56"})
    assert new.status_code == 201, new.text
    assert day(client, auth["manager"], new.json()["id"], MON)["status"] == "present"
    assert client.get("/attendance/exceptions", headers=auth["manager"], params={"date_from": "2026-10-01", "date_to": "2026-10-31"}).json()["unmatched_device_ids"] == []


def test_corrections_need_a_second_person_and_change_the_day(client, auth, at):
    set_policy(client, auth["manager"])
    upload(client, auth["manager"], at["dev"], csv(["17,2026-10-07 09:00:00,IN"]))
    d0 = day(client, auth["manager"], at["emp"], MON + timedelta(days=2))
    assert d0["exception"] == "missing_out" and d0["first_in_local"] == "09:00" and d0["last_out_local"] is None      # the check-in that exists is shown
    body = {"employee_id": at["emp"], "work_date": "2026-10-07", "kind": "add_punch", "direction": "out", "punch_time": "17:00", "reason": "Forgot to punch out"}
    assert client.post("/attendance/corrections", headers=auth["stock"], json=body).status_code == 403
    assert client.post("/attendance/corrections", headers=auth["manager"], json={**body, "reason": "x"}).status_code == 422
    assert client.post("/attendance/corrections", headers=auth["manager"], json={**body, "direction": None}).status_code == 422
    c = client.post("/attendance/corrections", headers=auth["manager"], json=body)
    assert c.status_code == 201, c.text
    assert client.post("/attendance/corrections", headers=auth["manager"], json=body).status_code == 409           # already waiting
    cid = c.json()["id"]
    assert day(client, auth["manager"], at["emp"], MON + timedelta(days=2))["exception"] == "missing_out"          # nothing changes until approved
    assert client.post(f"/attendance/corrections/{cid}/decision", headers=auth["manager"], json={"approve": True}).status_code == 403   # not your own request
    assert client.post(f"/attendance/corrections/{cid}/decision", headers=auth["stock"], json={"approve": True}).status_code == 403
    ok = client.post(f"/attendance/corrections/{cid}/decision", headers=auth["ceo"], json={"approve": True, "note": "Confirmed with supervisor"})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    d = day(client, auth["manager"], at["emp"], MON + timedelta(days=2))
    assert d["exception"] is None and d["worked_minutes"] == 480 and d["corrected"] is True
    assert client.post(f"/attendance/corrections/{cid}/decision", headers=auth["ceo"], json={"approve": True}).status_code == 409
    pun = client.get("/attendance/punches", headers=auth["manager"], params={"employee_id": at["emp"], "work_date": "2026-10-07"}).json()
    assert {p["source"] for p in pun} == {"device", "correction"}


def test_a_wrong_punch_is_ignored_by_correction_and_rejection_needs_a_reason(client, auth, at):
    set_policy(client, auth["manager"])
    upload(client, auth["manager"], at["dev"], csv(["17,2026-10-05 09:00:00,IN", "17,2026-10-05 12:00:00,IN", "17,2026-10-05 17:00:00,OUT"]))
    assert day(client, auth["manager"], at["emp"], MON)["exception"] == "missing_out"
    pun = client.get("/attendance/punches", headers=auth["manager"], params={"employee_id": at["emp"], "work_date": "2026-10-05"}).json()
    wrong = next(p for p in pun if p["local_time"] == "12:00:00")
    body = {"employee_id": at["emp"], "work_date": "2026-10-05", "kind": "ignore_punch", "punch_id": wrong["id"], "reason": "Duplicate tap"}
    c1 = client.post("/attendance/corrections", headers=auth["manager"], json=body).json()
    assert client.post(f"/attendance/corrections/{c1['id']}/decision", headers=auth["ceo"], json={"approve": False, "note": ""}).status_code == 422
    assert client.post(f"/attendance/corrections/{c1['id']}/decision", headers=auth["ceo"], json={"approve": False, "note": "Not convinced"}).json()["status"] == "rejected"
    assert day(client, auth["manager"], at["emp"], MON)["exception"] == "missing_out"
    c2 = client.post("/attendance/corrections", headers=auth["manager"], json=body).json()
    assert client.post(f"/attendance/corrections/{c2['id']}/decision", headers=auth["ceo"], json={"approve": True, "note": "ok"}).status_code == 200
    d = day(client, auth["manager"], at["emp"], MON)
    assert d["exception"] is None and d["worked_minutes"] == 480 and d["corrected"]
    bad = client.post("/attendance/corrections", headers=auth["manager"], json={**body, "punch_id": 99999})
    assert bad.status_code == 422


def test_an_employee_cannot_correct_or_approve_their_own_attendance(client, auth, at, users):
    set_policy(client, auth["manager"])
    me = client.post("/employees", headers=auth["manager"], json={"full_name": "Boss Person", "location_id": at["B1"], "user_id": users["manager"].id}).json()
    body = {"employee_id": me["id"], "work_date": "2026-10-05", "kind": "add_punch", "direction": "in", "punch_time": "09:00", "reason": "Forgot to punch"}
    assert client.post("/attendance/corrections", headers=auth["manager"], json=body).status_code == 403
    c = client.post("/attendance/corrections", headers=auth["ceo"], json=body)
    assert c.status_code == 201
    assert client.post(f"/attendance/corrections/{c.json()['id']}/decision", headers=auth["manager"], json={"approve": True}).status_code == 403


def test_raw_punches_cannot_be_edited_or_deleted_in_the_database(client, auth, at, db):
    upload(client, auth["manager"], at["dev"], csv(["17,2026-10-05 09:00:00,IN"]))
    with pytest.raises(DBAPIError):
        db.execute(text("UPDATE attendance_punches SET punched_at = punched_at + interval '1 hour'"))
    db.rollback()
    with pytest.raises(DBAPIError):
        db.execute(text("DELETE FROM attendance_punches"))
    db.rollback()
    db.execute(text("UPDATE attendance_punches SET status = 'ignored'"))            # the one permitted change (a correction ignoring a punch)
    db.rollback()


def test_rebuild_and_audit_trail(client, auth, at):
    set_policy(client, auth["manager"])
    upload(client, auth["manager"], at["dev"], csv(["17,2026-10-05 09:00:00,IN", "17,2026-10-05 17:00:00,OUT"]))
    r = client.post("/attendance/rebuild", headers=auth["manager"], json={"date_from": "2026-10-05", "date_to": "2026-10-07"})
    assert r.status_code == 200 and r.json()["days_built"] == 3
    assert client.post("/attendance/rebuild", headers=auth["manager"], json={"date_from": "2026-10-07", "date_to": "2026-10-05"}).status_code == 422
    assert client.post("/attendance/rebuild", headers=auth["stock"], json={"date_from": "2026-10-05", "date_to": "2026-10-07"}).status_code == 403
    acts = {a["action"] for a in client.get("/audit", headers=auth["ceo"], params={"page_size": 200}).json()["items"]}
    assert {"attendance_device.created", "attendance_policy.set", "attendance.imported"} <= acts
