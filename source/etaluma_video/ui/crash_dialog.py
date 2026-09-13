"""Crash window (0.7): an unexpected error says what happened, with Copy report and the log folder.

``install(window)`` creates the reporter; ``report_exception`` is called by the application's
exception hooks from any thread and hands the report to the GUI thread through a queued signal.
Several errors in a row open one window, not one per error.
"""
from __future__ import annotations

import platform
import sys
import time
import traceback

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from .. import APP_NAME, REPOSITORY_URL, VERSION


class CrashDialog(QDialog):
    def __init__(self, summary: str, report: str, log_folder: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME}: something went wrong")
        self.setMinimumSize(560, 360)
        self._report = report
        self._log_folder = log_folder
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        title = QLabel("Something went wrong")
        title.setProperty("role", "subtitle")
        layout.addWidget(title)
        text = QLabel(f"{summary}\n\nThe app kept running. If something looks wrong, restart it. To report the problem, "
                      f"copy the report below into an issue at {REPOSITORY_URL}/issues; it holds no image data.")
        text.setWordWrap(True)
        layout.addWidget(text)
        self.details = QPlainTextEdit(report)
        self.details.setReadOnly(True)
        layout.addWidget(self.details, 1)
        buttons = QHBoxLayout()
        self.copy_button = QPushButton("Copy report")
        self.copy_button.clicked.connect(self.copy_report)
        logs = QPushButton("Open log folder")
        logs.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self._log_folder)))
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons.addWidget(self.copy_button)
        buttons.addWidget(logs)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def report_text(self) -> str:
        return self._report

    def copy_report(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._report)
        self.copy_button.setText("Copied")


def build_report(exc_type, exc_value, exc_tb) -> str:
    try:
        from PySide6 import __version__ as pyside_version
    except Exception:  # pragma: no cover
        pyside_version = "?"
    head = (f"{APP_NAME} {VERSION}\nPython {sys.version.split()[0]} · PySide6 {pyside_version} · "
            f"{platform.system()} {platform.release()}\n\n")
    from .settings import friendly_text  # noqa: WPS433 - reports are pasted into public issues: no user name

    return friendly_text(head + "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))


class CrashReporter(QObject):
    raised = Signal(str, str)  # summary, report

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self.window = window
        self.dialog: CrashDialog | None = None
        self._last = 0.0
        self.raised.connect(self._show)

    def report(self, exc_type, exc_value, exc_tb) -> None:
        self.raised.emit(f"{exc_type.__name__}: {exc_value}", build_report(exc_type, exc_value, exc_tb))

    def _show(self, summary: str, report: str) -> None:
        now = time.monotonic()
        if self.dialog is not None and self.dialog.isVisible() and now - self._last < 10.0:
            return  # one window for a burst of errors
        self._last = now
        from .settings import logs_dir  # noqa: WPS433

        self.dialog = CrashDialog(summary, report, str(logs_dir()), self.window)
        self.dialog.open()


_REPORTER: CrashReporter | None = None


def install(window: QWidget) -> CrashReporter:
    global _REPORTER
    _REPORTER = CrashReporter(window)
    return _REPORTER


def uninstall() -> None:
    global _REPORTER
    _REPORTER = None


def report_exception(exc_type, exc_value, exc_tb) -> None:
    """Hand an unhandled exception to the crash window, if the window exists (safe from any thread)."""
    reporter = _REPORTER
    if reporter is None:
        return
    try:
        reporter.report(exc_type, exc_value, exc_tb)
    except RuntimeError:  # the window is being destroyed
        pass
