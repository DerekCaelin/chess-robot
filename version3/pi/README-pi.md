# Raspberry Pi bring-up & dev loop

Target: Pi 3B+, 5" HDMI+USB touch "hat" display (mounted **upside down** ->
software rotation handled here), direct micro-USB supply (PiSugar retired),
SSC-32U arm, webcam. Pi OS **Lite 64-bit (Trixie)** - the GPIO tools there
are libgpiod **v2** (`gpiod` package; `-e rising|falling`, chip via `-c`;
the old `-r/-f/-m exit` flags and `libgpiod-utils` package no longer
exist). Console only.

## One-time (fresh flash)

0. Raspberry Pi Imager -> OS customisation (settings gear): hostname
   `chesspi`, username `pi`, enable SSH (password auth), **Wi-Fi SSID +
   passphrase AND wireless country set up front**. The Pi then boots
   straight onto the LAN and every later step is `pi@chesspi` - no IP
   hunting. With the enable-verify write, always tick **verify**.
1. Copy this repo to the Pi **once** (any way you like, e.g. from the PC):
   `scp -r version3 pi@chesspi:~/chess/chess-robot-v3`
2. `cd ~/chess/chess-robot-v3 && bash pi/install_pi.sh`
3. raspi-config: 2 Boot Options -> B2 Console autologin.
4. `sudo reboot` -> kiosk UI should appear on the touch screen (boot.sh loop
   via .bash_profile).
5. Camera check: `v4l2-ctl --list-devices` (expect /dev/video0) and run the
   vision regression on the Pi itself: `python3 tests/run_tests.py` ->
   expect 11 PASS (`setup_center_2` and `setup_far` are known failures).
6. First-time calibration: PLAY is fine, but do **Calibrate -> Camera** on
   the real board (Pi's images/points.txt is device-local, created on demand).

If the display is black/wrong resolution, add to
/boot/firmware/config.txt and reboot:
`hdmi_group=2  hdmi_mode=87  hdmi_cvt=800 480 60 6 0 0 0`

## Daily dev loop (no SD card swaps, ever)

From the PC repo root:

```bash
bash pi/deploy.sh pi@chesspi                      # rsync code only
ssh pi@chesspi '~/chess/chess-robot-v3/pi/appctl restart'
ssh pi@chesspi                                    # watch directly if needed
```

The UI's "terminal" corner button drops the Pi to a console (mode file);
`~/chess/chess-robot-v3/pi/appctl start` (or the shell alias `chessui` you
may add) brings the UI back. "Power" button shuts down cleanly.

## Physical power button (final design - solder-under, no PiSugar)

The PiSugar has been retired: the Pi runs from a direct micro-USB supply,
and **one panel button does both directions**:

- **Short press while running** -> `powerbtn.sh` daemon -> UI modal
  *"Shut down? [Shut down] [Restart] [Cancel]"* -> clean `systemctl
  poweroff`. If the UI isn't consuming the request within 15 s (terminal
  mode), the daemon shuts down on its own instead.
- **Press while halted** (after any shutdown, ~17 mA draw, LED dark) ->
  the 3B+ PMIC boots the Pi -> autologin -> kiosk. Same button.
- A ~30 s ignore window after boot swallows the boot press itself.

**Wiring** (button = polarity-free switch; trimmed Duponts for the LED):

| signal | Pi GPIO header | method |
|---|---|---|
| button (both wires) | pin 5 (GPIO3) + pin 6 (GND) | stripped fine wires soldered to the **underside** header joints, routed out the edge, hot-glue strain relief; display hat seats normally |
| LED + | pin 38 (GPIO20) via ~330 R (skip R if the button module is rated 3-24 V internally) | trimmed Dupont |
| LED - | pin 34 (GND) | trimmed Dupont |

> The status LED is currently **skipped** (not wired, 2026-09-20) - the
> operator suspected it in an earlier board failure. `led.sh` is harmless
> when nothing is attached; wire it only when the rest is proven stable.

