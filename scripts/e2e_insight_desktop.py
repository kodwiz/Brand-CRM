"""Phase 8 desktop check: drive the REAL Dashboard, Reports and Approvals screens against the REAL running backend (demo environment).

    python scripts/e2e_insight_desktop.py CREDENTIALS_FILE OUTDIR

Business activity is created through the API (a delivered and paid order, a distributor-free stock request, a held discount) so the screens have real
records to show; the screens themselves are then driven as each role. Exits non-zero on any failed check."""
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_desktop as E  # noqa: E402  (builds the Qt application)
from e2e_desktop import check, creds, pump, sess  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QPushButton  # noqa: E402

from desktop.api.client import ApiError  # noqa: E402
from desktop.ui.shell import Shell  # noqa: E402

D = Decimal
SKU = "DEMO-LIP-01"


def buttons(w) -> list[str]:
    return [b.text() for b in w.findChildren(QPushButton) if b.isVisibleTo(w)]


def refused(fn):
    try:
        fn()
    except ApiError as e:
        return e.status
    return None


def tiles(page) -> dict[str, str]:
    """label -> displayed value of every KPI tile currently on the dashboard."""
    from desktop.pages.dashboard import _Tile
    from PySide6.QtWidgets import QLabel
    out = {}
    for t in page.body.findChildren(_Tile):
        ls = t.findChildren(QLabel)
        out[ls[0].text()] = ls[1].text()
    return out


def wait_for(cond, secs=8.0):
    import time
    end = time.time() + secs
    while time.time() < end and not cond():
        pump(0.1)
    return cond()


