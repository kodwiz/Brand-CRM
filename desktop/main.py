"""Desktop entry point:  python -m desktop.main"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMainWindow, QStackedWidget

from desktop.api.client import ApiClient
from desktop.session import Session
from desktop.ui import tokens as T
from desktop.ui.login import LoginView
from desktop.ui.shell import Shell


class MainWindow(QMainWindow):
    def __init__(self, client: ApiClient | None = None):
        super().__init__()
        self.setWindowTitle("Cosmetics Operations")
        self.resize(1366, 768)
        self.setMinimumSize(1100, 640)
        self.client = client or ApiClient()
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.login = LoginView(self.client)
        self.login.signed_in.connect(self._signed_in)
        self.stack.addWidget(self.login)
        self.shell: Shell | None = None

    def _signed_in(self, session: Session) -> None:
        self.shell = Shell(session)
        self.shell.sign_out_requested.connect(self._sign_out)
        self.stack.addWidget(self.shell)
        self.stack.setCurrentWidget(self.shell)

    def _sign_out(self) -> None:
        self.client.logout()
        if self.shell is not None:
            self.stack.removeWidget(self.shell)
            self.shell.deleteLater()
            self.shell = None
        self.login.reset()
        self.stack.setCurrentWidget(self.login)


def build_app() -> QApplication:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(T.stylesheet())
    return app


def main() -> int:
    app = build_app()
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
