"""Results dock: the runs written under ``<experiment>/analysis_output``.

Runs are found by their ``*_metadata.json`` (Codex convention), newest first. A run
shows its files by category; images preview in a label and videos play through
``cv2.VideoCapture`` frames driven by a QTimer (no QtMultimedia, decision from plan 4.8).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import threading

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .viewer import rgb_to_qimage

log = logging.getLogger("etaluma.ui")

VIDEO_SUFFIXES = {".mp4", ".avi", ".mov", ".mkv"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}

CATEGORIES: tuple[str, ...] = ("Videos", "Montages", "Composites", "Panels", "Masks", "CSV", "Logs", "Other")


def find_runs(root: Path | str) -> list[Path]:
    """Run folders under ``root`` (an experiment folder, or an analysis_output folder), newest first."""
    base = Path(root)
    if not base.is_dir():
        return []
    if base.name != "analysis_output" and (base / "analysis_output").is_dir():
        base = base / "analysis_output"
    runs: set[Path] = set()
    for meta in base.glob("**/*_metadata.json"):
        runs.add(meta.parent.parent if meta.parent.name == "info" else meta.parent)  # 0.6 keeps it in info/
    return sorted(runs, key=lambda p: p.stat().st_mtime, reverse=True)


def run_metadata(folder: Path) -> dict:
    """The run's metadata dict, or {} when it cannot be read."""
    for meta in [*sorted((Path(folder) / "info").glob("*_metadata.json")), *sorted(Path(folder).glob("*_metadata.json"))]:
        try:
            return json.loads(meta.read_text(encoding="utf-8"))
        except Exception as exc:
            log.debug("Metadata %s could not be read: %s", meta, exc)
    return {}


def run_kind(folder: Path) -> str:
    name = Path(folder).name.lower()
    if name.startswith("quick"):
        return "quick"
    if name.startswith("run"):
        return "run"
    return "output"


def describe_run(folder: Path) -> str:
    meta = run_metadata(folder)
    status = str(meta.get("status", "unknown"))
    seconds = meta.get("processing_seconds") or meta.get("elapsed_seconds")
    duration = f" · {float(seconds):.1f} s" if isinstance(seconds, (int, float)) else ""
    return f"{Path(folder).name} — {run_kind(folder)} · {status}{duration}"


def categorise(path: Path) -> str:
    suffix = path.suffix.lower()
    text = f"{path.parent.name}/{path.name}".lower()
    if suffix in VIDEO_SUFFIXES:
        return "Videos"
    if "montage" in text:
        return "Montages"
    if "composite" in text:
        return "Composites"
    if "panel" in text or "dashboard" in text:
        return "Panels"
    if "mask" in text:
        return "Masks"
    if suffix == ".csv":
        return "CSV"
    if suffix in (".txt", ".log", ".json"):
        return "Logs"
    if suffix in IMAGE_SUFFIXES:
        return "Panels"
    return "Other"


def run_files(folder: Path) -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = {}
    for path in sorted(Path(folder).glob("**/*")):
        if path.is_file():
            out.setdefault(categorise(path), []).append(path)
    return {c: out[c] for c in CATEGORIES if c in out}


class _FrameReader(QObject):
    """Decodes one video on its own thread and hands over frames already shrunk to the view.

    0.5 decoded and smooth-scaled every 1900 px frame on the interface thread, which made the
    results page stutter on 190 MB videos. The newest request wins, so a slow disk drops frames
    instead of queueing them.
    """

    frame_ready = Signal(int, object)  # index, RGB uint8 at display size

    def __init__(self, path: Path) -> None:
        super().__init__()
        import cv2  # noqa: WPS433 - heavy import, only needed here

        self._cv2 = cv2
        from ..engine.export import open_capture  # noqa: WPS433

        self._capture = open_capture(path)
        self.opened = bool(self._capture.isOpened())
        self.frames = int(self._capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0) if self.opened else 0
        fps = float(self._capture.get(cv2.CAP_PROP_FPS) or 0.0) if self.opened else 0.0
        self.fps = fps if fps > 0.1 else 20.0
        self.target = (960, 720)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._pending: int | None = None
        self._position = -1
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name="video reader", daemon=True)
        if self.opened:
            self._thread.start()

    def request(self, index: int) -> None:
        with self._lock:
            self._pending = int(index)
        self._wake.set()

    def close(self) -> None:
        self._stopped = True
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(2.0)
        try:
            self._capture.release()
        except Exception:  # pragma: no cover
            pass

    def _run(self) -> None:
        cv2 = self._cv2
        while True:
            self._wake.wait()
            if self._stopped:
                return
            with self._lock:
                index, self._pending = self._pending, None
                self._wake.clear()
            if index is None:
                continue
            if index != self._position + 1:
                self._capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = self._capture.read()
            if not ok or frame is None:
                continue
            self._position = index
            tw, th = self.target
            h, w = frame.shape[:2]
            scale = min(tw / float(w), th / float(h), 1.0)
            if scale < 0.999:
                frame = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
            rgb = np.ascontiguousarray(frame[:, :, ::-1])
            if self._stopped:
                return
            try:
                self.frame_ready.emit(index, rgb)
            except RuntimeError:  # the player was deleted
                return


