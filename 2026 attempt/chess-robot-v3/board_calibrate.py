# board_calibrate.py
#
# GUI for clicking the four corners of the chessboard by square name.
# Run this once whenever you reposition the camera.
#
# Usage:
#   python board_calibrate.py
#
# You will be asked to click the outer corner of each of these squares:
#   A1  (white's queen-rook corner)
#   H1  (white's king-rook corner)
#   A8  (black's queen-rook corner)
#   H8  (black's king-rook corner)
#
# Because you are identifying squares by name, the script works out the
# correct rotation and orientation automatically — no need to set
# CBstate.rotation manually.
#
# When all four corners are clicked, points.txt is written and a
# perspective-corrected preview with a labelled grid is shown so you
# can verify the result before starting the game.

import tkinter as tk
from tkinter import simpledialog, messagebox
from PIL import Image, ImageTk
import cv2
import numpy
import subprocess
import os
import json
import shutil

import CBstate
import playermove_rpd as RDpm
import camera

mydir = CBstate.mydir

# The four corners to click, in order, with their labels and colours
CORNERS = [
    {"label": "A1", "desc": "outer corner of square A1  (white queen-rook, nearest to you on the left)",  "colour": "red"},
    {"label": "H1", "desc": "outer corner of square H1  (white king-rook,  nearest to you on the right)", "colour": "blue"},
    {"label": "A8", "desc": "outer corner of square A8  (black queen-rook, far side on the left)",         "colour": "green"},
    {"label": "H8", "desc": "outer corner of square H8  (black king-rook,  far side on the right)",        "colour": "orange"},
]


def capture_image():
    print("Capturing image...")
    camera.capture_still(mydir + "1.jpg")
    print("Captured.")


def compute_and_save(clicks_by_label, img_dimension):
    """
    Given the four clicked corners (keyed by square name), compute the
    perspective homography so that:
      A1 -> bottom-left  (0,           img_dimension)
      H1 -> bottom-right (img_dimension, img_dimension)
      A8 -> top-left     (0,           0            )
      H8 -> top-right    (img_dimension, 0           )

    This maps the board so that:
      - White's side is at the bottom (rows 6-7 in the code)
      - Black's side is at the top    (rows 0-1 in the code)
      - The a-file is on the left     (col 0)
      - The h-file is on the right    (col 7)

    Writes points.txt and 4.jpg, returns the corrected image.
    """
    d = img_dimension

    # Source points — where the user clicked in the raw camera image
    pts_src = numpy.float32([
        clicks_by_label["A1"],   # -> bottom-left
        clicks_by_label["H1"],   # -> bottom-right
        clicks_by_label["A8"],   # -> top-left
        clicks_by_label["H8"],   # -> top-right
    ])

    # Destination points — fixed layout in the output square image
    pts_dst = numpy.float32([
        [0,   d],   # A1 -> bottom-left
        [d,   d],   # H1 -> bottom-right
        [0,   0],   # A8 -> top-left
        [d,   0],   # H8 -> top-right
    ])

    # Save corner coordinates for homolog() to reuse on each capture.
    # Normalised to [0,1] relative to the capture image size.
    im_src = cv2.imread(mydir + "1.jpg", 1)
    src_h, src_w = im_src.shape[:2]
    flat = []
    for label in ("A1", "H1", "A8", "H8"):
        flat.append("%.6f" % (clicks_by_label[label][0] / src_w))
        flat.append("%.6f" % (clicks_by_label[label][1] / src_h))
    with open(mydir + "points.txt", "w") as f:
        f.write(",".join(flat))

    # Apply homography to 1.jpg -> 4.jpg
    H = cv2.getPerspectiveTransform(pts_src, pts_dst)
    im_out = cv2.warpPerspective(im_src, H, (d + 1, d + 1),
                                 flags=cv2.INTER_LINEAR)
    # No CBstate.rotation needed — orientation is baked into pts_dst above
    cv2.imwrite(mydir + "4.jpg", im_out)
    return im_out


