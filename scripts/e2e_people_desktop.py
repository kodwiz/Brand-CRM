"""Phase 7 desktop check: drive the REAL Employees and Attendance screens against the REAL running backend (demo environment).

    python scripts/e2e_people_desktop.py CREDENTIALS_FILE OUTDIR

Reuses the helpers of scripts/e2e_desktop.py. Exits non-zero on any failed check."""
import csv
import sys
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_desktop as E  # noqa: E402  (builds the Qt application)
from e2e_desktop import add_line, check, click_submit, creds, pump, sess, stock_of  # noqa: E402
from PySide6.QtWidgets import QPushButton  # noqa: E402

from desktop.api.client import ApiError  # noqa: E402
from desktop.pages import attendance as at, employees as em  # noqa: E402
from desktop.ui.shell import Shell  # noqa: E402

D = Decimal
SKU = "DEMO-LIP-01"


def buttons(w) -> list[str]:
    return [b.text() for b in w.findChildren(QPushButton) if b.isVisibleTo(w)]


def pick(combo, text: str) -> None:
    i = next((i for i in range(combo.count()) if combo.itemText(i).startswith(text)), -1)
    assert i >= 0, f"{text!r} not in combo ({[combo.itemText(k) for k in range(combo.count())][:8]})"
    combo.setCurrentIndex(i)


def select(table, key, value) -> bool:
    for i in range(table.rowCount()):
        r = table.row_data(i)
        if r and r.get(key) == value:
            table.selectRow(i)
            return True
    return False


def auto_note(text: str = "Demo note for the record"):
    """Make NoteDialog answer itself so handlers that open it can be driven without a human."""
    def _exec(self):
        if hasattr(self.text, "setPlainText"):
            self.text.setPlainText(text)
        else:
            self.text.setText(text)
        self.submit_btn.click(); pump(1.5)
        return self.result()
    em.NoteDialog.exec = _exec
    at.NoteDialog.exec = _exec


def refused(fn) -> str | None:
    try:
        fn()
    except ApiError as e:
        return f"{e.status}"
    return None


def last_workdays(n: int) -> list[date]:
    out, d = [], date.today() - timedelta(days=1)
    while len(out) < n:
        if d.weekday() != 6:
            out.append(d)
        d -= timedelta(days=1)
    return out


