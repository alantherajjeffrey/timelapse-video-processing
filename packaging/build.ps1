<#
.SYNOPSIS
    Build Timelapse Video Processing 0.8: tests, PyInstaller freeze, NSIS
    installer + standalone uninstaller, SHA-256 sidecars.

.DESCRIPTION
    Adapted from history/04_2026-09_codex_v0.3/packaging/build.ps1. Run
    packaging\fetch-tools.ps1 first (once) to obtain makensis. Steps:
      1. Verify -Python is a 64-bit Python 3.12+.
      2. (only with -Install) pip install -r source\requirements.lock.txt
         into that interpreter. Default: use the venv as it already is.
      3. pytest tests -q, headless (QT_QPA_PLATFORM=offscreen), unless
         -SkipTests. A test failure fails the whole build.
      4. Regenerate packaging\THIRD_PARTY_NOTICES.txt (prepare_assets.py).
      5. PyInstaller one-dir build (packaging\app.spec) into
         dist\Timelapse Video Processing\.
      6. (unless -SkipInstaller) makensis on installer.nsi and
         uninstaller.nsi, then copy the two resulting exes from dist\ to
         the version folder root and write "<file>.sha256" sidecars next
         to each (format: "<lowercase sha256>  <filename>",
         sha256sum-compatible).
    Scratch (pytest, NSIS) lives in %LOCALAPPDATA%\EtalumaVP-dev\v1.0\build, or $env:ETALUMA_DEV_DIR (this
    script Push-Location's there) and are gitignored.

    Every external tool invocation is followed by an explicit $LASTEXITCODE
    check — $ErrorActionPreference = "Stop" only catches terminating
    PowerShell/cmdlet errors, not a non-zero exit code from a native EXE, so
    without this a failed pytest/PyInstaller/makensis run would be silently
    ignored and the build would appear to succeed.

.PARAMETER Python
    Path to the Python interpreter to build with. Default:
    "%LOCALAPPDATA%\EtalumaVP-dev\v1.0\.venv\Scripts\python.exe" (or $env:ETALUMA_DEV_DIR\.venv).

.PARAMETER Install
    Also run "pip install -r source\requirements.lock.txt" into -Python
    before building. Omit to build with the venv exactly as it is.

.PARAMETER SkipTests
    Skip the pytest step.

.PARAMETER SkipInstaller
    Stop after the PyInstaller freeze; do not build the NSIS installer or
    standalone uninstaller, and do not touch the version folder root.

.EXAMPLE
    packaging\build.ps1
    packaging\build.ps1 -Install
    packaging\build.ps1 -SkipTests -SkipInstaller
#>
[CmdletBinding()]
param(
    [string]$Python,
    [switch]$Install,
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$packagingDir = $PSScriptRoot
$appRoot = Split-Path $packagingDir -Parent
$devRoot = if ($env:ETALUMA_DEV_DIR) { $env:ETALUMA_DEV_DIR } else { Join-Path $env:LOCALAPPDATA "EtalumaVP-dev\v1.0" }  # .venv and build scratch live outside the version folder

if (-not $Python) {
    $Python = Join-Path $devRoot ".venv\Scripts\python.exe"
}
$Python = [IO.Path]::GetFullPath($Python)

function Invoke-Checked {
    param(
        [Parameter(Mandatory)][string]$Description,
        [Parameter(Mandatory)][scriptblock]$Action
    )
    Write-Host "==> $Description"
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed (exit code $LASTEXITCODE)."
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python not found at '$Python'. Run 'source\Install dev environment.bat' first, or pass -Python <path to python.exe>."
}

Write-Host "==> Checking Python version and architecture: $Python"
& $Python -c "import sys, struct; ok = sys.version_info[:2] >= (3, 12) and struct.calcsize('P') * 8 == 64; sys.exit(0 if ok else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "'$Python' must be a 64-bit Python 3.12 or later."
}
& $Python --version

Push-Location $appRoot
try {
    if ($Install) {
        Invoke-Checked "Installing dependencies from source\requirements.lock.txt" {
            & $Python -m pip install -r "source\requirements.lock.txt"
        }
    }

    if (-not $SkipTests) {
        $env:QT_QPA_PLATFORM = "offscreen"
        Invoke-Checked "Running pytest tests -q" {
            & $Python -m pytest "tests" -q --basetemp (Join-Path $devRoot "build\pytest")
        }
    } else {
        Write-Host "==> Skipping tests (-SkipTests)"
    }

    Invoke-Checked "Regenerating THIRD_PARTY_NOTICES.txt" {
        & $Python "packaging\prepare_assets.py"
    }

    # The version folder's own path is long (spaces, nested folders). PyInstaller bundles files such as
    # numpy's dist-info\licenses\...\pythoncapi-compat\COPYING that would then exceed Windows' 260-character
    # limit, which makensis cannot read. Freeze into a short staging folder instead; only the two
    # finished .exe files are copied back to the version folder root.
    $stageRoot = Join-Path $env:LOCALAPPDATA "EtalumaVP-build"
    $distPath = Join-Path $stageRoot "dist"
    $workPath = Join-Path $stageRoot "pyinstaller"
    Write-Host "Staging folder: $stageRoot"
    Invoke-Checked "Freezing the application with PyInstaller" {
        & $Python -m PyInstaller --noconfirm --clean --distpath $distPath --workpath $workPath "packaging\app.spec"
    }

    $frozenExe = Join-Path $distPath "Timelapse Video Processing\Timelapse Video Processing.exe"
    if (-not (Test-Path -LiteralPath $frozenExe)) {
        throw "Expected '$frozenExe' after the PyInstaller step but it is missing."
    }
    Write-Host "Frozen app: $frozenExe"

    if ($SkipInstaller) {
        Write-Host "==> Skipping installer/uninstaller build (-SkipInstaller)"
        Write-Host "==> Build complete (frozen app only)."
        return
    }

    $makensis = Join-Path $devRoot "build\tools\nsis-3.11\makensis.exe"
    if (-not (Test-Path -LiteralPath $makensis)) {
        throw "makensis not found at '$makensis'. Run packaging\fetch-tools.ps1 first."
    }

    $builtSetup = Join-Path $distPath "Timelapse Video Processing Setup 1.0.exe"
    $builtUninstall = Join-Path $distPath "Uninstall Timelapse Video Processing.exe"
    $payload = Join-Path $distPath "Timelapse Video Processing"
    Invoke-Checked "Building the installer (installer.nsi)" {
        & $makensis /V2 "/DDIST_DIR=$payload" "/DOUTFILE=$builtSetup" (Join-Path $packagingDir "installer.nsi")
    }
    Invoke-Checked "Building the standalone uninstaller (uninstaller.nsi)" {
        & $makensis /V2 "/DOUTFILE=$builtUninstall" (Join-Path $packagingDir "uninstaller.nsi")
    }
    foreach ($built in @($builtSetup, $builtUninstall)) {
        if (-not (Test-Path -LiteralPath $built)) {
            throw "Expected '$built' after the NSIS step but it is missing."
        }
    }

    $rootSetup = Join-Path $appRoot "Timelapse Video Processing Setup 1.0.exe"
    $rootUninstall = Join-Path $appRoot "Uninstall Timelapse Video Processing.exe"
    Copy-Item -LiteralPath $builtSetup -Destination $rootSetup -Force
    Copy-Item -LiteralPath $builtUninstall -Destination $rootUninstall -Force

    foreach ($final in @($rootSetup, $rootUninstall)) {
        $hash = (Get-FileHash -LiteralPath $final -Algorithm SHA256).Hash.ToLowerInvariant()
        $fileName = Split-Path $final -Leaf
        $sidecar = "$final.sha256"
        Set-Content -LiteralPath $sidecar -Value "$hash  $fileName" -Encoding ASCII -NoNewline
        $sizeMB = (Get-Item -LiteralPath $final).Length / 1MB
        Write-Host ("{0}: {1:N1} MB, SHA-256 {2}" -f $fileName, $sizeMB, $hash)
    }

    Write-Host "==> Build complete."
} finally {
    Pop-Location
}
