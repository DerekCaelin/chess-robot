# tests/test_arm_sag.py - the arm-sag compensation curve, the IK seam it is
# applied at, and the park/cancel motion order.
#   python tests/test_arm_sag.py
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import LEGO_Chess_rpd as RD  # noqa: E402
import arm_probe as ap  # noqa: E402


class FakeSp:
    def write(self, *a):
        pass

    def flush(self):
        pass


def test_sag_identity_when_empty():
    RD.set_sag([])
    assert RD.sag_pts == []
    assert RD.sag_comp(0.0) == 0.0 and RD.sag_comp(20.0) == 0.0
    assert RD.ik_Y(6.0, 4.0) == 4.0
    for h in (2.0, 4.0, 6.0):
        plain = (RD._work_cap() ** 2 - (h - RD.shoulder_height) ** 2) ** 0.5
        assert abs(RD.reach_at_height(h) - plain) < 1e-9


def test_sag_piecewise_linear_and_clamped():
    RD.set_sag([["d8", 0.2], ["d4", 0.5], ["h1", 0.9]])
    pts = sorted(RD.sag_pts)
    xs = [p[0] for p in pts]
    assert [p[1] for p in pts] == [0.2, 0.5, 0.9]
    # exact at the measured points, linear between, clamped outside
    for (x, y) in pts:
        assert abs(RD.sag_comp(x) - y) < 1e-9
    mid = 0.5 * (xs[0] + xs[1])
    assert abs(RD.sag_comp(mid) - 0.35) < 1e-9
    assert RD.sag_comp(xs[0] - 10.0) == 0.2
    assert RD.sag_comp(xs[-1] + 10.0) == 0.9
    assert RD.ik_Y(xs[0], 4.0) == 4.0 + 0.2
    RD.set_sag([])


def test_movemotors_applies_comp_at_the_seam():
    captured = []
    real_ik, real_move = RD.al5_2D_IK, RD.al5_moveMotors

    def fake_ik(t):
        captured.append(t)
        return (90.0, 90.0, 90.0, 90.0, 90.0, 90.0)

    RD.al5_2D_IK = fake_ik
    RD.al5_moveMotors = lambda *a, **k: None
    RD.sp = FakeSp()
    RD.set_sag([["d8", 0.4], ["h1", 0.4]])
    try:
        RD.targetXYZGWAWR = (6.0, 4.0, 90.0, 60, -90, 90)
        RD.movemotors()
        assert abs(captured[-1][1] - (4.0 + 0.4 - RD.shoulder_height)) < 1e-9
        RD.movemotors(compensate=False)   # park/rest: the droop pose itself
        assert abs(captured[-1][1] - (4.0 - RD.shoulder_height)) < 1e-9
        assert RD.pose_ok(6.0, 4.0, 90.0)
        # pose_ok must solve for the SAME compensated height movemotors uses
        assert abs(captured[-1][1] - (4.0 + 0.4 - RD.shoulder_height)) < 1e-9
    finally:
        RD.al5_2D_IK, RD.al5_moveMotors = real_ik, real_move
        RD.sp = None
        RD.set_sag([])


def test_planners_account_for_sag():
    RD.set_sag([["d8", 0.5], ["h1", 0.5]])
    cap = RD._work_cap()
    for h in (3.0, 5.0, 6.0):
        x = RD.reach_at_height(h)
        # at the returned reach the COMMANDED (compensated) height sits on
        # the annulus ceiling - that is what pose_ok will solve for
        ycmd = h + RD.sag_comp(x)
        r = (x * x + (ycmd - RD.shoulder_height) ** 2) ** 0.5
        assert abs(r - cap) < 1e-6, (h, x, r)
        # and the lift ceiling leaves room for the compensation on top
        want = RD.shoulder_height + (cap * cap - x * x) ** 0.5 - 0.5
        assert abs(RD.max_lift_height(x) - want) < 1e-6, (x, want)
    RD.set_sag([])


