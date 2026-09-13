"""Release files for GitHub: fixed names without spaces or version numbers, one zip, one SHA256SUMS.txt.

    python tools/release_assets.py <output folder>

GitHub turns spaces in release file names into dots, and the README links to
``releases/latest/download/<name>``; so the files have fixed names and the version lives in the
release tag and title. ``Timelapse-Video-Processing-Windows.zip`` holds everything needed to
install (installer, uninstaller, README.txt, checksums, licences) in one folder.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "source"))

from etaluma_video import APP_NAME, CREDITS, DISCLAIMER, REPOSITORY_URL, RESEARCH_USE, VERSION  # noqa: E402

#: GitHub refuses files over 100 MiB in a repository; since 1.0 the installer is committed too.
GITHUB_FILE_LIMIT = 100 * 1024 * 1024
DASHED = APP_NAME.replace(" ", "-")
ZIP_NAME = f"{DASHED}-Windows.zip"


def release_names() -> dict[str, str]:
    """Built file name at the version folder root -> release file name."""
    short = ".".join(VERSION.split(".")[:2])
    return {f"{APP_NAME} Setup {short}.exe": f"{DASHED}-Setup.exe",
            f"Uninstall {APP_NAME}.exe": f"Uninstall-{DASHED}.exe"}


def readme_text() -> str:
    """README.txt inside the zip: how to install, in plain words."""
    return f"""{APP_NAME} {VERSION} for Windows 10 and 11 (64-bit)
For Etaluma LS720 captures. {CREDITS}
{RESEARCH_USE}

INSTALL
1. Unpack this zip: right-click it, then Extract All.
2. Double-click {DASHED}-Setup.exe. It installs for the current Windows user:
   no administrator rights, no Python and no internet needed.
3. The installer is not code-signed, so Windows SmartScreen may say
   "Windows protected your PC": click More info, then Run anyway.
4. Start {APP_NAME} from the Start menu or the desktop shortcut.
   An earlier version is replaced and its settings are kept.

UNINSTALL
Windows Settings > Apps > {APP_NAME}, or double-click
Uninstall-{DASHED}.exe from this folder. The analysis_output
folders with your videos are never touched.

CHECK THE FILES (optional)
SHA256SUMS.txt lists the SHA-256 checksum of both programs. In PowerShell:
  Get-FileHash .\\{DASHED}-Setup.exe

Instructions, screenshots and bug reports: {REPOSITORY_URL}
Licence: MIT (LICENSE.txt). Third-party components: THIRD_PARTY_NOTICES.txt
(the bundled FFmpeg is a separate GPL-3.0 program).
{DISCLAIMER}
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _licence(root: Path) -> Path | None:
    for candidate in (root / "LICENSE", root.parent / "LICENSE"):
        if candidate.is_file():
            return candidate
    return None


def make_release_assets(root: Path | str, out: Path | str) -> list[Path]:
    root, out = Path(root), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    programs = []
    for built, release in release_names().items():
        source = root / built
        if not source.is_file():
            raise SystemExit(f"{source} is missing: build first (packaging\\build.ps1).")
        if source.stat().st_size >= GITHUB_FILE_LIMIT:
            raise SystemExit(f"{source.name} is {source.stat().st_size / 1024 ** 2:.1f} MB, over GitHub's "
                             "100 MB file limit: trim the bundle (packaging/app.spec).")
        target = out / release
        shutil.copy2(source, target)
        programs.append(target)
    program_sums = "".join(f"{_sha256(p)}  {p.name}\n" for p in programs)

    folder = f"{DASHED}-{VERSION}"
    archive = out / ZIP_NAME
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for program in programs:
            zf.write(program, f"{folder}/{program.name}")
        zf.writestr(f"{folder}/README.txt", readme_text().replace("\n", "\r\n"))
        zf.writestr(f"{folder}/SHA256SUMS.txt", program_sums)
        licence = _licence(root)
        if licence is not None:
            zf.write(licence, f"{folder}/LICENSE.txt")
        notices = root / "packaging" / "THIRD_PARTY_NOTICES.txt"
        if notices.is_file():
            zf.write(notices, f"{folder}/THIRD_PARTY_NOTICES.txt")

    checksums = out / "SHA256SUMS.txt"
    checksums.write_text(program_sums + f"{_sha256(archive)}  {archive.name}\n", encoding="utf-8")
    return programs + [archive, checksums]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    for path in make_release_assets(APP, sys.argv[1]):
        print(path)
