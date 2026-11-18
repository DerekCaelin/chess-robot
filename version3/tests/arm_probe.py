# tests/arm_probe.py — hover-and-click pointing calibration for the arm.
#
# The camera is the measuring instrument: the board homography maps any pixel
# to exact board coordinates, so "where did the gripper actually end up" is a
# mouse click, not an eyeball estimate.
#
# Modes
#   probe   (default)  hover over each square; you click the gripper tip on
#                       the warped, gridded camera image; records probes.json
#       python tests/arm_probe.py [--squares "d4 e5 ..."] [--out FILE]
#   fit     (no arm)   regress probe records -> suggested CBstate constants
#       python tests/arm_probe.py --fit [FILE]
#   verify hover again with CURRENT constants and report per-square error
#       python tests/arm_probe.py --verify [FILE]
#
# Model fitted (two affine channels, closed form):
#   radial: measured planar radius  hm = a*(hc + stickout) + b
#           -> new radscale = radscale/a,  new stickout = a*stickout - b
#           (commands built with those constants land exactly on target under
#           this model — non-linearity is what --verify then checks)
#   angle:  measured angle = commanded angle + dth
#           -> new Gfudge = Gfudge - dth
#
# Safety: arm moves one joint group at a time via the normal movearm()
#         sequence (rise, extend, rotate); Ctrl-C parks/idles.
# NOTE: closes cleanly only if no other program (CBint!) holds the camera.

import argparse
import json
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import CBstate  # noqa: E402
import camera  # noqa: E402
import piece_detect  # noqa: E402
import LEGO_Chess_rpd as RD  # noqa: E402

PROBE_SQUARES = ["d4", "f4", "e5", "c6", "g6", "b7", "d7", "f7", "a5",
                 "h5", "a7", "h7"]
DEFAULT_OUT = os.path.join(ROOT, "images", "probes.json")
TMP_CAPTURE = "/tmp/opencode/arm_probe.jpg"
PARK_TARGET = (5.5, 3.0, 10.0, 42 + RD.extrawidth, -90, 90)
LAND_CLEARANCE = 0.5   # extra inches above the true PICK posture used while
                       # hovering for a tap.  The descent height is now
                       # ph*cmtoin + pickheightadj + adjforlen(X): the old
                       # "extraheight" mechanical constant is folded into
                       # pickheightadj by settings.apply(), so it no longer
                       # appears here.  With this clearance the open tips
                       # hover ~0.5" off the surface at default; --sink closes
                       # the rest.


def land_for_hover(sq, sink=0.0):
    """Slow descent from cruise to the real pick posture over sq (same height
    pickuppiece() uses over an empty square).  --sink fine-tunes; hard floor
    guards the board."""
    X = RD.armlength(sq)
    y = RD.grip_y("p", X) + LAND_CLEARANCE - sink
    RD.moveXY(max(y, 2.5))


class Abort(Exception):
    pass


# ── coordinate helpers ───────────────────────────────────────────────────────

def model_sq(sq, flip_rank=None, flip_file=None):
    """Square name in the ARM-MODEL convention (axis beside rank 8, x
    positive towards a).  Board labelling can arrive in any of 4
    orientations; the flips are auto-detected by --fit / Arm calibration."""
    fr = CBstate.AL5D_flip_rank if flip_rank is None else flip_rank
    ff = CBstate.AL5D_flip_file if flip_file is None else flip_file
    f = "abcdefgh".index(sq[0])
    r = int(sq[1])
    if ff:
        f = 7 - f
    if fr:
        r = 9 - r
    return "abcdefgh"[f] + str(r)


def _planar_of(sq):
    f = "abcdefgh".index(sq[0])
    r = int(sq[1])
    return ((3.5 - f) * CBstate.AL5D_squaresize,
            (8 - r) * CBstate.AL5D_squaresize)


def sq_planar(sq):
    """Model planar coords (inches) of a square centre: x lateral (+a side),
    y depth from the rank-8 centreline (axis sits at y = -axistorow0).
    'sq' is the LABELED square; the model convention is applied first."""
    return _planar_of(model_sq(sq))


def click_planar(gu, gv):
    """Tap position in continuous warped board units 0..8 (cell f spans
    [f, f+1) from the a-file; cell of rank r spans [8-r, 9-r) from the top)
    -> model planar inches. A tap at a cell CENTRE (f+0.5, 8-r+0.5) must
    return exactly _planar_of(that square) - until 2026-09-18 this assumed
    integer coords were centres, biasing EVERY tap by (-0.75", +0.75"),
    inflating apparent errors ~1.06" and driving fits to chase it."""
    s = CBstate.AL5D_squaresize
    return ((4.0 - gu) * s, (gv - 0.5) * s)


