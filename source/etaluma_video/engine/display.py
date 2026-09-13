"""Display pipeline: planes -> RGB frame. Shared by the live preview and every export.

CONTRACT (docs/plans/v0.4_plan.md section 4.5):

  plane (uint8)
    -> optional rolling ball (fluorescence only)      subtract_background(plane, radius_px)
    -> apply_lut(plane, low, high, gamma) -> float32 in [0, 1]
    -> x channel colour -> RGB float
    -> blend across channels: screen 1 - prod(1 - c) | additive clip | max
    -> WHITE underlay (composite with WHITE only): weight * stretch(WHITE) ** gamma,
       screen-blended UNDER the fluorescence
    -> uint8 HxWx3 RGB

Auto methods on ChannelHistogram (overlay pixels excluded):
  adaptive : low = highest background peak + 1 robust sigma, high = percentile(99.9).
             "Background peaks" are histogram populations holding at least 5 % of the masked
             pixels (``BACKGROUND_PEAK_MASS``); sigma is the left HWHM of THAT peak, inside its
             own basin, over 1.177, minimum 1.0. With one background population this is exactly
             the plain "mode + sigma" rule; with several - a spheroid in an autofluorescent well
             gives a dark outside-well peak and a bright well peak - clipping above the *highest*
             one is what actually suppresses the well.
             If high - low < 8: fall back to classic when there is only one background population
             (no real signal above the noise), otherwise clamp low to high - 8 and keep clipping,
             because Classic's low = 0 would restore the very background just identified.
             ``auto_bounds_detail`` reports ``peaks``, ``peak_used``, ``method_used`` and
             ``reason`` so the UI/log can say what happened.
  classic  : low = 0, high = max over frames of per-frame p99.5 (ChannelHistogram.frame_p995_max);
             if that is 0 (histogram built without per-frame data) fall back to percentile(99.5)
  cut      : low = percentile(90), high = percentile(99.9)
WHITE standalone: low = p0.5, high = p99.5. Preset: median > WHITE_BRIGHTFIELD_MEDIAN ->
"brightfield" (gamma 2.0, weight 0.5) else "phase" (gamma 1.0, weight 0.6).

Every method clamps to [0, 255] and guarantees high > low.

Preview vs export
-----------------
The preview renders the 640-px cached planes and scales the rolling-ball radius by the same
downsample factor (radius * 640 / 1900); the export renders full-resolution planes with the
requested radius. The two are *visually equivalent, not pixel-identical*: the LUT stretch is
exact either way, but the morphological background estimate is computed on a different pixel
grid, so a rolling-ball preview differs from the exported frame by a fraction of a gray level
in smooth areas and slightly more at hard edges. Measurements and montages never go through
this module - they stay raw.

Performance
-----------
``apply_lut`` collapses the whole stretch (offset, scale, clip, gamma) into a 256-entry float32
table for uint8 input, so gamma is free and rendering is one ``cv2.LUT`` gather per channel plus
a handful of in-place vectorised multiplies on HxW component planes. Measured on the development
machine (Python 3.14, numpy 2.5, OpenCV 5.0), median of five runs:

  =============================================  =======  =========
  operation                                      640 px   1900 px
  =============================================  =======  =========
  render_frame, 3 fluorescence + WHITE            7.7 ms    67 ms
  render_frame, fluorescence only                 6.5 ms    59 ms
  render_single_channel                           3.4 ms    26 ms
  render_preview_variants (6 renders)              26 ms   214 ms
  subtract_background r=50                        1.0 ms   7.5 ms
  =============================================  =======  =========

The float maths is float32 throughout and the final cast rounds to nearest, which can differ by
one gray level from ``floor(v * 255 + 0.5)`` on exact halfway values. That is the only difference
from the naive formulation of the contract above.
"""
from __future__ import annotations

from functools import lru_cache

import cv2
import numpy as np

from .models import (
    AUTO_METHODS,
    BLEND_MODES,
    CHANNEL_ORDER,
    FLUOR_CHANNELS,
    WHITE_BRIGHTFIELD_MEDIAN,
    WHITE_PRESETS,
    Bounds,
    ChannelHistogram,
    DisplayProfile,
    HistogramSet,
)

__all__ = [
    "subtract_background",
    "apply_lut",
    "auto_bounds",
    "auto_bounds_detail",
    "auto_bounds_all",
    "white_bounds",
    "white_preset_for_median",
    "apply_white_preset",
    "estimate_white_median",
    "effective_bounds",
    "render_frame",
    "render_single_channel",
    "render_frame_fast",
    "render_single_channel_fast",
    "render_preview_variants",
    "profile_summary",
]

#: FWHM -> sigma conversion for a Gaussian peak: FWHM = 2 * sqrt(2 ln 2) * sigma = 2.3548 * sigma,
#: so a *half* width at half maximum is 1.1774 * sigma.
HWHM_TO_SIGMA = 1.177

