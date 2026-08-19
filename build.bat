@echo off
REM ====================================================================
REM  Build gps-sdr-sim.exe on Windows (only needed if you want to rebuild;
REM  a ready-to-run gps-sdr-sim.exe is already included in this folder).
REM ====================================================================
setlocal
cd /d "%~dp0"

echo(
echo  GPS-SDR-SIM "Master of All Master" - Windows build
echo  ---------------------------------------------------

where gcc >nul 2>nul
if %errorlevel%==0 goto :havegcc

echo  gcc (MinGW) was not found on your PATH.
echo(
echo  You do NOT need to build - a prebuilt gps-sdr-sim.exe is already here.
echo  Just run  START-DASHBOARD.bat.
echo(
echo  If you DO want to rebuild: install w64devkit or MinGW-w64, then re-run this.
echo(
pause
exit /b 0

:havegcc
echo  Compiling src\gpssim.c + src\getopt.c ...
gcc src\gpssim.c src\getopt.c -lm -O3 -D_FILE_OFFSET_BITS=64 -o gps-sdr-sim.exe
if exist gps-sdr-sim.exe (
  echo(
  echo  SUCCESS: gps-sdr-sim.exe built in this folder.
) else (
  echo(
  echo  BUILD FAILED. Use the prebuilt gps-sdr-sim.exe instead.
)
echo(
pause
