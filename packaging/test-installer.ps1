<#
.SYNOPSIS
    End-to-end smoke test of the Timelapse Video Processing 0.8 installer and
    both uninstall entry points (installed Uninstall.exe and the standalone
    "Uninstall Timelapse Video Processing.exe").

.DESCRIPTION
    Adapted from history/04_2026-09_codex_v0.3/verification/test-installer.ps1.
    Unlike 0.3, this installer has no offline WebView2 runtime step and
    installs into the real per-user default location
    ($LOCALAPPDATA\Programs\Timelapse Video Processing) rather than an
    isolated verification-only target — there is nothing left to isolate
    it with, since the whole point is a per-user install with no admin
    rights. That makes the pre-flight checks below load-bearing, not just
    polite: this script installs, reinstalls, and fully uninstalls the real
    per-user product and can touch real %LOCALAPPDATA% user data.

    Sequence:
      1. Refuse to run if TimelapseVideoProcessing is already registered in
         HKCU, unless -Force. Refuse if %LOCALAPPDATA%\Etaluma Video
         Processing already holds data, unless -Force (with -Force, that
         folder is *moved* to <dev folder>\build\userdata-backup-<tag>\ and moved back
         in a `finally` block regardless of how this script exits — best
         effort, not a guarantee, so don't run this with -Force against a
         machine whose data you cannot afford to risk).
      2. Silent install (/S); verify installed files, both shortcut pairs,
         and every registry value installer.nsi writes.
      3. Plant a sentinel file under the real user data folder, then try
         "<exe> --diagnostic <path.json>" (a supported build exits 0 and
         writes JSON within 15s); a build that does not support it yet
         instead gets a plain 10s run-then-kill, and this script checks for
         a logs\session_*.log file instead.
      4. Reinstall (/S again); verify the sentinel survived (reinstall must
         never touch user data).
      5. Uninstall via the STANDALONE "Uninstall Timelapse Video Processing.exe"
         with /S /KEEPSETTINGS=1 (Start-Process -Wait is reliable here: the
         standalone uninstaller is a plain NSIS program, not an
         NSIS-generated uninstaller stub, so it does not self-copy and
         return early); verify the sentinel/user-data folder survived and
         the program files/shortcuts/registry did not.
      6. Reinstall again, then uninstall via the INSTALLED
         "<InstallLocation>\Uninstall.exe" /S (no /KEEPSETTINGS, i.e.
         remove). NSIS-generated uninstaller stubs self-copy to %TEMP% and
         return immediately, so Start-Process -Wait would falsely report
         success instantly here — this path is driven with Start-Process
         (no -Wait) followed by a poll loop on the install/registry/data
         state instead, exactly as Codex 0.3's script did.
      7. Write a JSON report to <dev folder>\build\installer[-<ReportTag>].json.

.PARAMETER Setup
    Path to "Timelapse Video Processing Setup 0.9.exe" (built by build.ps1).
    The standalone uninstaller is expected next to it, as
    "Uninstall Timelapse Video Processing.exe".

.PARAMETER ReportTag
    Optional short tag appended to the report file name and to temporary
    paths this script creates, so repeated runs do not collide.

.PARAMETER Force
    Proceed even though the product (or leftover user data) already appears
    present. See the safety note above — this can still end with the real
    per-user product fully uninstalled if it was already installed before
    this script ran twice with -Force back to back; do not point this at a
    machine with a real installation you care about.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Setup,
    [ValidatePattern('^[a-zA-Z0-9.-]*$')][string]$ReportTag = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$packagingDir = $PSScriptRoot
$appRoot = Split-Path $packagingDir -Parent
$devRoot = if ($env:ETALUMA_DEV_DIR) { $env:ETALUMA_DEV_DIR } else { Join-Path $env:LOCALAPPDATA "EtalumaVP-dev\v0.9" }  # .venv and build scratch live outside the version folder
$buildDir = Join-Path $devRoot "build"
New-Item -ItemType Directory -Force -Path $buildDir | Out-Null

