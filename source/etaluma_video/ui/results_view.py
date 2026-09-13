"""Results page: every video and image of a run, in large tiles by position and type.

Replaces 0.4's results dock. Each row is a position; each column a kind of output (composite,
fluorescence only, each channel, montage, panel). Whole-run images (dashboard, plate overview)
sit in their own row at the bottom. 0.6: tiles show the small poster JPEG Process writes (0.5
decoded half of every video to find its middle frame), the grid is built once per run instead of
on every refresh, montages are hidden until their chip is ticked, and clicking a position in the
explorer scrolls to its row. Filter chips hide columns, the tile size is adjustable, and a
click plays the video (or shows the image) large; "All results" returns to the grid.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .results_dock import VideoPlayer, describe_run, find_runs, run_metadata
from .viewer import CHIP_QSS, rgb_to_qimage
from .workers import run_in_background

log = logging.getLogger("etaluma.ui")

VIDEO_SUFFIXES = (".mp4", ".avi")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
COLUMN_ORDER = ["composite", "fluorescence", "WHITE", "F1", "F2", "F3", "montage", "panel"]
COLUMN_LABELS = {"composite": "Composite", "fluorescence": "Fluorescence only",
                 "montage": "Montage", "panel": "Panel"}
TILE_SIZES = {"Large": 420, "Medium": 300, "Small": 200}
OVERVIEW = "overview"


def natural_key(text: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def label_for(kind: str) -> str:
    return COLUMN_LABELS.get(kind, kind)


@dataclass
class ResultItem:
    roi: str
    kind: str
    files: list[Path] = field(default_factory=list)
    poster: Path | None = None  # small JPEG written by Process (0.6)

    @property
    def is_video(self) -> bool:
        return any(p.suffix.lower() in VIDEO_SUFFIXES for p in self.files)

    @property
    def primary(self) -> Path:
        """What to show: the MP4 when there is one (smaller, seeks well), else the AVI, else the image."""
        for suffix in (".mp4", ".avi", *IMAGE_SUFFIXES):
            for p in self.files:
                if p.suffix.lower() == suffix:
                    return p
        return self.files[0]

    def formats(self) -> str:
        return " · ".join(sorted({p.suffix.lower().lstrip(".").upper() for p in self.files}))


KIND_ALIASES = {"composite_fluorescence_only": "fluorescence", "fluorescence_only": "fluorescence"}
#: File-name endings of the results, longest first so "composite_fluorescence_only" wins over "composite".
KINDS = ("composite_fluorescence_only", "fluorescence", "composite", "WHITE", "F1", "F2", "F3", "montage", "panel")
_POSITION_TAIL = re.compile(r"(?:^|_)((?:ROI-|Well)[A-Za-z0-9]+)$", re.I)
_POSTERS: dict[tuple, np.ndarray] = {}


def _split(stem: str, positions=()) -> tuple[str, str] | None:
    """(position, kind) from '<experiment>_<position>_<kind>' (0.6 on) or '<position>_<kind>' (0.4, 0.5).

    ``positions`` are the run's position names (from its metadata), longest first, so any name
    works, underscores included (0.7); without them, ROI- and Well names are recognised.
    """
    for kind in KINDS:
        if not stem.endswith("_" + kind):
            continue
        head = stem[: -len(kind) - 1]
        for position in positions:
            if head == position or head.endswith("_" + position):
                return position, KIND_ALIASES.get(kind, kind)
        m = _POSITION_TAIL.search(head)
        return (m.group(1), KIND_ALIASES.get(kind, kind)) if m else None
    return None


def _overview_label(stem: str) -> str:
    if "plate_overview" in stem:
        return "Plate overview"
    m = re.search(r"condition_(\d+)", stem)
    if m:
        return f"Condition {int(m.group(1))}"
    return stem.replace("_", " ").capitalize()


def collect(folder: Path) -> tuple[dict[tuple[str, str], ResultItem], list[ResultItem]]:
    """Group a run folder's files into (position, kind) items plus whole-run overview images.

    Reads the 0.6 layout (videos at the top named ``<experiment>_<position>_<kind>``, montages in
    ``montages/``, dashboard and posters in ``info/``) and the 0.4/0.5 layout (``videos/``,
    ``composites/``, ``panels/``, ``<position>_<kind>``).
    """
    folder = Path(folder)
    items: dict[tuple[str, str], ResultItem] = {}
    overview: list[ResultItem] = []

    def add(roi: str, kind: str, path: Path, poster_path: Path | None = None) -> None:
        item = items.setdefault((roi, kind), ResultItem(roi, kind))
        if path not in item.files:
            item.files.append(path)
        if poster_path is not None and item.poster is None:
            item.poster = poster_path

    meta = run_metadata(folder)
    names = {str(row.get("ROI")) for row in (meta.get("dataset") or {}).get("inventory") or [] if row.get("ROI")}
    names |= {str(v.get("roi")) for v in meta.get("videos") or [] if v.get("roi")}
    positions = sorted(names, key=len, reverse=True)
    seen: set[Path] = set()
    for record in meta.get("videos") or []:
        name = record.get("file")
        if not name:
            continue
        path = folder / name if (folder / name).is_file() else folder / "videos" / name
        if not path.is_file():
            continue
        split = ((record["roi"], KIND_ALIASES.get(record["kind"], record["kind"]))
                 if record.get("roi") and record.get("kind") else _split(path.stem, positions))
        if split is None:
            continue
        poster_path = folder / record["poster"] if record.get("poster") else None
        add(*split, path, poster_path if poster_path is not None and poster_path.is_file() else None)
        seen.add(path)
    for base in (folder, folder / "videos"):
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir()):
            if path in seen or path.suffix.lower() not in VIDEO_SUFFIXES:
                continue
            split = _split(path.stem, positions)
            if split is not None:
                add(*split, path)
    for base, default_kind in ((folder, None), (folder / "montages", "montage"),
                               (folder / "composites", None), (folder / "panels", "panel")):
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir()):
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            split = _split(path.stem, positions)
            if split is None:
                if base != folder:
                    overview.append(ResultItem(OVERVIEW, _overview_label(path.stem), [path]))
                continue
            roi, kind = split
            add(roi, default_kind or kind, path)
    for base in (folder / "info", folder):
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*_dashboard.png")):
            overview.append(ResultItem(OVERVIEW, "Dashboard", [path]))
        for path in sorted(base.glob("*_info_bar.png")):
            overview.append(ResultItem(OVERVIEW, "Info bar", [path]))
    return items, overview


def _run_signature(folder: Path) -> tuple:
    """Changes when files appear in the run folder; an unchanged run keeps its tiles."""
    parts: list = [str(folder)]
    for sub in (folder, folder / "info", folder / "montages"):
        try:
            parts.append((sub.stat().st_mtime_ns, sum(1 for _ in sub.iterdir())))
        except OSError:
            parts.append(None)
    return tuple(parts)


def _poster_key(path: Path, size: int) -> tuple:
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size, int(size))
    except OSError:
        return (str(path), 0, 0, int(size))


def _remember_poster(key: tuple, rgb: np.ndarray) -> None:
    if len(_POSTERS) > 300:
        _POSTERS.clear()
    _POSTERS[key] = rgb


def poster(path: Path, size: int) -> np.ndarray:
    """RGB thumbnail: the poster JPEG Process wrote, an image, or the first frame of a video.

    0.5 seeked every video to its middle frame, which decodes half of a 190 MB file per tile.
    """
    import cv2

    if path.suffix.lower() in VIDEO_SUFFIXES:
        from ..engine.export import open_capture  # noqa: WPS433

        cap = open_capture(path)
        try:
            ok, frame = cap.read()
        finally:
            cap.release()
        if not ok or frame is None:
            raise ValueError(f"{path.name}: no frame")
        rgb = frame[:, :, ::-1]
    else:
        from PIL import Image

        with Image.open(path) as img:
            img.draft("RGB", (size, size))  # JPEG: decode at a reduced scale
            img = img.convert("RGB")
            img.thumbnail((size, size))
            rgb = np.asarray(img)
    h, w = rgb.shape[:2]
    scale = size / float(max(h, w))
    if scale < 1:
        rgb = cv2.resize(np.ascontiguousarray(rgb), (max(1, int(w * scale)), max(1, int(h * scale))),
                         interpolation=cv2.INTER_AREA)
    return np.ascontiguousarray(rgb)


class ResultTile(QToolButton):
    def __init__(self, item: ResultItem, size: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.item = item
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.setIconSize(QSize(size, size))
        self.setFixedWidth(size + 16)
        title = f"{item.roi} · {label_for(item.kind)}" if item.roi != OVERVIEW else label_for(item.kind)
        self.setText(f"{title}\n{item.formats()}")
        self.setToolTip("\n".join(str(p) for p in item.files) + ("\nClick to play" if item.is_video else "\nClick to view"))
        self.setStyleSheet("QToolButton { border: 1px solid palette(mid); border-radius: 8px; padding: 6px; }"
                           "QToolButton:hover { border-color: palette(highlight); }")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_poster(self, rgb: np.ndarray) -> None:
        self.setIcon(QIcon(QPixmap.fromImage(rgb_to_qimage(rgb))))


class ResultsView(QWidget):
    """``ResultsView(ctx)``: the results page of the central area."""

    back_requested = Signal()

    def __init__(self, ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._runs: list[Path] = []
        self._items: dict[tuple[str, str], ResultItem] = {}
        self._overview: list[ResultItem] = []
        self._hidden: set[str] = {"montage"}  # 0.6: montages off by default; their chip brings them back
        self._tiles: list[ResultTile] = []
        self._poster_tasks: list = []
        self._folder: Path | None = None
        self._signature: tuple | None = None
        self._row_labels: dict[str, QLabel] = {}

        title = QLabel("Results")
        title.setProperty("role", "heading")
        self.run_combo = QComboBox()
        self.run_combo.setMinimumWidth(360)
        self.run_combo.currentIndexChanged.connect(self._on_run)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        self.open_button = QPushButton("Open folder")
        self.open_button.clicked.connect(self._open_folder)
        self.size_combo = QComboBox()
        self.size_combo.addItems(list(TILE_SIZES))
        self.size_combo.setCurrentText("Medium")
        self.size_combo.currentTextChanged.connect(lambda _t: self._rebuild_grid())
        self.back_button = QPushButton("Back to preview")
        self.back_button.clicked.connect(self.back_requested.emit)
        header = QHBoxLayout()
        header.addWidget(title)
        header.addSpacing(12)
        header.addWidget(self.run_combo, 1)
        header.addWidget(self.refresh_button)
        header.addWidget(self.open_button)
        header.addWidget(QLabel("Tiles"))
        header.addWidget(self.size_combo)
        header.addWidget(self.back_button)

        self.info = QLabel("")
        self.info.setProperty("role", "muted")
        self.chip_bar = QWidget()
        self.chip_layout = QHBoxLayout(self.chip_bar)
        self.chip_layout.setContentsMargins(0, 0, 0, 0)
        self.chip_layout.setSpacing(4)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(10)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.grid_host)
        self.scroll.setWidgetResizable(True)
        self.empty = QLabel("Nothing here yet. Run Quick video or Process, then choose the run above.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setProperty("role", "muted")

        self.player = VideoPlayer(self)
        self.player_title = QLabel("")
        self.player_title.setProperty("role", "heading")
        self.all_button = QPushButton("All results")
        self.all_button.clicked.connect(self.show_grid)
        self.player_folder = QPushButton("Show file in folder")
        self.player_folder.clicked.connect(self._open_file_folder)
        player_bar = QHBoxLayout()
        player_bar.addWidget(self.all_button)
        player_bar.addWidget(self.player_title, 1)
        player_bar.addWidget(self.player_folder)
        player_page = QWidget()
        pl = QVBoxLayout(player_page)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.addLayout(player_bar)
        pl.addWidget(self.player, 1)
        self._player_item: ResultItem | None = None

        self.pages = QStackedWidget()
        self.pages.addWidget(self.empty)
        self.pages.addWidget(self.scroll)
        self.pages.addWidget(player_page)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addWidget(self.info)
        layout.addWidget(self.chip_bar)
        layout.addWidget(self.pages, 1)

        if ctx is not None:
            ctx.results_changed.connect(self.set_runs)
            ctx.active_dataset_changed.connect(lambda _ds: self.refresh())
            ctx.position_changed.connect(self.scroll_to_position)

    # ---- runs --------------------------------------------------------------------- #
    def refresh(self) -> None:
        ds = getattr(self.ctx, "active", None)
        self.set_runs(find_runs(ds.root) if ds is not None else [])

    def set_runs(self, folders) -> None:
        folders = [Path(f) for f in folders or []]
        current = self._folder
        self._runs = folders
        self.run_combo.blockSignals(True)
        self.run_combo.clear()
        for folder in folders:
            self.run_combo.addItem(describe_run(folder), str(folder))
        index = folders.index(current) if current in folders else 0
        self.run_combo.setCurrentIndex(index if folders else -1)
        self.run_combo.blockSignals(False)
        if folders:
            self._load(folders[index])
        else:
            self._folder = None
            self._items, self._overview = {}, []
            self.info.setText("")
            self._rebuild_chips()
            self.pages.setCurrentWidget(self.empty)

    def _on_run(self, index: int) -> None:
        if 0 <= index < len(self._runs):
            self._load(self._runs[index])

    def _load(self, folder: Path) -> None:
        signature = _run_signature(Path(folder))
        if signature == self._signature and self._tiles:
            return  # nothing new in this run: keep the tiles (0.5 rebuilt them on every refresh)
        self._signature = signature
        self._folder = Path(folder)
        self._items, self._overview = collect(self._folder)
        meta = run_metadata(self._folder)
        seconds = meta.get("processing_seconds")
        videos = sum(1 for it in self._items.values() if it.is_video)
        parts = [f"{len({r for r, _k in self._items})} positions", f"{videos} videos"]
        if isinstance(seconds, (int, float)):
            parts.append(f"processed in {float(seconds):.1f} s")
        parts.append(str(self._folder))
        self.info.setText(" · ".join(parts))
        self._rebuild_chips()
        self._rebuild_grid()
        self.show_grid()

    # ---- grid ------------------------------------------------------------------------ #
    def columns(self) -> list[str]:
        kinds = {k for _r, k in self._items}
        ordered = [k for k in COLUMN_ORDER if k in kinds] + sorted(kinds - set(COLUMN_ORDER))
        return [k for k in ordered if k not in self._hidden]

    def _rebuild_chips(self) -> None:
        while self.chip_layout.count():
            w = self.chip_layout.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        kinds = {k for _r, k in self._items}
        ordered = [k for k in COLUMN_ORDER if k in kinds] + sorted(kinds - set(COLUMN_ORDER))
        for kind in ordered:
            chip = QToolButton(text=label_for(kind))
            chip.setCheckable(True)
            chip.setChecked(kind not in self._hidden)
            chip.setStyleSheet(CHIP_QSS)
            chip.setToolTip(f"Show or hide the {label_for(kind).lower()} column")
            chip.toggled.connect(lambda on, k=kind: self._toggle_kind(k, on))
            self.chip_layout.addWidget(chip)
        self.chip_layout.addStretch(1)

    def _toggle_kind(self, kind: str, on: bool) -> None:
        (self._hidden.discard if on else self._hidden.add)(kind)
        self._rebuild_grid()

    def _rebuild_grid(self) -> None:
        for task in self._poster_tasks:
            task.cancel()
        self._poster_tasks.clear()
        self._tiles.clear()
        self._row_labels.clear()
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        if not self._items and not self._overview:
            self.pages.setCurrentWidget(self.empty)
            return
        size = TILE_SIZES.get(self.size_combo.currentText(), 300)
        row = 0
        columns = self.columns()
        for col, kind in enumerate(columns):
            header = QLabel(label_for(kind))
            header.setProperty("role", "heading")
            header.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.grid.addWidget(header, row, col + 1)
        row += 1
        for roi in sorted({r for r, _k in self._items}, key=natural_key):
            roi_label = QLabel(roi)
            roi_label.setProperty("role", "heading")
            self.grid.addWidget(roi_label, row, 0, Qt.AlignmentFlag.AlignTop)
            self._row_labels[roi] = roi_label
            for col, kind in enumerate(columns):
                item = self._items.get((roi, kind))
                if item is not None:
                    self._add_tile(item, size, row, col + 1)
            row += 1
        if self._overview:  # whole-run images last and smaller, so the positions come first
            label = QLabel("Run overview")
            label.setProperty("role", "heading")
            self.grid.addWidget(label, row, 0, Qt.AlignmentFlag.AlignTop)
            small = max(160, size * 2 // 3)
            for col, item in enumerate(self._overview):
                self._add_tile(item, small, row, col + 1)
        self.pages.setCurrentWidget(self.scroll)

    def _add_tile(self, item: ResultItem, size: int, row: int, col: int) -> None:
        tile = ResultTile(item, size, self.grid_host)
        tile.clicked.connect(lambda _=False, it=item: self.open_item(it))
        self.grid.addWidget(tile, row, col)
        self._tiles.append(tile)
        path = item.poster if item.poster is not None else item.primary
        key = _poster_key(path, size)
        cached = _POSTERS.get(key)
        if cached is not None:
            tile.set_poster(cached)
            return

        def done(rgb, t=tile, k=key):
            _remember_poster(k, rgb)
            if t in self._tiles:
                t.set_poster(rgb)

        self._poster_tasks.append(run_in_background(lambda p=path: poster(p, size), done,
                                                    lambda m: log.debug("Poster unavailable: %s", m)))

    def tiles(self) -> list[ResultTile]:
        return list(self._tiles)

    def scroll_to_position(self, roi: str) -> None:
        """A click in the explorer while the results page is open brings that position's row into view."""
        label = self._row_labels.get(roi)
        if label is not None and self.pages.currentWidget() is self.scroll:
            self.scroll.ensureWidgetVisible(label, 0, 40)

    # ---- player ------------------------------------------------------------------------ #
    def open_item(self, item: ResultItem) -> None:
        self._player_item = item
        title = f"{item.roi} · {label_for(item.kind)}" if item.roi != OVERVIEW else label_for(item.kind)
        self.player_title.setText(f"{title}   ({item.primary.name})")
        self.pages.setCurrentIndex(2)
        if item.is_video:
            if self.player.open_video(item.primary):
                self.player.toggle_play()
        else:
            self.player.show_image(item.primary)
        log.debug("results.open(%s)", item.primary)

    def show_grid(self) -> None:
        self.player.close_video()
        self.pages.setCurrentWidget(self.scroll if (self._items or self._overview) else self.empty)

    def _open_folder(self) -> None:
        target = self._folder or (Path(self.ctx.active.root) if getattr(self.ctx, "active", None) else None)
        if target is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _open_file_folder(self) -> None:
        if self._player_item is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._player_item.primary.parent)))
