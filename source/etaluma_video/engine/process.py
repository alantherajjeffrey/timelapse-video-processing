"""The run: options, validation, and the pipeline that turns an experiment into a result folder.

``process_dataset`` is the only function that writes a run folder. It always creates a fresh
``analysis_output/run_<date>_<id>/`` (or ``quick_<date>_<id>/``) so nothing produced earlier
is ever overwritten, and it writes, per run (0.6 layout, decision 5):

* at the top: the videos, ``<experiment>_<position>_<kind>.mp4|.avi`` (timelapse), or the
  per-position composites and panels and ``<experiment>_quantification.csv`` (fixed images);
* ``montages/``: timepoint montages, plate overview and condition grids; ``masks/`` (fixed images);
* ``info/``: ``processing_log.txt``, ``display_profile.json``, ``calibration.json``,
  ``<experiment>_metadata.json``, ``_manifest.csv``, ``_frames.csv``, dashboard, info bar, posters.

Order of work: calibrate -> overlay mask -> WHITE preset -> sampled histogram pass over every
position (shared with the live preview through the disk cache; skipped in Manual timelapse mode)
-> effective bounds -> outputs. Videos, montages and panels all pass through
:mod:`etaluma_video.engine.display`; measurements use raw planes.

Channel selection vs. channel enabling: ``Options.channels`` chooses which channels get their
own video and which frames take part at all; ``DisplayProfile.channels[ch].enabled`` (and
``white.enabled``) chooses composite membership. Disabled channels are dropped *before* the
synchronized-serial intersection so they cannot shrink a composite.
"""
from __future__ import annotations

import errno
import json
import math
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from .. import VERSION
from . import calibration as calib
from . import display
from . import reports
from .histograms import SAMPLE_STEP, build_all_positions, build_histograms, first_white_median
from .jobs import PROGRESS_PREFIX, JobCancelled, ProgressFn, check_cancel, log_progress
from .models import (
    AUTO_METHODS,
    BLEND_MODES,
    CHANNEL_ORDER,
    OBJECTIVES,
    WHITE_PRESETS,
    Bounds,
    Calibration,
    DisplayProfile,
    HistogramSet,
    Overlays,
)
from .parsing import CHANNELS, Dataset, current_inventory_fingerprint, inventory, read_plane, summarize_warnings
from .quantify import quantify
from .export import MP4_BYTES_PER_PIXEL, VIDEO_QUALITY, export_video
from .fastexport import default_workers, export_position, fit_width, plan_videos
from .overlays import DAYS_MODES, OverlayPainter, format_elapsed, show_days
from .userdata import preview_cache_dir

__all__ = ["Options", "validate_options", "process_dataset", "process_batch", "estimate_output_bytes"]


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #


@dataclass
class Options:
    """Everything a run needs besides the dataset itself. The UI edits one of these."""

    # selection
    mode: str = "auto"  # "auto" follows Dataset.mode; "fixed" | "timelapse" override it
    channels: list[str] | None = None  # None: every channel present
    rois: list[str] | None = None  # None: every position

    # calibration
    objective: str | None = None  # manual override of the burned-in bar ("10x", ...)
    calibration: Calibration | None = None  # fully specified calibration (skips detection)

    # display
    profile: DisplayProfile = field(default_factory=DisplayProfile)
    lock_manual_profile_for_fixed: bool = False  # fixed mode: one profile instead of per-ROI auto
    exclude_overlays: bool = True  # keep Lumaview overlays out of histograms and measurements

    # playback
    fps: float | None = None
    playback_source: str = "duration"  # duration | fps | protocol | avs
    duration_seconds: float = 10.0
    interval_seconds: float | None = None

    # outputs
    width: int = 950  # rendered video width in pixels (1900 = native)
    avi: bool = True  # MJPG AVI
    mp4: bool = True  # H.264 MP4
    quick: bool = False  # writes quick_<date>_<id>/ instead of run_<date>_<id>/
    composite_videos: bool = True  # composite with the WHITE underlay
    fluorescence_only_video: bool = True  # composite without WHITE (when WHITE and fluorescence exist)
    channel_videos: list[str] | None = None  # channels that get their own video; None = every channel
    montages: bool = True
    dashboard: bool = True
    app_scale_bar: bool = False  # covers Lumaview's bar with a readable one; off by default
    app_timestamp: bool = False  # elapsed time, covering Lumaview's clock box (bottom left)
    channel_label: bool = False
    time_offset_seconds: float = 0.0  # added to every time label (one experiment split over folders)
    time_days: str = "auto"  # auto | always | never: the "Day d :" part of the time label
    name_label: bool = False  # "<experiment folder> / <position>" beside the time, bottom left
    video_quality: str = "standard"  # MP4: high (CRF 18) | standard (CRF 23) | small (CRF 28)
    channel_names: dict[str, str] = field(default_factory=dict)  # e.g. {"F2": "CFDA"}: montage rows, video key (0.7)
    channel_key: bool = False  # small colour key of the channels, top left of the videos (0.7)
    timepoint_range: list[int] | None = None  # [first, last] timepoint numbers, 1-based, inclusive; None = all (0.7)
    tile_width: int = 380
    plate_columns: int = 4
    workers: int = 0  # TIFF decode threads; 0 = automatic (logical cores - 2)

    # measurements (fixed mode)
    threshold: float = 50.0
    thresholds: dict[str, float] = field(default_factory=dict)
    exclude_bottom_px: int = 0
    conditions: dict[str, str] = field(default_factory=dict)
    grouping: str = "descriptor"

    @property
    def video_width(self) -> int:
        """Codex 0.3 name for :attr:`width`."""
        return self.width

    @classmethod
    def quick_options(cls, profile: DisplayProfile | None = None) -> "Options":
        """Decision 8: full pipeline, no preview step, Auto-normalised display, 1900 px, MP4 only (0.6: time and name on)."""
        p = profile.copy() if profile is not None else DisplayProfile(mode="auto", auto_method="adaptive")
        return cls(profile=p, width=1900, avi=False, mp4=True, quick=True,
                   app_scale_bar=False, app_timestamp=True, name_label=True, channels=None, rois=None)


