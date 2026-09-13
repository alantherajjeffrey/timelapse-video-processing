"""Output section of the right panel: what Process writes and where.

The Preset row at the top (0.8) fills the panel from a built-in or saved output preset and saves
the current choices as a new one; Quick video and the queue use presets the same way.
``options_for(dataset)`` turns the widgets plus ``ctx.profile`` / ``ctx.calibration``
into the engine ``Options`` dataclass; field names are resolved against the real
dataclass so the panel survives the engine port renaming ``video_width`` to ``width``.
"""
from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QSpinBox,
    QFileDialog,
    QInputDialog,
    QMenu,
    QMessageBox,
    QToolButton,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from .jobs_qt import EngineUnavailable, engine
from ..engine import presets as engine_presets
from . import presets_store
from .settings import WIDTH_CHOICES, Settings

log = logging.getLogger("etaluma.ui")

#: MJPG bytes per pixel (plan 4.8: 0.3–1.0, 0.5 used as the estimate); H.264 ≈ 1/10.
MJPG_BYTES_PER_PIXEL = 0.5
MJPG_RANGE = (0.3, 1.0)
H264_FRACTION = 0.1

#: Widget value -> candidate Options field names, in order of preference.
OPTION_ALIASES: dict[str, tuple[str, ...]] = {
    "width": ("width", "video_width"),
    "duration_seconds": ("duration_seconds", "duration"),
    "fps": ("fps",),
    "playback_source": ("playback_source",),
    "avi": ("avi",),
    "mp4": ("mp4", "mp4_copies"),
    "quick": ("quick",),
    "composite": ("composite_videos", "composite"),
    "fluorescence_only": ("fluorescence_only_video", "fluorescence_only", "fluor_only"),
    "channel_videos": ("channel_videos",),
    "per_channel": ("per_channel", "channel_videos"),
    "montages": ("montages",),
    "dashboard": ("dashboard",),
    "app_scale_bar": ("app_scale_bar", "video_scale_bar"),
    "app_timestamp": ("app_timestamp", "timestamp"),
    "exclude_overlays": ("exclude_overlays",),
    "rois": ("rois",),
    "channels": ("channels",),
    "output": ("output",),
    "profile": ("profile",),
    "calibration": ("calibration",),
    "objective": ("objective",),
}


def human_bytes(value: float) -> str:
    """1234567 -> '1.2 MB'."""
    step = 1024.0
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(value) < step or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} TB"  # pragma: no cover


def build_options(values: dict, options_class) -> object:
    """Instantiate ``Options`` from widget values, mapping names onto the real fields."""
    field_names = {f.name for f in dataclasses.fields(options_class)}
    kwargs: dict[str, object] = {}
    leftover: dict[str, object] = {}
    for key, value in values.items():
        for candidate in OPTION_ALIASES.get(key, (key,)):
            if candidate in field_names:
                kwargs[candidate] = value
                break
        else:
            leftover[key] = value
    if leftover:
        log.debug("Options fields not present in the engine dataclass, ignored: %s", sorted(leftover))
    return options_class(**kwargs)


def dataset_shape(dataset) -> tuple[int, int, int]:
    """(positions, channels, timepoints per position and channel) of a Dataset, defensively."""
    frames = list(getattr(dataset, "frames", []) or [])
    if not frames:
        return 0, 0, 0
    rois = {getattr(f, "roi", "") for f in frames}
    channels = {getattr(f, "channel", "") for f in frames}
    groups: dict[tuple[str, str], int] = {}
    for f in frames:
        key = (getattr(f, "roi", ""), getattr(f, "channel", ""))
        groups[key] = groups.get(key, 0) + 1
    timepoints = max(groups.values()) if groups else 0
    return len(rois), len(channels), timepoints


def local_estimate_bytes(dataset, values: dict) -> tuple[float, float, float]:
    """(estimate, low, high) bytes for the selected outputs, without the engine."""
    positions, channels, timepoints = dataset_shape(dataset)
    if not positions or not timepoints:
        return 0.0, 0.0, 0.0
    channel_names = {getattr(f, "channel", "") for f in getattr(dataset, "frames", [])}
    fluorescence = [c for c in channel_names if c.startswith("F")]
    videos = 0
    if values.get("per_channel", True):
        videos += max(1, len(channel_names))
    if values.get("composite", True) and fluorescence:
        videos += 1
    if values.get("fluorescence_only", True) and fluorescence:
        videos += 1
    videos = max(videos, 1)
    width = float(values.get("width", 950))
    pixels = width * width
    per_video_frames = timepoints
    streams = 0.0
    if values.get("avi", True):
        streams += 1.0
    if values.get("mp4", True):
        streams += H264_FRACTION
    total_frames = positions * videos * per_video_frames
    estimate = total_frames * pixels * MJPG_BYTES_PER_PIXEL * streams
    low = total_frames * pixels * MJPG_RANGE[0] * streams
    high = total_frames * pixels * MJPG_RANGE[1] * streams
    if values.get("montages", True):
        estimate += positions * 1.5e6
    if values.get("dashboard", True):
        estimate += 2.0e6
    return estimate, low, high


