# tests/reach_envelope.py - guard the Arm-grab commandable envelope so a future
# change can't shrink the dial/jog bounds or the hover cap and lock the far
# corners (a1/h1) out again.
#
# Two kinds of result:
#   ASSERT (hard fail): the UI lets you COMMAND out to the true wrist annulus and
#     span the edge angles - pure geometry, independent of calibration.
#   WARN  (advisory): with the CURRENT calibration a corner's pick radius may
#     still exceed the A+B reach ceiling; that's a calibration (radscale) issue,
#     not a code regression, so it only prints.
#
# Run: python tests/reach_envelope.py   (exit 1 on an ASSERT failure)

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from chessapp import settings
settings.apply()
import CBstate                                  # noqa: E402
import LEGO_Chess_rpd as RD                      # noqa: E402
import ui.app as A                               # noqa: E402


def main():
    AB = CBstate.AL5D_A + CBstate.AL5D_B
    G = A.ArmGrabScreen
    fails, warns = [], []

    # (1) reach bound allows commanding to the wrist limit at the LOWEST pick
    #     height: X_HI >= sqrt((A+B)^2 - Y_LO^2).
    x_limit = math.sqrt(max(0.0, AB * AB - G.Y_LO * G.Y_LO))
    if G.X_HI < x_limit - 0.05:
        fails.append("X_HI %.2f < lowest-height wrist limit %.2f (locks far reach)"
                     % (G.X_HI, x_limit))

    # (2) rotation bounds span the near-rank edge squares at a 1.5" board (the
    #     widest angle spread), so h8/a8 are still jogable.
    for f in ("h", "a"):
        ang = RD.armangle_xy((3.5 if f == "a" else -3.5) * 1.5, 0.0)
        if not (G.Z_LO - 1.0 <= ang <= G.Z_HI + 1.0):
            fails.append("%s8 edge angle %.1f outside jog bounds [%g,%g]"
                         % (f, ang, G.Z_LO, G.Z_HI))

    # (3) every square has SOME in-annulus hover height (the reach-aware cruise
    #     must leave a height above Y_LO, or the arm can't sit over it at the
    #     current calibration). Report which corners need a lower radscale.
    min_h = G.Y_LO + 0.4
    max_wrist = math.sqrt(max(0.0, AB * AB - min_h * min_h))
    for f in "abcdefgh":
        for r in range(1, 9):
            sq = f + str(r)
            X = RD.armlength(sq)
            if X > max_wrist:
                need = RD.radscale * max_wrist / X
                warns.append("%s out of annulus at radscale=%.3f "
                             "(lower to <= %.3f)" % (sq, RD.radscale, need))

    print("Reach envelope: X_HI=%.2f (limit %.2f)  Z=[%g,%g]  squaresize=%.2f  "
          "radscale=%.3f" % (G.X_HI, x_limit, G.Z_LO, G.Z_HI,
                             RD.squaresize, RD.radscale))
    if warns:
        print("WARN (calibration, not code):")
        for w in sorted(set(warns)):
            print("  " + w)
    if fails:
        print("FAIL:")
        for x in fails:
            print("  " + x)
        return 1
    print("PASS: envelope allows commanding the far corners.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
