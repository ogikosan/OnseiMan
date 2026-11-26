@echo off
rem Start OnseiMan (Rust edition). Keep this file ASCII-only.
cd /d "%~dp0"
if exist "onseiman.exe" (
    explorer.exe "%~dp0onseiman.exe"
    exit /b 0
)
echo onseiman.exe not found. Run cleanup_old.bat or download from GitHub Releases.
pause
exit /b 1
