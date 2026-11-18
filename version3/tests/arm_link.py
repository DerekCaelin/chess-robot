# arm_link.py — SSC-32U serial-link and power diagnostic.
#
# Run standalone (does NOT move any servos unless --motion is given):
#   python tests/arm_link.py            # enumerate ports, find the board,
#                                       # report link health + supply voltage
#   python tests/arm_link.py --motion   # gentle gripper-only motion test
#
# Decision tree it implements:
#   no FTDI device           -> USB cable / port / drivers
#   clean reply              -> link fully working
#   no reply, but --motion   -> WRITE-ONLY link (this board's known state):
#     commands execute, the reply path (PIC TX / FTDI RX) is dead. Harmless
#     for the game, which never reads back; useful info only.
#   no reply, no motion      -> servo VIN rail (PSU/wiring/terminal block) or
#     board not powered

import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serial
import serial.tools.list_ports

BAUDS = (9600, 115200, 57600, 38400, 19200, 14400)


def find_candidates():
    cands = []
    for p in serial.tools.list_ports.comports():
        is_ftdi = (p.vid, p.pid) == (0x0403, 0x6001)
        cands.append((p.device, is_ftdi))
    return cands


def probe(port, baud):
    try:
        sp = serial.Serial(port, baud, timeout=0.6)
    except serial.SerialException as e:
        return None, "open failed: %s" % e
    try:
        sp.reset_input_buffer()
        sp.write(b"#0?\r")
        resp = sp.read(12)
        sp.reset_input_buffer()
        sp.write(b"VB?\rVT?\r")
        resp2 = sp.read(24)
    finally:
        sp.close()
    return (resp, resp2), None


def classify(resp):
    digits = sum(c in b"0123456789" for c in resp[0])
    if digits >= 3:
        return "ALIVE"
    if len(resp[0]) >= 2:
        return "GARBLED"
    return "SILENT"


def main():
    motion = "--motion" in sys.argv
    cands = find_candidates()
    if not cands:
        print("No serial ports at all. Is the SSC-32U USB cable plugged in?")
        return 1
    print("Serial devices seen:")
    for dev, ft in cands:
        print("  %-14s %s" % (dev, "<- FTDI (SSC-32U)" if ft else ""))

    ports = [d for d, f in cands if f] or [d for d, _ in cands]
    best = None
    for port in ports:
        for baud in BAUDS:
            r, err = probe(port, baud)
            if err:
                print("%s @ %6d : %s" % (port, baud, err))
                continue
            st = classify(r)
            print("%s @ %6d : %-7s %r" % (port, baud, st, r[0]))
            if st == "ALIVE":
                best = (port, baud, r)
                break
        if best:
            break

    if best is None:
        print("\nNo position query was answered (see --motion below to test")
        print("blind command execution).")
        if not motion:
            print("NOTE: this project's board is known WRITE-ONLY: commands")
            print("execute but the reply path is dead. Run with --motion to")
            print("confirm; the robot code does not need replies.")
            return 2

    port, baud = (best[0], best[1]) if best else (ports[0], 9600)
    if best:
        print("\nLink ALIVE on %s @ %d baud; #0? -> %r" % (port, baud, best[2][0]))
        print("Voltage query (VB?/VT?) -> %r" % (best[2][1],))

    if motion:
        sp = serial.Serial(port, baud, timeout=0.6)
        sp.write(b"ES\r")
        time.sleep(0.2)
        print("Slowly opening gripper (#4 1077 -> 1400 @ S600)... watch #4:")
        sp.write(b"#4 P1077 S600\r")
        time.sleep(2.5)
        sp.write(b"#4?\r")
        pos = sp.read(8)
        print("  position now %r" % pos)
        sp.write(b"#4 P1400 S600\r")
        time.sleep(3.0)
        sp.write(b"#4?\r")
        pos2 = sp.read(8)
        print("  position now %r" % pos2)
        sp.write(b"#4 P1077 S600\r")
        time.sleep(2.5)
        sp.close()
        if not best:
            print("\nThe board never replied, so this was a BLIND test.")
        ans = input("Did the gripper visibly move/hold? [y/N] ").strip().lower()
        if ans == "y":
            print("VERDICT: commands execute — link is functional" +
                  ("" if best else " (WRITE-ONLY: replies broken, fine for play)."))
            return 0
        print("VERDICT: commands not executed — check servo VIN power at the")
        print("green terminal block (LED state under load), then cable/baud.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