def polar(x, y):
    ax = CBstate.AL5D_axistorow0
    return math.hypot(x, y + ax), math.degrees(math.atan2(x, y + ax))


# ── fit (pure math, unit-testable) ────────────────────────────────────────────

def max_reach_in():
    return CBstate.AL5D_A + CBstate.AL5D_B


def proposed_out_of_reach(radscale, stickout, squares, height=3.0,
                          margin=0.05):
    """Probe squares whose work radius leaves the reach annulus under the
    PROPOSED constants, tested at a realistic hover height (not the old 6" cruise
    that even a pickable far square cannot reach on a 1.5" board). A fit that
    cannot command its own test squares is built on bad data - never apply it
    (UI refuses; CLI warns)."""
    bad = []
    limit = max_reach_in() - margin
    for sq in squares:
        f = "abcdefgh".index(sq[0])
        r = int(sq[1])
        x = (3.5 - f) * CBstate.AL5D_squaresize
        planar = math.hypot(x, (8 - r) * CBstate.AL5D_squaresize
                            + CBstate.AL5D_axistorow0)
        L = (planar + stickout) * radscale
        if math.hypot(L, height) > limit:
            bad.append(sq)
    return bad


def best_flips(recs):
    """Mean pre-fit pointing error under each of the 4 board-labelling
    orientations.  Returns (flip_rank, flip_file, best_err, now_err).
    Wrong orientation puts every tap two board-diagonals from its square,
    so the right one typically wins by 3x+."""
    errs = {}
    for fr in (False, True):
        for ff in (False, True):
            acc = []
            for rec in recs:
                xc, yc = _planar_of(model_sq(rec["square"], fr, ff))
                xm, ym = click_planar(rec["gu"], rec["gv"])
                acc.append(math.hypot(xm - xc, ym - yc))
            errs[(fr, ff)] = sum(acc) / len(acc)
    (fr, ff), best = min(errs.items(), key=lambda kv: kv[1])
    now = errs[(CBstate.AL5D_flip_rank, CBstate.AL5D_flip_file)]
    return fr, ff, best, now


def fit_constants(recs):
    pts = []
    for rec in recs:
        xc, yc = sq_planar(rec["square"])
        xm, ym = click_planar(rec["gu"], rec["gv"])
        hc, thc = polar(xc, yc)
        hm, thm = polar(xm, ym)
        pts.append((rec["square"], hc, hm, thc, thm,
                    math.hypot(xm - xc, ym - yc)))

    X = np.array([p[1] + CBstate.AL5D_stickout for p in pts])
    Y = np.array([p[2] for p in pts])
    M = np.vstack([X, np.ones_like(X)]).T
    (a, b), res, *_ = np.linalg.lstsq(M, Y, rcond=None)
    dof = max(1, len(X) - 2)
    s2 = float(res[0] / dof) if len(res) else 0.0
    try:
        cov = s2 * np.linalg.inv(M.T @ M)
        se_a, se_b = float(np.sqrt(cov[0][0])), float(np.sqrt(cov[1][1]))
    except np.linalg.LinAlgError:
        se_a = se_b = 0.0
    dth = float(np.mean([p[4] - p[3] for p in pts]))
    # Regression is tip vs U=(planar + stickout_old).  PERFECT current
    # constants would give tip = planar = U - stickout_old exactly: slope 1,
    # intercept -stickout_old.  So: converged = both fit coefficients within
    # 2 standard errors of those values; unstable = the slope correction
    # being requested is itself inside the noise band (chasing it oscillates
    # - which is what happened 2026-09-18 across three consecutive fits).
    unstable = se_a > abs(a - 1.0) / 2.0
    converged = (abs(a - 1.0) < 2 * se_a + 1e-9
                 and abs(b + CBstate.AL5D_stickout) < 2 * se_b + 1e-9)
    # Physical response learned from the taps:  tip = a*(planar + s_old) + b
    # (X = (planar+stickout)*radscale commands, so a encodes radscale_old and
    # b is the exact tip radius commanded at zero work radius).  The NEW
    # constants must make tip(planar) == planar for EVERY square:
    #   A*r_new = 1   ->   r_new = r_old / a          (slope part, as before)
    #   A*r_new*s_new + B = 0   ->   s_new = -b       (intercept part)
    # The old code used s_new = a*s_old - b, which preserves a constant
    # radial offset a*s_old: every 2026-09 fit could fix scale but never the
    # ~3in bias - which is why stickout drifted upward each round.
    return {
        "per_square": [(p[0], round(p[5], 3)) for p in pts],
        "radial_a": float(a), "radial_b": float(b), "angle_dth_deg": dth,
        "new_radscale": CBstate.AL5D_radscale / float(a),
        "new_stickout": -float(b),
        "new_gfudge": CBstate.AL5D_Gfudge - dth,
        "se_a": se_a, "se_b": se_b, "unstable": unstable,
        "converged": converged,
        "scatter_in": float(np.sqrt(s2)),
    }


