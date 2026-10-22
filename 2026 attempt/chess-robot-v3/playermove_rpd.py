# Vision / board-reading module
#
# Original ALD5 version: copyright 2018 Richard Day, chess@gotobiz.co.uk
# versiongui upgrades merged in for the AL5D build.
#
# Key improvement over ALD5 original:
#   Uses *separate* white/black thresholds for pieces sitting on dark squares
#   vs. light squares (splitwbonb / splitwbonw).  This fixes the problem where
#   high-contrast boards fool a single global threshold.
#
# Image pipeline:
#   Camera -> images/1.jpg
#   homolog() applies a perspective-correction homography (you click the four
#   board corners once via the GUI) and writes images/4.jpg
#   getplayermove() then reads images/4.jpg square-by-square.

import cv2
import os
import shutil
import subprocess
import sys
import numpy
import CBstate
import camera
import piece_detect
mydir = CBstate.mydir
import logging

# Confirm working directory and image path at startup so path problems are obvious
import os as _os
print("playermove_rpd: working directory is", _os.getcwd())
print("playermove_rpd: image directory is", _os.path.abspath(mydir))
if not _os.path.isdir(mydir):
    print("WARNING: image directory does not exist yet - it will be created on first capture.")
    _os.makedirs(mydir, exist_ok=True)

firsttimeonly = 1
firstgbp     = 1
splitwb            = 112   # midpoint between white and black piece R values
splitwbonb         = 112   # (legacy)
splitwbonw         = 112   # (legacy)
stdrgb             = 23    # (legacy)
dark_board_thresh  = 80    # (legacy)
light_board_thresh = 160   # (legacy)
reliable_cols      = list(range(8))  # columns with clear dark/light contrast, updated at calibration
edge_thresh        = 10.0  # Canny edge density above this means a piece is present

# Homography corner points (updated by registerclicks / loaded from points.txt)
pts_src      = numpy.array([[100, 100], [100, 100], [100, 100], [100, 100]])
pts8         = [0, 0, 0, 0, 0, 0, 0, 0]
whereclick   = ["bottom left", "top left", "bottom right", "top right"]
pointscount  = 0
img_dimension = 0

xrevtrans = {
    1: "a", 2: "b", 3: "c", 4: "d",
    5: "e", 6: "f", 7: "g", 8: "h"
}

# ── Board state arrays ────────────────────────────────────────────────────────
xx, yy = 8, 8
pieces    = [["e" for _ in range(xx)] for _ in range(yy)]
oldpieces = [["e" for _ in range(xx)] for _ in range(yy)]
for _row in range(2):
    for _col in range(8):
        pieces[_row][_col]    = "b"
        oldpieces[_row][_col] = "b"
for _row in range(6, 8):
    for _col in range(8):
        pieces[_row][_col]    = "w"
        oldpieces[_row][_col] = "w"

# ── Helpers ───────────────────────────────────────────────────────────────────

def updateforcomputermove(board):
    global oldpieces, pieces, firsttimeonly
    if firsttimeonly:
        firsttimeonly = 0
        print("first time only:")
    else:
        for x in range(8):
            for y in range(8):
                current = board[x][y]
                if current == ".":
                    oldpieces[x][y] = "e"
                elif current.islower():
                    oldpieces[x][y] = "b"
                else:
                    oldpieces[x][y] = "w"
    print("After computer move:")
    print(oldpieces)


def dummymove(board):
    """Calibration pass: capture image once to compute split thresholds."""
    global firstgbp
    firstgbp = 1
    getplayermove(board, ())


def undistort():
    """Fisheye un-distortion — only called when CBstate.fisheye is True."""
    img = cv2.imread(mydir + "1.jpg")
    map1, map2 = cv2.fisheye.initUndistortRectifyMap(
        CBstate.K, CBstate.D, numpy.eye(3), CBstate.K, CBstate.DIM, cv2.CV_16SC2
    )
    undistorted = cv2.remap(img, map1, map2,
                            interpolation=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_CONSTANT)
    cv2.imwrite(mydir + "1.jpg", undistorted)


