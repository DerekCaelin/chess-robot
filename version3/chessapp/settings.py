# settings.py - layered configuration for the whole system.
#
# Layers (later wins):
#   1. hard-coded fallbacks in CBstate.py (source of truth if files missing)
#   2. config/defaults.json  - golden snapshot, shipped in the repo
#   3. data/settings.json    - user edits from the UI ("Direct input", sliders)
#
# Import this module BEFORE any chess/arm module and call apply() once:
# it overlays the merged values onto CBstate (and LEGO_Chess_rpd's PO offsets
# if that module is already imported), so every consumer keeps reading plain
# module constants as before.
#
# keys in the merged dict are dotted, e.g. "arm.gfudge", "stockfish.level",
# "audio.volume", "player.color".

import copy
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULTS_PATH = os.path.join(ROOT, "config", "defaults.json")
USER_PATH = os.path.join(ROOT, "data", "settings.json")

# CBstate attribute for each arm key (None = not mirrored in CBstate)
ARM_TO_CBSTATE = {
    "squaresize":    "AL5D_squaresize",
    "stickout":      "AL5D_stickout",
    "gfudge":        "AL5D_Gfudge",
    "axistorow0":    "AL5D_axistorow0",
    "shoulder_height": "AL5D_shoulder_height",
    "board_height":  "AL5D_board_height",
    "piece_grips":   "AL5D_piece_grips",
    "piece_jaws":    "AL5D_piece_jaws",
    "wrist_sign":    "AL5D_wrist_sign",
    "wrist_offset":  "AL5D_wrist_offset",
    "radscale":      "AL5D_radscale",
    "pickheightadj": "AL5D_pickheightadj",
    "pickheightreachslope": "AL5D_pickheightreachslope",
    "gripper_len":   "AL5D_gripper_len",
    "park_reach":    "AL5D_park_reach",
    "park_height":   "AL5D_park_height",
    "cruise_margin": "AL5D_cruise_margin",
    "grave_gap":     "AL5D_grave_gap",
    "cruise_height": "AL5D_cruise_height",
    "extrawidth":    "AL5D_extrawidth",
    "A":             "AL5D_A",
    "B":             "AL5D_B",
    "flip_rank":     "AL5D_flip_rank",
    "flip_file":     "AL5D_flip_file",
    "calib_mode":    "AL5D_calib_mode",
    "frame_px":      "AL5D_frame_px",
    "frame_py":      "AL5D_frame_py",
    "frame_scale":   "AL5D_frame_scale",
    "frame_offset":  "AL5D_frame_offset",
    "frame_a0":      "AL5D_frame_a0",
    "frame_sign":    "AL5D_frame_sign",
    "corner_res":    "AL5D_corner_res",
    "sag_points":    "AL5D_sag_points",
}

_cache = None


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(force=False):
    global _cache
    if _cache is None or force:
        _cache = _merge(_read_json(DEFAULTS_PATH), _read_json(USER_PATH))
    return _cache


def get(path, fallback=None):
    node = load()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return fallback
        node = node[part]
    return node


def set(path, value):
    """Persist one dotted key into data/settings.json and re-apply."""
    os.makedirs(os.path.dirname(USER_PATH), exist_ok=True)
    user = _read_json(USER_PATH)
    parts = path.split(".")
    node = user
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value
    tmp = USER_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(user, f, indent=1)
    os.replace(tmp, USER_PATH)
    load(force=True)
    apply()


def reset_user():
    """Delete user overrides (back to golden defaults)."""
    try:
        os.remove(USER_PATH)
    except OSError:
        pass
    load(force=True)
    apply()


