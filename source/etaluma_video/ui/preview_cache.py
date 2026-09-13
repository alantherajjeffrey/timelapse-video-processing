"""Live-preview cache: experiment positions held in memory at preview resolution.

Plan section 4.6, revised in 0.6. Opening a position streams every timepoint of every channel into
a ``PreviewStack`` (uint8 planes at 640 px by default). Timepoints are decoded on several threads
in a coarse-to-fine order (every 8th first, then every 4th, every 2nd, the rest), so the whole
timeline can be scrubbed after a few seconds; a frame that is not loaded yet shows its nearest
loaded neighbour. 0.5 read one image at a time: 38 s per CD14 position.

Auto-normalised bounds no longer come from these 640 px copies (0.5 did that, and its bounds disagreed with
the videos'). As soon as an experiment opens, every position is measured in the background at full
resolution by the engine's sampled histogram pass (every 8th timepoint), the current position
first, into the same disk cache Process and Quick video read. The display panel's Auto numbers are
therefore the numbers the videos use, and Process finds the measurement already done.

Only the controller touches AppContext, and only on the GUI thread.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import reduce

import cv2
import numpy as np
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot

from ..engine import display
from ..engine.jobs import CancelToken, JobCancelled
from ..engine.models import CHANNEL_ORDER, ChannelHistogram, HistogramSet, percentile_from_counts
from .state import dataset_key
from .workers import run_in_background

log = logging.getLogger("etaluma.ui")

MAX_STACKS = 2


def accumulate(hist: ChannelHistogram, plane: np.ndarray, mask: np.ndarray | None) -> None:
    """Add one plane to a histogram (overlay pixels excluded); same numbers as the engine pass."""
    values = plane[~mask] if mask is not None and mask.shape == plane.shape else plane.ravel()
    if values.size == 0:
        return
    counts = np.bincount(values.ravel(), minlength=256)[:256].astype(np.int64)
    hist.counts += counts
    hist.frame_p995_max = max(hist.frame_p995_max, percentile_from_counts(counts, 99.5))
    hist.frames += 1


def preview_size(full_h: int, full_w: int, width: int) -> tuple[int, int]:
    w = max(1, min(int(width), int(full_w)))
    return max(1, int(round(full_h * w / full_w))), w


def total_memory_bytes() -> int:
    """Physical memory of this PC (8 GB when it cannot be read)."""
    try:
        if sys.platform == "win32":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                            ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                            ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                            ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                            ("avail_extended", ctypes.c_ulonglong)]

            status = _Status()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.total_phys)
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except Exception:
        return 8 * 1024 ** 3


def preview_budget_bytes() -> int:
    """Memory the preview may hold (0.7): a quarter of the RAM, between 0.5 and 6 GB.

    ``ETALUMA_PREVIEW_BUDGET_MB`` overrides it (tests, small machines).
    """
    override = os.environ.get("ETALUMA_PREVIEW_BUDGET_MB")
    if override:
        return int(float(override) * 1_000_000)
    return int(min(6e9, max(5e8, total_memory_bytes() * 0.25)))


def fit_to_budget(full_h: int, full_w: int, width: int, timepoints: int, channels: int, budget: int) -> tuple[int, int]:
    """Preview (h, w) at ``width``, or smaller when the whole stack would not fit in ``budget`` bytes."""
    h, w = preview_size(full_h, full_w, width)
    needed = max(1, timepoints * channels * h * w)
    if needed > budget:
        factor = (budget / needed) ** 0.5
        h, w = preview_size(full_h, full_w, max(64, int(w * factor)))
    return h, w


def load_order(n: int) -> list[int]:
    """Coarse to fine: every 8th timepoint, then every 4th, every 2nd, then the rest."""
    order: list[int] = []
    seen: set[int] = set()
    for step in (8, 4, 2, 1):
        for i in range(0, n, step):
            if i not in seen:
                seen.add(i)
                order.append(i)
    return order


def loader_workers() -> int:
    """Threads for one preview load or the background measurement: half the decode threads."""
    from ..engine.histograms import default_workers

    return max(2, min(8, default_workers() // 2))


@dataclass
class PreviewStack:
    """Every timepoint of one position at preview resolution."""

    dataset_root: str
    roi: str
    channels: list[str]
    serials: list[int]
    full_shape: tuple[int, int]
    shape: tuple[int, int]  # preview (h, w)
    total_positions: int
    fingerprint: str
    requested_width: int = 640  # cache key: the width asked for, not the (possibly smaller) frame width
    planes: dict[str, np.ndarray] = field(default_factory=dict)  # ch -> (T, h, w)
    present: dict[str, np.ndarray] = field(default_factory=dict)  # ch -> (T,) bool
    ready: np.ndarray | None = None  # (T,) bool: timepoint decoded (they arrive coarse to fine)
    mask: np.ndarray | None = None  # overlay mask at preview resolution
    loaded: int = 0  # number of timepoints decoded so far (not a prefix since 0.6)
    histograms: HistogramSet | None = None  # 0.5 only; Auto-normalised bounds now come from the engine pass
    complete: bool = False
    seconds: float = 0.0

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.dataset_root, self.roi, self.requested_width)

    @property
    def n(self) -> int:
        return len(self.serials)

    @property
    def scale(self) -> float:
        return self.shape[1] / float(self.full_shape[1])

    @property
    def nbytes(self) -> int:
        return int(sum(a.nbytes for a in self.planes.values()))

    def is_ready(self, index: int) -> bool:
        if self.ready is None:
            return 0 <= index < self.loaded
        return 0 <= index < len(self.ready) and bool(self.ready[index])

    def nearest_ready(self, index: int) -> int | None:
        if self.ready is None:
            return min(index, self.loaded - 1) if self.loaded else None
        ready = np.flatnonzero(self.ready)
        if ready.size == 0:
            return None
        return int(ready[np.argmin(np.abs(ready - index))])

    def next_ready(self, index: int) -> int | None:
        """The first loaded timepoint at or after ``index``, wrapping to the start."""
        if self.ready is None:
            return index if index < self.loaded else (0 if self.loaded else None)
        after = np.flatnonzero(self.ready[index:])
        if after.size:
            return index + int(after[0])
        before = np.flatnonzero(self.ready)
        return int(before[0]) if before.size else None

    def planes_at(self, index: int) -> dict[str, np.ndarray]:
        return {ch: self.planes[ch][index] for ch in self.channels if self.present[ch][index]}

    def values_at(self, index: int, x: int, y: int) -> dict[str, int]:
        h, w = self.shape
        if not (0 <= x < w and 0 <= y < h) or not self.is_ready(index):
            return {}
        return {ch: int(self.planes[ch][index, y, x]) for ch in self.channels if self.present[ch][index]}


# --------------------------------------------------------------------------- #
# Loader (pool thread)
# --------------------------------------------------------------------------- #


class _LoaderSignals(QObject):
    started = Signal(object)  # PreviewStack, allocated
    progress = Signal(object, int)  # PreviewStack, timepoints loaded
    finished = Signal(object)  # PreviewStack, complete
    failed = Signal(object, str)  # key, message


class _StackLoader(QRunnable):
    def __init__(self, dataset, roi: str, width: int, token: CancelToken, signals: _LoaderSignals) -> None:
        super().__init__()
        self.dataset, self.roi, self.width = dataset, roi, int(width)
        self.token, self.signals = token, signals
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            self._run()
        except RuntimeError as exc:  # signal source deleted: the viewer was closed while loading
            self.token.cancel()
            log.debug("Preview load abandoned: %s", exc)

    def _run(self) -> None:
        from ..engine.histograms import overlay_mask_for
        from ..engine.parsing import read_plane

        ds, roi = self.dataset, self.roi
        key = (dataset_key(ds), roi, self.width)
        try:
            t0 = time.perf_counter()
            groups = ds.groups
            per_channel = groups.get(roi) or {}
            channels = [c for c in CHANNEL_ORDER if c in per_channel]
            if not channels:
                raise ValueError(f"position {roi} has no frames")
            by_serial = {ch: {f.serial: f for f in per_channel[ch]} for ch in channels}
            serials = sorted(set().union(*[set(m) for m in by_serial.values()]))
            first_channel = channels[0]
            first = by_serial[first_channel][serials[0]] if serials[0] in by_serial[first_channel] else next(iter(by_serial[first_channel].values()))
            first_plane = read_plane(first.path, first.channel)
            full_h, full_w = first_plane.shape
            budget = preview_budget_bytes() // MAX_STACKS
            h, w = fit_to_budget(full_h, full_w, self.width, len(serials), len(channels), budget)
            if w < min(self.width, full_w):
                log.info("Preview: %s kept at %d px to stay within the preview memory budget (%.0f MB per position)",
                         roi, w, budget / 1e6)
            mask_full, _overlays = overlay_mask_for(ds)
            if mask_full is not None and mask_full.shape != (full_h, full_w):
                mask_full = None
            stack = PreviewStack(
                dataset_root=key[0], roi=roi, channels=channels, serials=serials,
                full_shape=(full_h, full_w), shape=(h, w), total_positions=len(groups),
                fingerprint=getattr(ds, "source_fingerprint", ""), requested_width=self.width,
            )
            for ch in channels:
                stack.planes[ch] = np.zeros((len(serials), h, w), dtype=np.uint8)
                stack.present[ch] = np.zeros(len(serials), dtype=bool)
            stack.ready = np.zeros(len(serials), dtype=bool)
            if mask_full is not None:
                stack.mask = cv2.resize(mask_full.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST).astype(bool)
            self.signals.started.emit(stack)

            def load(i: int) -> int:
                if self.token.cancelled:
                    raise JobCancelled()
                serial = serials[i]
                for ch in channels:
                    frame = by_serial[ch].get(serial)
                    if frame is None:
                        continue
                    plane = first_plane if frame is first else read_plane(frame.path, ch)
                    if plane.shape != (full_h, full_w):
                        continue
                    stack.planes[ch][i] = plane if (h, w) == (full_h, full_w) else \
                        cv2.resize(plane, (w, h), interpolation=cv2.INTER_AREA)
                    stack.present[ch][i] = True
                return i

            workers = loader_workers()
            order = iter(load_order(len(serials)))
            last_emit = 0.0
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="preview") as pool:
                pending: deque = deque()
                for i in order:
                    pending.append(pool.submit(load, i))
                    if len(pending) >= workers * 2:
                        break
                try:
                    while pending:
                        i = pending.popleft().result()
                        stack.ready[i] = True
                        stack.loaded += 1
                        nxt = next(order, None)
                        if nxt is not None:
                            pending.append(pool.submit(load, nxt))
                        now = time.perf_counter()
                        if stack.loaded == 1 or now - last_emit > 0.12 or not pending:
                            last_emit = now
                            self.signals.progress.emit(stack, stack.loaded)
                except BaseException:
                    self.token.cancel()
                    for fut in pending:
                        fut.cancel()
                    raise
            stack.complete = True
            stack.seconds = time.perf_counter() - t0
            self.signals.finished.emit(stack)
        except JobCancelled:
            return
        except Exception as exc:  # reported on the GUI thread
            self.signals.failed.emit(key, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# Controller (GUI thread)
# --------------------------------------------------------------------------- #


class PreviewController(QObject):
    """Loads positions on demand and keeps the active experiment's Auto-normalised bounds current."""

    stack_started = Signal(object)  # PreviewStack for the current position
    stack_progress = Signal(object, int)
    stack_finished = Signal(object)
    status = Signal(str)

    def __init__(self, ctx, parent: QObject | None = None, width_getter=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._width_getter = width_getter or (lambda: 640)
        self._stacks: "OrderedDict[tuple, PreviewStack]" = OrderedDict()
        self._loading: dict[tuple, tuple[CancelToken, _LoaderSignals]] = {}
        #: (dataset key, rolling-ball radius) -> roi -> sampled full-resolution histograms
        self._measured: dict[tuple, dict[str, HistogramSet]] = {}
        self._measure_task = None
        self._measure_key: tuple | None = None
        self._auto_key: tuple | None = None
        self._radius_seen: int | None = None
        if ctx is not None:
            ctx.active_dataset_changed.connect(self._on_dataset)
            ctx.position_changed.connect(self._on_position)
            ctx.profile_changed.connect(self._on_profile)
            ctx.compute_all_requested.connect(self.compute_all)

    # ---- queries ------------------------------------------------------------ #
    def width(self) -> int:
        try:
            return int(self._width_getter() or 640)
        except Exception:
            return 640

    def current_key(self) -> tuple | None:
        ds = self.ctx.active
        if ds is None or not self.ctx.position:
            return None
        return (dataset_key(ds), self.ctx.position, self.width())

    def current(self) -> PreviewStack | None:
        key = self.current_key()
        return self._stacks.get(key) if key else None

    def is_loading(self) -> bool:
        return bool(self._loading)

    def is_measuring(self) -> bool:
        return self._measure_task is not None

    # ---- loading ------------------------------------------------------------ #
    def request(self, dataset, roi: str) -> None:
        if dataset is None or not roi:
            return
        key = (dataset_key(dataset), roi, self.width())
        if key in self._stacks:
            self._stacks.move_to_end(key)
            stack = self._stacks[key]
            self.stack_started.emit(stack)
            if stack.complete:
                self.stack_finished.emit(stack)
            else:
                self.stack_progress.emit(stack, stack.loaded)
            return
        if key in self._loading:
            return
        for other in list(self._loading):  # one load at a time: the newest request wins
            self._cancel_load(other)
        token, signals = CancelToken(), _LoaderSignals()
        signals.started.connect(self._on_started)
        signals.progress.connect(self._on_progress)
        signals.finished.connect(self._on_finished)
        signals.failed.connect(self._on_failed)
        self._loading[key] = (token, signals)
        log.debug("preview: loading %s at %d px", roi, key[2])
        self.status.emit(f"Loading {roi}…")
        QThreadPool.globalInstance().start(_StackLoader(dataset, roi, key[2], token, signals))

    def _cancel_load(self, key) -> None:
        entry = self._loading.pop(key, None)
        if entry is not None:
            entry[0].cancel()
        self._stacks.pop(key, None) if key in self._stacks and not self._stacks[key].complete else None

    def cancel_all(self) -> None:
        for key in list(self._loading):
            self._cancel_load(key)
        if self._measure_task is not None:
            self._measure_task.cancel()
            self._measure_task = None

    @Slot(object)
    def _on_started(self, stack: PreviewStack) -> None:
        if stack.key not in self._loading:
            return
        self._stacks[stack.key] = stack
        self._stacks.move_to_end(stack.key)
        while len(self._stacks) > MAX_STACKS:
            old_key, _old = self._stacks.popitem(last=False)
            if old_key in self._loading:
                self._cancel_load(old_key)
        if stack.key == self.current_key():
            self.stack_started.emit(stack)

    @Slot(object, int)
    def _on_progress(self, stack: PreviewStack, loaded: int) -> None:
        if stack.key == self.current_key():
            self.stack_progress.emit(stack, loaded)
            self.status.emit(f"Loading {stack.roi}: {loaded}/{stack.n} timepoints")

    @Slot(object)
    def _on_finished(self, stack: PreviewStack) -> None:
        self._loading.pop(stack.key, None)
        mb = stack.nbytes / 1e6
        log.info("Preview: %s cached %d × %d planes at %d px (%.0f MB) in %.1f s",
                 stack.roi, stack.n, len(stack.channels), stack.shape[1], mb, stack.seconds)
        if stack.key == self.current_key():
            self.stack_finished.emit(stack)
            self.status.emit(f"{stack.roi}: {stack.n} timepoints in memory ({mb:.0f} MB)")

    @Slot(object, str)
    def _on_failed(self, key, message: str) -> None:
        self._loading.pop(key, None)
        self._stacks.pop(key, None)
        log.warning("Preview of %s could not be loaded: %s", key[1] if key else "?", message)
        self.status.emit(f"Preview failed: {message}")

    # ---- context reactions ----------------------------------------------------- #
    def _on_dataset(self, dataset) -> None:
        active = dataset_key(dataset)
        for key in list(self._loading):
            if key[0] != active:
                self._cancel_load(key)
        if self._measure_task is not None:
            self._measure_task.cancel()
            self._measure_task = None
            self._measure_key = None
        self._auto_key = None
        if dataset is None:
            self.status.emit("")
            return
        self._radius_seen = self._radius()
        QTimer.singleShot(0, self._start_measure)

    def _on_position(self, roi: str) -> None:
        if self.ctx.active is not None and roi:
            self.request(self.ctx.active, roi)

    def _on_profile(self, profile) -> None:
        radius = self._radius()
        if radius != self._radius_seen:
            self._radius_seen = radius
            self._start_measure()
        self._refresh_auto()

    # ---- histograms and Auto-normalised bounds -------------------------------------------- #
    def _radius(self) -> int | None:
        rb = self.ctx.profile.rolling_ball
        return int(rb.radius_px) if rb.enabled and rb.radius_px > 0 else None

    def _start_measure(self, force: bool = False) -> None:
        """Measure every position not measured yet for the current rolling-ball radius (background)."""
        ds = self.ctx.active
        if ds is None:
            return
        from ..engine.histograms import SAMPLE_STEP, load_or_build_position, overlay_mask_for
        from ..engine.userdata import preview_cache_dir

        radius = self._radius()
        key = (dataset_key(ds), radius)
        store = self._measured.setdefault(key, {})
        if self._measure_task is not None:
            if self._measure_key == key and not force:
                return
            self._measure_task.cancel()
            self._measure_task = None
        first = self.ctx.position if self.ctx.position in ds.groups else None
        positions = [p for p in ([first] if first else []) + list(ds.groups) if p and p not in store]
        positions = list(dict.fromkeys(positions))
        if not positions:
            self._refresh_auto(force=True)
            return
        cache_dir = preview_cache_dir()
        workers = loader_workers()
        started = time.perf_counter()
        total = len(ds.groups)

        def work(progress, cancel):
            mask, _ov = overlay_mask_for(ds)
            for roi in positions:
                if cancel.cancelled:
                    raise JobCancelled()
                store[roi] = load_or_build_position(ds, roi, cache_dir, rolling_ball_radius=radius, overlay_mask=mask,
                                                    cancel=cancel, workers=workers, sampled=True)
                progress(roi)
            return len(positions)

        def on_progress(_roi: str) -> None:
            if self._measure_key == key:
                self._refresh_auto(force=True)
                self.status.emit(f"Measuring intensities: {len(store)}/{total} positions")

        def done(_count) -> None:
            if self._measure_key == key:
                self._measure_task = None
            seconds = time.perf_counter() - started
            how = "read from the cache" if seconds < 1.0 else f"measured in {seconds:.1f} s"
            log.info("Auto-normalised bounds: all %d positions %s (every %dth timepoint at full size; Process reuses them)",
                     total, how, SAMPLE_STEP)
            self.status.emit("Auto-normalised bounds cover every position")
            self._refresh_auto(force=True)

        def failed(message: str) -> None:
            if self._measure_key == key:
                self._measure_task = None
            log.warning("Measuring intensities failed: %s", message)

        def cancelled() -> None:
            if self._measure_key == key:
                self._measure_task = None

        self._measure_key = key
        self._measure_task = run_in_background(work, done, failed, on_progress=on_progress, on_cancelled=cancelled)

    def _refresh_auto(self, force: bool = False) -> None:
        ds = self.ctx.active
        if ds is None:
            return
        radius = self._radius()
        key = (dataset_key(ds), radius)
        per_roi = dict(self._measured.get(key, {}))
        method = self.ctx.profile.auto_method
        auto_key = (key, method, tuple(sorted(per_roi)))
        if not force and auto_key == self._auto_key:
            return
        self._auto_key = auto_key
        total = len(ds.groups)
        if not per_roi:
            self.ctx.coverage_changed.emit(0, total)
            return
        start = HistogramSet(total_positions=total, rolling_ball_radius=radius,
                             fingerprint=getattr(ds, "source_fingerprint", ""))
        merged = reduce(lambda a, b: a.merge(b), per_roi.values(), start)
        merged.total_positions = total
        merged.rolling_ball_radius = radius
        try:
            auto = display.auto_bounds_all(merged, method)
        except Exception as exc:  # a degenerate histogram must not break the preview
            log.warning("Auto-normalised bounds could not be computed: %s", exc)
            return
        self.ctx.set_histograms(merged, auto)
        self.ctx.coverage_changed.emit(len(per_roi), total)
        summary = " · ".join(f"{ch} {lo:.0f}–{hi:.0f}" for ch, (lo, hi) in auto.items())
        log.debug("Auto-normalised bounds (%s, %d/%d positions%s): %s", method, len(per_roi), total,
                  f", rolling ball {radius} px" if radius else "", summary)

    def compute_all(self) -> None:
        """Measure every position of the active experiment (normally already running or done)."""
        self._start_measure()