def scalefish():
    """Optionally shrink the captured image, then un-distort if fisheye."""
    frame = cv2.imread(mydir + "1.jpg")
    if CBstate.scale_percent != 100:
        w = int(frame.shape[1] * CBstate.scale_percent / 100)
        h = int(frame.shape[0] * CBstate.scale_percent / 100)
        frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
        cv2.imwrite(mydir + "1.jpg", frame)
    if CBstate.fisheye:
        undistort()


def homolog():
    """Apply perspective-correction homography: images/1.jpg -> images/4.jpg.

    If points.txt does not exist yet (i.e. the GUI corner-click calibration
    has not been run), we fall back to copying 1.jpg straight to 4.jpg so
    that the rest of the pipeline can still proceed.  A warning is printed so
    the user knows the perspective correction is not active.
    """
    global pts_src, img_dimension
    points_path = mydir + 'points.txt'

    if not os.path.exists(points_path):
        print("WARNING: " + points_path + " not found.")
        print("Perspective correction is disabled. Run the GUI calibration")
        print("(click the four board corners) to create this file.")
        src = cv2.imread(mydir + "1.jpg")
        if src is None:
            print("ERROR: could not read " + mydir + "1.jpg - camera capture may have failed.")
            return
        cv2.imwrite(mydir + "4.jpg", src)
        return

    # Load points.txt written by the calibration tool.
    # Format: A1x,A1y, H1x,H1y, A8x,A8y, H8x,H8y
    # Coordinates are normalised to [0,1] relative to the capture image, so
    # they stay valid if the capture resolution changes.
    pss = [0.0] * 8
    with open(points_path, 'r') as f:
        line = f.readline().rstrip()
    for i, v in enumerate(line.split(",")):
        pss[i] = float(v)

    print("imread " + mydir + "1.jpg")
    im_src = cv2.imread(mydir + "1.jpg", 1)
    if im_src is None:
        print("ERROR: could not read " + mydir + "1.jpg for homography.")
        return

    src_h, src_w = im_src.shape[:2]

    # Source points from points.txt: A1, H1, A8, H8 (normalised -> pixels).
    # Note: A1 may appear to the RIGHT of H1 in the camera image if the
    # camera is positioned on Black's side of the board. That is correct.
    A1 = (pss[0] * src_w, pss[1] * src_h)
    H1 = (pss[2] * src_w, pss[3] * src_h)
    A8 = (pss[4] * src_w, pss[5] * src_h)
    H8 = (pss[6] * src_w, pss[7] * src_h)

    print("homolog: corners A1=%s H1=%s A8=%s H8=%s" % (
          str(A1), str(H1), str(A8), str(H8)))

    pts_src = numpy.float32([A1, H1, A8, H8])

    # Output size: use the average of board_w and board_h as the square
    # output dimension.  We compute separate w and h spans from the four
    # corners to get the most accurate board size estimate.
    # Average the two horizontal spans (top and bottom of board)
    board_w = int((abs(A1[0] - H1[0]) + abs(A8[0] - H8[0])) / 2)
    # Average the two vertical spans (left and right of board)
    board_h = int((abs(A1[1] - A8[1]) + abs(H1[1] - H8[1])) / 2)
    img_dimension = int((board_w + board_h) / 2)  # use mean for square output
    d = img_dimension
    print("homolog: src=%dx%d board_w=%d board_h=%d output=%dx%d" % (
          src_w, src_h, board_w, board_h, d, d))

    # Destination: standard chess orientation — a-file on left, rank 1 at bottom.
    # The camera is on Black's side so A1 appears on the RIGHT in the camera
    # image. We map it to bottom-LEFT in the output, which means the homography
    # performs the mirror correction automatically — no separate flip needed.
    pts_dst = numpy.float32([
        [0, d],   # A1 -> bottom-left  (a-file, rank 1)
        [d, d],   # H1 -> bottom-right (h-file, rank 1)
        [0, 0],   # A8 -> top-left     (a-file, rank 8)
        [d, 0],   # H8 -> top-right    (h-file, rank 8)
    ])

    H = cv2.getPerspectiveTransform(pts_src, pts_dst)
    im_out = cv2.warpPerspective(im_src, H, (d + 1, d + 1),
                                 flags=cv2.INTER_LINEAR)
    # No rotation/mirror needed — orientation is baked into pts_dst above
    cv2.imwrite(mydir + "4.jpg", im_out)