# ``quick`` is a field name, so the Quick-video constructor is ``Options.quick_options`` and this
# module-level alias; both take an optional starting profile.
def quick_options(profile: DisplayProfile | None = None) -> Options:
    """Options for the one-click Quick video (1900 px, MP4 only, Auto-normalised display, all positions)."""
    return Options.quick_options(profile)


def validate_options(ds: Dataset, opts: Options) -> None:
    """Raise ValueError with a sentence the user can act on; never a traceback in the log."""
    if opts.objective is not None and opts.objective not in OBJECTIVES:
        raise ValueError(f"Select a supported objective: {', '.join(OBJECTIVES)}.")
    if opts.mode not in ("auto", "fixed", "timelapse"):
        raise ValueError("Select a supported mode: auto, fixed or timelapse.")
    if opts.playback_source not in ("duration", "fps", "protocol", "avs"):
        raise ValueError("Choose total duration, fixed FPS, protocol duration or AVS playback timing.")
    if not math.isfinite(opts.duration_seconds) or opts.duration_seconds <= 0:
        raise ValueError("Video duration must be a positive number of seconds.")
    if any(not math.isfinite(t) or not 0 <= t <= 255 for t in [opts.threshold, *opts.thresholds.values()]):
        raise ValueError("Thresholds must lie between 0 and 255.")
    for value in [opts.fps, opts.interval_seconds]:
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("FPS and interval must be positive numbers.")
    if not 160 <= opts.width <= 4096 or opts.width % 2:
        raise ValueError("Video width must be even, from 160 to 4096 pixels.")
    if opts.video_quality not in VIDEO_QUALITY:
        raise ValueError(f"Video quality must be one of {', '.join(VIDEO_QUALITY)}.")
    if opts.time_days not in DAYS_MODES:
        raise ValueError("Days in the time label must be auto, always or never.")
    if not math.isfinite(opts.time_offset_seconds):
        raise ValueError("The start time must be a finite number of seconds.")
    if opts.timepoint_range is not None:
        first, last = (int(v) for v in opts.timepoint_range)
        if first < 1 or last < first:
            raise ValueError("The timepoint range must run from a first to a later timepoint, starting at 1.")
    if not 160 <= opts.tile_width <= 1200 or not 1 <= opts.plate_columns <= 12 or opts.exclude_bottom_px < 0:
        raise ValueError("Invalid tile, plate, or measurement crop settings.")
    if opts.channels is not None and (not opts.channels or set(opts.channels) - set(ds.channels)):
        raise ValueError("Choose at least one channel present in this experiment.")
    if opts.rois is not None and (not opts.rois or set(opts.rois) - set(ds.groups)):
        raise ValueError("Choose at least one ROI present in this experiment.")
    valid_keys = set(ds.groups) | {f.id for f in ds.frames}
    if set(opts.conditions) - valid_keys:
        raise ValueError(f"Unknown condition IDs: {', '.join(sorted(set(opts.conditions) - valid_keys))}")
    profile = opts.profile
    if profile is not None:
        if profile.auto_method not in AUTO_METHODS:
            raise ValueError(f"Auto-normalised method must be one of {', '.join(AUTO_METHODS)}.")
        if profile.blend not in BLEND_MODES:
            raise ValueError(f"Blend mode must be one of {', '.join(BLEND_MODES)}.")
        if profile.rolling_ball.enabled and not 1 <= profile.rolling_ball.radius_px <= 500:
            raise ValueError("Rolling-ball radius must lie between 1 and 500 pixels.")
        for ch, cd in profile.channels.items():
            if ch not in CHANNELS:
                raise ValueError(f"Unknown channel in the display profile: {ch}")
            if not 0 <= cd.low < 256 or not 0 < cd.high <= 255 or cd.high <= cd.low:
                raise ValueError(f"{ch}: display bounds must satisfy 0 <= low < high <= 255.")
            if not 0.05 <= cd.gamma <= 5:
                raise ValueError(f"{ch}: gamma must lie between 0.05 and 5.")
        if not 0 <= profile.white.weight <= 1:
            raise ValueError("WHITE contribution must lie between 0 and 1.")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _resolve_calibration(ds: Dataset, opts: Options) -> tuple[Overlays, Calibration]:
    """Overlays always come from the capture; the pixel size may be overridden by the user."""
    try:
        overlays, detected = calib.calibrate(ds)
    except Exception as exc:  # a calibration failure must never stop a run
        return Overlays(), Calibration(source="none", message=f"calibration failed: {exc}")
    if opts.calibration is not None:
        return overlays, opts.calibration
    if opts.objective:
        manual = Calibration.manual(opts.objective)
        if detected.pixel_size_um:
            manual.bar_px, manual.label_um, manual.label_objective = detected.bar_px, detected.label_um, detected.label_objective
            manual.message += f"; burned-in bar read {detected.pixel_size_um:.3f} µm/px"
            manual.disagreement = abs(detected.pixel_size_um / manual.pixel_size_um - 1.0) > 0.05
        manual.source_frame = detected.source_frame
        return overlays, manual
    return overlays, detected


