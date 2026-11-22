@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem OnseiMan を GitHub の最新版に入れ替える（設定・辞書・履歴はそのまま）
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\update.ps1"
pause
