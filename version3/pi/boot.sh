#!/bin/bash
# boot.sh - console entry (from .bash_profile): while the mode file says
# "ui", keep relaunching the X kiosk; in "terminal" mode hold the console
# and wait (Ctrl-C here still drops to the normal shell); on "off" power
# down.  Terminal mode HOLDS rather than exiting (2026-11-05): exiting
# dropped the supervision chain and left the kiosk dead after
# 'appctl stop' with no way to restart it over SSH - X only starts from
# this console.  'appctl restart' now always finds us waiting.
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
    if [ "$mode" != "ui" ]; then
        sleep 2
        continue
    fi
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
