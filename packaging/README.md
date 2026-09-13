# Building Timelapse Video Processing 0.4

Windows 10/11 x64 only. The app is a Python 3.12+ x64 / PySide6 native
desktop app, PyInstaller-frozen (one-dir) and wrapped in a per-user NSIS
installer. There is no embedded browser runtime (unlike Codex 0.3's
pywebview/WebView2 window) and nothing here needs admin rights.

Adapted from `history/04_2026-09_codex_v0.3/packaging/`; see that folder's
own README for what 0.3 did differently.

## One-time setup

```powershell
packaging\fetch-tools.ps1
```

Downloads NSIS 3.11 from the same pinned mirror and SHA-256 Codex 0.3 used,
verifies the hash, and extracts it to `.build\tools\nsis-3.11\`. Idempotent
— safe to re-run; it skips the download/extraction if already present and
verified. `.build\` is gitignored.

## Build

```powershell
packaging\build.ps1
```

Or double-click `tools\build_release.bat`, which runs `fetch-tools.ps1`
first if `makensis.exe` isn't present yet, then `build.ps1`.

`build.ps1` parameters:

| Parameter | Default | Effect |
|---|---|---|
| `-Python <path>` | `.venv\Scripts\python.exe` (next to this folder) | Interpreter to build with. Must be 64-bit Python 3.12+. |
| `-Install` | off | Also run `pip install -r source\requirements.lock.txt` into `-Python` first. Omit to build with the venv exactly as it already is. |
| `-SkipTests` | off | Skip the `pytest tests -q` step. |
| `-SkipInstaller` | off | Stop after the PyInstaller freeze; don't build the NSIS installer/uninstaller or touch the version folder root. |

Steps, in order: verify the interpreter; (`-Install` only) install the lock
file; `pytest tests -q` headless (`QT_QPA_PLATFORM=offscreen`) — a test
failure fails the whole build; regenerate `THIRD_PARTY_NOTICES.txt`
(`prepare_assets.py`); PyInstaller one-dir freeze
(`app.spec`) into `dist\Timelapse Video Processing\`; `makensis` on
`installer.nsi` and `uninstaller.nsi`; copy both resulting `.exe` files from
`dist\` to the version folder root and write a `.sha256` sidecar next to
each (`<lowercase sha256>  <filename>`, `sha256sum`-compatible).

`dist\` and `.build\` are created under the version folder root and are
gitignored. **Binaries are never committed** — the two `.exe` files (and
their `.sha256` sidecars) at the version folder root are build output for
local use and GitHub Releases, per `docs/plans/v0.4_plan.md` decision 19
and milestone 5.6.

## Outputs

At the version folder root (sole exceptions to "only README.md here" —
decision 19):

- `Timelapse Video Processing Setup 0.4.exe` (+ `.sha256`) — the installer.
- `Uninstall Timelapse Video Processing.exe` (+ `.sha256`) — standalone
  uninstaller, for a user who lost their Start Menu/Desktop shortcuts.

Installer behaviour: per-user (`RequestExecutionLevel user`, no UAC
prompt), installs to `$LOCALAPPDATA\Programs\Timelapse Video Processing`,
Start Menu + Desktop shortcuts, HKCU uninstall registry entry
(`...\Uninstall\TimelapseVideoProcessing`). Detects a prior "Etaluma Codex"
(0.3) per-user install and offers (default **No**, including when silent)
to remove it first. Its own uninstall has one optional component, unchecked
by default: **"Keep my settings, profiles and logs"** — leaving it
unchecked (the default) removes `%LOCALAPPDATA%\Timelapse Video Processing`
(`settings.json`, `profiles\`, `preview_cache\`, `logs\`); checking it
leaves that folder alone. Neither the installer, the installed uninstaller,
nor the standalone uninstaller ever touch any `analysis_output` folder,
anywhere — those live inside the user's own dataset folders, never under
`$INSTDIR` or `$LOCALAPPDATA`.

Silent flags: `Setup.exe /S` (silent install); `Uninstall.exe /S` (the
installed uninstaller, and the standalone one) defaults to **removing**
settings; add `/KEEPSETTINGS=1` to keep them. The standalone uninstaller
also accepts running with nothing installed — it reports that and exits 0
rather than erroring.

## Test

```powershell
packaging\test-installer.ps1 -Setup "..\Timelapse Video Processing Setup 0.4.exe" -ReportTag smoke
```

Installs silently into the real per-user location, checks files/shortcuts/
registry, exercises `--diagnostic` (or falls back to a session-log check if
the build doesn't support it yet), reinstalls and checks user data
survived, uninstalls via the **standalone** uninstaller with
`/KEEPSETTINGS=1` and checks settings survived, reinstalls again, then
uninstalls via the **installed** `Uninstall.exe` (removing settings) and
checks `%LOCALAPPDATA%\Timelapse Video Processing` is gone. Writes a JSON
report to `.build\installer[-<ReportTag>].json`.

Refuses to run against an already-installed copy, or over existing real
user data, unless `-Force` — read this script's own header comment before
ever passing `-Force`; it installs into the real per-user profile of
whatever machine runs it (there is no admin-required location to sandbox
this in) and can end with a real prior installation fully uninstalled.
Prefer running it on a disposable Windows 10/11 VM, per
`docs/plans/v0.4_plan.md` milestone 5.5's verification note.

## Tool versions

- NSIS 3.11 (`packaging\fetch-tools.ps1`; pinned SHA-256, same source
  Codex 0.3 used: `github.com/tauri-apps/binary-releases`).
- PyInstaller 6.22.2, pinned in `source\requirements.lock.txt`.
- Python: 3.12+ 64-bit required; the packaging venv used during
  development was 3.14.6 64-bit (`.venv\Scripts\python.exe`).

## Files in this folder

| File | Purpose |
|---|---|
| `app.spec` | PyInstaller one-dir build spec. See its module docstring for the asset-loading contract (`importlib.resources`) and how PySide6 is trimmed. |
| `launcher.py` | PyInstaller entry-point shim; imports `etaluma_video.__main__.main()`. |
| `version_info.txt` | Windows version resource embedded in the frozen exe. |
| `installer.nsi` | Per-user NSIS installer. |
| `uninstaller.nsi` | Standalone uninstaller NSIS source. |
| `prepare_assets.py` | Regenerates `THIRD_PARTY_NOTICES.txt` from the venv's installed packages. No third-party dependencies. |
| `build.ps1` | Orchestrates the whole build (see above). |
| `fetch-tools.ps1` | One-time NSIS download/verify/extract. |
| `test-installer.ps1` | End-to-end installer/uninstaller smoke test. |
| `THIRD_PARTY_NOTICES.txt` | Generated by `prepare_assets.py`; bundled into the frozen app. Not hand-edited. |

## What's still pending (as of this packaging pass)

This toolchain was written while the application itself (`source/etaluma_video/`)
was still being built by other agents in parallel — `__main__.py` did not
exist yet. Nothing here has run a real PyInstaller build or a real
installer build; see `docs/INTEGRATION_NOTES.md` for the application-side
contract this packaging depends on (`main()`, `--diagnostic`, asset
layout). Once the application exists, run, in order:

```powershell
packaging\fetch-tools.ps1        # if not already done
packaging\build.ps1              # -Install on the very first run
packaging\test-installer.ps1 -Setup "..\Timelapse Video Processing Setup 0.4.exe" -ReportTag <tag>
```
