@echo off
rem One double-click build: fetch NSIS if missing, then run packaging\build.ps1.
rem Any arguments passed to this .bat are forwarded to build.ps1, e.g.:
rem   build_release.bat -Install
rem   build_release.bat -SkipTests -SkipInstaller
setlocal
set "DEV=%LOCALAPPDATA%\EtalumaVP-dev\v0.9"
if not "%ETALUMA_DEV_DIR%"=="" set "DEV=%ETALUMA_DEV_DIR%"

cd /d "%~dp0.."

where pwsh >nul 2>&1
if %errorlevel%==0 (
    set "PWSH=pwsh"
) else (
    set "PWSH=powershell"
)

if not exist "%DEV%\build\tools\nsis-3.11\makensis.exe" (
    echo Fetching packaging tools ^(NSIS^)...
    %PWSH% -NoProfile -ExecutionPolicy Bypass -File "packaging\fetch-tools.ps1"
    if errorlevel 1 (
        echo Fetching packaging tools failed.
        pause
        exit /b 1
    )
)

echo Building Timelapse Video Processing 0.8...
%PWSH% -NoProfile -ExecutionPolicy Bypass -File "packaging\build.ps1" %*
if errorlevel 1 (
    echo Build failed. See the output above.
    pause
    exit /b 1
)

echo.
echo Build complete. "Timelapse Video Processing Setup 0.9.exe" and
echo "Uninstall Timelapse Video Processing.exe" are at the version folder root.
pause
