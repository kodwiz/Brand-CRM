"""Customers and orders: customer records, manual order entry with price preview, discount approval, stock reservation,
packing/dispatch/delivery outcomes, customer payments and customer returns. All rules are enforced by the backend; this
file presents them and collects input. Accounting entries and distributor credit arrive in later phases."""
from __future__ import annotations

from decimal import Decimal

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLineEdit, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from desktop.pages.base import Page
from desktop.pages.inventory import _money, loc_codes, loc_combo
from desktop.pages.suppliers import QtyGrid, ReasonDialog, _cap, _date_edit, _dec, _Detail, _set_combo, _spin, _Tab
from desktop.session import Session
from desktop.ui.forms import FormDialog, FormGrid
from desktop.ui.lines import LinesEditor
from desktop.ui.widgets import Column, DataTable, PageHeader, Pager, StatePanel, button, label
from desktop.ui.worker import run_async

ORDER_STATUS = {"draft": "warn", "confirmed": "info", "packed": "info", "dispatched": "info", "partially_delivered": "info",
                "delivered": "ok", "delivery_failed": "error", "refused": "error", "cancelled": "neutral",
                "partially_returned": "warn", "returned": "neutral"}
PAY_STATUS = {"unpaid": "warn", "partially_paid": "info", "paid": "ok", "overpaid": "error", "refunded": "neutral",
              "partially_refunded": "info", "not_applicable": "neutral"}
SHIP_STATUS = {"packed": "info", "dispatched": "info", "delivered": "ok", "failed": "error", "refused": "error",
               "received_back": "neutral", "cancelled": "neutral"}
RETURN_STATUS = {"requested": "warn", "approved": "info", "rejected": "neutral", "received": "info", "inspected": "info",
                 "settled": "ok", "cancelled": "neutral"}
TERMS = [("Cash on delivery", "cod"), ("Prepaid (pay before dispatch)", "prepaid"), ("On credit (label only)", "credit")]
RECEIPT_METHODS = [("Cash", "cash"), ("Bank transfer", "bank_transfer"), ("Card", "card"), ("Courier cash on delivery", "cod_courier"),
                   ("Store credit", "store_credit"), ("Other", "other")]
REFUND_METHODS = [("Cash", "cash"), ("Bank transfer", "bank_transfer"), ("Card", "card"), ("Other", "other")]
CATEGORIES = [("Retail", "retail"), ("Employee", "employee"), ("Other", "other")]


def _txt(v) -> str:
    return "" if v in (None, "") else str(v)


def _combo(options: list[tuple[str, str]], current: str | None = None) -> QComboBox:
    cb = QComboBox()
    for t, v in options:
        cb.addItem(t, v)
    if current is not None:
        cb.setCurrentIndex(max(cb.findData(current), 0))
    return cb


# --------------------------------------------------------------------------------------------- customers
class CustomerDialog(FormDialog):
    def __init__(self, session: Session, customer: dict | None = None, parent=None):
        super().__init__("Edit customer" if customer else "New customer", "Save", parent, 600)
        self.session, self.customer = session, customer
        c = customer or {}
        addr = (c.get("addresses") or [{}])[0]
        g = FormGrid(2)
        self.name = QLineEdit(c.get("name", "")); self.phone = QLineEdit(c.get("phone", ""))
        self.email = QLineEdit(c.get("email") or "")
        self.category = _combo(CATEGORIES, c.get("category", "retail"))
        self.address = QLineEdit(addr.get("line", "")); self.city = QLineEdit(addr.get("city") or "")
        self.notes = QLineEdit(c.get("notes") or "")
        g.add("name", "Customer name", self.name, span=2)
        g.add("phone", "Phone", self.phone); g.add("email", "Email", self.email)
        g.add("category", "Category", self.category)
        g.add("city", "City", self.city)
        g.add("address", "Delivery address", self.address, span=2); g.add("notes", "Notes", self.notes, span=2)
        self.form = g
        self.body.addWidget(g)
        self.active = QCheckBox("Active (inactive customers cannot be given new orders)")
        self.active.setChecked(c.get("status", "active") == "active")
        if customer:
            self.body.addWidget(self.active)
        self._addr_id = addr.get("id")

    def submit(self) -> None:
        self.form.clear_errors()
        if not self.name.text().strip():
            self.form.set_error("name", "Enter the customer's name."); return self.reenable()
        if not self.phone.text().strip():
            self.form.set_error("phone", "Enter a phone number. It is used to find the customer and to avoid duplicates."); return self.reenable()
        body = {"name": self.name.text().strip(), "phone": self.phone.text().strip(), "email": self.email.text().strip() or None,
                "category": self.category.currentData(), "notes": self.notes.text().strip() or None}
        if self.address.text().strip():
            body["addresses"] = [{"kind": "delivery", "line": self.address.text().strip(), "city": self.city.text().strip() or None, "is_default": True}]
        c = self.session.client
        if self.customer:
            body["status"] = "active" if self.active.isChecked() else "inactive"
            fn = lambda: c.put(f"/customers/{self.customer['id']}", body)
        else:
            fn = lambda: c.post("/customers", body)
        run_async(fn, lambda r: (setattr(self, "saved", r), self.finish_ok()), lambda e: self.finish_error(e.message))


class CustomerDetail(_Detail):
    def __init__(self, session: Session, customer_id: int, parent=None):
        super().__init__(session, "Customer", parent, (900, 520))
        self.customer_id, self.cust = customer_id, None
        self.info = label("", "muted", wrap=True)
        self.lay.addWidget(self.info)
        self.lay.addWidget(label("Orders (double-click to open)", "fieldLabel"))
        self.orders = DataTable([Column("ref", "Order", 110), Column("order_date", "Date", 100), Column("units", "Units", 60, "right"),
                                 Column("total", "Total", 110, "right", fmt=_money),
                                 Column("payment_status", "Payment", 130, status=lambda v: PAY_STATUS.get(v, "neutral"), fmt=_cap),
                                 Column("status", "State", 150, status=lambda v: ORDER_STATUS.get(v, "neutral"), fmt=_cap)])
        self.orders.row_activated.connect(lambda r: (OrderDetail(self.session, r["id"], self).exec(), self.load()))
        self.panel = StatePanel(self.orders, "No orders yet.")
        self.lay.addWidget(self.panel, 1)
        self.bar = QHBoxLayout(); self.lay.addLayout(self.bar)
        if session.can("customers.manage"):
            self.bar.addWidget(button("Edit customer", on_click=self._edit))
        if session.can("orders.create"):
            self.bar.addWidget(button("New order", "primary", self._order))
        self.bar.addStretch(); self.bar.addWidget(button("Close window", on_click=self.accept))
        self.load()

    def load(self) -> None:
        c = self.session.client
        run_async(lambda: (c.get(f"/customers/{self.customer_id}"), c.get(f"/customers/{self.customer_id}/orders")), self._loaded, self.fail)

    def _loaded(self, d) -> None:
        cu, o = d
        self.cust = cu
        self.head.setText(f"{cu['name']}  ·  {cu['code']}")
        self.sub.setText(f"{_cap(cu['category'])}  ·  {_cap(cu['status'])}")
        bits = [f"Phone {cu['phone']}"] + ([cu["email"]] if cu.get("email") else [])
        for a in cu.get("addresses", [])[:2]:
            bits.append(a["line"] + (f", {a['city']}" if a.get("city") else ""))
        if "store_credit" in cu:
            bits.append(f"Store credit {_money(cu['store_credit'])}")
        if cu.get("notes"):
            bits.append(cu["notes"])
        self.info.setText("  ·  ".join(bits))
        for r in o["items"]:
            pass
        self.orders.set_rows(o["items"])
        self.panel.show_content() if o["items"] else self.panel.show_empty("This customer has no orders you can see yet.")

    def _edit(self) -> None:
        if CustomerDialog(self.session, self.cust, self).exec():
            self.changed = True; self.status.show_message("Customer saved.", "ok"); self.load()

    def _order(self) -> None:
        if OrderDialog(self.session, customer=self.cust, parent=self).exec():
            self.changed = True; self.status.show_message("Order saved.", "ok"); self.load()


class CustomersTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("Search name, phone, code or email"); self.search.setMinimumWidth(280)
        self.search.returnPressed.connect(self._reset)
        bar.addWidget(self.search); bar.addStretch()
        if session.can("customers.manage"):
            bar.addWidget(button("New customer", "primary", self._new))
        self.lay.addLayout(bar)
        self.table = DataTable([Column("code", "Code", 100), Column("name", "Name"), Column("phone", "Phone", 150), Column("email", "Email", 200),
                                Column("category", "Category", 100, fmt=_cap),
                                Column("status", "State", 100, status=lambda v: "ok" if v == "active" else "neutral", fmt=_cap)])
        self.table.row_activated.connect(self._open)
        self.panel = StatePanel(self.table, "No customers yet.")
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
            self.panel.show_content() if d["items"] else self.panel.show_empty(
                "No customers match." if self.search.text() else "No customers yet. Add the first one with New customer.")
        run_async(lambda: c.get("/customers", q=self.search.text().strip() or None, page=self.pager.page, page_size=50), done,
                  lambda e: self.panel.show_error(e.message, e.offline))

    def _new(self) -> None:
        if CustomerDialog(self.session, parent=self).exec():
            self.status.show_message("Customer saved.", "ok"); self.refresh()

    def _open(self, row: dict) -> None:
        d = CustomerDetail(self.session, row["id"], self); d.exec()
        self.refresh()


class CustomersPage(Page):
    title = "Customers"

    def __init__(self, session: Session):
        super().__init__(session)
        self.root.addWidget(PageHeader("Customers", "Customer records for manual order entry. Order bookers see the customers they added or sold to."))
        self.tab = CustomersTab(session)
        self.root.addWidget(self.tab, 1)

    def refresh(self) -> None:
        self.tab.refresh()


# --------------------------------------------------------------------------------------------- order entry
class CustomerPicker(QWidget):
    def __init__(self, session: Session):
        super().__init__()
        self.session = session
        lay = QHBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(); self.edit.setPlaceholderText("Type name or phone, then Enter")
        self.combo = QComboBox(); self.combo.setMinimumWidth(240); self.combo.addItem("No customer chosen", None)
        self.edit.returnPressed.connect(self.search)
        lay.addWidget(self.edit, 1); lay.addWidget(self.combo, 1)
        self.items: list[dict] = []

    def search(self) -> None:
        q = self.edit.text().strip()
        c = self.session.client

        def done(d):
            self.items = d["items"]
            self.combo.clear()
            if not d["items"]:
                self.combo.addItem("No match. Add the customer first.", None)
            for r in d["items"]:
                self.combo.addItem(f"{r['name']}  ·  {r['phone']}  ({r['code']})", r["id"])
        run_async(lambda: c.get("/customers", q=q or None, status="active", page_size=30), done, lambda e: None)

    def set_customer(self, cust: dict) -> None:
        self.items = [cust]
        self.combo.clear(); self.combo.addItem(f"{cust['name']}  ·  {cust['phone']}  ({cust['code']})", cust["id"])
        self.edit.setEnabled(False); self.combo.setEnabled(False)

    def current(self) -> dict | None:
        cid = self.combo.currentData()
        return next((i for i in self.items if i["id"] == cid), None)


