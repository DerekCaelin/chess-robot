#!/usr/bin/env python
#
# AL5D robot arm module for the merged chess robot.
# Original: copyright 2018 Richard Day, chess@gotobiz.co.uk
#
# Changes from original:
#   - calibrate_angle(): interactive Gfudge calibration at startup
#   - movearm() rises to CRUISE_HEIGHT before rotating base (safe travel)
#   - park() rotates 90 degrees right of board, low resting position
#   - All servos move together using SSC-32U T parameter (shared duration)

import CBstate
import sys
import time
from math import sqrt, atan, atan2
from subprocess import call

import serial
from lib_al5_2D_IK import al5_2D_IK, al5_moveMotors

_tts = None
if not CBstate.androidos:
    try:
        import pyttsx3
        _tts = pyttsx3.init()
        _tts.setProperty('rate', 125)
    except Exception:
        _tts = None   # speech is cosmetic; a missing espeak voice (e.g. the
                      # Pi lacks 'gmw/en') must never crash the whole app


# ── Speed constants (µs/s — SSC-32U S parameter) ─────────────────────────────
# Valid range is roughly 50–2000. Below ~50 the SSC-32U ignores the value
# and snaps to position. Above 2000 is effectively full speed.
CST_SPEED_DEFAULT  = 250   # elbow, wrist, gripper, wrist rotation
CST_SPEED_SHOULDER = 150    # shoulder — carries most load, run slower
#CST_SPEED_ELBOW = 60    # shoulder — carries most load, run slower
#CST_SPEED_WRIST = 60    # shoulder — carries most load, run slower
CST_SPEED_SLOW     = 70    # wake / park fold / vertical moves onto a piece
CST_SPEED_BASE     = 300   # base rotation
CST_SPEED_PICK     = 130   # descent onto a piece - faster than SLOW, still calm

# ── Safe travel height ────────────────────────────────────────────────────────
# Gripper length (wrist -> tips). The arm commands the WRIST (the IK's native
# output); the finger tips hang gripper_len below it. User-facing heights are
# gripper-TIP heights; the internal CRUISE_HEIGHT / park_height are WRIST
# heights (= tip + gripper_len) so all the IK/annulus math below is unchanged.
gripper_len   = getattr(CBstate, "AL5D_gripper_len", 4.5)
CRUISE_HEIGHT = CBstate.AL5D_cruise_height + gripper_len   # wrist height
PARK_HEIGHT   = 7.0   # inches — used during park rise   (driven, synced on apply)

# The AWAKE holding pose: base to the side, arm elevated at cruise height, jaws
# open, gripper vertical.  This is the pose movearm passes through right before
# it rotates the base onto a piece, and the shoulder/elbow sit mid-range there
# so it holds with very little servo buzz.  READY_REACH is the mid-board hover
# radius (8-9" is the quiet band); base angle 10deg -> pulse 611 = the side.
READY_REACH = 8.5

# The arm's measured UNPOWERED rest (the pose it droops to when torque is cut):
# base ~perpendicular to the board, elbow ~10 deg, wrist dropped back, jaws
# open.  channels: base,shoulder,elbow,wrist,gripper,wristrot.  This is the
# REFERENCE for the park pose: arm.park_reach / arm.park_height default to the
# IK pose that reproduces these pulses, so park() sits at the droop and cutting
# torque barely moves the arm.
REST_PULSES = {0: 611, 1: 1755, 2: 1995, 3: 739, 4: 1077, 5: 1500}

# ── Physical constants ────────────────────────────────────────────────────────
squaresize    = CBstate.AL5D_squaresize
stickout      = CBstate.AL5D_stickout
Gfudge        = CBstate.AL5D_Gfudge
axistorow0    = CBstate.AL5D_axistorow0
# Height of the shoulder pivot above the board surface (in). Every Y in this
# module is board-relative, but the 2D IK's Y=0 is the shoulder pivot, so we
# subtract this before calling the IK (see movemotors/pose_ok).
shoulder_height = getattr(CBstate, "AL5D_shoulder_height", 0.0)
radscale      = CBstate.AL5D_radscale
pickheightadj = CBstate.AL5D_pickheightadj
# extraheight is retired (folded away). Kept only so older tooling that reads
# RD.extraheight still resolves. It no longer appears in the descent formula.
extraheight   = getattr(CBstate, "AL5D_extraheight", 0.0)
# Grip height model (all board-relative, measured UP from the board surface):
#   Grip_height = piece_h + pickheightadj + pickheightreachslope * reach
#   Y_wrist     = Grip_height + gripper_len
# pickheightadj is a pure adjustment (POSITIVE raises the gripper);
# pickheightreachslope is per inch of reach (POSITIVE raises as it extends).
pickheightreachslope = getattr(CBstate, "AL5D_pickheightreachslope", 0.0)
PICK_REF = 6.0         # legacy reference reach (kept for tooling/tests)
extrawidth    = CBstate.AL5D_extrawidth
park_reach    = CBstate.AL5D_park_reach
park_height   = CBstate.AL5D_park_height + gripper_len   # wrist height
# Reach-aware cruise (1.5" board): at a long reach the arm cannot rise all the
# way to CRUISE_HEIGHT without leaving the work annulus (radius sqrt(reach^2 +
# height^2) must stay under A+B). cruise_margin is the safety inset from A+B
# used when computing the highest SAFE travel height for a given reach.
cruise_margin = getattr(CBstate, "AL5D_cruise_margin", 0.15)
# Graveyard offset beyond the a-file edge (in). The captured-piece grid now sits
# on the a side (the empty edge the arm leans into), first column a full
# grave_gap past the a-file, second column one square out from the first.
grave_gap     = getattr(CBstate, "AL5D_grave_gap", 1.0)
rtod          = 57.295779
cmtoin        = 0.393701

# Calibration mode + measured-corner frame (mode B). Mode "A" = parametric
# (gfudge/radscale/stickout/squaresize/axistorow0); mode "B" maps board units
# through a fitted pivot + scale (seeded from the A defaults; refined by the
# corner calibration).  settings.apply() syncs these from CBstate.
calib_mode    = getattr(CBstate, "AL5D_calib_mode", "A")
frame_px      = getattr(CBstate, "AL5D_frame_px", 4.0)
frame_py      = getattr(CBstate, "AL5D_frame_py", -2.75)
frame_scale   = getattr(CBstate, "AL5D_frame_scale", 1.0)
frame_offset  = getattr(CBstate, "AL5D_frame_offset", 3.878)
frame_a0      = getattr(CBstate, "AL5D_frame_a0", 81.829)
frame_sign    = getattr(CBstate, "AL5D_frame_sign", 1)

