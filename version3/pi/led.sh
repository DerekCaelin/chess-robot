#!/bin/bash
# led.sh on|off - status LED on GPIO20 (physical pin 38, trimmed Dupont).
# "on" = UI running; wired into kiosk.sh. Uses libgpiod gpioset.
# v2 (Trixie gpiod): '-m exit' is gone and gpioset now HOLDS the line by
# default. The set-and-exit idiom is '--toggle=1,0' (set -> flip after 1ms ->
# flip back with a trailing 0 period -> exit), verified live 2026-09-19:
#   -p 0   hangs forever (rc 124 under timeout)
#   -t 1,0 returns immediately (rc 0)
# The timeout stays as a belt-and-braces guard so kiosk.sh can never hang.
case "$1" in
    on)  exec timeout 3 gpioset -c gpiochip0 -t 1,0 20=1 ;;
    off) exec timeout 3 gpioset -c gpiochip0 -t 1,0 20=0 ;;
    *)   echo "usage: led.sh on|off" >&2; exit 1 ;;
esac
