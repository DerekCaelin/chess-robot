#!/bin/bash
# install_pi.sh - one-time setup on the Raspberry Pi 3B+ (Pi OS Lite, Bookworm).
# Run AS THE 'pi' USER from the repo root:  bash pi/install_pi.sh
#
# Hardware assumptions (see README-pi.md wiring table):
#   - power button: GPIO3 (pin 5) <-> GND (pin 6), wires soldered to the
#     UNDERSIDE header joints; display hat sits normally.
#   - status LED: GPIO20 (pin 38) via ~330R -> LED+, LED- -> GND (pin 34).
#   - Pi powered directly by micro-USB (PiSugar retired; button wakes from
#     halted state via the PMIC).
set -e

sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    python3-tk python3-numpy python3-opencv python3-pil python3-pil.imagetk python3-serial \
    espeak alsa-utils v4l-utils avahi-daemon \
    xinit xserver-xorg xserver-xorg-video-fbdev x11-xserver-utils xinput unclutter \
    gpiod \
    stockfish rsync

# raspi-gpio (non-blocking one-shot GPIO set, used by display_gpio.sh). Not
# present on a bare Lite image; best-effort so a missing package never aborts
# install_pi.sh (the helper falls back to `gpioset`).
sudo apt-get install -y --no-install-recommends raspberrypi-utils \
    || echo "(raspberrypi-utils unavailable; display_gpio.sh will use gpioset)"

# pyttsx3 (not packaged)
python3 -m pip install --user --break-system-packages pyttsx3 || true

# serial + gpio access for this user
sudo usermod -aG dialout,video,gpio "$USER" || true

# passwordless poweroff/reboot for the power button + UI icon
echo "$USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl poweroff, /usr/bin/systemctl reboot" | \
    sudo tee /etc/sudoers.d/chessui >/dev/null

# boot tweaks:
#   - splash off
#   - i2c explicitly off (frees GPIO2/3; nothing uses i2c now PiSugar is out)
#   - NOTE: deliberately NO gpio-shutdown overlay - the power-button daemon
#     owns GPIO3. See README for the overlay alternative.
if ! grep -q '^disable_splash' /boot/firmware/config.txt 2>/dev/null; then
    printf '\ndisable_splash=1\ndtparam=i2c_arm=off\ndtoverlay=disable-bt\n' | \
        sudo tee -a /boot/firmware/config.txt >/dev/null
fi

# XPT2046 resistive touchscreen (5" HDMI panel) - SPI + ads7846 overlay.
# Idempotent, so a re-run after wiring the touch picks it up too.
if ! grep -q 'dtoverlay=ads7846' /boot/firmware/config.txt 2>/dev/null; then
    printf '\ndtparam=spi=on\ndtoverlay=ads7846,cs=1,penirq=25,penirq_pull=2,speed=50000,keep_vref_on=0,swapxy=0,pmax=255,xohms=150,xmin=200,xmax=3900,ymin=200,ymax=3900\n' | \
        sudo tee -a /boot/firmware/config.txt >/dev/null
    echo "ads7846 touch overlay enabled (XPT2046 SPI touchscreen)."
fi

# autostart: run the kiosk loop after console autologin
PROFILE="$HOME/.bash_profile"
if ! grep -q 'chess-ui-boot' "$PROFILE" 2>/dev/null; then
    cat >> "$PROFILE" <<'EOF'
# chess-ui-boot: auto-launch the chess UI kiosk (see pi/README-pi.md)
if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    "$HOME/chess/chess-robot-v3/pi/boot.sh"
fi
EOF
fi

# power-button watcher as a user service
SD="$HOME/.config/systemd/user"
mkdir -p "$SD"
cp "$(dirname "$0")/powerbtn.service" "$SD/"
if systemctl --user daemon-reload 2>/dev/null && \
   systemctl --user enable powerbtn.service 2>/dev/null; then
    echo "powerbtn.service enabled (starts on login session)."
else
    echo "NOTE: enable the watcher after next login:"
    echo "  systemctl --user enable --now powerbtn.service"
fi

# display power switch (optional MOSFET on GPIO17, README-pi.md): a SYSTEM
# oneshot that powers the panel at boot and - crucially - drives GPIO17 LOW at
# halt/reboot.  The Pi latches gpioset values and keeps 3V3 live after
# `poweroff`, so without an explicit LOW the backlight would stay on; the
# display_gpio.sh helper holds-then-exits so the level sticks.
HELPER="$(cd "$(dirname "$0")" && pwd)/display_gpio.sh"
if [ -f "$HELPER" ]; then
    chmod +x "$HELPER"
    sudo tee /etc/systemd/system/display-power.service >/dev/null <<EOF
[Unit]
Description=5" HDMI display power MOSFET (GPIO17): on at boot, off at halt/reboot

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash $HELPER on
ExecStop=/bin/bash $HELPER off

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable display-power.service
    echo "display-power.service enabled (GPIO17 panel on at boot, off at halt)."
fi

echo
echo "Install done. Enable console autologin if not already:"
echo "  sudo raspi-config -> Boot Options -> Auto Login -> Console"
echo "Then: sudo reboot  (kiosk comes up; then run the README bring-up checks)"
