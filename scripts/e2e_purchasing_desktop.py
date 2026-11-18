"""Phase 3 desktop check: drive the REAL Suppliers screens against the REAL running backend (demo environment).

    python scripts/e2e_purchasing_desktop.py CREDENTIALS_FILE OUTDIR

Reuses the helpers of scripts/e2e_desktop.py. Exits non-zero on any failed check."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_desktop as E  # noqa: E402  (builds the Qt application)
from e2e_desktop import add_line, check, click_submit, creds, pump, sess  # noqa: E402
from PySide6.QtWidgets import QPushButton  # noqa: E402

from desktop.pages import suppliers as sp  # noqa: E402
from desktop.ui.shell import Shell  # noqa: E402


def buttons(w) -> list[str]:
    return [b.text() for b in w.findChildren(QPushButton) if b.isVisibleTo(w)]


def main() -> None:
    cred = creds(sys.argv[1])
    out = Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
    acc, mgr, stock, ceo, booker = (sess(u, cred[u]) for u in ("demo.accounts", "demo.manager", "demo.stock", "demo.ceo", "demo.booker"))
    WH = next(l["id"] for l in ceo.client.get("/locations") if l["code"] == "DEMO-WH")

    # ---- permissions as seen on screen ------------------------------------------------------------------------
    sh = Shell(stock); sh.resize(1366, 768); sh.show(); pump(0.3)
    sh.show_page("suppliers"); pump(1.5)
    tabs = [sh.pages["suppliers"].tabs.tabText(i) for i in range(sh.pages["suppliers"].tabs.count())]
    check("Orders" in tabs and "Invoices" not in tabs and "Payments" not in tabs and "Payables" not in tabs,
          f"stock keeper sees orders only, no invoices, payments or balances ({tabs})")
    sh.close()
    check("suppliers" not in Shell(booker).buttons, "order booker has no Suppliers module")

    # ---- supplier -----------------------------------------------------------------------------------------------
    d = sp.SupplierDialog(acc)
    d.name.setText("Demo Beauty Imports"); d.terms.setValue(30); click_submit(d)
    s = next(x for x in acc.client.get("/suppliers")["items"] if x["name"] == "Demo Beauty Imports")
    check(d.result() == 1 and s["payment_terms_days"] == 30 and s["balance"] == "0.00", "supplier created with 30-day terms and zero balance")
    d = sp.SupplierDialog(acc); d.name.setText("demo beauty imports"); click_submit(d)
    check(d.result() == 0 and "already" in d.banner.text().lower(), f"duplicate supplier name refused inline ({d.banner.text()[:60]})")
    d.close()

    # ---- purchase order (accounts creates, manager approves) ---------------------------------------------------
    p = sp.POEditDialog(acc); pump(1)
    p.supplier.setCurrentIndex(p.supplier.findData(s["id"])); p.loc.setCurrentIndex(p.loc.findData(WH))
    add_line(p.editor, "DEMO-LIP-01", qty_ordered=40, unit_cost="400")
    add_line(p.editor, "DEMO-LIP-02", qty_ordered=10, unit_cost="300")
    click_submit(p)
    po = acc.client.get("/purchase-orders", supplier_id=s["id"])["items"][0]
    check(po["status"] == "draft" and po["total"] == "19000.00", f"PO {po['ref']} saved as draft, total {po['total']}")
    own = sp.PODetail(acc, po["id"]); pump(1.2)
    check(not any(t == "Approve order" for t in buttons(own)), "creator is not offered Approve on their own order")
    own.close()
    md = sp.PODetail(mgr, po["id"]); pump(1.2)
    check("Approve order" in buttons(md), "manager is offered Approve")
    [b for b in md.findChildren(QPushButton) if b.text() == "Approve order"][0].click(); pump(1.5)
    check(mgr.client.get(f"/purchase-orders/{po['id']}")["status"] == "approved", "manager approved the order")
    md.close()

    # ---- receiving against the order (stock keeper) -------------------------------------------------------------
    sd = sp.PODetail(stock, po["id"]); pump(1.2)
    check("Enter invoice" not in buttons(sd), "stock keeper cannot enter invoices from the order")
    sd.close()
    po_full = stock.client.get(f"/purchase-orders/{po['id']}")
    check("unit_cost" not in po_full["lines"][0], "stock keeper never receives cost data from the order")
    r = sp.POReceiveDialog(stock, po_full)
    r.rows[0][1].setValue(25); r.rows[0][2].setValue(2); r.rows[0][3].setValue(1)     # LIP-01: 25 ok, 2 damaged, 1 rejected
    r.rows[1][1].setValue(10)
    click_submit(r)
    got = acc.client.get(f"/purchase-orders/{po['id']}")
    check(r.result() == 1 and got["status"] == "partially_received", f"receipt posted against the order ({got['status']})")
    l1 = got["lines"][0]
    check((l1["qty_accepted"], l1["qty_damaged"], l1["qty_rejected"], l1["qty_remaining"]) == (25, 2, 1, 13), f"line 1 quantities {(l1['qty_accepted'], l1['qty_damaged'], l1['qty_rejected'], l1['qty_remaining'])}")
    check(acc.client.get("/supplier-invoices", supplier_id=s["id"])["total"] == 0 and acc.client.get(f"/suppliers/{s['id']}")["balance"] == "0.00",
          "receiving created no supplier liability")
    over = sp.POReceiveDialog(stock, stock.client.get(f"/purchase-orders/{po['id']}"))
    over.rows[0][1].setValue(20); click_submit(over)
    check(over.result() == 0 and "ordered" in over.banner.text(), f"over-receipt refused inline ({over.banner.text()[:70]})")
    over.close()

    # ---- invoice with a price variance: held, then approved by someone else ------------------------------------
    inv = sp.InvoiceDialog(acc, acc.client.get(f"/purchase-orders/{po['id']}")); pump(1)
    inv.inv_no.setText("DB-1001")
    for ref, q, price, _ in inv.rows:
        if ref["po_line_id"] == po["lines"][0]["id"]:
            price.setText("440")
    click_submit(inv)
    held = next(i for i in acc.client.get("/supplier-invoices", supplier_id=s["id"])["items"] if i["invoice_no"] == "DB-1001")
    check(held["status"] == "held" and held["match_status"] == "variance", f"price variance holds the invoice ({held['status']}/{held['match_status']})")
    check(acc.client.get(f"/suppliers/{s['id']}")["balance"] == "0.00", "held invoice created no liability")
    hd = sp.InvoiceDetail(acc, held["id"]); pump(1.2)
    check("Approve variance" not in buttons(hd) and "Pay this invoice" not in buttons(hd), "entering user can neither approve nor pay a held invoice")
    hd.grab().save(str(out / "invoice_held_1366.png")); hd.close()
    md = sp.InvoiceDetail(mgr, held["id"]); pump(1.2)
    check("Approve variance" in buttons(md) and "Reject invoice" in buttons(md), "manager is offered approve / reject")
    md.close()
    mgr.client.post(f"/supplier-invoices/{held['id']}/decide", {"approve": True, "note": "price rise agreed by phone"})
    bal = acc.client.get(f"/suppliers/{s['id']}")["balance"]
    inv_total = float(acc.client.get(f"/supplier-invoices/{held['id']}")["total"])
    check(float(bal) == inv_total and inv_total > 0, f"liability created exactly once at posting ({bal})")

    # ---- partial payment ------------------------------------------------------------------------------------------
    pay = sp.PaymentDialog(acc, s["id"]); pump(1.5)
    check(len(pay.rows) == 1, "payment dialog lists the open invoice")
    pay.rows[0][1].setText("5000"); pay.amount.setText("5000"); pay.reference.setText("TRX-1")
    click_submit(pay)
    after = acc.client.get(f"/supplier-invoices/{held['id']}")
    check(after["payment_status"] == "partially_paid" and float(after["outstanding"]) == inv_total - 5000, f"partial payment: outstanding {after['outstanding']}")
    over = sp.PaymentDialog(acc, s["id"], after); pump(1.5)
    over.rows[0][1].setText(str(inv_total)); over.amount.setText(str(inv_total)); click_submit(over)
    check(over.result() == 0 and "outstanding" in over.banner.text(), f"overpayment refused inline ({over.banner.text()[:60]})")
    over.close()

    # ---- return + credit --------------------------------------------------------------------------------------------
    rt = sp.ReturnDialog(acc); pump(1)
    rt.supplier.setCurrentIndex(rt.supplier.findData(s["id"])); rt.loc.setCurrentIndex(rt.loc.findData(WH)); rt.reason.setText("Damaged on arrival")
    add_line(rt.editor, "DEMO-LIP-01", qty=2)
    click_submit(rt)
    ret = acc.client.get("/supplier-returns", supplier_id=s["id"])["items"][0]
    check(ret["status"] == "draft", "return saved as draft, stock not yet moved")
    acc.client.post(f"/supplier-returns/{ret['id']}/dispatch")
    adj = sp.AdjustmentDialog(acc, s["id"]); pump(1.2)
    adj.amount.setText("800"); adj.reason.setText("Credit for returned damaged lipsticks"); adj.doc.setText("CN-9")
    adj.ret.setCurrentIndex(max(adj.ret.findData(ret["id"]), 0)); adj.invoice.setCurrentIndex(max(adj.invoice.findData(held["id"]), 0))
    click_submit(adj)
    a = acc.client.get("/supplier-adjustments", supplier_id=s["id"])["items"][0]
    check(a["status"] == "pending" and float(acc.client.get(f"/suppliers/{s['id']}")["balance"]) == inv_total - 5000, "credit is pending and has not changed the balance")
    mgr.client.post(f"/supplier-adjustments/{a['id']}/approve", {"note": "ok"})
    check(float(acc.client.get(f"/suppliers/{s['id']}")["balance"]) == inv_total - 5800 and acc.client.get(f"/supplier-returns/{ret['id']}")["status"] == "credited",
          "approved credit reduced the balance; return marked credited")
    check(ceo.client.get("/purchasing/integrity")["consistent"] and ceo.client.get("/inventory/integrity")["consistent"], "supplier ledger and stock ledger reconcile")

    # ---- screenshots at both sizes ------------------------------------------------------------------------------------
    for w_, h_ in ((1366, 768), (1920, 1080)):
        sh = Shell(ceo); sh.resize(w_, h_); sh.show(); pump(0.3)
        sh.show_page("suppliers"); pg = sh.pages["suppliers"]
        for i in range(pg.tabs.count()):
            pg.tabs.setCurrentIndex(i); pump(1.6)
            sh.grab().save(str(out / f"suppliers_tab{i}_{w_}.png"))
        dlgs = {"po_detail": sp.PODetail(ceo, po["id"]), "invoice_detail": sp.InvoiceDetail(ceo, held["id"]),
                "payment_dialog": sp.PaymentDialog(ceo, s["id"]), "po_dialog": sp.POEditDialog(ceo),
                "receive_dialog": sp.POReceiveDialog(ceo, ceo.client.get(f"/purchase-orders/{po['id']}")),
                "invoice_dialog": sp.InvoiceDialog(ceo, ceo.client.get(f"/purchase-orders/{po['id']}")),
                "statement": sp.StatementDialog(ceo, s)}
        for name, dlg in dlgs.items():
            dlg.show(); pump(1.6); dlg.grab().save(str(out / f"{name}_{w_}.png")); dlg.close()
        sh.close()

    print("\nFAILED CHECKS:" if E.FAILS else "\nALL CHECKS PASSED", *E.FAILS, sep="\n  ")
    sys.exit(1 if E.FAILS else 0)


if __name__ == "__main__":
    main()
