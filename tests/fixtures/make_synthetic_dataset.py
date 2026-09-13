"""Build a small Etaluma-shaped experiment on disk, with real burned-in overlays.

The fixture exists so the parser, the overlay detector and the scale-bar reader can be
exercised in CI without the private captures. Everything the Codex filename grammar
and `parse_epf`/`parse_avs` look at is reproduced with the real tag names, and the
overlays are drawn from the glyph templates harvested from the real captures
(`source/etaluma_video/assets/glyphs/`), so `engine.calibration` reads the fixture the
same way it reads a real frame.

Geometry is copied from the 1900 px captures and kept **absolute**, because Lumaview
draws the overlays at native pixel size no matter how big the frame is:

    bar        rows h-59 .. h-53 (6 px), right end at x = w - 60
    label      15 rows, ending 10 rows above the bar, centred over the bar
    timestamp  rows h-52 .. h-26, black (exact zero) with yellow glyphs, left edge x = 52

Deterministic: every random value comes from ``numpy.random.default_rng(seed)`` with
``seed=0`` by default, so the same call always writes byte-identical files.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image

_HERE = Path(__file__).resolve().parent
_SOURCE = _HERE.parent.parent / "source"
if str(_SOURCE) not in sys.path:  # importable standalone as well as through pytest
    sys.path.insert(0, str(_SOURCE))

from etaluma_video.engine.calibration import GLYPH_CHARS, load_glyphs  # noqa: E402
from etaluma_video.engine.models import CHANNEL_PLANE, OBJECTIVES  # noqa: E402

__all__ = ["make_dataset", "render_overlay_frame", "draw_overlays", "bar_length_px", "OverlayGeometry"]

# --------------------------------------------------------------------------- #
# Overlay geometry (absolute pixel offsets measured on the real captures)
# --------------------------------------------------------------------------- #

BAR_BOTTOM_OFFSET = 53      # h - 53 is the first row *below* the bar
BAR_THICKNESS = 6
BAR_RIGHT_MARGIN = 60       # bar right end at w - 60
LABEL_GAP_ABOVE_BAR = 10    # rows between the label bottom and the bar top
GLYPH_ADVANCE_SPACE = 4     # extra advance for the spaces in "<N> µm, <M>x"
TIMESTAMP_TOP_OFFSET = 52   # box top at h - 52
TIMESTAMP_BOTTOM_OFFSET = 26
TIMESTAMP_LEFT = 52
TIMESTAMP_PAD_LEFT = 5
TIMESTAMP_PAD_RIGHT = 14
#: Digits drawn in the timestamp box. Only characters the harvest provides may appear
#: (3, 6, 7, 8 and 9 have no template), so this is a plausible date, not a real one.
TIMESTAMP_TEXT = "20250101"
YELLOW = (255, 255, 0)
#: Background floor of the signal plane. Real sensors never return exact zero, and the
#: timestamp-box detector relies on the black box being the only exact-zero region.
NOISE_FLOOR = (1, 7)

#: Character -> canonical template stem.
_CHAR_TO_STEM = {c: n for n, c in GLYPH_CHARS.items()}


class OverlayGeometry(dict):
    """Where the overlays were drawn: ``bar_box``/``label_box``/``timestamp_box`` as (y0, y1, x0, x1)."""


def bar_length_px(label_um: float, objective: str) -> int:
    """Bar length Lumaview would draw for this label at the table pixel size."""
    return int(round(float(label_um) / OBJECTIVES[objective]))


def _label_text(label_um: float, objective: str) -> list[str]:
    """"100 µm, 10x" as the list of glyph characters, spaces dropped."""
    magnification = objective.rstrip("xX")
    return [*f"{int(label_um)}", "µ", "m", ",", *magnification, "x"]


def _glyph(char: str) -> np.ndarray:
    stem = _CHAR_TO_STEM.get(char)
    templates = load_glyphs()
    if stem is None or stem not in templates:
        raise ValueError(f"no harvested glyph template for {char!r} "
                         f"(available: {sorted(GLYPH_CHARS[s] for s in templates if '_v' not in s)})")
    return templates[stem]


def _text_width(chars: list[str], spaces_before: set[int]) -> int:
    return sum(_glyph(c).shape[1] + (GLYPH_ADVANCE_SPACE if i in spaces_before else 0)
               for i, c in enumerate(chars))


def _blit_text(rgb: np.ndarray, chars: list[str], y: int, x: int, spaces_before: set[int]) -> tuple[int, int]:
    """Draw the glyphs left to right in yellow. Returns the (x0, x1) actually covered."""
    start = x
    for i, c in enumerate(chars):
        if i in spaces_before:
            x += GLYPH_ADVANCE_SPACE
        t = _glyph(c)
        h, w = t.shape
        if y < 0 or x < 0 or y + h > rgb.shape[0] or x + w > rgb.shape[1]:
            raise ValueError(f"glyph {c!r} does not fit at ({y}, {x}) in a {rgb.shape[1]}x{rgb.shape[0]} frame")
        rgb[y : y + h, x : x + w][t > 0] = YELLOW
        x += w
    return start, x


def draw_overlays(rgb: np.ndarray, *, label_um: float = 100, objective: str = "10x",
                  bar_px: int | None = None, timestamp: bool = True) -> OverlayGeometry:
    """Burn a Lumaview-style scale bar, label and timestamp box into an RGB frame in place.

    ``bar_px`` overrides the length derived from the objective table; that is how a
    capture whose bar disagrees with the table by more than 5 % is simulated.
    """
    h, w = rgb.shape[:2]
    length = int(bar_px) if bar_px is not None else bar_length_px(label_um, objective)
    bar_y1 = h - BAR_BOTTOM_OFFSET
    bar_y0 = bar_y1 - BAR_THICKNESS
    bar_x1 = w - BAR_RIGHT_MARGIN
    bar_x0 = bar_x1 - length
    if bar_y0 < 0 or bar_x0 < 4:
        raise ValueError(
            f"a {length} px bar for {label_um:g} µm at {objective} does not fit in a {w}x{h} frame "
            f"(needs at least {length + BAR_RIGHT_MARGIN + 4} px of width); "
            f"raise `size`, or use render_overlay_frame(shape=(h, w), ...) for an in-memory frame")
    rgb[bar_y0:bar_y1, bar_x0:bar_x1] = YELLOW

    chars = _label_text(label_um, objective)
    spaces = {len(f"{int(label_um)}"), len(f"{int(label_um)}") + 3}  # before "µ" and before the magnification
    label_h = _glyph(chars[0]).shape[0]
    label_w = _text_width(chars, spaces)
    label_y = bar_y0 - LABEL_GAP_ABOVE_BAR - label_h
    label_x = (bar_x0 + bar_x1) // 2 - label_w // 2
    if label_y < 0 or label_x < 0 or label_x + label_w > w:
        raise ValueError(f"the label {''.join(chars)!r} does not fit above the bar in a {w}x{h} frame")
    x0, x1 = _blit_text(rgb, chars, label_y, label_x, spaces)

    geometry = OverlayGeometry(bar_box=(bar_y0, bar_y1, bar_x0, bar_x1),
                               label_box=(label_y, label_y + label_h, x0, x1), timestamp_box=None)
    if not timestamp:
        return geometry

    ts_chars = list(TIMESTAMP_TEXT)
    ts_w = _text_width(ts_chars, set())
    ts_h = _glyph(ts_chars[0]).shape[0]
    box_y0, box_y1 = h - TIMESTAMP_TOP_OFFSET, h - TIMESTAMP_BOTTOM_OFFSET
    box_x0 = TIMESTAMP_LEFT
    box_x1 = box_x0 + TIMESTAMP_PAD_LEFT + ts_w + TIMESTAMP_PAD_RIGHT
    if box_y0 < 0 or box_x1 > w:
        raise ValueError(f"the timestamp box does not fit in a {w}x{h} frame")
    rgb[box_y0:box_y1, box_x0:box_x1] = 0  # exact zero, like Lumaview
    _blit_text(rgb, ts_chars, box_y0 + (box_y1 - box_y0 - ts_h) // 2, box_x0 + TIMESTAMP_PAD_LEFT, set())
    geometry["timestamp_box"] = (box_y0, box_y1, box_x0, box_x1)
    return geometry


# --------------------------------------------------------------------------- #
# Frame content
# --------------------------------------------------------------------------- #


def _white_plane(size: tuple[int, int], rng: np.random.Generator) -> np.ndarray:
    """Dark, phase-contrast-like texture: soft blobs with bright haloes on a dark ground."""
    h, w = size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    plane = np.full((h, w), 26.0, dtype=np.float32)
    for _ in range(14):
        cy, cx = rng.uniform(0, h), rng.uniform(0, w)
        r = rng.uniform(min(h, w) * 0.04, min(h, w) * 0.10)
        d = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2) / r
        plane += 70.0 * np.exp(-((d - 1.0) ** 2) / 0.06)  # halo ring
        plane -= 18.0 * np.exp(-(d**2) / 0.5)  # dark interior
    plane += rng.normal(0.0, 2.5, (h, w))
    return np.clip(plane, NOISE_FLOOR[0], 255).astype(np.uint8)


def _blob_plane(size: tuple[int, int], rng: np.random.Generator, centre: tuple[float, float],
                radius: float, peak: float) -> np.ndarray:
    h, w = size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    plane = np.exp(-(((yy - centre[0]) ** 2 + (xx - centre[1]) ** 2) / (2.0 * radius**2))) * peak
    plane += rng.integers(NOISE_FLOOR[0], NOISE_FLOOR[1], (h, w)).astype(np.float32)
    return np.clip(plane, NOISE_FLOOR[0], 255).astype(np.uint8)


def _frame(channel: str, size: tuple[int, int], position: int, t: int, timepoints: int,
           rng: np.random.Generator) -> np.ndarray:
    """One RGB frame with the channel's signal in its own plane (models.CHANNEL_PLANE)."""
    h, w = size
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    if channel == "WHITE":
        plane = _white_plane(size, rng)
    elif channel == "F2":
        # Moving blob: travels diagonally across the frame over the time course.
        frac = 0.0 if timepoints <= 1 else t / (timepoints - 1)
        centre = (h * (0.25 + 0.5 * frac), w * (0.30 + 0.4 * frac))
        plane = _blob_plane(size, rng, centre, min(h, w) * 0.09, 190.0)
    elif channel == "F3":
        centre = (h * 0.62, w * (0.30 + 0.12 * position))
        plane = _blob_plane(size, rng, centre, min(h, w) * 0.07, 150.0)
    else:  # F1
        centre = (h * 0.40, w * 0.55)
        plane = _blob_plane(size, rng, centre, min(h, w) * 0.05, 120.0)
    rgb[..., CHANNEL_PLANE[channel]] = plane
    return rgb