class OutputPanel(QWidget):
    """Duration, width, containers, products, overlay toggles and the output folder."""

    changed = Signal()

    def __init__(self, ctx, parent: QWidget | None = None, settings: Settings | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.settings = settings or Settings()
        self._building = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.heading = heading = QLabel("Output")
        heading.setProperty("role", "heading")
        layout.addWidget(heading)

        # 0.8: output presets
        preset_row = QHBoxLayout()
        preset_row.setSpacing(6)
        self.preset_combo = QComboBox()
        self.preset_combo.setToolTip("Built-in and saved output presets. Picking one fills this panel; "
                                     "any change shows 'Current (modified)'.")
        self.save_preset_button = QPushButton("Save as…")
        self.save_preset_button.setToolTip("Save these output choices as a preset for Process, Quick video "
                                           "and the queue")
        self.preset_menu_button = QToolButton()
        self.preset_menu_button.setText("⋯")
        self.preset_menu_button.setToolTip("Rename or delete the saved preset")
        self.preset_menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        preset_menu = QMenu(self.preset_menu_button)
        self.rename_preset_action = preset_menu.addAction("Rename preset…")
        self.delete_preset_action = preset_menu.addAction("Delete preset")
        self.preset_menu_button.setMenu(preset_menu)
        preset_row.addWidget(_label("Preset"))
        preset_row.addWidget(self.preset_combo, 1)
        preset_row.addWidget(self.save_preset_button)
        preset_row.addWidget(self.preset_menu_button)
        layout.addLayout(preset_row)
        self._preset_key = str(getattr(self.settings, "output_preset", "") or "builtin:standard")
        self._preset_modified = False

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)

        # playback: total duration or fixed fps
        timing = QHBoxLayout()
        timing.setSpacing(6)
        self.duration_radio = QRadioButton("Duration")
        self.duration_radio.setChecked(True)
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(0.5, 600.0)
        self.duration_spin.setDecimals(1)
        self.duration_spin.setSingleStep(1.0)
        self.duration_spin.setSuffix(" s")
        self.duration_spin.setValue(float(self.settings.duration_seconds))
        self.fps_radio = QRadioButton("Fps")
        self.fps_spin = QDoubleSpinBox()
        self.fps_spin.setRange(0.1, 120.0)
        self.fps_spin.setDecimals(1)
        self.fps_spin.setValue(20.0)
        self.fps_spin.setEnabled(False)
        for w in (self.duration_radio, self.duration_spin, self.fps_radio, self.fps_spin):
            timing.addWidget(w)
        timing.addStretch(1)
        form.addRow("Playback", _wrap(timing))

        self.width_combo = QComboBox()
        for w in WIDTH_CHOICES:
            self.width_combo.addItem(f"{w} px", w)
        self._select_width(int(self.settings.default_width))
        form.addRow("Video width", self.width_combo)
        range_row = QHBoxLayout()
        range_row.setSpacing(6)
        self.first_spin, self.last_spin = QSpinBox(), QSpinBox()
        for spin in (self.first_spin, self.last_spin):
            spin.setRange(1, 1)
            spin.setKeyboardTracking(False)
        self.range_total = _label("of 1")
        range_row.addWidget(self.first_spin)
        range_row.addWidget(QLabel("to"))
        range_row.addWidget(self.last_spin)
        range_row.addWidget(self.range_total)
        range_row.addStretch(1)
        self.range_widget = _wrap(range_row)
        self.range_widget.setToolTip("Export only these timepoints (videos and montages). "
                                     "The Auto-normalised display still uses every timepoint.")
        form.addRow("Timepoints", self.range_widget)

        containers = QHBoxLayout()
        containers.setSpacing(10)
        self.avi_check = QCheckBox("AVI (MJPG)")
        self.avi_check.setChecked(bool(self.settings.avi))
        self.mp4_check = QCheckBox("MP4 (H.264)")
        self.mp4_check.setChecked(bool(self.settings.mp4))
        containers.addWidget(self.avi_check)
        containers.addWidget(self.mp4_check)
        containers.addStretch(1)
        form.addRow("Containers", _wrap(containers))
        self.quality_combo = QComboBox()
        for key, label in (("high", "High (CRF 18, largest files)"), ("standard", "Standard (CRF 23)"),
                           ("small", "Small (CRF 28, for email and slides)")):
            self.quality_combo.addItem(label, key)
        quality_index = self.quality_combo.findData(str(getattr(self.settings, "video_quality", "standard") or "standard"))
        self.quality_combo.setCurrentIndex(quality_index if quality_index >= 0 else 1)
        self.quality_combo.setToolTip("H.264 quality of the MP4 files. Standard looks like High on screen at "
                                      "under half the size; Small suits email and slides.")
        form.addRow("MP4 quality", self.quality_combo)
        layout.addLayout(form)

        products = QGridLayout()
        products.setHorizontalSpacing(10)
        products.setVerticalSpacing(4)
        self.composite_check = QCheckBox("Composite")
        self.composite_check.setChecked(bool(self.settings.composite))
        self.fluor_check = QCheckBox("Fluorescence only")
        self.fluor_check.setChecked(bool(self.settings.fluorescence_only))
        self.per_channel_check = QCheckBox("Per channel")
        self.per_channel_check.setChecked(bool(self.settings.per_channel))
        self.montages_check = QCheckBox("Montages")
        self.montages_check.setChecked(bool(self.settings.montages))
        self.dashboard_check = QCheckBox("Dashboard")
        self.dashboard_check.setChecked(bool(self.settings.dashboard))
        for i, w in enumerate(
            (self.composite_check, self.fluor_check, self.per_channel_check, self.montages_check, self.dashboard_check)
        ):
            products.addWidget(w, i // 2, i % 2)
        self.per_channel_check.setVisible(False)  # 0.5: one checkbox per channel, below
        self.composite_check.setToolTip("Every enabled channel over the dimmed WHITE image")
        self.fluor_check.setToolTip("The fluorescence channels without WHITE (needs WHITE and fluorescence)")
        layout.addLayout(products)
        self.channel_label = _label("Single-channel videos")
        self.channel_label.setToolTip("Tick a channel to also write its own video. Unticked channels still "
                                      "take part in the composites.")
        self.channel_box = QWidget()
        self.channel_row = QHBoxLayout(self.channel_box)
        self.channel_row.setContentsMargins(0, 0, 0, 0)
        self.channel_row.setSpacing(10)
        self.channel_checks: dict[str, QCheckBox] = {}
        layout.addWidget(self.channel_label)
        layout.addWidget(self.channel_box)
        # 0.7: channel names, used on montages, in the channel key and in the app's labels
        self.names_label = _label("Channel names (montages, channel key)")
        self.names_box = QWidget()
        self.names_grid = QGridLayout(self.names_box)
        self.names_grid.setContentsMargins(0, 0, 0, 0)
        self.names_grid.setHorizontalSpacing(6)
        self.names_grid.setVerticalSpacing(4)
        self.name_edits: dict[str, QLineEdit] = {}
        layout.addWidget(self.names_label)
        layout.addWidget(self.names_box)

        # burned into the videos (0.6): time, experiment and position name, app scale bar; a section in 0.7
        from .sections import CollapsibleSection  # noqa: WPS433

        self.videos_box = QWidget()
        overlays = QGridLayout(self.videos_box)
        overlays.setContentsMargins(0, 4, 0, 0)
        overlays.setHorizontalSpacing(8)
        overlays.setVerticalSpacing(4)
        self.timestamp_check = QCheckBox("Time")
        self.timestamp_check.setChecked(bool(self.settings.app_timestamp))
        self.time_offset_edit = QLineEdit(str(getattr(self.settings, "time_offset", "") or ""))
        self.time_offset_edit.setPlaceholderText("start: Day 0 : 00:00:00")
        self._offset_tip = ("Start time added to every label, for one experiment split over several folders: "
                            "Day 2 : 06:00:00, 30:00:00, 6:30 or a number of hours. The interval comes from the protocol.")
        self.time_offset_edit.setToolTip(self._offset_tip)
        self.time_days_combo = QComboBox()
        for key, label in (("auto", "Days: auto"), ("always", "Days: always"), ("never", "Days: never")):
            self.time_days_combo.addItem(label, key)
        days_index = self.time_days_combo.findData(str(getattr(self.settings, "time_days", "auto") or "auto"))
        self.time_days_combo.setCurrentIndex(max(0, days_index))
        self.time_days_combo.setToolTip("Auto writes 'Day d :' only when the video reaches 24 hours.")
        self.name_label_check = QCheckBox("Experiment and position name")
        self.name_label_check.setChecked(bool(getattr(self.settings, "name_label", False)))
        self.name_label_check.setToolTip("'<experiment folder> / <position>' beside the time, at the same size. "
                                         "Quick video always shows it.")
        self.scale_bar_check = QCheckBox("App scale bar")
        self.scale_bar_check.setChecked(bool(self.settings.app_scale_bar))
        self.key_check = QCheckBox("Channel key (colour and name of each channel)")
        self.key_check.setChecked(bool(getattr(self.settings, "channel_key", False)))
        self.key_check.setToolTip("A small colour key of the channels in the video, top left.")
        self.exclude_overlays_check = QCheckBox("Leave Lumaview's overlays out of measurements")
        self.exclude_overlays_check.setChecked(bool(self.settings.exclude_overlays))
        self.exclude_overlays_check.setToolTip("Leave Lumaview's burned-in timestamp and scale bar out of histograms "
                                               "and measurements.")
        overlays.addWidget(self.timestamp_check, 0, 0)
        overlays.addWidget(self.time_offset_edit, 0, 1)
        overlays.addWidget(self.time_days_combo, 0, 2)
        overlays.addWidget(self.name_label_check, 1, 0, 1, 3)
        overlays.addWidget(self.scale_bar_check, 2, 0, 1, 3)
        overlays.addWidget(self.key_check, 3, 0, 1, 3)
        overlays.addWidget(self.exclude_overlays_check, 4, 0, 1, 3)
        overlays.setColumnStretch(1, 1)
        self.videos_section = CollapsibleSection("On the videos", self.videos_box)
        layout.addWidget(self.videos_section)
        self._offset_seconds = 0.0
        self._validate_offset()

        # fixed images: measurement threshold and the per-position display rule
        self.fixed_box = QWidget()
        fixed = QGridLayout(self.fixed_box)
        fixed.setContentsMargins(0, 4, 0, 0)
        fixed.setHorizontalSpacing(8)
        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(0.0, 255.0)
        self.threshold_spin.setDecimals(0)
        self.threshold_spin.setValue(50.0)
        self.threshold_spin.setToolTip("Pixels strictly above this raw 8-bit value count as positive area")
        self.lock_profile_check = QCheckBox("One manual display for every position")
        self.lock_profile_check.setToolTip(
            "Fixed images: composites use each position's own Auto-normalised bounds unless this is on "
            "and the display is Manual. Measurements are always raw.")
        fixed.addWidget(_label("Measurements (fixed images)"), 0, 0, 1, 2)
        fixed.addWidget(QLabel("Positive threshold"), 1, 0)
        fixed.addWidget(self.threshold_spin, 1, 1)
        fixed.addWidget(self.lock_profile_check, 2, 0, 1, 2)
        layout.addWidget(self.fixed_box)

        folder_row = QHBoxLayout()
        folder_row.setSpacing(6)
        self.folder_edit = QLineEdit(self.settings.last_output_folder)
        self.folder_edit.setPlaceholderText("<experiment>\\analysis_output")
        self.browse_button = QPushButton("Browse")
        self.browse_button.clicked.connect(self.browse_output_folder)
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(self.browse_button)
        layout.addWidget(_label("Output folder"))
        layout.addLayout(folder_row)

        self.estimate_label = QLabel("Estimated output size: open a folder first")
        self.estimate_label.setProperty("role", "muted")
        self.estimate_label.setWordWrap(True)
        layout.addWidget(self.estimate_label)
        layout.addStretch(1)

        # ---- wiring ----------------------------------------------------- #
        self.duration_radio.toggled.connect(self._on_timing_toggled)
        for widget in (self.duration_spin, self.fps_spin):
            widget.valueChanged.connect(self._on_changed)
        self.width_combo.currentIndexChanged.connect(self._on_changed)
        for check in (
            self.avi_check,
            self.mp4_check,
            self.composite_check,
            self.fluor_check,
            self.per_channel_check,
            self.montages_check,
            self.dashboard_check,
            self.scale_bar_check,
            self.timestamp_check,
            self.name_label_check,
            self.exclude_overlays_check,
        ):
            check.toggled.connect(self._on_changed)
        self.time_offset_edit.textChanged.connect(self._on_offset_changed)
        self.time_days_combo.currentIndexChanged.connect(self._on_changed)
        self.quality_combo.currentIndexChanged.connect(self._on_changed)
        self.key_check.toggled.connect(self._on_changed)
        self.first_spin.valueChanged.connect(self._on_range_changed)
        self.last_spin.valueChanged.connect(self._on_range_changed)
        self.folder_edit.textChanged.connect(self._on_changed)
        self.threshold_spin.valueChanged.connect(self._on_changed)
        self.lock_profile_check.toggled.connect(self._on_changed)
        self.preset_combo.activated.connect(self._on_preset_activated)
        self.save_preset_button.clicked.connect(self._ask_save_preset)
        self.rename_preset_action.triggered.connect(self._ask_rename_preset)
        self.delete_preset_action.triggered.connect(self._ask_delete_preset)
        self.reload_presets()

        if ctx is not None:
            ctx.active_dataset_changed.connect(self._on_dataset)
            ctx.calibration_changed.connect(lambda _cal, overlays: self._apply_overlays(overlays))
            ctx.mode_changed.connect(self._on_mode)
            self._apply_overlays(getattr(ctx, "overlays", None))
        self._on_mode(getattr(ctx, "mode", "timelapse") if ctx is not None else "timelapse")
        self._building = False
        self._on_dataset(getattr(ctx, "active", None) if ctx is not None else None)
        self._publish_time_format()

    # ---- values ---------------------------------------------------------- #
    def _select_width(self, width: int) -> None:
        index = self.width_combo.findData(int(width))
        self.width_combo.setCurrentIndex(index if index >= 0 else self.width_combo.findData(950))

    @property
    def width(self) -> int:
        return int(self.width_combo.currentData() or 950)

    def output_folder(self, dataset=None) -> Path | None:
        text = self.folder_edit.text().strip()
        if text:
            return Path(text)
        ds = dataset if dataset is not None else (getattr(self.ctx, "active", None) if self.ctx else None)
        root = getattr(ds, "root", None)
        return Path(root) / "analysis_output" if root else None

    def values(self) -> dict:
        """Plain dict of every control, used for the estimate and for Options."""
        use_fps = self.fps_radio.isChecked()
        return {
            "width": self.width,
            "duration_seconds": float(self.duration_spin.value()),
            "fps": float(self.fps_spin.value()) if use_fps else None,
            "playback_source": "fps" if use_fps else "duration",
            "avi": self.avi_check.isChecked(),
            "mp4": self.mp4_check.isChecked(),
            "composite": self.composite_check.isChecked(),
            "fluorescence_only": self.fluor_check.isChecked(),
            "per_channel": bool(self.selected_channel_videos()),
            "channel_videos": self.selected_channel_videos(),
            "montages": self.montages_check.isChecked(),
            "dashboard": self.dashboard_check.isChecked(),
            "app_scale_bar": self.scale_bar_check.isChecked(),
            "app_timestamp": self.timestamp_check.isChecked(),
            "name_label": self.name_label_check.isChecked(),
            "time_offset_seconds": float(self._offset_seconds),
            "time_days": str(self.time_days_combo.currentData() or "auto"),
            "video_quality": str(self.quality_combo.currentData() or "standard"),
            "channel_names": self.channel_names(),
            "channel_key": self.key_check.isChecked(),
            "timepoint_range": self.timepoint_range(),
            "exclude_overlays": self.exclude_overlays_check.isChecked(),
            "mode": str(getattr(self.ctx, "mode", "auto") or "auto") if self.ctx is not None else "auto",
            "threshold": float(self.threshold_spin.value()),
            "lock_manual_profile_for_fixed": self.lock_profile_check.isChecked(),
        }

    def selected_channel_videos(self) -> list[str]:
        return [ch for ch, check in self.channel_checks.items() if check.isChecked()]

    def _rebuild_channel_checks(self, dataset) -> None:
        while self.channel_row.count():
            w = self.channel_row.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self.channel_checks.clear()
        extra = self.settings.extra or {}
        # 0.8: until a choice is saved, every channel gets its own video, as in the Standard preset
        wanted = set(extra["channel_videos"]) if "channel_videos" in extra else None
        channels = list(getattr(dataset, "channels", []) or []) if dataset is not None else []
        for ch in channels:
            check = QCheckBox(ch)
            check.setChecked(wanted is None or ch in wanted)
            check.setToolTip(f"Also write a video of {ch} alone")
            check.toggled.connect(self._on_channel_toggled)
            self.channel_row.addWidget(check)
            self.channel_checks[ch] = check
        self.channel_row.addStretch(1)
        self.channel_label.setVisible(bool(channels))
        self.channel_box.setVisible(bool(channels))
        self._rebuild_names(channels)
        self._reset_range(dataset)

    # ---- 0.7: channel names, timepoint range, per-experiment memory ------------------- #
    CHANNEL_HINTS = {"WHITE": "phase, brightfield", "F1": "e.g. Hoechst", "F2": "e.g. CFDA, GFP", "F3": "e.g. PI, mCherry"}

    def _rebuild_names(self, channels) -> None:
        while self.names_grid.count():
            w = self.names_grid.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self.name_edits.clear()
        for i, ch in enumerate(channels):
            edit = QLineEdit()
            edit.setPlaceholderText(self.CHANNEL_HINTS.get(ch, "name"))
            edit.setClearButtonEnabled(True)
            edit.setToolTip(f"Name of {ch}, shown on montages, in the channel key and in the app")
            edit.textChanged.connect(self._on_names_changed)
            self.names_grid.addWidget(QLabel(ch), i // 2, (i % 2) * 2)
            self.names_grid.addWidget(edit, i // 2, (i % 2) * 2 + 1)
            self.name_edits[ch] = edit
        self.names_label.setVisible(bool(channels))
        self.names_box.setVisible(bool(channels))
        self._publish_channel_names()

    def channel_names(self) -> dict[str, str]:
        return {ch: e.text().strip() for ch, e in self.name_edits.items() if e.text().strip()}

    def _on_names_changed(self, _text: str) -> None:
        self._publish_channel_names()
        self._on_changed()

    def _publish_channel_names(self) -> None:
        if self.ctx is not None and hasattr(self.ctx, "set_channel_names"):
            self.ctx.set_channel_names(self.channel_names())

    def _reset_range(self, dataset) -> None:
        serials = sorted({getattr(f, "serial", 0) for f in (getattr(dataset, "frames", None) or [])})
        n = max(1, len(serials))
        for spin, value in ((self.first_spin, 1), (self.last_spin, n)):
            spin.blockSignals(True)
            spin.setRange(1, n)
            spin.setValue(value)
            spin.blockSignals(False)
        self.range_total.setText(f"of {n}")
        self.range_widget.setEnabled(n > 1)

    def _on_range_changed(self, _value: int) -> None:
        if self.first_spin.value() > self.last_spin.value():
            other = self.last_spin if self.sender() is self.first_spin else self.first_spin
            other.blockSignals(True)
            other.setValue(self.first_spin.value() if other is self.last_spin else self.last_spin.value())
            other.blockSignals(False)
        self._on_changed()

    def timepoint_range(self) -> list[int] | None:
        first, last, n = self.first_spin.value(), self.last_spin.value(), self.last_spin.maximum()
        return None if (first, last) == (1, n) else [first, last]

    def experiment_record(self) -> dict:
        return {"channel_names": self.channel_names(), "time_offset": self.time_offset_edit.text().strip(),
                "time_days": str(self.time_days_combo.currentData() or "auto"),
                "timepoint_range": self.timepoint_range()}

    def apply_experiment(self, record: dict) -> None:
        """Per-experiment memory: channel names, start time, days and timepoint range."""
        self._building = True
        try:
            for ch, text in (record.get("channel_names") or {}).items():
                if ch in self.name_edits:
                    self.name_edits[ch].setText(str(text))
            if "time_offset" in record:
                self.time_offset_edit.setText(str(record.get("time_offset") or ""))
            index = self.time_days_combo.findData(record.get("time_days"))
            if index >= 0:
                self.time_days_combo.setCurrentIndex(index)
            span = record.get("timepoint_range")
            if span and len(span) == 2:
                self.first_spin.setValue(int(span[0]))
                self.last_spin.setValue(int(span[1]))
        finally:
            self._building = False
        self._validate_offset()
        self._publish_channel_names()
        self._on_changed()

    def _on_channel_toggled(self, _on: bool) -> None:
        self.settings.extra["channel_videos"] = self.selected_channel_videos()
        log.debug("output: single-channel videos %s", self.selected_channel_videos() or "none")
        self._on_changed()

    def _on_mode(self, mode: str) -> None:
        self.fixed_box.setVisible(mode == "fixed")
        if not self._building:
            self.update_estimate()

    def apply_to_settings(self, settings: Settings | None = None) -> Settings:
        """Copy the current controls into Settings (saved when the window closes)."""
        s = settings or self.settings
        v = self.values()
        s.update(
            default_width=v["width"],
            duration_seconds=v["duration_seconds"],
            avi=v["avi"],
            mp4=v["mp4"],
            composite=v["composite"],
            fluorescence_only=v["fluorescence_only"],
            per_channel=v["per_channel"],
            montages=v["montages"],
            dashboard=v["dashboard"],
            app_scale_bar=v["app_scale_bar"],
            app_timestamp=v["app_timestamp"],
            name_label=v["name_label"],
            time_offset=self.time_offset_edit.text().strip(),
            time_days=v["time_days"],
            video_quality=v["video_quality"],
            channel_key=v["channel_key"],
            exclude_overlays=v["exclude_overlays"],
        )
        return s

    # ---- output presets (0.8) ----------------------------------------------- #
    def preset_snapshot(self) -> dict:
        """The panel's choices as an output preset (per channel: every channel ticked)."""
        values = self.values()
        snapshot = {key: values[key] for key in engine_presets.PRESET_KEYS if key in values}
        checks = list(self.channel_checks.values())
        snapshot["per_channel"] = all(c.isChecked() for c in checks) if checks else bool(self.settings.per_channel)
        return engine_presets.normalise(snapshot)

    @property
    def preset_key(self) -> str:
        """The preset the panel started from (``builtin:<id>`` or ``user:<name>``)."""
        return self._preset_key

    @property
    def preset_modified(self) -> bool:
        return self._preset_modified

    def reload_presets(self) -> None:
        combo = self.preset_combo
        combo.blockSignals(True)
        combo.clear()
        for key, label in presets_store.preset_choices():
            combo.addItem(label, key)
            combo.setItemData(combo.count() - 1, presets_store.preset_description(key), Qt.ItemDataRole.ToolTipRole)
        combo.blockSignals(False)
        if combo.findData(self._preset_key) < 0:
            self._preset_key = "builtin:standard"
        self._refresh_preset_state()

    def _refresh_preset_state(self) -> None:
        try:
            base = presets_store.preset_values(self._preset_key)
        except ValueError:
            base = None
        modified = base is None or not engine_presets.same(base, self.preset_snapshot())
        combo = self.preset_combo
        combo.blockSignals(True)
        try:
            index = combo.findData(presets_store.CURRENT)
            if modified:
                label = f"Current (modified from {presets_store.preset_label(self._preset_key)})"
                if index < 0:
                    combo.addItem(label, presets_store.CURRENT)
                    index = combo.count() - 1
                combo.setItemText(index, label)
                combo.setCurrentIndex(index)
            else:
                if index >= 0:
                    combo.removeItem(index)
                combo.setCurrentIndex(max(0, combo.findData(self._preset_key)))
        finally:
            combo.blockSignals(False)
        user = self._preset_key.startswith("user:")
        self.rename_preset_action.setEnabled(user)
        self.delete_preset_action.setEnabled(user)
        self._preset_modified = modified
        self.settings.output_preset = self._preset_key

    def _on_preset_activated(self, index: int) -> None:
        key = self.preset_combo.itemData(index)
        if key and key != presets_store.CURRENT:
            self.apply_preset(str(key))

    def apply_preset(self, key: str) -> bool:
        """Fill the panel from a preset. Experiment choices (names, start time, range) stay."""
        try:
            values = presets_store.preset_values(key)
        except ValueError as exc:
            log.error("%s", exc)
            self.reload_presets()
            return False
        self._preset_key = key
        self.apply_preset_values(values)
        log.info("Output preset: %s", presets_store.preset_label(key))
        return True

    def apply_preset_values(self, values: dict) -> None:
        v = engine_presets.normalise(values)
        self._building = True
        try:
            self._select_width(int(v["width"]))
            self.avi_check.setChecked(v["avi"])
            self.mp4_check.setChecked(v["mp4"])
            self.quality_combo.setCurrentIndex(max(0, self.quality_combo.findData(v["video_quality"])))
            if v["playback_source"] == "fps":
                self.fps_radio.setChecked(True)
                self.fps_spin.setValue(float(v["fps"]))
            else:
                self.duration_radio.setChecked(True)
                self.duration_spin.setValue(float(v["duration_seconds"]))
            self.composite_check.setChecked(v["composite"])
            self.fluor_check.setChecked(v["fluorescence_only"])
            self.per_channel_check.setChecked(v["per_channel"])
            for check in self.channel_checks.values():
                check.setChecked(v["per_channel"])
            self.montages_check.setChecked(v["montages"])
            self.dashboard_check.setChecked(v["dashboard"])
            self.timestamp_check.setChecked(v["app_timestamp"])
            self.time_days_combo.setCurrentIndex(max(0, self.time_days_combo.findData(v["time_days"])))
            self.name_label_check.setChecked(v["name_label"])
            self.scale_bar_check.setChecked(v["app_scale_bar"])
            self.key_check.setChecked(v["channel_key"])
            self.exclude_overlays_check.setChecked(v["exclude_overlays"])
        finally:
            self._building = False
        self.settings.extra["channel_videos"] = self.selected_channel_videos()
        self._on_timing_toggled(self.duration_radio.isChecked())

    def save_preset_as(self, name: str) -> str | None:
        try:
            key = presets_store.save_user_preset(name, self.preset_snapshot())
        except (ValueError, OSError) as exc:
            log.error("Output preset not saved: %s", exc)
            self._last_preset_error = str(exc)
            return None
        self._preset_key = key
        self.reload_presets()
        log.info("Output preset saved: %s", presets_store.preset_label(key))
        return key

    def rename_preset(self, new_name: str) -> str | None:
        if not self._preset_key.startswith("user:"):
            return None
        old = self._preset_key.partition(":")[2]
        try:
            key = presets_store.rename_user_preset(old, new_name)
        except (ValueError, OSError) as exc:
            log.error("Output preset not renamed: %s", exc)
            self._last_preset_error = str(exc)
            return None
        self._preset_key = key
        self.reload_presets()
        log.info("Output preset %s renamed to %s", old, presets_store.preset_label(key))
        return key

    def delete_preset(self) -> bool:
        if not self._preset_key.startswith("user:"):
            return False
        name = self._preset_key.partition(":")[2]
        try:
            presets_store.delete_user_preset(name)
        except (ValueError, OSError) as exc:
            log.error("Output preset not deleted: %s", exc)
            return False
        self._preset_key = "builtin:standard"
        self.reload_presets()
        log.info("Output preset deleted: %s", name)
        return True

    def _ask_save_preset(self) -> None:
        name, ok = QInputDialog.getText(self, "Save output preset", "Name of the new preset:")
        if ok and name.strip() and self.save_preset_as(name) is None:
            QMessageBox.warning(self, "Output preset", getattr(self, "_last_preset_error", "Not saved."))

    def _ask_rename_preset(self) -> None:
        old = self._preset_key.partition(":")[2]
        name, ok = QInputDialog.getText(self, "Rename output preset", "New name:", text=old)
        if ok and name.strip() and name.strip() != old and self.rename_preset(name) is None:
            QMessageBox.warning(self, "Output preset", getattr(self, "_last_preset_error", "Not renamed."))

    def _ask_delete_preset(self) -> None:
        name = self._preset_key.partition(":")[2]
        answer = QMessageBox.question(self, "Delete output preset", f"Delete the preset {name}?")
        if answer == QMessageBox.StandardButton.Yes:
            self.delete_preset()

    def apply_settings(self, settings: Settings) -> None:
        """Re-read defaults after the settings dialog changed them."""
        self.settings = settings
        self._building = True
        self._select_width(int(settings.default_width))
        self.duration_spin.setValue(float(settings.duration_seconds))
        self.avi_check.setChecked(bool(settings.avi))
        self.mp4_check.setChecked(bool(settings.mp4))
        self._building = False
        self._on_changed()

    # ---- Options --------------------------------------------------------- #
    def options_for(self, dataset, output: Path | str | None = None, quick: bool = False) -> object:
        """Build the engine ``Options`` for this dataset. Raises EngineUnavailable without the engine.

        ``output`` is the parent folder (``analysis_output``); the engine names the run folder
        itself (``run_<date>_<id>`` or ``quick_<date>_<id>`` when ``Options.quick`` is set), so it
        is only copied into Options when that dataclass has such a field.
        """
        engine_module = engine()
        options_class = getattr(engine_module, "Options", None)
        if options_class is None:
            raise EngineUnavailable("The processing engine does not provide Options yet.")
        profile = getattr(self.ctx, "profile", None) if self.ctx is not None else None
        calibration = getattr(self.ctx, "calibration", None) if self.ctx is not None else None
        if quick:
            factory = getattr(engine_module, "quick_options", None) or getattr(options_class, "quick_options", None)
            if callable(factory):
                try:
                    opts = factory(profile)
                except TypeError:
                    opts = factory()
                _set_if_possible(opts, OPTION_ALIASES["calibration"], calibration)
                _set_if_possible(opts, OPTION_ALIASES["output"], str(output) if output else None)
                chosen = self.values()
                for key in ("channel_videos", "composite", "fluorescence_only"):
                    _set_if_possible(opts, OPTION_ALIASES[key], chosen[key])
                # Quick always shows the time and the name (decision 9); the rest follows the panel
                for key in ("app_scale_bar", "time_offset_seconds", "time_days", "video_quality",
                            "channel_names", "channel_key"):
                    _set_if_possible(opts, (key,), chosen[key])
                return opts
            values = {"width": 1900, "mp4": True, "avi": False, "quick": True,
                      "duration_seconds": float(self.settings.duration_seconds), "playback_source": "duration",
                      "composite": True, "fluorescence_only": True, "per_channel": True, "montages": True,
                      "dashboard": True, "app_scale_bar": False, "app_timestamp": False, "exclude_overlays": True}
        else:
            values = self.values()
            values["quick"] = False
            if self.ctx is not None and dataset is not getattr(self.ctx, "active", None):
                values["mode"] = "auto"  # batch members follow their own detected mode
        if profile is not None:
            values["profile"] = profile
        values["calibration"] = calibration if getattr(calibration, "usable", False) else None
        objective = getattr(calibration, "objective", None)
        if objective:
            values["objective"] = objective
        values["rois"] = None
        values["channels"] = None
        if output is not None:
            values["output"] = str(output)
        return build_options(values, options_class)

    # ---- reactions ------------------------------------------------------- #
    def browse_output_folder(self) -> None:
        start = self.folder_edit.text().strip() or self.settings.last_output_folder or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose an output folder", start)
        if chosen:
            self.folder_edit.setText(chosen)
            self.settings.last_output_folder = chosen

    def _on_timing_toggled(self, duration_selected: bool) -> None:
        self.duration_spin.setEnabled(duration_selected)
        self.fps_spin.setEnabled(not duration_selected)
        self._on_changed()

    def _on_dataset(self, dataset) -> None:
        root = getattr(dataset, "root", None)
        if root:
            self.folder_edit.setText(str(Path(root) / "analysis_output"))
        self._rebuild_channel_checks(dataset)
        self.update_estimate()

    def _apply_overlays(self, overlays) -> None:
        """0.5 greyed the app overlays out whenever Lumaview's were found (always); now they cover them."""
        detected = bool(getattr(overlays, "detected", False))
        suffix = " It covers Lumaview's own." if detected else ""
        self.timestamp_check.setToolTip("Elapsed time (start time + timepoint × capture interval), bottom left. "
                                        "Quick video always shows it." + suffix)
        self.scale_bar_check.setToolTip("A readable scale bar, bottom right." + suffix)

    def _validate_offset(self) -> bool:
        from ..engine.overlays import parse_elapsed  # noqa: WPS433

        try:
            self._offset_seconds = parse_elapsed(self.time_offset_edit.text())
        except ValueError as exc:
            self.time_offset_edit.setStyleSheet("QLineEdit { border: 1px solid #e0a83c; }")
            self.time_offset_edit.setToolTip(str(exc))
            return False
        self.time_offset_edit.setStyleSheet("")
        self.time_offset_edit.setToolTip(self._offset_tip)
        return True

    def _on_offset_changed(self, _text: str) -> None:
        if self._validate_offset():
            self._on_changed()

    def _on_changed(self, *_args) -> None:
        if self._building:
            return
        self.update_estimate()
        if hasattr(self, "preset_combo"):
            self._refresh_preset_state()
        self._publish_time_format()
        self.changed.emit()

    def _publish_time_format(self) -> None:
        """The preview's time label follows the start time and days mode set here."""
        if self.ctx is not None and hasattr(self.ctx, "set_time_format"):
            self.ctx.set_time_format(float(self._offset_seconds), str(self.time_days_combo.currentData() or "auto"))

    def update_estimate(self) -> None:
        dataset = getattr(self.ctx, "active", None) if self.ctx is not None else None
        if dataset is None:
            self.estimate_label.setText("Estimated output size: open a folder first")
            return
        text = self.estimate_text(dataset)
        self.estimate_label.setText(text)

    def estimate_text(self, dataset, options=None) -> str:
        """Human-readable size estimate; uses the engine's own wording when it offers one."""
        values = self.values()
        estimate = low = high = None
        try:
            estimator = getattr(engine(), "estimate_output_bytes", None)
        except EngineUnavailable:
            estimator = None
        if estimator is not None:
            for call in (
                lambda: estimator(dataset, options if options is not None else self.options_for(dataset)),
                lambda: estimator(dataset, values),
                lambda: estimator(dataset),
            ):
                try:
                    result = call()
                except Exception as exc:  # the engine may not be ready; fall back quietly
                    log.debug("estimate_output_bytes call shape rejected: %s", exc)
                    continue
                if isinstance(result, dict):
                    if result.get("text"):
                        return str(result["text"])
                    estimate = float(result.get("total_bytes") or result.get("bytes") or 0.0)
                    low, high = result.get("avi_bytes_low"), result.get("avi_bytes_high")
                elif isinstance(result, (int, float)):
                    estimate = float(result)
                elif isinstance(result, (tuple, list)) and result:
                    estimate = float(result[0])
                    if len(result) >= 3:
                        low, high = float(result[1]), float(result[2])
                if estimate:
                    break
        if not estimate:
            estimate, low, high = local_estimate_bytes(dataset, values)
        if not estimate:
            return "Estimated output size: nothing selected"
        if low and high:
            return f"Estimated output size: {human_bytes(estimate)} (range {human_bytes(low)} – {human_bytes(high)})"
        return f"Estimated output size: {human_bytes(estimate)}"


def _set_if_possible(obj, names, value) -> None:
    if value is None:
        return
    for name in names:
        if hasattr(obj, name):
            setattr(obj, name, value)
            return


def _wrap(layout) -> QWidget:
    w = QWidget()
    layout.setContentsMargins(0, 0, 0, 0)
    w.setLayout(layout)
    return w


def _label(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", "muted")
    return label
