# Developing Timelapse Video Processing

How the code is organised, how to run and test it, where to make common changes, and how to build and publish a release. Code-level contracts (signals, rules) are in [CONTRACTS.md](CONTRACTS.md); the display maths in [ALGORITHMS.md](ALGORITHMS.md).

## Repository layout

```text
README.md                 what the app is and does (start here)
LICENSE                   MIT
source/
  etaluma_video/
    __init__.py           version, name, credits
    __main__.py           start-up: logging, crash hooks, theme, main window
    cli.py                the same engine without the window
    engine/               processing, no Qt anywhere
    ui/                   the PySide6 interface
    assets/               icon, glyph templates for reading scale-bar labels
  pyproject.toml, requirements.lock.txt
  Install dev environment.bat, Launch (source).bat
tests/                    pytest suite; synthetic data in tests/fixtures
tools/                    demo data, privacy scan, public-repository export, glyph harvesting, probes
packaging/                PyInstaller spec, NSIS installer and uninstaller, build and test scripts, GitHub templates
docs/                     algorithms, contracts, changelog, backlog, this guide, publishing
```

## Architecture

**Engine** (`source/etaluma_video/engine`), pure Python and NumPy/OpenCV:

| Module | Role |
|---|---|
| `parsing.py` | Finds captures, decodes file names (any position name), reads `.epf`/`.avs`/`.roi`, builds a `Dataset`. |
| `calibration.py` | Detects Lumaview's burned-in overlays and reads the scale-bar label by glyph matching. |
| `histograms.py` | Sampled full-size intensity histograms per position, cached on disk; the basis of Auto bounds. |
| `display.py` | Lookup tables, Auto methods, blends, WHITE presets, rolling ball; `render_frame_fast` renders every picture the app shows or writes (`render_frame` is the float reference). |
| `overlays.py` | Time format, and the time, name, scale-bar and channel-key overlays. |
| `fastexport.py` | One pass per position: decode each TIFF once, render every video, montage tile and poster, encode on separate threads. |
| `export.py` | MJPG and H.264 writers, quality presets. |
| `process.py` | `Options`, validation, the run folder, `process_dataset`, batches, the size estimate. |
| `reports.py`, `quantify.py` | Montages, dashboards, CSVs; raw measurements. |
| `models.py`, `jobs.py`, `userdata.py` | Data classes, cancellation and progress protocol, user-data folder. |

A run: `scan_dataset` → `calibrate` → overlay mask → WHITE preset → `build_all_positions` (sampled histograms, cached) → effective bounds → `fastexport.export_position` per position → reports.

**Interface** (`source/etaluma_video/ui`): widgets never call each other; they read and write `AppContext` (`state.py`) and react to its signals. User actions go through `actions.py`; long jobs run one at a time through `jobs_qt.JobRunner`; background work (preview loading, intensity measurement, posters) runs on the thread pool. Key pieces: `main_window.py` (layout, panels, menus), `explorer.py`, `viewer.py` and `preview_cache.py` (live preview), `display_panel.py` and `histogram_widget.py`, `output_panel.py`, `results_view.py` and `results_dock.py`, `log_dock.py`, `experiment_memory.py` (per-experiment settings), `panels.py`, `sections.py`, `welcome.py`, `theme.py` and `icons.py`, `about_dialog.py`, `crash_dialog.py`.

## Set up a development environment (Windows)

1. Install Python 3.12 or newer, 64-bit (development used 3.14).
2. Run `source\Install dev environment.bat`. It creates `%LOCALAPPDATA%\EtalumaVP-dev\v0.9\.venv` (or `%ETALUMA_DEV_DIR%\.venv`), outside the repository, and installs the pinned requirements and the package in editable mode.
3. Start the app from source with `source\Launch (source).bat`, or:

```text
"%LOCALAPPDATA%\EtalumaVP-dev\v0.9\.venv\Scripts\python.exe" -m etaluma_video
"%LOCALAPPDATA%\EtalumaVP-dev\v0.9\.venv\Scripts\python.exe" -m etaluma_video.cli --help
```

