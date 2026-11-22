@echo off
chcp 65001 >nul
cd /d "%~dp0..\.."
rem 新しいバージョンを公開する。先に client\version.py の __version__ を上げておく。
rem 例: packaging\exe-installer\release.bat "長い発話の認識を改善しました"
.venv\Scripts\python.exe packaging\exe-installer\release.py --notes "%~1"
