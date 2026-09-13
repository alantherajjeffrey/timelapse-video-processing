"""Main window: toolbar, explorer dock, viewer, display/output panel, log and results docks.

Layout follows plan 4.1. Every dock is movable and closable, the View menu toggles
them and resets the layout, and geometry plus dock state are remembered in settings.
Widgets read state from :class:`AppContext` and call :class:`Actions`; nothing here
touches the engine directly.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QAction, QActionGroup, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, VERSION
from .actions import Actions
from .display_panel import DisplayPanel
from .explorer import ExplorerWidget
from .jobs_qt import JobRunner
from .log_dock import LogDock
from .output_panel import OutputPanel
from . import theme as theme_module
from .experiment_memory import ExperimentMemory
from .panels import PanelManager
from .sections import CollapsibleSection
from .welcome import WelcomePage
from .queue import QueueController, QueuePage
from .results_view import ResultsView
from .settings import Settings
from .state import AppContext
from .theme import DEFAULT_THEME
from .toolbar import MainToolBar
from .viewer import ViewerWidget

log = logging.getLogger("etaluma.ui")

ASSETS = Path(__file__).resolve().parent.parent / "assets"
#: Saved dock layouts from older layouts are ignored (0.4 had a results dock; 0.6 moved the log
#: under the preview only and added the collapsed-panel strips).
LAYOUT_VERSION = 6


def app_icon() -> QIcon:
    for name in ("icon.ico", "icon.png"):
        path = ASSETS / name
        if path.is_file():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    return QIcon()


class MainWindow(QMainWindow):
    """The application window."""

    def __init__(self, ctx: AppContext | None = None, settings: Settings | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx if ctx is not None else AppContext()
        self.settings = settings if settings is not None else Settings.load()
        self._close_when_idle = False
        self._close_when_queue_idle = False
        self.last_update_result = None  # Help → Check for updates (0.8)
        self._message_boxes: list[QMessageBox] = []

        self.setObjectName("main_window")
        self.setWindowTitle(f"{APP_NAME} {VERSION}")
        self.setWindowIcon(app_icon())
        self.setAcceptDrops(True)
        self.setDockNestingEnabled(True)
        self.resize(1360, 860)

        self.runner = JobRunner(self)

        # ---- central viewer ------------------------------------------------ #
        self.viewer = ViewerWidget(self.ctx, self)
        self.results_view = ResultsView(self.ctx, self)
        self.central = QStackedWidget(self)
        self.welcome = WelcomePage(self)  # 0.7: shown while no experiment is open
        self.central.addWidget(self.viewer)
        self.central.addWidget(self.results_view)
        self.central.addWidget(self.welcome)
        self.setCentralWidget(self.central)
        self.central.setCurrentWidget(self.welcome)
        self.results_view.back_requested.connect(self.show_preview)

        # ---- docks --------------------------------------------------------- #
        self.explorer = ExplorerWidget(self.ctx, self)
        self.explorer_dock = self._dock("Explorer", "explorer_dock", self.explorer, Qt.DockWidgetArea.LeftDockWidgetArea)
        self.explorer_dock.setMinimumWidth(220)

        self.display_panel = DisplayPanel(self.ctx, self)
        self.output_panel = OutputPanel(self.ctx, self, settings=self.settings)
        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)
        # 0.7: collapsible sections, remembered, so the panel fits without scrolling
        sections = dict((self.settings.extra or {}).get("sections") or {})
        self.display_section = CollapsibleSection("Display", self.display_panel, bool(sections.get("display", True)))
        self.output_section = CollapsibleSection("Output", self.output_panel, bool(sections.get("output", True)))
        self.output_panel.videos_section.set_expanded(bool(sections.get("videos", True)))
        for key, section in (("display", self.display_section), ("output", self.output_section),
                             ("videos", self.output_panel.videos_section)):
            section.toggled.connect(lambda on, k=key: self._remember_section(k, on))
        self.display_panel.title_label.setVisible(False)
        self.output_panel.heading.setVisible(False)
        right_layout.addWidget(self.display_section)
        right_layout.addWidget(self.output_section)
        right_layout.addStretch(1)
        scroll = QScrollArea(self)
        scroll.setWidget(right)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.right_dock = self._dock("Display and output", "right_dock", scroll, Qt.DockWidgetArea.RightDockWidgetArea)
        self.right_dock.setMinimumWidth(280)
        self.experiment_memory = ExperimentMemory(self.ctx, self.display_panel, self.output_panel, self)

        # ---- queue (0.8) --------------------------------------------------- #
        self.queue = QueueController(self.settings, self)
        self.queue.blocker = self._queue_blocker
        self.queue_page = QueuePage(
            self.queue, self.settings,
            snapshot_display=lambda: self.ctx.profile.to_dict(),
            snapshot_preset=self.output_panel.preset_snapshot,
            open_experiments=lambda: [str(getattr(d, "root", "")) for d in getattr(self.ctx, "datasets", []) or []],
            parent=self)
        self.central.addWidget(self.queue_page)

        self.log_dock = LogDock(self.ctx, self, debug=bool(self.settings.log_debug))
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        # The log sits under the preview only: the explorer and Display & output keep the full height.
        self.setCorner(Qt.Corner.BottomLeftCorner, Qt.DockWidgetArea.LeftDockWidgetArea)
        self.setCorner(Qt.Corner.BottomRightCorner, Qt.DockWidgetArea.RightDockWidgetArea)
        self.log_dock.raise_()
        self.log_dock.set_theme(self.settings.theme or DEFAULT_THEME)

        # ---- collapsible panels (0.6) --------------------------------------- #
        self.panels = PanelManager(self)
        self.panels.add("explorer", self.explorer_dock, "left", "Explorer", "Ctrl+1")
        self.panels.add("right", self.right_dock, "right", "Display and output", "Ctrl+2")
        self.panels.add("log", self.log_dock, "bottom", "Log", "Ctrl+3", collapse_button=self.log_dock.collapse_button)
        self._before_results: dict[str, bool] | None = None

        # ---- actions, toolbar, menus --------------------------------------- #
        self.actions_api = Actions(self.ctx, self.runner, self.settings, window=self, output_panel=self.output_panel)
        self.toolbar = MainToolBar(self, self.actions_api, self)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.toolbar)
        self.toolbar.apply_theme(theme_module.colours(self.settings.theme or DEFAULT_THEME))
        self.actions_api.queue = self.queue
        self.central.currentChanged.connect(
            lambda _i: self.toolbar.queue_action.setChecked(self.central.currentWidget() is self.queue_page))
        self.welcome.queue_requested.connect(self._queue_from_welcome)
        self.queue.changed.connect(self._on_queue_changed)
        self.queue.progress.connect(self._on_queue_changed)
        self.welcome.open_requested.connect(lambda: self.actions_api.open_folder())
        self.welcome.recent_requested.connect(lambda folder: self.actions_api.open_folder(folder))
        self.welcome.set_recent(list(self.settings.recent_folders or []))
        self._build_menus()

        # ---- status bar ----------------------------------------------------- #
        self.status_label = QLabel("Ready")
        self.statusBar().addWidget(self.status_label, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(220)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        self.eta_label = QLabel("")  # 0.7: time remaining beside the progress bar
        self.statusBar().addPermanentWidget(self.eta_label)
        self.statusBar().addPermanentWidget(self.progress_bar)
        self._job_started = 0.0

        # ---- context wiring -------------------------------------------------- #
        self.ctx.job_state_changed.connect(self._on_job_state)
        self.ctx.job_progress.connect(self._on_job_progress)
        self.ctx.datasets_changed.connect(lambda _d: self._update_enabled())
        self.ctx.datasets_changed.connect(self._on_datasets)
        self.ctx.active_dataset_changed.connect(lambda _d: self._update_title())
        self.ctx.mode_changed.connect(self.toolbar.set_mode)
        self.ctx.theme_changed.connect(self._on_theme_changed)

        self._restore_layout()
        self._update_enabled()
        self.toolbar.set_mode(self.ctx.mode)
        self._on_queue_changed()
        log.info("%s %s ready", APP_NAME, VERSION)
        from PySide6.QtCore import QTimer  # noqa: WPS433

        QTimer.singleShot(0, self._offer_queue_resume)

    # ---- construction helpers -------------------------------------------- #
    def _dock(self, title: str, object_name: str, widget: QWidget, area) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(object_name)
        dock.setWidget(widget)
        dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.addDockWidget(area, dock)
        return dock

    def _build_menus(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        file_menu.addAction(self.toolbar.open_action)
        self.recent_menu = file_menu.addMenu("Open recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        file_menu.addAction(self.toolbar.quick_action)
        file_menu.addAction(self.toolbar.process_action)
        file_menu.addAction(self.toolbar.cancel_action)
        file_menu.addSeparator()
        results_folder = QAction("Open results folder", self)
        results_folder.triggered.connect(lambda: self.actions_api.open_results_folder())
        file_menu.addAction(results_folder)
        file_menu.addAction(self.toolbar.settings_action)
        file_menu.addSeparator()
        quit_action = QAction("Exit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        self.view_menu = bar.addMenu("&View")
        for key in ("explorer", "right", "log"):
            self.view_menu.addAction(self.panels.actions[key])
        self.view_menu.addSeparator()
        theme_menu = self.view_menu.addMenu("Theme")
        self.theme_group = QActionGroup(self)
        self.theme_group.setExclusive(True)
        for name in theme_module.THEMES:
            action = QAction(name.capitalize(), self)
            action.setCheckable(True)
            action.setChecked(self.settings.theme == name)
            action.setData(name)
            self.theme_group.addAction(action)
            theme_menu.addAction(action)
        self.theme_group.triggered.connect(lambda a: self.actions_api.set_theme(str(a.data())))
        self.view_menu.addSeparator()
        reset = QAction("Reset layout", self)
        reset.triggered.connect(self.reset_layout)
        self.view_menu.addAction(reset)

        help_menu = bar.addMenu("&Help")
        self.update_action = QAction("Check for updates…", self)
        self.update_action.setStatusTip("Ask GitHub whether a newer version exists. Only when you click; "
                                        "nothing is downloaded or installed.")
        self.update_action.triggered.connect(self.check_for_updates)
        help_menu.addAction(self.update_action)
        help_menu.addSeparator()
        about = QAction("About", self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    # ---- public API -------------------------------------------------------- #
    def show_results(self) -> None:
        """Show every video and image of the runs in the central area (click again for the preview)."""
        if self.central.currentWidget() is self.results_view:
            self.show_preview()
            return
        # The results page needs the room: collapse Display & output and the log, restore them on return.
        self._before_results = self.panels.snapshot(("right", "log"))
        self.panels.set_open("right", False)
        self.panels.set_open("log", False)
        self.central.setCurrentWidget(self.results_view)
        self.toolbar.results_action.setText("Preview")
        self.actions_api.refresh_results()

    def show_queue(self) -> None:
        """The queue page in the central area (0.8); click again for the preview."""
        if self.central.currentWidget() is self.queue_page:
            self.show_preview()
            return
        if self.central.currentWidget() is self.results_view:
            self.show_preview()
        self.central.setCurrentWidget(self.queue_page)
        self.queue_page.refresh(force=True)

    def _queue_from_welcome(self) -> None:
        self.show_queue()
        self.queue_page.add_folder_dialog()

    def _queue_blocker(self) -> str:
        if self.runner.busy and self.runner.kind in ("quick", "process", "batch"):
            return "wait for the running Quick video or Process to finish, or cancel it, before starting the queue."
        return ""

    def _on_queue_changed(self, *_args) -> None:
        queue = self.queue
        waiting = len(queue.pending())
        if queue.running:
            number, total = queue.position()
            text = f"Queue {number}/{total}"
        else:
            text = f"Queue ({waiting})" if waiting else "Queue"
        self.toolbar.queue_action.setText(text)
        self._update_enabled()
        if self._close_when_queue_idle and not queue.running:
            self._close_when_queue_idle = False
            from PySide6.QtCore import QTimer  # noqa: WPS433

            QTimer.singleShot(0, self.close)

    def _offer_queue_resume(self) -> None:
        """Items left from the last session: Resume or Clear (0.8)."""
        left = self.queue.unfinished()
        if not left:
            return
        self.queue.recover()
        log.info("Queue: %d item%s left from the last session", len(left), "s" if len(left) != 1 else "")
        from PySide6.QtWidgets import QApplication  # noqa: WPS433

        if QApplication.platformName() == "offscreen":
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Queue")
        box.setText(f"{len(left)} item{'s' if len(left) != 1 else ''} left in the queue from last time.")
        box.setInformativeText("Resume runs them now. Clear empties the queue. Later keeps them waiting.")
        resume = box.addButton("Resume", QMessageBox.ButtonRole.AcceptRole)
        clear = box.addButton("Clear", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        box.buttonClicked.connect(lambda button: self._queue_resume_answer(button is resume, button is clear))
        self._message_boxes.append(box)
        box.open()

    def _queue_resume_answer(self, resume: bool, clear: bool) -> None:
        if resume:
            if self.central.currentWidget() is not self.queue_page:
                self.show_queue()
            self.queue_page.start()
        elif clear:
            self.queue.clear_all()

    def ask_close_during_queue(self) -> str:
        """'after' | 'now' | 'keep' while the queue runs; separated out so tests can answer it."""
        from PySide6.QtWidgets import QApplication  # noqa: WPS433

        if QApplication.platformName() == "offscreen":
            return "now"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("The queue is running")
        box.setText(self.queue_page.status_label.text())
        box.setInformativeText("Stop after the current item, stop now (it starts again from the beginning next "
                               "time), or keep the window open? Waiting items stay in the queue.")
        after = box.addButton("Stop after this item", QMessageBox.ButtonRole.AcceptRole)
        now = box.addButton("Stop now", QMessageBox.ButtonRole.DestructiveRole)
        keep = box.addButton("Keep open", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.exec()
        clicked = box.clickedButton()
        return "after" if clicked is after else "now" if clicked is now else "keep"

    def check_for_updates(self) -> None:
        """Help → Check for updates (0.8): one request to GitHub, only when asked."""
        from . import update_check  # noqa: WPS433
        from .workers import run_in_background  # noqa: WPS433

        log.info("Checking GitHub for a newer version…")
        self.update_action.setEnabled(False)
        self._update_relay = run_in_background(
            lambda: update_check.check_latest(), on_done=self._show_update_result,
            on_failed=lambda message: self._show_update_result(update_check.UpdateResult("error", "", "", message)))

    def _show_update_result(self, result) -> None:
        from PySide6.QtCore import QUrl  # noqa: WPS433
        from PySide6.QtGui import QDesktopServices  # noqa: WPS433

        self.update_action.setEnabled(True)
        self.last_update_result = result
        (log.warning if result.status == "error" else log.info)("Check for updates: %s", result.message)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Check for updates")
        box.setText(result.message)
        if result.status == "newer" and result.url:
            box.setInformativeText("The release page lists what changed and has the new installer.")
            button = box.addButton("Open the release page", QMessageBox.ButtonRole.AcceptRole)
            button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(result.url)))
        box.addButton(QMessageBox.StandardButton.Close)
        self._message_boxes.append(box)
        box.open()

    def show_preview(self) -> None:
        self.results_view.show_grid()
        self.central.setCurrentWidget(self.viewer if getattr(self.ctx, "datasets", []) else self.welcome)
        self.toolbar.results_action.setText("Results")
        if self._before_results is not None:
            self.panels.restore(self._before_results)
            self._before_results = None

    def show_error(self, title: str, text: str) -> None:
        """Non-modal error box (a modal dialog would block headless runs)."""
        box = QMessageBox(QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.Ok, self)
        box.finished.connect(lambda _r: self._message_boxes.remove(box) if box in self._message_boxes else None)
        self._message_boxes.append(box)
        box.open()

    def apply_settings(self, settings: Settings) -> None:
        self.queue.settings = settings
        self.queue_page.settings = settings
        self.settings = settings
        self.output_panel.apply_settings(settings)
        self.log_dock.set_debug_visible(bool(settings.log_debug))
        self.log_dock.set_theme(settings.theme)

    def reset_layout(self) -> None:
        log.debug("reset_layout()")
        for dock, area in (
            (self.explorer_dock, Qt.DockWidgetArea.LeftDockWidgetArea),
            (self.right_dock, Qt.DockWidgetArea.RightDockWidgetArea),
            (self.log_dock, Qt.DockWidgetArea.BottomDockWidgetArea),
        ):
            dock.setFloating(False)
            dock.setVisible(True)
            self.addDockWidget(area, dock)
        self.toolbar.setVisible(True)
        for key in self.panels.docks:
            self.panels.set_open(key, True)
        self.resize(1360, 860)
        log.info("Layout reset.")

    # ---- context reactions -------------------------------------------------- #
    def _update_enabled(self) -> None:
        has_dataset = bool(getattr(self.ctx, "datasets", []))
        busy = self.ctx.job_status == "running"
        self.toolbar.update_enabled(has_dataset, busy, bool(getattr(self, "queue", None) and self.queue.running))

    def _update_title(self) -> None:
        name = getattr(getattr(self.ctx, "active", None), "name", "")
        self.setWindowTitle(f"{name} — {APP_NAME} {VERSION}" if name else f"{APP_NAME} {VERSION}")

    def _on_datasets(self, datasets) -> None:
        if datasets and self.central.currentWidget() is self.welcome:
            self.central.setCurrentWidget(self.viewer)
        elif not datasets and self.central.currentWidget() is self.viewer:
            self.central.setCurrentWidget(self.welcome)
        self.welcome.set_recent(list(self.settings.recent_folders or []))

    def _fill_recent_menu(self) -> None:
        self.recent_menu.clear()
        folders = [f for f in list(self.settings.recent_folders or []) if Path(f).is_dir()]
        for folder in folders:
            action = self.recent_menu.addAction(folder)
            action.triggered.connect(lambda _checked=False, f=folder: self.actions_api.open_folder(f))
        if not folders:
            self.recent_menu.addAction("No recent folders").setEnabled(False)

    def _remember_section(self, key: str, expanded: bool) -> None:
        self.settings.extra.setdefault("sections", {})[key] = bool(expanded)

    def _on_job_state(self, status: str, message: str) -> None:
        self.status_label.setText(message or status.capitalize())
        running = status == "running"
        if running and not self._job_started:
            self._job_started = time.monotonic()
        if not running:
            self._job_started = 0.0
            self.eta_label.setText("")
        self.progress_bar.setVisible(running)
        if not running:
            self.progress_bar.setValue(0)
        self._update_enabled()
        if not running and self._close_when_idle:
            self._close_when_idle = False
            self.close()

    def _on_job_progress(self, percent: int) -> None:
        if percent is None or percent < 0:
            self.progress_bar.setRange(0, 0)  # indeterminate
            return
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(int(percent))
        elapsed = time.monotonic() - self._job_started if self._job_started else 0.0
        if 3 <= percent < 100 and elapsed > 4.0:
            self.eta_label.setText(f"about {_duration(elapsed * (100 - percent) / percent)} left")

    def _on_theme_changed(self, name: str) -> None:
        self.log_dock.set_theme(name)
        self.toolbar.apply_theme(theme_module.colours(name))
        for action in self.theme_group.actions():
            if str(action.data()) == name and not action.isChecked():
                blocked = self.theme_group.blockSignals(True)
                action.setChecked(True)
                self.theme_group.blockSignals(blocked)

    def _about(self) -> None:
        from .about_dialog import AboutDialog  # noqa: WPS433
        from .settings import logs_dir, user_data_dir  # noqa: WPS433

        self.about_dialog = AboutDialog(str(user_data_dir()), str(logs_dir()), self)
        self._message_boxes.append(self.about_dialog)
        self.about_dialog.open()

    # ---- drag and drop ------------------------------------------------------ #
    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._dropped_folder(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt API
        folder = self._dropped_folder(event)
        if folder is None:
            event.ignore()
            return
        event.acceptProposedAction()
        log.info("Folder dropped: %s", folder)
        self.actions_api.open_folder(folder)

    @staticmethod
    def _dropped_folder(event) -> str | None:
        data = event.mimeData()
        if data is None or not data.hasUrls():
            return None
        for url in data.urls():
            path = Path(url.toLocalFile())
            if path.is_dir():
                return str(path)
        return None

    # ---- layout persistence and shutdown ------------------------------------ #
    def _restore_layout(self) -> None:
        geometry, state = self.settings.window_geometry, self.settings.window_state
        try:
            if geometry:
                self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
            if state:
                self.restoreState(QByteArray.fromBase64(state.encode("ascii")), LAYOUT_VERSION)
        except Exception as exc:  # a broken saved layout must never stop the app
            log.debug("Saved layout could not be restored: %s", exc)
        self.panels.sync_from_docks()

    def createPopupMenu(self):  # noqa: N802 - Qt API
        """No right-click menu of docks and toolbars: the panel strips keep the panels in step."""
        return None

    def save_layout(self) -> None:
        self.settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        self.settings.window_state = bytes(self.saveState(LAYOUT_VERSION).toBase64()).decode("ascii")

    def ask_close_during_job(self) -> str:
        """'wait' | 'cancel' | 'keep' — separated out so tests can answer it."""
        from PySide6.QtWidgets import QApplication  # noqa: WPS433

        if QApplication.platformName() == "offscreen":  # no screen: nobody can answer a modal box
            return "cancel"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("A job is running")
        box.setText(f"{self.ctx.job_status.capitalize()}: {self.status_label.text()}")
        box.setInformativeText("Close when the job finishes, cancel it and close, or keep the window open?")
        wait_button = box.addButton("Wait and close", QMessageBox.ButtonRole.AcceptRole)
        cancel_button = box.addButton("Cancel and close", QMessageBox.ButtonRole.DestructiveRole)
        keep_button = box.addButton("Keep open", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is wait_button:
            return "wait"
        if clicked is cancel_button:
            return "cancel"
        return "keep"

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self.queue.running:
            answer = self.ask_close_during_queue() if not self.isHidden() else "now"
            log.info("Close while the queue runs: %s", answer)
            if answer == "keep":
                event.ignore()
                return
            if answer == "after":
                self.queue.pause_after_current()
            else:
                self.queue.cancel(requeue=True)
            self._close_when_queue_idle = True
            event.ignore()
            return
        if self.runner.busy:
            # A window that is not on screen (tests, a frozen shutdown) cannot ask anybody.
            answer = self.ask_close_during_job() if not self.isHidden() else "cancel"
            log.info("Close during a job: %s", answer)
            if answer == "keep":
                event.ignore()
                return
            if answer == "cancel":
                self.actions_api.cancel()
            self._close_when_idle = True
            event.ignore()
            return
        try:
            self.experiment_memory.flush()
            self.save_layout()
            self.output_panel.apply_to_settings(self.settings)
            self.settings.log_debug = self.log_dock.debug_visible
            self.settings.save()
        except Exception as exc:  # pragma: no cover - saving must never block closing
            log.debug("Settings could not be saved: %s", exc)
        self.log_dock.detach()
        self.runner.wait(2000)
        self.queue.runner.wait(2000)
        log.info("%s closed.", APP_NAME)
        super().closeEvent(event)


def _duration(seconds: float) -> str:
    """45 s, 3 min 05 s, 1 h 12 min."""
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60:02d} s"
    return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"
