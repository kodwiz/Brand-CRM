"""Line entry used by receiving, adjustments, opening stock and transfers: pick an item, fill the fields, add."""
from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox, QDateEdit, QHBoxLayout, QLineEdit, QSpinBox, QVBoxLayout, QWidget,
)

from desktop.session import Session
from desktop.ui.widgets import Banner, Column, DataTable, button, label
from desktop.ui.worker import run_async

LOCATION_KINDS = ("location",)


class VariantPicker(QWidget):
    """Search by SKU, barcode or name; choose from the matches."""

    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.session = session
        self.items: list[dict] = []
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Scan or type SKU, barcode or name, then press Enter")
        self.edit.returnPressed.connect(self.search)
        self.combo = QComboBox()
        self.combo.setMinimumWidth(260)
        self.combo.addItem("No item chosen", None)
        lay.addWidget(self.edit, 1)
        lay.addWidget(self.combo, 1)
        self.combo.currentIndexChanged.connect(lambda _=0: self.changed())

    def changed(self) -> None:
        pass

    def search(self) -> None:
        q = self.edit.text().strip()
        if not q:
            return
        c = self.session.client

        def done(rows):
            self.items = rows
            self.combo.blockSignals(True)
            self.combo.clear()
            if not rows:
                self.combo.addItem("No match", None)
            for r in rows:
                self.combo.addItem(f"{r['sku']}  ·  {r['product_name']} - {r['name']}", r["id"])
            self.combo.blockSignals(False)
            self.combo.setCurrentIndex(0)
            self.changed()

        run_async(lambda: c.get("/variants/lookup", q=q), done, lambda e: None)

    def current(self) -> dict | None:
        vid = self.combo.currentData()
        return next((i for i in self.items if i["id"] == vid), None)

    def clear(self) -> None:
        self.edit.clear()
        self.items = []
        self.combo.clear()
        self.combo.addItem("No item chosen", None)


class LinesEditor(QWidget):
    """fields: (key, title, kind[, options]) where kind in int|money|text|date|bucket|batch|delta."""

    def __init__(self, session: Session, fields: list[tuple], columns: list[Column], parent=None):
        super().__init__(parent)
        self.session, self.fields = session, fields
        self.lines: list[dict] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.banner = Banner()
        lay.addWidget(self.banner)
        self.picker = VariantPicker(session)
        self.picker.changed = self._variant_changed
        lay.addWidget(self.picker)
        row = QHBoxLayout()
        self.inputs: dict[str, QWidget] = {}
        for f in fields:
            key, title, kind = f[0], f[1], f[2]
            w: QWidget
            if kind in ("int", "delta"):
                w = QSpinBox()
                w.setRange(-1_000_000 if kind == "delta" else 0, 1_000_000)
            elif kind == "date":
                w = QDateEdit()
                w.setCalendarPopup(True)
                w.setDisplayFormat("dd MMM yyyy")
                w.setSpecialValueText("None")
                w.setMinimumDate(QDate(2000, 1, 1))
                w.setDate(w.minimumDate())
            elif kind in ("bucket", "batch"):
                w = QComboBox()
                if kind == "bucket":
                    for t, v in f[3]:
                        w.addItem(t, v)
            else:
                w = QLineEdit()
                if kind == "money":
                    w.setPlaceholderText("0.00")
            self.inputs[key] = w
            col = QVBoxLayout()
            col.setSpacing(1)
            col.addWidget(label(title, "fieldLabel"))
            col.addWidget(w)
            row.addLayout(col)
        row.addStretch()
        add = button("Add line", on_click=self._add)
        row.addWidget(add, 0, Qt.AlignmentFlag.AlignBottom)
        lay.addLayout(row)
        self.table = DataTable([Column("sku", "SKU", 130), Column("variant", "Item")] + columns)
        self.table.setMinimumHeight(150)
        lay.addWidget(self.table, 1)
        rm = button("Remove selected line", on_click=self._remove)
        r2 = QHBoxLayout()
        r2.addStretch()
        r2.addWidget(rm)
        lay.addLayout(r2)

    def _variant_changed(self) -> None:
        v = self.picker.current()
        for f in self.fields:
            if f[2] == "batch":
                combo: QComboBox = self.inputs[f[0]]  # type: ignore[assignment]
                combo.clear()
                combo.addItem("-", None)
                if v and v["tracks_batches"]:
                    c = self.session.client
                    run_async(lambda: c.get("/inventory/batches", variant_id=v["id"]),
                              lambda rows: [combo.addItem(f"{b['batch_no']}" + (f"  (exp {b['expiry_date']})" if b["expiry_date"] else ""), b["id"]) for b in rows],
                              lambda e: None)

    def _value(self, f: tuple):
        w = self.inputs[f[0]]
        k = f[2]
        if k in ("int", "delta"):
            return w.value()
        if k == "date":
            return None if w.date() == w.minimumDate() else w.date().toString("yyyy-MM-dd")
        if k in ("bucket", "batch"):
            return w.currentData()
        return w.text().strip() or None

    def _add(self) -> None:
        self.banner.clear()
        v = self.picker.current()
        if not v:
            return self.banner.show_message("Search for an item and choose it first.", "error")
        line = {"variant_id": v["id"], "sku": v["sku"], "variant": f"{v['product_name']} - {v['name']}",
                "tracks_batches": v["tracks_batches"]}
        for f in self.fields:
            line[f[0]] = self._value(f)
            if f[2] == "batch" and line[f[0]] is not None:
                line[f[0] + "_label"] = self.inputs[f[0]].currentText()
        self.lines.append(line)
        self._render()
        self.picker.clear()
        for f in self.fields:
            if f[2] in ("int", "delta"):
                self.inputs[f[0]].setValue(0)
            elif f[2] in ("money", "text"):
                self.inputs[f[0]].clear()

    def _remove(self) -> None:
        i = self.table.currentRow()
        if 0 <= i < len(self.lines):
            del self.lines[i]
            self._render()

    def _render(self) -> None:
        self.table.set_rows(self.lines)

    def payload(self) -> list[dict]:
        keep = {f[0] for f in self.fields} | {"variant_id"}
        return [{k: v for k, v in ln.items() if k in keep} for ln in self.lines]
