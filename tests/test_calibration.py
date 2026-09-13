"""Overlay detection and scale-bar reading (engine/calibration.py, plan section 4.4).

Synthetic tests run everywhere. The real-sample tests read every experiment listed in
``tests/fixtures/scale_bar_ground_truth.json`` and skip when the captures are absent
(set ETALUMA_SAMPLES; see tests/conftest.py).
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pytest
import tifffile

# tests/ is a package (tests/__init__.py), so these are package-qualified imports.
from tests.conftest import require_samples
from tests.fixtures.make_synthetic_dataset import bar_length_px, make_dataset, render_overlay_frame

from etaluma_video.engine import calibration as C
from etaluma_video.engine.models import CALIBRATION_DISAGREEMENT, OBJECTIVES, Box

GROUND_TRUTH = Path(__file__).resolve().parent / "fixtures" / "scale_bar_ground_truth.json"


def _frame(root: Path, relative: str) -> Path:
    """The ground-truth frame; a ``*`` stands for the capture's user prefix."""
    if "*" in relative:
        return next(iter(sorted(root.glob(relative))), root / relative)
    return root / relative

#: (label_um, objective, frame shape). "500 µm, 10x" needs 605 px of bar and
#: "1000 µm, 40x" needs 4878, so those are rendered in memory rather than written out.
LABEL_CASES = [
    (100, "10x", 400),
    (200, "4x", 400),
    (50, "20x", 400),
    (500, "10x", 900),
    (1000, "40x", (400, 5000)),
]


def ground_truth() -> list[dict]:
    return json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))["experiments"]