def test_park_releases_before_lifting():
    events = []
    real = (RD.movemotors, RD.set_gripper, RD.idleall)
    RD.movemotors = lambda **k: events.append(
        ("move", RD.targetXYZGWAWR[1], k.get("compensate", True)))
    RD.set_gripper = lambda w: events.append(("release", w))
    RD.idleall = lambda: events.append(("idle",))
    RD.sp = FakeSp()
    try:
        # holding a pawn: jaw width well above the open width
        RD.targetXYZGWAWR = (6.0, 4.0, 90.0, RD.grip_width("p"), -90, 90)
        RD.park()
        assert events[0][0] == "release", events
        moves = [i for i, e in enumerate(events) if e[0] == "move"]
        assert moves and moves[0] > 0, events          # lift AFTER release
        assert events[-2][0] == "move" and events[-2][2] is False, events
        assert events[-1][0] == "idle", events
        # jaws already open (end of a game move): no extra release delay
        events.clear()
        open_jaw = 42 + RD.extrawidth
        RD.targetXYZGWAWR = (6.0, 4.0, 90.0, open_jaw, -90, 90)
        RD.park()
        assert not any(e[0] == "release" for e in events), events
    finally:
        RD.movemotors, RD.set_gripper, RD.idleall = real
        RD.sp = None


def test_fit_height_reproduces_the_measurements():
    # "Match grip height": fitting (adj, slope) from grab samples must make
    # grip_y reproduce the measured wrist heights at every sample reach.
    xs = (6.3, 12.9)
    ys = (RD.grip_y("p", xs[0]) + 0.2, RD.grip_y("p", xs[1]) - 0.1)
    samples = [{"square": "a8", "X": xs[0], "Z": 132.0, "Y": ys[0]},
               {"square": "h1", "X": xs[1], "Z": 68.0, "Y": ys[1]}]
    fit = ap.fit_height(samples)
    assert fit is not None
    adj, slope = fit
    for x, y in zip(xs, ys):
        got = RD.pieceheights["p"] + adj + slope * x + RD.gripper_len
        assert abs(got - y) < 1e-9, (x, got, y)


def test_arm_joints_move_together_with_tracking_wrist():
    """The wrist is the perpendicular-compensation joint. The arm joints must
    move as ONE group (staging the shoulder after elbow+wrist tilted the held
    piece through the final approach - it dragged on the board as it swung
    back perpendicular) and the wrist must have the rate margin to track
    shoulder+elbow (2026-10-21)."""
    import lib_al5_2D_IK as IK
    writes = []
    real_sleep = IK.time.sleep

    class FakeSerial:
        def write(self, data):
            writes.append(data.decode())

        def flush(self):
            pass

    IK.time.sleep = lambda s: None
    IK._last_pulses.update({0: 1500, 1: 1500, 2: 1500, 3: 1500, 4: 1500,
                            5: 1500})
    try:
        IK.al5_moveMotors((90.0, 60.0, 100.0, 45.0, 90.0, 90.0),
                          (100, 150, 150, 150, 150, 150), FakeSerial(),
                          pair_elbow_wrist=True)
        arm = [w for w in writes if w.startswith(("#1 ", "#2 ", "#3 "))]
        assert len(arm) == 3, writes
        idx = [writes.index(w) for w in arm]
        assert max(idx) - min(idx) == 2, writes    # contiguous = one group
        wr = int([w for w in writes if w.startswith("#3 ")][0].split("S")[1])
        others = [int(w.split("S")[1]) for w in writes
                  if w.startswith(("#1 ", "#2 "))]
        assert wr >= 2 * max(others), (wr, others, writes)
    finally:
        IK.time.sleep = real_sleep


def test_pick_drop_release_at_grip_height_servo_only():
    """The drop releases at the PICK height (the piece's base is already on
    the board there) and grip/release are servo-only - no IK re-solve can
    twitch the arm at the unclamp instant (2026-10-21)."""
    events = []
    real = (RD.moveXY, RD.set_gripper, RD.openjaws, RD.closejaws)
    RD.moveXY = lambda y, speeds=None: events.append(("descend", y))
    RD.set_gripper = lambda w: events.append(("jaw", w))
    RD.openjaws = lambda: events.append(("openjaws",))
    RD.closejaws = lambda p: events.append(("closejaws",))
    RD.targetXYZGWAWR = (6.0, 4.0, 90.0, RD.grip_width("p"), -90, 90)
    try:
        RD.droppiece("p", 6.0)
        assert events[0] == ("descend", RD.grip_y("p", 6.0)), events
        assert events[1] == ("jaw", RD.grip_open_width()), events
        events.clear()
        RD.pickuppiece("p", 6.0)
        assert events[0] == ("descend", RD.grip_y("p", 6.0)), events
        assert events[1] == ("jaw", RD.grip_width("p")), events
        assert not any(e[0] in ("openjaws", "closejaws") for e in events), \
            events
    finally:
        RD.moveXY, RD.set_gripper, RD.openjaws, RD.closejaws = real