def _grab_pts(samples):
    """[(square, X, Z, Y, planar_hc, planar_angle_deg, weight), ...]."""
    pts = []
    for s in samples:
        x, y = sq_planar(s["square"])
        hc, atandeg = polar(x, y)
        pts.append((s["square"], float(s["X"]), float(s["Z"]), float(s["Y"]),
                    hc, atandeg, float(s.get("w", 1.0))))
    return pts


def fit_angle(samples):
    """gfudge = mean of (jogged base Z - model angle). Independent of reach.
    Returns a float, or None with no samples."""
    pts = _grab_pts(samples)
    if not pts:
        return None
    return float(np.mean([p[2] - 90.0 - p[5] for p in pts]))


def fit_radial(samples):
    """Solve (radscale, stickout) from reach X vs planar radius hc. Needs >=2
    distinct radii. Returns (radscale, stickout) or None."""
    pts = _grab_pts(samples)
    if len(pts) < 2:
        return None
    hc = np.array([p[4] for p in pts], float)
    Xr = np.array([p[1] for p in pts], float)
    if float(hc.max() - hc.min()) < 1e-6:
        return None
    # X = radscale*hc + radscale*stickout
    M = np.vstack([hc, np.ones_like(hc)]).T
    (radscale, intercept), *_ = np.linalg.lstsq(M, Xr, rcond=None)
    if radscale <= 1e-6:
        return None
    return float(radscale), float(intercept / radscale)


def fit_height(samples):
    """Solve (pickheightadj, pickheightreachslope) from grab height Y vs reach X,
    WEIGHTED so a deliberate verify correction dominates. A near + far reach
    resolves the slope exactly; one reach -> slope 0, constant = weighted mean.
    Descent model: Y = piece_h + gripper_len + pickheightadj + pickheightreachslope*X.
    Returns (pickheightadj, pickheightreachslope) or None."""
    pts = _grab_pts(samples)
    if not pts:
        return None
    Xr = np.array([p[1] for p in pts], float)
    Yarr = np.array([p[3] for p in pts], float)
    W = np.array([p[6] for p in pts], float)
    sw = np.sqrt(W)
    ph_in = RD.pieceheights["p"]
    gl = RD.gripper_len
    if float(Xr.max() - Xr.min()) > 1e-6:
        A2 = np.vstack([Xr, np.ones_like(Xr)]).T * sw[:, None]
        (slope, intercept), *_ = np.linalg.lstsq(A2, Yarr * sw, rcond=None)
    else:
        slope = 0.0
        intercept = float(np.sum(Yarr * W) / np.sum(W))
    # intercept = piece_h + gripper_len + pickheightadj  ->  adj = intercept - ph - gl
    return float(intercept - ph_in - gl), float(slope)


def fit_grab(samples):
    """Direct closed-form fit of the pointing + pick-height constants from a
    list of grab samples: {"square","X"(reach in),"Z"(base deg),"Y"(height in),
    optional "w"}. Composes the three phase solvers (angle/radial/height); used
    by the verify corrections on the whole accumulated set. extraheight is
    folded into pickheightadj by settings.apply() (0 here)."""
    pts = _grab_pts(samples)
    if len(pts) < 2:
        return {"ok": False, "reason": "need at least 2 grab samples"}
    rs = fit_radial(samples)
    if rs is None:
        return {"ok": False, "reason": "samples all at one radius - cannot "
                "separate radscale and stickout"}
    radscale, stickout = rs
    gfudge = fit_angle(samples)
    pickheightadj, ph_slope = fit_height(samples)
    per_test = []
    for sq, X, Z, Y, hcx, ad, _w in pts:
        pre_rad = (hcx + CBstate.AL5D_stickout) * CBstate.AL5D_radscale - X
        post_rad = (hcx + stickout) * radscale - X
        pre_ang = (CBstate.AL5D_Gfudge + 90.0 + ad) - Z
        per_test.append((sq, round(pre_rad, 3), round(post_rad, 3),
                         round(pre_ang, 2)))
    post = np.array([(p[4] + stickout) * radscale - p[1] for p in pts])
    return {
        "ok": True, "per_test": per_test,
        "new_radscale": float(radscale), "new_stickout": stickout,
        "new_gfudge": gfudge, "new_pickheightadj": pickheightadj,
        "new_pickheightreachslope": float(ph_slope),
        "scatter_in": float(np.sqrt(np.mean(post ** 2))),
    }