class OrderDialog(FormDialog):
    """New order, or edit of a draft. The preview asks the server to price the lines; nothing is computed here."""

    def __init__(self, session: Session, customer: dict | None = None, order: dict | None = None, parent=None):
        super().__init__("Edit draft order" if order else "New order", "Save draft", parent, 980)
        self.session, self.order = session, order
        self.created_id: int | None = None
        g = FormGrid(3)
        self.customer = CustomerPicker(session)
        self.channel = QComboBox()
        self.loc = loc_combo(session)
        self.terms = _combo(TERMS, (order or {}).get("payment_terms", "cod"))
        self.date = _date_edit()
        self.fee = QLineEdit("0")
        d = (order or {}).get("delivery", {})
        self.d_name = QLineEdit(_txt(d.get("name"))); self.d_phone = QLineEdit(_txt(d.get("phone")))
        self.d_city = QLineEdit(_txt(d.get("city"))); self.d_addr = QLineEdit(_txt(d.get("address")))
        self.notes = QLineEdit(_txt((order or {}).get("notes")))
        g.add("customer", "Customer", self.customer, span=2); g.add("channel", "Sales channel", self.channel)
        g.add("loc", "Sold from (location)", self.loc); g.add("terms", "Payment terms", self.terms); g.add("date", "Order date", self.date)
        g.add("d_name", "Deliver to (name)", self.d_name); g.add("d_phone", "Delivery phone", self.d_phone); g.add("d_city", "City", self.d_city)
        g.add("d_addr", "Delivery address", self.d_addr, span=2); g.add("fee", "Delivery fee", self.fee)
        g.add("notes", "Notes", self.notes, span=3)
        self.form = g
        self.body.addWidget(g)
        self.body.addWidget(label("Leave the agreed price empty to sell at the list price less any promotion. A lower agreed price is a manual discount; "
                                  "above the configured limit it needs approval by someone else before the order can be confirmed.", "muted", wrap=True))
        self.editor = LinesEditor(session, [("qty", "Quantity", "int"), ("unit_price", "Agreed unit price (optional)", "money")],
                                  [Column("qty", "Qty", 70, "right"), Column("unit_price", "Agreed price", 110, "right")])
        self.body.addWidget(self.editor)
        self.preview = label("", "muted", wrap=True)
        row = QHBoxLayout(); row.addWidget(button("Preview prices", on_click=self._preview)); row.addWidget(self.preview, 1)
        self.body.addLayout(row)
        self.confirm_btn = button("Save and confirm", "primary", self._save_and_confirm)
        self.root.itemAt(self.root.count() - 1).layout().insertWidget(1, self.confirm_btn)
        self.channel_loaded = False
        run_async(lambda: session.client.get("/sales/channels"), self._channels, lambda e: self.banner.show_message(e.message, "error"))
        if customer:
            self.customer.set_customer(customer)
        if order:
            self._prefill(order)

    def _channels(self, rows) -> None:
        for r in rows:
            self.channel.addItem(r["name"], r["id"])
        if self.order:
            self.channel.setCurrentIndex(max(self.channel.findData(self.order.get("channel_id")), 0))

    def _prefill(self, o: dict) -> None:
        self.customer.set_customer({"id": o["customer_id"], "name": o["customer"]["name"], "phone": o["customer"].get("phone", ""), "code": o["customer"]["code"]})
        self.loc.setCurrentIndex(max(self.loc.findData(o["location_id"]), 0))
        self.fee.setText(o.get("delivery_fee", "0"))
        for l in o["lines"]:
            self.editor.lines.append({"variant_id": l["variant_id"], "sku": l["sku"], "variant": l["variant_name"], "qty": l["qty"],
                                      "unit_price": l["unit_price"] if l.get("unit_price") != l.get("list_price") else None})
        self.editor._render()

    def _body(self) -> dict | None:
        self.form.clear_errors()
        cust = self.customer.combo.currentData()
        if cust is None:
            self.form.set_error("customer", "Search for the customer and choose one."); return None
        if self.channel.currentData() is None:
            self.form.set_error("channel", "Choose the sales channel."); return None
        if self.loc.currentData() is None:
            self.form.set_error("loc", "Choose the location the goods are sold from."); return None
        fee = _dec(self.fee.text())
        if fee is None or fee < 0:
            self.form.set_error("fee", "Enter the delivery fee as a number, or 0."); return None
        lines = []
        for l in self.editor.payload():
            if not l["qty"]:
                self.editor.banner.show_message("Every line needs a quantity above zero.", "error"); return None
            p = _dec(l.get("unit_price") or "")
            if l.get("unit_price") and p is None:
                self.editor.banner.show_message("The agreed price must be a number.", "error"); return None
            lines.append({"variant_id": l["variant_id"], "qty": l["qty"], **({"unit_price": str(p)} if p is not None else {})})
        if not lines:
            self.editor.banner.show_message("Add at least one item.", "error"); return None
        return {"customer_id": cust, "channel_id": self.channel.currentData(), "location_id": self.loc.currentData(),
                "order_date": self.date.date().toString("yyyy-MM-dd"), "payment_terms": self.terms.currentData(),
                "delivery": {"name": self.d_name.text().strip() or None, "phone": self.d_phone.text().strip() or None,
                             "city": self.d_city.text().strip() or None, "address": self.d_addr.text().strip() or None},
                "delivery_fee": str(fee), "notes": self.notes.text().strip() or None, "lines": lines}

    def _preview(self) -> None:
        b = self._body()
        if b is None:
            return
        c = self.session.client
        q = {"customer_id": b["customer_id"], "order_date": b["order_date"], "delivery_fee": b["delivery_fee"],
             "lines": [{k: v for k, v in l.items()} for l in b["lines"]]}

        def done(r):
            txt = (f"List {_money(r['subtotal_list'])}  ·  discounts {_money(r['discount_total'])}  ·  tax {_money(r['tax_total'])}"
                   f"  ·  delivery {_money(r['delivery_fee'])}  ·  total {_money(r['total'])}")
            promos = sorted({p for l in r["lines"] for p in l["promotions"]})
            if promos:
                txt += "  ·  promotions: " + ", ".join(promos)
            if r["approval_required"]:
                txt += f"  ·  MANUAL DISCOUNT ABOVE {r['manual_discount_limit_pct']}%: needs approval before confirming"
            self.preview.setText(txt)
        run_async(lambda: c.post("/orders/quote", q), done, lambda e: self.banner.show_message(e.message, "error"))

    def _send(self, confirm: bool) -> None:
        b = self._body()
        if b is None:
            return self.reenable()
        c = self.session.client
        if self.order:
            b.pop("customer_id", None)
            fn = lambda: c.put(f"/orders/{self.order['id']}", {k: v for k, v in b.items() if k not in ("order_date",)})
        else:
            fn = lambda: c.post("/orders", {**b, "confirm_now": confirm})
        if self.order and confirm:
            fn2 = fn
            fn = lambda: (fn2(), c.post(f"/orders/{self.order['id']}/confirm"))[1]
        run_async(fn, lambda r: (setattr(self, "saved", r), self.finish_ok()), lambda e: self.finish_error(e.message))

    def submit(self) -> None:
        self._send(False)

    def _save_and_confirm(self) -> None:
        self.banner.clear()
        self.submit_btn.setEnabled(False); self.cancel_btn.setEnabled(False); self.confirm_btn.setEnabled(False)
        self._send(True)

    def finish_error(self, message: str) -> None:
        super().finish_error(message)
        self.confirm_btn.setEnabled(True)


