@echo off
rem OnseiMan setup. Keep this file ASCII-only: cmd.exe misreads non-ASCII text in batch files.
rem The real work (with Japanese messages) is in tools\install.ps1.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install.ps1" -Target client
pause
