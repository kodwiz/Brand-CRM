"""Phase 6 desktop check: drive the REAL Accounting screens against the REAL running backend (demo environment).

    python scripts/e2e_accounting_desktop.py CREDENTIALS_FILE OUTDIR

Reuses the helpers of scripts/e2e_desktop.py. Exits non-zero on any failed check."""
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_desktop as E  # noqa: E402  (builds the Qt application)
from e2e_desktop import check, click_submit, creds, pump, sess  # noqa: E402
from PySide6.QtWidgets import QPushButton  # noqa: E402

from desktop.pages import accounting as ap  # noqa: E402
from desktop.ui.shell import Shell  # noqa: E402

D = Decimal


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
    """Make NoteDialog answer itself so tab handlers that open it can be driven without a human."""
    def _exec(self):
        if hasattr(self.text, "setPlainText"):
            self.text.setPlainText(text)
        else:
            self.text.setText(text)
        self.submit_btn.click(); pump(1.5)
        return self.result()
    ap.NoteDialog.exec = _exec


def main() -> None:
    cred = creds(sys.argv[1])
    out = Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
    ceo, mgr, stk, bk, acc = (sess(u, cred[u]) for u in ("demo.ceo", "demo.manager", "demo.stock", "demo.booker", "demo.accounts"))
    auto_note()
    today = __import__("datetime").date.today().isoformat()
    B1 = next(l["id"] for l in ceo.client.get("/locations") if l["code"] == "DEMO-B1")
    var = ceo.client.get("/variants/lookup", q="DEMO-LIP-01")[0]
    ceo.client.patch(f"/variants/{var['id']}", {"distributor_price": "100.00"})

    # ---- operational data to account for (made through the normal APIs) ----------------------------------------------------------------------------
    dist = mgr.client.post("/distributors", {"name": "Demo Accounting Distributor", "discount_pct": "10", "credit_limit": "100000", "payment_terms_days": 30})
    stk.client.post("/distributor-dispatches", {"distributor_id": dist["id"], "location_id": B1, "invoice_date": today, "lines": [{"variant_id": var["id"], "qty": 3}], "post_now": True})
    acc.client.post("/distributor-payments", {"distributor_id": dist["id"], "amount": "100", "method": "cash", "paid_date": today, "allocations": []})
    cust = bk.client.post("/customers", {"name": "Demo Accounting Customer", "phone": "0300 7770001"})
    chan = bk.client.get("/sales/channels")[0]["id"]
    o = bk.client.post("/orders", {"customer_id": cust["id"], "channel_id": chan, "location_id": B1, "order_date": today, "payment_terms": "cod",
                                   "delivery": {"name": "Recipient", "phone": "0300 7770002", "address": "1 Demo Street", "city": "Demoville"},
                                   "lines": [{"variant_id": var["id"], "qty": 2}]})
    o = bk.client.post(f"/orders/{o['id']}/confirm") if o["status"] == "draft" else o
    shp = stk.client.post(f"/orders/{o['id']}/shipments", {"lines": [{"order_line_id": ln["id"], "qty": ln["to_ship"]} for ln in o["lines"]]})
    stk.client.post(f"/shipments/{shp['id']}/dispatch", {"courier": "DemoCourier", "tracking_no": "DMO1"})
    stk.client.post(f"/shipments/{shp['id']}/outcome", {"result": "delivered"})
    acc.client.post(f"/orders/{o['id']}/payments", {"amount": "50", "method": "cash"})

    def bal(code):
        a = {x["code"]: x for x in ceo.client.get("/accounting/accounts")["items"]}
        return D(a[code]["balance"])

    # ---- who sees what -------------------------------------------------------------------------------------------------------
    for s, name in ((bk, "booker"), (stk, "stock keeper"), (mgr, "manager")):
        sh = Shell(s); sh.resize(1366, 768); sh.show(); pump(0.3)
        check("accounting" not in sh.buttons, f"{name} has no Accounting module")
        sh.close()
    sh = Shell(acc); sh.resize(1366, 768); sh.show(); pump(0.3)
    check("accounting" in sh.buttons, "accounts sees Accounting")
    sh.show_page("accounting"); pump(1.5)
    pg = sh.pages["accounting"]
    check(type(pg).__name__ == "AccountingPage", "Accounting is a real screen, not the placeholder")
    names = [pg.tabs.tabText(i) for i in range(pg.tabs.count())]
    check(names == ["Overview", "Reports", "Journal", "Chart of accounts", "Cash and bank", "Expenses", "Periods", "Opening balances"], f"tabs {names}")
    ov = pg.items[0][1]
    check("Make the ledger official…" not in buttons(ov), "accounts cannot declare the ledger official")
    check("Post pending records" in buttons(ov), "accounts can post pending records")
    check("The ledger is being prepared" in ov.state.text(), f"new ledger is shown as provisional ({ov.state.text()})")

    # ---- posting engine from the overview -------------------------------------------------------------------------------------------
    pend0 = acc.client.get("/accounting/posting/pending")["total"]
    check(pend0 > 0, f"demo operations are waiting to be posted ({pend0})")
    ov.post_btn.click(); pump(2.5)
    pend1 = acc.client.get("/accounting/posting/pending")["total"]
    check(pend1 == 0, f"posting cleared the queue ({pend0} -> {pend1}); {ov.status.text()[:90]}")
    integ = acc.client.get("/accounting/integrity")
    check(all(r["agrees"] for r in integ["rows"]) and integ["trial_balance"]["balanced"], f"ledger agrees with stock, supplier and distributor records ({[ (r['name'][:12], r['difference']) for r in integ['rows']]})")
    jn = acc.client.get("/accounting/journals", size=5)["total"]
    check(acc.client.post("/accounting/posting/run")["posted"] == 0 and acc.client.get("/accounting/journals", size=5)["total"] == jn, "running posting again adds nothing")

    # ---- chart of accounts ---------------------------------------------------------------------------------------------------------------
    ch = pg.items[3][1]
    check("Add account" in buttons(ch), "accounts can add chart accounts")
    d = ap.AccountDialog(acc); d.code.setText(""); click_submit(d)
    check(d.result() == 0 and d.banner.text(), f"empty account refused ({d.banner.text()[:50]})")
    d.code.setText("5950"); d.name.setText("Demo Licences"); d.type.setCurrentIndex(5); click_submit(d)
    check(d.result() == 1 and any(a["code"] == "5950" for a in acc.client.get("/accounting/accounts")["items"]), "account added to the chart")
    d = ap.AccountDialog(acc); d.code.setText("5950"); d.name.setText("Another"); d.type.setCurrentIndex(5); click_submit(d)
    check(d.result() == 0 and d.banner.text(), f"duplicate code refused ({d.banner.text()[:60]})")
    d.close()

    # ---- manual journal: draft by accounts, posted by the CEO ---------------------------------------------------------------------------
    jd = ap.JournalDialog(acc); pump(1.0)
    jd.memo.setText("Demo owner investment"); jd.reason.setText("Owner paid cash into the business"); 
    g = jd.grid.widgets
    pick(g[0]["acct"], "1000"); g[0]["deb"].setText("5000"); pick(g[1]["acct"], "3000"); g[1]["cred"].setText("4000")
    click_submit(jd)
    check(jd.result() == 0 and "balance" in jd.banner.text().lower(), f"unbalanced journal refused ({jd.banner.text()[:70]})")
    g[1]["cred"].setText("5000"); click_submit(jd)
    check(jd.result() == 1, "balanced journal saved as a draft")
    draft = next(e for e in acc.client.get("/accounting/journals", status="draft")["items"] if "owner investment" in e["memo"])
    cash0 = bal("1000")
    dv = ap.JournalDetail(acc, draft["id"]); pump(1.2)
    check("Post this journal" not in buttons(dv) and "Delete draft" in buttons(dv), f"accounts can delete but not post ({buttons(dv)})")
    dv.close()
    dv = ap.JournalDetail(ceo, draft["id"]); pump(1.2)
    check("Post this journal" in buttons(dv), "CEO is offered to post the draft")
    next(b for b in dv.findChildren(QPushButton) if b.text() == "Post this journal").click(); pump(1.5)
    check(bal("1000") == cash0 + 5000, "posting moved the cash balance by exactly the journal amount")
    dv.close()

    # ---- cash accounts and reconciliation -------------------------------------------------------------------------------------------------
    cash = pg.items[4][1]
    check("Reconcile selected…" in buttons(cash) and "Add cash or bank account" in buttons(cash), "accounts can reconcile and add accounts")
    ca = ap.CashAccountDialog(acc); ca.name.setText("Demo petty cash"); ca.kind.setCurrentIndex(0); click_submit(ca)
    check(ca.result() == 1 and any(c["name"] == "Demo petty cash" for c in acc.client.get("/accounting/cash-accounts")["items"]), "new cash account created with its own ledger account")
    bank = next(c for c in acc.client.get("/accounting/cash-accounts")["items"] if c["kind"] == "bank")
    j2 = acc.client.post("/accounting/journals/manual", {"entry_date": __import__("datetime").date.today().isoformat(), "memo": "Demo bank deposit", "reason": "Owner transfer",
                                                         "lines": [{"account": "1010", "debit": "2000", "credit": "0"}, {"account": "3000", "debit": "0", "credit": "2000"}]})
    ceo.client.post(f"/accounting/journals/{j2['id']}/post")
    rd = ap.ReconcileDialog(acc, bank); pump(1.5)
    check(len(rd.lines) >= 1, f"unreconciled bank lines listed ({len(rd.lines)})")
    for cb in rd.checks:
        cb.setChecked(True)
    rd.balance.setText("1"); click_submit(rd)
    check(rd.result() == 0 and "difference" in rd.banner.text().lower(), f"statement that does not agree is refused with the difference ({rd.banner.text()[:80]})")
    total = D(rd.base) + sum((D(ln["amount"]) for ln in rd.lines), D(0))
    rd.balance.setText(str(total)); pump(0.3)
    check("Agrees with the statement" in rd.sum.text(), f"dialog shows agreement before submitting ({rd.sum.text()[-40:]})")
    click_submit(rd)
    check(rd.result() == 1 and acc.client.get(f"/accounting/cash-accounts/{bank['id']}/unreconciled")["items"] == [], "reconciliation completed and lines cleared")

    # ---- expenses ----------------------------------------------------------------------------------------------------------------------------------
    ex = pg.items[5][1]
    check("Record expense" in buttons(ex) and "Approve…" not in buttons(ex), f"accounts records but cannot approve ({buttons(ex)})")
    ed = ap.ExpenseDialog(acc); pump(1.2)
    pick(ed.acct, "Utilities"); pick(ed.paid, "Cash on hand"); ed.amount.setText("350"); ed.payee.setText("Demo Utility Co"); ed.desc.setText("Demo electricity bill")
    click_submit(ed)
    x = next(e for e in acc.client.get("/accounting/expenses")["items"] if e["description"] == "Demo electricity bill")
    check(ed.result() == 1 and x["status"] == "pending", f"expense waits for approval ({x['status']})")
    util0 = bal("5400")
    ex_c = ap.ExpensesTab(ceo); ex_c.refresh(); pump(1.2)
    check(select(ex_c.table, "id", x["id"]), "CEO sees the pending expense")
    ex_c._decide(True); pump(1.0)
    check(bal("5400") == util0 + 350, "approval posted the expense to Utilities")
    check(acc.client.get("/accounting/expenses", status="posted")["items"][0]["status"] == "posted", "expense is posted")

    # ---- reports -----------------------------------------------------------------------------------------------------------------------------------------
    rp = pg.items[1][1]
    shots = {}
    for i, (title, key, need) in enumerate(ap.REPORT_SPECS):
        rp.kind.setCurrentIndex(i); rp._params_changed()
        if "account" in need:
            pick(rp.account, "1000")
        if "party" in need:
            rp.ptype.setCurrentIndex(0); pump(1.2); pick(rp.party, "Demo Accounting Customer")
        rp.d_from.setDate(rp.d_from.date().addMonths(-3)); rp.run(); pump(1.6)
        rows = rp.table.rowCount() if rp.table is not None else 0
        ok_rows = rows > 0 or key == "aging/payables"
        check(bool(rp.title.text()), f"report ran: {title} -> '{rp.title.text()}'")
        check(ok_rows and not (rp.status.isVisible() and "error" in rp.status.objectName().lower()), f"{title}: {rows} row(s)")
        if key == "statement":
            check("Demo Accounting Customer" in rp.title.text(), f"statement is for the chosen customer ({rp.title.text()})")
        if key == "sales":
            check(rp.table.rowCount() >= 1, "sales report has the delivered order")
        if key == "aging/receivables":
            check(any("Demo Accounting" in str(r.get("party")) for r in rp.table._rows), "receivables ageing lists the customer and the distributor")
        if key == "trial-balance":
            check("Balanced: Yes" in rp.totals.text(), f"trial balance balances ({rp.totals.text()[:80]})")
        if key == "balance-sheet":
            check("Balanced: Yes" in rp.totals.text(), f"balance sheet balances ({rp.totals.text()[:90]})")
        if key == "cash-flow":
            check("Agrees with ledger: Yes" in rp.totals.text(), f"cash flow agrees with the ledger ({rp.totals.text()[-60:]})")
        shots[key] = True
    tbj = acc.client.get("/accounting/reports/trial-balance")
    check(tbj["totals"]["balanced"] and any("not yet official" in n for n in tbj["notes"]), "reports warn that the ledger is provisional")

    # ---- periods ---------------------------------------------------------------------------------------------------------------------------------------
    pe = pg.items[6][1]; pe.refresh(); pump(1.5)
    check("Close selected month" in buttons(pe) and "Request reopening…" not in buttons(pe) and "Approve reopening…" not in buttons(pe), f"accounts can close but not reopen/approve ({buttons(pe)})")
    pe_c = ap.PeriodsTab(ceo); pe_c.refresh(); pump(1.5)
    check("Request reopening…" in buttons(pe_c) and "Approve reopening…" in buttons(pe_c), "CEO can request and approve reopening")
    cur = next(p for p in pe_c.table._rows if p["start_date"] <= __import__("datetime").date.today().isoformat() <= p["end_date"])
    select(pe_c.table, "id", cur["id"]); pe_c._close(); pump(1.2)
    check("not ended" in pe_c.status.text().lower() or "has not ended" in pe_c.status.text().lower(), f"current month cannot be closed early ({pe_c.status.text()[:70]})")

    # ---- opening balances and go-live ------------------------------------------------------------------------------------------------------------------
    op = pg.items[7][1]
    check("New batch" in buttons(op) and "Approve selected…" not in buttons(op), f"accounts prepares but cannot approve opening balances ({buttons(op)})")
    od = ap.OpeningBatchDialog(acc); pump(2.0)
    od.desc.setText("Demo opening cash and capital")
    g = od.grid.widgets
    pick(g[0]["acct"], "1500"); g[0]["deb"].setText("1200"); g[0]["ev"].setText("Equipment list signed by owner")
    pick(g[1]["acct"], "3000"); g[1]["cred"].setText("1200"); g[1]["ev"].setText("Owner statement")
    click_submit(od)
    b = acc.client.get("/accounting/opening-batches")["items"][0]
    check(od.result() == 1 and b["status"] == "draft" and b["unverified"] == 2, f"draft saved with unverified lines ({b['ref']})")
    op_c = ap.OpeningTab(ceo); op_c.refresh(); pump(1.2)
    select(op_c.table, "id", b["id"]); op_c._approve(); pump(1.5)
    check("verified" in op_c.status.text().lower() or "not marked" in op_c.status.text().lower(), f"unverified batch cannot be approved ({op_c.status.text()[:90]})")
    od2 = ap.OpeningBatchDialog(acc); pump(2.0)
    od2.desc.setText("Demo verified batch"); g = od2.grid.widgets
    pick(g[0]["acct"], "1500"); g[0]["deb"].setText("1200"); g[0]["ev"].setText("Equipment list signed by owner"); g[0]["ver"].setChecked(True)
    pick(g[1]["acct"], "3000"); g[1]["cred"].setText("1200"); g[1]["ev"].setText("Owner statement"); g[1]["ver"].setChecked(True)
    click_submit(od2)
    b2 = acc.client.get("/accounting/opening-batches")["items"][0]
    check(od2.result() == 1 and b2["unverified"] == 0, "verified batch saved")
    op_a = ap.OpeningTab(acc); op_a.refresh(); pump(1.0)
    other = bal("1500")
    acc.client.post(f"/accounting/opening-batches/{b['id']}/cancel")            # the unverified draft would otherwise block go-live
    op_c.refresh(); pump(1.2); select(op_c.table, "id", b2["id"]); op_c._approve(); pump(1.5)
    check(bal("1500") == other + 1200, "approval by a different person posted the opening balances")
    gl = ceo.client.get("/accounting/go-live")
    print("      go-live checks:", [(c["name"][:40], c["ok"]) for c in gl["checks"]])
    check(isinstance(gl["checks"], list) and len(gl["checks"]) == 6, "go-live lists its six requirements")
    obe = bal("3200")
    if obe != 0:
        side = {"debit": str(abs(obe)), "credit": "0"} if obe < 0 else {"debit": "0", "credit": str(obe)}
        other_side = {"debit": side["credit"], "credit": side["debit"]}
        bb = acc.client.post("/accounting/opening-batches", {"as_of_date": __import__("datetime").date.today().isoformat(), "description": "Demo: move opening equity to owner capital",
                                                             "lines": [{"account": "3200", **side, "evidence": "Owner agreement", "verified": True},
                                                                       {"account": "3000", **other_side, "evidence": "Owner agreement", "verified": True}]})
        ceo.client.post(f"/accounting/opening-batches/{bb['id']}/approve")
    acc.client.post("/accounting/posting/run")
    gl = ceo.client.get("/accounting/go-live")
    check(gl["ready"], f"go-live checks all met ({[c['name'][:30] for c in gl['checks'] if not c['ok']]})")
    ov_c = ap.OverviewTab(ceo); ov_c.refresh(); pump(1.5)
    check("Make the ledger official…" in buttons(ov_c), "CEO is offered to make the ledger official")
    ov_c._go_live(); pump(1.5)
    st = ceo.client.get("/accounting/overview")["settings"]["ledger_status"]
    check(st == "official", f"ledger declared official by someone who did not enter the opening balances ({st})")
    ov_c.refresh(); pump(1.2)
    check("official" in ov_c.state.text().lower() and "being prepared" not in ov_c.state.text(), f"overview shows the ledger as official ({ov_c.state.text()})")
    check(not any("not yet official" in n for n in ceo.client.get("/accounting/reports/trial-balance")["notes"]), "provisional warning disappears from reports")

    # ---- screens at both sizes ------------------------------------------------------------------------------------------------------------------------
    for w_, h_ in ((1366, 768), (1920, 1080)):
        for who, label_ in ((ceo, "ceo"), (acc, "accounts")):
            sh = Shell(who); sh.resize(w_, h_); sh.show(); pump(0.3)
            sh.show_page("accounting"); pg = sh.pages["accounting"]
            for i in range(pg.tabs.count()):
                pg.tabs.setCurrentIndex(i)
                if pg.tabs.tabText(i) == "Reports":
                    rpt = pg.items[i][1]
                    rpt.kind.setCurrentIndex(1); rpt._params_changed(); rpt.d_from.setDate(rpt.d_from.date().addMonths(-3)); rpt.run()
                pump(2.0)
                sh.grab().save(str(out / f"accounting_{label_}_tab{i}_{w_}.png"))
            sh.close()
        dlgs = {"journal_dialog": ap.JournalDialog(acc), "expense_dialog": ap.ExpenseDialog(acc), "opening_dialog": ap.OpeningBatchDialog(acc),
                "reconcile_dialog": ap.ReconcileDialog(acc, bank), "account_dialog": ap.AccountDialog(acc), "cash_dialog": ap.CashAccountDialog(acc),
                "journal_detail": ap.JournalDetail(ceo, ceo.client.get("/accounting/journals", size=1)["items"][0]["id"])}
        for name, dlg in dlgs.items():
            dlg.resize(min(max(dlg.sizeHint().width(), dlg.minimumWidth()) + 20, w_ - 40), min(dlg.sizeHint().height() + 40, h_ - 60))
            dlg.show(); pump(1.8); dlg.grab().save(str(out / f"{name}_{w_}.png")); dlg.close()

    print("\nFAILED CHECKS:" if E.FAILS else "\nALL CHECKS PASSED", *E.FAILS, sep="\n  ")
    sys.exit(1 if E.FAILS else 0)


if __name__ == "__main__":
    main()
