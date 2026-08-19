# =====================================================================
#  GPS-SDR-SIM  -  "Master of All Master"  -  top-level Makefile
# ---------------------------------------------------------------------
#   make            -> Linux/macOS build         -> ./gps-sdr-sim
#   make realtime   -> Linux build + TCP/UDP live -> ./gps-sdr-sim-realtime
#   make windows    -> cross-compile Windows .exe -> ./gps-sdr-sim.exe   (needs mingw-w64)
#   make clean
# =====================================================================
CC       = gcc
CFLAGS   = -O3 -Wall -D_FILE_OFFSET_BITS=64
LDFLAGS  = -lm
SRC      = src/gpssim.c
WINSRC   = src/gpssim.c src/getopt.c
MINGW    = x86_64-w64-mingw32-gcc

.PHONY: all realtime windows clean

all: gps-sdr-sim

# Default file generator (uses the system getopt on Linux/macOS)
gps-sdr-sim: $(SRC) src/gpssim.h
	$(CC) $(SRC) $(CFLAGS) $(LDFLAGS) -o $@
	@echo "  built ./gps-sdr-sim"

# Optional realtime engine: streams I/Q over TCP to GNURadio (-n) and takes
# live position over UDP (-w).  Linux only (POSIX sockets + pthread).
realtime: $(SRC) src/gpssim.h src/socket.c
	$(CC) $(SRC) $(CFLAGS) -DENABLE_REALTIME $(LDFLAGS) -lpthread -o gps-sdr-sim-realtime
	@echo "  built ./gps-sdr-sim-realtime  (adds -n <port> / -w <port>)"

# Windows .exe (static, no DLLs needed).  Uses the bundled getopt.
windows: $(WINSRC) src/gpssim.h
	$(MINGW) $(WINSRC) $(CFLAGS) -static -static-libgcc $(LDFLAGS) -o gps-sdr-sim.exe
	@echo "  built ./gps-sdr-sim.exe"

clean:
	rm -f gps-sdr-sim gps-sdr-sim.exe gps-sdr-sim-realtime src/*.o
