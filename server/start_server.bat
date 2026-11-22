@echo off
rem Start the OnseiMan recognition server on port 8765. Keep this file ASCII-only.
cd /d "%~dp0.."
if not exist ".venv_server\Scripts\python.exe" call "%~dp0install_server_windows.bat"
if exist ".venv_server\Scripts\python.exe" ".venv_server\Scripts\python.exe" server\app.py --host 0.0.0.0 --port 8765 --provider cpu
pause