def drawredlines():
    """Overlay an 8x8 grid on images/4.jpg and save as images/redlines.jpg."""
    im = cv2.imread(mydir + '4.jpg')
    if im is None:
        print("WARNING: drawredlines() could not read " + mydir + "4.jpg - skipping grid overlay.")
        return
    h, w, _ = im.shape
    sqx = int(round(w / 8))
    sqy = int(round(h / 8))
    if sqx == 0 or sqy == 0:
        print("WARNING: drawredlines() got zero square size (w=%d h=%d) - image may be corrupt." % (w, h))
        return
    for col in range(0, w, sqx):
        for row in range(h):
            im[row, col] = (0, 0, 255)
    for row in range(0, h, sqy):
        for col in range(w):
            im[row, col] = (0, 0, 255)
    cv2.imwrite(mydir + "redlines.jpg", im)


def update_img_dimension(newdim):
    """Called by the GUI when the display image size is known."""
    img_dimension = int(newdim)


def registerclicks(x, y):
    """Record one corner click; once all four are in, save and run homolog()."""
    global pointscount, pts_src, pts8
    print(x, ' ', y)
    pts_src[pointscount] = [x, y]
    pts8[pointscount * 2]     = x
    pts8[pointscount * 2 + 1] = y
    pointscount += 1
    if pointscount == 4:
        print(pts_src)
        with open(mydir + 'points.txt', 'w') as f:
            f.write(",".join(str(v) for v in pts8))
        homolog()
        pointscount = 0
        return "finished"
    print("Now click " + whereclick[pointscount])
    return ""


# ── Move detection via temporal differencing ─────────────────────────────────

def getplayermove(board, validkingmoves, stop=None, status=None,
                  on_ready=None):
    """Detect the human's move by diffing the current frame against the
    reference captured after the computer's last move.

    board[x][y] is the engine's board state: '.' empty, lowercase = black,
    uppercase = white.  The human plays white.
    stop: optional callable checked while watching (UI abandons the turn).
    """
    changes = piece_detect.monitor(stop=stop, status=status,
                                      on_ready=on_ready)
    if changes is None:
        # camera could not be opened (busy/re-arming) - NOT a move; the
        # caller loops and watches again (crashed mid-game 2026-09-18).
        print("getplayermove: camera unavailable - retrying")
        sys.stdout.flush()
        return "m    "
    if changes == "NOBOARD":
        return "NOCAL"
    if changes == "NOCAM":
        return "NOCAM"
    print("changed squares:", [(xrevtrans[c + 1] + str(8 - r), round(d, 1))
                               for r, c, d in changes])

    # A normal move touches only a couple of squares; a broad change means the
    # board or camera shifted (stale homography).  Signal the caller to recover.
    if len(changes) > piece_detect.JOSTLE_THRESH:
        print("jostle detected: %d squares changed" % len(changes))
        sys.stdout.flush()
        return "JOSTLE"

    from_sqs = []
    to_sqs = []
    for (r, c, d) in changes:
        cur = board[r][c]
        if cur.isupper():
            from_sqs.append((r, c))
        elif cur == "." or cur.islower():
            to_sqs.append((r, c))

    # Castling: the king and a rook both left their home squares.
    if (7, 4) in from_sqs:
        if (7, 7) in from_sqs:
            print("castled king side")
            return "me1g1"
        if (7, 0) in from_sqs:
            print("castled queen side")
            return "me1c1"

    if not from_sqs or not to_sqs:
        print("no move detected")
        sys.stdout.flush()
        return "m    "

    fr, fc = from_sqs[0]
    tr, tc = to_sqs[0]

    constructmove = list("ma1a1")
    constructmove[1] = xrevtrans[fc + 1]
    constructmove[2] = str(8 - fr)
    constructmove[3] = xrevtrans[tc + 1]
    constructmove[4] = str(8 - tr)
    movestr = ''.join(constructmove)
    print(movestr)
    sys.stdout.flush()
    return movestr
