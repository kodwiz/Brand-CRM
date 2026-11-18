"""Form helpers: label above input, validation message directly under the field."""
from __future__ import annotations

from PySide6.QtWidgets import QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from desktop.ui.widgets import Banner, button, label


class FormGrid(QWidget):
    def __init__(self, columns: int = 1, parent: QWidget | None = None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(16)
        self.grid.setVerticalSpacing(2)
        self.columns = columns
        self._n = 0
        self.errors: dict[str, QLabel] = {}

    def add(self, key: str, title: str, widget: QWidget, span: int = 1) -> QWidget:
        r = (self._n // self.columns) * 3
        c = self._n % self.columns
        self.grid.addWidget(label(title, "fieldLabel"), r, c, 1, span)
        self.grid.addWidget(widget, r + 1, c, 1, span)
        err = label("", "error", wrap=True)
        err.setVisible(False)
        self.grid.addWidget(err, r + 2, c, 1, span)
        self.errors[key] = err
        self._n += span if span else 1
        if span > 1 and self._n % self.columns:
            self._n += self.columns - (self._n % self.columns)
        return widget

    def set_error(self, key: str, text: str | None) -> None:
        e = self.errors.get(key)
        if e is not None:
            e.setText(text or "")
            e.setVisible(bool(text))

    def clear_errors(self) -> None:
        for k in self.errors:
            self.set_error(k, None)


def mark_invalid(widget: QWidget, invalid: bool) -> None:
    widget.setProperty("invalid", invalid)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class FormDialog(QDialog):
    """Dialog with a form body, an inline banner for server errors, and a submit button that
    disables itself while a request is in flight (prevents duplicate submissions)."""

    def __init__(self, title: str, submit_text: str, parent: QWidget | None = None, width: int = 460):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(width)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(20, 18, 20, 16)
        self.root.setSpacing(10)
        self.root.addWidget(label(title, "sectionTitle"))
        self.banner = Banner()
        self.root.addWidget(self.banner)
        self.body = QVBoxLayout()
        self.root.addLayout(self.body)
        row = QHBoxLayout()
        row.addStretch()
        self.cancel_btn = button("Cancel", on_click=self.reject)
        self.submit_btn = button(submit_text, "primary", on_click=self._submit_clicked)
        self.submit_btn.setDefault(True)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.submit_btn)
        self.root.addLayout(row)

    # subclasses implement submit() and call self.finish_ok() / self.finish_error(msg)
    def submit(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def _submit_clicked(self) -> None:
        self.banner.clear()
        self.submit_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.submit()

    def finish_ok(self) -> None:
        self.accept()

    def finish_error(self, message: str) -> None:
        self.banner.show_message(message, "error")
        self.submit_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)

    def reenable(self) -> None:
        self.submit_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)


def password_edit() -> QLineEdit:
    e = QLineEdit()
    e.setEchoMode(QLineEdit.EchoMode.Password)
    return e