def test_set_gripper_waits_for_the_travel():
    """The grip must FINISH before the arm lifts: a full open->grip sweep is
    ~2.6 s at 250 us/s, so the wait has to follow the servo travel. The old
    flat 1.3 s let the lift start with the jaws still closing (2026-10-21)."""
    import time as _time
    import lib_al5_2D_IK as IK
    sleeps = []
    real_sleep = _time.sleep
    real_sp = RD.sp
    prev = IK._last_pulses.get(4)
    IK._last_pulses[4] = 1500                      # somewhere mid-travel
    _time.sleep = lambda s: sleeps.append(s)
    RD.sp = FakeSp()
    try:
        RD.set_gripper(RD.grip_width("p"))
        target = IK.getPulseFromAngle(RD.grip_width("p"))
        travel = abs(target - 1500) / float(RD.CST_SPEED_DEFAULT)
        assert sleeps and sleeps[-1] >= travel, (sleeps, travel)
    finally:
        _time.sleep = real_sleep
        RD.sp = real_sp
        if prev is None:
            IK._last_pulses.pop(4, None)
        else:
            IK._last_pulses[4] = prev


def test_cruise_runs_at_travel_speed():
    """Cruise moves run at the +50% travel profile (2026-10-22): the movearm
    steps, the lift-to-cruise and the park folds. The DESCENT onto a piece
    keeps the pick profile, and the park's final descent onto the droop keeps
    its own speed."""
    captured = []
    real = (RD.movemotors, RD.set_gripper, RD.idleall, RD.sp)
    RD.movemotors = lambda **k: captured.append(
        (k.get("speeds"), k.get("compensate", True)))
    RD.set_gripper = lambda w: None
    RD.idleall = lambda: None
    RD.sp = FakeSp()
    try:
        RD.targetXYZGWAWR = (6.0, 4.0, 90.0, 60, -90, 90)
        RD.movearm(6.0, 90.0)
        assert captured and all(s == RD.SPEED_TRAVEL for s, _ in captured), \
            captured

        captured.clear()
        RD._lift_clear(6.0, 90.0, 4.0)         # lift = cruise, not pick
        assert captured and all(s == RD.SPEED_TRAVEL for s, _ in captured), \
            captured

        captured.clear()
        RD.moveXY(5.0)                          # the pick/drop DESCENT
        assert captured == [(RD.SPEED_PICK, True)], captured

        captured.clear()
        RD.targetXYZGWAWR = (6.0, 4.0, 90.0, RD.grip_open_width(), -90, 90)
        RD.park()
        assert [s for s, _ in captured][:3] == [RD.SPEED_TRAVEL] * 3, captured
        assert captured[3][0] != RD.SPEED_TRAVEL and captured[3][1] is False, \
            captured
    finally:
        RD.movemotors, RD.set_gripper, RD.idleall, RD.sp = real


def main():
    test_sag_identity_when_empty()
    test_sag_piecewise_linear_and_clamped()
    test_movemotors_applies_comp_at_the_seam()
    test_planners_account_for_sag()
    test_park_releases_before_lifting()
    test_fit_height_reproduces_the_measurements()
    test_arm_joints_move_together_with_tracking_wrist()
    test_pick_drop_release_at_grip_height_servo_only()
    test_set_gripper_waits_for_the_travel()
    test_cruise_runs_at_travel_speed()
    print("PASS: sag curve + IK seam + park/pick/drop order + cruise speeds")


if __name__ == "__main__":
    main()