class VideoPlayer(QWidget):
    """Play/pause and a frame slider; frames are decoded and shrunk on a reader thread."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._reader: _FrameReader | None = None
        self._frames = 0
        self._fps = 20.0
        self._index = 0
        self._awaiting = False
        self._rgb: np.ndarray | None = None
        self._pixmap: QPixmap | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.image_label = QLabel("Select a file")
        self.image_label.setProperty("role", "viewer")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumSize(240, 160)
        layout.addWidget(self.image_label, 1)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.play_button = QPushButton("Play")
        self.play_button.setEnabled(False)
        self.play_button.clicked.connect(self.toggle_play)
        controls.addWidget(self.play_button)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setEnabled(False)
        self.slider.valueChanged.connect(self._on_slider)
        controls.addWidget(self.slider, 1)
        self.position_label = QLabel("")
        self.position_label.setProperty("role", "muted")
        controls.addWidget(self.position_label)
        layout.addLayout(controls)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._next_frame)

    @property
    def _capture(self):
        """The open video (0.5 name, kept for callers that test for an open video)."""
        return self._reader

    # ---- content --------------------------------------------------------- #
    def show_image(self, path: Path) -> None:
        self.close_video()
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.image_label.setText(f"{path.name}\ncould not be displayed")
            return
        self._set_pixmap(pixmap)
        self.position_label.setText(f"{pixmap.width()} × {pixmap.height()}")

    def open_video(self, path: Path) -> bool:
        self.close_video()
        try:
            reader = _FrameReader(path)
        except Exception as exc:  # pragma: no cover - cv2 is a dependency
            self.image_label.setText(f"Video playback needs OpenCV: {exc}")
            return False
        if not reader.opened:
            reader.close()
            self.image_label.setText(f"{path.name}\ncould not be opened")
            return False
        reader.target = self._target()
        reader.frame_ready.connect(self._on_frame)
        self._reader = reader
        self._frames, self._fps = reader.frames, reader.fps
        self.slider.blockSignals(True)
        self.slider.setRange(0, max(0, self._frames - 1))
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        self.slider.setEnabled(self._frames > 1)
        self.play_button.setEnabled(True)
        self.play_button.setText("Play")
        self._index = 0
        self._request(0)
        return True

    def close_video(self) -> None:
        self.timer.stop()
        reader, self._reader = self._reader, None
        if reader is not None:
            try:
                reader.frame_ready.disconnect(self._on_frame)
            except (RuntimeError, TypeError):
                pass
            reader.close()
        self._frames = 0
        self._awaiting = False
        self.play_button.setEnabled(False)
        self.play_button.setText("Play")
        self.slider.setEnabled(False)
        self.position_label.setText("")

    # ---- playback -------------------------------------------------------- #
    def toggle_play(self) -> None:
        if self._reader is None:
            return
        if self.timer.isActive():
            self.timer.stop()
            self.play_button.setText("Play")
        else:
            self.timer.start(max(10, int(1000.0 / self._fps)))
            self.play_button.setText("Pause")

    def _target(self) -> tuple[int, int]:
        return max(160, self.image_label.width()), max(120, self.image_label.height())

    def _request(self, index: int) -> None:
        if self._reader is None:
            return
        self._awaiting = True
        self._reader.request(index)

    def _next_frame(self) -> None:
        if self._reader is None or self._frames <= 0 or self._awaiting:
            return  # a tick while the reader is still busy is dropped, never queued
        self._request((self._index + 1) % self._frames)

    def _on_slider(self, value: int) -> None:
        self._request(int(value))

    def _show_frame(self, index: int) -> None:
        self._request(index)

    def _on_frame(self, index: int, rgb) -> None:
        if self._reader is None:
            return
        self._awaiting = False
        self._index = int(index)
        self._rgb = rgb
        self.slider.blockSignals(True)
        self.slider.setValue(self._index)
        self.slider.blockSignals(False)
        self._set_pixmap(QPixmap.fromImage(rgb_to_qimage(rgb)), smooth=False)
        self.position_label.setText(f"{self._index + 1}/{max(1, self._frames)}")

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if self._reader is not None:
            self._reader.target = self._target()
        elif self._pixmap is not None:
            self._set_pixmap(self._pixmap)

    def _set_pixmap(self, pixmap: QPixmap, smooth: bool = True) -> None:
        self._pixmap = pixmap
        target = self.image_label.size()
        if smooth or pixmap.width() > target.width() or pixmap.height() > target.height():
            mode = Qt.TransformationMode.SmoothTransformation if smooth else Qt.TransformationMode.FastTransformation
            pixmap = pixmap.scaled(target, Qt.AspectRatioMode.KeepAspectRatio, mode)
        self.image_label.setPixmap(pixmap)


class ResultsDock(QDockWidget):
    """Runs, their files and a preview."""

    def __init__(self, ctx, parent: QWidget | None = None) -> None:
        super().__init__("Results", parent)
        self.setObjectName("results_dock")
        self.ctx = ctx

        body = QWidget(self)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(6)

        top = QHBoxLayout()
        heading = QLabel("Results")
        heading.setProperty("role", "heading")
        top.addWidget(heading)
        top.addStretch(1)
        self.open_button = QPushButton("Open folder")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_selected_folder)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        top.addWidget(self.refresh_button)
        top.addWidget(self.open_button)
        layout.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.runs_list = QListWidget()
        self.runs_list.currentItemChanged.connect(lambda cur, _prev: self._on_run_selected(cur))
        splitter.addWidget(self.runs_list)
        self.files_tree = QTreeWidget()
        self.files_tree.setHeaderLabels(["File"])
        self.files_tree.currentItemChanged.connect(lambda cur, _prev: self._on_file_selected(cur))
        splitter.addWidget(self.files_tree)
        self.player = VideoPlayer()
        splitter.addWidget(self.player)
        splitter.setSizes([220, 260, 420])
        layout.addWidget(splitter, 1)

        self.status_label = QLabel("No runs yet")
        self.status_label.setProperty("role", "muted")
        layout.addWidget(self.status_label)
        self.setWidget(body)

        if ctx is not None:
            ctx.results_changed.connect(self.set_runs)
            ctx.active_dataset_changed.connect(lambda _ds: self.refresh())

    # ---- content --------------------------------------------------------- #
    def set_runs(self, folders) -> None:
        self.runs_list.clear()
        self.files_tree.clear()
        self.player.close_video()
        for folder in folders or []:
            item = QListWidgetItem(describe_run(Path(folder)))
            item.setData(Qt.ItemDataRole.UserRole, str(folder))
            self.runs_list.addItem(item)
        count = self.runs_list.count()
        self.status_label.setText("No runs yet" if not count else f"{count} run{'s' if count != 1 else ''}")
        if count:
            self.runs_list.setCurrentRow(0)

    def refresh(self) -> None:
        """Re-scan the active dataset's analysis_output and update the context."""
        dataset = getattr(self.ctx, "active", None) if self.ctx is not None else None
        root = getattr(dataset, "root", None)
        folders = find_runs(root) if root else []
        if self.ctx is not None:
            self.ctx.set_results(folders)
        else:  # pragma: no cover - only without a context
            self.set_runs(folders)

    def selected_run(self) -> Path | None:
        item = self.runs_list.currentItem()
        return Path(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def open_selected_folder(self) -> None:
        folder = self.selected_run()
        if folder is None:
            return
        log.info("Opening %s", folder)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ---- reactions ------------------------------------------------------- #
    def _on_run_selected(self, item: QListWidgetItem | None) -> None:
        self.files_tree.clear()
        self.player.close_video()
        self.open_button.setEnabled(item is not None)
        if item is None:
            return
        folder = Path(item.data(Qt.ItemDataRole.UserRole))
        for category, paths in run_files(folder).items():
            parent = QTreeWidgetItem([f"{category} ({len(paths)})"])
            self.files_tree.addTopLevelItem(parent)
            for path in paths:
                child = QTreeWidgetItem([path.name])
                child.setData(0, Qt.ItemDataRole.UserRole, str(path))
                child.setToolTip(0, str(path))
                parent.addChild(child)
            parent.setExpanded(category in ("Videos", "Montages"))

    def _on_file_selected(self, item: QTreeWidgetItem | None) -> None:
        if item is None:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        path = Path(data)
        if path.suffix.lower() in VIDEO_SUFFIXES:
            self.player.open_video(path)
        elif path.suffix.lower() in IMAGE_SUFFIXES:
            self.player.show_image(path)
        else:
            self.player.close_video()
            self.player.image_label.setText(f"{path.name}\n{path.stat().st_size / 1024:.0f} kB")
