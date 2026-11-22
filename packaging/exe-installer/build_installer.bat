@echo off
chcp 65001 >nul
cd /d "%~dp0..\.."
rem 公開せずにインストーラーだけ作る（build_dist\installer に出る）。公開は release.bat。
.venv\Scripts\python.exe packaging\exe-installer\release.py --dry-run
