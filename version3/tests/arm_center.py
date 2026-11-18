# tests/arm_center.py — servo horn neutral check, PO-offset calibration,
# and a per-joint sweep test.
#
# Energises every servo at its P1500 neutral pulse ONE JOINT AT A TIME
# (lightest joints first, big gravity-loaded joints slowest — simultaneous
# energising browned out the PSU in the field), then walks you through a
# per-joint check.  Corrections are entered as degrees and applied as live
# "P1500 PO<offset>" commands so the servo actually moves to the corrected
# neutral (a bare PO only takes effect on the next position command).
#
# At the end it prints the PO lines to paste into LEGO_Chess_rpd.init()
# (SSC-32U forgets PO at power-off; init() must re-send them every start),
# and optionally runs a SWEEP TEST that flexes each channel +/-150us to
# prove every servo still responds — useful after any suspected overcurrent
# event.
#
# Usage:  python tests/arm_center.py
# Safety: clear the arm's swing space; keep a hand near the power plug.
#         Ctrl-C releases all servos immediately.

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import LEGO_Chess_rpd as RD  # noqa: E402

# channel, name, what neutral should look like on this arm
JOINTS = [
    (0, "base",       "arm points straight forward along the centre of the board"),
    (1, "shoulder",   "upper arm roughly vertical (check with a phone level)"),
    (2, "elbow",      "forearm roughly horizontal"),
    (3, "wrist",      "gripper stem vertical"),
    (4, "gripper",    "jaws gently closed, not straining"),
    (5, "wrist rot",  "jaws aligned with the forearm plane"),
]

# energise order: light/no-load joints first, gravity-loaded joints last &
# slowest (pulse speed S in us/s, then wait long enough for worst-case travel)
NEUTRAL_SEQ = [
    (4, 500, 2.5),
    (5, 500, 2.5),
    (3, 400, 3.5),
    (2, 250, 7.5),   # elbow: full-fold -> neutral at slow speed
    (1, 250, 7.5),   # shoulder: carries everything
    (0, 300, 7.0),   # base
]

US_PER_DEG = 2000.0 / 180.0   # pulse range / IK angle range


def sp_write(sp, s):
    try:
        sp.write(s.encode() if isinstance(s, str) else s)
        sp.flush()
        return True
    except Exception as e:
        print("Serial write failed:", e)
        print("(The SSC-32U may have brownout-reset mid-run: check PSU and")
        print(" wiring, re-plug if the port vanished, then rerun this tool.)")
        return False


def release_all(sp):
    for i in range(6):
        try:
            sp.write(("#%d P0\r" % i).encode())
        except Exception:
            pass


def sweep_test(sp):
    """Flex every channel +/-150us from neutral; report dead channels."""
    misses = []
    for ch, name, _desc in JOINTS:
        motion = ("jaw widens then narrows (it may never fully close — that"
                  " is the neutral pulse, not the close extreme)" if ch == 4
                  else "sweeps down then up slightly")
        print("\nwatch %s (channel #%d) — %s:" % (name, ch, motion))
        if not (sp_write(sp, "#%d P1350 S400\r" % ch) and
                (time.sleep(1.4) or True) and
                sp_write(sp, "#%d P1650 S400\r" % ch)):
            return False
        time.sleep(1.6)
        sp_write(sp, "#%d P1500 S400\r" % ch)
        time.sleep(1.0)
        ans = input("   did it move BOTH ways? [Y/n] ").strip().lower()
        if ans == "n":
            misses.append((ch, name))
    if misses:
        print("\nSUSPECT CHANNELS (no motion observed):",
              ", ".join("#%d %s" % m for m in misses))
        print("Check: horn/screw, servo wire, or servo itself (overcurrent")
        print("damage). Cross-test by swapping two identical servos' wires.")
    else:
        print("\nAll six joints responded. Servos and channels are healthy.")
    return True


def main():
    sp = RD._resolve_port()
    if sp is None:
        print("SSC-32U not found. Is the USB cable in and the board powered?")
        return 1

    try:
        input("Press Enter to energise — joints will move to neutral ONE AT "
              "A TIME (~30 s). Clear the swing space now.")
        print()
        if not sp_write(sp, "ES\r"):
            return 1
        for ch, speed, wait in NEUTRAL_SEQ:
            print("  neutral: channel #%d ..." % ch, flush=True)
            if not sp_write(sp, "#%d P1500 S%d\r" % (ch, speed)):
                return 1
            time.sleep(wait)

        print("\nArm now HOLDS its all-neutral pose. If a joint looks wrong,")
        print("correct it below (degrees, signed). Enter = looks fine.\n")

        pos = {}
        for ch, name, desc in JOINTS:
            print("[%s]  expect: %s" % (name, desc))
            while True:
                raw = input("   offset deg (blank=ok): ").strip()
                if raw in ("", "ok"):
                    break
                try:
                    deg = float(raw)
                except ValueError:
                    print("   enter a number like -3 or +1.5, or blank")
                    continue
                us = pos.get(ch, 0) + int(round(deg * US_PER_DEG))
                # PO ONLY takes effect with a position command: send both.
                if not sp_write(sp, "#%d P1500 PO%d T800 S400\r" % (ch, us)):
                    return 1
                pos[ch] = us
                time.sleep(1.5)
                print("   cumulative offset %+dus — re-check, correct again"
                      " if needed, or blank when right" % us)

        if pos:
            print("\nAdd these to LEGO_Chess_rpd.init() (uncomment + edit the"
                  " 'Servo pulse offsets' block):\n")
            for ch, us in sorted(pos.items()):
                print('    sp.write(("#%d PO%d\\r").encode())   # %s'
                      % (ch, us, [j[1] for j in JOINTS if j[0] == ch][0]))
            print("\n(PO values live until power-off; init() must re-send them"
                  " at every start — they apply to all later moves.)")
        else:
            print("\nNo offsets needed — horns are centred.")

        if input("\nRun per-joint SWEEP TEST (verifies every servo answers"
                 " after the overcurrent scare)? [Y/n] ").strip().lower() != "n":
            sweep_test(sp)
        return 0
    finally:
        print("\nReleasing all servos.")
        release_all(sp)
        sp.close()


if __name__ == "__main__":
    sys.exit(main())
