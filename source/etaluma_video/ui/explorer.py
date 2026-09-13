"""Explorer dock: what is in the folder, before any processing.

A tree of the opened experiments (one node per experiment in a batch) with a thumbnail per
position, the files on disk, the acquisition summary, the stage map and the calibration card.
Clicking a position makes it the live preview's position. Independent of any processing.
"""
from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

import numpy as np
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFont, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QLabel,
    QScrollArea,
    QSplitter,
    QHeaderView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..engine.models import CHANNEL_ORDER
from .calibration_card import CalibrationCard
from .state import dataset_key
from .viewer import format_elapsed, rgb_to_qimage
from .workers import run_in_background

log = logging.getLogger("etaluma.ui")

ROLE = Qt.ItemDataRole.UserRole
SHORT = {"WHITE": "W", "F1": "F1", "F2": "F2", "F3": "F3"}
THUMB = 40


def load_thumbnail(path) -> QPixmap | None:
    """Lumaview thumbnails are PNG data with a .tif extension: decide the format from the content."""
    if not path:
        return None
    reader = QImageReader(str(path))
    reader.setDecideFormatFromContent(True)
    image = reader.read()
    if image.isNull():
        return None
    return QPixmap.fromImage(image).scaled(THUMB, THUMB, Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation)


def folder_layout(dataset) -> list[tuple[str, str]]:
    """(entry, description) rows describing the experiment folder on disk."""
    root = Path(dataset.root)
    rows: list[tuple[str, str]] = []
    for protocol in getattr(dataset, "protocols", []) or []:
        path = Path(protocol.get("path", ""))
        name = protocol.get("protocol_name") or ""
        rows.append((path.name, f"protocol{f' {name}' if name else ''}"))
    counts: Counter = Counter()
    for f in dataset.frames:
        try:
            rel = Path(f.path).parent.relative_to(root)
        except ValueError:
            rel = Path(f.path).parent
        counts[str(rel).replace("\\", "/")] += 1
    for rel, n in sorted(counts.items())[:12]:
        label = "(experiment folder)" if rel in (".", "") else f"{rel}/"
        rows.append((label, f"{n} images"))
    if len(counts) > 12:
        rows.append((f"… {len(counts) - 12} more folders", f"{sum(list(counts.values())[12:])} images"))
    avs = len(getattr(dataset, "avs", []) or [])
    if avs:
        rows.append(("*.avs", f"{avs} AviSynth scripts (frame counts)"))
    if any((root / d).is_dir() for d in ("analysis_output",)):
        rows.append(("analysis_output/", "earlier results"))
    return rows


