@echo off
cd /d "%~dp0"
echo Doubao standalone test. This uses the PC microphone and speaker FIRST.
echo Requires local credentials and explicit approval flags documented in DOUBAO_TEST.md.
echo No cloud call is allowed without --approve-doubao-cost --speaker-compatible.
".venv\Scripts\python.exe" -u -m tars_companion.robot_runtime --voice-mode doubao --live --doubao-config "doubao.example.json" %*
pause
