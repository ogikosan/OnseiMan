@echo off
rem Start OnseiMan. Keep this file ASCII-only.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" call "%~dp0install_client.bat"
if exist ".venv\Scripts\pythonw.exe" start "" ".venv\Scripts\pythonw.exe" "client\main.py"
