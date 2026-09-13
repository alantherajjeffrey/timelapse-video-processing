"""Headless entry point. ``--inspect`` is read-only and always prints JSON on stdout.

    python -m etaluma_video.cli <folder> [--batch] [--inspect] [--quick]
                                [--profile display_profile.json] [--auto adaptive|classic|cut]
                                [--rolling-ball 50] [--objective 10x] [--width 950]
                                [--no-avi] [--no-mp4] [--output DIR] [--rois ...] [--channels ...]

Progress goes to stderr so ``--inspect`` output stays machine-readable. Exit code 1 means at
least one experiment failed; the JSON says which.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import VERSION
from .engine import (
    DisplayProfile,
    Options,
    calibrate,
    discover_experiments,
    estimate_output_bytes,
    parse_conditions,
    process_dataset,
    quick_options,
    scan_dataset,
    summary,
)


from .engine.overlays import parse_elapsed


from .engine.jobs import PROGRESS_PREFIX


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="etaluma_video.cli",
                                     description=f"Timelapse Video Processing {VERSION} — headless runs")
    parser.add_argument("folder", type=Path, help="Experiment folder, or a parent folder with --batch")
    parser.add_argument("--inspect", action="store_true", help="Print inventory, calibration and overlay JSON; write nothing")
    parser.add_argument("--batch", action="store_true", help="Discover every experiment under the folder")
    parser.add_argument("--quick", action="store_true", help="Quick video: 1900 px, MP4 only, Auto-normalised display")
    parser.add_argument("--profile", type=Path, help="display_profile.json to render with")
    parser.add_argument("--auto", choices=["adaptive", "classic", "cut"], help="Auto-normalised bounds method (default adaptive)")
    parser.add_argument("--rolling-ball", type=int, metavar="RADIUS",
                        help="Rolling-ball background subtraction radius in pixels (fluorescence only)")
    parser.add_argument("--objective", choices=["4x", "10x", "20x", "40x"],
                        help="Override the burned-in scale bar with the objective table")
    parser.add_argument("--width", type=int, default=950, help="Rendered video width (default 950; 1900 is native)")
    parser.add_argument("--no-avi", action="store_true", help="Skip the MJPG AVI")
    parser.add_argument("--no-mp4", action="store_true", help="Skip the H.264 MP4")
    parser.add_argument("--output", type=Path, help="Output parent folder (default: analysis_output inside the input)")
    parser.add_argument("--rois", nargs="+", help="Only these positions, e.g. ROI-1a ROI-2b")
    parser.add_argument("--channels", nargs="+", choices=["WHITE", "F1", "F2", "F3"], help="Only these channels")
    parser.add_argument("--mode", choices=["auto", "fixed", "timelapse"], default="auto")
    parser.add_argument("--roi-file", help="Explicit standalone stage-position map (.roi)")
    parser.add_argument("--conditions", type=Path, help="CSV containing ROI,condition pairs")
    parser.add_argument("--threshold", type=float, default=50.0, help="Fixed-image threshold (default 50)")
    parser.add_argument("--exclude-bottom-px", type=int, default=0)
    parser.add_argument("--no-exclude-overlays", action="store_true",
                        help="Measure and histogram the burned-in Lumaview overlays too")
    parser.add_argument("--scale-bar", action="store_true", help="Burn this app's scale bar into videos and composites")
    parser.add_argument("--timestamp", action="store_true", help="Burn the elapsed time into video frames")
    parser.add_argument("--time-offset", default="", metavar="TIME",
                        help="Start time added to every label: 'Day 2 : 06:00:00', '06:00:00' or hours")
    parser.add_argument("--days", choices=["auto", "always", "never"], default="auto",
                        help="Show 'Day d :' in the time (auto: when the video reaches 24 h)")
    parser.add_argument("--name-label", action="store_true", help="Burn '<experiment> / <position>' beside the time")
    parser.add_argument("--channel-name", action="append", default=[], metavar="CH=NAME",
                        help="Name a channel on montages and the channel key, e.g. F2=CFDA (repeatable)")
    parser.add_argument("--channel-key", action="store_true", help="Colour key of the channels, top left of the videos")
    parser.add_argument("--timepoints", nargs=2, type=int, metavar=("FIRST", "LAST"),
                        help="Export only these timepoints (1-based, inclusive)")
    parser.add_argument("--quality", choices=["high", "standard", "small"], default="standard",
                        help="MP4 quality: high (CRF 18), standard (CRF 23, default), small (CRF 28)")
    parser.add_argument("--no-montages", action="store_true")
    parser.add_argument("--no-dashboard", action="store_true")
    parser.add_argument("--duration-seconds", type=float, default=10.0)
    parser.add_argument("--fps", type=float)
    parser.add_argument("--interval-seconds", type=float)
    parser.add_argument("--tile-width", type=int, default=380)
    parser.add_argument("--channel-videos", nargs="*", choices=["WHITE", "F1", "F2", "F3"],
                        help="Channels that get their own video (default: every channel; give the flag with no channel for none)")
    parser.add_argument("--no-composite", action="store_true", help="Skip the composite with WHITE")
    parser.add_argument("--no-fluorescence-only", action="store_true", help="Skip the fluorescence-only composite")
    parser.add_argument("--workers", type=int, default=0, help="TIFF decode threads (default: automatic)")
    return parser


def _profile_from_args(args) -> DisplayProfile:
    profile = DisplayProfile.load(args.profile) if args.profile else DisplayProfile()
    if args.auto:
        profile.auto_method = args.auto
    if args.rolling_ball:
        profile.rolling_ball.enabled = True
        profile.rolling_ball.radius_px = int(args.rolling_ball)
    return profile


def _options_from_args(args, conditions: dict) -> Options:
    profile = _profile_from_args(args)
    opts = quick_options(profile) if args.quick else Options(profile=profile, width=args.width,
                                                             avi=not args.no_avi, mp4=not args.no_mp4)
    if args.quick and args.width != 950:
        opts.width = args.width
    opts.mode = args.mode
    opts.channels = args.channels
    opts.rois = args.rois
    opts.objective = args.objective
    opts.conditions = conditions
    opts.threshold = args.threshold
    opts.exclude_bottom_px = args.exclude_bottom_px
    opts.exclude_overlays = not args.no_exclude_overlays
    opts.app_scale_bar = args.scale_bar
    opts.app_timestamp = args.timestamp or args.quick  # Quick always carries the time (0.6)
    opts.name_label = args.name_label or args.quick
    opts.time_offset_seconds = parse_elapsed(args.time_offset)
    opts.time_days = args.days
    opts.video_quality = args.quality
    opts.channel_names = dict(item.split("=", 1) for item in args.channel_name if "=" in item)
    opts.channel_key = args.channel_key
    opts.timepoint_range = list(args.timepoints) if args.timepoints else None
    opts.montages = not args.no_montages
    opts.dashboard = not args.no_dashboard
    opts.duration_seconds = args.duration_seconds
    opts.fps = args.fps
    opts.interval_seconds = args.interval_seconds
    opts.tile_width = args.tile_width
    opts.channel_videos = args.channel_videos  # None when the flag is absent: every channel
    opts.composite_videos = not args.no_composite
    opts.fluorescence_only_video = not args.no_fluorescence_only
    opts.workers = args.workers
    return opts


def inspect(ds) -> dict:
    """Everything ``--inspect`` prints for one experiment: inventory, calibration, overlays."""
    overlays, calibration = calibrate(ds)
    data = summary(ds)
    data["calibration"] = calibration.to_dict()
    data["overlays"] = overlays.to_dict()
    return data


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        roots = discover_experiments(args.folder, args.batch)
        conditions = parse_conditions(args.conditions.read_text(encoding="utf-8-sig")) if args.conditions else {}
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
        return 2

    def note(message: str) -> None:
        print(message.replace(PROGRESS_PREFIX, "", 1), file=sys.stderr, flush=True)

    results, failed = [], False
    for root in roots:
        try:
            ds = scan_dataset(root, args.roi_file)
            if args.inspect:
                data = inspect(ds)
                note(f"{ds.name}: {data['layout']} / {data['mode']} / {data['n_rois']} positions / "
                     f"{data['n_images']} images / {', '.join(data['channels'])} / "
                     f"{data['n_timepoints']} timepoints")
                note(f"  calibration: {data['calibration']['message'] or data['calibration']['source']}")
                note(f"  overlays: {data['overlays']}")
                for warning in ds.warnings[:10]:
                    note("  NOTE: " + warning)
                results.append(data)
                continue
            opts = _options_from_args(args, conditions)
            note(estimate_output_bytes(ds, opts)["text"])
            output = args.output
            if output is not None and args.batch:
                output = output / root.name
            result = process_dataset(ds, opts, output, note)
            results.append({"root": str(root), "status": "complete", "output": str(result["output"]),
                            "processing_seconds": result["processing_seconds"],
                            "videos": [v["file"] for v in result["videos"]],
                            "outputs": len(result["outputs"])})
        except Exception as exc:
            failed = True
            results.append({"root": str(root), "status": "failed", "error": str(exc)})
    print(json.dumps(results if args.batch else results[0], ensure_ascii=True, indent=2, default=str))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
