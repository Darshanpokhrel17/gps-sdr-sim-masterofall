@echo off
REM ====================================================================
REM  HackRF tools check / setup helper (Windows)
REM  Fixes: "hackrf_transfer is not recognized"
REM ====================================================================
setlocal
echo(
echo  Checking for the HackRF host tools...
echo(

where hackrf_info >nul 2>nul
if %errorlevel%==0 (
  echo  FOUND. Device check:
  echo  ------------------------------------------------------------
  hackrf_info
  echo  ------------------------------------------------------------
  echo(
  echo  HackRF tools are installed. You can transmit from the dashboard.
  echo  If "No HackRF boards found" above: plug the HackRF into a rear USB
  echo  port with a DATA cable, and run Zadig once ^(WinUSB^).
  echo(
  pause
  exit /b 0
)

echo  NOT FOUND on PATH. That is why "hackrf_transfer is not recognized".
echo(
echo  Install ONE of these (each bundles the tools AND adds them to PATH):
echo    1^) PothosSDR   https://downloads.myriadrf.org/builds/PothosSDR/
echo    2^) radioconda  https://github.com/radioconda/radioconda-installer/releases
echo(
echo  Then install the USB driver with Zadig ( https://zadig.akeo.ie/ ):
echo    Options -^> List All Devices -^> pick "HackRF One" -^> WinUSB -^> Install Driver
echo(
echo  Opening the download pages for you...
start "" https://downloads.myriadrf.org/builds/PothosSDR/
start "" https://zadig.akeo.ie/
echo(
echo  After installing: open a NEW terminal (or re-launch START-DASHBOARD.bat),
echo  then run this file again to confirm hackrf_info works.
echo(
echo  (You can also paste the install folder into the dashboard's
echo   "System & hardware status" panel if it is not on PATH.)
echo(
pause
