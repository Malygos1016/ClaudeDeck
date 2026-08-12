@echo off
rem ClaudeDeck installer: create venv and sync deps (pure ASCII, see win-env notes)
cd /d %~dp0
where uv >nul 2>nul
if errorlevel 1 (
  echo [ClaudeDeck] uv not found in PATH. Install uv first: https://docs.astral.sh/uv/
  exit /b 1
)
uv venv --python 3.14
if errorlevel 1 exit /b 1
uv sync
if errorlevel 1 exit /b 1
echo.
echo [ClaudeDeck] Install done. Next: start_claudedeck.bat
echo [ClaudeDeck] Note: first launch may take up to 1 minute (AV scan on new venv python.exe).
