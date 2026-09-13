"""The synthetic Etaluma fixture (tests/fixtures/make_synthetic_dataset.py).

These tests keep the fixture honest: it must look enough like a real capture that the
Codex filename grammar, ``parse_epf``/``parse_avs`` and the overlay detector all accept
it. When the engine port has landed the dataset is scanned for real; until then the
names are checked against the grammar directly and the scan part skips with a reason.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
import tifffile
from PIL import Image

from tests.fixtures.make_synthetic_dataset import bar_length_px, make_dataset, render_overlay_frame

from etaluma_video.engine import calibration as C
from etaluma_video.engine.models import CHANNEL_PLANE, OBJECTIVES

#: The Codex frame grammar (etaluma_common.END_RE).
END_RE = re.compile(r"_(WHITE|F1|F2|F3)_(\d{6})\.tif$", re.I)
ROI_RE = re.compile(r"(?:^|_)ROI-(\d+[a-z0-9]*)(?=_|$)", re.I)


def _scan_dataset():
    """``engine.scan_dataset`` once the engine agent has ported it, else None."""
    try:
        import etaluma_video.engine as engine
    except ImportError:  # pragma: no cover
        return None
    return getattr(engine, "scan_dataset", None)


@pytest.fixture(scope="module")
def scratch():
    """Private temp root.

    Not pytest's ``tmp_path``: ``%TEMP%\\pytest-of-<user>`` already exists on this machine
    with ACLs the test process cannot write through, so ``tmp_path`` errors at setup.
    """
    path = Path(tempfile.mkdtemp(prefix="etaluma_fixture_"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="module")
def dataset(scratch):
    return make_dataset(scratch / "20260101_120000_synthetic")


# --------------------------------------------------------------------------- #
# Shape on disk
# --------------------------------------------------------------------------- #


def test_layout_and_filenames_match_the_codex_grammar(dataset):
    tiffs = [p for p in dataset.rglob("*.tif") if "thumbnail" not in p.parts]
    assert len(tiffs) == 3 * 3 * 6, "3 positions x 3 channels x 6 timepoints"
    for p in tiffs:
        assert END_RE.search(p.name), f"{p.name} does not match the frame grammar"
        assert ROI_RE.search(p.stem), f"{p.name} carries no ROI token"
        assert p.parent.name.startswith("ROI-")
    assert {p.parent.name for p in tiffs} == {"ROI-1a", "ROI-2b", "ROI-3c"}
    assert {END_RE.search(p.name).group(1) for p in tiffs} == {"WHITE", "F2", "F3"}
    assert {int(END_RE.search(p.name).group(2)) for p in tiffs} == set(range(6))


def test_flat_layout_puts_every_tiff_in_the_root(scratch):
    root = make_dataset(scratch / "20260102_120000_flat", layout="flat", positions=2,
                        channels=("WHITE",), timepoints=2)
    tiffs = [p for p in root.rglob("*.tif") if "thumbnail" not in p.parts]
    assert tiffs and all(p.parent == root for p in tiffs)
    assert {ROI_RE.search(p.stem).group(1) for p in tiffs} == {"1a", "2b"}


def test_fixed_mode_writes_only_serial_zero(scratch):
    root = make_dataset(scratch / "20260103_120000_fixed", fixed=True, positions=1,
                        channels=("WHITE",), timepoints=6)
    tiffs = [p for p in root.rglob("*.tif") if "thumbnail" not in p.parts]
    assert len(tiffs) == 1
    assert END_RE.search(tiffs[0].name).group(2) == "000000"


def test_thumbnails_are_63x64_png_data_with_a_tif_extension(dataset):
    thumbs = sorted(dataset.rglob("thumbnail/*.thumbnail.tif"))
    assert len(thumbs) == 3 * 3 * 6
    with Image.open(thumbs[0]) as im:
        assert im.format == "PNG"
        assert im.size == (63, 64)
    # Thumbnails must be excluded from any TIFF glob by the Codex rule.
    assert all("thumbnail" in p.parts for p in thumbs)


def test_channel_signal_lives_in_the_right_plane(dataset):
    for channel in ("WHITE", "F2", "F3"):
        frame = next(dataset.glob(f"ROI-1a/*_{channel}_000000.tif"))
        rgb = tifffile.imread(frame)
        assert rgb.shape == (400, 400, 3) and rgb.dtype == np.uint8
        signal = CHANNEL_PLANE[channel]
        body = rgb[: rgb.shape[0] - 90]  # above the overlays
        assert body[..., signal].max() > 60, f"{channel} has no signal in plane {signal}"
        for other in range(3):
            if other != signal:
                assert body[..., other].max() == 0, f"{channel} leaks into plane {other}"


def test_background_is_never_exact_zero_outside_the_timestamp_box(dataset):
    """Real sensors never return exact zero; the timestamp detector depends on it."""
    frame = next(dataset.glob("ROI-1a/*_WHITE_000000.tif"))
    rgb = tifffile.imread(frame)
    overlays = C.detect_overlays(rgb)
    zero = (rgb[..., 0] == 0) & (rgb[..., 1] == 0) & (rgb[..., 2] == 0)
    box = overlays.timestamp_box
    zero[box.y0 : box.y1, box.x0 : box.x1] = False
    assert not zero.any(), "only the timestamp box may be exactly (0, 0, 0)"


def test_f2_blob_moves_and_f3_blob_does_not(dataset):
    def centroid(path):
        plane = tifffile.imread(path)[..., CHANNEL_PLANE[END_RE.search(path.name).group(1)]]
        plane = plane[:300].astype(np.float64)
        total = plane.sum()
        yy, xx = np.mgrid[0 : plane.shape[0], 0 : plane.shape[1]]
        return (yy * plane).sum() / total, (xx * plane).sum() / total

    f2 = [centroid(next(dataset.glob(f"ROI-1a/*_F2_{t:06d}.tif"))) for t in (0, 5)]
    f3 = [centroid(next(dataset.glob(f"ROI-1a/*_F3_{t:06d}.tif"))) for t in (0, 5)]
    assert abs(f2[0][1] - f2[1][1]) > 20, "the F2 blob should travel across the frame"
    assert abs(f3[0][1] - f3[1][1]) < 2, "the F3 blob should stay put"


def test_output_is_deterministic(scratch):
    a = make_dataset(scratch / "20260104_120000_det_a", positions=1, channels=("WHITE", "F2"), timepoints=2)
    b = make_dataset(scratch / "20260105_120000_det_b", positions=1, channels=("WHITE", "F2"), timepoints=2)
    for pa in sorted(p for p in a.rglob("*.tif") if "thumbnail" not in p.parts):
        pb = b / pa.relative_to(a)
        assert pa.read_bytes() == pb.read_bytes(), f"{pa.name} is not reproducible"


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #


def test_epf_uses_the_real_lumaview_tag_names(dataset):
    epf = next(dataset.glob("*.epf"))
    root = ET.parse(epf).getroot()
    assert root.tag == "ProtocolParametersSchema"
    tags = {el.tag for el in root.iter()}
    for required in ("protocolName", "captureEveryHours", "captureEveryMinutes", "captureEverySeconds",
                     "totalPeriodHours", "totalPeriodMinutes", "totalPeriodSeconds",
                     "saveFilesToSeparateSubFolderForEachWell", "roiPositions", "XYZPosition", "savedImages"):
        assert required in tags, f"missing .epf tag {required}"
    assert root.findtext("captureEveryMinutes") == "20"
    assert root.findtext("saveFilesToSeparateSubFolderForEachWell") == "true"

    positions = root.findall(".//roiPositions/List/XYZPosition")
    assert len(positions) == 3
    for el in positions:
        assert {c.tag for c in el} == {"x", "y", "z", "order", "id"}
    assert [el.findtext("id") for el in positions] == ["a", "b", "c"]
    assert [el.findtext("order") for el in positions] == ["1", "2", "3"]
    # scan_dataset matches frames to positions by "ROI-<order><id>".
    assert {f"ROI-{el.findtext('order')}{el.findtext('id')}" for el in positions} == {"ROI-1a", "ROI-2b", "ROI-3c"}
    assert root.findall(".//savedImages/string")


def test_flat_layout_records_the_subfolder_flag(scratch):
    root = make_dataset(scratch / "20260106_120000_flatflag", layout="flat", positions=1,
                        channels=("WHITE",), timepoints=1)
    epf = ET.parse(next(root.glob("*.epf"))).getroot()
    assert epf.findtext("saveFilesToSeparateSubFolderForEachWell") == "false"


def test_avs_scripts_give_the_frame_count(dataset):
    scripts = sorted(dataset.rglob("*.avs"))
    assert len(scripts) == 3 * 3
    for p in scripts:
        text = p.read_text(encoding="utf-8")
        call = re.search(r'ImageSource\s*\(\s*"([^"]+)"(.*?)\)', text, re.I | re.S)
        assert call, f"{p.name} has no ImageSource call"
        args = dict(re.findall(r"(start|end|fps)\s*=\s*([\d.]+)", call.group(2), re.I))
        assert int(args["start"]) == 0 and int(args["end"]) == 5, "6 timepoints means end = 5"
        assert "%06d" in call.group(1)
        first = p.parent / call.group(1).replace("%06d", "000000")
        assert first.is_file(), f"{p.name} points at a frame that was not written"


# --------------------------------------------------------------------------- #
# Burned-in overlays
# --------------------------------------------------------------------------- #


def test_every_written_frame_carries_readable_overlays(dataset):
    for frame in sorted(p for p in dataset.rglob("*.tif") if "thumbnail" not in p.parts):
        overlays, cal = C.calibrate_frame(tifffile.imread(frame), str(frame))
        assert overlays.detected, frame.name
        assert overlays.bar_len_px == bar_length_px(100, "10x")
        assert overlays.timestamp_box is not None
        assert cal.label_um == 100 and cal.label_objective == "10x"
        assert cal.disagreement is False


def test_overlays_can_be_switched_off(scratch):
    root = make_dataset(scratch / "20260107_120000_bare", overlays=False, positions=1,
                        channels=("WHITE",), timepoints=1)
    frame = next(p for p in root.rglob("*.tif") if "thumbnail" not in p.parts)
    overlays, cal = C.calibrate_frame(tifffile.imread(frame), str(frame))
    assert overlays.detected is False and cal.source == "none"


@pytest.mark.parametrize(("label_um", "objective"), [(100, "10x"), (200, "4x"), (50, "20x")])
def test_label_and_objective_are_configurable(scratch, label_um, objective):
    root = make_dataset(scratch / f"20260108_1200{int(label_um):02d}_{objective}", positions=1,
                        channels=("WHITE",), timepoints=1, label_um=label_um, objective=objective)
    frame = next(p for p in root.rglob("*.tif") if "thumbnail" not in p.parts)
    _, cal = C.calibrate_frame(tifffile.imread(frame), str(frame))
    assert cal.label_um == label_um and cal.label_objective == objective
    assert cal.pixel_size_um == pytest.approx(OBJECTIVES[objective], rel=0.01)


def test_a_bar_that_cannot_fit_is_refused(scratch):
    with pytest.raises(ValueError, match="does not fit"):
        make_dataset(scratch / "20260109_120000_toobig", positions=1, channels=("WHITE",),
                     timepoints=1, size=400, label_um=500, objective="10x")
    # The same label renders fine once the frame is big enough.
    rgb, drawn = render_overlay_frame(900, label_um=500, objective="10x")
    assert drawn["bar_box"][3] - drawn["bar_box"][2] == bar_length_px(500, "10x")


def test_unknown_glyph_is_refused(scratch):
    """3, 6, 7, 8 and 9 have no harvested template, so the fixture cannot draw them."""
    # 600 px so the 363 px bar fits and the missing glyph is what fails.
    with pytest.raises(ValueError, match="no harvested glyph template"):
        render_overlay_frame(600, label_um=300, objective="10x")


def test_rejects_bad_arguments(scratch):
    with pytest.raises(ValueError, match="layout"):
        make_dataset(scratch / "20260110_120000_bad", layout="nested")
    with pytest.raises(ValueError, match="objective"):
        make_dataset(scratch / "20260111_120000_bad", objective="63x")


# --------------------------------------------------------------------------- #
# The real parser, once it exists
# --------------------------------------------------------------------------- #


def test_fixture_parses_with_the_codex_grammar(dataset):
    scan_dataset = _scan_dataset()
    if scan_dataset is None:
        pytest.skip("etaluma_video.engine.scan_dataset is not ported yet (engine agent, plan 5.2 item 2); "
                    "the filename/`.epf`/`.avs` grammar is covered by the other tests in this module")
    ds = scan_dataset(dataset)
    assert len(ds.frames) == 3 * 3 * 6
    assert ds.mode == "timelapse"
    assert {f.roi for f in ds.frames} == {"ROI-1a", "ROI-2b", "ROI-3c"}
    assert {f.channel for f in ds.frames} == {"WHITE", "F2", "F3"}
    assert ds.protocols and ds.protocols[0]["interval_seconds"] == 1200
    assert len(ds.positions) == 3
    assert not any("Unrecognized" in w for w in ds.warnings), ds.warnings


def test_fixed_fixture_parses_as_fixed_mode(scratch):
    scan_dataset = _scan_dataset()
    if scan_dataset is None:
        pytest.skip("etaluma_video.engine.scan_dataset is not ported yet (engine agent, plan 5.2 item 2)")
    root = make_dataset(scratch / "20260112_120000_fixedscan", fixed=True, positions=2,
                        channels=("WHITE", "F2"), timepoints=4)
    ds = scan_dataset(root)
    assert ds.mode == "fixed"
    assert len(ds.frames) == 4