# ── Arm state ─────────────────────────────────────────────────────────────────
# Servo horn PO (pulse-offset) trims measured with tests/arm_center.py
# 2026-09-16 (base/shoulder/elbow centred at 0).  The SSC-32U FORGETS PO at
# power-off, so apply_po_offsets() must run after every port open — init()
# and the bench tools all call it.
PO_OFFSETS = ((3, 33), (4, 66), (5, 66))   # (channel, microseconds)


class IKUnreachable(Exception):
    """Raised by movemotors() instead of the silent all-middle fallback
    when STRICT_IK is on (calibration/probing sessions)."""


STRICT_IK = False

AL5_DefaultPos = (1500, 1500, 1500, 1500, 966, 1500)  # neutral centred

defaultTargetX  = 4;  defaultTargetY  = 4;  defaultTargetZ  = 90
defaultTargetG  = 90; defaultTargetWA = 0;  defaultTargetWR = 90
defaultTargetShoulder = 90; defaultTargetElbow = 90

targetXYZGWAWR  = (defaultTargetX, defaultTargetY, defaultTargetZ,
                   defaultTargetG, defaultTargetWA, defaultTargetWR)
sp = None

# Whether the servos currently hold torque (last command wasn't idleall). Lets
# energise() be a no-op when the arm is already awake, so it only ever performs
# the slow wake-from-sleep (right after park()/idleall()).
energised = False

# Per-motor speeds (µs/s): Base, Shoulder, Elbow, Wrist, Gripper, WristRot
speed_SEWBZWrG = (CST_SPEED_BASE,      # #0 base rotation
                  CST_SPEED_SHOULDER,  # #1 shoulder — slower
                  CST_SPEED_SHOULDER,  # #2 elbow
                  CST_SPEED_SHOULDER,  # #3 wrist angle
                  CST_SPEED_DEFAULT,   # #4 gripper
                  CST_SPEED_DEFAULT)   # #5 wrist rotation

# Travel = 1.5x the normal per-joint profile (used by the Arm-grab jog moves).
SPEED_TRAVEL = tuple(int(s * 1.5) for s in speed_SEWBZWrG)
# Pick descent: load joints a bit faster than SLOW (70) but well short of a
# snap - onto a piece, shoulder/elbow/wrist at CST_SPEED_PICK.
SPEED_PICK = (CST_SPEED_BASE, CST_SPEED_PICK, CST_SPEED_PICK, CST_SPEED_PICK,
              CST_SPEED_DEFAULT, CST_SPEED_DEFAULT)

# ── Piece tables ──────────────────────────────────────────────────────────────
xmtrans = {
    "a":  3.5, "b":  2.5, "c":  1.5, "d":  0.5,
    "e": -0.5, "f": -1.5, "g": -2.5, "h": -3.5
}
xtrans = {
    "a": 0, "b": 1, "c": 2, "d": 3,
    "e": 4, "f": 5, "g": 6, "h": 7
}
pieceheights = {"p": 1.26, "r": 1.42, "n": 1.57, "b": 1.93, "q": 2.24,
                "k": 2.48}   # inches (piece height above the board)
piecewidths  = {"p": 85,  "r": 81,  "n": 95,  "b": 80,  "q": 75,  "k": 77}

startrobotbtntext1 = "Start robot"
startrobotbtntext2 = "Initialise AL5D arm"
startrobotbtntext3 = "Arm ready — press to go home"
toplabeltext2 = "Serial port open. Position arm and press button."
toplabeltext3 = "Arm initialised. Adjust position, then press button."
toplabeltext4 = "Make your move, then press 'I've moved' button."

# ── Utilities ─────────────────────────────────────────────────────────────────

def speaker(text):
    if CBstate.androidos:
        from plyer import tts
        tts.speak(text)
    elif _tts is not None:
        try:
            _tts.say(text)
            _tts.runAndWait()
        except Exception:
            pass


def idleall():
    global energised
    print("< Idling motors... >")
    energised = False
    if sp is None:
        return
    for i in range(6):
        try:
            sp.write(("#%d P0\r" % i).encode())
        except Exception:      # never let shutdown cleanup raise
            break
    print("< Done >")


def quitter():
    print("Game ends")
    if sp:
        idleall()
        time.sleep(3)
    sys.exit()


# ── Geometry ──────────────────────────────────────────────────────────────────

def _model_sq(squarename):
    """Square name mapped into the arm-model convention (axis beside rank 8).
    Board labelling may be rotated/flipped vs that convention; the flips are
    auto-detected by arm calibration and persisted (arm.flip_rank / .file)."""
    f = "abcdefgh".index(squarename[0])
    r = int(squarename[1])
    if CBstate.AL5D_flip_file:
        f = 7 - f
    if CBstate.AL5D_flip_rank:
        r = 9 - r
    return "abcdefgh"[f] + str(r)


def armangle_xy(x, y):
    """Base angle (deg) for a model-plane point (x lateral inches +toward a,
    y depth inches from the rank-8 centreline; axis sits at y = -axistorow0)."""
    return Gfudge + 90 + rtod * atan(x / (y + axistorow0))


def armlength_xy(x, y):
    """IK work-reach (in) for the same model-plane point. radscale maps planar
    distance to the IK work radius; arm_probe.py --fit adjusts it empirically."""
    return ((sqrt(x**2 + (y + axistorow0)**2)) + stickout) * radscale


def board_sq_center(squarename):
    """Board-unit centre of a square: (bx, by), both 0..8. bx: a-file=0 ..
    h-file=8; by: rank-8=0 .. rank-1=8 (square CENTRES at n+0.5). Mode A applies
    the labelling flips first; mode B uses the raw label (measured corners)."""
    m = _model_sq(squarename) if calib_mode == "A" else squarename
    f = "abcdefgh".index(m[0])
    r = int(m[1])
    return f + 0.5, (8 - r) + 0.5


