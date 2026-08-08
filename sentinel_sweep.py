#!/usr/bin/env python3
"""
sentinel_sweep.py — CTW SENTINEL Exhaustive Wideband Power Mapper
=================================================================
Sweeps from START_MHZ to STOP_MHZ in STEP_KHZ increments (default 100 kHz).
Every frequency step gets a measured dBFS — no band table gating, no EARFCN
indexing, no scoring.  The LTE/federal band table is annotation only.

Hardware floor: AD9361 (PlutoSDR Rev.C) tunes 70 MHz – 6 GHz in stock
firmware.  Extended-tuning builds reach ~47 MHz.  25 MHz requires an
external downconverter; the code will attempt it and the hardware will
clamp at its actual minimum.

LO placement strategy:
  - LO steps in (SAMPLE_RATE - GUARD_HZ) increments so adjacent windows
    overlap by GUARD_HZ on each side — no channel falls on a DC spike or
    window edge.
  - Within each LO window, every 100 kHz channel whose center is within
    ±(SAMPLE_RATE/2 - GUARD_HZ) of the LO is extracted from the FFT.
  - FFT bin resolution at 10 Msps / 2048 points = 4.88 kHz/bin.
    A 100 kHz channel averages ~20 bins — solid power estimate.

Output:
  sweep_STAMP.jsonl.gz        — compressed log (one record per freq step per pass)
  sweep_chain_STAMP.log       — SHA-256 chain-linked record of anomalies
  runtime/sweep_live.jsonl    — live mirror

Usage:
  python sentinel_sweep.py
  python sentinel_sweep.py --start-mhz 70 --stop-mhz 6000
  python sentinel_sweep.py --start-mhz 25 --stop-mhz 6000  # attempts <70 MHz
  python sentinel_sweep.py --step-khz 100                   # default
  python sentinel_sweep.py --step-khz 25                    # 25 kHz resolution
  python sentinel_sweep.py --dwell-ms 10 --anomaly-db 10
  python sentinel_sweep.py --no-gui
  python sentinel_sweep.py --out D:\\sdr\\logs
"""

import argparse
import datetime
import gzip
import hashlib
import json
import math
import os
import threading
import time
from collections import defaultdict, deque

import numpy as np

try:
    import tkinter as tk
    from tkinter import font as tkfont
except ImportError:
    tk = None

try:
    import iio
except ImportError:
    iio = None

# ══════════════════════════════════════════════════════════════════════════════
# HARDWARE CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

AD9361_HW_MIN_HZ  =  70_000_000   # stock firmware floor
AD9361_HW_MAX_HZ  = 6_000_000_000
AD9361_EXT_MIN_HZ =  47_000_000   # extended-tuning patch floor
SAMPLE_RATE       =  10_000_000   # 10 Msps
FFT_SIZE          =       2048
BIN_HZ            = SAMPLE_RATE / FFT_SIZE          # ~4.88 kHz
GUARD_HZ          =   500_000    # edge guard — avoid DC and window roll-off

# Effective usable bandwidth per LO position
USABLE_BW_HZ = SAMPLE_RATE - 2 * GUARD_HZ          # 9.0 MHz

# LO step: advance by usable BW so windows butt up with no gap
LO_STEP_HZ = USABLE_BW_HZ                           # 9.0 MHz per hop

STAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

# ══════════════════════════════════════════════════════════════════════════════
# BAND ANNOTATION TABLE
# Lookup only — does not gate scanning.  Every 100 kHz channel gets measured
# regardless of whether it falls inside a known band.
# Sources: 3GPP TS 36.101, NTIA Manual of Regulations, FCC Part 90/27/25/15
# ══════════════════════════════════════════════════════════════════════════════