#: Minimum robust sigma for Adaptive: a histogram spike one bin wide still moves the black point.
MIN_SIGMA = 1.0

#: Adaptive falls back to Classic when the window it proposes is narrower than this (gray levels).
MIN_ADAPTIVE_WINDOW = 8.0

#: Adaptive: a histogram peak holding at least this fraction of the masked pixels is a background
#: population, not signal. A 1.4 % spheroid never reaches it; a 19 % autofluorescent well does.
BACKGROUND_PEAK_MASS = 0.05

#: Adaptive: peaks closer together than this many bins belong to the same population.
PEAK_MIN_SEPARATION = 6


# --------------------------------------------------------------------------- #
# Rolling-ball background subtraction (decision 16)
# --------------------------------------------------------------------------- #


def _shrink_factor(radius_px: int) -> int:
    """ImageJ's shrink ladder: 1 / 2 / 4 / 8 for radius <=10 / <=30 / <=100 / >100."""
    if radius_px <= 10:
        return 1
    if radius_px <= 30:
        return 2
    if radius_px <= 100:
        return 4
    return 8


def subtract_background(plane: np.ndarray, radius_px: int, mask: np.ndarray | None = None) -> np.ndarray:
    """ImageJ-style rolling-ball background subtraction on a uint8 plane (returns uint8).

    The background is a morphological opening (erode then dilate) with an elliptical structuring
    element of diameter ``2r' + 1`` computed on a shrunk copy of the plane, resized back to the
    original size and subtracted with clipping at 0. The shrink is what makes this fast: a
    1900x1900 plane with radius 50 is opened with a 25-px kernel on a 475x475 image.

    ``radius_px <= 0`` returns the plane unchanged. Non-uint8 input is clipped and cast.

    ``mask`` (True on Lumaview's burned-in boxes, same shape as the plane): those pixels are filled
    with the median background before the opening. Left in, the exact-black clock box drags the
    background estimate down around itself and leaves a bright band beside the box (0.6 fix).
    """
    plane = np.asarray(plane)
    if plane.ndim != 2:
        raise ValueError(f"subtract_background expects a 2-D plane, got shape {plane.shape}")
    if plane.dtype != np.uint8:
        plane = np.clip(plane, 0, 255).astype(np.uint8)
    radius = int(radius_px)
    if radius <= 0:
        return plane

    height, width = plane.shape
    shrink = _shrink_factor(radius)
    if shrink > 1:
        small = cv2.resize(
            plane,
            (max(1, width // shrink), max(1, height // shrink)),
            interpolation=cv2.INTER_AREA,
        )
    else:
        small = plane
    if mask is not None and mask.shape == plane.shape and mask.any():
        small_mask = mask if small is plane else cv2.resize(
            mask.astype(np.uint8), (small.shape[1], small.shape[0]), interpolation=cv2.INTER_NEAREST).astype(bool)
        if small_mask.any() and not small_mask.all():
            small = small.copy() if small is plane else small
            small[small_mask] = np.uint8(np.median(small[~small_mask]))
    r_small = max(1, int(round(radius / shrink)))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r_small + 1, 2 * r_small + 1))
    background = cv2.morphologyEx(small, cv2.MORPH_OPEN, kernel)
    if background.shape != plane.shape:
        # dsize is (width, height); resize to the exact original shape so odd sizes survive.
        background = cv2.resize(background, (width, height), interpolation=cv2.INTER_LINEAR)
    return cv2.subtract(plane, background)  # uint8 subtraction saturates at 0


# --------------------------------------------------------------------------- #
# LUT
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=256)
def _lut_table(low: float, high: float, gamma: float) -> np.ndarray:
    """256-entry float32 lookup table of ``clip((v - low) / (high - low), 0, 1) ** gamma``.

    Cached and read-only: scrubbing a timeline re-uses the same table for every frame.
    """
    values = np.arange(256, dtype=np.float32)
    if high <= low:
        table = values / np.float32(255.0)
    else:
        table = (values - np.float32(low)) / np.float32(high - low)
        np.clip(table, 0.0, 1.0, out=table)
        if gamma != 1.0:
            table = np.power(table, np.float32(gamma), dtype=np.float32)
    table = np.clip(table, 0.0, 1.0).astype(np.float32)
    table.flags.writeable = False
    return table


def apply_lut(plane: np.ndarray, low: float, high: float, gamma: float = 1.0) -> np.ndarray:
    """Linear stretch between low and high, clipped to [0, 1], then gamma. Returns float32.

    ``high <= low`` degenerates to ``plane / 255`` (the raw plane), which is what the UI shows
    while a user drags the two handles past each other.

    The returned array is always freshly allocated, so callers may modify it in place.
    """
    plane = np.asarray(plane)
    low, high, gamma = float(low), float(high), float(gamma)
    if plane.dtype == np.uint8:
        table = _lut_table(low, high, gamma)
        try:
            # cv2.LUT is ~4x faster than fancy indexing (numpy widens the uint8 index to intp).
            return cv2.LUT(plane, table)
        except cv2.error:  # pragma: no cover - exotic layouts only
            return table[plane]
    values = plane.astype(np.float32)
    if high <= low:
        return np.clip(values / np.float32(255.0), 0.0, 1.0)
    values = (values - np.float32(low)) / np.float32(high - low)
    np.clip(values, 0.0, 1.0, out=values)
    if gamma != 1.0:
        values = np.power(values, np.float32(gamma), dtype=np.float32)
    return values