### Touchscreen (XPT2046, over SPI)

The 5" panel's touch is NOT USB - it is a resistive XPT2046 controller on the
SPI0 bus, carried through the panel's pin header to the Pi (so the panel must
be SEATED on the header for touch to work). Enable it with:

    dtparam=spi=on
    dtoverlay=ads7846,cs=1,penirq=25,penirq_pull=2,speed=50000,keep_vref_on=0,swapxy=0,pmax=255,xohms=150,xmin=200,xmax=3900,ymin=200,ymax=3900

The device appears as "ADS7846 Touchscreen" (xinput/evdev); kiosk.sh already
greps for 'touch' and applies the 180-degree matrix for the upside-down mount.
If the panel's IRQ is on GPIO17 instead (different revision), swap penirq=25
for penirq=17. install_pi.sh adds this overlay automatically.

### Display resolution / flaky-EDID fix

The 5" panel's EDID can go intermittent after rework (dmesg spams
`EDID block 0 checksum is invalid` with a DIFFERENT remainder each ~10s =
a loose DDC connection, NOT a dead EEPROM). The symptom is X falling back
to 1024x768 -> an edge bar + vertically-squeezed UI. Fix WITHOUT touching the
panel: force 800x480 in firmware and stop trusting the EDID:

1. Ensure config.txt has (it ships with these already appended):
   `hdmi_group=2`, `hdmi_mode=87`, `hdmi_cvt=800 480 60 6 0 0 0`
2. COMMENT OUT the base-image line that suppresses them:
   `#disable_fw_kms_setup=1`   (otherwise the firmware ignores the hdmi_cvt)
3. Reboot.

Belt-and-braces (if the KMS driver still misbehaves): ship a fixed EDID -
drop a good 800x480 EDID blob in /lib/firmware/edid/800x480.bin and add
`drm.edid_firmware=HDMI-A-1:edid/800x480.bin` to /boot/firmware/cmdline.txt.
kiosk.sh ALSO re-asserts 800x480 via xrandr every boot as a last line of
defence, but that only works after the panel has already latched a mode, so
the config.txt fix is the real one.

### Optional: display power switch (dark at halt)

The Pi 3B leaves its 5V rail live after `poweroff`, so an HDMI display's
backlight keeps glowing. Kill it with a HIGH-SIDE switch (low-side would be
defeated by the HDMI-cable ground). P-MOSFET on the display's 5V, driven by
an NPN level-shifter:

Because the display HAT covers the TOP of the 40-pin header, tap the three
signals from the UNDERSIDE of the Pi's header (the same solder-the-underside
joints technique used for the power button): pin 2 (5V), pin 6 (GND), pin 11
(GPIO17). Or lift the hat on an extra-tall stacking header to expose them.

- P-MOSFET (IRLML6402 / IRF9540): Source -> pin 2 (5V), Drain -> display +5V,
  Gate -> 10k to 5V (pull-up) AND NPN collector.
- NPN (2N3904/BC547): Collector -> P-FET gate, Emitter -> pin 6 (GND),
  Base -> 1k -> GPIO17 (pin 11), with a 10k pull-down from GPIO17 to GND.
  The pull-down only guarantees **OFF at power-on** (the pad is tri-state
  before boot). It does NOT make the pin float on a soft halt: the Pi latches
  the last `gpioset` value and the 3V3 rail stays live after `poweroff`, so a
  pin driven HIGH stays HIGH and the display stays lit. You must drive it LOW.
- Software: `pi/display_gpio.sh on|off` drives GPIO17. It uses `raspi-gpio set
  17 dh|dl` (a non-blocking one-shot from `raspberrypi-utils` whose setting
  persists) and falls back to a brief `gpioset` hold-then-exit if that tool is
  absent. `install_pi.sh` writes a system unit `display-power.service`
  (`ExecStart=…display_gpio.sh on`, `ExecStop=…display_gpio.sh off`,
  `RemainAfterExit`) so the panel is powered at boot and driven **LOW on
  `poweroff` AND `reboot`** - that shutdown hook is essential because the pin
  does not release itself at halt. The app also calls `display_gpio.sh off` in
  its power-off/reboot handlers. Do NOT also set ads7846 `penirq=17` (keep the
  default 25) or it collides with the switch on GPIO17.
