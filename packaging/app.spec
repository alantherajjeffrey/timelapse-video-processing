# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for Timelapse Video Processing 0.7 (one-dir build).

Invoked by packaging\\build.ps1 as:
    python -m PyInstaller --noconfirm --clean --distpath dist --workpath <dev folder>\\build\\pyinstaller packaging\\app.spec
(run with the working directory set to the version folder, i.e. the parent
of packaging\\, so the relative paths below resolve correctly either way —
SPECPATH is used instead of the cwd for everything that matters).

Output: dist\\Timelapse Video Processing\\Timelapse Video Processing.exe plus its
_internal\\ payload. installer.nsi's default DIST_DIR
("..\\dist\\Timelapse Video Processing") expects exactly this COLLECT name.

Asset loading contract (also recorded in docs/INTEGRATION_NOTES.md): the
`assets` datas entry below places source/etaluma_video/assets at the
in-bundle path "etaluma_video/assets" — i.e. as if it were a real
subdirectory of the etaluma_video package, mirroring its on-disk layout
under source/. That is what lets application code use ONE code path in both
source and frozen builds:

    from importlib.resources import files
    assets_dir = files("etaluma_video") / "assets"

PyInstaller's frozen importer implements importlib.resources' resource
reader for data placed this way, so this works unmodified whether
etaluma_video is a real package on sys.path (`python -m etaluma_video`) or
compiled into the PYZ archive inside the frozen exe. Do NOT use a
`sys._MEIPASS`-conditional helper for this — it is unnecessary with the
datas layout below and would add a second, divergent code path to keep in
sync between UI shell/explorer/calibration agents.

PySide6 trimming: `excludes` below stops PyInstaller from analyzing/pulling
in the listed submodules in the first place. That alone does not fully
shrink the bundle — PySide6's own PyInstaller hook (from
pyinstaller-hooks-contrib) copies Qt plugin/translation directories once
PySide6 itself is collected, independent of which submodules the app
imports — so after Analysis() runs, a.binaries and a.datas are additionally
filtered by destination-path prefix to drop anything under an excluded Qt
module's plugin directory or under PySide6's translations. QtOpenGL /
QtOpenGLWidgets are deliberately kept (not excluded): QWidget's default
rendering path and several Qt Widgets features depend on them even when the
app draws no 3-D content itself; the task brief says to err on keeping it.
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

PACKAGING_DIR = Path(SPECPATH)
V04_DIR = PACKAGING_DIR.parent
SOURCE_DIR = V04_DIR / "source"
APP_PACKAGE_DIR = SOURCE_DIR / "etaluma_video"
ASSETS_DIR = APP_PACKAGE_DIR / "assets"

APP_NAME = "Timelapse Video Processing"
ICON = str(ASSETS_DIR / "icon.ico")
LAUNCHER = str(PACKAGING_DIR / "launcher.py")
VERSION_FILE = str(PACKAGING_DIR / "version_info.txt")

datas = []
binaries = []
hiddenimports = []

# The app's own assets (icon + glyphs/*.png harvested in M1; glyphs/ may be
# empty at spec-authoring time — PyInstaller globs the directory at build
# time, so glyphs added later need no change here). Destination mirrors the
# source layout; see the asset-loading contract in the module docstring.
datas.append((str(ASSETS_DIR), "etaluma_video/assets"))

# PySide6 is LGPL; ship the license/attribution notices prepare_assets.py
# regenerates so the frozen build carries them (required for LGPL, and for
# every other pinned dependency's license terms). Destination "." lands
# under dist\Timelapse Video Processing\_internal\ in PyInstaller 6's onedir
# layout, which installer.nsi's uninstaller already removes wholesale via
# `RMDir /r "$INSTDIR\_internal"` — do not add other root-level datas here
# without adding a matching Delete in installer.nsi's un.Program files
# section, or RMDir "$INSTDIR" will fail silently on leftover files.
_notices = PACKAGING_DIR / "THIRD_PARTY_NOTICES.txt"
if _notices.exists():
    datas.append((str(_notices), "."))

