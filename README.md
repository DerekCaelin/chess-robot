# chess-robot

A 3D-printed chess robot: a Raspberry Pi (or any Linux/Windows PC) drives
a camera and a robot arm, shows a touchscreen UI, and plays chess with
Stockfish.

The current build lives in `version3/` - run every command below from that
directory. Earlier builds live in `previous versions/`, kept for provenance
and reference; you can ignore them. Requires Python 3 (tested with 3.12).

## Hardware

- **Camera** - a UVC webcam mounted above the board with the whole playing
  area in view. Printable board layouts (1"/2" squares) are at the
  repository root: `chessboardprint*.png`.
- **Arm** - a [Lynxmotion AL5D servo arm](https://wiki.lynxmotion.com/info/wiki/lynxmotion/view/ses-v1/ses-v1-robots/ses-v1-arms/al5d/)
  driven through an SSC-32U controller. (The original Cartesian-arm module
  `robotmove.py` is kept as-is.)
- **Computer** - a Raspberry Pi 3B+ with a 5" touchscreen is the deployed
  target; any Linux or Windows PC works for development.

## Install (Linux)

Debian, Ubuntu and Raspberry Pi OS (on other distributions, install the
same list through your package manager):

```
sudo apt-get install python3 python3-tk python3-numpy python3-opencv \
    python3-pil python3-pil.imagetk python3-serial espeak alsa-utils \
    v4l-utils stockfish
python3 -m pip install --user chess pyttsx3
```

On a Raspberry Pi use `pi/install_pi.sh` instead - it adds the X server,
kiosk and GPIO pieces (`pi/README-pi.md` has the full bring-up). On
Windows: install Python, `pip install opencv-python numpy pillow pyserial
chess pyttsx3`, and Stockfish (`CBstate.py` already carries the usual
Windows engine path).

## Run

```
python3 ui/app.py              # windowed, for development
python3 ui/app.py --fullscreen # 800x480 kiosk (what the Pi runs)
```

The app opens on the home screen: **PLAY** starts a game against
Stockfish, **Calibrate** holds the camera and arm calibration. The console
front-end remains available for headless debugging: `python3 CBint.py`.

Nothing is required to try it on a desktop: the UI opens without the
robot, and the board detector and test suites run on any machine with the
packages above.

## First-time setup

1. **Camera calibration** - Automatic Calibration: Calibrate -> Camera ->
   Start. The board is detected automatically (including boards whose
   squares differ in wood tone rather than light/dark) and should find the
   corners in a few seconds. Manual mode is optional: tap each corner.
2. **Board calibration** - Calibrate -> Board. Place pawns at the
   instructed locations and move the gripper so that it picks up the
   piece. This maps out the board's dimensions in the arm's motion space -
   no ruler measurement required.
3. **Board & Pieces** - Calibration settings -> Board & Pieces. Every
   chess piece has a different ideal pickup height and grip level. Enter
   in the grip height and how tight the grip should be for each piece.
   Board height is a little superfluous now that pieces are dropped
   directly in the piece box for my setup, but people wanting a graveyard
   next to the board will find it handy.
4. **Arm sag** - Calibration settings -> Arm sag - The gripper will move
   to what it thinks is the right location but is impacted by gravity (as
   are we all, alas). Use arm sag to map the amount the arm dips depending
   on its reach, which will counter the drop during gameplay.
5. **Capture drop** - Calibrate -> Capture drop. If captured pieces go
   into a box or tray instead of a graveyard grid, jog the gripper until
   it hangs over the box and tap OK - the arm drops captured pieces
   straight into it.
6. **Speech** (optional) - The Pi locally generates a voice for gameplay
   and the analysis tool. espeak is included by default, but other voices
   can be used (I used piper). See "Not included in this repository"
   below.

## Configuration

- `config/defaults.json` - arm geometry (square size, reach, gripper).
- `CBstate.py` - camera (resolution, exposure lock), engine path and
  thinking time (`/usr/games/stockfish` on Debian-family systems; adjust
  if your Stockfish lives elsewhere).
- The in-app **Settings** screen, persisted to `data/settings.json`.

## Development

- Push code to the Pi: `bash pi/deploy.sh pi@chesspi`, then
  `ssh pi@chesspi '~/chess/chess-robot-v3/pi/appctl restart'`.
- Board-detector regression: `python3 tests/run_tests.py` (13 labelled
  camera frames; 11 pass, `setup_center_2` and `setup_far` are known
  failures).
- Test suites: `tests/test_*.py` and `tests/ui_smoke.py`.

---

## Credits, provenance & licensing

The 2026 build (`version3/`) merges and extends the
earlier versions in this repository. Who wrote what:

- **Richard Day** (`rpd123` on GitHub, chess@gotobiz.co.uk) — the
  foundational code: robot movement, board reading and kinematics
  (`robotmove.py`, `main.py`, `inversekinematics.py`, `calibrate_fisheye.py`,
  most of `CBstate.py`, `CBint.py`, `playermove_rpd.py`,
  `LEGO_Chess_rpd.py`), evolved from his earlier versions in this
  repository (`previous versions/` and `ALD5/`; with credit to
  www.chess.fortherapy.co.uk for parts of the original). Original
  repository:
  https://github.com/rpd123/chess-robot (Arduino firmware:
  https://github.com/rpd123/Arduino-Firmware). Distributed with his
  permission.
  **This code, and anything developed from it, must not be sold without
  Richard Day's agreement.**
- **John Eriksson** — `ChessBoard.py`, ChessBoard v2.05, GNU GPL
  (unmodified; its own header applies).
- **"scharette" (2017)** — `lib_al5_2D_IK.py`, the AL5 2D
  inverse-kinematics example.
- **Derek Caelin** — copyright 2026: the camera-calibration and vision
  pipeline (`board_autodetect.py`, `board_calibrate.py`, `board_state.py`,
  `calibrate2.py`, `camera.py`, `check_orientation.py`,
  `live_calibrate.py`, `piece_detect.py`), the touchscreen UI (`ui/`), the
  application layer (`chessapp/`), tests and tooling. These were written
  by a coding agent under my direction; I am responsible for their design,
  review and testing.

Derived files carry per-file headers saying which parts are whose, and the
git history (2022–2026) documents the evolution commit by commit.

### Not included in this repository

Runtime state and local models are deliberately kept out of git (see
`.gitignore`): camera captures (`images/*.jpg`), calibration state,
game/settings JSON, and the **voice models** (`data/voices/*.onnx`,
`data/voice_samples`, ~150 MB). The default **espeak** voice ships with
the repo - its cooked phrases are tiny - so a fresh install speaks out of
the box. The other voices are the player's own choice: they are picked in
the app and generated on the target machine with `tools/harvest_phrases.py`
and the speech prewarm (see `chessapp/speech.py` and
`tests/test_prewarm.py`).

