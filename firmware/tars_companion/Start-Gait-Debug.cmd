@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Missing project Python environment. See GAIT_DEBUG.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -u -m tars_companion.gait_debug --open %*
pause