def apply():
    """Push merged settings into CBstate (and live LEGO module if loaded)."""
    import CBstate
    for key, attr in ARM_TO_CBSTATE.items():
        v = get("arm." + key)
        if v is not None:
            setattr(CBstate, attr, v)
    CBstate.AL5D_extraheight = 0.0
    po = get("arm.po_offsets")
    if po is not None:
        try:
            import LEGO_Chess_rpd as RD
            RD.PO_OFFSETS = tuple((int(c), int(u)) for c, u in po)
        except ImportError:
            pass
    # Sync module-level copies that were read at import time.
    import sys
    lego = sys.modules.get("LEGO_Chess_rpd")
    if lego:
        gl = CBstate.AL5D_gripper_len
        lego.squaresize = CBstate.AL5D_squaresize
        lego.stickout = CBstate.AL5D_stickout
        lego.Gfudge = CBstate.AL5D_Gfudge
        lego.axistorow0 = CBstate.AL5D_axistorow0
        lego.board_height = CBstate.AL5D_board_height
        lego.shoulder_height = CBstate.AL5D_shoulder_height
        lego.radscale = CBstate.AL5D_radscale
        lego.gripper_len = gl
        lego.pickheightadj = CBstate.AL5D_pickheightadj
        lego.extraheight = CBstate.AL5D_extraheight
        lego.pickheightreachslope = CBstate.AL5D_pickheightreachslope
        lego.park_reach = CBstate.AL5D_park_reach
        # Settings store GRIPPER-TIP heights; the module commands WRIST heights.
        lego.park_height = CBstate.AL5D_park_height + gl
        lego.cruise_margin = CBstate.AL5D_cruise_margin
        lego.grave_gap = CBstate.AL5D_grave_gap
        lego.extrawidth = CBstate.AL5D_extrawidth
        lego.CRUISE_HEIGHT = CBstate.AL5D_cruise_height + gl
        lego.calib_mode = CBstate.AL5D_calib_mode
        lego.frame_px = CBstate.AL5D_frame_px
        lego.frame_py = CBstate.AL5D_frame_py
        lego.frame_scale = CBstate.AL5D_frame_scale
        lego.frame_offset = CBstate.AL5D_frame_offset
        lego.frame_a0 = CBstate.AL5D_frame_a0
        lego.frame_sign = CBstate.AL5D_frame_sign
        # after the frame: the sag points' reaches are frame-derived
        lego.set_sag(CBstate.AL5D_sag_points)
        lego.set_corner_res(CBstate.AL5D_corner_res)
        ph = get("arm.piece_heights")
        if isinstance(ph, dict) and ph:
            lego.pieceheights = {k: float(v) for k, v in ph.items()}
        # the capture box drop point (2026-11-18): {"reach","turn","height"}
        # or None = drop captures on the graveyard grid as before
        cd = get("arm.capture_drop")
        lego.capture_drop = cd if isinstance(cd, dict) else None
        pg = get("arm.piece_grips")
        if isinstance(pg, dict) and pg:
            lego.piecegrips = {k: float(v) for k, v in pg.items()}
        pj = get("arm.piece_jaws")
        if isinstance(pj, dict) and pj:
            lego.piecejaws = {k: float(v) for k, v in pj.items()}
    ik = sys.modules.get("lib_al5_2D_IK")
    if ik:
        ik.A = CBstate.AL5D_A
        ik.B = CBstate.AL5D_B
    ensure_points()
    return load()


def ensure_points():
    """points.txt is device state, but it can go missing (e.g. an abandoned
    manual corner-click session used to delete it).  If absent, restore the
    factory defaults captured in defaults.json so nothing downstream dies."""
    import CBstate
    path = CBstate.mydir + "points.txt"
    if os.path.exists(path):
        return True
    pf = get("points_file")
    if not pf:
        return False
    flat = []
    for k in ("A1", "H1", "A8", "H8"):
        flat += ["%.6f" % pf[k][0], "%.6f" % pf[k][1]]
    try:
        os.makedirs(CBstate.mydir, exist_ok=True)
        with open(path, "w") as f:
            f.write(",".join(flat))
        print("settings: points.txt missing - restored factory defaults")
        return True
    except OSError:
        return False


def snapshot():
    """Raw USER overrides + points.txt content as a dict (for toolrun
    backup/restore of calibration state). Only the user layer is stored, so
    restoring never pins values that should keep tracking defaults.json."""
    s = {"settings": _read_json(USER_PATH)}
    try:
        import CBstate
        with open(CBstate.mydir + "points.txt") as f:
            s["points.txt"] = f.read()
    except OSError:
        pass
    return s


# Settings that identify the PHYSICAL board. A calibration Cancel restores the
# constants its screen changed (radscale/gfudge/pick…), but must NEVER roll back
# these — otherwise a stale snapshot silently reverts the square size / axis /
# piece heights you've set on the bench.
BOARD_KEEP_KEYS = ("squaresize", "axistorow0", "piece_heights",
                   "piece_grips", "piece_jaws", "board_height")


def restore(snap):
    """Undo a snapshot(): rewrite the raw user overrides, but keep the current
    values of the board-identity keys (BOARD_KEEP_KEYS) so a Cancel can't reset
    your square size / axis / piece heights. Re-applies, restores points.txt."""
    os.makedirs(os.path.dirname(USER_PATH), exist_ok=True)
    user = dict(snap.get("settings") or {})
    cur_arm = (_read_json(USER_PATH).get("arm") or {})
    arm = dict(user.get("arm") or {})
    for k in BOARD_KEEP_KEYS:
        if k in cur_arm:
            arm[k] = cur_arm[k]
    if arm:
        user["arm"] = arm
    if user:
        with open(USER_PATH, "w") as f:
            json.dump(user, f, indent=1)
    else:
        try:
            os.remove(USER_PATH)
        except OSError:
            pass
    if "points.txt" in snap:
        import CBstate
        with open(CBstate.mydir + "points.txt", "w") as f:
            f.write(snap["points.txt"])
    load(force=True)
    apply()


