@echo off
REM ====================================================================
REM  GPS-SDR-SIM  "Master of All Master"  -  Mission Control Dashboard
REM  Double-click this file.  It opens the dashboard in your browser.
REM ====================================================================
setlocal
cd /d "%~dp0"

REM find a Python launcher
where py  >nul 2>nul && set PY=py
if not defined PY ( where python  >nul 2>nul && set PY=python )
if not defined PY ( where python3 >nul 2>nul && set PY=python3 )

if not defined PY (
  echo(
  echo  Python 3 was not found.
  echo  Install it from https://www.python.org/downloads/  (tick "Add Python to PATH"),
  echo  then double-click START-DASHBOARD.bat again.
  echo(
  pause
  exit /b 1
)

echo  Starting dashboard...  a browser tab will open at http://127.0.0.1:8770
echo  Keep this window open. Close it to stop the dashboard.
%PY% dashboard.py
pause
