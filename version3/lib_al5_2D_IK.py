# 2017-07-12 by scharette
# Basic 2D IK example of AL5 + Python (library)
#
# Link lengths now come from CBstate.AL5D_A / AL5D_B (pivot-to-pivot,
# measured at the bench) so calibration has ONE source of truth.

from math import sqrt, atan, acos, fabs
import time

import CBstate

A = CBstate.AL5D_A  # shoulder -> elbow (in)
B = CBstate.AL5D_B  # elbow -> wrist (in)

# Constants
rtod = 57.295779  # Radians to degrees constant
SETTLE_S = 0.15   # wobble-decay dwell after every move group (2026-11-20)
PULSE_UNKNOWN = 400   # assumed travel (us) for a joint whose position is
                      # unknown - the servos droop when idleall() cuts torque,
                      # so a stale _last_pulses entry must never shorten T
                      # (the wake used to whip to its pose, 2026-11-20)
CST_ANGLE_MIN = 0
CST_ANGLE_MAX = 180
CST_RC_MIN = 500
CST_RC_MAX = 2500


def ard_constrain(value, min, max):
    if (value < min):
        value = min
    if (value > max):
        value = max
    return (value)


def ard_map(value, min1, max1, min2, max2):
    value = ((value - min1) * (max2 - min2) / (max1 - min1) + min2);
    return (value)


def getPulseFromAngle(angle):
    if angle < CST_ANGLE_MIN or angle > CST_ANGLE_MAX:
        print("WARNING: IK angle %.1f outside 0..180 - clamped; "
              "target likely out of reach or wrist geometry wrong" % angle)
    angle = ard_constrain(angle, CST_ANGLE_MIN, CST_ANGLE_MAX)
    pulse = ard_map(angle, CST_ANGLE_MIN, CST_ANGLE_MAX, CST_RC_MIN, CST_RC_MAX)
    return (int(pulse));


#def al5_2D_IK(targetX, targetY, targetZ, targetGrip, targetWAgl, targetWRot):
def al5_2D_IK(targetXYZGWAWR):
    # Initialize variables
    x = targetXYZGWAWR[0]
    y = targetXYZGWAWR[1]
    z = targetXYZGWAWR[2]
    g = targetXYZGWAWR[3]
    wa = targetXYZGWAWR[4]
    wr = targetXYZGWAWR[5]
    Elbow = 0
    Shoulder = 0
    Wrist = 0

    # Get distance
    floatM = sqrt((y * y) + (x * x))
    #	print("floatM        = " + str(floatM))

    # Check X position for error
    if (floatM <= 0):
        return 1

    # Out of the annulus |A-B| .. A+B: acos below would raise ValueError and
    # crash the caller — report as error code 4 instead.
    if floatM >= (A + B) or floatM <= abs(A - B):
        print("IK: target radius %.2f out of reach (max %.2f)" % (floatM, A + B))
        return 4

    # Get first angle (radians)
    floatA1 = atan(y / x)
    #	print("floatA1       = " + str(floatA1))
    #	print("x             = " + str(x))

    # Check X position for error
    if (x <= 0):
        return 2

    # Get 2nd angle (radians)
    floatA2 = acos((A * A - B * B + floatM * floatM) / ((A * 2) * floatM))
    #	print("floatA2       = " + str(floatA2))

    # Calculate elbow angle (radians)
    floatElbow = acos((A * A + B * B - floatM * floatM) / ((A * 2) * B))
    #	print("floatElbow    = " + str(floatElbow))

    # Calculate shoulder angle (radians)
    floatShoulder = floatA1 + floatA2
    #	print("floatShoulder = " + str(floatShoulder))

    # Obtain angles for shoulder / elbow
    Elbow = floatElbow * rtod
    #	print("Elbow         = " + str(floatA2))
    Shoulder = floatShoulder * rtod
    #	print("Shoulder      = " + str(Shoulder))

    # Check elbow/shoulder angle for error
    if ((Elbow <= 0) or (Shoulder <= 0)):
        return 3
    Wrist = fabs(wa - Elbow - Shoulder) - 90

    # Return the new values
    motors_SEWBZWrG = (Shoulder, Elbow, Wrist, z, g, wr)

    # <<< debug <<<
    #print("SHOULDER\tELBOW   \tWRIST-A \tBASE-ROT\tGRIPPER \tWRIST-R \t")
    #print(str(Shoulder) + "\t" + str((180 - Elbow)) + "\t" + str((180 - Wrist)) + "\t" + str(z) + "\t" + str(g) + "\t" + str(wr) + "\t")
    #print(str(getPulseFromAngle(Shoulder)) + "\t" + str(getPulseFromAngle(180 - Elbow)) + "\t" + str(getPulseFromAngle(180 - Wrist)) + "\t" + str(getPulseFromAngle(z)) + "\t" + str(getPulseFromAngle(g)) + "\t" + str(getPulseFromAngle(wr)) + "\t")
    # >>> debug >>>

    return (motors_SEWBZWrG)


