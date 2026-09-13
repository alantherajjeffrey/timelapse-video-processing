"""Burned-in overlay detection and scale-bar calibration.

CONTRACT: docs/plans/v0.4_plan.md section 4.4.

Every LS720 capture carries two Lumaview overlays (all nine sample experiments):
  * timestamp box, bottom-left: exact-zero black rectangle with yellow text
  * scale bar, bottom-right: yellow bar (6 px thick at 1900 px), label "<N> µm, <M>x" above it
Yellow rule: R > 170 and G > 170 and B < 90. Search regions are relative to the image size.
Pixel size = label_um / bar_len_px. Table value from OBJECTIVES[label_objective] is a cross-check;
on > 5 % disagreement the bar wins and Calibration.disagreement is True.

Geometry measured on the real captures at 1900 px (tools/probe_scale_bars.py):
  label rows 1816-1830, bar rows 1841-1846 (6 px), bar right end x = 1840,
  timestamp box rows 1848-1873 x cols 52-521, its yellow text rows 1852-1870.

Sizes other than 1900 px: Lumaview draws the glyphs, the bar thickness and the
label gap at *native* pixel size, so only the search regions scale with the
image. Templates are never rescaled.
"""
from __future__ import annotations

import re
from pathlib import Path

import cv2
import numpy as np

from .models import CALIBRATION_DISAGREEMENT, OBJECTIVES, Box, Calibration, Overlays

__all__ = ["yellow_mask", "detect_overlays", "read_scale_label", "calibrate_frame", "calibrate", "corner_crop",
           "GLYPH_DIR", "GLYPH_CHARS", "load_glyphs"]

# --------------------------------------------------------------------------- #
# Constants (absolute px values were measured at 1900 px and stay absolute)
# --------------------------------------------------------------------------- #

#: Bottom fraction of the image searched for overlays, never fewer than MIN_BOTTOM_PX rows.
BOTTOM_FRACTION = 0.15
MIN_BOTTOM_PX = 80
#: The bar must be at least this fraction of the image width.
BAR_MIN_WIDTH_FRACTION = 0.03
#: Bar thickness window in pixels (absolute: Lumaview draws 6 px at every size).
BAR_THICKNESS_PX = (3, 12)
#: Rows above the bar that may hold the label (absolute).
LABEL_GAP_PX = 40
#: Horizontal margin around the bar in which label glyphs are accepted.
LABEL_X_MARGIN_PX = 60
#: Furthest an exact-zero timestamp-box edge may grow away from its yellow text.
TIMESTAMP_MAX_GROW_PX = 60
#: A timestamp box must be at least this tall and this filled with zero-or-yellow pixels.
#: Measured over all nine sample experiments the real box is 0.894-0.902 zero-or-yellow (the
#: remainder is the anti-aliasing halo around the text); the darkest non-overlay image block
#: found in the sample set reaches 0.597, so 0.85 separates them with margin.
TIMESTAMP_MIN_HEIGHT_PX = 10
TIMESTAMP_MIN_FILL = 0.85

#: Glyph template acceptance (plan section 4.4).
GLYPH_ACCEPT_SCORE = 0.85
GLYPH_RUNNER_UP_RATIO = 0.7
#: Candidate floor kept before non-maximum suppression.
GLYPH_CANDIDATE_FLOOR = 0.45
#: Two detections of the same glyph overlap by more than this fraction of the narrower template.
GLYPH_NMS_OVERLAP = 0.5
#: A *rival* must compete for the same footprint: it only vetoes a glyph when it covers at
#: least this fraction of the wider of the two. Without it the 3-px comma - two surviving
#: pixels of an anti-aliased glyph - vetoes every wide character it brushes against.
GLYPH_RIVAL_OVERLAP = 0.6

GLYPH_DIR = Path(__file__).resolve().parent.parent / "assets" / "glyphs"
#: Template file stem -> the character it stands for.
GLYPH_CHARS: dict[str, str] = {
    "d0": "0", "d1": "1", "d2": "2", "d3": "3", "d4": "4",
    "d5": "5", "d6": "6", "d7": "7", "d8": "8", "d9": "9",
    "mu": "µ", "m": "m", "comma": ",", "x": "x",
}
#: ``d0.png`` is the canonical rendering, ``d0_v2.png`` … further real renderings of the
#: same character. Lumaview anti-aliases the label against the image, so the strict yellow
#: rule yields a small family of binary shapes per character (a "1" is 3 px wide in one
#: position and 4 px in another). All variants are harvested from real captures.
GLYPH_VARIANT_RE = re.compile(r"^(?P<base>[a-z0-9]+?)(?:_v\d+)?$")

