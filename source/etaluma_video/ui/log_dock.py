"""Log console dock (decision 18).

Every part of the app logs through the ``etaluma`` logger hierarchy. ``QtLogHandler``
turns records into a Qt signal (queued, so worker threads are safe) and the dock
renders them: timestamped, coloured by level, Normal hides DEBUG, capped at 5000
lines, autoscroll unless the user scrolled up, Copy, Save and Save as buttons. The header row
is the dock's title bar, with the collapse button the panel manager connects (0.6). Records are
also forwarded to ``AppContext.log_line`` for anything else that wants them.
"""
from __future__ import annotations

import datetime as _dt
import html
import logging
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import theme as theme_module
from .settings import logs_dir

ROOT_LOGGER = "etaluma"
MAX_LINES = 5000


class LogBridge(QObject):
    """Signal carrier for :class:`QtLogHandler` (a logging.Handler must not also be a QObject)."""

    message = Signal(str, str)  # level name, text


class QtLogHandler(logging.Handler):
    """logging.Handler that re-emits records as a Qt signal on the GUI thread."""

    def __init__(self, level: int = logging.DEBUG) -> None:
        super().__init__(level)
        self.bridge = LogBridge()
        self._closed = False

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102 - logging API
        if self._closed:
            return
        try:
            text = self.format(record)
        except Exception:  # pragma: no cover - formatting must never raise into logging
            text = record.getMessage()
        try:
            self.bridge.message.emit(record.levelname, text)
        except RuntimeError:  # the bridge was deleted while a thread was logging
            self._closed = True

    def close(self) -> None:  # noqa: D102 - logging API
        self._closed = True
        super().close()


