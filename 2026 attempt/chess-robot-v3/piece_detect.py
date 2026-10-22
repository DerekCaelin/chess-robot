# piece_detect.py
#
# Hands-free move detection for the chess robot.
#
# The chess engine already knows the full board state (including piece types),
# so the camera only needs to spot the *change* the human just made.  Rather
# than classifying every square (fragile under lighting gradients), we watch
# the board continuously and detect the "stable -> moving -> stable" pattern
# that a human move produces:
#
#   - while the board is untouched, consecutive frames are near-identical;
#   - the human's hand + moved piece make frames change sharply;
#   - once the hand is gone and the piece is settled, frames go stable again.
#
# The move is the difference between the stable frame before the motion and the
# stable frame after it.  Because the reference is re-captured every time the
# board is idle, the two frames are only seconds apart, so ambient lighting
# drift is negligible.

import math
import time

import cv2
import numpy as np

import CBstate

xrevtrans = {1: "a", 2: "b", 3: "c", 4: "d",
             5: "e", 6: "f", 7: "g", 8: "h"}

mydir = CBstate.mydir

INSET = 0.22        # per-square inset (fraction) to avoid square borders
N_FRAMES = 3        # frames averaged per sample (reduces sensor noise)
# Threshold rationale (measured on the wood board, locked exposure):
#   idle noise (3-frame samples, 1 s apart): mean 2.7, max 4.6
#   shadow bleed beside a real move: up to ~5.8
#   real piece on/off square: 43-67 Lab distance
#   7.0 rejects both noise sources with >20% margin and stays far under
#   the weakest plausible real signal (a light-wood piece arriving on a
#   dark far-rank square).
CHANGE_THRESH = 7.0  # Lab distance above which a square/frame is "changed"
JOSTLE_THRESH = 6   # more changed squares than this => board/camera moved
DRIFT_LIMIT = 10.0  # corner drift (px) above which calibration is stale


def preprocess(img):
    """CLAHE on the L channel, returned as a float32 LAB image."""
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
    l = clahe.apply(l)
    return cv2.merge([l, a, b]).astype(np.float32)


def load_homography():
    """Compute the board perspective warp from points.txt (normalised 0-1)."""
    import os
    points_path = mydir + "points.txt"
    if not os.path.exists(points_path):
        raise RuntimeError("No points.txt — run board calibration first.")
    with open(points_path) as f:
        vals = [float(v) for v in f.readline().rstrip().split(",")]
    w, h = CBstate.cameraresolution
    a1 = (vals[0] * w, vals[1] * h)
    h1 = (vals[2] * w, vals[3] * h)
    a8 = (vals[4] * w, vals[5] * h)
    h8 = (vals[6] * w, vals[7] * h)
    board_w = int((abs(a1[0] - h1[0]) + abs(a8[0] - h8[0])) / 2)
    board_h = int((abs(a1[1] - a8[1]) + abs(h1[1] - h8[1])) / 2)
    d = int((board_w + board_h) / 2)
    src = np.float32([a1, h1, a8, h8])
    dst = np.float32([[0, d], [d, d], [0, 0], [d, 0]])
    H = cv2.getPerspectiveTransform(src, dst)
    return H, d


def _square_diffs(a, b):
    """Per-square pixel-level Lab distance between two frames (shape 8x8)."""
    h, w = a.shape[:2]
    sqy = h / 8.0
    sqx = w / 8.0
    out = np.zeros((8, 8), dtype=np.float32)
    for r in range(8):
        for c in range(8):
            r1 = int(r * sqy + sqy * INSET)
            r2 = int((r + 1) * sqy - sqy * INSET)
            c1 = int(c * sqx + sqx * INSET)
            c2 = int((c + 1) * sqx - sqx * INSET)
            out[r, c] = np.linalg.norm(a[r1:r2, c1:c2] - b[r1:r2, c1:c2],
                                       axis=2).mean()
    return out


def _motion(a, b):
    """Largest per-square change between two frames."""
    return float(_square_diffs(a, b).max())


def _read_warped_frame(cap, H, d):
    """Read N frames, average, warp, and preprocess.  None on failure."""
    frames = []
    for _ in range(N_FRAMES):
        ok, frame = cap.read()
        if not ok:
            return None
        frames.append(frame)
    avg = np.mean(frames, axis=0).astype(np.uint8)
    warped = cv2.warpPerspective(avg, H, (d + 1, d + 1), flags=cv2.INTER_LINEAR)
    return preprocess(warped)