class ExplorerWidget(QWidget):
    """``ExplorerWidget(ctx, parent=None)``: tree, summary, stage map and calibration card."""

    def __init__(self, ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._selecting = False
        self._items: dict[tuple[str, str], QTreeWidgetItem] = {}
        self._stage_task = None

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Experiment", "Contents"])
        self.tree.setIconSize(QSize(THUMB, THUMB))
        self.tree.setUniformRowHeights(False)
        self.tree.setAlternatingRowColors(False)
        self.tree.setMinimumHeight(160)
        self.tree.header().setStretchLastSection(True)
        # 0.7: names keep their width, the details column is elided, never a sideways scrollbar
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tree.currentItemChanged.connect(self._on_current_item)

        self.summary = QLabel("Open an experiment folder or a parent folder with several experiments.")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.stage = QLabel()
        self.stage.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stage.setToolTip("Captured positions on the stage")
        self.card = CalibrationCard(ctx, self)

        details = QWidget()
        dl = QVBoxLayout(details)
        dl.setContentsMargins(6, 6, 6, 6)
        dl.setSpacing(8)
        dl.addWidget(self.summary)
        dl.addWidget(self.stage)
        dl.addWidget(self.card)
        dl.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(details)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.tree)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        if ctx is not None:
            ctx.datasets_changed.connect(self._rebuild)
            ctx.active_dataset_changed.connect(self._on_active)
            ctx.position_changed.connect(self._on_position)

    # ---- tree ------------------------------------------------------------------ #
    def _rebuild(self, datasets) -> None:
        self._selecting = True
        try:
            self.tree.clear()
            self._items.clear()
            for index, ds in enumerate(datasets or []):
                self._add_dataset(index, ds)
            if datasets:
                first = self.tree.topLevelItem(0)
                first.setExpanded(True)
        finally:
            self._selecting = False
        for column in (0, 1):
            self.tree.resizeColumnToContents(column)

    def _add_dataset(self, index: int, ds) -> None:
        groups = ds.groups
        top = QTreeWidgetItem([ds.name, self._dataset_line(ds)])
        top.setData(0, ROLE, ("dataset", index, ""))
        top.setToolTip(0, str(ds.root))
        font = QFont(top.font(0))
        font.setBold(True)
        top.setFont(0, font)
        self.tree.addTopLevelItem(top)
        self._items[(dataset_key(ds), "")] = top
        for roi in sorted(groups, key=lambda r: min(f.order for fs in groups[r].values() for f in fs)):
            per_channel = groups[roi]
            channels = [c for c in CHANNEL_ORDER if c in per_channel]
            n = max(len(fs) for fs in per_channel.values())
            descriptor = next((f.descriptor for fs in per_channel.values() for f in fs if f.descriptor), "")
            label = roi + (f"  {descriptor}" if descriptor else "")
            item = QTreeWidgetItem([label, f"{' '.join(SHORT[c] for c in channels)} · {n} image{'s' if n != 1 else ''} each"])
            item.setData(0, ROLE, ("position", index, roi))
            first = self._first_frame(per_channel)
            if first is not None:
                item.setToolTip(0, str(Path(first.path).parent))
                pix = self._thumbnail(first)
                if pix is not None:
                    item.setIcon(0, pix)
            top.addChild(item)
            self._items[(dataset_key(ds), roi)] = item
        files = QTreeWidgetItem(["Files on disk", ""])
        files.setData(0, ROLE, ("files", index, ""))
        for entry, description in folder_layout(ds):
            files.addChild(QTreeWidgetItem([entry, description]))
        top.addChild(files)

    @staticmethod
    def _first_frame(per_channel):
        for ch in ("WHITE", "F2", "F3", "F1"):
            if ch in per_channel and per_channel[ch]:
                return sorted(per_channel[ch], key=lambda f: f.serial)[0]
        return None

    @staticmethod
    def _thumbnail(frame):
        try:
            from ..engine.parsing import thumbnail_path

            return load_thumbnail(thumbnail_path(frame))
        except Exception as exc:
            log.debug("Thumbnail of %s unavailable: %s", frame.path, exc)
            return None

    @staticmethod
    def _dataset_line(ds) -> str:
        parts = [ds.mode]
        if ds.mode == "timelapse":
            parts.append(f"{ds.n_timepoints} timepoints")
            if ds.interval:
                parts.append(f"every {format_elapsed(float(ds.interval))}")
        parts.append(f"{len(ds.groups)} positions")
        parts.append(" ".join(SHORT[c] for c in ds.channels))
        return " · ".join(parts)

    def _on_current_item(self, current, _previous) -> None:
        if self._selecting or current is None or self.ctx is None:
            return
        kind, index, roi = current.data(0, ROLE) or (None, None, None)
        if kind not in ("dataset", "position"):
            return
        datasets = self.ctx.datasets
        if index is None or index >= len(datasets):
            return
        ds = datasets[index]
        if ds is not self.ctx.active:
            log.debug("explorer.set_active(%s)", ds.name)
            self.ctx.set_active(ds)
        if kind == "position" and roi and roi != self.ctx.position:
            log.debug("explorer.set_position(%s)", roi)
            self.ctx.set_position(roi)

    def _on_position(self, roi: str) -> None:
        ds = self.ctx.active
        item = self._items.get((dataset_key(ds), roi)) if ds is not None else None
        if item is None:
            return
        self._selecting = True
        try:
            item.parent().setExpanded(True) if item.parent() is not None else None
            self.tree.setCurrentItem(item)
        finally:
            self._selecting = False

    # ---- details ------------------------------------------------------------------ #
    def _on_active(self, ds) -> None:
        for (key, roi), item in self._items.items():
            if not roi:
                font = QFont(item.font(0))
                font.setUnderline(ds is not None and key == dataset_key(ds))
                item.setFont(0, font)
        if ds is None:
            self.summary.setText("Open an experiment folder or a parent folder with several experiments.")
            self.stage.clear()
            return
        self.summary.setText(self._summary_text(ds))
        self.summary.setToolTip("\n".join(ds.warnings) if ds.warnings else "")
        self._load_stage_map(ds)

    @staticmethod
    def _summary_text(ds) -> str:
        lines = [f"<b>{ds.name}</b>",
                 f"{ds.mode.capitalize()} · layout {ds.layout.replace('_', ' ')}",
                 f"{len(ds.groups)} positions · {', '.join(ds.channels)} · {len(ds.frames)} images"]
        protocol = ds.protocol or {}
        if ds.mode == "timelapse":
            span = ""
            if ds.interval:
                span = f" · every {format_elapsed(float(ds.interval))}, {format_elapsed(float(ds.interval) * max(0, ds.n_timepoints - 1))} captured"
                planned = protocol.get("planned_duration_seconds")
                if planned:
                    span += f" of {format_elapsed(float(planned))} planned"
            lines.append(f"{ds.n_timepoints} timepoints{span}")
        if protocol.get("protocol_name"):
            lines.append(f"Protocol {protocol['protocol_name']}")
        if ds.warnings:
            lines.append(f"{len(ds.warnings)} acquisition note{'s' if len(ds.warnings) != 1 else ''} (hover to read)")
        return "<br>".join(lines)

    def _load_stage_map(self, ds) -> None:
        if self._stage_task is not None:
            self._stage_task.cancel()

        def work():
            from ..engine.reports import stage_map

            return np.asarray(stage_map(ds, width=520, height=300).convert("RGB"))

        def done(rgb):
            self._stage_task = None
            if ds is not self.ctx.active:
                return
            pix = QPixmap.fromImage(rgb_to_qimage(rgb))
            self.stage.setPixmap(pix.scaledToWidth(max(180, min(pix.width(), self.width() - 16)),
                                                   Qt.TransformationMode.SmoothTransformation))

        def failed(message):
            self._stage_task = None
            log.debug("Stage map unavailable: %s", message)
            self.stage.clear()

        self._stage_task = run_in_background(work, done, failed)