def corner_units(sq):
    """Board-unit centre of a square: (bx, by), a-file=0..h-file=8, rank-8=0.
    Square CENTRES are at n+0.5. Mode B measures the raw labelled corners."""
    f = "abcdefgh".index(sq[0].lower())
    r = int(sq[1])
    return f + 0.5, (8 - r) + 0.5


# Residual tolerances for the joint corner fit. The optimizer minimizes the
# residuals normalized by these, and the fit is REJECTED when either RMS
# exceeds its tolerance (the angle residual was once computed and thrown away:
# a frame could pass with a garbage base-angle offset).
CORNER_REACH_TOL = 1.0     # in
CORNER_ANG_TOL = 6.0       # deg


def fit_corner_frame(records):
    """Solve the mode-B corner frame from 4 measured corners.

    records: [{"square": "a8"|"h8"|"h1"|"a1", "X": reach (in), "Z": base angle
               (deg)}, ...].
    Model (board units):  reach = scale*hypot((bx,by)-(px,py)) + offset
                          angle = a0 - sign*atan2(bx-px, by-py)*rtod
    sign (±1) absorbs a MIRRORED labelling - chirality no pivot/rotation can.

    BOTH the reach and the angle measurements are fitted JOINTLY. The radial
    model alone is degenerate for four square corners (a whole family of
    pivots fits the radii equally well), so the base angles are what pins the
    pivot and a0. Fitting radii only let the solver slide along that family
    until the bounds stopped it: with the pivot bounds added 2026-10-14 the
    slide ended clipped at 'pivot behind rank-8' and the fit failed with ~3 in
    of phantom residual no matter where the corners were measured (2026-10-20).
    The pivot may now sit on ANY side of the board (the guard against the
    |px| ~ 1e8 runaway stays).

    Returns {ok, px, py, scale, offset, a0, sign, residual_in,
             ang_residual_deg, reach_res, ang_res}; on failure the same
    diagnostics are returned alongside {ok: False, reason} so the caller can
    say WHICH corner disagrees."""
    if len(records) < 4:
        return {"ok": False, "reason": "need all four corners"}
    bx = np.array([corner_units(r["square"])[0] for r in records], float)
    by = np.array([corner_units(r["square"])[1] for r in records], float)
    R = np.array([float(r["X"]) for r in records])
    A = np.array([float(r["Z"]) for r in records])

    def resid(p, sign):
        px, py, m, c, a0 = p
        d = np.hypot(bx - px, by - py)
        th = np.degrees(np.arctan2(bx - px, by - py))
        ang = (A - (a0 - sign * th) + 180.0) % 360.0 - 180.0   # wrap (-180,180]
        return np.concatenate([(m * d + c - R) / CORNER_REACH_TOL,
                               ang / CORNER_ANG_TOL])

    # Bounds: pivot anywhere near the board, scale/offset in a sane physical
    # range. Without these the Gauss-Newton step can run away on noisy samples
    # and write a frame with |px| ~ 1e8 (2026-10-14).
    lo = np.array([-30.0, -30.0, 0.1, -10.0, -720.0])
    hi = np.array([30.0, 30.0, 5.0, 10.0, 720.0])
    # Multi-start: the parametric model's own pivot and its mirrors (the arm
    # may sit beside ANY edge of the labelled board), plus the board centre.
    off = CBstate.AL5D_axistorow0 / max(CBstate.AL5D_squaresize, 1e-6)
    pivots = [(4.0, 0.5 - off), (4.0, 7.5 + off), (0.5 - off, 4.0),
              (7.5 + off, 4.0), (4.0, 4.0)]
    m0 = float(CBstate.AL5D_radscale * CBstate.AL5D_squaresize)
    c0 = float(CBstate.AL5D_radscale * CBstate.AL5D_stickout)
    STEP_MAX = 2.0                       # cap per-iteration change (damping)
    best = None
    for sign in (1, -1):
        for (sx, sy) in pivots:
            th0 = np.degrees(np.arctan2(bx - sx, by - sy))
            p = np.array([sx, sy, m0, c0, float(np.mean(A + sign * th0))])
            for _ in range(300):
                r = resid(p, sign)
                J = np.zeros((len(r), 5))
                h = 1e-6
                for j in range(5):
                    q = p.copy(); q[j] += h
                    J[:, j] = (resid(q, sign) - r) / h
                try:
                    step, *_ = np.linalg.lstsq(J, -r, rcond=None)
                except np.linalg.LinAlgError:
                    break
                ms = float(np.max(np.abs(step)))
                if ms > STEP_MAX:
                    step = step * (STEP_MAX / ms)     # damp the step
                p = np.clip(p + step, lo, hi)         # stay in the plausible box
                if float(np.max(np.abs(step))) < 1e-10:
                    break
            cost = float(np.sum(resid(p, sign) ** 2))
            if best is None or cost < best[0]:
                best = (cost, sign, p.copy())
    _cost, sign, p = best
    px, py, m, c, a0 = (float(v) for v in p)
    if m <= 1e-6:
        return {"ok": False, "reason": "implausible frame (positive scale "
                "required)"}
    d = np.hypot(bx - px, by - py)
    reach_res = m * d + c - R
    th = np.degrees(np.arctan2(bx - px, by - py))
    ang_res = (A - (a0 - sign * th) + 180.0) % 360.0 - 180.0
    residual_in = float(np.sqrt(np.mean(reach_res ** 2)))
    ang_residual_deg = float(np.sqrt(np.mean(ang_res ** 2)))
    out = {"px": px, "py": py, "scale": m, "offset": c,
           "a0": (a0 + 180.0) % 360.0 - 180.0, "sign": int(sign),
           "residual_in": residual_in, "ang_residual_deg": ang_residual_deg,
           "reach_res": [round(float(x), 3) for x in reach_res],
           "ang_res": [round(float(x), 2) for x in ang_res]}
    if residual_in > CORNER_REACH_TOL:
        out.update(ok=False, reason="fit residual %.2f in too high - "
                   "re-check the corner measurements" % residual_in)
        return out
    if ang_residual_deg > CORNER_ANG_TOL:
        out.update(ok=False, reason="fit angle residual %.1f deg too high - "
                   "re-check the corner measurements" % ang_residual_deg)
        return out
    out["ok"] = True
    return out


