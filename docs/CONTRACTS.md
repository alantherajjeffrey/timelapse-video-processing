# Code structure and contracts

How the parts of 0.7 fit together (0.4 structure, later additions marked). A friendlier tour: DEVELOPING.md. The plan is `../../docs/plans/v0.4_plan.md`; the historical build notes of the parallel agents are in `INTEGRATION_NOTES.md`.

## Modules

| Area | Files (under `source/etaluma_video/`) |
|---|---|
| Data contracts | `engine/models.py` (DisplayProfile, Bounds, Overlays, Calibration, ChannelHistogram, HistogramSet, constants), `engine/jobs.py` (CancelToken, check_cancel, JobCancelled) |
| Engine | `engine/parsing.py` (folders, filenames, Dataset, read_plane/read_rgb), `engine/calibration.py` (overlay detection, scale-bar reader), `engine/display.py` (LUTs, auto bounds, blends, WHITE presets, rolling ball, render_frame), `engine/histograms.py` (sampled full-size passes and their cache), `engine/fastexport.py` (single-pass export, montage tiles, posters), `engine/overlays.py` (0.6: time, name and scale-bar overlays, time format), `engine/quantify.py`, `engine/reports.py`, `engine/export.py` (AVI/MP4), `engine/process.py` (Options, process_dataset, process_batch, estimate), `engine/userdata.py`, `engine/__init__.py` (facade) |
| Interface | `__main__.py`, `ui/main_window.py`, `ui/toolbar.py`, `ui/theme.py`, `ui/state.py` (AppContext), `ui/actions.py` (every user action), `ui/jobs_qt.py` (JobRunner), `ui/workers.py` (thread-pool helper), `ui/explorer.py`, `ui/calibration_card.py`, `ui/viewer.py`, `ui/preview_cache.py`, `ui/display_panel.py`, `ui/histogram_widget.py`, `ui/output_panel.py`, `ui/results_view.py` (results page), `ui/results_dock.py` (run discovery, video player), `ui/log_dock.py`, `ui/panels.py` (0.6: collapsible panels and edge strips), `ui/sections.py`, `ui/welcome.py`, `ui/icons.py`, `ui/about_dialog.py`, `ui/crash_dialog.py`, `ui/experiment_memory.py` (0.7), `ui/settings.py`, `ui/settings_dialog.py` |
| Command line | `cli.py` |
| Packaging | `../packaging/` (`app.spec`, `launcher.py`, `installer.nsi`, `uninstaller.nsi`, `build.ps1`, `fetch-tools.ps1`, `test-installer.ps1`) |

## Rules

- `engine/` never imports Qt or anything from `ui/`.
- Preview, export, montages and fixed-image panels render pixels only through `engine.display.render_frame_fast` / `render_single_channel_fast` (8-bit, 0.6); `render_frame` / `render_single_channel` are the float reference the tests compare them with. Measurements never go through `display`.
- Auto bounds come only from `engine.histograms` (sampled, full size, disk cache in `preview_cache/`). The preview controller fills that cache in the background; Process reads it.
- Progress messages that start with `engine.jobs.PROGRESS_PREFIX` only move the progress bar and status line: the run's `processing_log.txt` leaves them out and the log dock shows them in Debug.
- Widgets never call each other; they read and write `AppContext` and react to its signals. Only the GUI thread touches `AppContext` or widgets.
- User jobs (scan, calibrate, Quick video, Process, batch) run one at a time through `ui.jobs_qt.JobRunner`, started only from `ui/actions.py`. Background conveniences (preview loading, histogram passes, calibration of a newly active experiment, crops, stage map) run on the thread pool through `ui.workers.run_in_background` or `ui.preview_cache`, and never block a user job.
- Engine functions take `progress: Callable[[str], None]` and `cancel: CancelToken | None` and raise `JobCancelled` when asked to stop.
- Everything logs through Python `logging`: `etaluma.engine` for progress, `etaluma.ui` for actions (DEBUG `<function>(<kwargs>)` before each action, INFO for the outcome). The log dock shows both; the session file is `%LOCALAPPDATA%\Timelapse Video Processing\logs\session_<date>.log`.
- No modal dialogs on paths reachable from `ui/actions.py` (headless tests would hang); user-initiated file and colour dialogs in widgets are fine.

## JobRunner signals

`started(str kind)`, `progress(str)`, `percent(int)` (−1 = indeterminate), `finished(str kind, object result)`, `failed(str kind, str message)`, `cancelled(str kind)`; plus `busy`, `cancel()`, `wait(ms)`.

## AppContext signals

`datasets_changed(list)`, `active_dataset_changed(object)`, `position_changed(str)`, `mode_changed(str)`, `profile_changed(object)`, `histograms_changed(object)`, `coverage_changed(int, int)`, `compute_all_requested()`, `calibration_changed(object, object)`, `job_state_changed(str, str)`, `job_progress(int)`, `log_line(str, str)`, `results_changed(list)`, `theme_changed(str)`, `time_format_changed(float, str)` (0.6) and `channel_names_changed(object)` (0.7). Calibrations and WHITE medians are cached per dataset root (`ctx.calibrations`, `ctx.white_medians`).

## Data conventions

- Planes are `uint8` 2-D arrays; `read_plane(path, channel)` extracts the channel's RGB plane (`models.CHANNEL_PLANE`).
- Bounds are absolute 8-bit values `{channel: (low, high)}`.
- Positions are keyed by `Frame.roi` (for example `ROI-1s1a`).
- Output folders: `<experiment>/analysis_output/run_<date>_<id>/` and `quick_<date>_<id>/`, created by the engine, never overwriting. 0.6 layout: videos (or fixed-image composites, panels and the quantification CSV) at the top, named `<experiment>_<position>_<kind>`; `montages/`; `masks/`; `info/` with the log, JSON, manifest, frames CSV, dashboard, info bar and `posters/`.
- User data: `%LOCALAPPDATA%\Timelapse Video Processing\` (`settings.json`, `profiles/`, `preview_cache/`, `logs/`); `ETALUMA_DATA_DIR` overrides it.

## Running

```
set DEV=%LOCALAPPDATA%\EtalumaVP-dev\v0.9
set QT_QPA_PLATFORM=offscreen
"%DEV%\.venv\Scripts\python.exe" -m pytest tests -q --basetemp="%DEV%\build\pytest"
"%DEV%\.venv\Scripts\python.exe" -m etaluma_video
```

`--basetemp` avoids a temp folder that some Python builds cannot write to. Since 0.7 the environment and scratch live outside the version folder. `ETALUMA_SAMPLES` points the real-sample tests at the datasets (default `../private/Sample set`).
