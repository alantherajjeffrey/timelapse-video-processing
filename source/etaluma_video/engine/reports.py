"""PNG and CSV reports: stage map, dashboard, info bar, tiles, montages, manifests.

Everything here is Pillow only. Codex 0.3 pulled its font out of matplotlib's data folder; 0.4
drops matplotlib from the bundle, so :func:`font` resolves a TrueType face from the system and
falls back to Pillow's own bundled face.

Two rules this module enforces:

* **montages and panels look like the videos** (0.6, user feedback on 0.5): their tiles are
  rendered by :mod:`etaluma_video.engine.display` with the run's bounds, rolling ball and
  colours, and laid out on the app's own dark palette. Measurements stay raw.
  ``raw_colorize``/``raw_composite`` remain for tools and tests.
* **scale bars come from the calibration**, so a bar is drawn only when the pixel size is known.

Ported from Codex 0.3 ``etaluma_common.py`` (``render_frame`` is renamed ``annotate_image``
because ``render_frame`` now means "compose planes into RGB" in ``display.py``).
"""
from __future__ import annotations

import csv
import io
import math
import textwrap
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .. import VERSION
from .models import Calibration, DisplayProfile
from .parsing import CHANNELS, Dataset, inventory, position_for

__all__ = ["font", "scale_geometry", "annotate_image", "raw_colorize", "raw_composite", "tile", "grid",
           "timepoint_montage",
           "stage_map", "dashboard", "info_bar", "write_csv", "parse_conditions", "condition_for",
           "manifest_rows", "frame_rows"]

#: The app's dark theme (ui/theme.py), so reports look like the window they came from.
BG, CARD, TEXT, MUTED, ACCENT = "#1e1f22", "#2d2e33", "#e6e6e6", "#9aa0a6", "#4c8dff"

#: Font files tried in order; the last resort is Pillow's bundled scalable default.
FONT_CANDIDATES = ("segoeui.ttf", "DejaVuSans.ttf", "arial.ttf", "LiberationSans-Regular.ttf")
BOLD_CANDIDATES = ("segoeuisb.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf", "arialbd.ttf", "LiberationSans-Bold.ttf")


@lru_cache(maxsize=64)
def font(size: int = 20, bold: bool = False) -> ImageFont.FreeTypeFont:
    """A scalable face at ``size`` px; identical metrics are not required, only legibility."""
    for name in (BOLD_CANDIDATES if bold else FONT_CANDIDATES):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


# --------------------------------------------------------------------------- #
# Raw colouring (montages, panels)
# --------------------------------------------------------------------------- #


def raw_colorize(a: np.ndarray, channel: str) -> np.ndarray:
    """Raw plane as RGB with the Codex channel colour; no contrast changes of any kind."""
    return (a[..., None] * np.array(CHANNELS[channel]["rgb"], dtype=np.uint8)).astype(np.uint8)


def raw_composite(planes: dict[str, np.ndarray]) -> np.ndarray:
    """Additive raw merge, clipped at 255 (Codex ``merge_planes``): used by raw montage tiles."""
    if not planes or len({a.shape for a in planes.values()}) != 1:
        raise ValueError("Composite channels must have identical image dimensions.")
    return np.clip(sum(raw_colorize(a, c).astype(np.uint16) for c, a in planes.items()), 0, 255).astype(np.uint8)


# --------------------------------------------------------------------------- #
# Annotation
# --------------------------------------------------------------------------- #


def scale_geometry(source_width: int, rendered_width: int, pixel_size: float) -> tuple[float, int]:
    """(bar length in µm, bar length in rendered pixels) for a 1-2-5 bar of about 22 % of the width."""
    target = source_width * pixel_size * 0.22
    exponent = 10 ** math.floor(math.log10(target))
    length_um = max(v * exponent for v in (0.1, 0.2, 0.5, 1, 2, 5) if v * exponent <= target)
    length_px = max(1, round(length_um / pixel_size * rendered_width / source_width))
    return length_um, length_px


