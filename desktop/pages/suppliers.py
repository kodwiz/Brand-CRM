"""Suppliers and purchasing: orders, receiving against orders, supplier invoices with three-way matching, payments,
credits/returns, balances and goods-received-not-invoiced. All rules are enforced by the backend; this file only
presents them and collects input."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QHBoxLayout, QLineEdit, QPlainTextEdit, QSpinBox, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget, QAbstractItemView, QHeaderView,
)

from desktop.pages.base import Page
from desktop.pages.inventory import _money, loc_codes, loc_combo
from desktop.session import Session
from desktop.ui.forms import FormDialog, FormGrid
from desktop.ui.lines import LinesEditor
from desktop.ui.widgets import Banner, Column, DataTable, PageHeader, Pager, StatePanel, button, label
from desktop.ui.worker import run_async

PO_STATUS = {"draft": "warn", "approved": "info", "partially_received": "info", "received": "ok", "closed": "neutral", "cancelled": "neutral"}
INV_STATUS = {"draft": "warn", "held": "error", "posted": "ok", "rejected": "neutral", "void": "neutral", "cancelled": "neutral"}
FLAG_TEXT = {
    "short_received": "Short of order", "over_received": "More than ordered", "invoiced_ahead_of_receipt": "Invoiced before received",
    "received_not_invoiced": "Received, not yet invoiced", "over_invoiced": "Invoiced above ordered",
    "rejected_on_receipt": "Rejected on receipt"}
VARIANCE_TEXT = {"price_variance": "Price differs from the order", "over_invoiced": "More invoiced than ordered",
                 "not_on_po": "Line is not on the order"}
MATCH_TEXT = {"matched": "Matched", "variance": "Needs review", "awaiting_receipt": "Awaiting receipt", "no_po": "No order"}
METHODS = [("Cash", "cash"), ("Bank transfer", "bank_transfer"), ("Cheque", "cheque"), ("Card", "card"), ("Other", "other")]


def _cap(v) -> str:
    return str(v).replace("_", " ").capitalize()


def _dec(text: str) -> Decimal | None:
    try:
        return Decimal(text.strip().replace(",", "")) if text and text.strip() else None
    except InvalidOperation:
        return None


def _date_edit(d: QDate | None = None, optional: bool = False) -> QDateEdit:
    w = QDateEdit()
    w.setCalendarPopup(True)
    w.setDisplayFormat("dd MMM yyyy")
    if optional:
        w.setMinimumDate(QDate(2000, 1, 1))
        w.setSpecialValueText("None")
        w.setDate(w.minimumDate())
    else:
        w.setDate(d or QDate.currentDate())
    return w


def _iso(w: QDateEdit) -> str | None:
    return None if w.date() == w.minimumDate() and w.specialValueText() else w.date().toString("yyyy-MM-dd")


def _price(v) -> str:
    """Unit prices keep their real precision but never show less than two decimals."""
    if v in (None, ""):
        return ""
    t = f"{Decimal(str(v)):f}"
    if "." in t:
        t = t.rstrip("0")
        if len(t.split(".")[1]) < 2:
            t = t + "0" * (2 - len(t.split(".")[1]))
    else:
        t += ".00"
    return t


def _match_label(row: dict) -> str:
    if row.get("status") == "posted" and row.get("match_status") == "variance":
        return "Variance approved"
    return MATCH_TEXT.get(row.get("match_status"), _cap(row.get("match_status")))


def _can_cost(s: Session) -> bool:
    return s.can("products.view_cost")


def _can_money(s: Session) -> bool:
    return s.can("suppliers.view")


class QtyGrid(QTableWidget):
    """Editable grid for quantities/prices keyed to existing document lines. Cells are spin boxes or line edits."""

    def __init__(self, headers: list[str], parent=None):
        super().__init__(0, len(headers), parent)
        self.setHorizontalHeaderLabels(headers)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(34)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setShowGrid(False)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(False)
        self.setMinimumHeight(170)

    def add_row(self, cells: list) -> int:
        r = self.rowCount()
        self.insertRow(r)
        for c, v in enumerate(cells):
            if isinstance(v, QWidget):
                self.setCellWidget(r, c, v)
            else:
                it = QTableWidgetItem("" if v is None else str(v))
                it.setFlags(Qt.ItemFlag.ItemIsEnabled)
                if isinstance(v, (int, float)) or (isinstance(v, str) and v.replace(".", "", 1).replace(",", "").isdigit()):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.setItem(r, c, it)
        return r

    def widget(self, r: int, c: int):
        return self.cellWidget(r, c)


def _spin(value: int = 0, maximum: int = 1_000_000) -> QSpinBox:
    s = QSpinBox()
    s.setRange(0, maximum)
    s.setValue(value)
    return s


def _supplier_combo(session: Session, with_all: bool = False) -> QComboBox:
    cb = QComboBox()
    cb.addItem("All suppliers" if with_all else "Select a supplier…", None)
    cb.setMinimumWidth(240)
    c = session.client
    run_async(lambda: c.get("/suppliers", active="true", page_size=200),
              lambda d: [cb.addItem(f"{s['name']}  ({s['code']})", s["id"]) for s in d["items"]], lambda e: None)
    return cb


def _set_combo(cb: QComboBox, value) -> None:
    """The combo fills asynchronously; select the value as soon as the item arrives."""
    def apply():
        i = cb.findData(value)
        if i >= 0:
            cb.setCurrentIndex(i)
    apply()
    if cb.findData(value) < 0:
        from PySide6.QtCore import QTimer
        t = QTimer(cb); t.setInterval(150); t.setSingleShot(False)
        n = {"i": 0}

        def tick():
            n["i"] += 1
            apply()
            if cb.findData(value) >= 0 or n["i"] > 20:
                t.stop()
        t.timeout.connect(tick); t.start()


# ------------------------------------------------------------------------------------------------ dialogs
class SupplierDialog(FormDialog):
    def __init__(self, session: Session, supplier: dict | None = None, parent=None):
        super().__init__("Edit supplier" if supplier else "New supplier", "Save", parent, 620)
        self.session, self.supplier = session, supplier
        s = supplier or {}
        g = FormGrid(2)
        self.name = QLineEdit(s.get("name", "")); self.contact = QLineEdit(s.get("contact_name") or "")
        self.phone = QLineEdit(s.get("phone") or ""); self.email = QLineEdit(s.get("email") or "")
        self.city = QLineEdit(s.get("city") or ""); self.tax = QLineEdit(s.get("tax_number") or "")
        self.terms = _spin(s.get("payment_terms_days", 0), 365)
        self.active = QCheckBox("Active"); self.active.setChecked(s.get("is_active", True))
        self.address = QLineEdit(s.get("address") or ""); self.notes = QLineEdit(s.get("notes") or "")
        g.add("name", "Supplier name", self.name, span=2)
        g.add("contact", "Contact person", self.contact); g.add("phone", "Phone", self.phone)
        g.add("email", "Email", self.email); g.add("city", "City", self.city)
        g.add("tax", "Tax number", self.tax); g.add("terms", "Payment terms (days from invoice date)", self.terms)
        g.add("address", "Address", self.address, span=2); g.add("notes", "Notes", self.notes, span=2)
        self.form = g
        self.body.addWidget(g)
        if supplier:
            self.body.addWidget(self.active)

    def submit(self) -> None:
        self.form.clear_errors()
        name = self.name.text().strip()
        if not name:
            self.form.set_error("name", "Enter the supplier name."); return self.reenable()
        body = {"name": name, "contact_name": self.contact.text().strip() or None, "phone": self.phone.text().strip() or None,
                "email": self.email.text().strip() or None, "city": self.city.text().strip() or None,
                "tax_number": self.tax.text().strip() or None, "payment_terms_days": self.terms.value(),
                "address": self.address.text().strip() or None, "notes": self.notes.text().strip() or None}
        c = self.session.client
        if self.supplier:
            body["is_active"] = self.active.isChecked()
            fn = lambda: c.put(f"/suppliers/{self.supplier['id']}", body)
        else:
            fn = lambda: c.post("/suppliers", body)
        run_async(fn, lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class POEditDialog(FormDialog):
    def __init__(self, session: Session, parent=None):
        super().__init__("New purchase order", "Save order", parent, 940)
        self.session = session
        g = FormGrid(3)
        self.supplier = _supplier_combo(session)
        self.loc = loc_combo(session)
        self.order_date = _date_edit()
        self.expected = _date_edit(optional=True)
        self.ref = QLineEdit(); self.ref.setPlaceholderText("Supplier's quote or order number")
        self.notes = QLineEdit()
        g.add("supplier", "Supplier", self.supplier); g.add("loc", "Deliver to", self.loc); g.add("order_date", "Order date", self.order_date)
        g.add("expected", "Expected delivery", self.expected); g.add("ref", "Supplier reference", self.ref); g.add("notes", "Notes", self.notes)
        self.form = g
        self.body.addWidget(g)
        self.body.addWidget(label("Prices on the order become the receiving cost. Orders must be approved by someone other than their creator before goods can be received.", "muted", wrap=True))
        self.editor = LinesEditor(session, [("qty_ordered", "Quantity", "int"), ("unit_cost", "Unit cost", "money")],
                                  [Column("qty_ordered", "Quantity", 90, "right"), Column("unit_cost", "Unit cost", 100, "right")])
        self.body.addWidget(self.editor)

    def submit(self) -> None:
        self.form.clear_errors()
        if self.supplier.currentData() is None:
            self.form.set_error("supplier", "Choose a supplier."); return self.reenable()
        if self.loc.currentData() is None:
            self.form.set_error("loc", "Choose the delivery location."); return self.reenable()
        lines = []
        for l in self.editor.payload():
            if not l["qty_ordered"] or _dec(l.get("unit_cost") or "") is None:
                self.editor.banner.show_message("Every line needs a quantity above zero and a unit cost.", "error"); return self.reenable()
            lines.append({"variant_id": l["variant_id"], "qty_ordered": l["qty_ordered"], "unit_cost": l["unit_cost"]})
        if not lines:
            self.editor.banner.show_message("Add at least one line.", "error"); return self.reenable()
        body = {"supplier_id": self.supplier.currentData(), "location_id": self.loc.currentData(),
                "order_date": self.order_date.date().toString("yyyy-MM-dd"), "expected_date": _iso(self.expected),
                "supplier_reference": self.ref.text().strip() or None, "notes": self.notes.text().strip() or None, "lines": lines}
        c = self.session.client
        run_async(lambda: c.post("/purchase-orders", body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class POReceiveDialog(FormDialog):
    """Receive against an order: one row per order line still open, with accepted / damaged / rejected counts."""

    def __init__(self, session: Session, po: dict, parent=None):
        super().__init__(f"Receive goods for {po['ref']}", "Post receipt", parent, 960)
        self.session, self.po = session, po
        self.body.addWidget(label(f"{po['supplier']['name']} · deliver to {loc_codes(session).get(po['location_id'], '')}. "
                                  "Only accepted units become saleable stock; damaged units are held apart; rejected units are recorded but never enter stock. "
                                  "Costs come from the order.", "muted", wrap=True))
        g = FormGrid(3)
        self.date = _date_edit()
        self.supplier_ref = QLineEdit(); self.supplier_ref.setPlaceholderText("Delivery note number")
        self.notes = QLineEdit()
        g.add("date", "Received on", self.date); g.add("ref", "Delivery note", self.supplier_ref); g.add("notes", "Notes", self.notes)
        self.form = g
        self.body.addWidget(g)
        self.grid = QtyGrid(["SKU", "Item", "Ordered", "Received so far", "Still to come", "Accepted", "Damaged", "Rejected"])
        self.rows: list[tuple[dict, QSpinBox, QSpinBox, QSpinBox]] = []
        for l in po["lines"]:
            a, d, r = _spin(), _spin(), _spin()
            self.rows.append((l, a, d, r))
            self.grid.add_row([l["sku"], l["variant_name"], l["qty_ordered"], l["qty_owned"], max(l["qty_remaining"], 0), a, d, r])
        for i, w in enumerate((120, 220, 70, 120, 100, 80, 80, 80)):
            self.grid.setColumnWidth(i, w)
        self.body.addWidget(self.grid)
        self.body.addWidget(label("Additional costs paid by us on this delivery (freight, duty, handling) are added to the stock cost. They are not a supplier debt; the supplier's own charges belong on their invoice.", "muted", wrap=True))
        self.cost_rows: list[tuple[QLineEdit, QLineEdit]] = []
        if _can_cost(session):
            for _ in range(2):
                d, a = QLineEdit(), QLineEdit()
                d.setPlaceholderText("Cost description, e.g. Freight"); a.setPlaceholderText("Amount"); a.setMaximumWidth(140)
                row = QHBoxLayout(); row.addWidget(d, 1); row.addWidget(a)
                self.body.addLayout(row)
                self.cost_rows.append((d, a))
        self.draft = QCheckBox("Save as a draft without adding stock yet")
        self.body.addWidget(self.draft)

    def submit(self) -> None:
        lines = []
        for l, a, d, r in self.rows:
            if a.value() + d.value() + r.value() == 0:
                continue
            lines.append({"variant_id": l["variant_id"], "qty_received": a.value() + d.value() + r.value(), "qty_accepted": a.value(),
                          "qty_damaged": d.value(), "qty_rejected": r.value()})
        if not lines:
            self.finish_error("Enter the quantities that arrived on at least one line."); return
        costs = []
        for d, a in self.cost_rows:
            if d.text().strip() or a.text().strip():
                amt = _dec(a.text())
                if not d.text().strip() or amt is None or amt <= 0:
                    self.finish_error("Each additional cost needs a description and an amount above zero."); return
                costs.append({"description": d.text().strip(), "amount": str(amt)})
        body = {"location_id": self.po["location_id"], "received_date": self.date.date().toString("yyyy-MM-dd"),
                "purchase_order_id": self.po["id"], "supplier_reference": self.supplier_ref.text().strip() or None,
                "notes": self.notes.text().strip() or None, "lines": lines, "additional_costs": costs,
                "post_now": not self.draft.isChecked()}
        c = self.session.client
        run_async(lambda: c.post("/inventory/receipts", body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class InvoiceDialog(FormDialog):
    """Enter a supplier's invoice. Against an order the lines are prefilled with what has not been invoiced yet."""

    def __init__(self, session: Session, po: dict | None = None, supplier_id: int | None = None, parent=None):
        super().__init__("Enter supplier invoice", "Save and post", parent, 920)
        self.session, self.po = session, po
        self.supplier_id = po["supplier"]["id"] if po else supplier_id
        g = FormGrid(3)
        self.supplier = _supplier_combo(session)
        if self.supplier_id:
            _set_combo(self.supplier, self.supplier_id)
            self.supplier.setEnabled(False)
        self.po_combo = QComboBox(); self.po_combo.addItem("No purchase order", None); self.po_combo.setMinimumWidth(220)
        self.inv_no = QLineEdit(); self.inv_no.setPlaceholderText("The supplier's invoice number")
        self.inv_date = _date_edit(); self.due = _date_edit(optional=True)
        self.discount = QLineEdit("0"); self.tax = QLineEdit("0")
        g.add("supplier", "Supplier", self.supplier); g.add("po", "Purchase order", self.po_combo); g.add("inv_no", "Invoice number", self.inv_no)
        g.add("inv_date", "Invoice date", self.inv_date); g.add("due", "Due date (blank = supplier terms)", self.due)
        g.add("discount", "Discount", self.discount); g.add("tax", "Tax", self.tax)
        self.form = g
        self.body.addWidget(g)
        self.hint = label("", "muted", wrap=True)
        self.body.addWidget(self.hint)
        self.grid = QtyGrid(["Item", "Ordered", "Received", "Already invoiced", "Qty on invoice", "Unit price"])
        self.body.addWidget(self.grid)
        self.rows: list[tuple[dict | None, QSpinBox, QLineEdit, QLineEdit | None]] = []
        self.charge_desc, self.charge_amt = QLineEdit(), QLineEdit()
        self.charge_desc.setPlaceholderText("Supplier charge on the invoice, e.g. Freight (optional)")
        self.charge_amt.setPlaceholderText("Amount"); self.charge_amt.setMaximumWidth(140)
        row = QHBoxLayout(); row.addWidget(self.charge_desc, 1); row.addWidget(self.charge_amt)
        self.body.addLayout(row)
        self.notes = QLineEdit(); self.notes.setPlaceholderText("Notes")
        self.body.addWidget(self.notes)
        self.draft = QCheckBox("Save as a draft without posting")
        self.body.addWidget(self.draft)
        self.po_combo.currentIndexChanged.connect(self._po_changed)
        if po:
            self.po_combo.addItem(po["ref"], po["id"]); self.po_combo.setCurrentIndex(1); self.po_combo.setEnabled(False)
            self._fill(po)
        else:
            self.supplier.currentIndexChanged.connect(self._supplier_changed)
            self._fill(None)
        self.grid.setMinimumHeight(190)

    def _supplier_changed(self) -> None:
        self.po_combo.blockSignals(True)
        self.po_combo.clear(); self.po_combo.addItem("No purchase order", None)
        sid = self.supplier.currentData()
        self.po_combo.blockSignals(False)
        if sid:
            c = self.session.client

            def done(d):
                for p in d["items"]:
                    if p["status"] in ("approved", "partially_received", "received", "closed"):
                        self.po_combo.addItem(p["ref"], p["id"])
            run_async(lambda: c.get("/purchase-orders", supplier_id=sid, page_size=100), done, lambda e: None)
        self._fill(None)

    def _po_changed(self) -> None:
        pid = self.po_combo.currentData()
        if self.po or not pid:
            if not self.po:
                self._fill(None)
            return
        c = self.session.client
        run_async(lambda: c.get(f"/purchase-orders/{pid}/matching"), lambda d: self._fill(d["po"], d["lines"]), lambda e: self.banner.show_message(e.message, "error"))

    def _fill(self, po: dict | None, lines: list[dict] | None = None) -> None:
        self.grid.setRowCount(0)
        self.rows = []
        for i, w in enumerate((330, 80, 80, 130, 120, 120)):
            self.grid.setColumnWidth(i, w)
        if po is None:
            self.hint.setText("Not against an order (for example a service bill). It will be recorded as having no order.")
            for _ in range(3):
                desc = QLineEdit(); desc.setPlaceholderText("Description")
                q = _spin(); p = QLineEdit(); p.setPlaceholderText("0.00")
                self.rows.append((None, q, p, desc))
                self.grid.add_row([desc, "", "", "", q, p])
            return
        self.hint.setText("Quantities are prefilled with what has been received and not yet invoiced. The price is taken from the order. "
                          "Differences beyond tolerance hold the invoice for approval; nothing is owed until it is posted.")
        src = lines
        if src is None:
            src = [{"po_line_id": l["id"], "sku": l["sku"], "variant_name": l["variant_name"], "ordered": l["qty_ordered"],
                    "owned": l["qty_owned"], "invoiced": l.get("qty_invoiced", 0), "unit_cost": l.get("unit_cost")} for l in po["lines"]]
        for l in src:
            todo = max(int(l["owned"]) - int(l["invoiced"]), 0)
            q = _spin(todo); p = QLineEdit(_price(l.get("unit_cost")))
            self.rows.append(({"po_line_id": l["po_line_id"]}, q, p, None))
            self.grid.add_row([f"{l['sku']}  ·  {l['variant_name']}", l["ordered"], l["owned"], l["invoiced"], q, p])

    def submit(self) -> None:
        self.form.clear_errors()
        if self.supplier.currentData() is None:
            self.form.set_error("supplier", "Choose a supplier."); return self.reenable()
        if not self.inv_no.text().strip():
            self.form.set_error("inv_no", "Enter the supplier's invoice number."); return self.reenable()
        lines = []
        for ref, q, p, desc in self.rows:
            if q.value() == 0:
                continue
            price = _dec(p.text())
            if price is None:
                self.finish_error("Every invoiced line needs a unit price."); return
            if ref:
                lines.append({"po_line_id": ref["po_line_id"], "qty": q.value(), "unit_price": str(price)})
            else:
                if not desc.text().strip():
                    self.finish_error("Describe each line that is not on an order."); return
                lines.append({"description": desc.text().strip(), "qty": q.value(), "unit_price": str(price)})
        if not lines:
            self.finish_error("Enter a quantity on at least one line."); return
        charges = []
        if self.charge_desc.text().strip() or self.charge_amt.text().strip():
            amt = _dec(self.charge_amt.text())
            if not self.charge_desc.text().strip() or amt is None or amt <= 0:
                self.finish_error("The supplier charge needs a description and an amount above zero."); return
            charges.append({"description": self.charge_desc.text().strip(), "amount": str(amt)})
        body = {"supplier_id": self.supplier.currentData(), "invoice_no": self.inv_no.text().strip(),
                "invoice_date": self.inv_date.date().toString("yyyy-MM-dd"), "due_date": _iso(self.due),
                "purchase_order_id": self.po_combo.currentData(), "lines": lines, "charges": charges,
                "discount": str(_dec(self.discount.text()) or 0), "tax": str(_dec(self.tax.text()) or 0),
                "notes": self.notes.text().strip() or None, "post_now": not self.draft.isChecked()}
        c = self.session.client
        run_async(lambda: c.post("/supplier-invoices", body), lambda r: self.finish_ok(), lambda e: self.finish_error(e.message))