def _wait_for_servo(current_pulse, target_pulse, speed):
    """Calculate and sleep for the time needed for one servo to complete its move."""
    distance = abs(target_pulse - current_pulse)
    if speed > 0 and distance > 0:
        seconds = (distance / speed) + 0.3  # travel time + small margin
    else:
        seconds = 0.3
    time.sleep(seconds)


# Track last known pulse positions so we can calculate travel time.
# Initialised to the collapsed idle pulses sent by LEGO_Chess_rpd.init().
def invalidate_pulses():
    """Forget where the servos are - after idleall() cuts their torque they
    droop, and the next move must be paced from PULSE_UNKNOWN instead of a
    stale estimate (2026-11-20: the wake-from-droop used to whip)."""
    _last_pulses.clear()


_last_pulses = {
    0: 611,  # base
    1: 1755,  # shoulder (init sends #1 P1755)
    2: 1995,  # elbow
    3: 739,  # wrist
    4: 1077,  # gripper
    5: 1500,  # wrist rotation
}

# --- Servo polarity (bench-confirmed; the servo tool's 1500 = each centre) ---
#  base(#0):  higher pulse -> arm rotates CLOCKWISE = "left" (player view)
#  shoulder:  higher pulse -> arm swings AWAY from the user; pulse < 1500 = "up"
#  wrist:     higher pulse tips the jaws AWAY from the base (more forward).
# Reference pose that safely grips H1: base1255 shoulder965 elbow945 wrist800.
#
# Reach-scaled wrist lean (pick/drop descent only): at full extension the
# perpendicular tool pose leaves the jaws leaning into the base and short of the
# far pieces, so we tip the wrist forward - ramping its pulse from the IK's
# perpendicular value toward PICK_LEAN_MAX as BOTH shoulder & elbow drop below
# PICK_LEAN_START (i.e. the arm is genuinely extended). Near rows stay vertical.
PICK_LEAN_START = 1100    # min(shoulder,elbow) pulse where the lean begins
PICK_LEAN_FULL  = 945     # ...pulse where it is complete (your H1 grip)
PICK_LEAN_MAX   = 800     # wrist pulse at full extension (jaws away from base)


