"""Output presets (0.8): named sets of output choices shared by Quick video, Process and the queue.

A preset says what to write and how it looks: which videos, their size, container and quality,
playback length, montages, dashboard and what is drawn on the videos. What belongs to one
experiment (channel names, start time, timepoint range) and the display stay outside presets.
Presets are plain dicts keyed by ``PRESET_KEYS``; ``apply_preset`` copies one onto ``Options``.
"""
from __future__ import annotations

import math

from .export import VIDEO_QUALITY
from .overlays import DAYS_MODES

PRESET_KEYS = ("width", "avi", "mp4", "video_quality", "playback_source", "duration_seconds", "fps",
               "composite", "fluorescence_only", "per_channel", "montages", "dashboard",
               "app_timestamp", "time_days", "name_label", "app_scale_bar", "channel_key", "exclude_overlays")

#: Process defaults since 0.4 (950 px, AVI + MP4, 10 s, every product), time on since 0.6.
STANDARD = {"width": 950, "avi": True, "mp4": True, "video_quality": "standard", "playback_source": "duration",
            "duration_seconds": 10.0, "fps": None, "composite": True, "fluorescence_only": True,
            "per_channel": True, "montages": True, "dashboard": True, "app_timestamp": True, "time_days": "auto",
            "name_label": False, "app_scale_bar": False, "channel_key": False, "exclude_overlays": True}

BUILTIN_PRESETS: dict[str, dict] = {
    "quick": {"label": "Quick",
              "description": "1900 px MP4, every video, montages and dashboard, time and name on the videos",
              "values": {**STANDARD, "width": 1900, "avi": False, "name_label": True}},
    "standard": {"label": "Standard",
                 "description": "950 px AVI and MP4, every video, montages and dashboard, time on the videos",
                 "values": dict(STANDARD)},
    "presentation": {"label": "Presentation",
                     "description": "1900 px MP4 at high quality, the composite only, time on the video",
                     "values": {**STANDARD, "width": 1900, "avi": False, "video_quality": "high",
                                "fluorescence_only": False, "per_channel": False, "montages": False,
                                "dashboard": False}},
}
BUILTIN_ORDER = ("quick", "standard", "presentation")


def builtin_values(key: str) -> dict:
    return dict(BUILTIN_PRESETS[key]["values"])


def normalise(values: dict | None) -> dict:
    """Every preset key, with the Standard value wherever ``values`` is missing or invalid."""
    source = dict(values or {})
    out = dict(STANDARD)
    for key in PRESET_KEYS:
        if key not in source:
            continue
        value, default = source[key], STANDARD[key]
        try:
            if key in ("width",):
                width = int(value)
                out[key] = width if 160 <= width <= 4096 and width % 2 == 0 else default
            elif key in ("duration_seconds", "fps"):
                number = None if value is None else float(value)
                ok = number is None or (math.isfinite(number) and number > 0)
                out[key] = number if ok and (number is not None or key == "fps") else default
            elif key == "video_quality":
                out[key] = value if value in VIDEO_QUALITY else default
            elif key == "time_days":
                out[key] = value if value in DAYS_MODES else default
            elif key == "playback_source":
                out[key] = value if value in ("duration", "fps") else default
            else:
                out[key] = bool(value)
        except (TypeError, ValueError):
            out[key] = default
    if out["playback_source"] == "fps" and not out["fps"]:
        out["playback_source"] = "duration"
    if out["playback_source"] == "duration":
        out["fps"] = None
    return out


def same(a: dict | None, b: dict | None) -> bool:
    return normalise(a) == normalise(b)


def apply_preset(opts, values: dict | None):
    """Copy a preset onto an engine ``Options`` (in place; also returned)."""
    v = normalise(values)
    opts.width = v["width"]
    opts.avi, opts.mp4 = v["avi"], v["mp4"]
    opts.video_quality = v["video_quality"]
    opts.playback_source = v["playback_source"]
    opts.duration_seconds = v["duration_seconds"]
    opts.fps = v["fps"]
    opts.composite_videos = v["composite"]
    opts.fluorescence_only_video = v["fluorescence_only"]
    opts.channel_videos = None if v["per_channel"] else []
    opts.montages, opts.dashboard = v["montages"], v["dashboard"]
    opts.app_timestamp, opts.time_days = v["app_timestamp"], v["time_days"]
    opts.name_label, opts.app_scale_bar = v["name_label"], v["app_scale_bar"]
    opts.channel_key, opts.exclude_overlays = v["channel_key"], v["exclude_overlays"]
    return opts
