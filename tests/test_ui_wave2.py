"""Explorer, live viewer, display panel, calibration card and fixed-mode controls (pytest-qt, offscreen).

Every test opens a small synthetic Etaluma experiment (tests/fixtures/make_synthetic_dataset.py),
so the suite needs no private data.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("pytestqt")

from tests.fixtures.make_synthetic_dataset import make_dataset  # noqa: E402

LONG = 60000


@pytest.fixture
def data_dir(monkeypatch):
    folder = Path(tempfile.mkdtemp(prefix="etaluma_ui2_"))
    monkeypatch.setenv("ETALUMA_DATA_DIR", str(folder / "userdata"))
    return folder


@pytest.fixture
def window(qtbot, data_dir):
    from etaluma_video.ui.main_window import MainWindow
    from etaluma_video.ui.settings import Settings
    from etaluma_video.ui.state import AppContext
    from etaluma_video.ui.workers import wait_for_background

    w = MainWindow(AppContext(), Settings())
    qtbot.addWidget(w)
    w.resize(1400, 900)
    w.show()
    yield w
    w.viewer.controller.cancel_all()
    qtbot.waitUntil(lambda: not w.runner.busy, timeout=LONG)
    wait_for_background(20000)


def open_synthetic(qtbot, window, data_dir, name="20260101_120000_synthetic", **kwargs):
    root = make_dataset(data_dir / name, **kwargs)
    assert window.actions_api.open_folder(str(root))
    qtbot.waitUntil(lambda: window.ctx.active is not None, timeout=LONG)
    return root


def wait_preview(qtbot, window):
    qtbot.waitUntil(lambda: window.viewer.last_image() is not None, timeout=LONG)
    qtbot.waitUntil(lambda: window.ctx.histograms is not None and bool(window.ctx.auto_bounds), timeout=LONG)


def test_open_fills_explorer_viewer_and_auto_bounds(qtbot, window, data_dir):
    open_synthetic(qtbot, window, data_dir)
    wait_preview(qtbot, window)
    tree = window.explorer.tree
    assert tree.topLevelItemCount() == 1
    top = tree.topLevelItem(0)
    labels = [top.child(i).text(0) for i in range(top.childCount())]
    assert sum(1 for label in labels if label.startswith("ROI-")) == 3
    assert "Files on disk" in labels
    assert {"F2", "F3", "WHITE"} <= set(window.ctx.auto_bounds)
    image = window.viewer.last_image()
    assert image.ndim == 3 and image.shape[2] == 3 and image.max() > 0
    stack = window.viewer.controller.current()
    assert stack is not None and stack.n == 6


def test_calibration_card_reads_the_synthetic_bar(qtbot, window, data_dir):
    open_synthetic(qtbot, window, data_dir)
    qtbot.waitUntil(lambda: window.ctx.calibration.source == "burned-in scale bar", timeout=LONG)
    cal = window.ctx.calibration
    assert cal.objective == "10x"
    assert abs(cal.pixel_size_um - 0.826) < 0.02
    assert not cal.disagreement
    assert "µm/px" in window.explorer.card.result.text()


def test_manual_handles_change_the_image_and_are_restored(qtbot, window, data_dir):
    open_synthetic(qtbot, window, data_dir)
    wait_preview(qtbot, window)
    # every position is measured in the background (0.6): let it finish so the Auto bounds hold still
    qtbot.waitUntil(lambda: bool(window.ctx.histograms is not None and window.ctx.histograms.complete), timeout=LONG)
    panel = window.display_panel
    panel.manual_button.click()
    assert window.ctx.profile.mode == "manual"
    seeded = window.ctx.profile.channels["F2"]
    auto_low, auto_high = window.ctx.auto_bounds["F2"]
    assert (seeded.low, seeded.high) == pytest.approx((auto_low, auto_high))
    qtbot.waitUntil(lambda: window.viewer.last_image() is not None, timeout=LONG)
    window.viewer.set_view("F2")
    qtbot.wait(50)
    before = window.viewer.last_image().copy()
    panel._rows["F2"].hist._set_and_emit(0.0, 20.0)  # what a drag of both handles emits
    assert window.ctx.profile.channels["F2"].high == pytest.approx(20.0)
    qtbot.waitUntil(lambda: not np.array_equal(window.viewer.last_image(), before), timeout=5000)
    assert panel._rows["F2"].high.value() == pytest.approx(20.0)
    # remembered as the last manual profile and restored for the next experiment
    panel._persist()
    assert '"mode": "manual"' in window.settings.last_profile_json
    window.ctx.set_active(window.ctx.active)
    assert window.ctx.profile.mode == "manual"
    assert window.ctx.profile.channels["F2"].high == pytest.approx(20.0)


def test_auto_method_and_rolling_ball_update_bounds(qtbot, window, data_dir):
    open_synthetic(qtbot, window, data_dir)
    wait_preview(qtbot, window)
    panel = window.display_panel
    panel.method.setCurrentIndex(panel.method.findData("classic"))
    assert window.ctx.profile.auto_method == "classic"
    qtbot.waitUntil(lambda: window.ctx.auto_bounds["F2"][0] == 0.0, timeout=5000)
    panel.rolling.setChecked(True)
    assert window.ctx.profile.rolling_ball.enabled
    qtbot.waitUntil(lambda: window.ctx.histograms is not None
                    and window.ctx.histograms.rolling_ball_radius == window.ctx.profile.rolling_ball.radius_px,
                    timeout=LONG)


def test_measure_all_positions_covers_every_position(qtbot, window, data_dir):
    open_synthetic(qtbot, window, data_dir)
    wait_preview(qtbot, window)
    window.ctx.compute_all_requested.emit()
    qtbot.waitUntil(lambda: window.ctx.histograms is not None and window.ctx.histograms.complete, timeout=LONG)
    assert "3 of 3" in window.display_panel.coverage_label.text()


def test_explorer_click_switches_position(qtbot, window, data_dir):
    open_synthetic(qtbot, window, data_dir)
    wait_preview(qtbot, window)
    top = window.explorer.tree.topLevelItem(0)
    second = next(top.child(i) for i in range(top.childCount()) if top.child(i).text(0).startswith("ROI-2"))
    window.explorer.tree.setCurrentItem(second)
    roi = second.data(0, 256)[2]
    assert window.ctx.position == roi
    qtbot.waitUntil(lambda: window.viewer.controller.current() is not None
                    and window.viewer.controller.current().roi == roi
                    and window.viewer.controller.current().complete, timeout=LONG)


def test_timeline_and_playback(qtbot, window, data_dir):
    open_synthetic(qtbot, window, data_dir)
    wait_preview(qtbot, window)
    qtbot.waitUntil(lambda: window.viewer.controller.current().complete, timeout=LONG)
    window.viewer.slider.setValue(5)
    qtbot.wait(50)
    assert "t 6/6" in window.viewer.time_label.text()
    window.viewer.play_button.setChecked(True)
    qtbot.waitUntil(lambda: window.viewer.slider.value() != 5, timeout=5000)
    window.viewer.play_button.setChecked(False)


def test_histogram_widget_drag_emits_bounds(qtbot):
    from PySide6.QtCore import QPoint, Qt

    from etaluma_video.ui.histogram_widget import HistogramWidget

    w = HistogramWidget()
    qtbot.addWidget(w)
    w.resize(300, 50)
    w.show()
    w.set_histogram(np.arange(256))
    w.set_bounds(0, 255)
    seen = []
    w.bounds_changed.connect(lambda lo, hi: seen.append((lo, hi)))
    right = int(w._x(255))
    qtbot.mousePress(w, Qt.MouseButton.LeftButton, pos=QPoint(right, 25))
    qtbot.mouseMove(w, QPoint(int(w._x(128)), 25))
    qtbot.mouseRelease(w, Qt.MouseButton.LeftButton, pos=QPoint(int(w._x(128)), 25))
    assert seen and seen[-1][1] < 140
    w.set_editable(False)
    seen.clear()
    qtbot.mousePress(w, Qt.MouseButton.LeftButton, pos=QPoint(int(w._x(10)), 25))
    assert not seen


def test_fixed_mode_shows_measurement_controls(qtbot, window, data_dir):
    open_synthetic(qtbot, window, data_dir, name="20260101_130000_fixed", fixed=True)
    qtbot.waitUntil(lambda: window.ctx.mode == "fixed", timeout=LONG)
    panel = window.output_panel
    assert panel.fixed_box.isVisible()
    panel.threshold_spin.setValue(70)
    options = panel.options_for(window.ctx.active)
    assert options.mode == "fixed"
    assert options.threshold == pytest.approx(70.0)
    window.actions_api.set_mode("timelapse")
    assert not panel.fixed_box.isVisible()


def test_quick_video_from_the_window(qtbot, window, data_dir):
    root = open_synthetic(qtbot, window, data_dir)
    qtbot.waitUntil(lambda: not window.runner.busy, timeout=LONG)
    assert window.actions_api.quick_video()
    qtbot.waitUntil(lambda: not window.runner.busy, timeout=180000)
    assert window.ctx.job_status == "finished"
    runs = sorted((root / "analysis_output").glob("quick_*"))
    assert runs, "no quick_* folder written"
    videos = sorted(p.name for p in runs[-1].glob("*.mp4"))  # 0.6: videos at the top of the run folder
    assert any("composite" in v for v in videos)
    assert (runs[-1] / "info" / "display_profile.json").is_file()
    assert (runs[-1] / "info" / "calibration.json").is_file()


# --------------------------------------------------------------------------- #
# 0.5: histogram views, single-channel video choice, results page, layout
# --------------------------------------------------------------------------- #


def test_frame_histogram_and_view_controls(qtbot, window, data_dir):
    open_synthetic(qtbot, window, data_dir)
    wait_preview(qtbot, window)
    panel = window.display_panel
    row = panel._rows["F2"]
    qtbot.waitUntil(lambda: row.hist._frame_counts is not None, timeout=LONG)
    assert int(row.hist._frame_counts.sum()) > 0
    panel.hist_view.setCurrentIndex(panel.hist_view.findData("frame"))
    assert row.hist.view() == "frame"
    panel.hist_log.setChecked(False)
    assert not row.hist.is_log()
    assert window.settings.extra["histogram_view"] == "frame"
    qtbot.waitUntil(lambda: window.viewer.controller.current().complete, timeout=LONG)
    before = row.hist._frame_counts.copy()
    window.viewer.slider.setValue(5)
    qtbot.waitUntil(lambda: not np.array_equal(row.hist._frame_counts, before), timeout=5000)


def test_single_channel_video_checkboxes(qtbot, window, data_dir):
    open_synthetic(qtbot, window, data_dir)
    panel = window.output_panel
    qtbot.waitUntil(lambda: set(panel.channel_checks) == {"WHITE", "F2", "F3"}, timeout=LONG)
    assert panel.selected_channel_videos() == ["WHITE", "F2", "F3"]  # 0.8: every channel until a choice is saved
    for check in panel.channel_checks.values():
        check.setChecked(False)
    assert panel.selected_channel_videos() == []
    opts = panel.options_for(window.ctx.active)
    assert opts.channel_videos == [] and opts.composite_videos and opts.fluorescence_only_video
    panel.channel_checks["F2"].setChecked(True)
    assert panel.options_for(window.ctx.active).channel_videos == ["F2"]
    assert window.settings.extra["channel_videos"] == ["F2"]
    panel.fluor_check.setChecked(False)
    quick = panel.options_for(window.ctx.active, quick=True)
    assert quick.channel_videos == ["F2"] and quick.width == 1900 and not quick.avi
    assert not quick.fluorescence_only_video
    qtbot.waitUntil(lambda: not window.runner.busy, timeout=LONG)  # never close the window mid-job


def test_log_sits_under_the_preview_only(qtbot, window):
    from PySide6.QtCore import Qt

    # 0.6: the explorer and Display & output both keep the full height
    assert window.corner(Qt.Corner.BottomLeftCorner) == Qt.DockWidgetArea.LeftDockWidgetArea
    assert window.corner(Qt.Corner.BottomRightCorner) == Qt.DockWidgetArea.RightDockWidgetArea


def test_panels_collapse_to_strips_and_the_log_saves(qtbot, window, tmp_path):
    from etaluma_video.ui.panels import StripButton

    window.show()
    qtbot.waitExposed(window)
    window.panels.set_open("explorer", False)
    assert window.explorer_dock.isHidden() and not window.panels.strips["explorer"].isHidden()
    window.panels.strips["explorer"].findChild(StripButton).click()
    assert not window.explorer_dock.isHidden() and window.panels.strips["explorer"].isHidden()
    window.log_dock.collapse_button.click()
    assert window.log_dock.isHidden() and not window.panels.actions["log"].isChecked()
    window.panels.actions["log"].trigger()
    assert not window.log_dock.isHidden()
    # 0.5's Save passed Qt's "checked" flag as the file name and crashed
    window.log_dock.save_button.click()
    saved = window.log_dock.save_to_file()
    assert saved is not None and saved.is_file() and saved.parent.name == "logs"
    target = window.log_dock.save_to_file(tmp_path / "chosen.txt")
    assert target.read_text(encoding="utf-8") == window.log_dock.text()


def test_results_page_shows_large_tiles_and_plays(qtbot, window, data_dir):
    open_synthetic(qtbot, window, data_dir)
    qtbot.waitUntil(lambda: not window.runner.busy and bool(window.output_panel.channel_checks), timeout=LONG)
    window.output_panel.channel_checks["F2"].setChecked(True)
    assert window.actions_api.quick_video()
    qtbot.waitUntil(lambda: not window.runner.busy, timeout=180000)
    window.show_results()
    view = window.results_view
    assert window.central.currentWidget() is view
    qtbot.waitUntil(lambda: len(view.tiles()) > 0, timeout=LONG)
    kinds = {t.item.kind for t in view.tiles()}
    assert {"composite", "fluorescence", "F2"} <= kinds
    assert "montage" not in kinds  # 0.6: hidden until its chip is ticked
    assert not window.panels.is_open("right") and not window.panels.is_open("log")
    qtbot.waitUntil(lambda: all(not t.icon().isNull() for t in view.tiles()), timeout=LONG)
    next(t for t in view.tiles() if t.item.kind == "composite").click()
    assert view.pages.currentIndex() == 2
    view.show_grid()
    window.show_results()
    assert window.central.currentWidget() is window.viewer
    assert window.panels.is_open("right") and window.panels.is_open("log")
    view._hidden.discard("montage")
    view._rebuild_grid()
    assert "montage" in {t.item.kind for t in view.tiles()}


# --------------------------------------------------------------------------- #
# 0.7: stability under quick switching
# --------------------------------------------------------------------------- #


def test_switching_experiments_and_positions_quickly_is_safe(qtbot, window, data_dir):
    roots = [make_dataset(data_dir / f"2026010{i}_120000_synthetic") for i in (1, 2)]
    for _ in range(3):
        for root in roots:
            window.actions_api.open_folder(str(root))  # refused while a scan runs: that is fine
            qtbot.wait(20)
    qtbot.waitUntil(lambda: not window.runner.busy, timeout=LONG)
    assert window.actions_api.open_folder(str(roots[1]))
    qtbot.waitUntil(lambda: bool(window.ctx.active is not None and Path(window.ctx.active.root).name == roots[1].name),
                    timeout=LONG)
    positions = list(window.ctx.active.groups)
    for roi in positions * 3:
        window.ctx.set_position(roi)
        qtbot.wait(10)
    qtbot.waitUntil(lambda: window.viewer.last_image() is not None, timeout=LONG)
    qtbot.waitUntil(lambda: not window.runner.busy, timeout=LONG)
    assert "Unhandled exception" not in window.log_dock.text()


def test_each_experiment_remembers_its_settings(qtbot, window, data_dir):
    first = open_synthetic(qtbot, window, data_dir)
    wait_preview(qtbot, window)
    qtbot.waitUntil(lambda: bool(window.output_panel.name_edits) and not window.runner.busy, timeout=LONG)
    window.output_panel.name_edits["F2"].setText("CFDA")
    window.output_panel.first_spin.setValue(2)
    window.display_panel.manual_button.click()
    window.display_panel._rows["F2"].hist._set_and_emit(0.0, 20.0)
    assert window.ctx.channel_names == {"F2": "CFDA"}
    window.experiment_memory.flush()
    second = make_dataset(data_dir / "20260102_120000_synthetic")
    assert window.actions_api.open_folder(str(second))
    qtbot.waitUntil(lambda: bool(window.ctx.active is not None and Path(window.ctx.active.root).name == second.name),
                    timeout=LONG)
    qtbot.waitUntil(lambda: bool("F2" in window.output_panel.name_edits
                                 and window.output_panel.name_edits["F2"].text() == ""), timeout=LONG)
    qtbot.waitUntil(lambda: not window.runner.busy, timeout=LONG)
    assert window.actions_api.open_folder(str(first))
    qtbot.waitUntil(lambda: bool(window.ctx.active is not None and Path(window.ctx.active.root).name == first.name),
                    timeout=LONG)
    qtbot.waitUntil(lambda: bool("F2" in window.output_panel.name_edits
                                 and window.output_panel.name_edits["F2"].text() == "CFDA"), timeout=LONG)
    assert window.output_panel.timepoint_range() == [2, 6]
    assert window.ctx.profile.mode == "manual" and window.ctx.profile.channels["F2"].high == pytest.approx(20.0)
    assert window.ctx.channel_names.get("F2") == "CFDA"
