#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gps_monitor.py  -  RECEIVE-ONLY GPS receiver monitor for the Mission Control dashboard.
=======================================================================================
Opens the receiver's serial (COM) port read-only and shows, live, what the receiver
under test actually sees:

  * NMEA  ($GxGGA / $GxGSV / $GxRMC / $GxGSA)  ->  fix type, sat count, per-SV C/N0,
    lat / lon / alt, HDOP, speed, UTC.
  * "Spoof confirm": distance from the live fix to the SIMULATED coordinates, and a
    latch when the receiver's position converges onto the simulation.
  * "Cage isolation": the raw sat/fix numbers the dashboard uses to tell you whether the
    real sky is gone (sats -> 0) before you transmit.
  * (Defensive, optional) UBX telemetry:  UBX-NAV-STATUS.spoofDetState,
    UBX-MON-RF / UBX-MON-HW jamming state + jamInd + AGC + noise, and a sudden-C/N0-jump
    heuristic  ->  a "possible spoof / denial" flag.

This module is RECEIVE-ONLY apart from the small, standard UBX *poll* requests it sends to
ask the receiver for its own status telemetry (NAV-STATUS / MON-RF / MON-HW). It never
changes receiver configuration and never transmits RF.

Dependency: pyserial  (pip install pyserial).  If pyserial is missing the dashboard still
runs; this panel just reports that it needs pyserial.
"""

import time, math, threading, struct
from collections import deque

# ---- pyserial is optional; the rest of the dashboard must still run without it ----
try:
    import serial                       # pyserial
    import serial.tools.list_ports as _list_ports
    AVAILABLE = True
    IMPORT_ERROR = ""
except Exception as e:                  # pragma: no cover - environment dependent
    serial = None
    _list_ports = None
    AVAILABLE = False
    IMPORT_ERROR = str(e)

BAUD_OPTIONS = [9600, 38400, 115200, 230400]

# NMEA GGA fix-quality codes
_GGA_QUALITY = {0: "no fix", 1: "GPS fix", 2: "DGPS", 3: "PPS",
                4: "RTK fixed", 5: "RTK float", 6: "dead-reckoning",
                7: "manual", 8: "simulation"}
# NMEA GSA / general fix type
_FIX_TYPE = {1: "no fix", 2: "2D fix", 3: "3D fix"}
# UBX-NAV-STATUS spoofDetState
_SPOOF_STATE = {0: "unknown", 1: "no spoofing", 2: "SPOOFING indicated",
                3: "MULTIPLE spoofing"}
# UBX-MON-RF / MON-HW jammingState
_JAM_STATE = {0: "unknown/disabled", 1: "ok", 2: "WARNING", 3: "CRITICAL"}


def list_ports():
    """Return [{device, description}] for the machine's serial ports (or an error)."""
    if not AVAILABLE:
        return {"available": False, "error": "pyserial not installed (pip install pyserial)",
                "ports": [], "baud_options": BAUD_OPTIONS}
    ports = []
    try:
        for p in _list_ports.comports():
            desc = (p.description or "").strip()
            # de-duplicate the common "COM5 - COM5" case
            if desc.lower().startswith(p.device.lower()):
                desc = desc[len(p.device):].lstrip(" -")
            ports.append({"device": p.device, "description": desc or p.device})
    except Exception as e:
        return {"available": True, "error": "port scan failed: %s" % e,
                "ports": [], "baud_options": BAUD_OPTIONS}
    return {"available": True, "error": "", "ports": ports, "baud_options": BAUD_OPTIONS}


def _f(v):
    """Parse a possibly-empty NMEA numeric field -> float or None."""
    try:
        v = v.strip()
        return float(v) if v not in ("", ".") else None
    except Exception:
        return None


def _nmea_deg(val, hemi):
    """Convert an NMEA ddmm.mmmm / dddmm.mmmm field + hemisphere to signed degrees."""
    f = _f(val)
    if f is None:
        return None
    deg = int(f / 100.0)
    minutes = f - deg * 100.0
    dec = deg + minutes / 60.0
    if hemi in ("S", "W"):
        dec = -dec
    return dec


def _checksum_ok(sentence):
    """Validate an NMEA '*HH' checksum if present; accept the sentence if absent."""
    star = sentence.rfind("*")
    if star < 0 or star + 3 > len(sentence):
        return True                      # no checksum supplied -> don't reject
    body = sentence[1:star]              # between '$' and '*'
    try:
        want = int(sentence[star + 1:star + 3], 16)
    except ValueError:
        return True
    got = 0
    for ch in body:
        got ^= ord(ch)
    return got == want


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