class ReasonDialog(FormDialog):
    def __init__(self, title: str, prompt: str, submit_text: str, call, parent=None, required: bool = True):
        super().__init__(title, submit_text, parent, 480)
        self.call, self.required = call, required
        self.body.addWidget(label(prompt, "muted", wrap=True))
        self.text = QLineEdit(); self.text.setPlaceholderText("Reason" + (" (required)" if required else ""))
        self.body.addWidget(self.text)

    def submit(self) -> None:
        t = self.text.text().strip()
        if self.required and not t:
            self.finish_error("A reason is required."); return
        run_async(lambda: self.call(t), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class PaymentDialog(FormDialog):
    def __init__(self, session: Session, supplier_id: int | None = None, invoice: dict | None = None, parent=None):
        super().__init__("Record payment to supplier", "Record payment", parent, 900)
        self.session = session
        g = FormGrid(4)
        self.supplier = _supplier_combo(session)
        self.amount = QLineEdit(); self.amount.setPlaceholderText("0.00")
        self.method = QComboBox()
        for t, v in METHODS:
            self.method.addItem(t, v)
        self.date = _date_edit(); self.reference = QLineEdit(); self.reference.setPlaceholderText("Cheque / transfer number")
        g.add("supplier", "Supplier", self.supplier, span=2); g.add("amount", "Amount paid", self.amount)
        g.add("method", "Method", self.method); g.add("date", "Paid on", self.date); g.add("reference", "Reference", self.reference, span=2)
        self.form = g
        self.body.addWidget(g)
        self.body.addWidget(label("Choose which invoices this payment settles. A payment can cover several invoices, or part of one. Only posted invoices can be paid.", "muted", wrap=True))
        self.grid = QtyGrid(["Invoice", "Supplier's no.", "Due", "Outstanding", "Pay now"])
        self.body.addWidget(self.grid)
        self.rows: list[tuple[dict, QLineEdit]] = []
        self.total_lbl = label("", "muted")
        self.on_account = QCheckBox("Keep any amount not allocated as a payment on account (prepayment)")
        self.body.addWidget(self.total_lbl); self.body.addWidget(self.on_account)
        self.supplier.currentIndexChanged.connect(self._load)
        self._invoice = invoice
        if supplier_id:
            _set_combo(self.supplier, supplier_id);
        self.amount.textChanged.connect(self._recount)
        self._load()

    def _load(self) -> None:
        sid = self.supplier.currentData()
        self.grid.setRowCount(0); self.rows = []
        if not sid:
            return
        c = self.session.client

        def done(d):
            self.grid.setRowCount(0); self.rows = []
            for i, w in enumerate((130, 160, 110, 130, 130)):
                self.grid.setColumnWidth(i, w)
            for inv in d["items"]:
                e = QLineEdit(); e.setPlaceholderText("0.00"); e.textChanged.connect(self._recount)
                if self._invoice and inv["id"] == self._invoice["id"]:
                    e.setText(inv["outstanding"])
                self.rows.append((inv, e))
                self.grid.add_row([inv["ref"], inv["invoice_no"], str(inv["due_date"]), _money(inv["outstanding"]), e])
            self._recount()
        run_async(lambda: c.get("/supplier-invoices", supplier_id=sid, unpaid_only="true", page_size=200), done,
                  lambda e: self.banner.show_message(e.message, "error"))

    def _recount(self) -> None:
        total = sum((_dec(e.text()) or Decimal(0) for _, e in self.rows), Decimal(0))
        amt = _dec(self.amount.text()) or Decimal(0)
        if total and not self.amount.hasFocus() and (amt == 0 or self._auto):
            self._auto = True
            self.amount.blockSignals(True); self.amount.setText(str(total)); self.amount.blockSignals(False)
            amt = total
        self.total_lbl.setText(f"Allocated to invoices: {total:,.2f}   ·   Not allocated: {max(amt - total, Decimal(0)):,.2f}")

    _auto = False

    def submit(self) -> None:
        self.form.clear_errors()
        if self.supplier.currentData() is None:
            self.form.set_error("supplier", "Choose a supplier."); return self.reenable()
        amt = _dec(self.amount.text())
        if amt is None or amt <= 0:
            self.form.set_error("amount", "Enter the amount paid."); return self.reenable()
        allocs = []
        for inv, e in self.rows:
            v = _dec(e.text())
            if v is None and e.text().strip():
                self.finish_error(f"{inv['ref']}: the amount is not a number."); return
            if v and v > 0:
                allocs.append({"invoice_id": inv["id"], "amount": str(v)})
        import uuid
        body = {"supplier_id": self.supplier.currentData(), "amount": str(amt), "method": self.method.currentData(),
                "paid_date": self.date.date().toString("yyyy-MM-dd"), "reference": self.reference.text().strip() or None,
                "allocations": allocs, "on_account": self.on_account.isChecked(), "idempotency_key": uuid.uuid4().hex[:40]}
        c = self.session.client
        run_async(lambda: c.post("/supplier-payments", body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class AdjustmentDialog(FormDialog):
    def __init__(self, session: Session, supplier_id: int | None = None, parent=None):
        super().__init__("Supplier credit or extra charge", "Submit for approval", parent, 620)
        self.session = session
        g = FormGrid(2)
        self.supplier = _supplier_combo(session)
        self.kind = QComboBox()
        self.kind.addItem("Credit from supplier (reduces what we owe)", "reduce_payable")
        self.kind.addItem("Extra charge from supplier (increases what we owe)", "increase_payable")
        self.amount = QLineEdit(); self.amount.setPlaceholderText("0.00")
        self.date = _date_edit(); self.doc = QLineEdit(); self.doc.setPlaceholderText("Supplier's credit/debit note number")
        self.invoice = QComboBox(); self.invoice.addItem("Not tied to one invoice", None)
        self.ret = QComboBox(); self.ret.addItem("Not for a returned shipment", None)
        self.reason = QLineEdit()
        g.add("supplier", "Supplier", self.supplier, span=2); g.add("kind", "Type", self.kind, span=2)
        g.add("amount", "Amount", self.amount); g.add("date", "Date", self.date)
        g.add("doc", "Document number", self.doc, span=2); g.add("invoice", "Apply to invoice", self.invoice, span=2)
        g.add("ret", "Linked supplier return", self.ret, span=2); g.add("reason", "Reason", self.reason, span=2)
        self.form = g
        self.body.addWidget(g)
        self.body.addWidget(label("A different person with approval rights must approve this before the balance changes.", "muted", wrap=True))
        self.supplier.currentIndexChanged.connect(self._load_links)
        if supplier_id:
            _set_combo(self.supplier, supplier_id)

    def _load_links(self) -> None:
        sid = self.supplier.currentData()
        self.invoice.clear(); self.invoice.addItem("Not tied to one invoice", None)
        self.ret.clear(); self.ret.addItem("Not for a returned shipment", None)
        if not sid:
            return
        c = self.session.client
        run_async(lambda: c.get("/supplier-invoices", supplier_id=sid, unpaid_only="true", page_size=100),
                  lambda d: [self.invoice.addItem(f"{i['ref']}  ·  {i['invoice_no']}  ·  outstanding {_money(i['outstanding'])}", i["id"]) for i in d["items"]], lambda e: None)
        run_async(lambda: c.get("/supplier-returns", supplier_id=sid, page_size=100),
                  lambda d: [self.ret.addItem(f"{r['ref']}  ·  {r['status']}", r["id"]) for r in d["items"] if r["status"] in ("dispatched", "credited")], lambda e: None)

    def submit(self) -> None:
        self.form.clear_errors()
        if self.supplier.currentData() is None:
            self.form.set_error("supplier", "Choose a supplier."); return self.reenable()
        amt = _dec(self.amount.text())
        if amt is None or amt <= 0:
            self.form.set_error("amount", "Enter an amount above zero."); return self.reenable()
        if not self.reason.text().strip():
            self.form.set_error("reason", "Say why."); return self.reenable()
        body = {"supplier_id": self.supplier.currentData(), "kind": self.kind.currentData(), "amount": str(amt),
                "adjustment_date": self.date.date().toString("yyyy-MM-dd"), "document_no": self.doc.text().strip() or None,
                "reason": self.reason.text().strip(), "invoice_id": self.invoice.currentData(), "supplier_return_id": self.ret.currentData()}
        c = self.session.client
        run_async(lambda: c.post("/supplier-adjustments", body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class ReturnDialog(FormDialog):
    BUCKETS = [("Damaged", "damaged"), ("Saleable", "good"), ("Expired", "expired"), ("Awaiting inspection", "inspection")]

    def __init__(self, session: Session, parent=None):
        super().__init__("Return goods to supplier", "Save return", parent, 880)
        self.session = session
        g = FormGrid(3)
        self.supplier = _supplier_combo(session); self.loc = loc_combo(session)
        self.reason = QLineEdit(); self.reason.setPlaceholderText("Why the goods are going back")
        g.add("supplier", "Supplier", self.supplier); g.add("loc", "Returning from", self.loc); g.add("reason", "Reason", self.reason)
        self.form = g
        self.body.addWidget(g)
        self.body.addWidget(label("Stock leaves only when the return is dispatched, valued at its current average cost. The supplier's credit is recorded separately as a credit and needs approval.", "muted", wrap=True))
        self.editor = LinesEditor(session, [("bucket", "Stock state", "bucket", self.BUCKETS), ("qty", "Quantity", "int")],
                                  [Column("bucket", "State", 100, fmt=lambda v: _cap(v)), Column("qty", "Quantity", 90, "right")])
        self.body.addWidget(self.editor)
        self.dispatch = QCheckBox("Dispatch now (remove the stock immediately)")
        self.body.addWidget(self.dispatch)

    def submit(self) -> None:
        self.form.clear_errors()
        if self.supplier.currentData() is None:
            self.form.set_error("supplier", "Choose a supplier."); return self.reenable()
        if self.loc.currentData() is None:
            self.form.set_error("loc", "Choose the location."); return self.reenable()
        if not self.reason.text().strip():
            self.form.set_error("reason", "Say why."); return self.reenable()
        lines = [{"variant_id": l["variant_id"], "bucket": l["bucket"], "qty": l["qty"]} for l in self.editor.payload() if l["qty"] > 0]
        if not lines:
            self.editor.banner.show_message("Add at least one line with a quantity.", "error"); return self.reenable()
        body = {"supplier_id": self.supplier.currentData(), "location_id": self.loc.currentData(), "reason": self.reason.text().strip(),
                "lines": lines, "dispatch_now": self.dispatch.isChecked()}
        c = self.session.client
        run_async(lambda: c.post("/supplier-returns", body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class StatementDialog(QDialog):
    def __init__(self, session: Session, supplier: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Statement: {supplier['name']}")
        self.setMinimumSize(820, 520)
        lay = QVBoxLayout(self)
        lay.addWidget(label(f"{supplier['name']}  ({supplier['code']})", "sectionTitle"))
        self.summary = label("Loading…", "muted", wrap=True)
        lay.addWidget(self.summary)
        self.table = DataTable([Column("date", "Date", 110), Column("type", "Entry", 120, fmt=_cap), Column("ref", "Document", 130), Column("memo", "Memo"),
                                Column("amount", "Amount", 110, "right", fmt=_money), Column("balance", "Balance", 120, "right", fmt=_money)])
        self.panel = StatePanel(self.table, "No transactions yet.")
        lay.addWidget(self.panel, 1)
        c = session.client

        def done(d):
            self.table.set_rows(d["entries"])
            self.summary.setText(f"Closing balance {_money(d['closing_balance'])}. Positive means we owe the supplier. "
                                 "Goods received but not yet invoiced do not appear here; see Payables.")
            self.panel.show_content() if d["entries"] else self.panel.show_empty()
        run_async(lambda: c.get(f"/suppliers/{supplier['id']}/statement"), done, lambda e: self.panel.show_error(e.message, e.offline))
        row = QHBoxLayout(); row.addStretch(); row.addWidget(button("Close", on_click=self.accept)); lay.addLayout(row)


class _Detail(QDialog):
    """Common frame for the read-and-act detail dialogs."""

    def __init__(self, session: Session, title: str, parent=None, size=(980, 640)):
        super().__init__(parent)
        self.session = session
        self.setWindowTitle(title)
        self.setMinimumSize(*size)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(20, 18, 20, 14)
        self.lay.setSpacing(10)
        self.head = label("", "sectionTitle")
        self.sub = label("Loading…", "muted", wrap=True)
        self.status = Banner()
        self.lay.addWidget(self.head); self.lay.addWidget(self.sub); self.lay.addWidget(self.status)
        self.changed = False

    def fail(self, e) -> None:
        self.status.show_message(e.message, "error")

    def act(self, fn, ok_text: str) -> None:
        run_async(fn, lambda _: (self.status.show_message(ok_text, "ok"), setattr(self, "changed", True), self.load()),
                  self.fail)


class PODetail(_Detail):
    def __init__(self, session: Session, po_id: int, parent=None):
        super().__init__(session, "Purchase order", parent, (1120, 680))
        self.po_id, self.po = po_id, None
        cost = _can_cost(session)
        cols = [Column("sku", "SKU", 120), Column("variant_name", "Item"), Column("ordered", "Ordered", 76, "right"),
                Column("accepted", "Accepted", 72, "right"), Column("damaged", "Damaged", 70, "right"), Column("rejected", "Rejected", 70, "right"),
                Column("remaining_to_receive", "To come", 70, "right")]
        if cost:
            cols += [Column("invoiced", "Invoiced", 72, "right"), Column("unit_cost", "Unit cost", 84, "right", fmt=_money)]
        cols.append(Column("flag_text", "Discrepancy", 250))
        self.lines = DataTable(cols)
        self.lay.addWidget(label("Ordered, received and invoiced", "fieldLabel"))
        self.lay.addWidget(self.lines, 2)
        self.inv_table = None
        if _can_money(session):
            self.lay.addWidget(label("Invoices against this order (double-click to open)", "fieldLabel"))
            self.inv_table = DataTable([Column("ref", "Invoice", 110), Column("invoice_no", "Supplier's no.", 140), Column("invoice_date", "Date", 100),
                                        Column("total", "Total", 110, "right", fmt=_money), Column("outstanding", "Outstanding", 110, "right", fmt=_money),
                                        Column("match_text", "Match", 150),
                                        Column("status", "State", 100, status=lambda v: INV_STATUS.get(v, "neutral"), fmt=_cap)])
            self.inv_table.row_activated.connect(self._open_invoice)
            self.lay.addWidget(self.inv_table, 1)
        self.bar = QHBoxLayout()
        self.lay.addLayout(self.bar)
        self.load()

    def _btn(self, text, fn, kind=None):
        b = button(text, kind, fn); self.bar.addWidget(b); return b

    def load(self) -> None:
        c = self.session.client
        run_async(lambda: c.get(f"/purchase-orders/{self.po_id}/matching"), self._loaded, self.fail)

    def _open_invoice(self, row: dict) -> None:
        InvoiceDetail(self.session, row["id"], self).exec()
        self.load()

    def _loaded(self, d: dict) -> None:
        po = self.po = d["po"]
        self.head.setText(f"{po['ref']}  ·  {_cap(po['status'])}")
        loc = loc_codes(self.session).get(po["location_id"], "")
        bits = [f"{po['supplier']['name']}", f"deliver to {loc}", f"ordered {po['order_date']}"]
        if po.get("expected_date"):
            bits.append(f"expected {po['expected_date']}")
        bits.append(f"terms {po['payment_terms_days']} days")
        if "total" in po:
            bits.append(f"total {_money(po['total'])}")
        bits.append(f"created by {po['created_by']}" + (f", approved by {po['approved_by']}" if po.get("approved_by") else ""))
        self.sub.setText("  ·  ".join(bits))
        rows = d["lines"]
        for r in rows:
            r["flag_text"] = ", ".join(FLAG_TEXT.get(f, f) for f in r["flags"]) or "-"
        self.lines.set_rows(rows)
        if self.inv_table is not None:
            c = self.session.client
            run_async(lambda: c.get("/supplier-invoices", purchase_order_id=self.po_id, page_size=100), lambda x: self.inv_table.set_rows([{**i, "match_text": _match_label(i)} for i in x["items"]]), self.fail)
        while self.bar.count():
            w = self.bar.takeAt(0).widget()
            if w:
                w.deleteLater()
        s, st = self.session, po["status"]
        if st == "draft" and s.can("purchasing.approve"):
            self._btn("Approve order", lambda: self.act(lambda: s.client.post(f"/purchase-orders/{self.po_id}/approve"), "Order approved."), "primary")
        if st in ("approved", "partially_received") and s.can("inventory.receive"):
            self._btn("Receive goods", self._receive, "primary")
        if st in ("approved", "partially_received", "received", "closed") and s.can("purchasing.manage") and _can_money(s):
            self._btn("Enter invoice", self._invoice)
        if st in ("approved", "partially_received", "received") and s.can("purchasing.manage"):
            self._btn("Close order", lambda: self._reason("Close order", "Closing says the rest of this order will not arrive. What was received stays received.", "Close",
                                                          lambda r: s.client.post(f"/purchase-orders/{self.po_id}/close", {"reason": r}), "Order closed."))
        if st in ("draft", "approved") and s.can("purchasing.manage"):
            self._btn("Cancel order", lambda: self._reason("Cancel order", "Only orders with nothing received can be cancelled.", "Cancel order",
                                                           lambda r: s.client.post(f"/purchase-orders/{self.po_id}/cancel", {"reason": r}), "Order cancelled."))
        self.bar.addStretch()
        self._btn("Close window", self.accept)

    def _reason(self, title, prompt, btn, call, ok) -> None:
        if ReasonDialog(title, prompt, btn, call, self).exec():
            self.changed = True; self.status.show_message(ok, "ok"); self.load()

    def _receive(self) -> None:
        if POReceiveDialog(self.session, self.po, self).exec():
            self.changed = True; self.status.show_message("Receipt saved.", "ok"); self.load()

    def _invoice(self) -> None:
        if InvoiceDialog(self.session, self.po, parent=self).exec():
            self.changed = True; self.status.show_message("Invoice saved. Check its match result below.", "ok"); self.load()


class InvoiceDetail(_Detail):
    def __init__(self, session: Session, invoice_id: int, parent=None):
        super().__init__(session, "Supplier invoice", parent, (900, 560))
        self.invoice_id, self.inv = invoice_id, None
        self.variance = label("", "muted", wrap=True)
        self.lay.addWidget(self.variance)
        self.table = DataTable([Column("sku", "SKU", 120), Column("description", "Description"), Column("qty", "Qty", 70, "right"),
                                Column("unit_price", "Unit price", 100, "right", fmt=_money), Column("line_total", "Line total", 110, "right", fmt=_money)])
        self.lay.addWidget(self.table, 1)
        self.totals = label("", "muted", wrap=True)
        self.lay.addWidget(self.totals)
        self.bar = QHBoxLayout(); self.lay.addLayout(self.bar)
        self.load()

    def load(self) -> None:
        c = self.session.client
        run_async(lambda: c.get(f"/supplier-invoices/{self.invoice_id}"), self._loaded, self.fail)

    def _loaded(self, inv: dict) -> None:
        self.inv = inv
        self.head.setText(f"{inv['ref']}  ·  {_cap(inv['status'])}  ·  supplier's no. {inv['invoice_no']}")
        self.sub.setText(f"{inv['supplier']['name']}  ·  dated {inv['invoice_date']}  ·  due {inv['due_date']}"
                         + (f"  ·  order {inv['po_ref']}" if inv.get("po_ref") else "  ·  no purchase order")
                         + f"  ·  match: {_match_label(inv)}")
        v = inv.get("variances") or []
        txt = ""
        if inv["status"] == "held":
            txt = "Held: nothing is owed to the supplier until this is approved. "
        if v:
            txt += "; ".join(f"{VARIANCE_TEXT.get(x['type'], x['type'])}" + (f" ({x['detail']})" if x.get("detail") else "") for x in v)
        if inv.get("decision_note"):
            txt += f"  Decision by {inv['decided_by']}: {inv['decision_note']}"
        if inv.get("void_reason"):
            txt += f"  Voided: {inv['void_reason']}"
        self.variance.setText(txt); self.variance.setVisible(bool(txt))
        self.table.set_rows(inv["lines"])
        ch = ", ".join(f"{c['description']} {_money(c['amount'])}" for c in inv["charges"])
        self.totals.setText(f"Items {_money(inv['subtotal'])}" + (f"  ·  charges {_money(inv['charges_total'])} ({ch})" if inv["charges"] else "")
                            + f"  ·  discount {_money(inv['discount'])}  ·  tax {_money(inv['tax'])}  ·  total {_money(inv['total'])}"
                            + f"  ·  paid {_money(inv['paid'])}  ·  credited {_money(inv['credited'])}  ·  outstanding {_money(inv['outstanding'])}")
        while self.bar.count():
            w = self.bar.takeAt(0).widget()
            if w:
                w.deleteLater()
        s, st = self.session, inv["status"]
        c = s.client
        if st == "draft" and s.can("purchasing.manage"):
            b = button("Post invoice", "primary", lambda: self.act(lambda: c.post(f"/supplier-invoices/{self.invoice_id}/post"), "Invoice processed."))
            self.bar.addWidget(b)
        if st == "held" and s.can("purchasing.approve_variance"):
            self.bar.addWidget(button("Approve variance", "primary", lambda: self._decide(True)))
            self.bar.addWidget(button("Reject invoice", None, lambda: self._decide(False)))
        if st == "posted" and s.can("purchasing.pay") and float(inv["outstanding"]) > 0:
            self.bar.addWidget(button("Pay this invoice", "primary", self._pay))
        if st == "posted" and s.can("purchasing.reverse"):
            self.bar.addWidget(button("Void invoice", None, lambda: self._void()))
        self.bar.addStretch()
        self.bar.addWidget(button("Close window", None, self.accept))

    def _decide(self, approve: bool) -> None:
        c = self.session.client
        t = "Approve the variance" if approve else "Reject the invoice"
        if ReasonDialog(t, "Say why. Someone other than the person who entered the invoice must decide.", "Approve" if approve else "Reject",
                        lambda r: c.post(f"/supplier-invoices/{self.invoice_id}/decide", {"approve": approve, "note": r}), self).exec():
            self.changed = True; self.status.show_message("Decision recorded.", "ok"); self.load()

    def _void(self) -> None:
        c = self.session.client
        if ReasonDialog("Void invoice", "Voiding removes the debt from the supplier balance. Invoices with payments or credits cannot be voided until those are reversed.", "Void",
                        lambda r: c.post(f"/supplier-invoices/{self.invoice_id}/void", {"reason": r}), self).exec():
            self.changed = True; self.status.show_message("Invoice voided.", "ok"); self.load()

    def _pay(self) -> None:
        if PaymentDialog(self.session, self.inv["supplier"]["id"], self.inv, self).exec():
            self.changed = True; self.status.show_message("Payment recorded.", "ok"); self.load()


# ------------------------------------------------------------------------------------------------ tabs
class _Tab(QWidget):
    def __init__(self, session: Session):
        super().__init__()
        self.session = session
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 12, 0, 0)
        self.lay.setSpacing(10)
        self.status = Banner()
        self.lay.addWidget(self.status)

    def refresh(self) -> None:
        pass


class OrdersTab(_Tab):
    def __init__(self, session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("Search order number"); self.search.setMinimumWidth(240)
        self.state = QComboBox(); self.state.addItem("All states", None)
        for k in PO_STATUS:
            self.state.addItem(_cap(k), k)
        self.search.returnPressed.connect(self._reset); self.state.currentIndexChanged.connect(lambda _: self._reset())
        bar.addWidget(self.search); bar.addWidget(self.state); bar.addStretch()
        if session.can("purchasing.manage") and _can_cost(session):
            bar.addWidget(button("New purchase order", "primary", self._new))
        self.lay.addLayout(bar)
        cols = [Column("ref", "Order", 110), Column("supplier_name", "Supplier"), Column("location", "Deliver to", 90), Column("order_date", "Ordered", 100),
                Column("expected_date", "Expected", 100), Column("units_ordered", "Units", 70, "right"), Column("units_owned", "Received", 80, "right")]
        if _can_cost(session):
            cols.append(Column("total", "Total", 110, "right", fmt=_money))
        cols.append(Column("status", "State", 130, status=lambda v: PO_STATUS.get(v, "neutral"), fmt=_cap))
        self.table = DataTable(cols)
        self.table.row_activated.connect(self._open)
        self.panel = StatePanel(self.table, "No purchase orders yet.")
        self.panel.retry.connect(self.refresh)
        self.pager = Pager(50); self.pager.changed.connect(self.refresh)
        self.lay.addWidget(self.panel, 1); self.lay.addWidget(self.pager)

    def _reset(self):
        self.pager.reset(); self.refresh()

    def refresh(self) -> None:
        self.panel.show_loading()
        c, s = self.session.client, self.session
        names = loc_codes(s)

        def done(d):
            for r in d["items"]:
                r["supplier_name"] = r["supplier"]["name"]; r["location"] = names.get(r["location_id"], f"#{r['location_id']}")
            self.table.set_rows(d["items"]); self.pager.update_total(d["total"])
            self.panel.show_content() if d["items"] else self.panel.show_empty("No purchase orders match." if self.search.text() or self.state.currentData() else "No purchase orders yet.")
        run_async(lambda: c.get("/purchase-orders", location_id=s.current_location_id, status=self.state.currentData(),
                                q=self.search.text().strip() or None, page=self.pager.page, page_size=50), done,
                  lambda e: self.panel.show_error(e.message, e.offline))

    def _new(self) -> None:
        if POEditDialog(self.session, self).exec():
            self.status.show_message("Purchase order saved as a draft. Someone else must approve it before goods can be received.", "ok"); self.refresh()

    def _open(self, row: dict) -> None:
        d = PODetail(self.session, row["id"], self); d.exec()
        if d.changed:
            self.refresh()


class InvoicesTab(_Tab):
    def __init__(self, session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("Search invoice number"); self.search.setMinimumWidth(240)
        self.state = QComboBox(); self.state.addItem("All states", None)
        for k in INV_STATUS:
            self.state.addItem(_cap(k), k)
        self.unpaid = QCheckBox("Unpaid only"); self.overdue = QCheckBox("Overdue only")
        for w in (self.unpaid, self.overdue):
            w.stateChanged.connect(lambda _: self._reset())
        self.search.returnPressed.connect(self._reset); self.state.currentIndexChanged.connect(lambda _: self._reset())
        for w in (self.search, self.state, self.unpaid, self.overdue):
            bar.addWidget(w)
        bar.addStretch()
        if session.can("purchasing.manage"):
            bar.addWidget(button("Enter invoice", "primary", self._new))
        self.lay.addLayout(bar)
        cols = [Column("ref", "Invoice", 110), Column("supplier_name", "Supplier"), Column("invoice_no", "Supplier's no.", 130), Column("po_ref", "Order", 100),
                Column("invoice_date", "Date", 100), Column("due_date", "Due", 100), Column("total", "Total", 110, "right", fmt=_money),
                Column("outstanding", "Outstanding", 110, "right", fmt=_money),
                Column("match_text", "Match", 150, status=lambda v: {"Matched": "ok", "Needs review": "error", "Awaiting receipt": "warn", "Variance approved": "info"}.get(v, "neutral")),
                Column("status", "State", 100, status=lambda v: INV_STATUS.get(v, "neutral"), fmt=_cap)]
        self.table = DataTable(cols)
        self.table.row_activated.connect(self._open)
        self.panel = StatePanel(self.table, "No supplier invoices yet.")
        self.panel.retry.connect(self.refresh)
        self.pager = Pager(50); self.pager.changed.connect(self.refresh)
        self.lay.addWidget(self.panel, 1); self.lay.addWidget(self.pager)

    def _reset(self):
        self.pager.reset(); self.refresh()

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client

        def done(d):
            for r in d["items"]:
                r["supplier_name"] = r["supplier"]["name"]; r["match_text"] = _match_label(r)
            self.table.set_rows(d["items"]); self.pager.update_total(d["total"])
            self.panel.show_content() if d["items"] else self.panel.show_empty("No invoices match." if (self.search.text() or self.state.currentData() or self.unpaid.isChecked() or self.overdue.isChecked()) else "No supplier invoices yet.")
        run_async(lambda: c.get("/supplier-invoices", status=self.state.currentData(), q=self.search.text().strip() or None,
                                unpaid_only="true" if self.unpaid.isChecked() else None, overdue_only="true" if self.overdue.isChecked() else None,
                                page=self.pager.page, page_size=50), done, lambda e: self.panel.show_error(e.message, e.offline))

    def _new(self) -> None:
        if InvoiceDialog(self.session, parent=self).exec():
            self.status.show_message("Invoice saved.", "ok"); self.refresh()

    def _open(self, row: dict) -> None:
        d = InvoiceDetail(self.session, row["id"], self); d.exec()
        if d.changed:
            self.refresh()


class PaymentsTab(_Tab):
    def __init__(self, session):
        super().__init__(session)
        bar = QHBoxLayout(); bar.addStretch()
        self.rev_btn = button("Reverse selected payment", None, self._reverse)
        self.rev_btn.setVisible(session.can("purchasing.reverse")); bar.addWidget(self.rev_btn)
        if session.can("purchasing.pay"):
            bar.addWidget(button("Record payment", "primary", self._new))
        self.lay.addLayout(bar)
        self.table = DataTable([Column("ref", "Payment", 110), Column("supplier_name", "Supplier"), Column("paid_date", "Paid on", 100),
                                Column("method", "Method", 110, fmt=_cap), Column("reference", "Reference", 140),
                                Column("amount", "Amount", 110, "right", fmt=_money), Column("unallocated", "On account", 110, "right", fmt=_money),
                                Column("applied", "Applied to", 200), Column("created_by", "Entered by", 100),
                                Column("status", "State", 100, status=lambda v: {"posted": "ok", "reversed": "neutral"}.get(v, "neutral"), fmt=_cap)])
        self.panel = StatePanel(self.table, "No supplier payments yet.")
        self.panel.retry.connect(self.refresh)
        self.pager = Pager(50); self.pager.changed.connect(self.refresh)
        self.lay.addWidget(self.panel, 1); self.lay.addWidget(self.pager)

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client

        def done(d):
            for r in d["items"]:
                r["supplier_name"] = r["supplier"]["name"]
                r["applied"] = ", ".join(f"{a['invoice_no']} ({_money(a['amount'])})" for a in r["allocations"] if not a["released"]) or "-"
            self.table.set_rows(d["items"]); self.pager.update_total(d["total"])
            self.panel.show_content() if d["items"] else self.panel.show_empty()
        run_async(lambda: c.get("/supplier-payments", page=self.pager.page, page_size=50), done, lambda e: self.panel.show_error(e.message, e.offline))

    def _new(self) -> None:
        if PaymentDialog(self.session, parent=self).exec():
            self.status.show_message("Payment recorded.", "ok"); self.refresh()

    def _reverse(self) -> None:
        row = self.table.selected_row()
        if not row or row["status"] != "posted":
            self.status.show_message("Select a posted payment first.", "info"); return
        c = self.session.client
        if ReasonDialog(f"Reverse {row['ref']}", "Use this when a payment bounced or was entered against the wrong supplier. The invoices become payable again. Someone other than the person who entered it must reverse it.",
                        "Reverse", lambda r: c.post(f"/supplier-payments/{row['id']}/reverse", {"reason": r}), self).exec():
            self.status.show_message("Payment reversed.", "ok"); self.refresh()


class CreditsTab(_Tab):
    def __init__(self, session):
        super().__init__(session)
        bar = QHBoxLayout(); bar.addStretch()
        can_dec = session.can("purchasing.approve")
        self.approve_btn = button("Approve selected", "primary", lambda: self._decide(True)); self.reject_btn = button("Reject selected", None, lambda: self._decide(False))
        self.cancel_btn = button("Cancel selected", None, self._cancel)
        self.dispatch_btn = button("Dispatch selected return", None, self._dispatch)
        for b, ok in ((self.approve_btn, can_dec), (self.reject_btn, can_dec), (self.cancel_btn, session.can("purchasing.manage"))):
            b.setVisible(ok); bar.addWidget(b)
        if session.can("purchasing.manage"):
            bar.addWidget(button("New credit / charge", None, self._new_adj)); bar.addWidget(button("New return to supplier", "primary", self._new_ret))
            bar.addWidget(self.dispatch_btn)
        self.lay.addLayout(bar)
        self.lay.addWidget(label("Credits and extra charges from suppliers", "fieldLabel"))
        self.adj = DataTable([Column("ref", "Reference", 110), Column("supplier_name", "Supplier", 150), Column("kind", "Type", 110, fmt=lambda v: "Credit" if v == "reduce_payable" else "Extra charge"),
                              Column("amount", "Amount", 100, "right", fmt=_money), Column("adjustment_date", "Date", 100), Column("document_no", "Document", 100),
                              Column("invoice_ref", "Invoice", 110), Column("return_ref", "Return", 110), Column("reason", "Reason"),
                              Column("requested_by", "Requested by", 140),
                              Column("status", "State", 110, status=lambda v: {"approved": "ok", "pending": "warn", "rejected": "neutral", "cancelled": "neutral"}.get(v, "neutral"), fmt=_cap)])
        self.adj_panel = StatePanel(self.adj, "No credits or extra charges yet.")
        self.adj_panel.retry.connect(self.refresh)
        self.lay.addWidget(self.adj_panel, 1)
        self.lay.addWidget(label("Returns to suppliers", "fieldLabel"))
        self.ret = DataTable([Column("ref", "Return", 110), Column("supplier_name", "Supplier"), Column("location", "From", 90), Column("reason", "Reason"),
                              Column("units", "Units", 70, "right"), Column("credit_ref", "Credit", 110), Column("created_by", "Entered by", 110),
                              Column("status", "State", 110, status=lambda v: {"credited": "ok", "dispatched": "info", "draft": "warn"}.get(v, "neutral"), fmt=_cap)])
        self.ret_panel = StatePanel(self.ret, "No supplier returns yet.")
        self.ret_panel.retry.connect(self.refresh)
        self.lay.addWidget(self.ret_panel, 1)

    def refresh(self) -> None:
        c, s = self.session.client, self.session
        names = loc_codes(s)
        self.adj_panel.show_loading(); self.ret_panel.show_loading()

        def done_a(d):
            for r in d["items"]:
                r["supplier_name"] = r["supplier"]["name"]
            self.adj.set_rows(d["items"])
            self.adj_panel.show_content() if d["items"] else self.adj_panel.show_empty()

        def done_r(d):
            for r in d["items"]:
                r["supplier_name"] = r["supplier"]["name"]; r["location"] = names.get(r["location_id"], f"#{r['location_id']}")
                r["units"] = sum(l["qty"] for l in r["lines"])
            self.ret.set_rows(d["items"])
            self.ret_panel.show_content() if d["items"] else self.ret_panel.show_empty()
        run_async(lambda: c.get("/supplier-adjustments", page_size=100), done_a, lambda e: self.adj_panel.show_error(e.message, e.offline))
        run_async(lambda: c.get("/supplier-returns", location_id=s.current_location_id, page_size=100), done_r, lambda e: self.ret_panel.show_error(e.message, e.offline))

    def _new_adj(self) -> None:
        if AdjustmentDialog(self.session, parent=self).exec():
            self.status.show_message("Submitted. It changes the supplier balance only once approved.", "ok"); self.refresh()

    def _new_ret(self) -> None:
        if ReturnDialog(self.session, self).exec():
            self.status.show_message("Return saved.", "ok"); self.refresh()

    def _decide(self, approve: bool) -> None:
        row = self.adj.selected_row()
        if not row or row["status"] != "pending":
            self.status.show_message("Select a pending credit or charge first.", "info"); return
        c = self.session.client
        if approve:
            call = lambda r: c.post(f"/supplier-adjustments/{row['id']}/approve", {"note": r or None})
        else:
            call = lambda r: c.post(f"/supplier-adjustments/{row['id']}/reject", {"reason": r})
        if ReasonDialog(("Approve " if approve else "Reject ") + row["ref"], f"{row['reason']} - {_money(row['amount'])}", "Approve" if approve else "Reject", call, self, required=not approve).exec():
            self.status.show_message("Decision recorded.", "ok"); self.refresh()

    def _cancel(self) -> None:
        row = self.adj.selected_row()
        if not row or row["status"] != "pending":
            self.status.show_message("Select a pending credit or charge first.", "info"); return
        c = self.session.client
        run_async(lambda: c.post(f"/supplier-adjustments/{row['id']}/cancel"), lambda _: (self.status.show_message("Cancelled.", "ok"), self.refresh()),
                  lambda e: self.status.show_message(e.message, "error"))

    def _dispatch(self) -> None:
        row = self.ret.selected_row()
        if not row or row["status"] != "draft":
            self.status.show_message("Select a draft return first.", "info"); return
        c = self.session.client
        run_async(lambda: c.post(f"/supplier-returns/{row['id']}/dispatch"), lambda _: (self.status.show_message("Dispatched. Stock removed.", "ok"), self.refresh()),
                  lambda e: self.status.show_message(e.message, "error"))


class SuppliersTab(_Tab):
    def __init__(self, session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("Search name or code"); self.search.setMinimumWidth(260)
        self.search.returnPressed.connect(self.refresh)
        self.inactive = QCheckBox("Include inactive"); self.inactive.stateChanged.connect(lambda _: self.refresh())
        bar.addWidget(self.search); bar.addWidget(self.inactive); bar.addStretch()
        if session.can("suppliers.manage"):
            bar.addWidget(button("Edit selected", None, self._edit)); bar.addWidget(button("New supplier", "primary", self._new))
        self.lay.addLayout(bar)
        cols = [Column("code", "Code", 90), Column("name", "Supplier"), Column("contact_name", "Contact", 140), Column("phone", "Phone", 130), Column("city", "City", 100),
                Column("payment_terms_days", "Terms (days)", 100, "right")]
        if _can_money(session):
            cols.append(Column("balance", "We owe", 120, "right", fmt=_money))
        cols.append(Column("is_active", "State", 90, status=lambda v: "ok" if v else "neutral", fmt=lambda v: "Active" if v else "Inactive"))
        self.table = DataTable(cols)
        self.table.row_activated.connect(self._statement)
        self.panel = StatePanel(self.table, "No suppliers yet. Add the first one to start raising purchase orders.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)
        if _can_money(session):
            self.lay.addWidget(label("Double-click a supplier to see its statement.", "muted"))

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client
        run_async(lambda: c.get("/suppliers", q=self.search.text().strip() or None, active=None if self.inactive.isChecked() else "true", page_size=200),
                  lambda d: (self.table.set_rows(d["items"]), self.panel.show_content() if d["items"] else self.panel.show_empty("No suppliers match." if self.search.text() else None)),
                  lambda e: self.panel.show_error(e.message, e.offline))

    def _new(self) -> None:
        if SupplierDialog(self.session, parent=self).exec():
            self.status.show_message("Supplier saved.", "ok"); self.refresh()

    def _edit(self) -> None:
        row = self.table.selected_row()
        if not row:
            self.status.show_message("Select a supplier first.", "info"); return
        if SupplierDialog(self.session, row, self).exec():
            self.status.show_message("Supplier updated.", "ok"); self.refresh()

    def _statement(self, row: dict) -> None:
        if _can_money(self.session):
            StatementDialog(self.session, row, self).exec()


class PayablesTab(_Tab):
    def __init__(self, session):
        super().__init__(session)
        self.lay.addWidget(label("What we owe, by age of the invoice past its due date", "fieldLabel"))
        self.table = DataTable([Column("name", "Supplier"), Column("balance", "We owe", 120, "right", fmt=_money), Column("not_due", "Not yet due", 110, "right", fmt=_money),
                                Column("1_30", "1-30 days", 100, "right", fmt=_money), Column("31_60", "31-60", 100, "right", fmt=_money),
                                Column("61_90", "61-90", 100, "right", fmt=_money), Column("over_90", "Over 90", 100, "right", fmt=_money),
                                Column("on_account", "Paid on account", 120, "right", fmt=_money)])
        self.panel = StatePanel(self.table, "Nothing is owed to any supplier.")
        self.panel.retry.connect(self.refresh)
        self.total = label("", "muted")
        self.lay.addWidget(self.panel, 1); self.lay.addWidget(self.total)
        self.lay.addWidget(label("Goods received but not yet invoiced (information only: nothing is owed until the supplier's invoice is posted)", "fieldLabel"))
        self.grni = DataTable([Column("po_ref", "Order", 100), Column("sku", "SKU", 120), Column("variant_name", "Item"), Column("received", "Received", 80, "right"),
                               Column("invoiced", "Invoiced", 80, "right"), Column("not_invoiced_qty", "Not invoiced", 100, "right"),
                               Column("value", "Value at order price", 140, "right", fmt=_money)])
        self.grni_panel = StatePanel(self.grni, "Everything received has been invoiced.")
        self.lay.addWidget(self.grni_panel, 1)

    def refresh(self) -> None:
        c = self.session.client
        self.panel.show_loading(); self.grni_panel.show_loading()

        def done(d):
            for r in d["rows"]:
                r.update(r["aging"])
            self.table.set_rows(d["rows"])
            self.total.setText(f"Total owed {_money(d['total_balance'])}   ·   not yet due {_money(d['aging_totals']['not_due'])}   ·   past due {_money(sum(float(v) for k, v in d['aging_totals'].items() if k != 'not_due'))}")
            self.panel.show_content() if d["rows"] else self.panel.show_empty()
        run_async(lambda: c.get("/purchasing/payables"), done, lambda e: self.panel.show_error(e.message, e.offline))
        run_async(lambda: c.get("/purchasing/grni"), lambda d: (self.grni.set_rows(d["rows"]), self.grni_panel.show_content() if d["rows"] else self.grni_panel.show_empty()),
                  lambda e: self.grni_panel.show_error(e.message, e.offline))


class SuppliersPage(Page):
    title = "Suppliers"

    def __init__(self, session: Session):
        super().__init__(session)
        self.root.addWidget(PageHeader("Suppliers and purchasing", "Orders, receiving, supplier invoices and payments. Debt to a supplier arises only when their invoice is posted. "
                                       "Accounting entries arrive in a later phase."))
        self.tabs = QTabWidget()
        self.root.addWidget(self.tabs, 1)
        items: list[tuple[str, _Tab]] = []
        if session.can("purchasing.view"):
            items.append(("Orders", OrdersTab(session)))
        if _can_money(session):
            items += [("Invoices", InvoicesTab(session)), ("Payments", PaymentsTab(session)), ("Credits and returns", CreditsTab(session)),
                      ("Suppliers", SuppliersTab(session)), ("Payables", PayablesTab(session))]
        else:
            items.append(("Suppliers", SuppliersTab(session)))
        self.items = items
        for t, w in items:
            self.tabs.addTab(w, t)
        self.tabs.currentChanged.connect(lambda _: self.refresh())

    def refresh(self) -> None:
        w = self.tabs.currentWidget()
        if isinstance(w, _Tab):
            w.refresh()
