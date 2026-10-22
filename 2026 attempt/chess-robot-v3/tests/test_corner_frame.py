# tests/test_corner_frame.py - pure-math checks for the mode-B corner frame fit
# and the board-unit seam (mode A must reproduce the parametric model exactly).
#   python tests/test_corner_frame.py
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import CBstate  # noqa: E402
import LEGO_Chess_rpd as RD  # noqa: E402
import arm_probe as ap  # noqa: E402

RTOD = 57.295779
SQUARES = ("a8", "h8", "h1", "a1")


def _old_len(sq):
    m = RD._model_sq(sq)
    x = RD.xmtrans[m[0]] * RD.squaresize
    y = (8 - int(m[1])) * RD.squaresize
    return (math.sqrt(x ** 2 + (y + RD.axistorow0) ** 2) + RD.stickout) * RD.radscale


def _old_ang(sq):
    m = RD._model_sq(sq)
    x = RD.xmtrans[m[0]] * RD.squaresize
    y = (8 - int(m[1])) * RD.squaresize
    return RD.Gfudge + 90 + RTOD * math.atan(x / (y + RD.axistorow0))


def _frame_recs(px, py, m, c, a0, sign=1, dreach=None):
    """Corner measurements generated from the frame model itself."""
    recs = []
    for i, sq in enumerate(SQUARES):
        bx, by = ap.corner_units(sq)
        dx, dy = bx - px, by - py
        x = m * math.hypot(dx, dy) + c
        if dreach:
            x += dreach[i]
        z = a0 - sign * RTOD * math.atan2(dx, dy)
        recs.append({"square": sq, "X": x, "Z": z})
    return recs


def test_mode_a_preserves_parametric():
    fails = []
    for sq in ("a1", "a8", "h1", "h8", "d4", "e5", "b7", "g2"):
        if abs(RD.armlength(sq) - _old_len(sq)) > 1e-9:
            fails.append("reach %s" % sq)
        if abs(RD.armangle(sq) - _old_ang(sq)) > 1e-9:
            fails.append("angle %s" % sq)
    assert not fails, "mode A diverged: %s" % fails


def test_corner_frame_recovers_exact():
    px, py, m, c, a0 = 4.2, -2.6, 0.95, 2.1, 80.0
    f = ap.fit_corner_frame(_frame_recs(px, py, m, c, a0))
    assert f.get("ok"), f
    assert abs(f["px"] - px) < 1e-6
    assert abs(f["py"] - py) < 1e-6
    assert abs(f["scale"] - m) < 1e-6
    assert abs(f["offset"] - c) < 1e-6
    assert abs(f["a0"] - a0) < 1e-6
    assert f["residual_in"] < 1e-9


def test_corner_frame_angles_pin_the_pivot():
    # Four square corners give only two distinct radii from any pivot on the
    # symmetry line: the RADIAL fit alone is degenerate (a family of pivots
    # fits equally well) and tiny radius noise used to send the solver sliding
    # along it - into the old 'pivot behind rank-8' bound, failing with ~3 in
    # of phantom residual (2026-10-20). The base angles must now pin the
    # pivot and a0.
    px, py, m, c, a0 = 4.0, -1.2, 1.10, 1.35, 87.0
    recs = _frame_recs(px, py, m, c, a0, dreach=(0.05, -0.03, 0.04, -0.02))
    f = ap.fit_corner_frame(recs)
    assert f.get("ok"), f
    assert abs(f["px"] - px) < 0.2, f
    assert abs(f["py"] - py) < 0.2, f
    assert abs(f["a0"] - a0) < 2.0, f
    assert f["residual_in"] < 0.1 and f["ang_residual_deg"] < 1.0, f


def test_corner_frame_pivot_on_far_side():
    # The 2026-10-14 bounds forced py < 0 ('pivot behind rank-8') and rejected
    # valid placements outright. The pivot may sit on ANY side of the board.
    px, py, m, c, a0 = 4.0, 10.3, 1.25, 0.0, 84.8
    f = ap.fit_corner_frame(_frame_recs(px, py, m, c, a0))
    assert f.get("ok"), f
    assert abs(f["py"] - py) < 0.1, f
    assert abs(f["scale"] - m) < 0.01, f


def test_corner_frame_mirrored_labels():
    # A MIRRORED labelling is a chirality flip no pivot/rotation can absorb;
    # the fit carries a sign on the atan2 term and must recover it (the
    # runtime frame uses the same sign - see planar_to_arm).
    px, py, m, c, a0 = 4.0, 10.3, 1.25, 0.0, 84.8
    f = ap.fit_corner_frame(_frame_recs(px, py, m, c, a0, sign=-1))
    assert f.get("ok"), f
    assert f["sign"] == -1, f
    assert f["residual_in"] < 1e-6, f
    assert f["ang_residual_deg"] < 1e-6, f


def test_corner_frame_reports_per_corner():
    # A failure must say WHICH corner disagrees (reach/angle per corner), not
    # just one RMS number. (A smaller single-corner error can be absorbed into
    # a stretched scale - 4 corners carry no redundancy to expose it - so use
    # a corruption the gate must catch.)
    recs = _frame_recs(4.2, -2.6, 0.95, 2.1, 80.0)
    recs[2]["X"] += 5.0                      # h1 blows up
    f = ap.fit_corner_frame(recs)
    assert not f.get("ok")
    assert len(f.get("reach_res", [])) == 4, f
    assert max(abs(v) for v in f["reach_res"]) > 1.0, f


