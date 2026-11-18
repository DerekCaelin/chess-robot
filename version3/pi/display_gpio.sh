#!/bin/bash
# display_gpio.sh on|off - drive the 5" display power MOSFET pin (GPIO17).
#
# The Pi does NOT release a GPIO on a soft halt (3V3 stays live and the pad
# latches), so "off at shutdown" must be an explicit drive LOW; the display
# power systemd unit calls this on|off.  Two mechanisms, best one first:
#   1. raspi-gpio (raspberrypi-utils): a NON-blocking one-shot that sets the
#      pad high/low and persists with no process holding it.  Preferred.
#   2. gpioset (libgpiod) fallback: BLOCKS, so we request the value, hold
#      ~0.4 s, then kill the process - the Pi kernel latches the value on exit.
set -u
V=""
case "${1:-}" in
  on)  V=h ;;
  off) V=l ;;
  *) echo "usage: $0 on|off" >&2; exit 2 ;;
esac

if command -v raspi-gpio >/dev/null 2>&1; then
    if [ "$V" = h ]; then
        raspi-gpio set 17 dh </dev/null >/dev/null 2>&1
    else
        raspi-gpio set 17 dl </dev/null >/dev/null 2>&1
    fi
    exit 0
fi

# fallback: gpioset holds-and-blocks, so set, hold, then exit to latch it.
N=1; [ "$V" = l ] && N=0
gpioset -c gpiochip0 "17=$N" </dev/null >/dev/null 2>&1 &
P=$!
sleep 0.4
kill "$P" 2>/dev/null
wait "$P" 2>/dev/null
exit 0
