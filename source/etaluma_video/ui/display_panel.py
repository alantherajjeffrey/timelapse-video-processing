"""Display panel: Auto | Manual LUT control for every channel (plan section 4.2).

Auto shows the automatic bounds (Adaptive, Classic or Background cut) read-only on each
channel's histogram. Manual starts from those values and lets the user drag the black and
white points, type them, and set gamma; they apply to every position and timepoint. Colour
preset, blend mode, WHITE preset and weight, and the rolling ball apply in both modes.

The live profile is ``ctx.profile``; every edit updates it in place and calls
``ctx.set_profile`` so the viewer re-renders. The last *manual* profile is saved in the
settings and restored the next time an experiment is opened (decision 12).
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..engine import display
from ..engine.models import (
    CHANNEL_ORDER,
    COLOUR_PRESETS,
    FLUOR_CHANNELS,
    WHITE_PRESETS,
    ChannelDisplay,
    DisplayProfile,
)
from .histogram_widget import HistogramWidget

log = logging.getLogger("etaluma.ui")

METHOD_LABELS = {"adaptive": "Adaptive", "classic": "Classic", "cut": "Background cut"}
METHOD_TIPS = {
    "adaptive": "Black point just above the brightest background level; white point at the 99.9th percentile",
    "classic": "Black point 0; white point at the brightest frame's 99.5th percentile (the March 2026 script)",
    "cut": "Black point at the 90th percentile, white point at the 99.9th (the July 2026 rule)",
}
BLEND_LABELS = {"screen": "Screen", "additive": "Additive", "max": "Maximum"}
WHITE_CHOICES = [("auto", "Auto-detect"), ("brightfield", "Brightfield"), ("phase", "Phase contrast")]
PRESET_LABELS = {"CGM": "Cyan · green · magenta", "RGB": "Blue · green · red", "custom": "Custom"}


def swatch_icon(colour: tuple[float, float, float], size: int = 14) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(QColor.fromRgbF(*[min(max(c, 0.0), 1.0) for c in colour]))
    return QIcon(pix)


def adapt_profile(profile: DisplayProfile, channels) -> DisplayProfile:
    """Make sure every fluorescence channel of the experiment has settings in the profile."""
    preset = COLOUR_PRESETS.get(profile.colour_preset, COLOUR_PRESETS["CGM"])
    for ch in channels:
        if ch in FLUOR_CHANNELS and ch not in profile.channels:
            profile.channels[ch] = ChannelDisplay(colour=preset[ch])
    return profile


class _ChannelRow(QWidget):
    """One fluorescence channel: include, colour, name, low/high/gamma and its histogram."""

    edited = Signal(str)  # channel

    def __init__(self, channel: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.channel = channel
        self.enabled = QCheckBox()
        self.enabled.setToolTip(f"Include {channel} in the composites")
        self.swatch = QToolButton()
        self.swatch.setToolTip(f"Choose the colour of {channel}")
        self.swatch.setAutoRaise(True)
        self.name = QLabel(channel)
        self.name.setMinimumWidth(38)
        self.low = QDoubleSpinBox()
        self.high = QDoubleSpinBox()
        for spin, tip in ((self.low, "Black point"), (self.high, "White point")):
            spin.setRange(0.0, 255.0)
            spin.setDecimals(0)
            spin.setSingleStep(1.0)
            spin.setToolTip(tip)
            spin.setKeyboardTracking(False)
            spin.setFixedWidth(58)
        self.gamma = QDoubleSpinBox()
        self.gamma.setRange(0.2, 5.0)
        self.gamma.setSingleStep(0.05)
        self.gamma.setDecimals(2)
        self.gamma.setPrefix("γ ")
        self.gamma.setToolTip("Gamma: below 1 lifts faint signal, above 1 darkens it")
        self.gamma.setKeyboardTracking(False)
        self.gamma.setFixedWidth(72)
        self.hist = HistogramWidget()
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 2, 0, 2)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(2)
        grid.addWidget(self.enabled, 0, 0)
        grid.addWidget(self.swatch, 0, 1)
        grid.addWidget(self.name, 0, 2)
        grid.addWidget(self.low, 0, 3)
        grid.addWidget(self.high, 0, 4)
        grid.addWidget(self.gamma, 0, 5)
        grid.addWidget(self.hist, 1, 0, 1, 6)
        grid.setColumnStretch(2, 1)


class _WhiteRow(QWidget):
    """WHITE (transmitted light): include, preset, underlay weight, stretch bounds and histogram."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.enabled = QCheckBox()
        self.enabled.setToolTip("Include WHITE as the dimmed underlay of the composite with WHITE")
        self.name = QLabel("WHITE")
        self.preset = QComboBox()
        for key, label in WHITE_CHOICES:
            self.preset.addItem(label, key)
        self.preset.setToolTip("Brightfield: gamma 2.0, weight 0.5. Phase contrast: gamma 1.0, weight 0.6. "
                               "Auto-detect picks one per experiment from the WHITE median.")
        self.weight = QSlider(Qt.Orientation.Horizontal)
        self.weight.setRange(0, 100)
        self.weight.setToolTip("How strongly WHITE shows under the fluorescence in the composite")
        self.weight_label = QLabel("0.60")
        self.weight_label.setFixedWidth(32)
        self.low = QDoubleSpinBox()
        self.high = QDoubleSpinBox()
        for spin, tip in ((self.low, "Black point of the WHITE video"), (self.high, "White point of the WHITE video")):
            spin.setRange(0.0, 255.0)
            spin.setDecimals(0)
            spin.setToolTip(tip)
            spin.setKeyboardTracking(False)
            spin.setFixedWidth(58)
        self.gamma = QDoubleSpinBox()
        self.gamma.setRange(0.2, 5.0)
        self.gamma.setSingleStep(0.05)
        self.gamma.setDecimals(2)
        self.gamma.setPrefix("γ ")
        self.gamma.setToolTip("Gamma of the WHITE underlay in the composite")
        self.gamma.setKeyboardTracking(False)
        self.gamma.setFixedWidth(72)
        self.hist = HistogramWidget()
        self.hist.set_colour(QColor(150, 150, 150))
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 2, 0, 2)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(2)
        grid.addWidget(self.enabled, 0, 0)
        grid.addWidget(self.name, 0, 1)
        grid.addWidget(self.preset, 0, 2, 1, 2)
        grid.addWidget(self.gamma, 0, 4)
        grid.addWidget(QLabel("Weight"), 1, 1)
        grid.addWidget(self.weight, 1, 2, 1, 2)
        grid.addWidget(self.weight_label, 1, 4)
        grid.addWidget(self.low, 2, 2)
        grid.addWidget(self.high, 2, 3)
        grid.addWidget(self.hist, 3, 0, 1, 5)
        grid.setColumnStretch(2, 1)
        grid.setColumnStretch(3, 1)


