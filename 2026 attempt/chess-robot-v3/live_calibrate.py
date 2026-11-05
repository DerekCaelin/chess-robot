# live_calibrate.py
#
# Live board-calibration tool.
#
# Streams the webcam and runs the auto-detector continuously.  The detector
# is expensive (up to a few seconds per frame with the partial-window
# fallback), so detection runs in a *worker thread* while the preview stays
# smooth.  Raw detections are fed through a temporal *stabilizer*:
#
#   * the displayed corners are the running median of the last few
#     consistent detections — never the raw per-frame result, so nothing
#     visibly jumps on a single noisy frame;
#   * orientation is latched: each detection is relabelled to agree with the
#     committed one, so corners cannot swap names (H1 <-> A1 ...) between
#     frames;
#   * while detection is running or has just failed, the last good result is
#     *held* ("searching..." ghost) instead of flickering;
#   * a new position is only adopted after several consistent detections
#     disagreeing with the committed one (i.e. the camera really moved).
#
# Controls:
#   s        save the committed corners to images/points.txt and quit
#   q        quit without saving
#   left-click  point at the true A1 corner.  This is the orientation
#               *authority*: it relabels the board with whichever symmetry
#               (rotation or mirror) puts A1 where you clicked.  Needed
#               because an empty board is 180-degree ambiguous, and because
#               auto-labelling can come out mirrored if the board is
#               physically rotated 90 degrees (a setup error the convention-
#               following detector cannot see).
#
# The board is detected whether pieces are set up or the board is empty
# (partial-window fallback handles the shadows and glare pieces cause).
#
# Usage:  python live_calibrate.py

import threading
import time

import cv2
import numpy as np

import CBstate
import board_autodetect as bd

mydir = CBstate.mydir

LABELS = ("A1", "H1", "A8", "H8")

# Timing log (2026-11-05): the UI's stdout dies with the kiosk X session, so
# the calibration timing prints were invisible exactly when needed - the
# in-app detection was reported at 30-120s while the same flow locked in 2s
# standalone.  Append every stage to a file the deploy can read back.
_TIMING_LOG = mydir + "/data/calib_timing.log"


def tlog(msg, fresh=False):
    """Append a timing line; fresh=True truncates (a new calibration run)."""
    try:
        with open(_TIMING_LOG, "w" if fresh else "a") as fh:
            fh.write("%.1f %s\n" % (time.time(), msg))
    except Exception:
        pass

CONSIST_PX = 25.0   # per-corner distance within which detections agree
K_LOCK = 4          # consistent detections to lock / to adopt a new position


# ---------------------------------------------------------------------------
# Geometry helpers on labelled-corner dicts ({label: (nx, ny) in [0,1]})
# ---------------------------------------------------------------------------

def _perimeter(c):
    return [c["A1"], c["H1"], c["H8"], c["A8"]]


def _err_px(a, b, w, h):
    return sum(np.hypot((a[k][0] - b[k][0]) * w, (a[k][1] - b[k][1]) * h)
               for k in LABELS) / 4.0


def _median(dicts, w, h):
    med = {k: (float(np.median([d[k][0] for d in dicts])),
               float(np.median([d[k][1] for d in dicts]))) for k in LABELS}
    return med


def _spread_ok(dicts, w, h):
    med = _median(dicts, w, h)
    return all(_err_px(d, med, w, h) <= CONSIST_PX for d in dicts)


def _relabel_match(cand, ref, w, h):
    """Relabel `cand` over the 8 square symmetries to best agree with `ref`.
    Returns (relabelled dict, mean error px).  This is the orientation latch:
    incoming detections can never introduce a different labelling."""
    cp = _perimeter(cand)
    rp = _perimeter(ref)
    best, best_err = None, float("inf")
    for rev in (False, True):
        seq = cp if not rev else [cp[0], cp[3], cp[2], cp[1]]
        for i in range(4):
            rot = seq[i:] + seq[:i]
            e = sum(np.hypot((a[0] - b[0]) * w, (a[1] - b[1]) * h)
                    for a, b in zip(rp, rot)) / 4.0
            if e < best_err:
                best_err, best = e, rot
    out = {"A1": best[0], "H1": best[1], "H8": best[2], "A8": best[3]}
    return out, best_err


