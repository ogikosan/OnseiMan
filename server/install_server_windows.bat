@echo off
rem OnseiMan recognition server setup (Windows). Keep this file ASCII-only.
rem The real work is in tools\install.ps1.
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\tools\install.ps1" -Target server
pause
