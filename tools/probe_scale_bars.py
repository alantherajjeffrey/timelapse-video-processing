"""Probe the burned-in Lumaview overlays of every experiment under a samples root.

For each experiment folder it picks the first WHITE raw frame (else F2/F3/F1),
writes a 2x nearest-neighbour crop of the bottom-right corner (scale bar +
label) and of the bottom-left corner (timestamp box), measures the yellow bar
and the label bounding box, and records the WHITE-plane median.

The crops exist so a human (or Claude's Read tool) can read the labels that
nobody has transcribed yet; the JSON feeds tests/fixtures/scale_bar_ground_truth.json.

Usage
-----
    .venv\\Scripts\\python.exe tools\\probe_scale_bars.py "..\\private\\Sample set" \\
        --out "..\\private\\probe\\scale_bar_crops"

Read-only with respect to the samples root; never writes inside it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image

# --------------------------------------------------------------------------- #
# Frame discovery (mirrors the Codex 0.3 grammar without importing the engine)
# --------------------------------------------------------------------------- #

#: Channel plane index in the RGB TIFF (models.CHANNEL_PLANE).
CHANNEL_PLANE = {"WHITE": 0, "F1": 2, "F2": 1, "F3": 0}
#: Preference order when choosing the frame to probe.
CHANNEL_PREFERENCE = ("WHITE", "F2", "F3", "F1")
END_RE = re.compile(r"_(WHITE|F1|F2|F3)_(\d{6})\.tiff?$", re.I)

#: Bottom fraction of the image searched for overlays.
BOTTOM_FRACTION = 0.15
#: Rows above the bar that may hold the label.
LABEL_GAP_PX = 40
#: Rows at the bottom excluded from the WHITE median (overlay contamination).
MEDIAN_EXCLUDE_BOTTOM = 90


def excluded(path: Path) -> bool:
    """True for thumbnail and analysis_output paths (Codex ``excluded``)."""
    return any("thumbnail" in p.lower() or p.lower() == "analysis_output" for p in path.parts)


def raw_frames(experiment: Path) -> list[tuple[str, int, Path]]:
    """(channel, serial, path) of every raw Etaluma TIFF under an experiment folder."""
    out: list[tuple[str, int, Path]] = []
    for p in sorted(experiment.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in (".tif", ".tiff") or excluded(p):
            continue
        m = END_RE.search(p.name)
        if m:
            out.append((m.group(1).upper(), int(m.group(2)), p))
    return out


def pick_frame(frames: list[tuple[str, int, Path]]) -> tuple[str, int, Path] | None:
    """First frame of the most preferred channel present, ordered by path then serial."""
    for ch in CHANNEL_PREFERENCE:
        cands = [f for f in frames if f[0] == ch]
        if cands:
            return sorted(cands, key=lambda f: (str(f[2].parent), f[1]))[0]
    return None


def find_experiments(root: Path) -> list[Path]:
    """Experiment folders: direct children of the samples root that hold raw TIFFs."""
    out = []
    for p in sorted(root.iterdir()):
        if p.is_dir() and not excluded(p) and raw_frames(p):
            out.append(p)
    return out


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #


def yellow_mask(rgb: np.ndarray) -> np.ndarray:
    """Lumaview overlay yellow: R > 170 and G > 170 and B < 90."""
    return (rgb[..., 0] > 170) & (rgb[..., 1] > 170) & (rgb[..., 2] < 90)


def longest_run(row: np.ndarray) -> tuple[int, int]:
    """(start, length) of the longest True run in a 1-D boolean row; (0, 0) when empty."""
    if not row.any():
        return 0, 0
    padded = np.concatenate(([False], row, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    starts, ends = edges[0::2], edges[1::2]
    i = int(np.argmax(ends - starts))
    return int(starts[i]), int(ends[i] - starts[i])


def measure_bar(mask: np.ndarray) -> dict | None:
    """Longest horizontal yellow run in the bottom BOTTOM_FRACTION of rows, grown to its thickness."""
    h, w = mask.shape
    y_start = int(h * (1.0 - BOTTOM_FRACTION))
    best = None
    for y in range(y_start, h):
        x0, length = longest_run(mask[y])
        if length and (best is None or length > best[2]):
            best = (y, x0, length)
    if best is None:
        return None
    y, x0, length = best
    # Grow up and down while the same horizontal span stays yellow (>=90 % covered).
    def covered(row: int) -> bool:
        return 0 <= row < h and float(mask[row, x0 : x0 + length].mean()) >= 0.9

    y0, y1 = y, y + 1
    while covered(y0 - 1):
        y0 -= 1
    while covered(y1):
        y1 += 1
    return {"bar_px": int(length), "bar_box": [int(y0), int(y1), int(x0), int(x0 + length)],
            "thickness_px": int(y1 - y0), "row": int(y)}


def measure_label(mask: np.ndarray, bar: dict) -> list[int] | None:
    """Bounding box of the yellow pixels within LABEL_GAP_PX rows above the bar."""
    y0b, _, x0b, x1b = bar["bar_box"]
    top = max(0, y0b - LABEL_GAP_PX)
    strip = mask[top:y0b, :]
    if not strip.any():
        return None
    ys, xs = np.nonzero(strip)
    return [int(top + ys.min()), int(top + ys.max() + 1), int(xs.min()), int(xs.max() + 1)]


def scale_nn(img: np.ndarray, factor: int) -> np.ndarray:
    return np.repeat(np.repeat(img, factor, axis=0), factor, axis=1)


def probe_experiment(experiment: Path, root: Path, out_dir: Path, scale: int = 2) -> dict:
    """Measure one experiment and write its two crops. Returns the JSON record."""
    frames = raw_frames(experiment)
    chosen = pick_frame(frames)
    record: dict = {"experiment": experiment.name, "frame": None, "channel": None,
                    "shape": None, "bar_px": None, "bar_box": None, "thickness_px": None,
                    "label_box": None, "white_median": None,
                    "channels_present": sorted({f[0] for f in frames}), "n_frames": len(frames)}
    if chosen is None:
        record["error"] = "no raw frames"
        return record
    channel, _, path = chosen
    rgb = tifffile.imread(path)
    if rgb.ndim != 3 or rgb.shape[2] < 3:
        record["error"] = f"unexpected TIFF shape {rgb.shape}"
        return record
    rgb = np.ascontiguousarray(rgb[..., :3]).astype(np.uint8, copy=False)
    h, w = rgb.shape[:2]
    record.update(frame=str(path.relative_to(root)).replace("\\", "/"), channel=channel, shape=[int(h), int(w)])

    mask = yellow_mask(rgb)
    bar = measure_bar(mask)
    if bar:
        record.update(bar)
        record["label_box"] = measure_label(mask, bar)

    # WHITE median from a WHITE frame if the experiment has one, excluding the bottom rows.
    white = next((f for f in sorted(frames, key=lambda f: (str(f[2].parent), f[1])) if f[0] == "WHITE"), None)
    if white is not None:
        wrgb = tifffile.imread(white[2])
        plane = np.ascontiguousarray(wrgb[..., CHANNEL_PLANE["WHITE"]])
        record["white_median"] = float(np.median(plane[: max(1, plane.shape[0] - MEDIAN_EXCLUDE_BOTTOM)]))
        record["white_frame"] = str(white[2].relative_to(root)).replace("\\", "/")

    out_dir.mkdir(parents=True, exist_ok=True)
    br = rgb[max(0, h - 160) : h, max(0, w - 480) : w]
    bl = rgb[max(0, h - 60) : h, 0 : min(w, 520)]
    Image.fromarray(scale_nn(br, scale)).save(out_dir / f"{experiment.name}_BR.png")
    Image.fromarray(scale_nn(bl, scale)).save(out_dir / f"{experiment.name}_BL.png")
    record["crops"] = {"BR": f"{experiment.name}_BR.png", "BL": f"{experiment.name}_BL.png", "scale": scale}
    return record


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):  # pragma: no cover - console without reconfigure
        pass
    here = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("samples", nargs="?", default=str(here.parent / "private" / "Sample set"),
                    help="folder holding the Etaluma experiment folders")
    ap.add_argument("--out", default=str(here.parent / "private" / "probe" / "scale_bar_crops"),
                    help="output folder for the crops and probe.json")
    ap.add_argument("--scale", type=int, default=2, help="nearest-neighbour zoom of the crops (default 2)")
    ap.add_argument("--verify", action="store_true",
                    help="also run engine.calibration on every frame and print the calibration table")
    args = ap.parse_args(argv)

    root = Path(args.samples).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    if not root.is_dir():
        print(f"samples root not found: {root}")
        return 2
    if out_dir.is_relative_to(root):
        print(f"refusing to write inside the samples root: {out_dir}")
        return 2

    records = [probe_experiment(exp, root, out_dir, args.scale) for exp in find_experiments(root)]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "probe.json").write_text(
        json.dumps({"samples_root": str(root), "experiments": records}, indent=2, ensure_ascii=False),
        encoding="utf-8")

    head = f"{'experiment':<46} {'ch':<6} {'bar':>5} {'thick':>6} {'label box':<28} {'white med':>9}"
    print(head)
    print("-" * len(head))
    for r in records:
        print(f"{r['experiment']:<46} {str(r['channel']):<6} {str(r['bar_px']):>5} {str(r['thickness_px']):>6} "
              f"{str(r['label_box']):<28} {'' if r['white_median'] is None else f'{r['white_median']:.0f}':>9}")
    print(f"\n{len(records)} experiments -> {out_dir}")
    if args.verify:
        verify(records, root)
    return 0


def verify(records: list[dict], root: Path) -> None:
    """Run engine.calibration on each probed frame and print the calibration table.

    Imported lazily and only on demand: the measurements above stay independent of the
    reader they are used to check.
    """
    here = Path(__file__).resolve().parent.parent
    if str(here / "source") not in sys.path:
        sys.path.insert(0, str(here / "source"))
    from etaluma_video.engine.calibration import calibrate_frame  # noqa: WPS433

    head = (f"\n{'experiment':<46} {'bar':>4} {'label read':<14} {'conf':>5} {'µm/px':>7} "
            f"{'table':>6} {'diff':>7} {'disagree':>8}")
    print(head)
    print("-" * (len(head) - 1))
    for r in records:
        if not r.get("frame"):
            continue
        rgb = np.ascontiguousarray(tifffile.imread(root / r["frame"])[..., :3])
        _, cal = calibrate_frame(rgb, r["frame"])
        label = f"{cal.label_um:g} µm, {cal.label_objective}" if cal.label_um else "UNREADABLE"
        diff = (f"{(cal.pixel_size_um / cal.table_pixel_size_um - 1) * 100:+.2f}%"
                if cal.pixel_size_um and cal.table_pixel_size_um else "")
        print(f"{r['experiment']:<46} {str(cal.bar_px):>4} {label:<14} {cal.confidence:>5.3f} "
              f"{'' if cal.pixel_size_um is None else f'{cal.pixel_size_um:.4f}':>7} "
              f"{'' if cal.table_pixel_size_um is None else f'{cal.table_pixel_size_um:.3f}':>6} "
              f"{diff:>7} {'YES' if cal.disagreement else 'no':>8}")


if __name__ == "__main__":
    raise SystemExit(main())
