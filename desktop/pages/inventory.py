"""Inventory screens: stock, movements, receiving, counts, adjustments and approvals, alerts, opening stock."""
from __future__ import annotations

from datetime import date, datetime

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QHBoxLayout, QLineEdit, QPlainTextEdit, QSpinBox, QTabWidget,
    QVBoxLayout, QWidget,
)

from desktop.pages.base import Page
from desktop.session import Session
from desktop.ui.forms import FormDialog, FormGrid
from desktop.ui.lines import LinesEditor
from desktop.ui.widgets import Banner, Column, DataTable, PageHeader, Pager, StatePanel, button, format_money, label
from desktop.ui.worker import run_async

BUCKETS = [("Saleable", "good"), ("Damaged", "damaged"), ("Expired", "expired"), ("Awaiting inspection", "inspection")]


def _when(v):
    if not v:
        return "-"
    try:
        return datetime.fromisoformat(v).astimezone().strftime("%d %b %Y %H:%M")
    except ValueError:
        return v


def _money(v):
    """Dense tables show the bare amount; the column header carries the currency."""
    return f"{float(v):,.2f}" if v not in (None, "") else "-"


def loc_combo(session: Session, required: bool = True) -> QComboBox:
    cb = QComboBox()
    if required and len(session.locations) != 1:
        cb.addItem("Select a location…", None)
    for l in session.locations:
        cb.addItem(f"{l['name']}  ({l['city']})", l["id"])
    if session.current_location_id is not None:
        cb.setCurrentIndex(max(cb.findData(session.current_location_id), 0))
    return cb


def loc_names(session: Session) -> dict[int, str]:
    return {l["id"]: l["name"] for l in session.locations}


def loc_codes(session: Session) -> dict[int, str]:
    """Short codes keep dense tables readable; the full name is in the location selector and tooltips."""
    return {l["id"]: l["code"] for l in session.locations}