def frame_predict(frame, square, corr=None):
    """(reach, base-angle) the mode-B frame maps `square` to: the exact math
    planar_to_arm() runs at game time, kept pure so the corner screen can show
    model-vs-measured (the round trip) without touching live settings. `corr`
    = the per-corner corrections the runtime adds back - pass them to predict
    exactly what will be commanded."""
    bx, by = corner_units(square)
    dx = bx - float(frame["px"])
    dy = by - float(frame["py"])
    sign = int(frame.get("sign", 1))
    reach = (float(frame["scale"]) * float(np.hypot(dx, dy))
             + float(frame["offset"]))
    angle = float(frame["a0"]) - sign * float(np.degrees(np.arctan2(dx, dy)))
    if corr:
        dr, da = corr.get(str(square).lower(), (0.0, 0.0))
        reach += float(dr)
        angle += float(da)
    return reach, angle


def fit_report_lines(recs, fit, source=None, orient_note=None,
                     reach_bad=None):
    """Canonical plain-text fit report - CLI prints it, the UI copies it."""
    lines = []
    if source:
        lines.append(source)
    if orient_note:
        lines.append(orient_note)
    lines.append("per-square pointing error BEFORE fit (inches):")
    for i, (sq, e) in enumerate(fit["per_square"]):
        lines.append("  %2d %-4s %.2f" % (i + 1, sq, e))
    lines.append("radial model:   measured = %.4f x (planar + stickout) "
                 "%+.3f" % (fit["radial_a"], fit["radial_b"]))
    lines.append("angle offset:   %+.2f deg" % fit["angle_dth_deg"])
    lines.append("suggested:      radscale=%.4f  stickout=%.3f  "
                 "gfudge=%.3f" % (fit["new_radscale"], fit["new_stickout"],
                                  fit["new_gfudge"]))
    lines.append("fit quality:    tap scatter %.2f in; slope %.3f +/- %.3f; "
                 "intercept %.3f +/- %.3f"
                 % (fit.get("scatter_in", 0), fit["radial_a"],
                    fit.get("se_a", 0), fit["radial_b"],
                    fit.get("se_b", 0)))
    if fit.get("converged"):
        lines.append("VERDICT: converged - current constants are already "
                     "within the tap noise. Apply is a no-op; stop here "
                     "and bake.")
    elif fit.get("unstable"):
        lines.append("VERDICT: fit INCONCLUSIVE - the requested correction "
                     "is smaller than the fit's own uncertainty. Keep "
                     "current constants; walk again if you disagree.")
    if reach_bad:
        lines.append("WARNING: suggested constants leave %s beyond the "
                     "arm's %.1f in reach - probe data suspect (wrong "
                     "taps/skips?). Do NOT bake these."
                     % (", ".join(reach_bad), max_reach_in()))
    return lines


