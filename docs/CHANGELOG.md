# Changelog

## 0.9.0 (2026-09-13)

The first public release, in a new GitHub repository (the short-lived 0.8.0 upload was withdrawn). The app is 0.8.0 as renamed Timelapse Video Processing (below), with:

- Release files with fixed names, without spaces or version numbers: `Timelapse-Video-Processing-Setup.exe`, `Uninstall-Timelapse-Video-Processing.exe` and one `SHA256SUMS.txt`, made by `tools/release_assets.py`. GitHub had turned the spaces in 0.8.0's file names into dots, so its checksum files no longer matched them.
- A download link at the top of the README that always points at the newest installer. Installers live on the Releases page only: GitHub does not accept files over 100 MB in the code.

## 0.8.0 (2026-09-13)

Unattended batch work, presets shared by Quick video, Process and the queue, a manual update check, and the first release on GitHub.

### Queue
- A **Queue** button beside Open folder (and "Add folders to the queue" on the welcome page) opens a page of experiment folders processed one after another. A folder of experiments adds each one inside it; folders can be dragged in from Explorer and rows dragged into order.
- Each item has a display choice and an output preset; channel names, start time and timepoint range come from each experiment's memory.
- A failed item is marked and the queue moves on; a full disk pauses it. Pause after this item, Skip current, Cancel. A summary on the page and a text report (`%LOCALAPPDATA%\Etaluma Video Processing\queue_reports`).
- Outputs in each experiment's `analysis_output`, or in one common folder with a subfolder per experiment.
- The queue has its own worker: folders can be opened and previewed while it runs, and Quick video and Process add to it.
- The queue is saved after every change. Closing the app asks whether to stop after the current item or at once; the next start offers Resume or Clear. A run folder left unfinished is renamed `…_incomplete`.
- The app never touches sleep, shutdown or other Windows settings.

### Presets and display choices
- "Auto" is called **Auto-normalised** everywhere: it is a linear stretch between histogram bounds, not histogram equalisation.
- Output presets: Quick, Standard and Presentation built in; save, rename and delete your own. A Preset row at the top of the Output panel shows "Current (modified from …)" after any change.
- Quick video is a split button: the arrow picks the display (Auto-normalised, Remembered for this experiment, Current display settings or a saved profile) and the output preset, remembered between sessions. Quick video now follows its preset (0.7 copied the scale bar, key and quality from the Output panel) and exports the experiment's timepoint range.
- Single-channel videos are on for every channel until a choice is saved, as in the Standard preset.
- Rolling-ball background subtraction stays off by default.

### New name and credits
- The app is now called **Timelapse Video Processing** (for Etaluma LS720 captures), and the GitHub repository `timelapse-video-processing`. Installing it removes an installed "Etaluma Video Processing", keeping its settings; the first start copies the settings, display profiles, output presets, per-experiment memory and queue from `%LOCALAPPDATA%\Etaluma Video Processing` (the preview and histogram caches are rebuilt).
- Credit: "Developed by BIOMIS Team, SATIE laboratory, ENS Paris-Saclay". No personal names in the app, the installer or the repository; the licence names "the Timelapse Video Processing contributors".
- "For research use only; not for diagnostic or clinical use" in the About box, the README and the release notes.
- The third-party notices now describe the bundled FFmpeg correctly: a GPL-3.0 build (with x264 and x265), run as a separate program, with the licence text, the source addresses and an offer to provide the source.

### Updates and publishing
- Help → Check for updates asks GitHub for the latest release and offers its page. Only when clicked: the app never checks by itself and downloads nothing.
- README screenshots made from synthetic demo data (`tools/make_screenshots.py`); release notes for the first public release.

### Verification
- 296 automated tests pass, 16 of them new: presets and display choices, the Output panel preset row, the Quick video menu, the queue (order, a failing folder, skip, stop and resume, recovery after a crash, the page and the toolbar), and the update check against fake GitHub answers (newer, same, none, offline, rate limit; nothing at start).
- Three real labware sample sets through the queue into one common folder: a fixed-image set (composites and panels), a timelapse with the Quick preset (16 MP4) and one with Presentation (composite only); 16 s in all.
- Installer 101.7 MB (SHA-256 50daaeca…), standalone uninstaller 56 kB (315fd062…); `test-installer.ps1` passed every check; the installed 0.7 and its settings were put back afterwards. The frozen app bundles SSL for the update check.
- The exported public repository (143 files) passes the privacy scan and, on its own, 267 tests (29 need real captures and skip).