@pytest.fixture
def scratch_dir():
    """A private temp folder.

    Deliberately not pytest's ``tmp_path``: the shared ``%TEMP%\\pytest-of-<user>`` root
    already exists on this machine with ACLs the test process cannot write through, which
    makes every ``tmp_path`` test error out. ``mkdtemp`` is unaffected.
    """
    path = Path(tempfile.mkdtemp(prefix="etaluma_calib_"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Glyph templates
# --------------------------------------------------------------------------- #


def test_canonical_glyph_templates_are_present():
    glyphs = C.load_glyphs()
    assert glyphs, f"no glyph templates in {C.GLYPH_DIR}; run tools/harvest_glyphs.py"
    for stem in ("d0", "d1", "d2", "d4", "d5", "mu", "m", "comma", "x"):
        assert stem in glyphs, f"missing canonical template {stem}.png"
        assert glyphs[stem].ndim == 2 and glyphs[stem].dtype == np.uint8
        assert set(np.unique(glyphs[stem])) <= {0, 255}, "templates must be binary"
    # Every template spans the full label height, so the baseline is part of the match.
    assert len({t.shape[0] for t in glyphs.values()}) == 1


def test_missing_digits_are_absent_and_documented():
    glyphs = C.load_glyphs()
    for digit in ("3", "6", "7", "8", "9"):
        assert f"d{digit}" not in glyphs, f"d{digit} was not in any sample label"
    readme = (C.GLYPH_DIR / "README.md").read_text(encoding="utf-8")
    assert "Missing digits" in readme


# --------------------------------------------------------------------------- #
# Synthetic detection and reading
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("label_um", "objective", "shape"), LABEL_CASES)
def test_synthetic_label_is_detected_and_read(label_um, objective, shape):
    rgb, drawn = render_overlay_frame(shape, label_um=label_um, objective=objective)
    overlays, cal = C.calibrate_frame(rgb, "synthetic")

    assert overlays.detected
    assert overlays.bar_len_px == bar_length_px(label_um, objective)
    assert overlays.bar_box.to_list() == list(drawn["bar_box"])
    assert overlays.label_box is not None
    assert 3 <= overlays.bar_box.height <= 12

    assert cal.label_um == pytest.approx(float(label_um))
    assert cal.label_objective == objective
    assert cal.objective == objective
    assert cal.source == "burned-in scale bar"
    assert cal.confidence >= C.GLYPH_ACCEPT_SCORE
    assert cal.pixel_size_um == pytest.approx(label_um / overlays.bar_len_px)
    # The fixture draws the bar at the table pixel size, so the two must agree.
    assert cal.pixel_size_um == pytest.approx(OBJECTIVES[objective], rel=0.01)
    assert cal.disagreement is False
    assert "DISAGREES" not in cal.message


def test_synthetic_timestamp_box_is_found_exactly():
    rgb, drawn = render_overlay_frame(400)
    overlays = C.detect_overlays(rgb)
    assert overlays.timestamp_box is not None
    assert overlays.timestamp_box.to_list() == list(drawn["timestamp_box"])


def test_frame_without_overlays_yields_no_calibration():
    rgb, drawn = render_overlay_frame(400, overlays=False)
    assert drawn is None
    overlays, cal = C.calibrate_frame(rgb, "synthetic")
    assert overlays.detected is False
    assert overlays.bar_box is None and overlays.label_box is None and overlays.timestamp_box is None
    assert cal.source == "none"
    assert cal.pixel_size_um is None
    assert cal.usable is False
    assert overlays.exclude_bottom_px() == 0
    assert not overlays.mask((rgb.shape[0], rgb.shape[1])).any()


def test_bar_that_disagrees_with_the_table_raises_the_warning():
    """A 500 µm label drawn over a 541 px bar is 0.924 µm/px against 0.826 in the table."""
    rgb, _ = render_overlay_frame(900, label_um=500, objective="10x", bar_px=541)
    overlays, cal = C.calibrate_frame(rgb)
    assert overlays.bar_len_px == 541
    assert cal.pixel_size_um == pytest.approx(500 / 541)  # the bar wins
    assert cal.table_pixel_size_um == OBJECTIVES["10x"]
    assert abs(cal.pixel_size_um / cal.table_pixel_size_um - 1.0) > CALIBRATION_DISAGREEMENT
    assert cal.disagreement is True
    assert "DISAGREES" in cal.message


def test_overlay_mask_covers_the_bar_the_label_and_the_box():
    rgb, drawn = render_overlay_frame(400)
    overlays = C.detect_overlays(rgb)
    mask = overlays.mask()
    for name in ("bar_box", "label_box", "timestamp_box"):
        y0, y1, x0, x1 = drawn[name]
        assert mask[y0:y1, x0:x1].all(), f"{name} is not covered by Overlays.mask()"
    # Every yellow overlay pixel is masked, and the mask stays in the bottom of the frame.
    assert not C.yellow_mask(rgb)[~mask].any()
    assert not mask[: overlays.label_box.y0 - overlays.pad_px].any()
    assert 0 < overlays.exclude_bottom_px() <= rgb.shape[0]


def test_masked_histogram_drops_the_yellow_bar():
    rgb, _ = render_overlay_frame(400)
    overlays = C.detect_overlays(rgb)
    plane = rgb[..., 0]
    assert int((plane[~overlays.mask()] == 255).sum()) == 0
    assert int((plane == 255).sum()) > 0


def test_corner_crop_returns_a_2x_rgb_crop():
    rgb, drawn = render_overlay_frame(400)
    overlays = C.detect_overlays(rgb)
    crop = C.corner_crop(rgb, overlays, scale=2)
    assert crop.ndim == 3 and crop.shape[2] == 3 and crop.dtype == np.uint8
    y0, y1, x0, x1 = drawn["bar_box"]
    assert crop.shape[0] >= 2 * (y1 - y0) and crop.shape[1] >= 2 * (x1 - x0)
    assert C.yellow_mask(crop).any(), "the corner crop must contain the bar"
    # Nearest-neighbour: every source pixel becomes a 2x2 block.
    assert np.array_equal(crop[0::2, 0::2], crop[1::2, 1::2])
    assert C.corner_crop(rgb, None, scale=2).shape[0] == 2 * min(160, rgb.shape[0])


def test_read_scale_label_reports_failure_without_a_box():
    rgb, _ = render_overlay_frame(400)
    assert C.read_scale_label(C.yellow_mask(rgb), None) == (None, None, 0.0)
    empty = np.zeros((60, 200), dtype=bool)
    assert C.read_scale_label(empty, Box(10, 25, 10, 120)) == (None, None, 0.0)


def test_detect_overlays_rejects_a_non_rgb_array():
    with pytest.raises(ValueError):
        C.detect_overlays(np.zeros((40, 40), dtype=np.uint8))


def test_overlays_on_a_written_dataset_frame(scratch_dir):
    """The same reading works on a TIFF that went through LZW compression."""
    root = make_dataset(scratch_dir / "20260101_120000_synthetic", positions=1,
                        channels=("WHITE", "F2"), timepoints=2, size=400)
    frame = next(iter(sorted(root.glob("ROI-1a/*_WHITE_000000.tif"))))
    overlays, cal = C.calibrate_frame(tifffile.imread(frame), str(frame))
    assert cal.label_um == 100 and cal.label_objective == "10x"
    assert cal.pixel_size_um == pytest.approx(100 / 121)
    assert overlays.timestamp_box is not None


# --------------------------------------------------------------------------- #
# Real captures
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("truth", ground_truth(), ids=lambda t: t["experiment"])
def test_real_experiment_matches_ground_truth(samples, truth):
    root = require_samples(samples, truth["experiment"])
    frame = _frame(root, truth["frame"])
    if not frame.is_file():
        pytest.skip(f"frame not available: {truth['frame']}")
    rgb = np.ascontiguousarray(tifffile.imread(frame)[..., :3])
    overlays, cal = C.calibrate_frame(rgb, str(frame))

    assert overlays.detected
    assert overlays.bar_len_px == truth["bar_px"]
    assert cal.bar_px == truth["bar_px"]
    assert cal.label_um == pytest.approx(truth["label_um"])
    assert cal.label_objective == truth["objective"]
    assert cal.objective == truth["objective"]
    assert cal.source == "burned-in scale bar"
    assert cal.confidence >= C.GLYPH_ACCEPT_SCORE
    assert cal.pixel_size_um == pytest.approx(truth["pixel_size_um"], rel=0.01)
    assert cal.table_pixel_size_um == truth["table_pixel_size_um"]
    assert cal.disagreement is truth["disagreement_expected"]


@pytest.mark.parametrize("truth", ground_truth(), ids=lambda t: t["experiment"])
def test_real_overlay_geometry(samples, truth):
    """Every 1900 px capture carries the same overlay geometry (plan section 2.4)."""
    root = require_samples(samples, truth["experiment"])
    frame = _frame(root, truth["frame"])
    if not frame.is_file():
        pytest.skip(f"frame not available: {truth['frame']}")
    rgb = np.ascontiguousarray(tifffile.imread(frame)[..., :3])
    overlays = C.detect_overlays(rgb)

    assert rgb.shape[:2] == (1900, 1900)
    assert overlays.timestamp_box.to_list() == [1848, 1874, 52, 522]
    assert overlays.bar_box.to_list()[:2] == [1841, 1847]
    assert overlays.bar_box.x1 == 1841
    assert overlays.label_box.to_list()[:2] == [1816, 1831]
    mask = overlays.mask()
    for box in overlays.boxes():
        assert mask[box.y0 : box.y1, box.x0 : box.x1].all()
    assert not C.yellow_mask(rgb)[~mask].any()
    assert overlays.exclude_bottom_px() == 1900 - 1816 + overlays.pad_px


def test_no_real_sample_disagrees_with_the_objective_table():
    """Measured on the captures: every bar is within 1 % of the table (plan section 2.4 corrected)."""
    for truth in ground_truth():
        rel = truth["pixel_size_um"] / truth["table_pixel_size_um"] - 1.0
        assert abs(rel) <= CALIBRATION_DISAGREEMENT
        assert truth["disagreement_expected"] is False


def test_ground_truth_file_is_self_consistent():
    rows = ground_truth()
    assert len(rows) == 9
    for t in rows:
        assert t["pixel_size_um"] == pytest.approx(t["label_um"] / t["bar_px"], rel=1e-5)
        assert t["table_pixel_size_um"] == OBJECTIVES[t["objective"]]
        expected = abs(t["pixel_size_um"] / t["table_pixel_size_um"] - 1.0) > CALIBRATION_DISAGREEMENT
        assert t["disagreement_expected"] is expected
        assert "/" in t["frame"] or t["frame"].endswith(".tif")