def planar_to_arm(bx, by):
    """(reach, base-angle) for a board-unit point. Mode A = the parametric
    model; mode B = the fitted corner frame (pivot + scale)."""
    if calib_mode == "B":
        dx = bx - frame_px
        dy = by - frame_py
        dr, da = corner_corr(bx, by)
        return (frame_scale * sqrt(dx * dx + dy * dy) + frame_offset + dr,
                frame_a0 - frame_sign * rtod * atan2(dx, dy) + da)
    x = (4.0 - bx) * squaresize
    y = (by - 0.5) * squaresize
    return armlength_xy(x, y), armangle_xy(x, y)


def armangle(squarename):
    return planar_to_arm(*board_sq_center(squarename))[1]


def armlength(squarename):
    return planar_to_arm(*board_sq_center(squarename))[0]


def parametric_pose(squarename):
    """(reach, base-angle) from the PARAMETRIC model, independent of calib_mode.
    Used to seed the corner calibration's best-guess, which must not depend on
    the as-yet-uncalibrated mode-B frame."""
    m = _model_sq(squarename)
    x = xmtrans[m[0]] * squaresize
    y = (8 - int(m[1])) * squaresize
    return armlength_xy(x, y), armangle_xy(x, y)


# Piecewise reach-dependent pick-height correction (step function). The
# fingertip's true height drifts with reach NON-LINEARLY, so a single linear
# slope can't express it. Values below are the old (working) arm's hand-fitted
# steps; re-tune after the shoulder-height fix.
def grip_y(piecetype, reach):
    """Board-relative WRIST height for the gripper tip to land at the piece's
    grip point: Grip_height = piece_h + pickheightadj + pickheightreachslope*reach,
    Y_wrist = Grip_height + gripper_len. 'piece_h' is in inches."""
    piece_h = pieceheights[piecetype]
    return piece_h + pickheightadj + pickheightreachslope * reach + gripper_len


# ── Arm sag compensation ─────────────────────────────────────────────────────
# The arm FLEXES as it extends: a commanded wrist height lands the gripper
# lower than commanded, and the droop grows with reach (non-linear - moment
# arm grows, bracket flexes). sag_pts is the measured compensation curve: the
# amount ADDED to any commanded height so the gripper physically lands where
# the model says. The "Arm sag compensation" screen calibrates it live against
# a pawn. Points are (reach in, compensation in), sorted; piecewise-linear
# between them, clamped outside the measured range. Empty = no compensation
# (the pre-2026-10-21 behaviour).
sag_pts = []


def set_sag(points):
    """Install the sag curve from the settings form [[square, comp], ...]:
    each sample square's CURRENT reach (frame-dependent) anchors its point."""
    global sag_pts
    pts = []
    for item in (points or []):
        try:
            pts.append((float(armlength(item[0])), float(item[1])))
        except Exception:
            continue
    sag_pts = sorted(pts)


def sag_comp(reach):
    """Compensation (in) to ADD to a commanded height at this reach."""
    if not sag_pts:
        return 0.0
    if reach <= sag_pts[0][0]:
        return sag_pts[0][1]
    if reach >= sag_pts[-1][0]:
        return sag_pts[-1][1]
    for i in range(1, len(sag_pts)):
        if reach <= sag_pts[i][0]:
            x0, y0 = sag_pts[i - 1]
            x1, y1 = sag_pts[i]
            return y0 + (reach - x0) / (x1 - x0) * (y1 - y0)
    return sag_pts[-1][1]


def ik_Y(X, Y):
    """The height actually commanded for a nominal target Y: sag compensation
    is added HERE so every caller above speaks real heights (picks, drops,
    cruise, jog - all land where the model says)."""
    return Y + sag_comp(X)


# Per-corner corrections for the mode-B frame (see CBstate.AL5D_corner_res):
# the 5-parameter frame cannot hit measured squares exactly, so the leftover
# residual is stored per square and added back - the model then reproduces the
# calibration EXACTLY at every measured point (the automatic calibration
# check: predicted == measured) and blends between them. The measurement set
# is the 3x3 lattice (2026-10-22): corners + edge midpoints + centre - the
# corners alone left interior squares like d4 on the wrong end of the chord
# through a curved (flexing) arm response.
corner_res = {}

# The 3x3 measurement lattice in board units: columns a, d, h (bx 0.5/3.5/7.5),
# rows rank 8, rank 4, rank 1 (by 0.5/4.5/7.5).
GRID_XS = (0.5, 3.5, 7.5)
GRID_YS = (0.5, 4.5, 7.5)
GRID9 = (("a8", "d8", "h8"),
         ("a4", "d4", "h4"),
         ("a1", "d1", "h1"))


def set_corner_res(points):
    """Install the corrections from the settings form [[square, dr, da], ...]."""
    global corner_res
    d = {}
    for item in (points or []):
        try:
            d[str(item[0]).lower()] = (float(item[1]), float(item[2]))
        except (TypeError, ValueError, IndexError):
            continue
    corner_res = d


def _lattice_locate(t, stops):
    """(index, fraction) of t within the (non-uniform) lattice stops."""
    if t <= stops[0]:
        return 0, 0.0
    if t >= stops[-1]:
        return len(stops) - 2, 1.0
    for i in range(1, len(stops)):
        if t <= stops[i]:
            span = stops[i] - stops[i - 1]
            return i - 1, ((t - stops[i - 1]) / span if span else 0.0)
    return len(stops) - 2, 1.0


def corner_corr(bx, by):
    """(reach, angle) correction at a board point. With the full 3x3 lattice
    measured it blends bilinearly over the grid - EXACT at every measured
    square (including interior ones like d4) and smooth between. Falls back to
    the four-corner blend for older 4-corner data."""
    if all(sq in corner_res for row in GRID9 for sq in row):
        i, s = _lattice_locate(float(bx), GRID_XS)
        j, t = _lattice_locate(float(by), GRID_YS)

        def node(ii, jj):
            return corner_res[GRID9[jj][ii]]
        (r00, a00), (r10, a10) = node(i, j), node(i + 1, j)
        (r01, a01), (r11, a11) = node(i, j + 1), node(i + 1, j + 1)
        dr = ((1 - s) * ((1 - t) * r00 + t * r01)
              + s * ((1 - t) * r10 + t * r11))
        da = ((1 - s) * ((1 - t) * a00 + t * a01)
              + s * ((1 - t) * a10 + t * a11))
        return dr, da
    if len(corner_res) < 4:
        return 0.0, 0.0
    u = min(1.0, max(0.0, (float(bx) - 0.5) / 7.0))
    v = min(1.0, max(0.0, (float(by) - 0.5) / 7.0))
    a8 = corner_res.get("a8", (0.0, 0.0))
    h8 = corner_res.get("h8", (0.0, 0.0))
    h1 = corner_res.get("h1", (0.0, 0.0))
    a1 = corner_res.get("a1", (0.0, 0.0))
    dr = ((1 - u) * ((1 - v) * a8[0] + v * a1[0])
          + u * ((1 - v) * h8[0] + v * h1[0]))
    da = ((1 - u) * ((1 - v) * a8[1] + v * a1[1])
          + u * ((1 - v) * h8[1] + v * h1[1]))
    return dr, da


