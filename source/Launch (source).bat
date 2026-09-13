@echo off
setlocal
set "DEV=%LOCALAPPDATA%\EtalumaVP-dev\v1.0"
if not "%ETALUMA_DEV_DIR%"=="" set "DEV=%ETALUMA_DEV_DIR%"
if not exist "%DEV%\.venv\Scripts\python.exe" (
  echo Run "Install dev environment.bat" first.
  pause
  exit /b 1
)
"%DEV%\.venv\Scripts\python.exe" -m etaluma_video %*
