"""Calibration card of the explorer: the burned-in scale bar and what it says.

Shows the bottom-right corner of the first frame (the Lumaview bar and its label), the
bar length, the label read by the glyph reader, the derived µm/px and the cross-check
against the objective table. The objective can be overridden; when the bar and the table
disagree by more than 5 % the bar wins and the table value is one click away.

Calibration of a newly active experiment is read on the thread pool, so switching between
experiments of a batch never waits for a running export.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..engine.models import OBJECTIVES, WHITE_PRESETS, Calibration
from .state import dataset_key
from .workers import run_in_background

log = logging.getLogger("etaluma.ui")

DETECTED = "From scale bar"


def apply_white_median(ctx, median: float | None) -> None:
    """Pick the WHITE preset from the median when the profile asks for automatic detection."""
    if median is None:
        return
    from ..engine.display import white_preset_for_median

    profile = ctx.profile
    if not profile.white.preset_auto:
        return
    name = white_preset_for_median(float(median))
    preset = WHITE_PRESETS[name]
    if (profile.white.preset, profile.white.gamma, profile.white.weight) == (name, preset["gamma"], preset["weight"]):
        return
    profile.white.preset = name
    profile.white.gamma = float(preset["gamma"])
    profile.white.weight = float(preset["weight"])
    ctx.set_profile(profile)
    log.info("WHITE preset: %s (median %.0f)", "brightfield" if name == "brightfield" else "phase contrast", float(median))


class CalibrationCard(QFrame):
    def __init__(self, ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._updating = False
        self._detected: tuple | None = None  # (Calibration, Overlays) read from the bar
        self._crop_key: str | None = None
        self._task = None
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setProperty("role", "card")

        title = QLabel("Calibration")
        title.setProperty("role", "heading")
        self.crop = QLabel()
        self.crop.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.crop.setMinimumHeight(48)
        self.crop.setStyleSheet("background: #1a1a1c; border-radius: 4px;")
        self.message = QLabel("Open an experiment to read its scale bar")
        self.message.setWordWrap(True)
        self.message.setProperty("role", "muted")
        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.objective = QComboBox()
        self.objective.addItems([DETECTED, *OBJECTIVES.keys()])
        self.objective.setToolTip("Objective used for the capture. The burned-in scale bar is read automatically.")
        self.objective.currentTextChanged.connect(self._on_objective)
        self.table_button = QPushButton("Use table value")
        self.table_button.setToolTip("Use the objective table instead of the bar (they disagree by more than 5 %)")
        self.table_button.clicked.connect(self._use_table)
        self.table_button.setVisible(False)

        row = QHBoxLayout()
        row.addWidget(QLabel("Objective"))
        row.addWidget(self.objective, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(title)
        layout.addWidget(self.crop)
        layout.addWidget(self.result)
        layout.addWidget(self.message)
        layout.addLayout(row)
        layout.addWidget(self.table_button)

        if ctx is not None:
            ctx.calibration_changed.connect(self._show)
            ctx.active_dataset_changed.connect(self._on_dataset)

    # ---- reading ------------------------------------------------------------ #
    def _on_dataset(self, dataset) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._detected = None
        if dataset is None:
            self._crop_key = None
            self.crop.clear()
            self.result.setText("")
            self.message.setText("Open an experiment to read its scale bar")
            return
        cached = self.ctx.calibrations.get(dataset_key(dataset))
        if cached is not None:
            if cached[0].source == "burned-in scale bar":
                self._detected = cached
            median = self.ctx.white_medians.get(dataset_key(dataset))
            apply_white_median(self.ctx, median)
            return
        self.message.setText("Reading the burned-in scale bar…")

        def work():
            from ..engine.calibration import calibrate
            from ..engine.histograms import first_white_median

            overlays, calibration = calibrate(dataset)
            mask = overlays.mask(overlays.image_shape) if overlays.detected and overlays.image_shape else None
            return overlays, calibration, first_white_median(dataset, mask)

        def done(result):
            self._task = None
            overlays, calibration, median = result
            if median is not None:
                self.ctx.white_medians[dataset_key(dataset)] = float(median)
            if calibration.source == "burned-in scale bar":
                self._detected = (calibration, overlays)
            if dataset is self.ctx.active:
                self.ctx.set_calibration(calibration, overlays, dataset=dataset)
                apply_white_median(self.ctx, median)
            else:
                self.ctx.calibrations[dataset_key(dataset)] = (calibration, overlays)

        def failed(message):
            self._task = None
            self.message.setText(f"Scale bar could not be read: {message}")
            log.warning("Calibration of %s failed: %s", getattr(dataset, "name", "?"), message)

        self._task = run_in_background(work, done, failed)

    # ---- display ----------------------------------------------------------------- #
    def _show(self, calibration: Calibration, overlays) -> None:
        self._updating = True
        try:
            if calibration is None or calibration.source == "none":
                self.result.setText("No scale bar found" if self.ctx.active is not None else "")
                if self.ctx.active is not None and self._task is None:
                    self.message.setText("Choose the objective manually; without it no scale bars are drawn.")
                self.objective.setCurrentIndex(0)
                self.table_button.setVisible(False)
            else:
                ok = not calibration.disagreement
                mark = "✓" if ok and calibration.table_pixel_size_um else ("✗" if calibration.disagreement else "")
                size = f"{calibration.pixel_size_um:.3f} µm/px" if calibration.pixel_size_um else "unknown µm/px"
                label = f"{calibration.label_um:g} µm, {calibration.label_objective}" if calibration.label_um else ""
                parts = [p for p in (label, f"{size} {mark}".strip()) if p]
                self.result.setText(" · ".join(parts))
                source = {"burned-in scale bar": "from the burned-in scale bar", "manual": "chosen manually",
                          "table": "from the objective table"}.get(calibration.source, calibration.source)
                detail = calibration.message
                if calibration.disagreement:
                    detail += " The bar wins unless you choose the table value."
                self.message.setText(f"{source}. {detail}")
                self.table_button.setVisible(bool(calibration.disagreement and calibration.table_pixel_size_um))
                if calibration.source == "burned-in scale bar":
                    self.objective.setCurrentIndex(0)
                elif calibration.objective in OBJECTIVES:
                    self.objective.setCurrentText(calibration.objective)
            self._load_crop(calibration, overlays)
        finally:
            self._updating = False

    def _load_crop(self, calibration, overlays) -> None:
        path = getattr(calibration, "source_frame", None) if calibration is not None else None
        if not path:
            if self._detected is not None:
                path = self._detected[0].source_frame
                overlays = self._detected[1]
        if not path or path == self._crop_key:
            return
        self._crop_key = path

        def work():
            from ..engine.calibration import corner_crop
            from ..engine.parsing import read_rgb

            return corner_crop(read_rgb(path), overlays if overlays is not None and overlays.detected else None, scale=2)

        def done(rgb):
            from .viewer import rgb_to_qimage

            pix = QPixmap.fromImage(rgb_to_qimage(rgb))
            self.crop.setPixmap(pix.scaledToWidth(min(pix.width(), max(160, self.width() - 24)),
                                                  Qt.TransformationMode.SmoothTransformation))

        run_in_background(work, done, lambda m: log.debug("Scale-bar crop failed: %s", m))

    # ---- user overrides ------------------------------------------------------------ #
    def _on_objective(self, text: str) -> None:
        if self._updating or self.ctx.active is None:
            return
        if text == DETECTED:
            if self._detected is not None:
                self.ctx.set_calibration(*self._detected)
                log.info("Calibration: back to the burned-in scale bar")
            return
        calibration = Calibration.manual(text)
        if self._detected is not None:
            calibration.source_frame = self._detected[0].source_frame
        self.ctx.set_calibration(calibration, self.ctx.overlays)
        log.info("Calibration: objective set manually to %s (%.3f µm/px)", text, OBJECTIVES[text])

    def _use_table(self) -> None:
        current = self.ctx.calibration
        if current is None or not current.table_pixel_size_um:
            return
        calibration = Calibration.from_dict(current.to_dict())
        calibration.pixel_size_um = current.table_pixel_size_um
        calibration.source = "table"
        calibration.disagreement = False
        calibration.message = f"{current.objective} table value {current.table_pixel_size_um:.3f} µm/px (bar said {current.pixel_size_um:.3f})"
        self.ctx.set_calibration(calibration, self.ctx.overlays)
        log.info("Calibration: using the %s table value %.3f µm/px instead of the bar", current.objective, current.table_pixel_size_um)
