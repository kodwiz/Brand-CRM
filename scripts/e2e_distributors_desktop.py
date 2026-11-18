"""Phase 5 desktop check: drive the REAL Distributors screens against the REAL running backend (demo environment).

    python scripts/e2e_distributors_desktop.py CREDENTIALS_FILE OUTDIR

Reuses the helpers of scripts/e2e_desktop.py. Exits non-zero on any failed check."""
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_desktop as E  # noqa: E402  (builds the Qt application)
from e2e_desktop import add_line, check, click_submit, creds, pump, sess  # noqa: E402
from PySide6.QtWidgets import QPushButton  # noqa: E402

from desktop.pages import distributors as dp  # noqa: E402
from desktop.ui.shell import Shell  # noqa: E402


def buttons(w) -> list[str]:
    return [b.text() for b in w.findChildren(QPushButton) if b.isVisibleTo(w)]


def main() -> None:
    cred = creds(sys.argv[1])
    out = Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
    ceo, mgr, stk, bk, acc = (sess(u, cred[u]) for u in ("demo.ceo", "demo.manager", "demo.stock", "demo.booker", "demo.accounts"))
    B1 = next(l["id"] for l in ceo.client.get("/locations") if l["code"] == "DEMO-B1")
    var = ceo.client.get("/variants/lookup", q="DEMO-LIP-01")[0]
    ceo.client.patch(f"/variants/{var['id']}", {"distributor_price": "100.00"})
    ceo.client.put("/distributors/settings", {"backdate_days": 7, "max_extra_discount_pct": "0"})

    def avail():
        return E.stock_of(ceo.client, B1, "DEMO-LIP-01")["available"]

    # ---- who sees what -------------------------------------------------------------------------------------------------------
    sh = Shell(bk); sh.resize(1366, 768); sh.show(); pump(0.3)
    check("distributors" not in sh.buttons, "booker has no Distributors module")
    sh.close()
    sh = Shell(stk); sh.resize(1366, 768); sh.show(); pump(0.3)
    check("distributors" in sh.buttons, "stock keeper sees Distributors")
    sh.show_page("distributors"); pump(1.5)
    pg = sh.pages["distributors"]
    check(type(pg).__name__ == "DistributorsPage", "Distributors is a real screen, not the placeholder")
    names = [pg.tabs.tabText(i) for i in range(pg.tabs.count())]
    check(names == ["Accounts", "Dispatches", "Returns", "Payments", "Adjustments", "Aging"], f"tabs {names}")
    check("New distributor" not in buttons(pg), "stock keeper cannot create distributors")
    sh.close()

    # ---- create a distributor (manager) ----------------------------------------------------------------------------------------
    d = dp.DistributorDialog(mgr); pump(0.8)
    d.name.setText(""); click_submit(d)
    check(d.result() == 0 and d.form.errors["name"].text(), "empty name shows a field error")
    d.name.setText("Demo Distributor Alpha"); d.contact.setText("Demo Contact"); d.phone.setText("0300 5550001"); d.city.setText("Demoville")
    d.discount.setText("10"); d.limit.setText("50000"); d.terms.setValue(30)
    i = d.keeper.findData(stk.user["id"])
    if i >= 0:
        d.keeper.setCurrentIndex(i)
    click_submit(d)
    dist = mgr.client.get("/distributors", q="Alpha")["items"][0]
    check(d.result() == 1 and dist["credit_limit"] == "50000.00", f"distributor created ({dist['code']})")
    d2 = dp.DistributorDialog(mgr); d2.name.setText("demo distributor alpha"); click_submit(d2)
    check(d2.result() == 0 and "already" in d2.banner.text().lower(), f"duplicate name refused ({d2.banner.text()[:60]})")
    d2.close()
    did = dist["id"]

    # ---- agreed price and opening balance --------------------------------------------------------------------------------------------
    p = dp.PriceDialog(mgr, dist); p.item.edit.setText("DEMO-LIP-01"); p.item.search(); pump(0.9); p.price.setText("90"); click_submit(p)
    check(p.result() == 1 and mgr.client.get(f"/distributors/{did}/prices")[0]["price"].startswith("90"), "agreed price saved")
    o = dp.OpeningDialog(mgr, dist); o.amount.setText("1000"); o.asof.setDate(o.asof.date().addDays(-60)); click_submit(o)
    check(o.result() == 1 and mgr.client.get(f"/distributors/{did}")["balance"] == "1000.00", "opening balance posted")
    o = dp.OpeningDialog(mgr, dist); o.amount.setText("5"); click_submit(o)
    check(o.result() == 0 and "already" in o.banner.text().lower(), f"second opening balance refused ({o.banner.text()[:60]})")
    o.close()

    # ---- dispatch on credit (stock keeper) ----------------------------------------------------------------------------------------------
    a0 = avail()
    dd = dp.DispatchDialog(stk, did); pump(1.2)
    dd.loc.setCurrentIndex(dd.loc.findData(B1))
    add_line(dd.editor, "DEMO-LIP-01", qty=5)
    click_submit(dd, 2)
    inv = stk.client.get("/distributor-dispatches", distributor_id=did)["items"][0]
    check(dd.result() == 1 and inv["status"] == "posted" and inv["total"] == "405.00", f"dispatch posted at agreed price less terms ({inv['total']})")
    check(avail() == a0 - 5, "stock left the location")
    check(mgr.client.get(f"/distributors/{did}")["balance"] == "1405.00", "account charged exactly once")
    dv = dp.DispatchDetail(stk, inv["id"]); pump(1.2)
    check("Approve or reject…" not in buttons(dv) and "Cancel dispatch" not in buttons(dv), f"posted dispatch offers no state changes ({buttons(dv)})")
    dv.close()

    # ---- credit limit hold and approval -----------------------------------------------------------------------------------------------------
    mgr.client.put(f"/distributors/{did}", {"credit_limit": "1500"})
    dd = dp.DispatchDialog(stk, did); pump(1.2)
    dd.loc.setCurrentIndex(dd.loc.findData(B1)); add_line(dd.editor, "DEMO-LIP-01", qty=2)
    click_submit(dd, 2)
    held = stk.client.get("/distributor-dispatches", distributor_id=did, status="held")["items"][0]
    check(dd.result() == 1 and held["status"] == "held" and avail() == a0 - 5, "dispatch over the limit is held; no stock moved")
    check(mgr.client.get(f"/distributors/{did}")["balance"] == "1405.00", "held dispatch owes nothing")
    sv = dp.DispatchDetail(stk, held["id"]); pump(1.2)
    check("Approve or reject…" not in buttons(sv), "stock keeper cannot decide")
    check("limit" in sv.holds.text().lower(), f"hold reason shown ({sv.holds.text()[:70]})")
    sv.close()
    mv = dp.DispatchDetail(mgr, held["id"]); pump(1.2)
    check("Approve or reject…" in buttons(mv), "manager is offered the decision")
    mv.grab().save(str(out / "held_dispatch_detail.png")); mv.close()
    mgr.client.post(f"/distributor-dispatches/{held['id']}/decide", {"approve": True, "note": "good payer, demo"})
    check(avail() == a0 - 7 and mgr.client.get(f"/distributors/{did}")["balance"] == "1567.00", "approval posted stock and charge (1000 + 405 + 162)")
    mgr.client.put(f"/distributors/{did}", {"credit_limit": "50000"})

    # ---- payment (accounts), oldest first ---------------------------------------------------------------------------------------------------------
    pd = dp.PaymentDialog(acc, did); pump(1.5)
    pd.amount.setText("1200"); pd.auto.setChecked(True); click_submit(pd, 2)
    check(pd.result() == 1 and acc.client.get(f"/distributors/{did}")["balance"] == "367.00", "payment recorded; balance 367.00")
    ag = acc.client.get(f"/distributors/{did}/aging")
    check(Decimal(ag["buckets"]["31_60"]) == Decimal(0) and Decimal(ag["unallocated_credit"]) == 0, f"oldest (opening) invoice settled first ({ag['buckets']})")
    pd = dp.PaymentDialog(acc, did); pump(1.5)
    pd.amount.setText("100"); click_submit(pd, 2)
    pay = acc.client.get("/distributor-payments", distributor_id=did)["items"][0]
    check(pay["unallocated"] == "100.00", "payment with no allocation stays on account")
    pdv = dp.PaymentDetail(acc, pay["id"]); pump(1.2)
    check("Allocate…" in buttons(pdv) and "Reverse payment" not in buttons(pdv), f"accounts can allocate but not reverse ({buttons(pdv)})")
    pdv.close()
    al = dp.AllocateDialog(acc, acc.client.get(f"/distributor-payments/{pay['id']}")); pump(1.2)
    al.alloc.rows[0][1].setText("100"); click_submit(al)
    check(al.result() == 1 and acc.client.get(f"/distributor-payments/{pay['id']}")["unallocated"] == "0.00", "allocated later to a dispatch")

    # ---- return and credit ---------------------------------------------------------------------------------------------------------------------------
    rd = dp.ReturnDialog(stk, did); pump(1.5)
    rd.loc.setCurrentIndex(rd.loc.findData(B1)); rd.reason.setText("Unsold demo stock")
    rd.rows[0][1].setValue(2); click_submit(rd)
    ret = stk.client.get("/distributor-returns", distributor_id=did)["items"][0]
    check(rd.result() == 1 and ret["status"] == "requested", "return requested")
    a1 = avail()
    rv = dp.ReturnDetail(stk, ret["id"]); pump(1.2)
    check("Receive goods" in buttons(rv) and "Approve or reject credit…" not in buttons(rv), f"stock keeper receives, does not approve ({buttons(rv)})")
    rv.close()
    rc = dp.ReceiveReturnDialog(stk, stk.client.get(f"/distributor-returns/{ret['id']}")); click_submit(rc)
    check(avail() == a1, "received goods are not saleable before inspection")
    ins = dp.InspectDialog(stk, stk.client.get(f"/distributor-returns/{ret['id']}"))
    ins.rows[0][1].setValue(1); ins.rows[0][2].setValue(1); click_submit(ins)
    check(avail() == a1 + 1 and stk.client.get(f"/distributor-returns/{ret['id']}")["status"] == "inspected", "one unit restocked, one damaged")
    rv = dp.ReturnDetail(mgr, ret["id"]); pump(1.2)
    check("Approve or reject credit…" in buttons(rv), "manager is offered the credit decision")
    rv.close()
    bal0 = Decimal(mgr.client.get(f"/distributors/{did}")["balance"])
    mgr.client.post(f"/distributor-returns/{ret['id']}/decide", {"approve": True, "note": "demo credit"})
    check(Decimal(mgr.client.get(f"/distributors/{did}")["balance"]) == bal0 - Decimal("162.00"), "credit note 2 x 81 applied to the account")

    # ---- adjustment ---------------------------------------------------------------------------------------------------------------------------------------------------
    ad = dp.AdjustmentDialog(stk, did); pump(1.2)
    ad.kind.setCurrentIndex(ad.kind.findData("write_off")); ad.inv.setCurrentIndex(1); ad.amount.setText("5"); ad.reason.setText("Demo small balance"); click_submit(ad)
    check(ad.result() == 1, f"adjustment requested ({ad.banner.text()[:80]})")
    adj = stk.client.get("/distributor-adjustments", distributor_id=did)["items"][0]
    check(adj["status"] == "pending", "adjustment waits for approval")
    mgr.client.post(f"/distributor-adjustments/{adj['id']}/decide", {"approve": True, "note": "demo"})

    # ---- statement, integrity ----------------------------------------------------------------------------------------------------------------------------------------
    sd = dp.StatementDialog(mgr, mgr.client.get(f"/distributors/{did}")); pump(1.8)
    check("closing" in sd.summary.text() and "DOES NOT RECONCILE" not in sd.summary.text(), f"statement reconciles ({sd.summary.text()[:90]})")
    sd.grab().save(str(out / "statement_dialog.png")); sd.close()
    check(ceo.client.get("/distributors/integrity")["consistent"] and ceo.client.get("/inventory/integrity")["consistent"], "distributor and stock integrity checks pass")

    # ---- screens at both sizes -----------------------------------------------------------------------------------------------------------------------------------------------
    for w_, h_ in ((1366, 768), (1920, 1080)):
        for who, label_ in ((ceo, "ceo"), (stk, "stock"), (acc, "accounts")):
            sh = Shell(who); sh.resize(w_, h_); sh.show(); pump(0.3)
            sh.show_page("distributors"); pg = sh.pages["distributors"]
            for i in range(pg.tabs.count()):
                pg.tabs.setCurrentIndex(i); pump(2.0)
                sh.grab().save(str(out / f"distributors_{label_}_tab{i}_{w_}.png"))
            sh.close()
        dlgs = {"account_detail": dp.AccountDetail(ceo, did), "dispatch_dialog": dp.DispatchDialog(ceo, did), "payment_dialog": dp.PaymentDialog(ceo, did),
                "return_dialog": dp.ReturnDialog(ceo, did), "distributor_dialog": dp.DistributorDialog(ceo, dist), "adjustment_dialog": dp.AdjustmentDialog(ceo, did),
                "return_detail": dp.ReturnDetail(ceo, ret["id"]), "payment_detail": dp.PaymentDetail(ceo, pay["id"]), "dispatch_detail": dp.DispatchDetail(ceo, inv["id"])}
        for name, dlg in dlgs.items():
            dlg.resize(min(max(dlg.sizeHint().width(), dlg.minimumWidth()) + 20, w_ - 40), min(dlg.sizeHint().height() + 40, h_ - 60))
            dlg.show(); pump(1.8); dlg.grab().save(str(out / f"{name}_{w_}.png")); dlg.close()

    print("\nFAILED CHECKS:" if E.FAILS else "\nALL CHECKS PASSED", *E.FAILS, sep="\n  ")
    sys.exit(1 if E.FAILS else 0)


if __name__ == "__main__":
    main()
