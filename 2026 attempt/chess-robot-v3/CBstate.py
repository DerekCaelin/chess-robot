import os
import platform
import logging
import numpy

cbstate = ""

scale_percent = 100  # percent of original size, to shrink image to fit screen if needed

# white should be on left of the Straightened Image
#rotation = -1   # do not rotate
rotation = 0  #cv2.ROTATE_90_CLOCKWISE
#rotation = 1   #cv2.ROTATE_180
#rotation = 2  #cv2.ROTATE_90_COUNTERCLOCKWISE
mirrorimage = False

# ---------------------------------------------------------------------------
# AL5D arm physical constants
# (REMEASURED at bench 2026-09-16 for the wood board + current arm mod;
#  old grey-board values in comments.  Verify by ruler before trusting fits.)
# ---------------------------------------------------------------------------
AL5D_squaresize  = 1.1875   # inches per square (1 3/16" board; was 1.0").
                            # Recalibrate after any change.
# NOTE stickout/radscale are FITTED affine constants (arm_probe --fit,
# 2026-09-16), not physical measurements: planar dist -> IK radius is
# X = radscale * (planar + stickout).  Re-fit with arm_probe after any
# horn/servo/board-geometry change; --verify should stay <= 0.2 in.
AL5D_stickout    = 4.186  # 2026-09-16 fit (2026-09-18 rebake reverted: demanded
                          # unreachable reach, IK saturated to the all-middle pose)
AL5D_Gfudge      = 5.749  # 2026-09-16 fit (09-18 rebake -0.566 reverted
                          # with the rest: probe data predated the white-side camera)
AL5D_axistorow0  = 3.875  # axis -> CENTERLINE of near rank-8 row (3 7/8" measured,
                           # 1 3/16" board).  (grey: 4.4)
AL5D_radscale    = 1.0     # planar->IK radius scale (1.0 = wrist radius equals the
                           #  planar board distance; re-fit with arm_probe)
# Board-labelling orientation vs the arm model (axis beside rank 8, x>0
# towards the a file).  A board calibration can come in any of 4 labelling
# orientations; arm calibration auto-detects the flips from the tap pattern
# (symptom seen 2026-09-17: arm appearing 180 deg from its highlighted
# square).  Persisted as arm.flip_rank / arm.flip_file.
AL5D_flip_rank   = False
AL5D_flip_file   = False
# Gripper length: wrist pivot -> finger tips (inches). A hidden constant - the
# arm commands the WRIST (the IK's native output); the finger tips hang this
# far below it. All user-facing heights are GRIPPER-TIP heights and are
# converted to wrist heights internally: Y_wrist = Y_tip + gripper_len.
AL5D_gripper_len = 4.5

# Grip height model (all board-relative, measured UP from the board surface):
#   Grip_height = piece_h + pickheightadj + pickheightreachslope * reach
#   Y_wrist     = Grip_height + gripper_len
# pickheightadj        - pure adjustment; POSITIVE raises the gripper.
# pickheightreachslope - adjustment per inch of reach; POSITIVE raises the
#                        gripper as the arm extends further.
# (These two replace the old pickheight_slope / adjforlen.)
AL5D_pickheightadj = 0.0
AL5D_pickheightreachslope = 0.0
AL5D_extraheight   = 0.0   # LEGACY - retired; kept only so old files resolve
# cruise/park are GRIPPER-TIP heights (what the user sees); the arm adds
# gripper_len to get the wrist height it commands.
AL5D_cruise_height = 3.6   # inches — gripper-tip travel height
AL5D_extrawidth    = 10     # degrees (-30 + 35 + 5)
AL5D_park_reach   = 8.0    # inches — IK reach (X) of the park/rest pose.
                           # EXTENDED past the measured droop (6.9) on
                           # 2026-11-05: at 6.9 the arm's mass sat behind the
                           # shoulder and it collapsed BACKWARDS at park and
                           # energise.  At 8.0 the mass rests in front of the
                           # shoulder pivot so the arm leans forward and holds.
AL5D_park_height  = 0.4    # inches — gripper-tip height at the rest pose (~ the
                           #  droop; lower = falls less when torque is cut)
