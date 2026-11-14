@echo off
cd /d "%~dp0"
py -3 cloud_update.py
if errorlevel 1 (
 echo Run failed: see output above.
 pause
 exit /b 1
)
start "" http://localhost:8765/web/
py -3 -m http.server 8765
