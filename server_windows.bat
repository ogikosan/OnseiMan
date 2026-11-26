@echo off
rem Run the OnseiMan recognition server (Rust edition) on this Windows PC.
rem Keep this file ASCII-only.
cd /d "%~dp0"
if exist "onseiman-server.exe" (
    "onseiman-server.exe" setup --root "%~dp0"
    "onseiman-server.exe" --host 0.0.0.0 --port 8765 --root "%~dp0"
    exit /b %errorlevel%
)
echo onseiman-server.exe not found. Download from GitHub Releases.
pause
exit /b 1
