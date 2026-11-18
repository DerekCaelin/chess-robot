#!/bin/bash
# powerbtn.sh - GPIO3 (physical pin 5) power-button watcher for the chess UI.
#
# Short press (<5 s): post a "power-request" to the UI fifo -> the UI shows a
#   cancellable modal (Shut down / Restart / Cancel).
# If nothing consumes the request within 15 s (UI not running, e.g. terminal
#   mode) fall back to a clean shutdown anyway.
# Hold >= 5 s: ignored here (reserved; the kernel gpio-shutdown overlay is
#   deliberately NOT loaded because it would hold the GPIO line exclusively.
#   A frozen system escapes via the power unplug - see README-pi.md.)
#
# Wake-from-halt on a press is a Pi 3B+ firmware/PMIC property (GPIO3),
# independent of this daemon: with the system halted, a press powers it back
# on. Verify once at bring-up (README checklist).
DIR="$(cd "$(dirname "$0")/.." && pwd)"
FIFO="$DIR/data/.powerbtn.fifo"
mkdir -p "$DIR/data"
[ -p "$FIFO" ] || mkfifo "$FIFO"

was_pressed() {
    # returns 0 if released within 5 s of the press, 1 if held longer
    sleep 0.15
    if timeout 5 gpiomon -q -e rising -n 1 -c gpiochip0 3 2>/dev/null; then
        return 0
    fi
    return 1
}

notify_and_maybe_poweroff() {
    ( printf 'power-request\n' > "$FIFO" ) &
    writer=$!
    n=0
    while kill -0 "$writer" 2>/dev/null && [ "$n" -lt 15 ]; do
        sleep 1
        n=$((n + 1))
    done
    if kill -0 "$writer" 2>/dev/null; then
        # UI never read the request (terminal mode / hung): shut down anyway
        kill "$writer" 2>/dev/null
        sudo -n systemctl poweroff 2>/dev/null || systemctl poweroff 2>/dev/null
    fi
    wait "$writer" 2>/dev/null
}

while :; do
    if gpiomon -q -e falling -n 1 -c gpiochip0 3 2>/dev/null; then
        if was_pressed; then
            notify_and_maybe_poweroff
        fi
    else
        sleep 2      # gpio access problem - back off rather than spin
    fi
done
