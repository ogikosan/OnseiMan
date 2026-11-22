@echo off
chcp 65001 >nul
cd /d "%~dp0\.."

if exist ".venv_server\Scripts\python.exe" (
    .venv_server\Scripts\python.exe server\app.py --host 0.0.0.0 --port 8765 --provider cpu
) else (
    python server\app.py --host 0.0.0.0 --port 8765 --provider cpu
)
pause
