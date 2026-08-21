@echo off
rem ClaudeDeck launcher (pure ASCII). Server runs hidden; look for the amber icon in the system tray.
rem First run bootstraps the venv automatically (needs uv: https://docs.astral.sh/uv/).
cd /d %~dp0
if exist .venv\Scripts\pythonw.exe goto run

echo [ClaudeDeck] First run: creating venv and installing deps (about 1 minute)...
where uv >nul 2>nul
if errorlevel 1 (
  echo [ClaudeDeck] uv not found in PATH. Install uv first: https://docs.astral.sh/uv/
  pause
  exit /b 1
)
uv venv --python 3.14
if errorlevel 1 (
  echo [ClaudeDeck] venv creation failed, see output above.
  pause
  exit /b 1
)
uv sync
if errorlevel 1 (
  echo [ClaudeDeck] dependency install failed, see output above.
  pause
  exit /b 1
)
echo [ClaudeDeck] Install done. Note: first launch may take up to 1 minute (AV scan on new venv).

:run
rem Some uv builds ship a console-subsystem pythonw shim (black window titled pythonw.exe;
rem closing it kills the tray). Launch hidden so either shim flavor stays windowless.
powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -WindowStyle Hidden -FilePath '%~dp0.venv\Scripts\pythonw.exe' -ArgumentList '-m','app.tray' -WorkingDirectory '%~dp0'"