def _board_sane(cap):
    """One capture: can the checkerboard still be found, and roughly where
    points.txt says it is?  False means the camera or board moved out of
    the calibrated view - the player must run Board calibration."""
    ok, frame = cap.read()
    if not ok or frame is None:
        return False
    det = _reliable_detect(frame)
    if det is None or not corners_sane(det):
        return False
    try:
        vals = _read_points()
    except Exception:
        return False
    h, w = frame.shape[:2]
    gt = {"A1": (vals[0], vals[1]), "H1": (vals[2], vals[3]),
          "A8": (vals[4], vals[5]), "H8": (vals[6], vals[7])}
    return _geometry_drift(det, gt, w, h) < 3 * DRIFT_LIMIT + 40


def monitor(interval=1.0, settle_frames=3, threshold=CHANGE_THRESH, stop=None,
          status=None, on_ready=None):
    """Watch the board until the human's move settles, then return the changed
    squares as a list of (row, col, diff), sorted by diff descending.
    stop: optional callable - when it returns True, give up and return []."""
    import camera
    camera.lock_camera()
    H, d = load_homography()

    cap = camera.open_camera()
    if cap is None:
        return "NOCAM"
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*CBstate.camerafourcc))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CBstate.cameraresolution[0])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CBstate.cameraresolution[1])
    for _ in range(30):
        cap.read()  # let the sensor settle

    try:
        # 1) Establish reference A: wait for a motionless board.
        print("monitor: waiting for a still board...", flush=True)
        if status:
            status("Waiting for the board to be still...")
        ref = None
        prev = None
        settle = 0
        waited = 0
        while ref is None:
            if stop is not None and stop():
                return []
            cur = _read_warped_frame(cap, H, d)
            if cur is None:
                return []
            if prev is not None and _motion(prev, cur) < threshold:
                settle += 1
            else:
                settle = 0
            if prev is not None and settle == 0:
                waited += 1
                if waited % 15 == 0:      # persistent motion: say what
                    top = _diff(prev, cur)[:5]   # is refusing to sit still
                    print("monitor: board not settling - changing squares:",
                          [(xrevtrans[c + 1] + str(8 - r), round(v, 1))
                           for r, c, v in top] or "scattered noise",
                          flush=True)
                    print("        (park the arm clear of the board, or "
                          "run Board calibration if the board itself moved)",
                          flush=True)
                if waited == 20:          # still stuck: IS the board even
                    if _board_sane(cap):  # there? (camera knocked aside
                        pass              # goes unnoticed otherwise)
                    else:
                        print("monitor: BOARD NOT DETECTED - calibration "
                              "needed.", flush=True)
                        return "NOBOARD"
            prev = cur
            if settle >= settle_frames:
                ref = cur
                break
            time.sleep(interval)

        # 2) Watch for motion, then settle.
        print("monitor: reference set - make your move.", flush=True)
        if status:
            status("Reference set - make your move.")
        if on_ready:
            on_ready()          # safe moment for the human to move
        prev = ref
        moving = False
        settle = 0
        samples = 0
        while True:
            if stop is not None and stop():
                return []
            cur = _read_warped_frame(cap, H, d)
            if cur is None:
                return []
            samples += 1
            if _motion(prev, cur) >= threshold:
                if not moving:
                    print("monitor: motion detected, waiting for settle...",
                          flush=True)
                    if status:
                        status("Motion detected - reading your move...")
                moving = True
                settle = 0
            elif moving:
                settle += 1
                if settle >= settle_frames:
                    return _diff(ref, cur)
            elif samples % 10 == 0:
                print(".", end="", flush=True)   # still waiting; keep proof of life
            prev = cur
            time.sleep(interval)
    finally:
        camera.close_camera(cap)


def _diff(ref, cur):
    """Changed squares between two frames (pixel-level Lab distance)."""
    diffs = _square_diffs(ref, cur)
    changes = [(r, c, float(diffs[r, c]))
               for r in range(8) for c in range(8)
               if diffs[r, c] >= CHANGE_THRESH]
    changes.sort(key=lambda t: -t[2])
    return changes


def _capture_frame():
    import camera
    camera.capture_still(mydir + "1.jpg")
    return cv2.imread(mydir + "1.jpg")


def _read_points():
    import os
    points_path = mydir + "points.txt"
    if not os.path.exists(points_path):
        raise RuntimeError("No points.txt — run board calibration first.")
    return [float(v) for v in open(points_path).read().rstrip().split(",")]


