#!/bin/bash
# kiosk.sh - runs INSIDE the X session (via startx): assert the panel's native
# 800x480, kill the cursor, launch the UI.  The display is now mounted
# right-side-up (the hollow-book / upside-down mount is gone), so there is NO
# 180-degree rotation and no mirrored touch matrix.
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
# seconds on the 3B).  This window runs in the BACKGROUND and is torn down by
# the app the instant the home screen is up (see app.py _kill_splash); the
# 60 s timeout is only a safety net if the app never starts.
if [ -f "$DIR/pi/bringup/background.png" ]; then
    python3 - "$DIR/pi/bringup/background.png" <<'PYEOF' &
import sys, tkinter as tk
from PIL import Image, ImageTk
root = tk.Tk(); root.overrideredirect(True)
img = ImageTk.PhotoImage(Image.open(sys.argv[1]))
tk.Label(root, image=img, bg="#232730").pack(fill="both", expand=True)
root.geometry("%dx%d+0+0" % (img.width(), img.height()))
root.update()
root.after(60000, root.destroy)   # safety net only; the app closes us first
root.mainloop()
PYEOF
    echo $! > "$DIR/data/.splash.pid"
fi

force_800x480
python3 ui/app.py --fullscreen
