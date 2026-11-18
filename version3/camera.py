# Copyright 2026 Derek Caelin - written by a coding agent under his direction.
# Licensing and credits: see README.md, "Credits & provenance".
# camera.py
#
# Shared webcam capture helper.
#
# Captures a single still frame at the resolution configured in CBstate,
# preferring the MJPG pixel format (compressed) so 720p/1080p are fast and
# reliable.  Falls back to fswebcam if OpenCV's VideoCapture cannot open the
# camera via V4L2.
#
# Both the game pipeline (playermove_rpd.py) and the calibration tools use
# capture_still() so the capture resolution is defined in exactly one place
# (CBstate.cameraresolution).

import cv2
import subprocess
import time
import os
import threading

import CBstate

_locked = False


_cam_lock = threading.Lock()
_cam_holder = None            # (token, cap) of the session owning _cam_lock


def _held_by_other():
    """True when ANOTHER process has the device open. Opening a Linux V4L2
    device that another process is streaming from can SEGFAULT the caller
    (VIDIOC_REQBUFS -> errno 25 -> crash inside OpenCV), so refuse instead.
    Best-effort: fuser may be absent."""
    import subprocess
    try:
        r = subprocess.run(["fuser", _dev()], capture_output=True, timeout=5)
        pids = {int(p) for p in r.stdout.split()}
        pids.discard(os.getpid())
        return bool(pids)
    except Exception:
        return False


def _open_configured():
    """Open with the safe backend and request the configured format. On
    Linux CBstate uses the DEVICE PATH plus an explicit CAP_V4L2 backend:
    opening by index runs an obsensor plugin probe that SEGFAULTS on the
    Brio, and the plain-path auto backend silently refuses 1280x720 MJPG."""
    if getattr(CBstate, "windowsos", False):
        cap = cv2.VideoCapture(CBstate.cameraportno)
    else:
        cap = cv2.VideoCapture(CBstate.cameraportno, cv2.CAP_V4L2)
    if cap.isOpened():
        cap.set(cv2.CAP_PROP_FOURCC,
                cv2.VideoWriter_fourcc(*CBstate.camerafourcc))
        w, h = CBstate.cameraresolution
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
    return cap


def open_camera(wait=6.0):
    """Serialised device open: ONE live session per process at a time, with
    a cross-process busy check. Returns the cap (call close_camera on it) or
    None if the device is busy elsewhere / held by a sibling thread."""
    global _cam_holder
    if _held_by_other():
        return None
    if not _cam_lock.acquire(timeout=wait):
        return None
    try:
        cap = _open_configured()
        if cap is None or not cap.isOpened():
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
            _cam_lock.release()
            return None
        global _cam_holder
        _cam_holder = (threading.get_ident(), cap)   # identity-checked on close
        return cap
    except Exception:
        _cam_lock.release()
        return None


def close_camera(cap):
    """Release a session opened by open_camera(); frees the process lock.
    Safe to call with None or a cap that was already closed. cv2 objects
    reject custom attributes, so ownership is tracked by object identity."""
    global _cam_holder
    try:
        if cap is not None:
            cap.release()
    except Exception:
        pass
    if cap is not None and _cam_holder is not None \
            and _cam_holder[1] is cap:
        _cam_holder = None
        if _cam_lock.locked():
            _cam_lock.release()


def _dev():
    """Device path whether cameraportno is an index (0) or a path string."""
    p = CBstate.cameraportno
    return p if isinstance(p, str) else "/dev/video%d" % p


def _get_ctrl(dev, name):
    """Read one v4l2 control value, or None."""
    try:
        out = subprocess.run(["v4l2-ctl", "-d", dev, "--get-ctrl=" + name],
                             capture_output=True, text=True,
                             timeout=5).stdout
        for ln in out.splitlines():
            if ":" in ln:
                return int(ln.split(":")[1].strip())
    except Exception:
        pass
    return None