AL5D_cruise_margin = 0.15  # inches — safety inset from the A+B link ceiling used
                           #  to pick a far-edge reach's travel height (reach-aware
                           #  cruise). Lower it to let the arm ride higher over far squares.
AL5D_grave_gap     = 1.0   # inches — graveyard column gap beyond the a-file edge

# Height of the shoulder pivot (servo output shaft) above the BOARD SURFACE.
# The 2D IK's Y=0 is the shoulder pivot, but every Y in the code is board-relative,
# so we subtract this before the IK: Y_ik = Y_board - shoulder_height. Measured
# 2.5" for the 1 3/16" board (board surface -> shoulder shaft centre).
AL5D_shoulder_height = 2.5

# Wrist servo pulse direction. The 2D IK outputs the wrist ANGLE; the servo
# pulse is 180 - Wrist in the stock arm. +1 = complement (180 - Wrist, the
# correct polarity for the modified arm), -1 = direct (Wrist). Not exposed -
# the polarity is fixed; fine-tuning is via wrist_offset.
AL5D_wrist_sign = 1

# Fine wrist-angle offset (degrees). Added to the IK wrist angle before the
# pulse so the gripper tilt can be nudged a few degrees until it hangs straight
# down at every reach. Exposed in Calibration settings.
AL5D_wrist_offset = 0.0

# Calibration mode + measured-corner frame (mode B).  "A" uses the parametric
# model (gfudge/radscale/stickout/squaresize/axistorow0); "B" maps board units
# (a-file=0 .. h-file=8, rank-8=0 .. rank-1=8) through a fitted pivot + scale:
#   reach = frame_scale * dist((bx,by), (frame_px,frame_py)) + frame_offset
#   angle = frame_a0 - frame_sign * atan2(bx-frame_px, by-frame_py)
# frame_* are seeded from the mode-A defaults so B starts identically to A and
# is refined by the corner calibration.  frame_sign (±1) is the labelling's
# CHIRALITY - it flips when the squares are named mirrored relative to the
# arm's sweep, which no pivot/rotation can absorb. Set by corner calibration.
AL5D_calib_mode   = "A"
AL5D_frame_px     = 4.0     # pivot X (board units)
AL5D_frame_py     = -2.75   # pivot Y (board units; behind rank-8)
AL5D_frame_scale  = 1.0     # reach scale (inches per board unit)
AL5D_frame_offset = 3.878   # reach offset (inches)
AL5D_frame_a0     = 81.829  # base-angle offset (deg)
AL5D_frame_sign   = 1       # angle chirality (+1 normal, -1 mirrored labels)

# Per-corner corrections [[square, reach_corr_in, ang_corr_deg], ...] for the
# mode-B frame. The 5-parameter frame cannot hit four measured corners exactly
# (8 measurements, 5 parameters), so the leftover per-corner residual is stored
# and added back - making the model reproduce the calibrated corners EXACTLY
# and interpolate between them (2026-10-21). Written by the corner calibration.
AL5D_corner_res   = []

# Arm sag compensation: the arm flexes as it extends, so a commanded height
# lands LOW and the droop grows with reach. [[square, comp_in], ...] - each
# sample square anchors a point of the compensation curve (the amount ADDED to
# a commanded height). Calibrated live on the "Arm sag compensation" screen;
# empty list = no compensation. Applied at the IK seam (LEGO_Chess_rpd.sag_comp).
AL5D_sag_points   = []

# Arm segment lengths (inches) for IK — pivot-to-pivot, measured 2026-09-16.
# lib_al5_2D_IK reads THESE values (single source of truth).
# Stock AL5D: A 5.75 / B 7.375.  Grey-board era assumed A 6.75 / B 8.75.
AL5D_A = 5.875   # shoulder -> elbow (measured 5 7/8")
AL5D_B = 8.875   # elbow -> wrist (measured "just over 8.75"; old RPD note: 8.875)
# ---------------------------------------------------------------------------

