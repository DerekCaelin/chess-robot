# chess-robot  
  
For the Android/GUI version see the instructions in the versiongui directory.
    
3D printed chess robot, Python code for Raspberry Pi, other Linux or Windows PC. New users should use version 2.

Install Stockfish engine, Python 3, python-pip, espeak

pip install pyserial  
pip install psutil  
pip install numpy  
pip install opencv-python  
pip install stockfish  
pip install pyttsx3  

Run CBint.py from Thonny or elsewhere.

The camera should be positioned directly over the centre of the chess board and lined up pretty well. Then camera calibration asks you to click on the four corners of the playing area of the chessboard (on the image), and you are shown the result. Click them in the order requested. (The robot arm should not be in the way, and all the pieces must be in their correct starting positions). By "playing area" we mean the chessboard squares, NOT including any external margin).

The calibration is remembered even if all systems are shut down, so if the robot, board and camera have not been moved, recalibration can optionally be avoided when a new game is started

The camera should be at least 56 cm above the board. Square size 3.4 cm. Tallest piece: 5.5 cm. The image of a piece should not fall across an adjacent square.

Calibrating the Cartesian robot arm: The lower strut must be vertical and the upper strut horizontal and coming out straight over the board.

Code configuration: You will need to check squaresize, axistorow8, etc. in robotmove.py. Check the declarations in CBstate.py.

Axistorow8 is the distance in mm horizontally along the y coordinate, between the robot’s vertical axis and the centre of row 8. 


The RPi or PC is connected to the Arduino by a USB A/B cable (printer cable). Or alternatively via Bluetooth (not BLE), using an HC-05 Bluetooth module.

Both USB and IP cameras are supported. Calibration for fish-eye distortion is provided, and is more likely to be needed for IP cameras. USB cameras are often OK without it.

Arduino code is at https://github.com/rpd123/Arduino-Firmware 
  
===  
Version1-old only:  
pip install pillow
pip install psutil  
On RPi install fswebcam, aplay

---

## Credits & provenance (section added in 2026)

The 2026 build (`2026 attempt/chess-robot-v3/`) merges and extends the
earlier versions in this repository. Who wrote what:

- **Richard Day** (chess@gotobiz.co.uk) — the foundational code: robot
  movement, board reading and kinematics (`robotmove.py`, `main.py`,
  `inversekinematics.py`, `calibrate_fisheye.py`, most of `CBstate.py`,
  `CBint.py`, `playermove_rpd.py`, `LEGO_Chess_rpd.py`), evolved from his
  `version1-old` / `version2` / `versiongui` / `ALD5` versions (with credit
  to www.chess.fortherapy.co.uk for parts of the original). Distributed
  with his permission: *"Feel free to distribute the code, as long as it
  contains my copyright statements and the classification that it or
  anything developed from it cannot be sold without my agreement."*
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

