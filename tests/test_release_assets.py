"""Release files: fixed names without spaces, one zip with everything needed to install, matching checksums."""
from __future__ import annotations

import hashlib
import importlib.util
import zipfile
from pathlib import Path

APP = Path(__file__).resolve().parent.parent


def _module():
    spec = importlib.util.spec_from_file_location("release_assets", APP / "tools" / "release_assets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_files_zip_and_checksums(tmp_path):
    module = _module()
    root = tmp_path / "version"
    (root / "packaging").mkdir(parents=True)
    (root / "packaging" / "THIRD_PARTY_NOTICES.txt").write_text("notices", encoding="utf-8")
    (root / "LICENSE").write_text("MIT", encoding="utf-8")
    for name in module.release_names():
        (root / name).write_bytes(name.encode("utf-8"))
    module.make_release_assets(root, tmp_path / "out")
    out = tmp_path / "out"
    assert sorted(p.name for p in out.iterdir()) == [
        "SHA256SUMS.txt", "Timelapse-Video-Processing-Setup.exe", "Timelapse-Video-Processing-Windows.zip",
        "Uninstall-Timelapse-Video-Processing.exe"]
    for line in (out / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ")
        assert " " not in name and hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    with zipfile.ZipFile(out / "Timelapse-Video-Processing-Windows.zip") as zf:
        names = sorted(zf.namelist())
        folder = names[0].split("/")[0]
        assert folder.startswith("Timelapse-Video-Processing-") and all(n.startswith(folder + "/") for n in names)
        assert sorted(n.split("/", 1)[1] for n in names) == [
            "LICENSE.txt", "README.txt", "SHA256SUMS.txt", "THIRD_PARTY_NOTICES.txt",
            "Timelapse-Video-Processing-Setup.exe", "Uninstall-Timelapse-Video-Processing.exe"]
        assert zf.read(f"{folder}/Timelapse-Video-Processing-Setup.exe") == (out / "Timelapse-Video-Processing-Setup.exe").read_bytes()
        readme = zf.read(f"{folder}/README.txt").decode("utf-8")
        assert "Extract All" in readme and "More info" in readme and "research use only" in readme.lower()