$suffix = if ($ReportTag) { "-$ReportTag" } else { "" }
$reportPath = Join-Path $buildDir "installer$suffix.json"

# Resolve relative to the caller's current location (GetFullPath alone uses the process directory).
# (0.6: an absolute path is checked first; joining it onto the current location made GetFullPath throw.)
$Setup = if ([IO.Path]::IsPathRooted($Setup)) { [IO.Path]::GetFullPath($Setup) } else { [IO.Path]::GetFullPath((Join-Path (Get-Location).Path $Setup)) }
if (-not (Test-Path -LiteralPath $Setup)) {
    throw "Setup exe not found: $Setup"
}
$standaloneUninstaller = Join-Path (Split-Path -Parent $Setup) "Uninstall Timelapse Video Processing.exe"
if (-not (Test-Path -LiteralPath $standaloneUninstaller)) {
    throw "Standalone uninstaller not found next to -Setup: $standaloneUninstaller (build.ps1 copies both to the same folder)."
}

$uninstKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\TimelapseVideoProcessing"
$dataDir = Join-Path $env:LOCALAPPDATA "Timelapse Video Processing"
$desktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Timelapse Video Processing.lnk"
$startMenuDir = Join-Path ([Environment]::GetFolderPath("Programs")) "Timelapse Video Processing"
$startAppShortcut = Join-Path $startMenuDir "Timelapse Video Processing.lnk"
$startUninstallShortcut = Join-Path $startMenuDir "Uninstall Timelapse Video Processing.lnk"

if ((Test-Path -LiteralPath $uninstKeyPath) -and -not $Force) {
    throw "Timelapse Video Processing already appears installed for this user (registry key present). Re-run with -Force if this is intentional (see this script's header for what -Force can do)."
}

$dataBackup = $null
if (Test-Path -LiteralPath $dataDir) {
    $hasContent = @(Get-ChildItem -LiteralPath $dataDir -Force -ErrorAction SilentlyContinue).Count -gt 0
    if ($hasContent -and -not $Force) {
        throw "$dataDir already holds data. Re-run with -Force to back it up for the duration of this test (best effort; see this script's header), or remove it manually first."
    }
    if ($hasContent) {
        $dataBackup = Join-Path $buildDir "userdata-backup$suffix"
        if (Test-Path -LiteralPath $dataBackup) { Remove-Item -LiteralPath $dataBackup -Recurse -Force }
        Write-Host "==> Backing up existing $dataDir to $dataBackup for the duration of this test"
        Move-Item -LiteralPath $dataDir -Destination $dataBackup
    }
}

function Wait-Removed {
    param([string[]]$Paths, [int]$TimeoutSeconds = 60)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $anyLeft = $false
        foreach ($p in $Paths) { if (Test-Path -LiteralPath $p) { $anyLeft = $true; break } }
        if (-not $anyLeft) { return $true }
        Start-Sleep -Milliseconds 250
    }
    foreach ($p in $Paths) { if (Test-Path -LiteralPath $p) { return $false } }
    return $true
}