def main() -> None:
    cred = creds(sys.argv[1])
    out = Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
    ceo, mgr, stk, bk, acc = (sess(u, cred[u]) for u in ("demo.ceo", "demo.manager", "demo.stock", "demo.booker", "demo.accounts"))
    auto_note()
    today = date.today().isoformat()
    B1 = next(l["id"] for l in ceo.client.get("/locations") if l["code"] == "DEMO-B1")
    var = ceo.client.get("/variants/lookup", q=SKU)[0]
    stock_user = next(u for u in ceo.client.get("/users", page_size=200)["items"] if u["username"] == "demo.stock")

    # ---- who sees what -----------------------------------------------------------------------------------------------------------
    seen = {}
    for s, name in ((bk, "booker"), (stk, "stock"), (acc, "accounts"), (mgr, "manager"), (ceo, "ceo")):
        sh = Shell(s); sh.resize(1366, 768); sh.show(); pump(0.3)
        seen[name] = ("employees" in sh.buttons, "attendance" in sh.buttons)
        sh.close()
    check(seen["booker"] == (False, False), f"order booker sees neither module ({seen['booker']})")
    check(seen["stock"] == (True, False), f"stock keeper sees Employees (to sell to staff) but never Attendance ({seen['stock']})")
    check(seen["accounts"] == (True, False), f"accounts sees Employees purchases but never Attendance ({seen['accounts']})")
    check(seen["manager"] == (True, True) and seen["ceo"] == (True, True), "manager and CEO see both modules")

    def tabs(s):
        sh = Shell(s); sh.resize(1366, 768); sh.show(); pump(0.3); sh.show_page("employees"); pump(1.2)
        pg = sh.pages["employees"]
        names = [pg.tabs.tabText(i) for i in range(pg.tabs.count())]
        sh.close()
        return type(pg).__name__, names
    k, names = tabs(stk)
    check(k == "EmployeesPage" and names == ["Purchases"], f"stock keeper gets only the purchase counter, no personnel records ({names})")
    check(tabs(acc)[1] == ["Purchases", "Discounts and credit"], f"accounts sees purchases and rules, no personnel records ({tabs(acc)[1]})")
    check(tabs(mgr)[1] == ["Employees", "Purchases", "Discounts and credit"], "manager sees all three tabs")
    for s, name in ((stk, "stock keeper"), (bk, "order booker"), (acc, "accounts")):
        check(refused(lambda: s.client.get("/attendance/days", date_from=today, date_to=today)) == "403", f"{name} is refused attendance data by the server")
    check(refused(lambda: bk.client.get("/employees")) == "403", "order booker is refused the employee list by the server")
    check(refused(lambda: stk.client.get("/employees")) == "403", "stock keeper is refused the employee list (only the name lookup)")

    # ---- setup through the screens a manager would use ----------------------------------------------------------------------------------
    sh_m = Shell(mgr); sh_m.resize(1366, 768); sh_m.show(); pump(0.3)
    sh_m.show_page("employees"); pump(1.5)
    ep = sh_m.pages["employees"]
    et, pt, dt = (w for _, w in ep.items)
    check("New employee" in buttons(et) and "Edit selected" in buttons(et), "manager can create and edit employees")
    check("Save settings" in buttons(dt) and "New rule" in buttons(dt), "manager can set discounts and credit")

    d = em.EmployeeDialog(mgr); click_submit(d)
    check(d.result() == 0, "employee without a name or location is refused")
    d.close()
    made = {}
    for name, dev, lim, user in (("Demo Staff Aisha", "101", "0", None), ("Demo Staff Bilal", "102", "5000", None), ("Demo Staff Stock", "103", "0", stock_user["id"])):
        d = em.EmployeeDialog(mgr); pump(1.0)
        d.name.setText(name); d.designation.setText("Sales assistant"); d.loc.setCurrentIndex(d.loc.findData(B1)); d.device.setText(dev); d.limit.setText(lim)
        if user:
            pick(d.user, "Demo Stock")
        click_submit(d)
        check(d.result() == 1, f"employee created: {name}")
        made[name] = next(e for e in mgr.client.get("/employees", q=name)["items"] if e["full_name"] == name)
    dup = em.EmployeeDialog(mgr); pump(1.0)
    dup.name.setText("Demo Staff Duplicate"); dup.loc.setCurrentIndex(dup.loc.findData(B1)); dup.device.setText("101"); click_submit(dup)
    check(dup.result() == 0 and dup.banner.text(), f"two employees cannot share a device number ({dup.banner.text()[:70]})")
    dup.close()
    aisha, bilal, selfemp = made["Demo Staff Aisha"], made["Demo Staff Bilal"], made["Demo Staff Stock"]
    check(aisha["code"].startswith("EMP"), f"employee codes are generated ({aisha['code']})")

    rd = em.RuleDialog(mgr); pump(1.0); click_submit(rd)
    check(rd.result() == 0, "rule without a name or percentage is refused")
    rd.name.setText("Demo staff 20%"); rd.pct.setText("120"); click_submit(rd)
    check(rd.result() == 0, "a discount above 100% is refused")
    rd.pct.setText("20"); click_submit(rd)
    check(rd.result() == 1 and any(r["name"] == "Demo staff 20%" for r in mgr.client.get("/employees/discount-rules")), "general staff discount rule saved")
    dt.refresh(); pump(1.2)
    check(dt.table.rowCount() >= 1 and not dt.credit.isChecked(), "credit is off by default")
    dt.credit.setChecked(True); dt.cap.setText("0"); dt._save(); pump(1.5)
    check(mgr.client.get("/employees/settings")["credit_allowed"] is True, "manager allowed employee credit")
    for s, name in ((acc, "accounts"), (stk, "stock keeper")):
        check(refused(lambda: s.client.put("/employees/settings", {"credit_allowed": False})) == "403", f"{name} cannot change discount or credit settings")

    # ---- a fully paid purchase at the counter (stock keeper) ----------------------------------------------------------------------------------
    s0 = stock_of(stk.client, B1, SKU)["on_hand_good"]
    sh_s = Shell(stk); sh_s.resize(1366, 768); sh_s.show(); pump(0.3); sh_s.show_page("employees"); pump(1.2)
    sp = sh_s.pages["employees"].items[0][1]
    check("Record purchase" in buttons(sp), "stock keeper can record a purchase")
    p = em.PurchaseDialog(stk); pump(1.2)
    click_submit(p)
    check(p.result() == 0, "purchase without an employee is refused")
    pick(p.emp, "Demo Staff Aisha"); p.loc.setCurrentIndex(p.loc.findData(B1))
    add_line(p.editor, SKU, qty=2); pump(1.5)
    check(p.total > 0 and "Discount" in p.summary.text(), f"live quote shown before saving ({p.summary.text()[-90:]})")
    list_total = p.total
    p.receipt.setText("DEMO-RCPT-1")
    click_submit(p, 2.0)
    check(p.result() == 1, f"paid purchase recorded ({p.banner.text()[:60]})")
    pu1 = next(x for x in stk.client.get("/employees/purchases")["items"] if x["employee"] == "Demo Staff Aisha")
    full = stk.client.get(f"/employees/purchases/{pu1['id']}")
    check(pu1["status"] == "completed" and D(pu1["balance"]) == 0 and D(pu1["discount_total"]) > 0, f"completed, fully paid, discount applied ({pu1['ref']}, discount {pu1['discount_total']})")
    check(D(full["lines"][0]["unit_discount"]) == D(full["lines"][0]["list_price"]) * D("0.20"), "20% rule applied to the list price")
    check(D(pu1["total"]) == list_total, "saved total equals the quote shown on screen")
    check(stock_of(stk.client, B1, SKU)["on_hand_good"] == s0 - 2, "stock reduced by exactly the quantity bought")
    check(full["receipt_ref"] == "DEMO-RCPT-1", "receipt reference kept")

    # ---- employee linked to the stock keeper's own login ----------------------------------------------------------------------------
    p = em.PurchaseDialog(stk); pump(1.2)
    pick(p.emp, "Demo Staff Stock"); p.loc.setCurrentIndex(p.loc.findData(B1)); add_line(p.editor, SKU, qty=1); pump(1.2); click_submit(p, 1.5)
    check(p.result() == 0 and p.banner.text(), f"staff cannot sell to themselves ({p.banner.text()[:80]})")
    p.close()
    over = em.PurchaseDialog(stk); pump(1.2)
    pick(over.emp, "Demo Staff Aisha"); over.loc.setCurrentIndex(over.loc.findData(B1)); add_line(over.editor, SKU, qty=100000); pump(1.0); click_submit(over, 1.5)
    check(over.result() == 0 and over.banner.text(), f"more than is in stock is refused ({over.banner.text()[:80]})")
    over.close()

    # ---- credit purchase: waits for a second person ---------------------------------------------------------------------------------------
    s1 = stock_of(stk.client, B1, SKU)["on_hand_good"]
    p = em.PurchaseDialog(stk); pump(1.2)
    pick(p.emp, "Demo Staff Aisha"); p.loc.setCurrentIndex(p.loc.findData(B1)); add_line(p.editor, SKU, qty=1); pump(1.2); p.now.setText("0"); pump(0.2)
    click_submit(p, 1.5)
    check(p.result() == 1 or "limit" in p.banner.text().lower(), f"credit above the employee's limit is refused ({p.banner.text()[:70]})")
    p.close()
    p = em.PurchaseDialog(stk); pump(1.2)
    pick(p.emp, "Demo Staff Bilal"); p.loc.setCurrentIndex(p.loc.findData(B1)); add_line(p.editor, SKU, qty=2); pump(1.2); p.now.setText("0"); pump(0.2)
    check("on credit" in p.credit_note.text(), f"dialog says the amount would be on credit ({p.credit_note.text()[:60]})")
    click_submit(p, 2.0)
    check(p.result() == 1, f"credit purchase recorded ({p.banner.text()[:60]})")
    pu2 = next(x for x in mgr.client.get("/employees/purchases", status="pending_approval")["items"] if x["employee"] == "Demo Staff Bilal")
    check(stock_of(stk.client, B1, SKU)["on_hand_good"] == s1, "goods are NOT released before approval")
    sd = em.PurchaseDetail(stk, pu2["id"]); pump(1.5)
    check("Approve credit…" not in buttons(sd), "the stock keeper cannot approve credit")
    sd.close()
    md = em.PurchaseDetail(mgr, pu2["id"]); pump(1.5)
    check("Approve credit…" in buttons(md) and "Reject…" in buttons(md), f"manager is offered approve or reject ({buttons(md)})")
    next(b for b in md.findChildren(QPushButton) if b.text() == "Approve credit…").click(); pump(2.0)
    pu2 = mgr.client.get(f"/employees/purchases/{pu2['id']}")
    check(pu2["status"] == "completed" and D(pu2["balance"]) == D(pu2["total"]), f"approved: now completed with the full amount owed ({pu2['balance']})")
    check(stock_of(stk.client, B1, SKU)["on_hand_good"] == s1 - 2, "goods released at approval")
    md.close()
    emp_row = next(e for e in mgr.client.get("/employees", q="Demo Staff Bilal")["items"] if e["full_name"] == "Demo Staff Bilal")
    check(D(emp_row["outstanding"]) == D(pu2["total"]), f"employee shows what they owe ({emp_row['outstanding']})")

    # ---- payment by accounts, then the ledger -----------------------------------------------------------------------------------------------------
    ad = em.PurchaseDetail(acc, pu2["id"]); pump(1.5)
    check("Record payment" in buttons(ad) and "Void purchase…" not in buttons(ad), f"accounts records payments but cannot void ({buttons(ad)})")
    pd = em.PaymentDialog(acc, pu2); pd.amount.setText("1"); click_submit(pd)
    check(pd.result() == 1, "part payment recorded")
    pd = em.PaymentDialog(acc, acc.client.get(f"/employees/purchases/{pu2['id']}")); pd.amount.setText("999999"); click_submit(pd)
    check(pd.result() == 0 and pd.banner.text(), f"payment above the balance is refused ({pd.banner.text()[:70]})")
    pd.close()
    left = D(acc.client.get(f"/employees/purchases/{pu2['id']}")["balance"])
    pd = em.PaymentDialog(acc, acc.client.get(f"/employees/purchases/{pu2['id']}")); pd.amount.setText(str(left)); click_submit(pd)
    after = acc.client.get(f"/employees/purchases/{pu2['id']}")
    check(pd.result() == 1 and D(after["balance"]) == 0, "balance cleared")
    ad.close()
    acc.client.post("/accounting/posting/run")
    integ = acc.client.get("/accounting/integrity")
    rows = {r["name"]: r for r in integ["rows"]}
    emp_row_ledger = next((r for n, r in rows.items() if "Employee" in n), None)
    check(emp_row_ledger is not None and all(r["agrees"] for r in integ["rows"]) and integ["trial_balance"]["balanced"],
          f"ledger agrees with stock, supplier, distributor and employee records ({[(n[:14], r['difference']) for n, r in rows.items()]})")

    # ---- void by someone other than the recorder -----------------------------------------------------------------------------------------------------
    s2 = stock_of(stk.client, B1, SKU)["on_hand_good"]
    vd = em.PurchaseDetail(mgr, pu1["id"]); pump(1.5)
    check("Void purchase…" in buttons(vd), "manager can void a completed purchase")
    vd._void(); pump(1.5)
    check(mgr.client.get(f"/employees/purchases/{pu1['id']}")["status"] == "voided", "purchase voided")
    check(stock_of(stk.client, B1, SKU)["on_hand_good"] == s2 + 2, "voiding returned the goods to stock")
    vd.close()
    acc.client.post("/accounting/posting/run")
    integ = acc.client.get("/accounting/integrity")
    check(all(r["agrees"] for r in integ["rows"]) and integ["trial_balance"]["balanced"], "ledger still agrees after the void")

    # ---- employees tab and purchases tab as manager -------------------------------------------------------------------------------------------------------------
    et.refresh(); pt.refresh(); pump(1.5)
    check(et.table.rowCount() >= 3 and pt.table.rowCount() >= 2, f"lists show employees ({et.table.rowCount()}) and purchases ({pt.table.rowCount()})")

    # ===== ATTENDANCE =====
    sh_a = Shell(mgr); sh_a.resize(1366, 768); sh_a.show(); pump(0.3); sh_a.show_page("attendance"); pump(1.5)
    ap = sh_a.pages["attendance"]
    names = [ap.tabs.tabText(i) for i in range(ap.tabs.count())]
    check(names == ["Daily", "Period summary", "Exceptions", "Corrections", "Import", "Devices and policy"], f"attendance tabs {names}")
    daily, period, exc, corr, imp, setup = (ap.tabs.widget(i) for i in range(ap.tabs.count()))
    setup.refresh(); imp.refresh(); pump(1.2)

    # device + policy
    dd = at.DeviceDialog(mgr, None, setup.adapters); pump(1.0)
    dd.name.setText("Demo punch machine"); dd.loc.setCurrentIndex(dd.loc.findData(B1)); dd.brand.setText("Not yet known"); dd.protocol.setText("File export")
    dd.config.setPlainText('{"columns": {"user_id": "UserID"}}'); click_submit(dd)
    check(dd.result() == 0 and dd.banner.text(), f"incomplete device settings refused ({dd.banner.text()[:70]})")
    dd.config.setPlainText('{"columns": {"user_id": "UserID", "timestamp": "Time", "direction": "Status"}, "timestamp_format": "%Y-%m-%d %H:%M:%S", "timezone": "Asia/Karachi", '
                           '"direction_map": {"in": ["IN"], "out": ["OUT"]}, "api_key": "x"}'); click_submit(dd)
    check(dd.result() == 0, "device settings holding a key or password are refused")
    dd.config.setPlainText('{"columns": {"user_id": "UserID", "timestamp": "Time", "direction": "Status"}, "timestamp_format": "%Y-%m-%d %H:%M:%S", "timezone": "Asia/Karachi", '
                           '"direction_map": {"in": ["IN"], "out": ["OUT"]}}'); click_submit(dd)
    check(dd.result() == 1, "device saved")
    setup.refresh(); imp.refresh(); pump(1.2)

    check(not mgr.client.get("/attendance/policies"), "no policy exists yet; nothing is assumed")
    setup.loc.setCurrentIndex(0); setup._save_policy(); pump(0.6)
    check(not mgr.client.get("/attendance/policies"), "policy without a time zone is refused")
    pick(setup.loc, "DEMO-B1") if any(setup.loc.itemText(i).startswith("DEMO-B1") for i in range(setup.loc.count())) else setup.loc.setCurrentIndex(max(setup.loc.findData(B1), 0))
    setup.tz.setText("Asia/Karachi"); setup.grace.setValue(15); setup.minp.setValue(240); setup.days[6].setChecked(True)
    setup._save_policy(); pump(2.0)
    pols = mgr.client.get("/attendance/policies")
    check(len(pols) == 1 and pols[0]["weekly_off"] == [6] and pols[0]["grace_minutes"] == 15, f"policy saved ({pols})")

    # import
    d1, d2 = last_workdays(2)
    tmp = Path(tempfile.mkdtemp())
    f1 = tmp / "punches_week.csv"
    rows = [("101", f"{d1} 09:02:00", "IN"), ("101", f"{d1} 18:10:00", "OUT"), ("101", f"{d2} 09:00:00", "IN"), ("101", f"{d2} 18:00:00", "OUT"),
            ("102", f"{d1} 09:40:00", "IN"), ("102", f"{d2} 09:05:00", "IN"), ("102", f"{d2} 17:00:00", "OUT"), ("999", f"{d1} 09:00:00", "IN"), ("102", "not a time", "IN")]
    with open(f1, "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["UserID", "Time", "Status"]); w.writerows(rows)
    imp.refresh(); pump(1.0)
    imp.path = str(f1)
    imp.check.setEnabled(True); imp.go.setEnabled(True)
    imp._send(True); pump(2.0)
    check("nothing saved" in imp.result.text().lower() and "1 not readable" in imp.result.text(), f"check-only reads the file, reports the bad row, saves nothing ({imp.result.text()[:130]})")
    check(imp.rejects.rowCount() == 1, "the unreadable row is listed with its reason")
    check(mgr.client.get("/attendance/imports") == [], "dry run stored nothing")
    imp._send(False); pump(2.5)
    hist = mgr.client.get("/attendance/imports")
    check(len(hist) == 1 and hist[0]["rows_total"] == 9 and hist[0]["rows_rejected"] == 1 and hist[0]["rows_unmatched"] == 1, f"import counted correctly ({hist[0] if hist else None})")
    imp._send(False); pump(2.0)
    check(len(mgr.client.get("/attendance/imports")) == 1 and imp.status.text(), f"the same file cannot be imported twice ({imp.status.text()[:80]})")
    f2 = tmp / "punches_overlap.csv"
    with open(f2, "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["UserID", "Time", "Status"]); w.writerows(rows[:2])
    imp.path = str(f2); imp._send(False); pump(2.0)
    h2 = mgr.client.get("/attendance/imports")[0]
    check(h2["rows_new"] == 0 and h2["rows_duplicate"] == 2, f"overlapping export adds no duplicate punches ({h2['rows_new']} new, {h2['rows_duplicate']} duplicate)")
    total_punches = len(mgr.client.get("/attendance/punches", employee_id=aisha["id"], work_date=str(d1)))
    check(total_punches == 2, f"Aisha has exactly her two punches for the day ({total_punches})")

    # summaries, exceptions
    daily.a.setDate(daily.a.date().addDays(-3)); daily.refresh(); pump(1.5)
    rows_ = mgr.client.get("/attendance/days", date_from=str(d2), date_to=str(d1))
    byk = {(r["name"], r["work_date"]): r for r in rows_}
    a1, b1, b2 = byk[("Demo Staff Aisha", str(d1))], byk[("Demo Staff Bilal", str(d1))], byk[("Demo Staff Bilal", str(d2))]
    check(a1["status"] in ("present", "late") and a1["first_in_local"] == "09:02" and a1["last_out_local"] == "18:10", f"full day computed in local time ({a1['status']} {a1['first_in_local']}-{a1['last_out_local']})")
    check(a1["status"] == "present", "09:02 is inside the 15-minute grace, so not late")
    check(b1["status"] == "incomplete" and b1["exception"] == "missing_out", f"check-in without check-out is flagged ({b1['status']}, {b1['exception']})")
    check(b2["status"] == "present" and b2["late_minutes"] == 0, f"09:05 against a 09:00 start is inside the 15-minute grace ({b2['status']}, late {b2['late_minutes']})")
    exc.refresh(); pump(1.5)
    check(exc.table.rowCount() >= 1 and exc.unmatched.rowCount() == 1 and exc.unmatched.row_data(0)["device_user_id"] == "999", "exceptions list the missing out-punch and the unknown device number 999")
    period.refresh(); pump(1.2)
    check(period.table.rowCount() >= 2, "period summary lists employees")

    # late device-id match
    late = mgr.client.post("/employees", {"full_name": "Demo Staff Late", "location_id": B1, "device_user_id": "999", "credit_limit": "0"})
    exc.refresh(); pump(1.2)
    check(exc.unmatched.rowCount() == 0, "adding the employee for device number 999 matched the stored punches")
    check(any(r["name"] == "Demo Staff Late" for r in mgr.client.get("/attendance/days", date_from=str(d1), date_to=str(d1))), "that employee now has a daily summary")

    # corrections
    cd = at.CorrectionDialog(mgr, {"employee_id": bilal["id"], "work_date": str(d1)}); pump(1.5)
    pick(cd.emp, "Demo Staff Bilal"); cd.reason.setText("x"); click_submit(cd)
    check(cd.result() == 0, "correction without a real reason is refused")
    pick(cd.direction, "Check-out"); cd.reason.setText("Forgot to punch out; confirmed by the branch supervisor"); cd.time.setTime(cd.time.time().fromString("18:00", "HH:mm")); click_submit(cd)
    check(cd.result() == 1, "correction requested")
    corr.refresh(); pump(1.2)
    c1 = mgr.client.get("/attendance/corrections", status="pending")[0]
    check(mgr.client.get("/attendance/days", date_from=str(d1), date_to=str(d1), employee_id=bilal["id"])[0]["status"] == "incomplete", "day unchanged until approved")
    select(corr.table, "id", c1["id"]); corr._decide(True); pump(1.5)
    check(mgr.client.get("/attendance/corrections")[0]["status"] == "pending", "the requester cannot approve their own correction")
    cs = Shell(ceo); cs.resize(1366, 768); cs.show(); pump(0.3); cs.show_page("attendance"); pump(1.2)
    corr_c = cs.pages["attendance"].tabs.widget(3); corr_c.refresh(); pump(1.2)
    select(corr_c.table, "id", c1["id"]); corr_c._decide(True); pump(2.0)
    day = mgr.client.get("/attendance/days", date_from=str(d1), date_to=str(d1), employee_id=bilal["id"])[0]
    check(day["status"] != "incomplete" and day["corrected"] and day["last_out_local"] == "18:00", f"approved correction completes the day without touching the raw punches ({day['status']}, out {day['last_out_local']})")
    check(len(mgr.client.get("/attendance/punches", employee_id=bilal["id"], work_date=str(d1))) >= 2, "the original punch is still stored")
    cs.close()

    # audit
    acts = {a["action"] for a in ceo.client.get("/audit", page_size=200)["items"]}
    for want in ("employee_purchase.create", "attendance.import", "attendance_policy.set"):
        check(any(a.startswith(want.split(".")[0]) for a in acts), f"audit trail has {want.split('.')[0]} events")

    # ---- screens at both sizes ----------------------------------------------------------------------------------------------------------------------------------
    for w_, h_ in ((1366, 768), (1920, 1080)):
        for who, key, label_ in ((mgr, "employees", "manager"), (mgr, "attendance", "manager"), (stk, "employees", "stock"), (acc, "employees", "accounts")):
            sh = Shell(who); sh.resize(w_, h_); sh.show(); pump(0.3)
            sh.show_page(key); pg = sh.pages[key]
            for i in range(pg.tabs.count()):
                pg.tabs.setCurrentIndex(i); pump(2.0)
                sh.grab().save(str(out / f"{key}_{label_}_tab{i}_{w_}.png"))
            sh.close()
        full = mgr.client.get(f"/employees/purchases/{pu2['id']}")
        pdlg = em.PurchaseDialog(stk); pump(1.2); pick(pdlg.emp, "Demo Staff Aisha"); pdlg.loc.setCurrentIndex(pdlg.loc.findData(B1)); add_line(pdlg.editor, SKU, qty=3); pump(1.8)
        dlgs = {"employee_dialog": em.EmployeeDialog(mgr), "purchase_dialog": pdlg, "purchase_detail": em.PurchaseDetail(mgr, pu2["id"]), "rule_dialog": em.RuleDialog(mgr),
                "payment_dialog": em.PaymentDialog(acc, {**full, "balance": "100"}), "correction_dialog": at.CorrectionDialog(mgr, None),
                "device_dialog": at.DeviceDialog(mgr, None, setup.adapters)}
        for name, dlg in dlgs.items():
            dlg.resize(min(max(dlg.sizeHint().width(), dlg.minimumWidth()) + 20, w_ - 40), min(dlg.sizeHint().height() + 40, h_ - 60))
            dlg.show(); pump(1.8); dlg.grab().save(str(out / f"{name}_{w_}.png")); dlg.close()

    print("\nFAILED CHECKS:" if E.FAILS else "\nALL CHECKS PASSED", *E.FAILS, sep="\n  ")
    sys.stdout.flush()
    # PySide aborts with a heap error while tearing down the many windows this script opens, after every check has already run.
    # Leave without the teardown so the exit status reports the checks, not the shutdown.
    import os
    os._exit(1 if E.FAILS else 0)


if __name__ == "__main__":
    main()
