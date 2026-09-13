"""Filename grammar, folder layouts, metadata parsing and pixel access.

Ported from Codex 0.3 ``tests/test_independent.py`` (FilenameTests, RealLayoutTests, EdgeCaseTests
and the plane-extraction part of ScientificTests). Real-sample tests skip when the private
Sample set is absent; nothing here writes inside a sample folder.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
import tifffile

from etaluma_video.engine import parsing as p
from etaluma_video.engine.parsing import (
    Dataset,
    discover_experiments,
    excluded,
    find_experiments,
    inventory,
    parse_avs,
    parse_filename,
    parse_roi,
    read_plane,
    read_rgb,
    scan_dataset,
    summary,
    thumbnail_path,
)

from tests.conftest import require_samples

V04 = Path(__file__).resolve().parent.parent
SOURCE = V04 / "source"

#: (layout, mode, positions, images, channels, frames per position/channel) for all nine samples.
EXPECTED = {
    "20230213": ("ROI subfolders", "fixed", 8, 24, ["WHITE", "F1", "F2"], 1),
    "20250317": ("flat", "fixed", 13, 26, ["WHITE", "F2"], 1),
    "20250522": ("nested channel folders", "fixed", 1, 3, ["WHITE", "F1", "F2"], 1),
    "20250708": ("flat + descriptor", "fixed", 4, 15, ["WHITE", "F1", "F2", "F3"], 1),
    "20260202": ("ROI subfolders", "timelapse", 8, 296, ["WHITE"], 37),
    "20260204": ("ROI subfolders", "timelapse", 1, 681, ["WHITE", "F2", "F3"], 227),
    "20260209": ("ROI subfolders", "timelapse", 6, 792, ["WHITE", "F3"], 66),
    "20260313": ("ROI subfolders", "timelapse", 6, 3528, ["WHITE", "F2", "F3"], 196),
    "20260707": ("ROI subfolders", "fixed", 1, 4, ["WHITE", "F1", "F2", "F3"], 1),
    # 0.7: labware well names, a folder per well, a folder per channel inside it, flat folders
    "20260913_001916": ("position subfolders", "fixed", 4, 4, ["WHITE"], 1),
    "20260913_002031": ("position subfolders", "timelapse", 4, 96, ["WHITE", "F1", "F2", "F3"], 6),
    "20260913_002653": ("nested channel folders", "timelapse", 3, 48, ["WHITE", "F1", "F2", "F3"], 4),
    "20260913_003049": ("nested channel folders", "timelapse", 2, 40, ["WHITE", "F1", "F2", "F3"], 5),
    "20260913_003438": ("flat", "timelapse", 2, 12, ["WHITE", "F3"], 3),
    "20260913_003801": ("flat", "timelapse", 4, 24, ["WHITE", "F3"], 3),
}


def capture(root: Path, name: str = "Sample_ROI-1a_F2_000000.tif", value: int = 20,
            shape: tuple[int, int] = (80, 100), array: np.ndarray | None = None) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(path, np.full(shape, value, dtype=np.uint8) if array is None else array)
    return path


# --------------------------------------------------------------------------- #
# Filename grammar
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name,roi,channel,serial,descriptor", [
    ("Sample_ROI-1a_WHITE_000000.tif", "ROI-1a", "WHITE", 0, ""),
    ("sample_ROI-10j_F2_000036.tif", "ROI-10j", "F2", 36, ""),
    ("Timelapse_ROI-1a_AZR_cf_F1_000000.tif", "ROI-1a", "F1", 0, "AZR_cf"),
    ("Timelapse_ROI-4d_2AZR_CF_F3_000000.tif", "ROI-4d", "F3", 0, "2AZR_CF"),
    ("Timelapse_ROI-1s1a_F2_000195.tif", "ROI-1s1a", "F2", 195, ""),
    ("Timelapse_ROI-3th1_F2_000195.tif", "ROI-3th1", "F2", 195, ""),
    (r"C:\capture\ROI-1b\WHITE\capture_000000.tif", "ROI-1b", "WHITE", 0, ""),
    ("capture/ROI-1b/F1/000000.tif", "ROI-1b", "F1", 0, ""),
    ("capture/ROI-2b/capture_F2_000001.tif", "ROI-2b", "F2", 1, ""),
    ("Sample_ROI-1a_f2_000000.TIFF", "ROI-1a", "F2", 0, ""),
    ("Sample_ROI-1a_text_F1_more_F3_999999.tif", "ROI-1a", "F3", 999999, "text_F1_more"),
])
def test_grammar_variants(name, roi, channel, serial, descriptor):
    f = parse_filename(name)
    assert f is not None
    assert (f.roi, f.channel, f.serial, f.descriptor) == (roi, channel, serial, descriptor)


@pytest.mark.parametrize("name", ["random.tif", "Sample_ROI-1a_F4_000000.tif", "Sample_ROI-1a_F2_00000.tif",
                                  "Sample_ROI-1a_F2_000000.jpg", "Sample_F2_000000.tif"])
def test_reject_unrecognized_names(name):
    assert parse_filename(name) is None


def test_generated_and_cache_folders_are_invisible(tmp_path):
    # The test name must not contain the excluded words: pytest puts it in the temp folder name.
    capture(tmp_path)
    capture(tmp_path, "ROI-1a/Sample_ROI-1a_F2_000002.tif")
    capture(tmp_path, "analysis_output/Sample_ROI-1a_F2_000000.tif")
    capture(tmp_path, "my_thumbnail_cache/Sample_ROI-1a_F2_000000.tif")
    ds = scan_dataset(tmp_path)
    assert len(ds.frames) == 2
    assert ds.mode == "timelapse"
    assert any("non-contiguous" in w for w in ds.warnings)
    assert excluded(tmp_path / "analysis_output" / "x.tif")
    capture(tmp_path, "duplicate/Sample_ROI-1a_F2_000000.tif")
    with pytest.raises(ValueError, match="Duplicate"):
        scan_dataset(tmp_path)


def test_sidecar_preview_path(tmp_path):
    path = capture(tmp_path)
    assert thumbnail_path(path) is None
    thumb = tmp_path / "thumbnail" / "Sample_ROI-1a_F2_000000.thumbnail.tif"
    thumb.parent.mkdir()
    thumb.write_bytes(b"PNG-data-with-a-tif-extension")
    assert thumbnail_path(path) == thumb
    ds = scan_dataset(tmp_path)
    assert len(ds.frames) == 1 and thumbnail_path(ds.frames[0]) == thumb


def test_malformed_metadata_warns_without_crashing(tmp_path):
    capture(tmp_path)
    (tmp_path / "broken.epf").write_text("<broken>")
    (tmp_path / "broken.avs").write_text('ImageSource("a.tif", start=0)')
    ds = scan_dataset(tmp_path)
    assert ds.mode == "fixed"
    assert sum("Metadata could not be read" in w for w in ds.warnings) == 2


def test_namespaced_roi_numeric_leading_id_and_uncaptured_position(tmp_path):
    capture(tmp_path, "Sample_ROI-11s1a_F2_000000.tif")
    xml = ('<R xmlns="urn:test"><Positions><Order>1</Order><ID>1s1a</ID><x>2.5</x><y>-2</y><z>6.1</z></Positions>'
           '<Positions><Order>2</Order><ID>b</ID><x>4</x><y>5</y><z>6</z></Positions></R>')
    (tmp_path / "map.roi").write_text(xml)
    ds = scan_dataset(tmp_path)
    assert (ds.frames[0].order, ds.frames[0].id) == (1, "1s1a")
    assert any("2/b has no captured" in w for w in ds.warnings)


def test_nonzero_single_serial_routes_timelapse(tmp_path):
    capture(tmp_path, "Sample_ROI-1a_F2_000020.tif")
    assert scan_dataset(tmp_path).mode == "timelapse"


def test_avs_is_crosscheck_not_frame_count_authority(tmp_path):
    capture(tmp_path, "Sample_ROI-1a_F2_000002.tif")
    capture(tmp_path, "Sample_ROI-1a_F2_000003.tif")
    path = tmp_path / "sequence.avs"
    path.write_text('ImageSource("Sample_ROI-1a_F2_%06d.tif", start=2, end=8, fps=2.5)')
    parsed = parse_avs(path)
    assert (parsed["n_frames"], parsed["fps"], parsed["roi"], parsed["channel"]) == (7, 2.5, "ROI-1a", "F2")
    ds = scan_dataset(tmp_path)
    assert inventory(ds)[0]["n_frames"] == 2
    assert any("2 TIFFs; AVS describes 7" in w for w in ds.warnings)


def test_explicit_objective_metadata_and_conflicts(tmp_path):
    capture(tmp_path)
    epf = tmp_path / "capture.epf"
    epf.write_text("<Protocol><objective><magnification>20</magnification></objective></Protocol>")
    assert scan_dataset(tmp_path).objective_metadata["objective"] == "20x"
    avs = tmp_path / "capture.avs"
    avs.write_text('# Objective = 10x\nImageSource("Sample_ROI-1a_F2_%06d.tif", start=0, end=0, fps=10)')
    assert scan_dataset(tmp_path).objective_metadata["objective"] is None
    avs.unlink()
    epf.write_text("<Protocol><objectiveMagnification>60x</objectiveMagnification></Protocol>")
    assert scan_dataset(tmp_path).objective_metadata["objective"] is None
    epf.write_text("<Protocol><protocolName>10x test</protocolName><videoLengthSeconds>10</videoLengthSeconds></Protocol>")
    assert scan_dataset(tmp_path).objective_metadata["status"] == "missing"


# --------------------------------------------------------------------------- #
# Pixels
# --------------------------------------------------------------------------- #


def test_rgb_plane_extraction(tmp_path):
    rgb = np.zeros((8, 10, 3), dtype=np.uint8)
    rgb[:] = (11, 22, 33)
    path = capture(tmp_path, array=rgb)
    for ch, value in (("WHITE", 11), ("F1", 33), ("F2", 22), ("F3", 11)):
        assert np.all(read_plane(path, ch) == value)
    whole = read_rgb(path)
    assert whole.shape == (8, 10, 3) and whole.dtype == np.uint8
    np.testing.assert_array_equal(whole[0, 0], (11, 22, 33))


def test_read_rgb_stacks_grayscale_and_rejects_multipage(tmp_path):
    path = capture(tmp_path, value=77, shape=(6, 7))
    gray = read_rgb(path)
    assert gray.shape == (6, 7, 3)
    assert np.all(gray == 77)
    with tifffile.TiffWriter(path) as tif:
        tif.write(np.zeros((6, 7), dtype=np.uint8))
        tif.write(np.zeros((6, 7), dtype=np.uint8))
    with pytest.raises(ValueError, match="Multi-page"):
        read_rgb(path)
    with pytest.raises(ValueError, match="Multi-page"):
        read_plane(path, "F2")


def test_higher_bit_rejected(tmp_path):
    path = capture(tmp_path, array=np.zeros((8, 10), dtype=np.uint16))
    with pytest.raises(ValueError, match="8-bit"):
        read_plane(path, "F2")
    with pytest.raises(ValueError, match="8-bit"):
        read_rgb(path)


# --------------------------------------------------------------------------- #
# Real samples
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def real_datasets(samples):
    if samples is None:
        pytest.skip("real Etaluma samples not available (set ETALUMA_SAMPLES)")
    # the six 0.7 sets share a date, so they are keyed by date and time
    return {(p.name[:15] if p.name.startswith("20260913") else p.name[:8]): scan_dataset(p)
            for p in sorted(samples.iterdir()) if p.is_dir()}


def test_every_layout_exact_inventory(real_datasets):
    assert set(real_datasets) <= set(EXPECTED), "unknown sample folder; extend EXPECTED"
    for key, ds in real_datasets.items():
        layout, mode, nroi, images, channels, frames = EXPECTED[key]
        assert (ds.layout, ds.mode, len(ds.groups), len(ds.frames), ds.channels) == (layout, mode, nroi, images, channels)
        for row in inventory(ds):
            assert (row["n_frames"], row["first_serial"], row["last_serial"]) == (frames, 0, frames - 1)
        # Independent file walk and filename decomposition, not the engine regex.
        raw = [q for q in Path(ds.root).rglob("*.tif")
               if "analysis_output" not in q.parts and "thumbnail" not in str(q).lower()]
        assert len(raw) == images
        assert {(f.roi, f.channel, f"{f.serial:06d}") for f in ds.frames} == {_key(q, Path(ds.root)) for q in raw}


def test_insphero_missing_red_is_explicit(real_datasets):
    ds = real_datasets.get("20250708")
    if ds is None:
        pytest.skip("sample 20250708 not available")
    assert set(ds.groups["ROI-4d"]) == {"WHITE", "F1", "F2"}
    assert any("ROI-4d" in w and "F3" in w for w in ds.warnings)


def test_cd14_order_mapping_warns_on_actual_id_mismatch(real_datasets):
    ds = real_datasets.get("20260313")
    if ds is None:
        pytest.skip("sample 20260313 not available")
    row = next(row for row in inventory(ds) if row["ROI"] == "ROI-6g")
    assert (row["ID"], row["protocol_ID"]) == ("g", "q2")
    assert any("ROI-6g" in w and "q2" in w for w in ds.warnings)
    assert row["x"] == pytest.approx(13.378596305847168)


def test_real_protocol_values_and_ordered_saved_images(real_datasets):
    for ds in real_datasets.values():
        for protocol in ds.protocols:
            root = ET.parse(protocol["path"]).getroot()
            raw = {x.tag: x.text for x in root if len(x) == 0}
            assert protocol["protocol_name"] == (raw["protocolName"] or "").strip()
            interval = sum(float(raw.get("captureEvery" + u, 0) or 0) * m
                           for u, m in [("Hours", 3600), ("Minutes", 60), ("Seconds", 1)])
            assert protocol["interval_seconds"] == interval
            assert protocol["saved_images"] == [x.text or "" for x in root.findall(".//savedImages/string")]
            for tag in ("whiteLedState", "f2LedGain", "masterLedExposure", "ledsOffBetweenCaptures", "videoLengthSeconds"):
                if tag in raw:
                    assert protocol["raw"][tag] == (raw[tag] or "").strip()
    ds = real_datasets.get("20260202")
    if ds is not None:
        d = ds.protocol
        assert (d["interval_seconds"], d["planned_duration_seconds"], d["expected_frames"]) == (600, 360000, 600)
        assert d["raw"]["whiteLedExposure"] == "1288"


def test_real_roi_and_avs_against_raw_files(samples, real_datasets):
    count_roi = count_avs = 0
    for path in samples.glob("*.roi"):
        assert _positions(path) == [(p["order"], p["id"], p["x"], p["y"], p["z"]) for p in parse_roi(path)]
        count_roi += 1
    for ds in real_datasets.values():
        for path in Path(ds.root).rglob("*.roi"):
            if excluded(path):
                continue
            assert _positions(path) == [(p["order"], p["id"], p["x"], p["y"], p["z"]) for p in parse_roi(path)]
            count_roi += 1
        for got in ds.avs:
            text = Path(got["path"]).read_text()
            start = int(re.search(r"start\s*=\s*(\d+)", text)[1])
            end = int(re.search(r"end\s*=\s*(\d+)", text)[1])
            fps = float(re.search(r"fps\s*=\s*([\d.]+)", text)[1])
            assert (got["start"], got["end"], got["n_frames"], got["fps"]) == (start, end, end - start + 1, fps)
            captured = len(ds.groups[got["roi"]][got["channel"]])
            if captured != got["n_frames"]:
                # A real discrepancy in the sample captures; the scan must say so rather than hide it.
                assert any(f'{got["roi"]}/{got["channel"]}' in w and "AVS describes" in w for w in ds.warnings)
            count_avs += 1
    assert count_roi > 0
    assert count_avs > 0


def _key(path: Path, root: Path) -> tuple[str, str, str]:
    """Position name, channel and serial read from the path by hand, without the engine's regex."""
    stem = path.stem
    folders = path.relative_to(root).parts[:-1]
    if "_ROI-" in stem or stem.startswith("ROI-"):
        token = "ROI-" + stem.split("ROI-", 1)[1].split("_")[0]
    elif any(part.upper().startswith("ROI-") for part in folders):
        token = next(part for part in reversed(folders) if part.upper().startswith("ROI-"))
    else:  # labware names (0.7): the part of the name that starts with "Well"
        token = next(part for part in stem.split("_") if part.lower().startswith("well"))
    tail = stem.rsplit("_", 2)[-2:]
    channel = tail[0].upper() if len(tail) == 2 and tail[0].upper() in ("WHITE", "F1", "F2", "F3") else         next(part.upper() for part in reversed(path.relative_to(root).parts) if part.upper() in ("WHITE", "F1", "F2", "F3"))
    return token, channel, stem[-6:]


