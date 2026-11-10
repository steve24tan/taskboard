@echo off
REM TaskBoard - start the dashboard on http://localhost:8765
cd /d "%~dp0"
if not exist config.json (
  echo config.json not found. Copy config.example.json to config.json and set the paths first.
  pause
  exit /b 1
)
python taskboard.py
pause