class Stabilizer:
    """Turns a stream of raw detections into one steady `committed` result."""

    def __init__(self):
        self.committed = None
        self.buf = []       # recent detections agreeing with committed
        self.alt = []       # candidate new position
        self.misses = 0     # consecutive frames without any result

    def feed(self, raw, w, h):
        """Return True if `committed` changed (new lock or re-adopt)."""
        if raw is None:
            self.misses += 1
            return False
        self.misses = 0
        if self.committed is None:
            self.buf.append(raw)
            self.buf = self.buf[-K_LOCK:]
            if len(self.buf) >= 2 and _spread_ok(self.buf, w, h):
                self.committed = _median(self.buf, w, h)
                return True
            return False
        rel, err = _relabel_match(raw, self.committed, w, h)
        if err <= CONSIST_PX:
            self.buf.append(rel)
            self.buf = self.buf[-K_LOCK:]
            self.committed = _median(self.buf, w, h)
            return False
        # disagrees: accumulate evidence for a new position
        self.alt.append(rel)
        self.alt = self.alt[-K_LOCK:]
        if len(self.alt) >= K_LOCK and _spread_ok(self.alt, w, h):
            self.committed = _median(self.alt, w, h)
            self.buf = [self.committed]
            self.alt = []
            return True
        return False

    def reorient(self, new_committed):
        """Force a relabelling (user A1-click): keep the smoothed geometry,
        adopt the user's orientation, and reset the tracking buffers."""
        self.committed = new_committed
        self.buf = [new_committed]
        self.alt = []


def _apply_a1_click(labelled, click, img_shape):
    """Relabel so the clicked corner becomes A1, using whichever symmetry
    (rotation or mirror) maps the nearest corner onto A1."""
    h, w = img_shape[:2]
    px = {k: (labelled[k][0] * w, labelled[k][1] * h) for k in LABELS}
    target = min(LABELS, key=lambda k: (px[k][0] - click[0]) ** 2
                                        + (px[k][1] - click[1]) ** 2)
    if target == "A1":
        return labelled
    if target == "H1":       # mirror across the rank axis
        return {"A1": labelled["H1"], "H1": labelled["A1"],
                "H8": labelled["A8"], "A8": labelled["H8"]}
    if target == "A8":       # mirror across the file axis
        return {"A1": labelled["A8"], "H1": labelled["H8"],
                "H8": labelled["A1"], "A8": labelled["H1"]}
    return {"A1": labelled["H8"], "H1": labelled["A8"],   # 180-degree turn
            "H8": labelled["A1"], "A8": labelled["H1"]}


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def name_by_position(res):
    """Re-name a 4-corner detection purely by WHERE the corners are in the
    frame: bottom-left = A1, bottom-right = H1, top-left = A8, top-right =
    H8.  This is authoritative because the camera is physically mounted on
    the WHITE side of the board, so the raw picture already is white's
    perspective.  Image coords: y grows downward, so bottom-left minimises
    (x - y), top-right maximises it; bottom-right maximises (x + y)."""
    pts = [tuple(v) for v in res.values()]
    bl = min(pts, key=lambda p: p[0] - p[1])
    tr = max(pts, key=lambda p: p[0] - p[1])
    br = max(pts, key=lambda p: p[0] + p[1])
    tl = min(pts, key=lambda p: p[0] + p[1])
    return {"A1": bl, "H1": br, "A8": tl, "H8": tr}


