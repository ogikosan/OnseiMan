@echo off
rem Start OnseiMan. Keep this file ASCII-only.
rem tools\install.ps1 writes .venv\onseiman-ready.txt when everything is installed and importable.
cd /d "%~dp0"
if not exist ".venv\onseiman-ready.txt" call "%~dp0install_client.bat"
if exist ".venv\Scripts\pythonw.exe" start "" ".venv\Scripts\pythonw.exe" "client\main.py"
