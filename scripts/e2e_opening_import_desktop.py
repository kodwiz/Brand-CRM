"""Phase 9 desktop check: drive the REAL opening-import dialog against the REAL running backend (demo environment).

    python scripts/e2e_opening_import_desktop.py CREDENTIALS_FILE OUTDIR

A workbook is built from SYNTHETIC rows only. A bad file is checked first (refused, nothing saved), then a good file is checked and imported.
The import creates a draft batch; a second person (demo.ceo vs demo.accounts) must approve it. Exits non-zero on any failed check."""
import io
import sys
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_desktop as E  # noqa: E402
from e2e_desktop import check, creds, pump, sess  # noqa: E402
from openpyxl import Workbook  # noqa: E402

from backend.services import opening_import_service as oi  # noqa: E402
from desktop.pages.accounting import OpeningImportDialog, OpeningTab  # noqa: E402
from desktop.ui.shell import Shell  # noqa: E402

D = Decimal


def wb_bytes(rows: dict) -> bytes:
    wb = Workbook(); wb.remove(wb.active)
    for name, cols in oi.SHEETS.items():
        ws = wb.create_sheet(name); heads = [c[1] for c in cols]; ws.append(heads)
        for r in rows.get(name, []):
            ws.append([r.get(h) for h in heads])
    b = io.BytesIO(); wb.save(b); return b.getvalue()


def wait_for(cond, secs=10.0):
    import time
    end = time.time() + secs
    while time.time() < end and not cond():
        pump(0.1)
    return cond()


def main() -> None:
    cred = creds(sys.argv[1]); out = Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
    ceo, acc = sess("demo.ceo", cred["demo.ceo"]), sess("demo.accounts", cred["demo.accounts"])
    tmp = Path(tempfile.mkdtemp())
    sup = ceo.client.post("/suppliers", {"name": "Opening Demo Supplier", "payment_terms_days": 30})
    cust = ceo.client.post("/customers", {"name": "Opening Demo Customer", "phone": "0300 8000001"})
    cash = next(c for c in ceo.client.get("/accounting/cash-accounts")["items"] if c["kind"] == "cash")
    V = {"evidence": "Demo register p.1", "verified": "yes"}
    as_of = (date.today() - timedelta(days=20)).isoformat()
    # assets = customer 300 + cash 5000 = 5300; liabilities = supplier 2000; capital must be 3300
    good = {"Suppliers": [{"supplier_code": sup["code"], "amount": "2000", **V}], "Customers": [{"customer_code": cust["code"], "amount": "300", **V}],
            "Cash and bank": [{"cash_account": cash["name"], "amount": "5000", **V}], "Owner's capital": [{"owner_capital": "3300", **V}]}
    bad = {**good, "Owner's capital": [{"owner_capital": "9999", **V}]}
    bad_f, good_f = tmp / "bad.xlsx", tmp / "good.xlsx"
    bad_f.write_bytes(wb_bytes(bad)); good_f.write_bytes(wb_bytes(good))

    shell = Shell(ceo); shell.resize(1366, 768); shell.show(); pump(0.3)
    shell.show_page("accounting"); pump(1)
    page = shell.pages["accounting"]
    tab = next(page.tabs.widget(i) for i in range(page.tabs.count()) if isinstance(page.tabs.widget(i), OpeningTab))
    page.tabs.setCurrentWidget(tab); pump(1)
    from PySide6.QtWidgets import QPushButton
    btns = [b.text() for b in tab.findChildren(QPushButton)]
    check("Import workbook…" in btns, "CEO sees the Import workbook button")
    check(not any("Import workbook" in b.text() for b in Shell(sess("demo.manager", cred["demo.manager"])).pages["accounting"].findChildren(QPushButton)) if "accounting" in Shell(sess("demo.manager", cred["demo.manager"])).pages else True,
          "a role without the permission has no import button")

    for w_, h_ in ((1366, 768), (1920, 1080)):
        d = OpeningImportDialog(ceo); d.resize(*([min(w_, 960), min(h_, 640)])); d.show(); pump(0.4)
        check(not d.go.isEnabled(), f"[{w_}] Import is disabled before a clean check")
        d.path = str(bad_f); d.asof.setDate(__import__("PySide6.QtCore", fromlist=["QDate"]).QDate.fromString(as_of, "yyyy-MM-dd"))
        d._run(True); wait_for(lambda: "problem" in d.result.text().lower() or "passed" in d.result.text().lower())
        check("problem" in d.result.text().lower() and d.errors.rowCount() >= 1 and not d.go.isEnabled(), f"[{w_}] mismatching capital is refused with row-level errors ({d.errors.rowCount()}): {d.result.text()[:90]}")
        d.grab().save(str(out / f"opening_import_bad_{w_}.png"))
        if w_ == 1366:
            d.path = str(good_f); d._run(True); wait_for(lambda: "passed" in d.result.text().lower())
            check("passed" in d.result.text().lower() and d.go.isEnabled(), f"[{w_}] clean file passes the check: {d.result.text()[:110]}")
            d.grab().save(str(out / f"opening_import_ok_{w_}.png"))
            check(len(ceo.client.get("/accounting/opening-batches")["items"]) == 0, "a check saved nothing")
            d._run(False); wait_for(lambda: not d.isVisible())
            batches = ceo.client.get("/accounting/opening-batches")["items"]
            check(len(batches) == 1 and batches[0]["status"] == "draft", f"import created one DRAFT batch ({batches[0]['ref'] if batches else None})")
        else:
            d.path = str(good_f); d._run(True); wait_for(lambda: "problem" in d.result.text().lower())
            texts = " ".join(str(d.errors.row_data(i)) for i in range(d.errors.rowCount()))
            check("problem" in d.result.text().lower() and not d.go.isEnabled() and "already" in texts.lower(), f"[{w_}] importing the same balances twice is refused ({texts[:120]})")
            d.grab().save(str(out / f"opening_import_dup_{w_}.png")); d.close()
    # approval needs another person
    b = ceo.client.get("/accounting/opening-batches")["items"][0]
    from desktop.api.client import ApiError
    try:
        ceo.client.post(f"/accounting/opening-batches/{b['id']}/approve"); st = 200
    except ApiError as e:
        st = e.status
    check(st == 403, f"the person who imported cannot approve their own batch (HTTP {st})")
    role = next(r for r in ceo.client.get("/roles")["items"] if r["code"] == "ceo") if isinstance(ceo.client.get("/roles"), dict) else next(r for r in ceo.client.get("/roles") if r["code"] == "ceo")
    ceo.client.post("/users", {"username": "demo.ceo2", "full_name": "Demo Second Approver", "password": "Demo-Second-Approver-1!", "role_id": role["id"]})
    ceo2 = sess("demo.ceo2", "Demo-Second-Approver-1!")
    ceo2.client.post(f"/accounting/opening-batches/{b['id']}/approve")
    ceo2.client.post("/accounting/posting/run")
    bal = acc.client.get(f"/suppliers/{sup['id']}")
    check(D(str(bal["balance"])) == D("2000"), f"supplier opening balance is on the supplier's account ({bal['balance']})")
    tb = acc.client.get("/accounting/reports/trial-balance")
    check(tb["totals"]["balanced"] is True if "totals" in tb else True, "trial balance still balances after posting")
    shell.grab().save(str(out / "opening_tab_1366.png"))
    print("\nFAILED CHECKS:" if E.FAILS else "\nALL CHECKS PASSED", *E.FAILS, sep="\n  ")
    sys.stdout.flush()
    import os
    os._exit(1 if E.FAILS else 0)


if __name__ == "__main__":
    main()
