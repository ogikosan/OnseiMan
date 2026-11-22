@echo off
rem Start OnseiMan. Keep this file ASCII-only.
rem tools\install.ps1 writes .venv\onseiman-ready.txt when everything is installed and importable.
rem If it is missing, set up first (no pause), then start. Pause only when the setup fails.
cd /d "%~dp0"
if exist ".venv\onseiman-ready.txt" goto run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install.ps1" -Target client -Starting
if errorlevel 1 goto failed
:run
if exist ".venv\Scripts\pythonw.exe" start "" ".venv\Scripts\pythonw.exe" "client\main.py"
exit /b 0
:failed
pause
exit /b 1
