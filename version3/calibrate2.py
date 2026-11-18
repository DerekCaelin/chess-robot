# Copyright 2026 Derek Caelin - written by a coding agent under his direction.
# Licensing and credits: see README.md, "Credits & provenance".
from __future__ import print_function  # use python 3 syntax but make it compatible with python 2
import os
import sys
import pygame
from PIL import Image

filepath = os.path.normpath(
    "/home/derek/Documents/Projects/chess-robot/version3/images/1.jpg"
)

pygame.init()
screen = pygame.display.set_mode((640, 480))
pygame.display.set_caption("Chess calibration")


def nudgecrop(im):
    nudcurrsplit = (195, 22, 517, 336)  # x1, y1, x2, y2; white on left
    nudinc = "0,0,0,0"
    while True:
        nudincsplit = nudinc.split(",")
        nudcurrsplit = (
            int(nudcurrsplit[0]) + int(nudincsplit[0]),
            int(nudcurrsplit[1]) + int(nudincsplit[1]),
            int(nudcurrsplit[2]) + int(nudincsplit[2]),
            int(nudcurrsplit[3]) + int(nudincsplit[3]),
        )
        cropped_image = im.crop(nudcurrsplit)
        cropped_image.save(filepath)
        print(nudcurrsplit)
        sys.stdout.flush()
        return  # remove this line and un-comment below to enable interactive nudging
        nudinc = input("x1, y1, x2, y2 OR q: ")
        if nudinc == "q":
            return


def drawredlines():
    with Image.open(filepath, "r") as im:
        # PIL images are not mutable after load() on some versions,
        # so we work on a copy and save it.
        img_copy = im.copy()
    pix = img_copy.load()
    squaresizex = int(round(float(img_copy.size[0]) / 8))
    squaresizey = int(round(float(img_copy.size[1]) / 8))

    # Draw vertical lines
    for col in range(0, img_copy.size[0], squaresizex):
        for row in range(img_copy.size[1]):
            pix[col, row] = (255, 0, 0)

    # Draw horizontal lines
    for row in range(0, img_copy.size[1], squaresizey):
        for col in range(img_copy.size[0]):
            pix[col, row] = (255, 0, 0)

    img_copy.save(filepath)


def calibratecamera():
    print("called calibratecamera")
    # BUG FIX: filepath must be an f-string/variable, not the literal word "filepath"
    os.system(
        f'fswebcam -d /dev/video0 -i 0 -r 640x480 -S 20 '
        f'--set "Power Line Frequency"="50 Hz" --list-controls --no-banner '
        f'--delay 2 --set brightness=50% --jpeg 99 --save "{filepath}"'
    )
    with Image.open(filepath, "r") as im:
        nudgecrop(im)
    drawredlines()


def displayImage():
    img = pygame.image.load(filepath)
    screen.blit(img, (0, 0))
    pygame.display.flip()


def main():
    # Capture and display once on startup
    calibratecamera()
    displayImage()

    # BUG FIX: use a proper event loop instead of mutual recursion.
    # Press R to recapture, Q or close the window to quit.
    print("Press R in the window to recapture. Close window or press Q to quit.")
    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_q:
                    running = False
                elif event.key == pygame.K_r:
                    calibratecamera()
                    displayImage()

    pygame.quit()


if __name__ == "__main__":
    main()
