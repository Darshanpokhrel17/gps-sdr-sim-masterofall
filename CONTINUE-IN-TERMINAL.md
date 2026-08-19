# Continue in terminal — handoff prompt

Paste everything in the code block below into Claude Code (terminal), run from inside the
project folder.

```
You are continuing an existing engineering project, "GPS-SDR-SIM Master of All Master" — a
GPS L1 C/A signal-simulation toolkit for AUTHORIZED, RF-SHIELDED (Faraday-cage) GNSS receiver
testing (counter-UAS / GNSS-resilience R&D). All transmission happens inside a sealed shielded
chamber through cable + attenuator.

PROJECT LOCATION (run from here):
  D:\Downloads\gps-sdr-sim-master (1)\MasterOfAll-READY-v2\GPS-SDR-SIM-MasterOfAll\

FIRST, read these to load full context:
  README.md, CAGE-RUNBOOK.md, QUICKSTART.txt,
  dashboard.py (the local "Mission Control v2" web app),
  verify_signal.py (GPS acquisition self-test),
  src/gpssim.c, src/gpssim.h, Makefile, build.bat,
  docs/ANALYSIS.md, docs/TARGET-PROFILES.md

WHAT ALREADY WORKS (do not rebuild):
- Merged gps-sdr-sim engine -> prebuilt gps-sdr-sim.exe (Windows, static). Default generation
  path uses realistic path-loss (0% clipping at 8-bit) and is byte-identical to upstream —
  keep it that way. `make realtime` adds optional TCP/UDP streaming (Linux only).
- dashboard.py: pure-Python-stdlib local web app on http://127.0.0.1:8770. It GENERATES a .bin
  (nav file + lat/lon/alt + duration <=600s + time=now UTC + -T), SELF-TESTS it (calls
  verify_signal.py), and TRANSMITS via hackrf_transfer (always -R loop, amp off, TX-gain and
  frequency-offset sliders, gated behind a "shielded chamber confirmed" checkbox). It
  auto-detects gps-sdr-sim.exe, hackrf_transfer, hackrf_info.
- verify_signal.py: numpy GPS L1 C/A acquisition proof (verdict PASS/FAIL).

HARDWARE / SIGNAL FACTS:
- HackRF One, 8-bit I/Q, 2.6 Msps, GPS L1 = 1575.42 MHz.
  Transmit command: hackrf_transfer -t <bin> -f 1575420000 -s 2600000 -a 0 -x <gain> -R
- Receivers under test: u-blox M8/M9/M10 on ArduPilot/Pixhawk (Mission Planner), and a DJI drone.
- Today's ephemeris: nav/brdc2290.26n (DOY 229, 2026).

CURRENT PROBLEM TO SOLVE:
On the bench the spoof "does nothing": the u-blox keeps a strong REAL multi-GNSS fix (~13 sats,
HDOP 0.9), so it ignores the weaker simulated L1. The sim only wins once the REAL signal is
removed by the sealed cage. Make that reliable and observable.

TASKS (in order):
1) Add a "Spoof Confirm" monitor to the dashboard: open the receiver's COM port (pyserial;
   note pip install pyserial), parse NMEA ($GxGGA/$GxGSV/$GxRMC) and show live fix type, sat
   count, C/N0, lat/lon, and clearly flag when the position converges on the SIMULATED
   coordinates. Receive-only. Add endpoints /api/gps/ports, /api/gps/open, /api/gps/status and
   a panel with a COM-port dropdown + baud selector (list 9600/38400/115200/230400).
2) Add a "cage isolation" indicator: show that sat count should fall toward 0 when the chamber
   is sealed (real signal gone) BEFORE transmitting; warn if sats stay high (leaky cage /
   antenna outside).
3) Improve spoof robustness: ensure today's brdc + time=now are used; add a one-click
   "regenerate with now()"; surface coupling/power guidance inline; keep path-loss default and
   -R-always.
4) (Optional, defensive) Detection module from the operator's GNSS-spoofing-detection papers:
   monitor u-blox UBX-NAV-STATUS.spoofDetState, UBX-MON-RF AGC/jamInd, and C/N0, and raise a
   "possible spoof/denial" flag. This is the counter-UAS resilience side.

CONSTRAINTS & SCOPE (important):
- Windows-first; keep the dashboard dependency-light (stdlib; pyserial only for the monitor).
  Must run fully offline (no internet in the cage).
- Keep the merged engine's default generation path byte-identical to upstream; don't regress the
  path-loss default or the -R transmit loop.
- Test what you can: python -m py_compile, run the dashboard and hit the APIs, gps-sdr-sim.exe
  generation + verify_signal.py PASS. You cannot test HackRF/serial hardware in the sandbox —
  write defensively and mock where needed.
- SCOPE BOUNDARY: this codebase builds only (a) GNSS signal simulation for the operator's OWN
  receivers inside a shielded chamber, and (b) PASSIVE receive/monitor/detection tools. Do NOT
  build a GNSS jammer (a noise transmitter to deny service) or any drone command-injection /
  force-land / takeover capability — those are out of scope.

HOW TO RUN:
  cd "D:\Downloads\gps-sdr-sim-master (1)\MasterOfAll-READY-v2\GPS-SDR-SIM-MasterOfAll"
  python dashboard.py            # opens http://127.0.0.1:8770

DELIVERABLES: updated dashboard.py (+ any new modules), a short note in README.md /
CAGE-RUNBOOK.md for the new monitor, everything self-contained in this folder.
```
