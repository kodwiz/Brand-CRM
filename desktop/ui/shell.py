from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QHBoxLayout, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from desktop.pages.base import Page
from desktop.pages.inventory import InventoryPage
from desktop.pages.overview import OverviewPage
from desktop.pages.approvals import ApprovalsPage
from desktop.pages.dashboard import DashboardPage
from desktop.pages.reports import ReportsPage
from desktop.pages.accounting import AccountingPage
from desktop.pages.attendance import AttendancePage
from desktop.pages.employees import EmployeesPage
from desktop.pages.placeholder import NotBuiltPage
from desktop.pages.sales import CustomersPage, OrdersPage
from desktop.pages.distributors import DistributorsPage
from desktop.pages.suppliers import SuppliersPage
from desktop.pages.products import ProductsPage
from desktop.pages.settings import SettingsPage
from desktop.pages.transfers import TransfersPage
from desktop.session import Session
from desktop.ui import tokens as T
from desktop.ui.login import ChangePasswordDialog
from desktop.ui.widgets import button, label

# key, label, group, visible-if-any-of permissions, phase, one-line description (for modules not yet built)
MODULES = [
    ("overview", "Dashboard", None, None, None, None),
    ("orders", "Orders", "Sales and stock", ("orders.view_own", "orders.view_all", "orders.create", "orders.fulfil"), "Phase 4",
     "Manual order entry, pricing, stock reservation, fulfilment and returns."),
    ("customers", "Customers", "Sales and stock", ("customers.view",), "Phase 4", "Customer records and order history."),
    ("products", "Products", "Sales and stock", ("products.view",), "Phase 2", "Catalogue, variants and shades, pricing and product images."),
    ("inventory", "Inventory", "Sales and stock", ("inventory.view",), "Phase 2", "Stock by location and batch, receipts, counts and adjustments."),
    ("transfers", "Transfers", "Sales and stock", ("transfers.view",), "Phase 2", "Requested → approved → dispatched → received stock transfers."),
    ("suppliers", "Suppliers", "Commercial", ("suppliers.view", "purchasing.view"), "Phase 3", "Purchase orders, goods receipts, supplier invoices and payments."),
    ("distributors", "Distributors", "Commercial", ("distributors.view",), "Phase 5", "Credit accounts, dispatches, returns, payments and statements."),
    ("accounting", "Accounting", "Commercial", ("accounting.view",), "Phase 6", "Double-entry ledger, statements, reconciliation and period close."),
    ("employees", "Employees", "People", ("employees.view", "employees.purchase", "employees.purchase_view"), "Phase 7", "Employee records, purchases and performance."),
    ("attendance", "Attendance", "People", ("attendance.view",), "Phase 7", "Biometric punch import, daily summaries and exceptions."),
    ("reports", "Reports", "Insight", ("reports.operational", "reports.financial", "reports.summaries", "attendance.view", "audit.view"), "Phase 8", "Filterable reports with PDF and Excel export."),
    ("approvals", "Approvals", "Insight", ("approvals.view",), "Phase 8", "Pending approvals for exceptional discounts, write-offs and adjustments."),
    ("settings", "Settings", "System", ("users.view", "locations.manage", "roles.manage", "audit.view"), None, None),
]