# --------------------------------------------------------------------------- #
# Automatic bounds (decision 11)
# --------------------------------------------------------------------------- #


def _half_maximum_crossing(counts: np.ndarray, mode: int, lo: int = 0, hi: int | None = None) -> tuple[float | None, str]:
    """Half-width at half-maximum of the histogram peak at ``mode``, preferring its left flank.

    Returns ``(hwhm, side)`` with ``side`` in {"left", "right", "none"}. The crossing is linearly
    interpolated between the two bins that straddle half the peak height, so a narrow background
    peak still yields sub-bin precision.

    ``lo``/``hi`` bound the search to the peak's own basin. That matters when the histogram has
    more than one background population: without it, the walk down the left flank of the *upper*
    peak would run straight into the rising flank of the taller lower peak, never cross half
    maximum there, and only find one far below it - yielding an absurdly wide sigma.
    """
    hi = int(counts.size) if hi is None else int(hi)
    peak = float(counts[mode])
    if peak <= 0:
        return None, "none"
    half = peak / 2.0

    # Left flank: walk down from the peak to the first bin at or below half maximum.
    for i in range(mode - 1, lo - 1, -1):
        if counts[i] <= half:
            span = float(counts[i + 1] - counts[i])
            frac = (half - float(counts[i])) / span if span > 0 else 1.0
            crossing = i + frac
            hwhm = mode - crossing
            if hwhm > 0:
                return hwhm, "left"
            break

    # Right flank (the peak sits at the bottom of its basin, or the left flank never halves).
    for j in range(mode + 1, hi):
        if counts[j] <= half:
            span = float(counts[j - 1] - counts[j])
            frac = (float(counts[j - 1]) - half) / span if span > 0 else 1.0
            crossing = (j - 1) + frac
            hwhm = crossing - mode
            if hwhm > 0:
                return hwhm, "right"
            break
    return None, "none"


def _smooth_counts(counts: np.ndarray) -> np.ndarray:
    """3-bin moving average with edge replication, so the end bins stay comparable."""
    padded = np.concatenate(([counts[0]], counts, [counts[-1]])).astype(np.float64)
    return (padded[:-2] + padded[1:-1] + padded[2:]) / 3.0


def _find_peaks(counts: np.ndarray) -> list[dict]:
    """Intensity populations in a 256-bin histogram, in increasing intensity order.

    Each entry is ``{"bin", "mass", "basin"}``: the bin is the *raw* mode inside the population's
    basin, the mass is the fraction of all pixels the basin holds, and the basin is the half-open
    bin range between the neighbouring valleys.

    Peaks are found on the lightly smoothed counts (so photon noise does not split one population
    into several) and merged when closer than ``PEAK_MIN_SEPARATION`` bins, tallest winning. With
    a single population the basin is the whole histogram and the bin is exactly ``argmax(counts)``,
    which keeps single-peak behaviour bit-for-bit identical to the plain-mode rule.
    """
    total = float(counts.sum())
    smooth = _smooth_counts(counts)
    n = int(smooth.size)
    candidates = [
        i for i in range(n)
        if (i == 0 or smooth[i] > smooth[i - 1]) and (i == n - 1 or smooth[i] >= smooth[i + 1])
    ]
    if not candidates:
        candidates = [int(np.argmax(smooth))]

    kept: list[int] = []
    for i in sorted(candidates, key=lambda b: (-smooth[b], b)):  # tallest first wins a collision
        if all(abs(i - j) >= PEAK_MIN_SEPARATION for j in kept):
            kept.append(i)
    kept.sort()

    # Valleys are searched strictly between two peaks (peaks are >= PEAK_MIN_SEPARATION apart, so
    # the open interval is never empty). That keeps every basin non-empty and containing its peak
    # even when the histogram is flat enough for argmin to land on a tie.
    edges = [0]
    for a, b in zip(kept, kept[1:]):
        edges.append(a + 1 + int(np.argmin(smooth[a + 1 : b])))
    edges.append(n)

    peaks: list[dict] = []
    for k, _ in enumerate(kept):
        lo_edge, hi_edge = edges[k], edges[k + 1]
        basin = counts[lo_edge:hi_edge]
        peaks.append({
            "bin": lo_edge + int(np.argmax(basin)),
            "mass": (float(basin.sum()) / total) if total > 0 else 0.0,
            "basin": (lo_edge, hi_edge),
        })
    return peaks