class DisplayPanel(QWidget):
    """``DisplayPanel(ctx, parent=None)``: edits ``ctx.profile`` and publishes it on every change."""

    def __init__(self, ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._updating = False
        self._rows: dict[str, _ChannelRow] = {}
        self._white: _WhiteRow | None = None
        self._coverage = (0, 0)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(700)
        self._save_timer.timeout.connect(self._persist)

        self.title_label = title = QLabel("Display")
        title.setProperty("role", "heading")
        self.auto_button = QToolButton(text="Auto-normalised")
        self.manual_button = QToolButton(text="Manual")
        self.mode_group = QButtonGroup(self)
        from .viewer import CHIP_QSS

        for i, b in enumerate((self.auto_button, self.manual_button)):
            b.setCheckable(True)
            b.setStyleSheet(CHIP_QSS)
            self.mode_group.addButton(b, i)
        self.auto_button.setToolTip("Black and white points measured from the histograms of every position: a linear stretch between them, not histogram equalisation")
        self.manual_button.setToolTip("Set the black and white points yourself; they start from the Auto-normalised values")
        self.mode_group.idClicked.connect(self._on_mode_clicked)
        self.method = QComboBox()
        for key in ("adaptive", "classic", "cut"):
            self.method.addItem(METHOD_LABELS[key], key)
            self.method.setItemData(self.method.count() - 1, METHOD_TIPS[key], Qt.ItemDataRole.ToolTipRole)
        self.method.setToolTip("Automatic method")
        self.method.currentIndexChanged.connect(self._on_method)
        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.auto_button)
        header.addWidget(self.manual_button)
        header.addWidget(self.method)

        self.colours = QComboBox()
        for key in ("CGM", "RGB"):
            self.colours.addItem(PRESET_LABELS[key], key)
        self.colours.setToolTip("Colour pair for the fluorescence channels (WHITE is always gray)")
        self.colours.currentIndexChanged.connect(self._on_colours)
        self.blend = QComboBox()
        for key in ("screen", "additive", "max"):
            self.blend.addItem(BLEND_LABELS[key], key)
        self.blend.setToolTip("Screen keeps overlapping colours visible; additive shows overlap as mixed colour "
                              "but can saturate; maximum keeps the brightest channel per pixel")
        self.blend.currentIndexChanged.connect(self._on_blend)
        options = QGridLayout()
        options.addWidget(QLabel("Colours"), 0, 0)
        options.addWidget(self.colours, 0, 1)
        options.addWidget(QLabel("Blend"), 1, 0)
        options.addWidget(self.blend, 1, 1)
        options.setColumnStretch(1, 1)

        self.hist_view = QComboBox()
        for key, label in (("both", "Current frame and all timepoints"), ("stack", "All timepoints"),
                           ("frame", "Current frame")):
            self.hist_view.addItem(label, key)
        self.hist_view.setToolTip("Histogram under each channel: the frame in the preview (outline), "
                                  "every loaded timepoint (filled; the Auto-normalised bounds come from it), or both")
        self.hist_log = QCheckBox("Log scale")
        self.hist_log.setChecked(True)
        self.hist_log.setToolTip("Log scale shows faint signal next to a large background peak")
        self.hist_view.currentIndexChanged.connect(lambda _i: self._on_hist_display())
        self.hist_log.toggled.connect(lambda _on: self._on_hist_display())
        self._frame_counts: dict = {}
        hist_row = QHBoxLayout()
        hist_row.addWidget(QLabel("Histogram"))
        hist_row.addWidget(self.hist_view, 1)
        hist_row.addWidget(self.hist_log)

        self.rows_box = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_box)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(6)
        self.empty_label = QLabel("Open an experiment to set its display")
        self.empty_label.setProperty("role", "muted")
        self.rows_layout.addWidget(self.empty_label)

        self.rolling = QCheckBox("Rolling-ball background")
        self.rolling.setToolTip("Subtract a smooth background from the fluorescence channels before display. "
                                "Measurements stay raw.")
        self.rolling.toggled.connect(self._on_rolling)
        self.radius = QSpinBox()
        self.radius.setRange(2, 500)
        self.radius.setSuffix(" px")
        self.radius.setToolTip("Radius in full-resolution pixels: larger than the biggest object you want to keep")
        self.radius.setKeyboardTracking(False)
        self.radius.valueChanged.connect(self._on_radius)
        rolling_row = QHBoxLayout()
        rolling_row.addWidget(self.rolling)
        rolling_row.addStretch(1)
        rolling_row.addWidget(self.radius)

        self.coverage_label = QLabel("")
        self.coverage_label.setProperty("role", "muted")
        self.coverage_label.setWordWrap(True)
        self.measure_button = QPushButton("Measure all positions")
        self.measure_button.setToolTip("Compute the Auto-normalised bounds over every position instead of the ones in memory")
        self.measure_button.clicked.connect(self._measure_all)
        self.measure_button.setVisible(False)  # 0.6: every position is measured automatically
        coverage_row = QHBoxLayout()
        coverage_row.addWidget(self.coverage_label, 1)
        coverage_row.addWidget(self.measure_button)

        self.reset_button = QPushButton("Reset to Auto-normalised")
        self.reset_button.setToolTip("Copy the current Auto-normalised bounds into the manual handles and set gamma to 1")
        self.reset_button.clicked.connect(self._reset_to_auto)
        self.save_button = QPushButton("Save profile…")
        self.save_button.clicked.connect(self._save_profile)
        self.load_button = QPushButton("Load profile…")
        self.load_button.clicked.connect(self._load_profile)
        buttons = QHBoxLayout()
        buttons.addWidget(self.reset_button)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.load_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addLayout(options)
        layout.addLayout(hist_row)
        layout.addWidget(self.rows_box)
        layout.addLayout(rolling_row)
        layout.addLayout(coverage_row)
        layout.addLayout(buttons)

        if ctx is not None:
            ctx.active_dataset_changed.connect(self._on_dataset)
            ctx.profile_changed.connect(self._on_profile)
            ctx.histograms_changed.connect(self._on_histograms)
            ctx.coverage_changed.connect(self._on_coverage)
            ctx.channel_names_changed.connect(self._on_channel_names)
            ctx.frame_histograms_changed.connect(self._on_frame_histograms)
            self._refresh_from_profile()

    # ---- helpers ---------------------------------------------------------------- #
    def _settings(self):
        return getattr(self.window(), "settings", None)

    @property
    def profile(self) -> DisplayProfile:
        return self.ctx.profile

    def _commit(self, what: str = "") -> None:
        """Publish the edited profile; remember it if it is manual."""
        self._updating = True
        try:
            self.ctx.set_profile(self.profile)
        finally:
            self._updating = False
        self._refresh_enabled()
        if what:
            log.debug("display: %s", what)
        self._save_timer.start()

    def _persist(self) -> None:
        settings = self._settings()
        if settings is None:
            return
        settings.last_profile_json = self.profile.to_json()
        try:
            settings.save()
        except Exception as exc:
            log.debug("Settings could not be saved: %s", exc)

    # ---- dataset and profile ------------------------------------------------------- #
    def _on_dataset(self, dataset) -> None:
        channels = list(getattr(dataset, "channels", []) or []) if dataset is not None else []
        profile = self._restored_profile(channels) if dataset is not None else None
        if profile is None:
            preset = self.profile.colour_preset if self.profile.colour_preset in COLOUR_PRESETS else "CGM"
            profile = DisplayProfile.default_for(channels, preset)
            profile.auto_method = self.profile.auto_method
            profile.blend = self.profile.blend
            settings = self._settings()
            profile.rolling_ball.radius_px = int(getattr(settings, "rolling_ball_radius", 50) or 50)
        self._build_rows(channels)
        self._updating = True
        try:
            self.ctx.set_profile(profile)
        finally:
            self._updating = False
        self._refresh_from_profile()

    def _restored_profile(self, channels) -> DisplayProfile | None:
        settings = self._settings()
        text = getattr(settings, "last_profile_json", "") if settings is not None else ""
        if not text:
            return None
        try:
            profile = DisplayProfile.from_json(text)
        except Exception as exc:
            log.debug("Saved display profile ignored: %s", exc)
            return None
        if profile.mode != "manual":
            return None
        log.info("Display: restored your last manual settings (%s)", display.profile_summary(profile, display.effective_bounds(profile, None)))
        return adapt_profile(profile, channels)

    def _build_rows(self, channels) -> None:
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            if item.widget() is not None and item.widget() is not self.empty_label:
                item.widget().deleteLater()
        self._rows.clear()
        self._white = None
        fluor = [c for c in CHANNEL_ORDER if c in channels and c in FLUOR_CHANNELS]
        if not channels:
            self.rows_layout.addWidget(self.empty_label)
            self.empty_label.show()
            return
        self.empty_label.hide()
        for ch in fluor:
            row = _ChannelRow(ch, self.rows_box)
            row.enabled.toggled.connect(lambda on, c=ch: self._on_enabled(c, on))
            row.swatch.clicked.connect(lambda _=False, c=ch: self._pick_colour(c))
            row.low.valueChanged.connect(lambda _v, c=ch: self._on_spin(c))
            row.high.valueChanged.connect(lambda _v, c=ch: self._on_spin(c))
            row.gamma.valueChanged.connect(lambda v, c=ch: self._on_gamma(c, v))
            row.hist.bounds_changed.connect(lambda lo, hi, c=ch: self._on_drag(c, lo, hi))
            self.rows_layout.addWidget(row)
            self._rows[ch] = row
        if "WHITE" in channels:
            w = _WhiteRow(self.rows_box)
            w.enabled.toggled.connect(self._on_white_enabled)
            w.preset.currentIndexChanged.connect(self._on_white_preset)
            w.weight.valueChanged.connect(self._on_white_weight)
            w.gamma.valueChanged.connect(self._on_white_gamma)
            w.low.valueChanged.connect(lambda _v: self._on_white_spin())
            w.high.valueChanged.connect(lambda _v: self._on_white_spin())
            w.hist.bounds_changed.connect(self._on_white_drag)
            self.rows_layout.addWidget(w)
            self._white = w
        self._restore_hist_display()

    # ---- histogram display ------------------------------------------------------ #
    def _hist_widgets(self):
        widgets = [row.hist for row in self._rows.values()]
        if self._white is not None:
            widgets.append(self._white.hist)
        return widgets

    def _restore_hist_display(self) -> None:
        settings = self._settings()
        extra = getattr(settings, "extra", None) or {}
        for w, value in ((self.hist_view, None), (self.hist_log, None)):
            w.blockSignals(True)
        view = extra.get("histogram_view", self.hist_view.currentData() or "both")
        self.hist_view.setCurrentIndex(max(0, self.hist_view.findData(view)))
        self.hist_log.setChecked(bool(extra.get("histogram_log", self.hist_log.isChecked())))
        self.hist_view.blockSignals(False)
        self.hist_log.blockSignals(False)
        self._apply_hist_display()

    def _apply_hist_display(self) -> None:
        view = self.hist_view.currentData() or "both"
        log_scale = self.hist_log.isChecked()
        for w in self._hist_widgets():
            w.set_view(view)
            w.set_log(log_scale)
        for ch, row in self._rows.items():
            row.hist.set_frame_histogram(self._frame_counts.get(ch))
        if self._white is not None:
            self._white.hist.set_frame_histogram(self._frame_counts.get("WHITE"))

    def _on_hist_display(self) -> None:
        self._apply_hist_display()
        settings = self._settings()
        if settings is not None:
            settings.extra["histogram_view"] = self.hist_view.currentData()
            settings.extra["histogram_log"] = self.hist_log.isChecked()
        log.debug("display: histogram view %s, %s scale", self.hist_view.currentData(),
                  "log" if self.hist_log.isChecked() else "linear")

    def _on_frame_histograms(self, counts) -> None:
        self._frame_counts = dict(counts or {})
        for ch, row in self._rows.items():
            row.hist.set_frame_histogram(self._frame_counts.get(ch))
        if self._white is not None:
            self._white.hist.set_frame_histogram(self._frame_counts.get("WHITE"))

    def _on_profile(self, _profile) -> None:
        if not self._updating:
            self._refresh_from_profile()

    def _bounds(self):
        return display.effective_bounds(self.profile, self.ctx.auto_bounds or {})

    def _refresh_from_profile(self) -> None:
        """Push the profile (and the effective bounds) into every widget without emitting edits."""
        p = self.profile
        widgets = [self.method, self.colours, self.blend, self.rolling, self.radius]
        for w in widgets:
            w.blockSignals(True)
        try:
            self.auto_button.setChecked(p.mode != "manual")
            self.manual_button.setChecked(p.mode == "manual")
            self.method.setCurrentIndex(max(0, self.method.findData(p.auto_method)))
            if p.colour_preset == "custom" and self.colours.findData("custom") < 0:
                self.colours.addItem(PRESET_LABELS["custom"], "custom")
            self.colours.setCurrentIndex(max(0, self.colours.findData(p.colour_preset)))
            self.blend.setCurrentIndex(max(0, self.blend.findData(p.blend)))
            self.rolling.setChecked(bool(p.rolling_ball.enabled))
            self.radius.setValue(int(p.rolling_ball.radius_px))
        finally:
            for w in widgets:
                w.blockSignals(False)
        self._refresh_rows()
        self._refresh_enabled()

    def _refresh_rows(self) -> None:
        p = self.profile
        bounds = self._bounds()
        manual = p.mode == "manual"
        hists = self.ctx.histograms
        for ch, row in self._rows.items():
            cd = p.channels.get(ch)
            if cd is None:
                continue
            lo, hi = bounds.get(ch, (0.0, 255.0))
            for w in (row.enabled, row.low, row.high, row.gamma):
                w.blockSignals(True)
            row.enabled.setChecked(cd.enabled)
            row.low.setValue(lo)
            row.high.setValue(hi)
            row.gamma.setValue(cd.gamma)
            for w in (row.enabled, row.low, row.high, row.gamma):
                w.blockSignals(False)
            row.swatch.setIcon(swatch_icon(cd.colour))
            row.hist.set_colour(QColor.fromRgbF(*[max(0.25, c) for c in cd.colour]))
            row.hist.set_bounds(lo, hi)
            row.hist.set_editable(manual)
            row.low.setEnabled(manual)
            row.high.setEnabled(manual)
            hist = hists.channels.get(ch) if hists is not None else None
            row.hist.set_histogram(hist.counts if hist is not None else None)
            row.hist.set_markers(self._peak_markers(hist))
        if self._white is not None:
            w = self._white
            lo, hi = bounds.get("WHITE", (0.0, 255.0))
            for x in (w.enabled, w.preset, w.weight, w.gamma, w.low, w.high):
                x.blockSignals(True)
            w.enabled.setChecked(p.white.enabled)
            key = "auto" if p.white.preset_auto else p.white.preset
            w.preset.setCurrentIndex(max(0, w.preset.findData(key)))
            label = "Brightfield" if p.white.preset == "brightfield" else "Phase contrast"
            w.preset.setItemText(0, f"Auto-detect ({label.lower()})")
            w.weight.setValue(int(round(p.white.weight * 100)))
            w.weight_label.setText(f"{p.white.weight:.2f}")
            w.gamma.setValue(p.white.gamma)
            w.low.setValue(lo)
            w.high.setValue(hi)
            for x in (w.enabled, w.preset, w.weight, w.gamma, w.low, w.high):
                x.blockSignals(False)
            w.hist.set_bounds(lo, hi)
            w.hist.set_editable(manual)
            w.low.setEnabled(manual)
            w.high.setEnabled(manual)
            hist = hists.channels.get("WHITE") if hists is not None else None
            w.hist.set_histogram(hist.counts if hist is not None else None)

    def _peak_markers(self, hist) -> list[float]:
        if hist is None or self.profile.auto_method != "adaptive":
            return []
        try:
            detail = display.auto_bounds_detail(hist, "adaptive")
        except Exception:
            return []
        return [float(b) for b, mass in (detail.get("peaks") or []) if mass >= 0.05]

    def _refresh_enabled(self) -> None:
        has = bool(self._rows) or self._white is not None
        manual = self.profile.mode == "manual"
        self.method.setEnabled(has)
        self.reset_button.setEnabled(has and manual)
        for w in (self.colours, self.blend, self.save_button, self.load_button, self.rolling, self.radius):
            w.setEnabled(has)
        self.radius.setEnabled(has and self.rolling.isChecked())

    def _on_histograms(self, _hists) -> None:
        self._refresh_rows()

    def _on_coverage(self, covered: int, total: int) -> None:
        self._coverage = (covered, total)
        if total <= 0:
            self.coverage_label.setText("")
            self.measure_button.setEnabled(False)
            return
        what = "Auto-normalised bounds" if self.profile.mode != "manual" else "Histograms"
        rest = ("measuring the rest in the background." if covered < total
                else "the same numbers Process and Quick video use.")
        self.coverage_label.setText(f"{what} use {covered} of {total} position{'s' if total != 1 else ''} "
                                    f"(every 8th timepoint at full size); {rest}")

    # ---- edits -------------------------------------------------------------------------- #
    def _on_mode_clicked(self, button_id: int) -> None:
        manual = button_id == 1
        p = self.profile
        if manual == (p.mode == "manual"):
            return
        if manual:
            bounds = self._bounds()  # still the Auto ones: seed the handles from them
            for ch, cd in p.channels.items():
                cd.low, cd.high = bounds.get(ch, (cd.low, cd.high))
            p.white.low, p.white.high = bounds.get("WHITE", (p.white.low, p.white.high))
            p.mode = "manual"
            log.info("Display: manual, starting from the Auto-normalised bounds (%s)", display.profile_summary(p, self._bounds()))
        else:
            p.mode = "auto"
            log.info("Display: Auto-normalised (%s)", METHOD_LABELS.get(p.auto_method, p.auto_method))
        self._commit(f"mode {p.mode}")
        self._refresh_rows()
        self._on_coverage(*self._coverage)

    def _on_method(self, _index: int) -> None:
        key = self.method.currentData()
        if key and key != self.profile.auto_method:
            self.profile.auto_method = key
            log.info("Display: Auto-normalised method %s", METHOD_LABELS[key])
            self._commit(f"auto_method={key}")
            self._refresh_rows()

    def _on_colours(self, _index: int) -> None:
        key = self.colours.currentData()
        if key in COLOUR_PRESETS and key != self.profile.colour_preset:
            self.profile.apply_colour_preset(key)
            idx = self.colours.findData("custom")
            if idx >= 0:
                self.colours.blockSignals(True)
                self.colours.removeItem(idx)
                self.colours.blockSignals(False)
            self._commit(f"colour_preset={key}")
            self._refresh_rows()

    def _on_blend(self, _index: int) -> None:
        key = self.blend.currentData()
        if key and key != self.profile.blend:
            self.profile.blend = key
            self._commit(f"blend={key}")

    def _on_rolling(self, on: bool) -> None:
        self.profile.rolling_ball.enabled = bool(on)
        self.radius.setEnabled(bool(on))
        log.info("Display: rolling ball %s", f"on, {self.profile.rolling_ball.radius_px} px" if on else "off")
        self._commit(f"rolling_ball={on}")

    def _on_radius(self, value: int) -> None:
        self.profile.rolling_ball.radius_px = int(value)
        settings = self._settings()
        if settings is not None:
            settings.rolling_ball_radius = int(value)
        if self.profile.rolling_ball.enabled:
            self._commit(f"rolling_ball.radius_px={value}")

    def _on_enabled(self, channel: str, on: bool) -> None:
        cd = self.profile.channels.get(channel)
        if cd is not None:
            cd.enabled = bool(on)
            self._commit(f"{channel}.enabled={on}")

    def _on_gamma(self, channel: str, value: float) -> None:
        cd = self.profile.channels.get(channel)
        if cd is not None:
            cd.gamma = float(value)
            self._commit(f"{channel}.gamma={value:.2f}")

    def _on_spin(self, channel: str) -> None:
        row, cd = self._rows.get(channel), self.profile.channels.get(channel)
        if row is None or cd is None or self.profile.mode != "manual":
            return
        lo, hi = row.low.value(), row.high.value()
        if hi <= lo:
            hi = min(255.0, lo + 1.0)
        cd.low, cd.high = lo, hi
        row.hist.set_bounds(lo, hi)
        self._commit(f"{channel} bounds {lo:.0f}–{hi:.0f}")

    def _on_drag(self, channel: str, low: float, high: float) -> None:
        row, cd = self._rows.get(channel), self.profile.channels.get(channel)
        if row is None or cd is None:
            return
        cd.low, cd.high = float(low), float(high)
        for spin, v in ((row.low, low), (row.high, high)):
            spin.blockSignals(True)
            spin.setValue(v)
            spin.blockSignals(False)
        self._commit()

    def _pick_colour(self, channel: str) -> None:
        cd = self.profile.channels.get(channel)
        if cd is None:
            return
        colour = QColorDialog.getColor(QColor.fromRgbF(*cd.colour), self, f"Colour of {channel}")
        if not colour.isValid():
            return
        cd.colour = (colour.redF(), colour.greenF(), colour.blueF())
        self.profile.colour_preset = "custom"
        self._refresh_from_profile()
        self._commit(f"{channel}.colour={cd.colour}")

    def _on_white_enabled(self, on: bool) -> None:
        self.profile.white.enabled = bool(on)
        self._commit(f"WHITE.enabled={on}")

    def _on_white_preset(self, _index: int) -> None:
        if self._white is None:
            return
        key = self._white.preset.currentData()
        white = self.profile.white
        if key == "auto":
            white.preset_auto = True
            median = self.ctx.white_medians.get(str(getattr(self.ctx.active, "root", "")))
            name = display.white_preset_for_median(median) if median is not None else white.preset
        else:
            white.preset_auto = False
            name = key
        display.apply_white_preset(self.profile, name)
        log.info("Display: WHITE %s%s", "brightfield" if name == "brightfield" else "phase contrast",
                 " (auto-detected)" if key == "auto" else "")
        self._refresh_rows()
        self._commit(f"WHITE preset={key}")

    def _on_white_weight(self, value: int) -> None:
        self.profile.white.weight = value / 100.0
        if self._white is not None:
            self._white.weight_label.setText(f"{value / 100.0:.2f}")
        self._commit()

    def _on_white_gamma(self, value: float) -> None:
        self.profile.white.gamma = float(value)
        self._commit(f"WHITE.gamma={value:.2f}")

    def _on_white_spin(self) -> None:
        w = self._white
        if w is None or self.profile.mode != "manual":
            return
        lo, hi = w.low.value(), w.high.value()
        if hi <= lo:
            hi = min(255.0, lo + 1.0)
        self.profile.white.low, self.profile.white.high = lo, hi
        w.hist.set_bounds(lo, hi)
        self._commit(f"WHITE bounds {lo:.0f}–{hi:.0f}")

    def _on_white_drag(self, low: float, high: float) -> None:
        w = self._white
        if w is None:
            return
        self.profile.white.low, self.profile.white.high = float(low), float(high)
        for spin, v in ((w.low, low), (w.high, high)):
            spin.blockSignals(True)
            spin.setValue(v)
            spin.blockSignals(False)
        self._commit()

    def _reset_to_auto(self) -> None:
        auto = self.ctx.auto_bounds or {}
        p = self.profile
        for ch, cd in p.channels.items():
            if ch in auto:
                cd.low, cd.high = auto[ch]
            cd.gamma = 1.0
        if "WHITE" in auto:
            p.white.low, p.white.high = auto["WHITE"]
        log.info("Display: manual handles reset to the Auto-normalised bounds")
        self._refresh_rows()
        self._commit("reset to auto")

    def _measure_all(self) -> None:
        self.measure_button.setEnabled(False)
        self.ctx.compute_all_requested.emit()

    # ---- profile files --------------------------------------------------------------------- #
    def _profiles_folder(self) -> str:
        settings = self._settings()
        folder = getattr(settings, "last_profile_folder", "") if settings is not None else ""
        if folder and Path(folder).is_dir():
            return folder
        try:
            from ..engine.userdata import profiles_dir

            return str(profiles_dir())
        except Exception:
            return str(Path.home())

    def _save_profile(self) -> None:
        ds = self.ctx.active
        name = f"{getattr(ds, 'name', 'display')}_profile.json"
        path, _ = QFileDialog.getSaveFileName(self, "Save display profile", str(Path(self._profiles_folder()) / name),
                                              "Display profile (*.json)")
        if not path:
            return
        self.profile.save(path)
        settings = self._settings()
        if settings is not None:
            settings.last_profile_folder = str(Path(path).parent)
        log.info("Display profile saved: %s", path)

    def _load_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load display profile", self._profiles_folder(), "Display profile (*.json)")
        if path:
            self.load_profile_file(path)

    def _on_channel_names(self, names) -> None:
        for ch, row in self._rows.items():
            row.name.setText(f"{ch} · {names[ch]}" if names.get(ch) else ch)

    def apply_profile(self, profile: DisplayProfile) -> None:
        """Show ``profile`` (per-experiment memory, 0.7) without making it the last manual profile."""
        adapt_profile(profile, list(getattr(self.ctx.active, "channels", []) or []))
        self._updating = True
        try:
            self.ctx.set_profile(profile)
        finally:
            self._updating = False
        self._refresh_from_profile()

    def load_profile_file(self, path) -> bool:
        try:
            profile = DisplayProfile.load(path)
        except Exception as exc:
            log.error("Display profile could not be read: %s", exc)
            return False
        channels = list(getattr(self.ctx.active, "channels", []) or [])
        adapt_profile(profile, channels)
        settings = self._settings()
        if settings is not None:
            settings.last_profile_folder = str(Path(path).parent)
        self._updating = True
        try:
            self.ctx.set_profile(profile)
        finally:
            self._updating = False
        self._refresh_from_profile()
        self._save_timer.start()
        log.info("Display profile loaded: %s (%s)", path, display.profile_summary(profile, self._bounds()))
        return True
