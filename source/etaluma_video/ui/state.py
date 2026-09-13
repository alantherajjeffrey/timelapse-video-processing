"""Application state and signal bus shared by every widget.

Widgets never call each other; they read/write AppContext and react to its signals.
Only the GUI thread touches AppContext. Worker threads deliver results through
JobRunner signals (ui/jobs_qt.py) or ui/workers.py relays, which run on the GUI thread.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ..engine.models import Bounds, Calibration, DisplayProfile, HistogramSet, Overlays


def dataset_key(dataset) -> str:
    """Stable key of a scanned dataset (its root folder)."""
    return str(getattr(dataset, "root", "")) if dataset is not None else ""


class AppContext(QObject):
    # dataset lifecycle
    datasets_changed = Signal(list)  # list[Dataset]; empty list = nothing open
    active_dataset_changed = Signal(object)  # Dataset | None
    position_changed = Signal(str)  # roi key of the active dataset ("" = none)
    mode_changed = Signal(str)  # "timelapse" | "fixed"
    # display
    profile_changed = Signal(object)  # DisplayProfile (the live one)
    histograms_changed = Signal(object)  # HistogramSet | None for the active dataset (partial or complete)
    frame_histograms_changed = Signal(object)  # {channel: 256 counts} of the frame shown in the preview
    coverage_changed = Signal(int, int)  # positions covered by the Auto histograms, total positions
    compute_all_requested = Signal()  # display panel asks the preview controller for every position
    calibration_changed = Signal(object, object)  # Calibration, Overlays
    # jobs and log
    job_state_changed = Signal(str, str)  # status: idle|running|finished|failed|cancelled, message
    job_progress = Signal(int)  # percent, -1 = indeterminate
    log_line = Signal(str, str)  # level name, text
    results_changed = Signal(list)  # list[Path] of run folders for the active dataset
    theme_changed = Signal(str)  # "dark" | "light"
    time_format_changed = Signal(float, str)  # start offset in seconds, days mode (0.6)
    channel_names_changed = Signal(object)  # {channel: name} typed in the Output panel (0.7)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.datasets: list = []
        self.active = None  # Dataset | None
        self.position: str = ""
        self.mode: str = "timelapse"
        self.profile: DisplayProfile = DisplayProfile()
        self.histograms: HistogramSet | None = None
        self.auto_bounds: Bounds | None = None
        self.calibration: Calibration = Calibration()
        self.overlays: Overlays = Overlays()
        #: Calibration read per dataset key, so switching experiments in a batch does not re-read it.
        self.calibrations: dict[str, tuple[Calibration, Overlays]] = {}
        #: Median of the first WHITE plane per dataset key (picks the WHITE preset).
        self.white_medians: dict[str, float] = {}
        self.job_status: str = "idle"
        self.results: list = []
        #: Start offset and days mode of the time label, set by the Output panel, shown by the preview.
        self.time_offset: float = 0.0
        self.time_days: str = "auto"
        self.channel_names: dict[str, str] = {}

    # ---- setters that emit ---------------------------------------------- #
    def set_datasets(self, datasets: list) -> None:
        self.datasets = list(datasets)
        self.datasets_changed.emit(self.datasets)
        self.set_active(self.datasets[0] if self.datasets else None)

    def set_active(self, dataset) -> None:
        self.active = dataset
        cached = self.calibrations.get(dataset_key(dataset)) if dataset is not None else None
        self.calibration, self.overlays = cached if cached else (Calibration(), Overlays())
        self.histograms = None
        self.auto_bounds = None
        self.active_dataset_changed.emit(dataset)
        self.calibration_changed.emit(self.calibration, self.overlays)
        self.histograms_changed.emit(None)
        if dataset is not None:
            self.set_mode(getattr(dataset, "mode", "timelapse"))
            rois = sorted({f.roi for f in dataset.frames})
            self.set_position(rois[0] if rois else "")
        else:
            self.set_position("")

    def set_position(self, roi: str) -> None:
        self.position = roi
        self.position_changed.emit(roi)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.mode_changed.emit(mode)

    def set_profile(self, profile: DisplayProfile) -> None:
        self.profile = profile
        self.profile_changed.emit(profile)

    def set_histograms(self, hists: HistogramSet | None, auto_bounds: Bounds | None) -> None:
        self.histograms = hists
        self.auto_bounds = auto_bounds
        self.histograms_changed.emit(hists)

    def set_calibration(self, calibration: Calibration, overlays: Overlays, dataset=None) -> None:
        """Store the calibration for ``dataset`` (default: the active one) and publish it if active."""
        target = dataset if dataset is not None else self.active
        if target is not None:
            self.calibrations[dataset_key(target)] = (calibration, overlays)
        if target is None or target is self.active:
            self.calibration = calibration
            self.overlays = overlays
            self.calibration_changed.emit(calibration, overlays)

    def set_job(self, status: str, message: str = "") -> None:
        self.job_status = status
        self.job_state_changed.emit(status, message)

    def set_results(self, folders: list) -> None:
        self.results = list(folders)
        self.results_changed.emit(self.results)

    def set_channel_names(self, names: dict) -> None:
        names = {str(k): str(v) for k, v in (names or {}).items() if str(v).strip()}
        if names != self.channel_names:
            self.channel_names = names
            self.channel_names_changed.emit(dict(names))

    def set_time_format(self, offset_seconds: float, days: str) -> None:
        if (float(offset_seconds), str(days)) == (self.time_offset, self.time_days):
            return
        self.time_offset, self.time_days = float(offset_seconds), str(days)
        self.time_format_changed.emit(self.time_offset, self.time_days)

    @property
    def busy(self) -> bool:
        return self.job_status == "running"
