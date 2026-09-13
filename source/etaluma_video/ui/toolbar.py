"""Main toolbar (decision 2): Open folder, Quick video, Process, Cancel, mode, Results, Settings.

Icons come from Qt's standard pixmaps with one embedded SVG gear as a fallback-safe
extra; text sits under the icons so the buttons read without a legend.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QAction, QActionGroup, QIcon, QImage, QPixmap
from PySide6.QtWidgets import QMenu, QSizePolicy, QStyle, QToolBar, QToolButton, QWidget

log = logging.getLogger("etaluma.ui")

GEAR_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"
 stroke="#c8c8c8" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
 <circle cx="12" cy="12" r="3.2"/>
 <path d="M19.2 13.4a7.6 7.6 0 0 0 0-2.8l2-1.5-2-3.4-2.4 1a7.6 7.6 0 0 0-2.4-1.4L14 2.6h-4l-.4 2.7a7.6 7.6 0 0 0-2.4 1.4l-2.4-1-2 3.4 2 1.5a7.6 7.6 0 0 0 0 2.8l-2 1.5 2 3.4 2.4-1a7.6 7.6 0 0 0 2.4 1.4l.4 2.7h4l.4-2.7a7.6 7.6 0 0 0 2.4-1.4l2.4 1 2-3.4z"/>
</svg>"""


def svg_icon(data: bytes, size: int = 24) -> QIcon:
    """QIcon from inline SVG; an empty icon when the SVG image plugin is missing."""
    image = QImage()
    if image.loadFromData(QByteArray(data), "SVG"):
        return QIcon(QPixmap.fromImage(image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                                    Qt.TransformationMode.SmoothTransformation)))
    return QIcon()


def standard_icon(widget: QWidget, name: str) -> QIcon:
    pixmap = getattr(QStyle.StandardPixmap, name, None)
    if pixmap is None:  # pragma: no cover - Qt always has these
        return QIcon()
    return widget.style().standardIcon(pixmap)


