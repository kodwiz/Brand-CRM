"""Reports: a catalogue of the reports this user may run, filters, results that show how they reconcile to the ledger, and PDF or Excel export.
The server decides which reports exist for the signed-in user; a report that is not listed cannot be run. Exports are produced by the server, which
checks permission again and writes an audit entry."""
from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QComboBox, QFileDialog, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QSpinBox, QVBoxLayout, QWidget

from desktop.pages.base import Page
from desktop.pages.suppliers import _date_edit, _iso
from desktop.session import Session
from desktop.ui.widgets import Banner, Column, DataTable, PageHeader, StatePanel, button, format_money, label
from desktop.ui.worker import run_async

GROUP_ORDER = ["Sales", "Stock", "People", "Money", "Statements", "Control"]
PARTY_ENDPOINTS = {"customer": ("/customers", "name"), "distributor": ("/distributors", "name"), "supplier": ("/suppliers", "name")}


class ReportsPage(Page):
    title = "Reports"

    def __init__(self, session: Session):
        super().__init__(session)
        self.catalogue: list[dict] = []
        self.current: dict | None = None
        self.last_params: dict = {}
        self.can_export = False
        self.root.addWidget(PageHeader("Reports", "Figures come from posted records. Each report shows how it agrees with the ledger."))
        body = QHBoxLayout(); body.setSpacing(18)

        self.listing = QListWidget()
        self.listing.setFixedWidth(220)
        self.listing.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.listing.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.listing.currentItemChanged.connect(self._picked)
        body.addWidget(self.listing)

        right = QVBoxLayout(); right.setSpacing(10)
        self.title_l = label("", "sectionTitle")
        self.help_l = label("", "muted", wrap=True)
        right.addWidget(self.title_l); right.addWidget(self.help_l)
        self.params_bar = QHBoxLayout(); self.params_bar.setSpacing(8)
        right.addLayout(self.params_bar)
        self.actions_bar = QHBoxLayout(); self.actions_bar.setSpacing(8)      # Run and export sit on their own row so the filters never push the window wider
        right.addLayout(self.actions_bar)
        self.status = Banner()
        right.addWidget(self.status)
        self.recon = QVBoxLayout(); self.recon.setSpacing(2)
        right.addLayout(self.recon)

        self.table_host = QWidget()
        self.table_lay = QVBoxLayout(self.table_host)
        self.table_lay.setContentsMargins(0, 0, 0, 0)
        self.table = DataTable([Column("x", "")])
        self.table_lay.addWidget(self.table)
        self.panel = StatePanel(self.table_host, "No records match these filters.")
        self.panel.retry.connect(self.run)
        self.panel.show_empty("Choose a report on the left.")
        right.addWidget(self.panel, 1)
        self.totals_l = label("", "muted", wrap=True)
        self.notes_l = label("", "muted", wrap=True)
        right.addWidget(self.totals_l); right.addWidget(self.notes_l)
        body.addLayout(right, 1)
        self.root.addLayout(body, 1)
        self.widgets: dict[str, QWidget] = {}

    # -- catalogue --------------------------------------------------------------------------
    def refresh(self) -> None:
        if self.catalogue:
            return
        c = self.session.client

        def done(r):
            self.catalogue, self.can_export = r["items"], r["can_export"]
            self.listing.clear()
            by: dict[str, list[dict]] = {}
            for rep in self.catalogue:
                by.setdefault(rep["group"], []).append(rep)
            for g in sorted(by, key=lambda x: GROUP_ORDER.index(x) if x in GROUP_ORDER else 99):
                h = QListWidgetItem(g.upper()); h.setFlags(Qt.ItemFlag.NoItemFlags)
                self.listing.addItem(h)
                for rep in by[g]:
                    it = QListWidgetItem("   " + rep["title"]); it.setData(Qt.ItemDataRole.UserRole, rep["key"])
                    self.listing.addItem(it)
            if not self.catalogue:
                self.panel.show_empty("Your role has no reports.")
        run_async(lambda: c.get("/insight/reports"), done, lambda e: self.status.show_message(e.message, "error"))

    def select(self, key: str) -> None:
        for i in range(self.listing.count()):
            if self.listing.item(i).data(Qt.ItemDataRole.UserRole) == key:
                self.listing.setCurrentRow(i)
                return

    def _picked(self, item, _prev=None) -> None:
        key = item.data(Qt.ItemDataRole.UserRole) if item else None
        rep = next((r for r in self.catalogue if r["key"] == key), None)
        if rep is None:
            return
        self.current = rep
        self.title_l.setText(rep["title"]); self.help_l.setText(rep.get("help", ""))
        self.status.clear()
        self._build_params(rep)
        self._clear_result()
        self.panel.show_empty("Set the filters and press Run.")

    # -- filters ----------------------------------------------------------------------------------
    def _build_params(self, rep: dict) -> None:
        for bar in (self.params_bar, self.actions_bar):
            while bar.count():
                it = bar.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()
        w: dict[str, QWidget] = {}
        first = QDate.currentDate().addDays(-QDate.currentDate().day() + 1)
        P = rep["params"]
        if "range" in P:
            w["date_from"] = _date_edit(first); w["date_to"] = _date_edit()
            for t, x in (("From", w["date_from"]), ("to", w["date_to"])):
                self.params_bar.addWidget(label(t, "fieldLabel")); self.params_bar.addWidget(x)
        if "as_of" in P:
            w["as_of"] = _date_edit()
            self.params_bar.addWidget(label("As of", "fieldLabel")); self.params_bar.addWidget(w["as_of"])
        if "location" in P:
            cb = QComboBox(); cb.addItem("All locations", None)
            for l in self.session.locations:
                cb.addItem(f"{l['name']}  ({l['city']})", l["id"])
            if self.session.current_location_id is not None:
                cb.setCurrentIndex(max(cb.findData(self.session.current_location_id), 0))
            elif len(self.session.locations) == 1:
                cb.setCurrentIndex(1)
            cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon); cb.setMinimumContentsLength(14); cb.setMaximumWidth(240)
            w["location_id"] = cb
            self.params_bar.addWidget(cb)
        if "group" in P:
            cb = QComboBox(); cb.addItem("By day", "day"); cb.addItem("By month", "month")
            w["group"] = cb; self.params_bar.addWidget(cb)
        if "order" in P:
            cb = QComboBox(); cb.addItem("Best by revenue", "revenue"); cb.addItem("Best by units", "units")
            w["order"] = cb; self.params_bar.addWidget(cb)
        if "days" in P:
            sp = QSpinBox(); sp.setRange(1, 730); sp.setValue(60); sp.setSuffix(" days ahead")
            w["days"] = sp; self.params_bar.addWidget(sp)
        for k, ph in (("status", "Status (optional)"), ("q", "Search item or SKU"), ("action", "Action contains…"), ("actor", "Done by…"), ("entity_type", "Record type…")):
            if k in P:
                e = QLineEdit(); e.setPlaceholderText(ph); e.setMinimumWidth(110); e.returnPressed.connect(self.run)
                w[k] = e; self.params_bar.addWidget(e)
        if "party" in P:
            t = QComboBox()
            for k in ("customer", "distributor", "supplier", "employee"):
                t.addItem(k.capitalize(), k)
            who = QComboBox(); who.setMinimumWidth(180); who.setMaximumWidth(260); who.addItem("Choose…", None)
            t.currentIndexChanged.connect(lambda _: self._load_parties(t, who))
            w["party_type"] = t; w["party_id"] = who
            self.params_bar.addWidget(t); self.params_bar.addWidget(who)
            self._load_parties(t, who)
        self.params_bar.addStretch()
        self.actions_bar.addWidget(button("Run", "primary", self.run))
        if self.can_export:
            self.xl = button("Export Excel", None, lambda: self.export("xlsx"))
            self.pdf = button("Export PDF", None, lambda: self.export("pdf"))
            self.xl.setEnabled(False); self.pdf.setEnabled(False)
            self.actions_bar.addWidget(self.xl); self.actions_bar.addWidget(self.pdf)
        self.actions_bar.addStretch()
        self.widgets = w

    def _load_parties(self, t: QComboBox, who: QComboBox) -> None:
        kind = t.currentData()
        c = self.session.client
        who.clear(); who.addItem("Loading…", None)

        def done(rows):
            who.clear(); who.addItem("Choose…", None)
            for r in rows:
                who.addItem(r.get("name") or r.get("full_name"), r["id"])

        def work():
            if kind == "employee":
                return c.get("/employees/lookup")
            path, _ = PARTY_ENDPOINTS[kind]
            return c.get(path, page_size=200)["items"]
        run_async(work, done, lambda e: (who.clear(), who.addItem("Not available to you", None)))

    def _params(self) -> dict:
        out: dict = {}
        for k, x in self.widgets.items():
            if hasattr(x, "date") and callable(x.date):
                out[k] = _iso(x)
            elif isinstance(x, QComboBox):
                out[k] = x.currentData()
            elif isinstance(x, QSpinBox):
                out[k] = x.value()
            elif isinstance(x, QLineEdit):
                out[k] = x.text().strip() or None
        return {k: v for k, v in out.items() if v not in (None, "")}

    # -- run --------------------------------------------------------------------------------------------
    def run(self) -> None:
        if self.current is None:
            return
        p = self._params()
        if "date_from" in p and "date_to" in p and p["date_to"] < p["date_from"]:
            self.status.show_message("The end date is before the start date.", "error")
            return
        self.status.clear()
        self.panel.show_loading()
        key, c = self.current["key"], self.session.client
        self.last_params = p
        run_async(lambda: c.get(f"/insight/reports/{key}", **p), self._loaded, self._failed)

    def _failed(self, e) -> None:
        self.panel.show_empty("The report could not be produced.")
        self.status.show_message(e.message, "error")

    def _clear_result(self) -> None:
        while self.recon.count():
            it = self.recon.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.totals_l.setText(""); self.notes_l.setText("")
        if self.can_export and hasattr(self, "xl"):
            try:
                self.xl.setEnabled(False); self.pdf.setEnabled(False)
            except RuntimeError:
                pass

    def _loaded(self, rep: dict) -> None:
        self._clear_result()
        self.result = rep
        cols = [Column(c["key"], c["label"], None if i == 0 else 120, c.get("align", "left")) for i, c in enumerate(rep["columns"])]
        # drop the old table, build one that matches this report's columns
        self.table_lay.removeWidget(self.table); self.table.deleteLater()
        self.table = DataTable(cols)
        self.table.set_rows(rep["rows"])
        self.table_lay.addWidget(self.table)
        self.title_l.setText(rep["title"] + (f"  ·  {rep['subtitle']}" if rep.get("subtitle") else ""))
        for r in rep.get("reconciliation") or []:
            ok = r["agrees"]
            t = (f"✓ {r['label']}: report {format_money(r['report'])}, ledger {format_money(r['ledger'])}" if ok else
                 f"✗ {r['label']}: report {format_money(r['report'])}, ledger {format_money(r['ledger'])}, difference {format_money(r['difference'])}. {r.get('note', '')}")
            l = label(t, "fieldLabel" if ok else "sectionTitle", wrap=True)
            self.recon.addWidget(l)
        tot = rep.get("totals") or {}
        pieces = [f"{c['label']}: {tot[c['key']]}" for c in rep["columns"] if c["key"] in tot and tot[c["key"]] not in (None, "")]
        self.totals_l.setText("Totals   " + "   ·   ".join(pieces) if pieces else "")
        self.notes_l.setText("\n".join(rep.get("notes") or []) + f"\nProduced {rep['generated_at'][:16].replace('T', ' ')} UTC by {rep['generated_by']}.")
        if rep["rows"]:
            self.panel.show_content()
        else:
            self.panel.show_empty("No records match these filters.")
        if self.can_export and hasattr(self, "xl"):
            self.xl.setEnabled(True); self.pdf.setEnabled(True)

    # -- export ---------------------------------------------------------------------------------------------
    def export(self, fmt: str) -> None:
        if self.current is None:
            return
        name = f"{self.current['key']}-{date.today().isoformat()}.{fmt}"
        path, _ = QFileDialog.getSaveFileName(self, "Save report", name)
        if path:
            self.export_to(path, fmt)

    def export_to(self, path: str, fmt: str, done=None) -> None:
        key, c, p = self.current["key"], self.session.client, dict(self.last_params)

        def ok(data: bytes):
            with open(path, "wb") as f:
                f.write(data)
            self.status.show_message(f"Saved {path}. The export has been recorded in the audit log.", "ok")
            if done:
                done(path)
        run_async(lambda: c.get_bytes(f"/insight/reports/{key}/export", format=fmt, **p), ok, lambda e: self.status.show_message(e.message, "error"))