def _positions(path: Path) -> list[tuple]:
    expected = []
    for el in ET.parse(path).getroot().iter():
        values = {c.tag.lower(): c.text for c in el}
        if {"order", "id", "x", "y", "z"} <= values.keys():
            expected.append((int(values["order"]), values["id"], *[float(values[k]) for k in ("x", "y", "z")]))
    return expected


def test_batch_discovery_and_parent_rejection(samples, real_datasets):
    found = {(q.name[:15] if q.name.startswith("20260913") else q.name[:8]) for q in find_experiments(samples)}
    assert found == set(real_datasets)
    assert discover_experiments(next(iter(samples.iterdir()))) is not None
    with pytest.raises(ValueError, match="nested experiments"):
        scan_dataset(samples)


def test_cli_inspect_prints_json_for_every_layout(samples, real_datasets):
    env = {**os.environ, "PYTHONPATH": str(SOURCE), "PYTHONIOENCODING": "utf-8"}
    for key, ds in real_datasets.items():
        process = subprocess.run([sys.executable, "-m", "etaluma_video.cli", ds.root, "--inspect"],
                                 capture_output=True, text=True, encoding="utf-8", timeout=180, env=env)
        assert process.returncode == 0, process.stderr
        data = json.loads(process.stdout)
        assert (data["layout"], data["mode"], data["n_rois"], data["n_images"], data["channels"]) == EXPECTED[key][:5]
        assert "calibration" in data and "overlays" in data


