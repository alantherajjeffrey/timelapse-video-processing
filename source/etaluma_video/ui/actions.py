"""Every user action of the application.

Widgets never call the engine; they call an :class:`Actions` method, which logs
``DEBUG etaluma.ui: <name>(<kwargs>)``, starts one job on the :class:`JobRunner`
and reports the outcome as an INFO line. Engine imports happen inside the worker
functions, so a half-finished engine port shows up as a log line and a dialog
instead of an import error at startup.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from ..engine.jobs import check_cancel
from .jobs_qt import EngineUnavailable, engine, engine_attr
from .settings import Settings

log = logging.getLogger("etaluma.ui")

JOB_LABELS = {
    "scan": "Scanning folder",
    "calibrate": "Reading calibration",
    "quick": "Quick video",
    "process": "Processing",
    "batch": "Processing batch",
}


# --------------------------------------------------------------------------- #
# Worker functions (run in the JobRunner thread; no Qt, no widgets)
# --------------------------------------------------------------------------- #


def scan_folder(path: str, progress=None, cancel=None) -> list:
    """Scan one experiment folder, or every experiment under a parent folder (batch)."""
    say = progress or (lambda _m: None)
    scan_dataset = engine_attr("scan_dataset")
    folder = Path(path)
    find_experiments = getattr(engine(), "find_experiments", None)
    candidates: list = []
    if find_experiments is not None:
        try:
            candidates = list(find_experiments(folder) or [])
        except Exception as exc:  # a parent folder is only a guess
            log.debug("find_experiments(%s) failed: %s", folder, exc)
            candidates = []
    if not candidates:
        candidates = [folder]
    datasets = []
    total = len(candidates)
    for index, candidate in enumerate(candidates, start=1):
        check_cancel(cancel)
        if hasattr(candidate, "frames"):  # already a Dataset
            datasets.append(candidate)
            continue
        target = Path(str(candidate))
        say(f"Scanning {target.name} ({index}/{total}) … {int(100 * (index - 1) / total)} %")
        datasets.append(scan_dataset(target))
    say(f"Scanned {len(datasets)} experiment{'s' if len(datasets) != 1 else ''} … 100 %")
    return datasets


def calibrate_dataset(dataset, progress=None, cancel=None) -> dict:
    """Calibration, overlays and the WHITE median of one dataset."""
    say = progress or (lambda _m: None)
    from ..engine import calibration as calibration_module  # noqa: WPS433 - lazy by design
    from ..engine.models import Calibration, Overlays  # noqa: WPS433

    check_cancel(cancel)
    say(f"Reading the scale bar of {getattr(dataset, 'name', '')} … 10 %")
    result = calibration_module.calibrate(dataset)
    calibration = overlays = None
    for item in result if isinstance(result, (tuple, list)) else (result,):
        if isinstance(item, Calibration):
            calibration = item
        elif isinstance(item, Overlays):
            overlays = item
    check_cancel(cancel)
    median = None
    try:
        first_white_median = getattr(engine(), "first_white_median", None)
        if first_white_median is not None:
            say("Measuring the WHITE median … 60 %")
            mask = None
            if overlays is not None and getattr(overlays, "image_shape", None):
                try:  # exclude the burned-in overlays from the median
                    mask = overlays.mask(overlays.image_shape)
                except Exception:
                    mask = None
            median = float(first_white_median(dataset, mask) if mask is not None else first_white_median(dataset))
    except Exception as exc:
        log.debug("first_white_median(%s) failed: %s", getattr(dataset, "name", dataset), exc)
    say("Calibration done … 100 %")
    return {"calibration": calibration, "overlays": overlays, "white_median": median}


def process_one(dataset, options, output, progress=None, cancel=None):
    """One experiment through the engine's process_dataset."""
    process_dataset = engine_attr("process_dataset")
    return process_dataset(dataset, options, output, progress=progress, cancel=cancel)


