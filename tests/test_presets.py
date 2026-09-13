"""0.8: output presets, display choices and the Quick video split button."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.fixtures.make_synthetic_dataset import make_dataset  # noqa: E402

LONG = 60000


@pytest.fixture
def data_dir(monkeypatch):
    folder = Path(tempfile.mkdtemp(prefix="etaluma_presets_"))
    monkeypatch.setenv("ETALUMA_DATA_DIR", str(folder / "userdata"))
    return folder


def test_builtin_presets_and_normalise():
    from etaluma_video.engine import presets
    from etaluma_video.engine.process import Options

    quick = presets.builtin_values("quick")
    assert quick["width"] == 1900 and quick["mp4"] and not quick["avi"] and quick["name_label"]
    standard = presets.builtin_values("standard")
    assert standard["width"] == 950 and standard["avi"] and standard["mp4"] and standard["app_timestamp"]
    show = presets.builtin_values("presentation")
    assert show["video_quality"] == "high" and show["composite"] and not show["per_channel"]
    assert not show["fluorescence_only"] and not show["montages"]
    odd = presets.normalise({"width": 951, "video_quality": "huge", "time_days": "sometimes", "avi": 0, "extra": 1})
    assert odd["width"] == 950 and odd["video_quality"] == "standard" and odd["time_days"] == "auto"
    assert odd["avi"] is False and "extra" not in odd
    opts = presets.apply_preset(Options(), show)
    assert opts.width == 1900 and opts.channel_videos == [] and opts.fluorescence_only_video is False
    assert presets.apply_preset(Options(), standard).channel_videos is None
    assert presets.same(standard, dict(standard, fps=None))


def test_user_presets_save_rename_delete(data_dir):
    from etaluma_video.ui import presets_store as store

    key = store.save_user_preset("Lab meeting", {"width": 1280, "avi": False})
    assert key == "user:Lab meeting" and store.preset_values(key)["width"] == 1280
    assert ("user:Lab meeting", "Lab meeting") in store.preset_choices()
    with pytest.raises(ValueError):
        store.save_user_preset("Quick", {})
    with pytest.raises(ValueError):
        store.save_user_preset("a/b", {})
    renamed = store.rename_user_preset("Lab meeting", "Talks")
    assert renamed == "user:Talks" and "Lab meeting" not in store.user_presets()
    store.delete_user_preset("Talks")
    with pytest.raises(ValueError):
        store.preset_values("user:Talks")
    assert store.preset_values(store.CURRENT, {"width": 640})["width"] == 640


def test_display_choices_and_options(data_dir):
    from etaluma_video.engine.models import DisplayProfile
    from etaluma_video.engine.parsing import scan_dataset
    from etaluma_video.engine.userdata import profiles_dir
    from etaluma_video.ui import presets_store as store
    from etaluma_video.ui.experiment_memory import save_record

    dataset = scan_dataset(make_dataset(data_dir / "20260101_120000_choices", positions=1, timepoints=2, size=200))
    auto, note = store.resolve_display(store.AUTO, dataset)
    assert auto.mode == "auto" and set(auto.channels) == {"F2", "F3"} and note == "Auto-normalised"
    remembered, note = store.resolve_display(store.REMEMBERED, dataset)
    assert remembered.mode == "auto" and "nothing remembered" in note
    manual = DisplayProfile.default_for(["F2", "F3"])
    manual.mode = "manual"
    manual.channels["F2"].low, manual.channels["F2"].high = 12.0, 90.0
    save_record(str(dataset.root), {"root": str(dataset.root), "profile": manual.to_dict(),
                                    "channel_names": {"F2": "CFDA"}, "time_offset": "Day 1 : 00:00:00",
                                    "timepoint_range": [1, 1]})
    remembered, _ = store.resolve_display(store.REMEMBERED, dataset)
    assert remembered.mode == "manual" and remembered.channels["F2"].high == 90.0
    manual.save(profiles_dir() / "Bright.json")
    assert ("profile:Bright", "Profile: Bright") in store.display_choices()
    assert store.resolve_display("profile:Bright", dataset)[0].channels["F2"].low == 12.0
    current, _ = store.resolve_display(store.CURRENT, dataset, manual.to_dict())
    assert current.mode == "manual"
    from etaluma_video.ui.experiment_memory import load_record

    opts = store.options_for(dataset, store.preset_values(store.QUICK), auto, load_record(str(dataset.root)), quick=True)
    assert opts.quick and opts.width == 1900 and not opts.avi
    assert opts.channel_names == {"F2": "CFDA"} and opts.time_offset_seconds == 86400.0
    assert opts.timepoint_range == [1, 1]


def test_output_panel_preset_row(qtbot, data_dir):
    from etaluma_video.ui.output_panel import OutputPanel
    from etaluma_video.ui.settings import Settings
    from etaluma_video.ui.state import AppContext

    panel = OutputPanel(AppContext(), settings=Settings())
    qtbot.addWidget(panel)
    assert panel.preset_key == "builtin:standard" and not panel.preset_modified
    assert panel.apply_preset("builtin:quick")
    assert panel.width == 1900 and not panel.avi_check.isChecked() and panel.name_label_check.isChecked()
    assert not panel.preset_modified and panel.preset_combo.currentData() == "builtin:quick"
    panel.mp4_check.setChecked(False)
    panel.avi_check.setChecked(True)
    assert panel.preset_modified and panel.preset_combo.currentText().startswith("Current (modified from Quick")
    assert panel.save_preset_as("AVI big") == "user:AVI big"
    assert not panel.preset_modified and panel.preset_combo.currentText() == "AVI big"
    assert panel.rename_preset("AVI large") == "user:AVI large"
    assert panel.delete_preset() and panel.preset_key == "builtin:standard" and panel.preset_modified


def test_quick_video_menu_and_preset_options(qtbot, data_dir):
    from etaluma_video.ui.main_window import MainWindow
    from etaluma_video.ui.settings import Settings
    from etaluma_video.ui.state import AppContext
    from etaluma_video.ui.workers import wait_for_background

    window = MainWindow(AppContext(), Settings())
    qtbot.addWidget(window)
    window.show()
    try:
        toolbar = window.toolbar
        toolbar._fill_quick_menu()
        keys = [a.data() for a in toolbar.quick_menu.actions() if a.isCheckable()]
        assert "auto" in keys and "builtin:quick" in keys and "current" in keys
        checked = [a.data() for a in toolbar.quick_menu.actions() if a.isChecked()]
        assert checked == ["auto", "builtin:quick"]
        toolbar.set_quick_choice("quick_preset", "builtin:presentation")
        assert window.actions_api.settings.quick_preset == "builtin:presentation"
        assert "Presentation" in toolbar.quick_action.toolTip()

        root = make_dataset(data_dir / "20260101_120000_quick", positions=1, timepoints=2, size=200)
        assert window.actions_api.open_folder(str(root))
        qtbot.waitUntil(lambda: window.ctx.active is not None, timeout=LONG)
        qtbot.waitUntil(lambda: not window.runner.busy, timeout=LONG)
        options, note = window.actions_api.preset_options(window.ctx.active, "auto", "builtin:presentation")
        assert options.width == 1900 and options.video_quality == "high" and not options.quick
        assert options.profile.mode == "auto" and note == "Auto-normalised"
        options, _ = window.actions_api.preset_options(window.ctx.active, "current", "builtin:quick")
        assert options.quick and options.name_label
    finally:
        window.viewer.controller.cancel_all()
        qtbot.waitUntil(lambda: not window.runner.busy, timeout=LONG)
        wait_for_background(20000)