# ── Reach-aware travel height (large-board support) ───────────────────────────
# The IK keeps the fingertip at (reach, height) with work radius
# floatM = sqrt(reach^2 + height^2) which must stay under A+B (link ceiling).
# At a long reach the arm therefore cannot rise all the way to CRUISE_HEIGHT -
# it can only lift to max_lift_height(reach). cruise_for_reach() gives the safe
# travel height for a square; the far corner squares (a1/h1) ride lower.
def _work_cap():
    return CBstate.AL5D_A + CBstate.AL5D_B - cruise_margin


def max_lift_height(reach):
    """Highest BOARD-relative fingertip height reachable at work-reach `reach`.
    The IK's work radius is sqrt(reach^2 + (Y_board - shoulder_height)^2), so the
    ceiling on Y_board is shoulder_height + sqrt(cap^2 - reach^2) - and the
    sag compensation the seam will ADD has to come out of that ceiling."""
    cap = _work_cap()
    r = cap * cap - float(reach) * float(reach)
    return shoulder_height + (sqrt(r) if r > 0 else 0.0) - sag_comp(reach)


def cruise_for_reach(reach):
    """Safe travel height at `reach`: CRUISE_HEIGHT when reachable, lower at the
    far edge so a vertical lift never leaves the work annulus."""
    return min(CRUISE_HEIGHT, max_lift_height(reach))


def reach_at_height(height):
    """Longest work-reach that can still sit at BOARD-relative fingertip
    `height` (the reach you must retract to before you can rise all the way to
    cruise height). Sag compensation grows with reach, so solve by iteration
    (converges fast: comp is bounded and monotone-ish)."""
    cap = _work_cap()

    def _cap_at(h):
        r = cap * cap - (float(h) - shoulder_height) ** 2
        return sqrt(r) if r > 0 else 0.0

    x = _cap_at(height)
    for _ in range(4):
        x = _cap_at(float(height) + sag_comp(x))
    return x


def is_afile_square(squarename):
    """True if the square sits on the a-file edge in the ARM model frame
    (the empty side the arm leans into for its edge approach)."""
    return _model_sq(squarename)[0] == "a"


# ── Capture graveyard ───────────────────────────────────────────────────────
# Two columns just off the A-file (the empty edge side the arm already leans
# into), seven rows deep (rank 8 down to rank 2 — rank 1 is out of reach on a
# 1.5" board) — 14 slots, filled by ALTERNATING inner/outer so a captured piece
# never sits in the far (outer) column until the near slot at that rank is used.
GRAVE_COLS = 2
GRAVE_ROWS = 7            # ranks 8..2 (rank 1 excluded: out of workspace)
_grave_next = 0


def reset_graveyard():
    global _grave_next
    _grave_next = 0


def grave_slot(n):
    """(reach, base-angle) of the nth graveyard slot (0-based). col = inner(0)
    /outer(1), row walks rank 8 -> 2; alternating means even n = inner, odd =
    outer at each rank. Placed on the a side (respects flip_file). Board-unit
    based so it works in either calibration mode."""
    col = n % GRAVE_COLS
    row = n // GRAVE_COLS
    units = squaresize if calib_mode == "A" else frame_scale
    gap = grave_gap / units                         # inches -> board units
    sgn = -1 if CBstate.AL5D_flip_file else 1       # a-file is model +x (bx<4)
    bx = 4.0 - sgn * (4.0 + gap + col)              # inner/outer column centres
    by = row + 0.5                                  # ranks 8 -> 2, near to far
    return planar_to_arm(bx, by)


def next_grave_pose():
    """Advance the graveyard and return the next drop pose (clamps to the last
    slot past 14 captures)."""
    global _grave_next
    n = _grave_next
    _grave_next = min(n + 1, GRAVE_COLS * GRAVE_ROWS - 1)
    return grave_slot(n)


# ── Motor control ─────────────────────────────────────────────────────────────

def movemotors(slow=False, speeds=None, pick_lean=False, compensate=True):
    global targetXYZGWAWR, energised
    if sp is None:
        print("ERROR: serial port not open — arm move skipped.")
        return
    t = targetXYZGWAWR
    # Convert board-relative Y to the IK's shoulder-relative Y (Y=0 at
    # shoulder). compensate adds the sag curve so the gripper physically
    # lands at the commanded height; the park/rest poses opt out (they ARE
    # the unpowered droop - compensating would lift the arm off it).
    ycmd = (ik_Y(t[0], t[1]) if compensate else t[1]) - shoulder_height
    result = al5_2D_IK((t[0], ycmd, t[2], t[3], t[4], t[5]))
    if isinstance(result, tuple):
        motors = result
    else:
        # Historically the arm silently drove to the all-middle default
        # pose, which looked like a hangup during calibration (2026-09-18:
        # a fitted stickout of ~6" put most squares out of reach).  In
        # STRICT_IK mode (calibration/probing) the caller is told instead.
        print("IK error:", result)
        if STRICT_IK:
            raise IKUnreachable("target (%.1f, %.1f) unsolvable by IK (err %s)"
                                % (targetXYZGWAWR[0], targetXYZGWAWR[1],
                                   result))
        motors = (defaultTargetShoulder, defaultTargetElbow,
                  defaultTargetWA, defaultTargetZ,
                  defaultTargetG, defaultTargetWR)
    if speeds is not None:
        speeds = tuple(speeds)
    elif slow:
        speeds = tuple(CST_SPEED_SLOW for _ in speed_SEWBZWrG)
    else:
        speeds = speed_SEWBZWrG
    try:
        al5_moveMotors(motors, speeds, sp, pair_elbow_wrist=slow,
                       pair_travel_joints=CBstate.AL5D_pair_travel_joints,
                       pick_lean=pick_lean)
    except (serial.SerialException, OSError) as e:
        print("SERIAL LINK LOST:", e)
        print("(SSC-32U most likely brownout-reset mid-move: the USB device")
        print(" dropped off the bus. Check the servo PSU rating (6V/4A+ for")
        print(" paired travel joints), then restart this program.)")
        raise
    energised = True
    # al5_moveMotors waits for each group to finish before returning


