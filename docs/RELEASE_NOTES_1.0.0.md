Timelapse Video Processing 1.0.0: a Windows desktop app that turns Etaluma LS720 microscope captures into timelapse videos, channel composites, montages and simple measurements.

**Install:** the easiest is `Timelapse-Video-Processing-Windows.zip` below: unpack it and double-click `Timelapse-Video-Processing-Setup.exe` inside (README.txt has the steps). The installer is also below on its own, and at the top of the repository. It installs for the current Windows user, with no administrator rights, no Python and no internet needed. The installer is not code-signed, so Windows SmartScreen may say "Windows protected your PC": click **More info**, then **Run anyway**. `Uninstall-Timelapse-Video-Processing.exe` removes it again, and `SHA256SUMS.txt` holds the checksums of both. A build installed under the earlier name "Etaluma Video Processing" is replaced and its settings kept.

New in 1.0: a smaller installer (84 MB instead of 102 MB), made by leaving out components the app never used, so it also fits in the repository itself.

What it does:

- Opens Lumaview capture folders as they come off the microscope, whatever the folder layout.
- Live preview of every position and timepoint, with each channel's histogram.
- **Auto-normalised** contrast: black and white points per channel from the whole experiment's histograms, the same for every position and timepoint, so brightness stays comparable (or set them by hand).
- One-click videos: composites with brightfield or phase contrast, fluorescence only, single channels, with elapsed time, names and a scale bar drawn on; montages, and simple measurements on the raw pixel values.
- **Queue**: a list of experiment folders processed one after another, unattended, with a report at the end.
- **Output presets** (Quick, Standard, Presentation and your own) for Quick video, Process and the queue.
- **Help → Check for updates**, only when you click it.

The full list of changes is in `docs/CHANGELOG.md`.

Developed by BIOMIS Team, SATIE laboratory, ENS Paris-Saclay. For research use only; not for diagnostic or clinical use. Not affiliated with, endorsed by or supported by Etaluma, Inc. MIT licence; the bundled FFmpeg is a separate GPL-3.0 program (see THIRD_PARTY_NOTICES.txt in the install folder).
