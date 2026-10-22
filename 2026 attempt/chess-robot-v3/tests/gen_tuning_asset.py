# tests/gen_tuning_asset.py - draws the pointing-tuning diagram used by the
# Arm calibration Instructions modal.  Run: python tests/gen_tuning_asset.py
import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "ui", "assets", "pointing_tuning.png")

W, H = 1000, 470
BG = (24, 27, 34)
CARD = (34, 38, 47)
GRID = (90, 76, 55)
LIGHT = (246, 242, 232)
DARK = (80, 70, 58)
FG = (235, 235, 235)
DIM = (150, 150, 155)
ACCENT = (60, 170, 255)
GOOD = (80, 220, 120)
BAD = (80, 80, 255)


def panel(img, x0, y0, w, h, title):
    cv2.rectangle(img, (x0, y0), (x0 + w, y0 + h), CARD, -1)
    cv2.putText(img, title, (x0 + 10, y0 + 26),
                cv2.FONT_HERSHEY_SIMPLEX, 0.62, ACCENT, 2)


def board(img, x, y, side, cells=8):
    s = side // cells
    for r in range(cells):
        for c in range(cells):
            col = LIGHT if (r + c) % 2 == 0 else DARK
            cv2.rectangle(img, (x + c * s, y + r * s),
                          (x + (c + 1) * s, y + (r + 1) * s), col, -1)
    cv2.rectangle(img, (x, y), (x + side, y + side), GRID, 2)
    # base sits along the TOP edge (rank 8 side, like the live pictures)
    bx = x + side // 2
    cv2.rectangle(img, (bx - 16, y - 26), (bx + 16, y - 4), (120, 120, 125), -1)
    cv2.putText(img, "base", (bx - 17, y - 32),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, DIM, 1)
    return s


def target(img, cx, cy):
    cv2.drawMarker(img, (cx, cy), GOOD, cv2.MARKER_CROSS, 14, 2)


def landed(img, cx, cy):
    cv2.circle(img, (cx, cy), 7, BAD, -1)
    cv2.arrowedLine(img, (cx, cy), (cx, cy), BAD, 2)


def main():
    img = np.full((H, W, 3), BG, np.uint8)

    panels = [
        ("OVERSHOOT: lands beyond the target",
         "evenly everywhere -> lower stickout",
         "worse on FAR squares -> lower radscale", "long"),
        ("UNDERSHOOT: lands short of target",
         "evenly everywhere -> raise stickout",
         "worse on FAR squares -> raise radscale", "short"),
        ("SIDEWAYS: all lands shift across files",
         "constant angle fan -> Gfudge",
         "step +/-2 deg until it straightens", "side"),
    ]
    pw = 316
    for i, (title, rule1, rule2, kind) in enumerate(panels):
        x0 = 14 + i * (pw + 12)
        y0 = 44
        panel(img, x0, y0, pw, 330, "")
        cv2.putText(img, title.split(":")[0], (x0 + 10, y0 + 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, ACCENT, 2)
        cv2.putText(img, title.split(": ", 1)[1], (x0 + 10, y0 + 48),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, FG, 1)
        bx = x0 + pw // 2
        s = board(img, x0 + 58, y0 + 100, 200)
        # target square: e-file-ish, 3rd row down from base (rank ~6)
        tcx = x0 + 58 + s * 3           # 'e' side of centre
        tcy = y0 + 100 + s * 2          # near base (rank 7): clear of centre
        target(img, tcx, tcy)
        cv2.putText(img, "target", (tcx - 22, tcy + 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, GOOD, 1)
        if kind == "long":
            # base is at the TOP, so "farther from base" = lower on screen
            landed(img, tcx, tcy + 40)
            cv2.arrowedLine(img, (tcx, tcy), (tcx, tcy + 40), BAD, 2)
        elif kind == "short":
            landed(img, tcx, tcy - 40)
            cv2.arrowedLine(img, (tcx, tcy), (tcx, tcy - 40), BAD, 2)
        else:
            landed(img, tcx + 42, tcy)
            cv2.arrowedLine(img, (tcx, tcy), (tcx + 42, tcy), BAD, 2)
        yy = y0 + 320
        cv2.putText(img, rule1[:48], (x0 + 10, yy),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, FG, 1)
        cv2.putText(img, rule2[:48], (x0 + 10, yy + 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, DIM, 1)

    cv2.putText(img,
                "landings measured from the base: radial = stickout/radscale, "
                "sideways = Gfudge",
                (16, H - 52), cv2.FONT_HERSHEY_SIMPLEX, 0.52, DIM, 1)
    cv2.putText(img,
                "the Arm calibration walk SCORES every change - tweak one "
                "value, walk again, compare",
                (16, H - 26), cv2.FONT_HERSHEY_SIMPLEX, 0.52, DIM, 1)

    assert cv2.imwrite(OUT, img), "write failed"
    print("wrote", OUT)


if __name__ == "__main__":
    sys.exit(main())