def test_corner_frame_rejects_high_residual():
    # Data that no scale*dist+offset can fit (huge outlier) -> residual guard.
    recs = [{"square": "a8", "X": 10.0, "Z": 90.0},
            {"square": "h8", "X": 10.0, "Z": 90.0},
            {"square": "h1", "X": 10.0, "Z": 90.0},
            {"square": "a1", "X": 50.0, "Z": 90.0}]
    f = ap.fit_corner_frame(recs)
    assert not f.get("ok")


def test_mode_b_round_trip():
    # The fitted frame must reproduce the measurements through the RUNTIME
    # seam: board square -> board_sq_center -> planar_to_arm equals the fit's
    # own model at corner_units ("does the board's understanding of A8 align
    # with the measured location?"), and lands back on the measurement.
    px, py, m, c, a0 = 4.2, -2.6, 0.95, 2.1, 80.0
    recs = _frame_recs(px, py, m, c, a0)
    f = ap.fit_corner_frame(recs)
    assert f.get("ok"), f
    old = (RD.calib_mode, RD.frame_px, RD.frame_py, RD.frame_scale,
           RD.frame_offset, RD.frame_a0, RD.frame_sign)
    RD.calib_mode = "B"
    RD.frame_px, RD.frame_py = f["px"], f["py"]
    RD.frame_scale, RD.frame_offset = f["scale"], f["offset"]
    RD.frame_a0, RD.frame_sign = f["a0"], f["sign"]
    try:
        for s in recs:
            Xp, Zp = ap.frame_predict(f, s["square"])
            Xr, Zr = RD.planar_to_arm(*RD.board_sq_center(s["square"]))
            assert abs(Xr - Xp) < 1e-9, (s["square"], Xr, Xp)
            # 1e-3 deg: the runtime's rtod is rounded (57.295779)
            assert abs((Zr - Zp + 180.0) % 360.0 - 180.0) < 1e-3
            assert abs(Xp - s["X"]) < 0.02, (s["square"], Xp, s["X"])
    finally:
        (RD.calib_mode, RD.frame_px, RD.frame_py, RD.frame_scale,
         RD.frame_offset, RD.frame_a0, RD.frame_sign) = old


def test_mode_b_round_trip_exact():
    # THE automatic calibration check (2026-10-21): predicted A1/A8/H1/H8 must
    # equal the MEASURED locations, through the runtime seam, using the values
    # AS SAVED (rounded frame + per-corner corrections). This is what the
    # corner screen reports as round-trip PASS.
    recs = _frame_recs(4.2, -2.6, 0.95, 2.1, 80.0,
                       dreach=(0.05, -0.03, 0.04, -0.02))
    recs[1]["Z"] += 1.3              # a residual the bare frame cannot hold
    f = ap.fit_corner_frame(recs)
    assert f.get("ok"), f
    fr = {"px": round(f["px"], 3), "py": round(f["py"], 3),
          "scale": round(f["scale"], 4), "offset": round(f["offset"], 3),
          "a0": round(f["a0"], 2), "sign": int(f["sign"])}
    corr = []
    for s in recs:
        Xp, Zp = ap.frame_predict(fr, s["square"])
        dz = (s["Z"] - Zp + 180.0) % 360.0 - 180.0
        corr.append([s["square"], round(s["X"] - Xp, 3), round(dz, 2)])
    old = (RD.calib_mode, RD.frame_px, RD.frame_py, RD.frame_scale,
           RD.frame_offset, RD.frame_a0, RD.frame_sign)
    old_res = dict(RD.corner_res)
    RD.calib_mode = "B"
    RD.frame_px, RD.frame_py = fr["px"], fr["py"]
    RD.frame_scale, RD.frame_offset = fr["scale"], fr["offset"]
    RD.frame_a0, RD.frame_sign = fr["a0"], fr["sign"]
    RD.set_corner_res(corr)
    try:
        for s in recs:
            Xr, Zr = RD.planar_to_arm(*RD.board_sq_center(s["square"]))
            assert abs(Xr - s["X"]) < 0.02, (s["square"], Xr, s["X"])
            assert abs((Zr - s["Z"] + 180.0) % 360.0 - 180.0) < 0.05, \
                (s["square"], Zr, s["Z"])
    finally:
        (RD.calib_mode, RD.frame_px, RD.frame_py, RD.frame_scale,
         RD.frame_offset, RD.frame_a0, RD.frame_sign) = old
        RD.corner_res = old_res


def main():
    test_mode_a_preserves_parametric()
    test_corner_frame_recovers_exact()
    test_corner_frame_angles_pin_the_pivot()
    test_corner_frame_pivot_on_far_side()
    test_corner_frame_mirrored_labels()
    test_corner_frame_reports_per_corner()
    test_corner_frame_rejects_high_residual()
    test_mode_b_round_trip()
    test_mode_b_round_trip_exact()
    print("PASS: corner-frame fit + board-unit seam")


if __name__ == "__main__":
    main()
