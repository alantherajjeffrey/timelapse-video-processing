"""0.9: release files have fixed names without spaces, and one checksum file that matches them."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

APP = Path(__file__).resolve().parent.parent


def test_release_files_have_fixed_names_and_matching_checksums(tmp_path):
    spec = importlib.util.spec_from_file_location("release_assets", APP / "tools" / "release_assets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "version"
    root.mkdir()
    for name in module.release_names():
        (root / name).write_bytes(name.encode("utf-8"))
    module.make_release_assets(root, tmp_path / "out")
    names = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert names == ["SHA256SUMS.txt", "Timelapse-Video-Processing-Setup.exe", "Uninstall-Timelapse-Video-Processing.exe"]
    for line in (tmp_path / "out" / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ")
        assert " " not in name
        assert hashlib.sha256((tmp_path / "out" / name).read_bytes()).hexdigest() == digest
