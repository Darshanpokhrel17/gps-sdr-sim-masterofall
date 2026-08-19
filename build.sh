#!/usr/bin/env bash
# Build gps-sdr-sim on Linux / macOS.  Run:  ./build.sh   (or ./build.sh realtime)
set -e
cd "$(dirname "$0")"
if [ "$1" = "realtime" ]; then
  echo "Building realtime engine (TCP -n / UDP -w) ..."
  gcc src/gpssim.c -O3 -Wall -D_FILE_OFFSET_BITS=64 -DENABLE_REALTIME -lm -lpthread -o gps-sdr-sim-realtime
  echo "OK -> ./gps-sdr-sim-realtime"
else
  echo "Building default file generator ..."
  gcc src/gpssim.c -O3 -Wall -D_FILE_OFFSET_BITS=64 -lm -o gps-sdr-sim
  echo "OK -> ./gps-sdr-sim"
fi
