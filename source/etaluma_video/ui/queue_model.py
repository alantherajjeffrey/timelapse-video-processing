"""The queue of experiment folders (0.8): items, the file that keeps them, the worker and the report.

Nothing here touches widgets. ``QueueController`` (``queue.py``) runs ``run_item`` on its own job
runner, one item at a time, and saves the list after every change, so a closed or crashed app
can resume where it stopped.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .. import APP_NAME, VERSION
from ..engine.jobs import check_cancel
from ..engine.userdata import atomic_write_json, user_data_dir
from . import presets_store

log = logging.getLogger("etaluma.ui")

PENDING, RUNNING, DONE, FAILED, SKIPPED, CANCELLED = "pending", "running", "done", "failed", "skipped", "cancelled"
FINISHED = (DONE, FAILED, SKIPPED, CANCELLED)
STATUS_LABELS = {PENDING: "Waiting", RUNNING: "Running", DONE: "Done", FAILED: "Failed", SKIPPED: "Skipped",
                 CANCELLED: "Cancelled"}
INCOMPLETE_SUFFIX = "_incomplete"


@dataclass
class QueueItem:
    folder: str
    display: str = presets_store.AUTO
    preset: str = "builtin:standard"
    display_snapshot: dict | None = None  # the Display panel when the item was added (display "current")
    preset_snapshot: dict | None = None  # the Output panel when the item was added (preset "current")
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = PENDING
    parent: str = ""  # the folder its run folder goes into, set when it starts
    output: str = ""  # the run folder, once done
    message: str = ""
    seconds: float = 0.0
    started_at: float = 0.0  # time.time() of the last start
    videos: int = 0

    @property
    def name(self) -> str:
        return Path(self.folder).name

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "QueueItem":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in dict(data).items() if k in known})


# ---- the file -------------------------------------------------------------------------------- #
def queue_path() -> Path:
    return user_data_dir() / "queue.json"


def load_queue(path: Path | str | None = None) -> list[QueueItem]:
    p = Path(path) if path else queue_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = []
    for entry in (data or {}).get("items", []) if isinstance(data, dict) else []:
        try:
            items.append(QueueItem.from_dict(entry))
        except (TypeError, ValueError) as exc:
            log.debug("Queue entry skipped: %s", exc)
    return items


def save_queue(items: list[QueueItem], path: Path | str | None = None) -> None:
    p = Path(path) if path else queue_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(p, {"schema": 1, "items": [i.to_dict() for i in items]})
    except OSError as exc:
        log.warning("The queue could not be saved: %s", exc)


# ---- folders and outputs --------------------------------------------------------------------- #
def expand_folders(folders) -> list[str]:
    """Experiment folders for what was added: a parent folder gives every experiment inside it."""
    from ..engine.parsing import find_experiments  # noqa: WPS433

    found: list[str] = []
    for folder in folders:
        path = Path(str(folder))
        if not str(folder) or not path.is_dir():
            continue
        try:
            experiments = find_experiments(path)
        except (OSError, ValueError) as exc:
            log.debug("find_experiments(%s): %s", path, exc)
            experiments = []
        found.extend(str(p) for p in (experiments or [path.resolve()]))
    unique, seen = [], set()
    for folder in found:
        if folder.casefold() not in seen:
            seen.add(folder.casefold())
            unique.append(folder)
    return unique


def output_parent(item: QueueItem, mode: str, folder: str) -> Path:
    """The experiment's analysis_output, or ``<common folder>/<experiment>``."""
    if mode == "folder" and str(folder or "").strip():
        return Path(folder) / item.name
    return Path(item.folder) / "analysis_output"


def _run_status(folder: Path) -> str:
    for meta in (folder / "info").glob("*_metadata.json"):
        try:
            return str(json.loads(meta.read_text(encoding="utf-8")).get("status", ""))
        except (OSError, ValueError):
            return ""
    return ""