## 0.7.0 (2026-09-13, in testing)

Looks, stability, small improvements, and readiness for a public GitHub repository, after 0.6 was confirmed to work well.

### Folders
- Any position name: Lumaview's labware wells (`Wella1`, `Welli11`) and custom names, in a folder per position, a folder per channel inside it, or every file in the experiment folder. 0.6 knew only `ROI-` names and refused five of six new sample layouts.
- Labware-named positions are numbered in capture order (from the file times); their protocols list no stage coordinates, so one note says the stage map leaves them out instead of one warning per position.
- Batch discovery recognises experiments with well folders.

### Look
- New Terracotta theme, the default: a dark theme with near-black panels, cream text and a terracotta accent; the image canvas stays near-black. Dark and Light remain; a saved "dark" from earlier versions (the old default) becomes Terracotta once.
- Toolbar icons redrawn as one set of line icons in the theme's colours, the two run buttons in the accent.
- A welcome page while nothing is open: open a folder, drop one, or pick a recent folder (also under File → Open recent).
- Display, Output and "On the videos" are collapsible sections, remembered between sessions, so the right panel fits without scrolling.
- Time remaining beside the progress bar; explorer names keep their width (details are shortened instead, no sideways scrollbar); histograms, view chips and result tiles follow the theme.
- About box with the credit, the disclaimer, the licence and links to the project page and the log folder; folders are shown as `%LOCALAPPDATA%\\…`, never with the Windows user name, and crash reports replace it the same way.

### New
- Channel names, typed in the Output panel (for example F2 = CFDA): on montage rows, in an optional colour key drawn on the videos, and in the app's own labels.
- Timepoint range: export only timepoints *first* to *last*; the Auto display still uses every timepoint.
- Each experiment remembers its display profile, channel names, start time, days mode and range, and gets them back when reopened.

### Stability
- A crash window for unexpected errors from any thread, with "Copy report" (versions and traceback, no image data) and the log folder.
- An unreadable TIFF in a long run becomes a black frame with a warning instead of ending the run, and is left out of the Auto measurement.
- Clear messages when the output folder cannot be created or the disk is full; the run is marked incomplete.
- The preview holds at most a quarter of the PC's memory (0.5 to 6 GB), shrinking a very large position rather than running out.
- Stress tests: rapid switching of experiments and positions, unreadable files, full disk, the crash window.

### Credits, privacy, publishing
- A credit line in the About box, README, licence, installer publisher field and file version; disclaimer that the project is not affiliated with Etaluma, Inc.
- Personal names removed from code comments, docs and synthetic test files; `tools/scrub_check.py` (and a test) fail on user-profile paths, data-drive paths and e-mail addresses.
- The development environment and build scratch moved out of the version folder (`%LOCALAPPDATA%\EtalumaVP-dev\v0.7`).
- README rewritten for any reader; `docs/DEVELOPING.md` (structure, setup, tests, common changes, build, releases); `docs/PUBLISHING.md` (what is public, export, private-first repository, releases); `tools/make_demo_dataset.py` (synthetic experiment to try the app); `tools/export_public_repo.py` (clean repository folder, privacy scan, one local commit, nothing pushed); GitHub test workflow and issue template.

### Command line
- `--channel-name CH=NAME` (repeatable), `--channel-key`, `--timepoints FIRST LAST`.

### Verification (2026-09-13, development PC)
- 279 automated tests pass (parsing of every layout, the 8-bit renderer against the float reference, overlays, exports, the interface offscreen, stability and release checks).
- Installer 101.6 MB, standalone uninstaller 56 kB; `packaging	est-installer.ps1` passed every check (install, installed diagnostic, reinstall, uninstall keeping settings, uninstall removing everything); the version installed before and its settings were put back afterwards.
- The frozen app opened a labware sample with a folder per well and a folder per channel: scale bar read (500 µm, 10x, 0.829 µm/px), preview in 0.4 s, every position measured in 1.0 s.
- The exported public repository folder (130 files) passes the privacy scan and, on its own, the test suite (250 passed; the 29 tests that need real captures skip, as they will on GitHub).
- Not verified: the GitHub workflow (it runs once the repository exists), and a clean Windows PC.

