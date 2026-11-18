from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from desktop.pages.base import Page
from desktop.ui.widgets import Banner, Column, DataTable, PageHeader, StatePanel, bool_status, hr, label
from desktop.ui.worker import run_async


def _kpi(title: str, value: str) -> tuple[QWidget, "QLabel"]:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 24, 0)
    lay.setSpacing(0)
    lay.addWidget(label(title, "fieldLabel"))
    v = label(value, "kpiValue")
    lay.addWidget(v)
    return w, v


def _when(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d %b %H:%M")
    except ValueError:
        return value


class OverviewPage(Page):
    """Foundation-phase overview: only figures the backend can currently supply are shown.
    Sales, stock value, receivables and profit appear once their modules exist (no placeholders)."""

    title = "Overview"

    def __init__(self, session):
        super().__init__(session)
        self.root.addWidget(PageHeader("Overview", "Locations and recent administrative activity"))

        self.kpi_row = QHBoxLayout()
        self.kpi_row.setSpacing(0)
        k1, self.k_locations = _kpi("Locations in scope", "-")
        k2, self.k_cities = _kpi("Cities", "-")
        k3, self.k_users = _kpi("User accounts", "-")
        for k in (k1, k2, k3):
            self.kpi_row.addWidget(k)
        self.kpi_row.addStretch()
        self.root.addLayout(self.kpi_row)
        self.root.addWidget(hr())

        self.note = Banner()
        self.note.show_message(
            "Sales, stock, receivables and profit are not shown yet: those modules are built in later phases, "
            "and this screen never displays figures the backend cannot supply.", "info")
        self.root.addWidget(self.note)

        cols = QHBoxLayout()
        cols.setSpacing(24)

        left = QVBoxLayout()
        left.addWidget(label("Locations", "sectionTitle"))
        self.loc_table = DataTable([
            Column("code", "Code", 90), Column("name", "Name"), Column("city", "City", 130),
            Column("location_type", "Type", 90, fmt=lambda v: str(v).capitalize()),
            Column("is_active", "Status", 100, status=bool_status, fmt=lambda v: "Active" if v else "Inactive"),
        ])
        self.loc_panel = StatePanel(self.loc_table, "No locations have been set up yet. Add them under Settings › Locations.")
        self.loc_panel.retry.connect(self.refresh)
        left.addWidget(self.loc_panel, 1)
        cols.addLayout(left, 3)

        right = QVBoxLayout()
        right.addWidget(label("Recent activity", "sectionTitle"))
        self.audit_table = DataTable([
            Column("created_at", "When", 110, fmt=_when), Column("actor_username", "By", 110),
            Column("action", "Action"),
        ])
        self.audit_panel = StatePanel(self.audit_table, "No activity recorded yet.")
        self.audit_panel.retry.connect(self.refresh)
        right.addWidget(self.audit_panel, 1)
        cols.addLayout(right, 2)
        self.root.addLayout(cols, 1)

    def refresh(self) -> None:
        self.loc_panel.show_loading()
        s = self.session

        def work():
            locs = s.client.get("/locations", include_inactive=True) if s.can("locations.manage") else s.client.get("/locations")
            users = s.client.get("/users", page_size=1)["total"] if s.can("users.view") else None
            audit = s.client.get("/audit", page_size=12)["items"] if s.can("audit.view") else None
            return locs, users, audit

        run_async(work, self._loaded, lambda e: self.loc_panel.show_error(e.message, e.offline))

    def _loaded(self, result) -> None:
        locs, users, audit = result
        sel = self.session.current_location_id
        shown = [l for l in locs if sel is None or l["id"] == sel]
        self.k_locations.setText(str(len([l for l in shown if l["is_active"]])))
        self.k_cities.setText(str(len({l["city"] for l in shown if l["is_active"]})))
        self.k_users.setText("-" if users is None else str(users))
        self.loc_table.set_rows(shown)
        self.loc_panel.show_content() if shown else self.loc_panel.show_empty()
        if audit is None:
            self.audit_table.set_rows([])
            self.audit_panel.show_empty("Activity history is visible to roles with audit access.")
        else:
            self.audit_table.set_rows(audit)
            self.audit_panel.show_content() if audit else self.audit_panel.show_empty()
