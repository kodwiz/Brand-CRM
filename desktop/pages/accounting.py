"""Accounting: overview and posting status, chart of accounts, journals, cash and bank (with reconciliation), expenses, financial reports,
periods and opening balances. Every rule (balanced entries, closed periods, separation of duties, approval limits) is enforced by the backend;
this file collects input and shows the results. All figures come from posted journal entries."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLineEdit, QPlainTextEdit, QScrollArea, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from desktop.pages.base import Page
from desktop.pages.inventory import _money
from desktop.pages.suppliers import _Detail, _Tab, _cap, _date_edit, _dec, _iso
from desktop.session import Session
from desktop.ui.forms import FormDialog, FormGrid
from desktop.ui.widgets import Banner, Column, DataTable, PageHeader, Pager, StatePanel, button, label
from desktop.ui.worker import run_async

JSTATUS = {"draft": "warn", "posted": "ok", "reversed": "neutral"}
ESTATUS = {"pending": "warn", "posted": "ok", "rejected": "neutral", "void": "neutral"}
BSTATUS = {"draft": "warn", "posted": "ok", "cancelled": "neutral"}
PSTATUS = {"open": "ok", "closed": "neutral"}
ACCOUNT_TYPES = [("Asset", "asset"), ("Liability", "liability"), ("Equity", "equity"), ("Income", "income"), ("Reduction of income (returns, discounts)", "contra_income"),
                 ("Expense", "expense")]
SOURCE_TEXT = {"stock_txn": "Stock movement", "supplier_ledger": "Supplier ledger", "distributor_ledger": "Distributor ledger", "order_payment": "Customer payment",
               "shipment": "Dispatched order", "sales_return": "Customer return", "expense": "Expense", "opening_batch": "Opening balances"}


def _signed(v) -> str:
    """Debit-minus-credit shown the way an accountant reads it."""
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return "-"
    return "0.00" if d == 0 else f"{abs(d):,.2f} {'Dr' if d > 0 else 'Cr'}"


def _num(v) -> str:
    try:
        return f"{Decimal(str(v)):,.2f}" if v not in (None, "") else ""
    except InvalidOperation:
        return str(v)


def _plain_error(e) -> str:
    return getattr(e, "message", str(e))


def _account_combo(session: Session, types: tuple[str, ...] | None = None, placeholder: str = "Select an account…", editable_only: bool = False) -> QComboBox:
    cb = QComboBox()
    cb.setMinimumWidth(260)
    cb.addItem(placeholder, None)
    c = session.client

    def fill(d):
        for a in d["items"]:
            if types and a["type"] not in types:
                continue
            if editable_only and a["is_control"]:
                continue
            cb.addItem(f"{a['code']}  {a['name']}", a["id"])
            cb.setItemData(cb.count() - 1, a, Qt.ItemDataRole.UserRole + 1)
    run_async(lambda: c.get("/accounting/accounts"), fill, lambda e: None)
    return cb


def _cash_combo(session: Session) -> QComboBox:
    cb = QComboBox()
    cb.setMinimumWidth(220)
    cb.addItem("Select cash or bank account…", None)
    c = session.client
    run_async(lambda: c.get("/accounting/cash-accounts"), lambda d: [cb.addItem(x["name"], x["id"]) for x in d["items"] if x["is_active"]], lambda e: None)
    return cb


class TableForm(QTableWidget):
    """A small grid of editable rows used for journal lines and opening-balance lines."""

    def __init__(self, headers: list[str], widths: list[int], make_row):
        super().__init__(0, len(headers))
        self.make_row = make_row
        self.setHorizontalHeaderLabels(headers)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(36)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setShowGrid(False)
        hh = self.horizontalHeader()
        for i, w in enumerate(widths):
            if w:
                hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
                self.setColumnWidth(i, w)
            else:
                hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
        self.widgets: list[dict] = []

    def add_row(self) -> dict:
        w = self.make_row()
        r = self.rowCount()
        self.insertRow(r)
        for i, key in enumerate(w["_order"]):
            self.setCellWidget(r, i, w[key])
        self.widgets.append(w)
        return w

    def remove_last(self) -> None:
        if self.rowCount() > 2:
            self.removeRow(self.rowCount() - 1)
            self.widgets.pop()


# ------------------------------------------------------------------------------------------------ dialogs
class AccountDialog(FormDialog):
    def __init__(self, session: Session, parent=None):
        super().__init__("Add an account", "Add account", parent, 520)
        self.session = session
        g = FormGrid(2)
        self.code = QLineEdit(); self.code.setPlaceholderText("e.g. 5950")
        self.name = QLineEdit()
        self.type = QComboBox()
        for t, v in ACCOUNT_TYPES:
            self.type.addItem(t, v)
        self.desc = QLineEdit()
        g.add("code", "Code", self.code); g.add("type", "Type", self.type)
        g.add("name", "Name", self.name, span=2); g.add("desc", "Description (optional)", self.desc, span=2)
        self.body.addWidget(g)
        self.body.addWidget(label("New accounts appear in the chart immediately. Accounts used by automatic postings cannot be removed or switched off.", "muted", wrap=True))

    def submit(self) -> None:
        body = {"code": self.code.text().strip(), "name": self.name.text().strip(), "type": self.type.currentData(), "description": self.desc.text().strip() or None}
        if not body["code"] or not body["name"]:
            self.finish_error("Enter a code and a name."); return
        c = self.session.client
        run_async(lambda: c.post("/accounting/accounts", json=body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class CashAccountDialog(FormDialog):
    def __init__(self, session: Session, parent=None):
        super().__init__("Add a cash or bank account", "Add account", parent, 520)
        self.session = session
        g = FormGrid(2)
        self.name = QLineEdit(); self.name.setPlaceholderText("e.g. Petty cash, Meezan current account")
        self.kind = QComboBox(); self.kind.addItem("Cash", "cash"); self.kind.addItem("Bank", "bank")
        self.bank = QLineEdit(); self.ref = QLineEdit(); self.ref.setPlaceholderText("Last digits only")
        g.add("name", "Name", self.name, span=2); g.add("kind", "Kind", self.kind); g.add("bank", "Bank (optional)", self.bank); g.add("ref", "Account reference (optional)", self.ref, span=2)
        self.body.addWidget(g)

    def submit(self) -> None:
        body = {"name": self.name.text().strip(), "kind": self.kind.currentData(), "bank_name": self.bank.text().strip() or None, "account_ref": self.ref.text().strip() or None}
        if not body["name"]:
            self.finish_error("Enter a name."); return
        c = self.session.client
        run_async(lambda: c.post("/accounting/cash-accounts", json=body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class JournalDialog(FormDialog):
    """Manual journal. It is saved as a draft; a second authorised person posts it."""

    def __init__(self, session: Session, parent=None):
        super().__init__("New manual journal", "Save as draft", parent, 1000)
        self.session = session
        g = FormGrid(3)
        self.date = _date_edit()
        self.memo = QLineEdit(); self.memo.setPlaceholderText("What this entry records")
        self.reason = QLineEdit(); self.reason.setPlaceholderText("Why a manual entry is needed")
        g.add("date", "Date", self.date); g.add("memo", "Description", self.memo, span=2); g.add("reason", "Reason", self.reason, span=3)
        self.body.addWidget(g)
        self.accounts: list[dict] = []
        self.grid = TableForm(["Account", "Debit", "Credit", "Note"], [330, 130, 130, 0], self._row)
        self.grid.setMinimumHeight(190)
        self.body.addWidget(self.grid)
        row = QHBoxLayout()
        row.addWidget(button("Add line", on_click=self._add)); row.addWidget(button("Remove last line", on_click=self.grid.remove_last)); row.addStretch()
        self.diff = label("", "muted")
        row.addWidget(self.diff)
        self.body.addLayout(row)
        for _ in range(2):
            self.grid.add_row()
        self.body.addWidget(label("Accounts filled by stock, supplier, distributor and employee records (inventory, payables, distributor and employee receivables) are not offered here: they follow their own records.", "muted", wrap=True))

    def _add(self) -> None:
        self.grid.add_row()

    def _row(self) -> dict:
        acct = _account_combo(self.session, editable_only=True)
        deb, cred, memo = QLineEdit(), QLineEdit(), QLineEdit()
        for w in (deb, cred):
            w.setPlaceholderText("0.00"); w.setAlignment(Qt.AlignmentFlag.AlignRight); w.textChanged.connect(self._sum)
        return {"_order": ["acct", "deb", "cred", "memo"], "acct": acct, "deb": deb, "cred": cred, "memo": memo}

    def _sum(self) -> None:
        d = sum((_dec(w["deb"].text()) or Decimal(0) for w in self.grid.widgets), Decimal(0))
        c = sum((_dec(w["cred"].text()) or Decimal(0) for w in self.grid.widgets), Decimal(0))
        self.diff.setText(f"Debits {d:,.2f}   Credits {c:,.2f}   " + ("Balanced" if d == c and d > 0 else f"Difference {d - c:,.2f}"))

    def submit(self) -> None:
        lines = []
        for w in self.grid.widgets:
            d, c = _dec(w["deb"].text()), _dec(w["cred"].text())
            if w["acct"].currentData() is None and not (d or c):
                continue
            if w["acct"].currentData() is None:
                self.finish_error("Choose an account for every line that has an amount."); return
            lines.append({"account_id": w["acct"].currentData(), "debit": str(d or 0), "credit": str(c or 0), "memo": w["memo"].text().strip() or None})
        body = {"entry_date": _iso(self.date), "memo": self.memo.text().strip(), "reason": self.reason.text().strip(), "lines": lines}
        if not body["memo"] or not body["reason"]:
            self.finish_error("Enter a description and a reason."); return
        c = self.session.client
        run_async(lambda: c.post("/accounting/journals/manual", json=body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class ExpenseDialog(FormDialog):
    def __init__(self, session: Session, parent=None):
        super().__init__("Record an expense", "Record expense", parent, 700)
        self.session = session
        self.key = uuid.uuid4().hex
        g = FormGrid(2)
        self.date = _date_edit()
        self.acct = QComboBox(); self.acct.addItem("Select an expense type…", None)
        self.paid = _cash_combo(session)
        self.amount = QLineEdit(); self.amount.setPlaceholderText("0.00")
        self.payee = QLineEdit(); self.desc = QLineEdit(); self.receipt = QLineEdit(); self.receipt.setPlaceholderText("Bill or receipt number")
        g.add("date", "Date", self.date); g.add("amount", "Amount", self.amount); g.add("acct", "Expense type", self.acct); g.add("paid", "Paid from", self.paid)
        g.add("payee", "Paid to", self.payee); g.add("receipt", "Receipt / bill number", self.receipt); g.add("desc", "What it was for", self.desc, span=2)
        self.body.addWidget(g)
        self.body.addWidget(label("Expenses above the approval limit wait for another person to approve them before they reach the ledger.", "muted", wrap=True))
        c = session.client
        run_async(lambda: c.get("/accounting/expense-accounts"), lambda d: [self.acct.addItem(f"{a['name']}", a["id"]) for a in d["items"]], lambda e: None)

    def submit(self) -> None:
        amt = _dec(self.amount.text())
        if self.acct.currentData() is None or self.paid.currentData() is None or not amt or amt <= 0 or not self.desc.text().strip():
            self.finish_error("Choose the expense type and the account it was paid from, then enter an amount and what it was for."); return
        body = {"expense_date": _iso(self.date), "account_id": self.acct.currentData(), "cash_account_id": self.paid.currentData(), "amount": str(amt),
                "payee": self.payee.text().strip() or None, "description": self.desc.text().strip(), "receipt_ref": self.receipt.text().strip() or None,
                "idempotency_key": self.key}
        c = self.session.client
        run_async(lambda: c.post("/accounting/expenses", json=body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class NoteDialog(FormDialog):
    def __init__(self, title: str, prompt: str, submit_text: str, call, parent=None, multiline: bool = False):
        super().__init__(title, submit_text, parent, 520)
        self.call = call
        self.body.addWidget(label(prompt, "muted", wrap=True))
        self.text = QPlainTextEdit() if multiline else QLineEdit()
        if multiline:
            self.text.setMinimumHeight(120)
        self.body.addWidget(self.text)

    def submit(self) -> None:
        t = (self.text.toPlainText() if isinstance(self.text, QPlainTextEdit) else self.text.text()).strip()
        if not t:
            self.finish_error("This cannot be empty."); return
        run_async(lambda: self.call(t), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class ReconcileDialog(FormDialog):
    """Match ledger lines to a bank or cash statement. The backend refuses unless the selected lines add up exactly to the statement balance."""

    def __init__(self, session: Session, cash: dict, parent=None):
        super().__init__(f"Reconcile {cash['name']}", "Complete reconciliation", parent, 960)
        self.session, self.cash = session, cash
        g = FormGrid(3)
        self.date = _date_edit()
        self.balance = QLineEdit(); self.balance.setPlaceholderText("Closing balance on the statement"); self.balance.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.notes = QLineEdit()
        g.add("date", "Statement date", self.date); g.add("balance", "Statement closing balance", self.balance); g.add("notes", "Notes (optional)", self.notes)
        self.body.addWidget(g)
        self.body.addWidget(label("Tick the ledger lines that appear on the statement. The tick-total plus what was reconciled before must equal the statement balance; "
                                  "nothing is adjusted automatically.", "muted", wrap=True))
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["", "Date", "Entry", "Description", "Amount"])
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        for i, w in ((0, 36), (1, 100), (2, 110), (4, 120)):
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive); self.table.setColumnWidth(i, w)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(260)
        self.body.addWidget(self.table)
        self.sum = label("", "muted")
        self.body.addWidget(self.sum)
        self.base = Decimal(0)
        self.lines: list[dict] = []
        self.checks: list[QCheckBox] = []
        self.balance.textChanged.connect(self._update)
        c = session.client
        run_async(lambda: c.get(f"/accounting/cash-accounts/{cash['id']}/unreconciled"), self._loaded, lambda e: self.banner.show_message(e.message, "error"))

    def _loaded(self, d) -> None:
        self.base = Decimal(d["cleared_balance"])
        self.lines = d["items"]
        if not self.lines:
            self.banner.show_message("Nothing is waiting to be matched on this account: every ledger line has already been reconciled.", "info")
        self.table.setRowCount(len(self.lines))
        for r, ln in enumerate(self.lines):
            cb = QCheckBox(); cb.stateChanged.connect(self._update)
            self.checks.append(cb)
            self.table.setCellWidget(r, 0, cb)
            for c, key in ((1, "entry_date"), (2, "ref"), (3, "memo"), (4, "amount")):
                it = QTableWidgetItem(_num(ln[key]) if key == "amount" else str(ln[key]))
                if key == "amount":
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, c, it)
        self._update()

    def _picked(self) -> list[int]:
        return [ln["line_id"] for ln, cb in zip(self.lines, self.checks) if cb.isChecked()]

    def _update(self) -> None:
        picked = sum((Decimal(ln["amount"]) for ln, cb in zip(self.lines, self.checks) if cb.isChecked()), Decimal(0))
        exp = self.base + picked
        bal = _dec(self.balance.text())
        txt = f"Reconciled before {self.base:,.2f}  +  ticked {picked:,.2f}  =  {exp:,.2f}"
        if bal is not None:
            diff = bal - exp
            txt += "   ·   " + ("Agrees with the statement" if diff == 0 else f"Difference from statement {diff:,.2f}")
        self.sum.setText(txt)

    def submit(self) -> None:
        bal = _dec(self.balance.text())
        if bal is None:
            self.finish_error("Enter the statement closing balance."); return
        body = {"statement_date": _iso(self.date), "statement_balance": str(bal), "line_ids": self._picked(), "notes": self.notes.text().strip() or None}
        c = self.session.client
        run_async(lambda: c.post(f"/accounting/cash-accounts/{self.cash['id']}/reconcile", json=body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class OpeningBatchDialog(FormDialog):
    """Opening balances: each line carries its evidence and a verified flag. Unverified lines cannot be approved."""

    def __init__(self, session: Session, parent=None):
        super().__init__("New opening-balance batch", "Save draft", parent, 1180)
        self.session = session
        g = FormGrid(3)
        self.date = _date_edit()
        self.desc = QLineEdit(); self.desc.setPlaceholderText("e.g. Opening balances at 1 July from the paper registers")
        g.add("date", "Balances as at", self.date); g.add("desc", "Description", self.desc, span=2)
        self.body.addWidget(g)
        self.body.addWidget(label("Enter only figures you can support with a document. Tick Verified once someone has checked the figure against its source; unverified lines "
                                  "are kept but cannot be posted. Opening stock, supplier payables and distributor balances are entered in their own modules. "
                                  "The batch must balance: add an owner-capital line if needed; nothing is balanced automatically.", "muted", wrap=True))
        self.customers: list[dict] = []
        self.suppliers: list[dict] = []
        c = session.client
        run_async(lambda: c.get("/customers", page_size=200), lambda d: self.customers.extend(d["items"]), lambda e: None)
        run_async(lambda: c.get("/suppliers", page_size=200), lambda d: self.suppliers.extend(d["items"]), lambda e: None)
        self.grid = TableForm(["Account", "Debit", "Credit", "Customer / supplier", "Evidence (document, who checked)", "Verified"], [250, 110, 110, 190, 0, 70], self._row)
        self.grid.setMinimumHeight(210)
        self.body.addWidget(self.grid)
        row = QHBoxLayout()
        row.addWidget(button("Add line", on_click=lambda: self.grid.add_row())); row.addWidget(button("Remove last line", on_click=self.grid.remove_last)); row.addStretch()
        self.diff = label("", "muted"); row.addWidget(self.diff)
        self.body.addLayout(row)
        for _ in range(2):
            self.grid.add_row()

    def _row(self) -> dict:
        acct = _account_combo(self.session, editable_only=False)
        deb, cred, ev = QLineEdit(), QLineEdit(), QLineEdit()
        for w in (deb, cred):
            w.setPlaceholderText("0.00"); w.setAlignment(Qt.AlignmentFlag.AlignRight); w.textChanged.connect(self._sum)
        party = QComboBox(); party.addItem("-", None); party.setEnabled(False)
        ver = QCheckBox()
        acct.currentIndexChanged.connect(lambda _=0, a=acct, p=party: self._account_changed(a, p))
        return {"_order": ["acct", "deb", "cred", "party", "ev", "ver"], "acct": acct, "deb": deb, "cred": cred, "party": party, "ev": ev, "ver": ver}

    def _account_changed(self, acct: QComboBox, party: QComboBox) -> None:
        a = acct.currentData(Qt.ItemDataRole.UserRole + 1) or {}
        party.clear(); party.addItem("-", None)
        src, ptype = (self.suppliers, "supplier") if a.get("code") == "1300" else (self.customers, "customer")
        if a.get("requires_party"):
            for p in src:
                party.addItem(p["name"], (ptype, p["id"]))
            party.setEnabled(True)
        else:
            party.setEnabled(False)

    def _sum(self) -> None:
        d = sum((_dec(w["deb"].text()) or Decimal(0) for w in self.grid.widgets), Decimal(0))
        c = sum((_dec(w["cred"].text()) or Decimal(0) for w in self.grid.widgets), Decimal(0))
        self.diff.setText(f"Debits {d:,.2f}   Credits {c:,.2f}   " + ("Balanced" if d == c and d > 0 else f"Difference {d - c:,.2f}"))

    def submit(self) -> None:
        lines = []
        for w in self.grid.widgets:
            d, c = _dec(w["deb"].text()), _dec(w["cred"].text())
            if w["acct"].currentData() is None and not (d or c):
                continue
            if w["acct"].currentData() is None:
                self.finish_error("Choose an account for every line that has an amount."); return
            p = w["party"].currentData()
            lines.append({"account_id": w["acct"].currentData(), "debit": str(d or 0), "credit": str(c or 0), "party_type": p[0] if p else None, "party_id": p[1] if p else None,
                          "evidence": w["ev"].text().strip() or None, "verified": w["ver"].isChecked()})
        if not self.desc.text().strip():
            self.finish_error("Describe the batch."); return
        body = {"as_of_date": _iso(self.date), "description": self.desc.text().strip(), "lines": lines}
        c = self.session.client
        run_async(lambda: c.post("/accounting/opening-batches", json=body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class JournalDetail(_Detail):
    def __init__(self, session: Session, entry_id: int, parent=None):
        super().__init__(session, "Journal entry", parent, (940, 560))
        self.entry_id = entry_id
        self.table = DataTable([Column("account", "Account"), Column("debit", "Debit", 120, "right", fmt=lambda v: _num(v) if Decimal(str(v or 0)) else ""),
                                Column("credit", "Credit", 120, "right", fmt=lambda v: _num(v) if Decimal(str(v or 0)) else ""), Column("memo", "Note", 260)])
        self.lay.addWidget(self.table, 1)
        self.actions = QHBoxLayout(); self.actions.addStretch()
        self.lay.addLayout(self.actions)
        self.load()

    def load(self) -> None:
        c = self.session.client
        run_async(lambda: c.get(f"/accounting/journals/{self.entry_id}"), self._show, self.fail)

    def _show(self, e: dict) -> None:
        self.e = e
        self.head.setText(f"{e['ref']}  ·  {_cap(e['status'])}")
        src = SOURCE_TEXT.get(e.get("source_type") or "", "Entered by hand")
        extra = f"  ·  originally dated {e['original_date']} (that period was closed)" if e.get("original_date") else ""
        self.sub.setText(f"{e['entry_date']}  ·  {e['memo']}\n{src}  ·  period {e['period']}  ·  prepared by {e.get('created_by') or 'system'}"
                         f"{'  ·  posted by ' + e['posted_by'] if e.get('posted_by') else ''}{extra}" + (f"\nReason: {e['reason']}" if e.get("reason") else ""))
        self.table.set_rows(e["lines"])
        while self.actions.count() > 1:
            w = self.actions.takeAt(0).widget()
            if w:
                w.deleteLater()
        s = self.session
        if e["status"] == "draft" and s.can("accounting.post"):
            self.actions.insertWidget(0, button("Delete draft", on_click=self._delete))
        if e["status"] == "draft" and s.can("accounting.approve"):
            self.actions.insertWidget(0, button("Post this journal", "primary", lambda: self.act(lambda: s.client.post(f"/accounting/journals/{self.entry_id}/post"), "Journal posted.")))
        if e["status"] == "posted" and e["kind"] in ("manual", "opening") and s.can("accounting.approve"):
            self.actions.insertWidget(0, button("Reverse…", on_click=self._reverse))

    def _delete(self) -> None:
        c = self.session.client
        run_async(lambda: c.delete(f"/accounting/journals/{self.entry_id}"), lambda _: (setattr(self, "changed", True), self.accept()), self.fail)

    def _reverse(self) -> None:
        c = self.session.client
        dlg = NoteDialog("Reverse journal", "A reversal posts the opposite entry today; the original stays on record. A different person from the one who posted it must reverse it.",
                         "Reverse", lambda t: c.post(f"/accounting/journals/{self.entry_id}/reverse", json={"reason": t}), self)
        if dlg.exec():
            self.changed = True; self.status.show_message("Reversal posted.", "ok"); self.load()


# ------------------------------------------------------------------------------------------------ tabs
class OverviewTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        self.state = label("", "sectionTitle")
        self.info = label("", "muted", wrap=True)
        self.lay.addWidget(self.state); self.lay.addWidget(self.info)
        row = QHBoxLayout()
        self.post_btn = button("Post pending records", "primary", self._post)
        self.post_btn.setVisible(session.can("accounting.post"))
        row.addWidget(self.post_btn)
        self.live_btn = button("Make the ledger official…", on_click=self._go_live)
        self.live_btn.setVisible(session.can("accounting.approve"))
        row.addWidget(self.live_btn)
        row.addStretch()
        self.lay.addLayout(row)
        self.lay.addWidget(label("Does the ledger agree with the stock, supplier, distributor and employee records?", "fieldLabel"))
        self.integ = DataTable([Column("name", "Check"), Column("ledger", "Ledger", 130, "right", fmt=_num), Column("subledger", "Records", 130, "right", fmt=_num),
                                Column("difference", "Difference", 120, "right", fmt=_num), Column("agrees", "Agrees", 90, status=lambda v: "ok" if v else "error", fmt=lambda v: "Yes" if v else "No")])
        self.integ.setMaximumHeight(190)
        self.lay.addWidget(self.integ)
        self.lay.addWidget(label("Before the ledger can be declared official", "fieldLabel"))
        self.checks = DataTable([Column("name", "Requirement"), Column("detail", "Detail", 340), Column("ok", "Met", 80, status=lambda v: "ok" if v else "error", fmt=lambda v: "Yes" if v else "No")])
        self.lay.addWidget(self.checks, 1)
        self.data: dict = {}

    def refresh(self) -> None:
        c = self.session.client

        def load():
            return {"o": c.get("/accounting/overview"), "i": c.get("/accounting/integrity")}

        def done(d):
            o, i = d["o"], d["i"]
            self.data = o
            st = o["settings"]
            official = st["ledger_status"] == "official"
            self.state.setText("The ledger is official" if official else "The ledger is being prepared: figures are provisional")
            pend = o["pending"]["total"]
            self.info.setText(f"{pend} operational record(s) waiting to be posted to the ledger  ·  {o['drafts']} draft journal(s)  ·  {o['pending_expenses']} expense(s) awaiting approval  ·  "
                              f"{o['pending_reopen']} period reopening request(s)  ·  current period {o['current_period']['name']} ({o['current_period']['status']})\n"
                              f"Revenue is recognised at {'dispatch' if st['revenue_recognition'] == 'dispatch' else 'delivery'}  ·  stock is valued at weighted-average cost  ·  "
                              f"expenses up to {_num(st['expense_approval_limit'])} post without a second approver.")
            self.post_btn.setEnabled(pend > 0)
            self.live_btn.setEnabled(not official)
            self.integ.set_rows(i["rows"])
            self.checks.set_rows(o["go_live"]["checks"])
        run_async(load, done, lambda e: self.status.show_message(e.message, "error"))

    def _post(self) -> None:
        c = self.session.client
        self.post_btn.setEnabled(False)

        def done(r):
            msg = f"Posted {r['posted']} record(s)" + (f"; {r['skipped']} need no entry" if r.get("skipped") else "") + (f"; {len(r['errors'])} could not be posted: {r['errors'][0]['message']}" if r["errors"] else ".")
            self.status.show_message(msg, "error" if r["errors"] else "ok"); self.refresh()
        run_async(lambda: c.post("/accounting/posting/run"), done, lambda e: (self.status.show_message(e.message, "error"), self.refresh()))

    def _go_live(self) -> None:
        c = self.session.client
        dlg = NoteDialog("Declare the ledger official", "Write the reconciliation statement: who checked the opening balances, against which documents, and any differences found. "
                         "A different person from the one who entered the opening balances must do this.", "Make official",
                         lambda t: c.post("/accounting/go-live", json={"note": t}), self, multiline=True)
        if dlg.exec():
            self.status.show_message("The ledger is now official.", "ok"); self.refresh()


class ChartTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.inactive = QCheckBox("Show switched-off accounts"); self.inactive.stateChanged.connect(lambda _: self.refresh())
        bar.addWidget(self.inactive); bar.addStretch()
        if session.can("accounting.manage_coa"):
            bar.addWidget(button("Add account", "primary", self._add))
        self.lay.addLayout(bar)
        self.table = DataTable([Column("code", "Code", 80), Column("name", "Account"), Column("type", "Type", 130, fmt=_cap), Column("category", "Section", 150, fmt=_cap),
                                Column("balance", "Balance", 150, "right", fmt=_signed), Column("flags", "Notes", 220)])
        self.panel = StatePanel(self.table, "No accounts yet.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client

        def done(d):
            for a in d["items"]:
                f = []
                if a["is_control"]:
                    f.append("filled automatically")
                elif a["is_system"]:
                    f.append("system account")
                if not a["is_active"]:
                    f.append("switched off")
                a["flags"] = ", ".join(f)
            self.table.set_rows(d["items"]); self.panel.show_content() if d["items"] else self.panel.show_empty()
        run_async(lambda: c.get("/accounting/accounts", include_inactive="true" if self.inactive.isChecked() else None), done, lambda e: self.panel.show_error(e.message, e.offline))

    def _add(self) -> None:
        if AccountDialog(self.session, self).exec():
            self.status.show_message("Account added.", "ok"); self.refresh()


class JournalTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("Search entry number or description"); self.search.setMinimumWidth(280)
        self.state = QComboBox(); self.state.addItem("All states", None)
        for k in ("draft", "posted", "reversed"):
            self.state.addItem(_cap(k), k)
        self.search.returnPressed.connect(self._reset); self.state.currentIndexChanged.connect(lambda _: self._reset())
        bar.addWidget(self.search); bar.addWidget(self.state); bar.addStretch()
        if session.can("accounting.post"):
            bar.addWidget(button("New manual journal", "primary", self._new))
        self.lay.addLayout(bar)
        self.table = DataTable([Column("ref", "Entry", 100), Column("entry_date", "Date", 100), Column("memo", "Description"), Column("source_type", "Source", 150, fmt=lambda v: SOURCE_TEXT.get(v, "Entered by hand")),
                                Column("total", "Amount", 120, "right", fmt=_money), Column("status", "State", 110, status=lambda v: JSTATUS.get(v, "neutral"), fmt=_cap)])
        self.table.row_activated.connect(self._open)
        self.panel = StatePanel(self.table, "No journal entries yet. Entries appear when records are posted to the ledger.")
        self.panel.retry.connect(self.refresh)
        self.pager = Pager(50); self.pager.changed.connect(self.refresh)
        self.lay.addWidget(self.panel, 1); self.lay.addWidget(self.pager)

    def _reset(self) -> None:
        self.pager.reset(); self.refresh()

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client

        def done(d):
            self.table.set_rows(d["items"]); self.pager.update_total(d["total"])
            self.panel.show_content() if d["items"] else self.panel.show_empty("No entries match." if self.search.text() or self.state.currentData() else "No journal entries yet.")
        run_async(lambda: c.get("/accounting/journals", status=self.state.currentData(), q=self.search.text().strip() or None, page=self.pager.page, size=50), done,
                  lambda e: self.panel.show_error(e.message, e.offline))

    def _new(self) -> None:
        if JournalDialog(self.session, self).exec():
            self.status.show_message("Saved as a draft. A different authorised person must post it.", "ok"); self.refresh()

    def _open(self, row: dict) -> None:
        d = JournalDetail(self.session, row["id"], self)
        d.exec()
        if d.changed:
            self.refresh()


class CashTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        bar = QHBoxLayout()
        bar.addWidget(label("Balances are read from the ledger. Pick an account and reconcile it against the statement.", "muted")); bar.addStretch()
        if session.can("accounting.manage_coa"):
            bar.addWidget(button("Add cash or bank account", on_click=self._add))
        if session.can("accounting.reconcile"):
            bar.addWidget(button("Reconcile selected…", "primary", self._reconcile))
        self.lay.addLayout(bar)
        self.table = DataTable([Column("name", "Account"), Column("kind", "Kind", 90, fmt=_cap), Column("account_code", "Ledger code", 100), Column("balance", "Ledger balance", 150, "right", fmt=_num),
                                Column("unreconciled_lines", "Not yet matched", 130, "right"), Column("last_reconciled_to", "Reconciled to", 130)])
        self.panel = StatePanel(self.table, "No cash or bank accounts.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client
        def done(d):
            self.table.set_rows(d["items"]); self.panel.show_content() if d["items"] else self.panel.show_empty()
            neg = [x["name"] for x in d["items"] if Decimal(x["balance"]) < 0]
            if neg:
                self.status.show_message(f"{', '.join(neg)}: the ledger balance is negative. More money was recorded going out than coming in; usually an opening balance or a deposit is missing.", "info")
            else:
                self.status.clear()
        run_async(lambda: c.get("/accounting/cash-accounts"), done, lambda e: self.panel.show_error(e.message, e.offline))

    def _add(self) -> None:
        if CashAccountDialog(self.session, self).exec():
            self.status.show_message("Account added.", "ok"); self.refresh()

    def _reconcile(self) -> None:
        r = self.table.selected_row()
        if r is None:
            self.status.show_message("Select an account first.", "info"); return
        if ReconcileDialog(self.session, r, self).exec():
            self.status.show_message("Reconciliation completed.", "ok"); self.refresh()


class ExpensesTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.state = QComboBox(); self.state.addItem("All states", None)
        for k in ("pending", "posted", "rejected", "void"):
            self.state.addItem(_cap(k), k)
        self.state.currentIndexChanged.connect(lambda _: self._reset())
        bar.addWidget(self.state); bar.addStretch()
        if session.can("accounting.approve"):
            bar.addWidget(button("Approve…", on_click=lambda: self._decide(True))); bar.addWidget(button("Reject…", on_click=lambda: self._decide(False)))
            bar.addWidget(button("Void…", on_click=self._void))
        if session.can("expenses.manage"):
            bar.addWidget(button("Record expense", "primary", self._new))
        self.lay.addLayout(bar)
        self.table = DataTable([Column("ref", "Expense", 100), Column("expense_date", "Date", 100), Column("account", "Type", 200), Column("payee", "Paid to", 150), Column("description", "For"),
                                Column("paid_from", "Paid from", 150), Column("amount", "Amount", 110, "right", fmt=_money), Column("created_by", "Recorded by", 120),
                                Column("status", "State", 100, status=lambda v: ESTATUS.get(v, "neutral"), fmt=_cap)])
        self.panel = StatePanel(self.table, "No expenses recorded yet.")
        self.panel.retry.connect(self.refresh)
        self.pager = Pager(50); self.pager.changed.connect(self.refresh)
        self.lay.addWidget(self.panel, 1); self.lay.addWidget(self.pager)

    def _reset(self) -> None:
        self.pager.reset(); self.refresh()

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client

        def done(d):
            self.table.set_rows(d["items"]); self.pager.update_total(d["total"]); self.panel.show_content() if d["items"] else self.panel.show_empty()
        run_async(lambda: c.get("/accounting/expenses", status=self.state.currentData(), page=self.pager.page, size=50), done, lambda e: self.panel.show_error(e.message, e.offline))

    def _new(self) -> None:
        if ExpenseDialog(self.session, self).exec():
            self.status.show_message("Expense recorded. If it is above the approval limit, another person must approve it before it reaches the ledger.", "ok"); self.refresh()

    def _sel(self, states: tuple[str, ...]) -> dict | None:
        r = self.table.selected_row()
        if r is None:
            self.status.show_message("Select an expense first.", "info"); return None
        if r["status"] not in states:
            self.status.show_message(f"{r['ref']} is {r['status']}; this action is not available.", "info"); return None
        return r

    def _decide(self, approve: bool) -> None:
        r = self._sel(("pending",))
        if r is None:
            return
        c = self.session.client
        dlg = NoteDialog("Approve expense" if approve else "Reject expense", f"{r['ref']}: {r['description']} ({_money(r['amount'])}). Add a note for the record.", "Approve" if approve else "Reject",
                         lambda t: c.post(f"/accounting/expenses/{r['id']}/decision", json={"approve": approve, "note": t}), self)
        if dlg.exec():
            self.status.show_message("Decision recorded.", "ok"); self.refresh()

    def _void(self) -> None:
        r = self._sel(("posted",))
        if r is None:
            return
        c = self.session.client
        dlg = NoteDialog("Void expense", f"Voiding {r['ref']} posts the opposite entry. Someone other than the person who recorded or approved it must do this.", "Void",
                         lambda t: c.post(f"/accounting/expenses/{r['id']}/void", json={"reason": t}), self)
        if dlg.exec():
            self.status.show_message("Expense voided.", "ok"); self.refresh()


REPORT_SPECS = [
    ("Trial balance", "trial-balance", ("asof", "location")),
    ("Profit and loss", "income-statement", ("range", "location")),
    ("Balance sheet", "balance-sheet", ("asof",)),
    ("Cash flow", "cash-flow", ("range",)),
    ("General ledger (one account)", "general-ledger", ("range", "account", "location")),
    ("Receivables ageing", "aging/receivables", ("asof",)),
    ("Payables ageing", "aging/payables", ("asof",)),
    ("Customer / supplier / distributor statement", "statement", ("range", "party")),
    ("Sales, returns and discounts", "sales", ("range", "location")),
    ("Expense summary", "expenses", ("range", "location")),
    ("Cost of goods sold and gross profit", "cogs", ("range", "location")),
    ("Inventory valuation", "inventory-valuation", ("location",)),
    ("Monthly summary", "monthly", ("year", "location")),
    ("Annual summary", "annual", ()),
]


class ReportsTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.kind = QComboBox()
        for t, k, p in REPORT_SPECS:
            self.kind.addItem(t, (k, p))
        self.kind.setMinimumWidth(300)
        self.kind.currentIndexChanged.connect(self._params_changed)
        bar.addWidget(self.kind)
        today = date.today()
        from PySide6.QtCore import QDate
        self.d_from = _date_edit(QDate(today.year, today.month, 1)); self.d_to = _date_edit(); self.d_asof = _date_edit()
        self.year = QLineEdit(); self.year.setPlaceholderText("Fiscal year"); self.year.setMaximumWidth(100)
        self.account = _account_combo(session)
        self.ptype = QComboBox()
        for t, v in (("Customer", "customer"), ("Supplier", "supplier"), ("Distributor", "distributor")):
            self.ptype.addItem(t, v)
        self.party = QComboBox(); self.party.setMinimumWidth(220)
        self.ptype.currentIndexChanged.connect(self._load_parties)
        self.location = QComboBox(); self.location.addItem("All locations", None)
        for loc in session.locations:
            self.location.addItem(loc.get("name") or loc.get("code"), loc["id"])
        self.parts = {"range": [label("From"), self.d_from, label("to"), self.d_to], "asof": [label("As at"), self.d_asof], "account": [self.account],
                      "party": [self.ptype, self.party], "location": [self.location], "year": [self.year]}
        for ws in self.parts.values():
            for w in ws:
                bar.addWidget(w)
        bar.addStretch()
        bar.addWidget(button("Show report", "primary", self.run))
        self.lay.addLayout(bar)
        self.title = label("", "sectionTitle"); self.sub = label("", "muted", wrap=True)
        self.lay.addWidget(self.title); self.lay.addWidget(self.sub)
        self.holder = QVBoxLayout()
        self.lay.addLayout(self.holder, 1)
        self.table: DataTable | None = None
        self.totals = label("", "muted", wrap=True); self.notes = label("", "muted", wrap=True)
        self.lay.addWidget(self.totals); self.lay.addWidget(self.notes)
        self.panel_msg = label("Choose a report and press Show report. Figures come from posted journal entries only; export to PDF or Excel arrives in Phase 8.", "muted", wrap=True)
        self.holder.addWidget(self.panel_msg)
        self._params_changed()
        self._load_parties()

    def _params_changed(self) -> None:
        _, need = self.kind.currentData()
        for key, ws in self.parts.items():
            for w in ws:
                w.setVisible(key in need)

    def _load_parties(self) -> None:
        self.party.clear()
        t = self.ptype.currentData()
        path = {"customer": "/customers", "supplier": "/suppliers", "distributor": "/distributors"}[t]
        c = self.session.client
        run_async(lambda: c.get(path, page_size=200), lambda d: [self.party.addItem(x["name"], x["id"]) for x in d["items"]], lambda e: None)

    def refresh(self) -> None:
        pass

    def run(self) -> None:
        key, need = self.kind.currentData()
        params: dict = {}
        if "range" in need:
            params.update(date_from=_iso(self.d_from), date_to=_iso(self.d_to))
        if "asof" in need:
            params["as_of"] = _iso(self.d_asof)
        if "location" in need and self.location.currentData():
            params["location_id"] = self.location.currentData()
        if "year" in need and self.year.text().strip().isdigit():
            params["fiscal_year"] = int(self.year.text().strip())
        if "account" in need:
            if self.account.currentData() is None:
                self.status.show_message("Choose an account for the general ledger.", "info"); return
            params["account_id"] = self.account.currentData()
        if "party" in need:
            if self.party.currentData() is None:
                self.status.show_message("Choose who the statement is for.", "info"); return
            params.update(party_type=self.ptype.currentData(), party_id=self.party.currentData())
        self.status.clear()
        c = self.session.client
        run_async(lambda: c.get(f"/accounting/reports/{key}", **params), self._show, lambda e: self.status.show_message(e.message, "error"))

    def _show(self, rep: dict) -> None:
        self.panel_msg.setVisible(False)
        if self.table is not None:
            self.table.setParent(None); self.table.deleteLater()
        cols = [Column(c["key"], c["label"], 130 if c["align"] == "right" else None, c["align"], fmt=(lambda v: _num(v)) if c["align"] == "right" else None) for c in rep["columns"]]
        self.table = DataTable(cols)
        rows = rep["rows"]
        self.table.set_rows(rows)
        from PySide6.QtGui import QFont
        for r, row in enumerate(rows):
            if row.get("kind") in ("head", "total", "grand"):
                for ci in range(len(cols)):
                    it = self.table.item(r, ci)
                    if it:
                        f = QFont(it.font()); f.setBold(True); it.setFont(f)
        self.holder.addWidget(self.table)
        self.title.setText(rep["title"]); self.sub.setText(rep.get("subtitle") or "")
        t = rep.get("totals") or {}
        self.totals.setText("   ·   ".join(f"{k.replace('_', ' ').capitalize()}: {_num(v) if not isinstance(v, (bool, str)) or (isinstance(v, str) and v.replace('.', '', 1).replace('-', '', 1).isdigit()) else ('Yes' if v is True else 'No' if v is False else v)}"
                                           for k, v in t.items()))
        self.notes.setText("\n".join(rep.get("notes") or []))
        if not rows:
            self.status.show_message("Nothing to show for these choices.", "info")


class PeriodsTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        bar = QHBoxLayout()
        bar.addWidget(label("A closed month accepts no new entries. Records dated in it are posted to the next open month and keep their original date.", "muted")); bar.addStretch()
        if session.can("accounting.close_period"):
            bar.addWidget(button("Close selected month", on_click=self._close))
        if session.can("accounting.reopen_period"):
            bar.addWidget(button("Request reopening…", on_click=self._request))
        self.lay.addLayout(bar)
        self.table = DataTable([Column("name", "Month", 110), Column("start_date", "From", 110), Column("end_date", "To", 110), Column("fiscal_year", "Fiscal year", 100),
                                Column("status", "State", 110, status=lambda v: PSTATUS.get(v, "neutral"), fmt=_cap), Column("reopened_count", "Times reopened", 130, "right")])
        self.lay.addWidget(self.table, 2)
        self.lay.addWidget(label("Reopening requests waiting for a decision", "fieldLabel"))
        self.req = DataTable([Column("period", "Month", 110), Column("reason", "Reason"), Column("requested_by", "Requested by", 150), Column("requested_at", "Requested", 180)])
        self.lay.addWidget(self.req, 1)
        row = QHBoxLayout(); row.addStretch()
        if session.can("accounting.approve"):
            row.addWidget(button("Approve reopening…", on_click=lambda: self._decide(True))); row.addWidget(button("Reject…", on_click=lambda: self._decide(False)))
        self.lay.addLayout(row)

    def refresh(self) -> None:
        c = self.session.client

        def done(d):
            names = {p["id"]: p["name"] for p in d["items"]}
            self.table.set_rows(d["items"])
            for r in d["pending_requests"]:
                r["period"] = names.get(r["period_id"], f"#{r['period_id']}")
            self.req.set_rows(d["pending_requests"])
        run_async(lambda: c.get("/accounting/periods"), done, lambda e: self.status.show_message(e.message, "error"))

    def _close(self) -> None:
        r = self.table.selected_row()
        if r is None:
            self.status.show_message("Select a month first.", "info"); return
        c = self.session.client
        run_async(lambda: c.post(f"/accounting/periods/{r['id']}/close"), lambda _: (self.status.show_message(f"{r['name']} is closed.", "ok"), self.refresh()),
                  lambda e: self.status.show_message(e.message, "error"))

    def _request(self) -> None:
        r = self.table.selected_row()
        if r is None or r["status"] != "closed":
            self.status.show_message("Select a closed month first.", "info"); return
        c = self.session.client
        dlg = NoteDialog("Request reopening", f"Why must {r['name']} be reopened? Another authorised person decides.", "Send request",
                         lambda t: c.post(f"/accounting/periods/{r['id']}/reopen-requests", json={"reason": t}), self)
        if dlg.exec():
            self.status.show_message("Request sent.", "ok"); self.refresh()

    def _decide(self, approve: bool) -> None:
        r = self.req.selected_row()
        if r is None:
            self.status.show_message("Select a request first.", "info"); return
        c = self.session.client
        dlg = NoteDialog("Approve reopening" if approve else "Reject request", f"{r['period']}: {r['reason']}", "Approve" if approve else "Reject",
                         lambda t: c.post(f"/accounting/reopen-requests/{r['id']}/decision", json={"approve": approve, "note": t}), self)
        if dlg.exec():
            self.status.show_message("Decision recorded.", "ok"); self.refresh()


class OpeningImportDialog(QDialog):
    """Load the paper-register opening balances from the filled-in workbook. 'Check' changes nothing; 'Import' needs a clean check."""
    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.session = session; self.path = ""; self.setWindowTitle("Import opening balances from workbook"); self.setMinimumSize(900, 560)
        lay = QVBoxLayout(self)
        lay.addWidget(label("Download the template, fill it from the paper registers (never estimate), choose the date the balances are true at, then check the file. "
                            "A check shows every problem by sheet and row and saves nothing. Importing creates draft batches that a second person must approve.", "muted", wrap=True))
        row = QHBoxLayout()
        row.addWidget(button("Download template…", on_click=self._template))
        row.addWidget(button("Choose file…", on_click=self._choose))
        self.file = label("No file chosen.", "muted"); row.addWidget(self.file, 1)
        row.addWidget(label("Balances as at", "fieldLabel")); self.asof = _date_edit(); row.addWidget(self.asof)
        lay.addLayout(row)
        self.result = label("", "muted", wrap=True); lay.addWidget(self.result)
        self.errors = DataTable([Column("sheet", "Sheet", 150), Column("row", "Row", 60, "right"), Column("message", "Problem")])
        lay.addWidget(self.errors, 1)
        self.msg = label("", "muted", wrap=True); lay.addWidget(self.msg)
        bar = QHBoxLayout(); bar.addStretch()
        self.check = button("Check file", on_click=lambda: self._run(True)); self.go = button("Import", "primary", lambda: self._run(False))
        self.go.setEnabled(False)
        bar.addWidget(button("Close", on_click=self.reject)); bar.addWidget(self.check); bar.addWidget(self.go); lay.addLayout(bar)

    def _template(self) -> None:
        p, _ = QFileDialog.getSaveFileName(self, "Save the opening-balance template", "opening-balances-template.xlsx", "Excel workbook (*.xlsx)")
        if not p:
            return
        c = self.session.client
        def ok(b):
            open(p, "wb").write(b); self.msg.setText(f"Template saved to {p}")
        run_async(lambda: c.get_bytes("/accounting/opening-import/template"), ok, lambda e: self.msg.setText(e.message))

    def _choose(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Choose the filled-in workbook", "", "Excel workbook (*.xlsx)")
        if p:
            self.path = p; self.file.setText(p); self.go.setEnabled(False)

    def _run(self, dry: bool) -> None:
        if not self.path:
            self.msg.setText("Choose the workbook first."); return
        c = self.session.client
        self.check.setEnabled(False); self.go.setEnabled(False)
        def done(r):
            self.check.setEnabled(True)
            self.errors.set_rows(r.get("errors", []))
            t = r.get("totals") or {}
            if r.get("ok"):
                self.result.setText(f"{'Check passed, nothing saved. ' if dry else 'Imported as ' + str(r.get('batch_ref')) + '. '}"
                                    f"Assets {t.get('assets')}, liabilities {t.get('liabilities')}, expected owner capital {t.get('expected_capital')}, declared {t.get('declared_capital')}.")
                self.go.setEnabled(dry)
                if not dry:
                    self.accept()
            else:
                self.result.setText(f"{len(r.get('errors', []))} problem(s). Nothing was saved. Fix the workbook and check again.")
        def fail(e):
            self.check.setEnabled(True); self.msg.setText(e.message)
        run_async(lambda: c.post_file("/accounting/opening-import", self.path, as_of=self.asof.date().toString("yyyy-MM-dd"), dry_run="true" if dry else "false"), done, fail)


class OpeningTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        bar = QHBoxLayout()
        bar.addWidget(label("Opening figures from the paper era, each with its evidence and a verified flag. Nothing is posted until a second person approves a balanced, fully verified batch.", "muted", wrap=True), 1)
        if session.can("accounting.opening_balances"):
            bar.addWidget(button("Import workbook…", on_click=self._import))
            bar.addWidget(button("Cancel selected draft", on_click=self._cancel))
            bar.addWidget(button("New batch", "primary", self._new))
        if session.can("accounting.approve"):
            bar.addWidget(button("Approve selected…", on_click=self._approve))
        self.lay.addLayout(bar)
        self.table = DataTable([Column("ref", "Batch", 100), Column("as_of_date", "As at", 110), Column("description", "Description"), Column("total_debit", "Debits", 120, "right", fmt=_money),
                                Column("total_credit", "Credits", 120, "right", fmt=_money), Column("unverified", "Unverified lines", 130, "right"),
                                Column("status", "State", 100, status=lambda v: BSTATUS.get(v, "neutral"), fmt=_cap)])
        self.table.row_activated.connect(self._show_lines)
        self.panel = StatePanel(self.table, "No opening-balance batches yet.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)
        self.lay.addWidget(label("Double-click a batch to see its lines and evidence.", "muted"))

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client
        run_async(lambda: c.get("/accounting/opening-batches"), lambda d: (self.table.set_rows(d["items"]), self.panel.show_content() if d["items"] else self.panel.show_empty()),
                  lambda e: self.panel.show_error(e.message, e.offline))

    def _import(self) -> None:
        if OpeningImportDialog(self.session, self).exec():
            self.status.show_message("Opening balances imported as draft batches. Verify, then ask another authorised person to approve.", "ok"); self.refresh()

    def _new(self) -> None:
        if OpeningBatchDialog(self.session, self).exec():
            self.status.show_message("Draft saved. Verify every line, then ask another authorised person to approve it.", "ok"); self.refresh()

    def _sel(self, state: str = "draft") -> dict | None:
        r = self.table.selected_row()
        if r is None or r["status"] != state:
            self.status.show_message(f"Select a {state} batch first.", "info"); return None
        return r

    def _cancel(self) -> None:
        r = self._sel()
        if r is None:
            return
        c = self.session.client
        run_async(lambda: c.post(f"/accounting/opening-batches/{r['id']}/cancel"), lambda _: (self.status.show_message("Draft cancelled.", "ok"), self.refresh()),
                  lambda e: self.status.show_message(e.message, "error"))

    def _approve(self) -> None:
        r = self._sel()
        if r is None:
            return
        c = self.session.client
        run_async(lambda: c.post(f"/accounting/opening-batches/{r['id']}/approve"), lambda _: (self.status.show_message(f"{r['ref']} posted to the ledger.", "ok"), self.refresh()),
                  lambda e: self.status.show_message(e.message, "error"))

    def _show_lines(self, row: dict) -> None:
        d = QDialog(self)
        d.setWindowTitle(f"{row['ref']}: lines"); d.setMinimumSize(980, 420)
        lay = QVBoxLayout(d)
        lay.addWidget(label(f"{row['ref']}  ·  as at {row['as_of_date']}  ·  {_cap(row['status'])}", "sectionTitle"))
        t = DataTable([Column("account", "Account"), Column("debit", "Debit", 110, "right", fmt=lambda v: _num(v) if Decimal(str(v or 0)) else ""),
                       Column("credit", "Credit", 110, "right", fmt=lambda v: _num(v) if Decimal(str(v or 0)) else ""), Column("evidence", "Evidence"),
                       Column("verified", "Verified", 90, status=lambda v: "ok" if v else "warn", fmt=lambda v: "Yes" if v else "No")])
        t.set_rows(row["lines"]); lay.addWidget(t)
        lay.addWidget(button("Close", on_click=d.accept))
        d.exec()


# ------------------------------------------------------------------------------------------------ page
class AccountingPage(Page):
    title = "Accounting"

    def __init__(self, session: Session):
        super().__init__(session)
        self.root.addWidget(PageHeader("Accounting", "Everything here is calculated from posted journal entries. Local tax and statutory treatment must be confirmed by a qualified accountant."))
        self.tabs = QTabWidget()
        self.root.addWidget(self.tabs, 1)
        items: list[tuple[str, _Tab]] = [("Overview", OverviewTab(session)), ("Reports", ReportsTab(session)), ("Journal", JournalTab(session)), ("Chart of accounts", ChartTab(session)),
                                         ("Cash and bank", CashTab(session))]
        if session.can("expenses.manage") or session.can("accounting.view"):
            items.append(("Expenses", ExpensesTab(session)))
        items.append(("Periods", PeriodsTab(session)))
        if session.can("accounting.opening_balances") or session.can("accounting.approve"):
            items.append(("Opening balances", OpeningTab(session)))
        self.items = items
        for t, w in items:
            self.tabs.addTab(w, t)
        self.tabs.currentChanged.connect(lambda _: self.refresh())

    def refresh(self) -> None:
        w = self.tabs.currentWidget()
        if isinstance(w, _Tab):
            w.refresh()
