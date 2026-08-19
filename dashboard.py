#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GPS-SDR-SIM  .  "Master of All Master"  .  Mission Control  v2
==============================================================
Zero-dependency local dashboard (Python standard library only - no pip,
works fully offline; ideal for an underground / Faraday-cage demo).

v2 adds the parts that make it actually WORK in the chamber:
  * detects the gps-sdr-sim engine AND the HackRF tools (hackrf_transfer / hackrf_info)
  * one-click HackRF device check (proves the tools + USB are alive)
  * GENERATE the .bin  (realistic path-loss, no clipping; -T time-lock; today's UTC)
  * VERIFY the .bin is real, lockable GPS before you transmit (self-test)
  * TRANSMIT straight from the dashboard: always loops (-R), live TX-gain slider,
    RF-amp toggle, and a frequency-offset slider to sweep onto lock if the HackRF
    crystal is off.  START / STOP.  No terminal needed.
  * transmit is gated behind a "shielded chamber confirmed" switch.

Run:  python dashboard.py     (or double-click START-DASHBOARD.bat on Windows)
"""

import os, re, sys, json, time, shutil, threading, subprocess, webbrowser
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# Receive-only serial GPS monitor (optional pyserial). Never blocks dashboard startup.
try:
    import gps_monitor
    GPS = gps_monitor.GpsMonitor()
except Exception as _e:                 # pragma: no cover
    gps_monitor = None
    GPS = None

HERE    = os.path.dirname(os.path.abspath(__file__))
NAV_DIR = os.path.join(HERE, "nav")
OUT_DIR = os.path.join(HERE, "output")
CFG     = os.path.join(HERE, ".hackrf_path")     # remembers a custom HackRF bin folder
PORT    = 8770
MAX_DUR = 600                 # hard cap (10 min) per requirement
L1_HZ   = 1575420000          # GPS L1 C/A
os.makedirs(NAV_DIR, exist_ok=True); os.makedirs(OUT_DIR, exist_ok=True)
NAV_RE = re.compile(r".*\.(\d{2}[nNgGpP]|nav|rnx|obs)$")
IS_WIN = (os.name == "nt")

# Last simulated position we generated for (used by the spoof-confirm monitor).
LAST_SIM = {"lat": None, "lon": None, "alt": None}

# ------------------------------------------------------------------ tool finding
COMMON_HACKRF_DIRS = [
    r"C:\Program Files\PothosSDR\bin",
    r"C:\Program Files\HackRF\bin",
    r"C:\Program Files (x86)\PothosSDR\bin",
    os.path.expanduser(r"~\radioconda\Library\bin"),
    os.path.expanduser(r"~\miniconda3\Library\bin"),
    "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin",
]

def _custom_dir():
    try:
        if os.path.isfile(CFG):
            d = open(CFG).read().strip()
            return d if d and os.path.isdir(d) else ""
    except Exception:
        pass
    return ""

def find_tool(names, extra_dir=""):
    dirs = ([extra_dir] if extra_dir else []) + ([_custom_dir()] if _custom_dir() else []) \
           + [HERE, os.path.join(HERE, "bin")] + COMMON_HACKRF_DIRS
    for n in names:
        for d in dirs:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return ""

def find_engine():
    return find_tool(["gps-sdr-sim.exe", "gps-sdr-sim"] if IS_WIN else ["gps-sdr-sim", "gps-sdr-sim.exe"])
def find_hackrf():
    return find_tool(["hackrf_transfer.exe", "hackrf_transfer"])
def find_hackrf_info():
    return find_tool(["hackrf_info.exe", "hackrf_info"])

# ------------------------------------------------------------------ nav files
def rinex_epoch(path):
    try:
        head = open(path, "r", errors="ignore").read(8000)
        m = re.search(r"\n[A-Z]\d{2}\s+(\d{4})\s+(\d{1,2})\s+(\d{1,2})\s", head)
        if m: return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.search(r"\n\s?\d{1,2}\s+(\d{2})\s+(\d{1,2})\s+(\d{1,2})\s+\d", head)
        if m:
            yy = int(m.group(1)); yr = 2000+yy if yy < 80 else 1900+yy
            return "%04d-%02d-%02d" % (yr, int(m.group(2)), int(m.group(3)))
    except Exception: pass
    return ""

def _today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def list_nav_files():
    today = _today_utc()
    out, seen = [], set()
    for d, tag in ((NAV_DIR, "nav/"), (HERE, "")):
        try:
            for fn in sorted(os.listdir(d)):
                full = os.path.join(d, fn)
                if os.path.isfile(full) and NAV_RE.match(fn) and full not in seen:
                    seen.add(full)
                    ep = rinex_epoch(full)
                    out.append({"name": fn, "path": full, "rel": tag+fn,
                                "size": os.path.getsize(full), "epoch": ep,
                                "fresh": bool(ep) and ep == today})
        except Exception: pass
    # newest epoch first, then today's file ahead of everything (stable sorts)
    out.sort(key=lambda f: (f.get("epoch") or "", f["name"]), reverse=True)
    out.sort(key=lambda f: not f.get("fresh"))
    return out

def human(n):
    n = float(n)
    for u in ("B","KB","MB","GB","TB"):
        if n < 1024 or u == "TB":
            return ("%.0f %s" % (n,u)) if u == "B" else ("%.2f %s" % (n,u))
        n /= 1024.0

# ================================================================== GENERATE job
class Gen:
    def __init__(s):
        s.lock=threading.Lock(); s.running=False; s.progress=0.0
        s.log=[]; s.status="idle"; s.result=None; s.cmd=""
    def snap(s):
        with s.lock:
            return {"running":s.running,"progress":round(s.progress,1),
                    "log":"\n".join(s.log[-40:]),"status":s.status,"result":s.result,"cmd":s.cmd}
GEN = Gen()

def run_generation(p):
    eng = find_engine()
    if not eng:
        with GEN.lock:
            GEN.status="error"; GEN.running=False
            GEN.log=["ERROR: gps-sdr-sim engine not found.",
                     "Put gps-sdr-sim.exe next to this dashboard, or run build.bat."]
        return
    dur = max(1, min(MAX_DUR, int(p["duration"])))
    lat, lon, alt = float(p["lat"]), float(p["lon"]), float(p["alt"])
    LAST_SIM.update({"lat": lat, "lon": lon, "alt": alt})
    if GPS is not None:
        GPS.set_target(lat, lon, alt, src="generated")   # spoof-confirm convergence target
    bits, samp = str(p.get("bits","8")), str(p.get("samp","2600000"))
    outname = re.sub(r"[^A-Za-z0-9._-]","_",p.get("outfile","gpssim.bin")) or "gpssim.bin"
    if not outname.lower().endswith(".bin"): outname += ".bin"
    outpath = os.path.join(OUT_DIR, outname)
    argv = [eng, "-e", p["navpath"], "-b", bits, "-s", samp,
            "-l", "%.6f,%.6f,%.1f"%(lat,lon,alt), "-d", str(dur)]
    dt = p.get("datetime","").strip()
    if dt:
        argv += ["-t", dt]
        if p.get("forceT", True): argv += ["-T", dt]
    if p.get("fixedgain", False): argv += ["-p", "80"]   # verified clip-free constant power
    if p.get("noiono", False):    argv += ["-i"]
    argv += ["-o", outpath]
    sh = lambda a: " ".join(('"%s"'%x if " " in x else x) for x in a)
    with GEN.lock:
        GEN.running=True; GEN.status="running"; GEN.progress=0.0; GEN.result=None
        GEN.log=["$ "+sh(argv),""]; GEN.cmd=sh(argv)
    t0=time.time()
    try:
        proc=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,bufsize=0,cwd=HERE)
        buf=b""
        while True:
            ch=proc.stdout.read(2048)
            if not ch: break
            buf+=ch
            for ln in buf.replace(b"\r",b"\n").decode("utf-8","ignore").split("\n")[:-1]:
                ln=ln.rstrip()
                if not ln: continue
                m=re.search(r"Time into run\s*=\s*([0-9.]+)",ln)
                with GEN.lock:
                    if m:
                        GEN.progress=min(100.0,float(m.group(1))/dur*100.0)
                        msg="Generating signal ... %5.1f / %d s"%(float(m.group(1)),dur)
                        if GEN.log and GEN.log[-1].startswith("Generating signal"): GEN.log[-1]=msg
                        else: GEN.log.append(msg)
                    else: GEN.log.append(ln)
            buf=buf.replace(b"\r",b"\n").split(b"\n")[-1]
        proc.wait(); el=time.time()-t0
        ok = proc.returncode==0 and os.path.isfile(outpath) and os.path.getsize(outpath)>0
        with GEN.lock:
            GEN.running=False
            if ok:
                sz=os.path.getsize(outpath); GEN.progress=100.0; GEN.status="done"
                GEN.result={"outfile":outpath,"outname":outname,"size":sz,"size_h":human(sz),
                            "seconds":dur,"gen_time":round(el,1),"samp":samp,"cmd":sh(argv)}
                GEN.log.append(""); GEN.log.append("DONE -> %s (%s) in %.1fs"%(outname,human(sz),el))
            else:
                GEN.status="error"; GEN.log.append("ERROR: generation failed (exit %s)."%proc.returncode)
    except Exception as e:
        with GEN.lock: GEN.running=False; GEN.status="error"; GEN.log.append("ERROR: %s"%e)

# ================================================================== VERIFY job
class Ver:
    def __init__(s): s.lock=threading.Lock(); s.running=False; s.log=""; s.status="idle"; s.verdict=""
    def snap(s):
        with s.lock: return {"running":s.running,"log":s.log,"status":s.status,"verdict":s.verdict}
VER = Ver()

def run_verify(binpath, samp):
    with VER.lock: VER.running=True; VER.status="running"; VER.log="Running acquisition self-test ...\n"; VER.verdict=""
    try:
        script=os.path.join(HERE,"verify_signal.py")
        py=sys.executable or ("python" if IS_WIN else "python3")
        proc=subprocess.run([py,script,binpath,"--fs",str(samp)],capture_output=True,text=True,timeout=300,cwd=HERE)
        out=(proc.stdout or "")+(proc.stderr or "")
        v="PASS" if "VERDICT: PASS" in out else ("WEAK" if "VERDICT: WEAK" in out else ("FAIL" if "VERDICT: FAIL" in out else "?"))
        with VER.lock: VER.running=False; VER.status="done"; VER.log=out; VER.verdict=v
    except Exception as e:
        msg=str(e)
        hint="\n(Install numpy for the self-test:  pip install numpy)" if "numpy" in msg.lower() else ""
        with VER.lock: VER.running=False; VER.status="error"; VER.log="verify error: %s%s"%(msg,hint); VER.verdict="?"

# ================================================================== TRANSMIT job
class Tx:
    def __init__(s): s.lock=threading.Lock(); s.proc=None; s.running=False; s.log=deque(maxlen=60); s.cmd=""
    def snap(s):
        with s.lock: return {"running":s.running,"log":"\n".join(s.log),"cmd":s.cmd}
TX = Tx()

def tx_reader(proc):
    try:
        for raw in iter(lambda: proc.stdout.read(1024), b""):
            for ln in raw.replace(b"\r",b"\n").decode("utf-8","ignore").split("\n"):
                ln=ln.strip()
                if ln:
                    with TX.lock: TX.log.append(ln)
    except Exception: pass
    with TX.lock: TX.running=False

def tx_start(p):
    hk=find_hackrf()
    if not hk: return (False,"hackrf_transfer not found. Install the HackRF tools (see the Tools panel).")
    binpath=p.get("binfile","")
    if not binpath or not os.path.isfile(binpath): return (False,"No .bin selected. Generate one first.")
    if not p.get("chamber"): return (False,"Confirm the shielded-chamber switch before transmitting.")
    with TX.lock:
        if TX.running: return (False,"Already transmitting. Stop first.")
    freq=int(L1_HZ)+int(p.get("offset",0))
    samp=str(p.get("samp","2600000")); amp=str(1 if p.get("amp") else 0); gain=str(int(p.get("gain",20)))
    argv=[hk,"-t",binpath,"-f",str(freq),"-s",samp,"-a",amp,"-x",gain,"-R"]
    bb=p.get("bbfilter")
    if bb: argv += ["-b",str(int(bb))]
    sh=" ".join(('"%s"'%x if " " in x else x) for x in argv)
    try:
        proc=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,bufsize=0,cwd=HERE)
    except Exception as e:
        return (False,"Failed to start hackrf_transfer: %s"%e)
    with TX.lock:
        TX.proc=proc; TX.running=True; TX.cmd=sh; TX.log.clear()
        TX.log.append("$ "+sh); TX.log.append("TRANSMITTING (looping with -R). Tune gain/offset until the receiver locks.")
    threading.Thread(target=tx_reader,args=(proc,),daemon=True).start()
    return (True,"started")

def tx_stop():
    with TX.lock: proc=TX.proc; TX.running=False
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            for _ in range(20):
                if proc.poll() is not None: break
                time.sleep(0.1)
            if proc.poll() is None: proc.kill()
        except Exception: pass
    with TX.lock: TX.log.append("-- transmission stopped --")
    return True

def run_hackrf_info():
    hi=find_hackrf_info()
    if not hi: return {"ok":False,"out":"hackrf_info not found. Install the HackRF tools."}
    try:
        r=subprocess.run([hi],capture_output=True,text=True,timeout=15)
        out=(r.stdout or "")+(r.stderr or "")
        return {"ok":("Found HackRF" in out or "Serial number" in out or "Board ID" in out),"out":out.strip() or "(no output)"}
    except Exception as e:
        return {"ok":False,"out":"error: %s"%e}

# ================================================================== GPS MONITOR (receive-only)
def isolation_assess(st, tx_running):
    """Turn the live receiver numbers + TX state into a cage-isolation / spoof verdict.

    Before TX:  sats should fall toward 0 as the sealed cage kills the real sky.
    During TX:  the sim wins when the fix converges on the simulated coordinates.
    """
    if not st.get("connected"):
        return {"state": "connect the receiver to monitor isolation", "level": "idle", "sats": 0}
    used = st.get("sats_used") or 0
    tracked = st.get("sats_tracked") or 0
    n = max(used, tracked)                       # any tracked SV means signal is present
    has_fix = (st.get("fix_quality", 0) >= 1) and bool(st.get("pos_valid"))
    conv = bool(st.get("converged"))
    if tx_running:
        if conv:
            return {"state": "SPOOF CONFIRMED - receiver locked onto the SIMULATED position",
                    "level": "spoof", "sats": n}
        if has_fix:
            return {"state": "Transmitting - receiver has a fix, converging on sim...",
                    "level": "info", "sats": n}
        if n > 0:
            return {"state": "Transmitting - receiver is acquiring the sim...",
                    "level": "info", "sats": n}
        return {"state": "Transmitting - no lock yet (raise gain / nudge freq-offset)",
                "level": "info", "sats": n}
    # --- TX off: this is the isolation gate you check BEFORE transmitting ---
    if n == 0 and not has_fix:
        return {"state": "ISOLATED - real sky is gone (0 sats). Cage sealed. Safe to transmit.",
                "level": "ok", "sats": n}
    if n <= 3 and not has_fix:
        return {"state": "Nearly isolated - %d SV(s) left. Wait for 0 before you transmit." % n,
                "level": "warn", "sats": n}
    return {"state": "NOT ISOLATED - real GNSS still present (%d SV%s). Leaky cage or antenna "
                     "outside the shield - the real fix will beat the sim." % (n, "" if n == 1 else "s"),
            "level": "bad", "sats": n}

def gps_status_payload():
    if GPS is None:
        return {"available": False, "connected": False,
                "error": "pyserial not installed (pip install pyserial)",
                "isolation": {"state": "monitor unavailable", "level": "idle", "sats": 0}}
    st = GPS.status()
    tx_running = TX.snap()["running"]
    st["tx_running"] = tx_running
    st["isolation"] = isolation_assess(st, tx_running)
    return st

# ================================================================== HTTP
class H(BaseHTTPRequestHandler):
    def log_message(s,*a): pass
    def _s(s,code,body,ct="application/json"):
        if isinstance(body,(dict,list)): body=json.dumps(body).encode()
        elif isinstance(body,str): body=body.encode()
        s.send_response(code); s.send_header("Content-Type",ct)
        s.send_header("Content-Length",str(len(body))); s.end_headers(); s.wfile.write(body)
    def _body(s):
        n=int(s.headers.get("Content-Length",0));
        try: return json.loads(s.rfile.read(n).decode() or "{}") if n else {}
        except Exception: return {}
    def do_GET(s):
        u=urlparse(s.path).path
        if u in ("/","/index.html"): return s._s(200,HTML,"text/html; charset=utf-8")
        if u=="/api/navfiles": return s._s(200,{"files":list_nav_files(),"engine":find_engine(),"max":MAX_DUR})
        if u=="/api/tools":
            return s._s(200,{"engine":find_engine(),"hackrf_transfer":find_hackrf(),
                             "hackrf_info":find_hackrf_info(),"custom":_custom_dir(),"win":IS_WIN})
        if u=="/api/status":       return s._s(200,GEN.snap())
        if u=="/api/verify/status":return s._s(200,VER.snap())
        if u=="/api/hackrf_info":  return s._s(200,run_hackrf_info())
        if u=="/api/tx/status":    return s._s(200,TX.snap())
        if u=="/api/gps/ports":
            return s._s(200, gps_monitor.list_ports() if gps_monitor else
                             {"available":False,"error":"pyserial not installed (pip install pyserial)",
                              "ports":[],"baud_options":[9600,38400,115200,230400]})
        if u=="/api/gps/status":   return s._s(200, gps_status_payload())
        return s._s(404,{"error":"not found"})
    def do_POST(s):
        u=urlparse(s.path).path; p=s._body()
        if u=="/api/generate":
            if GEN.snap()["running"]: return s._s(409,{"error":"already running"})
            try:
                float(p["lat"]); float(p["lon"]); float(p["alt"]); d=int(p["duration"])
                if not (1<=d<=MAX_DUR): return s._s(400,{"error":"duration 1..%d s"%MAX_DUR})
                if not p.get("navpath") or not os.path.isfile(p["navpath"]): return s._s(400,{"error":"pick a valid constellation file"})
            except Exception as e: return s._s(400,{"error":"invalid parameters: %s"%e})
            if p.get("usenow"):          # one-click "regenerate @ now" -> server UTC clock, force -T
                now=datetime.now(timezone.utc).strftime("%Y/%m/%d,%H:%M:%S")
                p["datetime"]=now; p["forceT"]=True
            threading.Thread(target=run_generation,args=(p,),daemon=True).start(); return s._s(200,{"ok":True})
        if u=="/api/verify":
            b=p.get("binfile","")
            if not b or not os.path.isfile(b): return s._s(400,{"error":"no bin"})
            threading.Thread(target=run_verify,args=(b,str(p.get("samp","2600000"))),daemon=True).start(); return s._s(200,{"ok":True})
        if u=="/api/tools/set":
            d=p.get("dir","").strip()
            try: open(CFG,"w").write(d)
            except Exception as e: return s._s(400,{"error":str(e)})
            return s._s(200,{"ok":True,"hackrf_transfer":find_hackrf()})
        if u=="/api/tx/start":
            ok,msg=tx_start(p); return s._s(200 if ok else 400,{"ok":ok,"msg":msg})
        if u=="/api/tx/stop":
            tx_stop(); return s._s(200,{"ok":True})
        if u=="/api/gps/open":
            if GPS is None: return s._s(400,{"ok":False,"msg":"pyserial not installed. Run:  pip install pyserial"})
            ok,msg=GPS.open(p.get("port",""),p.get("baud",9600))
            # seed the convergence target from the last generated position (if any)
            if ok and LAST_SIM["lat"] is not None:
                GPS.set_target(LAST_SIM["lat"],LAST_SIM["lon"],LAST_SIM["alt"],src="generated")
            return s._s(200 if ok else 400,{"ok":ok,"msg":msg})
        if u=="/api/gps/close":
            if GPS is not None: GPS.close()
            return s._s(200,{"ok":True})
        if u=="/api/gps/target":
            if GPS is None: return s._s(400,{"ok":False,"msg":"monitor unavailable"})
            try:
                GPS.set_target(float(p["lat"]),float(p["lon"]),p.get("alt"),src="manual")
                LAST_SIM.update({"lat":float(p["lat"]),"lon":float(p["lon"]),
                                 "alt":(float(p["alt"]) if p.get("alt") not in (None,"") else None)})
            except Exception as e: return s._s(400,{"ok":False,"msg":"bad target: %s"%e})
            return s._s(200,{"ok":True})
        return s._s(404,{"error":"not found"})

# ================================================================== FRONT-END
HTML = r"""<!DOCTYPE html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>GPS-SDR-SIM . Master of All Master</title>
<style>
:root{--bg:#070b12;--pan:#0e1622;--pan2:#0b1420;--edge:#1c2b3e;--ink:#e8f0fb;--dim:#8199b4;
--acc:#00e0a4;--acc2:#37b7ff;--warn:#ffb020;--bad:#ff5c6c}
*{box-sizing:border-box}html,body{margin:0;background:
radial-gradient(1200px 600px at 80% -10%,#0d2033 0,transparent 60%),
radial-gradient(900px 500px at -10% 110%,#10233a 0,transparent 55%),var(--bg);
color:var(--ink);font:14px/1.5 "Segoe UI",Roboto,Arial,sans-serif}
a{color:var(--acc2)}.wrap{max-width:1240px;margin:0 auto;padding:20px 20px 60px}
header{display:flex;align-items:center;gap:15px;margin:4px 0 16px}
.logo{width:44px;height:44px;border-radius:11px;flex:none;background:conic-gradient(from 210deg,#00e0a4,#37b7ff,#00e0a4);
position:relative;box-shadow:0 0 26px rgba(0,224,164,.35)}.logo::after{content:"";position:absolute;inset:6px;border-radius:8px;background:#08111c}
h1{font-size:19px;margin:0}h1 b{color:var(--acc)}.sub{color:var(--dim);font-size:12px;margin-top:2px}
.grid{display:grid;grid-template-columns:1.1fr .9fr;gap:16px}@media(max-width:940px){.grid{grid-template-columns:1fr}}
.card{background:linear-gradient(180deg,var(--pan),var(--pan2));border:1px solid var(--edge);border-radius:14px;padding:16px 16px 18px;box-shadow:0 10px 30px rgba(0,0,0,.35);margin-bottom:16px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:1.4px;color:var(--dim);margin:0 0 12px;display:flex;align-items:center;gap:8px}
.card h2 .dot{width:7px;height:7px;border-radius:50%;background:var(--acc);box-shadow:0 0 10px var(--acc)}
label{display:block;font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin:0 0 5px}
input,select{width:100%;background:#081120;border:1px solid var(--edge);color:var(--ink);border-radius:9px;padding:9px 10px;font:13px/1.2 Consolas,ui-monospace,monospace;outline:none}
input:focus,select:focus{border-color:var(--acc2);box-shadow:0 0 0 3px rgba(55,183,255,.15)}
.row{display:grid;gap:10px}.c3{grid-template-columns:1fr 1fr 1fr}.c2{grid-template-columns:1fr 1fr}
.field{margin-bottom:11px}.hint{font-size:11px;color:var(--dim);margin-top:5px}
.presets{display:flex;flex-wrap:wrap;gap:6px;margin:2px 0 4px}
.chip{cursor:pointer;font-size:11px;padding:4px 9px;border:1px solid var(--edge);border-radius:20px;background:#0a1524;color:var(--acc2)}
.chip:hover{border-color:var(--acc2);color:#fff}
.slider{display:flex;align-items:center;gap:10px}.slider input[type=range]{accent-color:var(--acc);flex:1}
.est{display:flex;justify-content:space-between;font-size:11px;color:var(--dim);margin-top:6px}.est b{color:var(--warn)}
.toggle{display:flex;align-items:center;gap:8px;margin:8px 0;cursor:pointer;font-size:12px}.toggle input{width:auto;accent-color:var(--acc)}
.btn{width:100%;margin-top:6px;border:0;border-radius:11px;padding:14px;cursor:pointer;font:700 15px "Segoe UI";letter-spacing:.5px;color:#022;background:linear-gradient(180deg,#00f0b0,#00c78d);box-shadow:0 8px 24px rgba(0,224,164,.3)}
.btn:hover{filter:brightness(1.07)}.btn:disabled{filter:grayscale(.7) brightness(.7);cursor:not-allowed}
.btn.tx{background:linear-gradient(180deg,#ff8a3c,#ff5c6c);box-shadow:0 8px 24px rgba(255,92,108,.3);color:#fff}
.btn.stop{background:linear-gradient(180deg,#39506b,#26384d);color:#fff;box-shadow:none}
.btn.sm{width:auto;padding:8px 13px;font-size:12px;background:#12253a;color:var(--acc2);box-shadow:none;border:1px solid var(--edge)}
.progress{height:11px;border-radius:8px;background:#08131f;border:1px solid var(--edge);overflow:hidden}
.bar{height:100%;width:0;background:linear-gradient(90deg,var(--acc2),var(--acc));transition:width .3s}
.pill{font-size:11px;padding:3px 10px;border-radius:20px;border:1px solid var(--edge);color:var(--dim)}
.pill.run{color:var(--warn);border-color:var(--warn)}.pill.ok{color:var(--acc);border-color:var(--acc)}.pill.err{color:var(--bad);border-color:var(--bad)}
pre.con{background:#050b13;border:1px solid var(--edge);border-radius:10px;padding:11px;height:150px;overflow:auto;font:12px/1.5 Consolas,monospace;color:#9fe9cf;white-space:pre-wrap;margin:0}
.tool{display:flex;align-items:center;gap:9px;font-size:12.5px;padding:6px 0;border-bottom:1px dashed #16273b}
.led{width:9px;height:9px;border-radius:50%;flex:none}.led.g{background:var(--acc);box-shadow:0 0 8px var(--acc)}.led.r{background:var(--bad);box-shadow:0 0 8px var(--bad)}
.tool code{color:var(--dim);font-size:11px;margin-left:auto;max-width:52%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.warnbox{border:1px solid var(--warn);background:rgba(255,176,32,.07);border-radius:10px;padding:9px 11px;font-size:12px;color:#ffd98a;margin-top:10px}
.badbox{border:1px solid var(--bad);background:rgba(255,92,108,.08);color:#ffb3ba;border-radius:10px;padding:10px 12px;font-size:12.5px;margin:8px 0}
.okbox{border:1px solid var(--acc);background:rgba(0,224,164,.06);border-radius:11px;padding:12px;margin-top:6px;display:none}.okbox.show{display:block}
.kv{display:flex;justify-content:space-between;font-size:12.5px;padding:3px 0;border-bottom:1px dashed #16273b}.kv b{color:var(--acc)}
.step{display:inline-flex;width:18px;height:18px;border-radius:50%;background:var(--acc);color:#022;font-size:11px;font-weight:700;align-items:center;justify-content:center;margin-right:6px}
details summary{cursor:pointer;color:var(--dim);font-size:12px;letter-spacing:.4px;margin:4px 0}
.foot{color:var(--dim);font-size:11px;text-align:center;margin-top:22px;line-height:1.7}
.big{font-size:34px;font-weight:800;text-align:center;letter-spacing:1px}.big.on{color:var(--warn)}.big.off{color:var(--dim)}
.vv{font-weight:700}.vv.PASS{color:var(--acc)}.vv.FAIL,.vv\?{color:var(--bad)}.vv.WEAK{color:var(--warn)}
.banner{border-radius:12px;padding:12px 14px;font-weight:700;font-size:15px;text-align:center;border:1px solid var(--edge);letter-spacing:.3px;line-height:1.35}
.banner.idle{color:var(--dim);background:#0a1420}
.banner.ok{color:var(--acc);border-color:var(--acc);background:rgba(0,224,164,.08)}
.banner.warn{color:var(--warn);border-color:var(--warn);background:rgba(255,176,32,.08)}
.banner.bad{color:var(--bad);border-color:var(--bad);background:rgba(255,92,108,.09)}
.banner.info{color:var(--acc2);border-color:var(--acc2);background:rgba(55,183,255,.08)}
.banner.spoof{color:#ff7cf0;border-color:#ff7cf0;background:rgba(255,124,240,.11);box-shadow:0 0 22px rgba(255,124,240,.25)}
.mgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}
@media(max-width:620px){.mgrid{grid-template-columns:repeat(2,1fr)}}
.mtile{background:#081120;border:1px solid var(--edge);border-radius:10px;padding:8px 10px}
.mtile .ml{display:block;font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim);margin:0 0 3px}
.mtile b{font-size:15px;color:var(--ink);font-variant-numeric:tabular-nums}
.freshbadge{font-size:10px;padding:2px 7px;border-radius:10px;margin-left:6px;font-weight:700}
.freshbadge.f{color:#022;background:var(--acc)}.freshbadge.s{color:#3a2a00;background:var(--warn)}
.btn.ghost{background:#12253a;color:var(--acc2);box-shadow:none;border:1px solid var(--edge)}
</style></head><body><div class=wrap>

<header><div class=logo></div><div>
<h1>GPS-SDR-SIM . <b>Master of All Master</b></h1>
<div class=sub>GPS L1 C/A . HackRF One . generate &rarr; self-test &rarr; transmit . shielded-chamber console</div>
</div><div style="margin-left:auto;text-align:right"><span class="pill ok">OFFLINE-SAFE</span></div></header>

<!-- TOOLS / DEVICE -->
<div class=card>
  <h2><span class=dot></span> System &amp; hardware status</h2>
  <div class=tool><span class="led r" id=led_eng></span> gps-sdr-sim engine <code id=p_eng>checking...</code></div>
  <div class=tool><span class="led r" id=led_tx></span> hackrf_transfer (transmit) <code id=p_tx>checking...</code></div>
  <div class=tool><span class="led r" id=led_info></span> hackrf_info (device tool) <code id=p_info>checking...</code></div>
  <div id=hkmiss class=badbox style=display:none>
    <b>HackRF tools not found.</b> Nothing can transmit until they're installed.
    <div style="margin-top:6px">Quickest fix (Windows): install <a href="https://downloads.myriadrf.org/builds/PothosSDR/" target=_blank>PothosSDR</a>
    (keep &ldquo;Add to PATH&rdquo; ticked) or <a href="https://github.com/radioconda/radioconda-installer/releases" target=_blank>radioconda</a>,
    then run <a href="https://zadig.akeo.ie/" target=_blank>Zadig</a> once (HackRF One &rarr; WinUSB). Re-open this app.</div>
    <div class=row style="grid-template-columns:1fr auto;margin-top:8px">
      <input id=hkdir placeholder="...or paste the folder that contains hackrf_transfer.exe">
      <button class="btn sm" id=hkset>Use folder</button></div>
  </div>
  <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
    <button class="btn sm" id=btninfo>&#128268; Check HackRF device (hackrf_info)</button>
    <span class=pill id=devpill>device: unknown</span>
  </div>
  <pre class=con id=infocon style="height:96px;margin-top:8px;display:none"></pre>
</div>

<div class=grid>
  <!-- LEFT: generate -->
  <div>
  <div class=card>
    <h2><span class=dot></span> 1 &middot; Generate signal</h2>
    <div class=field><label><span class=step>A</span>Constellation file (NASA RINEX brdc)</label>
      <select id=nav></select>
      <div class=hint>Drop today's <code>brdc</code> in <b>nav/</b> then <a href=# id=refresh>&#8635; refresh</a>.
        Detected epoch: <b id=epoch>&mdash;</b><span id=navfresh></span>. Old file? keep &ldquo;force time&rdquo; on.</div></div>
    <div class=field><label><span class=step>B</span>Simulated position (lat, lon, alt)</label>
      <div class=presets>
        <span class=chip data-lat=27.700769 data-lon=85.300140 data-alt=1337>Kathmandu</span>
        <span class=chip data-lat=28.394857 data-lon=84.124008 data-alt=1400>Nepal centre</span>
        <span class=chip data-lat=28.6139 data-lon=77.2090 data-alt=216>New Delhi</span>
        <span class=chip data-lat=27.988056 data-lon=86.925278 data-alt=8849>Everest</span></div>
      <div class="row c3"><div><label>Lat &deg;</label><input id=lat value=27.700769></div>
        <div><label>Lon &deg;</label><input id=lon value=85.300140></div>
        <div><label>Alt m</label><input id=alt value=1337></div></div></div>
    <div class=field><label><span class=step>C</span>Duration &mdash; seconds (max 600)</label>
      <div class=slider><input type=range id=durR min=30 max=600 step=10 value=300><input id=dur value=300 style="width:84px;text-align:center"></div>
      <div class=est><span>file size: <b id=estsize>&mdash;</b></span><span>generate time: <b id=esttime>&mdash;</b></span></div>
      <div class=hint>It loops forever on transmit (<code>-R</code>), so you don't need a long file. ~300 s is a good balance.</div></div>
    <div class=field><label><span class=step>D</span>Scenario date &amp; time (UTC)</label>
      <div class="row c2"><input id=date placeholder=YYYY/MM/DD><input id=time placeholder=hh:mm:ss></div>
      <div class=hint><a href=# id=setnow>&#8635; set to now (UTC)</a> &middot; today <b id=today></b></div>
      <div class=toggle><input type=checkbox id=forceT checked><span>Force ephemeris onto this time (<code>-T</code>) &mdash; makes an old file lock today</span></div></div>
    <details><summary>ADVANCED &middot; signal options</summary>
      <div style="padding:10px 2px 2px">
        <div class="row c2"><div><label>I/Q format</label><select id=bits>
          <option value=8 selected>8-bit &mdash; HackRF One</option><option value=16>16-bit &mdash; bladeRF/Pluto</option><option value=1>1-bit &mdash; Lime</option></select></div>
          <div><label>Sample rate Hz</label><input id=samp value=2600000></div></div>
        <div class=toggle><input type=checkbox id=fixedgain><span>Constant power <code>-p 80</code> (off = realistic path-loss, 0% clipping &mdash; recommended off)</span></div>
        <div class=toggle><input type=checkbox id=noiono><span>Disable ionosphere <code>-i</code> (leave off for a normal receiver)</span></div>
        <div class=field style=margin-top:8px><label>Output file name</label><input id=outfile value=gpssim.bin></div></div></details>
    <button class=btn id=go>&#9889;&nbsp; GENERATE BIN FILE</button>
    <button class="btn ghost" id=regennow style="margin-top:8px">&#8635;&nbsp; Regenerate @ now (UTC) &mdash; freshest brdc, time-locked</button>
    <div class=hint>One click: server picks the newest ephemeris, stamps the current UTC time and forces <code>-T</code> so the receiver locks to <b>now</b>. Keeps the path-loss default and <code>-R</code> loop.</div>
  </div>

  <!-- VERIFY -->
  <div class=card>
    <h2><span class=dot></span> 2 &middot; Self-test (is it real GPS?)</h2>
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
      <button class="btn sm" id=btnverify disabled>&#129517; Verify the .bin</button>
      <span class=pill id=verpill>no file yet</span>
      <span id=ververdict class=vv style=margin-left:auto></span></div>
    <pre class=con id=vercon style=height:120px>Generate a file, then verify it locks the right satellites before you transmit.
(needs numpy: pip install numpy)</pre>
  </div>
  </div>

  <!-- RIGHT: transmit -->
  <div>
  <div class=card>
    <h2><span class=dot></span> 3 &middot; Transmit to HackRF</h2>
    <div class=field><label>File to transmit</label><input id=binfile placeholder="generate a file first" readonly></div>
    <div class="row c2">
      <div class=field><label>Frequency offset (Hz) &mdash; sweep to lock</label>
        <div class=slider><input type=range id=offR min=-25000 max=25000 step=500 value=0><input id=off value=0 style="width:90px;text-align:center"></div>
        <div class=hint>L1 = 1575.42 MHz <b id=txfreq></b>. If no lock, nudge &plusmn; a few kHz.</div></div>
      <div class=field><label>TX gain <code>-x</code> (0&ndash;47 dB)</label>
        <div class=slider><input type=range id=gainR min=0 max=47 step=1 value=20><input id=gain value=20 style="width:70px;text-align:center"></div>
        <div class=hint>Cabled + attenuator: start ~20. Drops after lock = too hot &rarr; lower.</div></div></div>
    <div class=toggle><input type=checkbox id=amp><span>RF amp <code>-a 1</code> (leave OFF for a cabled/attenuated chamber)</span></div>
    <div class=warnbox><b>Shielded-chamber only.</b> Transmit through a cable + attenuator inside the Faraday cage. Never radiate GPS over the air.</div>
    <div class=toggle style=margin-top:8px><input type=checkbox id=chamber><span><b>I confirm I am transmitting inside a shielded chamber / Faraday cage.</b></span></div>
    <div style="display:flex;gap:10px;margin-top:8px">
      <button class="btn tx" id=txgo disabled>&#128225;&nbsp; START TRANSMIT (loops)</button>
      <button class="btn stop" id=txstop style=display:none>&#9632; STOP</button></div>
    <div class=big off id=txbig style=margin-top:12px>IDLE</div>
    <pre class=con id=txcon style=height:120px>hackrf_transfer output will show here.</pre>
  </div>

  <div class=card>
    <h2><span class=dot></span> Generation log</h2>
    <div style="display:flex;justify-content:space-between;margin-bottom:6px"><span style=font-size:12px>progress</span><span class=pill id=pill>idle</span></div>
    <div class=progress><div class=bar id=bar></div></div>
    <div style="text-align:right;font-size:11px;color:var(--dim);margin:5px 0 8px"><span id=pct>0</span>%</div>
    <pre class=con id=gencon>Waiting for a job...</pre>
    <div class=okbox id=okbox>
      <div class=kv><span>File</span><b id=r_name>&mdash;</b></div>
      <div class=kv><span>Size</span><b id=r_size>&mdash;</b></div>
      <div class=kv><span>Length</span><b id=r_secs>&mdash;</b></div></div>
  </div>
  </div>
</div>

<!-- SPOOF-CONFIRM MONITOR (receive-only) -->
<div class=card id=moncard>
  <h2><span class=dot></span> 4 &middot; Spoof-confirm monitor (receive-only)</h2>
  <div class=hint style="margin:-4px 0 10px">Open the receiver's COM port <b>read-only</b> and watch what it actually sees &mdash; live fix, sats, C/N0 and position. Confirms the sim has won when the fix snaps to your simulated coordinates. Needs <code>pip install pyserial</code>. This panel never transmits or reconfigures the receiver.</div>
  <div class="row" style="grid-template-columns:1.5fr .9fr auto auto;gap:10px;align-items:end">
    <div><label>COM port</label><select id=comport></select></div>
    <div><label>Baud</label><select id=combaud></select></div>
    <button class="btn sm" id=comrefresh style="height:38px">&#8635; ports</button>
    <button class="btn sm" id=comconn style="height:38px">Connect</button>
  </div>
  <div id=comerr class=badbox style="display:none"></div>

  <div id=isobanner class="banner idle" style="margin-top:12px">Connect the receiver to begin.</div>

  <div class=mgrid>
    <div class=mtile><span class=ml>Fix</span><b id=m_fix>&mdash;</b></div>
    <div class=mtile><span class=ml>Sats used</span><b id=m_used>&mdash;</b></div>
    <div class=mtile><span class=ml>Sats tracked</span><b id=m_track>&mdash;</b></div>
    <div class=mtile><span class=ml>C/N0 max</span><b id=m_cn0>&mdash;</b></div>
    <div class=mtile><span class=ml>HDOP</span><b id=m_hdop>&mdash;</b></div>
    <div class=mtile><span class=ml>Speed</span><b id=m_spd>&mdash;</b></div>
    <div class=mtile><span class=ml>Latitude</span><b id=m_lat>&mdash;</b></div>
    <div class=mtile><span class=ml>Longitude</span><b id=m_lon>&mdash;</b></div>
    <div class=mtile><span class=ml>Altitude</span><b id=m_alt>&mdash;</b></div>
  </div>

  <div class=kv style="margin-top:10px"><span>Distance to simulated position</span><b id=m_dist>&mdash;</b></div>
  <div class=kv><span>Convergence target</span><b id=m_targ>&mdash;</b></div>
  <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
    <button class="btn sm" id=targform>Use lat/lon from Generate form as target</button></div>

  <div class=warnbox style="margin-top:12px"><b>Cage-isolation gate.</b> BEFORE transmitting, seal the cage and watch sats fall toward <b>0</b>. If sats stay high the real sky is leaking in (loose door / antenna outside the shield) and the stronger real fix will beat the sim. Only transmit once this reads <b>ISOLATED</b>; then start TX and watch the fix converge on your coordinates.</div>

  <details id=detbox style="margin-top:10px"><summary>DEFENSIVE &middot; spoof / jamming detection (u-blox UBX telemetry)</summary>
    <div style="padding:8px 2px 2px">
      <div id=detflag class="banner idle" style="font-size:14px;padding:9px">UBX telemetry: waiting for the receiver&hellip;</div>
      <div class=mgrid style="margin-top:8px">
        <div class=mtile><span class=ml>spoofDetState</span><b id=d_spoof>&mdash;</b></div>
        <div class=mtile><span class=ml>Jamming</span><b id=d_jam>&mdash;</b></div>
        <div class=mtile><span class=ml>jamInd</span><b id=d_jind>&mdash;</b></div>
        <div class=mtile><span class=ml>AGC</span><b id=d_agc>&mdash;</b></div>
        <div class=mtile><span class=ml>Noise/ms</span><b id=d_noise>&mdash;</b></div>
        <div class=mtile><span class=ml>C/N0 jump</span><b id=d_jump>&mdash;</b></div>
      </div>
      <div class=hint id=detreasons style="margin-top:6px"></div>
      <div class=hint>spoofDetState: 0 unknown &middot; 1 none &middot; 2 indicated &middot; 3 multiple. Jamming: 0 unknown &middot; 1 ok &middot; 2 warning &middot; 3 critical. These are receiver <b>telemetry flags</b> &mdash; a u-blox still outputs a fix even when they trip. The monitor polls UBX-NAV-STATUS / MON-RF / MON-HW read-only; NMEA-only receivers leave this blank.</div>
    </div></details>

  <pre class=con id=moncon style="height:70px;margin-top:10px">Receiver serial output appears here once connected.</pre>
</div>

<div class=card>
  <h2><span class=dot></span> Receiver setup (u-blox M10) &amp; cage checklist</h2>
  <div style="font-size:12.5px;color:#c8d7ea;line-height:1.7">
  <b style=color:var(--acc)>M10 in u-center:</b> set <b>GPS-only</b> (CFG-VALSET &rarr; CFG-SIGNAL: GPS+L1CA on, Galileo/BeiDou/GLONASS/QZSS/SBAS off, save to Flash),
  then <b>cold start</b> (UBX-CFG-RST &rarr; navBbrMask 0xFFFF, controlled reset). Watch UBX-NAV-SAT for C/N0 bars; expect a fix in ~30 s once 4+ GPS sats sit at 35&ndash;45 dB-Hz.
  Spoof-detect is a flag only &mdash; it will still fix. &nbsp;
  <b style=color:var(--acc)>If it won't lock:</b> (1) is it transmitting? &mdash; TX shows TRANSMITTING; (2) nudge the frequency-offset slider &plusmn;5 kHz (HackRF crystal drift); (3) lower TX gain if it locks then drops (overpower); (4) let the HackRF warm up a few minutes; (5) use today's brdc or keep <code>-T</code> on.
  </div>
</div>

<div class=foot>GPS-SDR-SIM &ldquo;Master of All Master&rdquo; v2 &middot; authorised RF-shielded testing only &middot; never radiate GPS over the air</div>
</div>

<script>
var $=function(i){return document.getElementById(i)},navf=[],MAX=600,curbin="";
function human(n){var u=["B","KB","MB","GB","TB"],i=0;while(n>=1024&&i<4){n/=1024;i++}return(i?n.toFixed(2):n.toFixed(0))+" "+u[i]}
function pad(n){return(n<10?"0":"")+n}
function est(){var d=+$("dur").value||0,s=+$("samp").value||2600000,b=$("bits").value,bp=b=="16"?4:b=="1"?.25:2;
  $("estsize").textContent=human(d*s*bp);$("esttime").textContent="~"+Math.max(1,Math.round(d/9))+" s"}
function setnow(){var t=new Date();$("date").value=t.getUTCFullYear()+"/"+pad(t.getUTCMonth()+1)+"/"+pad(t.getUTCDate());
  $("time").value=pad(t.getUTCHours())+":"+pad(t.getUTCMinutes())+":"+pad(t.getUTCSeconds())}
function txfreq(){var f=1575420000+(+$("off").value||0);$("txfreq").textContent="&rarr; "+(f/1e6).toFixed(4)+" MHz";$("txfreq").innerHTML="&rarr; "+(f/1e6).toFixed(4)+" MHz"}
function tools(){fetch("/api/tools").then(r=>r.json()).then(t=>{
  function set(led,p,v){$(led).className="led "+(v?"g":"r");$(p).textContent=v||"not found"}
  set("led_eng","p_eng",t.engine);set("led_tx","p_tx",t.hackrf_transfer);set("led_info","p_info",t.hackrf_info);
  $("hkmiss").style.display=t.hackrf_transfer?"none":"block";
  txReady();});}
function loadnav(){fetch("/api/navfiles").then(r=>r.json()).then(j=>{navf=j.files;MAX=j.max||600;
  var s=$("nav");s.innerHTML="";if(!j.files.length){var o=document.createElement("option");o.textContent="(no RINEX in nav/ - add one)";o.value="";s.appendChild(o)}
  j.files.forEach(f=>{var o=document.createElement("option");o.value=f.path;o.textContent=f.rel+"  ("+human(f.size)+(f.epoch?", "+f.epoch:"")+(f.fresh?" (today)":"")+")";o.dataset.epoch=f.epoch||"";o.dataset.fresh=f.fresh?"1":"";s.appendChild(o)});epoch()})}
function epoch(){var o=$("nav").selectedOptions[0];$("epoch").textContent=(o&&o.dataset.epoch)||"—";
  var b=$("navfresh");if(!b)return;
  if(o&&o.dataset.fresh){b.textContent=" TODAY";b.className="freshbadge f"}
  else if(o&&o.dataset.epoch){b.textContent=" STALE - keep -T on";b.className="freshbadge s"}
  else{b.textContent="";b.className=""}}
function txReady(){var ok=curbin&&$("chamber").checked;fetch("/api/tools").then(r=>r.json()).then(t=>{$("txgo").disabled=!(ok&&t.hackrf_transfer)})}

$("durR").oninput=function(){$("dur").value=this.value;est()};
$("dur").oninput=function(){var v=Math.max(1,Math.min(MAX,+this.value||1));$("durR").value=v;est()};
$("samp").oninput=est;$("bits").onchange=est;$("nav").onchange=epoch;
$("offR").oninput=function(){$("off").value=this.value;txfreq()};$("off").oninput=function(){$("offR").value=this.value;txfreq()};
$("gainR").oninput=function(){$("gain").value=this.value};$("gain").oninput=function(){$("gainR").value=this.value};
$("refresh").onclick=e=>{e.preventDefault();loadnav()};$("setnow").onclick=e=>{e.preventDefault();setnow()};
$("chamber").onchange=txReady;
document.querySelectorAll(".chip").forEach(c=>c.onclick=()=>{$("lat").value=c.dataset.lat;$("lon").value=c.dataset.lon;$("alt").value=c.dataset.alt});
$("hkset").onclick=()=>fetch("/api/tools/set",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({dir:$("hkdir").value})}).then(r=>r.json()).then(()=>tools());
$("btninfo").onclick=function(){$("infocon").style.display="block";$("infocon").textContent="running hackrf_info...";
  fetch("/api/hackrf_info").then(r=>r.json()).then(d=>{$("infocon").textContent=d.out;
    $("devpill").textContent=d.ok?"device: FOUND":"device: not found";$("devpill").className="pill "+(d.ok?"ok":"err")})};

$("go").onclick=function(){
  var p={navpath:$("nav").value,lat:$("lat").value,lon:$("lon").value,alt:$("alt").value,duration:$("dur").value,
    bits:$("bits").value,samp:$("samp").value,outfile:$("outfile").value,
    datetime:($("date").value.trim()&&$("time").value.trim())?$("date").value.trim()+","+$("time").value.trim():"",
    forceT:$("forceT").checked,fixedgain:$("fixedgain").checked,noiono:$("noiono").checked};
  if(!p.navpath){alert("Pick a constellation file (put one in nav/).");return}
  $("okbox").classList.remove("show");
  fetch("/api/generate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(p)}).then(r=>r.json()).then(j=>{if(j.error){alert(j.error);return}genpoll()})};
function genpoll(){fetch("/api/status").then(r=>r.json()).then(s=>{
  $("bar").style.width=s.progress+"%";$("pct").textContent=s.progress.toFixed(0);if(s.log)$("gencon").textContent=s.log;$("gencon").scrollTop=9e9;
  var pl=$("pill");pl.className="pill "+(s.status=="running"?"run":s.status=="done"?"ok":s.status=="error"?"err":"");pl.textContent=s.status;$("go").disabled=s.running;
  if(s.status=="done"&&s.result){var r=s.result;curbin=r.outfile;$("binfile").value=r.outfile;$("r_name").textContent=r.outname;$("r_size").textContent=r.size_h;$("r_secs").textContent=r.seconds+" s";$("okbox").classList.add("show");$("btnverify").disabled=false;$("verpill").textContent="ready to verify";txReady();}
  if(s.running)setTimeout(genpoll,500)})}

$("btnverify").onclick=function(){$("vercon").textContent="running acquisition self-test... (a few seconds)";$("verpill").textContent="testing";$("verpill").className="pill run";$("ververdict").textContent="";
  fetch("/api/verify",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({binfile:curbin,samp:$("samp").value})}).then(r=>r.json()).then(j=>{if(j.error){alert(j.error);return}verpoll()})};
function verpoll(){fetch("/api/verify/status").then(r=>r.json()).then(s=>{if(s.log)$("vercon").textContent=s.log;$("vercon").scrollTop=9e9;
  if(s.status=="running"){setTimeout(verpoll,600);return}
  $("verpill").textContent=s.status;$("verpill").className="pill "+(s.verdict=="PASS"?"ok":s.verdict=="WEAK"?"run":"err");
  $("ververdict").textContent=s.verdict?("verdict: "+s.verdict):"";$("ververdict").className="vv "+s.verdict})}

$("txgo").onclick=function(){
  var p={binfile:curbin,offset:+$("off").value,gain:+$("gain").value,amp:$("amp").checked,samp:$("samp").value,chamber:$("chamber").checked};
  fetch("/api/tx/start",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(p)}).then(r=>r.json()).then(j=>{if(!j.ok){alert(j.msg);return}txpoll()})};
$("txstop").onclick=()=>fetch("/api/tx/stop",{method:"POST"}).then(()=>setTimeout(txpoll,300));
function txpoll(){fetch("/api/tx/status").then(r=>r.json()).then(s=>{if(s.log)$("txcon").textContent=s.log;$("txcon").scrollTop=9e9;
  $("txbig").textContent=s.running?"TRANSMITTING":"IDLE";$("txbig").className="big "+(s.running?"on":"off");
  $("txgo").style.display=s.running?"none":"block";$("txstop").style.display=s.running?"block":"none";
  if(s.running)setTimeout(txpoll,700)})}

// ---- regenerate @ now (UTC): server clock + freshest brdc + force -T ----
$("regennow").onclick=function(){
  var nav=$("nav").value; if(!nav && navf.length) nav=navf[0].path;
  if(!nav){alert("Put a RINEX brdc file in nav/ first.");return}
  var p={navpath:nav,lat:$("lat").value,lon:$("lon").value,alt:$("alt").value,duration:$("dur").value,
    bits:$("bits").value,samp:$("samp").value,outfile:$("outfile").value,
    usenow:true,forceT:true,fixedgain:$("fixedgain").checked,noiono:$("noiono").checked};
  if(!p.lat||!p.lon||!p.alt){alert("Set lat / lon / alt first.");return}
  $("okbox").classList.remove("show");setnow();
  fetch("/api/generate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(p)})
    .then(r=>r.json()).then(j=>{if(j.error){alert(j.error);return}genpoll()})};

// ---- spoof-confirm monitor (receive-only) ----
var gpsOn=false,baudInit=false;
function bnr(el,level,text){el.className="banner "+(level||"idle");el.textContent=text}
function fmtCoord(v,pos,neg){return (v==null)?"—":(Math.abs(v).toFixed(6)+"° "+(v>=0?pos:neg))}
function gpsPorts(){fetch("/api/gps/ports").then(r=>r.json()).then(j=>{
  var sp=$("comport");sp.innerHTML="";
  if(!j.available){var o=document.createElement("option");o.textContent="pyserial not installed - pip install pyserial";o.value="";sp.appendChild(o);$("comconn").disabled=true;return}
  $("comconn").disabled=false;
  if(!j.ports.length){var o=document.createElement("option");o.textContent="(no COM ports found - plug in the receiver)";o.value="";sp.appendChild(o)}
  j.ports.forEach(p=>{var o=document.createElement("option");o.value=p.device;o.textContent=p.device+(p.description?" - "+p.description:"");sp.appendChild(o)});
  var sb=$("combaud");if(!baudInit){(j.baud_options||[9600,38400,115200,230400]).forEach(b=>{var o=document.createElement("option");o.value=b;o.textContent=b;sb.appendChild(o)});sb.value=9600;baudInit=true}})}
function gpsConnect(){
  if(gpsOn){fetch("/api/gps/close",{method:"POST"}).then(()=>{gpsOn=false;$("comconn").textContent="Connect";$("comerr").style.display="none";bnr($("isobanner"),"idle","Disconnected.")});return}
  var port=$("comport").value,baud=$("combaud").value;
  if(!port){$("comerr").style.display="block";$("comerr").textContent="Pick a COM port first.";return}
  fetch("/api/gps/open",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({port:port,baud:+baud})}).then(r=>r.json()).then(j=>{
    if(!j.ok){$("comerr").style.display="block";$("comerr").textContent=j.msg||"could not open port";return}
    $("comerr").style.display="none";gpsOn=true;$("comconn").textContent="Disconnect";gpsPoll()})}
function targForm(){fetch("/api/gps/target",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({lat:$("lat").value,lon:$("lon").value,alt:$("alt").value})}).then(r=>r.json()).then(()=>{if(!gpsOn)alert("Target set. Connect the receiver to watch convergence.")})}
function gpsPoll(){fetch("/api/gps/status").then(r=>r.json()).then(s=>{
  var conn=s.connected,iso=s.isolation||{};
  bnr($("isobanner"),iso.level,iso.state||"—");
  $("m_fix").textContent=conn?((s.fix_type_text||"?")+" · "+(s.fix_quality_text||"?")):"—";
  $("m_used").textContent=conn&&s.sats_used!=null?s.sats_used:"—";
  $("m_track").textContent=conn&&s.sats_tracked!=null?s.sats_tracked:"—";
  $("m_cn0").textContent=(s.cn0_max!=null)?(s.cn0_max+" dB-Hz"):"—";
  $("m_hdop").textContent=(s.hdop!=null)?s.hdop:"—";
  $("m_spd").textContent=(s.speed_kn!=null)?(s.speed_kn+" kn"):"—";
  $("m_lat").textContent=fmtCoord(s.lat,"N","S");
  $("m_lon").textContent=fmtCoord(s.lon,"E","W");
  $("m_alt").textContent=(s.alt!=null)?(s.alt.toFixed(1)+" m"):"—";
  $("m_dist").textContent=(s.distance_m!=null)?(s.distance_m>=1000?(s.distance_m/1000).toFixed(2)+" km":s.distance_m+" m"):"—";
  var t=s.target||{};$("m_targ").textContent=(t.set&&t.lat!=null)?(t.lat.toFixed(6)+", "+t.lon.toFixed(6)+(t.src?" ("+t.src+")":"")):"— generate a file or set a target";
  if(conn&&s.baud_hint){$("comerr").style.display="block";$("comerr").textContent="Bytes are arriving but nothing parses - wrong baud? Try 38400 or 115200."}
  else if(conn&&$("comerr").textContent.indexOf("baud")>=0){$("comerr").style.display="none"}
  if(s.last_raw)$("moncon").textContent=s.last_raw;
  var d=s.detection||{};
  if(!conn||!d.ubx){bnr($("detflag"),"idle",conn?"No UBX telemetry (NMEA-only receiver or not u-blox).":"UBX telemetry: waiting for the receiver…")}
  else{var lvl=d.flag=="alert"?"bad":d.flag=="caution"?"warn":"ok";
    bnr($("detflag"),lvl,d.flag=="alert"?"POSSIBLE SPOOF / DENIAL":d.flag=="caution"?"CAUTION - anomaly detected":"clear - no spoof / jamming flag")}
  $("d_spoof").textContent=(d.spoofState!=null)?(d.spoofState+" - "+(d.spoofText||"")):"—";
  $("d_jam").textContent=(d.jamState!=null)?(d.jamState+" - "+(d.jamText||"")):"—";
  $("d_jind").textContent=(d.jamInd!=null)?(d.jamInd+"/255"):"—";
  $("d_agc").textContent=(d.agc!=null)?d.agc:"—";
  $("d_noise").textContent=(d.noise!=null)?d.noise:"—";
  $("d_jump").textContent=(d.cn0_jump!=null)?("+"+d.cn0_jump+" dB"):"—";
  $("detreasons").textContent=(d.reasons&&d.reasons.length)?("Flags: "+d.reasons.join(" · ")):"";
  if(gpsOn)setTimeout(gpsPoll,1000)})}
$("comrefresh").onclick=gpsPorts;$("comconn").onclick=gpsConnect;$("targform").onclick=targForm;

$("today").textContent=new Date().toDateString();setnow();est();txfreq();loadnav();tools();txpoll();gpsPorts();
setInterval(tools,4000);
</script></body></html>"""

def main():
    eng=find_engine(); hk=find_hackrf()
    print("="*66)
    print("  GPS-SDR-SIM . Master of All Master . Mission Control v2")
    print("="*66)
    print("  engine        :", eng or "NOT FOUND (put gps-sdr-sim.exe here / run build)")
    print("  hackrf_transfer:", hk or "NOT FOUND (install HackRF tools to transmit)")
    print("  nav / output   :", NAV_DIR, "|", OUT_DIR)
    print("  open           :  http://127.0.0.1:%d"%PORT)
    print("="*66)
    print("  (leave this window open; close it to stop)")
    try: threading.Timer(1.0,lambda:webbrowser.open("http://127.0.0.1:%d"%PORT)).start()
    except Exception: pass
    try: ThreadingHTTPServer(("127.0.0.1",PORT),H).serve_forever()
    except KeyboardInterrupt: print("\nStopped.")

if __name__=="__main__": main()