- Display GND stays on pin 6 (unswitched).

Polarity: the **button is indifferent** (it's just a contact). The **LED is
not**: long leg / non-flat side = + -> resistor -> pin 38; short leg / flat
side = - -> pin 34. Backwards it just won't light (3.3 V is safe) - swap.
If unsure whether a resistor is built in (meter reads ~500R-2k across the
LED terminals, or spec says 3-24 V), just include yours - the LED is merely
dimmer if one is already there.

LED is driven by the kiosk (`led.sh`): **solid = UI alive**, off when shut
down; a heartbeat blink during play is a later polish item.

Design notes:
- The kernel `gpio-shutdown` overlay is deliberately NOT installed: it
  would own GPIO3 exclusively and starve the daemon (which gives the
  nicer cancellable modal). If you ever prefer kernel-style behavior
  (3-s hold = off) instead: load `dtoverlay=gpio-shutdown`, stop the
  powerbtn service, and set `dtparam=i2c_arm=on` back if you also want
  PiSugar-style I2C later. One owner per pin.
- Frozen OS escape: unplug USB (no long-hold hard-off exists with this
  design; the modal path assumes a living OS or falls back after 15 s).
- Bring-up check once, after first install: UI -> modal Shut down ->
  wait for LED dark -> **press button -> should boot back into kiosk**.
  If your firmware doesn't wake on GPIO3, tell us - fallback is the
  overlay route above.

**Later - lid switch:** a normally-open reed/microswitch across GPIO4
(pin 7)+GND polled by the same daemon (second gpiomon loop) can blank
DPMS when the lid closes.

## Files here

| file | side | what |
|---|---|---|
| install_pi.sh | pi | apt deps, gpio group, sudoers poweroff/reboot, config.txt, autostart, powerbtn.service install |
| boot.sh | pi | console loop: startx kiosk while mode==ui, else drop to shell |
| kiosk.sh | pi | xrandr 180 + touch CTM + unclutter + led on + run ui/app.py |
| led.sh | pi | GPIO20 status LED on/off (called by kiosk) |
| powerbtn.sh + powerbtn.service | pi | GPIO3 press watcher -> UI fifo / fallback poweroff |
| deploy.sh | pc | rsync code (never data/ or images/) |

## GPIO wiring rules (learned the hard way, 2026-09-19)

- **Never wire or re-seat anything with the Pi powered.** The 40-pin header
  carries 5V (pins 2/4) and 3V3 (pins 1/17) next to every signal row; a
  Dupont one hole off, or a stray strand, shorts a rail - one such event
  killed a board mid-session (symptoms: screen flicker -> "no signal" ->
  red LED on, green dead, no boot, network gone).
- After any brownout, before writing a board off: unplug power and wait
  60 s (PMIC latch-up can self-recover), strip ALL wires off the header,
  boot with a KNOWN-GOOD card in a clean slot, and meter **3V3 (pin 1) vs
  GND (pin 6)**: ~3.3 V present but still no boot -> SoM dead; dead/low
  3V3 with 5V solid -> rail regulator damage. Either way the fix is a
  replacement board (3B+ has no USB device-boot rescue); the SD card and
  everything in this repo survive - a spare Pi reboots straight into the
  daily dev loop above.
| appctl | pi | start/stop/restart/status for the dev loop |

## Known current limits

- One game loop at a time; leaving via the in-game menu releases the camera
  only after the current move-detect wait returns (stop hook in place).
- Speech needs `espeak` (installed); volume/mute live in settings.
- The 800x480 layout assumes `displaymax` untouched; screens are scroll-free.