# imageio_ffmpeg bundles a prebuilt ffmpeg executable as package data (must
# be collected as a binary, not left for PyInstaller's default pure-Python
# module scan to find). imagecodecs ships compiled extension modules/DLLs
# for the TIFF codecs tifffile uses. collect_all pulls in each package's
# data files, binaries and any hook-declared hidden imports together.
for _package in ("imageio_ffmpeg", "imagecodecs"):
    _pkg_datas, _pkg_binaries, _pkg_hidden = collect_all(_package)
    datas += _pkg_datas
    binaries += _pkg_binaries
    hiddenimports += _pkg_hidden

# PySide6 modules this app does not use (task brief list). Excluding these
# keeps PyInstaller from analyzing them if anything imports them
# transitively; QtOpenGL/QtOpenGLWidgets are intentionally NOT here (kept).
PYSIDE6_EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D", "PySide6.QtQuickControls2",
    "PySide6.QtQml",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtCharts",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtDesigner",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech",
    "PySide6.QtWebSockets", "PySide6.QtWebChannel",
    "PySide6.QtHttpServer",
    "PySide6.QtGraphs", "PySide6.QtGraphsWidgets",
    "PySide6.QtDataVisualization",
    "PySide6.QtLocation",
]

OTHER_EXCLUDES = [
    "PyQt5", "PyQt6", "PySide2",
    "tkinter", "pytest", "pytestqt", "IPython", "matplotlib",
]

# Destination-path prefixes to drop from a.binaries/a.datas after Analysis —
# see the PySide6-trimming note in the module docstring for why `excludes`
# alone is not enough. The small Python binding modules land at
# "PySide6/QtWebEngineCore...", but the actual Qt shared libraries PySide6
# ships land at "PySide6/Qt6WebEngineCore.dll" (note the "Qt6" prefix, not
# "Qt") — both forms are derived below so the DLLs are actually stripped and
# not just the bindings. Qt translations are dropped for every module, not
# just the excluded ones (task brief: "translations excluded").
_STRIP_DEST_PREFIXES = tuple(
    name.replace("PySide6.", "PySide6/") for name in PYSIDE6_EXCLUDES
) + tuple(
    name.replace("PySide6.Qt", "PySide6/Qt6") for name in PYSIDE6_EXCLUDES
) + (
    "PySide6/translations",
    "PySide6/Qt/translations",
    "PySide6/Qt/qml",
    "PySide6/qml",
)


# 1.0: what the app never uses, left out so the installer stays under GitHub's 100 MB file limit.
# Qt's software OpenGL renderer, Pillow's AVIF plug-in, and every image codec but those TIFF and
# PNG need. OpenCV's FFmpeg plug-in stays: this OpenCV build reads MP4 files only through it.
_UNUSED_DEST_PREFIXES = ("PySide6/opengl32sw.dll", "PIL/_avif")
IMAGECODECS_KEEP = ("_imcd", "_shared", "_shared_cython", "_zlib", "_deflate", "_lzma", "_zstd", "_png", "_jpeg8")


def _unused_imagecodecs(dest):
    if not dest.startswith("imagecodecs/"):
        return False
    name = dest.split("/", 1)[1]
    return "/" not in name and name.startswith("_") and name.endswith((".pyd", ".dll")) \
        and name.split(".", 1)[0] not in IMAGECODECS_KEEP


def _survives_trim(entry):
    dest = entry[0].replace("\\", "/")
    return not dest.startswith(_STRIP_DEST_PREFIXES + _UNUSED_DEST_PREFIXES) and not _unused_imagecodecs(dest)


a = Analysis(
    [LAUNCHER],
    pathex=[str(SOURCE_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    excludes=PYSIDE6_EXCLUDES + OTHER_EXCLUDES,
    noarchive=False,
)

a.binaries = [b for b in a.binaries if _survives_trim(b)]
a.datas = [d for d in a.datas if _survives_trim(d)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON,
    version=VERSION_FILE,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
