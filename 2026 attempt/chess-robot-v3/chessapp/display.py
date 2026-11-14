# display.py - panel rotation (2026-11-14).  The DISPLAY and the TOUCH
# matrix flip together or every tap lands mirrored; this module owns both
# so the kiosk (at boot) and the Settings screen (live) cannot disagree.
#
# The rotation lives in data/settings.json as display.rotation:
# "normal" (default) or "inverted".

import json
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# the 3x3 Coordinate Transformation Matrix per rotation (9 floats)
MATRIX = {
    "normal": "1 0 0 0 1 0 0 0 1",
    "inverted": "-1 0 1 0 -1 1 0 0 1",
}


def rotation():
    """The configured rotation from data/settings.json, 'normal' unless it
    is explicitly inverted."""
    try:
        with open(os.path.join(ROOT, "data", "settings.json")) as f:
            v = (json.load(f).get("display") or {}).get("rotation")
        return v if v in MATRIX else "normal"
    except (OSError, ValueError):
        return "normal"


def apply(rot=None):
    """Rotate the panel and the touch matrix to `rot` (default: whatever
    the settings say).  Returns the rotation that is now active."""
    rot = rot if rot in MATRIX else rotation()
    try:
        out = subprocess.run(["xrandr"], capture_output=True, text=True,
                             timeout=5).stdout
        name = next((ln.split()[0] for ln in out.splitlines()
                     if " connected" in ln), None)
        if name:
            subprocess.run(["xrandr", "--output", name, "--rotate", rot],
                           capture_output=True, timeout=5)
    except Exception:
        pass
    try:
        names = subprocess.run(["xinput", "list", "--name-only"],
                               capture_output=True, text=True,
                               timeout=5).stdout.splitlines()
        dev = next((n for n in names if "touch" in n.lower()), None)
        if dev:
            subprocess.run(
                ["xinput", "set-prop", dev,
                 "Coordinate Transformation Matrix"] + MATRIX[rot].split(),
                capture_output=True, timeout=5)
    except Exception:
        pass
    return rot
