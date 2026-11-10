@echo off
cd /d "%~dp0"
if /I "%~1"=="--reset-key" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Tars-Director.ps1" -Setup -ResetKey
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Tars-Director.ps1" -Setup
)
if errorlevel 1 pause
