"""Dashboard: the role-specific view of the business for a chosen period and location.
The server decides which view and which figures the signed-in user may see; this screen only draws what it is given. Revenue, cash collected,
receivables, stock value and profit are drawn under separate headings because they are different things and must never be added together."""
from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from desktop.pages.base import Page
from desktop.pages.suppliers import _date_edit, _iso
from desktop.ui.widgets import Banner, Column, DataTable, PageHeader, StatePanel, button, format_money, hr, label
from desktop.ui.worker import run_async

PRESETS = [("This month", "month"), ("Today", "today"), ("Last 7 days", "7"), ("Last 30 days", "30"), ("This year", "year"), ("Custom", "custom")]


def _fmt(m: dict) -> str:
    v = m.get("value")
    if m.get("unit") == "money":
        return format_money(v)
    if m.get("unit") == "count":
        return f"{int(v):,}"
    return str(v)


def _clear(layout) -> None:
    while layout.count():
        it = layout.takeAt(0)
        w = it.widget()
        if w is not None:
            w.deleteLater()
        elif it.layout() is not None:
            _clear(it.layout())


class _Tile(QWidget):
    def __init__(self, m: dict):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 20, 6)
        lay.setSpacing(0)
        lay.addWidget(label(m["label"], "fieldLabel"))
        v = label(_fmt(m), "kpiValue")
        lay.addWidget(v)
        if m.get("hint"):
            lay.addWidget(label(m["hint"], "muted", wrap=True))
        self.setToolTip(f"{m['group_label']}: {m['label']}")


class DashboardPage(Page):
    title = "Dashboard"

    def __init__(self, session):
        super().__init__(session)
        self.header = PageHeader("Dashboard", None)
        self.root.addWidget(self.header)
        self.sub = label("", "muted")
        self.root.addWidget(self.sub)
        bar = QHBoxLayout()
        self.preset = QComboBox()
        for t, k in PRESETS:
            self.preset.addItem(t, k)
        self.d_from = _date_edit(QDate.currentDate().addDays(-QDate.currentDate().day() + 1))
        self.d_to = _date_edit()
        self.preset.currentIndexChanged.connect(self._preset)
        for w in (self.d_from, self.d_to):
            w.dateChanged.connect(lambda _: self._custom())
        bar.addWidget(label("Period", "fieldLabel")); bar.addWidget(self.preset)
        bar.addWidget(label("From", "fieldLabel")); bar.addWidget(self.d_from)
        bar.addWidget(label("to", "fieldLabel")); bar.addWidget(self.d_to)
        bar.addWidget(button("Refresh", None, self.refresh))
        bar.addStretch()
        self.scope_note = label("", "muted")
        bar.addWidget(self.scope_note)
        self.root.addLayout(bar)
        self.status = Banner()
        self.root.addWidget(self.status)

        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(0, 0, 8, 0)
        self.body_lay.setSpacing(14)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.body)
        self.panel = StatePanel(scroll, "Nothing to show for this period.")
        self.panel.retry.connect(self.refresh)
        self.root.addWidget(self.panel, 1)
        self._loading_preset = False

    # -- period -------------------------------------------------------------------------
    def _preset(self) -> None:
        k = self.preset.currentData()
        if k == "custom":
            return
        t = date.today()
        f = {"month": t.replace(day=1), "today": t, "7": t - timedelta(days=6), "30": t - timedelta(days=29), "year": t.replace(month=1, day=1)}[k]
        self._loading_preset = True
        self.d_from.setDate(QDate(f.year, f.month, f.day)); self.d_to.setDate(QDate(t.year, t.month, t.day))
        self._loading_preset = False
        self.refresh()

    def _custom(self) -> None:
        if not self._loading_preset:
            self.preset.blockSignals(True)
            self.preset.setCurrentIndex(self.preset.findData("custom"))
            self.preset.blockSignals(False)

    # -- data -----------------------------------------------------------------------------
    def refresh(self) -> None:
        if self.d_to.date() < self.d_from.date():
            self.status.show_message("The end date is before the start date.", "error")
            return
        self.status.clear()
        self.panel.show_loading()
        s = self.session
        params = {"date_from": _iso(self.d_from), "date_to": _iso(self.d_to), "location_id": s.current_location_id}
        run_async(lambda: s.client.get("/insight/dashboard", **params), self._loaded, lambda e: self.panel.show_error(e.message, e.offline))

    def _loaded(self, d: dict) -> None:
        a, b = date.fromisoformat(d["period"]["from"]), date.fromisoformat(d["period"]["to"])
        self.sub.setText(f"{d['title']}  ·  {a:%d %b %Y} to {b:%d %b %Y}")
        self.scope_note.setText(d.get("scope") or "")
        _clear(self.body_lay)
        if d["alerts"]:
            box = QVBoxLayout(); box.setSpacing(4)
            self.body_lay.addWidget(label("Needs attention", "sectionTitle"))
            for a in d["alerts"]:
                row = QHBoxLayout()
                b = Banner()
                b.show_message(a["text"], "error" if a["severity"] == "error" else "info")
                row.addWidget(b, 1)
                if a.get("screen"):
                    row.addWidget(button("Open", None, lambda s=a["screen"]: self.navigate_requested.emit(s)))
                box.addLayout(row)
            self.body_lay.addLayout(box)
        for sec in d["sections"]:
            self.body_lay.addWidget(hr())
            self.body_lay.addWidget(label(sec["title"], "sectionTitle"))
            if sec.get("note"):
                self.body_lay.addWidget(label(sec["note"], "muted", wrap=True))
            if sec["metrics"]:
                self.body_lay.addLayout(self._tiles(sec["metrics"]))
            t = sec.get("table")
            if t:
                cols = [Column(c["key"], c["label"], None if i == 0 else 130, c.get("align", "left")) for i, c in enumerate(t["columns"])]
                tbl = DataTable(cols)
                tbl.set_rows(t["rows"])
                tbl.setFixedHeight(min(34 * (len(t["rows"]) + 1) + 6, 300))
                if t["rows"]:
                    self.body_lay.addWidget(tbl)
                else:
                    self.body_lay.addWidget(label("Nothing to show for this period.", "muted"))
        self.body_lay.addStretch()
        self.panel.show_content()

    def _tiles(self, metrics: list[dict]) -> QVBoxLayout:
        """One row of tiles per group, each row headed by its group label, so different kinds of figure never sit in the same row."""
        out = QVBoxLayout(); out.setSpacing(2)
        groups: dict[str, list[dict]] = {}
        for m in metrics:
            groups.setdefault(m["group_label"], []).append(m)
        for name, items in groups.items():
            if len(groups) > 1:
                out.addWidget(label(name.upper(), "fieldLabel"))
            for k in range(0, len(items), 5):          # at most five tiles per row so they fit at 1366 wide; every tile has the same width
                row = QHBoxLayout(); row.setSpacing(0)
                for m in items[k:k + 5]:
                    t = _Tile(m); t.setFixedWidth(210)
                    row.addWidget(t, 0, Qt.AlignmentFlag.AlignTop)
                row.addStretch()
                out.addLayout(row)
        return out