def lock_camera():
    """Pin exposure and white balance - to values that suit the room.

    Auto-exposure and auto white-balance drift between frames, which breaks
    background subtraction and colour models (the empty reference and the
    pieces capture would get different lighting).  Locking them is essential
    for consistent piece detection.  2026-11-16: the lock used to pin a
    fixed config value and the board went dark the moment the room's light
    changed - the checker detector then had nothing to find.  The camera's
    own auto exposure/white balance runs briefly first and the SETTLED
    values are what get locked, so the lock matches the light in the room.
    """
    global _locked
    if _locked or not getattr(CBstate, "camera_lock", False):
        return
    dev = _dev()
    try:
        # let the sensor find this room's light
        for name, val in (("auto_exposure", 0),        # auto
                          ("white_balance_automatic", 1)):
            subprocess.call(["v4l2-ctl", "-d", dev,
                             "--set-ctrl=%s=%d" % (name, val)])
        time.sleep(2.0)              # AE/AWB settle
        exp = _get_ctrl(dev, "exposure_time_absolute")
        wb = _get_ctrl(dev, "white_balance_temperature")
        if not exp:
            exp = CBstate.camera_exposure    # fall back to the config value
        if not wb:
            wb = CBstate.camera_white_balance
        pairs = [
            ("auto_exposure", 1),  # 1 = manual
            ("exposure_time_absolute", int(exp)),
            ("white_balance_automatic", 0),
            ("white_balance_temperature", int(wb)),
        ]
        for name, val in pairs:
            subprocess.call(["v4l2-ctl", "-d", dev,
                             "--set-ctrl=%s=%d" % (name, val)])
        _locked = True
        print("camera.lock_camera: exposure/wb locked "
              "(exposure=%d, wb=%dK - matched to the room)"
              % (exp, wb))
    except FileNotFoundError:
        print("camera.lock_camera: v4l2-ctl not found; camera controls NOT locked.")
    except Exception as e:
        print("camera.lock_camera: failed:", e)


def capture_still(path):
    """Capture a single frame to `path`.

    Returns True on success, False if both OpenCV and fswebcam fail.
    """
    lock_camera()
    if _capture_opencv(path):
        return True
    print("camera.capture_still: OpenCV capture failed, trying fswebcam...")
    return _capture_fswebcam(path)


def _capture_opencv(path):
    cap = open_camera()
    if cap is None or not cap.isOpened():
        print("camera.capture_still: cannot open camera %s" % _dev())
        close_camera(cap)
        return False

    fourcc = cv2.VideoWriter_fourcc(*CBstate.camerafourcc)
    w, h = CBstate.cameraresolution
    cap.set(cv2.CAP_PROP_FOURCC, fourcc)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)

    got_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    got_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print("camera.capture_still: requested %dx%d, got %dx%d" % (w, h, got_w, got_h))

    # Warm-up grabs so auto-exposure / auto-gain settle before the real shot.
    for _ in range(CBstate.camerawarmup):
        cap.read()

    ok, frame = cap.read()
    close_camera(cap)      # NOT cap.release(): only close_camera frees the
                           # process lock - a bare release left the lock held
                           # and every later capture failed 'camera
                           # unavailable' (2026-11-03, the soak test: 1 of 40
                           # captures succeeded)

    if not ok:
        print("camera.capture_still: read() returned no frame")
        return False

    if not cv2.imwrite(path, frame):
        print("camera.capture_still: failed to write " + path)
        return False
    return True


def _capture_fswebcam(path):
    w, h = CBstate.cameraresolution
    cmd = (
        'fswebcam -d {d} -r {w}x{h} -S 20 '
        '--set "Power Line Frequency"="50 Hz" --no-banner '
        '--delay 2 --set brightness=50% --jpeg 99 '
        '--save "{path}"'
    ).format(d=_dev(), w=w, h=h, path=path)
    print("camera.capture_still: " + cmd)
    return subprocess.call(cmd, shell=True) == 0
