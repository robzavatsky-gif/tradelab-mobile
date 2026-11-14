@echo off
cd /d "%~dp0"
py -3 cloud_update.py --demo
if errorlevel 1 (
 echo Dashboard demo failed.
 pause
 exit /b 1
)
start "" http://localhost:8765/web/
py -3 -m http.server 8765
