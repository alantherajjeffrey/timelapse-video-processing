"""About box: what the app is, who maintains it, the disclaimer, the licence and where things live."""
from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .. import APP_NAME, CREDITS, DISCLAIMER, LICENSE_NAME, REPOSITORY_URL, RESEARCH_USE, VERSION


class AboutDialog(QDialog):
    """Non-modal About box (``open()``), so headless runs never block on it."""

    def __init__(self, user_data: str, log_folder: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(10)

        title = QLabel(APP_NAME)
        title.setProperty("role", "title")
        version = QLabel(f"Version {VERSION}")
        version.setProperty("role", "muted")
        layout.addWidget(title)
        layout.addWidget(version)

        what = QLabel("Timelapse videos, channel composites, montages and simple measurements "
                      "from Etaluma LS720 captures.")
        what.setWordWrap(True)
        layout.addWidget(what)

        credit = QLabel(CREDITS)
        credit.setWordWrap(True)
        credit.setProperty("role", "heading")
        layout.addWidget(credit)

        disclaimer = QLabel(DISCLAIMER)
        disclaimer.setWordWrap(True)
        disclaimer.setProperty("role", "muted")
        layout.addWidget(disclaimer)

        research = QLabel(RESEARCH_USE)
        research.setWordWrap(True)
        research.setProperty("role", "muted")
        layout.addWidget(research)

        licence = QLabel(f"{LICENSE_NAME} licence. Provided as is, without warranty. Third-party components "
                         "keep their own licences (THIRD_PARTY_NOTICES.txt in the install folder).")
        licence.setWordWrap(True)
        licence.setProperty("role", "muted")
        layout.addWidget(licence)

        from .settings import friendly_path  # noqa: WPS433 - the folders as they read on any PC, no user name

        paths = QLabel(f"Settings and cache: {friendly_path(user_data)}\nLogs: {friendly_path(log_folder)}")
        paths.setProperty("role", "muted")
        paths.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        paths.setWordWrap(True)
        layout.addWidget(paths)

        links = QHBoxLayout()
        self.repo_button = QPushButton("Project page and issues")
        self.repo_button.setToolTip(REPOSITORY_URL)
        self.repo_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(REPOSITORY_URL)))
        self.logs_button = QPushButton("Open log folder")
        self.logs_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(log_folder)))
        links.addWidget(self.repo_button)
        links.addWidget(self.logs_button)
        links.addStretch(1)
        layout.addLayout(links)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        buttons.accepted.connect(self.close)
        layout.addWidget(buttons)

    def text(self) -> str:
        """Everything the box says, for tests and the crash report."""
        return "\n".join(label.text() for label in self.findChildren(QLabel))