def openjaws():
    global targetXYZGWAWR
    targetXYZGWAWR = (targetXYZGWAWR[0], targetXYZGWAWR[1],
                      targetXYZGWAWR[2], 42 + extrawidth,
                      targetXYZGWAWR[4], targetXYZGWAWR[5])
    movemotors()


def closejaws(piecetype):
    global targetXYZGWAWR
    jawsize = piecewidths[piecetype] + 15 + extrawidth
    targetXYZGWAWR = (targetXYZGWAWR[0], targetXYZGWAWR[1],
                      targetXYZGWAWR[2], jawsize,
                      targetXYZGWAWR[4], targetXYZGWAWR[5])
    movemotors()
    time.sleep(1.4)


GRIP_CLOSED = 140  # gripper servo angle with the jaws meeting at the tip.
                   # (Open is 42+extrawidth; a pawn is gripped at 85+15+extra.
                   # 140 closes the jaws to a point for arm pointing - tune at
                   #  the bench: raise until the tips meet, lower if it stalls.)


def close_gripper():
    """Close the jaws to a point so the two tips mark the modelled fingertip
    (used by arm pointing calibration to make the centre unambiguous)."""
    global targetXYZGWAWR
    targetXYZGWAWR = (targetXYZGWAWR[0], targetXYZGWAWR[1],
                      targetXYZGWAWR[2], GRIP_CLOSED,
                      targetXYZGWAWR[4], targetXYZGWAWR[5])
    movemotors()


def set_gripper(width):
    """Drive ONLY the gripper servo (channel 4) to `width`, WITHOUT re-running
    the arm IK.  closejaws/openjaws re-solve the whole pose, so at a far square
    whose reach sits right on the IK limit a "grab" spuriously fails with
    'out of reach'.  Waits for the servo to FINISH the travel before returning:
    the old flat 1.3 s let the arm start lifting with the grip still closing
    (a full open->grip sweep is ~645 us at 250 us/s = 2.6 s, 2026-10-21)."""
    global targetXYZGWAWR
    targetXYZGWAWR = (targetXYZGWAWR[0], targetXYZGWAWR[1],
                      targetXYZGWAWR[2], width,
                      targetXYZGWAWR[4], targetXYZGWAWR[5])
    from lib_al5_2D_IK import getPulseFromAngle, _last_pulses
    pulse = getPulseFromAngle(width)
    prev = int(_last_pulses.get(4, pulse))
    if sp is not None:
        sp.write(("#4 P%d S%d\r" % (pulse, CST_SPEED_DEFAULT)).encode())
        sp.flush()
    _last_pulses[4] = pulse
    travel = (abs(pulse - prev) / float(CST_SPEED_DEFAULT)
              if CST_SPEED_DEFAULT > 0 else 0.0)
    time.sleep(max(1.3, travel + 1.0))


GRIP_OPEN_BASE = 42          # open jaws: GRIP_OPEN_BASE + extrawidth


def grip_open_width():
    """Open jaw width (mirrors openjaws' 42 + extrawidth)."""
    return GRIP_OPEN_BASE + extrawidth


def grip_width(piece="p"):
    """Jaw width that closes on a piece of this type (as closejaws uses)."""
    return piecewidths[piece] + 15 + extrawidth



def moveXYZGWA(X, Y, Z, G, WA, speeds=None):
    global targetXYZGWAWR
    targetXYZGWAWR = (float(X), float(Y), float(Z),
                      float(G), float(WA), targetXYZGWAWR[5])
    movemotors(speeds=speeds)


def moveXY(toheight, speeds=SPEED_PICK):
    """Vertical move to a height. The pick/drop DESCENT runs at SPEED_PICK
    (calm onto a piece); a lift-to-cruise passes SPEED_TRAVEL - a lift is
    cruise, not a pick (2026-10-22)."""
    global targetXYZGWAWR
    targetXYZGWAWR = (targetXYZGWAWR[0], float(toheight),
                      targetXYZGWAWR[2], targetXYZGWAWR[3],
                      -90, targetXYZGWAWR[5])
    movemotors(slow=True, speeds=speeds, pick_lean=True)


def movearm(X, Z, edge_square=None):
    """
    Safe travel to (X, Z) in three separate steps, each its own movemotors()
    call so no two large motors move simultaneously:
      1. Rise to a height safe at the CURRENT reach (cruise, or lower at a far
         edge reach so the straight-up move never leaves the work annulus)
      2. Rotate base to Z       — base only, arm already at a safe height
      3. Extend reach to X      — lower to the height that reach allows at cruise
    On a 1" board the safe height is CRUISE_HEIGHT everywhere, so this is the
    old rise/rotate/extend unchanged; on a 1.5" board the far corners (a1/h1)
    arrive a little lower and the leave-lift retracts (see _lift_clear).

    edge_square: for a-file squares, route the final cruise translation through
    the outward (+x, empty) pose FIRST, so the arm settles onto the square
    coming from OUTSIDE instead of drifting inward across b-file. All of this
    happens at cruise height above the pieces, so it can never sweep a piece.
    """
    global targetXYZGWAWR

    if edge_square is not None:
        eo = edge_outward_pose(edge_square)
        if eo is not None:
            movearm(eo[0], eo[1])            # come to the outward edge pose first

    cur = cruise_for_reach(targetXYZGWAWR[0])
    # Step 1: rise to the height safe for the current reach, base angle unchanged
    targetXYZGWAWR = (targetXYZGWAWR[0], cur,
                      targetXYZGWAWR[2], targetXYZGWAWR[3],
                      -90, targetXYZGWAWR[5])
    movemotors(speeds=SPEED_TRAVEL)

    # Step 2: rotate base to Z, reach unchanged (radius unchanged -> stays safe)
    targetXYZGWAWR = (targetXYZGWAWR[0], cur, float(Z),
                      targetXYZGWAWR[3], -90, targetXYZGWAWR[5])
    movemotors(speeds=SPEED_TRAVEL)

    # Step 3: extend reach to X at the height this reach can hold at cruise
    targetXYZGWAWR = (float(X), cruise_for_reach(float(X)), float(Z),
                      targetXYZGWAWR[3], -90, targetXYZGWAWR[5])
    movemotors(speeds=SPEED_TRAVEL)


