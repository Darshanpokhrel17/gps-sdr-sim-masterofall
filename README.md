# GPS-SDR-SIM — "Master of All Master" (v2)

A single, optimized GPS L1 C/A signal system for **HackRF One**, with a one-click **Mission
Control dashboard** that now **generates, self-tests, AND transmits** — built for authorized
RF-shielded testing (Faraday-cage / counter-UAS demos).

It merges your two source projects (newest upstream file-generator + the realtime fork) into one
engine, then wraps it in a dashboard that removes the terminal from the loop. See
`docs/ANALYSIS.md` for the codebase comparison and `docs/TARGET-PROFILES.md` for the DJI / u-blox
drone receiver profiles.

> ⚠️ **Transmit only inside a shielded chamber / Faraday cage, through a cable + attenuator.**
> Radiating GPS over the air is illegal and dangerous. The dashboard won't transmit until you
> confirm the shielded-chamber switch.

---

## What v2 fixes (from a 12-agent troubleshooting pass)

Your symptoms were "no data", "hackrf_transfer not recognized", and "15 sats then 0". Root causes
and fixes now baked in:

1. **`hackrf_transfer not recognized`** → the HackRF host tools weren't installed, so nothing
   transmitted. Run **`SETUP-HACKRF.bat`**; the dashboard also auto-detects the tools and lets you
   point at their folder.
2. **"15 sats → 0 after a few seconds"** → the file played once and ended. The dashboard now
   **always loops (`-R`)**.
3. **Acquire-then-drop** → HackRF crystal drift; the dashboard has a **frequency-offset slider** to
   sweep onto lock, plus a "warm it up" note.
4. **Overpower desense** → transmit defaults are **amp OFF + moderate gain**, with a live gain slider.
5. **`-p` clipping** → verified it clips 8-bit at gain 128; the default is now realistic
   **path-loss (0% clipping)**.
6. **Is the file even valid?** → a built-in **acquisition self-test** proves the bin locks real
   satellites before you transmit.

---

## What v3 adds — Spoof-confirm monitor (`gps_monitor.py`)

The bench problem: outdoors (or with a leaky cage) the receiver keeps a strong **real** multi-GNSS
fix and simply ignores the weaker simulated L1. The sim only wins once the real sky is gone. v3
makes that **observable** with a new **receive-only** panel in the dashboard:

1. **Spoof-confirm monitor** — open the receiver's COM port read-only (needs `pip install pyserial`),
   parse NMEA (`$GxGGA/$GxGSV/$GxRMC/$GxGSA`) and show live **fix type, sats used/tracked, C/N0,
   lat/lon/alt, HDOP, speed**. It knows the coordinates you generated and **latches "SPOOF
   CONFIRMED"** when the receiver's position converges onto them (default within 150 m).
2. **Cage-isolation gate** — before you transmit, seal the cage and watch sats fall toward **0**.
   The panel reads **ISOLATED** when the real signal is gone (safe to transmit) and **NOT ISOLATED**
   (red) if sats stay high — a leaky cage or an antenna outside the shield.
3. **One-click "Regenerate @ now (UTC)"** — picks the freshest `brdc`, stamps the server's UTC time
   and forces `-T` so the receiver locks to *now*. The dashboard also flags a **stale** ephemeris
   file vs. today's date.
4. **Defensive detection (optional)** — polls u-blox **UBX-NAV-STATUS.spoofDetState**,
   **UBX-MON-RF / MON-HW** (jamming state, `jamInd`, AGC, noise) and watches for a sudden C/N0 jump,
   raising a **"possible spoof / denial"** flag. This is the counter-UAS *resilience* side.

> The monitor is **receive-only**: it never transmits RF and never changes receiver configuration.
> It sends only the standard read-only UBX *poll* requests (the same ones u-center uses) to read
> back the receiver's own status telemetry.

---

## The workflow (barely any terminal)

1. **Install HackRF tools once** — double-click `SETUP-HACKRF.bat` (or install PothosSDR/radioconda
   + Zadig). Verify `hackrf_info` shows the board.