def test_cli_batch_inspect(samples, real_datasets):
    env = {**os.environ, "PYTHONPATH": str(SOURCE), "PYTHONIOENCODING": "utf-8"}
    process = subprocess.run([sys.executable, "-m", "etaluma_video.cli", str(samples), "--inspect", "--batch"],
                             capture_output=True, text=True, encoding="utf-8", timeout=600, env=env)
    assert process.returncode == 0, process.stderr
    assert len(json.loads(process.stdout)) == len(real_datasets)


# --------------------------------------------------------------------------- #
# 0.7: any position name, in every layout
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rel,roi,channel,serial", [
    ("Wella1/Sample_Wella1_WHITE_000000.tif", "Wella1", "WHITE", 0),
    ("Welli11/F1/Sample_Welli11_F1_000003.tif", "Welli11", "F1", 3),
    ("Sample_Wellc2_F3_000001.tif", "Wellc2", "F3", 1),
    ("My_Slide_1/Sample_My_Slide_1_F2_000000.tif", "My_Slide_1", "F2", 0),
    ("Sample_Chamber7_F2_000000.tif", "Chamber7", "F2", 0),
    ("Sample_ROI-2b_F2_000004.tif", "ROI-2b", "F2", 4),
])
def test_any_position_name(tmp_path, rel, roi, channel, serial):
    frame = parse_filename(tmp_path / rel, tmp_path)
    assert (frame.roi, frame.channel, frame.serial) == (roi, channel, serial)


