"""Reusable presentation components. No API calls and no business logic here."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel, QPushButton, QSizePolicy,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from desktop.ui import tokens as T

ALIGN_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
ALIGN_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


def hr() -> QFrame:
    f = QFrame()
    f.setObjectName("hr")
    f.setFrameShape(QFrame.Shape.NoFrame)
    return f


def label(text: str = "", name: str | None = None, wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    if name:
        lb.setObjectName(name)
    lb.setWordWrap(wrap)
    return lb


def button(text: str, kind: str | None = None, on_click: Callable[[], None] | None = None) -> QPushButton:
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if on_click:
        b.clicked.connect(lambda: on_click())
    return b


def format_money(value: str | float | int | None, currency: str = "PKR") -> str:
    """Display formatting only. Authoritative amounts always come from the backend."""
    if value is None or value == "":
        return "-"
    return f"{currency} {float(value):,.2f}"


class Column:
    def __init__(self, key: str, title: str, width: int | None = None, align: str = "left",
                 status: Callable[[object], str] | None = None, fmt: Callable[[object], str] | None = None,
                 swatch_key: str | None = None):
        self.key, self.title, self.width, self.align, self.status, self.fmt = key, title, width, align, status, fmt
        self.swatch_key = swatch_key  # row key holding a list of "#RRGGBB" shades drawn as a strip before the text


def swatch_icon(colors: list[str], size: int = 14) -> QIcon | None:
    cols = [c for c in colors if c][:5]
    if not cols:
        return None
    gap = 3
    pm = QPixmap(len(cols) * (size + gap), size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    for i, c in enumerate(cols):
        p.setBrush(QColor(c))
        p.setPen(QColor(T.BORDER_STRONG))
        p.drawEllipse(i * (size + gap), 0, size - 1, size - 1)
    p.end()
    return QIcon(pm)


class DataTable(QTableWidget):
    """Dense, read-only table: fixed row height, right-aligned numbers, status text in semantic colour."""

    row_activated = Signal(dict)

    def __init__(self, columns: list[Column], parent: QWidget | None = None):
        super().__init__(0, len(columns), parent)
        self.columns = columns
        self._rows: list[dict] = []
        self.setHorizontalHeaderLabels([c.title for c in columns])
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(T.ROW_HEIGHT)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        self.setWordWrap(False)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        hh = self.horizontalHeader()
        hh.setHighlightSections(False)
        # Stretch the flexible column(s); only stretch the last column when every column has a fixed width.
        hh.setStretchLastSection(all(c.width for c in columns))
        hh.setDefaultAlignment(ALIGN_LEFT)
        for i, c in enumerate(columns):
            if c.width:
                hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
                self.setColumnWidth(i, c.width)
            else:
                hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
            if c.align == "right":
                self.horizontalHeaderItem(i).setTextAlignment(ALIGN_RIGHT)
        self.doubleClicked.connect(self._activated)

    def set_rows(self, rows: list[dict]) -> None:
        self._rows = rows
        self.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, col in enumerate(self.columns):
                raw = row.get(col.key)
                text = col.fmt(raw) if col.fmt else ("-" if raw in (None, "") else str(raw))
                item = QTableWidgetItem(text)
                item.setTextAlignment(ALIGN_RIGHT if col.align == "right" else ALIGN_LEFT)
                if col.status:
                    kind = col.status(raw)
                    fg, _ = T.STATUS_STYLES.get(kind, T.STATUS_STYLES["neutral"])
                    item.setForeground(QBrush(QColor(fg)))
                    item.setText(f"●  {text}")
                item.setToolTip(text)
                if col.swatch_key:
                    ic = swatch_icon(row.get(col.swatch_key) or [])
                    if ic is not None:
                        item.setIcon(ic)
                self.setItem(r, c, item)

    def row_data(self, row: int) -> dict | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def selected_row(self) -> dict | None:
        idx = self.currentRow()
        return self.row_data(idx)

    def _activated(self, index) -> None:
        d = self.row_data(index.row())
        if d is not None:
            self.row_activated.emit(d)


class StatePanel(QWidget):
    """Wraps content with honest loading / empty / error / offline states."""

    retry = Signal()

    LOADING, EMPTY, ERROR, CONTENT = range(4)

    def __init__(self, content: QWidget, empty_text: str = "Nothing to show yet.", parent: QWidget | None = None):
        super().__init__(parent)
        self.stack = QStackedWidget()
        self._loading = label("Loading…", "muted")
        self._loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty = label(empty_text, "muted")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        err = QWidget()
        el = QVBoxLayout(err)
        el.addStretch()
        self._err_title = label("", "sectionTitle")
        self._err_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._err_msg = label("", "muted", wrap=True)
        self._err_msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        retry_btn = button("Try again", on_click=self.retry.emit)
        el.addWidget(self._err_title)
        el.addWidget(self._err_msg)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(retry_btn)
        row.addStretch()
        el.addLayout(row)
        el.addStretch()
        for w in (self._loading, self._empty, err, content):
            self.stack.addWidget(w)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.stack)
        self.show_loading()

    def show_loading(self) -> None:
        self.stack.setCurrentIndex(self.LOADING)

    def show_content(self) -> None:
        self.stack.setCurrentIndex(self.CONTENT)

    def show_empty(self, text: str | None = None) -> None:
        if text:
            self._empty.setText(text)
        self.stack.setCurrentIndex(self.EMPTY)

    def show_error(self, message: str, offline: bool = False) -> None:
        self._err_title.setText("Server unreachable" if offline else "Could not load this data")
        self._err_msg.setText(message)
        self.stack.setCurrentIndex(self.ERROR)


class Banner(QLabel):
    """Inline message near the content it relates to. kind: info | error | ok."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setVisible(False)

    def show_message(self, text: str, kind: str = "info") -> None:
        self.setObjectName({"info": "banner", "error": "bannerError", "ok": "bannerOk"}[kind])
        self.style().unpolish(self)
        self.style().polish(self)
        self.setText(text)
        self.setVisible(True)

    def clear(self) -> None:
        self.setVisible(False)
        self.setText("")