2. **Drop a NASA `brdc` file** into `nav/` (today = `brdc2290.26n`; a 2022 sample is included to test).
3. **Start the dashboard** — double-click `START-DASHBOARD.bat` → opens `http://127.0.0.1:8770`.
4. In the dashboard:
   - **① Generate** — set lat/lon/alt, duration, time (defaults to now UTC) → **GENERATE BIN FILE**.
   - **② Self-test** — **Verify the .bin** → must say **PASS** (locks the right satellites).
   - **③ Transmit** — tick *shielded-chamber confirmed*, set gain, **START TRANSMIT** (loops). Tune
     the gain / frequency-offset sliders live until the receiver locks. **STOP** when done.
5. **Configure the receiver** — see `CAGE-RUNBOOK.md` (u-blox M10 GPS-only + cold start; DJI coupling).

Everything runs **offline** (pure Python standard library) — fine underground.

---

## Folder layout

```
GPS-SDR-SIM-MasterOfAll/
├─ gps-sdr-sim.exe        ← prebuilt Windows engine (no compiler needed)
├─ dashboard.py           ← Mission Control (generate + verify + transmit + spoof-confirm monitor)
├─ gps_monitor.py         ← receive-only NMEA/UBX serial monitor (needs pyserial)
├─ verify_signal.py       ← independent GPS acquisition self-test (needs numpy)
├─ START-DASHBOARD.bat    ← double-click to run the dashboard
├─ SETUP-HACKRF.bat       ← fixes "hackrf_transfer not recognized"
├─ CAGE-RUNBOOK.md        ← the field runbook + troubleshooting decision tree
├─ build.bat / build.sh / Makefile   ← rebuild the engine (optional)
├─ nav/                   ← put NASA brdc files here (sample included)
├─ output/                ← generated .bin files
├─ src/                   ← merged C engine (gpssim.c, socket.c, getopt.*)
└─ docs/                  ← ANALYSIS.md, TARGET-PROFILES.md, gnuradio_top_block.py
```

---

## Requirements

- **Windows 10/11** — the `.exe` is prebuilt & static (no DLLs, no compiler).
- **Python 3** on PATH — to run the dashboard (and `pip install numpy` for the self-test).
- **HackRF host tools** — `hackrf_transfer` / `hackrf_info` (via `SETUP-HACKRF.bat`).
- **pyserial** — `pip install pyserial`, only for the receive-only spoof-confirm monitor. The rest
  of the dashboard runs without it (the monitor panel just says it needs pyserial).

---

## Command-line equivalents (updated to today, 2026/08/17)

Generate (path-loss default, time-locked to now):
```
gps-sdr-sim.exe -e nav/brdc2290.26n -b 8 -l 27.700769,85.300140,1337 -d 300 -t 2026/08/17,06:00:00 -T 2026/08/17,06:00:00 -o output/kathmandu.bin
```
Transmit — **note `-R` (loop) and amp off** for a cabled chamber:
```
hackrf_transfer -t output/kathmandu.bin -f 1575420000 -s 2600000 -a 0 -x 20 -R
```
- `-R` loops forever (fixes "sats drop to 0").
- `-a 0` amp off, `-x 20` TX gain — start here, lower if it locks then drops (overpower).
- If it won't lock, sweep `-f` a few kHz around 1575420000 (HackRF crystal offset).

---

## File-size vs duration

8-bit @ 2.6 Msps ≈ **312 MB/min**. It loops (`-R`), so a short file is fine:

| Duration | Size |
|---|---|
| 120 s | ~0.6 GB |
| 300 s | ~1.6 GB |
| 600 s (cap) | ~3.1 GB |

The dashboard shows the estimate live and caps at 600 s.

---

## Advanced: realtime / moving-target (Linux)

`make realtime` builds `gps-sdr-sim-realtime` with `-n` (stream to GNURadio) and `-w` (live UDP
position). Not needed for the file-based cage demo. Details in `docs/`.

---

Merged from `osqzss/gps-sdr-sim` (latest) + the realtime fork. Original © Takuji Ebinuma, MIT.
For authorized, RF-shielded testing only.