def annotate_image(rgb: np.ndarray, width: int, pixel_size: float | None, scale: bool = False,
                   label: str = "") -> Image.Image:
    """Resize an RGB array to ``width`` and optionally burn in a scale bar and a corner label.

    A scale bar needs a pixel size; without calibration ``scale`` is ignored (the caller logs it).
    """
    source_h, source_w = rgb.shape[:2]
    height = max(2, round(source_h * width / source_w))
    img = Image.fromarray(rgb).resize((width, height), Image.Resampling.LANCZOS)
    d = ImageDraw.Draw(img)
    size = max(12, round(width / 38))
    f = font(size)
    if scale and pixel_size:
        um, px = scale_geometry(source_w, width, pixel_size)
        x, y = width - 18 - px, height - 22
        text = f"{um:g} µm"
        tw = d.textlength(text, font=f)
        d.rectangle((min(x - 9, width - 18 - tw - 9), y - size - 13, width - 8, y + 10), fill="#000000")
        d.rectangle((x, y, x + px - 1, y + 3), fill="white")
        d.text((width - 18 - tw, y - size - 8), text, font=f, fill="white")
    if label:
        tw = d.textlength(label, font=f)
        d.rectangle((0, 0, min(width, tw + 22), size + 18), fill="#000000")
        d.text((10, 7), label, font=f, fill="white")
    return img


def tile(rgb: np.ndarray, title: str, width: int, pixel_size: float | None, scale: bool = True) -> Image.Image:
    """One captioned panel of a montage."""
    img = annotate_image(rgb, width, pixel_size, scale)
    out = Image.new("RGB", (width, img.height + 42), CARD)
    out.paste(img, (0, 42))
    d = ImageDraw.Draw(out)
    title_font = font(15)
    while d.textlength(title, font=title_font) > width - 20 and len(title) > 4:
        title = title[:-4] + "…"
    d.text((10, 12), title, font=title_font, fill=TEXT)
    return out


