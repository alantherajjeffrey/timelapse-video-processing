"""0.7: what a public release needs: the credit and disclaimer, and no machine paths or addresses."""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent


def scrub_module():
    spec = importlib.util.spec_from_file_location("scrub_check", APP / "tools" / "scrub_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_about_box_shows_the_credit_and_disclaimer(qtbot):
    from etaluma_video import CREDITS, DISCLAIMER, RESEARCH_USE
    from etaluma_video.ui.about_dialog import AboutDialog

    dialog = AboutDialog("user data folder", "log folder")
    qtbot.addWidget(dialog)
    text = dialog.text()
    assert "BIOMIS Team, SATIE laboratory, ENS Paris-Saclay" in text and RESEARCH_USE in text
    assert "not affiliated" in text and "Etaluma, Inc." in text
    assert CREDITS in text and DISCLAIMER in text


def test_about_box_and_crash_report_show_paths_without_the_user_name(qtbot, monkeypatch, tmp_path):
    from etaluma_video.ui.about_dialog import AboutDialog
    from etaluma_video.ui.crash_dialog import build_report

    local = tmp_path / "Users" / "someone" / "AppData" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "Users" / "someone"))
    dialog = AboutDialog(str(local / "Timelapse Video Processing"), str(local / "Timelapse Video Processing" / "logs"))
    qtbot.addWidget(dialog)
    text = dialog.text()
    assert "%LOCALAPPDATA%\\Timelapse Video Processing" in text and "someone" not in text
    victim = str(tmp_path / "Users" / "someone" / "data.tif")  # not on the raise line, which the traceback quotes
    try:
        raise ValueError(f"cannot read {victim}")
    except ValueError as exc:
        report = build_report(type(exc), exc, exc.__traceback__)
    assert "someone" not in report and "%USERPROFILE%" in report


def test_tracked_files_have_no_machine_paths_or_addresses():
    if shutil.which("git") is None:
        pytest.skip("git not available")
    try:
        findings = scrub_module().scan(APP, tracked_only=True)
    except subprocess.CalledProcessError:
        pytest.skip("not a git checkout")
    assert findings == []


def test_scrub_check_finds_paths_and_addresses(tmp_path):
    # built from pieces, so this file itself stays clean for the scan above
    profile_path = "C:" + "\\Users\\someone\\data"
    address = "someone" + "@" + "lab.org"
    (tmp_path / "notes.txt").write_text(f"saved to {profile_path} by {address}\n", encoding="utf-8")
    (tmp_path / "ok.txt").write_text("%LOCALAPPDATA%\\Timelapse Video Processing and C:\\Users\\<you>\n", encoding="utf-8")
    findings = scrub_module().scan(tmp_path, extra_words=["secretword"])
    assert len(findings) == 2 and all(f.startswith("notes.txt") for f in findings)


def test_no_personal_name_in_tracked_files():
    """0.8: the app, the installer and the repository name the team, never a person."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    try:
        files = subprocess.run(["git", "ls-files"], cwd=APP, capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError:
        pytest.skip("not a git checkout")
    surname = "raj" + "endran"  # built from pieces so this file passes its own test
    found = []
    for rel in files.splitlines():
        path = APP / rel.strip('"')
        try:
            if surname in path.read_text(encoding="utf-8").casefold():
                found.append(rel)
        except (OSError, UnicodeDecodeError):
            continue
    assert found == []