function Test-DiagnosticFlag {
    param([string]$ExePath, [string]$JsonOut)
    if (Test-Path -LiteralPath $JsonOut) { Remove-Item -LiteralPath $JsonOut -Force }
    $proc = Start-Process -FilePath $ExePath -ArgumentList "--diagnostic `"$JsonOut`"" -WindowStyle Hidden -PassThru
    $finished = $proc.WaitForExit(15000)
    if (-not $finished) {
        try { $proc.Kill() } catch {}
        return $false
    }
    return ($proc.ExitCode -eq 0) -and (Test-Path -LiteralPath $JsonOut)
}

function Test-SessionLogFallback {
    param([string]$ExePath, [string]$DataDir)
    $before = @()
    $logDir = Join-Path $DataDir "logs"
    if (Test-Path -LiteralPath $logDir) {
        $before = @(Get-ChildItem -LiteralPath $logDir -Filter "session_*.log" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
    }
    $proc = Start-Process -FilePath $ExePath -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 10
    try { if (-not $proc.HasExited) { $proc.Kill() } } catch {}
    if (-not (Test-Path -LiteralPath $logDir)) { return $false }
    $after = @(Get-ChildItem -LiteralPath $logDir -Filter "session_*.log" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
    return ($after.Count -gt 0) -and (($after | Where-Object { $before -notcontains $_ }).Count -gt 0 -or $before.Count -eq 0)
}

$result = [ordered]@{
    setup = $Setup
    standalone_uninstaller = $standaloneUninstaller
}

try {
    Write-Host "==> Installing silently: $Setup /S"
    $install = Start-Process -FilePath $Setup -ArgumentList "/S" -WindowStyle Hidden -Wait -PassThru
    $result.install_exit_code = $install.ExitCode
    if ($install.ExitCode -ne 0) { throw "Installer returned exit code $($install.ExitCode)." }

    $installLocation = (Get-ItemProperty -LiteralPath $uninstKeyPath -ErrorAction Stop).InstallLocation
    if (-not $installLocation) { throw "InstallLocation was not written to the registry." }
    $result.install_location = $installLocation
    $appExe = Join-Path $installLocation "Timelapse Video Processing.exe"
    $installedUninstaller = Join-Path $installLocation "Uninstall.exe"

    $result.files_present = (Test-Path -LiteralPath $appExe) -and (Test-Path -LiteralPath $installedUninstaller)
    $result.shortcuts_present = (Test-Path -LiteralPath $desktopShortcut) -and (Test-Path -LiteralPath $startAppShortcut) -and (Test-Path -LiteralPath $startUninstallShortcut)

    $reg = Get-ItemProperty -LiteralPath $uninstKeyPath
    $result.registry_ok = ($reg.DisplayName -eq "Timelapse Video Processing") -and
        ($reg.DisplayVersion -eq "0.9.0") -and
        ($reg.Publisher -eq "BIOMIS Team, SATIE laboratory, ENS Paris-Saclay") -and
        ($reg.UninstallString -match [regex]::Escape($installedUninstaller)) -and
        ($reg.NoModify -eq 1) -and
        ($reg.NoRepair -eq 1) -and
        ($reg.EstimatedSize -gt 0)

    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $dataDir "profiles") | Out-Null
    $sentinel = Join-Path $dataDir "profiles\test-sentinel.txt"
    Set-Content -LiteralPath $sentinel -Value "Timelapse Video Processing installer test sentinel; must survive a keep-settings uninstall." -Encoding UTF8

    $diagnosticJson = Join-Path $buildDir "installed$suffix-diagnostic.json"
    $result.diagnostic_supported = Test-DiagnosticFlag -ExePath $appExe -JsonOut $diagnosticJson
    if ($result.diagnostic_supported) {
        Write-Host "==> --diagnostic supported; wrote $diagnosticJson"
    } else {
        Write-Host "==> --diagnostic not supported (or failed); falling back to a session-log check"
        $result.session_log_present = Test-SessionLogFallback -ExePath $appExe -DataDir $dataDir
    }

    Write-Host "==> Reinstalling silently"
    $reinstall = Start-Process -FilePath $Setup -ArgumentList "/S" -WindowStyle Hidden -Wait -PassThru
    $result.reinstall_exit_code = $reinstall.ExitCode
    $result.reinstall_preserved_user_data = (Test-Path -LiteralPath $sentinel)

    Write-Host "==> Uninstalling via the standalone uninstaller, keeping settings: `"$standaloneUninstaller`" /S /KEEPSETTINGS=1"
    $keepUninstall = Start-Process -FilePath $standaloneUninstaller -ArgumentList "/S", "/KEEPSETTINGS=1" -WindowStyle Hidden -Wait -PassThru
    $result.keep_uninstall_exit_code = $keepUninstall.ExitCode
    $result.keep_uninstall_removed_program = -not (Test-Path -LiteralPath $installLocation)
    $result.keep_uninstall_removed_shortcuts = (-not (Test-Path -LiteralPath $desktopShortcut)) -and (-not (Test-Path -LiteralPath $startAppShortcut))
    $result.keep_uninstall_removed_registry = -not (Test-Path -LiteralPath $uninstKeyPath)
    $result.settings_preserved_after_keep_uninstall = (Test-Path -LiteralPath $sentinel)

    Write-Host "==> Reinstalling silently (second round)"
    $reinstall2 = Start-Process -FilePath $Setup -ArgumentList "/S" -WindowStyle Hidden -Wait -PassThru
    $result.reinstall2_exit_code = $reinstall2.ExitCode
    $result.settings_still_preserved_after_reinstall = (Test-Path -LiteralPath $sentinel)

    $reg2 = Get-ItemProperty -LiteralPath $uninstKeyPath
    $installLocation2 = $reg2.InstallLocation
    $installedUninstaller2 = Join-Path $installLocation2 "Uninstall.exe"

    Write-Host "==> Uninstalling via the installed uninstaller, removing settings: `"$installedUninstaller2`" /S"
    # NSIS-generated Uninstall.exe self-copies to %TEMP% and returns almost
    # immediately; Start-Process -Wait would report success before the real
    # removal work is done, so this is fire-and-forget plus a poll loop.
    Start-Process -FilePath $installedUninstaller2 -ArgumentList "/S" -WindowStyle Hidden | Out-Null
    $removed = Wait-Removed -Paths @($installLocation2, $uninstKeyPath) -TimeoutSeconds 60
    $result.remove_uninstall_completed = $removed
    $result.remove_uninstall_removed_program = -not (Test-Path -LiteralPath $installLocation2)
    $result.remove_uninstall_removed_shortcuts = (-not (Test-Path -LiteralPath $desktopShortcut)) -and (-not (Test-Path -LiteralPath $startAppShortcut)) -and (-not (Test-Path -LiteralPath $startUninstallShortcut))
    $result.remove_uninstall_removed_registry = -not (Test-Path -LiteralPath $uninstKeyPath)
    $result.user_data_removed = -not (Test-Path -LiteralPath $dataDir)

    $result.limitation = "Installs into the real per-user profile on whatever machine runs this script; no clean-VM isolation. Run on a disposable Windows 10/11 VM for release sign-off, not on a machine with data you cannot risk (see -Force in this script's header)."

    $result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $reportPath -Encoding UTF8
    Write-Host "==> Report written to $reportPath"

    $criticalChecks = @(
        "files_present", "shortcuts_present", "registry_ok",
        "reinstall_preserved_user_data",
        "keep_uninstall_removed_program", "keep_uninstall_removed_shortcuts", "keep_uninstall_removed_registry",
        "settings_preserved_after_keep_uninstall", "settings_still_preserved_after_reinstall",
        "remove_uninstall_completed", "remove_uninstall_removed_program",
        "remove_uninstall_removed_shortcuts", "remove_uninstall_removed_registry", "user_data_removed"
    )
    $failed = @($criticalChecks | Where-Object { $result[$_] -eq $false })
    if (-not $result.diagnostic_supported -and $result.session_log_present -ne $true) {
        $failed += "diagnostic_or_session_log"
    }
    if ($failed.Count -gt 0) {
        throw "Installer test checks failed: $($failed -join ', '). See $reportPath."
    }

    Write-Host "==> All installer checks passed."
    $result
} finally {
    if ($dataBackup) {
        Write-Host "==> Restoring backed-up user data to $dataDir"
        if (Test-Path -LiteralPath $dataDir) { Remove-Item -LiteralPath $dataDir -Recurse -Force -ErrorAction SilentlyContinue }
        Move-Item -LiteralPath $dataBackup -Destination $dataDir -ErrorAction SilentlyContinue
    }
}
