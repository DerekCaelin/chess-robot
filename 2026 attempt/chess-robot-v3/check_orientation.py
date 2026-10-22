# check_orientation.py
#
# Reads images/4.jpg and saves images/orientation_check.jpg with each square
# labelled with its chess name, mean-R value, and edge density.
# Output image is scaled up for readability.
#
# Run after CBint.py has run once (so 4.jpg exists).
# Usage:  python check_orientation.py

import cv2
import numpy
import os
import CBstate

def capture_image():
    cmd = (
        'fswebcam -d /dev/video0 -i 0 -r 640x480 -S 20 '
        '--set "Power Line Frequency"="50 Hz" --no-banner '
        '--delay 2 --set brightness=50% --jpeg 99 '
        '--save "' + mydir + '1.jpg"'
    )
    print("Capturing image...")
    subprocess.call(cmd, shell=True)
    print("Captured.")


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

# Compute Canny edges on greyscale
grey  = cv2.cvtColor(pix, cv2.COLOR_BGR2GRAY)
edges = cv2.Canny(grey, threshold1=30, threshold2=80)

# Collect measurements
all_meanR  = {}
all_edges  = {}
all_warmth = {}

print("\nMean-R per square:")
print("     " + "  ".join("c%d   " % c for c in range(8)))
for row in range(8):
    row_r = []
    for col in range(8):
        r1 = row * sqy + fudge;  r2 = (row+1) * sqy - fudge
        c1 = col * sqx + fudge;  c2 = (col+1) * sqx - fudge
        patch = pix[r1:r2, c1:c2]
        ep    = edges[r1:r2, c1:c2]
        meanR   = float(numpy.mean(patch[:,:,2]))
        meanB   = float(numpy.mean(patch[:,:,0]))
        warmth  = meanR - meanB
        ed      = float(numpy.mean(ep))
        all_meanR[(row,col)]  = meanR
        all_edges[(row,col)]  = ed
        all_warmth[(row,col)] = warmth
        row_r.append(int(meanR))
    print("row%d: %s" % (row, ["%-6d" % v for v in row_r]))

print("\nWarmth (R-B) per square  [black pieces ≈ 0, wood pieces > 0]:")
print("     " + "  ".join("c%d   " % c for c in range(8)))
for row in range(8):
    row_w = [int(all_warmth[(row,col)]) for col in range(8)]
    print("row%d: %s" % (row, ["%-6d" % v for v in row_w]))

print("\nEdge density per square:")
print("     " + "  ".join("c%d   " % c for c in range(8)))
for row in range(8):
    row_e = [int(all_edges[(row,col)]) for col in range(8)]
    print("row%d: %s" % (row, ["%-6d" % v for v in row_e]))

# Estimate thresholds
piece_edges = [all_edges[(r,c)] for r in list(range(2))+list(range(6,8)) for c in range(8)]
empty_edges = [all_edges[(r,c)] for r in range(2,6) for c in range(8)]
# Only use reliable columns (those with clear dark/light contrast) for threshold estimation
reliable = [c for c in range(8) if
    abs(sum(all_warmth[(r,c)] for r in range(2,6) if (r+c)%2==0) -
        sum(all_warmth[(r,c)] for r in range(2,6) if (r+c)%2==1)) > 10]
if not reliable:
    reliable = list(range(8))
print("Reliable cols for threshold:", reliable)

black_warmth = [all_warmth[(r,c)] for r in range(2) for c in reliable]
white_warmth = [all_warmth[(r,c)] for r in range(6,8) for c in reliable]

est_edge_thresh = (min(piece_edges) + max(empty_edges)) / 2
# Use median rather than midpoint to reduce overlap sensitivity
import statistics
est_splitwb = (statistics.median(white_warmth) + statistics.median(black_warmth)) / 2

print("\nEstimated thresholds:")
print("  edge_thresh:     %.1f  (min piece edges: %.1f, max empty edges: %.1f)" %
      (est_edge_thresh, min(piece_edges), max(empty_edges)))
print("  splitwb (R-B):   %.1f  (black warmth: %d-%d, white warmth: %d-%d)" %
      (est_splitwb, int(min(black_warmth)), int(max(black_warmth)),
       int(min(white_warmth)), int(max(white_warmth))))

# Draw labelled overlay — scale up 3x for readability
SCALE = 3
out_h = h * SCALE
out_w = w * SCALE
out = cv2.resize(pix, (out_w, out_h), interpolation=cv2.INTER_NEAREST)
font = cv2.FONT_HERSHEY_SIMPLEX
files = "ABCDEFGH"

for row in range(8):
    for col in range(8):
        meanR = all_meanR[(row, col)]
        ed    = all_edges[(row, col)]
        rank  = 8 - row
        name  = files[col] + str(rank)

        tl = (col * sqx * SCALE,       row * sqy * SCALE)
        br = ((col+1) * sqx * SCALE,   (row+1) * sqy * SCALE)

        # Colour-code by estimated classification
        if ed > est_edge_thresh:
            if all_warmth[(row,col)] < est_splitwb:
                colour = (120, 40, 40)    # blue-ish = black piece
                tc = (220, 180, 180)
            else:
                colour = (40, 120, 40)    # green = white piece
                tc = (180, 230, 180)
        else:
            colour = (50, 50, 50)         # dark grey = empty
            tc = (180, 180, 180)

        cv2.rectangle(out, tl, br, colour, -1)
        cv2.rectangle(out, tl, br, (0, 0, 0), 1)

        sq = sqy * SCALE
        cx = tl[0] + 4
        cy = tl[1] + sq // 4
        cv2.putText(out, name,        (cx, cy),          font, 0.45, tc, 1)
        cv2.putText(out, "w=%d" % int(all_warmth[(row,col)]), (cx, cy + sq//3),   font, 0.40, tc, 1)
        cv2.putText(out, "e=%d" % int(ed),                        (cx, cy + sq*2//3), font, 0.40, (100,220,220), 1)

out_path = mydir + "orientation_check.jpg"
cv2.imwrite(out_path, out)
print("\nSaved: " + out_path + " (%dx%d)" % (out_w, out_h))
print("\nColour key:  Green=white piece   Blue=black piece   Grey=empty")
print("Labels:      R=mean-R value      e=edge density")
