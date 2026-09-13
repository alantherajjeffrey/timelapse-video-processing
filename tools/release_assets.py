"""Release files for GitHub: fixed names without spaces or version numbers, and one SHA256SUMS.txt.

    python tools/release_assets.py <output folder>

GitHub turns spaces in release file names into dots, and the README links to
``releases/latest/download/Timelapse-Video-Processing-Setup.exe``; so the built installer and
uninstaller are copied under fixed names, and the version lives in the release tag and title.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "source"))

from etaluma_video import APP_NAME, VERSION  # noqa: E402


def release_names() -> dict[str, str]:
    """Built file name at the version folder root -> release file name."""
    short = ".".join(VERSION.split(".")[:2])
    dashed = APP_NAME.replace(" ", "-")
    return {f"{APP_NAME} Setup {short}.exe": f"{dashed}-Setup.exe",
            f"Uninstall {APP_NAME}.exe": f"Uninstall-{dashed}.exe"}


def make_release_assets(root: Path | str, out: Path | str) -> list[Path]:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    written, sums = [], []
    for built, release in release_names().items():
        source = Path(root) / built
        if not source.is_file():
            raise SystemExit(f"{source} is missing: build first (packaging\\build.ps1).")
        target = out / release
        shutil.copy2(source, target)
        sums.append(f"{hashlib.sha256(target.read_bytes()).hexdigest()}  {release}")
        written.append(target)
    checksums = out / "SHA256SUMS.txt"
    checksums.write_text("\n".join(sums) + "\n", encoding="utf-8")
    return written + [checksums]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    for path in make_release_assets(APP, sys.argv[1]):
        print(path)
