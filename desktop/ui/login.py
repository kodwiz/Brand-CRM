from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLineEdit, QVBoxLayout, QWidget

from desktop.api.client import ApiClient, ApiError
from desktop.session import Session
from desktop.ui.forms import FormDialog, FormGrid, mark_invalid, password_edit
from desktop.ui.widgets import Banner, button, label
from desktop.ui.worker import run_async


class LoginView(QWidget):
    """Sign-in. Messages are shown beside the form; nothing is claimed until the server confirms."""

    signed_in = Signal(object)  # Session

    def __init__(self, client: ApiClient, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("workspace")
        self.client = client
        outer = QVBoxLayout(self)
        outer.addStretch(2)

        panel = QWidget()
        panel.setObjectName("surface")
        panel.setFixedWidth(380)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(28, 26, 28, 24)
        lay.setSpacing(6)
        lay.addWidget(label("Operations", "pageTitle"))
        lay.addWidget(label("Sign in to continue", "muted"))
        lay.addSpacing(10)

        self.form = FormGrid()
        self.username = QLineEdit()
        self.username.setPlaceholderText("Username")
        self.password = password_edit()
        self.form.add("username", "Username", self.username)
        self.form.add("password", "Password", self.password)
        lay.addWidget(self.form)
        self.banner = Banner()
        lay.addWidget(self.banner)
        self.submit = button("Sign in", "primary", self._attempt)
        self.submit.setDefault(True)
        lay.addSpacing(6)
        lay.addWidget(self.submit)
        lay.addSpacing(6)
        lay.addWidget(label(f"Server: {client.base_url}", "muted"))

        outer.addWidget(panel, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(3)
        self.password.returnPressed.connect(self._attempt)
        self.username.returnPressed.connect(self.password.setFocus)

    def reset(self) -> None:
        self.password.clear()
        self.banner.clear()
        self.submit.setEnabled(True)
        self.username.setFocus()

    def _attempt(self) -> None:
        if not self.submit.isEnabled():
            return
        self.form.clear_errors()
        self.banner.clear()
        u, p = self.username.text().strip(), self.password.text()
        bad = False
        for key, widget, value, msg in (("username", self.username, u, "Enter your username."),
                                         ("password", self.password, p, "Enter your password.")):
            mark_invalid(widget, not value)
            if not value:
                self.form.set_error(key, msg)
                bad = True
        if bad:
            return
        self.submit.setEnabled(False)
        self.submit.setText("Signing in…")

        def work():
            data = self.client.login(u, p)
            s = Session(self.client)
            s.load_me()
            return s, data

        run_async(work, self._ok, self._fail)

    def _ok(self, result) -> None:
        session, data = result
        self.submit.setText("Sign in")
        self.password.clear()
        if data.get("must_change_password"):
            dlg = ChangePasswordDialog(session, forced=True, parent=self)
            if dlg.exec() != dlg.DialogCode.Accepted:
                self.client.logout()
                self.submit.setEnabled(True)
                self.banner.show_message("You must set a new password before continuing.", "info")
                return
            # the password change revokes existing sessions, so sign in again silently is not possible
            self.client._token = None
            self.banner.show_message("Password changed. Sign in with your new password.", "ok")
            self.submit.setEnabled(True)
            return
        self.signed_in.emit(session)

    def _fail(self, err: ApiError) -> None:
        self.submit.setEnabled(True)
        self.submit.setText("Sign in")
        self.banner.show_message(err.message, "error")


class ChangePasswordDialog(FormDialog):
    def __init__(self, session: Session, forced: bool = False, parent: QWidget | None = None):
        super().__init__("Set a new password" if forced else "Change password", "Change password", parent)
        self.session = session
        self.form = FormGrid()
        self.current = password_edit()
        self.new = password_edit()
        self.repeat = password_edit()
        self.form.add("current", "Current password", self.current)
        self.form.add("new", "New password", self.new)
        self.form.add("repeat", "Repeat new password", self.repeat)
        self.body.addWidget(self.form)
        self.body.addWidget(label("At least 10 characters, with upper and lower case letters and a digit.", "muted", wrap=True))
        if forced:
            self.cancel_btn.setText("Sign out")

    def submit(self) -> None:
        self.form.clear_errors()
        if self.new.text() != self.repeat.text():
            self.form.set_error("repeat", "Passwords do not match.")
            return self.reenable()
        body = {"current_password": self.current.text(), "new_password": self.new.text()}
        run_async(lambda: self.session.client.post("/auth/change-password", body),
                  lambda _: self.finish_ok(), lambda e: self.finish_error(e.message))
