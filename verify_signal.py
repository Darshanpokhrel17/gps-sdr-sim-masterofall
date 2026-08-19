#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_signal.py  -  Independent proof that a generated .bin is real, lockable GPS.

It runs a standards-based GPS L1 C/A acquisition (the same first step any real GPS
receiver performs) over the start of the file and reports which satellites (PRNs) it
locks onto, with a signal metric and Doppler for each. This is the objective
"does it pretend like actual GPS" test - run it before you go into the chamber.

Usage:
    python verify_signal.py output/delhi.bin
    python verify_signal.py output/delhi.bin --fs 2600000 --ms 10

Requires: numpy   (pip install numpy)
Assumes 8-bit signed I/Q interleaved (gps-sdr-sim -b 8), 2.6 Msps by default.
"""
import sys, argparse
try:
    import numpy as np
except ImportError:
    print("This tool needs numpy:  pip install numpy"); sys.exit(1)

FCHIP = 1.023e6; NCHIP = 1023
G2 = {1:(2,6),2:(3,7),3:(4,8),4:(5,9),5:(1,9),6:(2,10),7:(1,8),8:(2,9),9:(3,10),
     10:(2,3),11:(3,4),12:(5,6),13:(6,7),14:(7,8),15:(8,9),16:(9,10),17:(1,4),
     18:(2,5),19:(3,6),20:(4,7),21:(5,8),22:(6,9),23:(1,3),24:(4,6),25:(5,7),
     26:(6,8),27:(7,9),28:(8,10),29:(1,6),30:(2,7),31:(3,8),32:(4,9)}

def ca_code(prn):
    g1=[1]*10; g2=[1]*10; s1,s2=G2[prn]; out=np.empty(NCHIP,dtype=np.int8)
    for i in range(NCHIP):
        out[i]=g1[9]^g2[s1-1]^g2[s2-1]
        f1=g1[2]^g1[9]; f2=g2[1]^g2[2]^g2[5]^g2[7]^g2[8]^g2[9]
        g1=[f1]+g1[:9]; g2=[f2]+g2[:9]
    return 1-2*out.astype(np.float64)

def acquire(x, prn, spms, fs, nms, dopp):
    idx=(np.arange(spms)*FCHIP/fs).astype(int)%NCHIP
    c=np.conj(np.fft.fft(ca_code(prn)[idx])); n=np.arange(spms)
    best=(0.0,0,0)
    for f in dopp:
        acc=np.zeros(spms)
        for m in range(nms):
            blk=x[m*spms:(m+1)*spms]
            if len(blk)<spms: break
            acc+=np.abs(np.fft.ifft(np.fft.fft(blk*np.exp(-2j*np.pi*f*n/fs))*c))**2
        if acc.max()>best[0]: best=(acc.max(),f,int(acc.argmax()))
    peak,fd,cp=best
    acc=np.zeros(spms)
    for m in range(nms):
        blk=x[m*spms:(m+1)*spms]
        if len(blk)<spms: break
        acc+=np.abs(np.fft.ifft(np.fft.fft(blk*np.exp(-2j*np.pi*fd*n/fs))*c))**2
    g=int(round(fs/FCHIP)); mask=np.ones(spms,bool)
    for d in range(-g,g+1): mask[(cp+d)%spms]=False
    return peak/np.mean(acc[mask]), fd, cp

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("binfile"); ap.add_argument("--fs",type=float,default=2.6e6)
    ap.add_argument("--ms",type=int,default=10); ap.add_argument("--thr",type=float,default=12.0)
    a=ap.parse_args()
    spms=int(round(a.fs/1000.0))
    x8=np.fromfile(a.binfile,dtype=np.int8,count=spms*(a.ms+2)*2).astype(np.float64)
    if len(x8)<spms*2*3:
        print("File too short / not 8-bit I/Q."); sys.exit(1)
    x=x8[0::2]+1j*x8[1::2]
    dopp=np.arange(-6000,6001,250)
    print("="*60)
    print(" GPS L1 C/A signal verification")
    print(" file :",a.binfile)
    print(f" rate : {a.fs/1e6:.3f} Msps   integrated: {a.ms} ms")
    print("="*60)
    print(f"{'PRN':>3} {'metric':>8} {'Doppler':>9} {'code phase':>11}")
    acq=[]
    for prn in range(1,33):
        m,fd,cp=acquire(x,prn,spms,a.fs,a.ms,dopp)
        if m>a.thr:
            acq.append((prn,m,fd,cp)); print(f"{prn:3d} {m:8.1f} {fd:7d}Hz {cp:9d}   LOCK")
    print("-"*60)
    if len(acq)>=4:
        print(f" VERDICT: PASS  -  {len(acq)} satellites lock (>=4 needed for a fix).")
        print(" This file behaves as genuine GPS; a receiver will acquire and fix.")
    elif acq:
        print(f" VERDICT: WEAK  -  only {len(acq)} satellite(s). Check the ephemeris/params.")
    else:
        print(" VERDICT: FAIL  -  no satellites. Wrong rate? not 8-bit? empty file?")
    print("="*60)

if __name__=="__main__": main()