def _lift_clear(X, Z, from_height):
    """Rise off a picked piece at work-reach X back to full cruise. If X is a
    reach that can hold CRUISE_HEIGHT, this is a straight vertical (the old
    behaviour). At a far-edge reach a straight vertical would leave the annulus,
    so pull inward to the reach that CAN hold cruise while raising - the shoulder
    'retract-while-lift'. The work-region is convex so the straight servo move
    between the pick pose and this lifted pose never exceeds the annulus."""
    safe_reach = reach_at_height(CRUISE_HEIGHT)
    if X <= safe_reach:
        moveXY(CRUISE_HEIGHT, speeds=SPEED_TRAVEL)   # a lift is cruise, not a pick
    else:
        moveXYZGWA(safe_reach, CRUISE_HEIGHT, Z,
                   targetXYZGWAWR[3], -90, speeds=SPEED_TRAVEL)


def edge_outward_pose(squarename):
    """a-file edge-approach start, auto-derived (no bench knob): the pose one
    half-square OUTSIDE the a-file edge (x = 4*squaresize, past the a centre at
    3.5) at the square's rank. Returning it lets the arm come down onto the
    a-file from the empty +x side instead of sweeping inward across b-file.
    Returns None when even that outer pose is not in the workspace (degrade to
    a normal vertical descent)."""
    m = _model_sq(squarename)
    r = int(m[1])
    bx = 0.0                      # a-file outer edge (board-unit x=0 == +4 squares)
    by = (8 - r) + 0.5
    Xo, Zo = planar_to_arm(bx, by)
    if pose_ok(Xo, cruise_for_reach(Xo), Zo) and pose_ok(Xo, 2.5, Zo):
        return Xo, Zo
    return None


# ── Piece manipulation ────────────────────────────────────────────────────────

def pickuppiece(piecetype, armlen):
    """Descend (straight, -90 vertical) onto the piece, close, then lift clear.
    The a-file edge routing happens at cruise inside movearm (edge_square),
    before this is called, so nothing sweeps across the board at pick height.
    The grip is SERVO-ONLY (set_gripper, no IK re-solve) so nothing can twitch
    while the jaws close on the piece (2026-10-21)."""
    moveXY(grip_y(piecetype, armlen))   # vertical -90 approach, straight down
    set_gripper(grip_width(piecetype))
    _lift_clear(armlen, targetXYZGWAWR[2], targetXYZGWAWR[1])


def droppiece(piecetype, armlen):
    """Release at the PICK height: at grip height the piece's base is already
    sitting on the board (that is where the pick gripped it), so the release is
    a pure unclamp - no drop, no bounce. (It used to descend 0.1 in LOWER and
    press the piece into the board while the wrist finished its swing - the
    piece dragged, 2026-10-21.) The release is servo-only: no IK re-solve can
    move the arm at the unclamp instant."""
    moveXY(grip_y(piecetype, armlen))
    set_gripper(grip_open_width())
    _lift_clear(armlen, targetXYZGWAWR[2], targetXYZGWAWR[1])


def takepiece(X, Z, targetpiece, source_square=None):
    # NB: movepiece has already energised for this turn, and the arm stays awake
    # (torque on) through the capture, so we deliberately do NOT energise() or
    # park_hold() here - that pair of slow hold_ready side-swings made the arm
    # "very slowly return to neutral" before the next grab. droppiece below lifts
    # to a reach-safe cruise, so movearm(source) then runs directly at speed.
    movearm(X, Z, edge_square=(source_square if source_square
                               and is_afile_square(source_square) else None))
    pickuppiece(targetpiece, X)
    Xgraveyard, Zgraveyard = next_grave_pose()
    if not (pose_ok(Xgraveyard, cruise_for_reach(Xgraveyard), Zgraveyard)
            and pose_ok(Xgraveyard, 2.5, Zgraveyard)):
        # that graveyard slot sits outside the workspace - use the classic spot
        Xgraveyard = armlength("h9") + 0.5
        Zgraveyard = armangle("h9")
    movearm(Xgraveyard, Zgraveyard)
    time.sleep(0.5)      # was 1.5: the wrist tracking keeps the piece still now
    droppiece(targetpiece, Xgraveyard)

def iscastling(sourcesquarename):
    if CBstate.cbstate == 4:
        rsource = "h8"; rtarget = "f8"
    elif CBstate.cbstate == 5:
        rsource = "a8"; rtarget = "d8"
    else:
        return
    rsourceX = armlength(rsource); rsourceZ = armangle(rsource)
    rtargetX = armlength(rtarget); rtargetZ = armangle(rtarget)
    print("Castling " + sourcesquarename)
    movearm(rsourceX, rsourceZ,
            edge_square=rsource if is_afile_square(rsource) else None)
    pickuppiece("r", rsourceX)
    movearm(rtargetX, rtargetZ,
            edge_square=rtarget if is_afile_square(rtarget) else None)
    droppiece("r", rtargetX)
    park_hold()


def enpassant(sourcesquarename):
    if CBstate.cbstate == 2:
        col  = sourcesquarename[0]
        rank = str(int(sourcesquarename[1]) - 1)
        epsquarename = col + rank
        print("En passant remove:", epsquarename)
        takepiece(armlength(epsquarename), armangle(epsquarename), "p",
                  epsquarename)


def updateboard(source, target, boardbefore):
    sx = xtrans[source[0]]; sy = 8 - int(source[1])
    tx = xtrans[target[0]]; ty = 8 - int(target[1])
    boardbefore[ty][tx] = boardbefore[sy][sx]
    boardbefore[sy][sx] = "."
    return boardbefore


