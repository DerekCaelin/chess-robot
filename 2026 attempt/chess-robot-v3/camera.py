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


def lock_camera():
    """Pin exposure and white balance to fixed values.

    Auto-exposure and auto white-balance drift between frames, which breaks
    background subtraction and colour models (the empty reference and the
    pieces capture would get different lighting).  Locking them is essential
    for consistent piece detection.
    """
    global _locked
    if _locked or not getattr(CBstate, "camera_lock", False):
        return
    dev = _dev()
    pairs = [
        ("auto_exposure", 1),  # 1 = manual
        ("exposure_time_absolute", CBstate.camera_exposure),
        ("white_balance_automatic", 0),
        ("white_balance_temperature", CBstate.camera_white_balance),
    ]
    try:
        for name, val in pairs:
            subprocess.call(["v4l2-ctl", "-d", dev,
                             "--set-ctrl=%s=%d" % (name, val)])
        _locked = True
        print("camera.lock_camera: exposure/wb locked "
              "(exposure=%d, wb=%dK)" % (CBstate.camera_exposure,
                                         CBstate.camera_white_balance))
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
    cap.release()

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
