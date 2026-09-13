"""0.8: the queue of experiment folders (model, controller, page and window)."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("pytestqt")

from tests.fixtures.make_synthetic_dataset import make_dataset  # noqa: E402

LONG = 180000
#: small and quick: one composite MP4 at 320 px, nothing else
FAST = {"width": 320, "avi": False, "mp4": True, "per_channel": False, "fluorescence_only": False,
        "montages": False, "dashboard": False}


@pytest.fixture
def data_dir(monkeypatch):
    folder = Path(tempfile.mkdtemp(prefix="etaluma_queue_"))
    monkeypatch.setenv("ETALUMA_DATA_DIR", str(folder / "userdata"))
    return folder


def small(folder: Path, **kwargs) -> Path:
    return make_dataset(folder, positions=1, timepoints=3, size=200, **kwargs)


def test_parent_folder_gives_one_item_per_experiment_and_the_file_round_trips(data_dir):
    from etaluma_video.ui import queue_model as qm

    parent = data_dir / "batch"
    small(parent / "20260101_120000_a")
    small(parent / "20260102_120000_b")
    folders = qm.expand_folders([str(parent)])
    assert [Path(f).name for f in folders] == ["20260101_120000_a", "20260102_120000_b"]
    items = [qm.QueueItem(folder=f, preset="current", preset_snapshot=FAST) for f in folders]
    items[0].status = qm.RUNNING
    qm.save_queue(items)
    loaded = qm.load_queue()
    assert [i.id for i in loaded] == [i.id for i in items] and loaded[0].status == qm.RUNNING
    assert loaded[1].preset_snapshot == FAST


def test_partial_run_folders_are_labelled_incomplete(tmp_path):
    from etaluma_video.ui import queue_model as qm

    parent = tmp_path / "analysis_output"
    for name, status in (("run_20260101_000000_aaaaaa", "running"), ("run_20260101_000001_bbbbbb", "complete")):
        info = parent / name / "info"
        info.mkdir(parents=True)
        (info / "x_metadata.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    renamed = qm.label_incomplete(parent, time.time() - 60)
    assert [p.name for p in renamed] == ["run_20260101_000000_aaaaaa_incomplete"]
    assert (parent / "run_20260101_000001_bbbbbb").is_dir()


def test_queue_runs_in_order_continues_after_a_failure_and_writes_a_report(qtbot, data_dir):
    from etaluma_video.ui.queue import QueueController
    from etaluma_video.ui.settings import Settings

    settings = Settings(queue_output_mode="folder", queue_output_folder=str(data_dir / "out"))
    queue = QueueController(settings)
    a = small(data_dir / "20260101_120000_a")
    empty = data_dir / "not_an_experiment"
    empty.mkdir()
    b = small(data_dir / "20260102_120000_b")
    assert len(queue.add_folders([str(a), str(empty), str(b)], preset="current", preset_snapshot=FAST)) == 3
    reports = []
    queue.finished.connect(reports.append)
    assert queue.start()
    qtbot.waitUntil(lambda: not queue.running, timeout=LONG)
    assert [i.status for i in queue.items] == ["done", "failed", "done"]
    assert list((data_dir / "out" / a.name).glob("run_*/*.mp4"))
    assert list((data_dir / "out" / b.name).glob("run_*/*.mp4"))
    text = Path(reports[0]).read_text(encoding="utf-8")
    assert "2 done, 1 failed" in text and a.name in text


def test_skip_and_stop_now_keep_the_rest_waiting(qtbot, data_dir):
    from etaluma_video.ui import queue_model as qm
    from etaluma_video.ui.queue import QueueController
    from etaluma_video.ui.settings import Settings

    queue = QueueController(Settings())
    a, b = small(data_dir / "20260101_120000_a"), small(data_dir / "20260102_120000_b")
    queue.add_folders([str(a), str(b)], preset="current", preset_snapshot=FAST)
    assert queue.start()
    queue.skip_current()
    qtbot.waitUntil(lambda: not queue.running, timeout=LONG)
    assert [i.status for i in queue.items] == ["skipped", "done"]

    c = small(data_dir / "20260103_120000_c")
    queue.clear_finished()
    queue.add_folders([str(c)], preset="current", preset_snapshot=FAST)
    assert queue.start()
    queue.cancel(requeue=True)  # the app is closing
    qtbot.waitUntil(lambda: not queue.running, timeout=LONG)
    assert [i.status for i in queue.items] == ["pending"]
    again = QueueController(Settings())  # next start of the app
    assert len(again.unfinished()) == 1

    item = again.items[0]
    item.status, item.started_at, item.parent = qm.RUNNING, time.time() - 5, str(c / "analysis_output")
    assert again.recover() == 1 and again.items[0].status == qm.PENDING


def test_window_queue_page_toolbar_and_add_to_queue_while_running(qtbot, data_dir):
    from etaluma_video.ui.main_window import MainWindow
    from etaluma_video.ui.settings import Settings
    from etaluma_video.ui.state import AppContext
    from etaluma_video.ui.workers import wait_for_background

    window = MainWindow(AppContext(), Settings())
    qtbot.addWidget(window)
    window.show()
    try:
        root = small(data_dir / "20260101_120000_window")
        assert window.actions_api.open_folder(str(root))
        qtbot.waitUntil(lambda: window.ctx.active is not None, timeout=60000)
        qtbot.waitUntil(lambda: not window.runner.busy, timeout=60000)
        window.show_queue()
        assert window.central.currentWidget() is window.queue_page and window.toolbar.queue_action.isChecked()
        window.queue.state = "running"  # as if the queue were busy: the run buttons join it
        try:
            window._update_enabled()
            assert window.toolbar.quick_action.text() == "Quick → queue"
            assert window.actions_api.quick_video()
            assert window.actions_api.process()
            assert not window.actions_api.quick_video()  # the same item is already waiting
        finally:
            window.queue.state = "idle"
        assert [(i.display, i.preset) for i in window.queue.items] == [("auto", "builtin:quick"),
                                                                        ("current", "builtin:standard")]
        assert window.queue.items[1].display_snapshot is not None
        window._on_queue_changed()
        assert window.toolbar.queue_action.text() == "Queue (2)"
        window.queue_page.refresh()
        assert window.queue_page.tree.topLevelItemCount() == 2
        window.show_queue()
        assert window.central.currentWidget() is window.viewer and not window.toolbar.queue_action.isChecked()
    finally:
        window.viewer.controller.cancel_all()
        qtbot.waitUntil(lambda: not window.runner.busy, timeout=60000)
        wait_for_background(20000)
