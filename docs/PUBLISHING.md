# Publishing on GitHub

The strategy agreed for 0.7, first release 0.9.0: a new, clean public repository that holds only the app; the maintainer's workspace (older versions, development history, private data) stays on the lab PC. Releases carry the installers.

## What goes public, what stays private

| Public repository `timelapse-video-processing` | Stays private |
|---|---|
| `README.md`, `LICENSE`, `source/`, `tests/` (synthetic data only), `tools/`, `packaging/`, `docs/` | the capture data and every sample set (only synthetic data is published) |
| `.github/` (test workflow, issue template), `.gitignore` | the workspace's earlier versions (`history/`, `v0.4` to `v0.6` folders) and its plans |
| installers and checksums, as Release assets only | the development environment and build scratch (`%LOCALAPPDATA%\EtalumaVP-dev`, `EtalumaVP-build`) |

The credit is in the About box, the README, the licence and the installer: developed by BIOMIS Team, SATIE laboratory, ENS Paris-Saclay (no personal names anywhere; the licence names "the Timelapse Video Processing contributors"); the disclaimer says the project is not affiliated with Etaluma, Inc. Contact goes through GitHub issues; no e-mail address is published.

## 1. Export and check

```text
python tools/export_public_repo.py
```

builds `public repo/timelapse-video-processing/` beside the version folder: every tracked file of the version folder except installers, plus `LICENSE`, `.gitignore` and `.github/`. It runs the privacy scan (`tools/scrub_check.py`: Windows user paths, data-drive paths, e-mail addresses) and stops if anything is found; otherwise it makes a local git repository with one commit, authored with the GitHub no-reply identity. Nothing is pushed. `--replace` rebuilds an earlier export; `--no-git` skips the commit.

Before going further, read the exported folder once: README, About text in `source/etaluma_video/__init__.py`, `docs/`.

## 2. Create the repository, private first

Install the GitHub CLI once (`winget install --id GitHub.cli`), then sign in yourself in a terminal with `gh auth login` (the browser asks for your GitHub account; no password or token is stored in any file here):

```text
gh repo create alantherajjeffrey/timelapse-video-processing --private --source "public repo/timelapse-video-processing" --remote origin --push
```

Check on GitHub that the files and the README look right and that the `tests` workflow passes (Actions tab). Then make it public:

```text
gh repo edit alantherajjeffrey/timelapse-video-processing --visibility public --accept-visibility-change-consequences
```

and turn on Issues (Settings → General → Features) if it is off.

## 3. Publish a release

1. Build on the lab PC: `packaging\build.ps1` (tests, frozen app, installer, uninstaller, SHA-256 files at the version folder root). Optionally `packaging\test-installer.ps1 -Setup "<full path to the Setup exe>"`.
2. Tag and upload from the repository folder:

```text
python tools/release_assets.py "%TEMP%\etvp-release"
gh release create v1.0.0 "%TEMP%\etvp-release\Timelapse-Video-Processing-Setup.exe" "%TEMP%\etvp-release\Uninstall-Timelapse-Video-Processing.exe" "%TEMP%\etvp-release\Timelapse-Video-Processing-Windows.zip" "%TEMP%\etvp-release\SHA256SUMS.txt" --target main --title "Timelapse Video Processing 1.0.0" --notes-file docs/RELEASE_NOTES_1.0.0.md
```

Since 1.0 the installer, the uninstaller and SHA256SUMS.txt are also committed at the top of the public repository (the zip only goes to the release): copy them from `%TEMP%\etvp-release` into `public repo\timelapse-video-processing\` before the commit of step 4. Each must stay under GitHub's 100 MB file limit (`tools/release_assets.py` refuses a bigger one).

The release files get fixed names without spaces or version numbers (GitHub would turn spaces into dots), so the README's link `releases/latest/download/Timelapse-Video-Processing-Setup.exe` always gives the newest installer. The installer is about 100 MB: fine as a release file (2 GB limit), too big for the code itself (100 MB limit), which is why it is never committed. Help → Check for updates reads the repository's latest release, so publish each version as a normal release (not a draft or pre-release) tagged `vX.Y.Z`.

## 4. Later versions

Build the next version in its own folder as usual, then refresh the public repository without losing its history:

```text
python tools/export_public_repo.py "%TEMP%\etvp-export" --no-git
robocopy "%TEMP%\etvp-export" "public repo\timelapse-video-processing" /MIR /XD .git
cd "public repo\timelapse-video-processing" && git add -A && git commit -m "Timelapse Video Processing 0.9" && git push
```

and publish a release as in step 3.

## Before making it public: checklist

- [ ] `python tools/scrub_check.py --git` is clean (the tests run it too).
- [ ] The README describes the current version; the About box shows the credit and the disclaimer.
- [ ] No real images or experiment folders anywhere in the repository.
- [ ] The `tests` workflow is green on GitHub.
- [ ] Issues are enabled; the release has the installer, the uninstaller and both `.sha256` files.
