"""Tests for engine/display.py: LUTs, automatic bounds, blends, WHITE presets, rolling ball.

Synthetic arrays only, deterministic seeds. Real-data checks live in the probe scripts under
private/probe/display (see docs/ALGORITHMS.md); nothing here needs the sample set.
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from etaluma_video.engine import display as D
from etaluma_video.engine.models import (
    AUTO_METHODS,
    WHITE_PRESETS,
    ChannelDisplay,
    ChannelHistogram,
    DisplayProfile,
    HistogramSet,
)

SEED = 20260912


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def flat_profile(colours: dict[str, tuple[float, float, float]], blend: str = "screen") -> DisplayProfile:
    """Profile with the given channels/colours, bounds 0-255, gamma 1, WHITE off."""
    p = DisplayProfile()
    for ch, colour in colours.items():
        p.channels[ch] = ChannelDisplay(enabled=True, colour=colour, low=0.0, high=255.0, gamma=1.0)
    p.blend = blend
    p.white.enabled = False
    return p


def const_plane(value: int, size: int = 8) -> np.ndarray:
    return np.full((size, size), value, dtype=np.uint8)


def full_bounds(*channels: str) -> dict[str, tuple[float, float]]:
    return {ch: (0.0, 255.0) for ch in channels}


def hist_from_frames(frames: list[np.ndarray]) -> ChannelHistogram:
    """Accumulate a ChannelHistogram exactly as the histogram pass does (fills frame_p995_max)."""
    h = ChannelHistogram()
    for f in frames:
        h.add_frame(np.asarray(f, dtype=np.uint8))
    return h


def sparse_series(n_frames: int = 6, size: int = 200, seed: int = SEED) -> list[np.ndarray]:
    """Background essentially a delta at 0, plus bright blobs over ~4 % of the frame."""
    rng = np.random.default_rng(seed)
    frames = []
    for _ in range(n_frames):
        f = np.zeros((size, size), dtype=np.uint8)
        f[rng.random((size, size)) < 0.02] = 1  # a whisper of shot noise, mode stays at 0
        for _ in range(4):
            cy, cx = rng.integers(20, size - 20, 2)
            f[cy - 10 : cy + 10, cx - 10 : cx + 10] = 200
        frames.append(f)
    return frames


def dense_series(n_frames: int = 6, size: int = 200, seed: int = SEED) -> list[np.ndarray]:
    """Uniform bright background 120 +/- 5 with a few blobs at 240."""
    rng = np.random.default_rng(seed)
    frames = []
    for _ in range(n_frames):
        f = rng.normal(120.0, 5.0, (size, size)).clip(0, 255)
        for _ in range(3):
            cy, cx = rng.integers(20, size - 20, 2)
            f[cy - 10 : cy + 10, cx - 10 : cx + 10] = 240
        frames.append(f.astype(np.uint8))
    return frames


# --------------------------------------------------------------------------- #
# apply_lut
# --------------------------------------------------------------------------- #


def test_apply_lut_bounds_and_dtype():
    plane = np.array([[0, 10, 50, 100, 200, 255]], dtype=np.uint8)
    v = D.apply_lut(plane, 50, 200, 1.0)
    assert v.dtype == np.float32
    assert v.min() >= 0.0 and v.max() <= 1.0
    assert v[0, 0] == pytest.approx(0.0)  # below low -> clipped to 0
    assert v[0, 2] == pytest.approx(0.0)  # exactly low -> 0
    assert v[0, 4] == pytest.approx(1.0)  # exactly high -> 1
    assert v[0, 5] == pytest.approx(1.0)  # above high -> clipped to 1
    assert v[0, 3] == pytest.approx((100 - 50) / 150.0, abs=1e-6)


def test_apply_lut_matches_the_formula_for_every_level():
    plane = np.arange(256, dtype=np.uint8).reshape(16, 16)
    for low, high, gamma in ((0, 255, 1.0), (17, 200, 1.0), (17, 200, 2.0), (30, 60, 0.5)):
        expected = np.clip((plane.astype(np.float64) - low) / (high - low), 0, 1) ** gamma
        assert np.allclose(D.apply_lut(plane, low, high, gamma), expected, atol=1e-6)


def test_apply_lut_gamma_darkens_midtones_and_keeps_endpoints():
    plane = np.array([[0, 128, 255]], dtype=np.uint8)
    linear = D.apply_lut(plane, 0, 255, 1.0)
    dark = D.apply_lut(plane, 0, 255, 2.0)
    bright = D.apply_lut(plane, 0, 255, 0.5)
    assert dark[0, 1] < linear[0, 1] < bright[0, 1]
    assert dark[0, 0] == pytest.approx(0.0) and bright[0, 0] == pytest.approx(0.0)
    assert dark[0, 2] == pytest.approx(1.0) and bright[0, 2] == pytest.approx(1.0)
    assert dark[0, 1] == pytest.approx(linear[0, 1] ** 2, abs=1e-6)


def test_apply_lut_degenerate_high_le_low_returns_the_raw_plane():
    plane = np.array([[0, 64, 128, 255]], dtype=np.uint8)
    for low, high in ((100, 100), (200, 50)):
        v = D.apply_lut(plane, low, high, 1.0)
        assert np.allclose(v, plane / 255.0, atol=1e-6)


def test_apply_lut_accepts_a_non_contiguous_plane():
    rgb = np.random.default_rng(SEED).integers(0, 256, (32, 32, 3), dtype=np.uint8)
    plane = rgb[:, :, 1]  # what read_plane hands us: a strided view, not a copy
    assert not plane.flags["C_CONTIGUOUS"]
    assert np.allclose(D.apply_lut(plane, 10, 200, 1.2), D.apply_lut(np.ascontiguousarray(plane), 10, 200, 1.2))


def test_apply_lut_float_input_takes_the_same_path():
    plane = np.linspace(0, 255, 64, dtype=np.float32).reshape(8, 8)
    expected = np.clip((plane.astype(np.float64) - 20) / 180.0, 0, 1) ** 1.5
    assert np.allclose(D.apply_lut(plane, 20, 200, 1.5), expected, atol=1e-6)


# --------------------------------------------------------------------------- #
# blends
# --------------------------------------------------------------------------- #


def test_screen_blend_never_exceeds_255_with_three_saturated_layers():
    profile = flat_profile({"F1": (0.0, 1.0, 1.0), "F2": (0.0, 1.0, 0.0), "F3": (1.0, 0.0, 1.0)}, "screen")
    planes = {ch: const_plane(255) for ch in ("F1", "F2", "F3")}
    out = D.render_frame(planes, profile, full_bounds("F1", "F2", "F3"))
    assert out.dtype == np.uint8 and out.shape == (8, 8, 3)
    assert int(out.max()) <= 255


def test_screen_keeps_an_overlap_coloured_while_additive_clips_to_white():
    # Complementary CGM colours screen to white too, so use two colours that genuinely overlap
    # in all three components: only then do screen and additive differ.
    colours = {"F1": (1.0, 0.6, 0.6), "F2": (0.6, 0.6, 1.0)}
    value = 230  # 0.902 of full scale
    planes = {"F1": const_plane(value), "F2": const_plane(value)}
    bounds = full_bounds("F1", "F2")

    screen = D.render_frame(planes, flat_profile(colours, "screen"), bounds)
    additive = D.render_frame(planes, flat_profile(colours, "additive"), bounds)

    assert (additive == 255).all(), "additive must clip the overlap to white"
    r, g, b = (int(x) for x in screen[0, 0])
    assert r == b, "the two colours are symmetric in red and blue"
    assert g < r, "screen must keep the overlap coloured, not white"
    assert r < 255 and g < 255
    # 1 - (1 - v)(1 - v*0.6) in green vs 1 - (1 - v)(1 - v*0.6) mirrored in red/blue
    v = value / 255.0
    assert g == pytest.approx(round((1 - (1 - v * 0.6) ** 2) * 255), abs=1)
    assert r == pytest.approx(round((1 - (1 - v) * (1 - v * 0.6)) * 255), abs=1)


def test_screen_of_two_equal_layers_matches_the_formula_where_additive_saturates():
    colours = {"F1": (1.0, 1.0, 1.0), "F2": (1.0, 1.0, 1.0)}
    planes = {"F1": const_plane(204), "F2": const_plane(204)}  # 0.8 of full scale
    bounds = full_bounds("F1", "F2")
    screen = D.render_frame(planes, flat_profile(colours, "screen"), bounds)
    additive = D.render_frame(planes, flat_profile(colours, "additive"), bounds)
    v = 204 / 255.0
    assert int(screen[0, 0, 0]) == pytest.approx(round((1 - (1 - v) ** 2) * 255), abs=1)  # 245
    assert (additive == 255).all()


def test_max_blend_equals_elementwise_max_of_the_layers():
    colours = {"F1": (0.0, 1.0, 1.0), "F3": (1.0, 0.0, 1.0)}
    planes = {"F1": const_plane(120), "F3": const_plane(200)}
    bounds = full_bounds("F1", "F3")
    out = D.render_frame(planes, flat_profile(colours, "max"), bounds)
    f1 = D.render_single_channel(planes["F1"], "F1", flat_profile(colours, "max"), bounds)
    f3 = D.render_single_channel(planes["F3"], "F3", flat_profile(colours, "max"), bounds)
    assert np.array_equal(out, np.maximum(f1, f3))


def test_unknown_blend_mode_is_rejected():
    profile = flat_profile({"F2": (0.0, 1.0, 0.0)}, "lighten")
    with pytest.raises(ValueError, match="blend"):
        D.render_frame({"F2": const_plane(100)}, profile, full_bounds("F2"))


# --------------------------------------------------------------------------- #
# automatic bounds
# --------------------------------------------------------------------------- #


def test_adaptive_matches_classic_within_two_gray_levels_on_a_sparse_series():
    h = hist_from_frames(sparse_series())
    a_low, a_high = D.auto_bounds(h, "adaptive")
    c_low, c_high = D.auto_bounds(h, "classic")
    detail = D.auto_bounds_detail(h, "adaptive")
    assert detail["method_used"] == "adaptive"
    # sparse signal never forms a background population, so the black point stays on the dark peak
    assert detail["background_peaks"] == 1
    assert detail["peak_used"] == detail["mode"] == 0
    assert abs(a_low - c_low) <= 2.0, f"adaptive low {a_low} vs classic {c_low}"
    assert abs(a_high - c_high) <= 2.0, f"adaptive high {a_high} vs classic {c_high}"


def test_adaptive_cuts_the_background_on_a_dense_series():
    h = hist_from_frames(dense_series())
    detail = D.auto_bounds_detail(h, "adaptive")
    assert detail["method_used"] == "adaptive", detail
    assert detail["low"] > 120.0, f"adaptive must lift the black point above the bright background: {detail}"
    assert detail["high"] > detail["low"]
    assert 115 <= detail["mode"] <= 125
    assert detail["peak_used"] == detail["mode"], "one background population: peak is the mode"
    assert detail["background_peaks"] == 1
    assert 3.0 <= detail["sigma"] <= 7.0  # sigma of a 120 +/- 5 background
    # Classic, by contrast, leaves the whole bright background visible.
    assert D.auto_bounds(h, "classic")[0] == 0.0


def bimodal_series(bright: float, spot: float, spread: float = 8.0, n_frames: int = 4,
                   size: int = 300, seed: int = SEED) -> list[np.ndarray]:
    """80 % dark background at 10+/-3, 19 % brighter disc at ``bright``, ~1 % spot at ``spot``.

    The shape of a spheroid sitting in an autofluorescent well: two background populations, and
    the signal too small to form one of its own.
    """
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    disc = ((yy - size / 2) ** 2 + (xx - size / 2) ** 2) < (0.19 * size * size / np.pi)
    spot_mask = ((yy - size * 0.6) ** 2 + (xx - size * 0.55) ** 2) < (0.01 * size * size / np.pi)
    frames = []
    for _ in range(n_frames):
        f = rng.normal(10.0, 3.0, (size, size))
        f[disc] = rng.normal(bright, spread, int(disc.sum()))
        f[spot_mask] = spot
        frames.append(f.clip(0, 255).astype(np.uint8))
    return frames


def test_adaptive_clips_above_the_highest_background_peak():
    # 80 % at 10+/-3, 19 % at 113+/-8, 1 % at 240: the classic autofluorescent-well shape.
    h = hist_from_frames(bimodal_series(bright=113.0, spot=240.0))
    detail = D.auto_bounds_detail(h, "adaptive")
    assert detail["method_used"] == "adaptive", detail
    assert detail["background_peaks"] == 2, detail["peaks"]
    masses = dict(detail["peaks"])
    assert detail["mode"] < 20, "the tallest peak is still the dark one"
    assert abs(detail["peak_used"] - 113) <= 4, detail["peaks"]
    assert detail["peak_used"] in masses and masses[detail["peak_used"]] >= 0.05
    assert detail["low"] > 120.0, f"black point must clear the bright disc: {detail}"
    assert detail["high"] >= 235.0, detail
    assert "background populations" in detail["reason"]
    # the 1 % spot still renders bright, the disc does not
    profile = flat_profile({"F2": (0.0, 1.0, 0.0)})
    frame = bimodal_series(bright=113.0, spot=240.0, n_frames=1)[0]
    out = D.render_frame({"F2": frame}, profile, {"F2": (detail["low"], detail["high"])})
    green = out[..., 1]
    assert int(green[frame == 240].mean()) > 200, "the spot must stay bright"
    assert int(green[(frame > 95) & (frame < 130)].mean()) < 30, "the disc must be suppressed"


def test_adaptive_clamps_the_window_instead_of_falling_back_when_backgrounds_are_bimodal():
    # Same shape, but the signal only just clears the bright disc: 19 % at 60+/-4, 1 % at 70.
    # Falling back to Classic here would put the black point at 0 and restore the very disc
    # Adaptive just identified, so the window is clamped instead.
    h = hist_from_frames(bimodal_series(bright=60.0, spot=70.0, spread=4.0))
    detail = D.auto_bounds_detail(h, "adaptive")
    assert detail["background_peaks"] == 2, detail["peaks"]
    assert detail["method_used"] == "adaptive", detail
    assert detail["fallback"] is False
    assert detail["high"] - detail["low"] == pytest.approx(D.MIN_ADAPTIVE_WINDOW)
    assert detail["low"] > 60.0, f"the black point must stay above the disc: {detail}"
    assert "clamped" in detail["reason"]
    assert D.auto_bounds(h, "classic")[0] == 0.0, "Classic is what we are declining to fall back to"


def test_single_background_peak_still_falls_back_to_classic():
    # One population only: a narrow window really does mean "nothing above the noise" there.
    rng = np.random.default_rng(SEED)
    h = hist_from_frames([rng.normal(100.0, 2.0, (150, 150)).clip(0, 255).astype(np.uint8) for _ in range(4)])
    detail = D.auto_bounds_detail(h, "adaptive")
    assert detail["background_peaks"] == 1
    assert detail["method_used"] == "classic" and detail["fallback"] is True


def test_classic_uses_the_running_per_frame_p995_max():
    # Ten frames; one has a bright patch over 4 % of itself, which is only 0.4 % of the pooled
    # pixels. The pooled p99.5 therefore misses it entirely and the running per-frame max does not.
    frames = [np.full((100, 100), 40, dtype=np.uint8) for _ in range(10)]
    frames[2][0:20, 0:20] = 210
    h = hist_from_frames(frames)
    assert h.frame_p995_max == pytest.approx(210.0)
    assert h.percentile(99.5) == pytest.approx(40.0)
    low, high = D.auto_bounds(h, "classic")
    assert low == 0.0
    assert high == pytest.approx(210.0), "Classic must use the running per-frame p99.5 max"


def test_classic_falls_back_to_the_pooled_p995_without_per_frame_data():
    h = ChannelHistogram()
    h.counts += np.bincount(np.full(10000, 30, dtype=np.uint8), minlength=256)[:256]
    h.counts[200] = 100
    assert h.frame_p995_max == 0.0  # built without add_frame
    detail = D.auto_bounds_detail(h, "classic")
    assert detail["high"] == pytest.approx(h.percentile(99.5))
    assert "pooled p99.5" in detail["reason"]


def test_cut_is_p90_to_p999():
    h = hist_from_frames(dense_series())
    low, high = D.auto_bounds(h, "cut")
    assert low == pytest.approx(h.percentile(90.0))
    assert high == pytest.approx(h.percentile(99.9))
    assert high > low


def test_adaptive_falls_back_to_classic_when_the_window_is_too_narrow():
    rng = np.random.default_rng(SEED)
    frames = [rng.normal(100.0, 2.0, (150, 150)).clip(0, 255).astype(np.uint8) for _ in range(4)]
    h = hist_from_frames(frames)
    detail = D.auto_bounds_detail(h, "adaptive")
    assert detail["fallback"] is True
    assert detail["method_used"] == "classic"
    assert detail["method"] == "adaptive"
    assert "classic" in detail["reason"]
    assert (detail["low"], detail["high"]) == D.auto_bounds(h, "classic")
    assert D.auto_bounds(h, "adaptive") == (detail["low"], detail["high"])


def test_every_method_returns_high_above_low_inside_0_255():
    h = hist_from_frames(dense_series())
    for method in AUTO_METHODS:
        low, high = D.auto_bounds(h, method)
        assert 0.0 <= low < high <= 255.0, method


def test_saturated_histogram_still_yields_a_usable_window():
    h = ChannelHistogram()
    h.counts[255] = 1_000_000  # everything at full scale
    h.frame_p995_max = 255.0
    for method in AUTO_METHODS:
        low, high = D.auto_bounds(h, method)
        assert 0.0 <= low < high <= 255.0, method


def test_empty_histogram_does_not_raise():
    h = ChannelHistogram()
    for method in AUTO_METHODS:
        low, high = D.auto_bounds(h, method)
        assert (low, high) == (0.0, 1.0)
    assert D.auto_bounds_detail(h, "adaptive")["reason"] == "empty histogram"


def test_adaptive_uses_the_right_flank_when_the_mode_sits_at_zero():
    h = ChannelHistogram()
    h.counts[:] = 0
    for v, c in ((0, 1000), (1, 800), (2, 400), (3, 120), (4, 20)):
        h.counts[v] = c
    h.counts[200] = 5
    detail = D.auto_bounds_detail(h, "adaptive")
    assert detail["mode"] == 0
    assert detail["hwhm_side"] == "right"
    assert detail["low"] > 0.0


def test_auto_bounds_rejects_an_unknown_method():
    with pytest.raises(ValueError, match="auto method"):
        D.auto_bounds(hist_from_frames(sparse_series()), "otsu")


def test_auto_bounds_all_uses_white_bounds_for_white():
    hs = HistogramSet()
    hs.channels["F2"] = hist_from_frames(dense_series())
    hs.channels["WHITE"] = hist_from_frames([np.random.default_rng(1).normal(60, 10, (100, 100)).clip(0, 255).astype(np.uint8)])
    out = D.auto_bounds_all(hs, "adaptive")
    assert out["WHITE"] == D.white_bounds(hs.channels["WHITE"])
    assert out["F2"] == D.auto_bounds(hs.channels["F2"], "adaptive")


# --------------------------------------------------------------------------- #
# WHITE
# --------------------------------------------------------------------------- #


def test_white_bounds_are_p05_to_p995():
    h = hist_from_frames([np.random.default_rng(SEED).normal(60, 12, (200, 200)).clip(0, 255).astype(np.uint8)])
    assert D.white_bounds(h) == (h.percentile(0.5), h.percentile(99.5))


def test_white_bounds_degenerate_histogram_falls_back_to_full_range():
    h = ChannelHistogram()
    h.counts[77] = 5000  # every pixel identical: p0.5 == p99.5
    assert D.white_bounds(h) == (0.0, 255.0)


def test_white_preset_picked_from_the_median():
    assert D.white_preset_for_median(171) == "brightfield"  # 20250317 fixed images
    assert D.white_preset_for_median(60) == "phase"  # CD14 phase contrast
    assert D.white_preset_for_median(128) == "phase"  # threshold is strictly greater


def test_apply_white_preset_sets_the_documented_numbers():
    p = DisplayProfile.default_for(["F2"])
    D.apply_white_preset(p, "brightfield")
    assert (p.white.preset, p.white.gamma, p.white.weight) == ("brightfield", 2.0, 0.5)
    D.apply_white_preset(p, "phase")
    assert (p.white.preset, p.white.gamma, p.white.weight) == ("phase", 1.0, 0.6)
    for name, values in WHITE_PRESETS.items():
        D.apply_white_preset(p, name)
        assert p.white.gamma == values["gamma"] and p.white.weight == values["weight"]
    with pytest.raises(ValueError, match="WHITE preset"):
        D.apply_white_preset(p, "dic")


def test_estimate_white_median_honours_the_overlay_mask():
    plane = np.full((100, 100), 60, dtype=np.uint8)  # phase contrast
    plane[40:, :] = 255  # an (absurdly large) bright overlay that would flip the preset
    mask = np.zeros((100, 100), dtype=bool)
    mask[40:, :] = True
    assert D.estimate_white_median(plane) == pytest.approx(255.0)
    assert D.white_preset_for_median(D.estimate_white_median(plane)) == "brightfield"
    assert D.estimate_white_median(plane, mask) == pytest.approx(60.0)
    assert D.white_preset_for_median(D.estimate_white_median(plane, mask)) == "phase"
    assert D.estimate_white_median(np.zeros((0, 0), dtype=np.uint8)) == 0.0


def test_white_underlay_is_dimmed_and_sits_under_the_fluorescence():
    profile = DisplayProfile.default_for(["F2"])
    D.apply_white_preset(profile, "phase")  # gamma 1.0, weight 0.6
    planes = {"WHITE": const_plane(255), "F2": const_plane(0)}
    bounds = {"F2": (0.0, 255.0), "WHITE": (0.0, 255.0)}
    out = D.render_frame(planes, profile, bounds, include_white=True)
    assert int(out[0, 0, 0]) == pytest.approx(round(0.6 * 255), abs=1), "weight must dim the underlay"
    assert out[0, 0, 0] == out[0, 0, 1] == out[0, 0, 2], "the underlay is gray"
    # with fluorescence on top the composite is the screen of the two
    planes["F2"] = const_plane(255)
    lit = D.render_frame(planes, profile, bounds, include_white=True)
    assert int(lit[0, 0, 1]) == 255  # green channel saturated by F2
    assert int(lit[0, 0, 0]) == pytest.approx(round(0.6 * 255), abs=1)  # red only from the underlay


def test_white_weight_zero_makes_the_composite_identical_to_fluorescence_only():
    profile = DisplayProfile.default_for(["F2"])
    profile.white.weight = 0.0
    planes = {"WHITE": const_plane(200), "F2": const_plane(150)}
    bounds = {"F2": (0.0, 255.0), "WHITE": (0.0, 255.0)}
    assert np.array_equal(
        D.render_frame(planes, profile, bounds, include_white=True),
        D.render_frame(planes, profile, bounds, include_white=False),
    )


def test_brightfield_gamma_darkens_the_underlay_relative_to_phase():
    planes = {"WHITE": const_plane(128)}
    bounds = {"WHITE": (0.0, 255.0)}
    values = {}
    for preset in ("phase", "brightfield"):
        p = DisplayProfile.default_for(["F2"])
        D.apply_white_preset(p, preset)
        planes["F2"] = const_plane(0)
        bounds["F2"] = (0.0, 255.0)
        values[preset] = int(D.render_frame(planes, p, bounds)[0, 0, 0])
    assert values["brightfield"] < values["phase"], values


# --------------------------------------------------------------------------- #
# rolling ball
# --------------------------------------------------------------------------- #


def gradient_with_blob(size: int = 400, blob_px: int = 9, peak: int = 220) -> tuple[np.ndarray, int, int, float]:
    """Smooth left-to-right + top-to-bottom ramp 20..90 with one small bright blob on it."""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    ramp = 20.0 + 40.0 * xx / (size - 1) + 30.0 * yy / (size - 1)
    cy = cx = size // 2
    plane = ramp.copy()
    half = blob_px // 2
    plane[cy - half : cy + half + 1, cx - half : cx + half + 1] = peak
    return plane.clip(0, 255).astype(np.uint8), cy, cx, float(ramp[cy, cx])


def test_rolling_ball_removes_a_smooth_gradient():
    plane, cy, cx, _ = gradient_with_blob()
    out = D.subtract_background(plane, 50)
    assert out.dtype == np.uint8 and out.shape == plane.shape
    mask = np.ones(plane.shape, dtype=bool)
    mask[cy - 20 : cy + 20, cx - 20 : cx + 20] = False  # ignore the blob and its surround
    background_residual = out[mask]
    assert background_residual.mean() < 5.0, background_residual.mean()
    assert np.percentile(background_residual, 99.0) < 5.0


def test_rolling_ball_keeps_a_small_blob_peak_within_ten_percent():
    plane, cy, cx, local_background = gradient_with_blob()
    peak_before = float(plane[cy, cx]) - local_background
    out = D.subtract_background(plane, 50)
    peak_after = float(out[cy, cx])
    assert peak_after >= 0.9 * peak_before, (peak_after, peak_before)
    assert peak_after <= 1.1 * peak_before, (peak_after, peak_before)


def test_rolling_ball_shrink_ladder():
    assert [D._shrink_factor(r) for r in (1, 10, 11, 30, 31, 100, 101, 500)] == [1, 1, 2, 2, 4, 4, 8, 8]


def test_rolling_ball_radius_zero_is_a_no_op_and_odd_sizes_survive():
    plane = np.random.default_rng(SEED).integers(0, 256, (101, 97), dtype=np.uint8)
    assert np.array_equal(D.subtract_background(plane, 0), plane)
    out = D.subtract_background(plane, 150)
    assert out.shape == plane.shape and out.dtype == np.uint8
    assert (out <= plane).all(), "background subtraction can only remove signal"


def test_rolling_ball_accepts_a_non_contiguous_plane():
    # read_plane hands out rgb[:, :, k], a strided view rather than a copy.
    rgb = np.random.default_rng(SEED).integers(0, 256, (200, 200, 3), dtype=np.uint8)
    view = rgb[:, :, 0]
    assert not view.flags["C_CONTIGUOUS"]
    assert np.array_equal(D.subtract_background(view, 50), D.subtract_background(np.ascontiguousarray(view), 50))


def test_rolling_ball_rejects_a_non_2d_plane():
    with pytest.raises(ValueError, match="2-D"):
        D.subtract_background(np.zeros((4, 4, 3), dtype=np.uint8), 10)


def test_rolling_ball_is_wired_into_render_frame():
    plane, _, _, _ = gradient_with_blob(size=200)
    profile = flat_profile({"F2": (0.0, 1.0, 0.0)})
    without = D.render_frame({"F2": plane}, profile, full_bounds("F2"))
    profile.rolling_ball.enabled = True
    profile.rolling_ball.radius_px = 50
    with_ball = D.render_frame({"F2": plane}, profile, full_bounds("F2"))
    assert with_ball.mean() < without.mean(), "the gradient must be gone from the rendered frame"
    assert np.array_equal(
        with_ball,
        D.render_frame({"F2": D.subtract_background(plane, 50)}, flat_profile({"F2": (0.0, 1.0, 0.0)}), full_bounds("F2")),
    )


# --------------------------------------------------------------------------- #
# render_frame / effective_bounds / variants / summary
# --------------------------------------------------------------------------- #


def test_white_only_dataset_renders_gray():
    profile = DisplayProfile.default_for([])  # no fluorescence channels at all
    plane = np.arange(256, dtype=np.uint8).reshape(16, 16)
    out = D.render_frame({"WHITE": plane}, profile, {"WHITE": (0.0, 255.0)})
    assert out.shape == (16, 16, 3)
    assert (out[..., 0] == out[..., 1]).all() and (out[..., 1] == out[..., 2]).all()
    # a full-range stretch of WHITE is the identity to within the final rounding
    assert int(np.abs(out[..., 0].astype(int) - plane.astype(int)).max()) <= 1


def test_fluorescence_only_ignores_white():
    profile = DisplayProfile.default_for(["F2"])
    bounds = {"F2": (0.0, 255.0), "WHITE": (0.0, 255.0)}
    with_white = {"WHITE": const_plane(255), "F2": const_plane(100)}
    without = {"F2": const_plane(100)}
    assert np.array_equal(
        D.render_frame(with_white, profile, bounds, include_white=False),
        D.render_frame(without, profile, bounds, include_white=False),
    )
    assert not np.array_equal(
        D.render_frame(with_white, profile, bounds, include_white=True),
        D.render_frame(with_white, profile, bounds, include_white=False),
    )


def test_fluorescence_only_with_nothing_to_show_is_black_not_white():
    profile = DisplayProfile.default_for([])
    out = D.render_frame({"WHITE": const_plane(255)}, profile, {"WHITE": (0.0, 255.0)}, include_white=False)
    assert out.shape == (8, 8, 3) and int(out.max()) == 0


def test_render_frame_without_any_plane_raises():
    with pytest.raises(ValueError, match="no planes"):
        D.render_frame({}, DisplayProfile.default_for(["F2"]), {})


def test_disabled_channel_is_skipped():
    profile = DisplayProfile.default_for(["F2", "F3"])
    profile.white.enabled = False
    planes = {"F2": const_plane(200), "F3": const_plane(200)}
    bounds = full_bounds("F2", "F3")
    both = D.render_frame(planes, profile, bounds)
    profile.channels["F3"].enabled = False
    only_f2 = D.render_frame(planes, profile, bounds)
    assert not np.array_equal(both, only_f2)
    assert np.array_equal(only_f2, D.render_single_channel(planes["F2"], "F2", profile, bounds))


def test_channel_missing_from_the_profile_is_skipped():
    profile = DisplayProfile.default_for(["F2"])  # F3 not in the profile at all
    profile.white.enabled = False
    planes = {"F2": const_plane(200), "F3": const_plane(200)}
    bounds = full_bounds("F2", "F3")
    assert np.array_equal(D.render_frame(planes, profile, bounds),
                          D.render_frame({"F2": planes["F2"]}, profile, bounds))


def test_effective_bounds_manual_mode_uses_the_profile_numbers():
    profile = DisplayProfile.default_for(["F2", "F3"])
    profile.mode = "manual"
    profile.channels["F2"].low, profile.channels["F2"].high = 12.0, 88.0
    profile.channels["F3"].low, profile.channels["F3"].high = 3.0, 40.0
    profile.white.low, profile.white.high = 7.0, 180.0
    auto = {"F2": (0.0, 255.0), "F3": (0.0, 255.0), "WHITE": (0.0, 255.0)}
    eff = D.effective_bounds(profile, auto)
    assert eff == {"F2": (12.0, 88.0), "F3": (3.0, 40.0), "WHITE": (7.0, 180.0)}


def test_effective_bounds_manual_values_are_what_render_frame_uses():
    profile = DisplayProfile.default_for(["F2"])
    profile.mode = "manual"
    profile.white.enabled = False
    profile.channels["F2"].low, profile.channels["F2"].high = 100.0, 200.0
    eff = D.effective_bounds(profile, {"F2": (0.0, 255.0)})
    out = D.render_frame({"F2": const_plane(150)}, profile, eff)
    assert int(out[0, 0, 1]) == pytest.approx(round(0.5 * 255), abs=1)


def test_effective_bounds_auto_mode_and_missing_channels():
    profile = DisplayProfile.default_for(["F2", "F3"])
    profile.white.low, profile.white.high = 5.0, 190.0
    eff = D.effective_bounds(profile, {"F2": (6.0, 110.0)})  # F3 and WHITE not computed yet
    assert eff["F2"] == (6.0, 110.0)
    assert eff["F3"] == (0.0, 255.0)
    assert eff["WHITE"] == (5.0, 190.0)
    eff2 = D.effective_bounds(profile, None)
    assert eff2["F2"] == (0.0, 255.0) and eff2["WHITE"] == (5.0, 190.0)
    eff3 = D.effective_bounds(profile, {"F2": (6.0, 110.0), "F3": (4.0, 73.0), "WHITE": (2.0, 240.0)})
    assert eff3["WHITE"] == (2.0, 240.0)


def test_render_preview_variants_keys_and_shapes():
    profile = DisplayProfile.default_for(["F2", "F3"])
    planes = {"WHITE": const_plane(90), "F2": const_plane(120), "F3": const_plane(60)}
    bounds = full_bounds("WHITE", "F2", "F3")
    out = D.render_preview_variants(planes, profile, bounds)
    assert set(out) == {"composite", "fluorescence_only", "WHITE", "F2", "F3"}
    for name, frame in out.items():
        assert frame.shape == (8, 8, 3) and frame.dtype == np.uint8, name
    assert np.array_equal(out["composite"], D.render_frame(planes, profile, bounds, True))
    assert np.array_equal(out["fluorescence_only"], D.render_frame(planes, profile, bounds, False))


def test_render_preview_variants_on_a_white_only_dataset():
    profile = DisplayProfile.default_for([])
    out = D.render_preview_variants({"WHITE": const_plane(90)}, profile, {"WHITE": (0.0, 255.0)})
    assert set(out) == {"composite", "WHITE"}  # no fluorescence-only video for a WHITE-only set
    assert D.render_preview_variants({}, profile, {}) == {}


def test_profile_summary_reads_like_the_log_line():
    profile = DisplayProfile.default_for(["F2", "F3"])
    profile.channels["F3"].gamma = 1.2
    D.apply_white_preset(profile, "phase")
    text = D.profile_summary(profile, {"F2": (6.0, 110.0), "F3": (4.0, 73.0), "WHITE": (2.0, 240.0)})
    # the shape the plan asks for: "F2 6-110 g1.0 - F3 4-73 g1.2 - WHITE phase w0.6 - screen"
    assert "F2 6–110 γ1.0" in text, text
    assert "F3 4–73 γ1.2" in text, text
    assert "WHITE phase w0.6" in text
    assert text.endswith("screen")
    assert " · " in text  # middot separators
    profile.channels["F2"].enabled = False
    profile.rolling_ball.enabled = True
    profile.rolling_ball.radius_px = 50
    text2 = D.profile_summary(profile, {"F3": (4.0, 73.0)})
    assert "F2 off" in text2 and "rolling ball r50" in text2


# --------------------------------------------------------------------------- #
# performance (generous margins so CI does not flake)
# --------------------------------------------------------------------------- #


def _median_ms(fn, repeats: int = 3) -> float:
    fn()  # warm the LUT cache and let numpy touch the pages
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000.0)
    return float(np.median(times))


def _bench_planes(size: int) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(SEED)
    return {ch: rng.integers(0, 256, (size, size), dtype=np.uint8) for ch in ("WHITE", "F1", "F2", "F3")}


@pytest.mark.parametrize("size,budget_ms", [(640, 100.0), (1900, 1000.0)])
def test_render_frame_performance(size, budget_ms):
    planes = _bench_planes(size)
    profile = DisplayProfile.default_for(["F1", "F2", "F3"])
    bounds = {ch: (5.0, 200.0) for ch in ("F1", "F2", "F3")} | {"WHITE": (10.0, 240.0)}
    elapsed = _median_ms(lambda: D.render_frame(planes, profile, bounds, include_white=True))
    assert elapsed < budget_ms, f"render_frame {size}x{size} took {elapsed:.1f} ms (budget {budget_ms} ms)"


@pytest.mark.parametrize("size,budget_ms", [(640, 100.0), (1900, 1000.0)])
def test_rolling_ball_performance(size, budget_ms):
    plane = _bench_planes(size)["F2"]
    elapsed = _median_ms(lambda: D.subtract_background(plane, 50))
    assert elapsed < budget_ms, f"subtract_background {size}x{size} r=50 took {elapsed:.1f} ms"


# --------------------------------------------------------------------------- #
# 0.6: the 8-bit renderer used by preview and export matches the float reference
# --------------------------------------------------------------------------- #


def test_fast_renderer_matches_the_reference():
    rng = np.random.default_rng(1)
    planes = {ch: rng.integers(0, 256, (64, 80), dtype=np.uint8) for ch in ("WHITE", "F1", "F2", "F3")}
    bounds = {"F1": (10.0, 200.0), "F2": (5.0, 120.0), "F3": (30.0, 250.0), "WHITE": (20.0, 230.0)}
    palettes = ({"F1": (0.0, 1.0, 1.0), "F2": (0.0, 1.0, 0.0), "F3": (1.0, 0.0, 1.0)},
                {"F1": (0.2, 0.5, 1.0), "F2": (0.7, 1.0, 0.1), "F3": (1.0, 0.3, 0.0)})
    for blend in ("screen", "additive", "max"):
        for colours in palettes:
            profile = D.DisplayProfile.default_for(["WHITE", "F1", "F2", "F3"])
            profile.blend = blend
            for ch, colour in colours.items():
                profile.channels[ch].colour = colour
                profile.channels[ch].gamma = 1.3
            for include_white in (True, False):
                ref = D.render_frame(planes, profile, bounds, include_white=include_white).astype(int)
                fast = D.render_frame_fast(planes, profile, bounds, include_white=include_white).astype(int)
                assert fast.shape == ref.shape
                assert np.abs(ref - fast).max() <= 3 and np.abs(ref - fast).mean() < 0.8, (blend, include_white)
            for ch in ("WHITE", "F2"):
                ref = D.render_single_channel(planes[ch], ch, profile, bounds).astype(int)
                fast = D.render_single_channel_fast(planes[ch], ch, profile, bounds).astype(int)
                assert np.abs(ref - fast).max() <= 1
    white_only = {"WHITE": planes["WHITE"]}
    profile = D.DisplayProfile.default_for(["WHITE"])
    assert np.abs(D.render_frame(white_only, profile, bounds).astype(int)
                  - D.render_frame_fast(white_only, profile, bounds).astype(int)).max() <= 1
