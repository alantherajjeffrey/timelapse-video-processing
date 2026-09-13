@echo off
rem Creates the development environment in %LOCALAPPDATA%\EtalumaVP-dev\v0.9\.venv (or %ETALUMA_DEV_DIR%\.venv),
rem outside this folder, so the version folder only holds files that can be shared.
rem Not needed to use the installed app.
setlocal
set "DEV=%LOCALAPPDATA%\EtalumaVP-dev\v0.9"
if not "%ETALUMA_DEV_DIR%"=="" set "DEV=%ETALUMA_DEV_DIR%"
cd /d "%~dp0\.."
py -3.14 -m venv "%DEV%\.venv" 2>nul || py -3.12 -m venv "%DEV%\.venv" 2>nul || python -m venv "%DEV%\.venv"
"%DEV%\.venv\Scripts\python.exe" -m pip install --upgrade pip
"%DEV%\.venv\Scripts\python.exe" -m pip install -r "source\requirements.lock.txt"
"%DEV%\.venv\Scripts\python.exe" -m pip install --no-deps -e source
echo Done: %DEV%\.venv. Use "source\Launch (source).bat" to start the app from source.
pause