def _prepared_profile(opts: Options, ds: Dataset) -> DisplayProfile:
    """A private copy of the profile with one entry per fluorescence channel present."""
    profile = (opts.profile or DisplayProfile()).copy()
    from .models import COLOUR_PRESETS, ChannelDisplay, FLUOR_CHANNELS

    preset = COLOUR_PRESETS.get(profile.colour_preset, COLOUR_PRESETS["CGM"])
    for ch in ds.channels:
        if ch in FLUOR_CHANNELS and ch not in profile.channels:
            profile.channels[ch] = ChannelDisplay(colour=preset[ch])
    for ch in list(profile.channels):
        if ch not in ds.channels:
            del profile.channels[ch]
    return profile


def _bounds_dict(bounds: Bounds) -> dict:
    return {ch: [float(lo), float(hi)] for ch, (lo, hi) in bounds.items()}


def _enabled(channels: dict, profile: DisplayProfile) -> dict:
    """Channels of one position that take part in a composite, in canonical order."""
    out = {}
    for ch in CHANNEL_ORDER:
        if ch not in channels:
            continue
        if ch == "WHITE" and profile.white.enabled:
            out[ch] = channels[ch]
        elif ch in profile.channels and profile.channels[ch].enabled:
            out[ch] = channels[ch]
    return out


def estimate_output_bytes(ds: Dataset, opts: Options) -> dict:
    """Rough output size before a run starts (logged so a 40 GB mistake can be cancelled).

    MJPG is about 0.5 bytes per rendered pixel (0.3 to 1.0 observed on 0.3 runs at
    950 px). H.264 follows the quality preset (``export.MP4_BYTES_PER_PIXEL``, measured on CD14);
    0.5 assumed a tenth of MJPG and predicted 425 MB for a run that wrote 2.3 GB.
    """
    mode = ds.mode if opts.mode == "auto" else opts.mode
    groups = ds.groups
    rois = [r for r in groups if opts.rois is None or r in opts.rois]
    channels = opts.channels or ds.channels
    profile = opts.profile or DisplayProfile()
    videos, frames = 0, 0
    allowed = None
    if opts.timepoint_range is not None:
        serials = sorted({f.serial for f in ds.frames})
        allowed = set(serials[int(opts.timepoint_range[0]) - 1:int(opts.timepoint_range[1])])
    if mode == "timelapse":
        prof = _prepared_profile(opts, ds)
        for roi in rois:
            present = {ch: fs for ch, fs in groups[roi].items() if ch in channels}
            by_serial = {ch: {f.serial: f for f in fs if allowed is None or f.serial in allowed}
                         for ch, fs in present.items()}
            by_serial = {ch: frames_ for ch, frames_ in by_serial.items() if frames_}
            if not by_serial:
                continue
            plans, _warnings = plan_videos(by_serial, prof, composite=bool(opts.composite_videos),
                                           fluorescence_only=bool(opts.fluorescence_only_video),
                                           channel_videos=opts.channel_videos)
            videos += len(plans)
            frames += sum(len(p.serials) for p in plans)
    per_frame = 0.5 * opts.width ** 2
    avi = int(frames * per_frame) if opts.avi else 0
    mp4_rate = MP4_BYTES_PER_PIXEL.get(opts.video_quality, MP4_BYTES_PER_PIXEL["standard"])
    mp4 = int(frames * mp4_rate * opts.width ** 2) if opts.mp4 else 0
    low = int(frames * 0.3 * opts.width ** 2) if opts.avi else 0
    high = int(frames * 1.0 * opts.width ** 2) if opts.avi else 0
    total = avi + mp4
    def mb(v):
        return v / 1_000_000
    text = (f"Estimated output: {videos} videos, {frames} rendered frames at {opts.width} px -> "
            + (f"AVI {mb(avi):.0f} MB (range {mb(low):.0f}-{mb(high):.0f} MB)" if opts.avi else "no AVI")
            + (f", MP4 ({opts.video_quality}) {mb(mp4):.0f} MB" if opts.mp4 else ", no MP4")
            + f"; total about {mb(total):.0f} MB")
    return {"videos": videos, "frames": frames, "bytes_per_frame": per_frame,
            "avi_bytes": avi, "avi_bytes_low": low, "avi_bytes_high": high,
            "mp4_bytes": mp4, "total_bytes": total, "text": text}


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


