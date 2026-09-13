"""Smoke tests for the UI shell (pytest-qt, QT_QPA_PLATFORM=offscreen).

These cover what the shell owns: the window and its docks, the log path
(logging record -> dock -> AppContext.log_line), the job runner (progress,
finish, cancel), theme switching and the output panel's Options building.
Engine-dependent assertions skip while the engine port is unfinished.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QDockWidget  # noqa: E402

from etaluma_video.engine.jobs import CancelToken, JobCancelled  # noqa: E402
from etaluma_video.ui.jobs_qt import JobRunner, parse_percent  # noqa: E402
from etaluma_video.ui.main_window import MainWindow  # noqa: E402
from etaluma_video.ui.output_panel import OutputPanel, human_bytes, local_estimate_bytes  # noqa: E402
from etaluma_video.ui.results_dock import categorise, find_runs  # noqa: E402
from etaluma_video.ui.settings import Settings, user_data_dir  # noqa: E402
from etaluma_video.ui.state import AppContext  # noqa: E402
from etaluma_video.ui.theme import THEMES, apply_theme  # noqa: E402

SAMPLE_EXPERIMENT = "20260707_221520-verify if power"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """Redirect %LOCALAPPDATA%\\Timelapse Video Processing into a temp folder."""
    monkeypatch.setenv("ETALUMA_DATA_DIR", str(tmp_path / "userdata"))
    return user_data_dir()


@pytest.fixture()
def ctx(qtbot):
    return AppContext()


@pytest.fixture()
def window(qtbot, ctx, data_dir):
    settings = Settings.load()
    win = MainWindow(ctx, settings)
    qtbot.addWidget(win)
    yield win
    win.runner.cancel()  # no worker thread may outlive the test
    win.runner.wait(20000)
    win.log_dock.detach()


def fake_dataset(root: Path, positions=2, channels=("WHITE", "F2"), timepoints=4):
    """A real engine Dataset whose image files do not exist (widgets must cope without pixels)."""
    from etaluma_video.engine.parsing import Dataset, Frame

    frames = [
        Frame(path=str(root / f"{roi}_{channel}_{t:06d}.tif"), roi=roi, order=i + 1, id=f"{i + 1}s1a",
              descriptor="", channel=channel, serial=t)
        for i, roi in enumerate([f"ROI-{i + 1}s1a" for i in range(positions)])
        for channel in channels
        for t in range(timepoints)
    ]
    return Dataset(root=str(root), layout="roi_subfolders", mode="timelapse", frames=frames,
                   protocols=[], avs=[], positions=[], warnings=[], source_fingerprint="fake")


def engine_symbol(name: str):
    """The engine facade symbol, or None while the engine port is unfinished."""
    try:
        from etaluma_video import engine
    except Exception:
        return None
    return getattr(engine, name, None)


# --------------------------------------------------------------------------- #
# window
# --------------------------------------------------------------------------- #


def test_window_constructs_with_every_dock(window):
    names = {d.objectName() for d in window.findChildren(QDockWidget)}
    assert {"explorer_dock", "right_dock", "log_dock"} <= names
    assert window.central.currentWidget() is window.welcome  # 0.7: welcome page until a folder is open
    assert window.toolbar.objectName() == "main_toolbar"
    # Quick video and Process need a dataset; Cancel needs a running job.
    assert not window.toolbar.quick_action.isEnabled()
    assert not window.toolbar.process_action.isEnabled()
    assert not window.toolbar.cancel_action.isEnabled()
    assert window.toolbar.open_action.isEnabled()
    view_items = {a.text() for a in window.view_menu.actions()}
    assert "Reset layout" in view_items


def test_actions_enable_with_a_dataset(window, ctx, tmp_path):
    ctx.set_datasets([fake_dataset(tmp_path / "20260101_000000_demo")])
    assert window.toolbar.quick_action.isEnabled()
    assert window.toolbar.process_action.isEnabled()
    ctx.set_job("running", "Processing…")
    assert not window.toolbar.quick_action.isEnabled()
    assert window.toolbar.cancel_action.isEnabled()
    assert not window.progress_bar.isHidden()  # the window itself is never shown in tests
    ctx.set_job("finished", "Processing finished")
    assert window.toolbar.quick_action.isEnabled()
    assert not window.toolbar.cancel_action.isEnabled()


def test_mode_toggle_round_trip(window, ctx):
    window.actions_api.set_mode("fixed")
    assert ctx.mode == "fixed"
    assert window.toolbar.fixed_action.isChecked()
    window.toolbar.timelapse_action.trigger()
    assert ctx.mode == "timelapse"


def test_reset_layout_and_dock_toggles(window):
    window.log_dock.setVisible(False)
    assert window.log_dock.isHidden()
    window.reset_layout()
    assert not window.log_dock.isHidden()


def test_close_during_a_job_can_keep_the_window_open(window, monkeypatch, qtbot):
    started = threading.Event()
    release = threading.Event()

    def slow(progress=None, cancel=None):
        started.set()
        release.wait(5)
        return "done"

    window.show()  # the question is only asked when the window is on screen
    window.runner.start("process", slow)
    assert started.wait(5)
    answers = ["keep"]
    monkeypatch.setattr(window, "ask_close_during_job", lambda: answers.pop(0))
    assert window.close() is False  # the close event was ignored, the window stays
    assert not window.isHidden()
    release.set()
    with qtbot.waitSignal(window.runner.finished, timeout=5000):
        pass
    assert window.close() is True  # nothing running any more


def test_close_during_a_job_can_cancel_and_close(window, monkeypatch, qtbot):
    started = threading.Event()

    def slow(progress=None, cancel=None):
        started.set()
        while not (cancel is not None and cancel()):
            time.sleep(0.01)
        raise JobCancelled()

    window.show()
    window.runner.start("process", slow)
    assert started.wait(5)
    monkeypatch.setattr(window, "ask_close_during_job", lambda: "cancel")
    with qtbot.waitSignal(window.runner.cancelled, timeout=5000):
        window.close()
    qtbot.waitUntil(lambda: window.isHidden(), timeout=5000)  # closes itself once the job stopped


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #


def test_log_record_reaches_the_dock_and_the_context(window, ctx, qtbot):
    seen: list[tuple[str, str]] = []
    ctx.log_line.connect(lambda level, text: seen.append((level, text)))
    with qtbot.waitSignal(ctx.log_line, timeout=3000):
        logging.getLogger("etaluma.ui").info("scan_dataset(unit test) worked")
    assert any("scan_dataset(unit test) worked" in text for _level, text in seen)
    assert "scan_dataset(unit test) worked" in window.log_dock.text()


def test_normal_hides_debug_and_debug_shows_it(window, ctx, qtbot):
    window.log_dock.set_debug_visible(False)
    with qtbot.waitSignal(ctx.log_line, timeout=3000):
        logging.getLogger("etaluma.ui").debug("open_folder(path='x')")
    assert "open_folder(path='x')" not in window.log_dock.text()
    window.log_dock.set_debug_visible(True)
    assert "open_folder(path='x')" in window.log_dock.text()


def test_log_save_writes_a_file(window, ctx, qtbot, tmp_path):
    with qtbot.waitSignal(ctx.log_line, timeout=3000):
        logging.getLogger("etaluma.ui").warning("a warning line")
    target = window.log_dock.save_to_file(tmp_path / "log.txt")
    assert target is not None and "a warning line" in target.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# job runner
# --------------------------------------------------------------------------- #


def test_job_runner_reports_progress_and_finishes(qtbot):
    runner = JobRunner()
    events: list[str] = []
    runner.started.connect(lambda kind: events.append(f"started:{kind}"))
    runner.finished.connect(lambda kind, result: events.append(f"finished:{kind}:{result}"))
    percents: list[int] = []
    runner.percent.connect(percents.append)
    messages: list[str] = []
    runner.progress.connect(messages.append)

    def work(progress=None, cancel=None):
        for step in (0, 50, 100):
            progress(f"step {step} … {step} %")
        return "result"

    with qtbot.waitSignal(runner.finished, timeout=5000):
        runner.start("dummy", work)
    assert events[0] == "started:dummy"
    assert events[-1] == "finished:dummy:result"
    assert any("step 50" in m for m in messages)
    assert 50 in percents and percents[0] == -1
    assert not runner.busy


def test_job_runner_honours_cancel(qtbot):
    runner = JobRunner()
    order: list[str] = []
    runner.finished.connect(lambda *_: order.append("finished"))
    runner.cancelled.connect(lambda kind: order.append(f"cancelled:{kind}"))
    running = threading.Event()

    def work(progress=None, cancel=None):
        running.set()
        while True:
            if cancel is not None and cancel():
                raise JobCancelled()
            time.sleep(0.01)

    runner.start("slow", work)
    assert running.wait(5)
    with qtbot.waitSignal(runner.cancelled, timeout=5000):
        assert runner.cancel()
    assert order == ["cancelled:slow"]
    assert not runner.busy


def test_job_runner_refuses_a_second_job(qtbot):
    runner = JobRunner()
    release = threading.Event()

    def work(progress=None, cancel=None):
        release.wait(5)

    runner.start("first", work)
    with pytest.raises(RuntimeError):
        runner.start("second", work)
    release.set()
    with qtbot.waitSignal(runner.finished, timeout=5000):
        pass


def test_job_runner_reports_failures(qtbot):
    runner = JobRunner()

    def work(progress=None, cancel=None):
        raise ValueError("engine exploded")

    with qtbot.waitSignal(runner.failed, timeout=5000) as blocker:
        runner.start("boom", work)
    assert blocker.args[0] == "boom"
    assert "engine exploded" in blocker.args[1]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("12 %", 12), ("done 99.5 %", 99), ("no percent here", None), ("step 100%", 100)],
)
def test_parse_percent(text, expected):
    assert parse_percent(text) == expected


def test_cancel_token_protocol():
    token = CancelToken()
    assert not token()
    token.cancel()
    assert token() and token.cancelled


# --------------------------------------------------------------------------- #
# theme, settings, output panel, results
# --------------------------------------------------------------------------- #


def test_theme_switching_does_not_crash(window, qapp, ctx):
    for name in THEMES + ("dark",):
        assert apply_theme(qapp, name) == name
        window.actions_api.set_theme(name)
        assert window.settings.theme == name


def test_settings_round_trip(data_dir, tmp_path):
    settings = Settings.load()
    settings.theme = "light"
    settings.default_width = 1900
    path = settings.save()
    assert path.is_file()
    again = Settings.load()
    assert again.theme == "light" and again.default_width == 1900


def test_output_panel_values_and_estimate(qtbot, ctx, tmp_path, data_dir):
    panel = OutputPanel(ctx, settings=Settings.load())
    qtbot.addWidget(panel)
    dataset = fake_dataset(tmp_path / "20260101_000000_demo")
    ctx.set_datasets([dataset])
    values = panel.values()
    assert values["width"] in (640, 950, 1900)
    assert panel.output_folder(dataset) == Path(dataset.root) / "analysis_output"
    estimate, low, high = local_estimate_bytes(dataset, values)
    assert estimate > 0 and low < estimate < high
    assert panel.estimate_text(dataset).startswith("Estimated output")
    assert human_bytes(1536) == "1.5 kB"


def test_output_panel_keeps_app_overlays_with_lumaview_overlays(qtbot, ctx, data_dir):
    from etaluma_video.engine.models import Box, Calibration, Overlays

    panel = OutputPanel(ctx, settings=Settings.load())
    qtbot.addWidget(panel)
    overlays = Overlays(image_shape=(1900, 1900), bar_box=Box(1810, 1820, 1600, 1750))
    ctx.set_calibration(Calibration(), overlays)
    # 0.5 greyed these out whenever Lumaview's overlays were found, which is always
    assert panel.scale_bar_check.isEnabled() and panel.timestamp_check.isEnabled()
    assert "covers Lumaview" in panel.scale_bar_check.toolTip()
    panel.time_offset_edit.setText("Day 2 : 06:00:00")
    assert panel.values()["time_offset_seconds"] == 2 * 86400 + 6 * 3600
    panel.time_offset_edit.setText("nonsense")
    assert panel.values()["time_offset_seconds"] == 2 * 86400 + 6 * 3600  # the last valid value stays
    panel.quality_combo.setCurrentIndex(panel.quality_combo.findData("small"))
    assert panel.values()["video_quality"] == "small"


def test_output_panel_options_for(qtbot, ctx, tmp_path, data_dir):
    panel = OutputPanel(ctx, settings=Settings.load())
    qtbot.addWidget(panel)
    dataset = fake_dataset(tmp_path / "20260101_000000_demo")
    ctx.set_datasets([dataset])
    options_class = engine_symbol("Options")
    if options_class is None:
        pytest.skip("the engine facade does not export Options yet (engine port in progress)")
    options = panel.options_for(dataset, output=tmp_path / "out")
    assert isinstance(options, options_class)
    width = getattr(options, "width", None) or getattr(options, "video_width", None)
    assert width == panel.width


def test_results_helpers(tmp_path):
    run = tmp_path / "analysis_output" / "run_20260101_000000_abc123"
    run.mkdir(parents=True)
    (run / "demo_metadata.json").write_text('{"status": "complete", "processing_seconds": 12.5}', encoding="utf-8")
    (run / "videos").mkdir()
    (run / "videos" / "composite.mp4").write_bytes(b"")
    assert find_runs(tmp_path) == [run]
    assert categorise(run / "videos" / "composite.mp4") == "Videos"
    assert categorise(run / "demo_metadata.json") == "Logs"


def test_results_dock_lists_runs(window, ctx, tmp_path):
    dataset = fake_dataset(tmp_path / "20260101_000000_demo")
    Path(dataset.root).mkdir(parents=True, exist_ok=True)
    run = Path(dataset.root) / "analysis_output" / "quick_20260101_000000_abc123"
    run.mkdir(parents=True)
    (run / "demo_metadata.json").write_text('{"status": "complete"}', encoding="utf-8")
    ctx.set_datasets([dataset])
    window.actions_api.refresh_results()
    assert window.results_view.run_combo.count() == 1
    assert "quick" in window.results_view.run_combo.itemText(0)
    window.show_results()
    assert window.central.currentWidget() is window.results_view
    window.show_results()
    assert window.central.currentWidget() is window.viewer


# --------------------------------------------------------------------------- #
# end to end against the real engine, when it is ready
# --------------------------------------------------------------------------- #


def samples_root() -> Path | None:
    """Same rule as tests/conftest.py: ETALUMA_SAMPLES, else ../private/Sample set."""
    default = Path(__file__).resolve().parent.parent.parent / "private" / "Sample set"
    path = Path(os.environ.get("ETALUMA_SAMPLES", str(default)))
    return path if path.is_dir() else None


@pytest.mark.skipif(engine_symbol("scan_dataset") is None, reason="the engine facade does not export scan_dataset yet")
def test_open_folder_on_a_real_sample(window, ctx, qtbot):
    samples = samples_root()
    if samples is None or not (samples / SAMPLE_EXPERIMENT).is_dir():
        pytest.skip(f"sample {SAMPLE_EXPERIMENT} not available")
    with qtbot.waitSignal(ctx.datasets_changed, timeout=180000):
        assert window.actions_api.open_folder(samples / SAMPLE_EXPERIMENT)
    assert ctx.datasets and ctx.active is not None
    assert len(ctx.active.frames) > 0
    # Open folder chains a calibration job; wait for that one too, then check the shell state.
    with qtbot.waitSignal(ctx.calibration_changed, timeout=180000):
        pass
    qtbot.waitUntil(lambda: not window.runner.busy, timeout=180000)
    assert window.toolbar.quick_action.isEnabled()
    assert ctx.calibration is not None
    print("\ncalibration:", ctx.calibration.message, "| overlays detected:", ctx.overlays.detected)
    print("white preset:", ctx.profile.white.preset)
    options = window.output_panel.options_for(ctx.active, output=Path(ctx.active.root) / "analysis_output")
    quick = window.output_panel.options_for(ctx.active, output=Path(ctx.active.root) / "analysis_output", quick=True)
    assert quick.width == 1900 and quick.mp4 and not quick.avi and getattr(quick, "quick", True)
    validate = engine_symbol("validate_options")
    if validate is not None:  # the engine must accept what the panel builds
        validate(ctx.active, options)
        validate(ctx.active, quick)
    print("mode:", ctx.mode, "| options width:", options.width)
    print("estimate:", window.output_panel.estimate_text(ctx.active, options))