def _finalise(low: float, high: float) -> tuple[float, float]:
    """Clamp to [0, 255] and guarantee ``high > low``."""
    low = float(np.clip(low, 0.0, 255.0))
    high = float(np.clip(high, 0.0, 255.0))
    if high <= low:
        if low >= 254.0:
            low, high = 254.0, 255.0
        else:
            high = low + 1.0
    return low, high


def _classic_bounds(hist: ChannelHistogram) -> tuple[float, float, float]:
    """(low, high, high_source_value) for Classic."""
    running = float(hist.frame_p995_max)
    high = running if running > 0 else float(hist.percentile(99.5))
    return 0.0, max(1.0, high), running


def auto_bounds_detail(hist: ChannelHistogram, method: str) -> dict:
    """Automatic bounds for one fluorescence channel plus the numbers behind them.

    Keys: ``method`` (requested), ``method_used`` (differs when Adaptive falls back to Classic),
    ``low``, ``high``, ``fallback`` (bool), ``reason`` (short text for the log, "" when none),
    ``pixels``, and for Adaptive also ``mode``, ``hwhm``, ``hwhm_side``, ``sigma``, ``p99_9``.
    Classic reports ``frame_p995_max``; Cut reports ``p90`` and ``p99_9``.

    ``auto_bounds`` is this function's ``(low, high)``, so the two can never disagree.
    """
    method = str(method).lower()
    if method not in AUTO_METHODS:
        raise ValueError(f"unknown auto method {method!r}; expected one of {AUTO_METHODS}")

    counts = np.asarray(hist.counts, dtype=np.int64)
    detail: dict = {"method": method, "method_used": method, "fallback": False, "reason": "",
                    "pixels": int(counts.sum())}
    if detail["pixels"] <= 0:
        detail.update(low=0.0, high=1.0, reason="empty histogram")
        return detail

    if method == "classic":
        low, high, running = _classic_bounds(hist)
        detail["frame_p995_max"] = running
        if running <= 0:
            detail["reason"] = "no per-frame p99.5 recorded; used pooled p99.5"
        detail["low"], detail["high"] = _finalise(low, high)
        return detail

    if method == "cut":
        low, high = float(hist.percentile(90.0)), float(hist.percentile(99.9))
        detail["p90"], detail["p99_9"] = low, high
        detail["low"], detail["high"] = _finalise(low, high)
        return detail

    # Adaptive: the black point is built from the HIGHEST-intensity background population, not
    # simply the tallest one. A spheroid in an autofluorescent well gives two background peaks -
    # the dark medium outside the well and the glowing well itself - and only clipping above the
    # upper one suppresses the well.
    mode = int(np.argmax(counts))
    peaks = _find_peaks(counts)
    background = [p for p in peaks if p["mass"] >= BACKGROUND_PEAK_MASS]
    chosen = max(background, key=lambda p: p["bin"]) if background else \
        {"bin": mode, "mass": 1.0, "basin": (0, int(counts.size))}
    peak_used = int(chosen["bin"])
    hwhm, side = _half_maximum_crossing(counts, peak_used, chosen["basin"][0], chosen["basin"][1])
    sigma = max(MIN_SIGMA, (hwhm / HWHM_TO_SIGMA) if hwhm is not None else MIN_SIGMA)
    high = float(hist.percentile(99.9))
    low = float(peak_used) + sigma
    detail.update(mode=mode, peak_used=peak_used, peak_mass=float(chosen["mass"]),
                  peaks=[(int(p["bin"]), round(float(p["mass"]), 4)) for p in peaks],
                  background_peaks=len(background), hwhm=hwhm, hwhm_side=side, sigma=sigma, p99_9=high)
    if len(background) > 1:
        detail["reason"] = (f"{len(background)} background populations "
                            f"{[int(p['bin']) for p in background]}; clipped above the highest")
    if high - low < MIN_ADAPTIVE_WINDOW:
        if len(background) > 1:
            # With several background populations a narrow window does NOT mean "no signal above
            # the noise" - it means the signal only just clears a bright background we have
            # positively identified. Falling back to Classic would put the black point at 0 and
            # restore exactly the background we set out to clip, so clamp the window instead and
            # keep the clipping. (Measured on Insphero F2: Classic gives a disc luminance of 116,
            # the clamp gives 19.)
            low = max(0.0, high - MIN_ADAPTIVE_WINDOW)
            detail["reason"] = (f"window {high - float(peak_used) - sigma:.1f} < {MIN_ADAPTIVE_WINDOW:.0f} "
                                f"gray levels; clamped to {MIN_ADAPTIVE_WINDOW:.0f} above background peak {peak_used}")
        else:
            c_low, c_high, running = _classic_bounds(hist)
            detail.update(method_used="classic", fallback=True, frame_p995_max=running,
                          reason=f"adaptive window {high - low:.1f} < {MIN_ADAPTIVE_WINDOW:.0f} gray levels; used classic")
            detail["low"], detail["high"] = _finalise(c_low, c_high)
            return detail
    detail["low"], detail["high"] = _finalise(low, high)
    return detail