class CalibrationEngine:
    """Frame pump shared by the CLI window loop and the embedded UI screen.

    detect(frame)  - expensive; feed each raw full-res frame (worker thread)
    overlay(frame) - cheap; draw current committed state (UI thread)
    set_click(x,y) - full-res coords; queues/applies the A1 orientation click
    committed      - smoothed, latched result dict (or None)
    searching      - True while a stale/miss window means "holding last"
    """

    def __init__(self):
        self.stab = Stabilizer()
        self.pending_click = None
        self.click_pos = None
        self._shape = (720, 1280, 3)
        self.raw_found = False       # last detect saw a board structure at all
        self.status_overlay = True   # CLI draws status; UI screens turn it off

    @property
    def committed(self):
        return self.stab.committed

    @property
    def searching(self):
        return self.stab.committed is not None and self.stab.misses > 0

    def set_click(self, xy):
        self.click_pos = xy
        if self.stab.committed is not None:
            self.stab.reorient(_apply_a1_click(self.stab.committed, xy,
                                               self._shape))
            self.pending_click = None
        else:
            self.pending_click = xy

    def reset(self):
        """Forget the locked corners and start re-searching.  Used by the
        Camera screen's Reset button when the board got latched mid-move, so
        it locks onto the board's current resting place within a second."""
        self.stab = Stabilizer()
        self.pending_click = None
        self.click_pos = None
        self.raw_found = False

    def detect(self, frame):
        self._shape = frame.shape
        h, w = frame.shape[:2]
        t0 = time.time()
        res = bd.detect_board(frame)
        dt = time.time() - t0
        # timing + stage report (2026-11-05): the empty-board detection was
        # slow and nothing said where the time went
        n = getattr(self, "_log_n", 0)
        self._log_n = n + 1
        if n % 10 == 0:
            print("calibration.detect: %.0f ms | found=%s | committed=%s "
                  "| misses=%d" % (dt * 1000, res is not None,
                                   self.stab.committed is not None,
                                   self.stab.misses), flush=True)
        if res is None:
            self._dump_fail(frame)
        if res is not None:
            res = name_by_position(res)
        self.raw_found = res is not None
        self.stab.feed(res, w, h)
        tlog("detect #%d: %.0f ms | found=%s | committed=%s | misses=%d"
             % (n, dt * 1000, res is not None,
                self.stab.committed is not None, self.stab.misses))
        if self.pending_click is not None and self.stab.committed is not None:
            self.stab.reorient(_apply_a1_click(self.stab.committed,
                                               self.pending_click,
                                               self._shape))
            self.pending_click = None

    def _dump_fail(self, frame):
        """Debug (2026-11-03): keep a failing frame and say which detection
        stage gave up - the tone board's live failures were invisible
        without it."""
        n = getattr(self, "_fail_n", 0)
        self._fail_n = n + 1
        if n % 30 != 0:
            return
        try:
            cv2.imwrite("/tmp/live_detect_fail.png", frame)
            grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            th = cv2.adaptiveThreshold(
                cv2.GaussianBlur(grey, (0, 0), 3), 255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5)
            seams = 255 - th
            vp = bd._line_peaks(seams, 0)
            hp = bd._line_peaks(seams, 1)
            vl = bd._lattice(vp)
            hl = bd._lattice(hp)
            pts = bd._detect_seam_lattice(frame)
            print("live_calibrate: no detection - seam peaks v=%d h=%d | "
                  "lattice v=%s h=%s | validated=%s | frame saved to "
                  "/tmp/live_detect_fail.png"
                  % (len(vp), len(hp), "ok" if vl else "no",
                     "ok" if hl else "no", "ok" if pts is not None else "no"),
                  flush=True)
        except Exception as e:
            print("live_calibrate: debug dump failed:", e, flush=True)

    def overlay(self, frame):
        return _draw_overlay(frame, self.stab.committed, self.click_pos,
                             self.searching, status=self.status_overlay)