def pose_ok(X, Y, Z):
    """True only if the whole IK solution for this pose sits inside the
    servos' physical range. Used to REFUSE impossible moves (say so and let
    the human place the piece) instead of clamping a -19-degree wrist to
    the 0-degree endstop and dropping the gripper onto the board
    (2026-09-18)."""
    res = al5_2D_IK((X, ik_Y(X, Y) - shoulder_height, Z, 40 + extrawidth,
                     -90, 90))
    if not isinstance(res, tuple):
        return False
    # 181-ish on the wrist is normal (servo hard stop absorbs the last
    # degree - the game runs with those every move). What must never ship
    # is a solver failure or a back-folded NEGATIVE angle (the -19 that
    # clamped to 0 and dropped the gripper 2026-09-18).
    return all(float(a) >= -1.0 for a in res)


def movepiece(sourcesquarename, targetsquarename, boardbefore):
    sourceX = armlength(sourcesquarename); sourceZ = armangle(sourcesquarename)
    targetX = armlength(targetsquarename); targetZ = armangle(targetsquarename)
    print("[movepiece] Gfudge=%.2f  %s->%s  sourceZ=%.1f targetZ=%.1f"
          % (Gfudge, sourcesquarename, targetsquarename, sourceZ, targetZ))
    for tag, XX, ZZ in ((sourcesquarename, sourceX, sourceZ),
                        (targetsquarename, targetX, targetZ)):
        # check the pose we ACTUALLY command: reach-aware cruise (a far corner
        # like a1/h1 can only hold a lower travel height) and the pick descent.
        if not pose_ok(XX, cruise_for_reach(XX), ZZ) or not pose_ok(XX, 2.5, ZZ):
            raise ValueError("arm workspace cannot reach %s - make that "
                             "move by hand (recalibrate if a real square)"
                             % tag)
    energise()
    sx = xtrans[sourcesquarename[0]]; sy = 8 - int(sourcesquarename[1])
    tx = xtrans[targetsquarename[0]]; ty = 8 - int(targetsquarename[1])
    src_edge = sourcesquarename if is_afile_square(sourcesquarename) else None
    tgt_edge = targetsquarename if is_afile_square(targetsquarename) else None

    if boardbefore[ty][tx] != ".":
        print("Take piece!")
        takepiece(targetX, targetZ, boardbefore[ty][tx].lower(), targetsquarename)

    movearm(sourceX, sourceZ, edge_square=src_edge)
    sourcepiece = boardbefore[sy][sx].lower()
    pickuppiece(sourcepiece, sourceX)
    movearm(targetX, targetZ, edge_square=tgt_edge)
    droppiece(sourcepiece, targetX)

    iscastling(sourcesquarename)
    enpassant(sourcesquarename)
    # ONE torque cut at the very end of the whole move (capture + castle +
    # en-passant hold neutral with torque between sub-steps, so no sag->snap
    # jerk between them).
    park()


# ── Park ──────────────────────────────────────────────────────────────────────

def hold_pose(reach, height, speed=CST_SPEED_SLOW, settle=0.6, compensate=True):
    """Drive to an IK (reach, height) pose at the side base, all-at-once and
    slow (no limp-shoulder window).  Opens the port first if needed (cold wake).
    Used for the elevated READY wake pose and the low park/rest pose."""
    global targetXYZGWAWR, energised
    if sp is None and not arm_open():
        return
    targetXYZGWAWR = (float(reach), float(height), 10.0,
                      42 + extrawidth, -90, 90)
    movemotors(slow=True, speeds=tuple(speed for _ in speed_SEWBZWrG),
               compensate=compensate)
    energised = True
    time.sleep(settle)


def hold_ready(speed=CST_SPEED_SLOW, settle=0.6):
    """The AWAKE holding pose: base to the side, arm elevated at cruise height
    (the low-buzz pose just before a base rotation).  Used by park_hold()."""
    hold_pose(READY_REACH, CRUISE_HEIGHT, speed, settle)


def hold_rest(speed=CST_SPEED_SLOW, settle=0.6):
    """Wake INTO the low rest/park pose (the droop). Gentle: the arm is already
    near this pose after park()/idleall(), so this is a tiny re-seat - no
    dramatic lift or shake (2026-10-19). uncompensated like park() - this is
    the droop pose itself."""
    hold_pose(park_reach, park_height, speed, settle, compensate=False)


def park():
    """After-move park sequence, one axis at a time so the motion reads as
    RELEASE -> LIFT -> RETRACT -> ROTATE BASE -> LOWER -> (torque off):
      0. open the jaws FIRST at the current height - a held piece is set down
         where it is instead of dropped from lift height (2026-10-21)
      1. lift onto CRUISE_HEIGHT (reach + base as they are)
      2. retract the reach to arm.park_reach (base still at the square)
      3. rotate the base to the side (arm tucked in, at cruise height)
      4. lower onto arm.park_height, then idleall() (servos off).
    The rest pose defaults to the measured droop, so cutting torque at the end
    barely moves; the next action wakes into the REST pose via energise()."""
    global targetXYZGWAWR
    if sp is None and not arm_open():
        return
    jaw = 42 + extrawidth
    # 0. release at THIS height. Servo-only (set_gripper does not re-solve the
    #    arm) so a park from a grab sitting on the IK limit can never fail the
    #    release; its settle doubles as the jaw-opening window. Skipped when
    #    the jaws are already open (the normal end of a game move). Jaw width
    #    runs OPEN = 42+extrawidth up to a grip (pawn ~100+): anything above
    #    the open width is still holding.
    if targetXYZGWAWR[3] > jaw + 1:
        set_gripper(jaw)
    # 1. lift to cruise height, reach + base unchanged (cruise speed)
    targetXYZGWAWR = (targetXYZGWAWR[0], CRUISE_HEIGHT, targetXYZGWAWR[2],
                      jaw, -90, targetXYZGWAWR[5])
    movemotors(speeds=SPEED_TRAVEL)
    # 2. retract reach to the park reach, base still at the target square
    targetXYZGWAWR = (park_reach, CRUISE_HEIGHT, targetXYZGWAWR[2],
                      jaw, -90, targetXYZGWAWR[5])
    movemotors(speeds=SPEED_TRAVEL)
    # 3. rotate the base to the side, arm retracted at cruise height
    targetXYZGWAWR = (park_reach, CRUISE_HEIGHT, 10.0,
                      jaw, -90, targetXYZGWAWR[5])
    movemotors(speeds=SPEED_TRAVEL)
    # 4. lower onto the rest height, then release. uncompensated: the rest
    #    pose IS the unpowered droop - adding sag comp here would lift the arm
    #    off it and it would sag again at idleall.
    targetXYZGWAWR = (park_reach, park_height, 10.0,
                      jaw, -90, targetXYZGWAWR[5])
    movemotors(compensate=False)
    time.sleep(0.4)
    idleall()


