"""The queue (0.8): experiment folders processed one after another while nobody watches.

``QueueController`` owns the list and its own ``JobRunner``, so opening and previewing other
folders keeps working while the queue runs. ``QueuePage`` is its page in the central area,
opened from the Queue button beside Open folder. The app never touches sleep, shutdown or other
system settings while the queue runs.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import presets_store
from . import queue_model as qm
from .jobs_qt import JobRunner

log = logging.getLogger("etaluma.ui")

IDLE, RUNNING, PAUSING, PAUSED = "idle", "running", "pausing", "paused"


class QueueController(QObject):
    changed = Signal()  # the list or the state changed
    progress = Signal(int, str)  # percent of the current item, its last message
    finished = Signal(str)  # the report file

    def __init__(self, settings, parent: QObject | None = None, path: Path | str | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._path = path
        self.items: list[qm.QueueItem] = qm.load_queue(path)
        self.runner = JobRunner(self)
        self.state = IDLE
        self.current: qm.QueueItem | None = None
        self.percent = 0
        self.message = ""
        self.last_report: Path | None = None
        self.blocker: Callable[[], str] = lambda: ""  # the window refuses a start during a Process
        self._run_started = 0.0
        self._skip = self._stop = self._requeue = False
        self.runner.progress.connect(self._on_progress)
        self.runner.percent.connect(self._on_percent)
        self.runner.finished.connect(self._on_finished)
        self.runner.failed.connect(self._on_failed)
        self.runner.cancelled.connect(self._on_cancelled)

    # ---- state -------------------------------------------------------------------------- #
    @property
    def running(self) -> bool:
        return self.state in (RUNNING, PAUSING)

    def pending(self) -> list[qm.QueueItem]:
        return [i for i in self.items if i.status == qm.PENDING]

    def unfinished(self) -> list[qm.QueueItem]:
        return [i for i in self.items if i.status in (qm.PENDING, qm.RUNNING)]

    def item(self, item_id: str) -> qm.QueueItem | None:
        return next((i for i in self.items if i.id == item_id), None)

    def position(self) -> tuple[int, int]:
        """(number of the current item, items in this run) for 'Running 2 of 5'."""
        ran = [i for i in self.items if i.started_at >= self._run_started and i.status in qm.FINISHED]
        current = 1 if self.current is not None else 0
        return len(ran) + current, len(ran) + current + len(self.pending())

    # ---- editing ------------------------------------------------------------------------ #
    def add_folders(self, folders, display: str | None = None, preset: str | None = None,
                    display_snapshot: dict | None = None, preset_snapshot: dict | None = None) -> list[qm.QueueItem]:
        display = display or self.settings.queue_display or presets_store.AUTO
        preset = preset or self.settings.queue_preset or "builtin:standard"
        waiting = {(i.folder.casefold(), i.display, i.preset) for i in self.pending()}
        added = []
        for folder in qm.expand_folders(folders):
            key = (folder.casefold(), display, preset)
            if key in waiting:
                log.info("Queue: %s is already waiting with the same choices", Path(folder).name)
                continue
            item = qm.QueueItem(folder=folder, display=display, preset=preset,
                                display_snapshot=display_snapshot if display == presets_store.CURRENT else None,
                                preset_snapshot=preset_snapshot if preset == presets_store.CURRENT else None)
            self.items.append(item)
            added.append(item)
            waiting.add(key)
        if added:
            log.info("Queue: added %s (display %s, output preset %s)", ", ".join(i.name for i in added),
                     presets_store.display_label(display), presets_store.preset_label(preset))
            self._changed()
        return added

    def set_choice(self, item_id: str, display: str | None = None, preset: str | None = None,
                   display_snapshot: dict | None = None, preset_snapshot: dict | None = None) -> None:
        """Change one waiting item (saved at once; the page keeps the combo that made the change)."""
        item = self.item(item_id)
        if item is None or item.status != qm.PENDING:
            return
        if display is not None:
            item.display = display
            item.display_snapshot = display_snapshot if display == presets_store.CURRENT else None
        if preset is not None:
            item.preset = preset
            item.preset_snapshot = preset_snapshot if preset == presets_store.CURRENT else None
        self._save()

    def remove(self, ids) -> None:
        ids = set(ids)
        self.items = [i for i in self.items if i.id not in ids or i is self.current]
        self._changed()

    def reorder(self, ids: list[str]) -> None:
        order = {item_id: n for n, item_id in enumerate(ids)}
        self.items.sort(key=lambda i: order.get(i.id, len(order)))
        self._changed()

    def clear_finished(self) -> None:
        self.items = [i for i in self.items if i.status not in qm.FINISHED]
        self._changed()

    def clear_all(self) -> None:
        if self.running:
            return
        self.items = []
        self._changed()

    # ---- running ------------------------------------------------------------------------ #
    def start(self) -> bool:
        if self.running or self.runner.busy or not self.pending():
            return False
        reason = self.blocker()
        if reason:
            log.error("Queue: %s", reason)
            return False
        self.state = RUNNING
        self._run_started = time.time()
        self._skip = self._stop = self._requeue = False
        log.info("Queue: starting, %d item%s waiting", len(self.pending()), "s" if len(self.pending()) != 1 else "")
        self._next()
        return True

    def pause_after_current(self) -> None:
        if self.state == RUNNING:
            self.state = PAUSING
            log.info("Queue: pausing after %s", self.current.name if self.current else "the current item")
            self.changed.emit()

    def skip_current(self) -> None:
        if self.current is not None and self.runner.busy:
            self._skip = True
            log.info("Queue: skipping %s", self.current.name)
            self.runner.cancel()

    def cancel(self, requeue: bool = False) -> None:
        """Stop the queue now. ``requeue``: the current item waits again (the app is closing)."""
        if not self.running:
            return
        self._stop, self._requeue = True, requeue
        if self.runner.busy:
            self.runner.cancel()
        else:
            self._end(IDLE)

    def recover(self) -> int:
        """Items a crash left Running wait again; their partial output is labelled incomplete."""
        count = 0
        for item in self.items:
            if item.status == qm.RUNNING:
                self._label_incomplete(item)
                item.status = qm.PENDING
                item.message = "Starts again from the beginning: the app closed while it was running"
                count += 1
        if count:
            self._changed()
        return count

    def _next(self) -> None:
        if self.state == PAUSING:
            self._end(PAUSED)
            return
        waiting = self.pending()
        if not waiting:
            self._end(IDLE, finished=True)
            return
        item = waiting[0]
        parent = qm.output_parent(item, self.settings.queue_output_mode, self.settings.queue_output_folder)
        item.status, item.parent, item.output, item.message = qm.RUNNING, str(parent), "", ""
        item.started_at, item.seconds = time.time(), 0.0
        self.current, self.percent, self.message = item, 0, ""
        number, total = self.position()
        log.info("Queue %d/%d: %s (display %s, output preset %s) → %s", number, total, item.name,
                 presets_store.display_label(item.display), presets_store.preset_label(item.preset), parent)
        self._changed()
        try:
            self.runner.start("queue", qm.run_item, item.to_dict(), str(parent))
        except RuntimeError as exc:  # pragma: no cover - the runner is only ours
            self._on_failed("queue", str(exc))

    def _on_progress(self, message: str) -> None:
        self.message = message
        self.progress.emit(self.percent, message)

    def _on_percent(self, percent: int) -> None:
        if percent >= 0:
            self.percent = int(percent)
            self.progress.emit(self.percent, self.message)

    def _elapsed(self, item: qm.QueueItem) -> float:
        return round(time.time() - item.started_at, 1) if item.started_at else 0.0

    def _on_finished(self, _kind: str, result) -> None:
        item, result = self.current, dict(result or {})
        if item is None:
            return
        item.status = qm.DONE
        item.output = str(result.get("output", ""))
        item.seconds = self._elapsed(item)
        item.videos = int(result.get("videos", 0) or 0)
        warnings = list(result.get("warnings") or [])
        item.message = f"{len(warnings)} warning{'s' if len(warnings) != 1 else ''}, see the log" if warnings else ""
        log.info("Queue: %s done in %s → %s", item.name, qm.duration(item.seconds), item.output)
        self._after_item()

    def _on_failed(self, _kind: str, message: str) -> None:
        item = self.current
        if item is None:
            return
        item.seconds = self._elapsed(item)
        self._label_incomplete(item)
        if "disk" in message.lower() and "full" in message.lower():
            item.status = qm.PENDING
            item.message = "The disk was full. Free some space, then press Resume: this item starts again."
            log.error("Queue paused: %s", message)
            self.state = PAUSING
        else:
            item.status, item.message = qm.FAILED, message
            log.error("Queue: %s failed: %s", item.name, message)
        self._after_item()

    def _on_cancelled(self, _kind: str) -> None:
        item = self.current
        if item is None:
            return
        item.seconds = self._elapsed(item)
        self._label_incomplete(item)
        if self._requeue:
            item.status = qm.PENDING
            item.message = "Stopped when the app closed; it starts again from the beginning"
        elif self._skip:
            item.status, item.message = qm.SKIPPED, "Skipped"
        else:
            item.status, item.message = qm.CANCELLED, "Cancelled"
        self._skip = False
        if self._stop:
            self.current = None
            self._end(IDLE, finished=True)
            return
        self._after_item()

    def _after_item(self) -> None:
        self.current = None
        self._changed()
        self._next()

    def _end(self, state: str, finished: bool = False) -> None:
        self.state, self.current = state, None
        waiting = len(self.pending())
        if state == PAUSED:
            log.info("Queue paused: %d item%s waiting", waiting, "s" if waiting != 1 else "")
        ran = [i for i in self.items if i.started_at >= self._run_started and i.status in qm.FINISHED]
        if finished and ran:
            self.last_report = qm.write_report(self.items, self._run_started, time.time())
            done = sum(1 for i in ran if i.status == qm.DONE)
            failed = sum(1 for i in ran if i.status == qm.FAILED)
            log.info("Queue finished: %d done, %d failed, %d other, %d waiting. Report: %s",
                     done, failed, len(ran) - done - failed, waiting, self.last_report)
            self._changed()
            self.finished.emit(str(self.last_report))
            return
        self._changed()

    def _label_incomplete(self, item: qm.QueueItem) -> None:
        for folder in qm.label_incomplete(item.parent, item.started_at):
            log.info("Queue: the partial output of %s is kept as %s", item.name, folder)

    def _save(self) -> None:
        qm.save_queue(self.items, self._path)

    def _changed(self) -> None:
        self._save()
        self.changed.emit()


class _QueueTree(QTreeWidget):
    """The list: rows move by dragging; folders dropped from Explorer become items."""

    reordered = Signal(list)
    folders_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setRootIsDecorated(False)
        self.setAlternatingRowColors(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setAcceptDrops(True)

    @staticmethod
    def _folders(event) -> list[str]:
        mime = event.mimeData()
        if event.source() is not None or not mime.hasUrls():
            return []
        return [u.toLocalFile() for u in mime.urls() if u.isLocalFile() and Path(u.toLocalFile()).is_dir()]

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._folders(event):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._folders(event):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt API
        folders = self._folders(event)
        if folders:
            event.acceptProposedAction()
            self.folders_dropped.emit(folders)
            return
        super().dropEvent(event)
        self.reordered.emit([self.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
                             for i in range(self.topLevelItemCount())])


class QueuePage(QWidget):
    COLUMNS = ("#", "Experiment", "Display", "Output preset", "Status", "Time", "Output or message")

    def __init__(self, controller: QueueController, settings, snapshot_display=None, snapshot_preset=None,
                 open_experiments=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.settings = settings
        self._snapshot_display = snapshot_display or (lambda: None)
        self._snapshot_preset = snapshot_preset or (lambda: None)
        self._open_experiments = open_experiments or (lambda: [])
        self._signature = None
        self._rows: dict[str, QTreeWidgetItem] = {}
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 12)
        layout.setSpacing(8)
        head = QHBoxLayout()
        title = QLabel("Queue")
        title.setProperty("role", "title")
        self.status_label = QLabel("")
        self.status_label.setProperty("role", "subtitle")
        head.addWidget(title)
        head.addSpacing(14)
        head.addWidget(self.status_label, 1)
        layout.addLayout(head)
        intro = QLabel("Experiment folders processed one after another, unattended. Each item uses its display and "
                       "output preset; channel names, start time and timepoint range come from what each experiment "
                       "remembers. Drag folders here from Explorer, and drag rows to change the order.")
        intro.setWordWrap(True)
        intro.setProperty("role", "muted")
        layout.addWidget(intro)

        defaults = QHBoxLayout()
        defaults.setSpacing(6)
        self.display_combo = QComboBox()
        self.display_combo.setToolTip("Display of the items you add next. Current display settings are copied "
                                      "when the item is added; later changes in the Display panel do not affect it.")
        self.preset_combo = QComboBox()
        self.preset_combo.setToolTip("Output preset of the items you add next")
        for text, combo in (("New items: display", self.display_combo), ("output preset", self.preset_combo)):
            label = QLabel(text)
            label.setProperty("role", "muted")
            defaults.addWidget(label)
            defaults.addWidget(combo, 1)
        layout.addLayout(defaults)

        output_row = QHBoxLayout()
        output_row.setSpacing(6)
        self.output_combo = QComboBox()
        self.output_combo.addItem("Each experiment's own analysis_output", "experiment")
        self.output_combo.addItem("One folder, with a subfolder per experiment", "folder")
        self.output_combo.setCurrentIndex(max(0, self.output_combo.findData(settings.queue_output_mode or "experiment")))
        self.folder_edit = QLineEdit(settings.queue_output_folder or "")
        self.folder_edit.setPlaceholderText("Common output folder")
        self.browse_button = QPushButton("Browse…")
        label = QLabel("Outputs")
        label.setProperty("role", "muted")
        output_row.addWidget(label)
        output_row.addWidget(self.output_combo)
        output_row.addWidget(self.folder_edit, 1)
        output_row.addWidget(self.browse_button)
        layout.addLayout(output_row)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.add_button = QPushButton("Add folder…")
        self.add_button.setToolTip("An experiment folder, or a folder of experiments: each one inside becomes an item")
        self.add_open_button = QPushButton("Add the open experiments")
        self.remove_button = QPushButton("Remove")
        self.clear_button = QPushButton("Clear finished")
        self.start_button = QPushButton("Start")
        self.start_button.setProperty("role", "primary")
        self.pause_button = QPushButton("Pause after this item")
        self.skip_button = QPushButton("Skip current")
        self.cancel_button = QPushButton("Cancel")
        for button in (self.add_button, self.add_open_button, self.remove_button, self.clear_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        for button in (self.start_button, self.pause_button, self.skip_button, self.cancel_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)

        self.tree = _QueueTree(self)
        self.tree.setHeaderLabels(list(self.COLUMNS))
        header = self.tree.header()
        header.setStretchLastSection(True)
        for column in (0, 2, 3, 4, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(1, 260)
        layout.addWidget(self.tree, 1)

        footer = QHBoxLayout()
        self.report_label = QLabel("")
        self.report_label.setProperty("role", "muted")
        self.report_button = QPushButton("Open report")
        self.report_button.setVisible(False)
        footer.addWidget(self.report_label, 1)
        footer.addWidget(self.report_button)
        layout.addLayout(footer)

        self.display_combo.currentIndexChanged.connect(self._on_default_display)
        self.preset_combo.currentIndexChanged.connect(self._on_default_preset)
        self.output_combo.currentIndexChanged.connect(self._on_output_mode)
        self.folder_edit.textChanged.connect(self._on_output_folder)
        self.browse_button.clicked.connect(self._browse_output)
        self.add_button.clicked.connect(self.add_folder_dialog)
        self.add_open_button.clicked.connect(self.add_open_experiments)
        self.remove_button.clicked.connect(self._remove_selected)
        self.clear_button.clicked.connect(controller.clear_finished)
        self.start_button.clicked.connect(self.start)
        self.pause_button.clicked.connect(controller.pause_after_current)
        self.skip_button.clicked.connect(controller.skip_current)
        self.cancel_button.clicked.connect(lambda: controller.cancel())
        self.report_button.clicked.connect(self._open_report)
        self.tree.reordered.connect(controller.reorder)
        self.tree.folders_dropped.connect(self.add_folders)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.itemDoubleClicked.connect(self._open_output)
        controller.changed.connect(self.refresh)
        controller.progress.connect(self._on_progress)

        self.fill_default_combos()
        self._on_output_mode()
        self.refresh(force=True)

    # ---- defaults for new items -------------------------------------------------------- #
    def fill_default_combos(self) -> None:
        groups = ((self.display_combo, presets_store.display_choices(), self.settings.queue_display or presets_store.AUTO),
                  (self.preset_combo,
                   presets_store.preset_choices() + [(presets_store.CURRENT, presets_store.CURRENT_PRESET_LABEL)],
                   self.settings.queue_preset or "builtin:standard"))
        for combo, choices, current in groups:
            combo.blockSignals(True)
            combo.clear()
            for key, label in choices:
                combo.addItem(label, key)
            combo.setCurrentIndex(max(0, combo.findData(current)))
            combo.blockSignals(False)

    def _on_default_display(self, _index: int) -> None:
        self.settings.queue_display = str(self.display_combo.currentData() or presets_store.AUTO)

    def _on_default_preset(self, _index: int) -> None:
        self.settings.queue_preset = str(self.preset_combo.currentData() or "builtin:standard")

    def _on_output_mode(self, *_args) -> None:
        mode = str(self.output_combo.currentData() or "experiment")
        self.settings.queue_output_mode = mode
        self.folder_edit.setEnabled(mode == "folder")
        self.browse_button.setEnabled(mode == "folder")

    def _on_output_folder(self, text: str) -> None:
        self.settings.queue_output_folder = text.strip()

    def _browse_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Common output folder of the queue",
                                                  self.folder_edit.text() or str(Path.home()))
        if folder:
            self.folder_edit.setText(folder)

    # ---- adding ------------------------------------------------------------------------ #
    def add_folder_dialog(self) -> None:
        start = getattr(self.settings, "last_folder", "") or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Add an experiment folder, or a folder of experiments", start)
        if folder:
            self.add_folders([folder])

    def add_folders(self, folders) -> list:
        display = str(self.display_combo.currentData() or presets_store.AUTO)
        preset = str(self.preset_combo.currentData() or "builtin:standard")
        added = self.controller.add_folders(
            folders, display=display, preset=preset,
            display_snapshot=self._snapshot_display() if display == presets_store.CURRENT else None,
            preset_snapshot=self._snapshot_preset() if preset == presets_store.CURRENT else None)
        if not added:
            log.info("Queue: nothing new to add from %s", ", ".join(Path(str(f)).name for f in folders))
        return added

    def add_open_experiments(self) -> list:
        folders = [f for f in self._open_experiments() if f]
        if not folders:
            log.info("Queue: open an experiment first, or use Add folder…")
            return []
        return self.add_folders(folders)

    def start(self) -> bool:
        if self.settings.queue_output_mode == "folder" and not str(self.settings.queue_output_folder or "").strip():
            log.error("Queue: choose the common output folder first (Outputs, Browse…).")
            return False
        return self.controller.start()

    # ---- the list ---------------------------------------------------------------------- #
    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.fill_default_combos()  # profiles and presets saved meanwhile
        self.refresh(force=True)
        super().showEvent(event)

    def refresh(self, *_args, force: bool = False) -> None:
        items = self.controller.items
        signature = tuple((i.id, i.status, i.display, i.preset) for i in items)
        if force or signature != self._signature:
            self._rebuild(items)
            self._signature = signature
        else:
            self._update_texts(items)
        self._update_buttons()
        self._update_status()
        report = self.controller.last_report
        if report is not None:
            from .settings import friendly_path  # noqa: WPS433

            self.report_label.setText(f"Last report: {friendly_path(str(report))}")
        self.report_button.setVisible(report is not None)

    def _rebuild(self, items) -> None:
        self.tree.clear()
        self._rows.clear()
        for number, item in enumerate(items, start=1):
            row = QTreeWidgetItem([str(number), item.name, "", "", "", "", ""])
            row.setData(0, Qt.ItemDataRole.UserRole, item.id)
            row.setToolTip(1, item.folder)
            flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
            if item.status == qm.PENDING:
                flags |= Qt.ItemFlag.ItemIsDragEnabled
            row.setFlags(flags)
            self.tree.addTopLevelItem(row)
            self._rows[item.id] = row
            if item.status == qm.PENDING:
                self.tree.setItemWidget(row, 2, self._row_combo(item, "display"))
                self.tree.setItemWidget(row, 3, self._row_combo(item, "preset"))
            else:
                row.setText(2, presets_store.display_label(item.display))
                row.setText(3, presets_store.preset_label(item.preset))
        self._update_texts(items)

    def _row_combo(self, item, which: str) -> QComboBox:
        combo = QComboBox()
        if which == "display":
            choices, current, snapshot = presets_store.display_choices(), item.display, item.display_snapshot
            when_added = "Current display settings (when added)"
        else:
            choices = presets_store.preset_choices() + [(presets_store.CURRENT, presets_store.CURRENT_PRESET_LABEL)]
            current, snapshot, when_added = item.preset, item.preset_snapshot, "Current output settings (when added)"
        for key, label in choices:
            combo.addItem(when_added if key == presets_store.CURRENT and current == key and snapshot else label, key)
        index = combo.findData(current)
        if index < 0:
            combo.addItem(presets_store.display_label(current) if which == "display"
                          else presets_store.preset_label(current), current)
            index = combo.count() - 1
        combo.setCurrentIndex(index)
        combo.currentIndexChanged.connect(lambda _i, c=combo, i=item.id, w=which: self._row_changed(i, w, c.currentData()))
        return combo

    def _row_changed(self, item_id: str, which: str, key) -> None:
        key = str(key)
        if which == "display":
            snapshot = self._snapshot_display() if key == presets_store.CURRENT else None
            self.controller.set_choice(item_id, display=key, display_snapshot=snapshot)
        else:
            snapshot = self._snapshot_preset() if key == presets_store.CURRENT else None
            self.controller.set_choice(item_id, preset=key, preset_snapshot=snapshot)

    def _update_texts(self, items) -> None:
        for item in items:
            row = self._rows.get(item.id)
            if row is None:
                continue
            status = qm.STATUS_LABELS.get(item.status, item.status)
            if item is self.controller.current:
                status = f"Running · {self.controller.percent} %"
                seconds = time.time() - item.started_at if item.started_at else 0.0
            else:
                seconds = item.seconds
            row.setText(4, status)
            row.setText(5, qm.duration(seconds) if seconds else "")
            detail = item.output or item.message
            row.setText(6, detail)
            row.setToolTip(6, detail)

    def _on_progress(self, _percent: int, _message: str) -> None:
        current = self.controller.current
        if current is not None:
            self._update_texts([current])
        self._update_status()

    def _update_status(self) -> None:
        c = self.controller
        waiting = len(c.pending())
        if c.running and c.current is not None:
            number, total = c.position()
            text = f"Running {number} of {total}: {c.current.name} · {c.percent} %"
            if c.state == PAUSING:
                text += " · pausing after this item"
        elif c.state == PAUSED:
            text = f"Paused · {waiting} waiting"
        elif waiting:
            text = f"{waiting} waiting"
        elif c.items:
            text = "Nothing waiting"
        else:
            text = "Empty: add experiment folders"
        self.status_label.setText(text)

    def _selected_ids(self, removable: bool = False) -> list[str]:
        ids = [row.data(0, Qt.ItemDataRole.UserRole) for row in self.tree.selectedItems()]
        if removable:
            current = self.controller.current
            ids = [i for i in ids if current is None or i != current.id]
        return ids

    def _update_buttons(self) -> None:
        c = self.controller
        waiting = bool(c.pending())
        self.start_button.setText("Resume" if c.state == PAUSED else "Start")
        self.start_button.setEnabled(not c.running and waiting)
        self.pause_button.setEnabled(c.state == RUNNING)
        self.pause_button.setText("Pausing after this item…" if c.state == PAUSING else "Pause after this item")
        self.skip_button.setEnabled(c.running and c.current is not None)
        self.cancel_button.setEnabled(c.running)
        self.clear_button.setEnabled(any(i.status in qm.FINISHED for i in c.items))
        self.remove_button.setEnabled(bool(self._selected_ids(removable=True)))

    def _remove_selected(self) -> None:
        ids = self._selected_ids(removable=True)
        if ids:
            self.controller.remove(ids)

    def _open_report(self) -> None:
        if self.controller.last_report is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.controller.last_report)))

    def _open_output(self, row: QTreeWidgetItem, _column: int) -> None:
        item = self.controller.item(row.data(0, Qt.ItemDataRole.UserRole))
        target = (item.output or item.parent) if item is not None else ""
        if target and Path(target).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    # ---- drops outside the list -------------------------------------------------------- #
    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt API
        folders = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile() and Path(u.toLocalFile()).is_dir()]
        if folders:
            event.acceptProposedAction()
            self.add_folders(folders)
