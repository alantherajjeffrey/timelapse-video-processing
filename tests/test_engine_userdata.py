"""Per-user data folder: settings, profiles, preview cache, logs.

Ported from Codex 0.3 ``tests/test_desktop.py`` and the ``DesktopSupportTests`` of
``tests/test_release_review.py``; the Streamlit/WebView request bridge is gone, so only the
filesystem behaviour remains. Every test redirects the folder with ETALUMA_DATA_DIR.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from etaluma_video.engine import userdata


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ETALUMA_DATA_DIR", str(tmp_path / "userdata"))
    return tmp_path / "userdata"


def test_data_folder_is_redirectable_and_has_the_documented_subfolders(isolated):
    assert userdata.user_data_dir() == isolated
    assert isolated.is_dir()
    assert userdata.profiles_dir() == isolated / "profiles"
    assert userdata.preview_cache_dir() == isolated / "preview_cache"
    assert userdata.logs_dir() == isolated / "logs"
    assert userdata.settings_path() == isolated / "settings.json"
    for folder in (userdata.profiles_dir(), userdata.preview_cache_dir(), userdata.logs_dir()):
        assert folder.is_dir()


def test_settings_round_trip_and_corruption_is_survivable(isolated):
    assert userdata.load_settings() == {}
    userdata.save_settings({"objective": "10x", "last_input": "C:/Images", "profile": {"blend": "screen"}})
    assert userdata.load_settings()["objective"] == "10x"
    assert userdata.load_settings()["profile"]["blend"] == "screen"
    userdata.settings_path().write_text("corrupt")
    assert userdata.load_settings() == {}
    with pytest.raises(TypeError):
        userdata.save_settings([])


def test_atomic_write_leaves_no_temporary_file(isolated):
    target = isolated / "nested" / "profile.json"
    userdata.atomic_write_json(target, {"schema": 1, "name": "manual µm"})
    assert json.loads(target.read_text(encoding="utf-8"))["name"] == "manual µm"
    assert not list(target.parent.glob("*.tmp"))
    userdata.atomic_write_json(target, {"schema": 2})
    assert userdata.read_json(target)["schema"] == 2
    assert userdata.read_json(isolated / "missing.json", {"fallback": True}) == {"fallback": True}
    (isolated / "broken.json").write_text("{")
    assert userdata.read_json(isolated / "broken.json") is None


def test_atomic_write_retries_a_transient_windows_lock(isolated):
    target = isolated / "job.json"
    original = userdata.os.replace
    attempts = []

    def locked_once(source, destination):
        attempts.append(source)
        if len(attempts) == 1:
            raise PermissionError("Reader briefly holds destination")
        original(source, destination)

    with patch.object(userdata.os, "replace", side_effect=locked_once):
        userdata.atomic_write_json(target, {"active": True})
    assert json.loads(target.read_text(encoding="utf-8"))["active"] is True
    assert len(attempts) == 2
    assert not list(isolated.glob("*.tmp"))


def test_results_link_prefers_reviewed_then_recent_actual_output(tmp_path):
    reviewed, recent = tmp_path / "reviewed", tmp_path / "recent"
    reviewed.mkdir()
    recent.mkdir()
    assert userdata.output_folder_to_open(reviewed, [recent]) == reviewed
    assert userdata.output_folder_to_open(tmp_path / "missing", [recent]) == recent
    assert userdata.output_folder_to_open() is None


def test_format_duration():
    assert userdata.format_duration(4.25) == "4.2 seconds"
    assert userdata.format_duration(83) == "1m 23s"
    assert userdata.format_duration(3725) == "1h 2m 5s"


def test_display_profiles_persist_in_the_profiles_folder(isolated):
    from etaluma_video.engine.models import DisplayProfile

    profile = DisplayProfile.default_for(["F2", "F3"])
    profile.mode = "manual"
    profile.channels["F2"].low, profile.channels["F2"].high = 6.0, 110.0
    path = userdata.profiles_dir() / "lab.json"
    profile.save(path)
    loaded = DisplayProfile.load(path)
    assert loaded.mode == "manual"
    assert (loaded.channels["F2"].low, loaded.channels["F2"].high) == (6, 110)
    assert loaded.signature() == profile.signature()  # float-valued bounds round-trip exactly