def process_dataset(ds: Dataset, opts: Options, output: Path | str | None = None,
                    progress: ProgressFn | None = None, cancel=None,
                    histograms: HistogramSet | None = None) -> dict:
    """Produce one run folder. Returns a summary dict including its path.

    histograms: a partial HistogramSet already measured by the UI; the positions it does not
    cover are measured here, so the exported bounds always cover every position.
    """
    started = time.perf_counter()
    validate_options(ds, opts)
    progress = progress or log_progress
    mode = ds.mode if opts.mode == "auto" else opts.mode
    selected = [f for f in ds.frames
                if (opts.channels is None or f.channel in opts.channels)
                and (opts.rois is None or f.roi in opts.rois)]
    all_serials = sorted({f.serial for f in ds.frames})
    range_note = ""
    if opts.timepoint_range is not None and len(all_serials) > 1:  # 0.7: export a range; bounds still use all
        first, last = (int(v) for v in opts.timepoint_range)
        allowed = set(all_serials[first - 1:last])
        selected = [f for f in selected if f.serial in allowed]
        range_note = (f"Timepoints {first}-{min(last, len(all_serials))} of {len(all_serials)} "
                      "(the Auto-normalised display still uses every timepoint)")
    if not selected:
        raise ValueError("No images in this selection.")
    interval = opts.interval_seconds if opts.interval_seconds is not None else ds.interval
    parent = Path(output).expanduser().resolve() if output else Path(ds.root) / "analysis_output"
    root = Path(ds.root).resolve()
    if parent == root or parent in root.parents:
        raise ValueError("Output cannot be the source folder or its ancestor.")
    prefix = "quick" if opts.quick else "run"
    out = parent / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    info = out / "info"
    try:
        info.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise ValueError(f"Cannot create the output folder in {parent}: {exc.strerror or exc}. "
                         "Choose another output folder in the Output panel.") from exc

    meta = {"schema_version": 2, "app_version": VERSION, "status": "running",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "dataset": {"root": ds.root, "name": ds.name, "layout": ds.layout, "mode": ds.mode,
                        "n_rois": len(ds.groups), "n_images": len(ds.frames), "channels": ds.channels,
                        "interval_seconds": ds.interval, "inventory": inventory(ds), "warnings": ds.warnings,
                        "objective_metadata": ds.objective_metadata},
            "effective_mode": mode, "effective_interval_seconds": interval,
            "options": asdict(opts), "quick": bool(opts.quick),
            "channels": CHANNELS, "protocols": ds.protocols, "avs": ds.avs, "positions": ds.positions,
            "display": {}, "calibration": {}, "overlays": {}, "videos": [],
            "outputs": [], "warnings": list(ds.warnings),
            "measurement": "Native 8-bit values, intensity > threshold. Positive objects are not validated spheroids.",
            "composite_method": "display.render_frame: per-channel LUT, colour, screen/additive/max blend, "
                                "WHITE screen underlay; montages and panels render the same way; measurements use raw planes",
            "timing_policy": "serial * interval; available frames only; gaps are not filled"}
    if mode == "fixed" and ds.mode == "timelapse":
        meta["warnings"].append("Manual fixed-image override: first available frame per channel is measured and rendered.")
    meta_path = info / f"{ds.name}_metadata.json"
    log_path = info / "processing_log.txt"

    def save_meta() -> None:
        meta_path.write_text(_json_dumps(meta), encoding="utf-8")

    def log(message: str) -> None:
        check_cancel(cancel)
        if message.startswith(PROGRESS_PREFIX):  # progress bar only: not worth a line in the file
            progress(message)
            return
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().strftime('%H:%M:%S')}  {message}\n")
        progress(message)

    def save_image(img: Image.Image, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path)
        meta["outputs"].append(str(path.relative_to(out)))

    save_meta()
    try:
        log(f"Timelapse Video Processing {VERSION} | {ds.name} | {mode} | {len(selected)} images")
        log(f"Writing to {out}")
        if range_note:
            log(range_note)
        for warning in summarize_warnings(meta["warnings"]):
            log("NOTE: " + warning)
        if opts.app_timestamp and not interval:
            msg = "No capture interval is known, so the videos carry no time."
            meta["warnings"].append(msg)
            log("NOTE: " + msg)

        # ---- calibration and overlays ---------------------------------- #
        overlays, calibration = _resolve_calibration(ds, opts)
        meta["calibration"], meta["overlays"] = calibration.to_dict(), overlays.to_dict()
        log(f"Calibration: {calibration.message or calibration.source}")
        if calibration.disagreement:
            msg = "Scale bar and objective table disagree by more than 5 %; the bar wins."
            meta["warnings"].append(msg)
            log("NOTE: " + msg)
        pixel_size = calibration.pixel_size_um if calibration.usable else None
        if pixel_size is None:
            msg = "No usable pixel size: scale bars are omitted and µm² columns are not computed."
            meta["warnings"].append(msg)
            log("NOTE: " + msg)
        mask = None
        if opts.exclude_overlays and overlays.detected and overlays.image_shape:
            mask = overlays.mask(overlays.image_shape)
            log(f"Overlay exclusion: {int(mask.sum())} px masked out of histograms and measurements")

        # ---- display profile ------------------------------------------- #
        profile = _prepared_profile(opts, ds)
        white_median = None
        if "WHITE" in ds.channels and profile.white.preset_auto:
            white_median = first_white_median(ds, mask)
            if white_median is not None:
                profile.white.preset = display.white_preset_for_median(white_median)
                preset = WHITE_PRESETS[profile.white.preset]
                profile.white.gamma, profile.white.weight = preset["gamma"], preset["weight"]
                log(f"WHITE preset: {profile.white.preset} (first-frame median {white_median:g})")

        # ---- histograms and bounds ------------------------------------- #
        radius = profile.rolling_ball.radius_px if profile.rolling_ball.enabled else None
        measure_share = 0
        if profile.mode == "manual" and mode == "timelapse":
            hists = HistogramSet(total_positions=len(ds.groups), rolling_ball_radius=radius,
                                 fingerprint=ds.source_fingerprint)
            per_position: dict[str, HistogramSet] = {}
            auto: Bounds = {}
            log("Display: manual bounds, so no intensity measurement is needed.")
        else:
            measure_share = 10
            workers = opts.workers or default_workers()
            hist_started = time.perf_counter()

            def on_position(i: int, n: int, position: str) -> None:
                log(PROGRESS_PREFIX + f"Measuring intensities: {position} ({i + 1}/{n}) … "
                    f"{int(measure_share * i / max(1, n))} %")

            hists, per_position = build_all_positions(ds, partial=histograms, rolling_ball_radius=radius,
                                                      overlay_mask=mask, cancel=cancel, cache_dir=preview_cache_dir(),
                                                      workers=workers, sampled=True, on_position=on_position)
            images = sum(h.frames for h in hists.channels.values())
            log(f"Intensities: {images} images over {len(ds.groups)} positions (every {SAMPLE_STEP}th timepoint, "
                f"kept for the next run) in {time.perf_counter() - hist_started:.1f} s")
            auto = display.auto_bounds_all(hists, profile.auto_method)
        bounds = display.effective_bounds(profile, auto)
        log("Display bounds: " + ", ".join(f"{ch} {lo:.0f}-{hi:.0f}" for ch, (lo, hi) in bounds.items()))
        meta["display"] = {"profile": profile.to_dict(), "effective_bounds": _bounds_dict(bounds),
                           "auto_bounds": _bounds_dict(auto), "auto_method": profile.auto_method,
                           "white_first_frame_median": white_median,
                           "rolling_ball_radius": radius,
                           "histogram_positions": hists.positions, "histogram_frames":
                               {ch: h.frames for ch, h in hists.channels.items()}}
        (info / "display_profile.json").write_text(
            _json_dumps({**profile.to_dict(), "effective_bounds": _bounds_dict(bounds),
                         "auto_bounds": _bounds_dict(auto)}), encoding="utf-8")
        (info / "calibration.json").write_text(
            _json_dumps({**calibration.to_dict(), "overlays": overlays.to_dict()}), encoding="utf-8")

        # ---- manifests -------------------------------------------------- #
        sub = Dataset(ds.root, ds.layout, mode, selected, ds.protocols, ds.avs, ds.positions, ds.warnings)
        reports.write_csv(info / f"{ds.name}_manifest.csv",
                          reports.manifest_rows(sub, selected, opts, calibration, interval))
        reports.write_csv(info / f"{ds.name}_frames.csv",
                          reports.frame_rows(selected, root, calibration, interval))
        if opts.dashboard:
            save_image(reports.dashboard(ds, opts, mode, len(selected), calibration, profile),
                       info / f"{ds.name}_dashboard.png")
            save_image(reports.info_bar(ds, sub, opts, mode, len(selected), calibration, interval),
                       info / f"{ds.name}_info_bar.png")

        if opts.montages and pixel_size is None:
            log("NOTE: montage tiles carry no scale bar because the pixel size is unknown.")

        quant_rows: list[dict] = []
        plate_tiles: list[tuple] = []
        grand_total = sum(len({f.serial for fs in chs.values() for f in fs}) for chs in sub.groups.values())
        done_timepoints = 0
        last_serial = max(f.serial for f in selected)
        days = show_days(opts.time_days, opts.time_offset_seconds + last_serial * interval) if interval else False
        for ri, (roi, channels) in enumerate(sub.groups.items()):
            check_cancel(cancel)
            log(f"Processing {roi} ({ri + 1}/{len(sub.groups)})")
            if mode == "fixed":
                _fixed_position(ds, opts, profile, roi, channels, out, save_image, log,
                                calibration, pixel_size, mask, per_position, hists, bounds,
                                quant_rows, plate_tiles, cancel, overlays=overlays)
            else:
                _timelapse_position(ds, opts, profile, bounds, roi, channels, out, save_image, log,
                                    meta, pixel_size, interval, cancel, done_timepoints, grand_total,
                                    overlays=overlays, calibration=calibration, days=days,
                                    percent_start=measure_share)
                done_timepoints += len({f.serial for fs in channels.values() for f in fs})
        if plate_tiles and opts.montages:
            plate_tiles.sort(key=lambda t: (t[0], t[1]))
            save_image(reports.grid([t[2] for t in plate_tiles], opts.plate_columns,
                                    f"{ds.name}  ·  plate overview"), out / "montages" / f"{ds.name}_plate_overview.png")
            for i, condition in enumerate(dict.fromkeys(t[0] for t in plate_tiles)):
                if condition:
                    save_image(reports.grid([t[2] for t in plate_tiles if t[0] == condition], opts.plate_columns,
                                            f"{ds.name}  ·  {condition}"), out / "montages" / f"{ds.name}_condition_{i + 1:02d}.png")
        if quant_rows:
            reports.write_csv(out / f"{ds.name}_quantification.csv", quant_rows)

        check_cancel(cancel)
        if ds.source_fingerprint and current_inventory_fingerprint(ds) != ds.source_fingerprint:
            raise ValueError("Source files changed during export; this run is incomplete. Inspect the experiment again.")
        meta["status"] = "complete"
        meta["processing_seconds"] = round(time.perf_counter() - started, 3)
        meta["outputs"] = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
        save_meta()
        log(f"Complete in {meta['processing_seconds']:.1f} seconds: {out}")
        return {"status": "complete", "output": out, "metadata_path": meta_path,
                "processing_seconds": meta["processing_seconds"], "videos": meta["videos"],
                "outputs": meta["outputs"], "warnings": meta["warnings"],
                "calibration": calibration, "profile": profile, "bounds": bounds,
                "histograms": hists, "experiment": ds.name}
    except BaseException as exc:
        meta["status"] = "cancelled" if isinstance(exc, JobCancelled) else "failed"
        meta["error"] = str(exc)
        meta["processing_seconds"] = round(time.perf_counter() - started, 3)
        meta["incomplete"] = True
        meta["outputs"] = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
        try:  # on a full disk even these small files may not fit
            (out / "INCOMPLETE.txt").write_text(
                "This run did not complete. Do not interpret these files as a completed analysis.\n" + str(exc),
                encoding="utf-8")
            save_meta()
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{meta['status'].upper()} after {meta['processing_seconds']:.1f} seconds: {exc}\n")
        except OSError:
            pass
        if _disk_full(exc):
            try:
                free = f"{shutil.disk_usage(out).free / 1e9:.1f} GB free"
            except OSError:
                free = "no space left"
            raise ValueError(f"The disk holding {out.parent} is full ({free}). Free some space or choose another "
                             "output folder; this run is incomplete.") from exc
        raise


