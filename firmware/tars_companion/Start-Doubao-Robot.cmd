@echo off
cd /d "%~dp0"
echo Doubao ROBOT microphone and speaker test, PC relays audio. No motion enabled.
echo Close Start-Tars-Voice before starting; both use the paired LAN listener.
echo Requires --approve-doubao-cost --speaker-compatible --standalone-verified.
".venv\Scripts\python.exe" -u -m tars_companion.robot_runtime --voice-mode doubao --live --robot --config "wifi-audio.local.json" --doubao-config "doubao.example.json" %*
pause