class GpsMonitor:
    """Background reader that parses NMEA (+ optional UBX) from a receiver's COM port."""

    CONVERGE_M = 150.0                    # within this many metres = "on the sim position"
    CONVERGE_HITS = 3                     # consecutive in-range fixes to latch "converged"
    CN0_JUMP_DB = 10.0                    # sudden rise in strongest C/N0 that looks spoof-ish
    POLL_EVERY = 1.0                      # seconds between UBX status polls

    def __init__(self):
        self.lock = threading.Lock()
        self._ser = None
        self._thread = None
        self._stop = threading.Event()
        self.port = ""
        self.baud = 0
        self.error = ""
        self._reset_state()
        # target = the simulated coordinates the operator is trying to force
        self._target = {"lat": None, "lon": None, "alt": None, "set": False, "src": ""}

    # -------------------------------------------------------------- state
    def _reset_state(self):
        self.bytes_in = 0
        self.sentences = 0
        self.last_raw = ""
        self.last_data_t = 0.0
        self.connected = False
        # position / fix
        self.fix_quality = 0
        self.fix_type = 1
        self.pos_valid = False
        self.lat = None
        self.lon = None
        self.alt = None
        self.hdop = None
        self.pdop = None
        self.vdop = None
        self.sats_used = 0                # GGA numSV (used in solution)
        self.sats_in_view = 0            # GSV numSV (in view)
        self.speed_kn = None
        self.course = None
        self.utc = ""
        self.date = ""
        # per-SV C/N0: (talker,prn) -> (cn0, t)
        self._cn0 = {}
        self._gsv_acc = {}               # talker -> {prn:cn0} being assembled
        # convergence
        self._conv_hits = 0
        self.converged = False
        self.distance_m = None
        # C/N0 jump history: deque of (t, max_cn0)
        self._cn0_hist = deque(maxlen=60)
        # UBX telemetry
        self.ubx_seen = False
        self.spoof_state = None
        self.jam_state = None
        self.jam_ind = None
        self.agc = None
        self.noise = None
        self.ant_status = None
        self.ant_power = None
        self.ubx_t = 0.0

    # -------------------------------------------------------------- target
    def set_target(self, lat, lon, alt=None, src=""):
        try:
            lat = float(lat); lon = float(lon)
        except (TypeError, ValueError):
            return
        with self.lock:
            self._target = {"lat": lat, "lon": lon,
                            "alt": (float(alt) if alt not in (None, "") else None),
                            "set": True, "src": src}
            # a new target invalidates any prior convergence latch
            self._conv_hits = 0
            self.converged = False

    # -------------------------------------------------------------- open / close
    def open(self, port, baud):
        if not AVAILABLE:
            return (False, "pyserial not installed. Run:  pip install pyserial")
        try:
            baud = int(baud)
        except (TypeError, ValueError):
            return (False, "invalid baud")
        if not port:
            return (False, "no COM port selected")
        self.close()
        try:
            ser = serial.Serial(port=port, baudrate=baud, timeout=0.3)
        except Exception as e:
            with self.lock:
                self.error = str(e)
            return (False, "could not open %s @ %d: %s" % (port, baud, e))
        with self.lock:
            self._reset_state()
            self._ser = ser
            self.port = port
            self.baud = baud
            self.error = ""
            self.connected = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return (True, "opened %s @ %d" % (port, baud))

    def close(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=1.5)
        self._thread = None
        with self.lock:
            ser = self._ser
            self._ser = None
            self.connected = False
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        return True

    # -------------------------------------------------------------- reader thread
    def _run(self):
        buf = bytearray()
        last_poll = 0.0
        while not self._stop.is_set():
            ser = self._ser
            if ser is None:
                break
            # periodic, standard UBX status polls (receive-only telemetry requests)
            now = time.time()
            if now - last_poll >= self.POLL_EVERY:
                last_poll = now
                self._poll_ubx(ser)
            try:
                chunk = ser.read(4096)
            except Exception as e:
                with self.lock:
                    self.error = "read error: %s" % e
                    self.connected = False
                break
            if not chunk:
                continue
            with self.lock:
                self.bytes_in += len(chunk)
                self.last_data_t = time.time()
            buf.extend(chunk)
            self._consume(buf)
            if len(buf) > 65536:          # guard against runaway on a wrong baud
                del buf[:-4096]

    def _poll_ubx(self, ser):
        try:
            ser.write(_ubx(0x01, 0x03))   # NAV-STATUS
            ser.write(_ubx(0x0A, 0x38))   # MON-RF  (M8+)
            ser.write(_ubx(0x0A, 0x09))   # MON-HW  (older / fallback)
        except Exception:
            pass

    def _consume(self, buf):
        """Pull complete NMEA lines and UBX frames out of the shared byte buffer."""
        while buf:
            b0 = buf[0]
            if b0 == 0xB5 and len(buf) >= 2 and buf[1] == 0x62:
                if len(buf) < 8:
                    return                # need header + length + checksum
                ln = buf[4] | (buf[5] << 8)
                if ln > 4096:             # implausible -> corrupt, resync
                    del buf[:2]
                    continue
                total = 6 + ln + 2
                if len(buf) < total:
                    return                # frame not fully arrived yet
                frame = bytes(buf[:total])
                del buf[:total]
                self._handle_ubx(frame)
            elif b0 == 0x24:              # '$' -> NMEA
                nl = buf.find(b"\n")
                if nl < 0:
                    if len(buf) > 200:    # over-long junk, drop the stray '$'
                        del buf[:1]
                    return
                line = bytes(buf[:nl]).decode("ascii", "ignore").strip()
                del buf[:nl + 1]
                if line:
                    self._handle_nmea(line)
            else:
                del buf[:1]              # resync

    # -------------------------------------------------------------- NMEA
    def _handle_nmea(self, line):
        if not line.startswith("$") or not _checksum_ok(line):
            return
        star = line.rfind("*")
        core = line[1:star] if star >= 0 else line[1:]
        f = core.split(",")
        typ = f[0]
        if len(typ) < 5:
            return
        kind = typ[2:5]                   # GGA / RMC / GSV / GSA (talker-agnostic)
        talker = typ[0:2]
        with self.lock:
            self.sentences += 1
            self.last_raw = line
            try:
                if kind == "GGA":
                    self._gga(f)
                elif kind == "RMC":
                    self._rmc(f)
                elif kind == "GSA":
                    self._gsa(f)
                elif kind == "GSV":
                    self._gsv(talker, f)
            except Exception:
                pass                      # never let one bad sentence kill the reader

    def _gga(self, f):
        # $--GGA,utc,lat,N,lon,E,qual,numSV,HDOP,alt,M,...
        self.utc = f[1] if len(f) > 1 else ""
        q = _f(f[6]) if len(f) > 6 else None
        if q is not None:
            self.fix_quality = int(q)
        n = _f(f[7]) if len(f) > 7 else None
        if n is not None:
            self.sats_used = int(n)
        self.hdop = _f(f[8]) if len(f) > 8 else self.hdop
        lat = _nmea_deg(f[2], f[3]) if len(f) > 3 else None
        lon = _nmea_deg(f[4], f[5]) if len(f) > 5 else None
        alt = _f(f[9]) if len(f) > 9 else None
        if lat is not None and lon is not None and self.fix_quality > 0:
            self.lat, self.lon = lat, lon
            if alt is not None:
                self.alt = alt
            self._update_convergence()

    def _rmc(self, f):
        # $--RMC,utc,status,lat,N,lon,E,speed,course,date,...
        status = f[2] if len(f) > 2 else ""
        self.pos_valid = (status == "A")
        self.speed_kn = _f(f[7]) if len(f) > 7 else self.speed_kn
        self.course = _f(f[8]) if len(f) > 8 else self.course
        self.date = f[9] if len(f) > 9 else self.date
        lat = _nmea_deg(f[3], f[4]) if len(f) > 4 else None
        lon = _nmea_deg(f[5], f[6]) if len(f) > 6 else None
        if self.pos_valid and lat is not None and lon is not None:
            self.lat, self.lon = lat, lon
            self._update_convergence()

    def _gsa(self, f):
        # $--GSA,mode,fixType,sv...,PDOP,HDOP,VDOP
        ft = _f(f[2]) if len(f) > 2 else None
        if ft is not None:
            self.fix_type = int(ft)
        if len(f) >= 3:
            self.pdop = _f(f[-3])
            self.hdop = _f(f[-2]) if _f(f[-2]) is not None else self.hdop
            self.vdop = _f(f[-1])

    def _gsv(self, talker, f):
        # $--GSV,numMsg,msgNum,numSV, [prn,elev,az,cn0]x up to 4
        num_msg = _f(f[1]); msg_num = _f(f[2]); in_view = _f(f[3])
        if in_view is not None:
            self.sats_in_view = int(in_view)
        acc = self._gsv_acc.setdefault(talker, {})
        i = 4
        while i + 3 < len(f) + 1 and i + 3 <= len(f):
            prn = _f(f[i]); cn0 = _f(f[i + 3])
            if prn is not None:
                acc[int(prn)] = cn0 if cn0 is not None else 0.0
            i += 4
        # when the multi-message GSV set for this talker finishes, commit it
        if msg_num is not None and num_msg is not None and msg_num >= num_msg:
            now = time.time()
            for prn, cn0 in acc.items():
                self._cn0[(talker, prn)] = (cn0, now)
            self._gsv_acc[talker] = {}

    def _update_convergence(self):
        t = self._target
        if not t["set"] or self.lat is None or self.lon is None:
            return
        d = haversine_m(self.lat, self.lon, t["lat"], t["lon"])
        self.distance_m = d
        if d <= self.CONVERGE_M:
            self._conv_hits += 1
            if self._conv_hits >= self.CONVERGE_HITS:
                self.converged = True
        else:
            self._conv_hits = 0
            self.converged = False

    # -------------------------------------------------------------- UBX
    def _handle_ubx(self, frame):
        cls, mid = frame[2], frame[3]
        ln = frame[4] | (frame[5] << 8)
        payload = frame[6:6 + ln]
        # verify Fletcher checksum
        ck_a = ck_b = 0
        for byte in frame[2:6 + ln]:
            ck_a = (ck_a + byte) & 0xFF
            ck_b = (ck_b + ck_a) & 0xFF
        if len(frame) < 6 + ln + 2 or (ck_a, ck_b) != (frame[6 + ln], frame[6 + ln + 1]):
            return
        with self.lock:
            self.ubx_seen = True
            self.ubx_t = time.time()
            try:
                if cls == 0x01 and mid == 0x03:
                    self._ubx_nav_status(payload)
                elif cls == 0x0A and mid == 0x38:
                    self._ubx_mon_rf(payload)
                elif cls == 0x0A and mid == 0x09:
                    self._ubx_mon_hw(payload)
            except Exception:
                pass

    def _ubx_nav_status(self, p):
        if len(p) < 8:
            return
        flags2 = p[7]
        self.spoof_state = (flags2 >> 3) & 0x03

    def _ubx_mon_rf(self, p):
        # version,u1 nBlocks,u1 reserved,u2  then 24-byte blocks
        if len(p) < 4:
            return
        n = p[1]
        base = 4
        for b in range(n):
            off = base + b * 24
            if off + 24 > len(p):
                break
            flags = p[off + 1]
            self.jam_state = flags & 0x03
            self.ant_status = p[off + 2]
            self.ant_power = p[off + 3]
            self.noise = p[off + 12] | (p[off + 13] << 8)
            self.agc = p[off + 14] | (p[off + 15] << 8)
            self.jam_ind = p[off + 16]
            return                        # first block (L1) is enough

    def _ubx_mon_hw(self, p):
        if len(p) < 46:
            return
        # only fill fields MON-RF didn't already provide (MON-RF preferred)
        if self.noise is None:
            self.noise = p[16] | (p[17] << 8)
        if self.agc is None:
            self.agc = p[18] | (p[19] << 8)
        if self.ant_status is None:
            self.ant_status = p[20]
        if self.ant_power is None:
            self.ant_power = p[21]
        if self.jam_state is None:
            self.jam_state = (p[22] >> 2) & 0x03
        if self.jam_ind is None:
            self.jam_ind = p[45]

    # -------------------------------------------------------------- snapshot
    def _cn0_summary(self, now):
        vals = [c for (c, t) in self._cn0.values() if c and c > 0 and now - t < 6.0]
        vals.sort(reverse=True)
        cn0_max = vals[0] if vals else None
        cn0_avg = round(sum(vals) / len(vals), 1) if vals else None
        # sudden-jump heuristic over the strongest C/N0
        jump = 0.0
        if cn0_max is not None:
            self._cn0_hist.append((now, cn0_max))
            window = [c for (t, c) in self._cn0_hist if 3.0 < now - t < 15.0]
            if window:
                jump = round(cn0_max - min(window), 1)
        return cn0_max, cn0_avg, len(vals), vals[:12], jump

    def _detection(self, now, cn0_jump):
        """Fold UBX telemetry + the C/N0 jump into a single defensive flag."""
        reasons = []
        level = "clear"                   # clear | caution | alert
        if self.spoof_state is not None and self.spoof_state >= 2:
            level = "alert"
            reasons.append("UBX spoofDetState = %s" % _SPOOF_STATE.get(self.spoof_state))
        if self.jam_state is not None and self.jam_state >= 3:
            level = "alert"
            reasons.append("jamming CRITICAL")
        elif self.jam_state is not None and self.jam_state == 2:
            level = "alert" if level == "alert" else "caution"
            reasons.append("jamming WARNING")
        if self.jam_ind is not None and self.jam_ind >= 45:
            level = "alert" if level == "alert" else "caution"
            reasons.append("jamInd %d/255 high" % self.jam_ind)
        if cn0_jump >= self.CN0_JUMP_DB:
            level = "alert" if level == "alert" else "caution"
            reasons.append("C/N0 jumped +%.0f dB" % cn0_jump)
        fresh = self.ubx_seen and (now - self.ubx_t) < 5.0
        return {
            "ubx": self.ubx_seen, "ubx_fresh": fresh,
            "spoofState": self.spoof_state,
            "spoofText": _SPOOF_STATE.get(self.spoof_state) if self.spoof_state is not None else None,
            "jamState": self.jam_state,
            "jamText": _JAM_STATE.get(self.jam_state) if self.jam_state is not None else None,
            "jamInd": self.jam_ind, "agc": self.agc, "noise": self.noise,
            "antStatus": self.ant_status, "antPower": self.ant_power,
            "cn0_jump": cn0_jump, "flag": level, "reasons": reasons,
        }

    def status(self):
        now = time.time()
        with self.lock:
            if not self.connected:
                return {"available": AVAILABLE, "connected": False,
                        "error": self.error, "import_error": IMPORT_ERROR,
                        "target": dict(self._target)}
            age = now - self.last_data_t if self.last_data_t else None
            cn0_max, cn0_avg, cn0_n, cn0_list, cn0_jump = self._cn0_summary(now)
            data_flowing = age is not None and age < 3.0
            # if bytes are arriving but nothing parses, the baud is probably wrong
            baud_hint = (self.bytes_in > 200 and self.sentences == 0 and not self.ubx_seen)
            snap = {
                "available": AVAILABLE, "connected": True, "error": self.error,
                "port": self.port, "baud": self.baud,
                "bytes_in": self.bytes_in, "sentences": self.sentences,
                "age": round(age, 1) if age is not None else None,
                "data_flowing": data_flowing, "baud_hint": baud_hint,
                "last_raw": self.last_raw,
                "fix_quality": self.fix_quality,
                "fix_quality_text": _GGA_QUALITY.get(self.fix_quality, "?"),
                "fix_type": self.fix_type,
                "fix_type_text": _FIX_TYPE.get(self.fix_type, "?"),
                "pos_valid": self.pos_valid,
                "lat": self.lat, "lon": self.lon, "alt": self.alt,
                "hdop": self.hdop, "pdop": self.pdop, "vdop": self.vdop,
                "sats_used": self.sats_used, "sats_in_view": self.sats_in_view,
                "sats_tracked": cn0_n,
                "cn0_max": cn0_max, "cn0_avg": cn0_avg, "cn0_list": cn0_list,
                "speed_kn": self.speed_kn, "course": self.course,
                "utc": self.utc, "date": self.date,
                "target": dict(self._target),
                "distance_m": round(self.distance_m, 1) if self.distance_m is not None else None,
                "converged": self.converged,
                "detection": self._detection(now, cn0_jump),
            }
        return snap


def _ubx(cls, mid, payload=b""):
    """Build a UBX frame (used only to POLL the receiver for its own status)."""
    body = bytes([cls, mid]) + struct.pack("<H", len(payload)) + payload
    ck_a = ck_b = 0
    for byte in body:
        ck_a = (ck_a + byte) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return b"\xB5\x62" + body + bytes([ck_a, ck_b])