BAND_ANNOTATIONS = [
    # (lo_hz, hi_hz, label)
    # ── Sub-VHF / HF ────────────────────────────────────────────────────────
    (25_000_000,   30_000_000, "HF 25-30MHz"),
    # ── VHF Low ─────────────────────────────────────────────────────────────
    (30_000_000,   50_000_000, "VHF Low 30-50MHz"),
    (54_000_000,   72_000_000, "VHF TV Ch2-4"),
    (72_000_000,   76_000_000, "VHF 72-76 Gov"),
    (76_000_000,   88_000_000, "VHF TV Ch5-6"),
    (88_000_000,  108_000_000, "FM Broadcast"),
    (108_000_000, 137_000_000, "Aviation Nav VOR/ILS"),
    (137_000_000, 144_000_000, "Satellite 137-144"),
    (144_000_000, 148_000_000, "Amateur 2m"),
    (148_000_000, 150_000_000, "Land Mobile"),
    (150_000_000, 162_000_000, "VHF Land Mobile"),
    (162_000_000, 174_000_000, "NOAA WX / Gov VHF"),
    (174_000_000, 216_000_000, "VHF TV Ch7-13"),
    (216_000_000, 222_000_000, "Land Mobile 216-222"),
    (222_000_000, 225_000_000, "Amateur 1.25m"),
    (225_000_000, 400_000_000, "UHF Gov / DoD"),
    (380_000_000, 400_000_000, "NTIA Federal Land Mobile"),  # 386 MHz IOC
    # ── UHF Public Safety ───────────────────────────────────────────────────
    (406_000_000, 420_000_000, "Gov UHF 406-420"),
    (420_000_000, 450_000_000, "Amateur 70cm / Gov"),
    (450_000_000, 470_000_000, "UHF Land Mobile 450-470"),
    (470_000_000, 512_000_000, "UHF TV Ch14-20"),
    (512_000_000, 608_000_000, "UHF TV Ch21-36"),
    (608_000_000, 614_000_000, "Radio Astronomy / Whitespace"),
    (614_000_000, 617_000_000, "Guard Band 614-617"),
    (617_000_000, 652_000_000, "LTE Band 71 (600 MHz DL)"),
    (652_000_000, 663_000_000, "Guard Band 652-663"),
    (663_000_000, 698_000_000, "UHF TV Ch46-51"),
    (698_000_000, 729_000_000, "LTE Band 12/17 UL"),
    (729_000_000, 746_000_000, "LTE Band 12 DL / Band 17 DL"),
    (746_000_000, 756_000_000, "LTE Band 13 DL (700C)"),
    (758_000_000, 768_000_000, "FirstNet Band 14 DL"),
    (775_000_000, 788_000_000, "LTE Band 13 UL"),
    (788_000_000, 798_000_000, "LTE Band 14 UL"),
    (806_000_000, 824_000_000, "Public Safety 800MHz"),
    (824_000_000, 849_000_000, "Cellular 850 UL"),
    (851_000_000, 869_000_000, "Public Safety SMR 800"),
    (869_000_000, 894_000_000, "Cellular 850 DL / LTE Band 5 DL"),
    (894_000_000, 896_000_000, "Guard 894-896"),
    (896_000_000, 901_000_000, "SMR 900 UL"),
    (901_000_000, 902_000_000, "Guard 901-902"),
    (902_000_000, 928_000_000, "ISM 902-928 / Amateur"),
    (930_000_000, 931_000_000, "Paging 930-931"),
    (935_000_000, 940_000_000, "SMR 900 DL"),
    (940_000_000, 941_000_000, "Paging 940-941"),
    # ── L-Band ──────────────────────────────────────────────────────────────
    (1_164_000_000, 1_215_000_000, "GNSS L5 / Aviation"),
    (1_215_000_000, 1_240_000_000, "GNSS L2 / Galileo E6"),
    (1_240_000_000, 1_300_000_000, "Amateur 23cm / GNSS"),
    (1_350_000_000, 1_390_000_000, "Gov Radar 1.35-1.39G"),
    (1_390_000_000, 1_392_000_000, "Guard"),
    (1_392_000_000, 1_395_000_000, "Fixed/Mobile"),
    (1_427_000_000, 1_432_000_000, "MedRadio / Telemetry"),
    (1_432_000_000, 1_435_000_000, "Telemetry"),
    (1_452_000_000, 1_492_000_000, "L-Band Satellite DAB"),
    (1_525_000_000, 1_559_000_000, "Mobile Satellite L-Band"),
    (1_559_000_000, 1_610_000_000, "GNSS L1 (GPS/GLONASS/Galileo/BeiDou)"),
    (1_610_000_000, 1_621_000_000, "Iridium UL"),
    (1_621_000_000, 1_626_000_000, "Iridium DL"),
    (1_626_000_000, 1_660_000_000, "Mobile Satellite / Inmarsat"),
    (1_668_000_000, 1_670_000_000, "Meteosat"),
    (1_710_000_000, 1_755_000_000, "AWS-1 UL / LTE Band 4 UL"),
    (1_755_000_000, 1_780_000_000, "AWS-3 UL / Gov Relocation"),
    (1_850_000_000, 1_910_000_000, "PCS 1900 UL / LTE Band 2 UL"),
    (1_910_000_000, 1_930_000_000, "PCS G Block / Unlicensed PCS"),
    (1_930_000_000, 1_990_000_000, "PCS 1900 DL / LTE Band 2 DL"),
    (1_990_000_000, 2_000_000_000, "Guard / AWS Expansion"),
    # ── S-Band ──────────────────────────────────────────────────────────────
    (2_000_000_000, 2_020_000_000, "S-Band MSS UL"),
    (2_020_000_000, 2_025_000_000, "Space Research"),
    (2_110_000_000, 2_155_000_000, "AWS-1 DL / LTE Band 4 DL"),
    (2_155_000_000, 2_180_000_000, "AWS-3 DL"),
    (2_180_000_000, 2_200_000_000, "S-Band MSS DL"),
    (2_300_000_000, 2_310_000_000, "Amateur 13cm / WCS"),
    (2_310_000_000, 2_360_000_000, "WCS / Satellite DARS"),
    (2_360_000_000, 2_395_000_000, "Aviation Telemetry"),
    (2_395_000_000, 2_400_000_000, "Amateur"),
    (2_400_000_000, 2_483_000_000, "ISM 2.4 GHz / WiFi / BT"),
    (2_483_000_000, 2_496_000_000, "MSS 2.4G / Globalstar"),
    (2_496_000_000, 2_690_000_000, "LTE Band 41 TDD 2.5G / EBS/BRS"),
    (2_690_000_000, 2_700_000_000, "Radio Astronomy"),
    # ── C-Band ──────────────────────────────────────────────────────────────
    (3_300_000_000, 3_550_000_000, "C-Band 5G / CBRS"),
    (3_550_000_000, 3_700_000_000, "CBRS 3.5 GHz"),
    (3_700_000_000, 3_980_000_000, "C-Band Satellite / 5G C-Band"),
    (3_980_000_000, 4_000_000_000, "C-Band Guard"),
    (4_000_000_000, 4_200_000_000, "Fixed Satellite C-Band DL"),
    (4_200_000_000, 4_400_000_000, "Aviation Altimeter"),
    (4_400_000_000, 4_940_000_000, "Gov / DoD"),
    (4_940_000_000, 4_990_000_000, "Public Safety 4.9 GHz"),
    (4_990_000_000, 5_000_000_000, "Radio Astronomy"),
    # ── 5 GHz ───────────────────────────────────────────────────────────────
    (5_000_000_000, 5_150_000_000, "Aviation ARNS / Unlicensed"),
    (5_150_000_000, 5_250_000_000, "UNII-1 WiFi 5GHz"),
    (5_250_000_000, 5_350_000_000, "UNII-2A WiFi / DFS"),
    (5_350_000_000, 5_470_000_000, "Gov Radar"),
    (5_470_000_000, 5_725_000_000, "UNII-2C WiFi / DFS"),
    (5_725_000_000, 5_850_000_000, "ISM 5.8 GHz / UNII-3"),
    (5_850_000_000, 5_925_000_000, "UNII-4 / V2X / C-V2X"),
    (5_925_000_000, 6_000_000_000, "UNII-5 6GHz WiFi (lower)"),
    # ── 6 GHz ───────────────────────────────────────────────────────────────
    (6_000_000_000, 6_425_000_000, "Fixed Microwave / UNII-5/6/7/8 WiFi6E"),
]

# Sort by low edge for binary search
BAND_ANNOTATIONS.sort(key=lambda x: x[0])