# --------------------------------------------------------------------------------------- dialogs
class ReceiptDialog(FormDialog):
    def __init__(self, session: Session, parent=None):
        super().__init__("Receive goods", "Post receipt", parent, 940)
        self.session = session
        g = FormGrid(3)
        self.loc = loc_combo(session)
        self.date = QDateEdit(QDate.currentDate()); self.date.setCalendarPopup(True); self.date.setDisplayFormat("dd MMM yyyy")
        self.po = QLineEdit(); self.po.setPlaceholderText("Purchase order / reference")
        self.supplier = QLineEdit(); self.supplier.setPlaceholderText("Supplier (free text until Phase 3)")
        self.notes = QLineEdit()
        g.add("loc", "Receiving location", self.loc)
        g.add("date", "Received on", self.date)
        g.add("po", "Purchase reference", self.po)
        g.add("supplier", "Supplier", self.supplier)
        g.add("notes", "Notes", self.notes, span=2)
        self.form = g
        self.body.addWidget(g)
        self.body.addWidget(label("Only the accepted quantity becomes saleable stock. Damaged units are held separately; rejected units are recorded but never enter stock.", "muted", wrap=True))
        self.editor = LinesEditor(session, [
            ("batch_no", "Batch", "text"), ("expiry_date", "Expiry", "date"), ("qty_expected", "Expected", "int"),
            ("qty_accepted", "Accepted", "int"), ("qty_damaged", "Damaged", "int"), ("qty_rejected", "Rejected", "int"),
            ("unit_cost", "Unit cost", "money")],
            [Column("batch_no", "Batch", 90), Column("expiry_date", "Expiry", 100), Column("qty_expected", "Expected", 80, "right"),
             Column("qty_accepted", "Accepted", 80, "right"), Column("qty_damaged", "Damaged", 80, "right"),
             Column("qty_rejected", "Rejected", 80, "right"), Column("unit_cost", "Unit cost", 90, "right")])
        self.body.addWidget(self.editor)
        self.draft = QCheckBox("Save as a draft without adding stock yet")
        self.body.addWidget(self.draft)

    def submit(self) -> None:
        self.form.clear_errors()
        if self.loc.currentData() is None:
            self.form.set_error("loc", "Choose the receiving location."); return self.reenable()
        if not self.editor.lines:
            self.editor.banner.show_message("Add at least one line.", "error"); return self.reenable()
        lines = []
        for l in self.editor.payload():
            acc, dmg, rej = l["qty_accepted"], l["qty_damaged"], l["qty_rejected"]
            if not l.get("unit_cost"):
                self.editor.banner.show_message("Every line needs a unit cost.", "error"); return self.reenable()
            lines.append({"variant_id": l["variant_id"], "batch_no": l["batch_no"], "expiry_date": l["expiry_date"],
                          "qty_expected": l["qty_expected"] or None, "qty_received": acc + dmg + rej, "qty_accepted": acc,
                          "qty_damaged": dmg, "qty_rejected": rej, "unit_cost": l["unit_cost"]})
        body = {"location_id": self.loc.currentData(), "received_date": self.date.date().toString("yyyy-MM-dd"),
                "purchase_reference": self.po.text().strip() or None, "supplier_reference": self.supplier.text().strip() or None,
                "notes": self.notes.text().strip() or None, "lines": lines, "post_now": not self.draft.isChecked()}
        c = self.session.client
        run_async(lambda: c.post("/inventory/receipts", body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class MoveDialog(FormDialog):
    ACTIONS = [("Mark as damaged", "damage"), ("Mark as expired", "expiry"),
               ("Inspection result: return to saleable", "inspection_restock"),
               ("Inspection result: damaged", "inspection_damaged"), ("Inspection result: expired", "inspection_expired")]

    def __init__(self, session: Session, parent=None):
        super().__init__("Damaged, expired or returned stock", "Record", parent, 860)
        self.session = session
        g = FormGrid(2)
        self.loc = loc_combo(session)
        self.action = QComboBox()
        for t, v in self.ACTIONS:
            self.action.addItem(t, v)
        self.reason = QLineEdit(); self.reason.setPlaceholderText("What happened (required)")
        g.add("loc", "Location", self.loc); g.add("action", "What happened", self.action)
        g.add("reason", "Reason", self.reason, span=2)
        self.form = g
        self.body.addWidget(g)
        self.body.addWidget(label("Stock is moved out of saleable stock at its current average cost. Nothing is deleted; "
                                  "removing it from the books needs a separate write-off that another person approves.", "muted", wrap=True))
        self.editor = LinesEditor(session, [("batch_id", "Batch", "batch"), ("qty", "Quantity", "int")],
                                  [Column("batch_id_label", "Batch", 140), Column("qty", "Qty", 80, "right")])
        self.body.addWidget(self.editor)

    def submit(self) -> None:
        self.form.clear_errors()
        if self.loc.currentData() is None:
            self.form.set_error("loc", "Choose the location."); return self.reenable()
        if not self.reason.text().strip():
            self.form.set_error("reason", "Give a reason."); return self.reenable()
        if not self.editor.lines:
            self.editor.banner.show_message("Add at least one line.", "error"); return self.reenable()
        c, loc, act, why = self.session.client, self.loc.currentData(), self.action.currentData(), self.reason.text().strip()
        lines = self.editor.payload()

        def work():
            for l in lines:
                if l["qty"] <= 0:
                    raise ValueError
                c.post("/inventory/moves", {"action": act, "location_id": loc, "variant_id": l["variant_id"],
                                            "batch_id": l.get("batch_id"), "qty": l["qty"], "reason": why})

        run_async(work, lambda _: self.finish_ok(), lambda e: self.finish_error(
            e.message + " (Lines already recorded stay recorded; check Movements before retrying.)"))


class RequestDialog(FormDialog):
    def __init__(self, session: Session, parent=None):
        super().__init__("Stock adjustment or write-off", "Submit for approval", parent, 880)
        self.session = session
        g = FormGrid(3)
        self.loc = loc_combo(session)
        self.kind = QComboBox(); self.kind.addItem("Adjustment (correct a quantity)", "adjustment"); self.kind.addItem("Write-off (remove stock from the books)", "writeoff")
        self.reason = QLineEdit()
        g.add("loc", "Location", self.loc); g.add("kind", "Type", self.kind); g.add("reason", "Reason", self.reason)
        self.form = g
        self.body.addWidget(g)
        self.body.addWidget(label("Use a negative quantity to remove stock. A different authorised person must approve before anything changes. "
                                  "A unit cost is only needed when adding stock at a place where no cost is known yet.", "muted", wrap=True))
        self.editor = LinesEditor(session, [("bucket", "Stock type", "bucket", BUCKETS), ("batch_id", "Batch", "batch"),
                                            ("qty_delta", "Change (+/-)", "delta"), ("unit_cost", "Unit cost", "money")],
                                  [Column("bucket", "Type", 100), Column("batch_id_label", "Batch", 120),
                                   Column("qty_delta", "Change", 80, "right"), Column("unit_cost", "Unit cost", 90, "right")])
        self.body.addWidget(self.editor)

    def submit(self) -> None:
        self.form.clear_errors()
        if self.loc.currentData() is None:
            self.form.set_error("loc", "Choose the location."); return self.reenable()
        if not self.reason.text().strip():
            self.form.set_error("reason", "Give a reason."); return self.reenable()
        lines = [l for l in self.editor.payload()]
        if not lines or any(l["qty_delta"] == 0 for l in lines):
            self.editor.banner.show_message("Add lines with a non-zero change.", "error"); return self.reenable()
        body = {"kind": self.kind.currentData(), "location_id": self.loc.currentData(), "reason": self.reason.text().strip(),
                "lines": [{"variant_id": l["variant_id"], "batch_id": l.get("batch_id"), "bucket": l["bucket"],
                           "qty_delta": l["qty_delta"], "unit_cost": l.get("unit_cost")} for l in lines]}
        c = self.session.client
        self.result_data = None

        def done(r):
            self.result_data = r
            self.finish_ok()

        run_async(lambda: c.post("/inventory/requests", body), done, lambda e: self.finish_error(e.message))


class OpeningStockDialog(FormDialog):
    def __init__(self, session: Session, parent=None):
        super().__init__("Enter opening stock", "Check lines", parent, 900)
        self.session = session
        self.checked = False
        self.body.addWidget(label("Enter only verified counted quantities and verified costs. Nothing is guessed or filled in for you. "
                                  "First check the lines; stock is written only after a clean check and your confirmation, all together or not at all.",
                                  "muted", wrap=True))
        self.loc = loc_combo(session)
        g = FormGrid(2); g.add("loc", "Location", self.loc); self.form = g; self.body.addWidget(g)
        self.editor = LinesEditor(session, [("batch_no", "Batch", "text"), ("expiry_date", "Expiry", "date"),
                                            ("qty", "Counted qty", "int"), ("unit_cost", "Verified unit cost", "money")],
                                  [Column("batch_no", "Batch", 90), Column("expiry_date", "Expiry", 100),
                                   Column("qty", "Qty", 80, "right"), Column("unit_cost", "Unit cost", 100, "right")])
        self.body.addWidget(self.editor)
        self.result_lbl = label("", "muted", wrap=True)
        self.body.addWidget(self.result_lbl)

    def submit(self) -> None:
        self.form.clear_errors()
        if self.loc.currentData() is None:
            self.form.set_error("loc", "Choose the location."); return self.reenable()
        lines = [{"location_id": self.loc.currentData(), "variant_id": l["variant_id"], "batch_no": l.get("batch_no"),
                  "expiry_date": l.get("expiry_date"), "qty": l["qty"], "unit_cost": l.get("unit_cost") or "0"}
                 for l in self.editor.payload() if l["qty"] > 0 and l.get("unit_cost")]
        if not lines or len(lines) != len(self.editor.lines):
            self.editor.banner.show_message("Every line needs a quantity above zero and a verified unit cost.", "error"); return self.reenable()
        c, dry = self.session.client, not self.checked
        import uuid
        key = getattr(self, "_key", None) or uuid.uuid4().hex
        self._key = key

        def done(r):
            if not r["ok"]:
                self.checked = False
                self.submit_btn.setText("Check lines")
                msg = "; ".join(f"line {e['line']}: {e['message']}" for e in r["errors"][:6])
                self.finish_error("Nothing was saved. " + msg)
                return
            if dry:
                self.checked = True
                self.submit_btn.setText("Confirm and enter stock")
                self.result_lbl.setText(f"{r['lines']} line(s) check out. Press Confirm to write them to the stock ledger.")
                self.reenable()
            else:
                self.finish_ok()

        run_async(lambda: c.post("/inventory/opening-stock", {"lines": lines, "dry_run": dry, "idempotency_key": key}),
                  done, lambda e: self.finish_error(e.message))


class CountDialog(QDialog):
    def __init__(self, session: Session, count_id: int, parent=None):
        super().__init__(parent)
        self.session, self.count_id = session, count_id
        self.setWindowTitle("Stock count")
        self.setMinimumSize(900, 540)
        lay = QVBoxLayout(self); lay.setContentsMargins(20, 16, 20, 14)
        self.title = label("", "sectionTitle"); lay.addWidget(self.title)
        lay.addWidget(label("Count what is physically on the shelf and enter it. Saleable stock only. Lines with a difference need a reason. "
                            "Differences go to approval; stock changes only after another person approves.", "muted", wrap=True))
        self.banner = Banner(); lay.addWidget(self.banner)
        self.table = DataTable([Column("sku", "SKU", 128), Column("variant_name", "Item"), Column("batch_no", "Batch", 90),
                                Column("expected_qty", "Expected", 80, "right"), Column("moved_since_snapshot", "Moved since", 90, "right"),
                                Column("counted_qty", "Counted", 80, "right"), Column("variance", "Difference", 90, "right"),
                                Column("reason", "Reason", 200)])
        self.table.row_activated.connect(self._edit)
        lay.addWidget(self.table, 1)
        lay.addWidget(label("Double-click a line to enter the counted quantity and a reason.", "muted"))
        row = QHBoxLayout(); row.addStretch()
        self.cancel_count = button("Cancel this count", "danger", self._cancel)
        self.submit_btn = button("Submit count", "primary", self._submit)
        row.addWidget(self.cancel_count); row.addWidget(button("Close", on_click=self.accept)); row.addWidget(self.submit_btn)
        lay.addLayout(row)
        self.count = {}
        self.load()

    def load(self) -> None:
        c = self.session.client
        run_async(lambda: c.get(f"/inventory/counts/{self.count_id}"), self._loaded, lambda e: self.banner.show_message(e.message, "error"))

    def _loaded(self, d: dict) -> None:
        self.count = d
        self.title.setText(f"{d['ref']}  ·  {d['status'].replace('_', ' ')}")
        self.table.set_rows(d["lines"])
        open_ = d["status"] == "in_progress"
        self.submit_btn.setVisible(open_); self.cancel_count.setVisible(open_)

    def _edit(self, row: dict) -> None:
        if self.count.get("status") != "in_progress":
            return
        dlg = FormDialog(f"Count {row['sku']}", "Save line", self, 380)
        qty = QSpinBox(); qty.setRange(0, 1_000_000); qty.setValue(row["counted_qty"] if row["counted_qty"] is not None else row["expected_qty"])
        why = QLineEdit(row.get("reason") or "")
        g = FormGrid(1); g.add("qty", f"Counted quantity (expected {row['expected_qty']})", qty); g.add("why", "Reason for any difference", why)
        dlg.body.addWidget(g)
        c = self.session.client
        dlg.submit = lambda: run_async(  # type: ignore[method-assign]
            lambda: c.put(f"/inventory/counts/{self.count_id}/entries", {"entries": [{"line_id": row["id"], "counted_qty": qty.value(), "reason": why.text().strip() or None}]}),
            lambda _: dlg.finish_ok(), lambda e: dlg.finish_error(e.message))
        if dlg.exec():
            self.load()

    def _submit(self) -> None:
        c = self.session.client
        run_async(lambda: c.post(f"/inventory/counts/{self.count_id}/submit"),
                  lambda r: (self.banner.show_message("Count submitted." + (f" Differences sent for approval as {r['request_ref']}." if r.get("request_ref") else " No differences."), "ok"), self.load()),
                  lambda e: self.banner.show_message(e.message, "error"))

    def _cancel(self) -> None:
        c = self.session.client
        run_async(lambda: c.post(f"/inventory/counts/{self.count_id}/cancel"), lambda _: self.load(),
                  lambda e: self.banner.show_message(e.message, "error"))


class DecisionDialog(FormDialog):
    def __init__(self, session: Session, req: dict, approve: bool, parent=None):
        super().__init__(("Approve " if approve else "Reject ") + req["ref"], "Approve" if approve else "Reject", parent, 480)
        self.session, self.req, self.approve = session, req, approve
        self.note = QLineEdit(); self.note.setPlaceholderText("Note" + ("" if approve else " (required)"))
        self.body.addWidget(label(f"{req['kind'].replace('_', ' ').capitalize()}: {req['reason']}", "muted", wrap=True))
        self.body.addWidget(self.note)

    def submit(self) -> None:
        c, rid = self.session.client, self.req["id"]
        path = f"/inventory/requests/{rid}/" + ("approve" if self.approve else "reject")
        body = {"note": self.note.text().strip() or None} if self.approve else {"note": self.note.text().strip()}
        if not self.approve and not body["note"]:
            self.finish_error("Say why you are rejecting this."); return
        run_async(lambda: c.post(path, body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


# --------------------------------------------------------------------------------------- tabs
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


class StockTab(_Tab):
    def __init__(self, session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("Search SKU, barcode or name"); self.search.setMinimumWidth(300)
        self.search.returnPressed.connect(self._reload)
        self.low = QCheckBox("Low stock only"); self.low.toggled.connect(self._reload)
        self.issues = QCheckBox("Damaged / expired / inspection"); self.issues.toggled.connect(self._reload)
        self.zero = QCheckBox("Include zero stock"); self.zero.toggled.connect(self._reload)
        for w in (self.search, self.low, self.issues, self.zero):
            bar.addWidget(w)
        bar.addStretch()
        self.lay.addLayout(bar)
        cols = [Column("sku", "SKU", 128), Column("item", "Item"),
                Column("location", "Location", 104), Column("on_hand_good", "On hand", 72, "right"),
                Column("reserved", "Reserved", 76, "right"), Column("available", "Available", 76, "right"),
                Column("damaged", "Damaged", 76, "right"), Column("expired", "Expired", 62, "right"),
                Column("inspection", "Inspect", 58, "right"), Column("in_transit", "Transit", 60, "right")]
        self.has_cost = session.can("inventory.view_valuation")
        if self.has_cost:
            cols += [Column("avg_cost", "Avg (PKR)", 92, "right", fmt=_money), Column("value", "Value (PKR)", 112, "right", fmt=_money)]
        cols.append(Column("is_low", "Alert", 52, status=lambda v: "warn" if v else "neutral", fmt=lambda v: "Low" if v else ""))
        self.table = DataTable(cols)
        self.panel = StatePanel(self.table, "No stock has been recorded yet. Receive goods or enter verified opening stock to begin.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)
        self.total = label("", "muted")
        self.lay.addWidget(self.total)
        self.pager = Pager(100); self.pager.changed.connect(self.refresh)
        self.lay.addWidget(self.pager)

    def _reload(self) -> None:
        self.pager.reset(); self.refresh()

    def refresh(self) -> None:
        self.panel.show_loading()
        c, s = self.session.client, self.session
        names = loc_codes(s)
        params = dict(location_id=s.current_location_id, q=self.search.text().strip() or None,
                      low_only="true" if self.low.isChecked() else None, issues_only="true" if self.issues.isChecked() else None,
                      include_zero="true" if self.zero.isChecked() else None, page=self.pager.page, page_size=self.pager.page_size)

        def done(d):
            for r in d["items"]:
                r["location"] = names.get(r["location_id"], f"#{r['location_id']}")
                r["item"] = f"{r['product_name']} \u00b7 {r['variant_name']}"
            self.table.set_rows(d["items"])
            self.pager.update_total(d["total"])
            self.total.setText("Available = saleable on hand minus reserved. Damaged, expired, in-transit and awaiting-inspection stock is never counted as available.")
            if d["items"]:
                self.panel.show_content()
            else:
                self.panel.show_empty()

        run_async(lambda: c.get("/inventory/stock", **params), done, lambda e: self.panel.show_error(e.message, e.offline))


class MovementsTab(_Tab):
    def __init__(self, session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.kind = QComboBox(); self.kind.addItem("All movement types", None)
        for k in ("receipt", "opening_stock", "adjustment", "count_variance", "writeoff", "damage", "expiry", "return_disposition",
                  "transfer_out", "transfer_transit", "transfer_in", "transfer_shortage", "reversal"):
            self.kind.addItem(k.replace("_", " ").capitalize(), k)
        self.kind.currentIndexChanged.connect(self._reload)
        bar.addWidget(self.kind); bar.addStretch()
        self.reverse_btn = button("Reverse selected…", on_click=self._reverse)
        self.reverse_btn.setVisible(session.can("inventory.reverse"))
        bar.addWidget(self.reverse_btn)
        self.lay.addLayout(bar)
        cols = [Column("created_at", "When", 140, fmt=_when), Column("ref", "Ref", 108),
                Column("type_label", "Type", 150), Column("sku", "SKU", 128),
                Column("location", "Location", 104), Column("batch_no", "Batch", 72), Column("bucket", "Bucket", 74),
                Column("qty_delta", "Qty", 52, "right")]
        if session.can("inventory.view_valuation"):
            cols.append(Column("value_delta", "Value (PKR)", 108, "right", fmt=_money))
        cols += [Column("reason", "Reason"), Column("user", "By", 100)]
        self.table = DataTable(cols)
        self.panel = StatePanel(self.table, "No stock movements yet.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)
        self.pager = Pager(100); self.pager.changed.connect(self.refresh)
        self.lay.addWidget(self.pager)

    def _reload(self) -> None:
        self.pager.reset(); self.refresh()

    def refresh(self) -> None:
        self.panel.show_loading()
        c, s = self.session.client, self.session
        names = loc_codes(s)

        def done(d):
            for r in d["items"]:
                r["location"] = names.get(r["location_id"], f"#{r['location_id']}")
                r["type_label"] = str(r["txn_type"]).replace("_", " ").capitalize() + (" (reversed)" if r["txn_status"] == "reversed" else "")
            self.table.set_rows(d["items"]); self.pager.update_total(d["total"])
            self.panel.show_content() if d["items"] else self.panel.show_empty()

        run_async(lambda: c.get("/inventory/movements", location_id=s.current_location_id, txn_type=self.kind.currentData(),
                                page=self.pager.page, page_size=self.pager.page_size), done,
                  lambda e: self.panel.show_error(e.message, e.offline))

    def _reverse(self) -> None:
        row = self.table.selected_row()
        if not row:
            return
        dlg = FormDialog(f"Reverse {row['ref']}", "Reverse transaction", self, 480)
        why = QLineEdit(); why.setPlaceholderText("Reason (required)")
        dlg.body.addWidget(label("Reversal posts a new opposite transaction; the original stays in the history. "
                                 "You cannot reverse something you posted yourself, and it is refused if the stock has since been used.", "muted", wrap=True))
        dlg.body.addWidget(why)
        c = self.session.client

        def go():
            if not why.text().strip():
                dlg.finish_error("Give a reason."); return
            run_async(lambda: c.post(f"/inventory/transactions/{row['txn_id']}/reverse", {"reason": why.text().strip()}),
                      lambda _: dlg.finish_ok(), lambda e: dlg.finish_error(e.message))

        dlg.submit = go  # type: ignore[method-assign]
        if dlg.exec():
            self.status.show_message("Transaction reversed.", "ok"); self.refresh()


class ReceivingTab(_Tab):
    def __init__(self, session):
        super().__init__(session)
        bar = QHBoxLayout(); bar.addStretch()
        self.post_btn = button("Post selected draft", on_click=self._post)
        self.new_btn = button("Receive goods", "primary", self._new)
        for b in (self.post_btn, self.new_btn):
            b.setVisible(session.can("inventory.receive")); bar.addWidget(b)
        self.lay.addLayout(bar)
        cols = [Column("ref", "Receipt", 110), Column("received_date", "Received", 100), Column("location", "Location", 104),
                Column("purchase_reference", "Purchase ref", 130), Column("supplier_reference", "Supplier"),
                Column("accepted", "Accepted", 80, "right"), Column("damaged", "Damaged", 80, "right"), Column("rejected", "Rejected", 80, "right"),
                Column("created_by", "Entered by", 100),
                Column("status", "State", 100, status=lambda v: {"posted": "ok", "draft": "warn"}.get(v, "neutral"), fmt=lambda v: str(v).capitalize())]
        self.table = DataTable(cols)
        self.panel = StatePanel(self.table, "No goods receipts yet.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)

    def refresh(self) -> None:
        self.panel.show_loading()
        c, s = self.session.client, self.session
        names = loc_codes(s)

        def done(d):
            for r in d["items"]:
                r["location"] = names.get(r["location_id"], f"#{r['location_id']}")
                r.update(accepted=r["totals"]["accepted"], damaged=r["totals"]["damaged"], rejected=r["totals"]["rejected"])
            self.table.set_rows(d["items"])
            self.panel.show_content() if d["items"] else self.panel.show_empty()

        run_async(lambda: c.get("/inventory/receipts", location_id=s.current_location_id, page_size=100), done,
                  lambda e: self.panel.show_error(e.message, e.offline))

    def _new(self) -> None:
        if ReceiptDialog(self.session, self).exec():
            self.status.show_message("Receipt saved.", "ok"); self.refresh()

    def _post(self) -> None:
        row = self.table.selected_row()
        if not row or row["status"] != "draft":
            self.status.show_message("Select a draft receipt first.", "info"); return
        c = self.session.client
        run_async(lambda: c.post(f"/inventory/receipts/{row['id']}/post"),
                  lambda _: (self.status.show_message("Receipt posted. Stock updated.", "ok"), self.refresh()),
                  lambda e: self.status.show_message(e.message, "error"))


class CountsTab(_Tab):
    def __init__(self, session):
        super().__init__(session)
        bar = QHBoxLayout(); bar.addStretch()
        self.open_btn = button("Open selected", on_click=self._open)
        self.new_btn = button("Start a count", "primary", self._new)
        for b in (self.open_btn, self.new_btn):
            b.setVisible(session.can("inventory.count")); bar.addWidget(b)
        self.lay.addLayout(bar)
        self.table = DataTable([Column("ref", "Count", 110), Column("location", "Location", 120), Column("lines", "Lines", 70, "right"),
                                Column("created_at", "Started", 150, fmt=_when),
                                Column("status", "State", 120, status=lambda v: {"in_progress": "warn", "submitted": "ok"}.get(v, "neutral"),
                                       fmt=lambda v: str(v).replace("_", " ").capitalize())])
        self.table.row_activated.connect(lambda _: self._open())
        self.panel = StatePanel(self.table, "No stock counts yet.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)

    def refresh(self) -> None:
        if not self.session.can("inventory.count"):
            self.panel.show_empty("You do not have permission to run stock counts."); return
        self.panel.show_loading()
        c, s = self.session.client, self.session
        names = loc_codes(s)

        def done(rows):
            for r in rows:
                r["location"] = names.get(r["location_id"], f"#{r['location_id']}")
            self.table.set_rows(rows)
            self.panel.show_content() if rows else self.panel.show_empty()

        run_async(lambda: c.get("/inventory/counts", location_id=s.current_location_id), done, lambda e: self.panel.show_error(e.message, e.offline))

    def _new(self) -> None:
        dlg = FormDialog("Start a stock count", "Start count", self, 420)
        loc = loc_combo(self.session)
        g = FormGrid(1); g.add("loc", "Location", loc); dlg.body.addWidget(g)
        dlg.body.addWidget(label("Takes a snapshot of saleable stock at this location. Sales or receipts during the count are accounted for.", "muted", wrap=True))
        c = self.session.client
        result = {}

        def go():
            if loc.currentData() is None:
                g.set_error("loc", "Choose a location."); dlg.reenable(); return
            run_async(lambda: c.post("/inventory/counts", {"location_id": loc.currentData()}),
                      lambda r: (result.update(r), dlg.finish_ok()), lambda e: dlg.finish_error(e.message))

        dlg.submit = go  # type: ignore[method-assign]
        if dlg.exec():
            CountDialog(self.session, result["id"], self).exec(); self.refresh()

    def _open(self) -> None:
        row = self.table.selected_row()
        if row:
            CountDialog(self.session, row["id"], self).exec(); self.refresh()


class ApprovalsTab(_Tab):
    def __init__(self, session):
        super().__init__(session)
        bar = QHBoxLayout()
        self.filter = QComboBox()
        for t, v in (("Waiting for approval", "pending"), ("Approved", "approved"), ("Rejected", "rejected"), ("All", None)):
            self.filter.addItem(t, v)
        self.filter.currentIndexChanged.connect(self.refresh)
        bar.addWidget(self.filter); bar.addStretch()
        self.reject_btn = button("Reject…", on_click=lambda: self._decide(False))
        self.approve_btn = button("Approve…", "primary", lambda: self._decide(True))
        self.new_btn = button("New adjustment / write-off", on_click=self._new)
        self.open_btn = button("Enter opening stock", on_click=self._opening)
        self.move_btn = button("Damaged / expired / returned", on_click=self._move)
        self.new_btn.setVisible(session.can("inventory.adjust")); self.move_btn.setVisible(session.can("inventory.adjust"))
        self.open_btn.setVisible(session.can("inventory.opening_stock"))
        can_decide = session.can("inventory.adjust_approve", "inventory.writeoff_approve")
        self.approve_btn.setVisible(can_decide); self.reject_btn.setVisible(can_decide)
        for b in (self.move_btn, self.open_btn, self.new_btn, self.reject_btn, self.approve_btn):
            bar.addWidget(b)
        self.lay.addLayout(bar)
        cols = [Column("ref", "Request", 112), Column("kind", "Type", 112, fmt=lambda v: str(v).replace("_", " ").capitalize()),
                Column("location", "Location", 104), Column("summary", "Items"), Column("reason", "Reason", 190),
                Column("requested_by", "Requested by", 108), Column("requested_at", "When", 140, fmt=_when)]
        if session.can("inventory.view_valuation"):
            cols.append(Column("estimated_value", "Est. value (PKR)", 120, "right", fmt=_money))
        cols.append(Column("status", "State", 92, status=lambda v: {"pending": "warn", "approved": "ok"}.get(v, "neutral"), fmt=lambda v: str(v).capitalize()))
        self.table = DataTable(cols)
        self.panel = StatePanel(self.table, "Nothing here.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)

    def refresh(self) -> None:
        self.panel.show_loading()
        c, s = self.session.client, self.session
        names = loc_codes(s)

        def done(d):
            for r in d["items"]:
                r["location"] = names.get(r["location_id"], f"#{r['location_id']}")
                r["summary"] = ", ".join(f"{l['sku']} {l['qty_delta']:+d}" for l in r["lines"][:4]) + (" …" if len(r["lines"]) > 4 else "")
            self.table.set_rows(d["items"])
            self.panel.show_content() if d["items"] else self.panel.show_empty("Nothing waiting for approval.")

        run_async(lambda: c.get("/inventory/requests", location_id=s.current_location_id, status=self.filter.currentData(), page_size=100),
                  done, lambda e: self.panel.show_error(e.message, e.offline))

    def _decide(self, approve: bool) -> None:
        row = self.table.selected_row()
        if not row or row["status"] != "pending":
            self.status.show_message("Select a request that is waiting for approval.", "info"); return
        if row["requested_by_id"] == self.session.user["id"]:
            self.status.show_message("You cannot approve or reject a request you made yourself.", "error"); return
        if DecisionDialog(self.session, row, approve, self).exec():
            self.status.show_message("Approved. Stock updated." if approve else "Rejected.", "ok"); self.refresh()

    def _new(self) -> None:
        d = RequestDialog(self.session, self)
        if d.exec():
            r = d.result_data
            self.status.show_message("Submitted." if r["status"] == "pending" else "Applied (below the auto-approval limit).", "ok")
            self.refresh()

    def _opening(self) -> None:
        if OpeningStockDialog(self.session, self).exec():
            self.status.show_message("Opening stock entered.", "ok")

    def _move(self) -> None:
        if MoveDialog(self.session, self).exec():
            self.status.show_message("Recorded.", "ok")


class AlertsTab(_Tab):
    def __init__(self, session):
        super().__init__(session)
        self.lay.addWidget(label("Low stock (at or below the reorder threshold, using available quantity)", "sectionTitle"))
        self.low = DataTable([Column("sku", "SKU", 128), Column("variant_name", "Item"), Column("location", "Location", 110),
                              Column("available", "Available", 90, "right"), Column("reorder_threshold", "Reorder at", 90, "right")])
        self.low_panel = StatePanel(self.low, "Nothing is below its reorder threshold. (Items with no threshold set are not monitored.)")
        self.lay.addWidget(self.low_panel, 1)
        self.exp_title = label("Expiring or expired stock", "sectionTitle")
        self.lay.addWidget(self.exp_title)
        self.exp = DataTable([Column("sku", "SKU", 128), Column("variant_name", "Item"), Column("location", "Location", 110),
                              Column("batch_no", "Batch", 100), Column("expiry_date", "Expiry", 110), Column("qty", "Qty", 70, "right"),
                              Column("days_left", "Days left", 90, "right", status=lambda v: "danger" if (v is not None and v < 0) else "warn")])
        self.exp_panel = StatePanel(self.exp, "No batches are close to expiry.")
        self.lay.addWidget(self.exp_panel, 1)
        for p in (self.low_panel, self.exp_panel):
            p.retry.connect(self.refresh)

    def refresh(self) -> None:
        self.low_panel.show_loading(); self.exp_panel.show_loading()
        c, s = self.session.client, self.session
        names = loc_codes(s)

        def done(d):
            for r in d["low_stock"] + d["expiring"]:
                r["location"] = names.get(r["location_id"], f"#{r['location_id']}")
            self.low.set_rows(d["low_stock"]); self.exp.set_rows(d["expiring"])
            self.exp_title.setText(f"Expiring within {d['expiry_warning_days']} days, or already expired")
            self.low_panel.show_content() if d["low_stock"] else self.low_panel.show_empty()
            self.exp_panel.show_content() if d["expiring"] else self.exp_panel.show_empty()

        def fail(e):
            self.low_panel.show_error(e.message, e.offline); self.exp_panel.show_error(e.message, e.offline)

        run_async(lambda: c.get("/inventory/alerts", location_id=s.current_location_id), done, fail)


class ValuationTab(_Tab):
    def __init__(self, session):
        super().__init__(session)
        self.lay.addWidget(label("Stock value at weighted-average cost. Taken from posted ledger transactions only; the reference purchase cost on a product is never used.", "muted", wrap=True))
        self.table = DataTable([Column("location", "Location"), Column("units", "Units owned", 120, "right"),
                                Column("value", "Value (PKR)", 150, "right", fmt=_money), Column("in_transit_value", "In transit to location (PKR)", 220, "right", fmt=_money)])
        self.panel = StatePanel(self.table, "No stock value yet.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)
        self.total = label("", "sectionTitle")
        self.lay.addWidget(self.total)
        self.note = label("This is an operational valuation. It is not posted to the accounting ledger (that arrives in Phase 6).", "muted", wrap=True)
        self.lay.addWidget(self.note)

    def refresh(self) -> None:
        self.panel.show_loading()
        c, s = self.session.client, self.session
        names = loc_names(s)

        def done(d):
            for r in d["locations"]:
                r["location"] = names.get(r["location_id"], f"#{r['location_id']}")
            self.table.set_rows(d["locations"])
            self.total.setText(f"Total: {format_money(d['total_value'])}  ·  {d['total_units']} units  ·  in transit {format_money(d['total_in_transit_value'])}")
            self.panel.show_content() if d["locations"] else self.panel.show_empty()

        run_async(lambda: c.get("/inventory/valuation", location_id=s.current_location_id), done, lambda e: self.panel.show_error(e.message, e.offline))


class InventoryPage(Page):
    title = "Inventory"

    def __init__(self, session: Session):
        super().__init__(session)
        self.root.addWidget(PageHeader("Inventory", "Stock by location. Every change is a recorded movement; nothing is edited in place."))
        self.tabs = QTabWidget()
        self.root.addWidget(self.tabs, 1)
        self.stock = StockTab(session)
        self.items: list[tuple[str, _Tab]] = [("Stock", self.stock), ("Movements", MovementsTab(session)),
                                              ("Receiving", ReceivingTab(session)), ("Counts", CountsTab(session)),
                                              ("Adjustments and approvals", ApprovalsTab(session)), ("Alerts", AlertsTab(session))]
        if session.can("inventory.view_valuation"):
            self.items.append(("Valuation", ValuationTab(session)))
        for t, w in self.items:
            self.tabs.addTab(w, t)
        self.tabs.currentChanged.connect(lambda _: self.refresh())

    def refresh(self) -> None:
        w = self.tabs.currentWidget()
        if isinstance(w, _Tab):
            w.refresh()