def auto_bounds(hist: ChannelHistogram, method: str) -> tuple[float, float]:
    """Automatic (low, high) for one fluorescence channel. See ``auto_bounds_detail``."""
    detail = auto_bounds_detail(hist, method)
    return detail["low"], detail["high"]


def auto_bounds_all(hists: HistogramSet, method: str) -> Bounds:
    """Automatic bounds for every fluorescence channel in the set (WHITE uses white_bounds)."""
    out: Bounds = {}
    for ch, h in hists.channels.items():
        out[ch] = white_bounds(h) if ch == "WHITE" else auto_bounds(h, method)
    return out


def white_bounds(hist: ChannelHistogram) -> tuple[float, float]:
    """Standalone WHITE stretch: p0.5 -> p99.5."""
    lo, hi = hist.percentile(0.5), hist.percentile(99.5)
    return (lo, hi) if hi > lo else (0.0, 255.0)


def white_preset_for_median(median: float) -> str:
    """Brightfield above the median threshold (decision 15), phase contrast below it."""
    return "brightfield" if median > WHITE_BRIGHTFIELD_MEDIAN else "phase"


def estimate_white_median(plane: np.ndarray, mask: np.ndarray | None = None) -> float:
    """Median of the WHITE plane, overlay pixels excluded (``mask`` True = overlay)."""
    plane = np.asarray(plane)
    values = plane[~mask] if mask is not None else plane
    if values.size == 0:
        return 0.0
    return float(np.median(values))


def apply_white_preset(profile: DisplayProfile, preset: str) -> DisplayProfile:
    """Set ``profile.white.preset/.gamma/.weight`` from WHITE_PRESETS. Returns the same profile."""
    if preset not in WHITE_PRESETS:
        raise ValueError(f"unknown WHITE preset {preset!r}; expected one of {tuple(WHITE_PRESETS)}")
    values = WHITE_PRESETS[preset]
    profile.white.preset = preset
    profile.white.gamma = float(values["gamma"])
    profile.white.weight = float(values["weight"])
    return profile


def effective_bounds(profile: DisplayProfile, auto: Bounds | None) -> Bounds:
    """Bounds actually used for rendering: manual values in manual mode, else the automatic ones.

    Auto mode falls back to (0, 255) for any channel the automatic pass has not covered yet;
    WHITE falls back to the profile's stored standalone bounds.
    """
    out: Bounds = {}
    for ch, cd in profile.channels.items():
        if profile.mode == "manual":
            out[ch] = (cd.low, cd.high)
        elif auto and ch in auto:
            out[ch] = auto[ch]
        else:
            out[ch] = (0.0, 255.0)
    if profile.mode == "manual" or not auto or "WHITE" not in auto:
        out["WHITE"] = (profile.white.low, profile.white.high)
    else:
        out["WHITE"] = auto["WHITE"]
    return out


# --------------------------------------------------------------------------- #
# Blending and rendering
# --------------------------------------------------------------------------- #


ONE = np.float32(1.0)


def _composite(grays: list[np.ndarray], colours: list[tuple[float, float, float]], mode: str,
               inv_under: np.ndarray | None = None) -> list[np.ndarray]:
    """Blend coloured layers and screen the WHITE underlay beneath them.

    ``grays`` are float32 HxW in [0, 1]; ``colours`` are RGB multipliers; ``inv_under`` is
    ``1 - weight * stretch(WHITE)`` or None. Returns the three RGB component planes (float32
    HxW, clipped to [0, 1]).

    Reference semantics, layer ``c_i = gray_i * colour_i``:
        screen    rgb = 1 - prod(1 - c_i)
        additive  rgb = clip(sum(c_i))
        max       rgb = max(c_i)
    then, with the underlay,   rgb = 1 - (1 - under) * (1 - rgb).

    For ``screen`` the underlay is algebraically just one more factor in the same product, so it
    is folded in and only one inversion happens at the end. The work is done one output component
    at a time, in place on contiguous HxW arrays: that avoids HxWx3 temporaries and skips every
    component a channel colour does not touch (green touches one of three, cyan two).
    """
    if not grays:
        raise ValueError("no layers to blend")
    if mode not in BLEND_MODES:
        raise ValueError(f"unknown blend mode {mode!r}; expected one of {BLEND_MODES}")
    shape = grays[0].shape

    if mode == "screen":
        # acc holds prod(1 - c_i), i.e. the complement of the blended value.
        acc = [np.ones(shape, dtype=np.float32) for _ in range(3)]
        for gray, colour in zip(grays, colours):
            inverse: np.ndarray | None = None
            for k in range(3):
                weight = float(colour[k])
                if weight == 0.0:
                    continue
                if weight == 1.0:
                    if inverse is None:
                        inverse = ONE - gray
                    acc[k] *= inverse
                else:
                    acc[k] *= ONE - gray * np.float32(weight)
        if inv_under is not None:
            for k in range(3):
                acc[k] *= inv_under
        for k in range(3):
            np.subtract(ONE, acc[k], out=acc[k])
            np.clip(acc[k], 0.0, 1.0, out=acc[k])
        return acc

    acc = [np.zeros(shape, dtype=np.float32) for _ in range(3)]
    for gray, colour in zip(grays, colours):
        for k in range(3):
            weight = float(colour[k])
            if weight == 0.0:
                continue
            scaled = gray if weight == 1.0 else gray * np.float32(weight)
            if mode == "additive":
                acc[k] += scaled
            else:  # max
                np.maximum(acc[k], scaled, out=acc[k])
    for k in range(3):
        np.clip(acc[k], 0.0, 1.0, out=acc[k])
    if inv_under is not None:
        for k in range(3):
            np.subtract(ONE, acc[k], out=acc[k])
            acc[k] *= inv_under
            np.subtract(ONE, acc[k], out=acc[k])
            np.clip(acc[k], 0.0, 1.0, out=acc[k])
    return acc


