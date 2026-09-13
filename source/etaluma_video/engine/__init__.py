"""Processing engine. Pure Python/numpy/cv2/Pillow. Never imports Qt or Streamlit.

The public facade is assembled here so the UI, the CLI and the tests import from
``etaluma_video.engine`` only:

    from etaluma_video.engine import scan_dataset, Options, process_dataset

Module map:

===================  =====================================================================
``models``           data contracts: DisplayProfile, Bounds, Overlays, Calibration, histograms
``jobs``             progress callback and cooperative cancellation
``parsing``          folders and filenames -> Dataset; read_plane / read_rgb
``calibration``      burned-in overlay detection and scale-bar reading
``display``          planes -> RGB (LUTs, colours, blends, rolling ball, auto bounds)
``histograms``       exact histogram passes and their cache
``quantify``         raw threshold measurements
``reports``          PNG/CSV reports (stage map, dashboard, montages, manifests)
``export``           MJPG AVI and H.264 MP4 encoders
``process``          Options, validation, the run, batches, size estimates
``userdata``         %LOCALAPPDATA% settings, profiles, preview cache, logs
===================  =====================================================================
"""
from __future__ import annotations

from .. import APP_NAME, VERSION
from .calibration import calibrate, calibrate_frame, corner_crop, detect_overlays
from .display import (
    apply_lut,
    auto_bounds,
    auto_bounds_all,
    effective_bounds,
    render_frame,
    render_single_channel,
    subtract_background,
    white_bounds,
    white_preset_for_median,
)
from .export import export_video, playback_fps, render_video_frame, video_frame
from .histograms import build_histograms, first_white_median, histogram_cache_path, load_or_build_position, overlay_mask_for
from .jobs import CancelToken, JobCancelled, ProgressFn, check_cancel
from .models import (
    AUTO_METHODS,
    BLEND_MODES,
    CHANNEL_ORDER,
    CHANNEL_PLANE,
    COLOUR_PRESETS,
    FLUOR_CHANNELS,
    OBJECTIVES,
    WHITE_PRESETS,
    Bounds,
    Box,
    Calibration,
    ChannelDisplay,
    ChannelHistogram,
    DisplayProfile,
    HistogramSet,
    Overlays,
    RollingBall,
    WhiteDisplay,
)
from .parsing import (
    CHANNELS,
    Dataset,
    Frame,
    discover_experiments,
    find_experiments,
    inventory,
    parse_avs,
    parse_epf,
    parse_roi,
    read_plane,
    read_rgb,
    scan_dataset,
    summary,
    thumbnail_path,
)
from .process import Options, estimate_output_bytes, process_batch, process_dataset, quick_options, validate_options
from .quantify import quantify
from .reports import annotate_image, dashboard, parse_conditions, stage_map
from .userdata import user_data_dir

__all__ = [
    "VERSION", "APP_NAME",
    # parsing
    "scan_dataset", "find_experiments", "discover_experiments", "Dataset", "Frame", "inventory", "summary",
    "read_plane", "read_rgb", "thumbnail_path", "parse_epf", "parse_avs", "parse_roi", "CHANNELS",
    # display
    "render_frame", "render_single_channel", "apply_lut", "subtract_background",
    "auto_bounds", "auto_bounds_all", "white_bounds", "white_preset_for_median", "effective_bounds",
    # calibration
    "calibrate", "calibrate_frame", "detect_overlays", "corner_crop",
    # histograms
    "build_histograms", "first_white_median", "histogram_cache_path", "load_or_build_position", "overlay_mask_for",
    # measurements and reports
    "quantify", "stage_map", "dashboard", "annotate_image", "parse_conditions",
    # export
    "playback_fps", "video_frame", "render_video_frame", "export_video",
    # run
    "Options", "quick_options", "validate_options", "process_dataset", "process_batch", "estimate_output_bytes",
    # user data
    "user_data_dir",
    # models and jobs
    "DisplayProfile", "ChannelDisplay", "WhiteDisplay", "RollingBall", "Bounds", "Box", "Overlays", "Calibration",
    "ChannelHistogram", "HistogramSet", "CHANNEL_ORDER", "CHANNEL_PLANE", "FLUOR_CHANNELS", "COLOUR_PRESETS",
    "OBJECTIVES", "WHITE_PRESETS", "AUTO_METHODS", "BLEND_MODES",
    "CancelToken", "JobCancelled", "check_cancel", "ProgressFn",
]
