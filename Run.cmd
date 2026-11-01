@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Run Install.cmd first, or use the standalone OmniLocal.exe.
  pause
  exit /b 1
)
start "Omni Local" ".venv\Scripts\pythonw.exe" "app.py"