Settings, caches and logs of a source run go to `%LOCALAPPDATA%\Timelapse Video Processing` like the installed app; set `ETALUMA_DATA_DIR` to keep them apart.

## Tests

```text
set QT_QPA_PLATFORM=offscreen
python -m pytest tests -q --basetemp "%LOCALAPPDATA%\EtalumaVP-dev\v0.9\build\pytest"
```

- Everything runs on synthetic data (`tests/fixtures/make_synthetic_dataset.py`, the same generator as `tools/make_demo_dataset.py`), with burned-in overlays drawn from the real glyphs.
- Tests that need real captures skip unless `ETALUMA_SAMPLES` points at a folder of Lumaview experiments.
- `--basetemp` avoids a Windows temp folder that some Python builds cannot write to; any writable folder works.
- The suite covers parsing of every layout, calibration, display maths (8-bit renderer against the float reference), exports and run folders, overlays and time format, the interface (offscreen), stability cases (unreadable files, full disk, crash window) and release readiness (credits, privacy scan).

## Coding rules

- `engine/` never imports Qt or `ui/`.
- Every pixel shown or written goes through `display.render_frame_fast` / `render_single_channel_fast`; measurements use raw planes.
- Engine functions take `progress` (a string callable) and `cancel` (a `CancelToken`) and raise `JobCancelled`; progress lines that only move the bar start with `jobs.PROGRESS_PREFIX`.
- No modal dialogs on paths reachable from `actions.py`; the tests run headless.
- Write for the reader: docstrings say why, not only what; log messages are sentences a user can act on.

## Common changes

| To add… | Change |
|---|---|
| a run option | a field on `process.Options` (+ `validate_options`), use it in `process.py`/`fastexport.py`, a control and `values()` entry in `output_panel.py`, a flag in `cli.py`, a test. |
| an output file | `process.py` (`save_image` records it in the metadata), then `results_view.collect` if the results page should show it. |
| a folder layout or name scheme | `parsing.parse_filename` / `scan_dataset`, an `EXPECTED` row or a synthetic case in `tests/test_engine_parsing.py`. |
| a display method | `display.auto_bounds_detail`, `models.AUTO_METHODS`, the method combo in `display_panel.py`, ALGORITHMS.md. |
| a theme | a colour table in `ui/theme.py` (`THEME_COLOURS`, `LOG_COLOURS`); widgets follow the palette. |

## Build the installer

```text
powershell -ExecutionPolicy Bypass -File packaging\fetch-tools.ps1      (once: NSIS 3.11, hash-checked)
powershell -ExecutionPolicy Bypass -File packaging\build.ps1            (tests, PyInstaller, NSIS, SHA-256)
```

`build.ps1 -SkipTests` skips the tests. The frozen app is staged in `%LOCALAPPDATA%\EtalumaVP-build` (short paths avoid Windows' 260-character limit); the installer and the standalone uninstaller land at the repository root with `.sha256` files. `packaging\test-installer.ps1 -Setup <installer> [-Force]` installs, checks, reinstalls and uninstalls for the current user; it refuses to touch an existing installation unless given `-Force`, and with `-Force` it backs up and restores the user-data folder. See [packaging/README.md](../packaging/README.md).

## Privacy check

`python tools/scrub_check.py --git` fails when tracked files contain a Windows user-profile path, a data-drive path or an e-mail address (the third-party licence notices keep their authors' addresses). Add words of your own with `ETALUMA_SCRUB_WORDS=word1,word2`. A test runs it on every test run.

## Versions and releases

A new version changes `VERSION` in `source/etaluma_video/__init__.py` and `version` in `source/pyproject.toml`, the version strings in `packaging/installer.nsi`, `uninstaller.nsi`, `version_info.txt`, `build.ps1`, `test-installer.ps1` and `tools/build_release.bat`, the development folder name (`EtalumaVP-dev\v0.9`) in the scripts, and `tests/test_engine_outputs.py`. Record the changes in `docs/CHANGELOG.md`. Publishing: [PUBLISHING.md](PUBLISHING.md).