def label_incomplete(parent: Path | str, since: float) -> list[Path]:
    """Rename run folders under ``parent`` that an unfinished item left behind to ``…_incomplete``."""
    parent = Path(parent) if parent else None
    renamed: list[Path] = []
    if parent is None or not parent.is_dir():
        return renamed
    for folder in sorted(parent.iterdir()):
        if (not folder.is_dir() or not folder.name.startswith(("run_", "quick_"))
                or folder.name.endswith(INCOMPLETE_SUFFIX)):
            continue
        try:
            if folder.stat().st_ctime < since - 2:
                continue
        except OSError:
            continue
        if _run_status(folder) == "complete":
            continue
        target = folder.with_name(folder.name + INCOMPLETE_SUFFIX)
        try:
            folder.rename(target)
            renamed.append(target)
        except OSError as exc:
            log.warning("%s could not be labelled incomplete: %s", folder, exc)
    return renamed


# ---- the worker ------------------------------------------------------------------------------ #
def run_item(item: dict, output: str, progress=None, cancel=None) -> dict:
    """Scan one folder, build its options and process it (runs in the queue's worker thread)."""
    from ..engine.parsing import scan_dataset  # noqa: WPS433
    from ..engine.process import process_dataset  # noqa: WPS433
    from .experiment_memory import load_record  # noqa: WPS433
    from .state import dataset_key  # noqa: WPS433

    say = progress or (lambda _m: None)
    entry = QueueItem.from_dict(item)
    say(f"Scanning {entry.name}")
    dataset = scan_dataset(entry.folder)
    check_cancel(cancel)
    profile, note = presets_store.resolve_display(entry.display, dataset, entry.display_snapshot)
    preset = presets_store.preset_values(entry.preset, entry.preset_snapshot)
    record = load_record(dataset_key(dataset)) or {}
    options = presets_store.options_for(dataset, preset, profile, record, quick=presets_store.is_quick(entry.preset))
    say(f"{entry.name}: display {note}, output preset {presets_store.preset_label(entry.preset)}")
    result = process_dataset(dataset, options, output, progress=progress, cancel=cancel)
    return {"output": str(result.get("output", "")), "seconds": float(result.get("processing_seconds") or 0.0),
            "videos": len(result.get("videos") or []), "warnings": list(result.get("warnings") or [])}


# ---- the report ------------------------------------------------------------------------------ #
def duration(seconds: float) -> str:
    """45 s, 3 min 05 s, 1 h 12 min."""
    seconds = max(0, int(round(seconds or 0)))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60:02d} s"
    return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"


def report_text(items: list[QueueItem], started: float, finished: float) -> str:
    def when(t: float) -> str:
        return _dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")

    counts = {s: sum(1 for i in items if i.status == s) for s in (*FINISHED, PENDING)}
    lines = [f"{APP_NAME} {VERSION}: queue report",
             f"Started {when(started)}, finished {when(finished)}",
             f"{counts[DONE]} done, {counts[FAILED]} failed, {counts[SKIPPED]} skipped, "
             f"{counts[CANCELLED]} cancelled, {counts[PENDING]} waiting", ""]
    for number, item in enumerate(items, start=1):
        took = f" in {duration(item.seconds)}" if item.seconds else ""
        lines.append(f"{number}. {item.name}: {STATUS_LABELS.get(item.status, item.status)}{took}")
        lines.append(f"   folder: {item.folder}")
        lines.append(f"   display: {presets_store.display_label(item.display)}; "
                     f"output preset: {presets_store.preset_label(item.preset)}")
        if item.output:
            lines.append(f"   output: {item.output}")
        if item.message:
            lines.append(f"   {item.message}")
    return "\n".join(lines) + "\n"


def write_report(items: list[QueueItem], started: float, finished: float) -> Path:
    folder = user_data_dir() / "queue_reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"queue_{_dt.datetime.fromtimestamp(finished):%Y%m%d_%H%M%S}.txt"
    path.write_text(report_text(items, started, finished), encoding="utf-8")
    return path
