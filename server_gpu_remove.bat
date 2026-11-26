@echo off
rem Stop the OnseiMan GPU recognition server and remove its Linux (onseiman-gpu). Keep this file ASCII-only.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\remove_gpu_server.ps1"
pause
