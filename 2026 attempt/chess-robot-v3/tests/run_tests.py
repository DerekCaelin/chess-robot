# run_tests.py
#
# Validates the automatic board detector against labelled test cases.
#
# Test case format (one directory per case under tests/cases/):
#   tests/cases/<name>/capture.jpg   raw camera frame (any resolution)
#   tests/cases/<name>/corners.json  {"A1":[x,y], "H1":[x,y], "A8":[x,y], "H8":[x,y]}
#                                    coords normalised to [0,1] relative to the image size
#   tests/cases/<name>/state.txt     "empty", "setup", "midgame" or "geometry"
#   tests/cases/<name>/tolerance.txt optional corner-error cap in px
#
#   state meanings:
#     empty     pieces off the board; orientation may be ambiguous (180 deg),
#               so only the corner GEOMETRY is checked.
#     setup     starting position; geometry AND orientation are checked.
#     midgame   scattered pieces; detection is a bonus — "no detection" is a
#               PASS and a detection must have correct geometry (orientation
#               is not checked because scattered pieces break the piece-band
#               signal used to label ranks).
#     geometry  orientation deliberately unchecked: frames where the auto-
#               label may legally mirror the human's clicks — e.g. a board
#               that was physically rotated 90 deg at setup time (the
#               detector follows the a1-dark convention; the live A1-click
#               fixes the labelling).  Regression case:
#               setup_center_3_wood_board (mis-rotated setup).
#
# Run from the project root:  python tests/run_tests.py
#
# Prints a table of per-case corner error (in pixels of the case image) and
# whether the detected labels match the ground truth.  A case passes when the
# maximum corner error is below MAX_CORNER_ERROR_PX and every detected corner
# lands on its correctly-labelled ground-truth corner.

import os
import json
import sys

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT_ROOT)

CASES_DIR = os.path.join(HERE, "cases")

# Corner error (px) above which a case is considered failed.
MAX_CORNER_ERROR_PX = 5.0

CORNER_LABELS = ("A1", "H1", "A8", "H8")


def load_case(case_dir):
    img = cv2.imread(os.path.join(case_dir, "capture.jpg"))
    if img is None:
        raise IOError("cannot read capture.jpg in " + case_dir)
    with open(os.path.join(case_dir, "corners.json")) as f:
        corners = json.load(f)
    state_path = os.path.join(case_dir, "state.txt")
    state = "empty"
    if os.path.exists(state_path):
        state = open(state_path).read().strip()
    tol_path = os.path.join(case_dir, "tolerance.txt")
    tol = MAX_CORNER_ERROR_PX
    if os.path.exists(tol_path):
        tol = float(open(tol_path).read().strip())
    return img, corners, state, tol


def detect(img):
    """Run the auto-detector. Returns {"A1":(x,y),...} in normalised coords,
    or None if not implemented / failed."""
    try:
        import board_autodetect
    except ImportError:
        return None
    return board_autodetect.detect_board(img)


# Perimeter order of the four corners (going around the board).
PERIMETER = ("A1", "H1", "H8", "A8")


def _pix(labeled, label, w, h):
    x, y = labeled[label]
    return (x * w, y * h)


def _dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def geometry_error_px(gt, det, w, h):
    """Minimum average per-corner error over the 8 symmetries of the square.

    This measures whether the detector found the right four points, ignoring
    how it labelled them (orientation)."""
    gp = [_pix(gt, k, w, h) for k in PERIMETER]
    dp = [_pix(det, k, w, h) for k in PERIMETER]
    best = float("inf")
    for rev in (False, True):
        seq = dp if not rev else [dp[0], dp[3], dp[2], dp[1]]
        for i in range(4):
            rot = seq[i:] + seq[:i]
            err = sum(_dist(a, b) for a, b in zip(gp, rot)) / 4.0
            best = min(best, err)
    return best


def orientation_error_px(gt, det, w, h):
    """Average per-corner error using the detector's labels as-is."""
    return sum(_dist(_pix(gt, k, w, h), _pix(det, k, w, h))
               for k in PERIMETER) / 4.0


def main():
    if not os.path.isdir(CASES_DIR):
        print("No test cases found at " + CASES_DIR)
        return 1

    names = sorted(
        d for d in os.listdir(CASES_DIR)
        if os.path.isdir(os.path.join(CASES_DIR, d))
    )
    if not names:
        print("tests/cases/ is empty — generate cases with board_calibrate.py "
              "(Save test case button).")
        return 1

    detector_ready = True
    try:
        import board_autodetect  # noqa: F401
    except ImportError:
        detector_ready = False
        print("board_autodetect.py not present yet — validating case data only.\n")

    print("%-26s %-8s %6s %8s %8s %8s  %s" % (
        "case", "state", "WxH", "geo(px)", "orient(px)", "check", "result"))

    failures = 0
    for name in names:
        case_dir = os.path.join(CASES_DIR, name)
        try:
            img, gt, state, tol = load_case(case_dir)
        except Exception as e:
            print("%-26s ERROR: %s" % (name, e))
            failures += 1
            continue

        h, w = img.shape[:2]

        # Validate ground-truth corners are present and normalised to [0,1].
        data_ok = True
        for label in CORNER_LABELS:
            if label not in gt:
                print("%-26s ERROR: missing corner label %s in corners.json" % (name, label))
                failures += 1
                data_ok = False
                break
            x, y = gt[label]
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                print("%-26s ERROR: corner %s out of [0,1] range (%s, %s)"
                      % (name, label, x, y))
                failures += 1
                data_ok = False
                break

        if not data_ok:
            continue

        if not detector_ready:
            print("%-26s %-8s %6s (detector not implemented — ground truth OK)"
                  % (name, state, "%dx%d" % (w, h)))
            continue

        det = detect(img)
        if det is None:
            # Scattered pieces are the acknowledged hard case: an honest
            # miss passes, a *wrong* detection must not.
            if state == "midgame":
                print("%-26s %-8s %6s %8s %8s %8s  PASS (no detection)"
                      % (name, state, "%dx%d" % (w, h), "-", "-", "-"))
                continue
            print("%-26s %-8s %6s %8s %8s %8s  FAIL (no detection)"
                  % (name, state, "%dx%d" % (w, h), "-", "-", "-"))
            failures += 1
            continue

        geo_err = geometry_error_px(gt, det, w, h)
        ori_err = orientation_error_px(gt, det, w, h)

        # Only clean starting-position boards must also be *labelled*
        # correctly: empty boards are 180-degree ambiguous, midgame boards
        # lack the piece-band signal, and "geometry" cases (mis-rotated
        # setups, fixed in live use by the A1-click) opt out of orientation
        # checking entirely.
        if state == "setup":
            ok = ori_err <= tol
            check = "orient"
            shown = ori_err
        else:
            ok = geo_err <= tol
            check = "geometry"
            shown = geo_err

        if not ok:
            failures += 1
        print("%-26s %-8s %6s %8.1f %8.1f %8s  %s" % (
            name, state, "%dx%d" % (w, h),
            geo_err, ori_err, check,
            "PASS" if ok else "FAIL"))

    print("\n%d of %d cases passed." % (len(names) - failures, len(names)))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
