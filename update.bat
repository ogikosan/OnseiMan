@echo off
rem Update OnseiMan to the latest version on GitHub (settings, dictionary and history are kept).
rem Keep this file ASCII-only. The real work is in tools\update.ps1.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\update.ps1"
pause