#: "<digits> µm, <digits>x" - the comma and the spaces are optional so a missed
#: comma does not lose an otherwise perfect read.
LABEL_RE = re.compile(r"^(\d+)\s*µ\s*m\s*,?\s*(\d+)\s*x$")

_GLYPH_CACHE: dict[str, np.ndarray] | None = None


# --------------------------------------------------------------------------- #
# Masks
# --------------------------------------------------------------------------- #


def yellow_mask(rgb: np.ndarray) -> np.ndarray:
    """Boolean mask of Lumaview's yellow overlay pixels (R>170, G>170, B<90)."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    return (r > 170) & (g > 170) & (b < 90)


def _zero_mask(rgb: np.ndarray) -> np.ndarray:
    """Boolean mask of exact-zero (0, 0, 0) pixels - the timestamp box interior."""
    return (rgb[..., 0] == 0) & (rgb[..., 1] == 0) & (rgb[..., 2] == 0)


def _bottom_start(height: int) -> int:
    """First row of the overlay search band."""
    return max(0, height - max(int(round(height * BOTTOM_FRACTION)), MIN_BOTTOM_PX))


def _runs(row: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(starts, ends) of every True run in a 1-D boolean array (ends exclusive)."""
    padded = np.concatenate(([False], row, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return edges[0::2], edges[1::2]


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


def _find_bar(mask: np.ndarray) -> tuple[Box, int] | None:
    """Longest horizontal yellow run that ends in the right half of the bottom band."""
    h, w = mask.shape
    min_len = max(4, int(round(w * BAR_MIN_WIDTH_FRACTION)))
    best: tuple[int, int, int] | None = None  # (length, y, x0)
    for y in range(_bottom_start(h), h):
        row = mask[y]
        if not row.any():
            continue
        starts, ends = _runs(row)
        for x0, x1 in zip(starts, ends):
            length = int(x1 - x0)
            if length >= min_len and x1 > w // 2 and (best is None or length > best[0]):
                best = (length, y, int(x0))
    if best is None:
        return None
    length, y, x0 = best

    def covered(row: int) -> bool:
        return 0 <= row < h and float(mask[row, x0 : x0 + length].mean()) >= 0.9

    y0, y1 = y, y + 1
    while covered(y0 - 1):
        y0 -= 1
    while covered(y1):
        y1 += 1
    if not (BAR_THICKNESS_PX[0] <= y1 - y0 <= BAR_THICKNESS_PX[1]):
        return None
    return Box(y0, y1, x0, x0 + length), length


def _find_label(mask: np.ndarray, bar: Box) -> Box | None:
    """Bounding box of the yellow pixels within LABEL_GAP_PX rows above the bar."""
    h, w = mask.shape
    top = max(0, bar.y0 - LABEL_GAP_PX)
    x0 = max(0, bar.x0 - LABEL_X_MARGIN_PX)
    x1 = min(w, bar.x1 + LABEL_X_MARGIN_PX)
    strip = mask[top : bar.y0, x0:x1]
    if strip.size == 0 or not strip.any():
        return None
    ys, xs = np.nonzero(strip)
    return Box(top + int(ys.min()), top + int(ys.max()) + 1, x0 + int(xs.min()), x0 + int(xs.max()) + 1)


def _find_timestamp_box(text: np.ndarray, zero: np.ndarray, yellow: np.ndarray | None = None) -> Box | None:
    """Black rectangle behind the yellow timestamp text in the bottom-left quadrant.

    ``text`` is the yellow mask with the scale bar and its label already removed; ``yellow``
    is the full mask, used when testing whether an edge is free of image content. Seeded from
    the text (robust on frames whose background is largely exact zero) and grown outwards
    while each new edge is entirely zero-or-yellow.
    """
    yellow = text if yellow is None else yellow
    h, w = text.shape
    top = _bottom_start(h)
    seed = text[top:h, 0 : w // 2]
    if not seed.any():
        return None
    ys, xs = np.nonzero(seed)
    ty0, ty1 = top + int(ys.min()), top + int(ys.max()) + 1  # rows the yellow text occupies
    y0, y1 = ty0, ty1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    ok = zero | yellow

    def full(sl_y: slice, sl_x: slice) -> bool:
        region = ok[sl_y, sl_x]
        return region.size > 0 and bool(region.all())

    # Vertical growth first: the rows above and below the text are pure black.
    grown = 0
    while grown < TIMESTAMP_MAX_GROW_PX and y0 > 0 and full(slice(y0 - 1, y0), slice(x0, x1)):
        y0 -= 1
        grown += 1
    grown = 0
    while grown < TIMESTAMP_MAX_GROW_PX and y1 < h and full(slice(y1, y1 + 1), slice(x0, x1)):
        y1 += 1
        grown += 1

    # Horizontal growth is tested on the text-free margin rows only. The anti-aliasing halo
    # around the glyphs is neither exact zero nor yellow, so testing whole columns stops the
    # growth early and leaves the box narrower than the black rectangle actually is.
    def column_ok(x: int) -> bool:
        above, below = ok[y0:ty0, x : x + 1], ok[ty1:y1, x : x + 1]
        if above.size or below.size:
            return bool(above.all() and below.all())
        return full(slice(y0, y1), slice(x, x + 1))

    grown = 0
    while grown < TIMESTAMP_MAX_GROW_PX and x0 > 0 and column_ok(x0 - 1):
        x0 -= 1
        grown += 1
    grown = 0
    while grown < TIMESTAMP_MAX_GROW_PX and x1 < w and column_ok(x1):
        x1 += 1
        grown += 1

    box = Box(y0, y1, x0, x1)
    if box.height < TIMESTAMP_MIN_HEIGHT_PX:
        return None
    if float(ok[y0:y1, x0:x1].mean()) < TIMESTAMP_MIN_FILL:
        return None
    return box


def detect_overlays(rgb: np.ndarray) -> Overlays:
    """Find the timestamp box and the scale bar (bar box, label box, bar length).

    Returns an empty ``Overlays`` (``detected`` False) when nothing is found; never raises.
    """
    rgb = np.asarray(rgb)
    if rgb.ndim != 3 or rgb.shape[2] < 3:
        raise ValueError(f"detect_overlays expects an HxWx3 RGB array, got shape {rgb.shape}")
    h, w = int(rgb.shape[0]), int(rgb.shape[1])
    ov = Overlays(image_shape=(h, w))
    yellow = yellow_mask(rgb)
    if not yellow.any():
        return ov
    found = _find_bar(yellow)
    if found is not None:
        ov.bar_box, ov.bar_len_px = found
        ov.label_box = _find_label(yellow, ov.bar_box)
    # Seed the timestamp box from yellow that is *not* the scale bar or its label: a long bar
    # reaches into the bottom-left half (603 px at 1900 px, 4878 px for 1000 µm at 40x) and
    # would otherwise be mistaken for timestamp text.
    seed = yellow
    if ov.bar_box is not None or ov.label_box is not None:
        seed = yellow.copy()
        for b in (ov.bar_box, ov.label_box):
            if b is not None:
                seed[b.y0 : b.y1, b.x0 : b.x1] = False
    ov.timestamp_box = _find_timestamp_box(seed, _zero_mask(rgb), yellow)
    return ov


# --------------------------------------------------------------------------- #
# Glyph templates and the label reader
# --------------------------------------------------------------------------- #


def glyph_base(stem: str) -> str | None:
    """"d0_v2" -> "d0"; None when the stem is not a glyph template."""
    m = GLYPH_VARIANT_RE.match(stem)
    base = m.group("base") if m else None
    return base if base in GLYPH_CHARS else None


def load_glyphs(directory: Path | str | None = None, *, force: bool = False) -> dict[str, np.ndarray]:
    """Glyph templates as uint8 0/255 arrays, keyed by file stem ("d0", "d0_v2", "mu", ...).

    Templates are full label-height canvases (15 rows on real captures) trimmed
    left and right to the glyph, so the baseline position is part of the match.
    Cached at module level; pass ``force=True`` after re-harvesting.
    """
    global _GLYPH_CACHE
    if directory is None and _GLYPH_CACHE is not None and not force:
        return _GLYPH_CACHE
    folder = Path(directory) if directory is not None else GLYPH_DIR
    glyphs: dict[str, np.ndarray] = {}
    if folder.is_dir():
        for p in sorted(folder.glob("*.png")):
            if glyph_base(p.stem) is None:
                continue
            # PIL rather than cv2.imread: cv2 cannot open non-ASCII Windows paths.
            from PIL import Image  # local import keeps engine import cost low

            with Image.open(p) as im:
                a = np.array(im.convert("L"))
            glyphs[p.stem] = np.where(a > 127, 255, 0).astype(np.uint8)
    if directory is None:
        _GLYPH_CACHE = glyphs
    return glyphs


def _candidates(strip: np.ndarray, glyphs: dict[str, np.ndarray]) -> list[dict]:
    """Every (glyph, x, score) above the candidate floor, from cv2.matchTemplate."""
    out: list[dict] = []
    sh, sw = strip.shape
    for name, tmpl in glyphs.items():
        th, tw = tmpl.shape
        if th > sh or tw > sw:
            continue
        scores = cv2.matchTemplate(strip, tmpl, cv2.TM_CCOEFF_NORMED)
        scores = np.nan_to_num(scores, nan=0.0, posinf=0.0, neginf=0.0)
        ys, xs = np.nonzero(scores >= GLYPH_CANDIDATE_FLOOR)
        for y, x in zip(ys, xs):
            out.append({"name": name, "x": int(x), "y": int(y), "w": tw,
                        "score": float(scores[y, x]), "char": GLYPH_CHARS[glyph_base(name)]})
    return out


def _overlap(a: dict, b: dict) -> int:
    return max(0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))


def _suppress(cands: list[dict]) -> list[dict]:
    """Greedy non-maximum suppression by score.

    A candidate that overlaps a kept one by more than half the narrower template is
    dropped; when it stands for a *different* character **and** covers most of the wider
    template it becomes that kept glyph's ``rival`` - the "second-best different glyph at
    this position" of the accept rule.
    """
    kept: list[dict] = []
    # Ties go to the wider template: a 3-px comma must never displace an 8-px "µ".
    for c in sorted(cands, key=lambda c: (-c["score"], -c["w"], c["x"], c["name"])):
        hit = None
        for k in kept:
            if _overlap(c, k) > GLYPH_NMS_OVERLAP * min(c["w"], k["w"]):
                hit = k
                break
        if hit is None:
            kept.append({**c, "rival": 0.0})
        elif c["char"] != hit["char"] and _overlap(c, hit) >= GLYPH_RIVAL_OVERLAP * max(c["w"], hit["w"]):
            hit["rival"] = max(hit["rival"], c["score"])
    return kept


def read_scale_label(mask: np.ndarray, label_box: Box | None) -> tuple[float | None, str | None, float]:
    """Read "<N> µm, <M>x" from the yellow mask inside label_box by glyph template matching.

    Returns (label_um, objective such as "10x", confidence in [0, 1]).
    Confidence is the lowest accepted glyph score; (None, None, low) on failure.
    """
    if label_box is None:
        return None, None, 0.0
    glyphs = load_glyphs()
    if not glyphs:
        return None, None, 0.0
    h, w = mask.shape[:2]
    pad = 3
    y0, y1 = max(0, label_box.y0 - pad), min(h, label_box.y1 + pad)
    x0, x1 = max(0, label_box.x0 - pad), min(w, label_box.x1 + pad)
    strip = np.where(mask[y0:y1, x0:x1], 255, 0).astype(np.uint8)
    if strip.size == 0 or not strip.any():
        return None, None, 0.0

    cands = _candidates(strip, glyphs)
    if not cands:
        return None, None, 0.0
    kept = _suppress(cands)

    accepted = [c for c in kept
                if c["score"] >= GLYPH_ACCEPT_SCORE and c["rival"] <= GLYPH_RUNNER_UP_RATIO * c["score"]]
    if not accepted:
        return None, None, float(max((c["score"] for c in kept), default=0.0))

    accepted.sort(key=lambda c: c["x"])
    m = LABEL_RE.match("".join(c["char"] for c in accepted))
    if m is None:
        # The comma survives the strict yellow rule as one or two stray pixels, so it is
        # the one glyph that may be missed or hallucinated. Drop every comma and retry.
        accepted = [c for c in accepted if c["char"] != ","]
        m = LABEL_RE.match("".join(c["char"] for c in accepted))
    if m is None or not accepted:
        return None, None, float(min(c["score"] for c in kept if c["score"] >= GLYPH_ACCEPT_SCORE) if
                                 any(c["score"] >= GLYPH_ACCEPT_SCORE for c in kept) else 0.0)
    confidence = float(min(c["score"] for c in accepted))
    return float(m.group(1)), f"{int(m.group(2))}x", confidence


# --------------------------------------------------------------------------- #
# Calibration
# --------------------------------------------------------------------------- #


def calibrate_frame(rgb: np.ndarray, source_frame: str | None = None) -> tuple[Overlays, Calibration]:
    """Overlays and calibration from one RGB frame (first WHITE frame, else an F2/F3 frame)."""
    ov = detect_overlays(rgb)
    cal = Calibration(source="none", message="no scale bar detected", source_frame=source_frame)
    if ov.bar_box is None or ov.label_box is None or not ov.bar_len_px:
        return ov, cal
    label_um, objective, conf = read_scale_label(yellow_mask(rgb), ov.label_box)
    cal.bar_px, cal.label_um, cal.label_objective, cal.confidence = ov.bar_len_px, label_um, objective, conf
    if label_um and ov.bar_len_px:
        cal.pixel_size_um = float(label_um) / float(ov.bar_len_px)
        cal.source = "burned-in scale bar"
        if objective in OBJECTIVES:
            cal.objective = objective
            cal.table_pixel_size_um = OBJECTIVES[objective]
            cal.disagreement = abs(cal.pixel_size_um / cal.table_pixel_size_um - 1.0) > CALIBRATION_DISAGREEMENT
        cal.message = (f"bar {ov.bar_len_px} px, label {label_um:g} µm, {objective or '?'} -> {cal.pixel_size_um:.3f} µm/px"
                       + (f" (table {cal.table_pixel_size_um:.3f}: {'DISAGREES' if cal.disagreement else 'ok'})" if cal.table_pixel_size_um else ""))
    else:
        cal.message = f"bar {ov.bar_len_px} px found but label unreadable; choose the objective manually"
    return ov, cal


def calibrate(dataset) -> tuple[Overlays, Calibration]:
    """Calibrate a scanned Dataset: reads the first WHITE frame (else F2, F3, F1) of the first position."""
    from .parsing import read_rgb  # local import: parsing is ported by the engine agent

    frame = None
    for ch in ("WHITE", "F2", "F3", "F1"):
        cands = [f for f in dataset.frames if f.channel == ch]
        if cands:
            frame = sorted(cands, key=lambda f: (f.roi, f.serial))[0]
            break
    if frame is None:
        return Overlays(), Calibration(source="none", message="no frames")
    return calibrate_frame(read_rgb(frame.path), str(frame.path))


def corner_crop(rgb: np.ndarray, overlays: Overlays | None = None, scale: int = 2) -> np.ndarray:
    """Bottom-right corner (or the detected bar/label region) as an RGB uint8 image scaled by `scale`."""
    h, w = rgb.shape[:2]
    if overlays and overlays.bar_box and overlays.label_box:
        y0 = max(0, min(overlays.label_box.y0, overlays.bar_box.y0) - 12)
        y1 = min(h, max(overlays.label_box.y1, overlays.bar_box.y1) + 12)
        x0 = max(0, min(overlays.label_box.x0, overlays.bar_box.x0) - 16)
        x1 = min(w, max(overlays.label_box.x1, overlays.bar_box.x1) + 16)
    else:
        y0, y1, x0, x1 = max(0, h - 160), h, max(0, w - 480), w
    crop = rgb[y0:y1, x0:x1]
    if scale > 1:
        crop = np.repeat(np.repeat(crop, scale, axis=0), scale, axis=1)
    return np.ascontiguousarray(crop)
