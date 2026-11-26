@echo off
rem Start the OnseiMan recognition server on port 8765. Keep this file ASCII-only.
rem If .venv_server\onseiman-ready.txt is missing, set up first (no pause), then start.
cd /d "%~dp0.."
if exist ".venv_server\onseiman-ready.txt" goto run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\tools\install.ps1" -Target server -Starting
if errorlevel 1 goto failed
:run
if exist ".venv_server\Scripts\python.exe" ".venv_server\Scripts\python.exe" server\app.py --host 0.0.0.0 --port 8765
pause
exit /b 0
:failed
pause
exit /b 1
