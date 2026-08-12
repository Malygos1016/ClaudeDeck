@echo off
rem ClaudeDeck launcher (pure ASCII). First launch may take ~1 minute due to AV scan.
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo [ClaudeDeck] venv missing. Run install.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m app.serve
if errorlevel 1 pause
