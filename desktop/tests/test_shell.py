import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from backend.permissions import DEFAULT_ROLES  # noqa: E402
from desktop.api.client import ApiClient  # noqa: E402
from desktop.main import build_app  # noqa: E402
from desktop.session import Session  # noqa: E402
from desktop.ui.shell import Shell  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return build_app()


def session_for(role: str) -> Session:
    spec = DEFAULT_ROLES[role]
    me = {
        "user": {"full_name": "T", "role_name": spec["name"], "username": "t"},
        "permissions": sorted(spec["permissions"]), "all_locations": spec["scope_all_locations"],
        "locations": [{"id": 1, "code": "A", "name": "Alpha", "city": "X"}], "currency": "PKR", "environment": "development",
    }
    return Session(ApiClient("http://127.0.0.1:9"), me=me)


def nav(shell: Shell) -> set[str]:
    return set(shell.buttons)


@pytest.mark.parametrize("role, present, absent", [
    ("ceo", {"overview", "accounting", "attendance", "settings", "distributors", "reports", "approvals"}, set()),
    ("order_booker", {"overview", "orders", "customers", "products", "inventory"},
     {"accounting", "attendance", "distributors", "settings", "employees", "reports", "approvals"}),
    ("stock_keeper", {"inventory", "transfers", "distributors", "orders", "employees"}, {"accounting", "attendance", "settings", "customers"}),
    ("accounts", {"accounting", "reports", "employees"}, {"attendance", "settings"}),
    ("manager", {"employees", "attendance"}, set()),
])
def test_navigation_follows_server_permissions(app, role, present, absent):
    shell = Shell(session_for(role))
    items = nav(shell)
    assert present <= items
    assert not (absent & items)


def test_unreachable_backend_shows_error_state_not_fabricated_data(app):
    import time

    shell = Shell(session_for("ceo"))
    page = shell.pages["overview"]
    end = time.time() + 5
    while time.time() < end and page.panel.stack.currentIndex() == page.panel.LOADING:
        QApplication.processEvents()
        time.sleep(0.02)
    assert page.panel.stack.currentIndex() == page.panel.ERROR
    assert page.body_lay.count() == 0                      # no figure is drawn when the server cannot supply it
    assert "Server unreachable" in page.panel._err_title.text()


def test_location_selector_hidden_choice_for_single_location_user(app):
    shell = Shell(session_for("order_booker"))
    assert shell.location_combo.count() == 1
    assert not shell.location_combo.isEnabled()


def test_employees_page_shows_only_the_tabs_a_role_may_use(app):
    from desktop.pages.employees import EmployeesPage
    names = lambda role: [t for t, _ in EmployeesPage(session_for(role)).items]  # noqa: E731
    assert names("stock_keeper") == ["Purchases"]                                  # the counter only: no personnel records
    assert names("accounts") == ["Purchases", "Discounts and credit"]
    assert names("manager") == ["Employees", "Purchases", "Discounts and credit"]


def test_dashboard_reports_and_approvals_pages_are_real_screens_not_placeholders(app):
    from desktop.pages.approvals import ApprovalsPage
    from desktop.pages.dashboard import DashboardPage
    from desktop.pages.reports import ReportsPage
    shell = Shell(session_for("ceo"))
    assert isinstance(shell._page_for("overview"), DashboardPage)
    assert isinstance(shell._page_for("reports"), ReportsPage)
    assert isinstance(shell._page_for("approvals"), ApprovalsPage)
    booker = Shell(session_for("order_booker"))
    assert isinstance(booker._page_for("overview"), DashboardPage)


def test_alerts_and_inbox_only_open_screens_the_user_can_reach(app):
    shell = Shell(session_for("order_booker"))
    shell._navigate("accounting")                       # not in this user's navigation: ignored
    assert shell.header_title.text() != "Accounting"