def main() -> None:
    cred = creds(sys.argv[1])
    out = Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
    ceo, mgr, stk, bk, acc = (sess(u, cred[u]) for u in ("demo.ceo", "demo.manager", "demo.stock", "demo.booker", "demo.accounts"))
    today = date.today().isoformat()
    B1 = next(l["id"] for l in ceo.client.get("/locations") if l["code"] == "DEMO-B1")
    var = ceo.client.get("/variants/lookup", q=SKU)[0]

    # ---- business activity to report on ----------------------------------------------------------------------------------------
    chan = bk.client.get("/sales/channels")[0]["id"]
    cust = bk.client.post("/customers", {"name": "Insight Customer", "phone": "0300 7000001"})
    body = {"customer_id": cust["id"], "channel_id": chan, "location_id": B1, "order_date": today, "payment_terms": "cod",
            "delivery": {"name": "Recipient", "phone": "0300 7000002", "address": "5 Insight Road", "city": "Demoville"}, "lines": [{"variant_id": var["id"], "qty": 4}]}
    o = bk.client.post("/orders", body)
    o = bk.client.post(f"/orders/{o['id']}/confirm")
    acc.client.post(f"/orders/{o['id']}/payments", {"amount": o["total"], "method": "cash"})
    sh = stk.client.post(f"/orders/{o['id']}/shipments", {"lines": [{"order_line_id": l["id"], "qty": l["to_ship"]} for l in o["lines"]]})
    stk.client.post(f"/shipments/{sh['id']}/dispatch", {"courier": "Demo Courier", "tracking_no": "INS1"})
    stk.client.post(f"/shipments/{sh['id']}/outcome", {"result": "delivered"})
    req = stk.client.post("/inventory/requests", {"kind": "adjustment", "location_id": B1, "reason": "Insight demo count variance", "lines": [{"variant_id": var["id"], "qty_delta": -1}]})
    acc.client.post("/accounting/posting/run")
    check(True, f"activity created: order {o['ref']} delivered and paid, adjustment {req['ref']} waiting")

    # ---- navigation and role-specific dashboards -------------------------------------------------------------------------------
    views = {}
    for s, name in ((ceo, "ceo"), (mgr, "manager"), (acc, "accounts"), (stk, "stock"), (bk, "booker")):
        shell = Shell(s); shell.resize(1366, 768); shell.show(); pump(0.3)
        pg = shell.pages["overview"]
        ok = wait_for(lambda: pg.panel.stack.currentIndex() == pg.panel.CONTENT)
        views[name] = (pg, tiles(pg), pg.sub.text())
        check(ok, f"{name}: dashboard loads ({pg.sub.text()[:60]})")
        shell.grab().save(str(out / f"dashboard_{name}_1366.png"))
        check("reports" in shell.buttons or name in ("stock", "booker"), f"{name}: Reports nav {'present' if 'reports' in shell.buttons else 'absent'}")
        shell.close()
    _, ceo_t, _ = views["ceo"]
    _, mgr_t, _ = views["manager"]
    _, acc_t, _ = views["accounts"]
    _, bk_t, _ = views["booker"]
    check("Net sales" in ceo_t and "Gross profit" in ceo_t and "Received from customers" in ceo_t and "Customers owe" in ceo_t, f"CEO sees revenue, profit, cash and receivables as separate tiles")
    check("Net sales" in mgr_t and "Gross profit" not in mgr_t and "Net profit" not in mgr_t, "manager sees sales but not profit")
    check(any("Stock value" in k for k in acc_t) and "Gross profit" in acc_t, f"accounts sees the financial dashboard ({len(acc_t)} tiles)")
    check("Gross profit" not in bk_t and "Customers owe" not in bk_t and "Orders taken" in bk_t, "booker sees only own-order figures")
    check(D(ceo_t["Net sales"].replace("PKR", "").replace(",", "").strip()) > 0, f"CEO net sales reflect the delivered order ({ceo_t['Net sales']})")

    # ---- dashboard period and location filters -----------------------------------------------------------------------------------
    shell = Shell(ceo); shell.resize(1366, 768); shell.show(); pump(0.3)
    pg = shell.pages["overview"]; wait_for(lambda: pg.panel.stack.currentIndex() == pg.panel.CONTENT)
    shell.location_combo.setCurrentIndex(shell.location_combo.findData(B1)); wait_for(lambda: "B1" in pg.scope_note.text() or pg.panel.stack.currentIndex() == pg.panel.CONTENT, 4); pump(1.5)
    scoped = tiles(pg)
    check(scoped.get("Net sales") is not None and "Branch 1" in pg.scope_note.text(), f"location selector re-scopes the dashboard ({pg.scope_note.text()})")
    pg.preset.setCurrentIndex(pg.preset.findData("today")); pump(2)
    check(pg.d_from.date() == pg.d_to.date(), "'Today' preset sets both dates")
    pg.d_from.setDate(pg.d_to.date().addDays(1)); pump(0.3); pg.refresh(); pump(0.4)
    check("end date" in pg.status.text().lower(), "an inverted period is refused with a message")
    shell.close(); ceo.current_location_id = None

    # ---- approvals inbox ----------------------------------------------------------------------------------------------------------------
    for who, name, expect in ((mgr, "manager", True), (ceo, "ceo", True)):
        shell = Shell(who); shell.resize(1366, 768); shell.show(); pump(0.3)
        shell.show_page("approvals"); ap = shell.pages["approvals"]
        wait_for(lambda: ap.panel.stack.currentIndex() == ap.panel.CONTENT)
        types = {r["type"] for r in ap.items}
        check("Stock adjustment or write-off" in types and ap.table.rowCount() >= 1, f"{name}: inbox lists the waiting adjustment ({ap.table.rowCount()} rows)")
        row = next(r for r in ap.items if r["ref"] == req["ref"])
        check(row["own"] is False, f"{name}: the request raised by the stock keeper is not marked as theirs")
        for i in range(ap.table.rowCount()):
            if ap.table.row_data(i)["ref"] == req["ref"]:
                ap.table.selectRow(i)
        pump(0.2)
        check(ap.open_btn.isEnabled(), f"{name}: Open is enabled for a selected request")
        ap.open_btn.click(); pump(1.5)
        check(shell.header_title.text() == "Inventory", f"{name}: Open takes you to the owning screen ({shell.header_title.text()})")
        shell.show_page("approvals"); pump(1)
        shell.grab().save(str(out / f"approvals_{name}_1366.png"))
        shell.close()
    check("approvals" not in Shell(stk).buttons and "approvals" not in Shell(bk).buttons, "stock keeper and booker have no Approvals screen")
    check(refused(lambda: stk.client.get("/insight/approvals")) == 403 and refused(lambda: bk.client.get("/insight/approvals")) == 403, "the server refuses the inbox to them as well")

    # ---- reports: catalogue per role, run, reconcile, export -------------------------------------------------------------------------
    cats = {n: {r["key"] for r in s.client.get("/insight/reports")["items"]} for n, s in (("ceo", ceo), ("manager", mgr), ("accounts", acc), ("stock", stk))}
    check(len(cats["ceo"]) >= 17, f"CEO catalogue has {len(cats['ceo'])} reports")
    check("profit_and_loss" in cats["accounts"] and "profit_and_loss" not in cats["manager"], "profit and loss is for the finance roles only")
    check("inventory_movement" in cats["manager"] and "inventory_movement" not in cats["accounts"], "operational reports are not offered to accounts")
    check(refused(lambda: mgr.client.get("/insight/reports/profit_and_loss")) == 404, "a hidden report answers 404 as if it did not exist")

    shell = Shell(ceo); shell.resize(1366, 768); shell.show(); pump(0.3)
    shell.show_page("reports"); rp = shell.pages["reports"]; wait_for(lambda: rp.listing.count() > 5)
    check(rp.listing.count() >= 17, f"report list shows {rp.listing.count()} entries including group headings")
    rp.select("sales_by_date_location"); pump(0.5); rp.run(); wait_for(lambda: rp.panel.stack.currentIndex() == rp.panel.CONTENT)
    check(rp.table.rowCount() >= 1, f"sales report shows {rp.table.rowCount()} row(s)")
    texts = [rp.recon.itemAt(i).widget().text() for i in range(rp.recon.count())]
    check(bool(texts) and all(t.startswith("✓") for t in texts), f"sales report reconciles to the ledger on screen ({texts[:1]})")
    check(rp.xl.isEnabled() and rp.pdf.isEnabled(), "export buttons enabled after a run")
    shell.grab().save(str(out / "report_sales_1366.png"))
    tmp = Path(tempfile.mkdtemp())
    audits_before = len(ceo.client.get("/audit", page_size=200, action="report.export")["items"])
    for fmt in ("xlsx", "pdf"):
        p = tmp / f"sales.{fmt}"
        rp.export_to(str(p), fmt); wait_for(lambda: p.exists() and p.stat().st_size > 0)
        head = p.read_bytes()[:4]
        check(p.exists() and (head == b"%PDF" if fmt == "pdf" else head[:2] == b"PK"), f"{fmt.upper()} export saved and is a valid file ({p.stat().st_size} bytes)")
    audits = ceo.client.get("/audit", page_size=200, action="report.export")["items"]
    check(len(audits) >= audits_before + 2, f"both exports were written to the audit log ({len(audits)} entries)")

    for key in ("product_performance", "stock_valuation", "profit_and_loss", "balance_sheet", "trial_balance", "receivables_aging", "payables_aging", "expenses", "cash_movements",
                "inventory_movement", "expiry_damage", "transfer_reconciliation", "customer_orders_returns", "order_booker_performance", "discounts_promotions", "attendance",
                "employee_performance", "audit_history"):
        rp.select(key); pump(0.3); rp.result = None; rp.run()
        ok = wait_for(lambda: (getattr(rp, "result", None) or {}).get("key") == key or rp.status.isVisible())
        bad = [t for t in [rp.recon.itemAt(i).widget().text() for i in range(rp.recon.count())] if t.startswith("✗")]
        check(ok and (getattr(rp, "result", None) or {}).get("key") == key and not rp.status.isVisible() and not bad, f"{key}: runs on screen ({rp.table.rowCount()} rows){' MISMATCH ' + bad[0][:80] if bad else ''}{' ' + rp.status.text()[:80] if rp.status.isVisible() else ''}")
        if key == "profit_and_loss":
            shell.grab().save(str(out / "report_pnl_1366.png"))
    rp.select("party_statement"); pump(1.5)
    check(rp.widgets["party_type"].count() == 4 and wait_for(lambda: rp.widgets["party_id"].count() > 1), "statement offers customer, distributor, supplier and employee parties")
    rp.run(); pump(1)
    check(rp.status.isVisible() and "whose" in rp.status.text().lower() or rp.panel.stack.currentIndex() == rp.panel.EMPTY, "a statement without a party is refused with a message")
    shell.close()

    shell = Shell(stk); shell.resize(1366, 768); shell.show(); pump(0.3)
    shell.show_page("reports"); rp = shell.pages["reports"]; wait_for(lambda: rp.listing.count() > 1)
    keys = {rp.listing.item(i).data(Qt.ItemDataRole.UserRole) for i in range(rp.listing.count())} - {None}
    check("profit_and_loss" not in keys and "inventory_movement" in keys, f"stock keeper's report list is limited ({len(keys)} reports)")
    check(not rp.can_export, "stock keeper has no export buttons")
    rp.select("inventory_movement"); pump(0.3)
    loc_cb = rp.widgets["location_id"]
    check(all(loc_cb.itemData(i) in (None, *[l["id"] for l in stk.locations]) for i in range(loc_cb.count())), "location filter only offers the keeper's own locations")
    check(refused(lambda: stk.client.get("/insight/reports/inventory_movement/export", format="xlsx")) in (403, 404), "export refused by the server for the stock keeper")
    shell.close()

    # ---- other resolution and dialogs-free screenshots ----------------------------------------------------------------------------------
    for w_, h_ in ((1366, 768), (1920, 1080)):
        for who, label_ in ((ceo, "ceo"), (acc, "accounts"), (bk, "booker")):
            shell = Shell(who); shell.resize(w_, h_); shell.show(); pump(0.3)
            pg = shell.pages["overview"]; wait_for(lambda: pg.panel.stack.currentIndex() == pg.panel.CONTENT); pump(0.8)
            shell.grab().save(str(out / f"dash_{label_}_{w_}.png"))
            if "reports" in shell.buttons:
                shell.show_page("reports"); r2 = shell.pages["reports"]; wait_for(lambda: r2.listing.count() > 1)
                r2.select("profit_and_loss" if label_ != "booker" else "x"); pump(0.3); r2.run(); pump(2)
                shell.grab().save(str(out / f"reports_{label_}_{w_}.png"))
            if "approvals" in shell.buttons:
                shell.show_page("approvals"); pump(2); shell.grab().save(str(out / f"approvals_{label_}_{w_}.png"))
            shell.close()

    print("\nFAILED CHECKS:" if E.FAILS else "\nALL CHECKS PASSED", *E.FAILS, sep="\n  ")
    sys.stdout.flush()
    import os
    os._exit(1 if E.FAILS else 0)    # Qt aborts while tearing down the many windows opened here, after every check has already run


if __name__ == "__main__":
    main()
