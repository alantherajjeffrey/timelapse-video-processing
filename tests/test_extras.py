"""0.7 extras: timepoint range, channel names on montages, the channel key on videos."""
from __future__ import annotations

import numpy as np
import pytest

from etaluma_video.engine import Options, process_dataset, scan_dataset, validate_options
from etaluma_video.engine.fastexport import plan_montage
from etaluma_video.engine.models import DisplayProfile
from etaluma_video.engine.overlays import OverlayPainter
from tests.fixtures.make_synthetic_dataset import make_dataset


def test_timepoint_range_exports_only_those_timepoints(tmp_path):
    ds = scan_dataset(make_dataset(tmp_path / "20260101_120000_synthetic"))
    opts = Options(width=160, tile_width=160, objective="10x", avi=False, dashboard=False,
                   channel_videos=[], timepoint_range=[2, 4])
    result = process_dataset(ds, opts, tmp_path / "out", progress=lambda m: None)
    assert result["videos"]
    assert all(v["frames"] == 3 and v["serials"] == [1, 2, 3] for v in result["videos"])


def test_an_impossible_timepoint_range_is_refused(tmp_path):
    ds = scan_dataset(make_dataset(tmp_path / "20260101_120000_synthetic"))
    with pytest.raises(ValueError, match="timepoint range"):
        validate_options(ds, Options(timepoint_range=[3, 2]))


def test_channel_names_label_the_montage_rows():
    profile = DisplayProfile.default_for(["WHITE", "F2"])
    rows, picks = plan_montage({"WHITE": {0: 1, 1: 1}, "F2": {0: 1, 1: 1}}, profile, {"F2": "CFDA"})
    assert "F2 · CFDA" in [r.label for r in rows] and picks == [0, 1]


def test_channel_key_is_drawn_top_left():
    painter = OverlayPainter((400, 400), (400, 400))
    out = painter.draw(np.zeros((400, 400, 3), np.uint8), None, [("CFDA", (0.0, 1.0, 0.0)), ("PI", (1.0, 0.0, 1.0))])
    corner = out[:60, :320]
    assert corner[..., 1].max() == 255 and (corner[..., 0] == 255).any()
    assert not out[200:, :].any()