def _draw_overlay(frame, result, click, searching, status=True):
    h, w = frame.shape[:2]
    out = frame.copy()
    if result is None:
        if status:
            cv2.putText(out, "No board yet - position camera", (20, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
    else:
        pts = [(int(result[k][0] * w), int(result[k][1] * h)) for k in LABELS]
        colour = (0, 128, 255) if searching else (0, 255, 0)
        for i in range(4):
            cv2.line(out, pts[i], pts[(i + 1) % 4], colour, 2)
        cx = sum(p[0] for p in pts) / 4.0
        cy = sum(p[1] for p in pts) / 4.0
        fnt = cv2.FONT_HERSHEY_SIMPLEX
        for k, p in zip(LABELS, pts):
            cv2.circle(out, p, 7, (0, 0, 255), -1)
            # place labels OUTSIDE the quad, opposite the centroid, large
            dx, dy = p[0] - cx, p[1] - cy
            n = max(1.0, (dx * dx + dy * dy) ** 0.5)
            ux, uy = dx / n, dy / n
            qx, qy = p[0] + ux * 46, p[1] + uy * 46
            (tw, th), _ = cv2.getTextSize(k, fnt, 1.1, 3)
            ox = int(qx if ux >= 0 else qx - tw)
            oy = int(qy + th // 2 if uy >= 0 else qy - th // 2)
            ox = max(2, min(w - tw - 2, ox))
            oy = max(th + 2, min(h - 4, oy))
            cv2.putText(out, k, (ox, oy), fnt, 1.1, (0, 255, 255), 3)
        if status:
            if searching:
                cv2.putText(out, "HOLDING - searching...", (20, 40),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 128, 255), 2)
            else:
                cv2.putText(out, "Board locked", (20, 40),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
    if click is not None:
        cv2.circle(out, click, 7, (255, 255, 0), 2)
    return out


def _scale_for_display(frame, max_size):
    """Shrink `frame` so it fits within max_size, preserving aspect ratio."""
    h, w = frame.shape[:2]
    mw, mh = max_size
    scale = min(mw / w, mh / h, 1.0)
    if scale >= 1.0:
        return frame, 1.0
    disp = cv2.resize(frame, (int(w * scale), int(h * scale)),
                      interpolation=cv2.INTER_AREA)
    return disp, scale


def _write_points_txt(labelled):
    """Write normalised corners (0-1) in A1,H1,A8,H8 order."""
    flat = []
    for k in ("A1", "H1", "A8", "H8"):
        flat.append("%.6f" % labelled[k][0])
        flat.append("%.6f" % labelled[k][1])
    with open(mydir + "points.txt", "w") as f:
        f.write(",".join(flat))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_live():
    import camera
    camera.lock_camera()
    cap = camera.open_camera()
    if cap is None:
        return None
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*CBstate.camerafourcc))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CBstate.cameraresolution[0])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CBstate.cameraresolution[1])

    # Let auto-exposure / auto-gain settle before showing anything.
    print("Warming up the camera...")
    for _ in range(30):
        cap.read()

    engine = CalibrationEngine()
    lock = threading.Lock()
    slot = {"img": None, "seq": 0, "done": 0}
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            with lock:
                img, seq = slot["img"], slot["seq"]
                if img is None or seq == slot["done"]:
                    time.sleep(0.03)
                    continue
                slot["done"] = seq
            try:
                engine.detect(img)
            except Exception as e:
                print("live_calibrate: detection failed:", e)

    th = threading.Thread(target=worker, daemon=True)
    th.start()

    win = "Board calibration"
    mouse = {"scale": 1.0}
    click_pos = [None]

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            s = mouse["scale"]
            click_pos[0] = (int(x / s), int(y / s))
            engine.set_click(click_pos[0])

    cv2.namedWindow(win, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(win, on_mouse)

    print("Position the board so it fills the frame.")
    print("Press 's' to save, 'q' to quit.")
    print("Click the board's true A1 corner to fix orientation "
          "(works for any rotation or mirror).")

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("Camera read failed.")
                break
            with lock:
                slot["img"], slot["seq"] = frame, slot["seq"] + 1
            display, scale = _scale_for_display(engine.overlay(frame),
                                                CBstate.displaymax)
            mouse["scale"] = scale
            cv2.imshow(win, display)
            key = cv2.waitKey(30) & 0xFF
            if key == ord('q'):
                break
            elif key == ord('s'):
                if engine.committed is None:
                    print("No stable detection yet — nothing saved.")
                    continue
                _write_points_txt(engine.committed)
                print("Saved board corners to " + mydir + "points.txt")
                break
    finally:
        stop.set()
        th.join(timeout=5)
        camera.close_camera(cap)
        cv2.destroyAllWindows()


if __name__ == "__main__":
    run_live()
