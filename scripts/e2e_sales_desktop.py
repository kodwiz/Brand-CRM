"""Phase 4 desktop check: drive the REAL Customers and Orders screens against the REAL running backend (demo environment).

    python scripts/e2e_sales_desktop.py CREDENTIALS_FILE OUTDIR

Reuses the helpers of scripts/e2e_desktop.py. Exits non-zero on any failed check."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_desktop as E  # noqa: E402  (builds the Qt application)
from e2e_desktop import add_line, check, click_submit, creds, pump, sess  # noqa: E402
from PySide6.QtWidgets import QPushButton  # noqa: E402

from desktop.pages import sales as sl  # noqa: E402
from desktop.ui.shell import Shell  # noqa: E402


def buttons(w) -> list[str]:
    return [b.text() for b in w.findChildren(QPushButton) if b.isVisibleTo(w)]


def press(w, text: str, wait: float = 1.5) -> None:
    next(b for b in w.findChildren(QPushButton) if b.text() == text and b.isVisibleTo(w)).click()
    pump(wait)


def stock(client, loc, sku):
    return E.stock_of(client, loc, sku)


def main() -> None:
    cred = creds(sys.argv[1])
    out = Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
    ceo, mgr, stk, bk, acc = (sess(u, cred[u]) for u in ("demo.ceo", "demo.manager", "demo.stock", "demo.booker", "demo.accounts"))
    B1 = next(l["id"] for l in ceo.client.get("/locations") if l["code"] == "DEMO-B1")
    ceo.client.put("/sales/settings", {"max_manual_discount_pct": "5", "tax_rate_pct": "0", "return_window_days": 0})

    # ---- permissions as seen on screen -------------------------------------------------------------------------
    sh = Shell(bk); sh.resize(1366, 768); sh.show(); pump(0.3)
    check("orders" in sh.buttons and "customers" in sh.buttons and "suppliers" not in sh.buttons, "booker sees Orders and Customers, not Suppliers")
    sh.show_page("orders"); pump(1.5)
    tabs = [sh.pages["orders"].tabs.tabText(i) for i in range(sh.pages["orders"].tabs.count())]
    check(tabs == ["Orders", "Returns", "My sales"], f"booker tabs {tabs}")
    sh.close()
    sh = Shell(stk); sh.resize(1366, 768); sh.show(); pump(0.3)
    sh.show_page("orders"); pump(1.5)
    tabs = [sh.pages["orders"].tabs.tabText(i) for i in range(sh.pages["orders"].tabs.count())]
    check("Parcels" in tabs and "My sales" not in tabs, f"stock keeper tabs {tabs}")
    check("customers" not in sh.buttons, "stock keeper has no Customers module")
    sh.close()

    # ---- customer ---------------------------------------------------------------------------------------------------
    d = sl.CustomerDialog(bk)
    d.name.setText("Demo Customer One"); d.phone.setText("0300 7000001"); d.address.setText("12 Demo Road"); d.city.setText("Demoville")
    click_submit(d)
    cust = bk.client.get("/customers", q="Demo Customer One")["items"][0]
    check(d.result() == 1 and cust["phone"] == "0300 7000001", "customer created")
    d = sl.CustomerDialog(bk); d.name.setText("Dup"); d.phone.setText("+92 300 7000001"); click_submit(d)
    check(d.result() == 0 and "already" in d.banner.text().lower(), f"duplicate phone refused inline ({d.banner.text()[:70]})")
    d.close()
    d = sl.CustomerDialog(bk); d.name.setText(""); click_submit(d)
    check(d.result() == 0 and d.form.errors["name"].isVisibleTo(d) is not None and d.form.errors["name"].text(), "empty name shows field error")
    d.close()

    # ---- order entry (booker) ---------------------------------------------------------------------------------------
    before = stock(ceo.client, B1, "DEMO-LIP-01")["available"]
    od = sl.OrderDialog(bk); pump(1)
    od.customer.edit.setText("Demo Customer One"); od.customer.search(); pump(1)
    od.channel.setCurrentIndex(od.channel.findText("WhatsApp"))
    od.loc.setCurrentIndex(od.loc.findData(B1))
    od.d_name.setText("Demo Recipient"); od.d_addr.setText("12 Demo Road"); od.d_city.setText("Demoville")
    add_line(od.editor, "DEMO-LIP-01", qty=3)
    od._preview(); pump(1.2)
    check("total" in od.preview.text() and "NEEDS APPROVAL" not in od.preview.text().upper(), f"price preview shown ({od.preview.text()[:90]})")
    od.confirm_btn.click(); pump(2)
    o1 = bk.client.get("/orders")["items"][0]
    check(od.result() == 1 and o1["status"] == "confirmed", f"order saved and confirmed ({o1['ref']} {o1['status']})")
    check(stock(ceo.client, B1, "DEMO-LIP-01")["available"] == before - 3, "confirming reserved 3 units")

    # ---- manual discount needs approval ------------------------------------------------------------------------------
    od = sl.OrderDialog(bk, customer=cust); pump(1)
    od.channel.setCurrentIndex(od.channel.findText("Instagram")); od.loc.setCurrentIndex(od.loc.findData(B1))
    add_line(od.editor, "DEMO-LIP-01", qty=1, unit_price="1")
    od._preview(); pump(1.2)
    check("needs approval" in od.preview.text().lower(), "preview warns that the discount needs approval")
    click_submit(od, 2)
    o2 = next(x for x in bk.client.get("/orders")["items"] if x["status"] == "draft")
    check(o2["discount_approval"] == "pending", "draft saved with discount pending")
    det = sl.OrderDetail(bk, o2["id"]); pump(1.2)
    check("Confirm order" in buttons(det) and "Decide discount" not in buttons(det), "creator can confirm but not decide their own discount")
    press(det, "Confirm order")
    check("approval" in det.status.text().lower() or "approved" in det.status.text().lower(), f"confirm refused until approved ({det.status.text()[:80]})")
    det.close()
    md = sl.OrderDetail(mgr, o2["id"]); pump(1.2)
    check("Decide discount" in buttons(md), "manager is offered Decide discount")
    md.close()
    mgr.client.post(f"/orders/{o2['id']}/discount-decision", {"approve": False, "note": "too deep for demo"})
    bk.client.post(f"/orders/{o2['id']}/cancel", {"reason": "demo cleanup"})

    # ---- fulfilment (stock keeper) --------------------------------------------------------------------------------------
    sd = sl.OrderDetail(stk, o1["id"]); pump(1.2)
    check("Record payment" not in buttons(sd) and "Pack items" in buttons(sd), f"stock keeper can pack, cannot take payment ({buttons(sd)[:6]})")
    check("not shown" in sd.money.text().lower(), "stock keeper sees no prices or totals")
    sd.grab().save(str(out / "order_fulfil_view_1366.png"))
    sd.close()
    pd = sl.PackDialog(stk, stk.client.get(f"/orders/{o1['id']}")); pump(0.5)
    pd.rows[0][1].setValue(2)
    click_submit(pd)
    full = ceo.client.get(f"/orders/{o1['id']}")
    ship = full["shipments"][0]
    check(pd.result() == 1 and ship["status"] == "packed" and ship["lines"][0]["qty"] == 2, "partial parcel of 2 packed")
    cd = sl.CourierDialog("Dispatch", "Dispatch", lambda b: stk.client.post(f"/shipments/{ship['id']}/dispatch", b)); cd.courier.setText("Demo Courier")
    click_submit(cd)
    before_oh = stock(ceo.client, B1, "DEMO-LIP-01")["on_hand_good"]
    check(ceo.client.get(f"/orders/{o1['id']}")["shipments"][0]["status"] == "dispatched", "parcel dispatched")
    od_ = sl.OutcomeDialog(stk, ceo.client.get(f"/orders/{o1['id']}")["shipments"][0]); od_.result.setCurrentIndex(0); click_submit(od_)
    check(ceo.client.get(f"/orders/{o1['id']}")["status"] == "partially_delivered", "order partially delivered")
    pd = sl.PackDialog(stk, stk.client.get(f"/orders/{o1['id']}")); click_submit(pd)
    s2 = ceo.client.get(f"/orders/{o1['id']}")["shipments"][1]
    stk.client.post(f"/shipments/{s2['id']}/dispatch", {})
    stk.client.post(f"/shipments/{s2['id']}/outcome", {"result": "failed", "reason": "customer not home"})
    ff = sl.OrderDetail(stk, o1["id"]); pump(1.2)
    check("Receive back" in buttons(ff), "failed parcel can be received back")
    ff.close()
    stk.client.post(f"/shipments/{s2['id']}/receive-back")
    integ = ceo.client.get("/inventory/integrity")["consistent"]
    check(integ, "stock ledger reconciles after dispatch, failure and receive-back")

    # ---- payment (accounts) -----------------------------------------------------------------------------------------------
    o_acc = acc.client.get(f"/orders/{o1['id']}")
    rd = sl.ReceiptDialog(acc, o_acc); rd.amount.setText("999999"); click_submit(rd)
    check(rd.result() == 0 and "owed" in rd.banner.text().lower() or "more than" in rd.banner.text().lower(), f"overpayment refused inline ({rd.banner.text()[:70]})")
    rd.close()
    rd = sl.ReceiptDialog(acc, o_acc); rd.amount.setText("50"); click_submit(rd)
    check(rd.result() == 1 and acc.client.get(f"/orders/{o1['id']}")["payment_status"] == "partially_paid", "partial payment recorded")

    # ---- return -----------------------------------------------------------------------------------------------------------------
    rr = sl.ReturnRequestDialog(bk, bk.client.get(f"/orders/{o1['id']}")); pump(0.4)
    rr.rows[0][1].setValue(1); rr.reason.setText("Wrong shade"); click_submit(rr)
    ret = bk.client.get("/sales-returns")["items"][0]
    check(rr.result() == 1 and ret["status"] == "requested", "return requested")
    rdt = sl.ReturnDetail(bk, ret["id"]); pump(1.2)
    check("Approve or reject" not in buttons(rdt), "requester cannot approve their own return")
    rdt.close()
    mgr.client.post(f"/sales-returns/{ret['id']}/decide", {"approve": True, "note": "ok"})
    ret = stk.client.get(f"/sales-returns/{ret['id']}")
    avail0 = stock(ceo.client, B1, "DEMO-LIP-01")["available"]
    rv = sl.ReceiveReturnDialog(stk, ret); click_submit(rv)
    check(stock(ceo.client, B1, "DEMO-LIP-01")["available"] == avail0, "received return is not yet saleable")
    ret = stk.client.get(f"/sales-returns/{ret['id']}")
    ins = sl.InspectReturnDialog(stk, ret); ins.rows[0][1].setValue(0); ins.rows[0][2].setValue(1); ins.rows[0][4].setValue(1); click_submit(ins)
    check(stock(ceo.client, B1, "DEMO-LIP-01")["available"] == avail0, "damaged unit did not return to saleable stock")
    ret = acc.client.get(f"/sales-returns/{ret['id']}")
    st = sl.SettleReturnDialog(acc, ret); pump(0.2); click_submit(st)
    check(acc.client.get(f"/sales-returns/{ret['id']}")["status"] == "settled", "return settled")
    check(ceo.client.get("/inventory/integrity")["consistent"] and ceo.client.get("/sales/integrity")["consistent"], "stock and sales integrity checks pass")

    # ---- my sales + screenshots at both sizes ------------------------------------------------------------------------------------
    ms = sl.MySalesTab(bk); ms.refresh(); pump(1.2)
    check("orders" in ms.total.text(), f"booker's own summary ({ms.total.text()[:70]})")
    new_ord = acc.client.get(f"/orders/{o1['id']}")
    for w_, h_ in ((1366, 768), (1920, 1080)):
        for who, label_ in ((ceo, "ceo"), (bk, "booker"), (stk, "stock")):
            sh = Shell(who); sh.resize(w_, h_); sh.show(); pump(0.3)
            for key in ("orders", "customers"):
                if key not in sh.buttons:
                    continue
                sh.show_page(key); pg = sh.pages[key]
                tabs = getattr(pg, "tabs", None)
                for i in range(tabs.count() if tabs else 1):
                    if tabs:
                        tabs.setCurrentIndex(i)
                    pump(1.6)
                    sh.grab().save(str(out / f"{key}_{label_}_tab{i}_{w_}.png"))
            sh.close()
        dlgs = {"order_detail": sl.OrderDetail(ceo, o1["id"]), "order_dialog": sl.OrderDialog(ceo), "customer_dialog": sl.CustomerDialog(ceo),
                "return_detail": sl.ReturnDetail(ceo, ret["id"]), "customer_detail": sl.CustomerDetail(ceo, cust["id"]),
                "payment_dialog": sl.ReceiptDialog(ceo, new_ord), "return_request": sl.ReturnRequestDialog(ceo, new_ord)}
        for name, dlg in dlgs.items():
            dlg.resize(min(dlg.sizeHint().width() + 40, w_ - 40), min(dlg.sizeHint().height() + 40, h_ - 60))
            dlg.show(); pump(1.6); dlg.grab().save(str(out / f"{name}_{w_}.png")); dlg.close()

    print("\nFAILED CHECKS:" if E.FAILS else "\nALL CHECKS PASSED", *E.FAILS, sep="\n  ")
    sys.exit(1 if E.FAILS else 0)


if __name__ == "__main__":
    main()