def print_fit(f):
    print("\nper-square pointing error BEFORE fit (inches):")
    for i, (sq, e) in enumerate(f["per_square"]):
        print(" %2d %-4s %.2f" % (i + 1, sq, e))
    print("radial model:   measured = %.4f x (planar + stickout) %+.3f"
          % (f["radial_a"], f["radial_b"]))
    print("angle offset:   %+.2f deg" % f["angle_dth_deg"])
    print("\nSuggested CBstate values:")
    print("  AL5D_radscale    = %.4f   (was %.4f)"
          % (f["new_radscale"], CBstate.AL5D_radscale))
    print("  AL5D_stickout    = %.3f    (was %.3f)"
          % (f["new_stickout"], CBstate.AL5D_stickout))
    print("  AL5D_Gfudge      = %.3f    (was %.3f)"
          % (f["new_gfudge"], CBstate.AL5D_Gfudge))


# ── GUI ──────────────────────────────────────────────────────────────────────

def _overlay(warp, d, sq):
    img = warp.copy()
    step = d / 8.0
    for i in range(1, 8):
        cv2.line(img, (int(i * step), 0), (int(i * step), d), (255, 255, 0), 1)
        cv2.line(img, (0, int(i * step)), (d, int(i * step)), (255, 255, 0), 1)
    for i, ch in enumerate("abcdefgh"):
        cv2.putText(img, ch, (int((i + 0.3) * step), int(step * 0.6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
    for i in range(8):
        cv2.putText(img, str(8 - i), (int(0.15 * step), int((i + 0.6) * step)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
    f = "abcdefgh".index(sq[0])
    r = int(sq[1])
    p1 = (int(f * step), int((8 - r) * step))
    p2 = (int((f + 1) * step), int((9 - r) * step))
    cv2.rectangle(img, p1, p2, (0, 0, 255), 2)
    return img


def probe_one(warp, d, sq):
    """Block until user clicks+confirms (returns gu,gv) or skips (None)."""
    click = [None]
    win = "probe %s" % sq
    state = [None]          # live display scale

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            s = state[0] or 1.0
            click[0] = (x / s, y / s)      # un-scale to warp pixels

    def draw():
        img = _overlay(warp, d, sq)
        if click[0] is not None:
            cx, cy = int(click[0][0]), int(click[0][1])
            cv2.circle(img, (cx, cy), 6, (0, 255, 0), 2)
            cv2.drawMarker(img, (cx, cy), (0, 255, 0), cv2.MARKER_CROSS, 16, 1)
        disp, sc = _scale(img)
        state[0] = sc
        return disp

    cv2.namedWindow(win, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(win, on_mouse)
    print("  gripper landing over %s: click the point where its TIP touches"
          " the board, then 's'. Enter=skip, q=quit." % sq, flush=True)
    while True:
        cv2.imshow(win, draw())
        k = cv2.waitKey(30) & 0xFF
        if k == ord('s') and click[0] is not None:
            u, v = click[0]
            return (u / d * 8.0, v / d * 8.0)
        elif k == 13:
            return None
        elif k in (ord('q'), ord('x'), 27):
            raise Abort()


def _scale(img):
    h, w = img.shape[:2]
    mw, mh = CBstate.displaymax
    s = min(mw / w, mh / h, 1.0)
    if s >= 1.0:
        return img, 1.0
    return cv2.resize(img, (int(w * s), int(h * s)),
                      interpolation=cv2.INTER_AREA), s


# ── arm session ──────────────────────────────────────────────────────────────

def capture_warped(H, d):
    if not camera.capture_still(TMP_CAPTURE):
        raise Abort()
    img = cv2.imread(TMP_CAPTURE)
    if img is None:
        raise Abort()
    return cv2.warpPerspective(img, H, (d + 1, d + 1))


def run_session(squares, out_path, verify_recs=None, sink=0.0, cruise=6.0):
    # The board is EMPTY during probing, so the 8" clearance cruise is wasted
    # (and near-full-extension at 8" browns out the supply: elbow+wrist at
    # max torque).  Cruise lower; movearm()/park() honour this module value.
    RD.CRUISE_HEIGHT = cruise
    if not os.path.isdir(os.path.dirname(TMP_CAPTURE)):
        os.makedirs(os.path.dirname(TMP_CAPTURE), exist_ok=True)
    if not camera.capture_still(TMP_CAPTURE) or cv2.imread(TMP_CAPTURE) is None:
        print("Camera unavailable — is CBint or live_calibrate still running? "
              "Only one program can hold the webcam at a time.")
        return 1

    H, d = piece_detect.load_homography()
    RD.sp = RD._resolve_port()
    if RD.sp is None:
        print("SSC-32U not found/powered.")
        return 1

    results = []
    try:
        try:
            RD.sp.write(b"ES\r")
            RD.sp.flush()
        except Exception as e:
            print("serial write failed:", e)
            return 1
        time.sleep(0.2)
        RD.apply_po_offsets()          # same horn trims the game uses
        time.sleep(0.2)
        RD.STRICT_IK = True           # never the silent all-middle fallback
        RD.energise()
        RD.targetXYZGWAWR = PARK_TARGET
        RD.openjaws()                  # open tips straddle the contact point
        for sq in squares:
            print("moving over %s ..." % sq, flush=True)
            try:
                RD.movearm(RD.armlength(sq), RD.armangle(sq))
                land_for_hover(sq, sink)
            except RD.IKUnreachable as e:
                print("  %s UNREACHABLE (%s) - skipped" % (sq, e))
                continue
            time.sleep(1.6)           # arm motion is async: the photo must
                                      # not be taken mid-flight (stale frame)
            warp = capture_warped(H, d)
            guv = probe_one(warp, d, sq)
            if guv is None:
                print("  %s skipped" % sq)
                continue
            results.append({"square": sq, "gu": round(guv[0], 4),
                            "gv": round(guv[1], 4)})
            if verify_recs is not None:
                xc, yc = sq_planar(sq)
                xm, ym = click_planar(*guv)
                err = math.hypot(xm - xc, ym - yc)
                results[-1]["err_in"] = round(err, 3)
                print("  %s error %.2f in" % (sq, err), flush=True)
    except Abort:
        print("aborted — parking.")
    finally:
        RD.STRICT_IK = False
        try:
            RD.park()
        finally:
            cv2.destroyAllWindows()
            try:
                RD.idleall()
            except Exception:
                pass
            RD.sp.close()
            RD.sp = None

    if verify_recs is not None:
        errs = [r["err_in"] for r in results if "err_in" in r]
        worst = max(errs) if errs else 99
        print("\nVERIFY: %d squares, worst %.2f in, mean %.2f in  -> %s" % (
            len(errs), worst, sum(errs) / len(errs) if errs else -1,
            "PASS (<=0.20 in)" if worst <= 0.20 else
            "BORDERLINE" if worst <= 0.35 else "FAIL"))
        return 0 if worst <= 0.20 else 1
    if results:
        with open(out_path, "w") as fp:
            json.dump({"fp": _cur_fp(), "ts": time.strftime("%Y-%m-%d %H:%M"),
                       "consts": consts_snapshot(), "recs": results},
                      fp, indent=1)
        print("\nrecorded %d probes -> %s\nnow: python tests/arm_probe.py --fit"
              % (len(results), out_path))
    return 0


# ── entry ────────────────────────────────────────────────────────────────────

def consts_snapshot():
    """The arm-model constants IN EFFECT when a probe set is recorded.
    A recording + these is everything needed to re-derive intended coords,
    command radii and refit offline any number of times."""
    return {"radscale": CBstate.AL5D_radscale,
            "stickout": CBstate.AL5D_stickout,
            "gfudge": CBstate.AL5D_Gfudge,
            "axistorow0": CBstate.AL5D_axistorow0,
            "squaresize": CBstate.AL5D_squaresize,
            "flip_rank": int(bool(CBstate.AL5D_flip_rank)),
            "flip_file": int(bool(CBstate.AL5D_flip_file))}


def _cur_fp():
    """md5 of points.txt - ties a probe recording to a board calibration."""
    import hashlib
    try:
        with open(CBstate.mydir + "points.txt", "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError:
        return None


def _load_recs(path):
    """-> (recs, fp, data) where data is the raw json dict (None=legacy)."""
    with open(path) as fp:
        data = json.load(fp)
    if isinstance(data, dict):
        return data.get("recs", []), data.get("fp"), data
    return data, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--squares", default=" ".join(PROBE_SQUARES))
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--sink", type=float, default=1.0,
                    help="extra inches to descend toward the board (negative"
                         " = stay higher); 0.4-inch increments are plenty to"
                         " dial the tip into near-contact view")
    ap.add_argument("--cruise", type=float, default=6.0,
                    help="travel height in inches while moving between probe"
                         " squares (lower = less arm torque; board is empty)")
    ap.add_argument("--fit", nargs="?", const=DEFAULT_OUT, default=None,
                    metavar="FILE")
    ap.add_argument("--verify", nargs="?", const=DEFAULT_OUT, default=None,
                    metavar="FILE")
    ap.add_argument("--bake", action="store_true",
                    help="write CURRENT fitted arm settings + points.txt into"
                         " config/defaults.json (the golden defaults)")
    args = ap.parse_args()

    from chessapp import settings
    settings.apply()

    if args.bake:
        import json as _json
        vals = {k: settings.get("arm." + k,
                                getattr(CBstate, settings.ARM_TO_CBSTATE[k]))
                for k in ("radscale", "stickout", "gfudge", "axistorow0",
                          "squaresize", "A", "B", "pickheightadj",
                          "pickheightreachslope", "extrawidth", "flip_rank",
                          "flip_file")}
        dpath = os.path.join(ROOT, "config", "defaults.json")
        with open(dpath) as f:
            doc = _json.load(f)
        for k, v in vals.items():
            doc.setdefault("arm", {})[k] = round(float(v), 4) \
                if isinstance(v, float) else v
        vals2 = None
        try:
            with open(CBstate.mydir + "points.txt") as f:
                vals2 = [float(x) for x in f.read().rstrip().split(",")]
        except OSError:
            pass
        if vals2 and len(vals2) == 8:
            doc["points_file"] = {
                "A1": vals2[0:2], "H1": vals2[2:4],
                "A8": vals2[4:6], "H8": vals2[6:8]}
        doc["_comment"] = ("GOLDEN defaults - baked %s by arm_probe --bake "
                           "after confirmed arm calibration."
                           % time.strftime("%Y-%m-%d %H:%M"))
        tmp = dpath + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(doc, f, indent=4)
            f.write("\n")
        os.replace(tmp, dpath)
        print("baked arm.* + points_file into config/defaults.json")
        return 0

    if args.fit:
        recs, fp_saved, data = _load_recs(args.fit)
        age = time.strftime("%Y-%m-%d %H:%M",
                            time.localtime(os.path.getmtime(args.fit)))
        cur = _cur_fp()
        if fp_saved is None:
            fp_state = "board-calibration: UNKNOWN (legacy recording)"
        elif fp_saved == cur:
            fp_state = "board-calibration: current"
        else:
            fp_state = "board-calibration: STALE - predates the latest "                        "board calibration!"
        print("SOURCE: %s\n        %d probes recorded %s; %s"
              % (args.fit, len(recs), age, fp_state))
        consts = data.get("consts") if isinstance(data, dict) else None
        if consts:
            print("        commanded under: radscale=%.4f stickout=%.3f "
                  "gfudge=%.3f flips=%d/%d"
                  % (consts["radscale"], consts["stickout"],
                     consts["gfudge"], consts.get("flip_rank", 0),
                     consts.get("flip_file", 0)))
        if fp_saved is None or fp_saved != cur:
            print("        (a UI arm calibration, or a fresh probe run, "
                  "refreshes this file automatically)")
        fr, ff, best, now = best_flips(recs)
        if (fr, ff) != (bool(CBstate.AL5D_flip_rank),
                        bool(CBstate.AL5D_flip_file)) and best < now * 0.6:
            settings.set("arm.flip_rank", int(fr))
            settings.set("arm.flip_file", int(ff))
            print("Board labelling orientation corrected: flip_rank=%d "
                  "flip_file=%d (mean error %.2f in vs %.2f in as-is)."
                  % (fr, ff, best, now))
        elif (CBstate.AL5D_flip_rank or CBstate.AL5D_flip_file):
            print("Board labelling orientation kept as configured "
                  "(best alternative %.2f in vs current %.2f in)."
                  % (best, now))
        fit = fit_constants(recs)
        bad = proposed_out_of_reach(fit["new_radscale"],
                                    fit["new_stickout"],
                                    [r["square"] for r in recs])
        for line in fit_report_lines(
                recs, fit, reach_bad=bad or None):
            print(line)
        if fp_saved and fp_saved == _cur_fp():
            settings.set("arm.points_fingerprint", fp_saved)
            print("\nProbe set recorded against the CURRENT board "
                  "calibration: arm gate cleared (UI Play unlocked).")
        elif fp_saved:
            print("\nNOTE: these probes were recorded against an OLDER "
                  "board calibration - re-record before trusting them.")
        return 0

    squares = args.squares.split()
    if args.verify:
        recs, _, _ = _load_recs(args.verify)
        squares = [r["square"] for r in recs]
        return run_session(squares, None, verify_recs=True, sink=args.sink,
                           cruise=args.cruise)

    return run_session(squares, args.out, sink=args.sink, cruise=args.cruise)


if __name__ == "__main__":
    sys.exit(main())