def make_preview(img_dimension):
    """
    Load 4.jpg and overlay square labels (A1..H8) so the user can verify
    the orientation is correct before closing the window.
    """
    im = cv2.imread(mydir + "4.jpg")
    if im is None:
        return None
    h, w, _ = im.shape
    sqx = w // 8
    sqy = h // 8
    grey = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)

    # Choose label colour per square from the square's actual brightness, so
    # labels stay readable on both light and dark squares.
    brightness = {}
    for row in range(8):
        for col in range(8):
            r1 = row * sqy + sqy // 4
            r2 = (row + 1) * sqy - sqy // 4
            c1 = col * sqx + sqx // 4
            c2 = (col + 1) * sqx - sqx // 4
            brightness[(row, col)] = float(numpy.mean(grey[r1:r2, c1:c2]))
    med_bright = float(numpy.median(list(brightness.values())))

    font = cv2.FONT_HERSHEY_SIMPLEX
    files = "ABCDEFGH"
    for row in range(8):         # row 0 = rank 8 (black), row 7 = rank 1 (white)
        for col in range(8):     # col 0 = a-file
            rank = 8 - row
            label = files[col] + str(rank)
            cx = int((col + 0.1) * sqx)
            cy = int((row + 0.5) * sqy)
            colour = (0, 0, 0) if brightness[(row, col)] > med_bright else (0, 255, 255)
            cv2.putText(im, label, (cx, cy), font, 0.35, colour, 1)
    # Grid lines
    for col in range(0, w, sqx):
        cv2.line(im, (col, 0), (col, h), (0, 0, 255), 1)
    for row in range(0, h, sqy):
        cv2.line(im, (0, row), (w, row), (0, 0, 255), 1)
    preview_path = mydir + "calibration_preview.jpg"
    cv2.imwrite(preview_path, im)
    return preview_path


class CalibrationApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Chessboard Corner Calibration")

        self.clicks = []            # list of {"label":..., "img_x":..., "img_y":...}
        self.scale  = 1.0
        self.img_w  = 1
        self.img_h  = 1
        self.img_dimension = 0

        # ── Instruction label ─────────────────────────────────────────────
        self.label = tk.Label(
            root,
            text="Click the outer corner of " + CORNERS[0]["desc"],
            font=("Helvetica", 13),
            wraplength=860,
            pady=8,
        )
        self.label.pack()

        # ── Canvas ────────────────────────────────────────────────────────
        self.canvas = tk.Canvas(root, cursor="crosshair")
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self.on_click)

        # ── Buttons ───────────────────────────────────────────────────────
        btn_frame = tk.Frame(root)
        btn_frame.pack(pady=6)
        tk.Button(btn_frame, text="Recapture image",
                  command=self.recapture).pack(side=tk.LEFT, padx=6)
        tk.Button(btn_frame, text="Undo last click",
                  command=self.undo).pack(side=tk.LEFT, padx=6)
        tk.Button(btn_frame, text="Restart clicks",
                  command=self.restart).pack(side=tk.LEFT, padx=6)
        tk.Button(btn_frame, text="Save test case",
                  command=self.save_case).pack(side=tk.LEFT, padx=6)
        tk.Button(btn_frame, text="Close",
                  command=root.destroy).pack(side=tk.LEFT, padx=6)

        # ── Load image ────────────────────────────────────────────────────
        self.load_image(mydir + "1.jpg")

    # ── Image handling ────────────────────────────────────────────────────

    def load_image(self, path, show_clicks=True):
        if not os.path.exists(path):
            self.label.config(text="No image found — press 'Recapture image'.")
            self.canvas.config(width=640, height=480)
            return
        img = Image.open(path)
        self.img_w, self.img_h = img.size
        self.img_dimension = min(self.img_w, self.img_h)
        RDpm.update_img_dimension(self.img_dimension)

        # Scale to fit the actual screen, reserving space for the instruction
        # label and the button row so they always stay visible.
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        max_w = sw - 40
        max_h = sh - 180
        self.scale = min(max_w / self.img_w, max_h / self.img_h, 1.0)
        disp_w = int(self.img_w * self.scale)
        disp_h = int(self.img_h * self.scale)

        img_disp = img.resize((disp_w, disp_h), Image.LANCZOS)
        self.tk_img = ImageTk.PhotoImage(img_disp)
        self.canvas.config(width=disp_w, height=disp_h)
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.tk_img)
        if show_clicks:
            self._redraw_clicks()

    # ── Click handling ────────────────────────────────────────────────────

    def on_click(self, event):
        if len(self.clicks) >= 4:
            return
        img_x = int(event.x / self.scale)
        img_y = int(event.y / self.scale)
        corner = CORNERS[len(self.clicks)]
        self.clicks.append({"label": corner["label"],
                             "img_x": img_x, "img_y": img_y})
        self._redraw_clicks()

        if len(self.clicks) < 4:
            next_corner = CORNERS[len(self.clicks)]
            self.label.config(
                text="Click the outer corner of " + next_corner["desc"])
        else:
            self.label.config(text="Computing perspective correction...")
            self.root.update()
            self._finish()

    def _redraw_clicks(self):
        self.canvas.delete("click_marker")
        for entry in self.clicks:
            c = next(c for c in CORNERS if c["label"] == entry["label"])
            dx = int(entry["img_x"] * self.scale)
            dy = int(entry["img_y"] * self.scale)
            r  = 8
            self.canvas.create_oval(dx-r, dy-r, dx+r, dy+r,
                                    outline=c["colour"], width=2,
                                    tags="click_marker")
            self.canvas.create_text(dx + 14, dy,
                                    text=entry["label"],
                                    fill=c["colour"],
                                    font=("Helvetica", 12, "bold"),
                                    tags="click_marker")

    def _finish(self):
        clicks_by_label = {
            e["label"]: (e["img_x"], e["img_y"]) for e in self.clicks
        }
        compute_and_save(clicks_by_label, self.img_dimension)
        preview_path = make_preview(self.img_dimension)
        if preview_path:
            self.load_image(preview_path, show_clicks=False)
        self.label.config(
            text="Done! Check the square labels below match the physical board "
                 "(A1 = white queen-rook corner, H8 = black king-rook corner). "
                 "Close this window to start the game, or click 'Restart clicks' to redo."
        )

    def undo(self):
        if self.clicks:
            self.clicks.pop()
            self._redraw_clicks()
            if len(self.clicks) < 4:
                next_corner = CORNERS[len(self.clicks)]
                self.label.config(
                    text="Click the outer corner of " + next_corner["desc"])

    def restart(self):
        self.clicks = []
        self._redraw_clicks()
        self.load_image(mydir + "1.jpg")
        self.label.config(
            text="Click the outer corner of " + CORNERS[0]["desc"])

    def recapture(self):
        self.clicks = []
        capture_image()
        self.load_image(mydir + "1.jpg")
        self.label.config(
            text="Click the outer corner of " + CORNERS[0]["desc"])

    def save_case(self):
        """Archive the current capture + clicked corners as a test case.

        Writes tests/cases/<name>/ with:
          capture.jpg   raw camera frame (this capture)
          corners.json  {"A1":[x,y], "H1":[x,y], "A8":[x,y], "H8":[x,y]}
                        normalised to [0,1] relative to the image size
          state.txt     "empty" or "setup"
        """
        if len(self.clicks) < 4:
            messagebox.showwarning(
                "Not ready", "Click all four corners before saving a test case.")
            return

        name = simpledialog.askstring(
            "Test case name", "Name for this test case (e.g. empty_center):")
        if not name:
            return
        name = name.strip().replace(" ", "_")
        if not name:
            return

        setup = messagebox.askyesno(
            "Pieces?", "Are the pieces set up on the board?")
        state = "setup" if setup else "empty"

        case_dir = os.path.join("tests", "cases", name)
        os.makedirs(case_dir, exist_ok=True)

        src = mydir + "1.jpg"
        if not os.path.exists(src):
            messagebox.showerror("Missing image", "No capture found to save.")
            return
        shutil.copy(src, os.path.join(case_dir, "capture.jpg"))

        # Normalise against the actual capture dimensions (not self.img_w/h,
        # which may have been overwritten by the preview image in _finish()).
        cap_w, cap_h = Image.open(src).size

        corners = {}
        for e in self.clicks:
            corners[e["label"]] = [
                round(e["img_x"] / cap_w, 6),
                round(e["img_y"] / cap_h, 6),
            ]
        with open(os.path.join(case_dir, "corners.json"), "w") as f:
            json.dump(corners, f, indent=2)
        with open(os.path.join(case_dir, "state.txt"), "w") as f:
            f.write(state + "\n")

        messagebox.showinfo(
            "Saved", "Saved test case '%s' (%s) to %s" % (name, state, case_dir))


def run_calibration():
    """Show the calibration window. Blocks until closed.

    The previous points.txt is moved aside as a backup (NOT deleted):
    if the window is closed before all four corners are clicked, the
    old calibration is restored instead of destroyed.
    """
    points_path = mydir + "points.txt"
    backup = points_path + ".bak"
    if os.path.exists(points_path):
        os.replace(points_path, backup)
        print("Previous points.txt set aside; it will be restored if this "
              "session is abandoned.")
    capture_image()
    root = tk.Tk()
    CalibrationApp(root)
    root.mainloop()
    points_path = mydir + "points.txt"
    if os.path.exists(points_path):
        try:
            os.remove(backup)
        except OSError:
            pass
        print("Calibration complete. points.txt written.")
        return True
    if os.path.exists(backup):
        os.replace(backup, points_path)
        print("Closed before all four corners were clicked - previous "
              "points.txt restored.")
    else:
        print("Calibration window closed before all four corners were clicked.")
    return False


if __name__ == "__main__":
    run_calibration()
