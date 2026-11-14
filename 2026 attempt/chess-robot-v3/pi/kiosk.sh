#!/bin/bash
# kiosk.sh - runs INSIDE the X session (via startx): assert the panel's native
# 800x480, apply the Screen rotation setting (display + touch matrix
# together), kill the cursor, launch the UI.
export DISPLAY=:0
DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Force the panel's NATIVE 800x480.  The panel's EDID is flaky (dmesg:
# "EDID block 0 checksum is invalid"), so X occasionally discards it and falls
# back to a generic 1024x768 DMT mode - which the 5" panel can only accept by
# downscaling (blurry, wrong aspect).  Re-assert with retries so the app always
# lands on the native mode; 1024x768 is a fallback, NOT a higher-resolution win.
force_800x480() {
    local out i
    for i in 1 2 3 4 5 6 7 8; do
        out=$(xrandr 2>/dev/null | awk '/ connected/ {print $1; exit}')
        if [ -n "$out" ]; then
            # Prefer the firmware/EDID "800x480" (the panel's own timing).
            xrandr --output "$out" --mode 800x480 2>/dev/null
            # If the flaky EDID dropped that mode (no "800x480" in the list),
            # recreate it with a standard 800x480@60 (GTF) timing ourselves.
            if ! xrandr 2>/dev/null | grep -q "current 800 x 480"; then
                xrandr --newmode "800x480_60" 29.58 800 816 896 992 \
                    480 481 484 497 -HSync +Vsync 2>/dev/null
                xrandr --addmode "$out" "800x480_60" 2>/dev/null
                xrandr --output "$out" --mode "800x480_60" 2>/dev/null
            fi
        fi
        if xrandr 2>/dev/null | grep -q "current 800 x 480"; then
            return 0
        fi
        sleep 1
    done
    return 0
}
force_800x480

# Panel rotation comes from the settings (2026-11-14): 'Screen rotation'
# in the Settings screen flips the display AND the touch matrix together
# (a display flip without the matrix makes every tap land mirrored).
# Applied here at boot so the splash comes up the right way round.
python3 - "$DIR" <<'PYEOF' || true
import sys
sys.path.insert(0, sys.argv[1])
from chessapp import display
display.apply()
PYEOF

# Hide the pointer completely: a transparent 1x1 cursor (no touch crosshair),
# with unclutter as a fallback for any widget that sets its own cursor.
xsetroot -cursor "$DIR/ui/assets/blank_cursor.xbm" \
          "$DIR/ui/assets/blank_cursor.xbm" 2>/dev/null || true
unclutter -idle 0.01 -root &

# Disable X's own blanking/DPMS - the app manages the panel power itself via
# display_gpio.sh (off on idle, on at a tap), so X must never drop the signal.
xset s off 2>/dev/null || true
xset -dpms 2>/dev/null || true
xset dpms 0 0 0 2>/dev/null || true

# If the panel ever shows wrong resolution/black, add to
# /boot/firmware/config.txt (see README-pi.md):
#   hdmi_group=2  hdmi_mode=87  hdmi_cvt=800 480 60 6 0 0 0

cd "$DIR"
"$DIR/pi/led.sh" on 2>/dev/null || true
trap '"$DIR/pi/led.sh" off 2>/dev/null || true' EXIT

# Boot splash: the Pi 3B's firmware ignores /boot/firmware/background.png, so
# show the artwork ourselves while the UI imports (cv2/numpy/PIL take several
# seconds on the 3B).  The checker stays and the ORIGINAL splash artwork
# goes on top of it (2026-11-08: the CHESSBOT wordmark is gone) - scaled to
# 80% of the screen height, centred, in a black frame.  This window runs in
# the BACKGROUND and is torn down by the app the instant the home screen is
# up (see app.py _kill_splash); the 60 s timeout is only a safety net if the
# app never starts.
python3 - "$DIR" <<'PYEOF' &
import os, sys
import tkinter as tk
from PIL import Image, ImageDraw, ImageTk
W, H = 800, 480
img = Image.new("RGB", (W, H), "#0c0a09")
d = ImageDraw.Draw(img)
for r in range(H // 80 + 1):
    for c in range(W // 80 + 1):
        if (r + c) % 2 == 0:
            d.rectangle([c * 80, r * 80, c * 80 + 80, r * 80 + 80],
                        fill="#1d1a17")
# the original splash artwork at 80% screen height, black-framed
try:
    art = Image.open(os.path.join(sys.argv[1], "pi", "bringup",
                                  "background.png")).convert("RGB")
    h = int(H * 0.8)
    w = max(1, round(art.width * h / art.height))
    if w > W * 0.9:                 # never wider than 90% of the screen
        w = int(W * 0.9)
        h = max(1, round(art.height * w / art.width))
    art = art.resize((w, h), Image.LANCZOS)
    x, y = (W - w) // 2, (H - h) // 2
    b = 4                           # the black frame
    d.rectangle([x - b, y - b, x + w + b, y + h + b], fill="#000000")
    img.paste(art, (x, y))
except Exception:
    pass                            # checker alone still boots fine
root = tk.Tk(); root.overrideredirect(True)
photo = ImageTk.PhotoImage(img)
tk.Label(root, image=photo, bg="#0c0a09").pack(fill="both", expand=True)
root.geometry("%dx%d+0+0" % (W, H))
root.update()
root.after(60000, root.destroy)   # safety net only; the app closes us first
root.mainloop()
PYEOF
echo $! > "$DIR/data/.splash.pid"

force_800x480
python3 ui/app.py --fullscreen
