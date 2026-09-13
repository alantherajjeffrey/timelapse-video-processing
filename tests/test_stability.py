"""0.7 stability: unreadable files, output folders that cannot be made, a full disk, the crash window, memory."""
from __future__ import annotations

import errno
from pathlib import Path

import pytest

from etaluma_video.engine import Options, process_dataset, scan_dataset
from etaluma_video.engine import fastexport
from tests.fixtures.make_synthetic_dataset import make_dataset


def small() -> Options:
    return Options(width=160, tile_width=160, objective="10x", avi=False, dashboard=False)


def test_an_unreadable_tiff_does_not_end_the_run(tmp_path):
    root = make_dataset(tmp_path / "20260101_120000_synthetic")
    victim = next(f for f in scan_dataset(root).frames if f.channel == "F2" and f.serial == 3)
    Path(victim.path).write_bytes(b"this is not a TIFF")
    ds = scan_dataset(root)  # the file names are unchanged; only the content is broken
    result = process_dataset(ds, small(), tmp_path / "out", progress=lambda m: None)
    assert result["status"] == "complete"
    assert any("could not be read" in w and "black frame" in w for w in result["warnings"])


def test_an_output_folder_that_cannot_be_created_is_explained(tmp_path):
    ds = scan_dataset(make_dataset(tmp_path / "20260101_120000_synthetic"))
    blocker = tmp_path / "a file, not a folder.txt"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="Cannot create the output folder"):
        process_dataset(ds, small(), blocker / "out", progress=lambda m: None)


def test_a_full_disk_is_explained(tmp_path, monkeypatch):
    ds = scan_dataset(make_dataset(tmp_path / "20260101_120000_synthetic"))

    def full(self, rgb):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(fastexport._Mp4Pipe, "write", full)
    with pytest.raises(ValueError, match="is full"):
        process_dataset(ds, small(), tmp_path / "out", progress=lambda m: None)
    run = next((tmp_path / "out").glob("run_*"))
    assert (run / "INCOMPLETE.txt").is_file()


def test_crash_window_shows_and_copies_a_report(qtbot):
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QWidget

    from etaluma_video.ui import crash_dialog

    host = QWidget()
    qtbot.addWidget(host)
    reporter = crash_dialog.install(host)
    try:
        try:
            raise RuntimeError("boom for the test")
        except RuntimeError as exc:
            crash_dialog.report_exception(type(exc), exc, exc.__traceback__)
        qtbot.waitUntil(lambda: bool(reporter.dialog is not None and reporter.dialog.isVisible()), timeout=3000)
        report = reporter.dialog.report_text()
        assert "boom for the test" in report and __import__("etaluma_video").VERSION in report and "Traceback" in report
        reporter.dialog.copy_report()
        assert "boom for the test" in QGuiApplication.clipboard().text()
        reporter.dialog.close()
    finally:
        crash_dialog.uninstall()


def test_preview_memory_budget_shrinks_a_large_stack(monkeypatch):
    from etaluma_video.ui import preview_cache

    monkeypatch.setenv("ETALUMA_PREVIEW_BUDGET_MB", "100")
    assert preview_cache.preview_budget_bytes() == 100_000_000
    h, w = preview_cache.fit_to_budget(1900, 1900, 640, timepoints=196, channels=3, budget=50_000_000)
    assert w < 640 and 196 * 3 * h * w <= 50_000_000
    assert preview_cache.fit_to_budget(1900, 1900, 640, 10, 3, 50_000_000) == (640, 640)
    assert preview_cache.total_memory_bytes() > 0
