# test_motion_smoothing.py - the arm's shake fixes (2026-11-20):
#   python3 tests/test_motion_smoothing.py
# A) a move group goes out as ONE SSC-32U block with a shared move duration
#    T, so every servo finishes together (the old per-servo S rates finished
#    at staggered times - the shake on descents and lifts);
# B) moveXY descends in two stages - the bulk at the move's speed, the last
#    stretch at CST_SPEED_SLOW (the soft landing).

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import LEGO_Chess_rpd as RD                      # noqa: E402
import lib_al5_2D_IK as IK                       # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


class FakeSerial:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data.decode())


# A) the T block --------------------------------------------------------------
sleeps = []
orig = (dict(IK._last_pulses), IK.time)
try:
    IK.time = types.SimpleNamespace(sleep=lambda s: sleeps.append(s),
                                    time=lambda: 0.0)
    for ch in range(6):
        IK._last_pulses[ch] = IK.getPulseFromAngle(40)
    ser = FakeSerial()
    # every joint moves: S, E, W, Z, G, WR  (speeds indexed BY CHANNEL:
    # base, shoulder, elbow, wrist, gripper, wrist rotation)
    motors = (60.0, 120.0, 75.0, 100.0, 90.0, 30.0)
    speeds = (300, 150, 150, 250, 250, 250)
    IK.al5_moveMotors(motors, speeds, ser)
    blocks = ser.writes
    check(len(blocks) == 2, "expected arm+base blocks, got %r" % blocks)
    arm = blocks[0] if blocks else ""
    check("T" in arm and "\r" in arm, "arm block has no shared duration T")
    check("S" not in arm, "arm block still carries per-servo S rates")
    for ch in (1, 2, 3, 4, 5):
        check("#%d P" % ch in arm, "arm block missing channel %d" % ch)
    # T must equal the slowest joint's own pace: max(delta/speed) in ms,
    # with the wrist's bumped tracking rate (>= 2x the arm joints).  The
    # pulses come from the block itself so the test verifies the principle,
    # not the IK's angle mapping.
    speeds_by_ch = {1: 150, 2: 150, 3: 300, 4: 250, 5: 250}
    base = IK.getPulseFromAngle(40)
    worst = 0.0
    for ch, sp in speeds_by_ch.items():
        tok = "#%d P" % ch
        p = int(arm.split(tok, 1)[1].split()[0])
        worst = max(worst, abs(p - base) / sp)
    check("T%d" % int(worst * 1000) in arm,
          "T is %r, want %d" % (arm, int(worst * 1000)))
    check(sleeps and max(sleeps) >= IK.SETTLE_S,
          "no settle dwell after the move: %r" % sleeps)
finally:
    IK._last_pulses = orig[0]
    IK.time = orig[1]

# B) the two-stage descent ----------------------------------------------------
calls = []
orig_move = RD.movemotors
orig_t = RD.targetXYZGWAWR
try:
    RD.movemotors = lambda **kw: calls.append(
        (RD.targetXYZGWAWR[1], kw.get("speeds")))
    # a pick-style descent of 1.24 in: two stages
    RD.targetXYZGWAWR = (10.0, 2.5, 98.5, 85, -90, 90)
    RD.moveXY(1.26, speeds=RD.SPEED_PICK)
    check(len(calls) == 2, "descent was not two-stage: %r" % calls)
    if len(calls) == 2:
        check(abs(calls[0][0] - 1.76) < 0.01,
              "stage 1 did not stop at crawl height: %r" % calls)
        check(abs(calls[1][0] - 1.26) < 0.01,
              "stage 2 did not land on target: %r" % calls)
        check(calls[0][1] == RD.SPEED_PICK,
              "stage 1 lost the move speed: %r" % calls)
        check(calls[1][1] == tuple(RD.CST_SPEED_SLOW for _ in
                                   RD.speed_SEWBZWrG),
              "stage 2 is not the slow crawl: %r" % calls)
    # a lift, and a small descent: single stage
    calls[:] = []
    RD.targetXYZGWAWR = (6.9, 0.4, 98.5, 85, -90, 90)
    RD.moveXY(3.6, speeds=RD.SPEED_TRAVEL)
    check(len(calls) == 1, "lift must not two-stage: %r" % calls)
    calls[:] = []
    RD.targetXYZGWAWR = (10.0, 2.5, 98.5, 85, -90, 90)
    RD.moveXY(2.2, speeds=RD.SPEED_PICK)
    check(len(calls) == 1, "a 0.3in descent must not two-stage: %r" % calls)
finally:
    RD.movemotors = orig_move
    RD.targetXYZGWAWR = orig_t

print("motion FAILS:" if fails else
      "PASS: motion smoothing (shared T duration, settle, two-stage descent)")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