def _to_uint8(planes: list[np.ndarray]) -> np.ndarray:
    """Three float32 component planes in [0, 1] -> one contiguous uint8 HxWx3 RGB frame.

    Values are rounded to nearest (cv2's saturating cast); the clip is mandatory because
    ``convertScaleAbs`` takes an absolute value and would fold a negative into a positive.
    """
    for p in planes:
        np.clip(p, 0.0, 1.0, out=p)
    return cv2.convertScaleAbs(cv2.merge(planes), alpha=255.0)


def _fluorescence_plane(plane: np.ndarray, profile: DisplayProfile, mask: np.ndarray | None = None) -> np.ndarray:
    """Rolling ball applied to a fluorescence plane when the profile asks for it."""
    if profile.rolling_ball.enabled and profile.rolling_ball.radius_px > 0:
        return subtract_background(plane, profile.rolling_ball.radius_px, mask)
    return plane


def _white_underlay_inverse(plane: np.ndarray, profile: DisplayProfile, bounds: Bounds) -> np.ndarray:
    """``1 - weight * stretch(WHITE) ** gamma`` as a float32 HxW layer (the screen factor)."""
    preset = WHITE_PRESETS.get(profile.white.preset, WHITE_PRESETS["phase"])
    gamma = profile.white.gamma if profile.white.gamma else preset["gamma"]
    lo, hi = bounds.get("WHITE", (0.0, 255.0))
    under = apply_lut(plane, lo, hi, gamma)  # freshly allocated, safe to use as a buffer
    under *= np.float32(profile.white.weight)
    np.subtract(ONE, under, out=under)
    return under


def _colour_planes(gray: np.ndarray, colour: tuple[float, float, float]) -> list[np.ndarray]:
    """One gray layer times an RGB colour, as three component planes."""
    out: list[np.ndarray] = []
    for k in range(3):
        weight = float(colour[k])
        if weight == 0.0:
            out.append(np.zeros_like(gray))
        elif weight == 1.0:
            out.append(gray)
        else:
            out.append(gray * np.float32(weight))
    return out


def render_single_channel(plane: np.ndarray, channel: str, profile: DisplayProfile, bounds: Bounds,
                          background_mask: np.ndarray | None = None) -> np.ndarray:
    """One channel as an RGB uint8 frame (its own colour; WHITE as gray).

    Used for the per-channel videos and the viewer's per-channel chips. ``enabled`` is ignored
    here: a per-channel video is produced for every channel that exists (decision 14). WHITE is
    rendered with its standalone stretch and gamma 1.0, never with the underlay weight.
    """
    lo, hi = bounds.get(channel, (0.0, 255.0))
    if channel == "WHITE":
        gray = apply_lut(plane, lo, hi, 1.0)
        return _to_uint8([gray, gray, gray])
    cd = profile.channels.get(channel)
    gray = apply_lut(_fluorescence_plane(plane, profile, background_mask), lo, hi, cd.gamma if cd is not None else 1.0)
    return _to_uint8(_colour_planes(gray, cd.colour if cd is not None else (1.0, 1.0, 1.0)))


