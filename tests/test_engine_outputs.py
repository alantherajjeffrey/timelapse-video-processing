"""Runs end to end on synthetic captures: fixed-image outputs, timelapse videos, measurements.

Ported from Codex 0.3 ``tests/test_independent.py`` (SyntheticOutputTests, the quantification and
scale-bar parts of ScientificTests) and ``tests/test_iteration.py``. Assertions about *numbers*
that the display agent owns (auto bounds, LUT values) are replaced by structural and monotonic
checks; assertions about *measurements* are unchanged, because measurements are still raw.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
import tifffile
from PIL import Image

from etaluma_video.engine import display, reports
from etaluma_video.engine.models import ChannelHistogram, DisplayProfile, HistogramSet
from etaluma_video.engine.parsing import scan_dataset
from etaluma_video.engine.process import Options, estimate_output_bytes, process_dataset, quick_options, validate_options
from etaluma_video.engine.quantify import quantify


def capture(root: Path, name: str, value: int = 20, shape=(80, 100), array: np.ndarray | None = None) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(path, np.full(shape, value, dtype=np.uint8) if array is None else array)
    return path


def small(**kwargs) -> Options:
    """Options that keep a test run to a second or two."""
    return Options(width=160, tile_width=160, **kwargs)


# --------------------------------------------------------------------------- #
# Measurements (raw, unchanged from Codex)
# --------------------------------------------------------------------------- #


def test_quantification_raw_units_threshold_boundary_crop_and_connectivity():
    a = np.array([[0, 50, 51, 0], [0, 100, 0, 0], [0, 0, 0, 200], [255, 255, 255, 255]], dtype=np.uint8)
    got = quantify(a, 50, 0.826, exclude_bottom_px=1)
    assert got["integrated_density"] == 401
    assert got["mean_intensity"] == pytest.approx(401 / 12)
    assert got["positive_area_px"] == 3
    assert got["positive_area_percent"] == 25
    assert got["positive_area_um2"] == pytest.approx(3 * 0.826 ** 2)
    assert got["positive_object_count"] == 2
    assert got["largest_positive_object_px"] == 2
    assert got["measured_area_px"] == 12
    assert got["threshold_rule"] == "intensity > threshold"
    assert quantify(a, 255, 0.205)["positive_area_px"] == 0
    with pytest.raises(ValueError):
        quantify(a, 50, 0.826, 4)


def test_quantification_excludes_the_overlay_mask():
    a = np.zeros((4, 4), dtype=np.uint8)
    a[0] = 200  # "signal"
    a[3] = 255  # "burned-in overlay"
    mask = np.zeros((4, 4), dtype=bool)
    mask[3] = True
    plain = quantify(a, 50, 1.0)
    masked = quantify(a, 50, 1.0, exclude_mask=mask)
    assert plain["positive_area_px"] == 8
    assert masked["positive_area_px"] == 4
    assert masked["measured_area_px"] == 12
    assert masked["excluded_overlay_px"] == 4
    assert masked["mean_intensity"] == pytest.approx(200 * 4 / 12)
    with pytest.raises(ValueError):
        quantify(a, 50, 1.0, exclude_mask=np.ones((4, 4), dtype=bool))


@pytest.mark.parametrize("pixel_size", [2.068, 0.826, 0.411, 0.205])
@pytest.mark.parametrize("width", [1900, 950, 380])
def test_scale_geometry_accounts_for_resizing(pixel_size, width):
    microns, pixels = reports.scale_geometry(1900, width, pixel_size)
    assert abs(pixels * pixel_size * 1900 / width - microns) <= 0.50001 * pixel_size * 1900 / width
    assert pixels <= width * 0.22 + 1
    rgb = np.zeros((1200, 1900, 3), dtype=np.uint8)
    image = np.asarray(reports.annotate_image(rgb, width, pixel_size, True))
    row = image[image.shape[0] - 22]
    assert int(np.all(row == 255, axis=1).sum()) == pixels


def test_no_calibration_means_no_scale_bar():
    rgb = np.zeros((200, 300, 3), dtype=np.uint8)
    image = np.asarray(reports.annotate_image(rgb, 300, None, True))
    assert image.max() == 0


# --------------------------------------------------------------------------- #
# Fixed-image runs
# --------------------------------------------------------------------------- #


def test_fixed_outputs_preserve_raw_measurements_sources_and_existing_outputs(tmp_path):
    src = tmp_path / "src"
    a = np.zeros((80, 100), dtype=np.uint8)
    a[:40] = 100
    paths = [capture(src, f"Sample_ROI-1a_{ch}_000000.tif", array=a) for ch in ("WHITE", "F1", "F2", "F3")]
    hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    ds = scan_dataset(src)
    requested = tmp_path / "analysis_output"
    requested.mkdir()
    sentinel = requested / "existing_claude_result.txt"
    sentinel.write_text("original remains untouched")

    result = process_dataset(ds, small(objective="20x", threshold=50), requested)
    out = result["output"]
    assert out != requested and out.parent == requested
    assert sentinel.read_text() == "original remains untouched"
    assert hashes == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}

    metadata = json.loads((out / "info" / f"{ds.name}_metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "complete"
    assert metadata["app_version"] == "1.0.0"
    assert metadata["effective_mode"] == "fixed"
    assert metadata["calibration"]["pixel_size_um"] == pytest.approx(0.411)
    assert metadata["processing_seconds"] > 0
    assert "display" in metadata and "overlays" in metadata
    assert (out / "info" / "display_profile.json").is_file()
    assert (out / "info" / "calibration.json").is_file()
    assert "Complete in" in (out / "info" / "processing_log.txt").read_text(encoding="utf-8")

    profile = json.loads((out / "info" / "display_profile.json").read_text(encoding="utf-8"))
    assert profile["schema"] == 1 and "effective_bounds" in profile
    assert DisplayProfile.from_dict(profile).white.preset in ("brightfield", "phase")

    with (out / f"{ds.name}_quantification.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 4
    for row in rows:
        assert float(row["mean_intensity"]) == 50
        assert int(row["integrated_density"]) == 400000
        assert int(row["positive_area_px"]) == 4000
        assert float(row["positive_area_um2"]) == pytest.approx(4000 * 0.411 ** 2)

    assert (out / "src_ROI-1a_composite.png").is_file()
    assert (out / "src_ROI-1a_fluorescence.png").is_file()
    assert (out / "src_ROI-1a_panel.png").is_file()
    assert (out / "montages/src_plate_overview.png").is_file()
    assert len(list((out / "masks").glob("*_positive_mask.png"))) == 4
    composite = np.asarray(Image.open(out / "src_ROI-1a_composite.png"))
    assert composite.shape == (80, 100, 3) and composite.dtype == np.uint8
    for png in out.rglob("*.png"):
        with Image.open(png) as image:
            image.verify()
    # Generated masks and composites must never be discovered as new sources.
    assert len(scan_dataset(src).frames) == 4


def test_fixed_masks_respect_the_threshold(tmp_path):
    src = tmp_path / "src"
    a = np.zeros((40, 40), dtype=np.uint8)
    a[:10] = 200
    capture(src, "Sample_ROI-1a_F2_000000.tif", array=a)
    out = process_dataset(scan_dataset(src), small(objective="10x", threshold=100), tmp_path / "out")["output"]
    mask = np.asarray(Image.open(out / "masks/src_ROI-1a_F2_positive_mask.png"))
    assert mask[:10].all() and not mask[10:].any()


# --------------------------------------------------------------------------- #
# Timelapse runs
# --------------------------------------------------------------------------- #


def test_timelapse_synchronization_videos_and_montage(tmp_path):
    src = tmp_path / "src"
    for roi, intensity in (("1a", 30), ("2b", 150)):
        for ch, serials in (("F2", (0, 1, 2)), ("F3", (1, 2, 3))):
            for serial in serials:
                capture(src, f"Sample_ROI-{roi}_{ch}_{serial:06d}.tif", value=intensity + serial * 10)
    ds = scan_dataset(src)
    opts = small(objective="10x", rois=["ROI-1a"], fps=4, interval_seconds=600)
    result = process_dataset(ds, opts, tmp_path / "out")
    out = result["output"]
    metadata = json.loads((out / "info" / f"{ds.name}_metadata.json").read_text(encoding="utf-8"))
    records = {v["file"]: v for v in metadata["videos"]}
    assert set(records) == {"src_ROI-1a_F2.avi", "src_ROI-1a_F2.mp4", "src_ROI-1a_F3.avi", "src_ROI-1a_F3.mp4",
                            "src_ROI-1a_composite.avi", "src_ROI-1a_composite.mp4"}
    assert records["src_ROI-1a_composite.avi"]["serials"] == [1, 2]
    assert any("incomplete timepoints omitted" in w for w in metadata["warnings"])
    for filename, record in records.items():
        if not filename.endswith(".avi"):
            continue
        video = cv2.VideoCapture(str(out / filename))
        try:
            decoded = 0
            while True:
                ok, frame = video.read()
                if not ok:
                    break
                assert (frame.shape[1], frame.shape[0]) == tuple(record["size"])
                decoded += 1
            assert decoded == record["frames"]
            assert video.get(cv2.CAP_PROP_FPS) == 4
        finally:
            video.release()
    assert (out / "montages/src_ROI-1a_montage.png").is_file()
    assert not list(out.glob("*_quantification.csv"))
    # The AVI and the MP4 of one video come from the same render pass.
    assert records["src_ROI-1a_F2.avi"]["frames"] == records["src_ROI-1a_F2.mp4"]["frames"]


def test_white_only_experiment_produces_only_the_white_video(tmp_path):
    src = tmp_path / "src"
    for serial in range(3):
        capture(src, f"Sample_ROI-1a_WHITE_{serial:06d}.tif", value=60 + serial)
    out = process_dataset(scan_dataset(src), small(objective="10x"), tmp_path / "out")["output"]
    names = sorted(p.name for p in out.iterdir() if p.suffix in (".avi", ".mp4"))
    assert names == ["src_ROI-1a_WHITE.avi", "src_ROI-1a_WHITE.mp4"]


def test_disabled_channel_is_left_out_of_the_composite(tmp_path):
    src = tmp_path / "src"
    for ch in ("WHITE", "F2", "F3"):
        for serial in range(2):
            capture(src, f"Sample_ROI-1a_{ch}_{serial:06d}.tif", value=50)
    ds = scan_dataset(src)
    profile = DisplayProfile.default_for(ds.channels)
    profile.channels["F3"].enabled = False
    out = process_dataset(ds, small(objective="10x", profile=profile), tmp_path / "out")["output"]
    metadata = json.loads((out / "info" / f"{ds.name}_metadata.json").read_text(encoding="utf-8"))
    files = {v["file"] for v in metadata["videos"]}
    assert "src_ROI-1a_F3.avi" in files, "a disabled channel still gets its own video"
    assert "src_ROI-1a_composite.avi" in files
    assert metadata["display"]["profile"]["channels"]["F3"]["enabled"] is False


def test_container_selection(tmp_path):
    src = tmp_path / "src"
    for serial in range(2):
        capture(src, f"Sample_ROI-1a_F2_{serial:06d}.tif", value=90)
    out = process_dataset(scan_dataset(src), small(objective="10x", avi=False, mp4=True), tmp_path / "out")["output"]
    assert sorted(p.suffix for p in out.iterdir() if p.suffix in (".avi", ".mp4")) == [".mp4"]
    out2 = process_dataset(scan_dataset(src), small(objective="10x", avi=True, mp4=False), tmp_path / "out")["output"]
    assert sorted(p.suffix for p in out2.iterdir() if p.suffix in (".avi", ".mp4")) == [".avi"]


def test_quick_options_are_the_documented_preset():
    opts = quick_options()
    assert (opts.width, opts.avi, opts.mp4, opts.quick) == (1900, False, True, True)
    assert opts.profile.mode == "auto" and opts.profile.auto_method == "adaptive"
    assert opts.channels is None and opts.rois is None
    assert opts.app_scale_bar is False and opts.app_timestamp is True and opts.name_label is True


def test_quick_run_writes_a_quick_folder(tmp_path):
    src = tmp_path / "src"
    for serial in range(2):
        capture(src, f"Sample_ROI-1a_F2_{serial:06d}.tif", value=90)
    opts = quick_options()
    opts.width = 160
    opts.tile_width = 160
    out = process_dataset(scan_dataset(src), opts, tmp_path / "out")["output"]
    assert out.name.startswith("quick_")
    assert (out / "info" / "display_profile.json").is_file() and (out / "info" / "calibration.json").is_file()


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


def test_source_directory_guard_and_failed_run_metadata(tmp_path):
    src = tmp_path / "src"
    capture(src, "Sample_ROI-1a_F2_000000.tif")
    ds = scan_dataset(src)
    with pytest.raises(ValueError, match="source folder"):
        process_dataset(ds, small(), src)
    tifffile.imwrite(ds.frames[0].path, np.zeros((80, 100), dtype=np.uint16))
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="8-bit"):
        process_dataset(ds, small(), out)
    run = next(out.glob("run_*"))
    metadata = json.loads((run / "info" / f"{ds.name}_metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "failed"
    assert "8-bit" in metadata["error"]
    assert metadata["incomplete"] is True
    assert (run / "INCOMPLETE.txt").is_file()


def test_changing_video_dimensions_fail_instead_of_silent_resizing(tmp_path):
    src = tmp_path / "src"
    capture(src, "Sample_ROI-1a_F2_000000.tif", shape=(80, 100))
    capture(src, "Sample_ROI-1a_F2_000001.tif", shape=(90, 110))
    ds = scan_dataset(src)
    out = tmp_path / "out"
    with pytest.raises(ValueError, match="dimensions change"):
        process_dataset(ds, small(), out)
    run = next(out.glob("run_*"))
    assert json.loads((run / "info" / f"{ds.name}_metadata.json").read_text(encoding="utf-8"))["status"] == "failed"
    assert all(p.stat().st_size == 0 for p in [*run.glob("*.avi"), *run.glob("*.mp4")])


def test_unknown_interval_timestamp_and_invalid_options(tmp_path):
    src = tmp_path / "src"
    capture(src, "Sample_ROI-1a_F2_000000.tif")
    ds = scan_dataset(src)
    for opts in (Options(threshold=float("nan")), Options(fps=-1), Options(width=161), Options(channels=[]),
                 Options(objective="99x"), Options(conditions={"missing": "drug"})):
        with pytest.raises(ValueError):
            validate_options(ds, opts)
    bad_blend = Options()
    bad_blend.profile.blend = "multiply"
    with pytest.raises(ValueError, match="Blend mode"):
        validate_options(ds, bad_blend)
    result = process_dataset(ds, small(app_timestamp=True), tmp_path / "results")
    assert any("carry no time" in w for w in result["warnings"])  # 0.6: a note, never a failed run


def test_condition_csv_and_unknown_condition():
    assert reports.parse_conditions('ROI,condition\nROI-1a,"Drug, 10 uM"\n') == {"ROI-1a": "Drug, 10 uM"}
    for text in ("a,b\na,c", "a", "a,", "a,b,c"):
        with pytest.raises(ValueError):
            reports.parse_conditions(text)


# --------------------------------------------------------------------------- #
# Size estimate and display contract
# --------------------------------------------------------------------------- #


def test_estimate_grows_with_width_and_frames(tmp_path):
    src = tmp_path / "src"
    for ch in ("WHITE", "F2"):
        for serial in range(5):
            capture(src, f"Sample_ROI-1a_{ch}_{serial:06d}.tif", value=50)
    ds = scan_dataset(src)
    narrow = estimate_output_bytes(ds, Options(width=950))
    wide = estimate_output_bytes(ds, Options(width=1900))
    # WHITE, F2, composite with WHITE, fluorescence-only composite
    assert narrow["videos"] == 4 and narrow["frames"] == 20
    assert wide["total_bytes"] == pytest.approx(narrow["total_bytes"] * 4)
    assert narrow["avi_bytes_low"] < narrow["avi_bytes"] < narrow["avi_bytes_high"]
    assert estimate_output_bytes(ds, Options(width=950, avi=False))["avi_bytes"] == 0
    assert "Estimated output" in narrow["text"]


def test_auto_bounds_are_a_structure_not_a_promise():
    """The display agent owns the numbers; the engine only relies on the shape and ordering."""
    hists = HistogramSet(total_positions=1, positions=["ROI-1a"])
    counts = np.zeros(256, dtype=np.int64)
    counts[10:60] = 100
    hists.channels["F2"] = ChannelHistogram(counts.copy(), frame_p995_max=58.0, frames=1)
    for method in ("adaptive", "classic", "cut"):
        bounds = display.auto_bounds_all(hists, method)
        low, high = bounds["F2"]
        assert 0 <= low < high <= 255
    profile = DisplayProfile.default_for(["F2"])
    profile.mode = "manual"
    profile.channels["F2"].low, profile.channels["F2"].high = 20, 200
    assert display.effective_bounds(profile, display.auto_bounds_all(hists, "adaptive"))["F2"] == (20, 200)
    profile.mode = "auto"
    assert display.effective_bounds(profile, {"F2": (5.0, 50.0)})["F2"] == (5.0, 50.0)
