"""Approvals inbox: every request waiting for a decision that this user is allowed to decide, in one list.
The inbox only lists. A decision is taken on the screen that owns the request, so its own rules (a different person must approve, limits, stock checks)
stay in one place. Requests the signed-in user raised themselves are marked, because someone else has to decide them."""
from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QHBoxLayout

from desktop.pages.base import Page
from desktop.session import Session
from desktop.ui.widgets import Banner, Column, DataTable, PageHeader, StatePanel, button, format_money, label
from desktop.ui.worker import run_async

SCREEN_NAMES = {"inventory": "Inventory", "transfers": "Transfers", "orders": "Orders", "distributors": "Distributors", "suppliers": "Suppliers",
                "accounting": "Accounting", "employees": "Employees", "attendance": "Attendance"}


def _age(v) -> str:
    n = int(v or 0)
    return "Today" if n == 0 else f"{n} day{'s' if n != 1 else ''}"


def _age_kind(v) -> str:
    n = int(v or 0)
    return "error" if n > 3 else ("warn" if n > 1 else "neutral")


class ApprovalsPage(Page):
    title = "Approvals"

    def __init__(self, session: Session):
        super().__init__(session)
        self.items: list[dict] = []
        self.header = PageHeader("Approvals", "Requests waiting for a decision. Decisions are taken on the screen that owns the request.")
        self.root.addWidget(self.header)
        bar = QHBoxLayout()
        self.kind = QComboBox(); self.kind.addItem("All types", None)
        self.kind.currentIndexChanged.connect(self._filter)
        self.mine = QComboBox(); self.mine.addItem("All requests", "all"); self.mine.addItem("Raised by someone else", "others"); self.mine.addItem("Raised by me", "mine")
        self.mine.currentIndexChanged.connect(self._filter)
        self.summary = label("", "muted")
        bar.addWidget(self.kind); bar.addWidget(self.mine); bar.addWidget(self.summary); bar.addStretch()
        bar.addWidget(button("Refresh", None, self.refresh))
        self.open_btn = button("Open on its screen", "primary", self._open)
        self.open_btn.setEnabled(False)
        bar.addWidget(self.open_btn)
        self.root.addLayout(bar)
        self.status = Banner()
        self.root.addWidget(self.status)
        self.table = DataTable([
            Column("type", "Request", 220), Column("ref", "Reference", 120), Column("requested_by", "Raised by", 140),
            Column("own", "", 80, fmt=lambda v: "You" if v else ""),
            Column("age_days", "Waiting", 90, status=_age_kind, fmt=_age),
            Column("amount", "Amount", 120, "right", fmt=lambda v: format_money(v) if v else "-"),
            Column("summary", "Details"),
        ])
        self.table.itemSelectionChanged.connect(lambda: self.open_btn.setEnabled(self.table.selected_row() is not None))
        self.table.row_activated.connect(lambda _r: self._open())
        self.panel = StatePanel(self.table, "Nothing is waiting for your approval.")
        self.panel.retry.connect(self.refresh)
        self.root.addWidget(self.panel, 1)

    def refresh(self) -> None:
        self.panel.show_loading()
        self.status.clear()
        c = self.session.client
        run_async(lambda: c.get("/insight/approvals"), self._loaded, lambda e: self.panel.show_error(e.message, e.offline))

    def _loaded(self, r: dict) -> None:
        self.items = r["items"]
        cur = self.kind.currentData()
        self.kind.blockSignals(True)
        self.kind.clear(); self.kind.addItem("All types", None)
        for t in sorted({i["type"] for i in self.items}):
            self.kind.addItem(t, t)
        self.kind.setCurrentIndex(max(self.kind.findData(cur), 0))
        self.kind.blockSignals(False)
        c = r["counts"]
        self.summary.setText(f"{c['total']} waiting  ·  {c['raised_by_you']} raised by you  ·  {c['older_than_3_days']} older than 3 days")
        self._filter()

    def _filter(self) -> None:
        rows = self.items
        if self.kind.currentData():
            rows = [i for i in rows if i["type"] == self.kind.currentData()]
        m = self.mine.currentData()
        if m == "mine":
            rows = [i for i in rows if i["own"]]
        elif m == "others":
            rows = [i for i in rows if not i["own"]]
        self.table.set_rows(rows)
        if rows:
            self.panel.show_content()
        else:
            self.panel.show_empty("Nothing is waiting for your approval." if not self.items else "No requests match this filter.")

    def _open(self) -> None:
        row = self.table.selected_row()
        if not row:
            return
        if row["own"]:
            self.status.show_message("You raised this request, so someone else has to decide it. You can still open it to review or withdraw it.", "info")
        self.navigate_requested.emit(row["screen"])