def render_frame(planes: dict[str, np.ndarray], profile: DisplayProfile, bounds: Bounds,
                 include_white: bool = True, background_mask: np.ndarray | None = None) -> np.ndarray:
    """Composite RGB uint8 frame from the given planes.

    ``planes``: {channel: uint8 HxW}. Fluorescence channels missing from the profile or disabled
    in it are skipped. ``include_white=False`` renders the fluorescence-only composite.

    Edge cases, kept stable for every caller:
      * no fluorescence layer and WHITE present -> the WHITE frame as gray (its standalone stretch);
        this is the WHITE-only dataset case and it ignores ``white.enabled``.
      * no fluorescence layer, ``include_white=False`` -> a black frame the size of the planes
        given (a fluorescence-only composite genuinely has nothing in it).
      * no planes at all -> ValueError.

    Preview and export must both call this function; nothing else composes pixels.
    """
    fluor = [
        ch for ch in CHANNEL_ORDER
        if ch in FLUOR_CHANNELS and ch in planes and ch in profile.channels and profile.channels[ch].enabled
    ]
    grays: list[np.ndarray] = []
    colours: list[tuple[float, float, float]] = []
    for ch in fluor:
        cd = profile.channels[ch]
        lo, hi = bounds.get(ch, (0.0, 255.0))
        grays.append(apply_lut(_fluorescence_plane(planes[ch], profile, background_mask), lo, hi, cd.gamma))
        colours.append(tuple(float(c) for c in cd.colour))

    if not grays:
        if not planes:
            raise ValueError("no planes to render")
        if include_white and "WHITE" in planes:
            lo, hi = bounds.get("WHITE", (0.0, 255.0))
            gray = apply_lut(planes["WHITE"], lo, hi, 1.0)
            return _to_uint8([gray, gray, gray])
        shape = np.asarray(next(iter(planes.values()))).shape
        return np.zeros((shape[0], shape[1], 3), dtype=np.uint8)

    inv_under = None
    if include_white and "WHITE" in planes and profile.white.enabled:
        inv_under = _white_underlay_inverse(planes["WHITE"], profile, bounds)
    return _to_uint8(_composite(grays, colours, profile.blend, inv_under))


# --------------------------------------------------------------------------- #
# 8-bit renderer (0.6)
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1024)
def _lut_u8(low: float, high: float, gamma: float, weight: float = 1.0, invert: bool = False) -> np.ndarray:
    """uint8 table of ``weight * stretch ** gamma`` (or one minus it): stretch, colour and inversion in one."""
    values = _lut_table(float(low), float(high), float(gamma)) * np.float32(weight)
    if invert:
        values = np.float32(1.0) - values
    table = np.clip(np.rint(values * np.float32(255.0)), 0, 255).astype(np.uint8)
    table.flags.writeable = False
    return table


_INV255 = 1.0 / 255.0


def render_frame_fast(planes: dict[str, np.ndarray], profile: DisplayProfile, bounds: Bounds,
                      include_white: bool = True, background_mask: np.ndarray | None = None) -> np.ndarray:
    """:func:`render_frame` in 8-bit arithmetic: within a gray level or two of it, several times faster.

    At 1900 px the float32 pipeline moves about 300 MB of memory per frame and was most of 0.5's
    export time. Here each channel costs one table lookup per colour component (stretch, gamma,
    colour weight and, for screen, the inversion folded into one uint8 table) and the screen
    blend is a chain of ``cv2.multiply(..., scale=1/255)``. The preview, the videos, the montages
    and the fixed-image panels all use this function; ``render_frame`` is the reference the tests
    compare it with. Same edge cases as ``render_frame``.
    """
    fluor = [
        ch for ch in CHANNEL_ORDER
        if ch in FLUOR_CHANNELS and ch in planes and ch in profile.channels and profile.channels[ch].enabled
    ]
    if not fluor:
        if not planes:
            raise ValueError("no planes to render")
        if include_white and "WHITE" in planes:
            lo, hi = bounds.get("WHITE", (0.0, 255.0))
            gray = cv2.LUT(np.ascontiguousarray(planes["WHITE"]), _lut_u8(float(lo), float(hi), 1.0))
            return cv2.merge([gray, gray, gray])
        shape = np.asarray(next(iter(planes.values()))).shape
        return np.zeros((shape[0], shape[1], 3), dtype=np.uint8)
    if profile.blend not in BLEND_MODES:
        raise ValueError(f"unknown blend mode {profile.blend!r}; expected one of {BLEND_MODES}")
    layers = []
    for ch in fluor:
        cd = profile.channels[ch]
        lo, hi = bounds.get(ch, (0.0, 255.0))
        plane = np.ascontiguousarray(_fluorescence_plane(planes[ch], profile, background_mask))
        layers.append((plane, float(lo), float(hi), float(cd.gamma), tuple(float(c) for c in cd.colour)))
    shape = layers[0][0].shape
    inv_under = None
    if include_white and "WHITE" in planes and profile.white.enabled:
        preset = WHITE_PRESETS.get(profile.white.preset, WHITE_PRESETS["phase"])
        gamma = profile.white.gamma if profile.white.gamma else preset["gamma"]
        lo, hi = bounds.get("WHITE", (0.0, 255.0))
        inv_under = cv2.LUT(np.ascontiguousarray(planes["WHITE"]),
                            _lut_u8(float(lo), float(hi), float(gamma), float(profile.white.weight), True))
    components = []
    for k in range(3):
        acc = None
        if profile.blend == "screen":  # acc = prod(1 - c_i) * 255
            for plane, lo, hi, gamma, colour in layers:
                if colour[k] > 0.0:
                    inverse = cv2.LUT(plane, _lut_u8(lo, hi, gamma, colour[k], True))
                    acc = inverse if acc is None else cv2.multiply(acc, inverse, scale=_INV255)
            if inv_under is not None:
                acc = inv_under if acc is None else cv2.multiply(acc, inv_under, scale=_INV255)
            components.append(np.zeros(shape, np.uint8) if acc is None else cv2.bitwise_not(acc))
            continue
        for plane, lo, hi, gamma, colour in layers:
            if colour[k] > 0.0:
                layer = cv2.LUT(plane, _lut_u8(lo, hi, gamma, colour[k]))
                if acc is None:
                    acc = layer
                else:
                    acc = cv2.add(acc, layer) if profile.blend == "additive" else cv2.max(acc, layer)
        if acc is None:
            acc = np.zeros(shape, np.uint8)
        if inv_under is not None:  # rgb = 1 - (1 - under)(1 - rgb)
            acc = cv2.bitwise_not(cv2.multiply(cv2.bitwise_not(acc), inv_under, scale=_INV255))
        components.append(acc)
    return cv2.merge(components)