def corners_sane(det, margin=-0.005):
    """Every detected corner must lie (essentially) ON the frame - a
    negative or >1.0 normalized coordinate means the fit extrapolated off
    the sensor around an occluder (the arm's hand) and would build a
    homography into imaginary space (corrupted points.txt 2026-09-18)."""
    try:
        for k in ("A1", "H1", "A8", "H8"):
            x, y = det[k][0], det[k][1]
            if not (margin <= x <= 1.0 - margin
                    and margin <= y <= 1.0 - margin):
                return False
    except (KeyError, TypeError, IndexError):
        return False
    return True


def _reliable_detect(img):
    """Detect the board and return normalised labelled corners, or None.

    board_autodetect now contains only the reliable checkerboard paths (the
    broken outline/Hough fallbacks that caused calibration jumps were removed),
    so this simply delegates to detect_board()."""
    import board_autodetect as ba
    return ba.detect_board(img)


_PERIMETER = ("A1", "H1", "H8", "A8")


def _geometry_drift(detected, gt, w, h):
    """Average corner distance (px) between two labelled corner sets, minimised
    over the 8 square symmetries so an empty board's 180-degree ambiguity does
    not count as drift."""
    gp = [(gt[k][0] * w, gt[k][1] * h) for k in _PERIMETER]
    dp = [(detected[k][0] * w, detected[k][1] * h) for k in _PERIMETER]
    best = float("inf")
    for rev in (False, True):
        seq = dp if not rev else [dp[0], dp[3], dp[2], dp[1]]
        for i in range(4):
            rot = seq[i:] + seq[:i]
            err = sum(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
                      for a, b in zip(gp, rot)) / 4.0
            best = min(best, err)
    return best


def validate_calibration(limit=DRIFT_LIMIT):
    """Re-detect the board corners and compare their geometry to points.txt.

    Returns the average corner drift in pixels, or None if the board could
    not be detected (heavily scattered pieces can still defeat even the
    partial-window path)."""
    img = _capture_frame()
    if img is None:
        return None
    detected = _reliable_detect(img)
    if detected is None:
        return None
    vals = _read_points()
    h, w = img.shape[:2]
    gt = {"A1": (vals[0], vals[1]), "H1": (vals[2], vals[3]),
          "A8": (vals[4], vals[5]), "H8": (vals[6], vals[7])}
    return _geometry_drift(detected, gt, w, h)


def recover_calibration():
    """Re-detect the board corners after a jostle and update points.txt.

    With the partial-window lattice solver this now covers mid-game board
    states too, not just empty/starting positions — it only needs a couple
    of clean checkerboard windows somewhere on the board.  Returns True on
    success."""
    # Mid-game jostles are often someone adjusting the ARM while it hangs
    # over the board (2026-09-18: a single-frame re-lock fitted corners to a
    # partly-shadowed checkerboard, and the robot then drove at phantom
    # squares).  Require two captures ~0.4 s apart to agree before trusting
    # the new corners.
    import time as _t
    first = None
    for _attempt in range(3):
        img = _capture_frame()
        if img is None:
            return False
        detected = _reliable_detect(img)
        if detected is None:
            print("piece_detect.recover_calibration: board not reliably "
                  "detected.")
            return False
        if not corners_sane(detected):
            print("piece_detect.recover_calibration: detection has a corner "
                  "off the sensor (arm/shadow confusing the fit) - refused.")
            first = None
            _t.sleep(0.4)
            continue
        if first is not None:
            worst = max(math.hypot(detected[k][0] - first[k][0],
                                   detected[k][1] - first[k][1])
                        for k in ("A1", "H1", "A8", "H8"))
            if worst <= 0.035:
                break
            print("piece_detect.recover_calibration: view unstable "
                  "(corners moved %.3f between captures) - retrying."
                  % worst)
        first = detected
        _t.sleep(0.4)
    else:
        print("piece_detect.recover_calibration: view keeps changing - "
              "recovery aborted (clear the arm/board and retry).")
        return False
    flat = []
    for k in ("A1", "H1", "A8", "H8"):
        flat.append("%.6f" % detected[k][0])
        flat.append("%.6f" % detected[k][1])
    with open(mydir + "points.txt", "w") as f:
        f.write(",".join(flat))
    print("piece_detect.recover_calibration: corners updated.")
    return True


if __name__ == "__main__":
    # Standalone one-move test.  The board must be in the starting position.
    import playermove_rpd
    board = [
        ['r', 'n', 'b', 'q', 'k', 'b', 'n', 'r'],
        ['p'] * 8, ['.'] * 8, ['.'] * 8, ['.'] * 8, ['.'] * 8,
        ['P'] * 8, ['R', 'N', 'B', 'Q', 'K', 'B', 'N', 'R'],
    ]
    move = playermove_rpd.getplayermove(board, ())
    print("Detected move:", move)