# Known IOC frequencies (Hz) — annotated in log, highlighted in GUI
IOC_HZ = {
    386_000_000: "NTIA_FED_386MHz_PERSISTENT",
    386_020_000: "NTIA_FED_386.02MHz_PERSISTENT",
    66_586 * 100_000: None,   # placeholder — EARFCN maps don't apply here
}
# CTW-11 IOC LTE channel centers (computed from EARFCN)
# Band 66 EARFCN 66586: 2110.0 + 0.1*(66586-66436) = 2125.0 MHz
# Band 2  EARFCN 1000:  1930.0 + 0.1*(1000-600)    = 1970.0 MHz
# Band 71 EARFCN 68911: 617.0  + 0.1*(68911-68586) = 649.5 MHz
IOC_HZ_LABELED = {
    386_000_000:     "CTW11_386MHz_NTIA",
    386_020_000:     "CTW11_386.02MHz_NTIA",
    2_125_000_000:   "CTW11_B66_EARFCN66586_ROGUE",
    1_970_000_000:   "CTW11_B2_EARFCN1000_PHANTOM",
    649_500_000:     "CTW11_B71_EARFCN68911_PCI186",
}

# ══════════════════════════════════════════════════════════════════════════════
# OOB GUARD
# ══════════════════════════════════════════════════════════════════════════════

def _cf(v, lo, hi):
    try:
        f = float(v)
        if not (-1e18 < f < 1e18):
            return lo
        return max(lo, min(hi, f))
    except Exception:
        return lo

