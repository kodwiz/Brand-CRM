from __future__ import annotations

from PySide6.QtCore import Qt

from desktop.pages.base import Page
from desktop.ui.widgets import PageHeader, label


class NotBuiltPage(Page):
    """Honest empty state for a module whose backend is not implemented yet. Shows no figures."""

    def __init__(self, session, title: str, phase: str, summary: str):
        super().__init__(session)
        self.title = title
        self.root.addWidget(PageHeader(title))
        self.root.addSpacing(40)
        for text, name in (("Not available yet", "sectionTitle"), (summary, "muted"),
                           (f"Planned for {phase}. No data is shown here until the backend for this module exists.", "muted")):
            lb = label(text, name, wrap=True)
            lb.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            lb.setMaximumWidth(560)
            lb.setMinimumHeight(lb.sizeHint().height() + 20 if name == "muted" else 0)
            self.root.addWidget(lb, 0, Qt.AlignmentFlag.AlignHCenter)
        self.root.addStretch()
