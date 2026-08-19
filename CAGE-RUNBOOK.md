# Faraday-Cage Demo Runbook

Authorized, RF-shielded GPS receiver testing (counter-UAS / GNSS-resilience R&D).
Everything below happens **inside the shielded chamber, through a cable + attenuator**.
Never radiate GPS over the air — it is illegal and dangerous outside a shielded enclosure.

This runbook is built from a 12-agent troubleshooting pass on your exact symptoms
("no data", "hackrf_transfer not recognized", "15 sats then 0").

---

## 0. Fix the blocker first: `hackrf_transfer not recognized`

This means the HackRF host tools aren't installed / not on PATH — so **nothing was
transmitting** (that alone explains "no data"). Fix once:

1. Run **`SETUP-HACKRF.bat`** (double-click). If it prints a device block, you're done.
2. If not found: install **PothosSDR** (https://downloads.myriadrf.org/builds/PothosSDR/,
   keep "Add to PATH" ticked) *or* **radioconda**. Both bundle `hackrf_transfer` + `hackrf_info`.
3. Install the USB driver with **Zadig** (https://zadig.akeo.ie/): *Options → List All Devices →
   HackRF One → WinUSB → Install Driver.* Replug the HackRF.
4. Open a **new** terminal / re-launch the dashboard. `hackrf_info` must show
   `Found HackRF … Board ID 2 (HackRF One)`.

Common gotchas: charge-only USB cable, USB-3 hub (use a rear USB-2 port), or you didn't
open a fresh terminal after install.

---

## 1. Load a constellation file

Drop the **latest** NASA `brdc` into `nav/`. Today (2026-08-17) it's `brdc2290.26n`
(day-of-year 229). Login-free mirrors:

- IGN France: `https://igs.ign.fr/pub/igs/data/2026/229/`
- BKG Germany: `https://igs.bkg.bund.de/root_ftp/IGS/BRDC/2026/229/`
- NASA CDDIS (free login): `https://cddis.nasa.gov/archive/gnss/data/daily/2026/229/26n/brdc2290.26n.gz`

Fresh file → satellites are at true positions and you don't even need `-T`. Using the old
2022 sample still works if you keep **"force time (`-T`)"** ticked (GPS week rollover is *not*
a problem — 2022 and 2026 are the same 1024-week epoch).

## 2. Generate (dashboard step 1)

Enter lat/lon/alt, duration (300 s is a good default — it loops anyway), date/time = now (UTC).
Leave **constant-power OFF** (realistic path-loss, 0 % clipping — verified). Press **GENERATE**.

## 3. Self-test before you transmit (dashboard step 2)

Press **Verify the .bin**. It runs a real GPS acquisition and must say **VERDICT: PASS**
with the satellites it locked. If PASS, the file is genuine, lockable GPS — the problem (if
any) is then purely RF/receiver, not the file. (Needs `pip install numpy` once.)

## 4. Transmit (dashboard step 3)

Tick **"shielded chamber confirmed"**, then **START TRANSMIT**. The dashboard always loops
(`-R`) so the signal never ends. Defaults, from the research:

- **RF amp OFF** (`-a 0`) for a cabled/attenuated link.
- **TX gain `-x 20`** to start.
- Loop `-R` always on → fixes the "15 sats then 0" (that was the file ending).

Equivalent command it runs:
```
hackrf_transfer -t output\yourfile.bin -f 1575420000 -s 2600000 -a 0 -x 20 -R
```

## 5. Configure the receiver

**u-blox M10 / DIY drone (u-center):**
- GPS-only: *View → Generation 9/10 Config → CFG-SIGNAL* → GPS + L1C/A **on**,
  Galileo/BeiDou/GLONASS/QZSS/SBAS **off** → apply to RAM+BBR+Flash.
- Cold start: *UBX-CFG-RST → navBbrMask = 0xFFFF, Controlled software reset.*
- Watch: *UBX-NAV-SAT* (C/N0 bars), *UBX-NAV-STATUS* (TTFF, spoofDetState), *UBX-MON-RF* (AGC).
- Expect a fix in ~30 s once 4+ GPS SVs sit at 35–45 dB-Hz. (spoofDetState is a *flag only*;
  it does **not** block the fix.)

**DJI drone:** it fuses IMU + vision, so keep it **stationary and powered independently** so a
zero-velocity fix is consistent (avoids a fusion sanity-reject). Couple loosely to the internal
patch antenna via an attenuated near-field pad — don't blast it. Success = sat count climbs to
the model's "ready" threshold and the **home point / coordinates jump to your simulated lat/lon**.

---

## 5b. Spoof-confirm monitor & cage-isolation gate (dashboard step 4)

This is the panel that makes the spoof **observable** and tells you *why* a bench spoof "does
nothing": outdoors the receiver holds a strong real multi-GNSS fix (~13 sats, HDOP ~0.9) and
ignores the weaker simulated L1. The sim only wins once the real signal is gone.

**Wire it up (receive-only):** connect the receiver's USB/UART to this PC. In the dashboard's
**Spoof-confirm monitor** card pick the **COM port** and **baud** (u-blox default 9600; M9/M10 are
often 38400/115200), then **Connect**. It parses NMEA live. Install once with
`pip install pyserial`. The monitor never transmits and never reconfigures the receiver — it only
sends the standard read-only UBX poll requests (same as u-center) to read status telemetry.

**Use it as an isolation gate — the correct order:**

1. **Antenna in, cage OPEN, TX OFF.** You should see the *real* sky: many sats, a 3D fix, HDOP < 2.
   This confirms the receiver and the monitor are working.
2. **Seal the cage, TX still OFF.** Watch the sat count **fall toward 0**. The banner turns green
   **ISOLATED** when the real signal is gone. If it stays **NOT ISOLATED** (red) with sats still
   high, the cage is leaking or the antenna is outside the shield — **fix that before transmitting**,
   because the real fix will always beat the sim.
3. **Now START TRANSMIT.** The receiver re-acquires — this time from your HackRF. The banner shows
   *acquiring → fix present → converging*, and latches **SPOOF CONFIRMED** (magenta) when the
   receiver's reported position snaps onto your simulated lat/lon (within ~150 m). That jump is the
   proof the spoof took.

**Convergence target.** The monitor uses the coordinates from your last **Generate** automatically.
To check against arbitrary coordinates, type them in the Generate form and click *"Use lat/lon from
Generate form as target"*.

**Defensive detection panel (counter-UAS resilience).** Expand *DEFENSIVE · spoof / jamming
detection*. For a u-blox receiver it shows **spoofDetState** (UBX-NAV-STATUS), **jamming state /
jamInd / AGC / noise** (UBX-MON-RF or MON-HW) and a **sudden C/N0 jump** heuristic, and raises a
*possible spoof / denial* flag. These are telemetry flags only — the receiver still outputs a fix
even when they trip — but they are exactly what a resilient platform would watch to *notice* it is
being spoofed or denied.

---

## 6. "Still nothing / it locks then drops" — decision tree

| Symptom | Most likely cause | Fix |
|---|---|---|
| No sats appear at all | Not transmitting | `hackrf_info` OK? TX panel shows **TRANSMITTING**? amp/gain not zero? |
| Sats appear then **drop to 0 permanently** | Signal ended | Ensure looping (`-R`) — the dashboard does this automatically |
| Sats **flicker**, lock then drop repeatedly | HackRF clock drift | Warm HackRF up 3–5 min; nudge **frequency-offset** slider ±5 kHz to find lock |
| High C/N0 but no/unstable fix; drops when strongest | **Overpower** (desense) | Lower **TX gain**, keep **amp OFF**, add more attenuation |
| Weak C/N0 (<30), slow/no acquire | Too little power | Raise TX gain a few dB, or reduce attenuation |
| Verify says PASS but receiver won't lock | RF link / receiver config | Check coupling, GPS-only + cold start, plausible time |
| Verify says FAIL | Bad file / wrong rate | Regenerate; keep sample rate 2.6 MHz on both sides |
| Spoof "does nothing"; receiver keeps a real fix | Real GNSS still present (leaky/open cage) | Seal cage; use the **Spoof-confirm monitor** — wait for **ISOLATED** (sats → 0) *before* TX |

**30-second checklist:** `hackrf_info` sees the board → TX panel says TRANSMITTING →
`-R` looping → `-s 2600000` both sides → gain moderate, amp off → ephemeris fresh (or `-T` on)
→ receiver cold-started, GPS-only.

---

## Frequency-offset tip (your acquire-then-drop clue)

A stock HackRF crystal is ±20 ppm ≈ ±31 kHz at L1. A steady offset usually still falls inside
the receiver's search window, but **drift** (first minutes of warm-up) makes it lock then slip.
Let it warm up, and use the dashboard's **frequency-offset slider** to sweep ±5–15 kHz until the
receiver locks. A 0.5 ppm TCXO makes it rock-solid if you want a permanent fix.
