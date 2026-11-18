# Copyright 2026 Derek Caelin - written by a coding agent under his direction.
# Licensing and credits: see README.md, "Credits & provenance".
# board_state.py
#
# Reads images/4.jpg and saves images/board_state.jpg — a labelled view of
# the board showing what the computer currently thinks is on each square.
#
# Each square shows:
#   - Square name (A1..H8)
#   - w=  warmth (R-B): negative=black piece, positive=wood piece
#   - e=  edge density: high=piece present, low=empty
#   - Classified piece: [B]=black piece  [W]=white piece  [ ]=empty
#
# Output image is 2× the size of calibration_preview for readability.
# Run after CBint.py has run once (so 4.jpg exists).
# Usage:  python board_state.py

import cv2
import numpy
import os
import statistics
import CBstate

mydir = CBstate.mydir
src_path = mydir + "4.jpg"

if not os.path.exists(src_path):
    print("ERROR: " + src_path + " not found. Run CBint.py first.")
    exit(1)

pix = cv2.imread(src_path)

# Apply same CLAHE contrast boost used in getplayermove()
lab = cv2.cvtColor(pix, cv2.COLOR_BGR2LAB)
l, a, b_ch = cv2.split(lab)
clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
l = clahe.apply(l)
lab = cv2.merge([l, a, b_ch])
pix = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

h, w, _ = pix.shape
sqx = w // 8
sqy = h // 8
fudge = 9

print("4.jpg: %dx%d, square size %dx%d" % (w, h, sqx, sqy))

# Compute Canny edges
grey  = cv2.cvtColor(pix, cv2.COLOR_BGR2GRAY)
edges = cv2.Canny(grey, threshold1=30, threshold2=80)

all_meanR  = {}
all_warmth = {}
all_edges  = {}

for row in range(8):
    for col in range(8):
        r1 = row * sqy + fudge;  r2 = (row+1) * sqy - fudge
        c1 = col * sqx + fudge;  c2 = (col+1) * sqx - fudge
        patch = pix[r1:r2, c1:c2]
        ep    = edges[r1:r2, c1:c2]
        meanR  = float(numpy.mean(patch[:,:,2]))
        meanB  = float(numpy.mean(patch[:,:,0]))
        ed     = float(numpy.mean(ep))
        all_meanR[(row,col)]  = meanR
        all_warmth[(row,col)] = meanR - meanB
        all_edges[(row,col)]  = ed

# Compute thresholds (same logic as playermove_rpd calibration)
piece_edges  = [all_edges[(r,c)]  for r in list(range(2))+list(range(6,8)) for c in range(8)]
empty_edges  = [all_edges[(r,c)]  for r in range(2,6) for c in range(8)]
black_warmth = [all_warmth[(r,c)] for r in range(2)   for c in range(8)]
white_warmth = [all_warmth[(r,c)] for r in range(6,8) for c in range(8)]

est_edge_thresh = (min(piece_edges) + max(empty_edges)) / 2
est_splitwb     = (statistics.median(white_warmth) + statistics.median(black_warmth)) / 2

print("edge_thresh: %.1f  (min piece: %.1f, max empty: %.1f)" %
      (est_edge_thresh, min(piece_edges), max(empty_edges)))
print("splitwb:     %.1f  (black warmth: %d to %d, white warmth: %d to %d)" % (
      est_splitwb,
      int(min(black_warmth)), int(max(black_warmth)),
      int(min(white_warmth)), int(max(white_warmth))))

# Classify each square
classification = {}
for row in range(8):
    for col in range(8):
        ed     = all_edges[(row,col)]
        warmth = all_warmth[(row,col)]
        if ed > est_edge_thresh:
            classification[(row,col)] = "B" if warmth < est_splitwb else "W"
        else:
            classification[(row,col)] = " "

# Print board state
print("\nBoard state (row 0 = rank 8 = black pieces):")
files = "ABCDEFGH"
for row in range(8):
    rank   = 8 - row
    pieces = [classification[(row,c)] for c in range(8)]
    labels = [files[c]+str(rank) for c in range(8)]
    print("row%d: " % row + "  ".join("[%s]%-3s" % (pieces[c], labels[c]) for c in range(8)))

# Draw scaled overlay — 2× the source image size
SCALE = 2
out = cv2.resize(pix, (w * SCALE, h * SCALE), interpolation=cv2.INTER_LINEAR)
font = cv2.FONT_HERSHEY_SIMPLEX

for row in range(8):
    for col in range(8):
        warmth = all_warmth[(row,col)]
        ed     = all_edges[(row,col)]
        piece  = classification[(row,col)]
        rank   = 8 - row
        name   = files[col] + str(rank)

        tl = (col * sqx * SCALE,     row * sqy * SCALE)
        br = ((col+1) * sqx * SCALE, (row+1) * sqy * SCALE)
        sq = sqy * SCALE

        # Background colour by classification
        if piece == "B":
            bg = (100, 40, 40);   tc = (220, 180, 180)
        elif piece == "W":
            bg = (40, 110, 40);   tc = (180, 240, 180)
        else:
            bg = (45, 45, 45);    tc = (160, 160, 160)

        cv2.rectangle(out, tl, br, bg, -1)
        cv2.rectangle(out, tl, br, (0, 0, 0), 1)
        # Draw the actual sampling region (fudge inset) in yellow
        s_tl = ((col * sqx + fudge) * SCALE,     (row * sqy + fudge) * SCALE)
        s_br = (((col+1)*sqx - fudge) * SCALE,   ((row+1)*sqy - fudge) * SCALE)
        cv2.rectangle(out, s_tl, s_br, (0, 200, 200), 1)

        cx = tl[0] + 4
        cy = tl[1] + sq // 5

        cv2.putText(out, name,            (cx, cy),           font, 0.4,  tc,           1)
        cv2.putText(out, "w=%d" % int(warmth), (cx, cy + sq//4),  font, 0.35, tc,      1)
        cv2.putText(out, "e=%d" % int(ed),     (cx, cy + sq//2),  font, 0.35, (100,220,220), 1)
        # Big piece label
        piece_label = "[%s]" % piece
        cv2.putText(out, piece_label,     (cx, cy + sq*3//4), font, 0.5,  (255,255,255), 1)

out_path = mydir + "board_state.jpg"
cv2.imwrite(out_path, out)
print("\nSaved: " + out_path + " (%dx%d)" % (w*SCALE, h*SCALE))
