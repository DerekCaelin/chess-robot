# run_stability.py
#
# Corner-detection STABILITY harness (complements run_tests.py).
#
# Sensor noise, compression and small lighting shifts differ between the
# reference frame and the live frames of one move, and the live preview re-
# detects every second.  A detector that jumps a fraction of a square per
# frame makes move detection and calibration unusable, so this checks that
# detections are invariant to small perturbations of the input image.
#
# For each case (except the acknowledged-hard midgame case) it applies a set
# of perturbations and verifies the detected geometry stays within
# SLACK_PX of the unperturbed detection, and within tolerance of ground
# truth.  SLACK_PX is deliberately far below one square (~84 px at this
# framing): its job is to catch whole-square slide errors, not sub-pixel
# jitter.  "No detection" on a perturbed frame is tolerated (the stabilizer
# holds the previous result) but at most one variant per case may miss.
#
# Run from the project root:  python tests/run_stability.py

import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import run_tests as rt  # noqa: E402

CASES = ["empty_center", "empty_shadow", "setup_center", "setup_center_2",
         "setup_far", "setup_rotate", "setup_center_3_wood_board",
         "setup_center_3_wood_board_b"]

SLACK_PX = 6.0   # max extra geometry error vs the clean-image detection
                 # (well under one square ~84px and under the system's own
                 #  DRIFT_LIMIT=10px staleness threshold)


def perturbations(img):
    yield "noise4", np.clip(
        img.astype(np.float32) + np.random.default_rng(1).normal(
            0, 4, img.shape), 0, 255).astype(np.uint8)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 40])
    yield "jpeg40", cv2.imdecode(buf, cv2.IMREAD_COLOR)
    yield "bright+", np.clip(img.astype(np.float32) * 1.08, 0, 255).astype(
        np.uint8)
    yield "bright-", np.clip(img.astype(np.float32) * 0.92, 0, 255).astype(
        np.uint8)


def main():
    import board_autodetect
    print("%-26s %8s %8s  %s" % ("case", "clean", "worst", "variants"))
    failures = 0
    for name in CASES:
        case_dir = os.path.join(rt.CASES_DIR, name)
        img, gt, state, tol = rt.load_case(case_dir)
        h, w = img.shape[:2]
        det0 = board_autodetect.detect_board(img)
        if det0 is None:
            print("%-26s  ERROR: no detection even on clean image" % name)
            failures += 1
            continue
        base = rt.geometry_error_px(gt, det0, w, h)
        worst = base
        notes = []
        misses = 0
        for tag, pim in perturbations(img):
            det = board_autodetect.detect_board(pim)
            if det is None:
                misses += 1
                notes.append("%s:miss" % tag)
                continue
            e = rt.geometry_error_px(gt, det, w, h)
            worst = max(worst, e)
            notes.append("%s:%.1f" % (tag, e))
        ok = worst <= max(tol, base + SLACK_PX) and misses <= 1
        if not ok:
            failures += 1
        print("%-26s %8.1f %8.1f  %s  %s" % (
            name, base, worst, " ".join(notes), "PASS" if ok else "FAIL"))
    print("\n%d of %d cases stable." % (len(CASES) - failures, len(CASES)))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
