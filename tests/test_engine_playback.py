"""Playback timing. Ported from Codex 0.3 ``tests/test_playback.py`` plus synthetic coverage.

Decision 9: 10 seconds of playback by default, whatever the number of timepoints.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import tifffile

from etaluma_video.engine.export import playback_fps, video_frame
from etaluma_video.engine.parsing import scan_dataset
from etaluma_video.engine.process import Options, process_dataset

from tests.conftest import require_samples


def test_protocol_duration_avs_and_explicit_override_are_distinct(samples):
    require_samples(samples, "20260313_195541_CD14")
    ds = scan_dataset(samples / "20260313_195541_CD14")
    first = ds.frames[0]
    assert playback_fps(ds, Options(), first, 196) == pytest.approx(19.6)
    assert playback_fps(ds, Options(playback_source="protocol"), first, 196) == pytest.approx(39.2)
    assert playback_fps(ds, Options(playback_source="fps"), first, 196) == 10
    assert playback_fps(ds, Options(duration_seconds=20), first, 196) == pytest.approx(9.8)
    assert playback_fps(ds, Options(playback_source="avs"), first, 196) == 1
    assert playback_fps(ds, Options(fps=24), first, 196) == 24


def test_ten_second_rule_on_a_synthetic_experiment(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for serial in range(5):
        tifffile.imwrite(src / f"Sample_ROI-1a_F2_{serial:06d}.tif", np.full((40, 60), 40 + serial * 20, np.uint8))
    ds = scan_dataset(src)
    assert playback_fps(ds, Options(), ds.frames[0], 5) == 0.5
    out = process_dataset(ds, Options(width=160, tile_width=160, objective="10x", mp4=False), tmp_path / "out")["output"]
    frame, count, fps = video_frame(out / "src_ROI-1a_F2.avi", 0)
    assert count == 5
    assert count / fps == pytest.approx(10, rel=1e-3)
    assert frame.shape[1] == 160
