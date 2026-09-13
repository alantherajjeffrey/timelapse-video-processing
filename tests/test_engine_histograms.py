"""Exact histogram passes, overlay exclusion, merging and the on-disk cache.

Replaces Codex 0.3's ``prepare_render_profile`` tests: 0.4 has no draft/exact split (every pass
is exact) and the percentile rule lives in ``display``, so these tests check that the counts are
exactly the pixels of the covered frames and that the cache never silently serves stale numbers.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import tifffile

from etaluma_video.engine import histograms as H
from etaluma_video.engine.models import HistogramSet, percentile_from_counts
from etaluma_video.engine.parsing import read_plane, scan_dataset

from tests.conftest import require_samples


def build(root: Path, rois=("1a", "2b"), channels=("WHITE", "F2"), serials=3, shape=(24, 32)) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(7)
    for roi in rois:
        for ch in channels:
            for s in range(serials):
                a = rng.integers(0, 256, size=shape, dtype=np.uint8)
                a[0, :4] = 250  # a bright corner so percentiles are not degenerate
                tifffile.imwrite(root / f"Sample_ROI-{roi}_{ch}_{s:06d}.tif", a)
    return root


def test_counts_are_every_pixel_of_every_covered_frame(tmp_path):
    ds = scan_dataset(build(tmp_path / "src"))
    hists = H.build_histograms(ds)
    for ch in ds.channels:
        pixels = np.concatenate([read_plane(f.path, ch).ravel() for f in ds.frames if f.channel == ch])
        np.testing.assert_array_equal(hists.channels[ch].counts, np.bincount(pixels, minlength=256))
        assert hists.channels[ch].pixels == pixels.size
        assert hists.channels[ch].frames == 6
        for p in (0.5, 50, 99.5, 99.9):
            assert hists.channels[ch].percentile(p) == pytest.approx(np.percentile(pixels, p), abs=1e-9)


def test_running_per_frame_p995_matches_numpy(tmp_path):
    ds = scan_dataset(build(tmp_path / "src", rois=("1a",), channels=("F2",), serials=4))
    hists = H.build_histograms(ds)
    expected = max(float(np.percentile(read_plane(f.path, "F2"), 99.5)) for f in ds.frames)
    assert hists.channels["F2"].frame_p995_max == pytest.approx(expected)


def test_positions_partition_the_pass_and_merge_back(tmp_path):
    ds = scan_dataset(build(tmp_path / "src"))
    whole = H.build_histograms(ds)
    parts = [H.build_histograms(ds, [roi]) for roi in ds.groups]
    merged = parts[0].merge(parts[1])
    for ch in ds.channels:
        np.testing.assert_array_equal(merged.channels[ch].counts, whole.channels[ch].counts)
        assert merged.channels[ch].frames == whole.channels[ch].frames
    assert sorted(merged.positions) == sorted(whole.positions)
    assert merged.complete is (len(merged.positions) >= merged.total_positions)


def test_channel_subset_and_unknown_position(tmp_path):
    ds = scan_dataset(build(tmp_path / "src"))
    only = H.build_histograms(ds, channels=["F2"])
    assert set(only.channels) == {"F2"}
    empty = H.build_histograms(ds, ["ROI-nope"])
    assert empty.channels == {}


def test_overlay_mask_removes_those_pixels(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    a = np.zeros((20, 30), dtype=np.uint8)
    a[18:, :] = 255  # a burned-in strip along the bottom
    tifffile.imwrite(root / "Sample_ROI-1a_F2_000000.tif", a)
    ds = scan_dataset(root)
    mask = np.zeros((20, 30), dtype=bool)
    mask[18:, :] = True
    plain = H.build_histograms(ds)
    masked = H.build_histograms(ds, overlay_mask=mask)
    assert plain.channels["F2"].counts[255] == 60
    assert masked.channels["F2"].counts[255] == 0
    assert masked.channels["F2"].pixels == 18 * 30


def test_first_white_median_ignores_overlay(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    a = np.full((10, 10), 40, dtype=np.uint8)
    a[:5] = 255
    tifffile.imwrite(root / "Sample_ROI-1a_WHITE_000000.tif", a)
    ds = scan_dataset(root)
    assert H.first_white_median(ds) == pytest.approx(147.5)
    mask = np.zeros((10, 10), dtype=bool)
    mask[:5] = True
    assert H.first_white_median(ds, mask) == 40
    # No WHITE channel at all is not an error.
    other = tmp_path / "f2only"
    other.mkdir()
    tifffile.imwrite(other / "Sample_ROI-1a_F2_000000.tif", a)
    assert H.first_white_median(scan_dataset(other)) is None


def test_cache_path_depends_on_source_and_radius(tmp_path):
    a = H.histogram_cache_path(tmp_path, "fingerprint-a", "ROI-1a", None)
    b = H.histogram_cache_path(tmp_path, "fingerprint-b", "ROI-1a", None)
    c = H.histogram_cache_path(tmp_path, "fingerprint-a", "ROI-1a", 50)
    d = H.histogram_cache_path(tmp_path, "fingerprint-a", "ROI-2b", None)
    assert a.name.startswith("hist_v5_") and a.suffix == ".npz"
    assert len({a, b, c, d}) == 4
    assert a == H.histogram_cache_path(tmp_path, "fingerprint-a", "ROI-1a", None)


def test_cached_position_is_reused_without_reading_pixels(tmp_path, monkeypatch):
    ds = scan_dataset(build(tmp_path / "src", rois=("1a",)))
    cache = tmp_path / "cache"
    first = H.load_or_build_position(ds, "ROI-1a", cache)
    assert list(cache.glob("hist_v5_*.npz"))
    monkeypatch.setattr(H, "read_plane", lambda *a, **k: pytest.fail("the cache must not re-read pixels"))
    second = H.load_or_build_position(ds, "ROI-1a", cache)
    for ch in ds.channels:
        np.testing.assert_array_equal(first.channels[ch].counts, second.channels[ch].counts)
        assert first.channels[ch].frame_p995_max == second.channels[ch].frame_p995_max
    assert second.fingerprint == ds.source_fingerprint


def test_changed_source_invalidates_the_cache(tmp_path):
    src = build(tmp_path / "src", rois=("1a",))
    ds = scan_dataset(src)
    cache = tmp_path / "cache"
    H.load_or_build_position(ds, "ROI-1a", cache)
    tifffile.imwrite(src / "Sample_ROI-1a_F2_000000.tif", np.full((24, 32), 9, np.uint8))
    changed = scan_dataset(src)
    assert changed.source_fingerprint != ds.source_fingerprint
    rebuilt = H.load_or_build_position(changed, "ROI-1a", cache)
    assert rebuilt.channels["F2"].counts[9] >= 24 * 32


def test_partial_set_is_completed_not_remeasured(tmp_path, monkeypatch):
    ds = scan_dataset(build(tmp_path / "src"))
    partial = H.build_histograms(ds, ["ROI-1a"])
    whole = H.build_histograms(ds)
    read = []
    original = H.read_plane
    monkeypatch.setattr(H, "read_plane", lambda path, ch: (read.append(path), original(path, ch))[1])
    merged, per_position = H.build_all_positions(ds, partial=partial)
    assert set(per_position) == {"ROI-2b"}
    assert all("ROI-2b" in Path(p).name for p in read)
    for ch in ds.channels:
        np.testing.assert_array_equal(merged.channels[ch].counts, whole.channels[ch].counts)
    assert merged.complete


def test_histogram_set_round_trips_through_npz(tmp_path):
    ds = scan_dataset(build(tmp_path / "src", rois=("1a",)))
    hists = H.build_histograms(ds)
    path = tmp_path / "hist.npz"
    hists.save(path)
    loaded = HistogramSet.load(path)
    for ch in ds.channels:
        np.testing.assert_array_equal(loaded.channels[ch].counts, hists.channels[ch].counts)
    assert loaded.positions == hists.positions and loaded.fingerprint == hists.fingerprint


def test_percentile_from_counts_matches_numpy():
    rng = np.random.default_rng(3)
    values = rng.integers(0, 256, size=5000, dtype=np.uint8)
    counts = np.bincount(values, minlength=256)
    for p in (0, 0.5, 25, 50, 90, 99.5, 99.9, 100):
        assert percentile_from_counts(counts, p) == pytest.approx(np.percentile(values, p), abs=1e-9)


def test_real_sample_histogram_covers_one_position(samples):
    require_samples(samples, "20260707_221520-verify if power")
    ds = scan_dataset(samples / "20260707_221520-verify if power")
    hists = H.build_histograms(ds, ["ROI-1a"], channels=["WHITE"])
    assert hists.channels["WHITE"].pixels == 1900 * 1900
    assert hists.channels["WHITE"].frames == 1
    assert 0 <= hists.channels["WHITE"].percentile(99.9) <= 255
