<#
.SYNOPSIS
    Fetch pinned offline build tools for Timelapse Video Processing packaging.

.DESCRIPTION
    Downloads NSIS 3.11 (from the same mirror and pinned SHA-256 used by the
    Codex 0.3 packaging toolchain) and extracts it under
    "%LOCALAPPDATA%\EtalumaVP-dev\v1.0\build\tools\nsis-3.11". Unlike Codex 0.3's fetch-tools.ps1, this
    script does NOT fetch WebView2 — v0.4 is a native PySide6 app with no
    embedded browser runtime.

    Idempotent: skips the download when a zip with the matching hash already
    exists, and skips extraction when makensis.exe is already present.

.NOTES
    Run from anywhere; paths are resolved relative to this script's location
    (packaging\), which must remain a sibling of the version folder's other
    subfolders (source\, tests\, tools\, docs\).
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
# Windows PowerShell 5.1's default progress bar makes Invoke-WebRequest very
# slow; this has no effect on behaviour, only on download speed.
$ProgressPreference = "SilentlyContinue"

$appRoot = Split-Path $PSScriptRoot -Parent
$devRoot = if ($env:ETALUMA_DEV_DIR) { $env:ETALUMA_DEV_DIR } else { Join-Path $env:LOCALAPPDATA "EtalumaVP-dev\v1.0" }  # .venv and build scratch live outside the version folder
$toolsDir = Join-Path $devRoot "build\tools"
New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null

$nsisVersion = "3.11"
$nsisUrl = "https://github.com/tauri-apps/binary-releases/releases/download/nsis-3.11/nsis-3.11.zip"
$nsisSha256 = "C7D27F780DDB6CFFB4730138CD1591E841F4B7EDB155856901CDF5F214394FA1"
$archive = Join-Path $toolsDir "nsis-$nsisVersion.zip"
$nsisDir = Join-Path $toolsDir "nsis-$nsisVersion"
$makensis = Join-Path $nsisDir "makensis.exe"

function Test-ArchiveHash {
    param([string]$Path, [string]$ExpectedSha256)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -eq $ExpectedSha256
}

if (-not (Test-ArchiveHash -Path $archive -ExpectedSha256 $nsisSha256)) {
    Write-Host "Downloading NSIS $nsisVersion from $nsisUrl ..."
    Invoke-WebRequest -Uri $nsisUrl -OutFile $archive
    if (-not (Test-ArchiveHash -Path $archive -ExpectedSha256 $nsisSha256)) {
        Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
        throw "NSIS archive hash does not match the pinned release ($nsisSha256). Aborting; nothing was left on disk."
    }
} else {
    Write-Host "NSIS archive already present and verified: $archive"
}

if (-not (Test-Path -LiteralPath $makensis)) {
    Write-Host "Extracting NSIS to $toolsDir ..."
    Expand-Archive -LiteralPath $archive -DestinationPath $toolsDir -Force
    if (-not (Test-Path -LiteralPath $makensis)) {
        Write-Host "Contents of $toolsDir after extraction:"
        Get-ChildItem -LiteralPath $toolsDir -Recurse | Select-Object -ExpandProperty FullName | Write-Host
        throw "Expected $makensis after extraction but it is missing. See the listing above; the archive layout may have changed upstream."
    }
} else {
    Write-Host "NSIS already extracted: $makensis"
}

$version = & $makensis /VERSION 2>&1
Write-Host "makensis reports: $version"
Get-FileHash -LiteralPath $archive -Algorithm SHA256 | Format-List
Write-Host "NSIS ready at $makensis"
