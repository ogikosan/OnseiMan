@echo off
rem Publish a new version (exe installer). Bump __version__ in client\version.py first.
rem Usage: packaging\exe-installer\release.bat "release notes"
cd /d "%~dp0..\.."
.venv\Scripts\python.exe packaging\exe-installer\release.py --notes "%~1"