def _disk_full(exc: BaseException | None) -> bool:
    """True when ``exc`` or one of its causes says the disk is full (ENOSPC, Windows error 112)."""
    for _ in range(6):
        if exc is None:
            return False
        if isinstance(exc, OSError) and (exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in (39, 112)):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _json_dumps(data) -> str:
    def default(o):
        if isinstance(o, Path):
            return str(o)
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        return str(o)

    return json.dumps(data, indent=2, ensure_ascii=False, default=default)


# --------------------------------------------------------------------------- #
# Per-position work
# --------------------------------------------------------------------------- #


def _display_footer(profile: DisplayProfile, bounds: Bounds) -> str:
    """The display settings in one line, printed under montages and panels."""
    method = {"adaptive": "Adaptive", "classic": "Classic", "cut": "Background cut"}.get(profile.auto_method,
                                                                                         profile.auto_method)
    mode = "Manual" if profile.mode == "manual" else f"Auto-normalised ({method})"
    return f"Display: {mode} · {display.profile_summary(profile, bounds)} · Timelapse Video Processing {VERSION}"


def _timelapse_position(ds, opts, profile, bounds, roi, channels, out, save_image, log, meta,
                        pixel_size, interval, cancel, done_before: int = 0, grand_total: int = 0, *,
                        overlays=None, calibration=None, days: bool = False, percent_start: int = 0) -> None:
    """Every video of one position in one pass over its TIFFs (engine.fastexport), then its montage."""
    result = export_position(ds, roi, channels, opts, profile, bounds, out,
                             pixel_size=pixel_size, interval=interval, file_prefix=f"{ds.name}_{roi}",
                             poster_dir=out / "info" / "posters", montage=bool(opts.montages),
                             tile_width=opts.tile_width, overlays=overlays,
                             label_um=getattr(calibration, "label_um", None), show_days=days,
                             time_offset=opts.time_offset_seconds, progress=log, cancel=cancel,
                             workers=opts.workers or default_workers(), done_before=done_before,
                             grand_total=grand_total, percent_range=(percent_start, 100))
    meta["videos"].extend(result["videos"])
    for warning in result["warnings"]:
        message = f"{roi} {warning}"
        meta["warnings"].append(message)
        log(message)
    montage = result.get("montage") or {}
    if montage.get("frames"):
        serials = montage["serials"]
        columns = [format_elapsed(opts.time_offset_seconds + s * interval, days) if interval else f"frame {s}"
                   for s in serials]
        images = {(ri, ci): montage["frames"][(key, s)]
                  for ri, (key, _label, _colour) in enumerate(montage["rows"])
                  for ci, s in enumerate(serials) if (key, s) in montage["frames"]}
        tile_px = pixel_size * result["source_size"][0] / opts.tile_width if pixel_size else None
        img = reports.timepoint_montage(f"{ds.name}  ·  {roi}",
                                        [(label, colour) for _key, label, colour in montage["rows"]],
                                        columns, images,
                                        subtitle=f"{len(serials)} timepoints spread over the timelapse",
                                        footer=_display_footer(profile, bounds), pixel_size=tile_px)
        save_image(img, out / "montages" / f"{ds.name}_{roi}_montage.png")
    if result["timepoints"]:
        log(f"{roi}: {result['timepoints']} timepoints, {len(result['videos'])} video files in {result['seconds']:.1f} s")


