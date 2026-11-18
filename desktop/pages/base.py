from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from desktop.session import Session


class Page(QWidget):
    """Base for all screens. The shell calls refresh() when the page is shown and
    scope_changed() when the location selector changes."""

    title = ""
    navigate_requested = Signal(str)     # ask the shell to open another module (for example from an alert)

    def __init__(self, session: Session, parent: QWidget | None = None):
        super().__init__(parent)
        self.session = session
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(24, 20, 24, 16)
        self.root.setSpacing(14)

    def refresh(self) -> None:
        pass

    def scope_changed(self) -> None:
        self.refresh()