## 0.6.0 (2026-09-12, in testing)

A real run of 0.5 on CD14 (6 positions, 196 timepoints, 3 channels, 1900 px MP4, rolling ball 50 px, data on a hard disk) took 273 s: 74 s measuring intensities and 189 s writing the videos. Other feedback: the log under the preview only, Save as for the log, a results page that became heavy after a run, montages that did not follow the display, videos at the top of the run folder, and the app's time and scale bar, which could not be ticked. Plan and decisions: `../../docs/plans/v0.6_plan.md`.

### Speed
- Intensity measurement reads every 8th timepoint of every position at full size (at least 24 per position) instead of every image: 74 s to 8 s on CD14, with bounds within one gray level of the full pass. It starts in the background as soon as a folder opens and is kept on disk, so Process usually finds it done.
- Manual display skips the measurement entirely.
- x264 uses the machine's cores; 0.5 pinned it to 2 threads, about 14 frames per second at 1900 px.
- Whole timepoints (decode, rolling ball once per plane, every video, overlays) are rendered in parallel and in 8-bit arithmetic (`display.render_frame_fast`); the float renderer moved about 300 MB of memory per 1900 px frame. One 1900 px CD14 position: 30 s to 20 s.
- MP4 quality presets: High (CRF 18, 0.5's setting), Standard (CRF 23, the default, about half the size), Small (CRF 28).
- The preview loads a position on several threads, every 8th timepoint first, so the whole timeline can be scrubbed within seconds (0.5: 38 s per CD14 position, in order).

Measured on the development PC (6-core i7-8700), CD14 copied to its SSD, composite and fluorescence-only composite, rolling ball 50 px:

| Run | 0.5 | 0.6 |
|---|---|---|
| 1900 px, MP4, first run | 277 s | 134 s |
| 950 px, AVI + MP4, second run (intensities kept) | — | 62 s |
| Quick video (1900 px MP4, time and name on) | — | 127 s |
| Size of the 1900 px run | 2.3 GB | 1.8 GB |

On a 7200 rpm hard disk, reading CD14's 11.4 GB once takes about 75 s, which is the floor there; an SSD and more cores help directly.

### Display
- The preview and the videos use the same Auto bounds, measured at full size over every position. 0.5's preview measured 640 px copies of the positions visited so far (on CD14: F3 white point 32 in the preview, 62 in the video).
- The rolling ball fills Lumaview's burned-in boxes before estimating the background. The black clock box left a bright band beside it, which on CD14 was the brightest F2 feature and set F2's Auto white point (77 in 0.5, 43 now), so F2 now shows about twice as bright.
- The display panel says how many positions the Auto bounds cover while the background measurement runs; "Measure all positions" is no longer needed.

### Videos and outputs
- Time on the videos: `Day d : hh:mm:ss` with d the completed days (days shown when the video reaches 24 h, or always, or never), computed as start time + timepoint × the protocol's interval, with a typed start time for one experiment split over several folders. Drawn over Lumaview's clock, readable at any width. Quick video always shows it.
- Experiment and position name beside the time at the same size (always on in Quick video).
- App scale bar over Lumaview's own, readable at any width. Neither option is greyed out any more.
- Montages are rendered like the videos (bounds, rolling ball, colours): composite, fluorescence-only composite and each channel at 0/25/50/75/100 %, on the app's dark palette, with the time above each column and the display settings underneath. Fixed-image panels and plate overviews likewise. Measurements stay raw.
- Run folder: the videos (or, for fixed images, the composites, panels and quantification CSV) at the top, named `<experiment>_<position>_<kind>`; `montages/`; `info/` for the log, JSON, CSVs, dashboard, info bar and video posters. The fluorescence-only composite is `..._fluorescence.mp4`.

### Interface
- The log sits under the preview only; Explorer and Display & output use the full height.
- Explorer, Display & output and Log collapse to a slim strip on their own edge (button in each header, Ctrl+1, Ctrl+2, Ctrl+3, View menu); click the strip to open the panel again.
- Log: Save writes a new file in the logs folder, Save as asks where. 0.5's Save crashed every time.
- Results page: small posters written by Process instead of decoding half of every video, video frames decoded and shrunk off the interface thread, tiles built once per run, montages hidden until their chip is ticked, Display & output and Log collapsed while it is open and restored on return, and a click on a position in the explorer scrolls to its row.
- Output panel: MP4 quality, Time with start time and days, Experiment and position name, App scale bar.
- The preview's time label uses the videos' format and start time.

### Log
- The per-position, per-channel "captured vs planned" warnings collapse into one line and are no longer repeated at the end of a run.
- Output folders of earlier runs (`analysis_output*`, including `analysis_output_old`) are never scanned as sources.
- Progress-only lines drive the progress bar (weighted: measurement, then videos) and appear in Debug only; the run's processing log leaves them out.
- The size estimate follows the quality preset (0.5 predicted 425 MB for a 2.3 GB run).

### Command line
- `--quality high|standard|small`, `--time-offset`, `--days auto|always|never`, `--name-label`; `--quick` adds the time and the name.

### Verification (2026-09-12, development PC)
- 255 automated tests pass (engine, sampled measurement, overlays and time format, 8-bit renderer against the float reference, output layout, collapsible panels, log save, results page).
- Installer 101.6 MB, standalone uninstaller 56 kB; `packaging	est-installer.ps1` passed every check (install, installed diagnostic, reinstall, standalone uninstaller keeping settings, installed uninstaller removing everything). The test's handling of an absolute `-Setup` path was fixed.
- The frozen app on CD14 (offscreen): scale bar read (100 µm, 10x, 0.833 µm/px), preview position loaded in 10.5 s, all six positions measured in 11.2 s.
- Not verified: a clean Windows machine, and a long Process run started from the installed app (the same engine was timed from the command line on real data).

## 0.5.0 (2026-09-12, in testing)

Feedback on 0.4: processing felt slower than the first versions, histograms needed a log/linear choice, axis values and a current-frame view, the log should not take height from the explorer, per-channel videos should be optional, and results deserved a large view.

### Speed
- Timelapse export in one pass per position (`engine/fastexport.py`): each TIFF is decoded once instead of once per video, on several threads; planes are shrunk to the output width before rendering; every video variant is rendered from the same planes; each video is encoded on its own thread.
- The intensity pass (Auto bounds) decodes on several threads and is cached per position on disk, so a second run on the same data skips it; "Measure all positions" in the display panel fills the same cache.
- The Pillow resize of every finished frame (21 % of 0.4's time) is gone; frames are rendered at the output size.
- Where 0.4's time went on the Diffusion timelapse (225.5 s, profiled): TIFF decoding 91 s (2,514 decodes where 681 suffice), Pillow resizing 48 s, full-size rendering 39 s, MJPG writing 8 s.

Measured on the development PC (6-core i7-8700), Diffusion timelapse, 227 timepoints, three channels:

| Run | 0.4 | 0.5 |
|---|---|---|
| Process, every video (3 channels + 2 composites), AVI + MP4, 950 px | 225.5 s | 29.3 s; 18.1 s when run again |
| Process, composite + fluorescence only, AVI + MP4 | — | 12.2 s |
| Same, MP4 only | — | 11.5 s |
| Quick video, composite + fluorescence only, 1900 px | — | 29.5 s |
| Quick video, every video, 1900 px | — | 45.5 s |

Frames differ from 0.4's by 2.5 gray levels on average (99th percentile 10) because planes are shrunk before rendering instead of after.

### Interface
- Histograms: current-frame outline over the all-timepoints histogram, a selector for either or both, log or linear scale, intensity values on the axis.
- The log sits under the preview and the right panel; the explorer keeps the full window height.
- Results open as a page in the centre: one row per position, one column per video type, large tiles with a still of each video; click to play large.
- Output panel: composite and fluorescence-only composite on by default; one checkbox per channel for single-channel videos, off by default. Quick video uses the same selection.

### Verification (2026-09-12, development PC)
- 253 automated tests pass.
- Installer 101.5 MB, standalone uninstaller 56 kB; `packaging\test-installer.ps1` passed every check (install, reinstall keeping user data, uninstall keeping settings, uninstall removing everything).
- Frozen app on a real capture folder: scale bar read (200 µm, 10x, 0.830 µm/px), preview loaded in 0.5 s.
- Not verified: a clean Windows machine, and a long Process run started from the installed app.

### Fixes
- Background tasks (preview loading, calibration, stills) no longer raise when the window closes while they run.
- Closing the window during a job on a machine without a screen (tests, automation) cancels the job instead of waiting for an answer that cannot come. On a normal screen the app still asks.
- The scan log reports the real number of timepoints for experiments where a position lacks a channel (0.4 printed 0).

### Command line
- `--channel-videos [CH ...]` (no channel: none), `--no-composite`, `--no-fluorescence-only`, `--workers N`.

## 0.4.0 (2026-09-12, in testing)

A new interface on the Codex 0.3 engine. Plan and decisions: `../../docs/plans/v0.4_plan.md`.

### Interface
- Native PySide6 (Qt) desktop app replaces the Streamlit page inside a WebView2 window. One process, no local web server, no browser runtime.
- Non-linear layout: toolbar, explorer, live preview, display and output panel, log console and results, all dockable.
- Explorer shows each experiment and position with thumbnails, the files on disk, the acquisition summary, the stage map and the calibration card as soon as a folder is opened.
- Live preview of every timepoint from an in-memory cache (640 px), first frame in about 1.5 s, re-render in about 10 ms; timeline, playback, zoom, pan, raw values under the cursor.
- Display panel: Auto (Adaptive, Classic, Background cut) or Manual black and white points per channel on the channel's histogram, gamma, colour presets (cyan-green-magenta, blue-green-red, custom), screen/additive/maximum blend, WHITE brightfield and phase-contrast presets with weight, rolling-ball background, saved and loadable profiles, last manual profile restored on start.
- One-click Quick video: every video at 1900 px, MP4, Auto display, no questions.
- Log console with Normal and Debug levels (Debug shows engine calls and parameters), copy, save and a session log file.
- Fixed images mode with the measurement threshold and a per-position or locked display rule.

### Engine
- Objective and pixel size read from Lumaview's burned-in scale bar ("100 µm, 10x") by glyph matching; checked against the LS720 objective table; the bar wins on a disagreement above 5 %. All nine sample experiments read correctly.
- Burned-in timestamp and scale bar are excluded from display histograms and, by default, from measurements.
- Display profile (`display_profile.json`) drives every rendered pixel; preview and export share one renderer.
- Adaptive Auto method: black point above the brightest background population of the histogram, white point at the 99.9th percentile. Suppresses the autofluorescent well of the Insphero sample better than the July p90 cut.
- Composites use screen blending and a dimmed WHITE underlay instead of 0.3's additive merge with WHITE at full weight.
- Every run also writes `calibration.json`; metadata records the display, calibration and overlays.
- Command line gains `--quick`, `--profile`, `--auto`, `--rolling-ball`.

### Packaging
- Per-user NSIS installer without the WebView2 runtime, and a standalone uninstaller that removes settings, profiles, cache and logs unless asked to keep them. Offers to remove an installed Codex 0.3 first.

### Verification (2026-09-12, development PC)
- 243 automated tests pass (synthetic dataset plus the nine real sample experiments).
- Installer about 102 MB (Codex 0.3: 399 MB); standalone uninstaller 56 kB. SHA-256 values are in the `.sha256` files next to each exe.
- `packaging\test-installer.ps1`: silent install, files, shortcuts and registry present; frozen `--diagnostic` passes (Python 3.14, Qt 6.11, bundled FFmpeg found); reinstall keeps user data; standalone uninstaller with "keep settings" removes program, shortcuts and registry and keeps settings; installed uninstaller without it removes everything including user data. All checks passed.
- Frozen app on a real capture folder (Insphero, four channels): TIFFs decoded, scale bar read as 200 µm, 10x → 0.830 µm/px, preview loaded in 0.4 s.
- Not verified: a clean Windows machine without development tools, and a long Process run from the installed app (the same engine was run from the command line on real timelapses).

### Corrections to the plan found while building
- `20260707_221520-verify if power`: the burned-in bar is 603 px, not 541. It reads "500 µm, 10x" → 0.829 µm/px, which agrees with the table. No real sample disagrees; the disagreement path is tested on a synthetic frame.
- The four previously unread labels (20230213, 20260202, 20260204, 20260209) are all "100 µm, 10x".
- "Adaptive ≈ Classic on CD14" does not hold: CD14's F2 background sits near 82, so Classic lights almost the whole frame; Adaptive is the better default there too.
- Glyph templates are not rescaled for other frame sizes (Lumaview draws the label at native pixel size); only the search regions scale.
