@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3.11 -m venv .venv
  if errorlevel 1 goto :error
)
.venv\Scripts\python.exe configure.py --check-server
if errorlevel 2 goto :port_error
if not errorlevel 1 (
  start "" http://127.0.0.1:8765
  exit /b 0
)
.venv\Scripts\python.exe configure.py
if errorlevel 1 goto :error
.venv\Scripts\python.exe configure.py --check-runtime
if errorlevel 1 (
  .venv\Scripts\python.exe -m pip install -r requirements-lock.txt -e vendor\social-auto-upload
  if errorlevel 1 goto :error
  set "PLAYWRIGHT_BROWSERS_PATH=%cd%\.browsers"
  .venv\Scripts\python.exe -m patchright install chromium
  if errorlevel 1 goto :error
)
.venv\Scripts\python.exe configure.py
if errorlevel 1 goto :error
.venv\Scripts\python.exe -m pip check
if errorlevel 1 goto :error
.venv\Scripts\python.exe configure.py --check-runtime
if errorlevel 1 goto :error
start "" /b .venv\Scripts\python.exe configure.py --open-when-ready
.venv\Scripts\python.exe server.py --port 8765
pause
exit /b
:port_error
echo Port 8765 is occupied by another application. Close it and retry.
pause
exit /b 1
:error
echo Setup failed. Install Python 3.11 and Google Chrome, then retry.
pause
exit /b 1