def _fixed_position(ds, opts, profile, roi, channels, out, save_image, log, calibration,
                    pixel_size, mask, per_position, hists, global_bounds, quant_rows, plate_tiles, cancel,
                    overlays=None) -> None:
    planes = {ch: read_plane(fs[0].path, ch) for ch, fs in channels.items()}
    first = next(iter(channels.values()))[0]
    condition = reports.condition_for(first, opts)
    bounds = _fixed_bounds(ds, opts, profile, roi, per_position, hists, global_bounds, mask, cancel)
    log(f"{roi} display bounds: " + ", ".join(f"{ch} {lo:.0f}-{hi:.0f}" for ch, (lo, hi) in bounds.items()))
    enabled = {ch: planes[ch] for ch in _enabled(channels, profile) if ch in planes}
    if not enabled:
        enabled = planes
    stem = f"{ds.name}_{roi}"
    h, w = next(iter(planes.values())).shape
    painter = OverlayPainter((w, h), (w, h), overlays, name=f"{ds.name} / {roi}" if opts.name_label else "",
                             scale_bar=bool(opts.app_scale_bar and pixel_size), pixel_size=pixel_size,
                             label_um=getattr(calibration, "label_um", None))
    composite = display.render_frame_fast(enabled, profile, bounds, include_white=True, background_mask=mask)
    save_image(Image.fromarray(painter.draw(composite.copy())), out / f"{stem}_composite.png")
    views = [("Composite", composite)]
    fluorescence = {ch: a for ch, a in enabled.items() if ch != "WHITE"}
    if "WHITE" in enabled and fluorescence:
        fluor_rgb = display.render_frame_fast(enabled, profile, bounds, include_white=False, background_mask=mask)
        save_image(Image.fromarray(painter.draw(fluor_rgb.copy())), out / f"{stem}_fluorescence.png")
        views.append(("Fluorescence", fluor_rgb))
    for ch in CHANNEL_ORDER:
        if ch in planes:
            views.append((ch, display.render_single_channel_fast(planes[ch], ch, profile, bounds, background_mask=mask)))
    if opts.montages:
        tiles = {(0, i): fit_width(rgb, opts.tile_width) for i, (_label, rgb) in enumerate(views)}
        tile_px = pixel_size * w / opts.tile_width if pixel_size else None
        title = f"{ds.name}  ·  {roi}" + (f"  ·  {condition}" if condition else "")
        save_image(reports.timepoint_montage(title, [("", None)], [label for label, _rgb in views], tiles,
                                             footer=_display_footer(profile, bounds), pixel_size=tile_px),
                   out / f"{stem}_panel.png")
        plate_tiles.append((condition, first.order,
                            reports.tile(composite, f"{roi} · {condition}" if condition else roi,
                                         opts.tile_width, pixel_size, bool(pixel_size))))
    measure_pixel = pixel_size if pixel_size else float("nan")
    for ch, a in planes.items():
        check_cancel(cancel)
        stats = quantify(a, opts.thresholds.get(ch, opts.threshold), measure_pixel, opts.exclude_bottom_px,
                         exclude_mask=mask if opts.exclude_overlays else None)
        quant_rows.append({"ROI": roi, "ID": first.id, "condition": condition, "channel": ch,
                           "serial": channels[ch][0].serial,
                           "objective": calibration.objective, "pixel_size_um": pixel_size,
                           "calibration_source": calibration.source,
                           "source_path": channels[ch][0].path, **stats})
        positive = np.zeros_like(a)
        end = a.shape[0] - opts.exclude_bottom_px
        region = (a[:end] > stats["threshold"])
        if mask is not None and opts.exclude_overlays and mask.shape == a.shape:
            region &= ~mask[:end]
        positive[:end] = region.astype(np.uint8) * 255
        save_image(Image.fromarray(positive), out / "masks" / f"{stem}_{ch}_positive_mask.png")


