"""Employees: staff records, employee purchases (sales and stock movements at the staff discount), credit approval and payments, and the
central discount rules. Every rule (who may sell to whom, credit limits, separation of duties, stock availability) is enforced by the backend;
this file collects input and shows results. Personnel and purchase information is only shown to people who hold the matching permission."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QHBoxLayout, QLineEdit, QPlainTextEdit, QTabWidget, QVBoxLayout

from desktop.pages.accounting import NoteDialog
from desktop.pages.base import Page
from desktop.pages.inventory import _money, loc_codes, loc_combo
from desktop.pages.suppliers import _Detail, _Tab, _cap, _date_edit, _dec, _iso, _set_combo
from desktop.session import Session
from desktop.ui.forms import FormDialog, FormGrid
from desktop.ui.lines import LinesEditor
from desktop.ui.widgets import Column, DataTable, PageHeader, Pager, StatePanel, button, label
from desktop.ui.worker import run_async

PSTATUS = {"pending_approval": "warn", "completed": "ok", "rejected": "neutral", "voided": "neutral"}
PTEXT = {"pending_approval": "Waiting for approval", "completed": "Completed", "rejected": "Rejected", "voided": "Voided"}
PAY_METHODS = [("Cash", "cash"), ("Bank transfer", "bank_transfer"), ("Card", "card"), ("Other", "other")]
SOURCE_TEXT = {"price_list": "Staff price", "none": "No discount"}


def _source(v) -> str:
    v = str(v or "")
    return "Discount rule" if v.startswith("rule:") else SOURCE_TEXT.get(v, v)


def _bal(v) -> str:
    return _money(v) if v not in (None, "") else ""


# ------------------------------------------------------------------------------------------------ dialogs
class EmployeeDialog(FormDialog):
    def __init__(self, session: Session, row: dict | None = None, parent=None):
        super().__init__("Edit employee" if row else "New employee", "Save employee", parent, 700)
        self.session, self.row = session, row
        g = FormGrid(2)
        self.name = QLineEdit(); self.designation = QLineEdit(); self.phone = QLineEdit()
        self.loc = loc_combo(session)
        self.device = QLineEdit(); self.device.setPlaceholderText("The number enrolled on the attendance device")
        self.joined = _date_edit(optional=True)
        self.limit = QLineEdit(); self.limit.setPlaceholderText("0.00")
        self.user = QComboBox(); self.user.addItem("Not a system user", None)
        self.status = QComboBox(); self.status.addItem("Active", "active"); self.status.addItem("Inactive (left the company)", "inactive")
        g.add("name", "Full name", self.name, 2); g.add("designation", "Designation", self.designation); g.add("phone", "Phone", self.phone)
        g.add("loc", "Home location", self.loc); g.add("device", "Attendance device number", self.device)
        g.add("joined", "Joined on", self.joined); g.add("limit", "Credit limit", self.limit)
        g.add("user", "Linked user account", self.user, 2)
        if row:
            g.add("status", "Status", self.status)
        self.form = g
        self.body.addWidget(g)
        self.body.addWidget(label("The credit limit only matters if the company allows employee credit (Discounts and credit tab). Linking a user account stops that "
                                  "person recording, approving or paying their own purchases and attendance corrections.", "muted", wrap=True))
        if row:
            self.name.setText(row["full_name"]); self.designation.setText(row.get("designation") or ""); self.phone.setText(row.get("phone") or "")
            _set_combo(self.loc, row["location_id"]); self.device.setText(row.get("device_user_id") or "")
            if row.get("joined_on"):
                self.joined.setDate(QDate.fromString(row["joined_on"], "yyyy-MM-dd"))
            self.limit.setText(str(row.get("credit_limit") or "0")); _set_combo(self.status, row["status"])
        c = session.client

        def users(d):
            for u in d:
                self.user.addItem(f"{u['full_name']}  ({u['username']})", u["id"])
            if row:
                _set_combo(self.user, row.get("user_id"))
        if session.can("users.view"):
            run_async(lambda: c.get("/users", page_size=200).get("items", []), users, lambda e: None)

    def submit(self) -> None:
        self.form.clear_errors()
        if not self.name.text().strip():
            self.form.set_error("name", "Enter the employee's name."); return self.reenable()
        if self.loc.currentData() is None:
            self.form.set_error("loc", "Choose the home location."); return self.reenable()
        lim = _dec(self.limit.text() or "0")
        if lim is None or lim < 0:
            self.form.set_error("limit", "Enter the credit limit as a number."); return self.reenable()
        body = {"full_name": self.name.text().strip(), "designation": self.designation.text().strip() or None, "phone": self.phone.text().strip() or None,
                "location_id": self.loc.currentData(), "device_user_id": self.device.text().strip() or None, "joined_on": _iso(self.joined),
                "credit_limit": str(lim), "user_id": self.user.currentData() if self.user.count() > 1 or not self.row else self.row.get("user_id")}
        c = self.session.client
        if self.row:
            body["status"] = self.status.currentData()
            call = lambda: c.patch(f"/employees/{self.row['id']}", body)  # noqa: E731
        else:
            call = lambda: c.post("/employees", body)  # noqa: E731
        run_async(call, lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class _QuoteLines(LinesEditor):
    """The shared line editor, plus a callback so the discount and total update after every change."""

    def on_change(self) -> None:  # replaced on the instance by the dialog
        pass

    def _render(self) -> None:
        super()._render()
        self.on_change()


class PurchaseDialog(FormDialog):
    def __init__(self, session: Session, parent=None):
        super().__init__("Record an employee purchase", "Record purchase", parent, 980)
        self.session = session
        self.key = uuid.uuid4().hex
        self.total = Decimal(0)
        g = FormGrid(3)
        self.emp = QComboBox(); self.emp.addItem("Select an employee…", None); self.emp.setMinimumWidth(240)
        self.loc = loc_combo(session)
        self.date = _date_edit()
        self.receipt = QLineEdit(); self.receipt.setPlaceholderText("Printed or manual receipt number")
        g.add("emp", "Employee", self.emp); g.add("loc", "Sold from", self.loc); g.add("date", "Date", self.date)
        self.form = g
        self.body.addWidget(g)
        self.editor = _QuoteLines(session, [("qty", "Quantity", "int")], [Column("qty", "Qty", 60, "right")])
        self.editor.on_change = self._requote
        self.body.addWidget(self.editor, 1)
        self.summary = label("", "muted", wrap=True)
        self.body.addWidget(self.summary)
        g2 = FormGrid(3)
        self.now = QLineEdit(); self.now.setPlaceholderText("0.00"); self.now.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.method = QComboBox()
        for t, v in PAY_METHODS:
            self.method.addItem(t, v)
        self.ref = QLineEdit(); self.ref.setPlaceholderText("Needed for bank transfer or card")
        self.notes = QLineEdit()
        g2.add("now", "Paid now", self.now); g2.add("method", "Paid by", self.method); g2.add("ref", "Payment reference", self.ref)
        g2.add("receipt", "Receipt number", self.receipt); g2.add("notes", "Notes", self.notes, 2)
        self.form2 = g2
        self.body.addWidget(g2)
        self.credit_note = label("", "muted", wrap=True)
        self.body.addWidget(self.credit_note)
        self.now.textChanged.connect(self._credit_text)
        c = session.client
        run_async(lambda: c.get("/employees/lookup"), lambda d: [self.emp.addItem(f"{e['full_name']}  ({e['code']})", e["id"]) for e in d], lambda e: None)

    def _requote(self) -> None:
        lines = [{"variant_id": l["variant_id"], "qty": l["qty"]} for l in self.editor.lines if l.get("qty")]
        if not lines:
            self.total = Decimal(0); self.summary.setText(""); self.now.clear(); return self._credit_text()
        c = self.session.client

        def done(q):
            self.total = Decimal(q["total"])
            parts = [f"{l['sku']} × {l['qty']}: {_money(l['unit_price'])} each ({_source(l['discount_source'])}, discount {_money(l['unit_discount'])})" for l in q["lines"]]
            self.summary.setText("  ·  ".join(parts) + f"\nList price {_money(q['subtotal_list'])}   Discount {_money(q['discount_total'])}   Tax {_money(q['tax'])}   "
                                 f"To pay {_money(q['total'])}")
            self.now.setText(str(self.total)); self._credit_text()
        run_async(lambda: c.post("/employees/purchases/quote", json={"lines": lines}), done, lambda e: self.banner.show_message(e.message, "error"))

    def _credit_text(self) -> None:
        paid = _dec(self.now.text() or "0")
        if paid is not None and self.total > 0 and paid < self.total:
            self.credit_note.setText(f"{_money(self.total - paid)} would be on credit. That needs the company to allow employee credit and a second person to approve it "
                                     "before the goods are released.")
        else:
            self.credit_note.setText("")

    def submit(self) -> None:
        self.form.clear_errors()
        if self.emp.currentData() is None:
            self.form.set_error("emp", "Choose the employee."); return self.reenable()
        if self.loc.currentData() is None:
            self.form.set_error("loc", "Choose where the goods are sold from."); return self.reenable()
        if not self.editor.lines:
            self.banner.show_message("Add at least one item.", "error"); return self.reenable()
        paid = _dec(self.now.text() or "0")
        if paid is None or paid < 0:
            self.banner.show_message("Enter the amount paid now as a number (0 if all on credit).", "error"); return self.reenable()
        body = {"employee_id": self.emp.currentData(), "location_id": self.loc.currentData(), "purchase_date": _iso(self.date),
                "lines": [{"variant_id": l["variant_id"], "qty": l["qty"]} for l in self.editor.lines], "received_now": str(paid),
                "method": self.method.currentData() if paid > 0 else None, "reference": self.ref.text().strip() or None,
                "receipt_ref": self.receipt.text().strip() or None, "notes": self.notes.text().strip() or None, "idempotency_key": self.key}
        c = self.session.client
        run_async(lambda: c.post("/employees/purchases", json=body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class PaymentDialog(FormDialog):
    def __init__(self, session: Session, purchase: dict, parent=None):
        super().__init__("Record a payment", "Record payment", parent, 480)
        self.session, self.p = session, purchase
        self.key = uuid.uuid4().hex
        self.body.addWidget(label(f"{purchase['ref']} · {purchase['employee']}: {_money(purchase['balance'])} still owed.", "muted", wrap=True))
        g = FormGrid(2)
        self.amount = QLineEdit(); self.amount.setText(str(purchase["balance"])); self.amount.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.method = QComboBox()
        for t, v in PAY_METHODS:
            self.method.addItem(t, v)
        self.date = _date_edit(); self.ref = QLineEdit()
        g.add("amount", "Amount", self.amount); g.add("method", "Paid by", self.method); g.add("date", "Date", self.date); g.add("ref", "Reference", self.ref)
        self.form = g
        self.body.addWidget(g)

    def submit(self) -> None:
        a = _dec(self.amount.text())
        if a is None or a <= 0:
            self.form.set_error("amount", "Enter an amount above zero."); return self.reenable()
        c = self.session.client
        body = {"amount": str(a), "method": self.method.currentData(), "paid_date": _iso(self.date), "reference": self.ref.text().strip() or None, "idempotency_key": self.key}
        run_async(lambda: c.post(f"/employees/purchases/{self.p['id']}/payments", json=body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class RuleDialog(FormDialog):
    def __init__(self, session: Session, parent=None):
        super().__init__("New discount rule", "Save rule", parent, 480)
        self.session = session
        g = FormGrid(2)
        self.name = QLineEdit(); self.pct = QLineEdit(); self.pct.setPlaceholderText("e.g. 20")
        self.cat = QComboBox(); self.cat.addItem("All products", None)
        g.add("name", "Rule name", self.name, 2); g.add("cat", "Applies to", self.cat); g.add("pct", "Discount (%)", self.pct)
        self.form = g
        self.body.addWidget(g)
        self.body.addWidget(label("A product with its own staff price on the price list uses that price. Otherwise the most specific rule applies: a category rule beats the "
                                  "all-products rule.", "muted", wrap=True))
        c = session.client
        run_async(lambda: c.get("/categories"), lambda d: [self.cat.addItem(x["name"], x["id"]) for x in (d["items"] if isinstance(d, dict) else d)], lambda e: None)

    def submit(self) -> None:
        self.form.clear_errors()
        p = _dec(self.pct.text())
        if not self.name.text().strip():
            self.form.set_error("name", "Give the rule a name."); return self.reenable()
        if p is None or not 0 < p <= 100:
            self.form.set_error("pct", "A percentage above 0 and up to 100."); return self.reenable()
        c = self.session.client
        body = {"name": self.name.text().strip(), "category_id": self.cat.currentData(), "pct": str(p)}
        run_async(lambda: c.post("/employees/discount-rules", json=body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class PurchaseDetail(_Detail):
    def __init__(self, session: Session, purchase_id: int, parent=None):
        super().__init__(session, "Employee purchase", parent, (980, 620))
        self.pid, self.p = purchase_id, None
        self.lines = DataTable([Column("sku", "SKU", 130), Column("name", "Item"), Column("qty", "Qty", 60, "right"), Column("list_price", "List price", 100, "right", fmt=_money),
                                Column("unit_discount", "Discount", 100, "right", fmt=_money), Column("unit_price", "Pays", 100, "right", fmt=_money),
                                Column("line_total", "Line total", 110, "right", fmt=_money), Column("discount_source", "Discount from", 140, fmt=_source)])
        self.pays = DataTable([Column("ref", "Payment", 110), Column("paid_date", "Date", 100), Column("method", "Paid by", 110, fmt=_cap), Column("reference", "Reference"),
                               Column("amount", "Amount", 110, "right", fmt=_money), Column("status", "State", 100, status=lambda v: "ok" if v == "posted" else "neutral", fmt=_cap)])
        self.lay.addWidget(label("Items", "fieldLabel")); self.lay.addWidget(self.lines, 1)
        self.lay.addWidget(label("Payments", "fieldLabel")); self.lay.addWidget(self.pays, 1)
        self.bar = QHBoxLayout(); self.lay.addLayout(self.bar)
        self.load()

    def load(self) -> None:
        c = self.session.client
        run_async(lambda: c.get(f"/employees/purchases/{self.pid}"), self._show, self.fail)

    def _show(self, p: dict) -> None:
        self.p = p
        self.head.setText(f"{p['ref']} · {p['employee']} ({p['employee_code']})")
        extra = (f"  ·  decided by {p['decided_by']}" if p.get("decided_by") else "") + (f"  ·  voided by {p['voided_by']}: {p['void_reason']}" if p.get("voided_by") else "")
        self.sub.setText(f"{PTEXT.get(p['status'], p['status'])}  ·  {p['purchase_date']}  ·  recorded by {p['created_by']}  ·  receipt {p.get('receipt_ref') or '-'}\n"
                         f"List price {_money(p['subtotal_list'])}   Discount {_money(p['discount_total'])}   Tax {_money(p['tax'])}   Total {_money(p['total'])}   "
                         f"Paid {_money(p['paid'])}   Still owed {_money(p['balance'])}{extra}")
        self.lines.set_rows(p["lines"]); self.pays.set_rows(p["payments"])
        while self.bar.count():
            w = self.bar.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.bar.addStretch()
        s, can = self.session, self.session.can
        if p["status"] == "pending_approval" and can("employees.purchase_approve"):
            self.bar.addWidget(button("Reject…", on_click=lambda: self._decide(False))); self.bar.addWidget(button("Approve credit…", "primary", lambda: self._decide(True)))
        if p["status"] == "completed":
            if can("employees.purchase_pay"):
                self.bar.addWidget(button("Reverse selected payment…", on_click=self._reverse))
                if Decimal(p["balance"]) > 0:
                    self.bar.addWidget(button("Record payment", "primary", self._pay))
            if can("employees.purchase_approve"):
                self.bar.addWidget(button("Void purchase…", on_click=self._void))
        self.bar.addWidget(button("Close", on_click=self.accept))

    def _decide(self, approve: bool) -> None:
        c = self.session.client
        d = NoteDialog("Approve employee credit" if approve else "Reject purchase",
                       "Approving releases the goods from stock and puts the unpaid amount on the employee's account. Someone other than the person who recorded it must decide."
                       if approve else "Say why this purchase is being rejected. No goods are released.", "Approve" if approve else "Reject",
                       lambda t: c.post(f"/employees/purchases/{self.pid}/decision", json={"approve": approve, "note": t}), self)
        if d.exec():
            self.changed = True; self.load()

    def _pay(self) -> None:
        if PaymentDialog(self.session, self.p, self).exec():
            self.changed = True; self.load()

    def _reverse(self) -> None:
        r = self.pays.selected_row()
        if r is None or r["status"] != "posted":
            return self.status.show_message("Select a payment that has not been reversed.", "info")
        c = self.session.client
        d = NoteDialog("Reverse payment", f"Reversing {r['ref']} puts {_money(r['amount'])} back on the employee's account. Someone other than the person who recorded it must do this.",
                       "Reverse", lambda t: c.post(f"/employees/purchases/payments/{r['id']}/reverse", json={"reason": t}), self)
        if d.exec():
            self.changed = True; self.load()

    def _void(self) -> None:
        c = self.session.client
        d = NoteDialog("Void purchase", "The goods return to stock, every payment is reversed in the books, and the sale is reversed. The money itself must be handed back to the "
                       "employee outside the system. Someone other than the person who recorded or approved it must do this.", "Void",
                       lambda t: c.post(f"/employees/purchases/{self.pid}/void", json={"reason": t}), self)
        if d.exec():
            self.changed = True; self.load()


# ------------------------------------------------------------------------------------------------ tabs
class EmployeesTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("Search name or code"); self.search.setMinimumWidth(240)
        self.state = QComboBox(); self.state.addItem("Active only", "active"); self.state.addItem("Inactive", "inactive"); self.state.addItem("All", None)
        self.search.returnPressed.connect(self._reset); self.state.currentIndexChanged.connect(lambda _: self._reset())
        bar.addWidget(self.search); bar.addWidget(self.state); bar.addStretch()
        if session.can("employees.manage"):
            bar.addWidget(button("Edit selected", on_click=self._edit)); bar.addWidget(button("New employee", "primary", self._new))
        self.lay.addLayout(bar)
        codes = loc_codes(session)
        cols = [Column("code", "Code", 90), Column("full_name", "Name"), Column("designation", "Designation", 150),
                Column("location_id", "Location", 100, fmt=lambda v: codes.get(v, "")), Column("device_user_id", "Device no.", 90)]
        self.money = session.can("employees.purchase_view")
        if self.money:
            cols += [Column("credit_limit", "Credit limit", 110, "right", fmt=_bal), Column("outstanding", "Owes", 110, "right", fmt=_bal)]
        cols.append(Column("status", "Status", 90, status=lambda v: "ok" if v == "active" else "neutral", fmt=_cap))
        self.table = DataTable(cols)
        self.table.row_activated.connect(lambda r: self._edit() if session.can("employees.manage") else None)
        self.panel = StatePanel(self.table, "No employees recorded yet.")
        self.panel.retry.connect(self.refresh)
        self.pager = Pager(100); self.pager.changed.connect(self.refresh)
        self.lay.addWidget(self.panel, 1); self.lay.addWidget(self.pager)

    def _reset(self) -> None:
        self.pager.reset(); self.refresh()

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client

        def done(d):
            self.table.set_rows(d["items"]); self.pager.update_total(d["total"]); self.panel.show_content() if d["items"] else self.panel.show_empty()
        run_async(lambda: c.get("/employees", status=self.state.currentData(), q=self.search.text().strip() or None, location_id=self.session.current_location_id,
                                page=self.pager.page, page_size=100), done, lambda e: self.panel.show_error(e.message, e.offline))

    def _new(self) -> None:
        if EmployeeDialog(self.session, None, self).exec():
            self.status.show_message("Employee saved.", "ok"); self.refresh()

    def _edit(self) -> None:
        r = self.table.selected_row()
        if r is None:
            return self.status.show_message("Select an employee first.", "info")
        if EmployeeDialog(self.session, r, self).exec():
            self.status.show_message("Employee updated.", "ok"); self.refresh()


class PurchasesTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.state = QComboBox(); self.state.addItem("All states", None)
        for k, v in PTEXT.items():
            self.state.addItem(v, k)
        self.owing = QCheckBox("Only purchases with money still owed")
        self.state.currentIndexChanged.connect(lambda _: self._reset()); self.owing.toggled.connect(lambda _: self._reset())
        bar.addWidget(self.state); bar.addWidget(self.owing); bar.addStretch()
        bar.addWidget(button("Open selected", on_click=self._open))
        if session.can("employees.purchase"):
            bar.addWidget(button("Record purchase", "primary", self._new))
        self.lay.addLayout(bar)
        codes = loc_codes(session)
        self.table = DataTable([Column("ref", "Purchase", 120), Column("purchase_date", "Date", 100), Column("employee", "Employee"), Column("location_id", "Location", 100, fmt=lambda v: codes.get(v, "")),
                                Column("total", "Total", 100, "right", fmt=_money), Column("discount_total", "Discount", 100, "right", fmt=_money), Column("paid", "Paid", 100, "right", fmt=_money),
                                Column("balance", "Owed", 100, "right", fmt=_money), Column("created_by", "Recorded by", 110),
                                Column("status", "State", 170, status=lambda v: PSTATUS.get(v, "neutral"), fmt=lambda v: PTEXT.get(v, v))])
        self.table.row_activated.connect(lambda r: self._open())
        self.panel = StatePanel(self.table, "No employee purchases yet.")
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
        run_async(lambda: c.get("/employees/purchases", status=self.state.currentData(), outstanding_only="true" if self.owing.isChecked() else None,
                                location_id=self.session.current_location_id, page=self.pager.page, page_size=50), done, lambda e: self.panel.show_error(e.message, e.offline))

    def _new(self) -> None:
        if PurchaseDialog(self.session, self).exec():
            self.status.show_message("Purchase recorded. If part of it is on credit it waits for a second person to approve before the goods are released.", "ok"); self.refresh()

    def _open(self) -> None:
        r = self.table.selected_row()
        if r is None:
            return self.status.show_message("Select a purchase first.", "info")
        d = PurchaseDetail(self.session, r["id"], self)
        d.exec()
        if d.changed:
            self.refresh()


class DiscountsTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        self.can_edit = session.can("employees.manage_discounts")
        box = QHBoxLayout()
        self.credit = QCheckBox("Allow employees to buy on credit (needs a second person to approve each one)")
        self.cap = QLineEdit(); self.cap.setPlaceholderText("0 = no cap"); self.cap.setMaximumWidth(140); self.cap.setAlignment(Qt.AlignmentFlag.AlignRight)
        box.addWidget(self.credit); box.addSpacing(20); box.addWidget(label("Most one employee may buy per month", "fieldLabel")); box.addWidget(self.cap); box.addStretch()
        if self.can_edit:
            box.addWidget(button("Save settings", "primary", self._save))
        else:
            self.credit.setEnabled(False); self.cap.setEnabled(False)
        self.lay.addLayout(box)
        bar = QHBoxLayout()
        bar.addWidget(label("Staff discount rules", "sectionTitle")); bar.addStretch()
        if self.can_edit:
            bar.addWidget(button("Switch selected on or off", on_click=self._toggle)); bar.addWidget(button("New rule", "primary", self._new))
        self.lay.addLayout(bar)
        self.table = DataTable([Column("name", "Rule", 220), Column("category", "Applies to"), Column("pct", "Discount", 100, "right", fmt=lambda v: f"{Decimal(str(v)):.2f}%"),
                                Column("is_active", "State", 100, status=lambda v: "ok" if v else "neutral", fmt=lambda v: "On" if v else "Off")])
        self.panel = StatePanel(self.table, "No discount rules yet. Products with a staff price on the price list still get that price.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client

        def done(d):
            rules, st = d
            self.credit.setChecked(bool(st["credit_allowed"])); self.cap.setText(str(st["monthly_purchase_cap"]))
            self.table.set_rows(rules); self.panel.show_content() if rules else self.panel.show_empty()
        run_async(lambda: (c.get("/employees/discount-rules"), c.get("/employees/settings")), done, lambda e: self.panel.show_error(e.message, e.offline))

    def _save(self) -> None:
        cap = _dec(self.cap.text() or "0")
        if cap is None or cap < 0:
            return self.status.show_message("Enter the monthly cap as a number (0 for no cap).", "error")
        c = self.session.client
        run_async(lambda: c.put("/employees/settings", {"credit_allowed": self.credit.isChecked(), "monthly_purchase_cap": str(cap)}),
                  lambda _: (self.status.show_message("Settings saved.", "ok"), self.refresh()), lambda e: self.status.show_message(e.message, "error"))

    def _new(self) -> None:
        if RuleDialog(self.session, self).exec():
            self.status.show_message("Rule saved.", "ok"); self.refresh()

    def _toggle(self) -> None:
        r = self.table.selected_row()
        if r is None:
            return self.status.show_message("Select a rule first.", "info")
        c = self.session.client
        run_async(lambda: c.patch(f"/employees/discount-rules/{r['id']}", {"is_active": not r["is_active"], "reason": "Changed from the desktop app"}),
                  lambda _: (self.status.show_message("Rule updated.", "ok"), self.refresh()), lambda e: self.status.show_message(e.message, "error"))


class EmployeesPage(Page):
    title = "Employees"

    def __init__(self, session: Session):
        super().__init__(session)
        self.root.addWidget(PageHeader("Employees", "Staff records, employee purchases at the staff discount, and what employees owe. Visible only to authorised roles."))
        self.tabs = QTabWidget()
        self.root.addWidget(self.tabs, 1)
        items: list[tuple[str, _Tab]] = []
        if session.can("employees.view"):
            items.append(("Employees", EmployeesTab(session)))
        if session.can("employees.purchase_view") or session.can("employees.purchase"):
            items.append(("Purchases", PurchasesTab(session)))
        if session.can("employees.manage_discounts") or session.can("employees.purchase_view"):
            items.append(("Discounts and credit", DiscountsTab(session)))
        self.items = items
        for t, w in items:
            self.tabs.addTab(w, t)
        self.tabs.currentChanged.connect(lambda _: self.refresh())

    def refresh(self) -> None:
        w = self.tabs.currentWidget()
        if isinstance(w, _Tab):
            w.refresh()

    def scope_changed(self) -> None:
        self.refresh()
