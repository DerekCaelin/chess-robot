# tests/gen_instr_asset.py - capture the CURRENT camera setup, with the new
# overlay baked in, as the instructions-modal example image.
# Close the UI first (it holds the webcam).
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cv2  # noqa: E402
import camera  # noqa: E402
import board_autodetect as ba  # noqa: E402
import live_calibrate as LC  # noqa: E402

OUT = os.path.join(ROOT, "ui", "assets", "camera_locked.png")


def main():
    if not camera.capture_still("/tmp/opencode/asset_src.jpg"):
        print("capture failed - is the UI still holding the camera? Close it "
              "and retry.")
        return 1
    img = cv2.imread("/tmp/opencode/asset_src.jpg")
    det = ba.detect_board(img)
    if det is None:
        print("board not detected in current view - leave the setup as-is, "
              "check lighting, retry.")
        return 1
    ov = LC._draw_overlay(img, det, None, False, status=False)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    cv2.imwrite(OUT, ov)
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
