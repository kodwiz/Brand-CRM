"""Drive the REAL desktop widgets against the REAL running backend (demo environment) and check the results.

    python scripts/e2e_desktop.py CREDENTIALS_FILE OUTDIR

CREDENTIALS_FILE is the output of `backend.cli seed-demo` (lines like '  demo.ceo  <password>').
Screenshots of each screen at 1366x768 and 1920x1080 go to OUTDIR. Exits non-zero on any failed check.
"""
import io
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import load_workbook  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from desktop.api.client import ApiClient  # noqa: E402
from desktop.main import build_app  # noqa: E402
from desktop.pages import inventory as inv, products as prod, transfers as trf  # noqa: E402
from desktop.session import Session  # noqa: E402
from desktop.ui.shell import Shell  # noqa: E402

app: QApplication = build_app()
FAILS: list[str] = []


def pump(sec: float = 1.0) -> None:
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def check(cond: bool, msg: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


def creds(path: str) -> dict[str, str]:
    out = {}
    for ln in Path(path).read_text().splitlines():
        parts = ln.split()
        if len(parts) == 2 and parts[0].startswith("demo."):
            out[parts[0]] = parts[1]
    return out


def sess(user: str, pw: str) -> Session:
    c = ApiClient(); c.login(user, pw)
    s = Session(c); s.load_me()
    return s


def click_submit(dlg, wait: float = 1.5) -> None:
    dlg.submit_btn.click()
    pump(wait)


def add_line(editor, sku: str, **vals) -> None:
    editor.picker.edit.setText(sku); editor.picker.search(); pump(0.8)
    for k, v in vals.items():
        w = editor.inputs[k]
        if hasattr(w, "setValue"):
            w.setValue(v)
        elif hasattr(w, "setText"):
            w.setText(str(v))
        elif hasattr(w, "setDate"):
            from PySide6.QtCore import QDate
            w.setDate(QDate.fromString(v, "yyyy-MM-dd"))
    editor._add()


def stock_of(client, loc_id: int, sku: str) -> dict | None:
    rows = client.get("/inventory/stock", location_id=loc_id, q=sku, include_zero="true")["items"]
    return next((r for r in rows if r["sku"] == sku), None)


def main() -> None:
    cred = creds(sys.argv[1])
    out = Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
    stock, mgr, ceo = (sess(u, cred[u]) for u in ("demo.stock", "demo.manager", "demo.ceo"))
    locs = {l["code"]: l for l in ceo.client.get("/locations")}
    WH, B1, B2 = locs["DEMO-WH"]["id"], locs["DEMO-B1"]["id"], locs["DEMO-B2"]["id"]

    # ---- 1. catalogue as the stock keeper (can view, cannot edit) ----------------------------------------
    shell = Shell(stock); shell.resize(1366, 768); shell.show(); pump(0.3)
    shell.show_page("products"); pump(1.5)
    pg = shell.pages["products"]
    check(pg.table.rowCount() >= 3, f"products list shows the catalogue ({pg.table.rowCount()} rows)")
    check(not any(w.text() == "New product" for w in pg.findChildren(type(inv.button('x')))), "stock keeper sees no 'New product' button")
    d = prod.ProductDetailDialog(stock, pg.table.row_data(0)["id"], pg.categories); pump(2)
    check(d.vtable.rowCount() >= 1 and d.stock_table.rowCount() >= 1, "product detail shows variants and stock by location")
    check("employee_price" in d.product["variants"][0] and "purchase_cost" not in d.product["variants"][0],
          "stock keeper sees employee price but not cost price")
    d.close()

    # ---- 2. receive goods (damaged + rejected) ------------------------------------------------------------
    before = stock_of(stock.client, WH, "DEMO-LIP-01")
    r = inv.ReceiptDialog(stock)
    r.loc.setCurrentIndex(max(r.loc.findData(WH), 0)); r.po.setText("DEMO-PO-1")
    add_line(r.editor, "DEMO-LIP-01", qty_accepted=20, qty_damaged=2, qty_rejected=1, unit_cost="500")
    click_submit(r)
    after = stock_of(stock.client, WH, "DEMO-LIP-01")
    check(after["on_hand_good"] == before["on_hand_good"] + 20 and after["damaged"] == before["damaged"] + 2,
          "receipt: only accepted units became saleable; damaged held separately; rejected not stocked")
    check(r.result() == 1, "receipt dialog closed successfully")
    w = ceo.client.get("/inventory/stock", location_id=WH, q="DEMO-LIP-01")["items"][0]
    check(480 < float(w["avg_cost"]) < 500, f"weighted-average cost updated (avg {w['avg_cost']})")

    # ---- 3. transfer WH -> B1: request (stock) -> approve (manager) -> dispatch (stock) -> partial receive ----
    n = trf.NewTransferDialog(stock, ceo.client.get("/transfers/locations"))
    n.src.setCurrentIndex(n.src.findData(WH)); n.dst.setCurrentIndex(n.dst.findData(B1))
    add_line(n.editor, "DEMO-LIP-02", qty=10)
    click_submit(n)
    tr = stock.client.get("/transfers", status="requested")["items"][0]
    check(tr["status"] == "requested", f"transfer {tr['ref']} requested")
    names = {l["id"]: l["name"] for l in ceo.client.get("/transfers/locations")}
    own = trf.TransferDetail(stock, tr["id"], names); pump(1)
    check(not own.actions["approve"].isVisible() and not own.actions["approve"].isVisibleTo(own),
          "requester cannot see an Approve button for their own transfer")
    own.close()
    md = trf.TransferDetail(mgr, tr["id"], names); pump(1)
    check(md.actions["approve"].isVisibleTo(md), "manager sees Approve")
    md.actions["approve"].click(); pump(1.5)
    check(mgr.client.get(f"/transfers/{tr['id']}")["status"] == "approved", "manager approved; stock reserved")
    s2 = stock_of(stock.client, WH, "DEMO-LIP-02")
    check(s2["reserved"] == 10 and s2["available"] == s2["on_hand_good"] - 10, "reserved stock excluded from available")
    sd = trf.TransferDetail(stock, tr["id"], names); pump(1)
    sd.actions["dispatch"].click(); pump(1.5)
    t = stock.client.get(f"/transfers/{tr['id']}")
    check(t["status"] == "dispatched", "dispatched; goods in transit")
    check(stock_of(stock.client, B1, "DEMO-LIP-02")["in_transit"] == 10, "in-transit quantity shown at destination, not available")
    sd.load(); pump(1)
    rcv = trf.ReceiveDialog(stock, stock.client.get(f"/transfers/{tr['id']}"))
    rcv.rows[0][1].setValue(6); rcv.rows[0][2].setValue(1)
    click_submit(rcv)
    t = stock.client.get(f"/transfers/{tr['id']}")
    check(t["status"] == "partially_received", "partial receipt recorded (6 good + 1 damaged of 10)")
    sd.load(); pump(1)
    check(sd.actions["flag"].isVisibleTo(sd), "receiver can report the missing 3 units")
    # the prompt dialog is modal (exec), so submit the same request the dialog sends
    stock.client.post(f"/transfers/{tr['id']}/flag-discrepancy", {"reason": "3 units missing in carton"})
    pump(0.5)
    mgr.client.post(f"/transfers/{tr['id']}/resolve-discrepancy", {"note": "carrier confirmed loss"})
    t = mgr.client.get(f"/transfers/{tr['id']}")
    check(t["status"] == "received" and t["lines"][0]["qty_short"] == 3, "shortage approved by a different user and closed")
    check(ceo.client.get("/inventory/integrity")["consistent"], "ledger reconciles to balances and costs after transfer")

    # ---- 4. adjustment request needs another person ---------------------------------------------------------
    rq = inv.RequestDialog(stock)
    rq.loc.setCurrentIndex(rq.loc.findData(WH)); rq.reason.setText("Counted 2 short in demo")
    add_line(rq.editor, "DEMO-LIP-03", qty_delta=-2)
    click_submit(rq)
    req = stock.client.get("/inventory/requests", status="pending")["items"][0]
    before = stock_of(stock.client, WH, "DEMO-LIP-03")["on_hand_good"]
    check(before == 60, "adjustment submitted without changing stock yet")
    dd = inv.DecisionDialog(mgr, req, True); dd.note.setText("Verified"); click_submit(dd)
    check(stock_of(stock.client, WH, "DEMO-LIP-03")["on_hand_good"] == 58, "manager approved -> stock reduced by 2")

    # ---- 5. damaged stock moves out of saleable --------------------------------------------------------------
    mv = inv.MoveDialog(stock)
    mv.loc.setCurrentIndex(mv.loc.findData(WH)); mv.reason.setText("Dropped in demo")
    add_line(mv.editor, "DEMO-LIP-03", qty=3)
    click_submit(mv)
    s3 = stock_of(stock.client, WH, "DEMO-LIP-03")
    check(s3["on_hand_good"] == 55 and s3["damaged"] == 3 and s3["available"] == 55, "damaged stock separated and not available")

    # ---- 6. stock count with a variance ----------------------------------------------------------------------
    created = stock.client.post("/inventory/counts", {"location_id": B1})
    cd = inv.CountDialog(stock, created["id"]); pump(1.2)
    check(cd.table.rowCount() >= 3, f"count sheet lists {cd.table.rowCount()} lines")
    lines = stock.client.get(f"/inventory/counts/{created['id']}")["lines"]
    stock.client.put(f"/inventory/counts/{created['id']}/entries", {"entries": [
        {"line_id": l["id"], "counted_qty": l["expected_qty"] - (1 if i == 0 else 0), "reason": "shelf shortage" if i == 0 else None}
        for i, l in enumerate(lines)]})
    res = stock.client.post(f"/inventory/counts/{created['id']}/submit")
    check(bool(res["request_ref"]), "count variance raised an approval request")
    cd.close()

    # ---- 7. bulk import wizard with errors, then clean ---------------------------------------------------------
    wiz = prod.ImportWizard(ceo)
    tpl = ceo.client.get_bytes("/imports/template")
    wb = load_workbook(io.BytesIO(tpl))
    wp, wv = wb["Products"], wb["Variants"]
    wp.append(["X1", "DEMO-IMP", "Demo Imported Product", "Face", "Demo Brand", None, None, "no", "no", "yes"])
    wv.append(["X1", "DEMO-LIP-01", "Duplicate SKU", None, None, None, None, None, "10", None, None, None, None])
    bad = out / "import_bad.xlsx"; wb.save(bad)
    wiz.path = str(bad); wiz._validate(); pump(2)
    check(wiz.job and wiz.job["error_count"] > 0 and not wiz.commit_btn.isEnabled(), "import with a duplicate SKU is blocked at review")
    wiz.grab().save(str(out / "import_review_errors.png"))
    wv.delete_rows(3, 1); wv.append(["X1", "DEMO-IMP-01", "Imported variant", None, "Rose", "#CC8899", "5ml", None, "900", None, None, 5, None])
    good = out / "import_good.xlsx"; wb.save(good)
    wiz.path = str(good); wiz._validate(); pump(2)
    check(wiz.job["error_count"] == 0 and wiz.commit_btn.isEnabled(), "clean import passes review; nothing saved yet")
    check(not ceo.client.get("/products", q="DEMO-IMP")["items"], "nothing written before confirmation")
    wiz._commit(); pump(2)
    check(len(ceo.client.get("/products", q="DEMO-IMP")["items"]) == 1, "import committed after confirmation")
    wiz.close()

    # ---- 8. screenshots ---------------------------------------------------------------------------------------
    for w_, h_ in ((1366, 768), (1920, 1080)):
        sh = Shell(ceo); sh.resize(w_, h_); sh.show(); pump(0.3)
        shots = [("products", None), ("inventory", 0), ("inventory", 1), ("inventory", 2), ("inventory", 3), ("inventory", 4),
                 ("inventory", 5), ("inventory", 6), ("transfers", None)]
        for page, tab in shots:
            sh.show_page(page)
            if tab is not None:
                sh.pages[page].tabs.setCurrentIndex(tab)
            pump(1.6)
            sh.grab().save(str(out / f"{page}{'' if tab is None else '_tab' + str(tab)}_{w_}.png"))
        # dialogs
        dlg = prod.ProductDetailDialog(ceo, ceo.client.get("/products", q="DEMO-LIP")["items"][0]["id"], []); dlg.resize(1000, 640); dlg.show(); pump(2)
        dlg.grab().save(str(out / f"product_detail_{w_}.png")); dlg.close()
        rd = inv.ReceiptDialog(stock); rd.show(); pump(0.8); rd.grab().save(str(out / f"receipt_dialog_{w_}.png")); rd.close()
        tdl = trf.TransferDetail(ceo, tr["id"], names); tdl.resize(1000, 620); tdl.show(); pump(1.5)
        tdl.grab().save(str(out / f"transfer_detail_{w_}.png")); tdl.close()
        sh.close()

    print("\nFAILED CHECKS:" if FAILS else "\nALL CHECKS PASSED", *FAILS, sep="\n  ")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
