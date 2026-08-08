#!/usr/bin/env python3
"""
fs5000_serial.py — Bosean FS-5000 Serial-Only Forensic Logger (Windows/Linux)
No pyaudio dependency. Outputs serial_STAMP.jsonl.gz compatible with
geiger_sdr_correlator.py
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import threading
import time
from collections import deque
import queue
import threading

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("ERROR: pip install pyserial")
    sys.exit(1)

CH340_VID               = 0x1A86
CH340_PID               = 0x7523
BAUD                    = 115200
DEFAULT_SPIKE_THRESHOLD = 0.01
DANGEROUS_THRESHOLD     = 0.20

STAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

_LIVE_RE = re.compile(
    rb'DR:(?P<dr>[\d.]+)uSv/h;'
    rb'D:(?P<dose>[\d. ]+)uSv;'
    rb'(?:CPS:(?P<cps>[\d]+);)?'
    rb'CPM:(?P<cpm>[\d]+)'
)

def _cs(data: bytes) -> int:
    return sum(data) % 256

def make_packet(payload: bytes) -> bytes:
    hdr  = bytes([0xAA, len(payload) + 3])
    body = hdr + payload
    return body + bytes([_cs(body)]) + bytes([0x55])

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
        self.session_mono_ns  = self._mono_epoch
        self.session_wall_utc = datetime.datetime.fromtimestamp(
            self._wall_epoch / 1e9, tz=datetime.timezone.utc
        ).isoformat()
        whole_s = self._wall_epoch // 1_000_000_000
        self.session_wall_ns_remainder = self._wall_epoch - whole_s * 1_000_000_000

    def now(self) -> tuple:
        mono_now = time.perf_counter_ns()
        delta    = mono_now - self._mono_epoch
        return self._wall_epoch + delta, delta

    def format_wall_ns(self, wall_ns: int) -> str:
        whole_s = wall_ns // 1_000_000_000
        frac_ns = wall_ns  % 1_000_000_000
        base    = datetime.datetime.fromtimestamp(
            whole_s, tz=datetime.timezone.utc
        ).strftime('%Y-%m-%dT%H:%M:%S')
        return f"{base}.{frac_ns:09d}Z"

class GzipLog:
    def __init__(self, path: str, header: dict):
        self.path   = path
        self._q     = deque()
        self._event = threading.Event()
        self._stop  = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f'GzipLog:{os.path.basename(path)}'
        )
        first = json.dumps(header, separators=(',', ':')) + '\n'
        with gzip.open(self.path, 'ab', compresslevel=6) as gz:
            gz.write(first.encode())
        self._thread.start()

    def write(self, obj: dict):
        self._q.append(obj)
        self._event.set()

    def close(self):
        self._stop.set()
        self._event.set()
        self._thread.join(timeout=8)

    def _run(self):
        while not self._stop.is_set():
            self._event.wait()
            self._event.clear()
            self._drain()
        self._drain()

    def _drain(self):
        if not self._q:
            return
        lines = []
        while self._q:
            lines.append(json.dumps(self._q.popleft(), separators=(',', ':')))
        blob = ('\n'.join(lines) + '\n').encode()
        with gzip.open(self.path, 'ab', compresslevel=6) as gz:
            gz.write(blob)

class LiveJSONL:
    """Writes plain uncompressed JSONL alongside the gz for live tailing."""
    def __init__(self, path: str):
        self.path  = path
        self._lock = threading.Lock()
        with open(path, 'w', encoding='utf-8') as f:
            f.write('')

    def write(self, obj: dict):
        line = json.dumps(obj, separators=(',', ':')) + '\n'
        with self._lock:
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(line)
                f.flush()


def find_serial_port(forced=None) -> str:
    available = list(serial.tools.list_ports.comports())
    print("\nAvailable serial ports:")
    if not available:
        print("  (none detected)")
    for p in available:
        marker = " <- CH340 FS-5000" if (p.vid == CH340_VID and
                                          p.pid == CH340_PID) else ""
        print(f"  {p.device:<14} {p.description}{marker}")
    print()

    if forced:
        return forced

    for p in available:
        if p.vid == CH340_VID and p.pid == CH340_PID:
            print(f"[AUTO] FS-5000 detected on {p.device}")
            return p.device

    print("ERROR: CH340 not detected automatically.")
    choice = input("  Enter COM port manually (e.g. COM10) or press Enter to exit: ").strip()
    if choice:
        return choice
    sys.exit(1)

def run_serial(port_name, clock, log, live_jsonl, spike_threshold, quiet, stop_event):
    buf      = bytearray()
    last_dr  = None
    last_cpm = None
    seq      = 0
    try:
        with serial.Serial(port_name, BAUD, timeout=0.05) as port:
            port.reset_input_buffer()
            port.write(make_packet(bytes([0x0e, 0x00])))
            time.sleep(0.5)
            port.reset_input_buffer()
            port.write(make_packet(bytes([0x0e, 0x01])))
            time.sleep(0.3)
            ack = port.read(port.in_waiting or 1)
            if ack:
                if ack[0] == 0xAA and len(ack) > 1:
                    skip = 2 + ack[1]
                    if len(ack) > skip:
                        buf.extend(ack[skip:])
                else:
                    buf.extend(ack)

            while not stop_event.is_set():
                chunk = port.read(4096)
                if not chunk:
                    continue
                buf.extend(chunk)
                matches = list(_LIVE_RE.finditer(buf))
                if not matches:
                    if len(buf) > 512:
                        buf = buf[-512:]
                    continue
                for m in matches:
                    try:
                        dr      = float(m.group('dr'))
                        cpm     = int(m.group('cpm'))
                        cps_raw = m.group('cps')
                        cps     = int(cps_raw) if cps_raw is not None else (cpm // 60)
                        dose    = float(m.group('dose').strip())
                    except (ValueError, AttributeError):
                        continue
                    wall_ns, mono_ns = clock.now()
                    seq += 1
                    rec = {
                        "seq":      seq,
                        "wall_ns":  wall_ns,
                        "wall_iso": clock.format_wall_ns(wall_ns),
                        "mono_ns":  mono_ns,
                        "dr":       dr,
                        "cpm":      cpm,
                        "cps":      cps,
                        "dose":     dose,
                    }
                    log.write(rec)
                    live_jsonl.write(rec)
                    if not quiet and (dr != last_dr or cpm != last_cpm):
                        last_dr  = dr
                        last_cpm = cpm
                        if dr >= DANGEROUS_THRESHOLD:
                            flag = '!!! DANGEROUS !!!'
                        elif dr >= spike_threshold:
                            flag = '*** SPIKE ***'
                        else:
                            flag = ''
                        bar = chr(0x2588) * min(30, int(dr * 100))
                        ts  = datetime.datetime.now(
                            tz=datetime.timezone.utc).strftime('%H:%M:%S')
                        print(
                            f"\r  {ts}  {dr:7.4f} uSv/h  "
                            f"CPS={cps:>4}  CPM={cpm:>5}  "
                            f"{bar:<30}  {flag}   ",
                            end='', flush=True
                        )
                buf = buf[max(0, matches[-1].end() - 512):]
    except Exception as e:
        if not stop_event.is_set():
            print(f"\n[serial] error: {type(e).__name__}: {e}")
    finally:
        try:
            with serial.Serial(port_name, BAUD, timeout=2) as p:
                p.write(make_packet(bytes([0x0e, 0x00])))
                time.sleep(0.2)
        except Exception:
            pass

def main():
    ap = argparse.ArgumentParser(
        description="FS-5000 Serial Forensic Logger (no audio)")
    ap.add_argument('--port',
                    help='Serial port e.g. COM4 (default: auto-detect CH340)')
    ap.add_argument('--out', default='J:\\True-Sentinel', metavar='DIR')
    ap.add_argument('--spike-threshold', type=float,
                    default=DEFAULT_SPIKE_THRESHOLD)
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    clock     = ClockAnchor()
    port_name = find_serial_port(args.port)

    # ── Annotation prompt before start ────────────────────────────────────
    print(f"\n{'─'*58}")
    print("  SESSION ANNOTATION")
    print("  Enter location and notes, or press Enter to skip.")
    print(f"{'─'*58}")
    location = input("  Location : ").strip()
    notes    = input("  Notes    : ").strip()
    print(f"{'─'*58}")
    print("  Press Enter at any time to PAUSE and update annotation.")
    print("  Press Ctrl+C to STOP logging entirely.")
    print(f"{'─'*58}")
    input("  Press Enter to START logging...")

    # Shared state for pause/annotation
    annotation_holder = {
        'location': location,
        'notes':    notes,
    }
    pause_event  = threading.Event()
    resume_event = threading.Event()
    stdin_q      = queue.Queue()

    def _stdin_reader():
        """Daemon thread: puts each Enter press on stdin_q."""
        while True:
            try:
                line = sys.stdin.readline()
                stdin_q.put(line)
            except Exception:
                break

    stdin_thread = threading.Thread(target=_stdin_reader, daemon=True,
                                    name='StdinReader')
    stdin_thread.start()

    forensic_header = {
        "type":                      "forensic_session_header",
        "record":                    0,
        "session_wall_utc":          clock.session_wall_utc,
        "session_wall_ns":           clock.session_wall_ns,
        "session_wall_ns_remainder": clock.session_wall_ns_remainder,
        "session_mono_epoch_ns":     clock.session_mono_ns,
        "timing_note":
            "wall_ns = session_wall_ns + mono_ns. "
            "mono_ns from perf_counter_ns - monotonic, never jumps.",
        "instrument":               "Bosean FS-5000",
        "serial_port":              port_name,
        "spike_threshold_usvh":     args.spike_threshold,
        "dangerous_threshold_usvh": DANGEROUS_THRESHOLD,
        "serial_log":               f"serial_{STAMP}.jsonl.gz",
    }

    serial_log_path = os.path.join(out_dir, f"serial_{STAMP}.jsonl.gz")
    live_jsonl_path = os.path.join(out_dir, f"geiger_live.jsonl")
    serial_log      = GzipLog(serial_log_path, forensic_header)
    live_jsonl      = LiveJSONL(live_jsonl_path)

    # Write initial annotation record
    ann_rec = {
        "type":     "annotation",
        "wall_iso": clock.format_wall_ns(clock.session_wall_ns),
        "wall_ns":  clock.session_wall_ns,
        "location": annotation_holder['location'],
        "notes":    annotation_holder['notes'],
        "event":    "SESSION_START",
    }
    serial_log.write(ann_rec)
    live_jsonl.write(ann_rec)

    print(f"\n{'='*58}")
    print(f"  FS-5000 SERIAL FORENSIC LOGGER")
    print(f"{'='*58}")
    print(f"  Session start : {clock.session_wall_utc}")
    print(f"  Serial port   : {port_name}")
    print(f"  Spike thresh  : {args.spike_threshold} uSv/h")
    print(f"  Output        : serial_{STAMP}.jsonl.gz")
    print(f"  Out dir       : {out_dir}")
    print(f"  Ctrl+C to stop")
    print(f"{'='*58}\n")

    stop_event    = threading.Event()
    serial_thread = threading.Thread(
        target=run_serial,
        args=(port_name, clock, serial_log, live_jsonl,
              args.spike_threshold, args.quiet, stop_event),
        daemon=True,
        name='SerialStream',
    )
    serial_thread.start()

    try:
        while serial_thread.is_alive():
            time.sleep(0.3)
            # Check for Enter press (pause request)
            try:
                stdin_q.get_nowait()
            except queue.Empty:
                continue

            # ── PAUSE ─────────────────────────────────────────────────────
            print(f"\n\n{'─'*58}")
            print("  PAUSED  (logging continues in background)")
            print(f"{'─'*58}")
            print(f"  Current location : {annotation_holder['location'] or '(none)'}")
            print(f"  Current notes    : {annotation_holder['notes'] or '(none)'}")
            print("  Press Enter to keep current annotation,")
            print("  or type new value and press Enter.")
            print(f"{'─'*58}")

            new_loc = input(f"  Location [{annotation_holder['location']}]: ").strip()
            new_notes = input(f"  Notes    [{annotation_holder['notes']}]: ").strip()

            if new_loc:
                annotation_holder['location'] = new_loc
            if new_notes:
                annotation_holder['notes'] = new_notes

            # Log the annotation change
            wall_ns, _ = clock.now()
            ann_rec = {
                "type":     "annotation",
                "wall_iso": clock.format_wall_ns(wall_ns),
                "wall_ns":  wall_ns,
                "location": annotation_holder['location'],
                "notes":    annotation_holder['notes'],
                "event":    "ANNOTATION_UPDATE",
            }
            serial_log.write(ann_rec)
            live_jsonl.write(ann_rec)

            print(f"{'─'*58}")
            print("  Resuming display...")
            print(f"{'─'*58}\n")

    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        serial_thread.join(timeout=5)
        wall_ns, mono_ns = clock.now()
        serial_log.write({
            "type":     "session_end",
            "wall_ns":  wall_ns,
            "wall_iso": clock.format_wall_ns(wall_ns),
            "mono_ns":  mono_ns,
        })
        serial_log.close()
        print(f"\n\nSession complete.")
        print(f"  Duration    : {mono_ns/1e9:.1f}s")
        print(f"  Serial log  : {serial_log_path}")

if __name__ == '__main__':
    main()