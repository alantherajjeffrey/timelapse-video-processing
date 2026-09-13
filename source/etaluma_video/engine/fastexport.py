"""Single-pass timelapse export: decode each TIFF once, render every video variant from it.

Why this exists (0.5): 0.4 re-read every TIFF once per video it appeared in, so a three-channel
position cost 11 LZW decodes per timepoint (about 52 ms each), rendered every frame at the full
1900 px and then shrank it through Pillow (about 49 ms per frame per video).

Here one position is streamed once:

* the TIFFs of upcoming timepoints are decoded on a thread pool (tifffile/imagecodecs release the
  GIL) and each plane is shrunk to the output width immediately (``cv2.INTER_AREA``);
* every requested variant (composite with WHITE, fluorescence-only composite, single channels) is
  rendered from those shared, already-small planes through ``engine.display`` (the same functions
  the preview uses). 0.6: a whole timepoint (decode, rolling ball once per plane, every variant,
  overlays) is one task, and many timepoints are in flight at once; 0.5 rendered one timepoint at
  a time and spent 31 s per 1900 px CD14 position waiting on it;
* each video has its own writer thread, so MJPG and H.264 encoding overlap with rendering;
* 0.6: the time, name and scale-bar overlays are copied in from pre-drawn patches
  (``engine.overlays``); a small poster JPEG of each video's middle frame is saved for the results
  page; the montage tiles (composite, fluorescence-only, each channel at 0/25/50/75/100 %) are
  rendered on the way with the video's own bounds; x264 gets the machine's cores instead of 0.5's
  fixed 2 threads, which had capped a 1900 px stream at about 14 frames per second.

The rolling-ball radius is scaled to the output width, exactly as the live preview does; bounds
are measured on full-resolution planes.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from . import display
from .export import VIDEO_QUALITY, _fourcc, _Mp4Pipe, _verify_video, playback_fps
from .histograms import default_workers
from .jobs import PROGRESS_PREFIX, JobCancelled, check_cancel
from .models import CHANNEL_ORDER, FLUOR_CHANNELS, DisplayProfile
from .overlays import OverlayPainter, format_elapsed
from .parsing import read_plane

VIDEO_COMPOSITE = "composite"
VIDEO_FLUOR = "fluorescence_only"
FLUOR_NAME = "fluorescence"  # file-name part of the fluorescence-only composite (0.5: composite_fluorescence_only)
SINK_QUEUE = 8
POSTER_PX = 480
MONTAGE_PERCENTS = (0, 25, 50, 75, 100)


def output_size(src_h: int, src_w: int, width: int | None) -> tuple[int, int]:
    """(width, height) of the written video at the requested width, both even (H.264 needs even sizes).

    Like 0.4, a small capture is scaled up to the requested width; LS720 captures (1900 px) are only
    ever scaled down (950, 640) or kept (1900).
    """
    w = src_w if not width else int(width)
    w = max(2, w // 2 * 2)
    h = max(2, int(round(src_h * w / src_w)) // 2 * 2)
    return w, h


def scaled_profile(profile: DisplayProfile, scale: float) -> DisplayProfile:
    """The profile with its rolling-ball radius scaled to the output resolution (as the preview does)."""
    if profile.rolling_ball.enabled and abs(scale - 1.0) > 1e-6:
        p = profile.copy()
        p.rolling_ball.radius_px = max(1, int(round(p.rolling_ball.radius_px * scale)))
        return p
    return profile


def encoder_threads(streams: int) -> int:
    """x264 threads per video when ``streams`` videos are encoded side by side."""
    return max(2, (os.cpu_count() or 4) // max(1, int(streams)))


# --------------------------------------------------------------------------- #
# Video sinks: one writer thread per video, AVI and/or MP4
# --------------------------------------------------------------------------- #


class VideoSink:
    """Writes RGB frames to ``<base>.avi`` and/or ``<base>.mp4`` from its own thread."""

    def __init__(self, base: Path, fps: float, size: tuple[int, int], avi: bool, mp4: bool,
                 crf: int = VIDEO_QUALITY["standard"], threads: int = 0) -> None:
        self.base = Path(base)
        self.fps = float(fps)
        self.size = size
        self.avi_path = self.base.with_name(self.base.name + ".avi")
        self.mp4_path = self.base.with_name(self.base.name + ".mp4")
        self.count = 0
        self.error: BaseException | None = None
        self._writer = None
        self._pipe = None
        self.base.parent.mkdir(parents=True, exist_ok=True)
        if avi:
            self._writer = cv2.VideoWriter(str(self.avi_path), _fourcc("MJPG"), self.fps, size)
            if not self._writer.isOpened():
                raise RuntimeError(f"MJPG writer unavailable: {self.avi_path}")
        if mp4:
            self._pipe = _Mp4Pipe(self.mp4_path, size, self.fps, crf=crf, threads=threads)
        self._queue: queue.Queue = queue.Queue(maxsize=SINK_QUEUE)
        self._thread = threading.Thread(target=self._run, name=f"sink {self.base.name}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            rgb = self._queue.get()
            if rgb is None:
                return
            if self.error is not None:
                continue
            try:
                if self._writer is not None:
                    self._writer.write(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
                if self._pipe is not None:
                    self._pipe.write(rgb)  # ndarray.tobytes() is rgb24
                self.count += 1
            except BaseException as exc:  # reported by put()/close()
                self.error = exc

    def put(self, rgb: np.ndarray) -> None:
        if self.error is not None:
            raise RuntimeError(f"Writing {self.base.name} failed: {self.error}") from self.error
        if (rgb.shape[1], rgb.shape[0]) != self.size:
            raise ValueError(f"Image dimensions change within {self.base.name}")
        self._queue.put(np.ascontiguousarray(rgb))

    def close(self, cancel=None) -> None:
        self._queue.put(None)
        self._thread.join()
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        if self.error is not None:
            self.abort()
            raise RuntimeError(f"Writing {self.base.name} failed: {self.error}") from self.error
        if self._pipe is not None:
            self._pipe.close(cancel)
            self._pipe = None

    def abort(self) -> None:
        try:
            if self._thread.is_alive():
                while True:  # drain so the thread sees the stop marker
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        break
                self._queue.put(None)
                self._thread.join(timeout=10)
        finally:
            if self._writer is not None:
                self._writer.release()
                self._writer = None
            if self._pipe is not None:
                self._pipe.kill()
                self._pipe = None
            self.avi_path.unlink(missing_ok=True)
            self.mp4_path.unlink(missing_ok=True)

    def files(self) -> list[Path]:
        return [p for p in (self.avi_path, self.mp4_path) if p.exists()]


# --------------------------------------------------------------------------- #
# Planning which videos (and montage rows) a position gets
# --------------------------------------------------------------------------- #


@dataclass
class VideoPlan:
    name: str  # file stem after "<experiment>_<roi>_"
    variant: str  # VIDEO_COMPOSITE | VIDEO_FLUOR | channel
    members: list[str]
    serials: list[int]
    fps: float = 0.0
    sink: VideoSink | None = None
    written: list[dict] = field(default_factory=list)


@dataclass
class MontageRow:
    key: str  # "composite" | "fluorescence" | channel
    label: str
    variant: str
    members: list[str]
    colour: tuple[float, float, float] | None = None


def _enabled_channels(present: list[str], profile: DisplayProfile) -> list[str]:
    return [c for c in present if (c == "WHITE" and profile.white.enabled)
            or (c in FLUOR_CHANNELS and c in profile.channels and profile.channels[c].enabled)]


def plan_videos(by_serial: dict[str, dict[int, object]], profile: DisplayProfile, *,
                composite: bool, fluorescence_only: bool, channel_videos) -> tuple[list[VideoPlan], list[str]]:
    """Which videos one position gets, and the warnings about omitted timepoints.

    channel_videos: channels that get their own video (None = every channel present).
    A position with a single displayable channel gets that channel's video when the composite is
    requested, so WHITE-only experiments still produce a video.
    """
    present = [c for c in CHANNEL_ORDER if c in by_serial]
    wanted_single = present if channel_videos is None else [c for c in present if c in set(channel_videos)]
    plans: list[VideoPlan] = [VideoPlan(ch, ch, [ch], sorted(by_serial[ch])) for ch in wanted_single]
    warnings: list[str] = []
    enabled = _enabled_channels(present, profile)
    fluor = [c for c in enabled if c in FLUOR_CHANNELS]

    def common(members: list[str], label: str) -> list[int]:
        sets = [set(by_serial[m]) for m in members]
        shared = sorted(set.intersection(*sets))
        union = set.union(*sets)
        if len(shared) != len(union):
            warnings.append(f"{label}: using {len(shared)} synchronized timepoints; "
                            f"{len(union) - len(shared)} incomplete timepoints omitted.")
        return shared

    if composite:
        if len(enabled) > 1:
            serials = common(enabled, "composite")
            if serials:
                plans.append(VideoPlan("composite", VIDEO_COMPOSITE, enabled, serials))
            else:
                warnings.append("composite: no shared timepoints; video skipped.")
        elif len(enabled) == 1 and enabled[0] not in wanted_single:
            ch = enabled[0]
            plans.append(VideoPlan(ch, ch, [ch], sorted(by_serial[ch])))
    if fluorescence_only and fluor and "WHITE" in enabled:
        serials = common(fluor, FLUOR_NAME) if len(fluor) > 1 else sorted(by_serial[fluor[0]])
        if serials:
            plans.append(VideoPlan(FLUOR_NAME, VIDEO_FLUOR, fluor, serials))
    return plans, warnings


def plan_montage(by_serial: dict[str, dict[int, object]], profile: DisplayProfile,
                 names: dict[str, str] | None = None) -> tuple[list[MontageRow], list[int]]:
    """Montage rows (composite, fluorescence-only, each channel) and its evenly spaced timepoints."""
    present = [c for c in CHANNEL_ORDER if c in by_serial]
    if not present:
        return [], []
    enabled = _enabled_channels(present, profile)
    fluor = [c for c in enabled if c in FLUOR_CHANNELS]
    rows: list[MontageRow] = []
    if len(enabled) > 1:
        rows.append(MontageRow("composite", "Composite", VIDEO_COMPOSITE, enabled))
    if fluor and "WHITE" in enabled:
        rows.append(MontageRow(FLUOR_NAME, "Fluorescence", VIDEO_FLUOR, fluor))
    for ch in enabled or present:
        if ch == "WHITE":
            colour = (0.85, 0.85, 0.85)
        else:
            cd = profile.channels.get(ch)
            colour = tuple(float(c) for c in cd.colour) if cd is not None else (1.0, 1.0, 1.0)
        label = f"{ch} · {names[ch]}" if names and names.get(ch) else ch
        rows.append(MontageRow(ch, label, ch, [ch], colour))
    base = sorted(set.intersection(*(set(by_serial[m]) for m in rows[0].members))) or \
        sorted(set.union(*(set(v) for v in by_serial.values())))
    picks: list[int] = []
    for pct in MONTAGE_PERCENTS:
        target = base[0] + (base[-1] - base[0]) * pct / 100
        s = min(base, key=lambda x: abs(x - target))
        if s not in picks:
            picks.append(s)
    return rows, picks


# --------------------------------------------------------------------------- #
# The single pass
# --------------------------------------------------------------------------- #


def _decode(frame, channel: str, size: tuple[int, int]):
    plane = read_plane(frame.path, channel)
    h, w = plane.shape
    small = plane if (w, h) == size else cv2.resize(plane, size, interpolation=cv2.INTER_AREA)
    return plane.shape, small


def _render_variant(variant: str, members: list[str], planes: dict, profile, bounds) -> np.ndarray | None:
    sub = {m: planes[m] for m in members if m in planes}
    if not sub:
        return None
    if variant == VIDEO_COMPOSITE:
        return display.render_frame_fast(sub, profile, bounds, include_white=True)
    if variant == VIDEO_FLUOR:
        return display.render_frame_fast(sub, profile, bounds, include_white=False)
    return display.render_single_channel_fast(sub[variant], variant, profile, bounds)


def _without_rolling_ball(profile: DisplayProfile) -> DisplayProfile:
    if not profile.rolling_ball.enabled:
        return profile
    p = profile.copy()
    p.rolling_ball.enabled = False
    return p


def fit_width(rgb: np.ndarray, width: int) -> np.ndarray:
    """``rgb`` shrunk (or kept) to ``width`` pixels wide, aspect ratio kept."""
    h, w = rgb.shape[:2]
    if w == width:
        return rgb
    return cv2.resize(rgb, (int(width), max(1, round(h * width / w))),
                      interpolation=cv2.INTER_AREA if width < w else cv2.INTER_LINEAR)


def _save_poster(rgb: np.ndarray, path: Path) -> None:
    h, w = rgb.shape[:2]
    small = fit_width(rgb, min(w, round(POSTER_PX * w / max(h, w))))
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.ascontiguousarray(small)).save(path, quality=85)


def export_position(ds, roi: str, channels: dict[str, list], opts, profile: DisplayProfile, bounds, out_dir: Path, *,
                    pixel_size: float | None, interval: float | None, file_prefix: str | None = None,
                    poster_dir: Path | None = None, montage: bool = False, tile_width: int = 380,
                    overlays=None, label_um: float | None = None, show_days: bool = False,
                    time_offset: float = 0.0, progress=None, cancel=None, workers: int | None = None,
                    done_before: int = 0, grand_total: int = 0,
                    percent_range: tuple[int, int] = (0, 100)) -> dict:
    """Write every requested video of one position in one pass over its TIFFs.

    Files are ``<out_dir>/<file_prefix>_<kind>.mp4|.avi`` (file_prefix defaults to the position).
    Returns ``{"videos": [metadata per file], "warnings": [...], "montage": {"rows", "serials",
    "frames": {(row key, serial): tile RGB}}, "timepoints": n, "seconds": s, "size", "source_size"}``.
    """
    t0 = time.perf_counter()
    out_dir = Path(out_dir)
    prefix = file_prefix or roi
    by_serial = {ch: {f.serial: f for f in fs} for ch, fs in channels.items()}
    plans, warnings = plan_videos(by_serial, profile, composite=bool(opts.composite_videos),
                                  fluorescence_only=bool(getattr(opts, "fluorescence_only_video", True)),
                                  channel_videos=getattr(opts, "channel_videos", None))
    names = dict(getattr(opts, "channel_names", None) or {})
    rows, picks = plan_montage(by_serial, profile, names) if montage else ([], [])
    wanted_videos = bool(opts.avi or opts.mp4) and bool(plans)
    empty_montage = {"rows": [], "serials": [], "frames": {}}
    if not wanted_videos and not picks:
        return {"videos": [], "warnings": warnings, "montage": empty_montage, "timepoints": 0, "seconds": 0.0}

    # timeline: every serial any video or the montage needs, and which channels each needs
    need: dict[int, set[str]] = {}
    if wanted_videos:
        for plan in plans:
            for s in plan.serials:
                need.setdefault(s, set()).update(plan.members)
    for s in picks:
        need.setdefault(s, set()).update(ch for row in rows for ch in row.members if s in by_serial.get(ch, {}))
    need = {s: chs for s, chs in need.items() if chs}
    timeline = sorted(need)
    pick_set = set(picks)

    # output geometry from the first frame (every frame of a position must share it)
    first_serial = timeline[0]
    first_channel = sorted(need[first_serial], key=CHANNEL_ORDER.index)[0]
    first_frame = by_serial[first_channel][first_serial]
    src_h, src_w = read_plane(first_frame.path, first_channel).shape
    size = output_size(src_h, src_w, opts.width)
    scale = size[0] / float(src_w)
    render_profile = scaled_profile(profile, scale)
    out_pixel_size = pixel_size / scale if pixel_size else None
    rb_mask = None  # Lumaview's boxes at the output size, filled before the rolling ball
    if (render_profile.rolling_ball.enabled and overlays is not None and getattr(overlays, "detected", False)
            and overlays.image_shape and tuple(overlays.image_shape) == (src_h, src_w)):
        full_mask = overlays.mask(tuple(overlays.image_shape))
        rb_mask = full_mask if size == (src_w, src_h) else cv2.resize(
            full_mask.astype(np.uint8), size, interpolation=cv2.INTER_NEAREST).astype(bool)

    timed = bool(getattr(opts, "app_timestamp", False) and interval)

    def time_text(serial: int) -> str | None:
        return format_elapsed(time_offset + serial * interval, show_days) if timed else None

    painter = None
    wants_name = bool(getattr(opts, "name_label", False))
    wants_bar = bool(getattr(opts, "app_scale_bar", False) and out_pixel_size)
    if timed or wants_name or wants_bar:
        painter = OverlayPainter(size, (src_w, src_h), overlays, time=timed,
                                 time_template=time_text(timeline[-1]) or "",
                                 name=f"{ds.name} / {roi}" if wants_name else "",
                                 scale_bar=wants_bar, pixel_size=out_pixel_size, label_um=label_um)

    plan_keys: dict[int, list] = {}
    if getattr(opts, "channel_key", False):  # 0.7: colour and name of each channel, top left
        for plan in plans:
            plan_keys[id(plan)] = [(names.get(m) or m, (0.85, 0.85, 0.85) if m == "WHITE" else
                                    tuple(float(c) for c in (profile.channels[m].colour if m in profile.channels else (1, 1, 1))))
                                   for m in plan.members]
        if painter is None:
            painter = OverlayPainter(size, (src_w, src_h), overlays)

    # sinks
    workers = workers or default_workers()
    crf = VIDEO_QUALITY.get(getattr(opts, "video_quality", "standard"), VIDEO_QUALITY["standard"])
    threads = encoder_threads(len(plans))
    if wanted_videos:
        for plan in plans:
            first = by_serial[plan.members[0]][plan.serials[0]]
            plan.fps = opts.fps if getattr(opts, "playback_source", "duration") == "fps" and opts.fps else \
                playback_fps(ds, opts, first, len(plan.serials))
    plan_sets = {id(p): set(p.serials) for p in plans} if wanted_videos else {}
    poster_at = {id(p): p.serials[len(p.serials) // 2] for p in plans} if wanted_videos else {}
    posters: dict[int, Path] = {}
    montage_frames: dict[tuple[str, int], np.ndarray] = {}
    total = len(timeline)
    lo, hi = percent_range
    last_report = 0.0
    radius = render_profile.rolling_ball.radius_px if render_profile.rolling_ball.enabled else 0
    flat_profile = _without_rolling_ball(render_profile)  # the planes arrive background-subtracted

    problems: list[str] = []
    problems_lock = threading.Lock()

    def timepoint(s: int) -> tuple[dict, dict]:
        """Decode, subtract the background once per plane, render every variant needed at ``s``."""
        check_cancel(cancel)
        planes = {}
        for ch in need[s]:
            try:
                shape, small = _decode(by_serial[ch][s], ch, size)
            except JobCancelled:
                raise
            except Exception as exc:  # 0.7: one unreadable TIFF must not end a long run
                with problems_lock:
                    problems.append(f"{ch} timepoint {s}: {Path(by_serial[ch][s].path).name} could not be read "
                                    f"({type(exc).__name__}); a black frame stands in.")
                shape, small = (src_h, src_w), np.zeros((size[1], size[0]), dtype=np.uint8)
            if shape != (src_h, src_w):
                raise ValueError(f"Image dimensions change within {roi} ({ch}, timepoint {s})")
            if radius and ch in FLUOR_CHANNELS:
                small = display.subtract_background(small, radius, rb_mask)
            planes[ch] = small
        frames = {}
        if wanted_videos:
            label = time_text(s)
            for plan in plans:
                if s in plan_sets[id(plan)]:
                    rgb = _render_variant(plan.variant, plan.members, planes, flat_profile, bounds)
                    frames[id(plan)] = painter.draw(rgb, label, plan_keys.get(id(plan))) if painter is not None else rgb
        tiles = {}
        if s in pick_set:
            for row in rows:
                rgb = _render_variant(row.variant, row.members, planes, flat_profile, bounds)
                if rgb is not None:
                    tiles[row.key] = fit_width(rgb, tile_width)
        return frames, tiles

    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="timepoint") as pool:
            window: deque = deque()
            ahead = max(4, workers + 2)
            index = 0
            while index < min(ahead, total):
                window.append((timeline[index], pool.submit(timepoint, timeline[index])))
                index += 1
            for i in range(total):
                check_cancel(cancel)
                s, future = window.popleft()
                frames, tiles = future.result()
                if index < total:
                    window.append((timeline[index], pool.submit(timepoint, timeline[index])))
                    index += 1
                for plan in plans if wanted_videos else []:
                    rgb = frames.get(id(plan))
                    if rgb is None:
                        continue
                    if plan.sink is None:
                        plan.sink = VideoSink(out_dir / f"{prefix}_{plan.name}", plan.fps,
                                              (rgb.shape[1], rgb.shape[0]), bool(opts.avi), bool(opts.mp4),
                                              crf=crf, threads=threads)
                    if poster_dir is not None and poster_at[id(plan)] == s:
                        poster = Path(poster_dir) / f"{prefix}_{plan.name}.jpg"
                        _save_poster(rgb, poster)
                        posters[id(plan)] = poster
                    plan.sink.put(rgb)
                for key, tile in tiles.items():
                    montage_frames[(key, s)] = tile
                now = time.perf_counter()
                if progress and (i == total - 1 or now - last_report > 1.0):
                    last_report = now
                    msg = f"{roi}: {i + 1}/{total} timepoints"
                    if grand_total:
                        msg += f" … {int(lo + (hi - lo) * (done_before + i + 1) / grand_total)} %"
                    progress(PROGRESS_PREFIX + msg)
            for _s, future in window:  # nothing left on the normal path; after an error, stop quietly
                future.cancel()
        written: list[dict] = []
        for plan in plans if wanted_videos else []:
            if plan.sink is None:
                continue
            plan.sink.close(cancel)
            for path in (plan.sink.avi_path if opts.avi else None, plan.sink.mp4_path if opts.mp4 else None):
                if path is None:
                    continue
                _verify_video(path, len(plan.serials))
                record = {"file": path.name, "roi": roi, "kind": plan.name, "variant": plan.variant,
                          "frames": len(plan.serials), "fps": plan.fps, "size": [size[0], size[1]],
                          "bytes": path.stat().st_size, "serials": list(plan.serials)}
                poster = posters.get(id(plan))
                if poster is not None:
                    try:
                        record["poster"] = str(poster.relative_to(out_dir)).replace("\\", "/")
                    except ValueError:
                        record["poster"] = str(poster)
                written.append(record)
    except BaseException:
        for plan in plans:
            if plan.sink is not None:
                plan.sink.abort()
        raise
    warnings.extend(sorted(problems))
    montage_out = {"rows": [(r.key, r.label, r.colour) for r in rows], "serials": picks, "frames": montage_frames}
    return {"videos": written, "warnings": warnings, "montage": montage_out, "timepoints": total,
            "seconds": time.perf_counter() - t0, "size": list(size), "source_size": [src_w, src_h]}


__all__ = ["export_position", "plan_videos", "plan_montage", "VideoSink", "VideoPlan", "MontageRow",
           "output_size", "scaled_profile", "default_workers", "encoder_threads", "fit_width",
           "FLUOR_NAME", "MONTAGE_PERCENTS", "JobCancelled"]
