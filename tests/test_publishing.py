"""0.7: the public-repository export and the demo data generator."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from etaluma_video.engine import scan_dataset

TOOLS = Path(__file__).resolve().parent.parent / "tools"


def tool(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_makes_a_clean_repository_folder(tmp_path):
    target = tool("export_public_repo").export(tmp_path / "repo", init_git=False)
    for rel in ("README.md", "LICENSE", ".gitignore", ".github/workflows/tests.yml",
                ".github/ISSUE_TEMPLATE/bug_report.md", "source/etaluma_video/__init__.py"):
        assert (target / rel).is_file(), rel
    assert not list(target.rglob("*.exe")) and not list(target.rglob("*.sha256"))
    assert "the Timelapse Video Processing contributors" in (target / "LICENSE").read_text(encoding="utf-8")


def test_demo_dataset_opens_like_a_capture(tmp_path):
    assert tool("make_demo_dataset").main([str(tmp_path / "demo"), "--positions", "2", "--timepoints", "3",
                                           "--size", "400"]) == 0
    ds = scan_dataset(tmp_path / "demo")
    assert len(ds.groups) == 2 and ds.mode == "timelapse" and ds.channels == ["WHITE", "F2", "F3"]
