"""Per-experiment memory (0.7): reopening an experiment restores its display and output choices.

One small JSON file per experiment folder in ``<user data>/experiments/`` holds the display
profile, the channel names, the start time and days mode of the time label, and the timepoint
range. It is written a moment after the last change and when the window closes, and read back
when the experiment becomes active again; it takes precedence over the last manual profile.

Each change is captured at once, while it still describes the experiment it was made in: when
another experiment opens, the panels reset before this object hears about it, so saving the
panels' state at that point would store the new experiment's defaults under the old name.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QTimer

from ..engine.models import DisplayProfile
from .settings import user_data_dir
from .state import dataset_key

log = logging.getLogger("etaluma.ui")


def record_path(root: str) -> Path:
    digest = hashlib.sha1(str(root).casefold().encode("utf-8")).hexdigest()[:16]
    return user_data_dir() / "experiments" / f"{digest}.json"


def load_record(root: str) -> dict | None:
    path = record_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and str(data.get("root", "")).casefold() == str(root).casefold() else None


def save_record(root: str, record: dict) -> Path:
    path = record_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.stem}_{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return path


class ExperimentMemory(QObject):
    def __init__(self, ctx, display_panel, output_panel, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.ctx, self.display_panel, self.output_panel = ctx, display_panel, output_panel
        self._root: str | None = None
        self._pending: dict | None = None
        self._applying = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(800)
        self._timer.timeout.connect(self._write_pending)
        ctx.active_dataset_changed.connect(self._on_dataset)
        ctx.profile_changed.connect(self._capture)
        output_panel.changed.connect(self._capture)

    # ---- capture and write ------------------------------------------------------- #
    def _current(self) -> bool:
        active = getattr(self.ctx, "active", None)
        return self._root is not None and active is not None and dataset_key(active) == self._root

    def _snapshot(self) -> dict:
        return {"root": self._root, "saved": _dt.datetime.now().isoformat(timespec="minutes"),
                "profile": self.ctx.profile.to_dict(), **self.output_panel.experiment_record()}

    def _capture(self, *_args) -> None:
        if self._applying or not self._current():
            return  # restoring, or another experiment is being set up
        self._pending = self._snapshot()
        self._timer.start()

    def _write_pending(self) -> None:
        record, self._pending = self._pending, None
        if record is None:
            return
        try:
            save_record(record["root"], record)
        except OSError as exc:
            log.debug("Experiment settings could not be saved: %s", exc)

    def flush(self) -> None:
        """Write now (window closing, tests)."""
        self._timer.stop()
        if self._current() and not self._applying:
            self._pending = self._snapshot()
        self._write_pending()

    # ---- restore ------------------------------------------------------------------------ #
    def _on_dataset(self, dataset) -> None:
        self._timer.stop()
        self._write_pending()  # the last change made in the experiment being left
        self._root = None
        if dataset is not None:  # after every other handler has set the experiment up
            QTimer.singleShot(0, lambda ds=dataset: self._restore(ds))

    def _restore(self, dataset) -> None:
        if dataset is not self.ctx.active:
            return
        root = dataset_key(dataset)
        record = load_record(root)
        self._applying = True
        try:
            if record:
                if isinstance(record.get("profile"), dict):
                    self.display_panel.apply_profile(DisplayProfile.from_dict(record["profile"]))
                self.output_panel.apply_experiment(record)
        except Exception as exc:  # a stale or damaged record must not stop the experiment from opening
            log.warning("The settings saved for this experiment could not be restored: %s", exc)
            record = None
        finally:
            self._applying = False
            self._root = root
        if record:
            log.info("Restored the display and output settings last used for %s (saved %s)",
                     getattr(dataset, "name", root), record.get("saved", "earlier"))

    # kept for callers of 0.7 development builds
    save_now = flush