class LogDock(QDockWidget):
    """Bottom dock with the session log."""

    def __init__(self, ctx, parent=None, debug: bool = False) -> None:
        super().__init__("Log", parent)
        self.setObjectName("log_dock")
        self.ctx = ctx
        self._theme = "dark"
        self._records: deque[tuple[str, str, str]] = deque(maxlen=MAX_LINES)  # time, level, text
        self._autoscroll = True

        body = QWidget(self)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 0, 8, 6)
        layout.setSpacing(6)

        header = QWidget(self)
        bar = QHBoxLayout(header)
        bar.setContentsMargins(8, 3, 4, 3)
        bar.setSpacing(6)
        heading = QLabel("Log")
        heading.setProperty("role", "heading")
        bar.addWidget(heading)
        self.detail = QComboBox()
        self.detail.addItems(["Normal", "Debug"])
        self.detail.setCurrentIndex(1 if debug else 0)
        self.detail.setToolTip("Normal shows actions and engine progress; Debug adds engine calls with parameters.")
        self.detail.currentIndexChanged.connect(self._rerender)
        bar.addWidget(self.detail)
        bar.addStretch(1)
        self.copy_button = QPushButton("Copy")
        self.copy_button.clicked.connect(self.copy_to_clipboard)
        # 0.5's Save button passed Qt's "checked" flag as the file name and crashed every time.
        self.save_button = QPushButton("Save")
        self.save_button.setToolTip("Save the log as a new file in the app's logs folder (the path is logged)")
        self.save_button.clicked.connect(lambda _checked=False: self.save_to_file())
        self.save_as_button = QPushButton("Save as…")
        self.save_as_button.setToolTip("Choose where to save the log")
        self.save_as_button.clicked.connect(lambda _checked=False: self.save_as())
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear)
        for b in (self.copy_button, self.save_button, self.save_as_button, self.clear_button):
            bar.addWidget(b)
        self.collapse_button = QToolButton(header)
        self.collapse_button.setAutoRaise(True)
        bar.addWidget(self.collapse_button)
        self.setTitleBarWidget(header)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(MAX_LINES)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)
        self.view.setFont(font)
        self.view.verticalScrollBar().valueChanged.connect(self._scrolled)
        layout.addWidget(self.view, 1)

        self.setWidget(body)

        self.handler = QtLogHandler()
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.handler.bridge.message.connect(self.append_record, Qt.ConnectionType.QueuedConnection)
        app_logger = logging.getLogger(ROOT_LOGGER)
        # The dock is a DEBUG sink: without this the default WARNING level of an
        # unconfigured logger would drop every action and progress line.
        if app_logger.level == logging.NOTSET or app_logger.level > logging.DEBUG:
            app_logger.setLevel(logging.DEBUG)
        app_logger.addHandler(self.handler)
        handler = self.handler
        self.destroyed.connect(lambda *_: _detach(handler))

    # ---- lifecycle ------------------------------------------------------ #
    def detach(self) -> None:
        """Remove the log handler (called on window close and by tests)."""
        _detach(self.handler)

    @property
    def debug_visible(self) -> bool:
        return self.detail.currentIndex() == 1

    def set_debug_visible(self, on: bool) -> None:
        self.detail.setCurrentIndex(1 if on else 0)

    def set_theme(self, name: str) -> None:
        self._theme = name if name in theme_module.THEMES else "dark"
        self._rerender()

    # ---- content -------------------------------------------------------- #
    def append_record(self, level: str, text: str) -> None:
        stamp = _dt.datetime.now().strftime("%H:%M:%S")
        self._records.append((stamp, level, text))
        if self._visible(level):
            self._append_html(stamp, level, text)
        if self.ctx is not None:
            try:
                self.ctx.log_line.emit(level, text)
            except RuntimeError:  # pragma: no cover - context deleted during shutdown
                pass

    def clear(self) -> None:
        self._records.clear()
        self.view.clear()

    def text(self) -> str:
        return self.view.toPlainText()

    def copy_to_clipboard(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.text())

    @staticmethod
    def default_path() -> Path:
        return logs_dir() / f"log_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

    def save_to_file(self, path: str | Path | None = None) -> Path | None:
        """Write the log to ``path``, or to a new timestamped file in the logs folder."""
        if path is None or isinstance(path, bool):
            path = self.default_path()
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.text(), encoding="utf-8")
        logging.getLogger("etaluma.ui").info("Log saved to %s", p)
        return p

    def save_as(self) -> Path | None:
        """Ask where to save the log."""
        chosen, _ = QFileDialog.getSaveFileName(self, "Save log as", str(self.default_path()),
                                                "Text files (*.txt);;All files (*)")
        return self.save_to_file(chosen) if chosen else None

    # ---- internals ------------------------------------------------------ #
    def _visible(self, level: str) -> bool:
        return self.debug_visible or level.upper() != "DEBUG"

    def _append_html(self, stamp: str, level: str, text: str) -> None:
        colour = theme_module.log_colour(self._theme, level)
        muted = theme_module.colours(self._theme)["muted"]
        prefix = "" if level.upper() in ("INFO", "DEBUG") else f"{level.upper()}: "
        body = html.escape(f"{prefix}{text}").replace("\n", "<br>&nbsp;&nbsp;&nbsp;&nbsp;")
        self.view.appendHtml(f'<span style="color:{muted}">{stamp}</span>&nbsp;&nbsp;<span style="color:{colour}">{body}</span>')
        if self._autoscroll:
            bar = self.view.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _rerender(self) -> None:
        self.view.clear()
        for stamp, level, text in list(self._records):
            if self._visible(level):
                self._append_html(stamp, level, text)

    def _scrolled(self, value: int) -> None:
        bar = self.view.verticalScrollBar()
        self._autoscroll = value >= bar.maximum() - 2


def _detach(handler: logging.Handler) -> None:
    logger = logging.getLogger(ROOT_LOGGER)
    if handler in logger.handlers:
        logger.removeHandler(handler)
    try:
        handler.close()
    except Exception:  # pragma: no cover
        pass
