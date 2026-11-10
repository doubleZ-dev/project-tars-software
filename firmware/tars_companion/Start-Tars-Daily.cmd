@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Tars-Seeduplex.ps1" -Daily %*
pause