def _fixed_bounds(ds, opts, profile, roi, per_position, hists, global_bounds, mask, cancel) -> Bounds:
    """Decision 17: per-ROI Auto for fixed images unless one manual profile is locked."""
    if opts.lock_manual_profile_for_fixed:
        return global_bounds
    one = per_position.get(roi)
    if one is None:
        radius = profile.rolling_ball.radius_px if profile.rolling_ball.enabled else None
        one = build_histograms(ds, [roi], rolling_ball_radius=radius, overlay_mask=mask, cancel=cancel)
    local = profile.copy()
    local.mode = "auto"
    return display.effective_bounds(local, display.auto_bounds_all(one, profile.auto_method))


# --------------------------------------------------------------------------- #
# Batch
# --------------------------------------------------------------------------- #


def process_batch(datasets: list[Dataset], opts_per_dataset, output: Path | str | None = None,
                  progress: ProgressFn | None = None, cancel=None) -> list[dict]:
    """Run several experiments in sequence. A failure is recorded and the batch continues.

    opts_per_dataset: one Options for all, a list parallel to ``datasets``, or a dict keyed by
    ``Dataset.root``. Cancellation stops the batch after the current experiment.
    """
    progress = progress or log_progress
    results: list[dict] = []
    for i, ds in enumerate(datasets):
        if isinstance(opts_per_dataset, dict):
            opts = opts_per_dataset.get(ds.root) or opts_per_dataset.get(ds.name)
        elif isinstance(opts_per_dataset, (list, tuple)):
            opts = opts_per_dataset[i]
        else:
            opts = opts_per_dataset
        if opts is None:
            results.append({"experiment": ds.name, "root": ds.root, "status": "failed",
                            "error": "No options supplied for this experiment."})
            continue
        started = time.perf_counter()
        if cancel is not None and cancel():
            results.append({"experiment": ds.name, "root": ds.root, "status": "cancelled",
                            "error": "Batch cancelled before this experiment."})
            break
        destination = Path(output) / ds.name if output else None
        try:
            progress(f"[{i + 1}/{len(datasets)}] {ds.name}")
            result = process_dataset(ds, opts, destination, progress, cancel)
            results.append({"experiment": ds.name, "root": ds.root, "status": "complete",
                            "output": str(result["output"]), "processing_seconds": result["processing_seconds"],
                            "videos": len(result["videos"])})
        except JobCancelled as exc:
            results.append({"experiment": ds.name, "root": ds.root, "status": "cancelled", "error": str(exc),
                            "processing_seconds": round(time.perf_counter() - started, 3)})
            break
        except Exception as exc:
            results.append({"experiment": ds.name, "root": ds.root, "status": "failed", "error": str(exc),
                            "processing_seconds": round(time.perf_counter() - started, 3)})
    return results