def al5_moveMotors(motors_SEWBZWrG, speed_SEWBZWrG, serial,
                   pair_elbow_wrist=True, pair_travel_joints=False,
                   pick_lean=False):
    """
	Move servos in safe groups to limit current spikes.
	Order: gripper -> wrist rotation -> [elbow/wrist/shoulder groups] -> base.

	pair_elbow_wrist=True (the slow pick/drop descents): elbow+wrist move
	together so the gripper stays vertical over a piece.
	Travel moves (pair_elbow_wrist=False): with pair_travel_joints=False each
	joint moves alone (safe for weak supplies - the 6V/3A brick browned out on
	even one paired pair at full extension, 2026-09-16); with it True,
	elbow+wrist+shoulder move together in one slow group - smoother and much
	faster, requires a 6V/4A+ supply.
	"""
    global _last_pulses

    # Convert angles to pulse widths
    pulseShoulder = getPulseFromAngle(motors_SEWBZWrG[0])
    pulseElbow = getPulseFromAngle(180 - motors_SEWBZWrG[1])
    # Wrist pulse: polarity is fixed (wrist_sign +1 = complement); wrist_offset
    # nudges the angle a few degrees for fine perpendicular-gripper tuning.
    wrist_sign = getattr(CBstate, "AL5D_wrist_sign", 1)
    wrist_offset = getattr(CBstate, "AL5D_wrist_offset", 0.0)
    wrist_adjusted = motors_SEWBZWrG[2] + wrist_offset
    pulseWrist = (getPulseFromAngle(180 - wrist_adjusted)
                  if wrist_sign > 0 else getPulseFromAngle(wrist_adjusted))
    pulseZ = getPulseFromAngle(motors_SEWBZWrG[3])
    pulseG = getPulseFromAngle(motors_SEWBZWrG[4])
    pulseWR = getPulseFromAngle(motors_SEWBZWrG[5])

    # (Pick lean removed: the wrist stays at its IK-perpendicular angle at every
    # reach.  The forward lean was only needed for the 1.5" board's full-extension
    # corners; the smaller board reaches every square without it.)

    speedZ = speed_SEWBZWrG[0]
    speedShoulder = speed_SEWBZWrG[1]
    speedElbow = speed_SEWBZWrG[2]
    speedWrist = speed_SEWBZWrG[3]
    speedG = speed_SEWBZWrG[4]
    speedWR = speed_SEWBZWrG[5]
    # The wrist is the PERPENDICULAR-COMPENSATION joint: keeping the gripper
    # vertical needs wrist_rate >= shoulder_rate + elbow_rate. Give it the
    # margin to actually track them instead of trailing mid-move - a trailing
    # wrist tilted the held piece through the descent and it dragged on the
    # board as it swung back perpendicular (2026-10-21).
    speedWrist = max(speedWrist, 2 * max(speedShoulder, speedElbow))

    def run_group(items, note=""):
        """Send every command of the group as ONE SSC-32U block with a shared
        move duration T, then wait for it - and a short settle - to finish
        (skip entirely if nothing in this group actually moves).

        T (2026-11-20) makes every servo finish TOGETHER at a rate
        proportional to its own pulse delta, so the IK's straight line stays
        straight and the perpendicular wrist tracks the shoulder through the
        whole move instead of arriving early and holding its final angle
        mid-move.  The old per-servo S rates finished at staggered times (on
        a park lift the shoulder ground on for 1.1 s after the wrist had
        stopped) - that end-stretch mismatch was the arm's shake on
        descents and lifts."""
        longest = 0.0
        moved = False
        block = []
        for channel, pulse, speed in items:
            prev = _last_pulses.get(channel)
            if prev is None:
                delta = PULSE_UNKNOWN    # drooped: pace it conservatively
                moved = True
            else:
                delta = abs(pulse - prev)
                moved = moved or delta > 0
            block.append((channel, pulse))
            if speed > 0:
                longest = max(longest, delta / speed)
            _last_pulses[channel] = pulse
        if not moved:
            return
        # every joint is commanded every group (2026-11-20): skipping the
        # ones _last_pulses called 'already there' trusted a stale entry and
        # let the group finish early - the base swept over the board while
        # the arm was still travelling.
        t_ms = max(1, int(longest * 1000))
        cmd = ("".join("#%d P%d " % (c, p) for c, p in block)
               + "T%d\r" % t_ms)
        serial.write(cmd.encode())
        print("SSC-32U: %s%s" % (cmd.strip(), note))
        if longest > 0.001:
            time.sleep(longest + 0.08)      # was +0.4 -> big dead pauses
        time.sleep(SETTLE_S)   # let the arm's wobble decay before the next

    arm = [(2, pulseElbow, speedElbow),
           (3, pulseWrist, speedWrist),
           (1, pulseShoulder, speedShoulder)]
    light = [(4, pulseG, speedG), (5, pulseWR, speedWR)]
    # Gripper + wrist rotation + arm move as ONE group (2026-10-22): the old
    # per-group staging only limited current spikes on the weak 6V/3A brick,
    # and the light groups added two dead pauses per move. The base still
    # moves LAST - its sweep is the one that can hit pieces. (The old
    # pair_elbow_wrist / pair_travel_joints / move_all_at_once flags no longer
    # change the grouping; they stay accepted for API compatibility.)
    run_group(light + arm, " (arm together)")

    run_group([(0, pulseZ, speedZ)])                # base rotation last, alone
    return