# keys exposed to the "Direct input" screen:
#   (dotted key, label, min, max, step, plain-language definition)
EDITABLE = [
    ("arm.gfudge", "Base angle offset (deg)", -30.0, 30.0, 0.1,
     "Rotates where the arm points around its base, in degrees. Positive "
     "Gfudge (+) swings the gripper toward the a-file; negative Gfudge "
     "(\u2212) toward the h-file. Set by Arm grab / Board calibration."),
    ("arm.radscale", "Reach scale (radscale)", 0.3, 2.0, 0.01,
     "Scales how far the arm extends for every square. Too big overshoots "
     "the target (lands further from the base), too small undershoots. "
     "Set by Arm calibration."),
    ("arm.stickout", "Reach intercept (in)", -8.0, 8.0, 0.1,
     "A constant added to every reach distance before scaling - shifts all "
     "squares nearer or further together. Set by Arm calibration."),
    ("arm.pickheightreachslope", "Pick height reach slope (in/in)", -0.3, 0.3, 0.005,
     "Retired in favour of the arm-sag curve (2026-10-21) and no longer part "
     "of the grip model (2026-11-06); kept for tooling only."),
    ("arm.cruise_height", "Cruise height (in)", 1.0, 5.0, 0.1,
     "How high the gripper TIP sits above the board while travelling between "
     "squares (inches), for both games and calibration. Must clear the tallest "
     "standing piece or it knocks it; lower it only if the far rank can't be "
     "reached at this height."),
    ("arm.extrawidth", "Gripper jaws adjust", -20.0, 60.0, 1.0,
     "Controls the jaw-opening width (servo degrees). Lower values open the "
     "jaws wider. Decrease if the jaws pinch the sides or knock the piece "
     "over; increase if they fail to grip."),
    ("arm.wrist_offset", "Wrist tilt offset (deg)", -30.0, 30.0, 0.5,
     "Fine adjustment of the gripper's tilt. Nudges the wrist a few degrees "
     "until the gripper hangs straight down (perpendicular to the board) at "
     "every reach - check near AND far squares."),
    ("arm.park_reach", "Park reach (in)", 5.0, 11.0, 0.1,
     "How far the arm extends at the park/rest pose - where it settles when "
     "the servos release, and what it wakes into on the next move. A larger "
     "reach keeps the arm's mass in front of the shoulder so it cannot "
     "collapse backwards."),
    ("arm.frame_px", "Frame pivot X (squares)", -16.0, 16.0, 0.05,
     "Mode B: the arm pivot's X in board units (a-file = 0). Set by the "
     "corner calibration; edit only if you know a full re-fit is needed."),
    ("arm.frame_py", "Frame pivot Y (squares)", -16.0, 16.0, 0.05,
     "Mode B: the arm pivot's Y in board units (rank-8 = 0, behind the board "
     "is negative). Set by the corner calibration."),
    ("arm.frame_scale", "Frame reach scale (in/sq)", 0.2, 3.0, 0.01,
     "Mode B: inches of reach per board square. Set by the corner calibration."),
    ("arm.frame_offset", "Frame reach offset (in)", -4.0, 8.0, 0.05,
     "Mode B: constant reach offset in inches. Set by the corner calibration."),
    ("arm.frame_a0", "Frame angle offset (deg)", -180.0, 180.0, 0.1,
     "Mode B: base-angle offset in degrees. Set by the corner calibration."),
]

# Hidden from the Calibration-settings list - the setting still exists and the
# calibrations that own it still write it, it just stops taking up a row.
#   pickheightreachslope: retired in favour of the arm-sag curve (2026-10-21)
#   frame_*: written by the corner calibration; the manual rows were the whole
#            right-hand column and only invited hand-editing a fitted frame
HIDDEN = ("arm.pickheightreachslope", "arm.frame_px", "arm.frame_py",
          "arm.frame_scale", "arm.frame_offset", "arm.frame_a0")

# Which calibration mode(s) each setting is visible in. "A" = parametric
# (gfudge/radscale/stickout/board size), "B" = measured corner frame, "both".
MODE_OF = {
    "arm.gfudge": "A", "arm.radscale": "A", "arm.stickout": "A",
    "arm.pickheightadj": "both", "arm.pickheightreachslope": "both",
    "arm.cruise_height": "both", "arm.extrawidth": "both",
    "arm.wrist_offset": "both",
    "arm.frame_px": "B", "arm.frame_py": "B", "arm.frame_scale": "B",
    "arm.frame_offset": "B", "arm.frame_a0": "B",
    "arm.axistorow0": "A", "arm.squaresize": "A", "arm.grave_gap": "both",
}


def visible_in_mode(key, mode):
    return MODE_OF.get(key, "both") in ("both", mode)

# Board-identity settings moved to their own "Board size" screen so the main
# Calibration-settings list stays short enough to hit. (cruise_margin and
# flip_rank are deliberately NOT exposed: the former stays fixed, the latter
# is auto-set by Arm calibration.)
BOARD_EDITABLE = [
    ("arm.axistorow0", "Axis to rank-8 row (in)", 1.0, 8.0, 0.05,
     "Distance from the arm's base pivot axis to the centreline of the "
     "board's rank-8 row. Measure it with a ruler; too big reaches far, "
     "too small reaches short."),
    ("arm.squaresize", "Square size (in)", 1.0, 2.0, 0.01,
     "Width of one board square in inches. Affects every computed square "
     "location; measure with a ruler before changing."),
    ("arm.grave_gap", "Graveyard gap (in)", 0.25, 3.0, 0.05,
     "How far the captured-piece grid sits out from the a-file edge, in inches. "
     "Increase if the arm crowds the board edge dropping pieces there; decrease "
     "if the far graveyard slots fall out of reach."),
]