def test_labware_positions_follow_capture_order(tmp_path):
    # captured c3, then a1, then b2: names alone would sort a1, b2, c3
    for i, well in enumerate(["Wellc3", "Wella1", "Wellb2"]):
        for serial in range(2):
            path = capture(tmp_path, f"{well}/Sample_{well}_F2_{serial:06d}.tif")
            stamp = 1_000_000 + serial * 100 + i
            os.utime(path, (stamp, stamp))
    ds = scan_dataset(tmp_path)
    assert list(ds.groups) == ["Wellc3", "Wella1", "Wellb2"]
    assert ds.layout == "position subfolders" and ds.mode == "timelapse"
    notes = p.summarize_warnings(ds.warnings)
    assert sum("no stage coordinates" in w for w in notes) == 1


def test_well_channel_folders_and_flat_wells(tmp_path):
    nested = tmp_path / "nested"
    for ch in ("WHITE", "F1"):
        for serial in range(3):
            capture(nested, f"Welli11/{ch}/Sample_Welli11_{ch}_{serial:06d}.tif")
    ds = scan_dataset(nested)
    assert (ds.layout, list(ds.groups), ds.channels, len(ds.frames)) == ("nested channel folders", ["Welli11"], ["WHITE", "F1"], 6)
    flat = tmp_path / "flat"
    for well in ("Wellc2", "Wellc3"):
        capture(flat, f"Sample_{well}_F3_000000.tif")
    ds = scan_dataset(flat)
    assert (ds.layout, sorted(ds.groups), ds.mode) == ("flat", ["Wellc2", "Wellc3"], "fixed")