def park_hold():
    """Return to the elevated READY pose and KEEP TORQUE - the between-sub-move
    hold (capture / castling). The arm stays at the quiet ready posture for the
    next movearm() instead of sagging on a torque cut and snapping back (= the
    inter-move jerk). The single torque release happens once, at the end of
    movepiece() via park()."""
    hold_ready(settle=0.4)


def energise():
    """In-session wake into the REST/park pose (a near-no-op right after
    power_up, which wakes to the same pose; a gentle slow re-seat when coming
    from the limp droop, i.e. the lazy wake on the first move). All joints at
    once. 2x the old wake speed (still below the 150 normal shoulder speed).
    No-op when the arm is already awake - this is ONLY the wake-from-sleep.
    Waking into the rest pose (not the elevated READY pose) keeps the wake
    gentle - no dramatic lift / shake (2026-10-19)."""
    global energised
    if energised:
        return
    hold_rest(speed=CST_SPEED_SLOW * 2, settle=0.4)

# ── Angle calibration ─────────────────────────────────────────────────────────

def calibrate_angle():
    """
    Interactive calibration for Gfudge. Arm hovers over board centre (d4);
    enter +/- degree adjustments until it points at true centre, then save
    the printed Gfudge value to CBstate.py as AL5D_Gfudge.
    """
    global Gfudge, targetXYZGWAWR
    print("\n── Angle calibration ──────────────────────────────────────")
    print("Arm will hover over computed board centre (d4).")
    print("Enter +N or -N to nudge. Blank/0 when done.")
    print("──────────────────────────────────────────────────────────\n")

    movearm(armlength("d4"), armangle("d4"))
    time.sleep(1)
    moveXY(CRUISE_HEIGHT - 1)
    print("Current Gfudge: %.2f" % Gfudge)

    while True:
        raw = input("Adjustment (degrees): ").strip()
        if raw in ("", "0"):
            break
        try:
            Gfudge += float(raw)
            movearm(armlength("d4"), armangle("d4"))
            print("New Gfudge: %.2f" % Gfudge)
        except ValueError:
            print("Enter a number, e.g. +5 or -3")

    print("\nFinal Gfudge: %.2f" % Gfudge)
    print("Save in CBstate.py:  AL5D_Gfudge = %.2f" % Gfudge)
    park()


# ── Initialisation ────────────────────────────────────────────────────────────

def _ssc_alive(sp):
    """True if an SSC-32U answers a position query on this open port."""
    try:
        sp.reset_input_buffer()
        sp.write(b"ES\r")
        sp.write(b"#0?\r")
        resp = sp.read(16)
        return any(c in b"0123456789" for c in resp)
    except Exception:
        return False


def _resolve_port():
    """Open the SSC-32U port, or None.

    Prefers CBstate.serialport, then falls back to scanning for the SSC-32U's
    FTDI chip (0403:6001) so the arm survives USB renumbering (ttyUSB0 vs
    ttyUSB1 after a replug).  A query reply (if any) confirms the baud rate.

    This board's REPLY path is dead (queries answer on no baud, verified by
    continuous listening during a power-cycle), while blind commands execute
    normally at 9600 — confirmed 2026-09-16 by the base physically sweeping
    to centre and back in response to #0 commands.  The game code is
    write-only (it estimates motion timing in software via _last_pulses), so
    silence is tolerated here: we return the open port and note the mode."""
    import serial.tools.list_ports

    candidates = [CBstate.serialport] if CBstate.serialport else []
    for p in serial.tools.list_ports.comports():
        if (p.vid, p.pid) == (0x0403, 0x6001) and p.device not in candidates:
            candidates.append(p.device)
    if not candidates:
        print("No FTDI/serial device found — check the SSC-32U USB cable.")
        return None

    for port in candidates:
        for baud in (9600, 115200):
            try:
                sp = serial.Serial(port, baud, timeout=1.0)
            except serial.SerialException:
                continue
            if _ssc_alive(sp):
                print("SSC-32U answering queries on %s @ %d baud"
                      % (port, baud))
                return sp
            sp.close()
        # No query answered on any known baud — open at the empirically
        # confirmed command baud and run write-only.
        try:
            sp = serial.Serial(port, 9600, timeout=1.0)
            print("SSC-32U on %s @ 9600 — silent to queries, running "
                  "WRITE-ONLY (commands execute; position readback "
                  "unavailable)." % port)
            return sp
        except serial.SerialException:
            continue
    print("Could not open an SSC-32U port:", ", ".join(candidates))
    print("Check: USB cable, board power (servo VIN LED), link baud.")
    return None


def apply_po_offsets():
    """(Re-)apply measured horn trims to the open port.  Harmless to call
    more than once; PO is RAM-only on the SSC-32U so it must follow every
    power-up / port open."""
    if sp is None:
        return
    for ch, us in PO_OFFSETS:
        sp.write(("#%d PO%d\r" % (ch, us)).encode())
    sp.flush()


def arm_open():
    """Open the SSC-32U port + apply horn trims, WITHOUT moving any servo.
    Returns True if a link is present.  Used at game start just to detect the
    arm (so it can stay asleep until the first move); hold_ready()/power_up()
    do the actual wake."""
    global sp
    if sp is not None:
        return True
    sp = _resolve_port()
    if sp is None:
        return False
    apply_po_offsets()
    time.sleep(0.3)
    return True


def power_up():
    """Headless arm bring-up: open the link (arm_open) then wake ALL joints at
    once, slowly, into the REST/park pose and HOLD (servos energised) - a
    gentle wake, not the elevated READY lift. Returns False if no SSC-32U."""
    if not arm_open():
        return False
    print("Waking from idle...")
    hold_rest(speed=CST_SPEED_SLOW, settle=3.0)
    return True


def init():
    """CLI entry: power_up() plus the interactive angle-calibration question."""
    if not power_up():
        return False
    ans = input("Run angle calibration? (y/n): ").strip().lower()
    if ans == "y":
        energise()
        calibrate_angle()
    else:
        pass   # arm stays idled in park until first chess move
    return True


