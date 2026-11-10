@echo off
if not exist "%~dp0data" mkdir "%~dp0data"
echo stop>"%~dp0data\stop.request"
