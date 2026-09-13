"""0.8: Help → Check for updates, with the network replaced by a fake answer."""
from __future__ import annotations

import io
import json
import os
import urllib.error

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def _opener(payload=None, error=None):
    def open_url(request, timeout=None):
        assert request.full_url.endswith("/releases/latest") and "api.github.com" in request.full_url
        assert request.get_header("User-agent", "").startswith("Timelapse-Video-Processing/")
        if error is not None:
            raise error
        return _Answer(json.dumps(payload).encode("utf-8"))
    return open_url


def test_versions_and_slug():
    from etaluma_video.ui.update_check import parse_version, repository_slug

    assert repository_slug("https://github.com/someone/some-repo.git") == "someone/some-repo"
    assert parse_version("v0.10.2") == (0, 10, 2) and parse_version("0.8") == (0, 8, 0)
    assert parse_version("v0.9.0-beta") is None and parse_version("v0.10.0") > parse_version("0.9.9")


@pytest.mark.parametrize("tag, status", [("v99.0.0", "newer"), ("v0.8.0", "current"), ("v0.1.0", "current")])
def test_latest_release_is_compared(tag, status):
    from etaluma_video.ui.update_check import check_latest

    result = check_latest(opener=_opener({"tag_name": tag, "html_url": "https://github.com/x/y/releases/tag/" + tag}))
    assert result.status == status and result.url.endswith(tag)
    if status == "newer":
        assert "99.0.0 is available" in result.message


def test_no_release_offline_and_rate_limit_give_a_sentence():
    from etaluma_video.ui.update_check import check_latest

    def http(code):
        return urllib.error.HTTPError("https://api.github.com", code, "x", {}, None)

    assert check_latest(opener=_opener(error=http(404))).status == "none"
    assert "later" in check_latest(opener=_opener(error=http(403))).message
    offline = check_latest(opener=_opener(error=urllib.error.URLError("no network")))
    assert offline.status == "error" and "could not be reached" in offline.message


def test_help_menu_offers_the_check_and_nothing_runs_at_start(qtbot, monkeypatch, tmp_path):
    monkeypatch.setenv("ETALUMA_DATA_DIR", str(tmp_path / "userdata"))
    from etaluma_video.ui import update_check
    from etaluma_video.ui.main_window import MainWindow
    from etaluma_video.ui.settings import Settings
    from etaluma_video.ui.state import AppContext

    calls = []
    monkeypatch.setattr(update_check, "check_latest",
                        lambda *a, **k: calls.append(1) or update_check.UpdateResult("current", "0.8.0", "u", "ok"))
    window = MainWindow(AppContext(), Settings())
    qtbot.addWidget(window)
    qtbot.wait(200)
    assert calls == []  # never at start
    assert window.update_action.text() == "Check for updates…"
    window.update_action.trigger()
    qtbot.waitUntil(lambda: bool(calls) and window.last_update_result is not None, timeout=10000)
    assert window.last_update_result.status == "current"
