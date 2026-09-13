"""Cooperative cancellation and batch semantics.

Replaces Codex 0.3's ``JobManager`` tests: the threading now lives in ``ui/jobs_qt.py``, so the
engine side is the ``CancelToken`` protocol, the ``cancelled`` run metadata, and
``process_batch`` continuing after a failed experiment.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np
import pytest
import tifffile

from etaluma_video.engine import histograms as H
from etaluma_video.engine.jobs import CancelToken, JobCancelled, check_cancel
from etaluma_video.engine.parsing import scan_dataset
from etaluma_video.engine.process import Options, process_batch, process_dataset


def experiment(root: Path, channels=("F2",), serials=3, value=60) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for ch in channels:
        for serial in range(serials):
            tifffile.imwrite(root / f"Sample_ROI-1a_{ch}_{serial:06d}.tif",
                             np.full((40, 60), value + serial * 10, np.uint8))
    return root


def small(**kwargs) -> Options:
    return Options(width=160, tile_width=160, objective="10x", mp4=False, **kwargs)


def test_cancel_token_protocol():
    token = CancelToken()
    assert not token.cancelled and token() is False
    check_cancel(token)
    check_cancel(None)
    token.cancel()
    assert token.cancelled and token() is True
    with pytest.raises(JobCancelled):
        check_cancel(token)
    with pytest.raises(JobCancelled):
        token.check()
    # Any zero-argument callable is accepted (the Codex 0.3 protocol).
    with pytest.raises(JobCancelled):
        check_cancel(lambda: True)


def test_cancel_token_is_thread_safe():
    token = CancelToken()
    seen = []
    worker = threading.Thread(target=lambda: seen.append(token.cancelled))
    token.cancel()
    worker.start()
    worker.join(2)
    assert seen == [True]


def test_cancel_during_the_histogram_pass(tmp_path):
    ds = scan_dataset(experiment(tmp_path / "src", serials=4))
    state = {"cancel": False}
    with pytest.raises(JobCancelled):
        H.build_histograms(ds, progress=lambda _: state.update(cancel=True), cancel=lambda: state["cancel"])


def test_cancelled_run_leaves_incomplete_metadata(tmp_path):
    ds = scan_dataset(experiment(tmp_path / "src"))
    out = tmp_path / "out"
    with pytest.raises(JobCancelled):
        process_dataset(ds, small(), out, cancel=lambda: True)
    run = next(out.glob("run_*"))
    metadata = json.loads(next((run / "info").glob("*_metadata.json")).read_text(encoding="utf-8"))
    assert metadata["status"] == "cancelled"
    assert metadata["incomplete"] is True
    assert (run / "INCOMPLETE.txt").is_file()
    assert "CANCELLED" in (run / "info" / "processing_log.txt").read_text(encoding="utf-8")


def test_cancel_token_stops_a_run_midway(tmp_path):
    ds = scan_dataset(experiment(tmp_path / "src", serials=6))
    token = CancelToken()
    seen = []

    def progress(message: str) -> None:
        seen.append(message)
        if len(seen) > 3:
            token.cancel()

    with pytest.raises(JobCancelled):
        process_dataset(ds, small(), tmp_path / "out", progress, token)
    assert len(seen) >= 3


def test_batch_continues_after_a_failure_and_records_every_experiment(tmp_path):
    good = scan_dataset(experiment(tmp_path / "good"))
    broken_root = experiment(tmp_path / "broken")
    broken = scan_dataset(broken_root)
    tifffile.imwrite(broken.frames[0].path, np.zeros((40, 60), np.uint16))
    second_good = scan_dataset(experiment(tmp_path / "good2"))
    rows = process_batch([good, broken, second_good], small(), tmp_path / "out")
    assert [r["status"] for r in rows] == ["complete", "failed", "complete"]
    assert "8-bit" in rows[1]["error"]
    assert Path(rows[0]["output"]).is_dir() and Path(rows[2]["output"]).is_dir()
    assert all(r["experiment"] for r in rows)


def test_batch_stops_when_cancelled(tmp_path):
    datasets = [scan_dataset(experiment(tmp_path / name)) for name in ("a", "b", "c")]
    token = CancelToken()
    calls = []

    def progress(message: str) -> None:
        calls.append(message)
        token.cancel()

    rows = process_batch(datasets, small(), tmp_path / "out", progress, token)
    assert rows[-1]["status"] == "cancelled"
    assert len(rows) < 3 or rows[-1]["status"] == "cancelled"


def test_batch_accepts_per_dataset_options(tmp_path):
    a = scan_dataset(experiment(tmp_path / "a"))
    b = scan_dataset(experiment(tmp_path / "b"))
    rows = process_batch([a, b], {a.root: small(), b.root: small(montages=False)}, tmp_path / "out")
    assert [r["status"] for r in rows] == ["complete", "complete"]
    assert not list((Path(rows[1]["output"]) / "montages").glob("*")) if (Path(rows[1]["output"]) / "montages").is_dir() else True
    rows = process_batch([a, b], [small(), None], tmp_path / "out2")
    assert rows[1]["status"] == "failed"
