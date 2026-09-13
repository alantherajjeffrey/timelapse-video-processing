"""Welcome page shown while no experiment is open (0.7): Open folder, drop a folder, recent folders."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from .. import APP_NAME, CREDITS, VERSION


class WelcomePage(QWidget):
    open_requested = Signal()
    recent_requested = Signal(str)
    queue_requested = Signal()  # 0.8

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QHBoxLayout(self)
        outer.addStretch(1)
        column = QVBoxLayout()
        column.setSpacing(10)
        column.addStretch(2)

        title = QLabel(APP_NAME)
        title.setProperty("role", "title")
        subtitle = QLabel("Timelapse videos, channel composites, montages and simple measurements "
                          "from Etaluma LS720 captures.")
        subtitle.setProperty("role", "subtitle")
        subtitle.setWordWrap(True)
        column.addWidget(title)
        column.addWidget(subtitle)
        column.addSpacing(14)

        self.open_button = QPushButton("Open an experiment folder…")
        self.open_button.setProperty("role", "primary")
        self.open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.open_button.clicked.connect(self.open_requested.emit)
        column.addWidget(self.open_button)
        hint = QLabel("or drag an experiment folder, or a folder of experiments, onto this window. "
                      "Every Lumaview layout works: a folder per position, a folder per channel, or all files together.")
        hint.setProperty("role", "muted")
        hint.setWordWrap(True)
        column.addWidget(hint)
        self.queue_button = QPushButton("Add folders to the queue…")
        self.queue_button.setProperty("role", "link")
        self.queue_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.queue_button.setToolTip("Process several experiment folders one after another, unattended")
        self.queue_button.clicked.connect(self.queue_requested.emit)
        column.addWidget(self.queue_button)
        column.addSpacing(18)

        self.recent_heading = QLabel("Recent")
        self.recent_heading.setProperty("role", "heading")
        column.addWidget(self.recent_heading)
        self.recent_box = QVBoxLayout()
        self.recent_box.setSpacing(2)
        column.addLayout(self.recent_box)
        column.addStretch(3)

        footer = QLabel(f"{CREDITS}  ·  version {VERSION}")
        footer.setProperty("role", "muted")
        footer.setWordWrap(True)
        column.addWidget(footer)

        holder = QWidget()
        holder.setLayout(column)
        holder.setMaximumWidth(620)
        holder.setMinimumWidth(360)
        outer.addWidget(holder, 3)
        outer.addStretch(1)
        self.recent_buttons: list[QPushButton] = []
        self.set_recent([])

    def set_recent(self, folders: list[str]) -> None:
        for button in self.recent_buttons:
            button.deleteLater()
        self.recent_buttons.clear()
        shown = [f for f in folders if Path(f).is_dir()][:8]
        for folder in shown:
            path = Path(folder)
            button = QPushButton(f"{path.name}    {path.parent}")
            button.setProperty("role", "link")
            button.setToolTip(str(path))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, f=str(path): self.recent_requested.emit(f))
            self.recent_box.addWidget(button)
            self.recent_buttons.append(button)
        self.recent_heading.setVisible(bool(shown))