class PageHeader(QWidget):
    """Title on the left, primary/secondary actions on the right."""

    def __init__(self, title: str, subtitle: str | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(label(title, "pageTitle"))
        if subtitle:
            col.addWidget(label(subtitle, "muted"))
        lay.addLayout(col)
        lay.addStretch()
        self.actions = QHBoxLayout()
        self.actions.setSpacing(T.SPACE)
        lay.addLayout(self.actions)

    def add_action(self, btn: QPushButton) -> QPushButton:
        self.actions.addWidget(btn)
        return btn


def bool_status(active: object) -> str:
    return "ok" if active else "neutral"


def spacer() -> QWidget:
    w = QWidget()
    w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    return w


class Pager(QWidget):
    """Previous / next with an honest 'x-y of N' label."""

    changed = Signal()

    def __init__(self, page_size: int = 50, parent: QWidget | None = None):
        super().__init__(parent)
        self.page, self.page_size, self.total = 1, page_size, 0
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.info = label("", "muted")
        self.prev_btn = button("Previous", on_click=lambda: self._go(-1))
        self.next_btn = button("Next", on_click=lambda: self._go(1))
        lay.addWidget(self.info)
        lay.addStretch()
        lay.addWidget(self.prev_btn)
        lay.addWidget(self.next_btn)

    def _go(self, d: int) -> None:
        self.page = max(1, self.page + d)
        self.changed.emit()

    def reset(self) -> None:
        self.page = 1

    def update_total(self, total: int) -> None:
        self.total = total
        first = (self.page - 1) * self.page_size + 1 if total else 0
        last = min(total, self.page * self.page_size)
        self.info.setText(f"{first}-{last} of {total}" if total else "")
        self.prev_btn.setEnabled(self.page > 1)
        self.next_btn.setEnabled(last < total)
        self.setVisible(total > self.page_size or self.page > 1)