def render_single_channel_fast(plane: np.ndarray, channel: str, profile: DisplayProfile, bounds: Bounds,
                               background_mask: np.ndarray | None = None) -> np.ndarray:
    """:func:`render_single_channel` in 8-bit arithmetic (same rules: WHITE gray with gamma 1)."""
    lo, hi = bounds.get(channel, (0.0, 255.0))
    if channel == "WHITE":
        gray = cv2.LUT(np.ascontiguousarray(plane), _lut_u8(float(lo), float(hi), 1.0))
        return cv2.merge([gray, gray, gray])
    cd = profile.channels.get(channel)
    gamma = float(cd.gamma) if cd is not None else 1.0
    colour = tuple(float(c) for c in cd.colour) if cd is not None else (1.0, 1.0, 1.0)
    source = np.ascontiguousarray(_fluorescence_plane(plane, profile, background_mask))
    return cv2.merge([cv2.LUT(source, _lut_u8(float(lo), float(hi), gamma, c)) if c > 0.0 else np.zeros_like(source)
                      for c in colour])


def render_preview_variants(planes: dict[str, np.ndarray], profile: DisplayProfile,
                            bounds: Bounds) -> dict[str, np.ndarray]:
    """Every view the viewer offers as a chip, rendered once.

    Returns ``{"composite", "fluorescence_only"?, <channel>...}``. ``fluorescence_only`` is
    present only when at least one enabled fluorescence channel exists (decision 14). The
    per-channel entries cover every channel in ``planes``, enabled or not, because per-channel
    videos are always produced.
    """
    out: dict[str, np.ndarray] = {}
    if not planes:
        return out
    out["composite"] = render_frame(planes, profile, bounds, include_white=True)
    has_fluor = any(
        ch in planes and ch in profile.channels and profile.channels[ch].enabled for ch in FLUOR_CHANNELS
    )
    if has_fluor:
        out["fluorescence_only"] = render_frame(planes, profile, bounds, include_white=False)
    for ch in CHANNEL_ORDER:
        if ch in planes:
            out[ch] = render_single_channel(planes[ch], ch, profile, bounds)
    return out


def _fmt(value: float) -> str:
    """Bounds as a human reads them: 6, 110, 24 (they are gray levels, so no decimals)."""
    return f"{value:.0f}"


def _fmt_gamma(value: float) -> str:
    """Gamma always carries a decimal so 1.0 does not read as "1": 1.0, 1.2, 0.75."""
    value = float(value)
    return f"{value:.1f}" if value == round(value, 1) else f"{value:g}"


def profile_summary(profile: DisplayProfile, bounds: Bounds) -> str:
    """One line for the log, e.g. ``F2 6-110 g1.0 - F3 4-73 g1.2 - WHITE phase w0.6 - screen``.

    (En dashes and a real gamma character in the actual output.) Disabled fluorescence channels
    appear as ``F1 off`` so the line always accounts for every channel; the rolling-ball radius is
    appended only when it is on.
    """
    parts: list[str] = []
    for ch in CHANNEL_ORDER:
        if ch not in profile.channels or ch not in FLUOR_CHANNELS:
            continue
        cd = profile.channels[ch]
        if not cd.enabled:
            parts.append(f"{ch} off")
            continue
        lo, hi = bounds.get(ch, (cd.low, cd.high))
        parts.append(f"{ch} {_fmt(lo)}–{_fmt(hi)} γ{_fmt_gamma(cd.gamma)}")
    if profile.white.enabled:
        parts.append(f"WHITE {profile.white.preset} w{profile.white.weight:g}")
    else:
        parts.append("WHITE off")
    parts.append(profile.blend)
    if profile.rolling_ball.enabled and profile.rolling_ball.radius_px > 0:
        parts.append(f"rolling ball r{int(profile.rolling_ball.radius_px)}")
    return " · ".join(parts)
