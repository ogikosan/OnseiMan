@echo off
rem Remove data and files that older versions of OnseiMan left behind (asks before deleting). Keep this file ASCII-only.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\cleanup_old.ps1"
pause