class MainToolBar(QToolBar):
    """The window's single toolbar. Actions stay accessible as attributes."""

    def __init__(self, window, actions, parent: QWidget | None = None) -> None:
        super().__init__("Main toolbar", parent or window)
        self.setObjectName("main_toolbar")
        self.setMovable(True)
        self.setIconSize(QSize(22, 22))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.window = window
        self.actions_api = actions

        self.open_action = self._action("Open folder", "SP_DirOpenIcon", "Open an experiment or a parent folder (Ctrl+O)", "Ctrl+O")
        self.open_action.triggered.connect(lambda: actions.open_folder())
        self.addAction(self.open_action)

        self.queue_action = self._action("Queue", "SP_FileDialogListView",
                                         "Experiment folders processed one after another (Ctrl+Shift+Q); "
                                         "click again for the preview", "Ctrl+Shift+Q")
        self.queue_action.setCheckable(True)
        self.queue_action.triggered.connect(lambda: window.show_queue())
        self.addAction(self.queue_action)

        self.quick_action = self._action("Quick video", "SP_MediaSeekForward", "Quick video (Ctrl+Q)", "Ctrl+Q")
        self.quick_action.triggered.connect(lambda: actions.quick_video())
        self.addAction(self.quick_action)
        # 0.8: a split button; the arrow picks the display choice and the output preset
        self.quick_menu = QMenu(self)
        self.quick_menu.aboutToShow.connect(self._fill_quick_menu)
        self.quick_button = self.widgetForAction(self.quick_action)
        if isinstance(self.quick_button, QToolButton):
            self.quick_button.setMenu(self.quick_menu)
            self.quick_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.update_quick_tip()

        self.process_action = self._action("Process", "SP_MediaPlay", "Run with the current output settings (Ctrl+R)", "Ctrl+R")
        self.process_action.triggered.connect(lambda: actions.process())
        self.addAction(self.process_action)

        self.cancel_action = self._action("Cancel", "SP_MediaStop", "Ask the running job to stop (Esc)", "Esc")
        self.cancel_action.triggered.connect(lambda: actions.cancel())
        self.addAction(self.cancel_action)

        self.addSeparator()
        self.mode_group = QActionGroup(self)
        self.mode_group.setExclusive(True)
        self.timelapse_action = QAction("Timelapse", self)
        self.fixed_action = QAction("Fixed", self)
        for action, mode in ((self.timelapse_action, "timelapse"), (self.fixed_action, "fixed")):
            action.setCheckable(True)
            action.setData(mode)
            action.setToolTip("Timelapse videos over time" if mode == "timelapse" else "Fixed images: per-position composites and quantification")
            self.mode_group.addAction(action)
            self.addAction(action)
        self.timelapse_action.setChecked(True)
        self.mode_group.triggered.connect(self._on_mode_triggered)

        spacer = QWidget(self)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.addWidget(spacer)

        self.results_action = self._action("Results", "SP_FileDialogContentsView", "Every video and image of the runs, large (Ctrl+Shift+R); click again for the preview", "Ctrl+Shift+R")
        self.results_action.triggered.connect(lambda: window.show_results())
        self.addAction(self.results_action)

        self.settings_action = self._action("Settings", "SP_FileDialogDetailedView", "Preferences (Ctrl+,)", "Ctrl+,")
        gear = svg_icon(GEAR_SVG)
        if not gear.isNull():
            self.settings_action.setIcon(gear)
        self.settings_action.triggered.connect(lambda: actions.show_settings())
        self.addAction(self.settings_action)

    def apply_theme(self, colours: dict) -> None:
        """Themed line icons (0.7): the two run buttons in the accent colour, the rest in the text colour."""
        from .icons import icon  # noqa: WPS433

        for action, name, key in ((self.open_action, "open", "text"), (self.queue_action, "queue", "text"),
                                  (self.quick_action, "quick", "accent"),
                                  (self.process_action, "process", "accent"), (self.cancel_action, "cancel", "text"),
                                  (self.results_action, "results", "text"), (self.settings_action, "settings", "text"),
                                  (self.timelapse_action, "timelapse", "text"), (self.fixed_action, "fixed", "text")):
            themed = icon(name, colours[key])
            if not themed.isNull():
                action.setIcon(themed)

    # ---- Quick video choices (0.8) ------------------------------------------ #
    def update_quick_tip(self) -> None:
        from . import presets_store  # noqa: WPS433

        s = self.actions_api.settings
        tip = (f"Quick video of the open experiment: {presets_store.display_label(s.quick_display or 'auto')}, "
               f"output preset {presets_store.preset_label(s.quick_preset or presets_store.QUICK)} (Ctrl+Q). "
               "The arrow changes both.")
        self.quick_action.setToolTip(tip)
        self.quick_action.setStatusTip(tip)

    def _fill_quick_menu(self) -> None:
        from . import presets_store  # noqa: WPS433

        s = self.actions_api.settings
        menu = self.quick_menu
        menu.clear()
        groups = (("Display", "quick_display", presets_store.display_choices(), s.quick_display or presets_store.AUTO),
                  ("Output preset", "quick_preset",
                   presets_store.preset_choices() + [(presets_store.CURRENT, presets_store.CURRENT_PRESET_LABEL)],
                   s.quick_preset or presets_store.QUICK))
        for index, (title, field, choices, current) in enumerate(groups):
            if index:
                menu.addSeparator()
            heading = menu.addAction(title)
            heading.setEnabled(False)
            group = QActionGroup(menu)
            group.setExclusive(True)
            for key, label in choices:
                action = menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(key == current)
                action.setData(key)
                group.addAction(action)
                action.triggered.connect(lambda _checked=False, f=field, k=key: self.set_quick_choice(f, k))

    def set_quick_choice(self, field: str, key: str) -> None:
        from . import presets_store  # noqa: WPS433

        setattr(self.actions_api.settings, field, key)
        self.update_quick_tip()
        if field == "quick_display":
            log.info("Quick video display: %s", presets_store.display_label(key))
        else:
            log.info("Quick video output preset: %s", presets_store.preset_label(key))

    # ---- helpers --------------------------------------------------------- #
    def _action(self, text: str, icon_name: str, tip: str, shortcut: str = "") -> QAction:
        action = QAction(standard_icon(self, icon_name), text, self)
        action.setToolTip(tip)
        action.setStatusTip(tip)
        if shortcut:
            action.setShortcut(shortcut)
        return action

    def _on_mode_triggered(self, action: QAction) -> None:
        self.actions_api.set_mode(str(action.data()))

    # ---- state ----------------------------------------------------------- #
    def set_mode(self, mode: str) -> None:
        """Reflect the context mode without re-triggering the action group."""
        target = self.fixed_action if str(mode).startswith("fix") else self.timelapse_action
        if not target.isChecked():
            blocked = self.mode_group.blockSignals(True)
            target.setChecked(True)
            self.mode_group.blockSignals(blocked)

    def update_enabled(self, has_dataset: bool, busy: bool, queue_running: bool = False) -> None:
        self.open_action.setEnabled(not busy)
        self.quick_action.setEnabled(has_dataset and not busy)
        self.process_action.setEnabled(has_dataset and not busy)
        # 0.8: while the queue runs, both buttons add the open experiment(s) to it
        self.quick_action.setText("Quick → queue" if queue_running else "Quick video")
        self.process_action.setText("Process → queue" if queue_running else "Process")
        self.cancel_action.setEnabled(busy)
        self.results_action.setEnabled(True)
        self.settings_action.setEnabled(not busy)
