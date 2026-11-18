from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
    QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from desktop.pages.base import Page
from desktop.session import Session
from desktop.ui.forms import FormDialog, FormGrid, password_edit
from desktop.ui.widgets import Banner, Column, DataTable, PageHeader, StatePanel, bool_status, button, label
from desktop.ui.worker import run_async


def _when(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d %b %Y %H:%M")
    except ValueError:
        return value


# --------------------------------------------------------------------------------------- dialogs
class UserDialog(FormDialog):
    def __init__(self, session: Session, roles: list[dict], locations: list[dict], user: dict | None, parent=None):
        super().__init__("New user" if user is None else f"Edit {user['username']}", "Create user" if user is None else "Save changes", parent, 520)
        self.session, self.user, self.roles = session, user, {r["id"]: r for r in roles}
        self.form = FormGrid(columns=2)
        self.username = QLineEdit()
        self.full_name = QLineEdit()
        self.email = QLineEdit()
        self.phone = QLineEdit()
        self.role = QComboBox()
        if user is None:  # never preselect a role (least privilege): the admin must choose deliberately
            self.role.addItem("Select a role\u2026", None)
        for r in roles:
            if r["is_active"]:
                self.role.addItem(r["name"], r["id"])
        self.form.add("username", "Username", self.username)
        self.form.add("full_name", "Full name", self.full_name)
        self.form.add("email", "Email", self.email)
        self.form.add("phone", "Phone", self.phone)
        self.form.add("role", "Role", self.role)
        self.password = None
        self.active = None
        if user is None:
            self.password = password_edit()
            self.form.add("password", "Temporary password", self.password)
        else:
            self.username.setText(user["username"])
            self.username.setEnabled(False)
            self.full_name.setText(user["full_name"])
            self.email.setText(user.get("email") or "")
            self.phone.setText(user.get("phone") or "")
            i = self.role.findData(next((r["id"] for r in roles if r["code"] == user["role_code"]), None))
            self.role.setCurrentIndex(max(i, 0))
            self.active = QCheckBox("Account is active")
            self.active.setChecked(user["is_active"])
            self.form.add("active", "Status", self.active)
        self.body.addWidget(self.form)

        self.loc_label = label("Locations (required for roles limited to assigned locations)", "fieldLabel")
        self.body.addWidget(self.loc_label)
        self.loc_list = QListWidget()
        self.loc_list.setMaximumHeight(130)
        for l in locations:
            it = QListWidgetItem(f"{l['code']}  ·  {l['name']}")
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if user and l["id"] in user["location_ids"] else Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, l["id"])
            self.loc_list.addItem(it)
        self.body.addWidget(self.loc_list)
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("Reason for change (recorded in the audit log)")
        if user is not None:
            self.body.addWidget(self.reason)
        self.role.currentIndexChanged.connect(self._role_changed)
        self._role_changed()

    def _role_changed(self) -> None:
        r = self.roles.get(self.role.currentData())
        scoped = bool(r) and not r["scope_all_locations"]
        self.loc_list.setEnabled(scoped)
        self.loc_label.setText("Select a role to assign locations" if r is None
                               else "Assigned locations" if scoped else "This role sees every location")

    def _ids(self) -> list[int]:
        if not self.loc_list.isEnabled():
            return []
        return [self.loc_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.loc_list.count())
                if self.loc_list.item(i).checkState() == Qt.CheckState.Checked]

    def submit(self) -> None:
        self.form.clear_errors()
        if self.role.currentData() is None:
            self.form.set_error("role", "Choose a role.")
            return self.reenable()
        c = self.session.client
        reason = self.reason.text().strip() or None

        def work():
            if self.user is None:
                return c.post("/users", {
                    "username": self.username.text().strip(), "full_name": self.full_name.text().strip(),
                    "password": self.password.text(), "role_id": self.role.currentData(),
                    "email": self.email.text().strip() or None, "phone": self.phone.text().strip() or None,
                    "location_ids": self._ids()})
            uid = self.user["id"]
            c.patch(f"/users/{uid}", {"full_name": self.full_name.text().strip(), "email": self.email.text().strip() or None,
                                      "phone": self.phone.text().strip() or None, "role_id": self.role.currentData(),
                                      "is_active": self.active.isChecked(), "reason": reason})
            return c.put(f"/users/{uid}/locations", {"location_ids": self._ids(), "reason": reason})

        run_async(work, lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class LocationDialog(FormDialog):
    TYPES = [("Branch", "branch"), ("Warehouse", "warehouse"), ("Warehouse and branch", "both")]

    def __init__(self, session: Session, loc: dict | None, parent=None):
        super().__init__("New location" if loc is None else f"Edit {loc['code']}", "Create location" if loc is None else "Save changes", parent)
        self.session, self.loc = session, loc
        self.form = FormGrid(columns=2)
        self.code, self.name, self.city = QLineEdit(), QLineEdit(), QLineEdit()
        self.type = QComboBox()
        for t, v in self.TYPES:
            self.type.addItem(t, v)
        self.address = QPlainTextEdit()
        self.address.setFixedHeight(60)
        self.active = QCheckBox("Location is active")
        self.active.setChecked(True)
        self.form.add("code", "Code", self.code)
        self.form.add("name", "Name", self.name)
        self.form.add("city", "City", self.city)
        self.form.add("type", "Type", self.type)
        self.form.add("address", "Address", self.address, span=2)
        if loc is not None:
            self.code.setText(loc["code"])
            self.code.setEnabled(False)
            self.name.setText(loc["name"])
            self.city.setText(loc["city"])
            self.type.setCurrentIndex(max(self.type.findData(loc["location_type"]), 0))
            self.address.setPlainText(loc.get("address") or "")
            self.active.setChecked(loc["is_active"])
            self.form.add("active", "Status", self.active)
        self.body.addWidget(self.form)
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("Reason for change (recorded in the audit log)")
        if loc is not None:
            self.body.addWidget(self.reason)

    def submit(self) -> None:
        c = self.session.client
        payload = {"name": self.name.text().strip(), "city": self.city.text().strip(),
                   "address": self.address.toPlainText().strip() or None, "location_type": self.type.currentData()}
        if self.loc is None:
            run_async(lambda: c.post("/locations", {"code": self.code.text().strip(), **payload}),
                      lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))
        else:
            payload.update(is_active=self.active.isChecked(), reason=self.reason.text().strip() or None)
            run_async(lambda: c.patch(f"/locations/{self.loc['id']}", payload),
                      lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


class ResetPasswordDialog(FormDialog):
    def __init__(self, session: Session, user: dict, parent=None):
        super().__init__(f"Reset password for {user['username']}", "Reset password", parent)
        self.session, self.user = session, user
        self.form = FormGrid()
        self.pw = password_edit()
        self.form.add("pw", "Temporary password", self.pw)
        self.body.addWidget(self.form)
        self.body.addWidget(label("The user is signed out everywhere and must choose a new password at next sign-in.", "muted", wrap=True))

    def submit(self) -> None:
        c = self.session.client
        run_async(lambda: c.post(f"/users/{self.user['id']}/reset-password", {"new_password": self.pw.text()}),
                  lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))


