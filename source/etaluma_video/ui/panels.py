"""Collapsible side panels (0.6): the explorer, the display and output panel, and the log.

Each dock gets a title bar with a collapse button. A collapsed panel leaves a slim strip on its
own edge (a one-button toolbar, so the main window lays it out and remembers it) with the panel's
name; clicking the strip opens the panel again. The View menu and shortcuts do the same.
The results page collapses Display & output and the log and restores them on the way back.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QRectF, QSize, Qt
from PySide6.QtGui import QAction, QColor, QKeySequence, QPainter, QPalette
from PySide6.QtWidgets import QDockWidget, QHBoxLayout, QLabel, QMainWindow, QToolBar, QToolButton, QWidget

SIDES = {
    "left": (Qt.ToolBarArea.LeftToolBarArea, "‹", "›"),
    "right": (Qt.ToolBarArea.RightToolBarArea, "›", "‹"),
    "bottom": (Qt.ToolBarArea.BottomToolBarArea, "⌄", "⌃"),
}


class StripButton(QToolButton):
    """The button of a collapsed panel's strip; its text runs top to bottom on the side strips."""

    def __init__(self, text: str, vertical: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._label = text
        self._vertical = vertical
        self.setText(text)
        self.setAutoRaise(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:  # noqa: D102 - Qt API
        fm = self.fontMetrics()
        long_side, short_side = fm.horizontalAdvance(self._label) + 28, fm.height() + 12
        return QSize(short_side, long_side) if self._vertical else QSize(long_side, short_side)

    def minimumSizeHint(self) -> QSize:  # noqa: D102 - Qt API
        return self.sizeHint()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not self._vertical:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        if self.underMouse():
            accent = self.palette().color(QPalette.ColorRole.Highlight)
            painter.fillRect(self.rect(), QColor(accent.red(), accent.green(), accent.blue(), 60))
        painter.setPen(self.palette().color(QPalette.ColorRole.ButtonText))
        painter.translate(self.width() / 2, self.height() / 2)
        painter.rotate(90)
        painter.drawText(QRectF(-self.height() / 2, -self.width() / 2, self.height(), self.width()),
                         Qt.AlignmentFlag.AlignCenter, self._label)
        painter.end()


class DockTitleBar(QWidget):
    """Title and collapse button; dragging and double-clicking still move and float the dock."""

    def __init__(self, title: str, glyph: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 4, 2)
        layout.setSpacing(4)
        label = QLabel(title)
        label.setProperty("role", "heading")
        layout.addWidget(label)
        layout.addStretch(1)
        self.collapse_button = QToolButton(self)
        self.collapse_button.setText(glyph)
        self.collapse_button.setAutoRaise(True)
        self.collapse_button.setToolTip(f"Collapse {title.lower()}")
        layout.addWidget(self.collapse_button)


class PanelManager(QObject):
    """Opens and collapses the window's panels and keeps their edge strips in step."""

    def __init__(self, window: QMainWindow) -> None:
        super().__init__(window)
        self.window = window
        self.docks: dict[str, QDockWidget] = {}
        self.strips: dict[str, QToolBar] = {}
        self.actions: dict[str, QAction] = {}
        self._open: dict[str, bool] = {}

    def add(self, key: str, dock: QDockWidget, side: str, title: str, shortcut: str = "",
            collapse_button: QToolButton | None = None) -> None:
        area, collapse_glyph, open_glyph = SIDES[side]
        if collapse_button is None:
            bar = DockTitleBar(title, collapse_glyph, dock)
            dock.setTitleBarWidget(bar)
            collapse_button = bar.collapse_button
        else:
            collapse_button.setText(collapse_glyph)
            collapse_button.setToolTip(f"Collapse {title.lower()}")
        collapse_button.clicked.connect(lambda _checked=False, k=key: self.set_open(k, False))

        strip = QToolBar(f"{title} (collapsed)", self.window)
        strip.setObjectName(f"{key}_strip")
        strip.setMovable(False)
        strip.setFloatable(False)
        strip.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        vertical = side != "bottom"
        button = StripButton(f"{open_glyph}  {title}", vertical, strip)
        button.setToolTip(f"Open {title.lower()}")
        button.clicked.connect(lambda _checked=False, k=key: self.set_open(k, True))
        strip.addWidget(button)
        self.window.addToolBar(area, strip)
        strip.setVisible(False)

        action = QAction(f"Show {title.lower()}", self.window)
        action.setCheckable(True)
        action.setChecked(True)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.toggled.connect(lambda on, k=key: self.set_open(k, on))

        self.docks[key], self.strips[key], self.actions[key] = dock, strip, action
        self._open[key] = True

    def is_open(self, key: str) -> bool:
        return self._open.get(key, False)

    def set_open(self, key: str, open_: bool) -> None:
        if key not in self.docks:
            return
        open_ = bool(open_)
        self._open[key] = open_
        dock = self.docks[key]
        if open_ and dock.isFloating():
            dock.setFloating(False)
        dock.setVisible(open_)
        self.strips[key].setVisible(not open_)
        action = self.actions[key]
        if action.isChecked() != open_:
            blocked = action.blockSignals(True)
            action.setChecked(open_)
            action.blockSignals(blocked)

    def sync_from_docks(self) -> None:
        """After QMainWindow.restoreState: trust the docks and show the strips of the closed ones."""
        for key, dock in self.docks.items():
            self.set_open(key, not dock.isHidden())

    def snapshot(self, keys) -> dict[str, bool]:
        return {k: self.is_open(k) for k in keys}

    def restore(self, state: dict[str, bool]) -> None:
        for key, open_ in state.items():
            self.set_open(key, open_)
