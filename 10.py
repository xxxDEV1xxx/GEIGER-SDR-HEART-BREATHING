#!/usr/bin/env python3
import serial, sys

PORT = "COM3"
ser  = serial.Serial(PORT, 115200, timeout=0.1)
out  = open(r"J:\True-Sentinel\mmwave\mmwave_raw.log", "wb")

sys.stderr.write(f"Writing to mmwave_raw.log\n")
sys.stderr.flush()

while True:
    data = ser.read(256)
    if data:
        out.write(data.hex().encode('ascii') + b'\n')
        out.flush()