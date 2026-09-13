"""256-bin intensity histograms over whole experiments, and their on-disk cache.

Auto display bounds are derived from these histograms (``display.auto_bounds_all``), and since
0.6 the live preview and the exported videos use the same ones, so the pass measures:

* full-resolution planes (0.5's preview measured its 640 px copies, whose upper bounds came out
  about 10 gray levels lower than the videos'),
* every position, and within a position every 8th timepoint with at least 24 per position (all of
  them when there are fewer). On CD14 the bounds from every 16th frame equal those from every
  frame to within one gray level for all three Auto methods; decoding is the cost (58 ms per
  1900 px LZW TIFF), so sampling is what makes the pass short,
* burned-in Lumaview overlay pixels excluded through a boolean mask,
* rolling-ball background subtraction applied first when a radius is given (fluorescence only),
  with the overlay boxes filled so they do not bend the background estimate,
* the per-frame 99.5th percentile tracked as a running maximum, which is what the Classic
  auto method uses.

``build_histograms(..., sampled=False)`` still measures every frame (tests and the CLI use it).

This module owns the histogram pass and its cache; :mod:`etaluma_video.engine.display` owns the
rules that turn a histogram into bounds.
"""
from __future__ import annotations

import hashlib
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import numpy as np

from . import display
from .jobs import ProgressFn, check_cancel
from .models import FLUOR_CHANNELS, ChannelHistogram, HistogramSet, Overlays, percentile_from_counts
from .parsing import Dataset, read_plane, read_rgb

__all__ = ["build_histograms", "first_white_median", "overlay_mask_for", "histogram_cache_path",
           "load_or_build_position", "build_all_positions", "sample_frames", "cached_positions",
           "default_workers", "SAMPLE_STEP", "SAMPLE_MINIMUM"]

CACHE_SCHEMA = "hist_v5"
SAMPLE_STEP = 8
SAMPLE_MINIMUM = 24


# --------------------------------------------------------------------------- #
# Accumulation
# --------------------------------------------------------------------------- #


_MASK_INDEX: dict[int, tuple[np.ndarray, np.ndarray]] = {}
_MASK_LOCK = threading.Lock()


def _flat_mask(mask: np.ndarray) -> np.ndarray:
    """Flat indices of the masked pixels, computed once per mask array."""
    with _MASK_LOCK:
        hit = _MASK_INDEX.get(id(mask))
        if hit is not None and hit[0] is mask:
            return hit[1]
        index = np.flatnonzero(mask)
        if len(_MASK_INDEX) > 8:
            _MASK_INDEX.clear()
        _MASK_INDEX[id(mask)] = (mask, index)
        return index


def _add_frame(hist: ChannelHistogram, plane: np.ndarray, mask: np.ndarray | None) -> None:
    """ChannelHistogram.add_frame without the per-frame sort (identical numbers, much faster).

    The masked histogram is the whole plane's minus the masked pixels' (17 k of 3.6 M pixels on
    an LS720 capture), which avoids copying the unmasked 3.6 M values out of every frame.
    """
    flat = plane.ravel()
    if flat.size == 0:
        return
    counts = np.bincount(flat, minlength=256)[:256].astype(np.int64)
    if mask is not None:
        index = _flat_mask(mask)
        if index.size:
            counts -= np.bincount(flat[index], minlength=256)[:256]
    if counts.sum() == 0:
        return
    hist.counts += counts
    hist.frame_p995_max = max(hist.frame_p995_max, percentile_from_counts(counts, 99.5))
    hist.frames += 1


