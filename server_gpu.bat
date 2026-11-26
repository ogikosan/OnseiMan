@echo off
rem Set up and start the OnseiMan recognition server that uses an NVIDIA GPU (Haas-ja, in WSL2).
rem Keep this file ASCII-only. After setup it starts by itself when you sign in.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup_gpu_server.ps1"
pause