def _ci(v, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except Exception:
        return lo

# ══════════════════════════════════════════════════════════════════════════════
# BAND ANNOTATION LOOKUP
# ══════════════════════════════════════════════════════════════════════════════

def annotate_hz(freq_hz: int) -> str:
    """Return the band label for a frequency, or empty string."""
    for lo, hi, label in BAND_ANNOTATIONS:
        if lo <= freq_hz < hi:
            return label
        if lo > freq_hz:
            break
    return ""

def ioc_label(freq_hz: int, step_hz: int = 100_000) -> str:
    """Return IOC label if freq_hz is within one step of a known IOC."""
    for ioc_f, label in IOC_HZ_LABELED.items():
        if abs(freq_hz - ioc_f) <= step_hz:
            return label
    return ""

# ══════════════════════════════════════════════════════════════════════════════
# CLOCK ANCHOR
# ══════════════════════════════════════════════════════════════════════════════

class ClockAnchor:
    def __init__(self):
        best_gap = None
        for _ in range(32):
            t1 = time.perf_counter_ns()
            w  = time.time_ns()
            t2 = time.perf_counter_ns()
            gap = t2 - t1
            if best_gap is None or gap < best_gap:
                best_gap         = gap
                self._mono_epoch = (t1 + t2) // 2
                self._wall_epoch = w
        self.session_wall_ns  = self._wall_epoch
        self.session_wall_utc = datetime.datetime.fromtimestamp(
            self._wall_epoch / 1e9, tz=datetime.timezone.utc).isoformat()

    def now(self):
        delta = time.perf_counter_ns() - self._mono_epoch
        return self._wall_epoch + delta, delta

    def fmt(self, w: int) -> str:
        s, f = w // 1_000_000_000, w % 1_000_000_000
        return (datetime.datetime.fromtimestamp(s, tz=datetime.timezone.utc)
                .strftime('%Y-%m-%dT%H:%M:%S') + f'.{f:09d}Z')

# ══════════════════════════════════════════════════════════════════════════════
# GZIP LOG + LIVE MIRROR
# ══════════════════════════════════════════════════════════════════════════════

class GzipLog:
    def __init__(self, path, header):
        self.path  = path
        self._q    = deque()
        self._ev   = threading.Event()
        self._stop = threading.Event()
        self._th   = threading.Thread(target=self._run, daemon=True)
        with gzip.open(path, 'ab', compresslevel=6) as gz:
            gz.write((json.dumps(header, separators=(',', ':')) + '\n').encode())
        self._th.start()

    def write(self, obj):
        self._q.append(obj); self._ev.set()

    def close(self):
        self._stop.set(); self._ev.set(); self._th.join(timeout=8)

    def _run(self):
        while not self._stop.is_set():
            self._ev.wait(); self._ev.clear(); self._drain()
        self._drain()

    def _drain(self):
        if not self._q: return
        lines = []
        while self._q:
            lines.append(json.dumps(self._q.popleft(), separators=(',', ':')))
        with gzip.open(self.path, 'ab', compresslevel=6) as gz:
            gz.write(('\n'.join(lines) + '\n').encode())


class LiveMirror:
    def __init__(self, path):
        self.path  = path
        self._lock = threading.Lock()
        open(path, 'w').close()

    def write(self, obj):
        line = json.dumps(obj, separators=(',', ':')) + '\n'
        with self._lock:
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(line)

# ══════════════════════════════════════════════════════════════════════════════
# SHA-256 CHAIN LOG
# ══════════════════════════════════════════════════════════════════════════════

class EvidenceChain:
    """
    Chain-links every anomaly and every IOC hit.
    Normal samples are NOT chained individually — the pass-end record
    commits a cumulative SHA-256 of all samples in the pass instead,
    keeping the chain log tractable for a 59,000-channel sweep.
    """

    def __init__(self, path: str):
        self._path    = path
        self._prev    = "GENESIS"
        self._seq     = 0
        self._lock    = threading.Lock()
        self._pass_h  = hashlib.sha256()   # rolling hash of all pass samples
        with open(path, 'w', encoding='utf-8') as f:
            f.write(
                "# CTW SENTINEL — Wideband Sweep Chain Log\n"
                "# SEQ|TIMESTAMP|FREQ_HZ|FREQ_MHZ|DBFS|EXCESS_DB|IOC|BAND|PREV_HASH|THIS_HASH\n"
                f"# Started: {datetime.datetime.utcnow().isoformat()}\n"
            )

    def feed_sample(self, freq_hz: int, dbfs: float):
        """Fold every sample into the pass rolling hash (no file I/O)."""
        self._pass_h.update(f"{freq_hz}:{dbfs:.2f}\n".encode())

    def write_anomaly(self, freq_hz: int, freq_mhz: float,
                      dbfs: float, excess_db: float,
                      ioc: str, band: str) -> str:
        with self._lock:
            self._seq += 1
            ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            record = (f"{self._seq}|{ts}|{freq_hz}|{freq_mhz:.4f}|"
                      f"{dbfs:.2f}|{excess_db:.2f}|{ioc or 'ANOMALY'}|"
                      f"{band or 'UNALLOCATED'}|{self._prev}")
            h = hashlib.sha256(record.encode()).hexdigest()
            with open(self._path, 'a', encoding='utf-8') as f:
                f.write(f"{record}|{h}\n")
            self._prev = h
            return h

    def write_pass_end(self, pass_num: int, samples: int,
                       anomalies: int) -> str:
        """Commit the rolling pass hash into the chain."""
        with self._lock:
            self._seq += 1
            ts        = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            pass_digest = self._pass_h.hexdigest()
            record    = (f"{self._seq}|{ts}|PASS_END|{pass_num}|"
                         f"samples={samples}|anomalies={anomalies}|"
                         f"pass_digest={pass_digest}|{self._prev}")
            h = hashlib.sha256(record.encode()).hexdigest()
            with open(self._path, 'a', encoding='utf-8') as f:
                f.write(f"{record}|{h}\n")
            self._prev   = h
            self._pass_h = hashlib.sha256()   # reset for next pass
            return h

    @property
    def tip(self): return self._prev

# ══════════════════════════════════════════════════════════════════════════════
# SWEEP STATE  — thread-safe, shared between sweep thread and GUI
# ══════════════════════════════════════════════════════════════════════════════

class SweepState:
    """
    Stores per-frequency-step measurements.

    freq_hz keys are the nominal channel center (multiples of step_hz).
    Noise floor = 25th-percentile of the last HISTORY_DEPTH samples for
    that channel — adapts to band-level differences in background noise.
    """

    HISTORY_DEPTH = 32   # samples per channel for floor estimation

    def __init__(self, step_hz: int, anomaly_db: float):
        self._lock        = threading.Lock()
        self.step_hz      = step_hz
        self.anomaly_db   = anomaly_db
        # freq_hz -> latest dBFS
        self._latest:  dict[int, float] = {}
        # freq_hz -> all-time peak dBFS
        self._peak:    dict[int, float] = {}
        # freq_hz -> rolling history for floor
        self._history: dict[int, deque] = defaultdict(
            lambda: deque(maxlen=self.HISTORY_DEPTH))
        self.pass_count    = 0
        self.total_samples = 0
        self.anomaly_count = 0

    def update(self, freq_hz: int, dbfs: float):
        with self._lock:
            self._latest[freq_hz] = dbfs
            h = self._history[freq_hz]
            h.append(dbfs)
            if freq_hz not in self._peak or dbfs > self._peak[freq_hz]:
                self._peak[freq_hz] = dbfs
            self.total_samples += 1

    def noise_floor(self, freq_hz: int) -> float:
        with self._lock:
            h = list(self._history[freq_hz])
        if not h:
            return -120.0
        s = sorted(h)
        return s[max(0, len(s) // 4)]

    def excess_db(self, freq_hz: int) -> float:
        with self._lock:
            lat = self._latest.get(freq_hz)
        if lat is None:
            return 0.0
        return lat - self.noise_floor(freq_hz)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "latest":    dict(self._latest),
                "peak":      dict(self._peak),
                "pass":      self.pass_count,
                "samples":   self.total_samples,
                "anomalies": self.anomaly_count,
            }

# ══════════════════════════════════════════════════════════════════════════════
# PLUTO INTERFACE
# ══════════════════════════════════════════════════════════════════════════════

def configure_pluto(uri: str, gain_db: int = 40):
    ctx   = iio.Context(uri)
    phy   = ctx.find_device("ad9361-phy")
    rxdev = ctx.find_device("cf-ad9361-lpc")
    rx    = phy.find_channel("voltage0", False)
    for attr, val in [
        ("gain_control_mode",  "manual"),
        ("hardwaregain",       str(gain_db)),
        ("rf_bandwidth",       str(SAMPLE_RATE)),
        ("sampling_frequency", str(SAMPLE_RATE)),
    ]:
        try:
            rx.attrs[attr].value = val
        except Exception:
            pass
    for ch in rxdev.channels:
        ch.enabled = ch.id in ("voltage0", "voltage1")
    buf = iio.Buffer(rxdev, FFT_SIZE * 2, False)
    lo  = phy.find_channel("altvoltage0", True)
    return ctx, phy, rxdev, buf, lo


def fft_dbfs(raw: np.ndarray) -> tuple:
    """
    Returns (freqs_relative_hz, power_dbfs) arrays.
    freqs_relative_hz is relative to the LO (0 = DC).
    """
    i_ch = raw[0::2].astype(np.float32)
    q_ch = raw[1::2].astype(np.float32)
    n    = min(len(i_ch), len(q_ch), FFT_SIZE)
    win  = np.blackman(n)
    spec = np.abs(np.fft.fftshift(
        np.fft.fft((i_ch[:n] + 1j * q_ch[:n]) * win, n=FFT_SIZE)
    )) ** 2
    dbfs  = 10.0 * np.log10(spec / (32768.0 ** 2) / n + 1e-12)
    freqs = np.fft.fftshift(np.fft.fftfreq(FFT_SIZE, 1.0 / SAMPLE_RATE))
    return freqs, dbfs

# ══════════════════════════════════════════════════════════════════════════════
# CHANNEL LIST BUILDER
# Produces the complete ordered list of (freq_hz) channel centers to sweep.
# ══════════════════════════════════════════════════════════════════════════════

def build_channel_list(start_hz: int, stop_hz: int, step_hz: int) -> list:
    """
    Returns sorted list of channel center frequencies in Hz.
    start_hz and stop_hz are inclusive.
    """
    # Snap start to nearest step multiple
    start_snap = (start_hz // step_hz) * step_hz
    channels = []
    f = start_snap
    while f <= stop_hz:
        if f >= start_hz:
            channels.append(f)
        f += step_hz
    return channels


def build_lo_schedule(channels: list, start_hz: int, stop_hz: int) -> list:
    """
    Compute the minimal ordered list of LO center frequencies needed to
    cover every channel.  Each LO position covers a USABLE_BW_HZ window.
    LO positions are constrained to AD9361 range; channels outside hardware
    range are flagged but still included in the schedule for logging.

    Returns list of (lo_hz_clamped, lo_hz_requested) tuples.
    """
    if not channels:
        return []
    lo_positions = []
    lo_hz = start_hz + SAMPLE_RATE // 2
    while lo_hz <= stop_hz + SAMPLE_RATE // 2:
        lo_clamped = _ci(lo_hz, AD9361_HW_MIN_HZ, AD9361_HW_MAX_HZ)
        lo_positions.append((lo_clamped, lo_hz))
        lo_hz += LO_STEP_HZ
    return lo_positions

# ══════════════════════════════════════════════════════════════════════════════
# SWEEP LOOP
# ══════════════════════════════════════════════════════════════════════════════

def sweep_loop(uri: str, channels: list, lo_schedule: list,
               step_hz: int, dwell_ms: int,
               state: SweepState,
               log: GzipLog, mirror: LiveMirror, chain: EvidenceChain,
               clock: ClockAnchor, stop_event: threading.Event,
               anomaly_db: float, gain_db: int,
               start_hz: int):
    """
    Main sweep loop.  One pass = one full traversal of lo_schedule.
    For each LO position, capture one FFT buffer, then extract dBFS for
    every channel whose center falls within the usable window.
    """

    if iio is None:
        print("[SWEEP] libiio not available — cannot sweep")
        stop_event.wait()
        return

    n_channels = len(channels)
    # Build channel index for fast window lookup:
    # sorted array of channel Hz values for np.searchsorted
    ch_array = np.array(channels, dtype=np.int64)

    # Warn if hardware can't reach requested start
    if start_hz < AD9361_HW_MIN_HZ:
        print(
            f"[SWEEP] WARNING: requested start {start_hz/1e6:.2f} MHz is below "
            f"AD9361 hardware minimum {AD9361_HW_MIN_HZ/1e6:.0f} MHz. "
            f"Channels below {AD9361_HW_MIN_HZ/1e6:.0f} MHz will be logged "
            f"with dbfs=null (hardware clamped)."
        )

    print(f"[SWEEP] Connecting to PlutoSDR at {uri}...")
    try:
        ctx, phy, rxdev, buf, lo = configure_pluto(uri, gain_db)
    except Exception as e:
        print(f"[SWEEP] PlutoSDR connect failed: {e}")
        stop_event.wait()
        return

    n_lo = len(lo_schedule)
    print(
        f"[SWEEP] PlutoSDR up.  "
        f"{n_channels:,} channels  |  "
        f"{n_lo} LO positions per pass  |  "
        f"step={step_hz/1e3:.0f} kHz  |  "
        f"dwell={dwell_ms} ms/LO"
    )

    pass_num = 0

    while not stop_event.is_set():
        pass_num        += 1
        state.pass_count = pass_num
        pass_samples     = 0
        pass_anomalies   = 0

        for lo_idx, (lo_clamped, lo_requested) in enumerate(lo_schedule):
            if stop_event.is_set():
                break

            hw_limited = (lo_requested < AD9361_HW_MIN_HZ
                          or lo_requested > AD9361_HW_MAX_HZ)

            if not hw_limited:
                try:
                    lo.attrs["frequency"].value = str(lo_clamped)
                    time.sleep(max(0.002, dwell_ms / 1000.0))
                    buf.refill()
                    raw = np.frombuffer(buf.read(), dtype=np.int16).copy()
                except Exception as e:
                    print(f"\n[SWEEP] buf error LO={lo_clamped/1e6:.2f}MHz: {e}")
                    continue
                if len(raw) < FFT_SIZE * 2:
                    continue
                freqs_rel, dbfs_spectrum = fft_dbfs(raw)
            else:
                freqs_rel       = None
                dbfs_spectrum   = None

            wall_ns, _ = clock.now()

            # Window boundaries in absolute Hz
            win_lo = lo_clamped - (SAMPLE_RATE // 2) + GUARD_HZ
            win_hi = lo_clamped + (SAMPLE_RATE // 2) - GUARD_HZ

            # Find channels in this window using searchsorted
            idx_lo = np.searchsorted(ch_array, win_lo, side='left')
            idx_hi = np.searchsorted(ch_array, win_hi, side='right')

            for ch_idx in range(idx_lo, idx_hi):
                freq_hz  = int(ch_array[ch_idx])
                freq_mhz = freq_hz / 1e6

                if hw_limited or freqs_rel is None:
                    dbfs = None
                else:
                    offset_hz = freq_hz - lo_clamped
                    # Extract bins within ±(step_hz/2) of channel center
                    half = step_hz / 2.0
                    mask = np.abs(freqs_rel - offset_hz) <= half
                    if mask.sum() == 0:
                        # Widen to nearest single bin if step < bin resolution
                        nearest = np.argmin(np.abs(freqs_rel - offset_hz))
                        mask    = np.zeros(len(freqs_rel), dtype=bool)
                        mask[nearest] = True
                    dbfs = _cf(float(np.mean(dbfs_spectrum[mask])),
                               -130.0, 0.0)

                band = annotate_hz(freq_hz)
                ioc  = ioc_label(freq_hz, step_hz)

                if dbfs is not None:
                    state.update(freq_hz, dbfs)
                    excess   = state.excess_db(freq_hz)
                    is_anom  = (excess >= anomaly_db)
                    chain.feed_sample(freq_hz, dbfs)
                else:
                    excess  = 0.0
                    is_anom = False

                pass_samples += 1

                # ── Log record ───────────────────────────────────────────────
                rec = {
                    "t":    "S",              # "S" = SWEEP_SAMPLE (compact)
                    "p":    pass_num,
                    "w":    wall_ns,
                    "f":    freq_hz,
                    "mhz":  round(freq_mhz, 2),
                    "db":   round(dbfs, 2) if dbfs is not None else None,
                    "ex":   round(excess, 2) if dbfs is not None else None,
                    "a":    int(is_anom),
                    "ioc":  ioc or None,
                    "band": band or None,
                }
                log.write(rec)

                # Mirror and chain only anomalies + IOC hits to limit I/O
                if is_anom or ioc:
                    mirror.write(rec)
                    h = chain.write_anomaly(
                        freq_hz, freq_mhz,
                        dbfs if dbfs is not None else -999.0,
                        excess, ioc, band
                    )
                    state.anomaly_count += 1
                    pass_anomalies      += 1
                    print(
                        f"\n  {'[IOC] ' if ioc else '[!]  '}"
                        f"{freq_mhz:>9.2f} MHz  "
                        f"{dbfs:>7.2f} dBFS  "
                        f"+{excess:.1f} dB  "
                        f"{ioc or ''}  {band or ''}  "
                        f"hash={h[:12]}...",
                        flush=True
                    )
                else:
                    print(
                        f"\r  pass={pass_num}  "
                        f"LO={lo_clamped/1e6:>8.2f}MHz  "
                        f"{freq_mhz:>9.2f}MHz  "
                        f"{dbfs if dbfs is not None else '---':>7} dBFS  "
                        f"ch={pass_samples:,}/{n_channels:,}   ",
                        end='', flush=True
                    )

        # Pass complete
        h_end = chain.write_pass_end(pass_num, pass_samples, pass_anomalies)
        end_rec = {
            "t":         "P",
            "pass":      pass_num,
            "w":         clock.now()[0],
            "samples":   pass_samples,
            "anomalies": pass_anomalies,
            "chain_tip": h_end[:16] + "...",
        }
        log.write(end_rec)
        mirror.write(end_rec)
        print(
            f"\n  [PASS {pass_num}] "
            f"samples={pass_samples:,}  "
            f"anomalies={pass_anomalies}  "
            f"chain={h_end[:16]}...",
            flush=True
        )

# ══════════════════════════════════════════════════════════════════════════════
# GUI — WATERFALL + POWER SPECTRUM
#
# Two panels stacked vertically:
#   Top: Power spectrum — frequency on X, dBFS on Y, color = band annotation
#        Anomalies marked with vertical red tick above the trace.
#        IOC frequencies marked with a labeled vertical line.
#
#   Bottom: Waterfall — frequency on X, time (passes) scrolling down,
#           pixel color = dBFS mapped to heat palette.
#
# Scrollable X axis via horizontal scrollbar.
# Toggle between LATEST and PEAK power traces.
# ══════════════════════════════════════════════════════════════════════════════

# Heat palette: black -> deep blue -> cyan -> green -> yellow -> red
_PALETTE_STOPS = [
    (0.00, (  0,   0,   0)),
    (0.15, (  0,   0, 128)),
    (0.35, (  0,  80, 200)),
    (0.55, (  0, 200, 180)),
    (0.70, ( 60, 220,  60)),
    (0.85, (220, 220,   0)),
    (1.00, (255,   0,   0)),
]

def _palette_color(v: float) -> str:
    """v in [0,1] -> '#rrggbb'"""
    v = max(0.0, min(1.0, v))
    for i in range(len(_PALETTE_STOPS) - 1):
        t0, c0 = _PALETTE_STOPS[i]
        t1, c1 = _PALETTE_STOPS[i + 1]
        if t0 <= v <= t1:
            frac = (v - t0) / (t1 - t0)
            r = int(c0[0] + (c1[0] - c0[0]) * frac)
            g = int(c0[1] + (c1[1] - c0[1]) * frac)
            b = int(c0[2] + (c1[2] - c0[2]) * frac)
            return f"#{r:02x}{g:02x}{b:02x}"
    return "#ff0000"

def dbfs_to_heat(dbfs: float, floor: float = -120.0, ceil: float = 0.0) -> str:
    v = (dbfs - floor) / (ceil - floor)
    return _palette_color(v)


class SweepGUI:
    """
    Wideband power spectrum + waterfall display.

    The full channel list is too wide to fit on screen at 1px/channel
    when scanning 25 MHz – 6 GHz at 100 kHz steps (~59,000 channels).
    The canvas is rendered at a fixed px-per-MHz scale; a horizontal
    scrollbar lets you pan.  The visible window is ~500 MHz wide by
    default.

    Waterfall panel accumulates one row per GUI refresh tick (250 ms),
    not per pass — passes are much slower than the refresh rate, so
    the waterfall shows the live scan position sweeping across rather
    than completed passes stacking.
    """

    SPEC_H       = 280     # height of power spectrum panel (px)
    WFALL_H      = 280     # height of waterfall panel (px)
    PX_PER_MHZ   = 0.12   # horizontal scale: 0.12 px per MHz = ~720 px for 6000 MHz full span
    DB_FLOOR     = -120.0
    DB_CEIL      =    0.0
    WFALL_ROWS   =  200    # waterfall history depth

    def __init__(self, state: SweepState, channels: list,
                 start_hz: int, stop_hz: int, clock: ClockAnchor):
        if tk is None:
            raise RuntimeError("tkinter not available")
        self.state    = state
        self.channels = channels          # sorted list of freq_hz
        self.ch_arr   = np.array(channels, dtype=np.int64)
        self.start_hz = start_hz
        self.stop_hz  = stop_hz
        self.clock    = clock
        self._mode    = "LATEST"          # LATEST | PEAK

        self.root = tk.Tk()
        self.root.title("CTW SENTINEL — Wideband RF Power Map")
        self.root.configure(bg="#0e0e10")
        self.root.minsize(900, 640)

        self._span_hz      = stop_hz - start_hz
        self._canvas_w_px  = max(900, int(self._span_hz / 1e6 * self.PX_PER_MHZ * 1e6 / 1e6 * 1000))
        # Actually: px = span_MHz * PX_PER_MHZ  where span_MHz could be 5975
        self._canvas_w_px  = max(900, int((self._span_hz / 1e6) * self.PX_PER_MHZ))
        # That gives 0.12 * 5975 ≈ 717 px for full span — fine default
        # At --px-per-mhz 1.0 it would be 5975 px — still scrollable

        # Waterfall row buffer: list of dicts {freq_hz: dbfs}
        self._wfall_rows: deque = deque(maxlen=self.WFALL_ROWS)

        self._status = tk.StringVar(value="Waiting...")
        self._build_ui()
        self._tick()

    def _hz_to_x(self, freq_hz: int) -> float:
        return (freq_hz - self.start_hz) / 1e6 * self.PX_PER_MHZ

    def _build_ui(self):
        # ── Mode toggle ───────────────────────────────────────────────────────
        ctrl = tk.Frame(self.root, bg="#0e0e10", pady=4, padx=8)
        ctrl.pack(fill="x")
        tk.Label(ctrl, text="Trace:", bg="#0e0e10", fg="#888890",
                 font=("Segoe UI", 9)).pack(side="left")
        for label in ("LATEST", "PEAK"):
            tk.Radiobutton(
                ctrl, text=label,
                variable=tk.StringVar(value=self._mode),
                value=label,
                command=lambda l=label: setattr(self, "_mode", l),
                bg="#0e0e10", fg="#888890", selectcolor="#0e0e10",
                font=("Consolas", 9)
            ).pack(side="left", padx=4)

        # ── Canvas + scrollbar ────────────────────────────────────────────────
        frame = tk.Frame(self.root, bg="#0e0e10")
        frame.pack(fill="both", expand=True)

        total_h = self.SPEC_H + self.WFALL_H + 20  # +20 for freq axis
        self.canvas = tk.Canvas(
            frame,
            width=900,
            height=total_h,
            bg="#0e0e10",
            highlightthickness=0,
            scrollregion=(0, 0, self._canvas_w_px, total_h)
        )
        hbar = tk.Scrollbar(frame, orient="horizontal",
                            command=self.canvas.xview)
        self.canvas.configure(xscrollcommand=hbar.set)
        hbar.pack(side="bottom", fill="x")
        self.canvas.pack(side="top", fill="both", expand=True)

        # ── Status bar ────────────────────────────────────────────────────────
        sb = tk.Frame(self.root, bg="#111115", pady=3, padx=8)
        sb.pack(fill="x", side="bottom")
        tk.Label(sb, textvariable=self._status, bg="#111115", fg="#555560",
                 font=("Segoe UI", 8)).pack(anchor="w")

    def _tick(self):
        try:
            self._draw()
        except Exception:
            pass
        s = self.state.snapshot()
        self._status.set(
            f"Pass {s['pass']}  |  "
            f"Samples {s['samples']:,}  |  "
            f"Anomalies {s['anomalies']:,}  |  "
            f"Mode: {self._mode}"
        )
        self.root.after(250, self._tick)

    def _draw(self):
        c = self.canvas
        c.delete("trace")    # spectrum trace
        c.delete("wfall")    # waterfall pixels
        c.delete("axis")     # frequency axis

        snap = self.state.snapshot()
        data = snap["latest"] if self._mode == "LATEST" else snap["peak"]
        if not data:
            return

        W   = self._canvas_w_px
        SH  = self.SPEC_H
        WH  = self.WFALL_H
        AH  = 20
        DB_RANGE = self.DB_CEIL - self.DB_FLOOR

        # ── Spectrum panel background grid ────────────────────────────────────
        for db in (-120, -90, -60, -30, 0):
            y = SH - int((db - self.DB_FLOOR) / DB_RANGE * SH)
            c.create_line(0, y, W, y, fill="#1e1e22", tags="trace")
            c.create_text(4, y - 6, text=f"{db}", fill="#555560",
                          font=("Consolas", 7), anchor="w", tags="trace")

        # Band boundary lines (draw lightly before trace)
        for lo, hi, label in BAND_ANNOTATIONS:
            if hi < self.start_hz or lo > self.stop_hz:
                continue
            x0 = self._hz_to_x(max(lo, self.start_hz))
            x1 = self._hz_to_x(min(hi, self.stop_hz))
            mid = (x0 + x1) / 2
            c.create_rectangle(x0, 0, x1, SH,
                                fill="#111116", outline="", tags="trace")
            if x1 - x0 > 30:
                c.create_text(mid, 8, text=label[:16],
                              fill="#333340", font=("Consolas", 6),
                              anchor="n", tags="trace")

        # ── Spectrum trace: polyline of (x, y) per channel ───────────────────
        wfall_row = {}
        pts = []
        for freq_hz, dbfs in sorted(data.items()):
            x  = self._hz_to_x(freq_hz)
            y  = SH - int((dbfs - self.DB_FLOOR) / DB_RANGE * SH)
            y  = max(0, min(SH, y))
            pts.extend([x, y])
            wfall_row[freq_hz] = dbfs

        if len(pts) >= 4:
            c.create_line(*pts, fill="#2a78d6", width=1, tags="trace")

        # Anomaly ticks and IOC labels
        for freq_hz, dbfs in data.items():
            excess = self.state.excess_db(freq_hz)
            ioc    = ioc_label(freq_hz)
            x      = self._hz_to_x(freq_hz)
            if excess >= self.state.anomaly_db:
                c.create_line(x, 0, x, 10, fill="#eda100",
                              width=2, tags="trace")
            if ioc:
                c.create_line(x, 0, x, SH, fill="#ff0000",
                              width=1, dash=(3, 2), tags="trace")
                c.create_text(x + 2, 18, text=ioc.split("_")[0],
                              fill="#ff4444", font=("Consolas", 6),
                              anchor="w", tags="trace")

        # ── Waterfall ─────────────────────────────────────────────────────────
        if wfall_row:
            self._wfall_rows.append(wfall_row)

        row_h = max(1, WH // max(1, len(self._wfall_rows)))
        for row_idx, row in enumerate(self._wfall_rows):
            y_top = SH + row_idx * row_h
            for freq_hz, dbfs in row.items():
                x0 = self._hz_to_x(freq_hz)
                x1 = x0 + max(1, self._hz_to_x(freq_hz + self.state.step_hz) - x0)
                color = dbfs_to_heat(dbfs, self.DB_FLOOR, self.DB_CEIL)
                c.create_rectangle(x0, y_top, x1, y_top + row_h,
                                   fill=color, outline="", tags="wfall")

        # ── Frequency axis ────────────────────────────────────────────────────
        axis_y = SH + WH
        c.create_line(0, axis_y, W, axis_y, fill="#2a2a30", tags="axis")

        # Tick every 100 MHz
        f = (self.start_hz // 100_000_000) * 100_000_000
        while f <= self.stop_hz:
            if f >= self.start_hz:
                x = self._hz_to_x(f)
                c.create_line(x, axis_y, x, axis_y + 5,
                              fill="#33333a", tags="axis")
                c.create_text(x, axis_y + 6, text=f"{f//1_000_000}",
                              fill="#555560", font=("Consolas", 6),
                              anchor="n", tags="axis")
            f += 100_000_000

    def run(self):
        self.root.mainloop()

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(
        description="CTW SENTINEL — Exhaustive Wideband Signal Strength Sweeper"
    )
    ap.add_argument("--uri",         default="ip:192.168.2.1")
    ap.add_argument("--start-mhz",   type=float, default=25.0,
                    help="Start frequency in MHz (hardware floor is 70 MHz "
                         "stock, ~47 MHz extended; 25 MHz requires downconv)")
    ap.add_argument("--stop-mhz",    type=float, default=6000.0,
                    help="Stop frequency in MHz (max 6000)")
    ap.add_argument("--step-khz",    type=float, default=100.0,
                    help="Channel step in kHz (default 100 = 0.1 MHz)")
    ap.add_argument("--dwell-ms",    type=int,   default=10,
                    help="Settle+capture time per LO hop in ms (min 2)")
    ap.add_argument("--anomaly-db",  type=float, default=12.0,
                    help="dB above per-channel floor to flag as anomaly")
    ap.add_argument("--gain",        type=int,   default=40,
                    help="PlutoSDR RX manual gain in dB")
    ap.add_argument("--no-gui",      action="store_true")
    ap.add_argument("--px-per-mhz",  type=float, default=0.12,
                    help="GUI horizontal scale: pixels per MHz (default 0.12)")
    ap.add_argument("--out",         default=".", metavar="DIR")
    args = ap.parse_args()

    # Sanitize inputs
    start_hz  = _ci(int(args.start_mhz  * 1e6), 25_000_000, 6_000_000_000)
    stop_hz   = _ci(int(args.stop_mhz   * 1e6), start_hz + 1_000_000, 6_000_000_000)
    step_hz   = _ci(int(args.step_khz   * 1e3), 1_000, 10_000_000)
    dwell_ms  = max(2, args.dwell_ms)
    anomaly_db = max(1.0, args.anomaly_db)
    gain_db   = _ci(args.gain, 0, 73)

    # Build channel list and LO schedule
    channels    = build_channel_list(start_hz, stop_hz, step_hz)
    lo_schedule = build_lo_schedule(channels, start_hz, stop_hz)
    n_ch        = len(channels)
    n_lo        = len(lo_schedule)

    # Estimate pass time
    pass_time_s = n_lo * (dwell_ms / 1000.0 + 0.003)   # +3ms overhead/hop

    out_dir     = os.path.abspath(args.out)
    runtime_dir = os.path.join(out_dir, "runtime")
    os.makedirs(out_dir,     exist_ok=True)
    os.makedirs(runtime_dir, exist_ok=True)

    gz_path    = os.path.join(out_dir,     f"sweep_{STAMP}.jsonl.gz")
    live_path  = os.path.join(runtime_dir, "sweep_live.jsonl")
    chain_path = os.path.join(out_dir,     f"sweep_chain_{STAMP}.log")

    clock = ClockAnchor()
    state = SweepState(step_hz, anomaly_db)

    header = {
        "type":             "sweep_header",
        "session_wall_utc": clock.session_wall_utc,
        "session_wall_ns":  clock.session_wall_ns,
        "pluto_uri":        args.uri,
        "start_hz":         start_hz,
        "stop_hz":          stop_hz,
        "step_hz":          step_hz,
        "step_khz":         step_hz / 1e3,
        "start_mhz":        start_hz / 1e6,
        "stop_mhz":         stop_hz  / 1e6,
        "n_channels":       n_ch,
        "n_lo_positions":   n_lo,
        "dwell_ms":         dwell_ms,
        "anomaly_db":       anomaly_db,
        "fft_size":         FFT_SIZE,
        "sample_rate_hz":   SAMPLE_RATE,
        "usable_bw_hz":     USABLE_BW_HZ,
        "guard_hz":         GUARD_HZ,
        "gain_db":          gain_db,
        "hw_min_hz":        AD9361_HW_MIN_HZ,
        "hw_max_hz":        AD9361_HW_MAX_HZ,
        "stamp":            STAMP,
        "ioc_hz":           list(IOC_HZ_LABELED.keys()),
    }

    log    = GzipLog(gz_path, header)
    mirror = LiveMirror(live_path)
    mirror.write(header)
    chain  = EvidenceChain(chain_path)

    span_mhz = (stop_hz - start_hz) / 1e6

    print(f"\n{'='*72}")
    print(f"  CTW SENTINEL — Exhaustive Wideband Signal Strength Sweeper")
    print(f"{'='*72}")
    print(f"  Range       : {start_hz/1e6:.2f} MHz — {stop_hz/1e6:.2f} MHz  ({span_mhz:.0f} MHz span)")
    print(f"  Step        : {step_hz/1e3:.1f} kHz  ({n_ch:,} channels)")
    print(f"  LO hops/pass: {n_lo:,}  (usable BW {USABLE_BW_HZ/1e6:.0f} MHz/hop)")
    print(f"  Est. pass   : {pass_time_s:.0f} s  ({pass_time_s/60:.1f} min)")
    print(f"  Gain        : {gain_db} dB  |  Dwell: {dwell_ms} ms/hop")
    print(f"  Anomaly thr : {anomaly_db} dB above per-channel floor")
    if start_hz < AD9361_HW_MIN_HZ:
        print(f"  *** {start_hz/1e6:.0f}–{AD9361_HW_MIN_HZ/1e6:.0f} MHz below HW floor — "
              f"will log dbfs=null for those channels ***")
    print(f"  Log         : {gz_path}")
    print(f"  Chain       : {chain_path}")
    print(f"  Ctrl+C to stop.")
    print(f"{'='*72}\n")

    stop_event = threading.Event()

    sweep_th = threading.Thread(
        target=sweep_loop,
        args=(args.uri, channels, lo_schedule, step_hz, dwell_ms,
              state, log, mirror, chain, clock, stop_event,
              anomaly_db, gain_db, start_hz),
        daemon=True, name="SweepThread"
    )
    sweep_th.start()

    if args.no_gui or tk is None:
        try:
            while not stop_event.is_set():
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
    else:
        SweepGUI.PX_PER_MHZ = args.px_per_mhz
        gui = SweepGUI(state, channels, start_hz, stop_hz, clock)
        try:
            gui.run()
        except KeyboardInterrupt:
            pass

    stop_event.set()
    sweep_th.join(timeout=10)

    snap = state.snapshot()
    wall_ns, _ = clock.now()
    end_rec = {
        "type":      "session_end",
        "wall_iso":  clock.fmt(wall_ns),
        "passes":    snap["pass"],
        "samples":   snap["samples"],
        "anomalies": snap["anomalies"],
        "chain_tip": chain.tip,
    }
    log.write(end_rec)
    mirror.write(end_rec)
    log.close()

    print(f"\n\nSession complete.")
    print(f"  Passes     : {snap['pass']}")
    print(f"  Samples    : {snap['samples']:,}")
    print(f"  Anomalies  : {snap['anomalies']:,}")
    print(f"  Chain tip  : {chain.tip}")
    print(f"  Log        : {gz_path}")
    print(f"  Chain      : {chain_path}")


if __name__ == "__main__":
    main()