def sample_frames(frames: list, step: int = SAMPLE_STEP, minimum: int = SAMPLE_MINIMUM) -> list:
    """Every ``step``-th frame by serial (closer when needed to keep ``minimum``), always the last one."""
    ordered = sorted(frames, key=lambda f: f.serial)
    if len(ordered) <= minimum:
        return ordered
    k = max(1, min(int(step), len(ordered) // int(minimum)))
    picked = ordered[::k]
    if picked[-1] is not ordered[-1]:
        picked.append(ordered[-1])
    return picked


def overlay_mask_for(ds: Dataset, frame=None) -> tuple[np.ndarray | None, object]:
    """(boolean overlay mask, Overlays) detected once per dataset from its first frame.

    Returns ``(None, Overlays())`` when nothing is detected, which is what the placeholder
    detector does and what a capture without burned-in overlays looks like.
    """
    from . import calibration as calib  # local: calibration imports parsing lazily

    if frame is None:
        for ch in ("WHITE", "F2", "F3", "F1"):
            candidates = [f for f in ds.frames if f.channel == ch]
            if candidates:
                frame = sorted(candidates, key=lambda f: (f.roi, f.serial))[0]
                break
    if frame is None:
        return None, Overlays()
    overlays = calib.detect_overlays(read_rgb(frame.path))
    if not overlays.detected or not overlays.image_shape:
        return None, overlays
    return overlays.mask(overlays.image_shape), overlays


def build_histograms(ds: Dataset, positions: list[str] | None = None, *,
                     rolling_ball_radius: int | None = None,
                     overlay_mask: np.ndarray | None = None,
                     channels: list[str] | None = None,
                     progress: ProgressFn | None = None,
                     cancel=None, workers: int | None = None,
                     sampled: bool = False) -> HistogramSet:
    """Histograms of ``positions`` (all positions when None): every frame, or a sample (``sampled``).

    rolling_ball_radius: when given, fluorescence planes are background-subtracted first,
    so the bounds describe what the viewer will actually see.
    overlay_mask: boolean HxW array, True on pixels to ignore (``Overlays.mask(shape)``).
    """
    groups = ds.groups
    wanted = list(groups) if positions is None else [p for p in positions if p in groups]
    wanted_channels = channels or ds.channels
    frames = []
    for p in wanted:
        for ch, fs in groups[p].items():
            if ch in wanted_channels:
                frames.extend(sample_frames(fs) if sampled else fs)
    hists = HistogramSet(positions=sorted(wanted), total_positions=len(groups),
                         rolling_ball_radius=rolling_ball_radius, fingerprint=ds.source_fingerprint)
    for ch in wanted_channels:
        hists.channels[ch] = ChannelHistogram()
    total = len(frames)

    def load(f):
        check_cancel(cancel)
        try:
            plane = read_plane(f.path, f.channel)
        except Exception as exc:  # 0.7: an unreadable TIFF is left out of the bounds, not fatal
            import logging  # noqa: WPS433

            logging.getLogger("etaluma.engine").warning("Intensities: %s could not be read (%s); left out.",
                                                        Path(f.path).name, type(exc).__name__)
            return None
        if rolling_ball_radius and f.channel in FLUOR_CHANNELS:
            fill = overlay_mask if overlay_mask is not None and overlay_mask.shape == plane.shape else None
            plane = display.subtract_background(plane, int(rolling_ball_radius), fill)
        return plane

    # TIFF decoding releases the GIL: decode on threads, accumulate here in order.
    n_workers = max(1, int(workers) if workers else default_workers())
    step = n_workers * 4
    done = 0
    with ThreadPoolExecutor(max_workers=n_workers, thread_name_prefix="histogram") as pool:
        for start in range(0, total, step):
            check_cancel(cancel)
            chunk = frames[start:start + step]
            for f, plane in zip(chunk, pool.map(load, chunk)):
                if plane is None:
                    continue
                mask = overlay_mask if overlay_mask is not None and overlay_mask.shape == plane.shape else None
                _add_frame(hists.channels[f.channel], plane, mask)
            done += len(chunk)
            if progress:
                progress(f"Intensity measurement: {done}/{total} images")
            check_cancel(cancel)
    for ch in [c for c, h in hists.channels.items() if h.frames == 0]:
        del hists.channels[ch]
    return hists


def default_workers() -> int:
    """Decode threads: every logical core but two (the GUI and the encoders), at least 2.

    0.5 stopped at 10; processing PCs often have more cores than that, and decoding scales with them.
    """
    return max(2, min(32, (os.cpu_count() or 4) - 2))


def first_white_median(ds: Dataset, overlay_mask: np.ndarray | None = None) -> float | None:
    """Median of the first WHITE plane, overlay pixels excluded: picks the WHITE auto preset.

    Decision 15: median > 128 means brightfield, otherwise phase contrast.
    """
    whites = [f for f in ds.frames if f.channel == "WHITE"]
    if not whites:
        return None
    frame = sorted(whites, key=lambda f: (f.order, f.roi, f.serial))[0]
    plane = read_plane(frame.path, "WHITE")
    values = plane[~overlay_mask] if overlay_mask is not None and overlay_mask.shape == plane.shape else plane
    return float(np.median(values))


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


def histogram_cache_path(cache_dir: Path | str, fingerprint: str, position: str,
                         radius: int | None = None, masked: bool = False, sampled: bool = False) -> Path:
    """``hist_v4_<sha>.npz`` for one position of one source state, rolling-ball radius and sampling."""
    key = (f"{CACHE_SCHEMA}:{fingerprint}:{position}:{radius or 0}:{'masked' if masked else 'raw'}"
           f":{'sampled' if sampled else 'all'}")
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    return Path(cache_dir) / f"{CACHE_SCHEMA}_{digest}.npz"


def cached_positions(ds: Dataset, cache_dir: Path | str, *, rolling_ball_radius: int | None = None,
                     masked: bool = False, sampled: bool = True) -> list[str]:
    """Positions whose histograms are already on disk for this source state (no reading)."""
    return [p for p in ds.groups
            if histogram_cache_path(cache_dir, ds.source_fingerprint, p, rolling_ball_radius, masked, sampled).exists()]


def load_or_build_position(ds: Dataset, position: str, cache_dir: Path | str, *,
                           rolling_ball_radius: int | None = None,
                           overlay_mask: np.ndarray | None = None,
                           channels: list[str] | None = None,
                           progress: ProgressFn | None = None,
                           cancel=None, workers: int | None = None,
                           sampled: bool = False) -> HistogramSet:
    """Histograms of one position, reusing the cached file when the source is unchanged."""
    path = histogram_cache_path(cache_dir, ds.source_fingerprint, position, rolling_ball_radius,
                                masked=overlay_mask is not None, sampled=sampled)
    if path.exists():
        try:
            cached = HistogramSet.load(path)
            if cached.fingerprint == ds.source_fingerprint and cached.channels:
                cached.total_positions = len(ds.groups)
                return cached
        except (OSError, ValueError, KeyError, EOFError):
            path.unlink(missing_ok=True)
    hists = build_histograms(ds, [position], rolling_ball_radius=rolling_ball_radius,
                             overlay_mask=overlay_mask, channels=channels, progress=progress, cancel=cancel,
                             workers=workers, sampled=sampled)
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.stem + "_" + uuid.uuid4().hex + ".npz")
    try:
        hists.save(temporary)
        temporary.replace(path)
    except OSError:
        pass
    finally:
        temporary.unlink(missing_ok=True)
    return hists


def build_all_positions(ds: Dataset, *, partial: HistogramSet | None = None,
                        rolling_ball_radius: int | None = None,
                        overlay_mask: np.ndarray | None = None,
                        channels: list[str] | None = None,
                        progress: ProgressFn | None = None,
                        cancel=None, cache_dir: Path | str | None = None,
                        workers: int | None = None, sampled: bool = False,
                        on_position: Callable[[int, int, str], None] | None = None,
                        ) -> tuple[HistogramSet, dict[str, HistogramSet]]:
    """(merged set over every position, per-position sets).

    ``partial`` is reused when it belongs to this source state; only the positions it does not
    cover are read. Fixed mode needs the per-position sets for its per-ROI auto bounds.
    ``on_position(i, n, position)`` is called before each remaining position (progress bars);
    without it, ``progress`` receives one line per position.
    """
    per_position: dict[str, HistogramSet] = {}
    merged = HistogramSet(total_positions=len(ds.groups), rolling_ball_radius=rolling_ball_radius,
                          fingerprint=ds.source_fingerprint)
    covered: list[str] = []
    if (partial is not None and partial.channels
            and (not partial.fingerprint or partial.fingerprint == ds.source_fingerprint)
            and partial.rolling_ball_radius == rolling_ball_radius):
        merged = merged.merge(partial)
        covered = list(partial.positions)
    remaining = [p for p in ds.groups if p not in covered]
    for i, position in enumerate(remaining):
        check_cancel(cancel)
        if on_position is not None:
            on_position(i, len(remaining), position)
        elif progress:
            progress(f"Intensity measurement: position {i + 1}/{len(remaining)} ({position})")
        if cache_dir is not None and channels is None:
            one = load_or_build_position(ds, position, cache_dir, rolling_ball_radius=rolling_ball_radius,
                                         overlay_mask=overlay_mask, cancel=cancel, workers=workers,
                                         sampled=sampled)
        else:
            one = build_histograms(ds, [position], rolling_ball_radius=rolling_ball_radius,
                                   overlay_mask=overlay_mask, channels=channels, cancel=cancel,
                                   workers=workers, sampled=sampled)
        per_position[position] = one
        merged = merged.merge(one)
    merged.total_positions = len(ds.groups)
    merged.fingerprint = ds.source_fingerprint
    merged.rolling_ball_radius = rolling_ball_radius
    return merged, per_position
