"""Settings dialog: the few preferences that survive between sessions."""
from __future__ import annotations

import dataclasses
import logging

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from .settings import PREVIEW_WIDTH_CHOICES, WIDTH_CHOICES, Settings, user_data_dir
from .theme import THEMES

log = logging.getLogger("etaluma.ui")


class SettingsDialog(QDialog):
    """Edits a copy of :class:`Settings`; the caller saves the result."""

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self._settings = settings

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(8)

        self.theme_combo = QComboBox()
        for name in THEMES:
            self.theme_combo.addItem(name.capitalize(), name)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(settings.theme)))
        form.addRow("Theme", self.theme_combo)

        self.width_combo = QComboBox()
        for w in WIDTH_CHOICES:
            self.width_combo.addItem(f"{w} px", w)
        self.width_combo.setCurrentIndex(max(0, self.width_combo.findData(int(settings.default_width))))
        form.addRow("Default video width", self.width_combo)

        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(0.5, 600.0)
        self.duration_spin.setDecimals(1)
        self.duration_spin.setSuffix(" s")
        self.duration_spin.setValue(float(settings.duration_seconds))
        form.addRow("Default playback duration", self.duration_spin)

        self.avi_check = QCheckBox("AVI (MJPG) on by default")
        self.avi_check.setChecked(bool(settings.avi))
        form.addRow("", self.avi_check)
        self.mp4_check = QCheckBox("MP4 (H.264) on by default")
        self.mp4_check.setChecked(bool(settings.mp4))
        form.addRow("", self.mp4_check)

        self.preview_combo = QComboBox()
        for w in PREVIEW_WIDTH_CHOICES:
            self.preview_combo.addItem(f"{w} px", w)
        self.preview_combo.setCurrentIndex(max(0, self.preview_combo.findData(int(settings.preview_cache_width))))
        form.addRow("Preview cache width", self.preview_combo)

        self.rolling_ball_spin = QSpinBox()
        self.rolling_ball_spin.setRange(5, 500)
        self.rolling_ball_spin.setSuffix(" px")
        self.rolling_ball_spin.setValue(int(settings.rolling_ball_radius))
        form.addRow("Rolling ball radius", self.rolling_ball_spin)

        self.debug_check = QCheckBox("Start the log console in Debug")
        self.debug_check.setChecked(bool(settings.log_debug))
        form.addRow("", self.debug_check)
        layout.addLayout(form)

        self.folder_label = QLabel(str(user_data_dir()))
        self.folder_label.setProperty("role", "muted")
        self.folder_label.setWordWrap(True)
        layout.addWidget(self.folder_label)
        open_button = QPushButton("Open user data folder")
        open_button.clicked.connect(self.open_user_data_folder)
        layout.addWidget(open_button)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def open_user_data_folder(self) -> None:
        folder = user_data_dir()
        log.info("Opening %s", folder)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def result_settings(self) -> Settings:
        """A copy of the settings with the dialog's values applied."""
        updated = dataclasses.replace(self._settings)
        updated.update(
            theme=str(self.theme_combo.currentData()),
            default_width=int(self.width_combo.currentData()),
            duration_seconds=float(self.duration_spin.value()),
            avi=self.avi_check.isChecked(),
            mp4=self.mp4_check.isChecked(),
            preview_cache_width=int(self.preview_combo.currentData()),
            rolling_ball_radius=int(self.rolling_ball_spin.value()),
            log_debug=self.debug_check.isChecked(),
        )
        return updated