if platform.system() == "Windows":
    windowsos = True
    mydir = r'C:\Users\Richard\Github\chess-robot\merged_chess\images' + "\\"
    stockfishexe = r'C:\Program Files\Stockfish\stockfish.exe'
    cameraportno = 0
    cameratype = 'usb'
    serialport = "COM3"          # SSC-32U port on Windows — adjust as needed
    cameraresolution = (1280, 720)
    windowsize = (cameraresolution[0] // 2, 960)
    stockfishparams = {"Threads": 4}
else:
    windowsos = False
    chessenginepath = '/usr/games/stockfish'
    # Anchor the images dir to THIS file's location, not the process CWD, so
    # tools work when launched from tests/ etc.
    mydir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'images') + os.sep
    # Device PATH, not index: cv2.VideoCapture(0) probes the obsensor plugin,
    # which SEGFAULTS (uncaught SIGSEGV, takes the whole UI down) on the Brio
    # in this OpenCV build when the device is busy or wedged. A path skips
    # that probe and fails as a clean isOpen()==False instead.
    cameraportno = "/dev/video0"
    serialport = '/dev/ttyUSB0'  # AL5D SSC-32U typically appears as ttyUSB0
    cameraresolution = (1280, 720)
    windowsize = (720, 1280)

# Camera capture settings (used by camera.py).
# MJPG is a compressed format that lets most USB webcams deliver 720p/1080p
# at 30fps, whereas raw YUYV is often limited to ~8fps at those sizes.
camerafourcc = "MJPG"
camerawarmup = 10  # frames to grab before the real capture (auto-exposure settle)

# Live-preview window size cap.  The Pi touchscreen is 800x480, so keep the
# preview within (800, 450) to leave room for the window title bar.
displaymax = (800, 450)

# Manual camera controls.  Auto-exposure and auto white-balance drift between
# frames, which breaks background subtraction and colour models, so we pin
# them to fixed values (see camera.lock_camera).
camera_lock = True
camera_exposure = 100          # exposure_time_absolute (camera-specific range)
camera_white_balance = 4620    # white_balance_temperature (K)

sunfishengine = False

if 'ANDROID_STORAGE' in os.environ:
    print("Android!")
    androidos = True
    bluetoothdevicename = 'HC-05'
    chessenginepath = "/data/app/ccc.chess.engine.stockfish-1/lib/arm/libstockfish15.so"
    depth = "10"
    mirrorimage = True
else:
    androidos = False

if sunfishengine:
    myfish = "sunfish.py"
    depth = "5"
else:
    depth = "15"

movetime  = "10000"   # Chess engine thinking time in milliseconds
slowmover = "50"

# Travel motion style: False = one joint at a time (safest for small PSUs);
# True = elbow+wrist+shoulder move together (slow) while travelling - far
# smoother to watch/time, needs a 6V/4A+ supply.
# (Arm calibration enables it for its own session; if it browns out there,
#  the supply cannot support it anywhere - keep this False.)
AL5D_pair_travel_joints = False

# With the 6V/4A+ supply, drive shoulder+elbow+wrist in ONE group for ALL
# moves (travel AND pick/drop descents) instead of the old staged
# elbow+wrist-then-shoulder - noticeably faster. Base rotation still stays its
# own final group. Set False to revert to the safe staged/one-at-a-time motion.
AL5D_move_all_at_once = True

# Robot type flags — only motorsareservos matters for the AL5D
motorsareservos = True   # AL5D uses servos via SSC-32U
SCARA = False
steppergripper = False

try:
    os.makedirs(mydir, exist_ok=True)   # logging.FileHandler below cannot create it
except OSError:
    pass
logging.basicConfig(
    level=logging.DEBUG,
    filename=mydir + 'chesslog.log',
    filemode='w',
    format='%(levelname)s-%(message)s'
)
kingincheck = False

# ---------------------------------------------------------------------------
# Fisheye / camera-distortion correction
# Set fisheye = True only if you are using a fisheye IP camera.
# Replace DIM / K / D with output from calibrate_fisheye.py if needed.
# ---------------------------------------------------------------------------
fisheye = False
DIM=(1280, 720)
K=numpy.array([[825.8718431624574, 0.0, 617.355521141012], [0.0, 826.9330327203571, 382.5554965590892], [0.0, 0.0, 1.0]])
D=numpy.array([[-0.2537429711491497], [2.891763540132472], [-21.482527730084254], [52.48929511139795]])
fisheyeimages = "fishimages/"