# --------------------------------------------------------------------------------------- tabs
class _Tab(QWidget):
    def __init__(self, session: Session):
        super().__init__()
        self.session = session
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 14, 0, 0)
        self.lay.setSpacing(10)
        self.status = Banner()
        self.lay.addWidget(self.status)

    def refresh(self) -> None:
        pass


class UsersTab(_Tab):
    def __init__(self, session):
        super().__init__(session)
        self.roles: list[dict] = []
        self.locations: list[dict] = []
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search name or username")
        self.search.setMaximumWidth(280)
        self.search.returnPressed.connect(self.refresh)
        bar.addWidget(self.search)
        bar.addStretch()
        self.reset_btn = button("Reset password", on_click=self._reset)
        self.edit_btn = button("Edit", on_click=self._edit)
        self.new_btn = button("New user", "primary", self._new)
        can = session.can("users.manage")
        for b in (self.reset_btn, self.edit_btn, self.new_btn):
            b.setVisible(can)
            bar.addWidget(b)
        self.lay.addLayout(bar)
        self.table = DataTable([
            Column("username", "Username", 150), Column("full_name", "Name"), Column("role_name", "Role", 140),
            Column("location_ids", "Locations", 100, fmt=lambda v: "All" if v == [] else str(len(v))),
            Column("last_login_at", "Last sign-in", 150, fmt=_when),
            Column("is_active", "Status", 100, status=bool_status, fmt=lambda v: "Active" if v else "Inactive"),
        ])
        self.table.row_activated.connect(lambda _: self._edit())
        self.panel = StatePanel(self.table, "No users match.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)

    def refresh(self) -> None:
        self.panel.show_loading()
        c, s = self.session.client, self.session
        q = self.search.text().strip() or None

        def work():
            users = c.get("/users", page_size=200, q=q)["items"]
            roles = c.get("/roles")
            locs = c.get("/locations", include_inactive=True)
            return users, roles, locs

        def done(r):
            users, self.roles, self.locations = r
            for u in users:  # "All" label: roles that see every location have no assignment rows
                role = next((x for x in self.roles if x["code"] == u["role_code"]), None)
                if role and role["scope_all_locations"]:
                    u["location_ids"] = []
            self.table.set_rows(users)
            self.panel.show_content() if users else self.panel.show_empty()

        run_async(work, done, lambda e: self.panel.show_error(e.message, e.offline))

    def _new(self) -> None:
        if UserDialog(self.session, self.roles, [l for l in self.locations if l["is_active"]], None, self).exec():
            self.status.show_message("User created. They must set a new password at first sign-in.", "ok")
            self.refresh()

    def _edit(self) -> None:
        row = self.table.selected_row()
        if not row or not self.session.can("users.manage"):
            return
        if UserDialog(self.session, self.roles, [l for l in self.locations if l["is_active"]], row, self).exec():
            self.status.show_message("User updated.", "ok")
            self.refresh()

    def _reset(self) -> None:
        row = self.table.selected_row()
        if row and ResetPasswordDialog(self.session, row, self).exec():
            self.status.show_message(f"Password reset for {row['username']}.", "ok")


class LocationsTab(_Tab):
    def __init__(self, session):
        super().__init__(session)
        bar = QHBoxLayout()
        bar.addStretch()
        can = session.can("locations.manage")
        self.edit_btn = button("Edit", on_click=self._edit)
        self.new_btn = button("New location", "primary", self._new)
        for b in (self.edit_btn, self.new_btn):
            b.setVisible(can)
            bar.addWidget(b)
        self.lay.addLayout(bar)
        self.table = DataTable([
            Column("code", "Code", 100), Column("name", "Name", 280), Column("city", "City", 140),
            Column("location_type", "Type", 160, fmt=lambda v: {"both": "Warehouse and branch"}.get(v, str(v).capitalize())),
            Column("address", "Address"),
            Column("is_active", "Status", 100, status=bool_status, fmt=lambda v: "Active" if v else "Inactive"),
        ])
        self.table.row_activated.connect(lambda _: self._edit())
        self.panel = StatePanel(self.table, "No locations yet. Create the first one with New location.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client

        def done(rows):
            self.table.set_rows(rows)
            self.panel.show_content() if rows else self.panel.show_empty()

        run_async(lambda: c.get("/locations", include_inactive=True), done, lambda e: self.panel.show_error(e.message, e.offline))

    def _new(self) -> None:
        if LocationDialog(self.session, None, self).exec():
            self.status.show_message("Location created.", "ok")
            self.refresh()

    def _edit(self) -> None:
        row = self.table.selected_row()
        if row and self.session.can("locations.manage") and LocationDialog(self.session, row, self).exec():
            self.status.show_message("Location updated.", "ok")
            self.refresh()


class RolesTab(_Tab):
    def __init__(self, session):
        super().__init__(session)
        self.roles: list[dict] = []
        self.catalogue: dict[str, str] = {}
        body = QHBoxLayout()
        body.setSpacing(20)
        left = QVBoxLayout()
        left.addWidget(label("Roles", "sectionTitle"))
        self.role_list = QListWidget()
        self.role_list.setMaximumWidth(240)
        self.role_list.currentRowChanged.connect(self._show_role)
        left.addWidget(self.role_list, 1)
        body.addLayout(left)

        right = QVBoxLayout()
        self.role_desc = label("", "muted", wrap=True)
        right.addWidget(self.role_desc)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.perm_host = QWidget()
        self.perm_lay = QVBoxLayout(self.perm_host)
        self.perm_lay.setContentsMargins(0, 0, 0, 0)
        self.scroll.setWidget(self.perm_host)
        right.addWidget(self.scroll, 1)
        foot = QHBoxLayout()
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("Reason for change (recorded in the audit log)")
        self.save_btn = button("Save permissions", "primary", self._save)
        foot.addWidget(self.reason, 1)
        foot.addWidget(self.save_btn)
        right.addLayout(foot)
        body.addLayout(right, 1)
        self.lay.addLayout(body, 1)
        self.checks: dict[str, QCheckBox] = {}

    def refresh(self) -> None:
        c = self.session.client

        def work():
            return c.get("/roles"), c.get("/roles/permission-catalogue")

        def done(r):
            self.roles, self.catalogue = r
            self.role_list.blockSignals(True)
            self.role_list.clear()
            for role in self.roles:
                self.role_list.addItem(role["name"])
            self.role_list.blockSignals(False)
            if self.roles:
                self.role_list.setCurrentRow(0)
                self._show_role(0)

        run_async(work, done, lambda e: self.status.show_message(e.message, "error"))

    def _show_role(self, row: int) -> None:
        if not (0 <= row < len(self.roles)):
            return
        role = self.roles[row]
        scope = "All locations" if role["scope_all_locations"] else "Assigned locations only"
        self.role_desc.setText(f"{role['description'] or ''}  ·  {scope}")
        while self.perm_lay.count():
            it = self.perm_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.checks = {}
        groups: dict[str, list[str]] = {}
        for code in self.catalogue:
            groups.setdefault(code.split(".")[0], []).append(code)
        held = set(role["permissions"])
        can = self.session.can("roles.manage")
        for g, codes in groups.items():
            self.perm_lay.addWidget(label(g.replace("_", " ").title(), "fieldLabel"))
            for code in codes:
                cb = QCheckBox(f"{self.catalogue[code]}")
                cb.setToolTip(code)
                cb.setChecked(code in held)
                cb.setEnabled(can)
                self.checks[code] = cb
                self.perm_lay.addWidget(cb)
        self.perm_lay.addStretch()
        self.save_btn.setEnabled(can)

    def _save(self) -> None:
        row = self.role_list.currentRow()
        if row < 0:
            return
        role = self.roles[row]
        perms = [c for c, cb in self.checks.items() if cb.isChecked()]
        self.save_btn.setEnabled(False)
        c = self.session.client

        def done(_):
            self.status.show_message(f"Permissions saved for {role['name']}. Affected users pick them up on next request.", "ok")
            self.reason.clear()
            self.refresh()

        def fail(e):
            self.save_btn.setEnabled(True)
            self.status.show_message(e.message, "error")

        run_async(lambda: c.put(f"/roles/{role['id']}/permissions", {"permissions": perms, "reason": self.reason.text().strip() or None}), done, fail)


class AuditTab(_Tab):
    PAGE = 50

    def __init__(self, session):
        super().__init__(session)
        self.page = 1
        bar = QHBoxLayout()
        self.entity = QComboBox()
        self.entity.addItem("All records", None)
        for e in ("user", "location", "role"):
            self.entity.addItem(e.capitalize(), e)
        self.action = QLineEdit()
        self.action.setPlaceholderText("Action, e.g. user.updated")
        self.action.setMaximumWidth(240)
        self.entity.currentIndexChanged.connect(self._reset_page)
        self.action.returnPressed.connect(self._reset_page)
        bar.addWidget(self.entity)
        bar.addWidget(self.action)
        bar.addStretch()
        self.count = label("", "muted")
        self.prev_btn = button("Previous", on_click=lambda: self._go(-1))
        self.next_btn = button("Next", on_click=lambda: self._go(1))
        for w in (self.count, self.prev_btn, self.next_btn):
            bar.addWidget(w)
        self.lay.addLayout(bar)
        self.table = DataTable([
            Column("created_at", "When", 150, fmt=_when), Column("actor_username", "Actor", 120),
            Column("action", "Action", 190), Column("entity_type", "Record", 80),
            Column("entity_id", "ID", 60), Column("reason", "Reason"),
        ])
        self.table.row_activated.connect(self._detail)
        self.panel = StatePanel(self.table, "No audit entries match.")
        self.panel.retry.connect(self.refresh)
        self.lay.addWidget(self.panel, 1)
        self.lay.addWidget(label("Double-click an entry to see before / after values. Audit entries cannot be edited or deleted.", "muted"))

    def _reset_page(self) -> None:
        self.page = 1
        self.refresh()

    def _go(self, d: int) -> None:
        self.page = max(1, self.page + d)
        self.refresh()

    def refresh(self) -> None:
        self.panel.show_loading()
        c = self.session.client
        act = self.action.text().strip() or None
        et = self.entity.currentData()

        def done(r):
            self.table.set_rows(r["items"])
            total = r["total"]
            last = max(1, -(-total // self.PAGE))
            self.count.setText(f"{total} {'entry' if total == 1 else 'entries'}  ·  page {self.page} of {last}")
            self.prev_btn.setEnabled(self.page > 1)
            self.next_btn.setEnabled(self.page < last)
            self.panel.show_content() if r["items"] else self.panel.show_empty()

        run_async(lambda: c.get("/audit", page=self.page, page_size=self.PAGE, action=act, entity_type=et), done,
                  lambda e: self.panel.show_error(e.message, e.offline))

    def _detail(self, row: dict) -> None:
        import json

        d = FormDialog(f"{row['action']}  #{row['id']}", "Close", self, 620)
        d.cancel_btn.setVisible(False)
        d.submit_btn.clicked.disconnect()
        d.submit_btn.clicked.connect(d.accept)
        txt = QPlainTextEdit()
        txt.setReadOnly(True)
        txt.setMinimumHeight(220)
        txt.setPlainText(json.dumps({"before": row.get("before"), "after": row.get("after"), "reason": row.get("reason"),
                                     "ip": row.get("ip_address")}, indent=2, ensure_ascii=False))
        d.body.addWidget(txt)
        d.exec()


class SettingsPage(Page):
    title = "Settings"

    def __init__(self, session):
        super().__init__(session)
        self.root.addWidget(PageHeader("Settings", "Users, locations, roles and audit history"))
        self.tabs = QTabWidget()
        self._tabs: list[_Tab] = []
        spec = [("Users", UsersTab, "users.view"), ("Locations", LocationsTab, "locations.view"),
                ("Roles and permissions", RolesTab, "roles.manage"), ("Audit log", AuditTab, "audit.view")]
        for name, cls, perm in spec:
            if session.can(perm):
                t = cls(session)
                self._tabs.append(t)
                self.tabs.addTab(t, name)
        self.tabs.currentChanged.connect(lambda _: self.refresh())
        self.root.addWidget(self.tabs, 1)

    def refresh(self) -> None:
        w = self.tabs.currentWidget()
        if isinstance(w, _Tab):
            w.refresh()