def process_many(items, progress=None, cancel=None) -> list:
    """A batch: the engine's process_batch when it exists, else process_dataset in sequence.

    ``items`` is a list of ``(dataset, options, output parent)``; the engine writes each run
    into the experiment's own ``analysis_output`` when no batch destination is given.
    """
    say = progress or (lambda _m: None)
    batch = getattr(engine(), "process_batch", None)
    if batch is not None:
        try:
            return batch([i[0] for i in items], [i[1] for i in items], None, progress, cancel)
        except TypeError as exc:
            log.debug("process_batch call shape rejected, falling back to a sequence: %s", exc)
    results = []
    total = len(items)
    for index, (dataset, options, output) in enumerate(items, start=1):
        check_cancel(cancel)
        name = getattr(dataset, "name", str(index))
        say(f"Processing {name} ({index}/{total}) … {int(100 * (index - 1) / total)} %")
        try:
            target = process_one(dataset, options, output, progress=progress, cancel=cancel)
            results.append({"experiment": name, "status": "complete", "output": str(target)})
        except Exception as exc:
            if "cancel" in type(exc).__name__.lower():
                raise
            log.error("%s failed: %s", name, exc)
            results.append({"experiment": name, "status": "failed", "error": str(exc)})
    say(f"Batch finished ({len(results)} experiments) … 100 %")
    return results


def output_parent(dataset, base: Path | str | None = None) -> Path:
    """The folder the engine creates ``run_<date>_<id>`` / ``quick_<date>_<id>`` inside.

    The engine owns the run-folder name, so the UI only chooses the parent: the experiment's
    ``analysis_output`` unless the Output panel points somewhere else.
    """
    if base:
        folder = Path(base)
        return folder.parent if folder.name.startswith(("run_", "quick_")) else folder
    return Path(getattr(dataset, "root", ".")) / "analysis_output"


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #


class Actions(QObject):
    """The one place where the UI talks to the engine."""

    def __init__(self, ctx, runner, settings: Settings, window=None, output_panel=None) -> None:
        super().__init__(window)
        self.ctx = ctx
        self.runner = runner
        self.settings = settings
        self.window = window
        self.output_panel = output_panel
        self.queue = None  # QueueController, set by the window (0.8)
        runner.started.connect(self._on_started)
        runner.progress.connect(self._on_progress)
        runner.percent.connect(self._on_percent)
        runner.finished.connect(self._on_finished)
        runner.failed.connect(self._on_failed)
        runner.cancelled.connect(self._on_cancelled)

    # ---- user actions ---------------------------------------------------- #
    def open_folder(self, path: str | Path | None = None) -> bool:
        log.debug("open_folder(path=%r)", str(path) if path else None)
        if path is None:
            start = self.settings.last_folder or str(Path.home())
            chosen = QFileDialog.getExistingDirectory(self.window, "Open an experiment or a parent folder", start)
            if not chosen:
                log.info("Open folder cancelled.")
                return False
            path = chosen
        folder = Path(path)
        if not folder.is_dir():
            self._error("Open folder", f"{folder} is not a folder.")
            return False
        self.settings.last_folder = str(folder)
        self.settings.remember_folder(str(folder))
        if not self._start("scan", scan_folder, str(folder)):
            return False
        log.info("Opening %s", folder)
        return True

    def quick_video(self) -> bool:
        log.debug("quick_video(dataset=%r)", self._active_name())
        dataset = self._require_dataset()
        if dataset is None:
            return False
        from . import presets_store  # noqa: WPS433

        display_key = self.settings.quick_display or presets_store.AUTO
        preset_key = self.settings.quick_preset or presets_store.QUICK
        if self.queue is not None and self.queue.running:
            return self._add_to_queue([dataset], display_key, preset_key, "Quick video")
        try:
            output = output_parent(dataset)
            options, note = self.preset_options(dataset, display_key, preset_key)
        except (EngineUnavailable, ValueError) as exc:
            self._error("Quick video", str(exc))
            return False
        self._log_estimate(dataset, options, prefix="Quick video")
        log.info("Quick video: display %s, output preset %s → a new %s folder in %s", note,
                 presets_store.preset_label(preset_key), "quick_" if options.quick else "run_", output)
        return self._start("quick", process_one, dataset, options, str(output))

    def preset_options(self, dataset, display_key: str, preset_key: str):
        """(Options, display note) for the open experiment from a display choice and an output preset (0.8)."""
        from . import presets_store  # noqa: WPS433

        memory = getattr(self.window, "experiment_memory", None)
        if memory is not None:
            memory.flush()  # "Remembered" reads what was just changed
        profile = getattr(self.ctx, "profile", None)
        profile, note = presets_store.resolve_display(display_key, dataset,
                                                      profile.to_dict() if profile is not None else None)
        panel = self.output_panel
        preset = presets_store.preset_values(preset_key, panel.preset_snapshot() if panel is not None else None)
        experiment = panel.experiment_record() if panel is not None else {}
        options = presets_store.options_for(dataset, preset, profile, experiment, quick=presets_store.is_quick(preset_key),
                                            calibration=getattr(self.ctx, "calibration", None))
        return options, note

    def process(self) -> bool:
        datasets = list(getattr(self.ctx, "datasets", []) or [])
        log.debug("process(datasets=%d, mode=%r)", len(datasets), getattr(self.ctx, "mode", ""))
        if not datasets:
            self._error("Process", "Open an experiment folder first.")
            return False
        if self.queue is not None and self.queue.running:
            from . import presets_store  # noqa: WPS433

            panel = self.output_panel
            preset_key = presets_store.CURRENT if panel is None or panel.preset_modified else panel.preset_key
            return self._add_to_queue(datasets, presets_store.CURRENT, preset_key, "Process")
        try:
            items = []
            for dataset in datasets:
                base = self.output_panel.output_folder(dataset) if self.output_panel is not None else None
                output = output_parent(dataset, base)
                items.append((dataset, self._options(dataset, output, quick=False), str(output)))
        except EngineUnavailable as exc:
            self._error("Process", str(exc))
            return False
        if len(items) == 1:
            dataset, options, output = items[0]
            self._log_estimate(dataset, options, prefix="Process")
            log.info("Processing %s → a new run_ folder in %s", getattr(dataset, "name", ""), output)
            return self._start("process", process_one, dataset, options, output)
        self._log_estimate(self.ctx.active, items[0][1], prefix="Process (first experiment)")
        log.info("Processing %d experiments in sequence", len(items))
        return self._start("batch", process_many, items)

    def _add_to_queue(self, datasets, display_key: str, preset_key: str, what: str) -> bool:
        """While the queue runs, Quick video and Process add the open experiment(s) to it (0.8)."""
        from . import presets_store  # noqa: WPS433

        memory = getattr(self.window, "experiment_memory", None)
        if memory is not None:
            memory.flush()  # the queue reads each experiment's remembered settings
        profile = getattr(self.ctx, "profile", None)
        added = self.queue.add_folders(
            [str(getattr(d, "root", "")) for d in datasets], display=display_key, preset=preset_key,
            display_snapshot=profile.to_dict() if profile is not None else None,
            preset_snapshot=self.output_panel.preset_snapshot() if self.output_panel is not None else None)
        if added:
            log.info("%s: the queue is running, so %s joined it", what, ", ".join(i.name for i in added))
        else:
            log.info("%s: already waiting in the queue with the same choices", what)
        return bool(added)

    def cancel(self) -> bool:
        log.debug("cancel()")
        if not self.runner.cancel():
            log.info("Nothing to cancel.")
            return False
        return True

    def set_mode(self, mode: str) -> None:
        mode = "fixed" if str(mode).lower().startswith("fix") else "timelapse"
        if getattr(self.ctx, "mode", None) == mode:
            return
        log.debug("set_mode(mode=%r)", mode)
        self.ctx.set_mode(mode)
        log.info("Mode: %s", "fixed images" if mode == "fixed" else "timelapse")

    def open_results_folder(self) -> bool:
        log.debug("open_results_folder(dataset=%r)", self._active_name())
        dataset = self._require_dataset()
        if dataset is None:
            return False
        folder = Path(getattr(dataset, "root", ".")) / "analysis_output"
        if not folder.is_dir():
            log.info("No analysis_output folder yet; opening %s", dataset.root)
            folder = Path(dataset.root)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        log.info("Opened %s", folder)
        return True

    def refresh_results(self) -> list:
        from .results_dock import find_runs  # noqa: WPS433 - avoids an import cycle at module load

        dataset = getattr(self.ctx, "active", None)
        folders = find_runs(getattr(dataset, "root", "")) if dataset is not None else []
        self.ctx.set_results(folders)
        log.debug("refresh_results(runs=%d)", len(folders))
        return folders

    def show_settings(self) -> bool:
        log.debug("show_settings()")
        from .settings_dialog import SettingsDialog  # noqa: WPS433 - lazy, keeps import graph flat

        dialog = SettingsDialog(self.settings, self.window)
        if dialog.exec() != dialog.DialogCode.Accepted:
            log.info("Settings unchanged.")
            return False
        self.settings = dialog.result_settings()
        self.settings.save()
        self.set_theme(self.settings.theme)
        if self.output_panel is not None:
            self.output_panel.apply_settings(self.settings)
        if self.window is not None and hasattr(self.window, "apply_settings"):
            self.window.apply_settings(self.settings)
        log.info("Settings saved to %s", self.settings.save())
        return True

    def set_theme(self, theme: str | None = None) -> str:
        from .theme import DEFAULT_THEME, apply_theme  # noqa: WPS433

        name = theme or self.settings.theme or DEFAULT_THEME
        log.debug("set_theme(theme=%r)", name)
        app = QApplication.instance()
        if app is not None:
            name = apply_theme(app, name)
        self.settings.theme = name
        self.ctx.theme_changed.emit(name)
        log.info("Theme: %s", name)
        return name

    # ---- job plumbing ---------------------------------------------------- #
    def _start(self, kind: str, fn, *args, **kwargs) -> bool:
        if self.runner.busy:
            self._error("Busy", "Another job is running. Wait for it or cancel it first.")
            return False
        try:
            self.runner.start(kind, fn, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - start must never take the window down
            self._error(JOB_LABELS.get(kind, kind), str(exc))
            return False
        return True

    def _on_started(self, kind: str) -> None:
        self.ctx.set_job("running", f"{JOB_LABELS.get(kind, kind)}…")

    def _on_progress(self, message: str) -> None:
        self.ctx.set_job("running", message)

    def _on_percent(self, percent: int) -> None:
        self.ctx.job_progress.emit(int(percent))

    def _on_finished(self, kind: str, result) -> None:
        if kind == "scan":
            self._after_scan(result)
        elif kind == "calibrate":
            self._after_calibration(result)
        elif kind in ("quick", "process", "batch"):
            self._after_processing(kind, result)
        else:
            self.ctx.set_job("finished", f"{JOB_LABELS.get(kind, kind)} finished")

    def _on_failed(self, kind: str, message: str) -> None:
        label = JOB_LABELS.get(kind, kind)
        log.error("%s failed: %s", label, message)
        self.ctx.set_job("failed", f"{label} failed: {message}")
        self._error(label, message)

    def _on_cancelled(self, kind: str) -> None:
        label = JOB_LABELS.get(kind, kind)
        log.warning("%s cancelled.", label)
        self.ctx.set_job("cancelled", f"{label} cancelled")

    # ---- job results ----------------------------------------------------- #
    def _after_scan(self, datasets) -> None:
        datasets = list(datasets or [])
        self.ctx.set_datasets(datasets)
        for dataset in datasets:
            rois = sorted({getattr(f, "roi", "") for f in getattr(dataset, "frames", [])})
            channels = list(getattr(dataset, "channels", []) or [])
            timepoints = int(getattr(dataset, "n_timepoints", 0) or 0)
            log.info(
                'scan_dataset("%s") → %d positions, %d channels, %d timepoints, mode %s',
                getattr(dataset, "name", ""), len(rois), len(channels), timepoints, getattr(dataset, "mode", "?"),
            )
            from ..engine.parsing import summarize_warnings  # noqa: WPS433

            for warning in summarize_warnings(list(getattr(dataset, "warnings", []) or [])):
                log.warning("%s: %s", getattr(dataset, "name", ""), warning)
        self.ctx.set_job("finished", f"{len(datasets)} experiment{'s' if len(datasets) != 1 else ''} scanned")
        self.refresh_results()
        if datasets:
            QTimer.singleShot(0, self._start_calibration)

    def _start_calibration(self) -> None:
        dataset = getattr(self.ctx, "active", None)
        if dataset is None or self.runner.busy:
            return
        self._start("calibrate", calibrate_dataset, dataset)

    def _after_calibration(self, result) -> None:
        result = result or {}
        calibration, overlays = result.get("calibration"), result.get("overlays")
        if calibration is not None or overlays is not None:
            from ..engine.models import Calibration, Overlays  # noqa: WPS433

            self.ctx.set_calibration(calibration or Calibration(), overlays or Overlays())
        message = getattr(calibration, "message", "") or "no scale bar found"
        if getattr(calibration, "disagreement", False):
            log.warning("Calibration: %s (the burned-in bar wins over the objective table)", message)
        else:
            log.info("Calibration: %s", message)
        self._apply_white_preset(result.get("white_median"))
        self.ctx.set_job("finished", "Calibration read")

    def _apply_white_preset(self, median) -> None:
        """Store the WHITE median and pick the preset when the profile asks for auto-detection."""
        if median is None:
            return
        from .calibration_card import apply_white_median  # noqa: WPS433
        from .state import dataset_key  # noqa: WPS433

        active = getattr(self.ctx, "active", None)
        if active is not None:
            self.ctx.white_medians[dataset_key(active)] = float(median)
        try:
            apply_white_median(self.ctx, float(median))
        except Exception as exc:
            log.debug("WHITE preset could not be applied: %s", exc)

    def _after_processing(self, kind: str, result) -> None:
        label = JOB_LABELS.get(kind, kind)
        if isinstance(result, list):  # process_batch: one dict per experiment
            done = sum(1 for r in result if isinstance(r, dict) and r.get("status") == "complete")
            log.info("%s finished: %d of %d experiments complete", label, done, len(result))
            for entry in result:
                if isinstance(entry, dict) and entry.get("status") != "complete":
                    log.warning("%s: %s — %s", entry.get("experiment", "?"), entry.get("status"), entry.get("error", ""))
        elif isinstance(result, dict):  # process_dataset summary
            seconds = result.get("processing_seconds")
            videos = result.get("videos")
            log.info(
                "%s finished (%s) → %s%s%s",
                label,
                result.get("status", "complete"),
                result.get("output", "?"),
                f", {len(videos)} videos" if isinstance(videos, list) else "",
                f", {float(seconds):.1f} s" if isinstance(seconds, (int, float)) else "",
            )
        elif result:
            log.info("%s finished → %s", label, result)
        else:
            log.info("%s finished", label)
        self.ctx.set_job("finished", f"{label} finished")
        self.refresh_results()

    # ---- helpers --------------------------------------------------------- #
    def _options(self, dataset, output, quick: bool):
        if self.output_panel is not None:
            return self.output_panel.options_for(dataset, output=output, quick=quick)
        from .output_panel import OutputPanel  # noqa: WPS433 - headless fallback

        panel = OutputPanel(self.ctx, settings=self.settings)
        try:
            return panel.options_for(dataset, output=output, quick=quick)
        finally:
            panel.deleteLater()

    def _log_estimate(self, dataset, options=None, prefix: str = "Process") -> None:
        if dataset is None or self.output_panel is None:
            return
        try:
            log.info("%s: %s", prefix, self.output_panel.estimate_text(dataset, options))
        except Exception as exc:  # an estimate must never stop a run
            log.debug("Output size estimate failed: %s", exc)

    def _require_dataset(self):
        dataset = getattr(self.ctx, "active", None)
        if dataset is None:
            self._error("No experiment open", "Open an experiment folder first.")
            return None
        return dataset

    def _active_name(self) -> str:
        return str(getattr(getattr(self.ctx, "active", None), "name", ""))

    def _error(self, title: str, text: str) -> None:
        log.error("%s: %s", title, text)
        if self.window is not None and hasattr(self.window, "show_error"):
            self.window.show_error(title, text)
            return
        box = QMessageBox(QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.Ok, self.window)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        box.open()  # never modal: a blocking dialog would freeze headless runs
        self._last_error_box = box  # keep a reference until Qt deletes it