class Shell(QWidget):
    sign_out_requested = Signal()

    def __init__(self, session: Session, parent: QWidget | None = None):
        super().__init__(parent)
        self.session = session
        self.setObjectName("workspace")
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        right.addWidget(self._build_header())
        self.stack = QStackedWidget()
        right.addWidget(self.stack, 1)
        wrap = QWidget()
        wrap.setLayout(right)
        root.addWidget(wrap, 1)

        self.pages: dict[str, Page] = {}
        self.buttons: dict[str, QPushButton] = {}
        self._populate_nav()
        first = "overview"
        self.buttons[first].setChecked(True)
        self.show_page(first)

    # -- construction ----------------------------------------------------------------------
    def _build_sidebar(self) -> QWidget:
        sb = QWidget()
        sb.setObjectName("sidebar")
        sb.setFixedWidth(T.SIDEBAR_WIDTH)
        self.nav_layout = QVBoxLayout(sb)
        self.nav_layout.setContentsMargins(0, 16, 0, 12)
        self.nav_layout.setSpacing(0)
        self.nav_layout.addWidget(label("OPERATIONS", "brand"))
        self.nav_layout.addWidget(label("Cosmetics management", "brandSub"))
        self.nav_layout.addSpacing(8)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        return sb

    def _populate_nav(self) -> None:
        last_group = object()
        for key, title, group, perms, phase, desc in MODULES:
            if perms and not self.session.can(*perms):
                continue
            if group != last_group and group:
                self.nav_layout.addWidget(label(group.upper(), "navGroup"))
            last_group = group
            b = QPushButton(title)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.show_page(k))
            self.group.addButton(b)
            self.nav_layout.addWidget(b)
            self.buttons[key] = b
            if key == "overview":
                self.nav_layout.addSpacing(4)
        self.nav_layout.addStretch()
        u = self.session.user
        self.nav_layout.addWidget(label(u["full_name"], "userName"))
        self.nav_layout.addWidget(label(u["role_name"], "userRole"))
        self.nav_layout.addSpacing(4)
        pw = button("Change password", "linkButton", self._change_password)
        out = button("Sign out", "linkButton", self.sign_out_requested.emit)
        self.nav_layout.addWidget(pw)
        self.nav_layout.addWidget(out)

    def _build_header(self) -> QWidget:
        h = QWidget()
        h.setObjectName("header")
        h.setFixedHeight(T.HEADER_HEIGHT)
        lay = QHBoxLayout(h)
        lay.setContentsMargins(24, 0, 24, 0)
        self.header_title = label("", "headerTitle")
        lay.addWidget(self.header_title)
        if self.session.environment != "production":
            badge = label(self.session.environment.upper(), "envBadge")
            badge.setToolTip("This is not the production environment. Do not enter real company records here.")
            lay.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addStretch()
        lay.addWidget(label("Location", "muted"))
        self.location_combo = QComboBox()
        self.location_combo.setMinimumWidth(220)
        locs = self.session.locations
        if self.session.me.get("all_locations") or len(locs) != 1:
            self.location_combo.addItem("All locations" if self.session.me.get("all_locations") else "All my locations", None)
        for l in locs:
            self.location_combo.addItem(f"{l['name']}  ({l['city']})", l["id"])
        self.location_combo.setEnabled(self.location_combo.count() > 1)
        self.location_combo.currentIndexChanged.connect(self._scope_changed)
        lay.addWidget(self.location_combo)
        return h

    # -- behaviour -------------------------------------------------------------------------
    def _page_for(self, key: str) -> Page:
        if key not in self.pages:
            spec = next(m for m in MODULES if m[0] == key)
            if key == "overview":
                page: Page = DashboardPage(self.session) if self.session.can("reports.operational", "reports.financial", "reports.summaries", "orders.view_own") else OverviewPage(self.session)
            elif key == "settings":
                page = SettingsPage(self.session)
            elif key == "products":
                page = ProductsPage(self.session)
            elif key == "inventory":
                page = InventoryPage(self.session)
            elif key == "suppliers":
                page = SuppliersPage(self.session)
            elif key == "orders":
                page = OrdersPage(self.session)
            elif key == "customers":
                page = CustomersPage(self.session)
            elif key == "distributors":
                page = DistributorsPage(self.session)
            elif key == "accounting":
                page = AccountingPage(self.session)
            elif key == "transfers":
                page = TransfersPage(self.session)
            elif key == "employees":
                page = EmployeesPage(self.session)
            elif key == "attendance":
                page = AttendancePage(self.session)
            elif key == "reports":
                page = ReportsPage(self.session)
            elif key == "approvals":
                page = ApprovalsPage(self.session)
            else:
                page = NotBuiltPage(self.session, spec[1], spec[4], spec[5])
            page.navigate_requested.connect(self._navigate)
            self.pages[key] = page
            self.stack.addWidget(page)
        return self.pages[key]

    def _navigate(self, key: str) -> None:
        if key in self.buttons:                      # only modules this user can open
            self.show_page(key)

    def show_page(self, key: str) -> None:
        page = self._page_for(key)
        if key in self.buttons:
            self.buttons[key].setChecked(True)
        self.stack.setCurrentWidget(page)
        self.header_title.setText(next(m[1] for m in MODULES if m[0] == key))
        page.refresh()

    def _scope_changed(self) -> None:
        self.session.current_location_id = self.location_combo.currentData()
        cur = self.stack.currentWidget()
        if isinstance(cur, Page):
            cur.scope_changed()

    def _change_password(self) -> None:
        if ChangePasswordDialog(self.session, parent=self).exec():
            self.sign_out_requested.emit()  # server revoked every session on password change
