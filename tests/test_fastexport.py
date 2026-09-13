"""0.5 single-pass export: which videos a position gets, output geometry, and an end-to-end run."""
from __future__ import annotations

from pathlib import Path

from etaluma_video.engine import Options, process_dataset, scan_dataset
from etaluma_video.engine.fastexport import output_size, plan_videos
from etaluma_video.engine.models import DisplayProfile
from tests.fixtures.make_synthetic_dataset import make_dataset


def by(serials: dict) -> dict:
    return {ch: {s: object() for s in ss} for ch, ss in serials.items()}


def test_default_ui_selection_is_composite_and_fluorescence_only():
    p = DisplayProfile.default_for(["WHITE", "F2", "F3"])
    plans, warnings = plan_videos(by({"WHITE": range(5), "F2": range(5), "F3": range(5)}), p,
                                  composite=True, fluorescence_only=True, channel_videos=[])
    assert [x.name for x in plans] == ["composite", "fluorescence"]
    assert not warnings


def test_every_channel_and_missing_timepoints():
    p = DisplayProfile.default_for(["WHITE", "F2"])
    plans, warnings = plan_videos(by({"WHITE": [0, 1, 2], "F2": [0, 1, 2, 3]}), p,
                                  composite=True, fluorescence_only=True, channel_videos=None)
    assert [x.name for x in plans] == ["WHITE", "F2", "composite", "fluorescence"]
    assert next(x for x in plans if x.name == "composite").serials == [0, 1, 2]
    assert next(x for x in plans if x.name == "F2").serials == [0, 1, 2, 3]
    assert warnings


def test_white_only_experiment_still_gets_a_video():
    p = DisplayProfile.default_for(["WHITE"])
    plans, _ = plan_videos(by({"WHITE": range(3)}), p, composite=True, fluorescence_only=True, channel_videos=[])
    assert [x.name for x in plans] == ["WHITE"]


def test_disabled_channel_is_left_out_of_composites():
    p = DisplayProfile.default_for(["WHITE", "F2", "F3"])
    p.channels["F3"].enabled = False
    plans, _ = plan_videos(by({"WHITE": range(3), "F2": range(3), "F3": range(3)}), p,
                           composite=True, fluorescence_only=True, channel_videos=[])
    assert next(x for x in plans if x.name == "composite").members == ["WHITE", "F2"]
    assert next(x for x in plans if x.name == "fluorescence").members == ["F2"]


def test_output_size_is_even_and_follows_the_requested_width():
    assert output_size(1900, 1900, 950) == (950, 950)
    assert output_size(1900, 1900, None) == (1900, 1900)
    assert output_size(401, 401, None) == (400, 400)
    assert output_size(400, 400, 950) == (950, 950)


def test_process_writes_only_the_selected_videos(tmp_path):
    ds = scan_dataset(make_dataset(tmp_path / "20260101_120000_synthetic"))
    opts = Options()
    opts.channel_videos = ["F2"]
    opts.avi = False
    result = process_dataset(ds, opts, output=tmp_path / "out", progress=lambda m: None)
    kinds = sorted(v["kind"] for v in result["videos"])
    assert kinds == sorted(["F2", "composite", "fluorescence"] * 3)
    assert all(v["frames"] == 6 and v["file"].endswith(".mp4") for v in result["videos"])
    montages = list((Path(result["output"]) / "montages").glob("*_montage.png"))
    assert len(montages) == 3
