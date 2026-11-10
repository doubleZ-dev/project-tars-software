@echo off
cd /d "%~dp0"
echo TARS wireless voice control. No automatic Arm or motion.
echo At the TARS prompt, manually Arm, then enter /listen for continuous voice.
".venv\Scripts\python.exe" -u -m tars_companion.robot_runtime --live --robot --config "wifi-audio.local.json" --provider deepseek --allow-motion
pause
