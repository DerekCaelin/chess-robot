# test_capture_drop.py - the capture box drop point (2026-11-18): a box
# hanging off the bottom of the board replaces the graveyard grid.
#   python3 tests/test_capture_drop.py
# Checks the arm.capture_drop setting round-trips into the arm module, and
# that takepiece drops the captured piece in the box (move above it, open
# the gripper) when it is set - and on the graveyard grid when it is not.

import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import LEGO_Chess_rpd as RD                      # noqa: E402
from chessapp import settings                    # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


# 1. the default config carries the key (the file other settings live in)
d = json.load(open(os.path.join(ROOT, "config", "defaults.json")))
check("capture_drop" in d.get("arm", {}),
      "config/defaults.json lacks arm.capture_drop")

# 2. pose parsing / validation
prev_drop = RD.capture_drop
RD.capture_drop = {"reach": 10.0, "turn": 98.5, "height": 3.0}
check(RD._capture_drop_pose() == (10.0, 3.0, 98.5),
      "valid capture_drop pose not parsed")
RD.capture_drop = {"reach": 99.0, "turn": 98.5, "height": 3.0}
check(RD._capture_drop_pose() is None,
      "out-of-workspace capture_drop pose not rejected")
RD.capture_drop = {"reach": 10.0}
check(RD._capture_drop_pose() is None, "incomplete capture_drop not rejected")
RD.capture_drop = None
check(RD._capture_drop_pose() is None,
      "capture_drop=None must mean the graveyard grid")

# 3. takepiece routes the captured piece to the box when configured, and
#    back onto the graveyard grid when it is not (arm calls stubbed out)
calls = []
orig = dict((n, getattr(RD, n)) for n in
            ("movearm", "moveXY", "pickuppiece", "set_gripper", "droppiece",
             "next_grave_pose", "time"))
try:
    RD.movearm = lambda X, Z, edge_square=None: calls.append(
        ("movearm", round(X, 1), round(Z, 1)))
    RD.moveXY = lambda Y, speeds=None: calls.append(("moveXY", round(Y, 1)))
    RD.pickuppiece = lambda p, X: calls.append(("pickuppiece", p))
    RD.set_gripper = lambda w: calls.append(("set_gripper", round(w, 1)))
    RD.droppiece = lambda *a, **k: calls.append(("droppiece",) + a)
    RD.next_grave_pose = (lambda: calls.append(("next_grave_pose",))
                          or (8.0, 40.0))
    RD.time = types.SimpleNamespace(time=lambda: 0.0, sleep=lambda s: None)

    RD.capture_drop = {"reach": 10.0, "turn": 98.5, "height": 3.0}
    RD.takepiece(5.0, 30.0, "p")
    names = [c[0] for c in calls]
    check(names == ["movearm", "pickuppiece", "movearm", "moveXY",
                    "set_gripper"], "box path wrong: %s" % names)
    check(("movearm", 10.0, 98.5) in calls, "box path missed the spot")
    check(("moveXY", 3.0) in calls, "box path missed the stored height")
    check(RD.last_grave_slot() is None,
          "box path must not claim a graveyard slot")

    calls[:] = []
    RD._last_grave_slot = None
    RD.capture_drop = None
    RD.takepiece(5.0, 30.0, "p")
    names = [c[0] for c in calls]
    check("next_grave_pose" in names and "droppiece" in names,
          "graveyard fallback broken: %s" % names)
    check("set_gripper" not in names,
          "graveyard fallback must use droppiece, not a box release")
finally:
    for n, fn in orig.items():
        setattr(RD, n, fn)
    RD.capture_drop = prev_drop

# 4. settings.apply() pushes the setting into the arm module
prev_drop = RD.capture_drop
real_get = settings.get
try:
    settings.get = (lambda p, fb=None: {"reach": 11.0, "turn": 88.0,
                                        "height": 4.0}
                    if p == "arm.capture_drop" else real_get(p, fb))
    settings.apply()
    check(RD.capture_drop == {"reach": 11.0, "turn": 88.0, "height": 4.0},
          "settings.apply() did not push capture_drop to the arm module")
    settings.get = (lambda p, fb=None: None
                    if p == "arm.capture_drop" else real_get(p, fb))
    settings.apply()
    check(RD.capture_drop is None,
          "settings.apply() did not clear capture_drop")
finally:
    settings.get = real_get
    RD.capture_drop = prev_drop

print("capture-drop FAILS:" if fails else
      "PASS: capture drop (setting, box routing, graveyard fallback)")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