def grid(tiles: list[Image.Image], columns: int, title: str = "") -> Image.Image:
    w, h = max(t.width for t in tiles), max(t.height for t in tiles)
    gap, top = 12, 64 if title else 12
    columns = min(columns, len(tiles))
    out = Image.new("RGB", (columns * (w + gap) + gap, math.ceil(len(tiles) / columns) * (h + gap) + top), BG)
    if title:
        ImageDraw.Draw(out).text((16, 18), title, font=font(23, True), fill=TEXT)
    for i, t in enumerate(tiles):
        out.paste(t, (gap + i % columns * (w + gap), top + i // columns * (h + gap)))
    return out


def _fit(d: ImageDraw.ImageDraw, text: str, f, max_width: float) -> str:
    while text and d.textlength(text, font=f) > max_width and len(text) > 2:
        text = text[:-2] + "…"
    return text


def timepoint_montage(title: str, rows: list[tuple[str, tuple | None]], columns: list[str],
                      images: dict[tuple[int, int], np.ndarray], *, subtitle: str = "", footer: str = "",
                      pixel_size: float | None = None) -> Image.Image:
    """Rows x columns of equal tiles on the app's dark background (0.6 montages and panels).

    rows: (label, display colour in [0, 1] or None); when every label is "" there is no label column.
    columns: header text per column (times for a timelapse, view names for a fixed-image panel).
    images: {(row, column): RGB uint8 tile}; a missing cell is drawn as an empty card.
    pixel_size: µm per tile pixel; one scale bar goes on the bottom-right tile.
    """
    if not images:
        raise ValueError("no tiles for the montage")
    th, tw = next(iter(images.values())).shape[:2]
    gap, margin = 10, 22
    title_f, sub_f, head_f, label_f, foot_f = font(22, True), font(14), font(15, True), font(16, True), font(13)
    label_w = 150 if any(label for label, _colour in rows) else 0
    top = margin + 32 + (22 if subtitle else 0) + 6
    head_h = 28
    width = 2 * margin + label_w + len(columns) * tw + (len(columns) - 1) * gap
    height = top + head_h + len(rows) * th + (len(rows) - 1) * gap + margin + (24 if footer else 0)
    out = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(out)
    d.text((margin, margin), _fit(d, title, title_f, width - 2 * margin), font=title_f, fill=TEXT)
    if subtitle:
        d.text((margin, margin + 34), _fit(d, subtitle, sub_f, width - 2 * margin), font=sub_f, fill=MUTED)
    x0 = margin + label_w
    for ci, text in enumerate(columns):
        cx = x0 + ci * (tw + gap)
        d.text((cx + tw / 2, top + head_h / 2), _fit(d, text, head_f, tw - 8), font=head_f, fill=TEXT, anchor="mm")
    for ri, (label, colour) in enumerate(rows):
        y = top + head_h + ri * (th + gap)
        if label_w and label:
            tx = margin
            if colour is not None:
                swatch = tuple(int(round(255 * float(c))) for c in colour)
                d.rounded_rectangle((margin, y + th // 2 - 7, margin + 14, y + th // 2 + 7), radius=3, fill=swatch)
                tx += 22
            d.text((tx, y + th / 2), _fit(d, label, label_f, label_w - (tx - margin) - 8), font=label_f,
                   fill=TEXT, anchor="lm")
        for ci in range(len(columns)):
            cx = x0 + ci * (tw + gap)
            img = images.get((ri, ci))
            if img is None:
                d.rectangle((cx, y, cx + tw - 1, y + th - 1), fill=CARD)
            else:
                out.paste(Image.fromarray(np.ascontiguousarray(img[:th, :tw])), (cx, y))
    if pixel_size:
        um, px = scale_geometry(tw, tw, pixel_size)
        cx = x0 + (len(columns) - 1) * (tw + gap)
        y = top + head_h + (len(rows) - 1) * (th + gap)
        f = font(13)
        text = f"{um:g} µm"
        box_w = max(px, d.textlength(text, font=f)) + 16
        bx1, by1 = cx + tw - 6, y + th - 6
        d.rectangle((bx1 - box_w, by1 - 34, bx1, by1), fill="#000000")
        d.rectangle((bx1 - 8 - px, by1 - 9, bx1 - 9, by1 - 6), fill="white")
        d.text((bx1 - 8 - px / 2, by1 - 21), text, font=f, fill="white", anchor="mm")
    if footer:
        d.text((margin, height - margin - 16), _fit(d, footer, foot_f, width - 2 * margin), font=foot_f, fill=MUTED)
    return out


# --------------------------------------------------------------------------- #
# Stage map and dashboard
# --------------------------------------------------------------------------- #


def stage_map(ds: Dataset, width: int = 1000, height: int = 560) -> Image.Image:
    """Captured positions in stage coordinates, with a full-stage reference inset."""
    image = Image.new("RGB", (width, height), CARD)
    d = ImageDraw.Draw(image)
    d.text((22, 18), "Stage positions", font=font(22, True), fill=TEXT)
    points = []
    for channels in ds.groups.values():
        f = next(iter(channels.values()))[0]
        p = position_for(ds, f)
        if p:
            points.append((f, p))
    if not points:
        d.text((22, 75), "No matched stage coordinates in metadata", font=font(18), fill=MUTED)
        return image
    xs, ys = [p["x"] for _, p in points], [p["y"] for _, p in points]
    mx, my = max(1, (max(xs) - min(xs)) * .15), max(1, (max(ys) - min(ys)) * .15)
    xmin, xmax, ymin, ymax = min(xs) - mx, max(xs) + mx, min(ys) - my, max(ys) + my
    left, top, right, bottom = 130, 92, width - 300, height - 70
    ratio = min((right - left) / (xmax - xmin), (bottom - top) / (ymax - ymin))
    cx, cy = (left + right) / 2, (top + bottom) / 2

    def xy(x, y):
        return cx + (x - (xmin + xmax) / 2) * ratio, cy - (y - (ymin + ymax) / 2) * ratio

    x0, y0 = xy(xmin, ymax)
    x1, y1 = xy(xmax, ymin)
    d.rectangle((x0, y0, x1, y1), outline="#42546a", width=2)
    d.text((left, height - 42), f"X {xmin:.1f}–{xmax:.1f} mm  ·  Y {ymin:.1f}–{ymax:.1f} mm", font=font(14), fill=MUTED)
    # Labels occupy separate, spaced gutters; duplicates receive individual leaders.
    sides: list[list] = [[], []]
    for i, (f, p) in enumerate(sorted(points, key=lambda fp: -fp[1]["y"])):
        x, y = xy(p["x"], p["y"])
        sides[i % 2].append((x, y, f))
    for side, entries in enumerate(sides):
        for i, (x, y, f) in enumerate(entries):
            ly = top + (i + .5) * (bottom - top) / len(entries)
            lx = 20 if side == 0 else right + 20
            label = f"{f.order} / {f.id}"
            d.line((x, y, lx + (95 if side == 0 else -6), ly + 8), fill="#62788f", width=1)
            d.ellipse((x - 5, y - 5, x + 5, y + 5), fill=ACCENT)
            d.text((lx, ly), label, font=font(15), fill=TEXT)
    ix, iy, iw, ih = width - 170, 90, 145, 97
    d.text((ix, iy - 25), "120 × 80 mm", font=font(14), fill=MUTED)
    d.rectangle((ix, iy, ix + iw, iy + ih), outline="#62788f", width=2)
    for _, p in points:
        x, y = ix + p["x"] / 120 * iw, iy + (1 - p["y"] / 80) * ih
        if ix <= x <= ix + iw and iy <= y <= iy + ih:
            d.ellipse((x - 2, y - 2, x + 2, y + 2), fill=ACCENT)
    d.text((ix, iy + ih + 10), "Full stage reference", font=font(12), fill=MUTED)
    return image


def _calibration_line(calibration: Calibration | None) -> str:
    if calibration is None or not calibration.usable:
        return "Pixel calibration   unknown (no readable scale bar)"
    objective = f" / {calibration.objective}" if calibration.objective else ""
    return f"Pixel calibration   {calibration.pixel_size_um:.3f} µm/px{objective}"


def dashboard(ds: Dataset, opts, mode: str, selected_count: int,
              calibration: Calibration | None = None, profile: DisplayProfile | None = None) -> Image.Image:
    """One-page experiment report: counts, stage map, acquisition settings, display settings."""
    width = 1800
    image = Image.new("RGB", (width, 1160), BG)
    d = ImageDraw.Draw(image)
    d.text((42, 30), "ETALUMA  /  EXPERIMENT REPORT", font=font(19, True), fill=ACCENT)
    for i, line in enumerate(textwrap.wrap(ds.name, 64)[:2]):
        d.text((42, 67 + i * 38), line, font=font(30, True), fill=TEXT)
    objective = (calibration.objective if calibration and calibration.objective else None) or "objective unknown"
    d.text((42, 154), f"{mode.upper()}   ·   {ds.layout}   ·   {objective}", font=font(20), fill=MUTED)
    interval = opts.interval_seconds if opts.interval_seconds is not None else ds.interval
    duration = (max(f.serial for f in ds.frames) - min(f.serial for f in ds.frames)) * interval if interval else None
    values = [("Positions", str(len(ds.groups))), ("Captured images", str(len(ds.frames))),
              ("Capture interval", f"{interval:g} s" if interval else "Unknown"),
              ("Captured span", f"{duration / 3600:.2f} h" if duration is not None else "Unknown")]
    for i, (label, value) in enumerate(values):
        x = 42 + i * 438
        d.rounded_rectangle((x, 204, x + 416, 326), radius=12, fill=CARD)
        d.text((x + 22, 223), label, font=font(17), fill=MUTED)
        d.text((x + 22, 258), value, font=font(32, True), fill=TEXT)
    image.paste(stage_map(ds, 1090, 570), (42, 350))
    d.rounded_rectangle((1158, 350, 1758, 920), radius=12, fill=CARD)
    d.text((1182, 373), "Acquisition & rendering", font=font(24, True), fill=TEXT)
    d.text((1182, 418), _calibration_line(calibration), font=font(19), fill=TEXT)
    d.text((1182, 451), f"Selected images   {selected_count}", font=font(18), fill=MUTED)
    d.text((1182, 484), "LS720 · fixed Pinkel multiband filter", font=font(17), fill=MUTED)
    raw = ds.protocol.get("raw", {})
    for i, ch in enumerate(ds.channels):
        y = 538 + i * 76
        colour = tuple(int(v * 210 + 30) for v in CHANNELS[ch]["rgb"])
        d.ellipse((1184, y + 4, 1200, y + 20), fill=colour)
        d.text((1214, y), f"{ch}   {CHANNELS[ch]['led']}   /   {CHANNELS[ch]['lut']}", font=font(19, True), fill=TEXT)
        prefix = ch.lower()
        settings = "   ".join(f"{label} {raw.get(prefix + key, '?')}"
                              for label, key in [("Illum.", "LedIllumination"), ("Gain", "LedGain"), ("Exp.", "LedExposure")])
        d.text((1214, y + 28), settings, font=font(14), fill=MUTED)
    d.text((1182, 862), f"Master gain {raw.get('masterLedGain', '?')}  /  exposure {raw.get('masterLedExposure', '?')}",
           font=font(15), fill=MUTED)
    if profile is not None:
        note = (f"Display: {profile.mode} bounds"
                + (f" ({profile.auto_method})" if profile.mode == "auto" else "")
                + f", {profile.blend} blend, WHITE {profile.white.preset}"
                + (f", rolling ball {profile.rolling_ball.radius_px} px" if profile.rolling_ball.enabled else "")
                + ".  Montages and measurements are raw.")
    else:
        note = "Raw intensities; no display scaling."
    d.text((42, 950), note, font=font(19), fill=TEXT)
    for i, warning in enumerate(ds.warnings[:3]):
        d.text((42, 989 + i * 29), textwrap.shorten(warning, 135), font=font(16), fill="#eec78b")
    d.text((42, 1100), f"{len(ds.warnings)} acquisition notes in metadata and log   ·   "
                       f"Timelapse Video Processing {VERSION}   ·   Calibration: {(calibration.source if calibration else 'none')}",
           font=font(15), fill=MUTED)
    return image


def info_bar(ds: Dataset, sub: Dataset, opts, mode: str, selected_count: int,
             calibration: Calibration | None, interval: float | None) -> Image.Image:
    """Narrow strip summarising the run, for pasting above montages in slides."""
    bar = Image.new("RGB", (1800, 200), BG)
    d = ImageDraw.Draw(bar)
    pixel = f"{calibration.pixel_size_um:.3f} µm/px" if calibration and calibration.usable else "uncalibrated"
    objective = calibration.objective if calibration and calibration.objective else "objective unknown"
    lines = [f"{ds.name}  |  {mode}  |  {objective} / {pixel}",
             f"{len(sub.groups)} positions  ·  {selected_count} images  ·  {' / '.join(sub.channels)}  ·  "
             f"interval {interval if interval else 'unknown'} s"]
    raw = ds.protocol.get("raw", {})
    lines.append("   |   ".join(
        f"{c}: illum {raw.get(c.lower() + 'LedIllumination', '?')}, gain {raw.get(c.lower() + 'LedGain', '?')}, "
        f"exp {raw.get(c.lower() + 'LedExposure', '?')}" for c in sub.channels))
    for i, line in enumerate(lines):
        d.text((28, 20 + i * 54), line, font=font(21 if i == 0 else 17, i == 0), fill=TEXT if i == 0 else MUTED)
    return bar


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #


def write_csv(path: Path | str, rows: list[dict]) -> None:
    if not rows:
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with Path(path).open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_conditions(text: str) -> dict[str, str]:
    """``ROI,condition`` pairs, one per line; quoted conditions may contain commas."""
    mapping: dict[str, str] = {}
    for row in csv.reader(io.StringIO(text)):
        if not row or not any(v.strip() for v in row):
            continue
        if len(row) != 2:
            raise ValueError("Enter one ROI,condition pair per line (quote conditions containing commas).")
        key, value = [v.strip() for v in row]
        if key.lower() in ("roi", "id") and value.lower() == "condition":
            continue
        if not key or not value or key in mapping:
            raise ValueError("Condition rows need unique nonempty ROI IDs and conditions.")
        mapping[key] = value
    return mapping


def condition_for(f, opts) -> str:
    return opts.conditions.get(f.roi, opts.conditions.get(f.id, f.descriptor if opts.grouping == "descriptor" else f.id))


def manifest_rows(sub: Dataset, selected, opts, calibration: Calibration | None, interval: float | None) -> list[dict]:
    """One row per selected position x channel, with condition and calibration columns."""
    pixel_size = calibration.pixel_size_um if calibration and calibration.usable else None
    objective = calibration.objective if calibration else None
    rows = inventory(sub)
    for row in rows:
        f = next(f for f in selected if f.roi == row["ROI"] and f.channel == row["channel"])
        row.update({"condition": condition_for(f, opts), "objective": objective,
                    "pixel_size_um": pixel_size, "interval_seconds": interval})
    return rows


def frame_rows(selected, root: Path, calibration: Calibration | None, interval: float | None) -> list[dict]:
    """One row per captured file that took part in the run."""
    pixel_size = calibration.pixel_size_um if calibration and calibration.usable else None
    out = []
    for f in selected:
        try:
            relative = str(Path(f.path).relative_to(root))
        except ValueError:
            relative = f.path
        out.append({"path": f.path, "roi": f.roi, "order": f.order, "id": f.id, "descriptor": f.descriptor,
                    "channel": f.channel, "serial": f.serial, "source_relative_path": relative,
                    "elapsed_seconds": f.serial * interval if interval else None, "pixel_size_um": pixel_size})
    return out
