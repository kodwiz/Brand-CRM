"""Distributor credit: accounts, dispatches on credit, returns and exchanges, payments and allocations, adjustments,
aging and monthly statements. Every rule (credit limit, approvals, separation of duties, balances) is enforced by the
backend; this file presents the results and collects input. Balances shown here are read from the posted ledger."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLineEdit, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from desktop.pages.base import Page
from desktop.pages.inventory import _money, loc_codes, loc_combo
from desktop.pages.suppliers import (
    METHODS, QtyGrid, ReasonDialog, _Detail, _Tab, _cap, _date_edit, _dec, _iso, _price, _set_combo, _spin,
)
from desktop.session import Session
from desktop.ui.forms import FormDialog, FormGrid
from desktop.ui.lines import LinesEditor, VariantPicker
from desktop.ui.widgets import Banner, Column, DataTable, PageHeader, Pager, StatePanel, button, label
from desktop.ui.worker import run_async

INV_STATUS = {"draft": "warn", "held": "error", "posted": "ok", "rejected": "neutral", "cancelled": "neutral", "void": "neutral"}
RET_STATUS = {"requested": "warn", "received": "info", "inspected": "info", "credited": "ok", "rejected": "neutral", "cancelled": "neutral"}
PAY_STATUS = {"pending": "warn", "posted": "ok", "rejected": "neutral", "reversed": "neutral"}
ADJ_STATUS = {"pending": "warn", "approved": "ok", "rejected": "neutral"}
ADJ_KINDS = [("Write-off (we stop expecting this money)", "write_off"), ("Credit to the account", "credit"), ("Extra charge (debit)", "debit")]
BUCKETS = [("current", "Not yet due"), ("1_30", "1-30 days"), ("31_60", "31-60 days"), ("61_90", "61-90 days"), ("over_90", "Over 90 days")]
KIND_TEXT = {"dispatch": "Dispatch", "opening": "Opening balance", "debit_note": "Debit note"}
HOLD_TEXT = {"credit_limit": "Over the credit limit", "backdated": "Back-dated", "discount": "Extra discount"}


def _hold_text(reason: str) -> str:
    key = reason.split(":")[0]
    return f"{HOLD_TEXT.get(key, _cap(key))}: {reason.split(':', 1)[1].strip()}" if ":" in reason else HOLD_TEXT.get(key, reason)


def _dist_combo(session: Session, with_all: bool = False, active_only: bool = True) -> QComboBox:
    cb = QComboBox()
    cb.addItem("All distributors" if with_all else "Select a distributor…", None)
    cb.setMinimumWidth(260)
    c = session.client
    run_async(lambda: c.get("/distributors", active="true" if active_only else None, page_size=200),
              lambda d: [cb.addItem(f"{s['name']}  ({s['code']})", s["id"]) for s in d["items"]], lambda e: None)
    return cb


# ------------------------------------------------------------------------------------------------ dialogs
class DistributorDialog(FormDialog):
    def __init__(self, session: Session, row: dict | None = None, parent=None):
        super().__init__("Edit distributor" if row else "New distributor", "Save distributor", parent, 760)
        self.session, self.row = session, row
        g = FormGrid(2)
        self.name = QLineEdit(); self.contact = QLineEdit(); self.phone = QLineEdit(); self.email = QLineEdit(); self.city = QLineEdit()
        self.address = QLineEdit()
        self.discount = QLineEdit(); self.discount.setPlaceholderText("0")
        self.limit = QLineEdit(); self.limit.setPlaceholderText("0.00")
        self.terms = _spin(30, 365)
        self.keeper = QComboBox(); self.keeper.addItem("Not assigned", None)
        self.notes = QLineEdit()
        g.add("name", "Distributor name", self.name, 2); g.add("contact", "Contact person", self.contact); g.add("phone", "Phone", self.phone)
        g.add("email", "Email", self.email); g.add("city", "City", self.city); g.add("address", "Address", self.address, 2)
        g.add("discount", "Discount terms (%)", self.discount); g.add("limit", "Credit limit", self.limit)
        g.add("terms", "Payment terms (days)", self.terms); g.add("keeper", "Assigned stock keeper", self.keeper)
        g.add("notes", "Notes", self.notes, 2)
        self.form = g
        self.body.addWidget(g)
        self.body.addWidget(label("A credit limit of 0 means no credit: every dispatch will wait for approval until a limit is set.", "muted", wrap=True))
        if row:
            for w, k in ((self.name, "name"), (self.contact, "contact_name"), (self.phone, "phone"), (self.email, "email"), (self.city, "city"),
                         (self.address, "address"), (self.notes, "notes")):
                w.setText(str(row.get(k) or ""))
            self.discount.setText(str(row.get("discount_pct") or "0")); self.limit.setText(str(row.get("credit_limit") or "0"))
            self.terms.setValue(int(row.get("payment_terms_days") or 0))
        c = session.client

        def users(d):
            for u in d:
                self.keeper.addItem(f"{u['full_name']}  ({u['username']})", u["id"])
            if row:
                _set_combo(self.keeper, row.get("assigned_user_id"))
        if session.can("users.view"):
            run_async(lambda: c.get("/users", page_size=200).get("items", []), users, lambda e: None)

    def submit(self) -> None:
        self.form.clear_errors()
        if not self.name.text().strip():
            self.form.set_error("name", "Enter the distributor's name."); return self.reenable()
        disc, lim = _dec(self.discount.text() or "0"), _dec(self.limit.text() or "0")
        if disc is None or not 0 <= disc <= 100:
            self.form.set_error("discount", "A percentage between 0 and 100."); return self.reenable()
        if lim is None or lim < 0:
            self.form.set_error("limit", "Enter the credit limit as a number."); return self.reenable()
        body = {"name": self.name.text().strip(), "contact_name": self.contact.text().strip() or None, "phone": self.phone.text().strip() or None,
                "email": self.email.text().strip() or None, "address": self.address.text().strip() or None, "city": self.city.text().strip() or None,
                "discount_pct": str(disc), "credit_limit": str(lim), "payment_terms_days": self.terms.value(),
                "assigned_user_id": self.keeper.currentData(), "notes": self.notes.text().strip() or None}
        c = self.session.client
        call = (lambda: c.put(f"/distributors/{self.row['id']}", body)) if self.row else (lambda: c.post("/distributors", body))
        run_async(call, lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class OpeningDialog(FormDialog):
    def __init__(self, session: Session, distributor: dict, parent=None):
        super().__init__("Opening balance", "Post opening balance", parent, 480)
        self.session, self.d = session, distributor
        self.body.addWidget(label(f"What {distributor['name']} already owed before this system. It can be entered once and is posted to the ledger "
                                  "as a dated opening invoice, so later payments can be allocated against it.", "muted", wrap=True))
        g = FormGrid(2)
        self.amount = QLineEdit(); self.amount.setPlaceholderText("0.00"); self.asof = _date_edit(); self.note = QLineEdit()
        g.add("amount", "Amount owed", self.amount); g.add("asof", "As of", self.asof); g.add("note", "Note", self.note, 2)
        self.form = g
        self.body.addWidget(g)

    def submit(self) -> None:
        self.form.clear_errors()
        a = _dec(self.amount.text())
        if a is None or a <= 0:
            self.form.set_error("amount", "Enter the amount owed."); return self.reenable()
        body = {"amount": str(a), "as_of": _iso(self.asof), "note": self.note.text().strip() or None}
        c = self.session.client
        run_async(lambda: c.post(f"/distributors/{self.d['id']}/opening-balance", body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class PriceDialog(FormDialog):
    def __init__(self, session: Session, distributor: dict, parent=None):
        super().__init__("Agreed price", "Save price", parent, 700)
        self.session, self.d = session, distributor
        self.body.addWidget(label("An agreed price overrides the standard distributor price for this distributor. The distributor's discount terms still apply on top. "
                                  "Leave the price empty to remove the agreement.", "muted", wrap=True))
        self.item = VariantPicker(session)
        self.price = QLineEdit(); self.price.setPlaceholderText("Agreed price per unit")
        g = FormGrid(2)
        g.add("item", "Item", self.item, 2); g.add("price", "Agreed price", self.price)
        self.form = g
        self.body.addWidget(g)

    def submit(self) -> None:
        self.form.clear_errors()
        v = self.item.current()
        if v is None:
            self.form.set_error("item", "Search for an item and choose it."); return self.reenable()
        p = _dec(self.price.text())
        if self.price.text().strip() and p is None:
            self.form.set_error("price", "Enter the price as a number."); return self.reenable()
        c, vid = self.session.client, v["id"]
        run_async(lambda: c.put(f"/distributors/{self.d['id']}/prices/{vid}", {"price": str(p) if p is not None else None}),
                  lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class DispatchDialog(FormDialog):
    def __init__(self, session: Session, distributor_id: int | None = None, parent=None):
        super().__init__("Dispatch goods on credit", "Save dispatch", parent, 960)
        self.session = session
        g = FormGrid(3)
        self.dist = _dist_combo(session)
        self.loc = loc_combo(session)
        self.date = _date_edit()
        self.exchange = QComboBox(); self.exchange.addItem("Not an exchange", None)
        self.notes = QLineEdit()
        g.add("dist", "Distributor", self.dist); g.add("loc", "Dispatch from", self.loc); g.add("date", "Invoice date", self.date)
        g.add("exchange", "Replaces returned goods", self.exchange, 2); g.add("notes", "Notes", self.notes)
        self.form = g
        self.body.addWidget(g)
        self.body.addWidget(label("Prices come from the distributor's agreed price list (or the standard distributor price) less their discount terms. "
                                  "Leave the price empty to use that price. A dispatch over the credit limit, back-dated, or with extra discount waits for approval and moves nothing until then.",
                                  "muted", wrap=True))
        self.editor = LinesEditor(session, [("qty", "Quantity", "int"), ("unit_price", "Price (optional)", "money")],
                                  [Column("qty", "Quantity", 90, "right"), Column("unit_price", "Price", 100, "right", fmt=lambda v: _price(v) if v else "Agreed")])
        self.body.addWidget(self.editor)
        self.post_now = QCheckBox("Post now (remove the stock and charge the account)")
        self.post_now.setChecked(True)
        self.body.addWidget(self.post_now)
        self.dist.currentIndexChanged.connect(self._exchange_options)
        if distributor_id:
            _set_combo(self.dist, distributor_id)

    def _exchange_options(self) -> None:
        self.exchange.clear(); self.exchange.addItem("Not an exchange", None)
        did, c = self.dist.currentData(), self.session.client
        if not did:
            return
        run_async(lambda: c.get("/distributor-returns", distributor_id=did, status="credited", page_size=100),
                  lambda d: [self.exchange.addItem(f"{r['ref']}  ·  credit {_money(r['credit_value'])}", r["id"]) for r in d["items"]], lambda e: None)

    def submit(self) -> None:
        self.form.clear_errors()
        if self.dist.currentData() is None:
            self.form.set_error("dist", "Choose a distributor."); return self.reenable()
        if self.loc.currentData() is None:
            self.form.set_error("loc", "Choose where the goods leave from."); return self.reenable()
        lines = []
        for ln in self.editor.payload():
            if not ln["qty"]:
                self.finish_error("Every line needs a quantity."); return
            row = {"variant_id": ln["variant_id"], "qty": ln["qty"]}
            if ln.get("unit_price"):
                p = _dec(ln["unit_price"])
                if p is None:
                    self.finish_error("A price is not a number."); return
                row["unit_price"] = str(p)
            lines.append(row)
        if not lines:
            self.finish_error("Add at least one item."); return
        body = {"distributor_id": self.dist.currentData(), "location_id": self.loc.currentData(), "invoice_date": _iso(self.date), "lines": lines,
                "notes": self.notes.text().strip() or None, "exchange_return_id": self.exchange.currentData(),
                "idempotency_key": uuid.uuid4().hex[:40], "post_now": self.post_now.isChecked()}
        c = self.session.client
        run_async(lambda: c.post("/distributor-dispatches", body), lambda r: (setattr(self, "created", r), self.finish_ok()), lambda e: self.finish_error(e.message))

    created: dict | None = None


class ReturnDialog(FormDialog):
    def __init__(self, session: Session, distributor_id: int | None = None, parent=None):
        super().__init__("Request a return", "Request return", parent, 960)
        self.session = session
        g = FormGrid(3)
        self.dist = _dist_combo(session, active_only=False)
        self.loc = loc_combo(session)
        self.reason = QLineEdit(); self.reason.setPlaceholderText("Why is it coming back?")
        g.add("dist", "Distributor", self.dist); g.add("loc", "Return to", self.loc); g.add("reason", "Reason", self.reason)
        self.form = g
        self.body.addWidget(g)
        self.body.addWidget(label("Choose how many units of each dispatched item are coming back. Credit is given at the price on the original dispatch, "
                                  "only after the goods are received, inspected and the credit approved. For an exchange, create the replacement dispatch afterwards.", "muted", wrap=True))
        self.grid = QtyGrid(["Dispatch", "Item", "Dispatched", "Already returned", "Returning now"])
        self.body.addWidget(self.grid)
        self.rows: list[tuple[dict, QSpinBox]] = []
        self.dist.currentIndexChanged.connect(self._load)
        if distributor_id:
            _set_combo(self.dist, distributor_id)

    def _load(self) -> None:
        did, c = self.dist.currentData(), self.session.client
        self.grid.setRowCount(0); self.rows = []
        if not did:
            return

        def done(d):
            self.grid.setRowCount(0); self.rows = []
            for i, w in enumerate((130, 330, 100, 130, 130)):
                self.grid.setColumnWidth(i, w)
            for inv in d["items"]:
                for ln in inv["lines"]:
                    left = ln.get("returnable", ln["qty"])
                    sp = _spin(0, max(int(left), 0))
                    self.rows.append((ln, sp))
                    self.grid.add_row([inv["ref"], f"{ln['sku']} · {ln['variant_name']}", ln["qty"], int(ln["qty"]) - int(left), sp])
        run_async(lambda: c.get("/distributor-dispatches", distributor_id=did, status="posted", kind="dispatch", page_size=100), done,
                  lambda e: self.banner.show_message(e.message, "error"))

    def submit(self) -> None:
        self.form.clear_errors()
        if self.dist.currentData() is None:
            self.form.set_error("dist", "Choose a distributor."); return self.reenable()
        if self.loc.currentData() is None:
            self.form.set_error("loc", "Choose where the goods come back to."); return self.reenable()
        if not self.reason.text().strip():
            self.form.set_error("reason", "A reason is required."); return self.reenable()
        lines = [{"invoice_line_id": ln["id"], "qty": sp.value()} for ln, sp in self.rows if sp.value() > 0]
        if not lines:
            self.finish_error("Enter how many units are coming back."); return
        body = {"distributor_id": self.dist.currentData(), "location_id": self.loc.currentData(), "reason": self.reason.text().strip(), "lines": lines}
        c = self.session.client
        run_async(lambda: c.post("/distributor-returns", body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class ReceiveReturnDialog(FormDialog):
    def __init__(self, session: Session, ret: dict, parent=None):
        super().__init__("Receive returned goods", "Record receipt", parent, 760)
        self.session, self.ret = session, ret
        self.body.addWidget(label("Count what actually arrived. Returned goods go to inspection and cannot be sold until inspected.", "muted", wrap=True))
        self.grid = QtyGrid(["Item", "Expected", "Received"])
        self.grid.setColumnWidth(0, 360); self.grid.setColumnWidth(1, 100); self.grid.setColumnWidth(2, 120)
        self.spins = []
        for ln in ret["lines"]:
            sp = _spin(int(ln["qty_requested"]), int(ln["qty_requested"]))
            self.spins.append((ln, sp))
            self.grid.add_row([f"{ln['sku']} · {ln['variant_name']}", ln["qty_requested"], sp])
        self.body.addWidget(self.grid)

    def submit(self) -> None:
        body = {"lines": [{"line_id": ln["id"], "qty_received": sp.value()} for ln, sp in self.spins]}
        c, rid = self.session.client, self.ret["id"]
        run_async(lambda: c.post(f"/distributor-returns/{rid}/receive", body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class InspectDialog(FormDialog):
    def __init__(self, session: Session, ret: dict, parent=None):
        super().__init__("Inspect returned goods", "Record inspection", parent, 960)
        self.session, self.ret = session, ret
        self.body.addWidget(label("Decide where each received unit goes: back to saleable stock, damaged, or expired. "
                                  "Credit quantity is how many units the distributor is credited for; it defaults to all received units.", "muted", wrap=True))
        self.grid = QtyGrid(["Item", "Received", "Saleable", "Damaged", "Expired", "Credit units"])
        for i, w in enumerate((330, 90, 100, 100, 100, 110)):
            self.grid.setColumnWidth(i, w)
        self.rows = []
        for ln in ret["lines"]:
            n = int(ln["qty_received"] or 0)
            a, b, c_, d = _spin(n, n), _spin(0, n), _spin(0, n), _spin(n, int(ln["qty_requested"]))
            self.rows.append((ln, a, b, c_, d))
            self.grid.add_row([f"{ln['sku']} · {ln['variant_name']}", n, a, b, c_, d])
        self.body.addWidget(self.grid)

    def submit(self) -> None:
        lines = []
        for ln, a, b, c_, d in self.rows:
            if a.value() + b.value() + c_.value() != int(ln["qty_received"] or 0):
                self.finish_error(f"{ln['sku']}: saleable + damaged + expired must equal the {ln['qty_received']} received."); return
            lines.append({"line_id": ln["id"], "restock": a.value(), "damaged": b.value(), "expired": c_.value(), "credit_qty": d.value()})
        c, rid = self.session.client, self.ret["id"]
        run_async(lambda: c.post(f"/distributor-returns/{rid}/inspect", {"lines": lines}), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class DecisionDialog(FormDialog):
    def __init__(self, title: str, prompt: str, call, parent=None):
        super().__init__(title, "Confirm", parent, 500)
        self.call = call
        self.body.addWidget(label(prompt, "muted", wrap=True))
        self.approve = QComboBox(); self.approve.addItem("Approve", True); self.approve.addItem("Reject", False)
        self.note = QLineEdit(); self.note.setPlaceholderText("Reason for the decision (required)")
        self.body.addWidget(self.approve); self.body.addWidget(self.note)

    def submit(self) -> None:
        if not self.note.text().strip():
            self.finish_error("A note is required."); return
        ok, note = self.approve.currentData(), self.note.text().strip()
        run_async(lambda: self.call({"approve": ok, "note": note}), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class _AllocGrid:
    """Open invoices with an amount box each; shared by the payment and allocate dialogs."""

    def __init__(self, session: Session, grid: QtyGrid, on_change):
        self.session, self.grid, self.on_change = session, grid, on_change
        self.rows: list[tuple[dict, QLineEdit]] = []

    def load(self, distributor_id: int | None, fill_first: float | None = None) -> None:
        self.grid.setRowCount(0); self.rows = []
        if not distributor_id:
            return
        c = self.session.client

        def done(d):
            self.grid.setRowCount(0); self.rows = []
            for i, w in enumerate((130, 150, 110, 130, 130)):
                self.grid.setColumnWidth(i, w)
            for inv in d["items"]:
                if Decimal(inv["outstanding"]) <= 0:
                    continue
                e = QLineEdit(); e.setPlaceholderText("0.00"); e.textChanged.connect(lambda _=None: self.on_change())
                self.rows.append((inv, e))
                self.grid.add_row([inv["ref"], KIND_TEXT.get(inv["kind"], inv["kind"]), str(inv["due_date"]), _money(inv["outstanding"]), e])
            self.on_change()
        run_async(lambda: c.get("/distributor-dispatches", distributor_id=distributor_id, status="posted", page_size=200), done, lambda e: None)

    def total(self) -> Decimal:
        return sum((_dec(e.text()) or Decimal(0) for _, e in self.rows), Decimal(0))

    def payload(self) -> list[dict] | str:
        out = []
        for inv, e in self.rows:
            v = _dec(e.text())
            if v is None and e.text().strip():
                return f"{inv['ref']}: the amount is not a number."
            if v and v > 0:
                out.append({"invoice_id": inv["id"], "amount": str(v)})
        return out


class PaymentDialog(FormDialog):
    def __init__(self, session: Session, distributor_id: int | None = None, parent=None):
        super().__init__("Record payment received", "Record payment", parent, 900)
        self.session = session
        g = FormGrid(4)
        self.dist = _dist_combo(session, active_only=False)
        self.amount = QLineEdit(); self.amount.setPlaceholderText("0.00")
        self.method = QComboBox()
        for t, v in METHODS:
            self.method.addItem(t, v)
        self.date = _date_edit(); self.reference = QLineEdit(); self.reference.setPlaceholderText("Cheque / transfer number")
        g.add("dist", "Distributor", self.dist, 2); g.add("amount", "Amount received", self.amount)
        g.add("method", "Method", self.method); g.add("date", "Received on", self.date); g.add("reference", "Reference", self.reference, 3)
        self.form = g
        self.body.addWidget(g)
        self.body.addWidget(label("Choose which dispatches this payment settles, or tick the box to settle the oldest first. Anything left over stays on the account as "
                                  "unallocated credit and can be allocated later. A payment dated before the allowed back-dating window waits for approval.", "muted", wrap=True))
        self.grid = QtyGrid(["Document", "Type", "Due", "Outstanding", "Apply now"])
        self.body.addWidget(self.grid)
        self.alloc = _AllocGrid(session, self.grid, self._recount)
        self.total_lbl = label("", "muted")
        self.auto = QCheckBox("Settle the oldest unpaid dispatches first")
        self.body.addWidget(self.total_lbl); self.body.addWidget(self.auto)
        self.dist.currentIndexChanged.connect(lambda _: self.alloc.load(self.dist.currentData()))
        self.amount.textChanged.connect(self._recount)
        if distributor_id:
            _set_combo(self.dist, distributor_id)

    def _recount(self) -> None:
        t, a = self.alloc.total(), _dec(self.amount.text()) or Decimal(0)
        self.total_lbl.setText(f"Allocated to dispatches: {t:,.2f}   ·   Left on account: {max(a - t, Decimal(0)):,.2f}")

    def submit(self) -> None:
        self.form.clear_errors()
        if self.dist.currentData() is None:
            self.form.set_error("dist", "Choose a distributor."); return self.reenable()
        amt = _dec(self.amount.text())
        if amt is None or amt <= 0:
            self.form.set_error("amount", "Enter the amount received."); return self.reenable()
        allocs = self.alloc.payload()
        if isinstance(allocs, str):
            self.finish_error(allocs); return
        if sum((Decimal(a["amount"]) for a in allocs), Decimal(0)) > amt:
            self.finish_error("More is allocated than the amount received."); return
        body = {"distributor_id": self.dist.currentData(), "amount": str(amt), "method": self.method.currentData(), "paid_date": _iso(self.date),
                "reference": self.reference.text().strip() or None, "allocations": allocs, "auto_allocate": self.auto.isChecked() and not allocs,
                "idempotency_key": uuid.uuid4().hex[:40]}
        c = self.session.client
        run_async(lambda: c.post("/distributor-payments", body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class AllocateDialog(FormDialog):
    def __init__(self, session: Session, payment: dict, parent=None):
        super().__init__("Allocate unallocated payment", "Allocate", parent, 800)
        self.session, self.payment = session, payment
        self.body.addWidget(label(f"{payment['ref']} has {_money(payment['unallocated'])} not yet applied to any dispatch.", "muted", wrap=True))
        self.grid = QtyGrid(["Document", "Type", "Due", "Outstanding", "Apply now"])
        self.body.addWidget(self.grid)
        self.total_lbl = label("", "muted"); self.body.addWidget(self.total_lbl)
        self.alloc = _AllocGrid(session, self.grid, lambda: self.total_lbl.setText(f"Applying {self.alloc.total():,.2f} of {_money(payment['unallocated'])}"))
        self.alloc.load(payment["distributor"]["id"])

    def submit(self) -> None:
        allocs = self.alloc.payload()
        if isinstance(allocs, str) or not allocs:
            self.finish_error(allocs if isinstance(allocs, str) else "Enter an amount against at least one dispatch."); return
        c, did = self.session.client, self.payment["distributor"]["id"]
        body = {"source_type": "payment", "source_id": self.payment["id"], "allocations": allocs}
        run_async(lambda: c.post(f"/distributors/{did}/allocate", body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class AdjustmentDialog(FormDialog):
    def __init__(self, session: Session, distributor_id: int | None = None, parent=None):
        super().__init__("Request an account adjustment", "Request adjustment", parent, 720)
        self.session = session
        g = FormGrid(2)
        self.dist = _dist_combo(session, active_only=False)
        self.kind = QComboBox()
        for t, v in ADJ_KINDS:
            self.kind.addItem(t, v)
        self.amount = QLineEdit(); self.amount.setPlaceholderText("0.00")
        self.inv = QComboBox(); self.inv.addItem("Not tied to one dispatch", None)
        self.reason = QLineEdit(); self.reason.setPlaceholderText("Why is this needed?")
        g.add("dist", "Distributor", self.dist, 2); g.add("kind", "Adjustment", self.kind); g.add("amount", "Amount", self.amount)
        g.add("inv", "Against dispatch (optional)", self.inv, 2); g.add("reason", "Reason", self.reason, 2)
        self.form = g
        self.body.addWidget(g)
        self.body.addWidget(label("Adjustments change what a distributor owes, so they take effect only after someone else approves them. "
                                  "A write-off or credit can never exceed what is still owed on the dispatch it names.", "muted", wrap=True))
        self.dist.currentIndexChanged.connect(self._load)
        if distributor_id:
            _set_combo(self.dist, distributor_id)

    def _load(self) -> None:
        self.inv.clear(); self.inv.addItem("Not tied to one dispatch", None)
        did, c = self.dist.currentData(), self.session.client
        if did:
            run_async(lambda: c.get("/distributor-dispatches", distributor_id=did, status="posted", page_size=200),
                      lambda d: [self.inv.addItem(f"{i['ref']}  ·  owed {_money(i['outstanding'])}", i["id"]) for i in d["items"] if Decimal(i["outstanding"]) > 0],
                      lambda e: None)

    def submit(self) -> None:
        self.form.clear_errors()
        if self.dist.currentData() is None:
            self.form.set_error("dist", "Choose a distributor."); return self.reenable()
        a = _dec(self.amount.text())
        if a is None or a <= 0:
            self.form.set_error("amount", "Enter the amount."); return self.reenable()
        if not self.reason.text().strip():
            self.form.set_error("reason", "A reason is required."); return self.reenable()
        body = {"distributor_id": self.dist.currentData(), "kind": self.kind.currentData(), "amount": str(a), "invoice_id": self.inv.currentData(),
                "reason": self.reason.text().strip()}
        c = self.session.client
        run_async(lambda: c.post("/distributor-adjustments", body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class StatementDialog(QDialog):
    def __init__(self, session: Session, distributor: dict, parent=None):
        super().__init__(parent)
        self.session, self.d = session, distributor
        self.setWindowTitle(f"Statement: {distributor['name']}")
        self.setMinimumSize(900, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 14)
        lay.addWidget(label(f"{distributor['name']}  ({distributor['code']})", "sectionTitle"))
        bar = QHBoxLayout()
        self.month = QComboBox()
        today = date.today()
        y, m = today.year, today.month
        for _ in range(24):
            self.month.addItem(date(y, m, 1).strftime("%B %Y"), f"{y:04d}-{m:02d}")
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        self.month.currentIndexChanged.connect(lambda _: self.load())
        bar.addWidget(label("Month", "fieldLabel")); bar.addWidget(self.month); bar.addStretch()
        lay.addLayout(bar)
        self.summary = label("Loading…", "muted", wrap=True)
        lay.addWidget(self.summary)
        self.table = DataTable([Column("date", "Date", 110), Column("type", "Entry", 120, fmt=_cap), Column("ref", "Document", 130), Column("memo", "Memo"),
                                Column("amount", "Amount", 110, "right", fmt=_money), Column("balance", "Balance", 120, "right", fmt=_money)])
        self.panel = StatePanel(self.table, "No transactions this month.")
        lay.addWidget(self.panel, 1)
        row = QHBoxLayout(); row.addStretch(); row.addWidget(button("Close", on_click=self.accept)); lay.addLayout(row)
        self.load()

    def load(self) -> None:
        c, did, month = self.session.client, self.d["id"], self.month.currentData()
        self.panel.show_loading()

        def done(d):
            self.table.set_rows(d["entries"])
            ok = "" if d.get("reconciles") else "   ·   DOES NOT RECONCILE: report this to a manager."
            self.summary.setText(f"Opening {_money(d['opening_balance'])}  +  charges {_money(d['charges'])}  −  credits {_money(d.get('credits', 0))}  "
                                 f"−  payments {_money(d['payments'])}  =  closing {_money(d['closing_balance'])}. Positive means the distributor owes us.{ok}")
            self.panel.show_content() if d["entries"] else self.panel.show_empty()
        run_async(lambda: c.get(f"/distributors/{did}/statement", month=month), done, lambda e: self.panel.show_error(e.message, e.offline))


# ------------------------------------------------------------------------------------------------ detail dialogs
class AccountDetail(_Detail):
    def __init__(self, session: Session, distributor_id: int, parent=None):
        super().__init__(session, "Distributor account", parent, (1020, 700))
        self.did, self.data = distributor_id, None
        self.cards = label("", "muted", wrap=True)
        self.lay.addWidget(self.cards)
        self.lay.addWidget(label("Open dispatches (oldest first)", "fieldLabel"))
        self.inv = DataTable([Column("ref", "Document", 110), Column("kind", "Type", 120, fmt=lambda v: KIND_TEXT.get(v, v)), Column("invoice_date", "Dated", 100),
                              Column("due_date", "Due", 100), Column("total", "Total", 110, "right", fmt=_money), Column("outstanding", "Outstanding", 120, "right", fmt=_money),
                              Column("days_overdue", "Days overdue", 110, "right", fmt=lambda v: str(v) if v else "-")])
        self.invp = StatePanel(self.inv, "Nothing is owed.")
        self.lay.addWidget(self.invp, 1)
        self.lay.addWidget(label("Agreed prices", "fieldLabel"))
        self.prices = DataTable([Column("sku", "SKU", 130), Column("name", "Item"), Column("price", "Agreed price", 120, "right", fmt=_price),
                                 Column("standard_price", "Standard distributor price", 180, "right", fmt=_price)])
        self.pp = StatePanel(self.prices, "No agreed prices; the standard distributor price applies.")
        self.lay.addWidget(self.pp, 1)
        self.bar = QHBoxLayout(); self.lay.addLayout(self.bar)
        self.load()

    def load(self) -> None:
        c, did = self.session.client, self.did
        run_async(lambda: c.get(f"/distributors/{did}"), self._head, self.fail)
        run_async(lambda: c.get(f"/distributors/{did}/aging"), self._aging, self.fail)
        run_async(lambda: c.get(f"/distributors/{did}/prices"), self._prices, lambda e: self.pp.show_error(e.message, e.offline))

    def _head(self, d: dict) -> None:
        self.data = d
        self.head.setText(f"{d['name']}  ({d['code']})")
        self.sub.setText(f"{d.get('contact_name') or 'No contact'}  ·  {d.get('phone') or 'no phone'}  ·  {d.get('city') or ''}   —   discount terms {d['discount_pct']}%  ·  "
                         f"{d['payment_terms_days']} days  ·  assigned to {d.get('assigned_user') or 'nobody'}  ·  {'Active' if d['is_active'] else 'Inactive'}")
        limit_note = "no credit allowed" if Decimal(d["credit_limit"]) == 0 else f"limit {_money(d['credit_limit'])}, available {_money(d['available_credit'])}"
        self.cards.setText(f"Owes us {_money(d['balance'])}   ·   {limit_note}   ·   overdue {_money(d['overdue'])}   ·   unallocated credit {_money(d['unallocated_credit'])}")
        while self.bar.count():
            w = self.bar.takeAt(0).widget()
            if w:
                w.deleteLater()
        s = self.session
        self.bar.addWidget(button("Statement", None, lambda: StatementDialog(s, d, self).exec()))
        if s.can("distributors.manage"):
            self.bar.addWidget(button("Edit terms", None, self._edit)); self.bar.addWidget(button("Set agreed price", None, self._price))
            self.bar.addWidget(button("Opening balance", None, self._opening))
        self.bar.addStretch()
        if s.can("distributors.receive_payment"):
            self.bar.addWidget(button("Record payment", None, lambda: self._do(PaymentDialog(s, self.did, self), "Payment recorded.")))
        if s.can("distributors.transact"):
            self.bar.addWidget(button("Request return", None, lambda: self._do(ReturnDialog(s, self.did, self), "Return requested.")))
            self.bar.addWidget(button("New dispatch", "primary", lambda: self._do(DispatchDialog(s, self.did, self), "Dispatch saved.")))
        self.bar.addWidget(button("Close", None, self.accept))

    def _aging(self, a: dict) -> None:
        rows = a["invoices"]
        self.inv.set_rows(rows)
        self.invp.show_content() if rows else self.invp.show_empty()
        b = a["buckets"]
        self.sub.setText(self.sub.text() + "\n" + "Aging:  " + "   ·   ".join(f"{t} {_money(b[k])}" for k, t in BUCKETS))

    def _prices(self, d) -> None:
        rows = d if isinstance(d, list) else d.get("items", d.get("rows", []))
        self.prices.set_rows(rows)
        self.pp.show_content() if rows else self.pp.show_empty()

    def _do(self, dlg: FormDialog, ok: str) -> None:
        if dlg.exec():
            self.changed = True
            self.status.show_message(ok, "ok")
            self.load()

    def _edit(self) -> None:
        self._do(DistributorDialog(self.session, self.data, self), "Terms saved.")

    def _price(self) -> None:
        self._do(PriceDialog(self.session, self.data, self), "Agreed price saved.")

    def _opening(self) -> None:
        self._do(OpeningDialog(self.session, self.data, self), "Opening balance posted.")


class DispatchDetail(_Detail):
    def __init__(self, session: Session, invoice_id: int, parent=None):
        super().__init__(session, "Dispatch", parent, (940, 600))
        self.iid, self.data = invoice_id, None
        self.holds = Banner()
        self.lay.addWidget(self.holds)
        cols = [Column("sku", "SKU", 120), Column("variant_name", "Item"), Column("qty", "Qty", 70, "right"), Column("base_price", "List", 100, "right", fmt=_price),
                Column("discount_pct", "Disc. %", 80, "right"), Column("unit_price", "Charged", 100, "right", fmt=_price), Column("line_total", "Line total", 110, "right", fmt=_money)]
        if session.can("products.view_cost"):
            cols.append(Column("stock_value", "Stock cost", 110, "right", fmt=_money))
        self.table = DataTable(cols)
        self.lay.addWidget(self.table, 1)
        self.totals = label("", "muted", wrap=True)
        self.lay.addWidget(self.totals)
        self.bar = QHBoxLayout(); self.lay.addLayout(self.bar)
        self.load()

    def load(self) -> None:
        c, iid = self.session.client, self.iid
        run_async(lambda: c.get(f"/distributor-dispatches/{iid}"), self._show, self.fail)

    def _show(self, d: dict) -> None:
        self.data = d
        self.head.setText(f"{d['ref']}  ·  {d['distributor']['name']}")
        self.sub.setText(f"{KIND_TEXT.get(d['kind'], d['kind'])}  ·  {_cap(d['status'])}  ·  dated {d['invoice_date']}, due {d['due_date']}  ·  created by {d.get('created_by')}"
                         + (f"  ·  decided by {d['decided_by']}: {d.get('decision_note')}" if d.get("decided_by") else ""))
        if d["status"] == "held":
            self.holds.show_message("Waiting for approval, nothing has moved and nothing is owed yet.\n" + "\n".join(_hold_text(r) for r in d["hold_reasons"]), "info")
        else:
            self.holds.clear()
        self.table.set_rows(d["lines"])
        self.totals.setText(f"Subtotal {_money(d['subtotal'])}  −  discount {_money(d['discount_total'])}  =  total {_money(d['total'])}   ·   settled {_money(d['allocated'])}   ·   outstanding {_money(d['outstanding'])}"
                            + ("   ·   Replaces returned goods (exchange)" if d.get("exchange_return_id") else ""))
        while self.bar.count():
            w = self.bar.takeAt(0).widget()
            if w:
                w.deleteLater()
        s, st = self.session, d["status"]
        self.bar.addStretch()
        if st == "draft" and s.can("distributors.transact"):
            self.bar.addWidget(button("Post dispatch", "primary", lambda: self.act(lambda: s.client.post(f"/distributor-dispatches/{self.iid}/post"), "Posted (or held for approval, see above).")))
        if st == "held" and s.can("distributors.approve"):
            self.bar.addWidget(button("Approve or reject…", "primary", self._decide))
        if st in ("draft", "held") and s.can("distributors.transact"):
            self.bar.addWidget(button("Cancel dispatch", None, self._cancel))
        self.bar.addWidget(button("Close", None, self.accept))

    def _decide(self) -> None:
        c, iid = self.session.client, self.iid
        if DecisionDialog("Decide on held dispatch", "You cannot approve a dispatch you created. Approving posts it: stock leaves and the account is charged.",
                          lambda body: c.post(f"/distributor-dispatches/{iid}/decide", body), self).exec():
            self.changed = True; self.status.show_message("Decision recorded.", "ok"); self.load()

    def _cancel(self) -> None:
        c, iid = self.session.client, self.iid
        if ReasonDialog("Cancel dispatch", "Nothing has moved yet; the dispatch is closed with your reason.", "Cancel dispatch",
                        lambda r: c.post(f"/distributor-dispatches/{iid}/cancel", {"reason": r}), self).exec():
            self.changed = True; self.load()


class ReturnDetail(_Detail):
    def __init__(self, session: Session, return_id: int, parent=None):
        super().__init__(session, "Return", parent, (960, 560))
        self.rid, self.data = return_id, None
        self.table = DataTable([Column("sku", "SKU", 120), Column("variant_name", "Item"), Column("qty_requested", "Asked", 70, "right"), Column("qty_received", "Received", 80, "right"),
                                Column("qty_restock", "Saleable", 80, "right"), Column("qty_damaged", "Damaged", 80, "right"), Column("qty_expired", "Expired", 80, "right"),
                                Column("credit_qty", "Credit units", 90, "right"), Column("unit_price", "Original price", 110, "right", fmt=_price)])
        self.lay.addWidget(self.table, 1)
        self.totals = label("", "muted", wrap=True)
        self.lay.addWidget(self.totals)
        self.bar = QHBoxLayout(); self.lay.addLayout(self.bar)
        self.load()

    def load(self) -> None:
        c, rid = self.session.client, self.rid
        run_async(lambda: c.get(f"/distributor-returns/{rid}"), self._show, self.fail)

    def _show(self, d: dict) -> None:
        self.data = d
        self.head.setText(f"{d['ref']}  ·  {d['distributor']['name']}")
        self.sub.setText(f"{_cap(d['status'])}  ·  {d['reason']}  ·  requested by {d.get('requested_by')}")
        self.table.set_rows(d["lines"])
        self.totals.setText("Credit value " + (_money(d["credit_value"]) if d.get("credit_value") is not None else "not yet known (after inspection)")
                            + ". The account is credited only when the credit is approved.")
        while self.bar.count():
            w = self.bar.takeAt(0).widget()
            if w:
                w.deleteLater()
        s, st = self.session, d["status"]
        self.bar.addStretch()
        if s.can("distributors.transact"):
            if st == "requested":
                self.bar.addWidget(button("Receive goods", "primary", lambda: self._go(ReceiveReturnDialog(s, d, self))))
                self.bar.addWidget(button("Cancel return", None, self._cancel))
            if st == "received":
                self.bar.addWidget(button("Inspect", "primary", lambda: self._go(InspectDialog(s, d, self))))
        if st == "inspected" and s.can("distributors.approve"):
            self.bar.addWidget(button("Approve or reject credit…", "primary", self._decide))
        self.bar.addWidget(button("Close", None, self.accept))

    def _go(self, dlg: FormDialog) -> None:
        if dlg.exec():
            self.changed = True; self.status.show_message("Saved.", "ok"); self.load()

    def _decide(self) -> None:
        c, rid = self.session.client, self.rid
        if DecisionDialog("Decide on return credit", "Approving credits the distributor's account at the original dispatch price and applies it to that dispatch. "
                          "You cannot approve a return you requested, received or inspected.", lambda b: c.post(f"/distributor-returns/{rid}/decide", b), self).exec():
            self.changed = True; self.status.show_message("Decision recorded.", "ok"); self.load()

    def _cancel(self) -> None:
        c, rid = self.session.client, self.rid
        if ReasonDialog("Cancel return", "Only a return whose goods have not arrived can be cancelled.", "Cancel return",
                        lambda r: c.post(f"/distributor-returns/{rid}/cancel", {"reason": r}), self).exec():
            self.changed = True; self.load()


class PaymentDetail(_Detail):
    def __init__(self, session: Session, payment_id: int, parent=None):
        super().__init__(session, "Payment", parent, (720, 360))
        self.pid, self.data = payment_id, None
        self.body = label("", None, wrap=True)
        self.lay.addWidget(self.body); self.lay.addStretch()
        self.bar = QHBoxLayout(); self.lay.addLayout(self.bar)
        self.load()

    def load(self) -> None:
        c, pid = self.session.client, self.pid
        run_async(lambda: c.get(f"/distributor-payments/{pid}"), self._show, self.fail)

    def _show(self, d: dict) -> None:
        self.data = d
        self.head.setText(f"{d['ref']}  ·  {d['distributor']['name']}")
        self.sub.setText(f"{_cap(d['status'])}  ·  {_cap(d['method'])}  ·  received {d['paid_date']}  ·  recorded by {d.get('created_by')}")
        txt = f"Amount {_money(d['amount'])}   ·   applied to dispatches {_money(d['allocated'])}   ·   unallocated {_money(d['unallocated'])}"
        if d.get("reference"):
            txt += f"\nReference: {d['reference']}"
        if d["status"] == "pending":
            txt += f"\nWaiting for approval ({d.get('hold_reason') or 'back-dated'}). It does not change the balance until approved."
        if d["status"] == "reversed":
            txt += f"\nReversed by {d.get('reversed_by')}: {d.get('reverse_reason')}"
        self.body.setText(txt)
        while self.bar.count():
            w = self.bar.takeAt(0).widget()
            if w:
                w.deleteLater()
        s, st = self.session, d["status"]
        self.bar.addStretch()
        if st == "posted" and Decimal(d["unallocated"]) > 0 and s.can("distributors.receive_payment"):
            self.bar.addWidget(button("Allocate…", "primary", self._allocate))
        if st == "pending" and s.can("distributors.approve"):
            self.bar.addWidget(button("Approve or reject…", "primary", self._decide))
        if st == "posted" and s.can("distributors.approve"):
            self.bar.addWidget(button("Reverse payment", None, self._reverse))
        self.bar.addWidget(button("Close", None, self.accept))

    def _allocate(self) -> None:
        if AllocateDialog(self.session, self.data, self).exec():
            self.changed = True; self.status.show_message("Allocated.", "ok"); self.load()

    def _decide(self) -> None:
        c, pid = self.session.client, self.pid
        if DecisionDialog("Decide on back-dated payment", "You cannot approve a payment you recorded.", lambda b: c.post(f"/distributor-payments/{pid}/decide", b), self).exec():
            self.changed = True; self.status.show_message("Decision recorded.", "ok"); self.load()

    def _reverse(self) -> None:
        c, pid = self.session.client, self.pid
        if ReasonDialog("Reverse payment", "The payment is cancelled in the ledger with an opposite entry (the original stays on record) and its allocations are released, "
                        "so the dispatches become payable again.", "Reverse payment", lambda r: c.post(f"/distributor-payments/{pid}/reverse", {"reason": r}), self).exec():
            self.changed = True; self.load()


# ------------------------------------------------------------------------------------------------ tabs
class _ListTab(_Tab):
    """Shared list tab: distributor + status filters, a table, paging and a detail dialog."""
    path = ""
    states: dict = {}
    empty = "Nothing here yet."
    detail = None

    def __init__(self, session: Session, columns: list[Column], new_text: str | None = None, can_new: bool = False):
        super().__init__(session)
        bar = QHBoxLayout()
        self.dist = _dist_combo(session, with_all=True, active_only=False)
        self.state = QComboBox(); self.state.addItem("All states", None)
        for k in self.states:
            self.state.addItem(_cap(k), k)
        self.dist.currentIndexChanged.connect(lambda _: self._reset()); self.state.currentIndexChanged.connect(lambda _: self._reset())
        bar.addWidget(self.dist); bar.addWidget(self.state); bar.addStretch()
        if new_text and can_new:
            bar.addWidget(button(new_text, "primary", self._new))
        self.lay.addLayout(bar)
        self.table = DataTable(columns)
        self.table.row_activated.connect(self._open)
        self.panel = StatePanel(self.table, self.empty)
        self.panel.retry.connect(self.refresh)
        self.pager = Pager(50); self.pager.changed.connect(self.refresh)
        self.lay.addWidget(self.panel, 1); self.lay.addWidget(self.pager)

    def _reset(self) -> None:
        self.pager.reset(); self.refresh()

    def prepare(self, rows: list[dict]) -> None:
        for r in rows:
            r["distributor_name"] = r["distributor"]["name"]

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client

        def done(d):
            self.prepare(d["items"])
            self.table.set_rows(d["items"]); self.pager.update_total(d["total"])
            self.panel.show_content() if d["items"] else self.panel.show_empty("Nothing matches these filters." if (self.dist.currentData() or self.state.currentData()) else self.empty)
        run_async(lambda: c.get(self.path, distributor_id=self.dist.currentData(), status=self.state.currentData(), page=self.pager.page, page_size=50),
                  done, lambda e: self.panel.show_error(e.message, e.offline))

    def _new(self) -> None:
        pass

    def _open(self, row: dict) -> None:
        if self.detail is None:
            return
        d = self.detail(self.session, row["id"], self)
        d.exec()
        if d.changed:
            self.refresh()


class AccountsTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("Search name or code"); self.search.setMinimumWidth(260)
        self.search.returnPressed.connect(self.refresh)
        self.inactive = QCheckBox("Include inactive"); self.inactive.stateChanged.connect(lambda _: self.refresh())
        bar.addWidget(self.search); bar.addWidget(self.inactive); bar.addStretch()
        if session.can("distributors.manage"):
            bar.addWidget(button("New distributor", "primary", self._new))
        self.lay.addLayout(bar)
        self.table = DataTable([Column("code", "Code", 90), Column("name", "Distributor"), Column("city", "City", 100), Column("balance", "Owes us", 120, "right", fmt=_money),
                                Column("overdue", "Overdue", 110, "right", fmt=_money), Column("credit_limit", "Credit limit", 120, "right", fmt=_money),
                                Column("available_credit", "Available", 120, "right", fmt=_money), Column("payment_terms_days", "Terms (days)", 100, "right"),
                                Column("is_active", "State", 90, status=lambda v: "ok" if v else "neutral", fmt=lambda v: "Active" if v else "Inactive")])
        self.table.row_activated.connect(self._open)
        self.panel = StatePanel(self.table, "No distributors yet. Add the first one to start dispatching goods on credit.")
        self.panel.retry.connect(self.refresh)
        self.total = label("", "muted")
        self.lay.addWidget(self.panel, 1); self.lay.addWidget(self.total)
        self.lay.addWidget(label("Double-click a distributor for its account, aging, agreed prices and statement.", "muted"))

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client

        def done(d):
            self.table.set_rows(d["items"])
            owed = sum((Decimal(r["balance"]) for r in d["items"]), Decimal(0)); late = sum((Decimal(r["overdue"]) for r in d["items"]), Decimal(0))
            self.total.setText(f"Total owed by listed distributors {_money(owed)}   ·   overdue {_money(late)}")
            self.panel.show_content() if d["items"] else self.panel.show_empty("No distributors match." if self.search.text() else None)
        run_async(lambda: c.get("/distributors", q=self.search.text().strip() or None, active=None if self.inactive.isChecked() else "true", page_size=200),
                  done, lambda e: self.panel.show_error(e.message, e.offline))

    def _new(self) -> None:
        if DistributorDialog(self.session, parent=self).exec():
            self.status.show_message("Distributor saved. Set agreed prices and an opening balance from its account.", "ok"); self.refresh()

    def _open(self, row: dict) -> None:
        d = AccountDetail(self.session, row["id"], self)
        d.exec()
        if d.changed:
            self.refresh()


class DispatchesTab(_ListTab):
    path, states, empty, detail = "/distributor-dispatches", INV_STATUS, "No dispatches yet.", DispatchDetail

    def __init__(self, session: Session):
        names = loc_codes(session)
        self._names = names
        super().__init__(session, [Column("ref", "Document", 110), Column("distributor_name", "Distributor"), Column("kind", "Type", 150, fmt=lambda v: KIND_TEXT.get(v, v)),
                                   Column("location", "From", 110), Column("invoice_date", "Dated", 100), Column("due_date", "Due", 100),
                                   Column("total", "Total", 110, "right", fmt=_money), Column("outstanding", "Outstanding", 110, "right", fmt=_money),
                                   Column("status", "State", 110, status=lambda v: INV_STATUS.get(v, "neutral"), fmt=_cap)],
                         "New dispatch", session.can("distributors.transact"))

    def prepare(self, rows):
        super().prepare(rows)
        for r in rows:
            r["location"] = self._names.get(r.get("location_id"), "-")

    def _new(self) -> None:
        d = DispatchDialog(self.session, parent=self)
        if d.exec():
            held = bool(d.created and d.created.get("status") == "held")
            self.status.show_message("Dispatch is waiting for approval: nothing has moved." if held else "Dispatch saved.", "info" if held else "ok"); self.refresh()


class ReturnsTab(_ListTab):
    path, states, empty, detail = "/distributor-returns", RET_STATUS, "No returns yet.", ReturnDetail

    def __init__(self, session: Session):
        super().__init__(session, [Column("ref", "Return", 110), Column("distributor_name", "Distributor"), Column("return_date", "Requested", 100), Column("reason", "Reason"),
                                   Column("credit_value", "Credit", 110, "right", fmt=_money),
                                   Column("status", "State", 110, status=lambda v: RET_STATUS.get(v, "neutral"), fmt=_cap)],
                         "Request return", session.can("distributors.transact"))

    def _new(self) -> None:
        if ReturnDialog(self.session, parent=self).exec():
            self.status.show_message("Return requested.", "ok"); self.refresh()


class PaymentsTab(_ListTab):
    path, states, empty, detail = "/distributor-payments", PAY_STATUS, "No payments recorded yet.", PaymentDetail

    def __init__(self, session: Session):
        super().__init__(session, [Column("ref", "Receipt", 110), Column("distributor_name", "Distributor"), Column("paid_date", "Received", 100),
                                   Column("method", "Method", 110, fmt=_cap), Column("reference", "Reference", 140), Column("amount", "Amount", 110, "right", fmt=_money),
                                   Column("unallocated", "Unallocated", 110, "right", fmt=_money),
                                   Column("status", "State", 100, status=lambda v: PAY_STATUS.get(v, "neutral"), fmt=_cap)],
                         "Record payment", session.can("distributors.receive_payment"))

    def _new(self) -> None:
        if PaymentDialog(self.session, parent=self).exec():
            self.status.show_message("Payment recorded.", "ok"); self.refresh()


class AdjustmentsTab(_ListTab):
    path, states, empty = "/distributor-adjustments", ADJ_STATUS, "No adjustments requested."

    def __init__(self, session: Session):
        super().__init__(session, [Column("ref", "Request", 110), Column("distributor_name", "Distributor"), Column("kind", "Adjustment", 120, fmt=_cap),
                                   Column("entry_date", "Dated", 100), Column("amount", "Amount", 110, "right", fmt=_money), Column("reason", "Reason"),
                                   Column("requested_by", "Requested by", 120),
                                   Column("status", "State", 100, status=lambda v: ADJ_STATUS.get(v, "neutral"), fmt=_cap)],
                         "Request adjustment", session.can("distributors.transact"))
        self.lay.addWidget(label("Double-click a pending request to approve or reject it. You cannot approve your own request.", "muted"))

    def _new(self) -> None:
        if AdjustmentDialog(self.session, parent=self).exec():
            self.status.show_message("Adjustment requested. It changes nothing until someone else approves it.", "ok"); self.refresh()

    def _open(self, row: dict) -> None:
        if row["status"] != "pending" or not self.session.can("distributors.approve"):
            self.status.show_message(f"{row['ref']}: {_cap(row['status'])}" + (f". {row['decided_by']}: {row.get('decision_note')}" if row.get("decided_by") else ""), "info"); return
        c = self.session.client
        if DecisionDialog(f"Decide on {row['ref']}", f"{_cap(row['kind'])} of {_money(row['amount'])} for {row['distributor']['name']}: {row['reason']}",
                          lambda b: c.post(f"/distributor-adjustments/{row['id']}/decide", b), self).exec():
            self.status.show_message("Decision recorded.", "ok"); self.refresh()


class AgingTab(_Tab):
    def __init__(self, session: Session):
        super().__init__(session)
        self.lay.addWidget(label("What distributors owe, by how far past its due date each open dispatch is", "fieldLabel"))
        self.table = DataTable([Column("name", "Distributor"), Column("balance", "Owes us", 120, "right", fmt=_money)]
                               + [Column(k, t, 110, "right", fmt=_money) for k, t in BUCKETS]
                               + [Column("unallocated_credit", "Unallocated credit", 140, "right", fmt=_money)])
        self.panel = StatePanel(self.table, "No distributor owes anything.")
        self.panel.retry.connect(self.refresh)
        self.total = label("", "muted")
        self.lay.addWidget(self.panel, 1); self.lay.addWidget(self.total)

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client

        def load():
            rows = []
            for d in c.get("/distributors", active=None, page_size=200)["items"]:
                a = c.get(f"/distributors/{d['id']}/aging")
                if Decimal(a["balance"]) != 0 or Decimal(a["outstanding_invoices"]) != 0:
                    rows.append({"name": d["name"], "balance": a["balance"], "unallocated_credit": a["unallocated_credit"], **a["buckets"]})
            return rows

        def done(rows):
            self.table.set_rows(rows)
            late = sum((Decimal(r[k]) for r in rows for k, _ in BUCKETS if k != "current"), Decimal(0))
            self.total.setText(f"Total owed {_money(sum((Decimal(r['balance']) for r in rows), Decimal(0)))}   ·   past due {_money(late)}")
            self.panel.show_content() if rows else self.panel.show_empty()
        run_async(load, done, lambda e: self.panel.show_error(e.message, e.offline))


class DistributorsPage(Page):
    title = "Distributors"

    def __init__(self, session: Session):
        super().__init__(session)
        self.root.addWidget(PageHeader("Distributors", "Goods dispatched on credit, returns and exchanges, payments and statements. The amount owed is always calculated from posted "
                                       "entries, never typed in. Accounting journals and reports arrive in later phases."))
        self.tabs = QTabWidget()
        self.root.addWidget(self.tabs, 1)
        items: list[tuple[str, _Tab]] = [("Accounts", AccountsTab(session)), ("Dispatches", DispatchesTab(session)), ("Returns", ReturnsTab(session)),
                                         ("Payments", PaymentsTab(session)), ("Adjustments", AdjustmentsTab(session)), ("Aging", AgingTab(session))]
        self.items = items
        for t, w in items:
            self.tabs.addTab(w, t)
        self.tabs.currentChanged.connect(lambda _: self.refresh())

    def refresh(self) -> None:
        w = self.tabs.currentWidget()
        if isinstance(w, _Tab):
            w.refresh()
