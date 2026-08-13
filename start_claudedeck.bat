@echo off
rem ClaudeDeck tray launcher (pure ASCII). Server runs hidden; look for the amber icon in the system tray.
cd /d %~dp0
if not exist .venv\Scripts\pythonw.exe (
  echo [ClaudeDeck] venv missing. Run install.bat first.
  pause
  exit /b 1
)
start "" .venv\Scripts\pythonw.exe -m app.tray
