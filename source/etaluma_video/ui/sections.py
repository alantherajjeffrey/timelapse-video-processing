"""Collapsible sections for the right panel: Display, Output and On the videos (0.7)."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QSizePolicy, QToolButton, QVBoxLayout, QWidget


class CollapsibleSection(QWidget):
    """A header button that shows or hides one content widget."""

    toggled = Signal(bool)

    def __init__(self, title: str, content: QWidget, expanded: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.content = content
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.header = QToolButton(self)
        self.header.setProperty("role", "section")
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.toggled.connect(self._apply)
        layout.addWidget(self.header)
        layout.addWidget(content)
        self.header.setChecked(expanded)
        self._apply(expanded)

    def _apply(self, expanded: bool) -> None:
        self.header.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.header.setToolTip(f"{'Hide' if expanded else 'Show'} {self.header.text().lower()}")
        self.content.setVisible(expanded)
        self.toggled.emit(expanded)

    def is_expanded(self) -> bool:
        return self.header.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        self.header.setChecked(bool(expanded))
