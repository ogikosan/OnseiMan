@echo off
rem Build the exe installer without publishing (output: build_dist\installer).
cd /d "%~dp0..\.."
.venv\Scripts\python.exe packaging\exe-installer\release.py --dry-run
