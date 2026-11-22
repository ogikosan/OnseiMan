@echo off
rem Update OnseiMan to the latest version on GitHub (settings, dictionary and history are kept).
rem Keep this file ASCII-only. The real work is in tools\update.ps1.
rem The update replaces this file while it runs, so everything is on ONE line (cmd reads a line fully before running it).
cd /d "%~dp0" & powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\update.ps1" & pause & exit /b
