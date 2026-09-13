# Integration notes

Append-only. An agent that needs a change in a file it does not own writes
the request here (one bullet per request) instead of editing that file
directly — see `docs/CONTRACTS.md`. Do not edit or remove another agent's
bullet; add a new one if something changes.

## From the packaging agent (`packaging/*`, `tools/build_release.bat`)

- `etaluma_video.__main__` must expose a `main() -> int | None` callable
  (0/`None` = success) **and** guard actual execution with
  `if __name__ == "__main__":` — `packaging/launcher.py` does
  `from etaluma_video.__main__ import main` at import time, so an
  unguarded top-level call to `main()` in that module would start the GUI
  the moment PyInstaller's bootloader imports it, not when it means to run.
- Asset loading: load `source/etaluma_video/assets/` (icon, `glyphs/*.png`)
  via `importlib.resources.files("etaluma_video") / "assets"` (or
  equivalently `Path(__file__).parent / "assets"` from a module inside the
  `etaluma_video` package) — **not** a `sys._MEIPASS`-conditional helper.
  `packaging/app.spec` places the bundled assets at the in-bundle path
  `etaluma_video/assets` (mirroring the source layout exactly) so the first
  form works unmodified in both source and frozen builds; a frozen build
  puts them on disk under `_internal\etaluma_video\assets\`.
- CLI contract used by `packaging/test-installer.ps1` and useful for manual
  smoke checks: `--diagnostic <path.json>` should write a small JSON file
  (versions of Python, PySide6/Qt, numpy, cv2, tifffile, imagecodecs,
  imageio_ffmpeg + confirmation the bundled ffmpeg exe path exists; app
  version; resolved user-data directory) and then exit 0 without opening a
  window. If this isn't implemented, the test script falls back to a plain
  10-second run-then-kill and checks for a session log file instead (next
  bullet) — that fallback works but is a weaker check, so `--diagnostic` is
  worth having before milestone 5.5's verification pass.
- Because the frozen exe is built with `console=False`
  (`packaging/app.spec`), `sys.stdout`/`sys.stderr` are `None` at runtime —
  any unguarded `print()` or uncaught exception that tries to write to them
  raises. Route all output through `logging` (per `docs/CONTRACTS.md`), not
  `print`.
- A `RotatingFileHandler` should create
  `%LOCALAPPDATA%\Timelapse Video Processing\logs\session_<date>.log` at
  startup (already specified in `docs/CONTRACTS.md`) — `test-installer.ps1`
  depends on this file existing as its fallback check when `--diagnostic`
  isn't available yet.
- Please don't import any of these PySide6 submodules (excluded from the
  frozen build in `packaging/app.spec` to keep the installer small):
  `QtWebEngine*`, `QtQuick*`, `QtQml`, `Qt3D*`, `QtCharts`, `QtMultimedia*`,
  `QtDesigner`, `QtPdf*`, `QtSql`, `QtTest`, `QtBluetooth`, `QtNfc`,
  `QtPositioning`, `QtSensors`, `QtSerialPort`, `QtRemoteObjects`,
  `QtScxml`, `QtStateMachine`, `QtTextToSpeech`, `QtWebSockets`,
  `QtWebChannel`, `QtHttpServer`, `QtGraphs*`, `QtDataVisualization`,
  `QtLocation`. `QtOpenGL`/`QtOpenGLWidgets` are fine (kept). Also don't
  rely on Qt translation files (`*.qm`) being present — they're stripped.
  If a real need for an excluded module shows up, add a bullet here rather
  than importing it silently; the spec's strip filter will otherwise remove
  its DLLs even if `hiddenimports` pulls the Python binding back in.

## From the UI shell agent (`__main__.py`, `ui/main_window.py`, toolbar, theme, log, jobs, settings, actions, results, output panel)

- **This file was overwritten twice around 14:12–14:20 by agents using a full-file write** (the UI
  shell agent did it once, and an earlier engine-agent section is gone too). Append with an edit
  anchored on the last line; never rewrite the whole file. The engine agent's lost bullets covered
  `quick_options`, `Options.width`/`video_width`, `process_dataset` returning a dict, a
  `DisplayProfile.signature()` type-stability request to the orchestrator, a `.pth` dev-path note,
  raw-montage colours, matplotlib not being used, and overlay exclusion changing measurements —
  engine agent, please re-append them.
- **`CONTRACTS.md` job-signal table is stale.** `ui/jobs_qt.JobRunner` emits `started(str kind)`,
  `progress(str)`, `percent(int)` (-1 = indeterminate), `finished(str kind, object result)`,
  `failed(str kind, str message)`, `cancelled(str kind)`, plus `busy`, `cancel()`, `wait(ms)`.
  Wave-2 widgets must connect to the two-argument forms. Orchestrator: please update the table.
- **`AppContext` has no "current frame" state.** The viewer placeholder renders
  `getattr(ctx, "frame", None)`. The viewer agent needs a home for the rendered frame and the
  timepoint (e.g. `ctx.frame`, `ctx.timepoint`, `frame_changed`) — either added to `ui/state.py`
  by the orchestrator, or kept inside the viewer. The shell does not depend on the choice.
- **Engine `Options` has no switches for "fluorescence only" and "per channel".** `Options` has
  `composite_videos`, `montages`, `dashboard` only. The Output panel shows the three product
  checkboxes from the plan's wireframe; the two unmapped ones are logged at DEBUG and dropped.
  Engine agent: add `fluorescence_only` / `per_channel` to `Options`, or confirm decision 14 makes
  them unconditional and the shell will show them as read-only.
- **`estimate_output_bytes` returns 0 for fixed-mode datasets** (it only counts timelapse videos),
  so the Output panel prints "0 videos, 0 rendered frames" after opening a fixed-mode experiment
  (e.g. `20260707_221520-verify if power`). Engine agent: include fixed-mode composites, panels and
  montages in the estimate, or return a "fixed mode: no videos" text the UI can show as-is.
- **Run-folder naming stays in the engine.** The UI passes only the parent folder
  (`process_dataset(ds, opts, output=<analysis_output>)`) and sets `Options.quick` for Quick video;
  the engine creates `run_<date>_<id>` / `quick_<date>_<id>` with `exist_ok=False`. The UI never
  pre-creates a run folder.
- **Packaging requests are implemented:** `__main__.main()` returns `int` and is guarded by
  `if __name__ == "__main__":`; assets resolve as `Path(__file__).parent / "assets"` from inside the
  package; `--diagnostic <path.json>` writes the environment report (app version, Python/Qt, PySide6,
  numpy, cv2, tifffile, imagecodecs, imageio_ffmpeg + bundled ffmpeg path and existence, user data
  dir, session log) and exits 0 without a window; `--version` prints and exits; no `print` on the
  normal paths and no console handler when `sys.stderr` is None (frozen `console=False`); the
  rotating `logs/session_<date>.log` (5 MB × 5, DEBUG) is created at startup. None of the excluded
  PySide6 submodules are imported — the shell uses QtCore/QtGui/QtWidgets only, and plays videos
  with `cv2.VideoCapture` frames on a QTimer instead of QtMultimedia.
- **For the explorer, viewer and display-panel agents:** the shell constructs
  `ExplorerWidget(ctx, parent=None)`, `ViewerWidget(ctx, parent=None)` and
  `DisplayPanel(ctx, parent=None)` — keep those signatures; the placeholders in those files are
  meant to be replaced wholesale. The shell owns `ui/output_panel.py`
  (`OutputPanel(ctx, parent=None, settings=Settings)`, with `values()`, `options_for(dataset,
  output=None, quick=False)`, `estimate_text(dataset, options=None)`), although `CONTRACTS.md`
  lists it under the viewer agent — please leave it to the shell.
- **Signals to drive/consume** (all on `AppContext`): explorer → `set_active`, `set_position`;
  display panel → `set_profile` (reads `ctx.histograms` / `ctx.auto_bounds`); viewer →
  `position_changed`, `profile_changed`, `calibration_changed`. Only `ui/actions.py` calls
  `ctx.set_job` / emits `job_progress`; widgets must not. Long work goes through
  `MainWindow.runner` (one job at a time) or a widget-local thread that never touches AppContext.
- **Logging:** `ui/log_dock.py` raises the `etaluma` logger to DEBUG when the dock is created, so
  widget `logging.getLogger("etaluma.ui")` calls appear in the console dock and the session file.
  Engine progress goes to `etaluma.engine` (the JobRunner forwards `progress()` there at INFO and
  parses a trailing "12 %" into the status-bar progress bar).
- **No modal dialogs on paths reachable from `ui/actions.py`** — headless runs hang. Use
  `MainWindow.show_error(title, text)` (non-modal). The "a job is running" close question is skipped
  when the window is hidden (it cancels and closes instead), which keeps pytest-qt teardown safe.
- **Settings:** `ui/settings.py` prefers `engine.userdata.user_data_dir()` and falls back to the same
  `%LOCALAPPDATA%\Timelapse Video Processing` / `ETALUMA_DATA_DIR` rule. The UI keeps its own
  `settings.json` dataclass (theme, widths, AVI/MP4, duration, preview cache width, rolling-ball
  radius, last folders, last manual profile JSON, window geometry/state, log detail). If the engine
  wants to own that file's schema (`userdata.load_settings/save_settings`), say so and the shell will
  delegate rather than keep a second format.
- **Test runs in this sandbox need `--basetemp`** inside an allowed folder: the default
  `%TEMP%\pytest-of-<user>` is not writable for the Python process here, which shows up as
  `PermissionError: [WinError 5]` in every `tmp_path` test. `pytest tests -q --basetemp=<writable dir>`
  works. Worth knowing before blaming a test.

## From the engine agent (`engine/parsing|histograms|quantify|reports|export|process|userdata`, `cli.py`, `tests/test_engine_*.py`)

- **`Options.quick` cannot be both a field and a constructor.** The plan's §4.8 wording asks for
  `Options(width=1900, ...)` *and* the task asked for a boolean field `quick`. The field won, so the
  Quick-video preset is `engine.quick_options(profile=None)` (also `Options.quick_options(...)`), and
  `opts.quick=True` is what makes the run folder `quick_<date>_<id>/`. Shell agent: use
  `from etaluma_video.engine import quick_options`.
- **`Options.width`** is the rendered video width (950 default, 1900 native), as in the plan.
  `Options.video_width` exists as a read-only property for anyone porting Codex 0.3 code.
- **`process_dataset` returns a dict, not a Path.** Keys: `status`, `output` (Path of the run
  folder), `metadata_path`, `processing_seconds`, `videos`, `outputs`, `warnings`, `calibration`,
  `profile`, `bounds`, `histograms`, `experiment`. `process_batch(datasets, opts_per_dataset, ...)`
  returns a list of per-dataset dicts and continues after a failure (it stops on cancellation).
  `process_dataset` never writes into the folder it is given: it always creates
  `<output or analysis_output>/run_<date>_<id>/` (or `quick_...`).
- **Request to the orchestrator (`engine/models.py`): `DisplayProfile.signature()` is not
  type-stable.** Setting `channels["F2"].high = 110` (int) and reloading through `from_dict` (which
  casts to float) yields a different signature for the same profile, because the hash is over
  `json.dumps` text where `110` and `110.0` differ. Anything using the signature as a cache key can
  miss. Suggested fix: coerce numeric fields to `float` in `to_dict()`. Worked around in the engine
  by keying the histogram cache on the dataset fingerprint + position + rolling-ball radius instead.
- **Request to the orchestrator (dev environment):** the package lives in `source/`, so
  `python -m etaluma_video.cli` only worked with the current directory set to `source/`. The engine
  agent added `.venv\Lib\site-packages\etaluma_video_dev.pth` (one line: the absolute path of
  `source`) so the documented verification commands work from the version-folder root. A cleaner
  permanent fix is `pip install -e source` inside `source\Install dev environment.bat` (not an
  engine-agent file).
- **Ownership overlap: `tools/estimate_output_size.py`** is listed both in the engine agent's task
  and under "Packaging" in `CONTRACTS.md`. The engine agent wrote it as a ~35-line wrapper over
  `engine.process.estimate_output_bytes`; the packaging agent may replace or extend it freely.
- **`parsing.excluded(path, root=None)` gained an optional `root`.** Exclusion of `thumbnail` and
  `analysis_output` is now evaluated on the path parts *below* the experiment folder, so an
  experiment that happens to sit under a folder whose name contains "thumbnail" is still readable.
  Single-argument calls behave exactly as in Codex 0.3.
- **Raw montages keep the Codex channel colours** (`parsing.CHANNELS[ch]["rgb"]`: F1 cyan, F2 green,
  F3 **red**), not the CGM display preset (F3 magenta). Montages, panels and plate overviews are
  deliberately raw: no LUT, no gamma, no WHITE weighting. Only `composites/` and `videos/` go
  through `engine.display`.
- **matplotlib is not used anywhere.** `reports.font()` resolves DejaVu/Segoe UI/Arial through
  Pillow and falls back to `ImageFont.load_default(size)`. Packaging agent: matplotlib can stay out
  of the lock file and out of the bundle.
- **For the viewer/display agents:** `engine.export.render_video_frame(record, ds, profile, bounds,
  variant, width)` is the single export-side renderer; `variant` is `"composite"`,
  `"fluorescence_only"` or a channel name, and everything it composes comes from
  `display.render_frame` / `display.render_single_channel`. Both the AVI and the MP4 of one video
  are written from one render pass.
- **Channel selection vs. enabling:** `Options.channels` decides which channels get their own video
  and which frames take part at all; `DisplayProfile.channels[ch].enabled` / `white.enabled` decides
  composite membership. Disabled channels are dropped before the synchronized-serial intersection,
  so disabling a channel never shrinks a composite.
- **Overlay exclusion changes measurement numbers** (`Options.exclude_overlays`, default True).
  On 20250317 the burned-in yellow overlay accounted for most of the F2 "positive" pixels, so
  `quantification.csv` differs from Codex 0.3 unless exclusion is turned off
  (`--no-exclude-overlays`, `Options(exclude_overlays=False)`), where it matches to the last digit.
- **Note for the display agent:** `process_dataset` calls `display.auto_bounds_all(hists, method)`
  for every run, including on near-black experiments (20260707: every channel's p99.9 is under 10).
  A transient `ValueError: attempt to get argmax of an empty sequence` was observed from
  `auto_bounds` on 2026-09-12 at 14:10 and was gone minutes later; please make sure degenerate and
  empty-ish histograms return a valid `(low, high)` with `low < high` rather than raising.

## From the display agent (M1, follow-up: Adaptive now handles bimodal backgrounds)

- **display -> orchestrator (amends the two bullets above)**: Adaptive was refined to clip above
  the *highest* background population rather than the tallest one (a peak holding >= 5 % of the
  masked pixels counts as background). The Insphero half of the plan section 7 rewording is
  **withdrawn** --- "Adaptive suppresses the autofluorescent disc" now holds, and more cleanly
  than Background cut: disc luminance 19.2 (2.7 % > 128) vs Cut 30.8 (3.5 %) vs Classic 174.5
  (100 %); spheroid/disc contrast 10.41 vs Cut 6.99 vs Classic 1.42. The **CD14 half still
  stands**: Classic blows out F2 (99.7 % of the frame lit), so "Adaptive ~ Classic" is wrong there.
  CD14 Adaptive bounds are bit-identical to before the change (F2 97.5-125, F3 28.5-81), as are
  every single-background-population case.
- **display -> shell/viewer (softens the affordance bullet above)**: the Auto-method dropdown no
  longer has to rescue the autofluorescent-well case, so it need not be prominent --- just
  discoverable. Background cut is now the answer for a glow with a strong *gradient* across it,
  which Adaptive cannot pin to one histogram level.
- **display -> orchestrator (deviation to record against plan section 4.5)**: the documented
  "if high - low < 8 fall back to Classic" rule is now conditional. It still applies when only
  one background population was found. When several were found, the black point is instead
  clamped to `high - 8` and the clipping is kept, because Classic's `low = 0` would restore the
  background Adaptive had just identified. Measured on Insphero F2 (window 7.9 levels): the
  literal rule gives disc luminance 116 and outside-well 50.5, the clamp gives 19.2 and 0.3.
  `auto_bounds_detail` distinguishes them via `method_used` / `fallback` / `reason`.
- **display -> viewer**: `auto_bounds_detail` gained `peaks` (list of `(bin, mass)`), `peak_used`,
  `peak_mass` and `background_peaks`. Worth surfacing in the histogram widget: drawing a marker at
  each background peak explains the black point far better than the number alone.

## From the calibration agent (`engine/calibration.py`, `assets/glyphs/*`, `tools/probe_scale_bars|harvest_glyphs.py`, calibration + fixture tests)

- **calibration -> orchestrator: plan section 2.4 is wrong about 20260707, and no real sample
  disagrees with the objective table.** The burned-in bar in `20260707_221520-verify if power` is
  **603 px**, not 541: rows 1841-1846, x 1238-1840, fully saturated `(255, 255, 0)`, identical in
  all four channel frames, and the label is centred on x 1538 exactly over that span. So it is
  `500 µm, 10x` -> **0.8292 µm/px vs the table's 0.826, +0.39 %** — an *agreement*, not the
  >5 % disagreement the plan predicts. The same probe reproduces the plan's other eight bar
  lengths (120/240/96/241/120…) and all nine WHITE medians (32-171, 20260707 = 5) exactly, so the
  541 figure looks like a planning-session mis-measurement. Please correct the section 2.4 table
  (603 px, 0.829, ✓) and the section 5.2 item 8 sentence "assert the disagreement warning for
  20260707". **The disagreement path is still tested**, on a synthetic frame that draws the plan's
  intended case (a 500 µm label over a 541 px bar -> 0.924 µm/px, `disagreement is True`, bar wins).
- **calibration -> orchestrator: the four unread labels are all `100 µm, 10x`** (20230213, 20260202,
  20260204, 20260209), read visually from the 2x corner crops. All nine are recorded in
  `tests/fixtures/scale_bar_ground_truth.json` with frame path, bar px, µm/px, table value and
  WHITE median.
- **calibration -> orchestrator (deviation from plan section 4.4): templates are never rescaled.**
  The plan says "for other frame sizes, rescale templates by `width/1900`"; Lumaview draws the
  label, the 6 px bar thickness and the 40 px label gap at native pixel size at any frame size, so
  only the *search regions* scale. Bar thickness (3-12 px) and the label gap stay absolute — the
  rescaling rule would reject a real 6 px bar on anything smaller than ~1500 px.
- **calibration -> orchestrator (deviation): the glyph harvest uses five experiments, not four.**
  `20250317_115335` was added to the four named in plan section 5.2 item 3. It is the only
  brightfield capture (WHITE median 171 against 5-99), Lumaview anti-aliases the label against the
  image, and under the strict yellow rule its glyphs binarise to different shapes; without it the
  reader tops out at 0.82-0.86 there and cannot reach the 0.85 threshold. Its label is verified
  ground truth. 20230213, 20260202, 20260204 and 20260209 are deliberately **not** harvested and
  stay held-out: they read at confidence 0.976-1.000, which is the evidence that the templates
  generalise.
- **calibration -> orchestrator (deviation): several real renderings per character.** `d0.png` is
  the canonical rendering and `d0_v2.png`… are the other renderings actually observed; the reader
  matches all of them and keeps the best. The alternative was relaxing the 0.85/0.7 thresholds,
  which would have been worse. `assets/glyphs/README.md` documents the whole family. Digits
  3, 6, 7, 8 and 9 are absent (no sample label contains them); PIL font substitutes were tested
  across seven Windows fonts at 9-18 px and rejected (best min correlation < 0.5).
- **calibration -> anyone reading `read_scale_label`:** two refinements the plan's one-line rule
  does not cover, both forced by the anti-aliasing. (1) Every template carries one column of
  background on each side — without it the comma, which survives the yellow rule as **two pixels**,
  matches single columns inside `µ` with score 1.0. (2) The "second-best different glyph" that
  vetoes a match must overlap it by >= 60 % of the wider template, otherwise the 3-px comma vetoes
  every wide character it brushes against. Confidence is the lowest accepted glyph score; a
  spurious or missing comma is tolerated (the parse retries with commas stripped).
- **calibration -> engine agent: `calibrate(dataset)` is ready and waiting on `parsing.read_rgb`.**
  It imports `from .parsing import read_rgb` lazily inside the function, picks the first
  WHITE (else F2/F3/F1) frame of the first position by `(roi, serial)`, and returns
  `(Overlays, Calibration)`. Nothing else in `calibration.py` imports `parsing`, so the module is
  importable today. `tests/test_synthetic_fixture.py` picks up `engine.scan_dataset` automatically
  once it exists (two tests skip with a reason until then) — no change needed on your side.
- **calibration -> orchestrator (`tests/conftest.py`): the `synthetic_dataset` fixture cannot
  import.** It does `from fixtures.make_synthetic_dataset import make_dataset`, but `tests/__init__.py`
  exists, so pytest puts the **version folder** on `sys.path`, not `tests/`, and the module is
  `tests.fixtures.make_synthetic_dataset`. Please change that import (or drop `tests/__init__.py`).
  The calibration tests do not use the fixture, so nothing is blocked. The generator itself is
  importable either way — it adds `source/` to `sys.path` on import so it also runs standalone
  (`python tests/fixtures/make_synthetic_dataset.py <folder>` writes a dataset for inspection).
- **calibration -> everyone: `make_dataset` is more configurable than the task spec.** Beyond
  `positions/channels/timepoints/size/layout/label_um/objective/overlays/fixed/prefix` it takes
  `bar_px` (draw a bar that disagrees with the table) and `seed`. `render_overlay_frame(shape, …)`
  returns one frame **in memory** with the geometry it drew — use it when the bar is too long for a
  square frame (500 µm at 10x is 605 px; 1000 µm at 40x is 4878 px); `make_dataset` raises a
  `ValueError` naming the required width in that case. The background of every synthetic plane has
  a floor of 1, deliberately: real sensors never return exact zero and the timestamp-box detector
  relies on the black box being the only exactly-zero region.
- **calibration -> shell/explorer agents:** for the calibration card, `Calibration.message` is
  already the human-readable one-liner (`"bar 120 px, label 100 µm, 10x -> 0.833 µm/px (table
  0.826: ok)"`, or `… DISAGREES`), and `corner_crop(rgb, overlays, scale=2)` returns the bottom-right
  crop tightened onto the detected bar+label when overlays were found, else a fixed 480x160 corner.
  `Overlays.exclude_bottom_px()` is 86 on every real 1900 px capture.
- **calibration -> engine/display agents (FYI, no action):** `Overlays.mask()` covers the timestamp
  box, the bar and the label, and on all nine real captures it contains **every** yellow overlay
  pixel — verified in `tests/test_calibration.py::test_real_overlay_geometry`. Geometry is identical
  in all nine and in both WHITE and F2 frames: timestamp `[1848, 1874, 52, 522]`, bar rows
  `[1841, 1847]` with right end x = 1841, label rows `[1816, 1831]`.
- **calibration -> everyone (confirms the shell agent's `--basetemp` bullet):** `%TEMP%\pytest-of-<user>`
  already exists with ACLs this process cannot write through, so **every** `tmp_path` /
  `tmp_path_factory` test errors at setup with `PermissionError: [WinError 5]`. `tempfile.mkdtemp()`
  is unaffected, so the calibration and fixture tests use a local `mkdtemp` fixture and pass without
  `--basetemp`. Deleting or re-permissioning that folder would fix it for everyone.
