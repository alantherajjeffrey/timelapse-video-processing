"""Small stand-in widgets used while the explorer, viewer and display panel are built.

The wave-2 agents replace ``ui/explorer.py``, ``ui/viewer.py`` and ``ui/display_panel.py``
with the real widgets; the shell only depends on the constructor signature
``Widget(ctx, parent=None)`` and on the widget being a ``QWidget``.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget


class PlaceholderWidget(QWidget):
    """A titled, muted panel that says which agent fills this area."""

    def __init__(self, title: str, note: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        self.title_label = QLabel(title)
        self.title_label.setProperty("role", "heading")
        layout.addWidget(self.title_label)
        self.note_label = QLabel(note)
        self.note_label.setProperty("role", "muted")
        self.note_label.setWordWrap(True)
        self.note_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.note_label)
        layout.addStretch(1)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    def set_note(self, text: str) -> None:
        self.note_label.setText(text)
