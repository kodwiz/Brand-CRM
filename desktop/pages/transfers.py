"""Stock transfers: list, new request, approve / dispatch / receive actions and a traceable timeline."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLineEdit, QSpinBox, QVBoxLayout, QWidget

from desktop.pages.base import Page

from desktop.session import Session
from desktop.ui.forms import FormDialog, FormGrid
from desktop.ui.lines import LinesEditor
from desktop.ui.widgets import Banner, Column, DataTable, PageHeader, Pager, StatePanel, button, label
from desktop.ui.worker import run_async

STATE = {"requested": ("Requested", "warn"), "approved": ("Approved", "warn"), "dispatched": ("In transit", "warn"),
         "partially_received": ("Partly received", "warn"), "discrepancy_review": ("Discrepancy", "danger"),
         "received": ("Received", "ok"), "rejected": ("Rejected", "neutral"), "cancelled": ("Cancelled", "neutral")}
ORDER = ["requested", "approved", "dispatched", "received"]


def _when(v):
    if not v:
        return "-"
    try:
        return datetime.fromisoformat(v).astimezone().strftime("%d %b %Y %H:%M")
    except ValueError:
        return v


class NewTransferDialog(FormDialog):
    def __init__(self, session: Session, locations: list[dict], parent=None):
        super().__init__("Request a transfer", "Submit request", parent, 860)
        self.session = session
        g = FormGrid(3)
        self.src, self.dst = QComboBox(), QComboBox()
        for cb in (self.src, self.dst):
            cb.addItem("Select…", None)
        for l in locations:
            self.src.addItem(f"{l['name']}  ({l['city']})", l["id"]); self.dst.addItem(f"{l['name']}  ({l['city']})", l["id"])
        self.notes = QLineEdit()
        g.add("src", "Send from", self.src); g.add("dst", "Send to", self.dst); g.add("notes", "Notes", self.notes)
        self.form = g
        self.body.addWidget(g)
        self.body.addWidget(label("Stock is reserved at the sending location when the request is approved. Another person must approve.", "muted", wrap=True))
        self.editor = LinesEditor(session, [("batch_id", "Batch (optional)", "batch"), ("qty", "Quantity", "int")],
                                  [Column("batch_id_label", "Batch", 140), Column("qty", "Qty", 80, "right")])
        self.body.addWidget(self.editor)

    def submit(self) -> None:
        self.form.clear_errors()
        if self.src.currentData() is None or self.dst.currentData() is None:
            self.form.set_error("src", "Choose both locations."); return self.reenable()
        if self.src.currentData() == self.dst.currentData():
            self.form.set_error("dst", "Sending and receiving locations must differ."); return self.reenable()
        lines = [l for l in self.editor.payload() if l["qty"] > 0]
        if not lines or len(lines) != len(self.editor.lines):
            self.editor.banner.show_message("Add lines with a quantity above zero.", "error"); return self.reenable()
        body = {"from_location_id": self.src.currentData(), "to_location_id": self.dst.currentData(),
                "notes": self.notes.text().strip() or None,
                "lines": [{"variant_id": l["variant_id"], "batch_id": l.get("batch_id"), "qty": l["qty"]} for l in lines]}
        c = self.session.client
        run_async(lambda: c.post("/transfers", body), lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class ReceiveDialog(FormDialog):
    def __init__(self, session: Session, transfer: dict, parent=None):
        super().__init__(f"Receive {transfer['ref']}", "Record receipt", parent, 640)
        self.session, self.t = session, transfer
        self.body.addWidget(label("Enter what actually arrived. You can receive part now and the rest later. Damaged units are held out of saleable stock.", "muted", wrap=True))
        self.rows: list[tuple[dict, QSpinBox, QSpinBox]] = []
        g = FormGrid(3)
        for ln in transfer["lines"]:
            outstanding = ln["qty_dispatched"] - ln["qty_received_good"] - ln["qty_received_damaged"] - ln["qty_short"]
            if outstanding <= 0:
                continue
            good, dmg = QSpinBox(), QSpinBox()
            good.setRange(0, outstanding); dmg.setRange(0, outstanding)
            good.setValue(outstanding)
            title = f"{ln['sku']}" + (f" (batch {ln['batch_no']})" if ln.get("batch_no") else "") + f": {outstanding} expected"
            g.add(f"l{ln['id']}", title, good); g.add(f"d{ln['id']}", "Damaged on arrival", dmg)
            self.rows.append((ln, good, dmg))
            g._n += 1
        self.form = g
        self.body.addWidget(g)
        self.notes = QLineEdit(); self.notes.setPlaceholderText("Notes (optional)")
        self.body.addWidget(self.notes)

    def submit(self) -> None:
        lines = []
        for ln, good, dmg in self.rows:
            outstanding = ln["qty_dispatched"] - ln["qty_received_good"] - ln["qty_received_damaged"] - ln["qty_short"]
            if good.value() + dmg.value() > outstanding:
                self.finish_error(f"{ln['sku']}: more than the {outstanding} expected."); return
            if good.value() + dmg.value() > 0:
                lines.append({"line_id": ln["id"], "qty_good": good.value(), "qty_damaged": dmg.value()})
        if not lines:
            self.finish_error("Enter at least one received quantity."); return
        c = self.session.client
        run_async(lambda: c.post(f"/transfers/{self.t['id']}/receive", {"lines": lines, "notes": self.notes.text().strip() or None}),
                  lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class TransferDetail(QDialog):
    def __init__(self, session: Session, transfer_id: int, names: dict, parent=None):
        super().__init__(parent)
        self.session, self.tid, self.t, self.names = session, transfer_id, {}, names
        self.changed = False
        self.setWindowTitle("Transfer")
        self.setMinimumSize(900, 600)
        lay = QVBoxLayout(self); lay.setContentsMargins(20, 16, 20, 14)
        self.title = label("", "pageTitle"); self.sub = label("", "muted")
        lay.addWidget(self.title); lay.addWidget(self.sub)
        self.banner = Banner(); lay.addWidget(self.banner)
        self.progress = label("", "sectionTitle"); lay.addWidget(self.progress)
        lay.addWidget(label("Items", "sectionTitle"))
        cols = [Column("sku", "SKU", 122), Column("variant_name", "Item"), Column("batch_no", "Batch", 90),
                Column("qty_requested", "Requested", 84, "right"), Column("qty_dispatched", "Sent", 60, "right"),
                Column("qty_received_good", "Received", 76, "right"), Column("qty_received_damaged", "Damaged", 76, "right"),
                Column("qty_short", "Short", 60, "right")]
        self.lines = DataTable(cols)
        self.lines.setMaximumHeight(170)
        lay.addWidget(self.lines)
        lay.addWidget(label("History", "sectionTitle"))
        self.timeline = DataTable([Column("at", "When", 140, fmt=_when), Column("event", "Event", 170, fmt=lambda v: str(v).replace("_", " ").capitalize()),
                                   Column("by", "By", 130), Column("note", "Note")])
        lay.addWidget(self.timeline, 1)
        row = QHBoxLayout(); row.addStretch()
        self.actions: dict[str, object] = {}
        spec = [("reject", "Reject…", "transfers.approve", None), ("cancel", "Cancel transfer", "transfers.request", "danger"),
                ("approve", "Approve", "transfers.approve", "primary"), ("dispatch", "Dispatch", "transfers.dispatch", "primary"),
                ("receive", "Receive…", "transfers.receive", "primary"), ("flag", "Report missing goods…", "transfers.receive", None),
                ("resolve", "Approve shortage…", "transfers.approve_discrepancy", "primary")]
        for key, text, perm, kind in spec:
            b = button(text, kind, lambda k=key: self._act(k)); b.setVisible(False); b.setProperty("perm", perm)
            self.actions[key] = b; row.addWidget(b)
        row.addWidget(button("Close", on_click=self.accept))
        lay.addLayout(row)
        self.load()

    def load(self) -> None:
        c = self.session.client
        run_async(lambda: c.get(f"/transfers/{self.tid}"), self._loaded, lambda e: self.banner.show_message(e.message, "error"))

    def _loaded(self, t: dict) -> None:
        self.t = t
        names = self.names
        label_, _ = STATE.get(t["status"], (t["status"], "neutral"))
        self.title.setText(f"{t['ref']}  ·  {label_}")
        self.sub.setText(f"{names.get(t['from_location_id'], t['from_location_id'])}  →  {names.get(t['to_location_id'], t['to_location_id'])}"
                         + (f"\n{t['notes']}" if t.get("notes") else "")
                         + (f"\nDiscrepancy: {t['shortage_reason']}" if t.get("shortage_reason") else ""))
        if t["status"] in ORDER:
            parts = []
            for s in ORDER:
                mark = "●" if ORDER.index(s) <= ORDER.index(t["status"]) else "○"
                parts.append(f"{mark} {STATE[s][0]}")
            self.progress.setText("   →   ".join(parts))
        else:
            self.progress.setText(label_ + (f": {t['decision_reason']}" if t.get("decision_reason") else ""))
        self.lines.set_rows(t["lines"])
        self.timeline.set_rows([{"at": e["at"], "event": e["event"], "by": e["by"], "note": e["note"]} for e in t["events"]])
        st, me = t["status"], self.session.user["id"]
        show = {"approve": st == "requested" and t["requested_by_id"] != me, "reject": st == "requested",
                "cancel": st in ("requested", "approved"), "dispatch": st == "approved",
                "receive": st in ("dispatched", "partially_received"), "flag": st in ("dispatched", "partially_received"),
                "resolve": st == "discrepancy_review"}
        for k, b in self.actions.items():
            b.setVisible(show[k] and self.session.can(b.property("perm")))

    def _post(self, path: str, body: dict | None = None, ok: str = "Done.") -> None:
        c = self.session.client
        run_async(lambda: c.post(path, body or {}), lambda _: (self.banner.show_message(ok, "ok"), setattr(self, "changed", True), self.load()),
                  lambda e: self.banner.show_message(e.message, "error"))

    def _ask(self, title: str, prompt: str, path: str, key: str, ok: str) -> None:
        dlg = FormDialog(title, "Confirm", self, 440)
        txt = QLineEdit(); txt.setPlaceholderText(prompt)
        dlg.body.addWidget(txt)
        c = self.session.client

        def go():
            if not txt.text().strip():
                dlg.finish_error("This is required."); return
            run_async(lambda: c.post(path, {key: txt.text().strip()}), lambda _: dlg.finish_ok(), lambda e: dlg.finish_error(e.message))

        dlg.submit = go  # type: ignore[method-assign]
        if dlg.exec():
            self.changed = True; self.banner.show_message(ok, "ok"); self.load()

    def _act(self, key: str) -> None:
        p = f"/transfers/{self.tid}"
        if key == "approve":
            self._post(p + "/approve", {}, "Approved. Stock is now reserved at the sending location.")
        elif key == "dispatch":
            self._post(p + "/dispatch", None, "Dispatched. The goods are now in transit.")
        elif key == "reject":
            self._ask("Reject transfer", "Reason (required)", p + "/reject", "reason", "Rejected.")
        elif key == "cancel":
            self._ask("Cancel transfer", "Reason (required)", p + "/cancel", "reason", "Cancelled. Reserved stock released.")
        elif key == "flag":
            self._ask("Report missing goods", "What is missing or wrong", p + "/flag-discrepancy", "reason", "Sent for review.")
        elif key == "resolve":
            self._ask("Approve shortage", "Note (required)", p + "/resolve-discrepancy", "note", "Shortage approved and closed.")
        elif key == "receive":
            if ReceiveDialog(self.session, self.t, self).exec():
                self.changed = True; self.banner.show_message("Receipt recorded.", "ok"); self.load()


class TransfersPage(Page):
    title = "Transfers"

    def __init__(self, session: Session):
        super().__init__(session)
        head = PageHeader("Transfers", "Move stock between locations: requested, approved, dispatched, received.")
        if session.can("transfers.request"):
            head.add_action(button("Request transfer", "primary", self._new))
        self.root.addWidget(head)
        self.status = Banner(); self.root.addWidget(self.status)
        bar = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.addItem("All transfers", None)
        for k, (t, _) in STATE.items():
            self.filter.addItem(t, k)
        self.filter.currentIndexChanged.connect(self.refresh)
        bar.addWidget(self.filter); bar.addStretch()
        self.root.addLayout(bar)
        self.table = DataTable([Column("ref", "Transfer", 112), Column("from", "From", 200), Column("to", "To", 200),
                                Column("items", "Items"), Column("requested_by", "Requested by", 116),
                                Column("requested_at", "Requested", 140, fmt=_when),
                                Column("status", "State", 140, status=lambda v: STATE.get(v, ("", "neutral"))[1],
                                       fmt=lambda v: STATE.get(v, (v, ""))[0])])
        self.table.row_activated.connect(self._open)
        self.panel = StatePanel(self.table, "No transfers yet.")
        self.panel.retry.connect(self.refresh)
        self.root.addWidget(self.panel, 1)
        self.pager = Pager(50); self.pager.changed.connect(self.refresh)
        self.root.addWidget(self.pager)
        self.locations: list[dict] = []
        self.names: dict[int, str] = {}

    def refresh(self) -> None:
        self.panel.show_loading()
        c, s = self.session.client, self.session

        def work():
            locs = c.get("/transfers/locations")
            data = c.get("/transfers", location_id=s.current_location_id, status=self.filter.currentData(),
                         page=self.pager.page, page_size=self.pager.page_size)
            return locs, data

        def done(res):
            self.locations, d = res
            names = self.names = {l["id"]: l["name"] for l in self.locations}
            for r in d["items"]:
                r["from"] = names.get(r["from_location_id"], f"#{r['from_location_id']}")
                r["to"] = names.get(r["to_location_id"], f"#{r['to_location_id']}")
                r["items"] = ", ".join(f"{l['sku']} ×{l['qty_requested']}" for l in r["lines"][:3]) + (" …" if len(r["lines"]) > 3 else "")
            self.table.set_rows(d["items"]); self.pager.update_total(d["total"])
            self.panel.show_content() if d["items"] else self.panel.show_empty()

        run_async(work, done, lambda e: self.panel.show_error(e.message, e.offline))

    def _open(self, row: dict) -> None:
        d = TransferDetail(self.session, row["id"], self.names, self)
        d.exec()
        if d.changed:
            self.refresh()

    def _new(self) -> None:
        if NewTransferDialog(self.session, self.locations, self).exec():
            self.status.show_message("Transfer requested. It needs approval before stock is reserved.", "ok")
            self.refresh()
