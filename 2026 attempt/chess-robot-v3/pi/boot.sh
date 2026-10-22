#!/bin/bash
# boot.sh - console entry (from .bash_profile): while the mode file says
# "ui", keep relaunching the X kiosk; when the UI exits in "terminal" mode
# (or the file says terminal/off), drop to the normal shell.
DIR="$HOME/chess/chess-robot-v3"
MODE="$DIR/data/.chess_mode"

# Physical power-button watcher (GPIO3 -> FIFO).  Started here (not a systemd
# service) so it survives reboot via the console-autologin chain and needs no
# sudo.  Idempotent: skip if an instance is already watching.
pgrep -f "pi/powerbtn.sh" >/dev/null 2>&1 ||     nohup "$DIR/pi/powerbtn.sh" >/dev/null 2>&1 &

while :; do
    mode=$(cat "$MODE" 2>/dev/null || echo ui)
    if [ "$mode" = "off" ]; then
        echo ui > "$MODE"      # reset FIRST: must not re-fire next boot
        sudo systemctl poweroff
        exit
    fi
    [ "$mode" != "ui" ] && break
    if [ ! -x /usr/bin/X ]; then
        # X server package missing (e.g. fresh Lite image): say so once,
        # then stop relaunching instead of printing an infinite error loop.
        echo "X server not installed - run: sudo apt-get install -y"
        echo "  xserver-xorg xserver-xorg-video-fbdev"
        echo "then reboot. (boot.sh exiting; install_pi.sh now installs it.)"
        exit 1
    fi
    startx "$DIR/pi/kiosk.sh" -- :0 vt1 -audit 0
    # give X a moment so a crash-loop can't spin hot
    sleep 1
done