# --------------------------------------------------------------------------------------------- order actions
class PackDialog(FormDialog):
    def __init__(self, session: Session, order: dict, parent=None):
        super().__init__(f"Pack {order['ref']}", "Create parcel", parent, 760)
        self.session, self.order = session, order
        self.body.addWidget(label("Choose what goes in this parcel. Only reserved stock can be packed; the rest stays on the order for a later parcel.", "muted", wrap=True))
        self.grid = QtyGrid(["SKU", "Item", "Ordered", "Still to ship", "In this parcel"])
        self.rows = []
        for l in order["lines"]:
            sp = _spin(l["to_ship"], l["to_ship"])
            self.rows.append((l, sp))
            self.grid.add_row([l["sku"], l["variant_name"], l["qty"], l["to_ship"], sp])
        for i, w in enumerate((120, 260, 80, 100, 110)):
            self.grid.setColumnWidth(i, w)
        self.body.addWidget(self.grid)
        self.notes = QLineEdit(); self.notes.setPlaceholderText("Packing notes (optional)")
        self.body.addWidget(self.notes)

    def submit(self) -> None:
        lines = [{"order_line_id": l["id"], "qty": sp.value()} for l, sp in self.rows if sp.value() > 0]
        if not lines:
            self.finish_error("Enter a quantity on at least one line."); return
        c = self.session.client
        run_async(lambda: c.post(f"/orders/{self.order['id']}/shipments", {"lines": lines, "notes": self.notes.text().strip() or None}),
                  lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class CourierDialog(FormDialog):
    def __init__(self, title: str, submit_text: str, call, courier: str = "", tracking: str = "", parent=None):
        super().__init__(title, submit_text, parent, 480)
        self.call = call
        self.courier = QLineEdit(courier); self.courier.setPlaceholderText("Courier (optional)")
        self.tracking = QLineEdit(tracking); self.tracking.setPlaceholderText("Tracking number (optional)")
        self.body.addWidget(self.courier); self.body.addWidget(self.tracking)

    def submit(self) -> None:
        run_async(lambda: self.call({"courier": self.courier.text().strip() or None, "tracking_no": self.tracking.text().strip() or None}),
                  lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class OutcomeDialog(FormDialog):
    def __init__(self, session: Session, shipment: dict, parent=None):
        super().__init__(f"Delivery outcome for {shipment['ref']}", "Record outcome", parent, 480)
        self.session, self.shipment = session, shipment
        self.result = _combo([("Delivered", "delivered"), ("Delivery failed", "failed"), ("Customer refused", "refused")])
        self.reason = QLineEdit(); self.reason.setPlaceholderText("Reason (required unless delivered)")
        self.body.addWidget(self.result); self.body.addWidget(self.reason)
        self.body.addWidget(label("Failed or refused parcels stay out of saleable stock until they are received back and inspected.", "muted", wrap=True))

    def submit(self) -> None:
        r = self.result.currentData()
        if r != "delivered" and not self.reason.text().strip():
            self.finish_error("Say why the delivery failed or was refused."); return
        c = self.session.client
        run_async(lambda: c.post(f"/shipments/{self.shipment['id']}/outcome", {"result": r, "reason": self.reason.text().strip() or None}),
                  lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class ReceiptDialog(FormDialog):
    def __init__(self, session: Session, order: dict, parent=None):
        super().__init__(f"Record payment for {order['ref']}", "Record payment", parent, 520)
        self.session, self.order = session, order
        due = order["money"]["balance_due"]
        self.body.addWidget(label(f"Still due on this order: {_money(due)}. A payment cannot exceed what is owed.", "muted", wrap=True))
        g = FormGrid(2)
        self.amount = QLineEdit(due if _dec(due) and _dec(due) > 0 else "")
        self.method = _combo(RECEIPT_METHODS)
        self.date = _date_edit()
        self.ref = QLineEdit(); self.ref.setPlaceholderText("Required for bank transfer and card")
        g.add("amount", "Amount", self.amount); g.add("method", "Method", self.method)
        g.add("date", "Received on", self.date); g.add("ref", "Transaction reference", self.ref)
        self.form = g
        self.body.addWidget(g)
        self.key = f"ui-{order['id']}-{id(self)}"

    def submit(self) -> None:
        self.form.clear_errors()
        amt = _dec(self.amount.text())
        if amt is None or amt <= 0:
            self.form.set_error("amount", "Enter an amount above zero."); return self.reenable()
        c = self.session.client
        body = {"amount": str(amt), "method": self.method.currentData(), "paid_date": self.date.date().toString("yyyy-MM-dd"),
                "reference": self.ref.text().strip() or None, "idempotency_key": self.key}
        run_async(lambda: c.post(f"/orders/{self.order['id']}/payments", body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class RefundDialog(FormDialog):
    def __init__(self, session: Session, order: dict, parent=None):
        super().__init__(f"Refund overpayment on {order['ref']}", "Refund", parent, 520)
        self.session, self.order = session, order
        over = order["money"].get("overpaid", "0")
        self.body.addWidget(label(f"Overpaid: {_money(over)}. Refunds above this are refused. Refunds for returned goods are settled from the return.", "muted", wrap=True))
        g = FormGrid(2)
        self.amount = QLineEdit(over); self.method = _combo(REFUND_METHODS)
        self.ref = QLineEdit(); self.reason = QLineEdit()
        g.add("amount", "Amount", self.amount); g.add("method", "Method", self.method)
        g.add("ref", "Transaction reference", self.ref); g.add("reason", "Reason", self.reason)
        self.form = g
        self.body.addWidget(g)

    def submit(self) -> None:
        self.form.clear_errors()
        amt = _dec(self.amount.text())
        if amt is None or amt <= 0:
            self.form.set_error("amount", "Enter an amount above zero."); return self.reenable()
        if not self.reason.text().strip():
            self.form.set_error("reason", "Say why the money is being refunded."); return self.reenable()
        c = self.session.client
        body = {"amount": str(amt), "method": self.method.currentData(), "reference": self.ref.text().strip() or None, "reason": self.reason.text().strip()}
        run_async(lambda: c.post(f"/orders/{self.order['id']}/refunds", body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class DecisionDialog(FormDialog):
    def __init__(self, title: str, prompt: str, call, parent=None):
        super().__init__(title, "Submit", parent, 520)
        self.call = call
        self.body.addWidget(label(prompt, "muted", wrap=True))
        self.decision = _combo([("Approve", True), ("Reject", False)])
        self.note = QLineEdit(); self.note.setPlaceholderText("Note (required)")
        self.body.addWidget(self.decision); self.body.addWidget(self.note)

    def submit(self) -> None:
        if not self.note.text().strip():
            self.finish_error("Add a note explaining the decision."); return
        run_async(lambda: self.call(self.decision.currentData(), self.note.text().strip()), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class ReturnRequestDialog(FormDialog):
    def __init__(self, session: Session, order: dict, parent=None):
        super().__init__(f"Request return for {order['ref']}", "Request return", parent, 800)
        self.session, self.order = session, order
        self.body.addWidget(label("Only delivered units can be returned. Stock is not touched until the goods physically arrive and are inspected.", "muted", wrap=True))
        self.grid = QtyGrid(["SKU", "Item", "Delivered", "Already returned", "Return qty"])
        self.rows = []
        for l in order["lines"]:
            room = max(l["delivered"] - l["returned"], 0)
            sp = _spin(0, room)
            self.rows.append((l, sp))
            self.grid.add_row([l["sku"], l["variant_name"], l["delivered"], l["returned"], sp])
        for i, w in enumerate((120, 260, 90, 120, 100)):
            self.grid.setColumnWidth(i, w)
        self.body.addWidget(self.grid)
        g = FormGrid(2)
        self.reason = QLineEdit()
        self.mode = _combo([("Refund money", "refund"), ("Issue store credit", "store_credit")])
        g.add("reason", "Reason", self.reason); g.add("mode", "Settle as", self.mode)
        self.form = g
        self.body.addWidget(g)
        self.fee = QCheckBox("Also refund the delivery fee")
        if _dec(order.get("delivery_fee", "0")) and _dec(order["delivery_fee"]) > 0:
            self.body.addWidget(self.fee)

    def submit(self) -> None:
        self.form.clear_errors()
        lines = [{"order_line_id": l["id"], "qty": sp.value()} for l, sp in self.rows if sp.value() > 0]
        if not lines:
            self.finish_error("Enter a quantity on at least one line."); return
        if not self.reason.text().strip():
            self.form.set_error("reason", "Say why the goods are being returned."); return self.reenable()
        c = self.session.client
        body = {"lines": lines, "reason": self.reason.text().strip(), "refund_mode": self.mode.currentData(), "refund_delivery_fee": self.fee.isChecked()}
        run_async(lambda: c.post(f"/orders/{self.order['id']}/returns", body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class OrderDetail(_Detail):
    def __init__(self, session: Session, order_id: int, parent=None):
        super().__init__(session, "Order", parent, (1120, 720))
        self.order_id, self.o = order_id, None
        self.money = label("", "muted", wrap=True)
        self.lay.addWidget(self.money)
        self.approval = label("", "muted", wrap=True)
        self.lay.addWidget(self.approval)
        full = self._full()
        cols = [Column("sku", "SKU", 120), Column("variant_name", "Item"), Column("qty", "Qty", 56, "right")]
        if full:
            cols += [Column("list_price", "List", 84, "right", fmt=_money), Column("unit_price", "Price", 84, "right", fmt=_money),
                     Column("line_total", "Total", 96, "right", fmt=_money)]
        cols += [Column("packed", "Packed", 66, "right"), Column("dispatched", "Sent", 56, "right"), Column("delivered", "Delivered", 76, "right"),
                 Column("returned", "Returned", 76, "right")]
        self.lines = DataTable(cols)
        self.lay.addWidget(label("Items", "fieldLabel")); self.lay.addWidget(self.lines, 2)
        self.ships = DataTable([Column("ref", "Parcel", 110), Column("status", "State", 130, status=lambda v: SHIP_STATUS.get(v, "neutral"), fmt=_cap),
                                Column("contents", "Contents"), Column("courier", "Courier", 110), Column("tracking_no", "Tracking", 120),
                                Column("attempts", "Attempts", 70, "right")])
        self.lay.addWidget(label("Parcels (select one to act on it)", "fieldLabel")); self.lay.addWidget(self.ships, 1)
        self.pays = None
        if full:
            self.pays = DataTable([Column("ref", "Payment", 110), Column("kind", "Kind", 80, fmt=_cap), Column("method", "Method", 120, fmt=_cap),
                                   Column("amount", "Amount", 100, "right", fmt=_money), Column("paid_date", "Date", 100),
                                   Column("status", "State", 100, fmt=_cap), Column("created_by", "By", 110)])
            self.lay.addWidget(label("Payments and refunds (select one to reverse)", "fieldLabel")); self.lay.addWidget(self.pays, 1)
        self.hist = label("", "muted", wrap=True)
        self.lay.addWidget(self.hist)
        self.bar = QHBoxLayout(); self.lay.addLayout(self.bar)
        self.load()

    def _full(self) -> bool:
        return self.session.can("orders.view_all") or self.session.can("orders.view_own")

    def load(self) -> None:
        c = self.session.client
        run_async(lambda: c.get(f"/orders/{self.order_id}"), self._loaded, self.fail)

    def _btn(self, text, fn, kind=None):
        b = button(text, kind, fn); self.bar.addWidget(b); return b

    def _loaded(self, o: dict) -> None:
        self.o = o
        full = "money" in o
        self.head.setText(f"{o['ref']}  ·  {_cap(o['status'])}" + (f"  ·  payment {_cap(o['payment_status'])}" if full else ""))
        loc = loc_codes(self.session)
        d = o["delivery"]
        bits = [o["customer"]["name"], f"via {o['channel']}", f"sold from {loc.get(o['location_id'], '')}", f"ordered {o['order_date']}",
                f"by {o['created_by']}", dict((v, t) for t, v in TERMS).get(o.get("payment_terms", ""), "") if full else ""]
        dest = ", ".join(x for x in (d.get("name"), d.get("address"), d.get("city"), d.get("phone")) if x)
        if dest:
            bits.append("deliver to " + dest)
        if o.get("cancel_reason"):
            bits.append("cancelled: " + o["cancel_reason"])
        self.sub.setText("  ·  ".join(b for b in bits if b))
        if full:
            m = o["money"]
            self.money.setText(f"Items {_money(o['subtotal_list'])}  ·  discounts {_money(o['discount_total'])}  ·  tax {_money(o['tax_total'])}  ·  delivery {_money(o['delivery_fee'])}"
                               f"  ·  total {_money(o['total'])}  ·  paid {_money(m.get('paid'))}  ·  balance due {_money(m.get('balance_due'))}"
                               + (f"  ·  overpaid {_money(m['overpaid'])}" if _dec(m.get("overpaid", "0")) and _dec(m["overpaid"]) > 0 else ""))
            appr = o.get("discount_approval", "none")
            self.approval.setVisible(appr != "none")
            self.approval.setText({"pending": f"Manual discount {o['manual_discount_pct']}% is above the limit and waits for approval by someone other than the order's creator.",
                                   "approved": f"Manual discount {o['manual_discount_pct']}% approved by {o.get('discount_approved_by')}: {o.get('discount_approval_note') or ''}",
                                   "rejected": f"Manual discount rejected: {o.get('discount_approval_note') or ''}. Change the prices and save again."}.get(appr, ""))
        else:
            self.money.setText("Fulfilment view: prices and payments are not shown to your role.")
            self.approval.setVisible(False)
        rows = o["lines"]
        self.lines.set_rows(rows)
        ships = o["shipments"]
        names = {l["id"]: l["sku"] for l in o["lines"]}
        for s in ships:
            s["contents"] = ", ".join(f"{l['sku']} × {l['qty']}" for l in s["lines"])
        self.ships.set_rows(ships)
        if self.pays is not None:
            self.pays.set_rows(o.get("payments", []))
        self.hist.setText("History: " + "  ·  ".join(f"{e['at'][:16].replace('T', ' ')} {_cap(e['event'])} by {e['by'] or 'system'}" + (f" ({e['note']})" if e.get("note") else "")
                                                       for e in o["events"][-8:]))
        self._buttons()

    def _selected_ship(self):
        return self.ships.selected_row()

    def _buttons(self) -> None:
        while self.bar.count():
            w = self.bar.takeAt(0).widget()
            if w:
                w.deleteLater()
        s, o = self.session, self.o
        st, c = o["status"], s.client
        full = "money" in o
        me = s.user.get("username")
        mine = o["created_by"] == me
        if st == "draft" and full and s.can("orders.create") and (mine or s.can("orders.view_all")):
            self._btn("Edit draft", self._edit)
            self._btn("Confirm order", lambda: self.act(lambda: c.post(f"/orders/{self.order_id}/confirm"), "Order confirmed and stock reserved."), "primary")
        if full and o.get("discount_approval") == "pending" and s.can("discounts.approve") and not mine:
            self._btn("Decide discount", self._discount, "primary")
        if st in ("confirmed", "packed", "partially_delivered", "delivery_failed", "refused") and s.can("orders.fulfil") and any(l["to_ship"] > 0 for l in o["lines"]):
            self._btn("Pack items", self._pack, "primary")
        if st in ("confirmed", "packed") and s.can("orders.update_status"):
            self._btn("Reserve remaining stock", lambda: self.act(lambda: c.post(f"/orders/{self.order_id}/reserve-remaining"), "Stock reserved."))
        if s.can("orders.fulfil"):
            states = {x["status"] for x in o["shipments"]}
            if "packed" in states:
                self._btn("Dispatch parcel", self._dispatch); self._btn("Unpack parcel", self._unpack)
            if "dispatched" in states:
                self._btn("Record outcome", self._outcome)
            if "failed" in states:
                self._btn("Send again", self._redispatch)
            if states & {"failed", "refused"}:
                self._btn("Receive back", self._receive_back)
        if full and st not in ("draft", "cancelled") and s.can("orders.payments"):
            self._btn("Record payment", self._pay)
        if full and s.can("orders.refund"):
            if _dec(o["money"].get("overpaid", "0")) and _dec(o["money"]["overpaid"]) > 0:
                self._btn("Refund overpayment", self._refund)
            if any(p["status"] == "posted" and p["kind"] == "receipt" for p in o.get("payments", [])):
                self._btn("Reverse payment", self._reverse)
        if full and st in ("delivered", "partially_delivered", "partially_returned") and (s.can("orders.update_status") or s.can("orders.returns")):
            self._btn("Request return", self._return)
        if full and st in ("draft", "confirmed", "packed") and s.can("orders.update_status") and (mine or s.can("orders.view_all")):
            self._btn("Cancel order", lambda: self._reason("Cancel order", "Cancelling releases the reserved stock.", "Cancel order",
                                                           lambda r: c.post(f"/orders/{self.order_id}/cancel", {"reason": r}), "Order cancelled."))
        if full and st == "cancelled" and s.can("orders.view_all", "orders.update_status"):
            self._btn("Reopen", lambda: self._reason("Reopen order", "The order returns to draft; prices are checked again when you confirm.", "Reopen",
                                                     lambda r: c.post(f"/orders/{self.order_id}/reopen", {"reason": r}), "Order reopened."))
        self.bar.addStretch(); self._btn("Close window", self.accept)

    def _done(self, ok: str) -> None:
        self.changed = True; self.status.show_message(ok, "ok"); self.load()

    def _reason(self, title, prompt, btn, call, ok) -> None:
        if ReasonDialog(title, prompt, btn, call, self).exec():
            self._done(ok)

    def _edit(self) -> None:
        run_async(lambda: self.session.client.get(f"/customers/{self.o['customer_id']}"), lambda cu: self._edit_with(cu), self.fail)

    def _edit_with(self, cu) -> None:
        self.o["customer"] = {**self.o["customer"], **{k: cu.get(k) for k in ("phone",)}}
        if OrderDialog(self.session, order=self.o, parent=self).exec():
            self._done("Draft saved.")

    def _discount(self) -> None:
        c = self.session.client
        if DecisionDialog("Manual discount", "Approve or reject the manual discount on this order.",
                          lambda a, n: c.post(f"/orders/{self.order_id}/discount-decision", {"approve": a, "note": n}), self).exec():
            self._done("Decision recorded.")

    def _pack(self) -> None:
        if PackDialog(self.session, self.o, self).exec():
            self._done("Parcel created. Dispatch it when it leaves.")

    def _need_ship(self, *states):
        s = self._selected_ship()
        if s is None:
            self.status.show_message("Select a parcel in the table first.", "error"); return None
        if states and s["status"] not in states:
            self.status.show_message(f"{s['ref']} is {_cap(s['status'])}; this action does not apply.", "error"); return None
        return s

    def _dispatch(self) -> None:
        s = self._need_ship("packed")
        if s and CourierDialog(f"Dispatch {s['ref']}", "Dispatch", lambda b: self.session.client.post(f"/shipments/{s['id']}/dispatch", b),
                               self.o["delivery"].get("courier") or "", "", self).exec():
            self._done("Parcel dispatched; stock has left the location.")

    def _outcome(self) -> None:
        s = self._need_ship("dispatched")
        if s and OutcomeDialog(self.session, s, self).exec():
            self._done("Outcome recorded.")

    def _unpack(self) -> None:
        s = self._need_ship("packed")
        if s:
            self.act(lambda: self.session.client.post(f"/shipments/{s['id']}/unpack"), "Parcel unpacked; the stock stays reserved for the order.")

    def _redispatch(self) -> None:
        s = self._need_ship("failed")
        if s and CourierDialog(f"Send {s['ref']} again", "Send again", lambda b: self.session.client.post(f"/shipments/{s['id']}/redispatch", {"tracking_no": b["tracking_no"]}),
                               "", s.get("tracking_no") or "", self).exec():
            self._done("Parcel sent out again.")

    def _receive_back(self) -> None:
        s = self._need_ship("failed", "refused")
        if s:
            self.act(lambda: self.session.client.post(f"/shipments/{s['id']}/receive-back"),
                     "Parcel received back. The goods wait in inspection; they are not saleable yet.")

    def _pay(self) -> None:
        if ReceiptDialog(self.session, self.o, self).exec():
            self._done("Payment recorded.")

    def _refund(self) -> None:
        if RefundDialog(self.session, self.o, self).exec():
            self._done("Refund recorded.")

    def _reverse(self) -> None:
        p = self.pays.selected_row() if self.pays is not None else None
        if p is None:
            self.status.show_message("Select a payment in the table first.", "error"); return
        c = self.session.client
        self._reason("Reverse payment", f"Reverse {p['ref']} ({_money(p['amount'])}). You cannot reverse a payment you recorded yourself.", "Reverse",
                     lambda r: c.post(f"/order-payments/{p['id']}/reverse", {"reason": r}), "Payment reversed.")

    def _return(self) -> None:
        if ReturnRequestDialog(self.session, self.o, self).exec():
            self._done("Return requested. It needs approval before goods can come back.")


# --------------------------------------------------------------------------------------------- orders list
class OrdersTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        self.full = session.can("orders.view_all") or session.can("orders.view_own")
        bar = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("Search order, tracking, customer or phone"); self.search.setMinimumWidth(280)
        self.state = QComboBox(); self.state.addItem("All states", None)
        for k in ORDER_STATUS:
            self.state.addItem(_cap(k), k)
        self.search.returnPressed.connect(self._reset); self.state.currentIndexChanged.connect(lambda _: self._reset())
        bar.addWidget(self.search); bar.addWidget(self.state); bar.addStretch()
        if session.can("orders.create"):
            bar.addWidget(button("New order", "primary", self._new))
        self.lay.addLayout(bar)
        cols = [Column("ref", "Order", 110), Column("customer_name", "Customer"), Column("channel", "Channel", 100), Column("order_date", "Date", 100),
                Column("units", "Units", 56, "right")]
        if self.full:
            cols += [Column("total", "Total", 100, "right", fmt=_money), Column("balance_due", "Due", 100, "right", fmt=_money),
                     Column("payment_status", "Payment", 150, status=lambda v: PAY_STATUS.get(v, "neutral"), fmt=_cap)]
        cols.append(Column("status", "State", 150, status=lambda v: ORDER_STATUS.get(v, "neutral"), fmt=_cap))
        self.table = DataTable(cols)
        self.table.row_activated.connect(self._open)
        self.panel = StatePanel(self.table, "No orders yet.")
        self.panel.retry.connect(self.refresh)
        self.pager = Pager(50); self.pager.changed.connect(self.refresh)
        self.lay.addWidget(self.panel, 1); self.lay.addWidget(self.pager)

    def _reset(self) -> None:
        self.pager.reset(); self.refresh()

    def refresh(self) -> None:
        self.panel.show_loading()
        c, s = self.session.client, self.session

        def done(d):
            for r in d["items"]:
                r["customer_name"] = r["customer"]["name"]
            self.table.set_rows(d["items"]); self.pager.update_total(d["total"])
            self.panel.show_content() if d["items"] else self.panel.show_empty(
                "No orders match." if self.search.text() or self.state.currentData() else "No orders yet. Enter the first one with New order.")
        run_async(lambda: c.get("/orders", location_id=s.current_location_id, status=self.state.currentData(), q=self.search.text().strip() or None,
                                page=self.pager.page, page_size=50), done, lambda e: self.panel.show_error(e.message, e.offline))

    def _new(self) -> None:
        if OrderDialog(self.session, parent=self).exec():
            self.status.show_message("Order saved.", "ok"); self.refresh()

    def _open(self, row: dict) -> None:
        d = OrderDetail(self.session, row["id"], self); d.exec()
        if d.changed:
            self.refresh()


# --------------------------------------------------------------------------------------------- parcels queue
class ParcelsTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.state = QComboBox(); self.state.addItem("All parcels", None)
        for k in SHIP_STATUS:
            self.state.addItem(_cap(k), k)
        self.state.currentIndexChanged.connect(lambda _: self.refresh())
        bar.addWidget(self.state); bar.addStretch()
        self.lay.addLayout(bar)
        self.table = DataTable([Column("ref", "Parcel", 110), Column("order_ref", "Order", 110), Column("contents", "Contents"), Column("destination", "Deliver to"),
                                Column("tracking_no", "Tracking", 120), Column("status", "State", 130, status=lambda v: SHIP_STATUS.get(v, "neutral"), fmt=_cap)])
        self.table.row_activated.connect(lambda r: (OrderDetail(self.session, r["order_id"], self).exec(), self.refresh()))
        self.panel = StatePanel(self.table, "No parcels yet. Confirmed orders appear in Orders; pack them there.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)

    def refresh(self) -> None:
        self.panel.show_loading()
        c, s = self.session.client, self.session

        def done(d):
            for r in d["items"]:
                r["contents"] = ", ".join(f"{l['sku']} × {l['qty']}" for l in r["lines"])
                dl = r["delivery"]
                r["destination"] = ", ".join(x for x in (dl.get("name"), dl.get("city")) if x)
            self.table.set_rows(d["items"])
            self.panel.show_content() if d["items"] else self.panel.show_empty("No parcels match.")
        run_async(lambda: c.get("/shipments", location_id=s.current_location_id, status=self.state.currentData(), page_size=100), done,
                  lambda e: self.panel.show_error(e.message, e.offline))


# --------------------------------------------------------------------------------------------- returns
class ReceiveReturnDialog(FormDialog):
    def __init__(self, session: Session, ret: dict, parent=None):
        super().__init__(f"Receive return {ret['ref']}", "Receive goods", parent, 760)
        self.session, self.ret = session, ret
        self.body.addWidget(label("Count what actually arrived. Received goods are held for inspection and are not saleable until inspected.", "muted", wrap=True))
        self.grid = QtyGrid(["SKU", "Item", "Approved", "Arrived"])
        self.rows = []
        for l in ret["lines"]:
            sp = _spin(l["qty_requested"], l["qty_requested"])
            self.rows.append((l, sp))
            self.grid.add_row([l["sku"], l["variant_name"], l["qty_requested"], sp])
        for i, w in enumerate((120, 300, 90, 100)):
            self.grid.setColumnWidth(i, w)
        self.body.addWidget(self.grid)

    def submit(self) -> None:
        c = self.session.client
        body = {"lines": [{"line_id": l["id"], "qty_received": sp.value()} for l, sp in self.rows]}
        run_async(lambda: c.post(f"/sales-returns/{self.ret['id']}/receive", body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class InspectReturnDialog(FormDialog):
    def __init__(self, session: Session, ret: dict, parent=None):
        super().__init__(f"Inspect return {ret['ref']}", "Save inspection", parent, 920)
        self.session, self.ret = session, ret
        self.body.addWidget(label("Decide what happens to every unit received: back to saleable stock, damaged, or expired. "
                                  "'Refund units' is how many units the customer is credited for.", "muted", wrap=True))
        self.grid = QtyGrid(["SKU", "Item", "Received", "Restock", "Damaged", "Expired", "Refund units", "Note"])
        self.rows = []
        for l in ret["lines"]:
            r, d, e, f = _spin(l["qty_received"], l["qty_received"]), _spin(0, l["qty_received"]), _spin(0, l["qty_received"]), _spin(l["qty_received"], l["qty_received"])
            n = QLineEdit()
            self.rows.append((l, r, d, e, f, n))
            self.grid.add_row([l["sku"], l["variant_name"], l["qty_received"], r, d, e, f, n])
        for i, w in enumerate((110, 220, 80, 80, 80, 80, 100, 180)):
            self.grid.setColumnWidth(i, w)
        self.body.addWidget(self.grid)

    def submit(self) -> None:
        lines = [{"line_id": l["id"], "restock": r.value(), "damaged": d.value(), "expired": e.value(), "refund_qty": f.value(), "note": n.text().strip() or None}
                 for l, r, d, e, f, n in self.rows]
        c = self.session.client
        run_async(lambda: c.post(f"/sales-returns/{self.ret['id']}/inspect", {"lines": lines}), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class SettleReturnDialog(FormDialog):
    def __init__(self, session: Session, ret: dict, parent=None):
        super().__init__(f"Settle return {ret['ref']}", "Settle", parent, 520)
        self.session, self.ret = session, ret
        self.body.addWidget(label(f"Return value {_money(ret.get('return_value'))}. Only what the customer has actually overpaid is given back; the rest reduces what they owe. "
                                  "You cannot settle a return you requested yourself.", "muted", wrap=True))
        g = FormGrid(2)
        self.method = _combo(REFUND_METHODS)
        self.amount = QLineEdit(); self.amount.setPlaceholderText("Leave empty to let the system work it out")
        self.ref = QLineEdit()
        if ret["refund_mode"] == "refund":
            g.add("method", "Pay refund by", self.method)
            g.add("ref", "Transaction reference", self.ref)
        g.add("amount", "Refund amount (optional)", self.amount, span=2)
        self.form = g
        self.body.addWidget(g)

    def submit(self) -> None:
        amt = _dec(self.amount.text())
        body = {"refund_amount": str(amt) if amt is not None else None,
                "method": self.method.currentData() if self.ret["refund_mode"] == "refund" else None, "reference": self.ref.text().strip() or None}
        c = self.session.client
        run_async(lambda: c.post(f"/sales-returns/{self.ret['id']}/settle", body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class ReturnDetail(_Detail):
    def __init__(self, session: Session, return_id: int, parent=None):
        super().__init__(session, "Customer return", parent, (960, 520))
        self.return_id, self.r = return_id, None
        self.table = DataTable([Column("sku", "SKU", 120), Column("variant_name", "Item"), Column("qty_requested", "Requested", 84, "right"),
                                Column("qty_received", "Arrived", 76, "right"), Column("qty_restock", "Restocked", 84, "right"),
                                Column("qty_damaged", "Damaged", 76, "right"), Column("qty_expired", "Expired", 70, "right"), Column("refund_qty", "Refund units", 94, "right")])
        self.lay.addWidget(self.table, 1)
        self.info = label("", "muted", wrap=True)
        self.lay.addWidget(self.info)
        self.bar = QHBoxLayout(); self.lay.addLayout(self.bar)
        self.load()

    def load(self) -> None:
        c = self.session.client
        run_async(lambda: c.get(f"/sales-returns/{self.return_id}"), self._loaded, self.fail)

    def _loaded(self, r: dict) -> None:
        self.r = r
        self.head.setText(f"{r['ref']}  ·  {_cap(r['status'])}  ·  order {r['order_ref']}")
        self.sub.setText(f"{r['customer']['name']}  ·  settle as {_cap(r['refund_mode'])}  ·  requested by {r['requested_by']}  ·  reason: {r['reason']}")
        self.table.set_rows(r["lines"])
        bits = []
        if r.get("decided_by"):
            bits.append(f"Decision by {r['decided_by']}: {r.get('decision_note') or ''}")
        if r.get("return_value") is not None:
            bits.append(f"Return value {_money(r['return_value'])}")
        if r.get("refund_paid") is not None:
            bits.append(f"Refunded {_money(r['refund_paid'])}")
        self.info.setText("  ·  ".join(bits))
        while self.bar.count():
            w = self.bar.takeAt(0).widget()
            if w:
                w.deleteLater()
        s, st, c = self.session, r["status"], self.session.client
        if st == "requested" and s.can("orders.returns") and r["requested_by"] != s.user.get("username"):
            self.bar.addWidget(button("Approve or reject", "primary", self._decide))
        if st == "approved" and s.can("returns.receive"):
            self.bar.addWidget(button("Receive goods", "primary", lambda: self._dlg(ReceiveReturnDialog(s, r, self), "Goods received and held for inspection.")))
        if st == "received" and s.can("returns.receive"):
            self.bar.addWidget(button("Inspect", "primary", lambda: self._dlg(InspectReturnDialog(s, r, self), "Inspection saved.")))
        if st == "inspected" and s.can("orders.refund") and r["requested_by"] != s.user.get("username"):
            self.bar.addWidget(button("Settle refund", "primary", lambda: self._dlg(SettleReturnDialog(s, r, self), "Return settled.")))
        if st in ("requested", "approved") and (s.can("orders.returns") or r["requested_by"] == s.user.get("username")):
            self.bar.addWidget(button("Cancel return", on_click=lambda: self._reason()))
        self.bar.addStretch(); self.bar.addWidget(button("Close window", on_click=self.accept))

    def _dlg(self, dlg, ok: str) -> None:
        if dlg.exec():
            self.changed = True; self.status.show_message(ok, "ok"); self.load()

    def _decide(self) -> None:
        c = self.session.client
        self._dlg(DecisionDialog("Return decision", "Approve to let the customer send the goods back; reject to close the request.",
                                 lambda a, n: c.post(f"/sales-returns/{self.return_id}/decide", {"approve": a, "note": n}), self), "Decision recorded.")

    def _reason(self) -> None:
        c = self.session.client
        self._dlg(ReasonDialog("Cancel return", "Only returns whose goods have not arrived can be cancelled.", "Cancel return",
                               lambda r: c.post(f"/sales-returns/{self.return_id}/cancel", {"reason": r}), self), "Return cancelled.")


class ReturnsTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.state = QComboBox(); self.state.addItem("All returns", None)
        for k in RETURN_STATUS:
            self.state.addItem(_cap(k), k)
        self.state.currentIndexChanged.connect(lambda _: self._reset())
        bar.addWidget(self.state); bar.addStretch()
        self.lay.addLayout(bar)
        self.table = DataTable([Column("ref", "Return", 110), Column("order_ref", "Order", 110), Column("customer_name", "Customer"), Column("units", "Units", 60, "right"),
                                Column("refund_mode", "Settle as", 110, fmt=_cap), Column("reason", "Reason"),
                                Column("status", "State", 120, status=lambda v: RETURN_STATUS.get(v, "neutral"), fmt=_cap)])
        self.table.row_activated.connect(self._open)
        self.panel = StatePanel(self.table, "No customer returns yet.")
        self.panel.retry.connect(self.refresh)
        self.pager = Pager(50); self.pager.changed.connect(self.refresh)
        self.lay.addWidget(self.panel, 1); self.lay.addWidget(self.pager)

    def _reset(self) -> None:
        self.pager.reset(); self.refresh()

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client

        def done(d):
            for r in d["items"]:
                r["customer_name"] = r["customer"]["name"]; r["units"] = sum(l["qty_requested"] for l in r["lines"])
            self.table.set_rows(d["items"]); self.pager.update_total(d["total"])
            self.panel.show_content() if d["items"] else self.panel.show_empty("No returns match." if self.state.currentData() else "No customer returns yet.")
        run_async(lambda: c.get("/sales-returns", status=self.state.currentData(), page=self.pager.page, page_size=50), done,
                  lambda e: self.panel.show_error(e.message, e.offline))

    def _open(self, row: dict) -> None:
        d = ReturnDetail(self.session, row["id"], self); d.exec()
        self.refresh()


class MySalesTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        self.lay.addWidget(label("Your own orders by state. Cancelled and draft orders are left out of the total value.", "muted", wrap=True))
        self.total = label("", "sectionTitle")
        self.lay.addWidget(self.total)
        self.table = DataTable([Column("status", "State", 200, status=lambda v: ORDER_STATUS.get(v, "neutral"), fmt=_cap),
                                Column("orders", "Orders", 100, "right"), Column("value", "Value", 140, "right", fmt=_money)])
        self.panel = StatePanel(self.table, "You have not entered any orders yet.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client

        def done(d):
            self.table.set_rows(d["by_status"])
            self.total.setText(f"{d['orders']} orders  ·  value excluding cancelled and draft {_money(d['value_excluding_cancelled'])}")
            self.panel.show_content() if d["by_status"] else self.panel.show_empty()
        run_async(lambda: c.get("/orders/my-summary"), done, lambda e: self.panel.show_error(e.message, e.offline))


class OrdersPage(Page):
    title = "Orders"

    def __init__(self, session: Session):
        super().__init__(session)
        self.root.addWidget(PageHeader("Orders", "Manual order entry, fulfilment, customer payments and returns. Accounting entries and distributor credit "
                                       "arrive in later phases; payment status here is tracked on the order only."))
        self.tabs = QTabWidget()
        self.root.addWidget(self.tabs, 1)
        items: list[tuple[str, _Tab]] = [("Orders", OrdersTab(session))]
        if session.can("orders.fulfil"):
            items.append(("Parcels", ParcelsTab(session)))
        if session.can("orders.view_all") or session.can("orders.view_own") or session.can("returns.receive"):
            items.append(("Returns", ReturnsTab(session)))
        if session.can("orders.view_own") and not session.can("orders.view_all"):
            items.append(("My sales", MySalesTab(session)))
        self.items = items
        for t, w in items:
            self.tabs.addTab(w, t)
        self.tabs.currentChanged.connect(lambda _: self.refresh())

    def refresh(self) -> None:
        w = self.tabs.currentWidget()
        if isinstance(w, _Tab):
            w.refresh()