def render_overlay_frame(shape: tuple[int, int] | int = 400, *, channel: str = "WHITE",
                         label_um: float = 100, objective: str = "10x", bar_px: int | None = None,
                         overlays: bool = True, timestamp: bool = True, seed: int = 0
                         ) -> tuple[np.ndarray, OverlayGeometry | None]:
    """One synthetic RGB frame in memory, with or without overlays.

    Use this instead of ``make_dataset`` when the bar is too long for a small frame
    (a 500 µm bar at 10x is 605 px, a 1000 µm bar at 40x is 4878 px).
    """
    size = (int(shape), int(shape)) if isinstance(shape, int) else (int(shape[0]), int(shape[1]))
    rgb = _frame(channel, size, 0, 0, 1, np.random.default_rng(seed))
    if not overlays:
        return rgb, None
    return rgb, draw_overlays(rgb, label_um=label_um, objective=objective, bar_px=bar_px, timestamp=timestamp)


# --------------------------------------------------------------------------- #
# Metadata files
# --------------------------------------------------------------------------- #


def _epf_xml(name: str, roi_ids: list[str], timepoints: int, channels: tuple[str, ...],
             subfolders: bool, saved: list[str]) -> str:
    minutes = 20
    total = minutes * max(1, timepoints)
    positions = "\n".join(
        f"""      <XYZPosition>
        <x>{12.0 + 1.5 * i:.6f}</x>
        <y>{44.0 - 1.5 * i:.6f}</y>
        <z>{6.70 + 0.01 * i:.6f}</z>
        <order>{i + 1}</order>
        <id>{rid[1:]}</id>
      </XYZPosition>""" for i, rid in enumerate(roi_ids))
    images = "\n".join(f"    <string>{s}</string>" for s in saved)
    led = "\n".join(f"  <{c.lower() if c != 'WHITE' else 'white'}LedState>"
                    f"{'true' if c in channels else 'false'}"
                    f"</{c.lower() if c != 'WHITE' else 'white'}LedState>" for c in ("WHITE", "F1", "F2", "F3"))
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ProtocolParametersSchema xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Version>2</Version>
  <protocolName>{name}</protocolName>
  <userNotes>Synthetic fixture written by tests/fixtures/make_synthetic_dataset.py</userNotes>
  <recordingMode>1</recordingMode>
  <captureEveryHours>0</captureEveryHours>
  <captureEveryMinutes>{minutes}</captureEveryMinutes>
  <captureEverySeconds>0</captureEverySeconds>
  <totalPeriodHours>{total // 60}</totalPeriodHours>
  <totalPeriodMinutes>{total % 60}</totalPeriodMinutes>
  <totalPeriodSeconds>0</totalPeriodSeconds>
  <ledsOffBetweenCaptures>true</ledsOffBetweenCaptures>
{led}
  <whiteLedGain>1</whiteLedGain>
  <f1LedGain>1</f1LedGain>
  <f2LedGain>47</f2LedGain>
  <f3LedGain>47</f3LedGain>
  <whiteLedExposure>1335</whiteLedExposure>
  <f1LedExposure>1943</f1LedExposure>
  <f2LedExposure>1943</f2LedExposure>
  <f3LedExposure>1943</f3LedExposure>
  <saveFilesToSeparateSubFolderForEachWell>{'true' if subfolders else 'false'}</saveFilesToSeparateSubFolderForEachWell>
  <roiPositions>
    <List>
{positions}
    </List>
  </roiPositions>
  <savedImages>
{images}
  </savedImages>
  <zStackEnabled>false</zStackEnabled>
</ProtocolParametersSchema>
"""


def _avs(pattern: str, frames: int) -> str:
    return ('#Input file\n'
            f'ImageSource( "{pattern}", start = 0, end = {max(0, frames - 1)}, use_Devil=false, fps=1.0 )\n'
            "# Convert to YV12 Colorspace.\n"
            "ConvertToYV12()\n")


# --------------------------------------------------------------------------- #
# The generator
# --------------------------------------------------------------------------- #


def make_dataset(path: Path | str, *, positions: int = 3,
                 channels: tuple[str, ...] = ("WHITE", "F2", "F3"), timepoints: int = 6,
                 size: int = 400, layout: str = "subfolders", label_um: float = 100,
                 objective: str = "10x", overlays: bool = True, fixed: bool = False,
                 bar_px: int | None = None, prefix: str = "Synthetic", seed: int = 0) -> Path:
    """Write a synthetic Etaluma experiment and return its folder.

    Parameters mirror the shapes the parser must cope with: ``layout="flat"`` puts every
    TIFF in the experiment root, ``fixed=True`` writes serial 000000 only, and ``overlays``
    turns the burned-in Lumaview overlays off so the "no calibration" path can be tested.
    """
    if layout not in ("subfolders", "flat"):
        raise ValueError(f"layout must be 'subfolders' or 'flat', not {layout!r}")
    if objective not in OBJECTIVES:
        raise ValueError(f"objective must be one of {sorted(OBJECTIVES)}, not {objective!r}")
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    n_t = 1 if fixed else max(1, timepoints)
    subfolders = layout == "subfolders"
    roi_ids = [f"{i + 1}{chr(ord('a') + i)}" for i in range(positions)]  # 1a, 2b, 3c, ...

    saved: list[str] = []
    for p, rid in enumerate(roi_ids):
        folder = root / f"ROI-{rid}" if subfolders else root
        folder.mkdir(parents=True, exist_ok=True)
        thumbs = folder / "thumbnail"
        thumbs.mkdir(exist_ok=True)
        for channel in channels:
            stem = f"{prefix}_ROI-{rid}_{channel}"
            for t in range(n_t):
                # One generator per frame keyed by (position, channel, t): the output does not
                # depend on the order or the number of frames written.
                rng = np.random.default_rng([seed, p, channels.index(channel), t])
                rgb = _frame(channel, (size, size), p, t, n_t, rng)
                if overlays:
                    draw_overlays(rgb, label_um=label_um, objective=objective, bar_px=bar_px)
                name = f"{stem}_{t:06d}.tif"
                tifffile.imwrite(folder / name, rgb, compression="lzw", photometric="rgb")
                saved.append(rf"C:\Etaluma\{root.name}\ROI-{rid}\{name}")
                thumb = Image.fromarray(rgb).convert("RGBA").resize((63, 64), Image.NEAREST)
                thumb.save(thumbs / f"{stem}_{t:06d}.thumbnail.tif", format="PNG")
            (folder / f"{stem}.avs").write_text(_avs(f"{stem}_%06d.tif", n_t), encoding="utf-8")

    (root / f"{root.name.split('_')[0]}.epf").write_text(
        _epf_xml(root.name, roi_ids, n_t, tuple(channels), subfolders, saved), encoding="utf-8")
    return root


if __name__ == "__main__":  # pragma: no cover - manual smoke run
    import argparse
    import tempfile

    ap = argparse.ArgumentParser(description="Write a synthetic Etaluma experiment for inspection.")
    ap.add_argument("out", nargs="?", default=str(Path(tempfile.gettempdir()) / "20260101_120000_synthetic"))
    ap.add_argument("--size", type=int, default=400)
    ap.add_argument("--layout", default="subfolders")
    args = ap.parse_args()
    print(make_dataset(args.out, size=args.size, layout=args.layout))
